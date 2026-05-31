# 검색 정확도 분석 및 개선 방안

작성: 2026-05-31

---

## 1. 현재 아키텍처 요약

| 소스 | 방식 |
|---|---|
| LH 규정 | BM25(키워드, kiwipiepy) + Dense(Qwen3-Embedding-0.6B, DeepInfra) → RRF(k=60) |
| KCSC 건설기준 | 위와 동일 + 인용그래프 1-hop 확장 |
| 조달청 해석사례 | 위와 동일 |
| 법령/판례 | 법제처 외부 API (평가 범위 외) |

**공통 파라미터**: 후보 20개(BM25·Dense 각각) → RRF 상위 5~7개 반환. 점수 임계값 없음. Reranker 없음.

---

## 2. 평가 체계 (신설)

### 파일 구조

```
eval/
  golden_set.jsonl        # 31개 질의 + 정답 문서키 (수동 라벨)
  results/                # eval_retrieval.py 실행 결과 (.gitignore)
scripts/
  eval_common.py          # BM25/Dense/RRF 프리미티브 직접 호출, 문서키 매핑
  eval_collect.py         # 넓게 검색해 후보 덤프 (라벨링 보조)
  eval_retrieval.py       # Recall@k / MRR / nDCG 계산
```

### 문서(조문) 안정키

청크 ID(`{base}__c{idx:04d}`)는 재인덱싱 시 불안정하므로 평가는 **문서키**로 한다.

| 소스 | 문서키 |
|---|---|
| lh | `metadata["title"]` (규정명, 예: `여비규정`) |
| kcsc | `metadata["node_id"]` (예: `KCS:142010:3.4`) |
| pps | `metadata["id"]` (법령해석일련번호, 예: `448634`) |

### 실행

```bash
# 새 질의 후보 검토 (라벨링 전)
python scripts/eval_collect.py --source kcsc --query "거푸집 존치기간" --keywords "거푸집 동바리 존치기간" --top 20

# 현재 baseline 측정
python scripts/eval_retrieval.py --mode hybrid --ks 5 10

# 개선 전후 비교
python scripts/eval_retrieval.py --mode bm25
python scripts/eval_retrieval.py --mode dense
```

---

## 3. Baseline 지표 (2026-05-31, n=31)

골든셋: LH 16개, KCSC 7개, PPS 8개.
라벨 방법: eval_collect.py로 실제 후보를 top-20 뽑아 사람이 검토 → 반드시 있어야 할 문서키 선별.

| mode | recall@5 | recall@10 | MRR | nDCG@10 |
|---|---|---|---|---|
| **hybrid** | **0.839** | **0.968** | **0.771** | **0.799** |
| bm25 | 0.823 | 0.903 | 0.745 | 0.754 |
| dense | 0.790 | 0.839 | 0.738 | 0.738 |

### 소스별 (hybrid 기준)

| 소스 | recall@5 | recall@10 | MRR | nDCG@10 |
|---|---|---|---|---|
| lh | **1.000** | **1.000** | **0.969** | **0.973** |
| pps | 0.750 | 0.938 | 0.708 | 0.687 |
| kcsc | 0.571 | 0.929 | 0.391 | 0.531 |

---

## 4. 주요 발견

### 4-1. KCSC — 리랭커 도입 효과가 가장 클 소스

- **R@10=0.93이지만 MRR=0.39**: 관련 조문이 10위 안에는 들지만 상위 5위 밖으로 밀려남.
- BM25 단독에선 R@5=0.79·MRR=0.79로 오히려 높음 → RRF가 조문 순위를 오히려 밀어내는 경우 발생.
- Dense 단독이 KCSC에서 가장 약함(R@5=0.36): KCSC 본문은 코드·조문번호 중심이라 의미 임베딩이 불리.
- **결론**: 후보를 넓게 뽑은 뒤 cross-encoder로 재정렬하면 MRR이 크게 오를 것.

### 4-2. LH — BM25만으로 이미 완벽

- 세 모드 모두 R@5=1.00, MRR도 hybrid 0.97로 최고.
- 규정명이 쿼리에 그대로 등장하는 경우가 많아 BM25의 어휘 매칭이 충분히 강함.
- Dense가 MRR을 약간 올려주나(bm25 0.84 → hybrid 0.97) 실용적 차이는 작음.

### 4-3. PPS — Dense가 BM25보다 훨씬 우수

