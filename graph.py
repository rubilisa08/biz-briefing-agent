"""비즈 브리핑 — 경제·기업 뉴스 5건을 골라 Discord로 보내는 LangGraph 에이전트.

collect → select → report(기사 수만큼 Send) → verify → publish
독자·카테고리·중요도 기준·제외 조건·소스는 audience.yaml 에서 읽는다.
실행마다 수집·선별 근거·검수 결과를 store/metrics.jsonl 에 한 줄씩 남긴다.
"""

import html
import json
import operator
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, TypedDict
from urllib.parse import urlsplit

import feedparser
import requests
import trafilatura
import yaml
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from pydantic import BaseModel, Field

# Windows에서 출력을 파일·파이프로 넘기면 cp949로 인코딩돼 이모지·'—'에서 멈춘다. 출력은 늘 UTF-8로.
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
try:                                            # 내 PC에서는 .env 의 웹훅 주소·키를 읽는다 (Actions 는 Secrets 로 받는다)
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass
AUD = yaml.safe_load((ROOT / "audience.yaml").read_text(encoding="utf-8"))
SEL = AUD["selection"]
# 레포에 남는 기록은 GitHub Actions 실행분만 — 내 PC에서 돌린 기록은 따로 두어 커밋이 엉키지 않게
METRICS = ROOT / "store" / ("metrics.jsonl" if os.environ.get("GITHUB_ACTIONS") == "true" else "metrics.local.jsonl")

class Brief(TypedDict):
    hours:      int                              # 수집 time window(시간)
    collected:  list                             # ① 수집한 기사
    stats:      dict                             # ① 소스별 건수 · 응답 없음 · 제외 건수
    picked:     list                             # ② 선별해 남긴 3~5건 (카테고리·점수·키워드 라벨 포함)
    runners_up: list                             # ② 본선에서 아깝게 떨어진 후보 — 선별 근거 기록용
    drafted:    Annotated[list, operator.add]    # ③ 취재한 초안 — 워커들이 나눠 채운다
    verified:   list                             # ④ 검수를 통과한 것
    checks:     list                             # ④ 초안마다 검수 판정
    published:  bool                             # ⑤ 실제로 보냈는지
    log:        Annotated[list, operator.add]    # 무슨 일이 있었는지

# ── ① 수집 ──
UA = {"User-Agent": "Mozilla/5.0 (biz-briefing-agent)"}
KST = timezone(timedelta(hours=9))
EXCLUDE = [re.compile(p) for p in AUD["exclude"]["title_patterns"]]

def strip_tags(s):
    s = html.unescape(s or "")                             # 한국은행처럼 태그를 &lt;p&gt; 로 한 번 더 감싼 피드가 있다
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))          # 문단 태그 자리에 띄어쓰기, &quot; 같은 기호는 글자로
    s = re.sub(r"([.!?])(?=[가-힣A-Z\"'“‘])", r"\1 ", s)   # "앞섰다.중국"처럼 붙은 문장 사이 (3.8 같은 숫자는 그대로)
    return re.sub(r"\s+", " ", s).strip()

BYLINE = re.compile(
    r"^\s*(?:[\[(][^\])]{1,20}=[^\])]{1,15}[\])]\s*)?"                                  # [서울=뉴시스] · (서울=연합뉴스)
    r"(?:(?:[가-힣]{1,4}\s+){0,3}[가-힣]{1,4}\s*(?:기자|특파원|선임기자|객원기자)\s*=\s*)?"  # 홍길동 기자 = · 정회인 곽윤아 기자 =
    r"(?:\[[^\]]{1,25}(?:기자|특파원)\]\s*)?")                                           # [아시아경제 이민지 기자]
BOILERPLATE = re.compile(r"\[이 기사에 나온[^\]]*\]")                                     # 머니투데이 데이터랩 안내문

def drop_byline(text):                         # 요약 첫머리의 매체·기자 표기와 안내문은 독자에게 정보가 아니다
    return BOILERPLATE.sub("", BYLINE.sub("", text, count=1)).strip()

