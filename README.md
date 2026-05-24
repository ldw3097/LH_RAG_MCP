# LH RAG MCP 서버

LH(한국토지주택공사) 임직원이 Claude와 대화할 때 법령과 내부 규정을 자동으로 검색해주는 MCP 서버입니다.

---

## 1. 무엇을 해주는가

Claude에 연결하면 `search_lh_knowledge` 도구 하나로 두 가지 소스를 검색합니다.

| 소스 | 내용 | 방식 |
|---|---|---|
| **국가법령정보센터** | 법률, 시행령, 시행규칙, 판례, 행정규칙, 헌재결정례 | 실시간 API |
| **LH 내부 규정** | 업무지침, 시행세칙, 내규, 사규 | 로컬 벡터DB (하이브리드 검색) |

질문이 들어오면 경량 LLM(라우터)이 **어느 소스를 검색할지** + **각 소스에 맞는 키워드**를 한 번에 판단하고, 선택된 소스만 병렬로 검색합니다. 관련 없는 소스는 호출하지 않습니다.

```
사용자 질문
    └─▶ LLM 라우터 (소스 선택 + 키워드 추출)
            ├─▶ 법제처 API 검색          ─┐
            └─▶ LH 벡터DB 하이브리드 검색 ─┴─▶ 결과 통합 ─▶ Claude
                  Dense(ChromaDB) + BM25
                        └─ RRF 융합
```

---

## 2. 실행 방법

### 사전 요구사항

```bash
python 3.11+
uv  # 패키지 관리자 (pip install uv)
```

### 설치

```bash
git clone https://github.com/ldw3097/code.git
cd code
uv sync

# OCR 품질 향상 (선택, macOS 권장)
pip install ocrmac
```

### 환경 설정

```bash
cp .env.example .env
```

`.env` 파일을 열어 필수 항목을 채웁니다:

```ini
# LLM 라우터 — DeepInfra, OpenAI, 로컬 Ollama 어디든 가능
ROUTER_BASE_URL=https://api.deepinfra.com/v1/openai
ROUTER_API_KEY=your_key_here
ROUTER_MODEL=Qwen/Qwen2.5-7B-Instruct

# 법제처 API 키 (없으면 사용자가 URL 파라미터로 전달해야 함)
LAW_OC_DEFAULT=your_law_oc_key

# MCP 서버 인증 (공개 배포 시 필수, 비워두면 인증 없음)
MCP_API_KEY=

# LH 규정 RSS (인덱스 빌드에 사용)
LH_RSS_URL=https://www.lh.or.kr/rss/board.es?mid=a10108020000&bid=0055

# PDF 변환 가속 장치 (mps | cuda | cpu)
TORCH_DEVICE=mps
```

### LH 규정 인덱스 빌드

MCP 서버를 처음 실행하기 전에 LH 규정을 수집합니다. MPS 기준 약 20~30분 소요됩니다.

```bash
# 전체 빌드
python scripts/build_index.py

# 테스트 (일부만 처리)
python scripts/build_index.py --limit 5
```

실행하면 먼저 처리 계획을 출력하고 진행 상황을 표시합니다:

```
[인덱싱 계획]  전체 266건  →  신규 215건  업데이트 0건  스킵 51건  제외 11건

인덱싱:  45%|████▌     | 97/215 [18:30<22:10, 취업규칙 시행세칙]
```

이후 규정이 업데이트되면 다시 실행하면 변경된 문서만 갱신됩니다(증분 동기화).

### MCP 서버 실행

```bash
python scripts/build_index.py  # 또는 uv run lh-build-index
uv run lh-rag-mcp
```

기본 주소: `http://0.0.0.0:8000/mcp`

### Claude에 연결

Claude Desktop `claude_desktop_config.json` 에 추가:

```json
{
  "mcpServers": {
    "lh-rag": {
      "url": "http://localhost:8000/mcp?law_oc=YOUR_LAW_OC_KEY"
    }
  }
}
```

`MCP_API_KEY`를 설정한 경우 헤더 인증이 필요합니다:

```json
{
  "mcpServers": {
    "lh-rag": {
      "url": "http://your-server.com/mcp?law_oc=YOUR_LAW_OC_KEY",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_API_KEY"
      }
    }
  }
}
```

---

## 3. 개발자 참고 사항

### 프로젝트 구조

