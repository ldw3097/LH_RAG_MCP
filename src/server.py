import logging

from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.config import settings
from src.context import law_oc_var
from src.sources.base import SearchResult
from src.sources.law_api import LawApiSource
from src.sources.lh_vector import LHVectorSource

SOURCE_LABELS = {
    "law_api": "국가법령정보센터",
    "lh_vector_db": "LH 규정",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="LH RAG MCP",
    instructions=(
        "LH 임직원 업무 지원 서버입니다. "
        "법률·시행령·판례 등 국가법령은 search_law 도구로, "
        "LH 내부 규정·지침은 search_lh_regulations 도구로 검색하세요."
    ),
)

_law_source = LawApiSource()
_lh_source = LHVectorSource()


def _format_results(query: str, source_label: str, results: list[SearchResult]) -> str:
    """검색 결과를 텍스트로 포맷합니다."""
    lines = [f"검색어: {query}", f"검색 소스: {source_label}", ""]
    for i, r in enumerate(results, 1):
        label = SOURCE_LABELS.get(r.source_id, r.source_id)
        lines.append(f"[{i}] [{label}] {r.to_text()}")
    return "\n".join(lines)


@mcp.tool()
async def search_law(query: str) -> str:
    """
    국가법령정보센터에서 법령·시행령·시행규칙·판례·행정규칙을 검색합니다.

    법률 조항, 시행령, 판례, 고시·훈령 등 국가 공식 법령 정보를 찾을 때 사용하세요.

    Args:
        query: 사용자의 요약된 질의 내용
    """
    logger.info("[법령 검색] %s", query)
    try:
        results = await _law_source.search(query)
    except Exception as e:
        logger.error("법령 검색 오류: %s", e)
        return "법령 검색 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."

    if not results:
        return "관련 법령을 찾지 못했습니다. 다른 키워드로 다시 질문해 주세요."

    logger.info("[법령 검색] 완료: %d개 결과", len(results))
    return _format_results(query, SOURCE_LABELS["law_api"], results)


@mcp.tool()
async def search_lh_regulations(query: str) -> str:
    """
    LH 내부 규정집(규정·시행세칙·지침)을 검색합니다.

    LH 사규, 업무 처리 기준, 내부 지침 등 LH 고유 규정 정보를 찾을 때 사용하세요.

    Args:
        query: 사용자의 요약된 질의 내용
    """
    logger.info("[LH 규정 검색] %s", query)
    try:
        results = await _lh_source.search(query)
    except Exception as e:
        logger.error("LH 규정 검색 오류: %s", e)
        return "LH 규정 검색 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."

    if not results:
        return "관련 LH 규정을 찾지 못했습니다. 다른 키워드로 다시 질문해 주세요."

    logger.info("[LH 규정 검색] 완료: %d개 결과", len(results))
    return _format_results(query, SOURCE_LABELS["lh_vector_db"], results)


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
    _lh_source._ensure_loaded()
    logger.info("BM25 인덱스 사전 로딩 완료")

    app = mcp.http_app(transport="streamable-http")
    # 미들웨어는 역순으로 실행되므로 LawOc → ApiKey 순으로 등록
    app.add_middleware(LawOcMiddleware)
    app.add_middleware(ApiKeyMiddleware)

    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
