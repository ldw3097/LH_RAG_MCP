import logging
from urllib.parse import urlencode

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import settings
from src.context import law_oc_var
from src.sources.base import SearchResult, SearchSource

logger = logging.getLogger(__name__)

LAW_API_BASE = "https://www.law.go.kr/DRF"
LAW_PORTAL_BASE = "https://www.law.go.kr"

# 소스별 후보 수 (server.py에서 재랭킹 후 최종 10개로 축소)
CANDIDATE_K = 15


class LawApiSource(SearchSource):
    source_id = "law_api"

    def __init__(self):
        self._client = httpx.AsyncClient(
            timeout=15.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            },
            # stale 커넥션 풀 재사용 실패(RST) 시 자동 재시도
            transport=httpx.AsyncHTTPTransport(retries=1),
        )

    async def search(self, query: str) -> list[SearchResult]:
        # AI 자연어검색 우선, 실패하면 일반검색 fallback
        try:
            results = await self._ai_search(query)
        except Exception as e:
            logger.warning("AI 법령검색 실패 (%s), 일반검색으로 전환", e)
            results = []
        if not results:
            results = await self._general_search(query)
        return results

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    async def _ai_search(self, query: str) -> list[SearchResult]:
        """법제처 AI 자연어법령검색 API."""
        params = {
            "OC": law_oc_var.get() or settings.law_oc_default,
            "target": "aiSearch",
            "query": query,
            "type": "JSON",
            "display": CANDIDATE_K,
        }
        url = f"{LAW_API_BASE}/lawSearch.do?{urlencode(params)}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        return self._parse_ai_results(data)

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    async def _general_search(self, query: str) -> list[SearchResult]:
        """법제처 일반법령검색 API."""
        params = {
            "OC": law_oc_var.get() or settings.law_oc_default,
            "target": "law",
            "query": query,
            "type": "JSON",
            "display": CANDIDATE_K,
            "page": 1,
            "sort": "score",
        }
        url = f"{LAW_API_BASE}/lawSearch.do?{urlencode(params)}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        return self._parse_general_results(data)

    async def _fetch_article(self, mst: str, query: str) -> str:
        """법령 조문 전문 조회 (검색 결과 보강용)."""
        params = {
            "OC": law_oc_var.get() or settings.law_oc_default,
            "target": "law",
            "MST": mst,
            "type": "JSON",
        }
        url = f"{LAW_API_BASE}/lawService.do?{urlencode(params)}"
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            data = resp.json()
            articles = data.get("법령", {}).get("조문", {}).get("조문단위", [])
            if isinstance(articles, dict):
                articles = [articles]
            # 쿼리 키워드가 포함된 조문만 발췌 (최대 2개)
            matched = []
            for art in articles:
                content = art.get("조문내용", "")
                if any(kw in content for kw in query.split()[:3]):
                    matched.append(
                        f"제{art.get('조문번호', '')}조 {art.get('조문제목', '')}\n{content}"
                    )
                if len(matched) >= 2:
                    break
            return "\n\n".join(matched) if matched else ""
        except Exception as e:
            logger.debug("조문 조회 실패 mst=%s: %s", mst, e)
            return ""

    def _parse_ai_results(self, data: dict) -> list[SearchResult]:
        items = data.get("aiSearch", {}).get("법령조문", [])
        if isinstance(items, dict):
            items = [items]
        results = []
        for item in items:
            law_name = item.get("법령명", "")
            mst = item.get("법령일련번호", "")
            article_no = item.get("조문번호", "").lstrip("0") or ""
            article_title = item.get("조문제목", "")
            content = item.get("조문내용", "")
            if not content:
                content = f"{law_name} 관련 조문"
            title_parts = [law_name]
            if article_no:
                title_parts.append(f"제{article_no}조")
                if article_title:
                    title_parts.append(f"({article_title})")
            title = " ".join(title_parts)
            url = f"{LAW_PORTAL_BASE}/법령/{law_name}" if law_name else ""
            results.append(SearchResult(
                source_id=self.source_id,
                title=title,
                content=content[:2000],
                url=url,
                metadata={"mst": mst, "law_name": law_name},
            ))
        return results

    def _parse_general_results(self, data: dict) -> list[SearchResult]:
        items = data.get("LawSearch", {}).get("law", [])
        if isinstance(items, dict):
            items = [items]
        results = []
        for item in items:
            law_name = item.get("법령명한글", "")
            mst = item.get("법령일련번호", "")
            dept = item.get("소관부처명", "")
            effective_date = item.get("시행일자", "")
            content = f"소관부처: {dept} | 시행일: {effective_date}"
            url = f"{LAW_PORTAL_BASE}/법령/{law_name}" if law_name else ""
            results.append(SearchResult(
                source_id=self.source_id,
                title=law_name,
                content=content,
                url=url,
                metadata={"mst": mst, "law_name": law_name},
            ))
        return results

    async def aclose(self):
        await self._client.aclose()
