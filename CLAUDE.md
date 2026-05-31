# CLAUDE.md — LH RAG MCP 프로젝트 컨텍스트

LH(한국토지주택공사) 임직원용 법령·규정 RAG MCP 서버. Claude Desktop/Web에 연결해 사용한다.
배포 주소: `https://lh-rag-mcp.fly.dev/mcp`

## 빠른 실행

```bash
source .venv/bin/activate            # 또는 uv sync

# LH 규정 인덱스 빌드 (최초 1회, ~20~30분)
python scripts/build_index.py

# KCSC 건설기준 인덱스 빌드 (최초 1회, 전체 ~수십분)
python scripts/build_kcsc_index.py
# 테스트 (유형별 5개씩만)
python scripts/build_kcsc_index.py --type KDS --limit 5
python scripts/build_kcsc_index.py --type KCS --limit 5
python scripts/build_kcsc_index.py --type LHCS --limit 5
python scripts/build_kcsc_index.py --from-cache   # 인덱스만 재빌드

# 조달청 해석사례 인덱스 빌드 (최초 1회, 864건 ~수분)
python scripts/build_pps_index.py
python scripts/build_pps_index.py --limit 20      # 테스트
python scripts/build_pps_index.py --from-cache    # 인덱스만 재빌드

python -m src.server                 # MCP 서버 실행 (또는 lh-rag-mcp)
```

## 아키텍처

```
search_law(query, keywords)                   → aiSearch(법령조문+행정규칙조문) + 비법령명어제거·병렬 키워드검색(법령·행정규칙) 정확매칭재정렬 → Claude (3블록)
search_lh_regulations(query, keywords)        → LH 규정 BM25(keywords)+Dense(query) RRF       → Claude
search_construction_standards(query,keywords) → KCSC 건설기준 BM25+Dense RRF + 인용그래프 1-hop → Claude
search_precedents(keywords)                   → 법제처 판례검색(prec) 상위 N건 요지 조회        → Claude
search_procurement_interpretations(query,kw)  → 조달청 해석사례 BM25(keywords)+Dense(query) RRF → Claude
```

- MCP 도구 5개. Claude가 질문 성격에 맞게 선택(또는 여러 개 호출)한다.
- `search_law`/`search_lh_regulations`/`search_construction_standards`는 `query`(자연어 — AI검색·Dense)와 `keywords`(키워드 — 일반검색·BM25·admrul)를 받는다.
- `search_law`: ① aiSearch `search=0`(법령조문)·`search=2`(행정규칙조문) 각 5건 병렬 → 조문 블록(최대 10건).
  ② keywords에서 비법령명어(`NON_LAW_NAME_RE`) 제거본·원본을 병렬 법령 일반검색 → 합집합 → `score_law_relevance` 재정렬 → 상위 10건 본문 조회(300자) → 법령 블록.
  ③ 같은 방식으로 국토부 행정규칙(admrul) 키워드검색 → 행정규칙 블록(300자).
  세 블록을 분리해 반환. aiSearch·키워드검색 어느 쪽이 실패해도 나머지 블록은 유지.
- `search_construction_standards`: KDS(설계기준)·KCS(표준시방서)·LHCS(LH 전문시방서)를 BM25+Dense
  하이브리드로 검색하고, 1차 결과 조문이 인용하는 대상 조문을 인용 그래프로 1-hop 확장해 함께 반환.
- `search_precedents`: `keywords`만 받는다(판례 API는 키워드 AND 매칭). 상위 N건의 판시사항·판결요지·
  참조조문·참조판례를 조회(전문 제외). 결과 0건이면 첫 번째 키워드만으로 자동 재시도하므로,
  핵심 키워드를 맨 앞에 두고 최대 2개로 주는 것이 좋다.
- `search_procurement_interpretations`: 조달청 계약법규 해석사례(법제처 OPEN API `ppsCgmExpc`로
  적재한 864건, 2022~)를 BM25(keywords)+Dense(query) RRF로 검색. 안건명+질의요지+회답+이유+
  관련법령 본문 전체가 인덱싱 대상이라 제목에 없는 표현도 의미검색된다. 인증키는 법제처 OC
  (`LAW_OC_DEFAULT`) 재사용. 과거(2014~2021)분은 API 미제공.

