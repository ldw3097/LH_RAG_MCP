# KCSC 건설기준(KDS/KCS/LHCS) RAG MCP 도구 추가 계획

## Context

LH 임직원은 사내 규정뿐 아니라 **국가건설기준**(KDS 설계기준, KCS 표준시방서, LHCS LH 전문시방서)을
자주 참조한다. 현재 MCP 서버에는 `search_law`(법령)와 `search_lh_regulations`(LH 규정) 두 도구만 있어
건설기준 질의에 답할 수 없다. 본 작업은 KCSC 데이터를 기존 LH 규정 RAG와 **동일한 BM25+Dense 하이브리드
자체 RAG** 방식으로 인덱싱하고, 새 MCP 도구 `search_construction_standards`를 추가한다.

핵심 차이점: LH는 RSS→PDF→docling 크롤링이 필요했지만, **KCSC는 공식 Open API(JSON)를 제공**한다.
따라서 docling/HWP/크롤링 계층 없이 API 호출 → 구조화된 본문 → 마크다운 조립으로 훨씬 단순하다.

### KCSC Open API (references/KCSC_API_Guide.md)
- 인증키 필수(키 없으면 HTTP 400). 사용자 보유/발급 가능 확인됨.
- `GET https://kcsc.re.kr/OpenApi/CodeList?key=KEY` → 전체 코드 목록(KDS/KCS/LHCS), 각 항목에
  `codeType`, `code`, `fullCode`, `name`, `version`, `updateDate`.
- `GET https://kcsc.re.kr/OpenApi/CodeViewer/{Type}/{Code}?key=KEY` → 해당 코드의 `list`
  (목차 항목 배열: `sort`, `title`, `level`, `label`, `contents`(HTML)).

### 설계 결정 (사용자 확인됨)
- **통합 도구 1개**: KDS+KCS+LHCS를 단일 컬렉션 `kcsc_standards`에 저장, `codeType`은 metadata에 보관.
- **조문(섹션) 단위 청킹·그래프 RAG (인용 확장)**: 건설기준은 상호 인용이 매우 활발하며, 인용은
  문서 전체가 아니라 **세부 조문(절·항) 단위**(예: "KCS 14 20 10의 3.2에 따른다")로 가는 경우가
  많다. CodeViewer API가 이미 본문을 목차 항목(`label`="3.2" 등) 단위로 구조화해 주므로, 청킹과
  그래프 노드를 **조문 단위**로 맞춘다. 인덱싱 시 조문 간 인용 관계를 그래프로 추출하고, 검색 시
  1-hop 이웃 조문의 청크를 결과에 포함한다.

## 재사용할 기존 자산

기존 인덱싱/검색 유틸은 collection 파라미터화가 잘 되어 있어 그대로 재사용한다:
- `crawler/indexer.py`: `chunk_text()`, `title_to_key()`, `chunk_id()`
- `crawler/bm25_index.py`: `build_and_save(ids, corpus, metadatas, collection=)`, `load_bm25(collection=)`,
  `bm25_search()`, `tokenize()`, `warmup_kiwi()`
- `crawler/dense_index.py`: `build_and_save_dense(ids, corpus, collection=)`, `load_dense(collection=)`,
  `embed_query()`, `dense_search()`
- `src/sources/base.py`: `SearchResult`, `SearchSource`
- `src/sources/lh_vector.py`: 하이브리드+RRF 검색 로직 (그대로 일반화)

## 구현

### 1. config.py — 환경변수 추가
`src/config.py` Settings에 추가:
```python
kcsc_api_key: str = ""                       # KCSC Open API 인증키
kcsc_api_base: str = "https://kcsc.re.kr/OpenApi"
kcsc_bm25_collection: str = "kcsc_standards"
```
`.env`/배포 시크릿에 `KCSC_API_KEY` 등록. CLAUDE.md 환경변수 섹션에도 반영.

### 2. crawler/kcsc_api.py — Open API 클라이언트 (신규)
docling 크롤러 대신 가벼운 API 클라이언트. `httpx.AsyncClient` 사용.
- `async def fetch_code_list() -> list[CodeMeta]` — CodeList 호출, KDS/KCS/LHCS만 필터.
- `async def fetch_code_viewer(code_type, code) -> list[dict]` — CodeViewer의 `list` 반환.
- `def viewer_to_sections(name, code_type, code, version, items) -> list[Section]` —
  CodeViewer의 `list` 항목을 **조문 단위 섹션**으로 변환. 각 Section은
  `{label, level, section_title, text}`. `text`는 HTML 제거(`BeautifulSoup` get_text)한 본문.
  너무 짧은 헤더-only 항목(예: "1. 일반사항")은 하위 항목과 묶고, 너무 긴 섹션만 추가 분할.
  (문서 전체 마크다운은 마크다운 캐시·디버그용으로 별도 조립)
