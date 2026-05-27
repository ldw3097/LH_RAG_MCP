# CLAUDE.md — LH RAG MCP 프로젝트 컨텍스트

LH(한국토지주택공사) 임직원용 법령·규정 RAG MCP 서버. Claude Desktop/Web에 연결해 사용한다.
배포 주소: `https://lh-rag-mcp.fly.dev/mcp`

## 빠른 실행

```bash
source .venv/bin/activate       # 또는 uv sync
python scripts/build_index.py   # 인덱스 빌드 (최초 1회, ~20~30분)
python -m src.server            # MCP 서버 실행 (또는 lh-rag-mcp)
```

## 아키텍처

```
질문 → 법제처 AI 검색 API (7개)  ─┐
     → LH 규정 BM25 검색 (7개)  ─┴→ 이어붙이기 → Claude
```

- 라우터 없음. 모든 소스를 항상 병렬 검색한다.
- 두 소스는 문서 집합이 겹치지 않으므로 RRF 불필요, 단순 concat.

## 파일 지도

| 파일 | 역할 |
|---|---|
| `src/server.py` | FastMCP 앱, 미들웨어, `search_law` / `search_lh_regulations` 툴 |
| `src/config.py` | 환경변수 (pydantic-settings) |
| `src/context.py` | `law_oc_var` — 요청별 법제처 API 키 격리 (contextvars) |
| `src/sources/law_api.py` | 법제처 AI검색 → 일반검색 fallback, 결과 7개 |
| `src/sources/lh_vector.py` | BM25(kiwipiepy) 검색, 결과 7개 |
| `src/sources/base.py` | `SearchResult`, `SearchSource` 추상 클래스 |
| `crawler/lh_crawler.py` | RSS 파싱 + LH 사이트 크롤링 + 파일 다운로드 |
| `crawler/pdf_converter.py` | PDF → 마크다운 변환 (docling, 표 구조 + OCR) |
| `crawler/indexer.py` | BM25 증분 동기화 (title 기본키, pubDate 비교) |
| `crawler/bm25_index.py` | BM25 인덱스 빌드·저장·로드·검색 |
| `crawler/rss_watcher.py` | 주기적 RSS 감시 데몬 |
| `scripts/build_index.py` | 인덱스 빌드 엔트리포인트 (`--limit N` 옵션) |

## 새 소스 추가

1. `src/sources/` 에 `SearchSource` 상속 클래스 작성
2. `src/server.py` 에 `@mcp.tool()` 데코레이터로 새 툴 함수 추가

## 환경변수 (.env)

```ini
LAW_OC_DEFAULT=...   # 법제처 API 기본 키 (없으면 ?law_oc= URL 파라미터 필수)
MCP_API_KEY=...      # Bearer 인증 (비워두면 인증 없음)
LH_RSS_URL=https://www.lh.or.kr/rss/board.es?mid=a10108020000&bid=0055
TORCH_DEVICE=mps     # mps | cuda | cpu
BM25_PATH=./data/bm25
MARKDOWN_PATH=./data/markdown
```

## 데이터 경로

```
data/
  markdown/  ← docling 변환 캐시: {YYMMDD}_{title_key}.md
  bm25/      ← BM25 인덱스: lh_regulations.pkl
```

인덱스 초기화:
```bash
rm -f data/bm25/*.pkl
python scripts/build_index.py
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