## 파일 지도

| 파일 | 역할 |
|---|---|
| `src/server.py` | FastMCP 앱, 미들웨어, `search_law`·`search_lh_regulations`·`search_construction_standards`·`search_precedents`·`search_procurement_interpretations` 툴 |
| `src/config.py` | 환경변수 (pydantic-settings) |
| `src/context.py` | `law_oc_var` — 요청별 법제처 API 키 격리 (contextvars) |
| `src/sources/law_normalize.py` | `NON_LAW_NAME_RE`·`strip_non_law_keywords`·`score_law_relevance` — 비법령명어 제거·정확매칭 점수 |
| `src/sources/law_api.py` | aiSearch(법령+행정규칙조문) + 키워드 병렬검색(법령·행정규칙) 정확매칭재정렬·3블록 반환 |
| `src/sources/lh_vector.py` | LH 규정 BM25(keywords)+Dense(query) 하이브리드, RRF 결합 |
| `src/sources/kcsc_vector.py` | KCSC 건설기준 BM25+Dense 하이브리드 + 인용 그래프 1-hop 확장 |
| `src/sources/prec_api.py` | 법제처 판례(prec) 검색 + 상위 N건 요지(판시사항·판결요지·참조조문·참조판례) 조회 |
| `src/sources/pps_vector.py` | 조달청 해석사례 BM25(keywords)+Dense(query) 하이브리드, RRF 결합 |
| `src/sources/base.py` | `SearchResult`, `SearchSource` 추상 클래스 |
| `crawler/lh_crawler.py` | RSS 파싱 + LH 사이트 크롤링 + 파일 다운로드 |
| `crawler/pdf_converter.py` | PDF → 마크다운 변환 (docling, 표 구조 + OCR) |
| `crawler/indexer.py` | LH 규정 BM25 증분 동기화 (title 기본키, pubDate 비교) |
| `crawler/bm25_index.py` | BM25 인덱스 빌드·저장·로드·검색 (`base_path`로 LH/KCSC 분리) |
| `crawler/dense_index.py` | Dense 임베딩 인덱스 빌드·저장·로드·증분 갱신 |
| `crawler/kcsc_api.py` | KCSC Open API 클라이언트, HTML→텍스트 변환, 인용 추출(2-pass 정규식) |
| `crawler/kcsc_indexer.py` | KCSC 크롤(JSON 캐시) + BM25/Dense/그래프 빌드 |
| `crawler/pps_api.py` | 조달청 해석사례 클라이언트 (법제처 OPEN API `ppsCgmExpc`, 목록+본문 JSON) |
| `crawler/pps_indexer.py` | 조달청 해석사례 적재(JSON 캐시) + BM25/Dense 빌드 (데이터기준일시 증분) |
| `crawler/rss_watcher.py` | 주기적 RSS 감시 데몬 |
| `scripts/build_index.py` | LH 규정 인덱스 빌드 엔트리포인트 (`--limit N` 옵션) |
| `scripts/build_kcsc_index.py` | KCSC 인덱스 빌드 엔트리포인트 (`--type`, `--limit`, `--from-cache`, `--force`) |
| `scripts/build_pps_index.py` | 조달청 해석사례 인덱스 빌드 엔트리포인트 (`--limit`, `--from-cache`, `--force`) |

## 새 소스 추가

1. `src/sources/` 에 `SearchSource` 상속 클래스 작성 (`search(query, keywords)` 구현)
2. `src/server.py` `_sources` 딕셔너리에 인스턴스 추가
3. `src/server.py` 에 `@mcp.tool()` 함수 추가 (`_search_single(source_id, query, keywords)` 호출)
   - 단일 파라미터 툴은 `_search_single`을 우회해 직접 호출 가능 (예: `search_precedents`는 `keywords`만 받음)

## 테스트

MCP Inspector로 도구를 직접 호출해 확인한다:

```bash
npx @modelcontextprotocol/inspector python -m src.server
```

배포 서버 대상 엔드투엔드 테스트는 `https://lh-rag-mcp.fly.dev/mcp?law_oc=<키>` 에
streamable-http(JSON-RPC)로 `initialize` → `tools/list` → `tools/call` 순으로 호출한다.

## 환경변수 (.env)

