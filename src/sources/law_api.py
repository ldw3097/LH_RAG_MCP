import asyncio
import logging
import re
from urllib.parse import urlencode

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import settings
from src.context import law_oc_var
from src.sources.base import SearchResult, SearchSource

logger = logging.getLogger(__name__)

LAW_API_BASE = "https://www.law.go.kr/DRF"
LAW_PORTAL_BASE = "https://www.law.go.kr"

CANDIDATE_K = 7
ADMRUL_DETAIL_K = 3          # 본문까지 가져올 행정규칙 상위 건수
ADMRUL_ORG_MOLIT = "1613000"  # 국토교통부 소관부처코드 (행정규칙 필터)

_IMG_TAG_RE = re.compile(r"<img[^>]*>(?:</img>)?", re.IGNORECASE)


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

    async def search(self, query: str, keywords: str) -> list[SearchResult]:
        # 법령(자연어 AI검색)과 행정규칙(키워드 검색)을 병렬 실행 후 이어붙임
        law_res, admrul_res = await asyncio.gather(
            self._search_law(query, keywords),
            self._search_admrul(keywords),
            return_exceptions=True,
        )
        results: list[SearchResult] = []
        if isinstance(law_res, list):
            results += law_res
        else:
            logger.warning("법령 검색 실패: %s", law_res)
        if isinstance(admrul_res, list):
            results += admrul_res
        else:
            logger.warning("행정규칙 검색 실패: %s", admrul_res)
        return results

    async def _search_law(self, query: str, keywords: str) -> list[SearchResult]:
        """법령 검색: AI 자연어검색 우선, 실패하면 키워드 일반검색 fallback."""
        try:
            results = await self._ai_search(query)
        except Exception as e:
            logger.warning("AI 법령검색 실패 (%s), 일반검색으로 전환", e)
            results = []
        if not results:
            results = await self._general_search(keywords)
        return results

    async def _search_admrul(self, keywords: str) -> list[SearchResult]:
        """국토교통부 행정규칙 검색 (키워드) 후 상위 N건 본문까지 조회."""
        items = await self._admrul_search(keywords)
        if not items:
            return []
        contents = await asyncio.gather(
            *(self._fetch_admrul_content(it["id"]) for it in items),
            return_exceptions=True,
        )
        results = []
        for it, content in zip(items, contents):
            if isinstance(content, Exception):
                logger.debug("행정규칙 본문 조회 실패 id=%s: %s", it["id"], content)
                content = ""
            kind = it.get("kind", "")
            title = f"{it['name']} ({kind})" if kind else it["name"]
            body = content or f"소관부처: {it.get('dept', '')} | 시행일: {it.get('date', '')}"
            results.append(SearchResult(
                source_id=self.source_id,
                title=title,
                content=body[:2000],
                url=it.get("url", ""),
                metadata={"admrul_id": it["id"], "kind": kind, "type": "admrul"},
            ))
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
    async def _general_search(self, keywords: str) -> list[SearchResult]:
        """법제처 일반법령검색 API (키워드)."""
        params = {
            "OC": law_oc_var.get() or settings.law_oc_default,
            "target": "law",
            "query": keywords,
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

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    async def _admrul_search(self, keywords: str) -> list[dict]:
        """법제처 행정규칙 검색 API (키워드, 국토교통부 한정). 메타데이터 목록 반환."""
        params = {
            "OC": law_oc_var.get() or settings.law_oc_default,
            "target": "admrul",
            "query": keywords,
            "org": ADMRUL_ORG_MOLIT,
            "type": "JSON",
            "display": ADMRUL_DETAIL_K,
        }
        url = f"{LAW_API_BASE}/lawSearch.do?{urlencode(params)}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        return self._parse_admrul_list(data)

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    async def _fetch_admrul_content(self, admrul_id: str) -> str:
        """행정규칙 본문(조문내용) 조회."""
        params = {
            "OC": law_oc_var.get() or settings.law_oc_default,
            "target": "admrul",
            "ID": admrul_id,
            "type": "JSON",
        }
        url = f"{LAW_API_BASE}/lawService.do?{urlencode(params)}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        body = data.get("AdmRulService", {}).get("조문내용", "")
        if isinstance(body, list):
            parts = []
            for item in body:
                if isinstance(item, dict):
                    parts.append(item.get("조문내용", "") or str(item))
                else:
                    parts.append(str(item))
            body = "\n".join(p for p in parts if p)
        return _IMG_TAG_RE.sub("", body or "").strip()

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

    def _parse_admrul_list(self, data: dict) -> list[dict]:
        items = data.get("AdmRulSearch", {}).get("admrul", [])
        if isinstance(items, dict):
            items = [items]
        parsed = []
        for item in items:
            admrul_id = item.get("행정규칙일련번호", "")
            if not admrul_id:
                continue
            name = item.get("행정규칙명", "")
            link = item.get("행정규칙상세링크", "")
            url = f"{LAW_PORTAL_BASE}{link}" if link else ""
            parsed.append({
                "id": admrul_id,
                "name": name,
                "kind": item.get("행정규칙종류", ""),
                "dept": item.get("소관부처명", ""),
                "date": item.get("시행일자", ""),
                "url": url,
            })
        return parsed

    async def aclose(self):
        await self._client.aclose()
