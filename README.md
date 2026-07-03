# 🏛️ LH RAG MCP 서버

> 한국토지주택공사 임직원이 AI와 대화할 때 **법령·내부규정·건설기준·판례·조달청 해석사례**를 자동으로 검색해주는 MCP 서버

---

## 📡 검색 소스

AI와 대화할 때 질문 성격에 맞는 소스를 **자동으로 선택**하여 답변 근거를 제공합니다.


| 소스               | 내용                      | 출처           |
| ---------------- | ----------------------- | --------------- |
| 🏛️ **국가법령정보센터** | 법률, 시행령, 시행규칙, 판례, 행정규칙 | [국가법령정보센터](https://www.law.go.kr/main.html)   |
| 📋 **LH 규정**  | 공개된 LH 규정, 시행세칙     | [LH 홈페이지 게시판](https://www.lh.or.kr/board.es?mid=a10108020000&bid=0055) |
| 🏗️ **건설기준(KCSC)** | KDS 설계기준, KCS 표준시방서, LHCS LH 전문시방서 | [국가건설기준센터](https://www.kcsc.re.kr) |
| 📜 **조달청 해석사례** | 국가계약법규 유권해석(질의요지·회답·이유) | [조달청 해석사례](https://www.pps.go.kr/kor/petition/selectPetitionOpenList.do?key=00049) |
| 🦺 **건설안전 사고통계** | 건설안전 사고사례 3.7만건(2019~2025) — 공종·작업단계·시설물별 사고유형 통계 | [국토안전관리원 CSI](https://www.csi.go.kr) |


---

## 🚀 사용법 (Claude 웹)

### Claude 웹에서 MCP 서버 연결하기

1. [claude.ai](https://claude.ai) 에 로그인합니다.
2. 좌측 사이드바 → **사용자 지정** 탭을 누릅니다.
3. **커넥터** 탭 → `+` 버튼 → **커스텀 커넥터 추가**를 누릅니다.
4. 아래 내용을 입력합니다.


| 항목      | 값                                    |
| ------- | ------------------------------------ |
| **이름**  | `lh-rag` (원하는 이름 OK)                 |
| **URL** | `https://lh-rag-mcp.fly.dev/mcp`     |


5. **추가** 버튼을 누르면 등록 완료!

> ⏱️ **첫 번째 질문은 응답이 느릴 수 있습니다** (약 20초). 서버가 잠시 대기 상태였다가 깨어나는 시간입니다. 두 번째 질문부터는 약 2~3초 내에 응답합니다.

---

## 🤖 GPTs Actions 연결

Custom GPT 공개용으로 GPTs Actions REST API도 함께 제공합니다.

| 항목 | 값 |
| --- | --- |
| **Actions API Base URL** | `https://lh-rag-mcp.fly.dev` |
| **OpenAPI 스키마** | [`docs/gpts-actions-openapi.yaml`](docs/gpts-actions-openapi.yaml) |
| **GPT Instructions 초안** | [`docs/gpts-instructions.md`](docs/gpts-instructions.md) |
| **개인정보 처리방침** | [`docs/privacy.md`](docs/privacy.md) |
| **프로필 이미지** | [`docs/lh-rag-gpt-profile.png`](docs/lh-rag-gpt-profile.png) |
| **인증** | 현재 공개 배포는 인증 없음 |

GPT Builder의 **Configure → Actions**에서 `docs/gpts-actions-openapi.yaml` 내용을 붙여넣으면 아래 REST 엔드포인트를 호출합니다.

```text
POST /actions/search_law
POST /actions/search_lh_regulations
POST /actions/search_construction_standards
POST /actions/search_precedents
POST /actions/search_procurement_interpretations
POST /actions/assess_construction_risk
POST /actions/get_law_article
POST /actions/get_admrul_article
```

> 공개 GPT의 Privacy Policy URL에는 GitHub에 push된 `docs/privacy.md`의 공개 URL을 사용하세요.

---

## 💬 예시 질문

### 🏛️ 국가법령·행정규칙 관련 예시 (search_law)
- 공공주택 특별법에 따른 영구임대주택 입주 자격 중 자산 보유 기준이 어떻게 되나요?
- 토지보상법 시행규칙에서 규정하는 농업 손실보상금 산정 방식과 지급 대상에 대해 알려주세요.
- 공공주택 특별법 시행령에 명시된 임대료 인상률 상한선(5%)에 대한 예외 조항이 있나요?

### 📋 LH 내부규정·지침 관련 예시 (search_lh_regulations)
- LH 직원이 국내 출장 시 직급별 일비와 숙박비 지급 한도액은 얼마인가요?
- LH 임대주택 관리 업무 수행 시 세대 내 시설물 보수 책임(본인 부담 vs 공사 부담) 구분표를 찾아주세요.
- LH 취업규칙상 직원이 근무시간을 변경하거나 원격지에서 근무할 수 있는 요건은 무엇인가요?

### 🏗️ 건설기준(KDS·KCS·LHCS) 관련 예시 (search_construction_standards)
- 지반설계기준에 따른 옹벽 구조물의 토압 산정 시 적용해야 할 안전율은?
- 아파트 지하주차장 바닥 에폭시 도장 공사 시 기온 및 습도 제한 조건이 무엇인가요?
- 내진 설계 기준상 중요도 '특' 등급 건축물에 적용되는 지진구역 계수와 응답수정계수 산정 방식은?

### ⚖️ 법원 판례 예시 (search_precedents)
- 토지수용보상금 증액 소송에서 사업인정고시일 이후 설치한 지장물에 대해 보상 의무가 없다고 본 대법원 판례를 찾아주세요.
- 이주대책대상자 선정 시 '거주 요건'을 충족하지 못한 경우에도 예외적으로 보상 자격을 인정한 사례가 있나요?
- 지방자치단체의 도시계획시설 결정 실효(일몰제) 이후 토지 소유자의 손실보상 청구권 인정 여부에 대한 판례가 궁금합니다.

### 📜 조달청 계약법규 해석사례 예시 (search_procurement_interpretations)
- 공사 중 자재 가격이 급등했는데 물가변동으로 계약금액 조정을 받을 수 있나요?
- 낙찰자가 계약 체결을 거부할 경우 입찰보증금 국고귀속 처리 기준이 어떻게 되나요?
- 하도급 업체가 부도난 경우 원도급자의 하도급대금 직불 의무와 발주기관의 책임 범위는 어떻게 되나요?

### 🦺 건설안전 사고통계 예시 (assess_construction_risk)
- 공동주택 현장에서 철근콘크리트 타설작업을 할 때 어떤 유형의 사고를 주의해야 하나요?
- 교량 가설공사 설치작업의 과거 사고유형 분포가 일반 작업 대비 어떻게 다른가요?
- 굴착작업 시 어떤 인적사고가 평균보다 두드러지나요?

---

## 🔐 보안 유의사항

- MCP 또는 GPTs Actions 툴 호출이 발생하면 **AI가 요약한 질의가 개발자의 서버(`lh-rag-mcp.fly.dev`)로 전송됩니다.**
- MCP 서버는 임베딩값 추출을 위해 요약된 질의를 오픈소스 LLM 서빙 서비스인 `deepinfra`로 전송합니다.
- 응답이 완료되면 모든 입력값은 개발자의 서버에서 즉시 삭제됩니다.
- **기밀 정보나 개인정보가 포함된 질문을 MCP 서버로 넘기지 않도록 주의하세요.**
- **이 MCP 툴을 사용하지 않을경우, Claude 커넥터 설정에서 '차단됨'으로 변경하는것을 권장드립니다.**
- 공개 GPT 개인정보 처리방침은 [`docs/privacy.md`](docs/privacy.md)를 참고하세요.

---

## 🛠️ 사용 가능한 도구

질문을 하면 Claude가 내용에 맞는 도구를 **자동으로 골라** 검색합니다. 도구를 따로 지정할 필요는 없습니다.


| 도구 | 검색 대상 | 이런 질문에 |
| --- | --- | --- |
| 🏛️ `search_law` | 국가법령정보센터 법령 · 국토교통부 행정규칙 | "공공임대주택 임대료 인상 상한은?" |
| 📋 `search_lh_regulations` | 공개된 LH 규정 · 시행세칙 | "해외 출장 일비·숙박비 지급 기준은?" |
| 🏗️ `search_construction_standards` | 건설기준 KDS · KCS · LHCS (설계기준 · 시방서) | "옹벽 설계 시 토압 산정 방법은?" |
| ⚖️ `search_precedents` | 법원 판례 (판시사항 · 판결요지 · 참조조문) | "토지수용 보상금 증액 판례 있어?" |
| 📜 `search_procurement_interpretations` | 조달청 계약법규 유권해석 사례 (본문 의미검색) | "물가변동으로 계약금액 조정 가능한가?" |
| 🦺 `assess_construction_risk` | 건설안전 사고통계 (공종·작업단계·시설물별 사고유형 분포 + 기준선 대비 증감) | "공동주택 철근콘크리트 타설 작업의 위험 사고유형은?" |
| 📄 `get_law_article` | 법령의 특정 조문 전문 (조 단위 전체 항·호·목) | "방금 그 법 제3조 전문 보여줘" |
| 📄 `get_admrul_article` | 행정규칙의 특정 조문 전문 | "그 고시 제5조 원문 알려줘" |

> 💡 `get_law_article` · `get_admrul_article`는 `search_law` 결과의 목차에서 원하는 조문을 확인한 뒤,
> 해당 조문의 **전문을 그대로** 가져오는 후속 조회 도구입니다. Claude가 목차를 보고 자동으로 이어서 호출합니다.


## 📊 유용성 평가 결과

MCP 연결 시 AI 답변 정확도가 **55% → 94%** 로 향상되고 응답 속도는 2배 빨라지는 것을 500개 실무 질문으로 검증했습니다.

→ [전체 평가 보고서 보기](https://htmlpreview.github.io/?https://raw.githubusercontent.com/ldw3097/LH_RAG_MCP/main/docs/full_test_report.html)

---

## 🖥️ 개발 참고사항
개발과 관련된 내용은 CLAUDE.md, docs 폴더를 참고해주세요.
