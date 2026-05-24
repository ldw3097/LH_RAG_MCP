import json
import logging
from dataclasses import dataclass

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import SOURCE_REGISTRY, settings

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=settings.router_api_key or "no-key",
            base_url=settings.router_base_url,
        )
    return _client


@dataclass
class RouterResult:
    sources: list[str]
    keywords: dict[str, str]  # source_id -> 검색 키워드


def _build_system_prompt() -> str:
    source_list = "\n".join(
        f"- {sid}: {desc}" for sid, desc in SOURCE_REGISTRY.items()
    )
    return f"""당신은 LH(한국토지주택공사) 업무 지원 시스템의 검색 라우터입니다.
사용자 질문을 분석하여 검색할 소스와 각 소스에 맞는 검색 키워드를 결정합니다.

사용 가능한 검색 소스:
{source_list}

다음 JSON 형식으로만 응답하세요:
{{
  "sources": ["소스ID", ...],
  "keywords": {{
    "소스ID": "해당 소스에 최적화된 검색어"
  }}
}}

규칙:
- 질문이 법령/판례/법적 근거에 관한 것이면 law_api 선택
- 질문이 LH 내부 절차/기준/지침에 관한 것이면 lh_vector_db 선택
- 두 소스 모두 관련될 수 있으면 둘 다 선택 (불확실할 때도 둘 다 선택)
- law_api 키워드: 법령 용어 중심 (예: "임대주택법 보증금 반환 기간")
- lh_vector_db 키워드: 업무 절차 중심 (예: "전세임대 보증금 반환 처리 절차")"""


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
async def _call_router_llm(query: str) -> RouterResult:
    response = await _get_client().chat.completions.create(
        model=settings.router_model,
        messages=[
            {"role": "system", "content": _build_system_prompt()},
            {"role": "user", "content": query},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
        max_tokens=256,
    )
    raw = response.choices[0].message.content
    data = json.loads(raw)
    sources = [s for s in data.get("sources", []) if s in SOURCE_REGISTRY]
    keywords = {
        sid: kw
        for sid, kw in data.get("keywords", {}).items()
        if sid in sources
    }
    # 소스에 키워드가 없으면 원본 질문으로 채움
    for sid in sources:
        if sid not in keywords:
            keywords[sid] = query
    return RouterResult(sources=sources, keywords=keywords)


def _fallback(query: str) -> RouterResult:
    """라우터 실패 시 모든 소스를 원본 질문으로 검색."""
    return RouterResult(
        sources=list(SOURCE_REGISTRY.keys()),
        keywords={sid: query for sid in SOURCE_REGISTRY},
    )


async def route(query: str) -> RouterResult:
    try:
        result = await _call_router_llm(query)
        if not result.sources:
            logger.warning("라우터가 소스를 반환하지 않아 fallback 실행")
            return _fallback(query)
        return result
    except Exception as e:
        logger.error("라우터 LLM 오류, fallback 실행: %s", e)
        return _fallback(query)
