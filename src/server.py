import asyncio
import logging

from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.config import settings
from src.context import law_oc_var
from src.router import route
from src.sources.law_api import LawApiSource
from src.sources.lh_vector import LHVectorSource

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="LH RAG MCP",
    instructions=(
        "LH 임직원 업무 지원 서버입니다. "
        "법령, LH 내부 규정·지침에 관한 질문을 search_lh_knowledge 도구로 검색하세요."
    ),
)

_sources = {
    "law_api": LawApiSource(),
    "lh_vector_db": LHVectorSource(),
}


@mcp.tool()
async def search_lh_knowledge(query: str) -> str:
    """
    LH 업무 관련 질문에 대해 관련 법령 및 규정을 검색합니다.

    국가법령정보센터(법령·시행령·판례·행정규칙)와 LH 내부 규정집을 검색하여
    관련 정보를 반환합니다.

    Args:
        query: 검색할 질문 또는 키워드 (자연어 가능)
    """
    logger.info("검색 요청: %s", query)

    # 1. 라우터: 소스 선정 + 소스별 키워드 추출 (단일 LLM 호출)
    routing = await route(query)
    logger.info("라우팅 결과: sources=%s", routing.sources)

    # 2. 선정된 소스만 병렬 검색
    tasks = {
        sid: _sources[sid].search(routing.keywords[sid])
        for sid in routing.sources
        if sid in _sources
    }
    search_results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    # 3. 결과 통합
    sections = []
    for sid, result in zip(tasks.keys(), search_results):
        if isinstance(result, Exception):
            logger.error("소스 %s 검색 오류: %s", sid, result)
            continue
        if not result:
            continue
        source_label = {
            "law_api": "국가법령정보센터",
            "lh_vector_db": "LH 규정",
        }.get(sid, sid)
        section_lines = [f"=== {source_label} ==="]
        for i, r in enumerate(result, 1):
            section_lines.append(f"\n[{i}] {r.to_text()}")
        sections.append("\n".join(section_lines))

    if not sections:
        return "관련 정보를 찾지 못했습니다. 다른 키워드로 다시 질문해 주세요."

    header = f"검색어: {query}\n검색 소스: {', '.join(routing.sources)}\n"
    return header + "\n\n".join(sections)


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
    if not settings.router_api_key:
        logger.warning("ROUTER_API_KEY 미설정 — 라우터가 fallback(전체검색)으로 동작합니다.")

    app = mcp.http_app(transport="streamable-http")
    # 미들웨어는 역순으로 실행되므로 LawOc → ApiKey 순으로 등록
    app.add_middleware(LawOcMiddleware)
    app.add_middleware(ApiKeyMiddleware)

    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
