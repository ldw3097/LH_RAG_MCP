import asyncio
import logging
import re
from urllib.parse import urlencode

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import settings
from src.context import law_oc_var
from src.sources.base import SearchResult, SearchSource
from src.sources.law_normalize import score_law_relevance, strip_non_law_keywords

logger = logging.getLogger(__name__)

LAW_API_BASE = "https://www.law.go.kr/DRF"
LAW_PORTAL_BASE = "https://www.law.go.kr"

AI_SEARCH_K = 5       # aiSearch 각 타입(법령조문/행정규칙조문)별 건수
LAW_DETAIL_K = 5      # 법령 키워드검색 본문 조회 건수
ADMRUL_DETAIL_K = 5   # 행정규칙 키워드검색 본문 조회 건수
KW_VARIANTS_MAX = 3   # 키워드 검색 변형 상한 (초과 시 HTTP fan-out 증가)
ADMRUL_ORG_MOLIT = "1613000"  # 국토교통부 소관부처코드

_IMG_TAG_RE = re.compile(r"<img[^>]*>(?:</img>)?", re.IGNORECASE)
_SUMMARY_LEN = 300


def _truncate(text: str, n: int = _SUMMARY_LEN) -> str:
    return text[:n] + ("…" if len(text) > n else "")


_ART_RE = re.compile(r"^(제\d+조(?:의\d+)?(?:\([^)]+\))?)")
_ID_RE = re.compile(r"^제(\d+)조(?:의(\d+))?(?:\(([^)]+)\))?$")
_NO_RE = re.compile(r"^0*(\d+)(?:의0*(\d+))?$")
# 평문 조문내용(행정규칙)에서 항 구분이 없을 때 환형 항번호(①~⑳) 앞에 줄바꿈
_HANG_NO_RE = re.compile(r"(?<=\S)([①-⑳])")


def _parse_article_id(identifier: str) -> tuple[str, str, str]:
    """조문 식별자를 (조번호, 조의번호, 조문제목)으로 파싱합니다. 형식 오류 시 ValueError."""
    m = _ID_RE.match(identifier.strip())
    if not m:
        raise ValueError(
            f"article 형식 오류: '{identifier}'. "
            "올바른 형식: 제3조 / 제7조의2 / 제3조(정의) / 제7조의2(특례)"
        )
    return m.group(1), m.group(2) or "", m.group(3) or ""


def _as_list(v) -> list:
    if isinstance(v, dict):
        return [v]
    if isinstance(v, list):
        return v
    return []


def _normalize_art_no(article: dict) -> tuple[str, str]:
    """조문단위 dict에서 (조번호, 가지번호)를 정규화해 반환합니다.

    법제처 JSON은 조번호(`조문번호`)와 가지번호(`조문가지번호`)를 별도 필드로 둔다.
    평문 파싱 경로(`_fetch_all_admrul_articles`)는 `조문번호`에 의N을 합쳐 넣으므로
    `^N(의M)?` 형태도 함께 분해한다.
    """
    raw = article.get("조문번호", "")
    # 법령은 `조문가지번호`, 행정규칙은 `조의번호` 필드로 가지번호를 준다
    branch = (article.get("조문가지번호") or article.get("조의번호") or "").lstrip("0")
    m = _NO_RE.match(raw)
    if m:
        return m.group(1), branch or (m.group(2) or "")
    return raw.lstrip("0"), branch


def _build_toc(articles: list[dict], max_items: int = 40) -> str:
    """조문단위 리스트에서 목차 문자열을 생성합니다."""
    parts = [h for art in articles[:max_items] if (h := _article_header(art))]
    if not parts:
        return ""
    suffix = " …" if len(articles) > max_items else ""
    return "[목차] " + " ".join(parts) + suffix


def _article_header(art: dict) -> str:
    """조문단위 dict → '제N조의M(제목)' 헤더 문자열."""
    art_no, art_of = _normalize_art_no(art)
    header = f"제{art_no}조" if art_no else ""
    if art_of:
        header += f"의{art_of}"
    title = art.get("조문제목", "")
    if title:
        header += f"({title})"
    return header


