import logging

from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.config import settings
from src.context import law_oc_var
from src.sources.law_api import LawApiSource
from src.sources.lh_vector import LHVectorSource
from src.sources.kcsc_vector import KCSCVectorSource
from src.sources.prec_api import PrecedentSource
from src.sources.pps_vector import PpsVectorSource

SOURCE_LABELS = {
    "law_api": "국가법령정보센터",
    "lh_vector_db": "LH 규정",
    "kcsc_vector_db": "건설기준(KDS/KCS/LHCS)",
    "prec": "법원 판례",
    "pps_vector_db": "조달청 해석사례",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="LH RAG MCP",
    instructions=(
        "LH 임직원 업무 지원 서버입니다. "
        "대한민국 법령·행정규칙은 search_law 도구로, "
        "LH 사내 규정(인사·보수·직제·감사·보안·문서·업무 등 임직원 적용 규정·규칙·시행세칙)은 "
        "search_lh_regulations 도구로 검색하세요. "
        "법원 판례는 search_precedents 도구로 키워드 검색하세요. "
        "건설기준(KDS 설계기준·KCS 표준시방서·LHCS LH 전문시방서)은 "
        "search_construction_standards 도구로 검색하세요. "
        "조달청 계약법규 해석사례(국가계약법규 유권해석)는 "
        "search_procurement_interpretations 도구로 검색하세요. "
        "검색 도구는 자연어 질의(query)와 핵심 키워드(keywords)를 함께 전달하세요. "
        "질문이 여러 영역에 걸쳐 있으면 해당 도구들을 모두 호출하세요. "
        "답변 생성시 반드시 출처를 명시하세요."
    ),
)

_sources = {
    "law_api": LawApiSource(),
    "lh_vector_db": LHVectorSource(),
    "kcsc_vector_db": KCSCVectorSource(),
    "prec": PrecedentSource(),
    "pps_vector_db": PpsVectorSource(),
}


async def _search_single(source_id: str, query: str, keywords: str) -> str:
    """단일 소스를 검색해 포맷된 결과 문자열을 반환합니다."""
    logger.info("검색 요청 [%s]: query=%s | keywords=%s", source_id, query, keywords)
    try:
        results = await _sources[source_id].search(query, keywords)
    except Exception as e:
        logger.error("소스 %s 검색 오류: %s", source_id, e)
        return "검색 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."

    if not results:
        return "관련 정보를 찾지 못했습니다. 다른 키워드로 다시 질문해 주세요."

    label = SOURCE_LABELS.get(source_id, source_id)
    logger.info("검색 완료 [%s]: %d개 결과", source_id, len(results))
    lines = [f"검색어: {query}", f"키워드: {keywords}", f"검색 소스: {label}", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] [{label}] {r.to_text()}")
    return "\n".join(lines)


@mcp.tool()
async def search_law(query: str, keywords: str) -> str:
    """
    국가법령정보센터에서 법령(법률·시행령·시행규칙) 조문과 행정규칙(고시·훈령·예규)을 검색합니다.

    대한민국 법령과 국토교통부 행정규칙에 관한 질문에 사용하세요. LH 내규가 아닌
    국가 차원의 법령·규칙이 필요할 때 적합합니다.

    Args:
        query: 자연어로 요약한 질의 (예: "전세 보증금을 못 돌려받을 때 임차인 보호").
        keywords: 핵심 키워드를 공백으로 구분 (예: "주택임대차보호법 보증금 우선변제").
    """
    return await _search_single("law_api", query, keywords)


@mcp.tool()
async def search_lh_regulations(query: str, keywords: str) -> str:
    """
    LH(한국토지주택공사) 사내 규정을 검색합니다.

    인사·보수·채용·교육·직제(조직)·감사·보안·문서/기록물/정보화/데이터 관리·물품·
    소송·경영·개발사업 등 LH 임직원에게 적용되는 내부 규정·규칙·시행세칙·취업규칙을
    검색합니다. 사내 업무 절차·복무·조직 운영에 관한 질문에 사용하세요.
    (국가 법령이 아닌 LH 자체 내규이며, 임대주택 신청 등 대외 정책 안내가 아닙니다.)

    Args:
        query: 자연어로 요약한 질의.
        keywords: 핵심 키워드를 공백으로 구분.
    """
    return await _search_single("lh_vector_db", query, keywords)


_CATEGORY_MAP: dict[str, set[str] | None] = {
    "design":       {"KDS"},
    "construction": {"KCS", "LHCS"},
    "all":          None,
}


