"""워크플로가 구조대로 도는지 파일로 확인한다 (OpenAI 호출 없음 · 무료).

    uv run python scripts/trace_run.py

만드는 파일 두 개:
  WORKFLOW.md                 그래프 구조 그림 — graph.py 에서 그대로 뽑아 늘 실제 코드와 같다
  logs/run-YYYYMMDD-HHMM.md   이번 실행 기록 — 노드가 어떤 순서로 돌았고, 어느 칸에 무엇을 남겼는지

.md 안의 ```mermaid 그림은 GitHub, VS Code 미리보기 등에서 도형으로 보인다.
"""

import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from graph import INIT, build

ROLE = {
    "collect": "① 수집 — audience.yaml 의 경제 매체·한국은행 RSS 8곳에서 24시간 안 기사를 모으고 형식(포토·부고 등)을 거른다",
    "select":  "② 선별 — 카테고리 키워드·수치 점수로 40건씩 예선 8건, 본선 3~5건 (매체·카테고리당 2건, 같은 사건 1건)",
    "report":  "③ 취재·요약 — 고른 기사 수만큼 펼쳐 본문 600자 관문을 보고 초안(요약·인사이트)을 만든다",
    "verify":  "④ 검수 — 불합격 초안은 스킵하고 통과한 것만 verified 칸에 담는다",
    "publish": "⑤ 발행 — verified 칸을 카테고리 색 embed 카드로 Discord에 보낸다 (DRY_RUN=0 일 때만)",
}


def mermaid(graph) -> str:
    text = graph.get_graph().draw_mermaid()
    if text.startswith("---"):                      # 맨 앞 설정 블록은 빼고 그림만 남긴다
        text = text.split("---", 2)[2]
    return text.strip()


def short(item) -> str:
    return f"[{item['source']}] {item.get('headline') or item['title']}"[:80]


def main() -> int:
    graph = build().compile()
    diagram = mermaid(graph)

    (ROOT / "WORKFLOW.md").write_text(
        "# 뉴스레터 워크플로\n\n"
        "`scripts/trace_run.py` 가 graph.py 의 그래프에서 자동으로 만든 그림이에요. "
        "점선(`-.->`)은 기사 수만큼 펼쳐지는 엣지(Send)예요.\n\n"
        f"```mermaid\n{diagram}\n```\n\n"
        "| 노드 | 하는 일 |\n|---|---|\n"
        + "".join(f"| `{n}` | {r} |\n" for n, r in ROLE.items()),
        encoding="utf-8")

    started = datetime.now()
    steps, state = [], {k: (list(v) if isinstance(v, list) else v) for k, v in INIT.items()}
    for update_by_node in graph.stream(INIT, stream_mode="updates"):
        for node, update in update_by_node.items():
            update = update or {}
            steps.append((node, update))
            for key, value in update.items():     # drafted·log 는 이어 붙이고 나머지는 덮어쓴다
                state[key] = state[key] + value if key in ("drafted", "log") else value

    lines = [f"# 실행 기록 · {started:%Y-%m-%d %H:%M}", "",
             "## 1. 노드가 돈 순서", "",
             "| # | 노드 | 바꾼 칸 | 로그 |", "|---|---|---|---|"]
    for i, (node, update) in enumerate(steps, 1):
        changed = ", ".join(f"{k} {len(v)}건" if isinstance(v, list) else k
                            for k, v in update.items() if k != "log") or "-"
        log = " / ".join(x.strip() for x in update.get("log", [])) or "-"
        lines.append(f"| {i} | `{node}` | {changed} | {log} |")

    order = [n for n, _ in steps]
    expected_head = ["collect", "select"]
    expected_tail = ["verify", "publish"]
    workers = order.count("report")
    ok = (order[:2] == expected_head and order[-2:] == expected_tail
          and order[2:-2] == ["report"] * workers and workers == len(state["picked"]))
    lines += ["", f"**구조 확인: {'✅ 설계대로' if ok else '❌ 설계와 다름'}** — "
              f"collect → select → report ×{workers} (고른 기사 {len(state['picked'])}건) → verify → publish", ""]

    by_source = Counter(it["source"] for it in state["collected"])
    lines += ["## 2. 칸마다 남은 것", "",
              f"### collected · {len(state['collected'])}건", "",
              ", ".join(f"{s} {n}" for s, n in by_source.most_common()) or "-", "",
              f"### picked · {len(state['picked'])}건", ""]
    lines += [f"- {short(it)}" for it in state["picked"]] or ["-"]
    lines += ["", f"### drafted · {len(state['drafted'])}건", ""]
    lines += [f"- {short(d)}  \n  {d['summary'][:120]}…" for d in state["drafted"]] or ["-"]
    kept = {d["url"] for d in state["verified"]}
    lines += ["", f"### verified · {len(state['verified'])}건", ""]
    lines += [f"- {'✅' if d['url'] in kept else '❌'} {short(d)}" for d in state["drafted"]] or ["-"]
    lines += ["", "## 3. 전체 로그", "", "```"] + state["log"] + ["```", "",
              "## 4. 그래프 구조", "", f"```mermaid\n{diagram}\n```", ""]

    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    out = log_dir / f"run-{started:%Y%m%d-%H%M}.md"
    out.write_text("\n".join(lines), encoding="utf-8")

    print("\n".join(state["log"]))
    print(f"\n구조 확인: {'설계대로' if ok else '설계와 다름'}")
    print(f"그래프 그림 → {ROOT / 'WORKFLOW.md'}")
    print(f"실행 기록   → {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
