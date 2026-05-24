# CLAUDE.md — LH RAG MCP 프로젝트 컨텍스트

LH(한국토지주택공사) 임직원용 법령·규정 RAG MCP 서버. Claude Desktop에 연결해 사용한다.

## 빠른 실행

```bash
# 가상환경 활성화 (항상 먼저)
source .venv/bin/activate   # 또는 uv run

# 인덱스 빌드 (처음 한 번, ~10분)
python scripts/build_index.py

# MCP 서버 실행
python -m src.server       # 또는 lh-rag-mcp
```

## 아키텍처 한 줄 요약

```
질문 → LLM 라우터(소스 선택 + 키워드) → 법제처 API | ChromaDB 병렬검색 → 결과 통합
```

## 파일 지도

| 파일 | 역할 |
|---|---|
| `src/server.py` | FastMCP 앱, 미들웨어, `search_lh_knowledge` 툴 정의 |
| `src/router.py` | OpenAI 호환 LLM으로 소스 선택 + 키워드 추출 (JSON mode, 1회 호출) |
| `src/config.py` | 환경변수 설정. **`SOURCE_REGISTRY`** — 새 소스 추가 시 여기에 등록 |
| `src/context.py` | `law_oc_var: ContextVar` — 요청별 법제처 API 키 격리 |
| `src/sources/law_api.py` | 법제처 API (AI검색 → 일반검색 fallback) |
| `src/sources/lh_vector.py` | ChromaDB 코사인 유사도 검색 |
| `src/sources/base.py` | `BaseSource` 추상 클래스 |
| `crawler/lh_crawler.py` | RSS 파싱 + LH 사이트 크롤링 + 파일 다운로드 |
| `crawler/pdf_converter.py` | PDF 추출: pdftext 1차 / marker 폴백 |
| `crawler/indexer.py` | ChromaDB 증분 동기화 (title 기본키, pubDate 비교) |
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

## PDF 변환: 가장 중요한 발견

### 문제
LH 규정은 HWP→PDF 변환 파일이다. PDF 내부에서 단어 간 공백이 글리프가 아닌 **절대 좌표 이동(`Tm` 연산자)**으로 표현된다. 글리프 스트림에는 `취업규칙을다음과같이` 처럼 공백 없이 들어있다.

marker를 쓰면 내부적으로 pdftext로 읽은 텍스트를 span 단위로 join할 때 공백이 3개씩 생기고(`'취업규칙을   다음과'`), 이게 스캔 OCR 파손 패턴처럼 보여서 surya OCR 모델(`OCRErrorPredictor`)이 "bad" 판정 → **전체 OCR 파이프라인 구동 → 문서당 ~8분**.

### 해결
`pdftext`(pdfium 기반)를 직접 사용한다. pdfium은 인접 글자 bbox 간격을 분석해 공백을 올바르게 추론 → **0.3초, 공백 정상**.

```python
# crawler/pdf_converter.py
# 1차: pdftext (0.3초) — 페이지당 80자 이상이면 사용
# 폴백: marker OCR — 실제 스캔본일 때만
_MIN_CHARS_PER_PAGE = 80
```

**절대 marker를 1차로 되돌리지 말 것.** 266개 문서 기준 35시간 → 10분 차이다.

### pymupdf(MuPDF)와 pdftext(pdfium) 차이
- MuPDF: 글리프 스트림 순서대로 읽음, 좌표 gap 추론 안 함 → 공백 없는 텍스트
- pdfium: 인접 글자 bbox 간격 분석 → 공백 올바르게 복원

## ChromaDB 인덱스 구조

- **기본키**: `title`(규정 제목) — 동일 제목이 갱신되면 청크 전체 교체
- **업데이트 조건**: RSS `pubDate` > 저장된 `pub_date` 메타데이터
- **청킹 우선순위**: `## ` 헤딩(조문) → `# ` 헤딩(장) → 빈줄(단락) → 고정 800자
- **청크 ID 형식**: `{title_key}__c{idx:04d}` (예: `취업규칙__c0003`)
- **임베딩**: `jhgan/ko-sroberta-multitask`, cosine 유사도

## LH 크롤러 주의사항

- **SSL**: LH 사이트 인증서 체인 문제 → `httpx.AsyncClient(verify=False)` 처리 중
- **다운로드 URL**: `boardDownload.es?bid=...&list_no=...&seq=1` 형태 (확장자 없음)
- **파일 형식 판별**: Content-Disposition 헤더의 `filename` 파싱 → `_guess_suffix()`
- **미리보기 제외**: `attachApiPreview.es` URL은 HTML 뷰어이므로 "preview" 포함 URL 스킵

## 환경변수 (.env)

```ini
ROUTER_BASE_URL=https://api.deepinfra.com/v1/openai  # OpenAI 호환이면 어디든
ROUTER_API_KEY=...
ROUTER_MODEL=Qwen/Qwen2.5-7B-Instruct

LAW_OC_DEFAULT=...        # 법제처 API 키 (없으면 사용자가 URL 파라미터로)
MCP_API_KEY=...           # Bearer 인증 (비워두면 인증 없음)

LH_RSS_URL=https://www.lh.or.kr/rss/board.es?mid=a10108020000&bid=0055
TORCH_DEVICE=mps          # mps | cuda | cpu (marker 폴백 시에만 사용)
EMBEDDING_MODEL=jhgan/ko-sroberta-multitask
CHROMA_PATH=./data/chroma
```

## 알려진 제약

- `TableRecEncoderDecoderModel` (surya 테이블 인식)은 MPS 미지원 → marker 폴백 시 CPU 동작
- `marker/builders/line.py`의 `min_document_ocr_threshold = 0.85`는 정의만 있고 **실제로 사용되지 않는다** (dead config)
- HWP 파일: LibreOffice 미설치 시 건너뜀
