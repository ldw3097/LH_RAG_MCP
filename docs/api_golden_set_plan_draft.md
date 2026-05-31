# API 툴 골든셋 테스트 설계 방안 (임시 초안)

작성: 2026-05-31  
상태: 설계 초안 — 구현 전

> 법제처 외부 API를 사용하는 `search_law`(법령+행정규칙)와 `search_precedents`(판례) 두 도구에 대한 골든셋 및 평가 파이프라인 설계안.

## 구현 TODO

- [ ] 법령/판례 doc_key 추출 로직 확정 (법령명·조문번호·사건번호 중 granularity 결정)
- [ ] `scripts/eval_api_common.py` 작성 — display=top_k 직접 호출 wrapper + doc_key 추출
- [ ] `scripts/eval_collect_api.py` 작성 — eval_api_common 기반 + 캐시 + 후보 출력 (--top 30)
- [ ] `eval/api_queries.jsonl` 초안 작성 (35건: law-ai 12, law-admrul 6, law-fallback 4, prec 13)
- [ ] eval_collect_api.py --batch 실행 → 후보 확인 → api_golden_set.jsonl 수동 라벨링
- [ ] `scripts/eval_api.py` 작성 — Recall@k/MRR/nDCG, retrieval(top=20)·production(cap) 2모드 + 캐시
- [ ] 첫 baseline 측정 후 docs/retrieval_accuracy_analysis.md API 섹션 추가

---

## 현황 진단

현재 평가 시스템은 로컬 벡터 인덱스 3종(lh, kcsc, pps)만 대상으로 한다.

| 구분 | 현황 |
|---|---|
| 평가 대상 | lh / kcsc / pps (BM25+Dense RRF) |
| 평가 제외 | `search_law` (법령 API), `search_precedents` (판례 API) |
| 현재 baseline | recall@5=0.839, MRR=0.771 (n=31) |

API 도구는 다음 이유로 벡터 평가와 **설계가 달라야** 한다:

- 로컬 인덱스 없음 → 실제 HTTP 호출 필수
- 시간에 따른 API 결과 변동 → 정답 안정성 관리 필요
- 두 경로(AI검색 / 키워드 fallback / 행정규칙) 독립 검증 필요

**반환 건수는 평가 시 확장 가능.** 운영 cap은 `src/sources/law_api.py`의 `CANDIDATE_K=7`, `ADMRUL_DETAIL_K=3`, `src/sources/prec_api.py`의 `PREC_DETAIL_K=5`로 임의 설정된 값이며, 법제처 API `display` 파라미터에 그대로 전달된다. 벡터 eval이 `eval_common.py`에서 운영 `search()` cap을 우회하듯, API eval도 `_ai_search` / `_general_search` / `_admrul_search` / `_prec_search`를 **`display=top_k`로 직접 호출**해 임의 건수를 받을 수 있다.

---

## 1. 정답 식별자(doc_key) 설계

API 결과에서 불변·안정적인 식별자를 정답으로 사용한다.

| 소스 | doc_key | 예시 | 안정성 |
|---|---|---|---|
| `law` (법령 AI검색) | `법령명` | `공동주택관리법` | 법령명 자체는 거의 불변 |
| `law` (행정규칙) | `행정규칙명` | `공동주택 회계처리기준` | admrul 이름 안정 |
| `prec` (판례) | `사건번호` | `2019다123456` | 완전 불변 |

`법령명+조문번호`(`공동주택관리법 제36조`)까지 좁히면 정밀도는 높지만 법 개정 시 조문번호가 바뀔 수 있으므로, **법령명 수준**을 기본 granularity로 한다. 조문 수준이 필요한 케이스는 `relevant_articles` 필드를 병행 기재한다.

---

## 2. 골든셋 파일 구조

`eval/api_golden_set.jsonl` 신규 생성 (목표 35건 이상):

