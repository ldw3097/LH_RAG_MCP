from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


ActionHandler = Callable[..., Awaitable[str]]


class BadRequest(ValueError):
    pass


async def _read_json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as e:
        raise BadRequest("Request body must be valid JSON.") from e
    if not isinstance(body, dict):
        raise BadRequest("Request body must be a JSON object.")
    return body


def _require_str(body: dict[str, Any], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value.strip():
        raise BadRequest(f"Missing required field: {field}")
    return value.strip()


def _optional_str(body: dict[str, Any], field: str, default: str = "") -> str:
    value = body.get(field, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise BadRequest(f"Field must be a string: {field}")
    return value.strip()


def _json_result(text: str) -> JSONResponse:
    return JSONResponse({"result": text})


def _json_error(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=400)


def _wrap_action(call: Callable[[Request], Awaitable[JSONResponse]]) -> Callable[[Request], Awaitable[JSONResponse]]:
    async def endpoint(request: Request) -> JSONResponse:
        try:
            return await call(request)
        except BadRequest as e:
            return _json_error(str(e))

    return endpoint


def register_action_routes(app, handlers: dict[str, ActionHandler]) -> None:
    """Register REST endpoints for GPTs Actions.

    The handlers are the existing MCP tool functions from server.py. This keeps
    REST as a thin JSON wrapper and avoids duplicating search logic.
    """

    async def action_search_law(request: Request) -> JSONResponse:
        body = await _read_json(request)
        result = await handlers["search_law"](
            query=_require_str(body, "query"),
            keywords=_require_str(body, "keywords"),
        )
        return _json_result(result)

    async def action_search_lh_regulations(request: Request) -> JSONResponse:
        body = await _read_json(request)
        result = await handlers["search_lh_regulations"](
            query=_require_str(body, "query"),
            keywords=_require_str(body, "keywords"),
        )
        return _json_result(result)

    async def action_search_construction_standards(request: Request) -> JSONResponse:
        body = await _read_json(request)
        category = _optional_str(body, "category", "all")
        if category not in {"all", "design", "construction"}:
            raise BadRequest("Field category must be one of: all, design, construction")
        result = await handlers["search_construction_standards"](
            query=_require_str(body, "query"),
            keywords=_require_str(body, "keywords"),
            category=category,
        )
        return _json_result(result)

    async def action_search_precedents(request: Request) -> JSONResponse:
        body = await _read_json(request)
        law_name = _optional_str(body, "law_name")
        keywords = _optional_str(body, "keywords")
        if not law_name and not keywords:
            raise BadRequest("One of law_name or keywords is required.")
        result = await handlers["search_precedents"](
            query=_require_str(body, "query"),
            law_name=law_name,
            keywords=keywords,
        )
        return _json_result(result)

    async def action_search_procurement_interpretations(request: Request) -> JSONResponse:
        body = await _read_json(request)
        result = await handlers["search_procurement_interpretations"](
            query=_require_str(body, "query"),
            keywords=_require_str(body, "keywords"),
        )
        return _json_result(result)

    async def action_assess_construction_risk(request: Request) -> JSONResponse:
        body = await _read_json(request)
        result = await handlers["assess_construction_risk"](
            work_process=_require_str(body, "work_process"),
            work_subtype=_optional_str(body, "work_subtype"),
            facility_subtype=_optional_str(body, "facility_subtype"),
        )
        return _json_result(result)

    async def action_get_law_article(request: Request) -> JSONResponse:
        body = await _read_json(request)
        result = await handlers["get_law_article"](
            law_name=_require_str(body, "law_name"),
            article=_require_str(body, "article"),
        )
        return _json_result(result)

    async def action_get_admrul_article(request: Request) -> JSONResponse:
        body = await _read_json(request)
        result = await handlers["get_admrul_article"](
            admrul_name=_require_str(body, "admrul_name"),
            article=_require_str(body, "article"),
            ministry=_optional_str(body, "ministry"),
        )
        return _json_result(result)

    routes = [
        Route("/actions/search_law", _wrap_action(action_search_law), methods=["POST"]),
        Route(
            "/actions/search_lh_regulations",
            _wrap_action(action_search_lh_regulations),
            methods=["POST"],
        ),
        Route(
            "/actions/search_construction_standards",
            _wrap_action(action_search_construction_standards),
            methods=["POST"],
        ),
        Route("/actions/search_precedents", _wrap_action(action_search_precedents), methods=["POST"]),
        Route(
            "/actions/search_procurement_interpretations",
            _wrap_action(action_search_procurement_interpretations),
            methods=["POST"],
        ),
        Route(
            "/actions/assess_construction_risk",
            _wrap_action(action_assess_construction_risk),
            methods=["POST"],
        ),
        Route("/actions/get_law_article", _wrap_action(action_get_law_article), methods=["POST"]),
        Route(
            "/actions/get_admrul_article",
            _wrap_action(action_get_admrul_article),
            methods=["POST"],
        ),
    ]
    app.routes.extend(routes)