- BM25 R@5=0.50 vs Dense R@5=0.75.
- 조달청 해석사례는 안건명·질의요지·회답·이유가 긴 산문으로 돼 있어 키워드 매칭보다 의미검색이 유리.
- hybrid가 두 방식의 장점을 결합해 R@5=0.75·R@10=0.94 달성.

### 4-4. 하이브리드(RRF)의 기여

- 전 소스 평균: hybrid > bm25 > dense (nDCG 0.800 > 0.754 > 0.738).
- R@10에서 특히 두드러짐(0.968 vs 0.903 vs 0.839) — 상위 10위 안에 정답을 포함시키는 능력이 탁월.
- 단, KCSC에서는 MRR 기준으로 BM25(0.786)가 hybrid(0.391)보다 높아, RRF가 오히려 순위를 낮추는 사례가 있음.

### 4-5. 라벨링 주의사항

첫 시도에서 추측 라벨로 전체 지표 0.30이 나온 원인:
- `복무규정`, `포상규정`, `보안업무규정` 등은 인덱스에 없는 제목이었음.
- KCSC 키 형식 오류: `KCS 14 20 10:3.4` (❌) → `KCS:142010:3.4` (✓).
- PPS 키는 안건명이 아닌 숫자 일련번호(`448634` 등).

**반드시 eval_collect.py로 후보를 뽑은 뒤 검토해서 라벨해야 한다.**

---

## 5. 개선 방향 (우선순위 순)

### 5-1. Reranker 도입 [우선 순위 1, KCSC 효과 최대]

현재 RRF 이후 재정렬 없이 반환. cross-encoder로 상위 15개를 재정렬하면 특히 KCSC의 MRR이 개선될 것.

- 구현 위치: `src/sources/rerank.py` 신설. DeepInfra 리랭커 API (bm25_path·기존 키 재사용).
- 환경변수 `RERANK_ENABLED` 토글. API 실패 시 RRF 순위로 graceful fallback.
- KCSC의 인용그래프 1-hop 확장은 reranking **이후** 유지 (점수와 무관하게 첨부).

### 5-2. 점수 임계값 + 품질 필터 [신뢰성]

현재 임계값이 사실상 없어(`score > 0`만 체크) 관련성 낮은 청크도 그대로 반환.

- `SearchResult`에 `score: float` 필드 추가.
- 소스별 임계값 환경변수(`RERANK_MIN_SCORE` 등). 임계값 미만이면 결과에서 제외.
- 모든 결과가 임계값 미만이면 "관련 규정을 찾지 못했습니다"를 명시해 Claude 환각 방지.
- 임계값은 eval_retrieval.py로 precision↑·recall↓ 트레이드오프를 측정해 보정.

### 5-3. 출처 메타데이터 강화 [인용 신뢰성]

현재 `SearchResult.to_text()`는 `source_id`, `title`, `url`, `content`만 출력하고 `metadata`를 활용하지 않음.

- LH: 개정일(`pub_date`) 추가.
- KCSC: `code_type`/`code`/`section_label`(§) 명시, 인용경로(`[인용 참조: ...]`) 유지.
- PPS: 해석일자(`reply_date`), 관련법령 강조.
- 법령/판례: 법령번호·조문번호 / 법원·선고일·사건번호.

---

## 6. 골든셋 확장 절차

현재 31개로 파라미터 튜닝 신뢰도가 낮다. **50~100개**를 목표로 확장한다.
반드시 아래 순서를 지킨다 — 추측 라벨은 지표를 신뢰할 수 없게 만든다.

```
1. 질의 초안 작성 (query, keywords, source 결정)
2. eval_collect.py 로 후보 top-20 덤프
3. 출력 검토 → 반드시 있어야 할 문서키 선별 (1~3개)
4. golden_set.jsonl 에 한 줄 추가
5. eval_retrieval.py 실행해 지표 변화 확인 (sanity check)
```

```bash
# 2단계: 후보 덤프
python scripts/eval_collect.py --source pps \
    --query "선금 지급 요건과 비율" --keywords "선금 선급금 지급" --top 20

# 4단계: golden_set.jsonl 에 추가
# {"id":"pps-09","source":"pps","query":"선금 지급 요건과 비율",
#  "keywords":"선금 선급금 지급","relevant":["438272","445212"]}

# 5단계: 확인
python scripts/eval_retrieval.py --mode hybrid --no-save
```

**소스별 문서키 형식 주의**

