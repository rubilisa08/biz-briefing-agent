"""소스 후보를 실제로 요청해 채택표에 쓸 숫자를 잰다 — REPORT.md '소스 채택표'의 근거.

    uv run python scripts/probe_sources.py

잰 값은 store/source_probe.json 에 저장한다.
  http      응답 코드 (200이 아니면 G3 접근 탈락)
  entries   피드가 한 번에 주는 건수
  last24h   최근 24시간 안 건수 (0이면 G2 생존 탈락에 가깝다)
  summary   첫 글 요약 길이 (피드가 주는 재료의 양)
  newest_h  최신 글이 몇 시간 전인지 (음수면 시간대 없는 날짜)
  body_ok   최근 글 3건 중 본문 600자 이상 뽑힌 건수 (G1 본문)
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
import trafilatura

ROOT = Path(__file__).resolve().parent.parent
UA = {"User-Agent": "Mozilla/5.0 (biz-briefing-agent)"}
CANDIDATES = [
    ("연합뉴스 경제",   "https://www.yna.co.kr/rss/economy.xml"),
    ("연합뉴스 산업",   "https://www.yna.co.kr/rss/industry.xml"),
    ("한국경제 경제",   "https://www.hankyung.com/feed/economy"),
    ("한국경제 금융",   "https://www.hankyung.com/feed/finance"),
    ("한국경제 산업",   "https://www.hankyung.com/feed/industry"),
    ("매일경제 경제",   "https://www.mk.co.kr/rss/30100041/"),
    ("매일경제 기업",   "https://www.mk.co.kr/rss/50100032/"),
    ("조선비즈",       "https://biz.chosun.com/arc/outboundfeeds/rss/?outputType=xml"),
    ("이데일리",       "http://rss.edaily.co.kr/edaily_news.xml"),
    ("머니투데이",     "https://rss.mt.co.kr/mt_news.xml"),
    ("아시아경제",     "https://www.asiae.co.kr/rss/all.htm"),
    ("뉴시스 경제",    "https://newsis.com/RSS/economy.xml"),
    ("한국은행 보도자료", "https://www.bok.or.kr/portal/bbs/B0000501/news.rss?menuNo=201264"),
    ("정책브리핑 기재부", "https://www.korea.kr/rss/dept_moef.xml"),
    ("CNBC Economy",  "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
]
MIN_BODY = 600


def strip(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def probe(name, url):
    row = {"source": name, "url": url, "http": None, "entries": 0, "last24h": 0,
           "summary": 0, "newest_h": None, "body_ok": 0, "body_tried": 0, "error": ""}
    try:
        r = requests.get(url, headers=UA, timeout=20)
        row["http"] = r.status_code
        r.raise_for_status()
        f = feedparser.parse(r.content)
    except Exception as ex:
        row["error"] = type(ex).__name__
        return row
    now = datetime.now(timezone.utc)
    ages = []
    for e in f.entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        if t:
            ages.append((now - datetime(*t[:6], tzinfo=timezone.utc)).total_seconds() / 3600)
    row["entries"] = len(f.entries)
    row["last24h"] = sum(1 for a in ages if -12 <= a <= 24)   # 시간대 없는 날짜(KST) 오차를 감안
    row["newest_h"] = round(min(ages), 1) if ages else None
    if f.entries:
        row["summary"] = len(strip(f.entries[0].get("summary", "")))
    for e in f.entries[:3]:
        if not e.get("link"):
            continue
        row["body_tried"] += 1
        try:
            d = trafilatura.fetch_url(e.link)
            body = trafilatura.extract(d) if d else None
        except Exception:
            body = None
        if body and len(body) >= MIN_BODY:
            row["body_ok"] += 1
    return row


def main() -> int:
    rows = [probe(n, u) for n, u in CANDIDATES]
    out = ROOT / "store" / "source_probe.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"measured_at": datetime.now().isoformat(timespec="minutes"),
                               "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'소스':<14}{'HTTP':>5}{'건수':>5}{'24h':>5}{'요약':>6}{'최신h':>7}{'본문':>6}  오류")
    for r in rows:
        print(f"{r['source']:<14}{str(r['http']):>5}{r['entries']:>5}{r['last24h']:>5}{r['summary']:>6}"
              f"{str(r['newest_h']):>7}{r['body_ok']:>3}/{r['body_tried']}  {r['error']}")
    print(f"\n→ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
