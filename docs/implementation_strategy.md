# LH RAG MCP 서버 — 구현 전략 문서

> 작성 기준일: 2026-05-30  
> 대상 독자: 이 프로젝트를 이어서 개발하거나 유사한 RAG MCP 서버를 설계하는 개발자

---

## 1. 프로젝트 개요

LH(한국토지주택공사) 임직원이 Claude Desktop/Web에서 사용하는 **법령·규정·건설기준 검색 MCP 서버**다.
Claude가 질문을 받으면 MCP 도구를 호출해 관련 문서 청크를 검색하고, 그 결과를 바탕으로 답변을 생성한다.

### MCP 도구 4개

| 도구 | 검색 대상 | 방식 |
|---|---|---|
| `search_law` | 국가법령·국토부 행정규칙 | 법제처 AI검색 API + admrul API |
| `search_lh_regulations` | LH 사내 규정 | BM25 + Dense 하이브리드 RAG |
| `search_construction_standards` | 건설기준(KDS/KCS/LHCS) | BM25 + Dense 하이브리드 RAG + 인용 그래프 1-hop 확장 |
| `search_precedents` | 법원 판례 | 법제처 판례 API |

---

## 2. 검색 아키텍처

### 2-1. 법령·판례 — API 패스스루

`search_law`와 `search_precedents`는 외부 API를 직접 호출하고 결과를 포맷해 반환한다.
자체 인덱스가 없으므로 항상 최신 데이터를 가져오지만 API 가용성에 의존한다.

`search_law`는 법제처 AI검색(`aiSearch`) 우선, 실패 시 키워드 일반검색으로 자동 fallback한다.
`search_precedents`는 키워드 AND 매칭이므로 결과 0건이면 첫 번째 키워드만으로 자동 재시도한다.

### 2-2. LH 규정·건설기준 — 자체 하이브리드 RAG

외부 의존 없이 로컬 인덱스(pkl 파일)로 검색한다. 검색기는 두 종류를 병렬 실행하고 RRF로 합산한다.

```
BM25 (keywords)  ──┐
                    ├─ RRF ─→ top-K 청크 ─→ (KCSC만) 인용 그래프 1-hop 확장
Dense (query)    ──┘
```

- **BM25**: kiwipiepy 형태소 분석기로 명사·동사 어간 추출 → BM25Okapi 점수. 전문용어/코드번호/조문번호처럼 정확한 표면 문자열이 중요할 때 강하다.
- **Dense**: DeepInfra API의 `Qwen3-Embedding-0.6B` 모델로 벡터 임베딩 → 코사인 유사도. 표현이 다르거나 자연어 질의에서 의미적 연결을 담당한다.
- **RRF (Reciprocal Rank Fusion)**: 두 결과의 순위를 `1/(k+rank)` 방식으로 합산. 어느 한쪽이 부정확해도 다른 쪽이 보완한다. DEEPINFRA_API_KEY 미설정 시 Dense 없이 BM25만 동작한다.

### 2-3. 인용 그래프 1-hop 확장 (KCSC 전용)

건설기준은 조문 단위("KCS 14 20 10의 3.2에 따른다")로 타 기준을 상호 인용하는 특성이 있다.
1차 하이브리드 검색으로 찾은 청크가 인용하는 다른 기준의 조문을 함께 반환해 컨텍스트를 넓힌다.

```
1차 결과 청크 (최대 7건)
    → 각 청크의 hdr_node (lv2 헤더 노드)를 graph.edges에서 조회
    → 인용 대상 노드 수집 (1차 결과와 중복 제거)
    → node_to_chunks로 대표 청크 선정
    → Dense 유사도순 정렬 후 최대 3건 추가
    → "[인용 참조: 출처조문 → 대상조문]" 태그 붙여 반환
```

---

## 3. 데이터 파이프라인

### 3-1. LH 규정 파이프라인

```
LH 사이트 RSS
    ↓ lh_crawler.py — RSS 파싱, HWP/PDF 다운로드
    ↓ pdf_converter.py — docling(pdfium 백엔드)으로 PDF → 마크다운
    ↓ data/lh_regulation/markdown/{YYMMDD}_{title_key}.md  ← source of truth
    ↓ indexer.py — 파일명 날짜 vs BM25 pub_date 비교 → 변경분만 처리
    ↓ BM25: data/lh_regulation/lh_regulations.pkl
       Dense: data/lh_regulation/lh_regulations_dense.pkl
```