def _render_article_body(art: dict) -> str:
    """조문 본문을 항·호·목 계층까지 펼쳐 문자열로 만듭니다.

    본문은 `조문내용`(헤더 줄만 담기도 함)이 아니라 `항`/`호`/`목` 계층에 들어 있다.
    계층이 없는 단순 조문은 `조문내용`에서 헤더 접두어를 떼어 사용한다.
    """
    # 조문내용에서 헤더 접두어를 뗀 도입문 (항내용 없이 호만 있는 조문은 여기에 도입문이 있음)
    content = art.get("조문내용", "")
    if isinstance(content, list):
        content = "\n".join(str(c) for c in content if c)
    intro = _ART_RE.sub("", content, count=1).strip()

    hangs = _as_list(art.get("항"))
    if not hangs:
        intro = _HANG_NO_RE.sub(r"\n\1", intro)
        return _IMG_TAG_RE.sub("", intro).strip()

    lines: list[str] = []
    # 첫 항에 항내용이 없으면(호만 있는 단일 항) 도입문을 본문 머리로 보존
    if intro and not str(hangs[0].get("항내용", "")).strip():
        lines.append(intro)
    for hang in hangs:
        t = str(hang.get("항내용", "")).strip()
        if t:
            lines.append(t)
        for ho in _as_list(hang.get("호")):
            t = str(ho.get("호내용", "")).strip()
            if t:
                lines.append(t)
            for mok in _as_list(ho.get("목")):
                t = str(mok.get("목내용", "")).strip()
                if t:
                    lines.append(t)
    return _IMG_TAG_RE.sub("", "\n".join(lines)).strip()


def _format_full_article(art: dict) -> str:
    """조문단위 dict를 전문 문자열로 포맷합니다 (truncation 없음)."""
    header = _article_header(art)
    body = _render_article_body(art)
    return f"{header}\n{body}".strip() if body else header


def _dedup(raw_lists, key_fn, fail_label: str) -> list:
    """return_exceptions=True gather 결과에서 중복 없이 항목을 수집합니다."""
    seen: set[str] = set()
    out = []
    for res in raw_lists:
        if not isinstance(res, list):
            logger.debug("%s 실패: %s", fail_label, res)
            continue
        for item in res:
            k = key_fn(item)
            if k and k not in seen:
                seen.add(k)
                out.append(item)
    return out