- `def extract_citations(text, known_codes) -> set[str]` — 본문에서 인용 **조문 노드** 추출.
  정규식으로 `(KDS|KCS|LHCS|EXCS|SMCS|…)\s*\d{2}\s*\d{2}\s*\d{2}` 코드 + 뒤따르는 선택적 절 번호
  (`의?\s*\(?\d+(\.\d+)*\)?`, 예: "의 3.2", "(3.2.1)")를 함께 포착. 코드는 공백 제거해
  `code` 형식("14 20 10"→"142010")으로 정규화, 절 번호가 있으면 `code_id:label`,
  없으면 `code_id`(문서 레벨)로 반환. `known_codes`(CodeList의 codeType+code 집합)에 존재하는
  코드만 채택(자기 자신 제외).
- API 키는 `settings.kcsc_api_key`에서 읽음. 키 없으면 명확한 에러.

### 3. crawler/kcsc_indexer.py — 인덱스 빌드 (신규, indexer.py 패턴 차용)
LH의 RSS 동기화 대신 API 기반 전체/증분 빌드:
- CodeList로 전체 코드 열거 → 각 코드 CodeViewer 조회 → `viewer_to_sections()`
- 마크다운 캐시: `data/markdown/kcsc/{codeType}_{code}_{title_key}.md` (전체 문서, 디버그/감사용.
  LH와 디렉토리 분리. `updateDate` 비교로 증분 스킵 — LH의 pubDate 비교 패턴 차용)
- **조문 단위 청킹**: 각 Section = 1 청크(과대 섹션만 `chunk_text`로 추가 분할). 청크 ID는 조문
  라벨을 포함해 안정·조회 가능하게: `chunk_id = "{codeType}{code}__{label}__c{idx:04d}"`
  (label 예: "3.2"; 그래프 노드 `code_id:label`와 직접 매핑).
- metadata: `{title(name), code_type, code, full_code, version, update_date, url,
  section_label, section_title, level, chunk_index}`
  - url은 사람이 보는 뷰어 URL: `https://www.kcsc.re.kr/StandardCode/Viewer/{type}/{code}` 형태(확인 후 확정)
- `build_and_save(..., collection=settings.kcsc_bm25_collection)`
- `build_and_save_dense(..., collection=...)` (DEEPINFRA_API_KEY 있을 때)
- 증분 갱신은 `update_dense_incremental()` 재사용.
- API rate limit 대비 동시성 제한(세마포어) + 재시도.

**인용 그래프 빌드 (조문 노드)**: 각 섹션 텍스트에서 `extract_citations()`로 인용 조문 노드 집합
추출 → 인접리스트 구성 → `data/bm25/kcsc_graph.pkl`에 저장. 구조:
```python
KcscGraph(
    edges:    dict[str, list[str]],   # 출발 노드(조문) -> 인용하는 노드 목록 (out-edges)
    node_to_chunks: dict[str, list[str]],  # node_id -> chunk_id 목록 (1-hop 청크 조회용)
    node_names: dict[str, str],       # node_id -> "name §label section_title" (표시용)
)
```
- node_id 형식: 조문 `"{codeType}:{code}:{label}"`(예: `"KCS:142010:3.2"`),
  문서 레벨 인용은 `"{codeType}:{code}"`.
- **인용 대상 해석**: 인용이 절 번호를 포함하면 해당 조문 노드로, 없거나 정확히 매칭되는 라벨이
  없으면 가장 가까운 상위 라벨(최장 접두 매칭) 또는 문서 레벨로 폴백.
- 단일 pkl이라 검색 시 즉시 로드.

### 4. src/sources/kcsc_vector.py — SearchSource (신규)
`src/sources/lh_vector.py`와 거의 동일. collection만 `kcsc_standards`.
```python
class KCSCVectorSource(SearchSource):
    source_id = "kcsc_vector_db"
    def __init__(self, collection=None):
        self._collection = collection or settings.kcsc_bm25_collection
        ...  # lh_vector.py의 _ensure_loaded / search / _rrf 그대로,
             # load_bm25/load_dense에 collection 전달
```
주: lh_vector.py의 검색 로직이 컬렉션만 다르고 동일하므로, 선택적으로 공통 베이스
`HybridVectorSource(source_id, collection)`로 리팩터해 LHVectorSource/KCSCVectorSource가 상속하게
할 수 있다(중복 제거). 위험을 줄이려면 신규 클래스 복제도 허용 — 구현 시 판단.