```
src/
├── server.py           # FastMCP 앱, 미들웨어, search_lh_knowledge 툴
├── router.py           # LLM 라우터 (소스 선택 + 키워드 추출)
├── config.py           # 환경변수 설정 (pydantic-settings)
├── context.py          # law_oc 요청별 격리 (contextvars)
└── sources/
    ├── law_api.py      # 법제처 API 검색
    └── lh_vector.py    # 하이브리드 검색 (Dense + BM25 RRF)

crawler/
├── lh_crawler.py       # RSS 파싱 + 페이지 크롤링 + 파일 다운로드
├── pdf_converter.py    # PDF 텍스트 추출 (docling — 표 구조 + OCR)
├── indexer.py          # ChromaDB 증분 동기화 + BM25 재빌드
├── bm25_index.py       # BM25 인덱스 빌드·저장·로드·검색 (kiwipiepy)
└── rss_watcher.py      # 주기적 RSS 감시 데몬

scripts/
└── build_index.py      # lh-build-index 엔트리포인트 (--limit 옵션 지원)

data/
├── markdown/           # docling 변환 캐시: {YYMMDD}_{title}.md
├── bm25/               # BM25 인덱스: lh_regulations.pkl
└── chroma/             # ChromaDB (Dense 임베딩)
```

### 하이브리드 검색 구조

```
쿼리
  ├─ Dense:  ChromaDB 코사인 유사도 → 상위 20개
  └─ Sparse: BM25(kiwipiepy 형태소) → 상위 20개
                    ↓
             RRF(k=60) 융합
                    ↓
              최종 상위 5개 반환
```

BM25 인덱스(`data/bm25/lh_regulations.pkl`)가 없으면 Dense 단독으로 자동 폴백합니다.

### 인덱싱 스킵 규칙

RSS 항목 중 다음 패턴으로 끝나는 제목은 인덱싱하지 않습니다:

- `예고`, `안내문` — 공지 게시물
- `일부개정`, `전부개정`, `개정안`, `개정(안)`, `개정 시행` — 개정 공고
- `규정 제N호` — 규정 번호 부기 버전

### 새 검색 소스 추가

1. `src/sources/base.py`의 `BaseSource`를 상속해 `search()` 구현
2. `src/config.py`의 `SOURCE_REGISTRY`에 소스 ID와 설명 등록
3. `src/server.py`의 `_sources` 딕셔너리에 인스턴스 추가

라우터 LLM이 `SOURCE_REGISTRY` 설명을 보고 자동으로 라우팅을 학습합니다.

### PDF 변환 전략

LH 규정 PDF는 HWP에서 변환된 파일입니다. 단어 사이 공백이 글리프가 아닌 좌표 이동(`Tm` 연산자)으로 표현되어 있어, MuPDF 계열 파서는 공백 없는 텍스트를 반환합니다.

**docling** (pdfium 백엔드)을 사용합니다. pdfium은 인접 글자 bbox 간격을 분석해 공백을 올바르게 복원하며, 표를 마크다운 테이블로 구조화하고, 스캔 페이지는 자동으로 OCR을 적용합니다.

OCR 엔진: `ocrmac`(macOS, 권장) → RapidOCR(폴백) 순으로 자동 선택.

### 법제처 API 키

사용자마다 자신의 API 키를 MCP 서버 URL에 파라미터로 전달합니다:

```
http://your-server.com/mcp?law_oc=USER_KEY
```

서버는 `law_oc_var` (contextvars)로 요청별로 키를 격리합니다. 서버 공용 키는 `LAW_OC_DEFAULT`로 설정할 수 있습니다. 법제처 API 키는 [국가법령정보 오픈API](https://open.law.go.kr) 에서 발급받습니다.

### 알려진 제약

- **LH 사이트 SSL**: LH 웹사이트의 SSL 인증서 체인이 pyenv Python에서 검증 실패하는 문제로 `httpx.AsyncClient(verify=False)` 처리 중입니다.
- **docling 초기 로딩**: 레이아웃·테이블 모델 최초 로드 시 수십 초 소요됩니다. 이후 실행은 싱글턴으로 재사용합니다.
- **HWP 파일**: LibreOffice가 설치된 경우 HWP/HWPX 파일도 텍스트 추출이 가능합니다. 미설치 시 HWP 파일은 건너뜁니다.
- **구버전 스캔 PDF**: RSS에 포함된 오래된 게시물 일부는 스캔 기반 PDF입니다. `ocrmac` 미설치 시 해당 문서는 텍스트 추출에 실패하고 스킵됩니다.