| 소스 | 올바른 형식 | 흔한 실수 |
|---|---|---|
| lh | `여비규정` (인덱스 title 그대로) | `복무규정` 등 인덱스에 없는 이름 추측 |
| kcsc | `KCS:142010:3.4` (공백 없음, 콜론 구분) | `KCS 14 20 10:3.4` (공백 포함) |
| pps | `448634` (숫자 일련번호) | 안건명 텍스트로 라벨 |

---

## 7. 파라미터 튜닝

### 조정 가능한 파라미터

| 파라미터 | 위치 | 현재값 |
|---|---|---|
| `TOP_K_FINAL` | `lh_vector.py`, `pps_vector.py` | 7, 5 |
| `KCSC_TOP_K_PRIMARY` | `kcsc_vector.py` | 5 |
| `TOP_K_CANDIDATES` | 각 소스 | 10~20 |
| `RRF_K` | `lh_vector.py` (공유) | 60 |

### Grid Search 방법

파라미터 수가 적어 완전 탐색이 현실적이다. 74개 골든셋 기준 36개 조합 × ~1초 = **약 40초**.

```python
# scripts/tune_params.py 개요
from itertools import product
import src.sources.lh_vector as lh_mod
from scripts.eval_retrieval import evaluate

grid = {
    "top_k_final": [7, 10, 15, 20],
    "candidates_k": [10, 20, 30],
    "rrf_k":        [20, 60, 100],
}
best = {"score": 0}
for top_k, cands, rrf in product(*grid.values()):
    # monkeypatch 후 evaluate() 호출
    lh_mod.TOP_K_FINAL = top_k
    lh_mod.TOP_K_CANDIDATES = cands
    lh_mod.RRF_K = rrf
    r = evaluate(golden_path, mode="hybrid", ks=[5, 10])
    score = r["summary"]["__overall__"]["ndcg@10"]
    if score > best["score"]:
        best = {"top_k": top_k, "cands": cands, "rrf": rrf, "score": score}
print(best)
```

### 주의사항

- **골든셋 50개 미만에서는 튜닝 결과를 과신하지 말 것.** nDCG 0.01~0.02 차이는 통계적 잡음일 수 있다.
- RRF_K·TOP_K_CANDIDATES는 세 소스가 공유 코드를 쓰므로 monkeypatch 시 모두 영향받는다. 소스별로 독립 튜닝이 필요하면 파라미터를 config로 분리하는 리팩터가 선행돼야 한다.
- 튜닝 결과를 코드에 반영하기 전 반드시 MCP Inspector로 실제 도구 응답을 확인한다 (지표가 좋아도 컨텍스트 양이 너무 많아지면 LLM 응답 품질이 떨어질 수 있음).

### LLM에 많이 주기 vs 리랭커

TOP_K_FINAL을 늘려 LLM에 더 많은 후보를 주는 방식이 리랭커보다 **속도 면에서 유리**하다.

- 리랭커: DeepInfra API 호출 1회 추가 → **+500ms~1s** 지연
- TOP_K 증가: LLM 입력 토큰 증가 (~2,000토큰/10개 추가)이지만 Claude는 입력 처리가 빠르고 체감 지연 미미

따라서 먼저 TOP_K_FINAL 튜닝으로 개선을 시도하고, 그래도 부족하면 리랭커를 검토한다.

---

## 8. 테스트 레이어 구분

| 레이어 | 도구 | 목적 | 시점 |
|---|---|---|---|
| 검색 정확도 | `eval_retrieval.py` | Recall·MRR·nDCG 회귀 검증 | 파라미터·파이프라인 변경 시 |
| 기능(도구) | MCP Inspector | 실제 도구 응답 형식·내용 육안 확인 | 서버 코드 변경 시 |
| E2E | `scripts/test_fly_e2e.py` | 배포 서버 대상 엔드투엔드 | 배포 전후 |

세 레이어는 역할이 다르다. `eval_retrieval.py`가 통과해도 서버 코드 버그는 잡지 못하고, MCP Inspector로 봐도 검색 순위 회귀는 보이지 않는다.

### 회귀 검증 절차 (코드 변경 후)

1. `python scripts/eval_retrieval.py --mode hybrid` → nDCG·recall 회귀 없는지 확인
2. `python scripts/eval_retrieval.py --mode bm25 && python scripts/eval_retrieval.py --mode dense` → hybrid 우위 유지 확인
3. `npx @modelcontextprotocol/inspector python -m src.server` → 실제 도구 응답 육안 확인