def _format_articles_toc(articles: list[dict], limit: int = 700) -> str:
    """조문단위 배열 → [목차] + 제1조 본문 문자열."""
    if not articles:
        return ""
    toc = _build_toc(articles, max_items=len(articles))
    body = _truncate(_format_full_article(articles[0]), limit)
    return f"{toc}\n{body}" if toc else body


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
            transport=httpx.AsyncHTTPTransport(retries=1),
        )

    async def search(self, query: str, keywords: str) -> list[SearchResult]:
        """aiSearch(법령조문+행정규칙조문) · 법령 키워드검색 · 행정규칙 키워드검색을 병렬 실행."""
        stripped = strip_non_law_keywords(keywords)
        # 원본 전체 → 부가어 제거본 → 개별 토큰 순으로 검색 변형 생성.
        # 법제처 일반검색은 공백 구분 AND 매칭이라 키워드가 여러 개면 0건이 되기 쉬우므로
        # 개별 토큰별로도 따로 검색해 합집합을 구한다.
        tokens = [t for t in stripped.split() if len(t) > 1]
        kw_variants = list(dict.fromkeys([keywords, stripped] + tokens))[:KW_VARIANTS_MAX]

        ai0_res, ai2_res, law_res, admrul_res = await asyncio.gather(
            self._ai_search(query, search_type="0"),
            self._ai_search(query, search_type="2"),
            self._search_law_keyword(keywords, kw_variants),
            self._search_admrul_keyword(keywords, kw_variants),
            return_exceptions=True,
        )

        results: list[SearchResult] = []

        # 조문 블록 (aiSearch search=0 + search=2)
        for label, res in [("aiSearch-0", ai0_res), ("aiSearch-2", ai2_res)]:
            if isinstance(res, list):
                results += res
            else:
                logger.warning("%s 실패: %s", label, res)

        # 법령 블록
        if isinstance(law_res, list):
            results += law_res
        else:
            logger.warning("법령 키워드검색 실패: %s", law_res)

        # 행정규칙 블록
        if isinstance(admrul_res, list):
            results += admrul_res
        else:
            logger.warning("행정규칙 키워드검색 실패: %s", admrul_res)

        return results

    # ── aiSearch ──────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    async def _ai_search(self, query: str, search_type: str = "0") -> list[SearchResult]:
        """법제처 AI 자연어법령검색 API.
        search_type: "0"=법령조문, "2"=행정규칙조문
        """
        params = {
            "OC": law_oc_var.get() or settings.law_oc_default,
            "target": "aiSearch",
            "query": query,
            "search": search_type,
            "type": "JSON",
            "display": AI_SEARCH_K,
        }
        url = f"{LAW_API_BASE}/lawSearch.do?{urlencode(params)}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        return self._parse_ai_results(data, search_type)

    # ── 법령 키워드검색 ───────────────────────────────────────────

    async def _search_law_keyword(
        self, keywords: str, variants: list[str]
    ) -> list[SearchResult]:
        """비법령명어 제거본과 원본을 병렬 검색 → 합집합 → 정확매칭 재정렬 → 본문 조회."""
        raw_lists = await asyncio.gather(
            *(self._general_search(v) for v in variants),
            return_exceptions=True,
        )
        candidates: list[SearchResult] = _dedup(
            raw_lists, lambda r: r.metadata.get("mst") or r.title, "법령 일반검색 변형"
        )

        if not candidates:
            return []

        query_words = keywords.split()
        candidates.sort(
            key=lambda r: score_law_relevance(r.title, keywords, query_words),
            reverse=True,
        )
        top = candidates[:LAW_DETAIL_K]
        logger.info("법령 키워드검색: %d개 후보 → 상위 %d건 본문 조회", len(candidates), len(top))

        # 본문 조회 병렬
        contents = await asyncio.gather(
            *(self._fetch_law_content(r.metadata.get("mst", "")) for r in top),
            return_exceptions=True,
        )
        results = []
        for r, content in zip(top, contents):
            if isinstance(content, Exception) or not content:
                body = r.content  # fallback: 기존 메타 한 줄
            else:
                body = _truncate(content)
            results.append(SearchResult(
                source_id=self.source_id,
                title=r.title,
                content=body,
                url=r.url,
                metadata={**r.metadata, "block": "법령"},
            ))
        return results

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    async def _general_search(self, keywords: str) -> list[SearchResult]:
        """법제처 일반법령검색 API (키워드)."""
        params = {
            "OC": law_oc_var.get() or settings.law_oc_default,
            "target": "law",
            "query": keywords,
            "type": "JSON",
            "display": LAW_DETAIL_K,
            "page": 1,
            "sort": "score",
        }
        url = f"{LAW_API_BASE}/lawSearch.do?{urlencode(params)}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        return self._parse_general_results(data)

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    async def _fetch_law_content(self, mst: str) -> str:
        """법령 본문 첫 조문 조회 (목적·정의 등)."""
        if not mst:
            return ""
        params = {
            "OC": law_oc_var.get() or settings.law_oc_default,
            "target": "law",
            "MST": mst,
            "type": "JSON",
        }
        url = f"{LAW_API_BASE}/lawService.do?{urlencode(params)}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        articles = data.get("법령", {}).get("조문", {}).get("조문단위", [])
        if isinstance(articles, dict):
            articles = [articles]
        return _IMG_TAG_RE.sub("", _format_articles_toc(articles)).strip()

    # ── 행정규칙 키워드검색 ───────────────────────────────────────

    async def _search_admrul_keyword(
        self, keywords: str, variants: list[str]
    ) -> list[SearchResult]:
        """비법령명어 제거본과 원본을 병렬 검색 → 합집합 → 본문 조회."""
        raw_lists = await asyncio.gather(
            *(self._admrul_search(v) for v in variants),
            return_exceptions=True,
        )
        items: list[dict] = _dedup(
            raw_lists, lambda it: it["id"], "행정규칙 검색 변형"
        )

        if not items:
            return []

        query_words = keywords.split()
        items.sort(
            key=lambda it: score_law_relevance(it["name"], keywords, query_words),
            reverse=True,
        )
        top = items[:ADMRUL_DETAIL_K]
        logger.info("행정규칙 키워드검색: %d개 후보 → 상위 %d건 본문 조회", len(items), len(top))

        contents = await asyncio.gather(
            *(self._fetch_admrul_content(it["id"]) for it in top),
            return_exceptions=True,
        )
        results = []
        for it, content in zip(top, contents):
            if isinstance(content, Exception):
                logger.debug("행정규칙 본문 조회 실패 id=%s: %s", it["id"], content)
                content = ""
            kind = it.get("kind", "")
            title = f"{it['name']} ({kind})" if kind else it["name"]
            body = content if content else f"소관부처: {it.get('dept', '')} | 시행일: {it.get('date', '')}"
            results.append(SearchResult(
                source_id=self.source_id,
                title=title,
                content=body,
                url=it.get("url", ""),
                metadata={"admrul_id": it["id"], "kind": kind, "block": "행정규칙"},
            ))
        return results

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
        """행정규칙 본문 조회 — 목차 + 제1조 본문 (최대 700자)."""
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
        svc = data.get("AdmRulService", {})

        # 구조화된 조문단위가 있으면 법령과 동일 경로
        articles = svc.get("조문", {}).get("조문단위", [])
        if isinstance(articles, dict):
            articles = [articles]
        if articles:
            return _IMG_TAG_RE.sub("", _format_articles_toc(articles)).strip()

        # 조문내용이 문자열 배열인 경우 (행정규칙 고시 등) — 목차 + 제1조 추출
        # 평문 문자열이라 조문번호·제목이 분리되어 있지 않아 _format_articles_toc와 통합 불가
        raw = svc.get("조문내용", "")
        if isinstance(raw, list):
            strs = [str(s).strip() for s in raw if s]
            toc_parts, first_body = [], ""
            for s in strs:
                m = _ART_RE.match(s)
                if m:
                    toc_parts.append(m.group(1))
                    if not first_body:
                        first_body = _truncate(s, 700)
            toc = "[목차] " + " ".join(toc_parts) if toc_parts else ""
            return _IMG_TAG_RE.sub("", f"{toc}\n{first_body}" if toc else first_body).strip()

        return _IMG_TAG_RE.sub("", _truncate(raw or "", 700)).strip()

    # ── 파서 ──────────────────────────────────────────────────────

    def _parse_ai_results(self, data: dict, search_type: str) -> list[SearchResult]:
        if search_type == "2":
            items = data.get("aiSearch", {}).get("행정규칙조문", [])
        else:
            items = data.get("aiSearch", {}).get("법령조문", [])
        if isinstance(items, dict):
            items = [items]
        results = []
        for item in items:
            if search_type == "2":
                law_name = item.get("행정규칙명", "")
                mst = item.get("행정규칙ID", "")
            else:
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
                metadata={"mst": mst, "law_name": law_name, "block": "조문"},
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

    # ── 조문 직접 조회 (public) ──────────────────────────────────────

    async def get_law_article(self, law_name: str, article: str) -> str:
        """법령에서 특정 조문 전문을 조회합니다."""
        try:
            art_no, art_of, _ = _parse_article_id(article)
        except ValueError as e:
            return str(e)

        candidates = await self._general_search(law_name)
        if not candidates:
            return f"'{law_name}' 법령을 찾지 못했습니다."

        query_words = law_name.split()
        candidates.sort(
            key=lambda r: score_law_relevance(r.title, law_name, query_words),
            reverse=True,
        )
        mst = candidates[0].metadata.get("mst", "")
        found_name = candidates[0].title

        articles = await self._fetch_all_law_articles(mst)
        if not articles:
            return f"'{found_name}' 본문을 불러오지 못했습니다."

        matched = self._match_articles(articles, art_no, art_of)
        if not matched:
            toc = _build_toc(articles)
            return f"'{found_name}'에서 '{article}'을 찾지 못했습니다.\n{toc}"

        parts = [f"【{found_name}】"]
        for art in matched:
            parts.append(_format_full_article(art))
        return "\n\n".join(parts)

    async def get_admrul_article(self, admrul_name: str, article: str) -> str:
        """행정규칙에서 특정 조문 전문을 조회합니다."""
        try:
            art_no, art_of, _ = _parse_article_id(article)
        except ValueError as e:
            return str(e)

        items = await self._admrul_search(admrul_name)
        if not items:
            return f"'{admrul_name}' 행정규칙을 찾지 못했습니다."

        query_words = admrul_name.split()
        items.sort(
            key=lambda it: score_law_relevance(it["name"], admrul_name, query_words),
            reverse=True,
        )
        admrul_id = items[0]["id"]
        found_name = items[0]["name"]

        articles = await self._fetch_all_admrul_articles(admrul_id)
        if not articles:
            return f"'{found_name}' 본문을 불러오지 못했습니다."

        matched = self._match_articles(articles, art_no, art_of)
        if not matched:
            toc = _build_toc(articles)
            return f"'{found_name}'에서 '{article}'을 찾지 못했습니다.\n{toc}"

        parts = [f"【{found_name}】"]
        for art in matched:
            parts.append(_format_full_article(art))
        return "\n\n".join(parts)

    # ── 전체 조문 조회 (private) ──────────────────────────────────────

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    async def _fetch_all_law_articles(self, mst: str) -> list[dict]:
        """법령 전체 조문단위 리스트를 반환합니다."""
        if not mst:
            return []
        params = {
            "OC": law_oc_var.get() or settings.law_oc_default,
            "target": "law",
            "MST": mst,
            "type": "JSON",
        }
        url = f"{LAW_API_BASE}/lawService.do?{urlencode(params)}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        data = resp.json()
        articles = data.get("법령", {}).get("조문", {}).get("조문단위", [])
        if isinstance(articles, dict):
            articles = [articles]
        return list(articles)

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    async def _fetch_all_admrul_articles(self, admrul_id: str) -> list[dict]:
        """행정규칙 전체 조문 리스트를 반환합니다. 구조화/평문 양식 모두 처리."""
        if not admrul_id:
            return []
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
        svc = data.get("AdmRulService", {})

        articles = svc.get("조문", {}).get("조문단위", [])
        if isinstance(articles, dict):
            articles = [articles]
        if articles:
            return list(articles)

        # 평문 문자열 배열 파싱 (고시 등)
        raw = svc.get("조문내용", "")
        if not isinstance(raw, list):
            return []

        parsed: list[dict] = []
        current: dict | None = None
        for s in raw:
            s = str(s).strip()
            if not s:
                continue
            art_m = _ART_RE.match(s)
            if art_m:
                if current is not None:
                    parsed.append(current)
                id_m = _ID_RE.match(art_m.group(1))
                if id_m:
                    current = {
                        "조문번호": id_m.group(1),
                        "조의번호": id_m.group(2) or "",
                        "조문제목": id_m.group(3) or "",
                        "조문내용": s[len(art_m.group(1)):].strip(),
                    }
                else:
                    current = {"조문번호": "", "조의번호": "", "조문제목": "", "조문내용": s}
            elif current is not None:
                current["조문내용"] += "\n" + s
        if current is not None:
            parsed.append(current)
        return parsed

    @staticmethod
    def _match_articles(articles: list[dict], art_no: str, art_of: str) -> list[dict]:
        """조문단위 리스트에서 (조번호, 조의번호)가 일치하는 조문을 반환합니다."""
        return [
            art for art in articles
            if _normalize_art_no(art) == (art_no, art_of)
        ]

    async def aclose(self):
        await self._client.aclose()
