# MCP 툴 유용성 평가 — 설계·진행 현황

작성: 2026-06-12  
상태: **완료** — WF-1~3 전체 완료, 판례 툴 개선 후 재평가까지 완료 (2026-06-28)

---

## 1. 목적

LH RAG MCP 서버(5개 도구)의 실효성을 **최종 답변 정확도** 기준으로 정량 검증한다.  
기존 `eval/golden_set.jsonl`(31건)은 검색 단계(Recall@k) 평가였고, 본 평가는 엔드투엔드 답변 품질을 측정한다.

- 모든 단계(질문 생성·답변·채점)는 Claude Code **Workflow** 서브에이전트로 수행 → API 추가 비용 없이 구독 사용량만 소모.
- **조건 A(베이스라인)**: MCP 없음, 웹검색 허용 — "MCP 없이 Claude를 쓰는 현실적 상황".
- **조건 B**: 조건 A + LH RAG MCP 툴 — MCP의 순기여분 측정.
- 채점: 3단계(정답/부분정답/오답), 조건 블라인드.

---

## 2. 평가 설계

### 카테고리별 정답 근거 확보

| 카테고리 | 원문 소스 | 생성 방식 |
|---|---|---|
| lh | `data/lh_regulation/markdown/` 69개 | 파일당 1문항 (파일 부족 시 다른 섹션 재활용) |
| kcsc | `data/kcsc/cache/` 1,822개 JSON | KDS/KCS/LHCS 층화 샘플, 파일당 1문항 |
| pps | `data/pps/cache/` 864건 JSON | 질의요지→자연어 질문, 회답→gold_answer |
| law | 로컬 없음 → MCP `search_law`로 조문 조회 | LH 핵심 법령 25개 × 4문항 |
| prec | MCP `search_precedents`로 요지 조회 | 시드 키워드 25개 × 4문항 |

**질문 요건**: LH 직원 실무 질문체 / 자립적(법령명·기준명 포함) / 사실형(수치·기간·요건·결론) / 정답 누설 금지 / source_quote로 gold_answer 검증 가능.

### 워크플로 파이프라인 (3단계 순차 실행)

```
WF-1 (생성+검증)  →  full_questions.jsonl  →  WF-2 (답변)  →  full_answers.jsonl  →  WF-3 (채점)  →  full_judgments.jsonl  →  집계·report
```

| 단계 | 스크립트 | 에이전트 수 | 소요시간 |
|---|---|---|---|
| WF-1 (생성+검증) | `eval-wf1-gen-full.js` | ~70개 | ~40분 |
| WF-1 보완 | `eval-wf1-fill.js`, `eval-wf1-fill2.js` | ~13개 | ~15분 |
| WF-2 (답변) | `eval-wf2-answers-full.js` (미작성) | ~100개 | ~90~120분 |
| WF-3 (채점) | `eval-wf3-judge-full.js` (미작성) | ~50개 | ~20분 |

---

## 3. 현재 진행 상황

### 완료

- **파일럿2 (5문항/카테고리 × 5 = 25문항)**: WF-1~3 전부 완료, 결과 `eval/llm_eval/pilot2_*.jsonl`
- **전체 WF-1**: 500문항 생성+검증 완료 → `eval/llm_eval/full_questions.jsonl`
  - lh 100 / kcsc 100 / pps 100 / law 100 / prec 100

### 파일럿2 결과 요약 (참고용)

| 카테고리 | 조건 A (웹만) | 조건 B (웹+MCP) | Δ |
|---|---|---|---|
| lh | 0% (0/5) | 100% (5/5) | +100pp |
| kcsc | 20% (1/5) | 80% (4/5) | +60pp |
| pps | 100% (5/5) | 100% (5/5) | 0 |
| law | 60% (3/5) | 100% (5/5) | +40pp |
| prec | 100% (5/5) | 100% (5/5) | 0 |
| **전체** | **56% (14/25)** | **96% (24/25)** | **+40pp** |

