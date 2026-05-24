# CLAUDE.md — LH RAG MCP 프로젝트 컨텍스트

LH(한국토지주택공사) 임직원용 법령·규정 RAG MCP 서버. Claude Desktop에 연결해 사용한다.

## 빠른 실행

```bash
# 가상환경 활성화 (항상 먼저)
source .venv/bin/activate   # 또는 uv sync

# OCR 품질 향상 (최초 1회, 선택)
pip install ocrmac           # macOS Vision 기반 — RapidOCR보다 한국어 인식률 높음

# 인덱스 빌드 (처음 한 번, MPS 기준 ~20~30분)
python scripts/build_index.py

# 테스트용 (N건만 처리)
python scripts/build_index.py --limit 5

# MCP 서버 실행
python -m src.server        # 또는 lh-rag-mcp
```

## 아키텍처 한 줄 요약

```
질문 → LLM 라우터(소스 선택 + 키워드) → 법제처 API | LH 벡터DB 병렬검색 → 결과 통합
LH 벡터DB: Dense(ChromaDB) + Sparse(BM25/kiwipiepy) → RRF 융합
```

## 파일 지도

| 파일 | 역할 |
|---|---|
| `src/server.py` | FastMCP 앱, 미들웨어, `search_lh_knowledge` 툴 정의 |
| `src/router.py` | OpenAI 호환 LLM으로 소스 선택 + 키워드 추출 (JSON mode, 1회 호출) |
| `src/config.py` | 환경변수 설정. **`SOURCE_REGISTRY`** — 새 소스 추가 시 여기에 등록 |
| `src/context.py` | `law_oc_var: ContextVar` — 요청별 법제처 API 키 격리 |
| `src/sources/law_api.py` | 법제처 API (AI검색 → 일반검색 fallback) |
| `src/sources/lh_vector.py` | **하이브리드 검색**: Dense(ChromaDB) + BM25 → RRF 융합 |
| `src/sources/base.py` | `BaseSource` 추상 클래스 |
| `crawler/lh_crawler.py` | RSS 파싱 + LH 사이트 크롤링 + 파일 다운로드 |
| `crawler/pdf_converter.py` | PDF 추출: docling (표 구조 + OCR, threading.Lock으로 스레드 세이프) |
| `crawler/indexer.py` | ChromaDB 증분 동기화 (title 기본키, pubDate 비교) + BM25 재빌드 |
| `crawler/bm25_index.py` | BM25 인덱스 빌드·저장·로드·검색 (kiwipiepy 형태소 분석) |
| `crawler/rss_watcher.py` | 주기적 RSS 감시 데몬 |

## 핵심 설계 결정

### 1. 라우터 패턴
소스가 늘어날수록 무조건 병렬 검색하면 낭비다. 경량 LLM이 소스 선택 + 소스별 키워드를 **한 번에** 결정한다. `SOURCE_REGISTRY`에 소스 설명을 추가하면 라우터가 자동으로 라우팅을 학습한다.

### 2. 법제처 API 키를 URL 파라미터로 받는 이유
임직원마다 자신의 키를 `?law_oc=KEY`로 전달한다. `LawOcMiddleware`가 이를 `law_oc_var` (contextvars)에 저장해 요청별 격리한다. 서버 공용 기본값은 `LAW_OC_DEFAULT`.

### 3. 새 소스 추가 방법 (3단계)
1. `src/sources/` 에 `BaseSource` 상속 클래스 작성
2. `src/config.py` `SOURCE_REGISTRY`에 `"source_id": "설명"` 등록
3. `src/server.py` `_sources` 딕셔너리에 인스턴스 추가

### 4. 하이브리드 검색 (Dense + BM25 RRF)
- **Dense**: ChromaDB + `jhgan/ko-sroberta-multitask` 코사인 유사도 (의미 검색)
- **Sparse**: BM25Okapi + kiwipiepy 형태소 분석 (조문 번호·정확 키워드 검색)
- **융합**: Reciprocal Rank Fusion (k=60) — 각 20개 후보 → 최종 5개
- BM25 인덱스는 `data/bm25/lh_regulations.pkl`에 저장, 동기화 완료 시 자동 재빌드
- BM25 인덱스 없으면 Dense 전용으로 자동 폴백

### 5. title_to_key 정규화
`title_to_key()`는 공백·특수문자를 **제거**(언더스코어 치환 아님)한다.
- `"경영심의회 운영규정"` → `"경영심의회운영규정"` (동일 키)
- `"경영심의회운영규정"` → `"경영심의회운영규정"` (동일 키)
- LH가 규정 개정 시 제목 표기를 미세하게 변경해도 중복 저장 방지

### 6. 인덱싱 제외 필터 (`_should_skip`)
다음 패턴으로 끝나는 RSS 항목은 인덱싱하지 않는다:
- `예고`, `안내문` — 개정 예고·안내 게시물
- `일부개정`, `전부개정`, `개정안`, `개정(안)`, `개정 시행` — 개정 공고
- `규정 제N호` — 규정 번호가 제목에 붙은 버전

## PDF 변환: 가장 중요한 발견

### 배경 (왜 marker를 쓰지 않는가)
LH 규정은 HWP→PDF 변환 파일이다. 단어 간 공백이 글리프가 아닌 **절대 좌표 이동(`Tm` 연산자)**으로 표현된다.

marker는 내부적으로 pdftext(pdfium)로 읽은 span들을 join할 때 공백이 3개씩 붙고(`'취업규칙을   다음과'`), surya의 `OCRErrorPredictor`가 이를 스캔 파손 패턴으로 판정 → **전체 OCR 파이프라인 구동 → 문서당 ~8분, 266개 = 35시간**.

