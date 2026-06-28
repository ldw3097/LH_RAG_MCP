import asyncio
import logging
import math
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
DEEPINFRA_RERANK_URL = "https://api.deepinfra.com/v1/inference"

POOL_K = 25          # 법령앵커 후보 풀 (본문 조회 대상)
FINAL_K = 5          # 최종 반환 건수

RERANK_DOC_MAXLEN = 5000   # 리랭커 입력 문서 길이 예산 (우선순위 누적 채우기)
BODY_MAXLEN = 3000         # 반환 본문 길이 상한

# 본문(판시사항·판결요지·판례내용)을 법제처 API로 받을 수 없는 외부연계 출처.
# 검색 인덱스엔 메타데이터만 있고 본문은 외부 시스템에 위임 → detail 호출 전 제외.
EXCLUDED_SOURCES = {"국세법령정보시스템"}

_TAG_RE = re.compile(r"<[^>]+>")
# 판례내용(전문) 안의 【이    유】 섹션 헤더 (조사 결과 이/유 사이 공백 가변)
_REASON_RE = re.compile(r"【\s*이\s*유\s*】")


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

    async def search(self, query: str, keywords: str = "", law_name: str = "") -> list[SearchResult]:
        if not law_name and not keywords:
            return []

        # 1. 후보 수집: 키워드·law_name을 각각 개별 검색 후 합집합
        # (law_name도 일반 키워드처럼 검색어로 취급). POOL_K를 검색어 수로 나눠 할당량 결정
        search_terms = keywords.split() + ([law_name] if law_name else [])
        per_term = math.ceil(POOL_K / len(search_terms))
        logger.info("판례 후보 수집: %s (law_name=%s, per_term=%d)", search_terms, law_name, per_term)
        raw = await asyncio.gather(
            *(self._prec_list(t, display=per_term, sort="score") for t in search_terms),
            return_exceptions=True,
        )
        seen: set[str] = set()
        candidates: list[dict] = []
        for res in raw:
            if not isinstance(res, list):
                continue
            for it in res:
                if it["id"] in seen:
                    continue
                if it.get("source") in EXCLUDED_SOURCES:
                    continue  # 본문 미제공 외부연계(국세 등) → detail 호출 생략
                seen.add(it["id"])
                candidates.append(it)
        if not candidates:
            return []

        # 2. 본문 병렬 조회
        details = await asyncio.gather(
            *(self._fetch_prec_detail(c["id"]) for c in candidates),
            return_exceptions=True,
        )
        details = [d if isinstance(d, dict) else {} for d in details]

        # 3. 본문(판시사항·판결요지·판례내용) 없는 후보 제외
        pairs = [
            (c, d) for c, d in zip(candidates, details)
            if d.get("판시사항") or d.get("판결요지") or d.get("판례내용")
        ]
        if not pairs:
            return []
        candidates, details = zip(*pairs)
        candidates, details = list(candidates), list(details)

        # 4. 리랭커로 재정렬
        order = await self._rank(query, candidates, details)

        # 5. 상위 FINAL_K 빌드
        results = [self._build_result(candidates[i], details[i]) for i in order[:FINAL_K]]
        logger.info("판례 검색 완료: 후보 %d건 → 상위 %d건", len(candidates), len(results))
        return results

    # ── 재정렬 ────────────────────────────────────────────────────

    async def _rank(
        self, query: str,
        candidates: list[dict], details: list[dict],
    ) -> list[int]:
        """리랭커 점수로 후보 인덱스를 정렬 (어휘중첩 폴백)."""
        docs = [_rerank_doc(c, d) for c, d in zip(candidates, details)]
        try:
            scores = await self._rerank_deepinfra(query, docs)
        except Exception as e:
            logger.info("리랭커 사용 불가(%s) → 어휘중첩 폴백", e)
            scores = _lexical_scores(query, docs)
        return sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True)

    async def _rerank_deepinfra(self, query: str, docs: list[str]) -> list[float]:
        """DeepInfra 리랭커 API. 키 없거나 응답 형식이 다르면 예외 → 호출측 폴백."""
        api_key = settings.deepinfra_api_key
        if not api_key:
            raise RuntimeError("DEEPINFRA_API_KEY 미설정")
        url = f"{DEEPINFRA_RERANK_URL}/{settings.reranker_model}"
        resp = await self._client.post(
            url,
            headers={"Authorization": f"bearer {api_key}"},
            json={"queries": [query], "documents": docs},
            timeout=30.0,
        )
        resp.raise_for_status()
        scores = resp.json().get("scores")
        if not isinstance(scores, list) or len(scores) != len(docs):
            raise ValueError("리랭커 응답 형식 불일치")
        return [float(s) for s in scores]

    # ── 후보 검색 ─────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    async def _prec_list(
        self, query: str, display: int = POOL_K, sort: str | None = "ddes"
    ) -> list[dict]:
        """법제처 판례 검색 API (키워드 AND 매칭). 메타데이터만 반환."""
        params = {
            "OC": law_oc_var.get() or settings.law_oc_default,
            "target": "prec",
            "query": query,
            "type": "JSON",
            "display": display,
        }
        if sort:
            params["sort"] = sort
        url = f"{LAW_API_BASE}/lawSearch.do?{urlencode(params)}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        return self._parse_prec_list(resp.json())

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=4))
    async def _fetch_prec_detail(self, prec_id: str) -> dict:
        """판례 상세 조회 → 섹션 dict 반환.

        대법원 등 요지 수록 판례는 판시사항·판결요지가 오고, 하급심은 보통 이들이 비고
        판례내용(전문)만 온다. 둘 다 없으면(국세 외부연계 등) 빈 dict.
        """
        params = {
            "OC": law_oc_var.get() or settings.law_oc_default,
            "target": "prec",
            "ID": prec_id,
            "type": "JSON",
        }
        url = f"{LAW_API_BASE}/lawService.do?{urlencode(params)}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        prec = resp.json().get("PrecService", {})
        return {
            label: self._clean(prec.get(label, ""))
            for label in ("판시사항", "판결요지", "참조조문", "참조판례", "판례내용")
        }

    # ── 결과 빌드 ─────────────────────────────────────────────────

    def _build_result(self, it: dict, detail: dict) -> SearchResult:
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

        pansi = detail.get("판시사항", "")
        yoji = detail.get("판결요지", "")
        parts = []
        if not (pansi or yoji) and it.get("name"):
            # 하급심형 — 사건명(쟁점요약)을 앞세움
            parts.append(f"[사건명]\n{it['name']}")
        if pansi:
            parts.append(f"[판시사항]\n{pansi}")
        if yoji:
            parts.append(f"[판결요지]\n{yoji}")
        else:
            # 판결요지 결측 → 판례내용(이유)로 판단부 보강
            reason = _extract_reason(detail.get("판례내용", ""))
            if reason:
                parts.append(f"[판례내용]\n{reason}")
        for label in ("참조조문", "참조판례"):
            text = detail.get(label, "")
            if text:
                parts.append(f"[{label}]\n{text}")
        body = "\n\n".join(parts)

        return SearchResult(
            source_id=self.source_id,
            title=title,
            content=body[:BODY_MAXLEN],
            url=it.get("url", ""),
            metadata={
                "prec_id": it["id"],
                "court": court,
                "date": date,
                "case_no": case_no,
                "type": "prec",
            },
        )

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
                "source": item.get("데이터출처명", ""),
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