**docling을 선택한 이유**: LH 규정 PDF는 HWP→PDF 변환 파일로, 단어 공백이 글리프가 아닌
좌표 이동(`Tm` 연산자)으로 표현된다. MuPDF(PyMuPDF, pdfminer 등) 계열은 공백 없는 텍스트를
반환한다. docling은 bbox 간격 분석으로 공백을 복원하고 표를 마크다운 테이블로 구조화한다.

**증분 업데이트 기준**: 마크다운 파일명의 날짜(`{YYMMDD}`)를 source of truth로 쓴다.
파일시스템 조회만으로 최신 여부를 판단할 수 있어 JSON을 열지 않아도 된다.

### 3-2. KCSC 건설기준 파이프라인

```
KCSC Open API (CodeList + CodeViewer)
    ↓ kcsc_api.py — 비동기 HTTP, HTML→텍스트, 인용 추출
    ↓ data/kcsc/cache/{YYYYMMDD}_{doc_key}.json  ← source of truth
    ↓ kcsc_indexer.py — 파일명 날짜 vs API updateDate 비교 → 변경분만 호출
    ↓ BM25: data/kcsc/kcsc_standards.pkl
       Dense: data/kcsc/kcsc_standards_dense.pkl
       Graph: data/kcsc/kcsc_standards_graph.pkl
```

LH와 달리 PDF 변환 없이 공식 API JSON을 직접 사용한다.
캐시 파일 규모: 1,822개 문서 → 41,177청크 → 17,604 인용 엣지 (2026-05 기준)

---

## 4. 청킹 전략

### 4-1. LH 규정 — 헤딩 기반 청킹

마크다운 `##` → `#` → 단락 순으로 시도해 청크가 2개 이상 나올 때까지 분할한다.
어느 헤딩으로도 2개 이상이 안 되면 800자 고정 분할(`_split_fixed`)로 폴백한다.
각 청크는 최대 800자, 100자 오버랩.

### 4-2. KCSC 건설기준 — lv2 집계 청킹

KCSC API는 조문을 level 1~4 의 평면 배열로 반환한다.
lv3/lv4 조문을 각각 청킹하면 문맥 없는 미니청크(5~200자)가 되므로, **lv1/lv2 헤더를 경계로
하위 조문(lv3/lv4) 텍스트를 하나로 합산**한다.

```
[lv2] §3.4 양생  ← hdr_node, 청크 경계
  [lv3] 3.4.1 일반사항
    [lv4] (1) 콘크리트는 타설한 후...
    [lv4] (2) 습윤양생 기간은...
  [lv3] 3.4.2 양생 방법
    [lv4] (1) 양생 방법의 종류...
→ 이 모두를 하나의 텍스트로 합산 → 1청크
```

추가 규칙:
- **60자 미만 그룹 병합**: lv1 헤더만 있는 그룹("1. 일반사항")은 다음 그룹에 병합
- **800자 초과 분할**: `_split_fixed`로 추가 분할
- **"내용 없음" 필터**: API placeholder 텍스트는 인덱싱 전 제거

### 4-3. 인용 그래프 노드와 청크의 관계

인용 엣지는 lv2 헤더 노드(`KCS:142010:3.4`) 단위로 집계한다.
lv3/lv4 자식 조문에서 발생한 인용도 모두 부모 hdr_node에 합산하므로,
검색 시 BM25 메타의 `node_id`(hdr_node)만으로 해당 청크 전체의 인용을 조회할 수 있다.

---

## 5. 인용 추출 — 2-pass 정규식

LHCS 코드는 6자리(`LHCS 10 10 00`)와 8자리(`LHCS 10 10 05 05`)가 혼재한다(250개 vs 294개).
단순한 6자리 정규식으로 8자리 코드를 파싱하면 뒤 2자리를 절 번호로 잘못 해석한다.

**2-pass 전략**:
1. Pass 1 — LHCS 8자리 전용 패턴(`\d{2}\s*\d{2}\s*\d{2}\s*\d{2}`)으로 먼저 매칭,
   `known_codes`에 존재하면 채택하고 위치를 기록
2. Pass 2 — 6자리 패턴으로 나머지 매칭,
   LHCS이고 Pass 1에서 이미 처리된 위치면 스킵

`known_codes`(CodeList 전체)가 안전망 역할을 해서 잘못된 코드가 걸러진다.

---

## 6. 증분 업데이트 구조

### 공통 원칙
- **파일명이 source of truth**: 날짜를 파일명에 인코딩(`{YYYYMMDD}_{doc_key}.json`)해
  파일 열지 않고도 최신 여부 판단 가능