def published_at(entry, tz=timezone.utc):
    t = entry.get("published_parsed") or entry.get("updated_parsed")   # Atom은 updated만 줄 때가 있다
    return datetime(*t[:6], tzinfo=tz) if t else None

def url_key(link):                             # utm_ 같은 추적용 꼬리표만 떼고 비교 (기사 번호 쿼리는 남긴다)
    parts = urlsplit(link)
    query = "&".join(q for q in parts.query.split("&") if q and not q.startswith("utm_"))
    return f"{parts.netloc}{parts.path.rstrip('/')}?{query}"

def collect(s: dict) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=s["hours"])
    items, dead, seen, excluded = [], [], set(), 0
    for src in AUD["sources"]:
        name = src["name"]
        try:
            r = requests.get(src["url"], headers=UA, timeout=20)
            r.raise_for_status()                # 403·500 오류 페이지를 '0건'이 아니라 '응답 없음'으로 센다
            feed = feedparser.parse(r.content)
        except Exception:
            dead.append(name)                   # 한 곳이 죽어도 나머지는 계속
            continue
        for e in feed.entries:
            if not e.get("link") or not e.get("title"):   # 링크나 제목이 빠진 글 하나로 전체가 멈추지 않게
                continue
            at = published_at(e, KST if src.get("naive_kst") else timezone.utc)
            if not at or at < cutoff:           # time window 밖이거나 날짜가 없으면 버린다
                continue
            if any(p.search(e.title) for p in EXCLUDE):   # 포토·부고·인사·사설 같은 형식은 버린다
                excluded += 1
                continue
            key = url_key(e.link)
            if key in seen:
                continue
            seen.add(key)
            text = drop_byline(strip_tags(e.get("summary", "")))
            item = {"title": strip_tags(e.title), "url": e.link, "source": name, "at": at,
                    "summary": text[:300]}
            if src.get("feed_as_body"):         # 본문 추출이 안 되는 1차 기관 — 피드 글을 본문으로 쓴다
                item["feed_body"] = text[:6000]
                item["min_body"] = src.get("min_body", MIN_BODY)
            items.append(item)
    by_source = dict(Counter(it["source"] for it in items))
    return {"collected": items,
            "stats": {"by_source": by_source, "dead": dead, "excluded": excluded},
            "log": [f"① 수집   {s['hours']}시간 time window · {len(items)}건 · 형식 제외 {excluded}건"
                    + (f" · 응답 없음 {dead}" if dead else "")]}

# ── ② 선별 ──
class Pick(BaseModel):                          # 구조화 출력 스키마 — LLM으로 바꿔도 같은 모양을 받는다
    index: int = Field(description="후보 목록에서의 번호")
    reason: str = Field(description="왜 골랐는지 한 문장")
    event: str = Field(description="이 기사가 속한 카테고리")

class Shortlist(BaseModel):
    picks: list[Pick]

def _kw_pattern(kw):
    if kw.isascii():                            # AI·CPI·Fed 같은 짧은 영문은 단어 경계로 (예: 'AI'가 'SAID'에 걸리지 않게)
        return re.compile(rf"(?<![A-Za-z]){re.escape(kw)}(?![A-Za-z])", re.IGNORECASE)
    return re.compile(re.escape(kw))

# 긴 키워드부터 — '소비자물가'가 걸리면 그 안의 '물가'·'소비'는 다시 세지 않는다 (classify)
CATS = [{**c, "patterns": [(kw, _kw_pattern(kw)) for kw in sorted(c["keywords"], key=len, reverse=True)]}
        for c in AUD["categories"]]
NUMBER = re.compile(AUD["signals"]["number_pattern"])
PENALTY = [re.compile(p) for p in AUD["exclude"]["penalty_patterns"]]

