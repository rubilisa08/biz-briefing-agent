# 뉴스레터 워크플로

`scripts/trace_run.py` 가 graph.py 의 그래프에서 자동으로 만든 그림이에요. 점선(`-.->`)은 기사 수만큼 펼쳐지는 엣지(Send)예요.

```mermaid
graph TD;
	__start__([<p>__start__</p>]):::first
	collect(collect)
	select(select)
	report(report)
	verify(verify)
	publish(publish)
	__end__([<p>__end__</p>]):::last
	__start__ --> collect;
	collect --> select;
	report --> verify;
	select -.-> report;
	select -.-> verify;
	verify --> publish;
	publish --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

| 노드 | 하는 일 |
|---|---|
| `collect` | ① 수집 — audience.yaml 의 경제 매체·한국은행 RSS 8곳에서 24시간 안 기사를 모으고 형식(포토·부고 등)을 거른다 |
| `select` | ② 선별 — 카테고리 키워드·수치 점수로 40건씩 예선 8건, 본선 3~5건 (매체·카테고리당 2건, 같은 사건 1건) |
| `report` | ③ 취재·요약 — 고른 기사 수만큼 펼쳐 본문 600자 관문을 보고 초안(요약·인사이트)을 만든다 |
| `verify` | ④ 검수 — 불합격 초안은 스킵하고 통과한 것만 verified 칸에 담는다 |
| `publish` | ⑤ 발행 — verified 칸을 카테고리 색 embed 카드로 Discord에 보낸다 (DRY_RUN=0 일 때만) |
