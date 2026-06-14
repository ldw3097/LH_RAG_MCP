"""로컬 stdio MCP 서버 — Claude Code mcpServers 등록용.

HTTP 미들웨어(인증·law_oc) 없이 순수 stdio 모드로 실행한다.
law_oc_var는 contextvar default("")이므로 law_api.py의
`law_oc_var.get() or settings.law_oc_default` 분기가 .env 값을 사용한다.
"""
import logging
logging.basicConfig(level=logging.WARNING)

from src.server import mcp, _sources  # noqa: F401 — tool decorators 이미 등록됨

if __name__ == "__main__":
    logger = logging.getLogger(__name__)
    logger.warning("BM25 인덱스 사전 로딩 시작...")
    _sources["lh_vector_db"]._ensure_loaded()
    _sources["kcsc_vector_db"]._ensure_loaded()
    _sources["pps_vector_db"]._ensure_loaded()
    logger.warning("BM25 인덱스 사전 로딩 완료 — stdio 모드 시작")
    mcp.run()  # stdio transport
