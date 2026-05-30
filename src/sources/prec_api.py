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

PREC_DETAIL_K = 5  # 요지까지 조회할 상위 건수

_IMG_TAG_RE = re.compile(r"<img[^>]*>(?:</img>)?", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


class PrecedentSource(SearchSource):
    source_id = "prec"

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
            transport=httpx.AsyncHTTPTransport(retries=1),
        )

    async def search(self, query: str, keywords: str) -> list[SearchResult]:
        """판례 키워드 검색 후 상위 N건 요지를 병렬 조회해 반환."""
        kw = keywords or query
        items = await self._prec_search(kw)
        if not items:
            # 결과 없으면 첫 번째 키워드만으로 재시도
            first_kw = kw.split()[0]
            if first_kw != kw:
                logger.info("판례 검색 결과 없음, 첫 키워드로 재시도: %s", first_kw)
                items = await self._prec_search(first_kw)
        if not items:
            return []
        contents = await asyncio.gather(
            *(self._fetch_prec_detail(it["id"]) for it in items),
            return_exceptions=True,
        )
        results = []
        for it, content in zip(items, contents):
            if isinstance(content, Exception):
                logger.debug("판례 요지 조회 실패 id=%s: %s", it["id"], content)
                content = ""
            court = it.get("court", "")
            date = it.get("date", "")
            case_no = it.get("case_no", "")
            ptype = it.get("type", "")
            title_parts = [p for p in [court, date] if p]
            if date:
                title_parts.append("선고")
            if case_no:
                title_parts.append(case_no)
            if ptype:
                title_parts.append(f"({ptype})")
            title = " ".join(title_parts) or it.get("name", "") or "판례"
            body = content or f"사건명: {it.get('name', '')} | 법원: {court} | 선고일: {date}"
            results.append(SearchResult(
                source_id=self.source_id,
                title=title,
                content=body[:2000],
                url=it.get("url", ""),
                metadata={
                    "prec_id": it["id"],
                    "court": court,
                    "date": date,
                    "case_no": case_no,
                    "type": "prec",
                },
            ))
        return results

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    async def _prec_search(self, keywords: str) -> list[dict]:
        """법제처 판례 검색 API (키워드 AND 매칭). 최신순, 메타데이터만 반환."""
        params = {
            "OC": law_oc_var.get() or settings.law_oc_default,
            "target": "prec",
            "query": keywords,
            "type": "JSON",
            "display": PREC_DETAIL_K,
            "sort": "ddes",  # 선고일 내림차순
        }
        url = f"{LAW_API_BASE}/lawSearch.do?{urlencode(params)}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        return self._parse_prec_list(data)

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    async def _fetch_prec_detail(self, prec_id: str) -> str:
        """판례 요지 조회 (판시사항·판결요지·참조조문·참조판례, 전문 제외)."""
        params = {
            "OC": law_oc_var.get() or settings.law_oc_default,
            "target": "prec",
            "ID": prec_id,
            "type": "JSON",
        }
        url = f"{LAW_API_BASE}/lawService.do?{urlencode(params)}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        prec = data.get("PrecService", {})
        sections = [
            ("판시사항", prec.get("판시사항", "")),
            ("판결요지", prec.get("판결요지", "")),
            ("참조조문", prec.get("참조조문", "")),
            ("참조판례", prec.get("참조판례", "")),
        ]
        parts = []
        for label, value in sections:
            text = self._clean(value)
            if text:
                parts.append(f"[{label}]\n{text}")
        return "\n\n".join(parts)

    def _parse_prec_list(self, data: dict) -> list[dict]:
        items = data.get("PrecSearch", {}).get("prec", [])
        if isinstance(items, dict):
            items = [items]
        parsed = []
        for item in items:
            prec_id = item.get("판례일련번호", "")
            if not prec_id:
                continue
            case_no = item.get("사건번호", "")
            url = f"{LAW_PORTAL_BASE}/판례/({case_no})" if case_no else ""
            parsed.append({
                "id": str(prec_id),
                "name": item.get("사건명", ""),
                "case_no": case_no,
                "court": item.get("법원명", ""),
                "date": item.get("선고일자", ""),
                "type": item.get("판결유형", ""),
                "url": url,
            })
        return parsed

    @staticmethod
    def _clean(value) -> str:
        if isinstance(value, list):
            value = "\n".join(str(v) for v in value if v)
        text = _TAG_RE.sub("", str(value or ""))
        return text.strip()

    async def aclose(self):
        await self._client.aclose()
