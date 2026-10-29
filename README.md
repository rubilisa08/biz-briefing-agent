# 비즈 브리핑 — 경제·기업 뉴스 에이전트

기획·재무 의사결정에 필요한 경제·기업 뉴스 **5건**을 매일 아침 골라 Discord로 보내는 LangGraph 에이전트예요.
독자는 경제·경영에 관심 있는 취업준비생, 주니어 사업기획·경영기획·재무기획(FP&A) 담당자예요.

설계 판단과 측정 근거는 [REPORT.md](REPORT.md)에 있어요.

## 어떻게 동작하나요

```mermaid
graph LR
  collect[① 수집] --> select[② 선별]
  select -. 기사 수만큼 .-> report[③ 취재·요약]
  report --> verify[④ 검수] --> publish[⑤ 발행]
```

| 노드 | 하는 일 |
|---|---|
| ① `collect` | 경제 매체·한국은행 RSS 8곳에서 최근 24시간 기사를 모으고 [포토]·[부고]·[사설] 같은 형식을 걸러요 (하루 약 550건) |
| ② `select` | 경제 지표·기업 실적·산업·경쟁·경영 의사결정 네 카테고리 키워드와 수치 신호로 점수를 매겨 40건씩 예선 8건, 본선 3~5건을 골라요 |
| ③ `report` | 고른 기사 수만큼 펼쳐(`Send`) 원문 본문이 600자 이상인지 확인하고 요약·인사이트 초안을 만들어요 |
| ④ `verify` | 초안을 검수해 불합격은 건너뛰고 통과한 것만 발행해요 |
| ⑤ `publish` | 카테고리별 색깔 embed 카드로 Discord에 보내요. `DRY_RUN=0`일 때만 실제로 보내요 |

## 파일 구조

```
graph.py                    LangGraph 워크플로 (State · 노드 다섯 개 · build() · run())
run.py                      한 번 실행 (GitHub Actions 진입점)
audience.yaml               타깃 독자 · 카테고리 · 중요도 기준 · 제외 조건 · 소스
requirements.txt            의존성
store/metrics.jsonl         실행마다 수집·선별 근거·검수 결과 기록
store/source_probe.json     소스 후보 측정값 (scripts/probe_sources.py)
REPORT.md                   프로젝트 수행 결과 보고서
.github/workflows/daily.yml 매일 22:30 UTC(= 07:30 KST) 실행 + 손으로 실행
scripts/trace_run.py        한 번 돌려 노드 순서를 확인하고 WORKFLOW.md 를 갱신
```

## 직접 돌려 보기

```bash
uv sync                          # 또는 pip install -r requirements.txt
uv run python run.py --dry-run   # 보내지 않고 로그만
uv run python scripts/probe_sources.py
uv run python scripts/trace_run.py
```

## 매일 자동 실행 (GitHub Actions)

1. **Settings → Secrets and variables → Actions**에 Discord 웹훅 주소를 `WEBHOOK_URL`이라는 이름으로 등록해요.
2. 매일 22:30 UTC에 실행돼요. GitHub 예약 실행은 몇 분~몇 시간 늦게 시작할 수 있어요.
3. **Actions → 매일 비즈 브리핑 → Run workflow**로 손으로도 돌릴 수 있어요. "발행하지 않고 로그만 남기기"를 체크하면 보내지 않아요.
4. 실행이 끝나면 `store/metrics.jsonl`에 기록이 커밋돼요.

웹훅 주소는 비밀번호처럼 다루고 코드나 커밋에 넣지 않아요 (`.env`는 `.gitignore`에 있어요).

## 현재 한계

지금은 무료판이라 요약은 매체가 피드에 준 첫 문단, 인사이트 칸은 카테고리 분석 질문과 근거 키워드예요.
Gemini API를 붙여 요약·인사이트·환각 검수를 LLM으로 바꿀 예정이에요 (`ask_picks`, `draft`, `check` 세 함수만 교체).

제목·요약·원문 링크만 사용하고 기사 본문은 다시 배포하지 않아요.
