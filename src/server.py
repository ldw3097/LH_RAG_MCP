import asyncio
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

FINAL_K = 10   # 재랭킹 후 Claude에게 넘길 최종 결과 수
_RRF_K = 60

SOURCE_LABELS = {
    "law_api": "국가법령정보센터",
    "lh_vector_db": "LH 규정",
}


def _rrf_rerank(source_results: dict[str, list[SearchResult]]) -> list[SearchResult]:
    """소스별 결과를 RRF로 합산해 FINAL_K개 반환.

    소스가 하나면 단순 슬라이스, 둘 이상이면 각 소스의 순위(position)를
    RRF 점수로 변환해 합산 후 정렬한다.
    """
    if len(source_results) == 1:
        return list(source_results.values())[0][:FINAL_K]

    scored: list[tuple[float, SearchResult]] = []
    for results in source_results.values():
        for rank, r in enumerate(results):
            scored.append((1.0 / (_RRF_K + rank + 1), r))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:FINAL_K]]

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

    # 모든 소스를 동시에 검색
    tasks = {sid: src.search(query) for sid, src in _sources.items()}
    search_results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    # 소스별 결과 수집
    source_results: dict[str, list] = {}
    for sid, result in zip(tasks.keys(), search_results):
        if isinstance(result, Exception):
            logger.error("소스 %s 검색 오류: %s", sid, result)
            continue
        if result:
            source_results[sid] = result

    if not source_results:
        return "관련 정보를 찾지 못했습니다. 다른 키워드로 다시 질문해 주세요."

    # 4. RRF 재랭킹 → 상위 FINAL_K개
    reranked = _rrf_rerank(source_results)
    logger.info("재랭킹 완료: %d개 결과 (소스: %s)", len(reranked), list(source_results.keys()))

    # 5. 출력 포맷
    lines = [f"검색어: {query}", f"검색 소스: {', '.join(source_results.keys())}", ""]
    for i, r in enumerate(reranked, 1):
        label = SOURCE_LABELS.get(r.source_id, r.source_id)
        lines.append(f"[{i}] [{label}] {r.to_text()}")
    return "\n".join(lines)


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
    logger.info("BM25 인덱스 사전 로딩 완료")

    app = mcp.http_app(transport="streamable-http")
    # 미들웨어는 역순으로 실행되므로 LawOc → ApiKey 순으로 등록
    app.add_middleware(LawOcMiddleware)
    app.add_middleware(ApiKeyMiddleware)

    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
