# 🏛️ LH RAG MCP 서버

> LH(한국토지주택공사) 임직원이 AI와 대화할 때 **법령**과 **내부 규정**을 자동으로 검색해주는 MCP 서버


---

## 📡 검색 소스

AI와 대화할 때 아래 두 가지 소스를 **동시에 병렬 검색**하여 답변 근거를 제공합니다.

| 소스 | 내용 | 검색 방식 |
|---|---|---|
| 🏛️ **국가법령정보센터** | 법률, 시행령, 시행규칙, 판례, 행정규칙 | 법제처 AI 검색 API |
| 📋 **LH 내부 규정** | 업무지침, 시행세칙, 내규, 사규 | BM25 + 형태소 분석 |

---

## 🚀 사용법 (Claude 웹)

### 1단계 — 법제처 API 키 발급

1. [법제처 Open API 신청 페이지](https://open.law.go.kr/LSO/openApi/guideList.do)에 접속합니다.
2. 회원가입 후 로그인합니다.
3. **"Open API 사용 신청"** 버튼을 누릅니다.
4. 신청서를 작성하면 **인증키(OC)** 가 발급됩니다.
5. 이 인증키를 아래 설정에서 사용합니다.

### 2단계 — Claude 웹에서 MCP 서버 연결하기

1. [claude.ai](https://claude.ai) 에 로그인합니다.
2. 좌측 사이드바 → **사용자 지정** 탭을 누릅니다.
3. **커넥터** 탭 → `+` 버튼 → **커스텀 커넥터 추가**를 누릅니다.
4. 아래 내용을 입력합니다.

| 항목 | 값 |
|---|---|
| **이름** | `lh-rag` (원하는 이름 OK) |
| **URL** | 아래 주소에서 `법제처키` 부분을 본인 인증키로 교체 |

```
https://lh-rag-mcp.fly.dev/mcp?law_oc=법제처키
```

5. **추가** 버튼을 누르면 등록 완료!

6. 아래 예시 질문으로 테스트해 보세요:

> 💬 *"수용 예정인 토지의 공부상 소유주가 수십 년 전 사망한 조상으로 되어 있어 상속인들 간에 합의가 안 되고 있습니다. LH에서 보상금을 법원에 공탁한다고 하는데, 나중에 이 공탁금을 찾으려면 어떤 법적 절차를 거쳐야 하나요?"*

> ⏱️ **첫 번째 질문은 응답이 느릴 수 있습니다** (약 20초). 서버가 잠시 대기 상태였다가 깨어나는 시간입니다. 두 번째 질문부터는 약 2~3초 내에 응답합니다.

---

## 🗂️ 코드 구조

### 프로젝트 파일 구성

```
src/
├── server.py           # FastMCP 앱, 미들웨어, search_lh_knowledge 툴 정의
├── config.py           # 환경변수 설정 (pydantic-settings)
├── context.py          # law_oc 키 요청별 격리 (contextvars)
└── sources/
    ├── base.py         # SearchResult, SearchSource 추상 클래스
    ├── law_api.py      # 법제처 AI 검색 API (일반검색 fallback 포함)
    └── lh_vector.py    # LH 규정 BM25 검색 (kiwipiepy 형태소 분석)

crawler/
├── lh_crawler.py       # RSS 파싱 + 페이지 크롤링 + 파일 다운로드
├── pdf_converter.py    # PDF → 마크다운 변환 (docling, 표 구조 + OCR)
├── indexer.py          # BM25 인덱스 증분 동기화
├── bm25_index.py       # BM25 인덱스 빌드·저장·로드·검색
└── rss_watcher.py      # 주기적 RSS 감시 데몬

scripts/
└── build_index.py      # LH 규정 인덱스 빌드 엔트리포인트

data/
├── markdown/           # docling 변환 캐시: {YYMMDD}_{title}.md
└── bm25/               # BM25 인덱스: lh_regulations.pkl
```

### 🔍 검색 흐름

```
search_lh_knowledge(query)
    │
    ├─ 🏛️  LawApiSource.search(query)
    │       └─ 법제처 AI검색 API (실패 시 일반검색 fallback)
    │
    └─ 📋  LHVectorSource.search(query)
            └─ BM25(kiwipiepy) → pkl 파일에서 로드
    │
    ▼
결과 이어붙이기 (law_api 7개 + lh_vector_db 7개)
    │
    ▼
🤖 Claude에 텍스트로 반환
```

### 🔐 법제처 API 키 격리

사용자마다 자신의 법제처 API 키를 MCP 서버 URL 파라미터로 전달합니다.

```
https://lh-rag-mcp.fly.dev/mcp?law_oc=USER_KEY
```

`LawOcMiddleware`가 이를 수신하여 `law_oc_var` (Python contextvars)에 저장합니다.  
동시에 여러 사용자가 요청해도 각 요청이 **독립적인 API 키**를 사용합니다.  
서버 공용 기본값은 `LAW_OC_DEFAULT` 환경변수로 설정합니다.

### ➕ 새 검색 소스 추가

1. `src/sources/base.py`의 `SearchSource`를 상속하여 `search()` 메서드 구현
2. `src/server.py`의 `_sources` 딕셔너리에 인스턴스 추가

---

## ⚙️ 환경변수

| 변수 | 설명 | 기본값 |
|---|---|---|
| `LAW_OC_DEFAULT` | 법제처 API 기본 키 | (없음, URL 파라미터 필수) |
| `MCP_API_KEY` | MCP 서버 Bearer 인증 키 (미사용중) | (없음, 인증 생략) |
| `LH_RSS_URL` | LH 규정 RSS 주소 | (없음) |
| `BM25_PATH` | BM25 인덱스 저장 경로 | `./data/bm25` |
| `MARKDOWN_PATH` | 마크다운 캐시 경로 | `./data/markdown` |

---

## ⚠️ 알려진 제약

| 항목 | 내용 |
|---|---|
| 🥶 **Cold Start** | fly.io 머신이 대기 상태일 때 첫 요청에 약 20초 소요. 이후 warm 상태에서는 약 2초 |
| 🔒 **LH 사이트 SSL** | LH 웹사이트 SSL 인증서 체인 검증 실패로 `verify=False` 처리 중 |