- **MCP 핵심 가치**: lh(+100pp) — 비공개 내부 문서는 웹으로 완전히 불가.
- **kcsc-03 이슈**: 유사 표준(KCS vs LHCS) 혼동으로 condB 오답 → 검색 정밀도 개선 과제.
- **pps·prec Δ=0**: 공개 DB와 차별화 없음 — 비공개·최신 사례 위주로 질문 재설계 시 효과 더 뚜렷해질 것.

### 전체 평가 완료 결과 (2026-06-28)

500문항 전체 WF-2·WF-3 완료 + 판례 툴 개선 후 prec 100문항 재평가까지 완료.

| 카테고리 | 조건 A (웹만) | 조건 B (웹+MCP) | Δ |
|---|---|---|---|
| lh (LH 규정) | 25% | 93% | +68pp |
| kcsc (건설기준) | 35% | 94% | +59pp |
| pps (조달청 해석사례) | 53% | 94% | +41pp |
| law (법령) | 90% | 96% | +6pp |
| prec (판례) | 72% | **93%** *(개선 후)* | +21pp |
| **전체** | **55%** | **94%** | **+39pp** |

- 판례 툴 개선(다중 키워드 병렬 검색 + DeepInfra 리랭커): 64% → 93% (+29pp)
- 전체 보고서: `docs/full_test_report.html`

---

## 4. 파일 목록

```
.claude/workflows/
  eval-wf1-gen.js         ← 파일럿 WF-1 (5/카테고리, 참고용)
  eval-wf1-gen-full.js    ← 전체 WF-1 (100/카테고리)
  eval-wf1-fill.js        ← WF-1 갭 보완 1차
  eval-wf1-fill2.js       ← WF-1 갭 보완 2차 (lh 전용)
  eval-wf2-answers.js     ← 파일럿 WF-2 (참고용)
  eval-wf3-judge.js       ← 파일럿/전체 공용 WF-3

eval/llm_eval/
  full_sample.json        ← seed=42 샘플 파일 목록 (lh/kcsc/pps/law_seeds/prec_seeds)
  fill_sample.json        ← 보완 샘플 (lh 15개, kcsc 14개, pps 17개, law 2개)
  fill2_sample.json       ← lh 추가 보완용 (6개)
  full_questions.jsonl    ← ★ 확정 500문항 (id/category/question/gold_answer/source_ref/source_quote)
  full_answers.jsonl      ← WF-2 결과 (미생성)
  full_judgments.jsonl    ← WF-3 결과 (미생성)
  full_report.md          ← 최종 보고서 (미생성)
  pilot2_questions.jsonl  ← 파일럿 25문항
  pilot2_answers.jsonl    ← 파일럿 답변 50건
  pilot2_judgments.jsonl  ← 파일럿 채점 50건
  pilot2_report.md        ← 파일럿 보고서

scripts/
  eval_sample_full.py     ← seed=42 샘플 목록 생성 스크립트
```

---

## 5. WF-2 실행 방법 (다음 세션)

### WF-2 스크립트 작성 시 주의사항

파일럿 WF-2(`eval-wf2-answers.js`) 구조를 그대로 재사용하되 경로·배치크기만 교체:

```js
const QUESTIONS_PATH = '.../eval/llm_eval/full_questions.jsonl'
const BATCH_SIZE = 10   // 문항/에이전트 (파일럿과 동일)
```

condA 프롬프트 규칙:
- `WebSearch`·`WebFetch`만 허용
- `data/`·`eval/` 디렉터리 파일 Read 금지
- ToolSearch·MCP 툴 호출 금지
- 웹에서 찾지 못하면 "웹 검색으로 확인 불가" 명시

condB 프롬프트 규칙:
- ToolSearch로 카테고리별 MCP 툴 로드 후 우선 사용
- MCP 결과로 충분하면 웹 검색 불필요
- `tools_used` 필드에 사용한 도구 목록 기록 (오염 검사용)

MCP 툴 힌트:
```
lh   → ToolSearch "search_lh_regulations"
kcsc → ToolSearch "search_construction_standards"
pps  → ToolSearch "search_procurement_interpretations"
law  → ToolSearch "search_law"
prec → ToolSearch "search_precedents"
```