### 현재 방식: docling (단일)
`docling`은 pdfium 백엔드(`pypdfium2`)를 사용해 한국어 공백을 올바르게 복원하고, 표 구조를 마크다운 테이블로 출력하며, 스캔 페이지는 자동 OCR 적용한다.

| 방식 | 속도/문서 | 전체 (MPS) | 표 구조 | 스캔 PDF |
|---|---|---|---|---|
| marker (구) | ~8분 | ~35시간 | ✅ | ✅ |
| pdftext (중간) | 0.3초 | ~1분 | ❌ | ❌ |
| **docling (현재)** | **~3~16초** | **~20~90분** | **✅** | **✅** |

**marker로 되돌리지 말 것.** docling도 pdfium을 쓰므로 한국어 공백 문제가 없다.

### OCR 엔진 선택
- `ocrmac` 설치 시 자동 선택 (macOS Vision, 한국어 인식률 우수, MPS 가속)
- 미설치 시 RapidOCR (PP-OCRv4, 중국어 중심 → 한국어 인식률 낮아 경고 빈발)
- 구버전 스캔 PDF 대응을 위해 `do_ocr=True` 유지 필수

```bash
pip install ocrmac  # 최초 1회
```

## 데이터 디렉토리 구조

```
data/
  markdown/   ← docling 변환 결과 캐시: {YYMMDD}_{title_key}.md
  bm25/       ← BM25 인덱스: lh_regulations.pkl
  chroma/     ← Dense 임베딩 + 메타데이터 (ChromaDB)
```

### 마크다운 캐시 (`data/markdown/`)
- 파일명: `260428_취업규칙.md` (YYMMDD + 정규화 제목)
- 동일 규정이 업데이트되면 구 파일 삭제 후 새 날짜로 저장
- 변환 품질 확인·디버깅 시 직접 열람 가능
- **주의**: `rm data/markdown/*.md` 하면 재인덱싱 시 docling이 다시 모든 PDF를 변환해야 함

### 인덱스 초기화 방법
```bash
# ChromaDB만 초기화 (마크다운 캐시 보존)
python -c "
import sys; sys.path.insert(0, '.')
from dotenv import load_dotenv; load_dotenv()
import chromadb; from src.config import settings
chromadb.PersistentClient(path=settings.chroma_path).delete_collection(settings.chroma_collection)
"
rm -f data/bm25/*.pkl
```

## ChromaDB 인덱스 구조

- **기본키**: `title_key`(정규화 제목) — 동일 규정이 갱신되면 청크 전체 교체
- **업데이트 조건**: RSS `pubDate` > 저장된 `pub_date` 메타데이터
- **청킹 우선순위**: `## ` 헤딩(조문) → `# ` 헤딩(장) → 빈줄(단락) → 고정 800자
- **청크 ID 형식**: `{title_key}__c{idx:04d}` (예: `취업규칙__c0003`)
- **임베딩**: `jhgan/ko-sroberta-multitask`, cosine 유사도

## 인덱싱 동작 흐름

```
1. RSS 수신 → 전체 항목 스캔 (사전 스캔)
   └─ 신규 N건 / 업데이트 N건 / 스킵 N건 / 제외 N건 출력

2. tqdm 진행바로 처리 대상 문서 순차 처리
   각 문서: 크롤 → PDF 다운로드 → docling 변환 → markdown 저장 → ChromaDB upsert

3. 전체 완료 후 BM25 인덱스 1회 재빌드
```

중단 시 ChromaDB는 처리된 문서까지 보존, 재실행 시 스킵하고 나머지만 처리.

## LH 크롤러 주의사항

- **SSL**: LH 사이트 인증서 체인 문제 → `httpx.AsyncClient(verify=False)` 처리 중 (RSS + 다운로드 모두)
- **다운로드 URL**: `boardDownload.es?bid=...&list_no=...&seq=1` 형태 (확장자 없음)
- **파일 형식 판별**: Content-Disposition 헤더의 `filename` 파싱 → `_guess_suffix()`
- **미리보기 제외**: `attachApiPreview.es` URL은 HTML 뷰어이므로 "preview" 포함 URL 스킵
- **구버전 PDF 일부 스캔본**: RSS에는 최신 디지털 PDF와 구 스캔 PDF가 섞여 있음 → `ocrmac` 설치 권장

## 환경변수 (.env)

```ini
ROUTER_BASE_URL=https://api.deepinfra.com/v1/openai  # OpenAI 호환이면 어디든
ROUTER_API_KEY=...
ROUTER_MODEL=Qwen/Qwen2.5-7B-Instruct

LAW_OC_DEFAULT=...        # 법제처 API 키 (없으면 사용자가 URL 파라미터로)
MCP_API_KEY=...           # Bearer 인증 (비워두면 인증 없음)

LH_RSS_URL=https://www.lh.or.kr/rss/board.es?mid=a10108020000&bid=0055
TORCH_DEVICE=mps          # mps | cuda | cpu — docling 레이아웃/테이블 모델 가속
EMBEDDING_MODEL=jhgan/ko-sroberta-multitask
CHROMA_PATH=./data/chroma
MARKDOWN_PATH=./data/markdown
```

## 알려진 제약

- **docling 초기 로딩**: 레이아웃·테이블 모델 첫 로드 시 수십 초 소요 (이후 싱글턴 재사용)
- **docling 속도**: MPS 기준 문서당 ~3~16초. 대용량 PDF(1MB+)는 최대 수분 소요
- **HWP 파일**: LibreOffice 미설치 시 건너뜀
- **SSL**: LH 사이트 인증서 체인 문제 → `httpx.AsyncClient(verify=False)` 처리 중
- **구버전 스캔 PDF**: `ocrmac` 미설치 시 텍스트 추출 실패 가능 (실패 시 해당 문서 스킵)