**그래프 1-hop 확장 (KCSC 전용, 조문 단위)**: 기본 RRF로 1차 top-K(7건) 청크를 얻은 뒤:
1. 1차 결과 청크들의 출처 **조문 노드 id**(`code_id:label`) 집합을 모음(metadata로 복원).
2. `kcsc_graph.pkl`의 `edges`로 각 노드의 1-hop 인용 이웃 노드를 수집(1차 결과에 이미 포함된
   노드는 제외).
3. 각 이웃 노드의 `node_to_chunks`에서 청크를 가져옴:
   - 조문 노드면 해당 조문 청크를 **정확히** 사용(1개; 과대 분할 시 쿼리와 가장 맞는 조각).
   - 문서 레벨 노드면 그 코드의 청크 중 쿼리와 가장 잘 맞는 1개 — 메모리의 Dense 임베딩/BM25
     점수를 재활용한 미니 재랭크. (이웃당 대표 1청크로 결과 폭증 방지.)
4. 모은 이웃 청크를 쿼리 유사도순 정렬 후 **최대 N건(예: 3)** 추가, `metadata.via="citation"` 표시.
   포맷에서 `[인용 참조: {인용한 조문}→{이웃 코드명 §label}]` 라벨로 구분.
5. 1차 결과 + 1-hop 결과를 합쳐 반환. 그래프 미존재(pkl 없음) 시 기본 동작으로 폴백.

### 5. src/server.py — 소스 등록 + 도구 추가
```python
from src.sources.kcsc_vector import KCSCVectorSource

SOURCE_LABELS["kcsc_vector_db"] = "건설기준(KDS/KCS/LHCS)"
_sources["kcsc_vector_db"] = KCSCVectorSource()

@mcp.tool()
async def search_construction_standards(query: str, keywords: str) -> str:
    """국가건설기준센터(KCSC)의 건설기준을 검색합니다.
    KDS(설계기준)·KCS(표준시방서)·LHCS(LH 전문시방서) — 설계·시공·검측·유지관리
    기술기준에 관한 질문에 사용하세요.
    Args:
        query: 자연어 질의 (의미 검색)
        keywords: 핵심 키워드 공백 구분 (어휘 검색)
    """
    return await _search_single("kcsc_vector_db", query, keywords)
```

### 6. scripts/build_kcsc_index.py — 빌드 엔트리포인트 (신규)
`scripts/build_index.py` 패턴 차용. `--limit N`, `--type KDS|KCS|LHCS`(선택) 옵션으로
부분 빌드 지원. 내부적으로 kcsc_indexer 전체 빌드 호출.

### 7. 문서
CLAUDE.md: 아키텍처 다이어그램, 파일 지도, 환경변수, 데이터 경로(`data/markdown/kcsc/`,
`data/bm25/kcsc_standards*.pkl`)에 KCSC 항목 추가.

## 검증

1. `.env`에 `KCSC_API_KEY` 설정 후 소규모 빌드:
   `python scripts/build_kcsc_index.py --type KDS --limit 5`
   → `data/markdown/kcsc/*.md`, `data/bm25/kcsc_standards.pkl`, `*_dense.pkl` 생성 확인.
2. 마크다운 캐시 1개를 열어 HTML 제거·헤딩 구조·표 텍스트가 온전한지 확인.
3. 서버 실행 `python -m src.server` 후 MCP 클라이언트(또는 직접 호출)로
   `search_construction_standards(query="콘크리트 압축강도 기준", keywords="콘크리트 압축강도")`
   → 관련 KCS/KDS 청크 7건 + `[인용 참조]` 라벨의 1-hop 이웃 청크와 뷰어 URL 반환 확인.
4. 인용이 많은 기준(예: 본문에 다른 KCS를 명시 인용하는 코드)으로 질의해 1-hop 확장이
   실제로 인용 대상 문서 청크를 끌어오는지, 1차 결과와 중복 제거되는지 확인.
   `kcsc_graph.pkl`의 edges가 합리적인지(예: 무작위 코드의 인용 이웃) 점검.
5. BM25만(DEEPINFRA 키 없을 때) 동작하는지, 인덱스/그래프 미구축 시 안내·폴백하는지 확인.
6. 기존 `search_lh_regulations`·`search_law` 회귀 없음 확인(공통 유틸 변경 시).

## 미해결 / 빌드 전 확인
- CodeList 응답에 LHCS `codeType`이 실제 포함되는지(키 없이 미검증) — 빌드 첫 단계에서 확인.
- 사람용 뷰어 URL 정확한 경로 — 실제 사이트에서 확정 후 metadata.url에 반영.
- 전체 코드 수/Dense 임베딩 비용 규모 — `--limit`로 점진 확인.
- 인용 표기 실제 형식 — 본문 contents 샘플을 보고 `extract_citations` 정규식이 실제 인용 패턴
  (공백/점 구분, 코드번호 vs 이름 인용)을 잘 잡는지 빌드 첫 단계에서 검증 후 보정.