- **문서 단위 증분**: Dense 임베딩은 변경된 `doc_key`의 청크만 재임베딩
  (`update_dense_incremental`으로 기존 벡터 유지 + 신규 벡터 추가)
- **BM25는 항상 전체 재빌드**: BM25 인덱스는 구조상 부분 업데이트가 불가해 전체 재빌드

### KCSC 갱신 조건
```
_load_cached_date(meta)  !=  _date_prefix(meta.update_date)
      ↑ 파일명에서 파싱          ↑ API 응답에서 파싱
```
날짜가 같으면 스킵, 다르면 기존 캐시 삭제 후 새 파일로 저장한다.

### 동시성 제어
크롤 시 세마포어(`_CONCURRENCY = 5`)로 KCSC API 동시 호출을 제한한다.
전체 1,874개 코드 기준 크롤 약 5~15분, 빌드 약 3~5분.

---

## 7. 파일 경로 분리

LH와 KCSC 데이터는 `data/` 하위에 명시적으로 분리한다.
`bm25_index.py`와 `dense_index.py`에 `base_path` 파라미터를 추가해 경로를 주입받는다.

```
data/
  lh_regulation/             ← BM25_PATH 환경변수
    lh_regulations.pkl
    lh_regulations_dense.pkl
    markdown/
  kcsc/                      ← KCSC_DATA_PATH 환경변수
    cache/
      {YYYYMMDD}_{doc_key}.json
    kcsc_standards.pkl
    kcsc_standards_dense.pkl
    kcsc_standards_graph.pkl
```

---

## 8. 서버 구조

### FastMCP + Starlette 미들웨어

```python
mcp = FastMCP(name="LH RAG MCP", instructions="...")

_sources = {
    "law_api":       LawApiSource(),
    "lh_vector_db":  LHVectorSource(),
    "kcsc_vector_db": KCSCVectorSource(),
    "prec":          PrecedentSource(),
}

@mcp.tool()
async def search_xxx(query: str, keywords: str) -> str:
    return await _search_single("source_id", query, keywords)
```

미들웨어:
- `ApiKeyMiddleware`: Bearer 토큰 인증 (MCP_API_KEY 미설정 시 비활성)
- `LawOcMiddleware`: URL `?law_oc=` 파라미터를 contextvars로 요청 스코프에 주입

### 서버 시작 시 사전 로딩

서버 시작 시 BM25 인덱스와 Kiwi 형태소 분석기를 미리 로딩해 첫 요청 지연을 막는다.
Dense 인덱스는 첫 검색 시 `_ensure_loaded`로 lazy 로딩한다.

---

## 9. 새 검색 소스 추가 방법

1. `src/sources/`에 `SearchSource` 상속 클래스 작성
   - `source_id = "my_source"` 선언
   - `async def search(self, query: str, keywords: str) -> list[SearchResult]` 구현
2. `src/server.py`의 `_sources` 딕셔너리에 인스턴스 추가
3. `@mcp.tool()` 함수 추가 (`_search_single` 호출)
4. `SOURCE_LABELS`에 표시 이름 추가

---

## 10. 알려진 제약 및 운영 고려사항

| 항목 | 내용 |
|---|---|
| **cold start** | fly.io 머신 일시정지 후 첫 요청 ~20초 (이후 warm ~2초) |
| **BM25 재빌드 시간** | 41k청크 기준 약 3~4분 (kiwipiepy 토크나이징 병목) |
| **Dense 최초 빌드** | ~20~30분 (DeepInfra API, 100청크 배치) |
| **KCSC 전체 크롤** | ~5~15분 (동시성 5, API 응답속도 의존) |
| **LH 사이트 SSL** | 인증서 체인 문제로 `verify=False` 처리 중 |
| **HWP 변환** | LibreOffice 미설치 시 건너뜀 |
| **스캔 PDF** | ocrmac 미설치 시 텍스트 추출 실패 → 해당 문서 스킵 |
| **BM25 전용 모드** | DEEPINFRA_API_KEY 미설정 시 Dense 없이 BM25만 동작 (graceful degradation) |
| **KCSC 인용 그래프** | 그래프 pkl 없으면 1-hop 확장 비활성, 기본 하이브리드 검색으로 폴백 |

### 검색 품질 관점
KCSC는 전문용어·코드번호 중심이라 BM25가 주 검색축이고 Dense는 recall 보강용이다.
사용자가 `keywords`에 정확한 용어(코드명, 조문 번호, 재료명)를 넣을수록 결과가 좋아진다.
MCP 도구 docstring에서 `query`(자연어)와 `keywords`(핵심 전문용어) 역할을 명시하는 것이 중요하다.