**로컬 MCP 설정**: `.mcp.json`과 `.claude/settings.json`의 `enabledMcpjsonServers`가 이미 설정되어 있음 → 새 세션에서 자동 활성화. fly.io cold start(~20초) 없이 로컬 stdio 사용.

### WF-2 실행 예상 규모

- 배치 수: 500문항 / 10 = 50 condA 에이전트 + 50 condB 에이전트 = 100개
- 병렬 상한: 16개 동시 → 약 7라운드 × 평균 3분 ≈ **90~120분**
- stall 재시도: condB에서 MCP 타임아웃 시 자동 재시도 (파일럿에서 5회 발생)

### WF-3 실행 방법

파일럿 WF-3(`eval-wf3-judge.js`) 경로만 교체:

```js
const ANSWERS_PATH  = '.../eval/llm_eval/full_answers.jsonl'
const QUESTIONS_PATH = '.../eval/llm_eval/full_questions.jsonl'
const JUDGE_BATCH_SIZE = 20
```

1,000건 / 20 = 50 배치 에이전트 → 약 20분.

---

## 6. 집계 코드 (Python)

```python
import json
from collections import Counter

judgments = [json.loads(l) for l in open('eval/llm_eval/full_judgments.jsonl')]
cats = ['lh','kcsc','pps','law','prec']

for cond in ['A','B']:
    items = [j for j in judgments if j['condition'] == cond]
    total = len(items)
    correct = sum(1 for j in items if j['verdict'] == '정답')
    partial = sum(1 for j in items if j['verdict'] == '부분정답')
    wrong   = sum(1 for j in items if j['verdict'] == '오답')
    print(f"\n조건{cond}: 정답 {correct}/{total} ({100*correct/total:.1f}%)")
    for cat in cats:
        citems = [j for j in items if j['qid'].startswith(cat)]
        c = sum(1 for j in citems if j['verdict'] == '정답')
        print(f"  {cat}: {c}/{len(citems)} ({100*c/len(citems):.1f}%)")
```

---

## 7. 설계 결정 기록

| 결정 | 이유 |
|---|---|
| 조건 A = 웹검색 허용 | "MCP 없이 Claude를 쓰는" 현실적 베이스라인. 완전 무지 베이스라인은 평가 가치 낮음. |
| 채점 블라인드 (A→X, B→Y) | 채점자가 어느 조건인지 모르게 해 편향 방지. |
| source_quote 필수 기록 | 채점 기준이 되는 원문 인용 — 추측 라벨 방지 (1차 골든셋 실패 교훈). |
| 배치 크기 10문항/에이전트 | 5는 너무 작아 에이전트 수 증가, 20이상은 컨텍스트 과부하 위험. |
| law/prec 정답 근거를 MCP로 확보 | 공개된 원문이 없으므로 불가피. condB에 구조적으로 유리하나, "MCP가 근거 문서를 찾는가" 자체가 검증 대상이므로 평가 목적에 부합. |
| WF-1 생성+검증 1에이전트 | 파일럿에서 2단계(생성→검증)보다 1단계 병합이 더 빠르고 품질 동등 확인. |
| 전체 500문항 중 lh 69파일 제약 | lh markdown 파일이 69개뿐 → 100문항 채우려면 일부 파일 재사용(다른 섹션). |

---

## 8. 알려진 이슈

| 이슈 | 내용 | 대응 |
|---|---|---|
| kcsc 유사 표준 혼동 | KCS 61 20 20과 LHCS 11 50 15 05 유사 내용 혼동 → condB 오답 | 질문에 표준 코드 명시 또는 MCP 필터 개선 |
| pps·prec Δ≈0 | 공개 DB에서도 접근 가능 → MCP 차별화 낮음 | 최신·비공개 사례로 질문 재설계 시 효과 증가 예상 |
| WF-2 condB stall | MCP 툴 ToolSearch 로드 지연으로 에이전트 stall → 자동 재시도 | 로컬 MCP(.mcp.json) 전환으로 개선 예상 |
| lh 파일 부족 | 69개 파일 전부 소진 → 4문항은 기존 파일 다른 섹션에서 확보 | 향후 lh 파일 추가 크롤링 시 해소 |
