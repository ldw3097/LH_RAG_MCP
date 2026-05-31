"""법령 검색어 정규화 유틸 — korean-law-mcp/src/lib/law-search.ts 로직 포팅."""

import re

# 법령명이 아닌 부가어 제거 정규식.
# TS의 NON_LAW_NAME_RE 포팅: Python \b는 한글 단어경계를 인식 못하므로
# 단어 앞뒤 공백(\s) 경계를 사용. 문자열 앞뒤는 (?:^|\s)/(?:\s|$)로 처리.
NON_LAW_NAME_RE = re.compile(
    r"(?:(?<=\s)|(?<=^))"
    r"(?:과태료|절차|비용|처벌|기준|허가|신청|부과|근거|위반|요건|"
    r"조건|처분|수수료|신고|등록|면허|인가|승인|취소|정지|벌칙|벌금|과징금|"
    r"이행강제금|시정명령|체계|구조|판례|해석|개정|별표|시행령|시행규칙|서식|"
    r"수입|수출|통관|반환|납부|감면|면제|제한|금지|의무|권리|자격|종류|기간|"
    r"대상|범위|적용|감경|영향|분석|위임|현황|변화|처리|민원|업무|담당|"
    r"저촉|검증|파급|불복|소송|쟁송)"
    r"(?=\s|$)",
    re.MULTILINE,
)


def strip_non_law_keywords(query: str) -> str:
    """법령명이 아닌 부가어를 제거해 이름 검색에 적합한 키워드로 정리한다."""
    result = NON_LAW_NAME_RE.sub("", query)
    result = re.sub(r"\s{2,}", " ", result).strip()
    return result if result else query


def score_law_relevance(law_name: str, query: str, query_words: list[str]) -> int:
    """후보 법령명에 정확매칭 점수를 부여한다 (높을수록 쿼리와 관련성 높음).

    korean-law-mcp/src/lib/law-search.ts scoreLawRelevance 포팅:
      +100  쿼리가 법령명을 포함 (쿼리 ⊇ 법령명)
      +80   법령명이 쿼리(공백 제거)를 포함 (법령명 ⊇ 쿼리)
      +10   각 쿼리 단어가 법령명에 매칭
      +5    시행령·시행규칙이 아닌 본법(모법) 우선
    """
    score = 0
    query_no_space = query.replace(" ", "")

    if query in law_name:
        score += 100
    if law_name in query:
        score += 100
    if law_name.replace(" ", "") in query_no_space:
        score += 80

    for w in query_words:
        if w and w in law_name:
            score += 10

    if not re.search(r"시행령|시행규칙", law_name):
        score += 5

    return score