# ── 모듈 헬퍼 ──────────────────────────────────────────────────────

def _extract_reason(content: str) -> str:
    """판례내용(전문)에서 【이유】 이후 본문만 추출. 헤더 없으면 전체.

    전문 앞부분(【원고】【피고】【원심판결】【주문】)은 당사자·변호사명 위주의 노이즈라
    제외하고, 실제 사실관계·판단이 담긴 【이유】 이하만 사용한다.
    """
    if not content:
        return ""
    m = _REASON_RE.search(content)
    return content[m.end():].strip() if m else content.strip()


def _rerank_doc(cand: dict, detail: dict) -> str:
    """후보의 리랭커 입력 문서 생성 — 우선순위 누적 채우기.

    판시사항(없으면 사건명) > 판결요지 > 【이유】 순으로 예산(MAXLEN)이 찰 때까지
    통째로 넣고, 마지막 항목이 넘치면 남은 만큼만 잘라 넣는다. 대부분 케이스에서
    판시+판결요지가 먼저 들어가고, 남은 예산을 【이유】가 채운다.
    - 판시 + 판결요지       (56%)  → 판시 + 판결요지 (+남으면 이유)
    - 판시 + 판례내용        (13%)  → 판시 + 【이유】 (판결요지 결측 보강)
    - 판례내용만(하급심) (31%)  → 사건명 + 【이유】
    """
    pieces = [
        detail.get("판시사항", "") or cand.get("name", ""),
        detail.get("판결요지", ""),
        _extract_reason(detail.get("판례내용", "")),
    ]
    chunks: list[str] = []
    used = 0
    for p in pieces:
        if not p or used >= RERANK_DOC_MAXLEN:
            continue
        take = p[: RERANK_DOC_MAXLEN - used]
        chunks.append(take)
        used += len(take)
    return "\n".join(chunks)


def _lexical_scores(query: str, docs: list[str]) -> list[float]:
    """어휘 중첩 비율 (리랭커 폴백). query 토큰 중 문서에 등장하는 비율."""
    qtokens = [t for t in query.split() if len(t) > 1]
    if not qtokens:
        return [0.0] * len(docs)
    return [sum(1 for t in qtokens if t in d) / len(qtokens) for d in docs]