def classify(item):
    """카테고리·점수·근거 키워드를 매긴다. 제목에서 걸린 키워드 2점, 요약에서 1점."""
    best = {"category": "기타", "score": 0, "keywords": []}
    for c in CATS:
        score, hits = 0, []
        for kw, pat in c["patterns"]:
            if any(kw in h for h in hits):      # 이미 센 더 긴 키워드 안에 들어 있으면 건너뛴다
                continue
            if pat.search(item["title"]):
                score, hits = score + 2, hits + [kw]
            elif pat.search(item["summary"]):
                score, hits = score + 1, hits + [kw]
        if score > best["score"]:
            best = {"category": c["name"], "score": score, "keywords": hits}
    text = f"{item['title']} {item['summary']}"
    has_number = bool(NUMBER.search(text))
    promo = any(p.search(text) for p in PENALTY)
    best["score"] += (AUD["signals"]["number_bonus"] if has_number else 0) - (AUD["exclude"]["penalty"] if promo else 0)
    best["has_number"], best["promo"] = has_number, promo
    return best

def reason_of(c):
    parts = [c["category"], f"{c['score']}점", "키워드 " + "·".join(c["keywords"][:4]) if c["keywords"] else "키워드 없음"]
    if c["has_number"]:
        parts.append("수치 포함")
    if c["promo"]:
        parts.append("홍보성 감점")
    return " · ".join(parts)

# 실적·지표 기사라면 어디에나 나오는 말 — 이것만 겹쳐서는 같은 사건이 아니다
# (예: '대한유화, 3분기 영업이익 작년 동기 대비…' ↔ '금호타이어, 3분기 영업이익 작년 동기 대비…')
COMMON = {"속보", "단독", "종합", "분기", "3분기", "영업이익", "영업익", "순이익", "매출", "작년", "전년", "동기", "대비",
          "증가", "감소", "상승", "하락", "흑자", "적자", "흑자전환", "적자전환", "기록", "발표", "전망", "기준금리",
          "금리", "동결", "인상", "인하", "환율", "원달러", "달러", "실적", "역대", "최대", "최고", "최저", "올해", "내년"}

def title_words(title):                        # 같은 사건 판단용 — 숫자와 흔한 말을 뺀 두 글자 이상 낱말
    words = re.findall(r"[가-힣A-Za-z0-9]{2,}", title)
    return {w for w in words if not re.search(r"\d", w) and w not in COMMON}