```jsonl
// search_law — 법령 AI검색 경로 (law_ai)
{
  "id": "law-ai-01",
  "source": "law",
  "path": "ai",
  "query": "공동주택 하자담보책임 기간과 범위",
  "keywords": "공동주택 하자 담보책임 기간",
  "relevant": ["공동주택관리법"],
  "relevant_articles": ["공동주택관리법 제36조"],
  "notes": "LH 핵심 업무 법령"
}

// search_law — 행정규칙 경로 (admrul)
{
  "id": "law-admrul-01",
  "source": "law",
  "path": "admrul",
  "query": "",
  "keywords": "공동주택 회계처리 기준",
  "relevant": ["공동주택 회계처리기준"],
  "notes": "국토교통부 행정규칙 hit 검증"
}

// search_precedents — 판례 (prec)
{
  "id": "prec-01",
  "source": "prec",
  "keywords": "공사대금 지체상금",
  "relevant": ["2019다123456"],
  "notes": "사건번호로 정답 고정"
}
```

**케이스 구성 목표 (35건)**:

| 소스 | 경로 | 건수 | 검증 포인트 |
|---|---|---|---|
| law | AI 검색 | 12건 | LH 관련 법령(주택법·공동주택관리법·국가계약법·건설산업기본법 등) |
| law | 키워드 fallback | 4건 | AI 검색 실패 패턴 — 매우 짧거나 영문 포함 query |
| law | admrul | 6건 | 국토부 행정규칙 단독 히트 케이스 |
| prec | 일반 | 10건 | 건설공사·계약·토지·임대차 분야 |
| prec | 1st-token 재시도 | 3건 | 2개 이상 키워드 AND → 0건 후 첫 키워드 단독 재시도 성공 케이스 |

---

## 2-1. 평가 모드 vs 운영 모드 (2층 지표)

| 모드 | top_k | 목적 | k 값 |
|---|---|---|---|
| **retrieval** (기본) | `--top 20` (또는 30) | API 랭킹 품질 측정 — 벡터 eval과 동일한 Recall@10 비교 | k = 5, 10, 20 |
| **production** | 운영 cap 그대로 (7/3/5) | 실제 MCP 툴이 사용자에게 주는 결과 품질 | k = 3, 5, 7 |

- **라벨링(collect)**: `--top 30`으로 넓게 후보를 뽑아 정답 doc_key 선정 (벡터 `eval_collect.py --top 40`과 동일 패턴)
- **baseline 측정**: retrieval 모드로 Recall@5/@10/MRR 산출 → 벡터 baseline(0.839)과 직접 비교 가능
- **운영 sanity check**: production 모드로 별도 집계 — "실제 7건 안에 정답이 들어오는가"

구현 방식: eval 스크립트에서 `LawApiSource` / `PrecedentSource`의 public `search()`를 쓰지 않고, 내부 메서드에 `display` 인자를 넘기는 thin wrapper (`scripts/eval_api_common.py`)를 둔다. 운영 코드 상수는 변경하지 않는다.

---

## 3. 수집 스크립트: `scripts/eval_collect_api.py`

기존 `eval_collect.py`의 API 버전. 실제 HTTP 호출 후 후보를 출력하고 캐시에 저장한다.

**핵심 설계**:

- `eval_api_common.py`의 `retrieve()`로 API 직접 호출 (`display=--top`, 기본 30)
- 결과마다 `doc_key` 추출: law → `법령명` (metadata), prec → `사건번호`
- `--cache eval/api_cache/` 에 요청별 JSON 저장 (재실행 시 캐시 우선)
- 출력: `[rank] doc_key | title | content 미리보기 120자`
- 끝에 `relevant` 후보 JSON 배열 출력 (골든셋 붙여넣기용)

```bash
python scripts/eval_collect_api.py --source law \
    --query "공동주택 하자담보책임" --keywords "하자 담보책임" --top 30

python scripts/eval_collect_api.py --source prec \
    --keywords "공사대금 지체상금" --top 20

# 배치 모드 (JSONL 파일로)
python scripts/eval_collect_api.py --batch eval/api_queries.jsonl --top 30
```

---

## 4. 평가 스크립트: `scripts/eval_api.py`

`eval_retrieval.py`의 API 버전.

**지표 체계** (벡터 eval과 동일):

| 지표 | 산식 | 비고 |
|---|---|---|
| Recall@k | \|relevant ∩ ranked[:k]\| / \|relevant\| | 복수 정답 케이스 대응 |
| MRR | 1/rank(첫 relevant hit) | 순위 품질 |
| nDCG@k | 표준 nDCG | 벡터 eval과 비교 가능 |

