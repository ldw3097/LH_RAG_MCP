"""
조달청 해석사례 클라이언트 — 법제처 OPEN API(target=ppsCgmExpc).

법제처 DRF API가 조달청 법령해석(유권해석)을 JSON으로 제공하므로 HTML 크롤링이 불필요하다.
인증키(OC)는 법령·판례 소스와 동일하게 settings.law_oc_default를 사용한다.

  - lawSearch.do?target=ppsCgmExpc  : 목록(법령해석일련번호·안건명·해석일자 등), totalCnt
  - lawService.do?target=ppsCgmExpc : 본문(질의요지·회답·이유·관련법령)

전체 864건 규모(2022~)이므로 목록 9페이지(display=100) + 건별 본문 조회로 전건 적재한다.
"""

import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings

logger = logging.getLogger(__name__)

LAW_API_BASE = "https://www.law.go.kr/DRF"

_TARGET = "ppsCgmExpc"
_PAGE_SIZE = 100        # display 최대
_CONCURRENCY = 5
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class PpsCase:
    """조달청 해석사례 1건 (본문 포함)."""
    id: str               # 법령해석일련번호
    title: str            # 안건명
    reply_date: str       # 해석일자 (예: '2022.04.06')
    base_date: str        # 데이터기준일시 (증분 비교 기준)
    question: str         # 질의요지
    answer: str           # 회답
    reason: str           # 이유
    related_law: str      # 관련법령
    org: str              # 해석기관명
    url: str

    def body(self) -> str:
        """인덱싱용 본문 — 안건명 + 질의요지 + 회답 + 이유 + 관련법령."""
        parts = [f"[안건명] {self.title}"]
        if self.question:
            parts.append(f"[질의요지]\n{self.question}")
        if self.answer:
            parts.append(f"[회답]\n{self.answer}")
        if self.reason:
            parts.append(f"[이유]\n{self.reason}")
        if self.related_law:
            parts.append(f"[관련법령]\n{self.related_law}")
        return "\n\n".join(parts)


def _clean(value) -> str:
    if isinstance(value, list):
        value = "\n".join(str(v) for v in value if v)
    text = _TAG_RE.sub("", str(value or ""))
    # 흔한 HTML 엔티티 정리
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return text.strip()


def to_iso(date_str: str) -> str:
    """'2025.12.04' / '2025-12-04' / '20251204' → 'YYYY-MM-DD'. 파싱 실패 시 ''.

    목록 API는 점 구분('2025.12.04'), 본문 API는 8자리('20251204')로 주므로 둘 다 처리한다.
    """
    if not date_str:
        return ""
    s = date_str.strip()
    m = re.match(r"(\d{4})[.\-/](\d{2})[.\-/](\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"(\d{4})(\d{2})(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


class PpsApiClient:
    def __init__(self):
        self._oc = settings.law_oc_default
        self._client = httpx.AsyncClient(
            timeout=20.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            },
            transport=httpx.AsyncHTTPTransport(retries=1),
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def _get_json(self, endpoint: str, params: dict) -> dict:
        url = f"{LAW_API_BASE}/{endpoint}?{urlencode(params)}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.json()

    async def fetch_id_list(self, limit: int | None = None) -> list[dict]:
        """전체 해석사례 목록(메타)을 페이지네이션으로 수집합니다.

        Returns: [{"id", "title", "reply_date", "base_date", "org"}, ...]
        """
        first = await self._search_page(1)
        total = int(first.get("totalCnt", "0") or 0)
        items = list(first.get("items", []))
        last_page = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
        logger.info("ppsCgmExpc 목록: 총 %d건, %d페이지", total, last_page)

        for page in range(2, last_page + 1):
            if limit and len(items) >= limit:
                break
            page_data = await self._search_page(page)
            items.extend(page_data.get("items", []))

        return items[:limit] if limit else items

    async def _search_page(self, page: int) -> dict:
        params = {
            "OC": self._oc,
            "target": _TARGET,
            "type": "JSON",
            "display": _PAGE_SIZE,
            "page": page,
            "sort": "ddes",
        }
        data = await self._get_json("lawSearch.do", params)
        root = data.get("CgmExpc", {}) or {}
        raw = root.get("cgmExpc", [])
        if isinstance(raw, dict):
            raw = [raw]
        items = []
        for it in raw:
            sid = str(it.get("법령해석일련번호", "")).strip()
            if not sid:
                continue
            items.append({
                "id": sid,
                "title": _clean(it.get("안건명", "")),
                "reply_date": str(it.get("해석일자", "")).strip(),
                "base_date": str(it.get("데이터기준일시", "")).strip(),
                "org": _clean(it.get("해석기관명", "")),
            })
        return {"totalCnt": root.get("totalCnt", "0"), "items": items}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def _fetch_detail_raw(self, sid: str) -> dict:
        params = {"OC": self._oc, "target": _TARGET, "type": "JSON", "ID": sid}
        url = f"{LAW_API_BASE}/lawService.do?{urlencode(params)}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.json()

    async def fetch_detail(self, meta: dict) -> PpsCase | None:
        """단건 본문을 조회해 PpsCase로 반환합니다."""
        sid = meta["id"]
        data = await self._fetch_detail_raw(sid)
        svc = data.get("CgmExpcService", {}) or {}
        if not svc:
            logger.warning("본문 없음, 스킵: ID=%s", sid)
            return None
        # 법제처 API는 OC 키 포함 URL만 제공하므로, OC 없이 ID만 명시한 참조 URL 구성
        # (직접 접근 불가 — 실제 본문은 위 search_procurement_interpretations 결과에 포함됨)
        url = f"{LAW_API_BASE}/lawService.do?target={_TARGET}&ID={sid}&type=HTML"
        return PpsCase(
            id=sid,
            title=_clean(svc.get("안건명", "")) or meta.get("title", ""),
            reply_date=str(svc.get("해석일자", "")).strip() or meta.get("reply_date", ""),
            base_date=str(svc.get("데이터기준일시", "")).strip() or meta.get("base_date", ""),
            question=_clean(svc.get("질의요지", "")),
            answer=_clean(svc.get("회답", "")),
            reason=_clean(svc.get("이유", "")),
            related_law=_clean(svc.get("관련법령", "")),
            org=_clean(svc.get("해석기관명", "")) or meta.get("org", ""),
            url=url,
        )

    async def fetch_details(self, metas: list[dict]) -> list[PpsCase]:
        """본문을 동시성 제한으로 병렬 조회합니다."""
        sem = asyncio.Semaphore(_CONCURRENCY)

        async def _work(meta: dict) -> PpsCase | None:
            async with sem:
                try:
                    return await self.fetch_detail(meta)
                except Exception as e:
                    logger.warning("본문 조회 실패 ID=%s: %s", meta.get("id"), e)
                    return None

        results = await asyncio.gather(*(_work(m) for m in metas))
        return [r for r in results if r is not None]

    async def aclose(self):
        await self._client.aclose()