```ini
LAW_OC_DEFAULT=...       # 법제처 API 기본 키 (없으면 ?law_oc= URL 파라미터 필수)
MCP_API_KEY=...          # Bearer 인증 (비워두면 인증 없음)
LH_RSS_URL=https://www.lh.or.kr/rss/board.es?mid=a10108020000&bid=0055
TORCH_DEVICE=mps         # mps | cuda | cpu
BM25_PATH=./data/lh_regulation
MARKDOWN_PATH=./data/lh_regulation/markdown
KCSC_API_KEY=...         # 국가건설기준센터 Open API 인증키
KCSC_DATA_PATH=./data/kcsc
DEEPINFRA_API_KEY=...    # Dense 임베딩 (미설정 시 BM25만 사용)
```

## 데이터 경로

```
data/
  lh_regulation/
    lh_regulations.pkl        ← LH 규정 BM25 인덱스
    lh_regulations_dense.pkl  ← LH 규정 Dense 인덱스
    markdown/                 ← docling 변환 캐시: {YYMMDD}_{title_key}.md
  kcsc/
    cache/                    ← KCSC API 응답 캐시: {YYYYMMDD}_{doc_key}.json
    kcsc_standards.pkl        ← KCSC BM25 인덱스
    kcsc_standards_dense.pkl  ← KCSC Dense 인덱스
    kcsc_standards_graph.pkl  ← 인용 그래프 (노드·엣지·청크 매핑)
  pps/
    cache/                    ← 조달청 해석사례 캐시: {YYYYMMDD}_{법령해석일련번호}.json
    pps_interpretations.pkl       ← 조달청 해석사례 BM25 인덱스
    pps_interpretations_dense.pkl ← 조달청 해석사례 Dense 인덱스
docs/  ← 구현 전략, 참고할 지식
```

KCSC 청킹: lv1/lv2 헤더를 경계로 하위 섹션(lv3/lv4)을 합산. 60자 미만 그룹은 다음 그룹에 병합.
KCSC 증분: `cache/` 파일명 날짜(`YYYYMMDD`) ↔ API `updateDate` 비교로 변경분만 크롤.

인덱스 초기화:
```bash
# LH 규정
rm -f data/lh_regulation/*.pkl
python scripts/build_index.py

# KCSC 건설기준 (캐시 포함 전체 초기화)
rm -rf data/kcsc/
python scripts/build_kcsc_index.py

# KCSC 인덱스만 재빌드 (캐시 유지)
rm -f data/kcsc/*.pkl
python scripts/build_kcsc_index.py --from-cache

# 조달청 해석사례 (캐시 포함 전체 초기화)
rm -rf data/pps/
python scripts/build_pps_index.py

# 조달청 해석사례 인덱스만 재빌드 (캐시 유지)
rm -f data/pps/*.pkl
python scripts/build_pps_index.py --from-cache
```

## PDF 변환 (docling 사용 이유)

LH 규정 PDF는 HWP→PDF 변환 파일로, 단어 공백이 글리프가 아닌 좌표 이동(`Tm` 연산자)으로 표현된다. MuPDF 계열 파서는 공백 없는 텍스트를 반환한다. **marker로 되돌리지 말 것** (문서당 ~8분, 전체 35시간).

docling(pdfium 백엔드)은 bbox 간격 분석으로 공백 복원, 표를 마크다운 테이블로 구조화, 스캔 페이지 자동 OCR 적용. 문서당 ~3~16초.

OCR 엔진: `ocrmac`(macOS, 한국어 우수) → RapidOCR(폴백). `pip install ocrmac` 권장.

## 인덱싱 스킵 규칙

제목이 아래 패턴으로 끝나는 RSS 항목은 인덱싱하지 않는다:
`예고` / `안내문` / `일부개정` / `전부개정` / `개정안` / `개정(안)` / `개정 시행` / `규정 제N호`

## 알려진 제약

- **cold start**: fly.io 머신 일시정지 후 첫 요청 ~20초 (이후 warm ~2초)
- **LH 사이트 SSL**: 인증서 체인 문제 → `verify=False` 처리 중
- **HWP**: LibreOffice 미설치 시 건너뜀
- **스캔 PDF**: `ocrmac` 미설치 시 텍스트 추출 실패 → 해당 문서 스킵