def same_story(a, b):                          # 고유한 낱말(회사·기관 이름 등)이 40% 이상 겹치면 같은 사건으로 본다
    wa, wb = title_words(a), title_words(b)    # 예: 'OCI홀딩스, 3분기 영업익…흑자전환' ↔ 'OCI홀딩스, 매출 1조 돌파에 흑자전환'
    return bool(wa & wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.4

# 무료 버전 — LLM 대신 키워드 점수로 고른다. Gemini로 바꿀 땐 ask_picks 만 갈아 끼운다 (같은 Pick 모양).
def ask_picks(items, n):
    final = n <= SEL["target"]                  # 본선에서만 매체·카테고리 칸 수·최저 점수·같은 사건을 따진다
    labels = [classify(it) for it in items]
    ranked = sorted(range(len(items)), key=lambda i: (labels[i]["score"], items[i]["at"]), reverse=True)
    picks, per_source, per_cat = [], Counter(), Counter()
    for i in ranked:
        if len(picks) == n:
            break
        c = labels[i]
        if final and (c["score"] < SEL["min_score"] or c["category"] == "기타"
                      or len(items[i]["summary"]) < MIN_SUMMARY   # 요약이 비면 검수에서 떨어질 칸 — 본선에서 미리 뺀다
                      or per_source[items[i]["source"]] >= SEL["per_source"]
                      or per_cat[c["category"]] >= SEL["per_category"]
                      or any(same_story(items[i]["title"], items[p.index]["title"]) for p in picks)):
            continue
        per_source[items[i]["source"]] += 1
        per_cat[c["category"]] += 1
        picks.append(Pick(index=i, reason=reason_of(c), event=c["category"]))
    return [p for p in picks if 0 <= p.index < len(items)]      # 없는 번호는 버린다

def select(s: dict) -> dict:                   # 예선(묶음마다 8건) → 본선(한 화면에서 3~5건)
    items = s["collected"]
    survivors = []
    for i in range(0, len(items), SEL["batch"]):
        chunk = items[i:i + SEL["batch"]]
        survivors += [chunk[p.index] for p in ask_picks(chunk, SEL["prelim_per_batch"])]
    finals = ask_picks(survivors, SEL["target"])
    picked = [{**survivors[p.index], **classify(survivors[p.index]), "reason": p.reason} for p in finals]
    chosen = {p.index for p in finals}
    runners = sorted((i for i in range(len(survivors)) if i not in chosen
                      and not any(same_story(survivors[i]["title"], p["title"]) for p in picked)),
                     key=lambda i: classify(survivors[i])["score"], reverse=True)[:3]
    runners_up = [{**survivors[i], **classify(survivors[i])} for i in runners]
    return {"picked": picked, "runners_up": runners_up,
            "log": [f"② 선별   {len(items)} → 예선 {len(survivors)} → {len(picked)}건"]
                   + [f"   ✔ [{p['category']}] {p['score']}점 · {p['source']} · {p['title'][:40]}" for p in picked]}

# ── ③ 취재·요약 ──
MIN_BODY = 600                                  # 요약을 쓰려면 최소 이 정도는 있어야 한다 (G1)
QUESTION = {c["name"]: c["question"] for c in AUD["categories"]}

class ReportIn(TypedDict):                      # 워커가 받는 것은 기사 하나뿐
    item: dict

def extract_body(url):
    d = trafilatura.fetch_url(url)
    return trafilatura.extract(d) if d else None

# 무료 버전 — LLM 없이 피드 요약을 그대로 쓰고, 인사이트 칸에는 카테고리의 분석 질문과 근거 키워드를 단다.
# 본문은 G1 관문과 검수용으로만 쓰고 발행하지 않는다 (제목·요약·원문 링크만 재배포).
# Gemini로 바꿀 땐 draft 만 갈아 끼워 audience.yaml 의 핵심 질문 네 가지에 답하게 한다.
def draft(item, body):
    kws = "·".join(item["keywords"][:4])
    return {"headline": item["title"],
            "summary":  item["summary"],
            "why":      f"{item['category']} — {QUESTION.get(item['category'], '')}"
                        + (f" (근거: {kws})" if kws else "")}

def fan_report(s: dict):                        # 기사 수만큼 워커를 펼친다
    if not s["picked"]:                         # 고른 게 없으면 바로 검수로 — 조용한 날 카드라도 보내려고
        return "verify"
    return [Send("report", {"item": it}) for it in s["picked"]]

def report(s: ReportIn) -> dict:
    it = s["item"]
    try:
        body = extract_body(it["url"])
    except Exception:
        body = None
    need = it.get("min_body", MIN_BODY)         # 1차 기관은 audience.yaml 에서 기준을 따로 정한다
    if (not body or len(body) < MIN_BODY) and len(it.get("feed_body", "")) >= need:
        body = it["feed_body"]                  # 한국은행처럼 본문 추출은 안 되지만 피드가 핵심 문장을 주는 1차 소스
    if not body or len(body) < need:
        return {"drafted": [],
                "log": [f"   취재 제외 {it['source']} · 본문 {len(body or '')}자"]}
    return {"drafted": [{**it, "body": body[:6000], **draft(it, body)}],
            "log": [f"   취재 {it['source']} · 본문 {len(body)}자"]}

# ── ④ 검수 ──
class Verdict(BaseModel):
    ok:       bool      = Field(description="요약이 원문에 근거하면 true")
    problems: list[str] = Field(description="근거 없는 부분. 없으면 빈 목록")

# 무료 버전 — 발행 요약이 매체가 쓴 피드 요약 그대로라 환각은 생기지 않는다. 대신 형식을 본다.
# Gemini로 바꾸면 check 를 원문 대조(환각 검수)로 갈아 끼우고, 불합격이면 한 번 다시 쓰고 그래도 안 되면 버린다.
MIN_SUMMARY = 40

def check(d):
    problems = []
    summary = d["summary"].strip()
    if len(summary) < MIN_SUMMARY:
        problems.append(f"요약이 비었거나 너무 짧음 ({len(summary)}자)")
    if summary == d["headline"].strip():
        problems.append("요약이 제목을 되풀이함")
    if not d["url"].startswith(("http://", "https://")):
        problems.append("원문 링크가 올바르지 않음")
    # 숫자 글자 대조는 넣지 않는다 — '6.2조'와 '6조2000억'처럼 같은 값을 다르게 써서 정확한 기사도 떨어뜨린다 (27장 오탐)
    return Verdict(ok=not problems, problems=problems)

def verify(s: dict) -> dict:                   # 불합격은 스킵 — 버리고 로그·기록에 남긴다
    kept, checks = [], []
    for d in s["drafted"]:
        v = check(d)
        checks.append({"title": d["title"], "source": d["source"], "ok": v.ok, "problems": v.problems})
        if v.ok:
            kept.append(d)
    dropped = [c for c in checks if not c["ok"]]
    return {"verified": kept, "checks": checks,
            "log": [f"④ 검수   {len(s['drafted'])} → {len(kept)}건"
                    + (f" · 불합격 {[c['source'] for c in dropped]}" if dropped else "")]}

# ── ⑤ 발행 ──
COLORS = {c["name"]: c["color"] for c in AUD["categories"]}
DEFAULT = 0x5F7476
TITLE_MAX, DESC_MAX, EMBED_MAX, TOTAL_MAX = 256, 4096, 10, 5800   # 6000에서 여유를 둔다

def build_embeds(run_id, lead, articles):
    name = AUD["newsletter"]["name"]
    if not articles:                                   # 조용한 날에도 한 장은 보낸다
        return [{"title": f"📊 {run_id} · {name}", "color": DEFAULT,
                 "description": "오늘은 기준을 넘는 소식이 없습니다."}]
    embeds = [{"title": f"📊 {run_id} · {name}", "description": lead, "color": DEFAULT}]
    for i, a in enumerate(articles, 1):
        desc = a["summary"]
        if a.get("why"):
            desc += f"\n\n💡 **{a['why']}**"
        embeds.append({
            "title":       f"{i}. [{a['topic']}] {a['headline']}"[:TITLE_MAX],
            "description": desc[:DESC_MAX],
            "url":         a["url"],
            "color":       COLORS.get(a.get("topic", ""), DEFAULT),
            "footer":      {"text": f"{a['source']} · {a['when']}"},
        })
    total = lambda es: sum(len(e.get("title", "")) + len(e.get("description", ""))
                           + len(e.get("footer", {}).get("text", "")) for e in es)
    while len(embeds) > EMBED_MAX or total(embeds) > TOTAL_MAX:
        embeds.pop()                                   # 뒤에서부터 덜어낸다
    return embeds

def send(run_id, lead, articles, webhook=None, dry_run=True):
    payload = {"username": "비즈 브리핑", "embeds": build_embeds(run_id, lead, articles)}
    if dry_run or not webhook:
        print(f"[dry-run] embed {len(payload['embeds'])}개 · "
              f"{len(json.dumps(payload, ensure_ascii=False))}자 — 보내지 않음")
        print(json.dumps(payload["embeds"][-1], ensure_ascii=False, indent=2)[:400])
        return False
    r = requests.post(webhook, json=payload, timeout=20)
    ok = r.status_code in (200, 204)
    print("발행:", "성공" if ok else f"실패 {r.status_code} {r.text[:120]}")
    return ok

def make_lead(arts):
    if not arts:
        return ""
    cats = Counter(a["topic"] for a in arts)
    return (f"{AUD['audience']['persona']}를 위한 오늘의 {len(arts)}건입니다.\n"
            + " · ".join(f"{c} {n}" for c, n in cats.items()))

def publish(s: dict) -> dict:
    arts = [{"headline": a["headline"], "summary": a["summary"], "why": a["why"],
             "url": a["url"], "source": a["source"], "topic": a["category"],
             "when": a["at"].astimezone(KST).strftime("%m-%d %H:%M")}   # 독자 기준 한국 시각으로
            for a in s["verified"]]
    today   = datetime.now(KST).strftime("%Y-%m-%d")
    dry_run = os.environ.get("DRY_RUN", "1") != "0"   # "0"일 때만 보낸다 — 그 밖의 값은 모두 보내지 않음
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not dry_run and not webhook:                    # 보내라고 했는데 주소가 없으면 실행을 실패로 끝낸다
        raise RuntimeError("DRY_RUN=0 인데 DISCORD_WEBHOOK_URL 이 비어 있습니다")
    sent = send(today, make_lead(arts), arts, webhook=webhook, dry_run=dry_run)
    if not dry_run and not sent:                       # Discord가 거절하면(4xx·5xx) 초록불로 끝나지 않게 한다
        raise RuntimeError("Discord 발행 실패 — 위 '발행: 실패' 줄의 상태 코드를 확인하세요")
    label = f"{len(arts)}건" if arts else "조용합니다"
    return {"published": sent, "log": [f"⑤ 발행   {label} · {'보냄' if sent else 'dry-run'}"]}

# ── 그래프 ──
def build():                                    # 노드를 이름으로 찾아 붙인다
    g = StateGraph(Brief)
    for name in ("collect", "select", "report", "verify", "publish"):
        g.add_node(name, globals()[name])
    g.add_edge(START, "collect")
    g.add_edge("collect", "select")
    g.add_conditional_edges("select", fan_report, ["report", "verify"])   # 기사 수만큼 report를 펼친다
    g.add_edge("report", "verify")
    g.add_edge("verify", "publish")
    g.add_edge("publish", END)
    return g

INIT = {"hours": 24, "collected": [], "stats": {}, "picked": [], "runners_up": [],
        "drafted": [], "verified": [], "checks": [], "published": False, "log": []}

def _brief(it):                                 # 기록용 — 날짜·본문을 빼고 JSON에 담을 칸만
    return {k: it[k] for k in ("title", "source", "url", "category", "score", "keywords", "reason") if k in it}

def write_metrics(out):
    """실행 한 번을 store/metrics.jsonl 에 한 줄로 남긴다 — 선별 기준이 의도대로 동작했는지의 근거."""
    drafted = {d["url"] for d in out["drafted"]}
    record = {
        "run_at":     datetime.now(KST).isoformat(timespec="seconds"),
        "dry_run":    os.environ.get("DRY_RUN", "1") != "0",
        "published":  out["published"],
        "collected":  len(out["collected"]),
        **out["stats"],
        "picked":     [_brief(p) for p in out["picked"]],
        "runners_up": [_brief(p) for p in out["runners_up"]],
        "report_excluded": [_brief(p) for p in out["picked"] if p["url"] not in drafted],
        "checks":     out["checks"],
        "verified":   len(out["verified"]),
        "log":        out["log"],
    }
    METRICS.parent.mkdir(exist_ok=True)
    with METRICS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

def run():
    try:
        out = build().compile().invoke(INIT)
    except Exception as ex:                     # 발행 실패 등으로 멈춘 실행도 기록에 남긴다
        METRICS.parent.mkdir(exist_ok=True)
        with METRICS.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"run_at": datetime.now(KST).isoformat(timespec="seconds"),
                                "dry_run": os.environ.get("DRY_RUN", "1") != "0", "published": False,
                                "error": f"{type(ex).__name__}: {ex}"}, ensure_ascii=False) + "\n")
        raise
    write_metrics(out)
    return out

if __name__ == "__main__":
    for line in run()["log"]:
        print(line)