@mcp.tool()
async def search_construction_standards(query: str, keywords: str, category: str = "all") -> str:
    """
    건설기준(KDS·KCS·LHCS)을 검색합니다.

    KDS(설계기준)·KCS(표준시방서)·LHCS(LH 전문시방서) 등 건설 설계·시공 기준을
    검색합니다. 구조·지반·토목·건축·시공 방법, 재료·품질 기준, 설계 하중·안전율 등
    기술적 건설기준에 관한 질문에 사용하세요. (국가 법령이나 LH 사내 행정규정이 아닌
    건설 기술기준입니다.)

    검색 결과는 해당 조문이 인용하는 다른 기준의 조문(예: KDS가 참조하는 KCS)을
    인용 그래프로 1-hop 확장해 함께 반환합니다.

    Args:
        query: 자연어로 요약한 질의 (예: "옹벽 설계 시 토압 산정 방법").
        keywords: 핵심 키워드를 공백으로 구분 (예: "옹벽 토압 안정성 설계").
        category: 검색 범위.
            "design"       — KDS(설계기준)만. 구조 계산·하중·안전율·설계 공식 등 설계 단계 질문.
            "construction" — KCS(표준시방서)·LHCS(LH 전문시방서)만. 공법·재료·품질관리·시공 절차 질문.
            "all"          — 전체 검색 (기본값). 설계·시공 경계가 불분명하거나 둘 다 필요한 경우.
    """
    code_types = _CATEGORY_MAP.get(category)
    logger.info("검색 요청 [kcsc_vector_db]: query=%s | keywords=%s | category=%s", query, keywords, category)
    try:
        results = await _sources["kcsc_vector_db"].search(query, keywords, code_types=code_types)
    except Exception as e:
        logger.error("소스 kcsc_vector_db 검색 오류: %s", e)
        return "검색 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    if not results:
        return "관련 정보를 찾지 못했습니다. 다른 키워드로 다시 질문해 주세요."
    label = SOURCE_LABELS["kcsc_vector_db"]
    logger.info("검색 완료 [kcsc_vector_db]: %d개 결과", len(results))
    lines = [f"검색어: {query}", f"키워드: {keywords}", f"검색 소스: {label}", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] [{label}] {r.to_text()}")
    return "\n".join(lines)


@mcp.tool()
async def search_precedents(keywords: str) -> str:
    """
    법원 판례를 키워드로 검색합니다. 판시사항·판결요지·참조조문 중심으로 반환합니다.

    키워드는 공백 구분 AND 매칭입니다. 가장 중요한 핵심 키워드를 맨 앞에 두고
    최대 2개만 사용하세요. 결과가 없으면 자동으로 첫 번째 키워드만으로 재검색합니다.
    search_law 결과의 법령명·조문번호를 포함하면 해당 법령을 인용한 판례를 찾을 수 있습니다.

    Args:
        keywords: 핵심 키워드 최대 2개를 공백으로 구분. 가장 중요한 키워드를 맨 앞에.
            첫 번째 키워드는 검색 실패 시 단독 재검색에 쓰이므로 구체적으로 선택.
            (예: "토지수용 보상", "임대차 해지", "주택임대차보호법 제3조")
    """
    logger.info("검색 요청 [prec]: keywords=%s", keywords)
    try:
        results = await _sources["prec"].search("", keywords)
    except Exception as e:
        logger.error("소스 prec 검색 오류: %s", e)
        return "검색 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    if not results:
        return "관련 판례를 찾지 못했습니다. 다른 키워드로 다시 질문해 주세요."
    label = SOURCE_LABELS["prec"]
    logger.info("검색 완료 [prec]: %d개 결과", len(results))
    lines = [f"키워드: {keywords}", f"검색 소스: {label}", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] [{label}] {r.to_text()}")
    return "\n".join(lines)


@mcp.tool()
async def search_procurement_interpretations(query: str, keywords: str) -> str:
    """
    조달청 계약법규 해석사례(유권해석)를 검색합니다.

    국가계약법·지방계약법·계약예규 등 국가계약법규에 대한 조달청의 유권해석 사례를
    질의요지·회답 본문까지 의미검색해 반환합니다. 입찰·낙찰자 선정, 계약 체결·관리,
    물가변동/설계변경에 따른 계약금액 조정, 지체상금, 하자담보 등 조달 업무 관련 질문에 사용하세요.

    Args:
        query: 자연어로 요약한 질의 (예: "물가가 올라 계약금액을 조정받을 수 있는지").
        keywords: 핵심 키워드를 공백으로 구분 (예: "물가변동 계약금액 조정").
    """
    return await _search_single("pps_vector_db", query, keywords)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Bearer 토큰으로 MCP 서버 접근을 제한합니다."""

    async def dispatch(self, request: Request, call_next):
        if not settings.mcp_api_key:
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {settings.mcp_api_key}":
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


class LawOcMiddleware(BaseHTTPMiddleware):
    """URL 파라미터 law_oc를 요청 컨텍스트에 주입합니다.

    사용자는 MCP 서버 URL에 자신의 법제처 API 키를 포함합니다:
        https://your-server.com/mcp?law_oc=YOUR_KEY
    """

    async def dispatch(self, request: Request, call_next):
        law_oc = request.query_params.get("law_oc", settings.law_oc_default)
        token = law_oc_var.set(law_oc)
        try:
            return await call_next(request)
        finally:
            law_oc_var.reset(token)


def main():
    if not settings.law_oc_default:
        logger.warning(
            "LAW_OC_DEFAULT 미설정 — 법령 API는 요청 URL의 ?law_oc= 파라미터가 필수입니다."
        )

    # 첫 요청 전 BM25 인덱스·kiwipiepy 사전 로딩 (ML 모델 없음 — 수초 이내)
    logger.info("BM25 인덱스 사전 로딩 시작...")
    _sources["lh_vector_db"]._ensure_loaded()
    _sources["kcsc_vector_db"]._ensure_loaded()
    _sources["pps_vector_db"]._ensure_loaded()
    logger.info("BM25 인덱스 사전 로딩 완료")

    app = mcp.http_app(transport="streamable-http")
    # 미들웨어는 역순으로 실행되므로 LawOc → ApiKey 순으로 등록
    app.add_middleware(LawOcMiddleware)
    app.add_middleware(ApiKeyMiddleware)

    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