- `--top 20` (기본): API `display` 파라미터 — retrieval 품질 측정용
- `--ks 5 10 20`: Recall@k / nDCG@k 계산 k값 (벡터 eval과 동일)
- `--mode retrieval` (기본) | `--mode production`: 운영 cap(7/3/5) 적용 여부
- `--use-cache` 옵션: 캐시된 응답으로 오프라인 재실행 (API 키 불필요)
- `--source law|prec|all`로 소스 필터
- `--path ai|admrul|fallback`로 경로별 세분 분석
- 결과: `eval/results/{timestamp}_api_{mode}.json`

**경로별 세분 집계** (retrieval 모드, top=20 예시):

```
=== API 평가 (mode=retrieval, top=20, n=35) ===
source     path       recall@5  recall@10  recall@20   MRR  nDCG@10
law        ai            ...       ...        ...      ...     ...
law        admrul        ...       ...        ...      ...     ...
law        fallback      ...       ...        ...      ...     ...
prec       normal        ...       ...        ...      ...     ...
prec       retry         ...       ...        ...      ...     ...
전체                      ...       ...        ...      ...     ...

=== API 평가 (mode=production, n=35) ===  ← 운영 cap 그대로
source     path       recall@3  recall@5  recall@7   MRR
...
```

---

## 5. 평가 설계 상의 특수 고려사항

### 5-1. API 결과 시간 변동성 대응

- `relevant` 에 복수 법령명 허용 (개정으로 법령명 변경 시 구법령명도 보조로 기재)
- 분기 1회 `eval_collect_api.py --batch` 로 재라벨링 검토
- `relevant_articles` (조문번호 수준)는 선택적 필드로 두어 조문 이동 시 영향 최소화

### 5-2. 캐시 전략

```
eval/
  api_cache/
    law_{query_hash}.json     # search_law 응답 캐시
    prec_{kw_hash}.json       # search_precedents 응답 캐시
  api_golden_set.jsonl        # 골든셋
  results/
    {ts}_api.json             # 평가 결과
```

- 캐시 만료: 파일명에 날짜 포함, 30일 이상이면 stale 경고

### 5-3. OC 키 격리

- `eval_api.py`는 `.env`의 `LAW_OC_DEFAULT`만 사용 (contextvars 불필요)
- API 호출 오류 시 해당 질의 `skip` 처리 후 결과에 `"error"` 필드 기록

### 5-4. fallback 경로 강제 테스트

- `path: "fallback"` 케이스는 일부러 `query`를 비워두거나, 모의 AI검색 실패 상황을 만들기 어려움
- 대신 `keywords` 만으로 `_general_search` 경로를 직접 호출하는 `--path general` 옵션 추가

---

## 6. 구현 파일 목록

| 파일 | 역할 |
|---|---|
| `eval/api_golden_set.jsonl` | 35건 골든셋 (신규) |
| `scripts/eval_api_common.py` | API 직접 호출 wrapper (`display=top_k`, doc_key 추출) (신규) |
| `scripts/eval_collect_api.py` | API 결과 수집 + 캐시 + 라벨링 보조 (신규) |
| `scripts/eval_api.py` | Recall@k / MRR / nDCG 계산, retrieval·production 2모드 (신규) |
| `eval/api_cache/` | API 응답 캐시 디렉토리 |
| `docs/retrieval_accuracy_analysis.md` | API 평가 섹션 추가 (수정) |

기존 `eval_common.py`, `eval_retrieval.py`는 변경 없음.

---

## 7. 골든셋 구축 순서

```
1. eval_collect_api.py 작성 (캐시 포함)
2. 질의 초안 30~40개 작성 (eval/api_queries.jsonl)
3. eval_collect_api.py --batch 실행 → 후보 목록 확인
4. 각 질의별 relevant doc_key 수동 라벨링 → api_golden_set.jsonl 완성
5. eval_api.py 작성 및 첫 baseline 측정
6. docs/retrieval_accuracy_analysis.md에 결과 기록
```
