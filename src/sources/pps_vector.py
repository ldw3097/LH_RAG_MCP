"""조달청 해석사례 검색 소스 — BM25 + Dense 하이브리드, RRF 결합.

법제처 ppsCgmExpc API로 적재한 해석사례(안건명·질의요지·회답·이유·관련법령)를
LH 규정과 동일한 BM25(keywords)+Dense(query) RRF 파이프라인으로 검색한다.
"""

import asyncio
import logging
import re
import threading

from src.sources.base import SearchResult, SearchSource
from src.sources.lh_vector import _rrf
from src.config import settings
from crawler.bm25_index import BM25Store, load_bm25, bm25_search, warmup_kiwi
from crawler.dense_index import DenseStore, load_dense, embed_query, dense_search

logger = logging.getLogger(__name__)

# 해석사례는 1건 = 질의+회답 전체가 평균 ~1,400자. 청크를 크게 잡아 1건 = 1청크로 유지.
# 반환 건수는 LH/KCSC보다 적게 잡아 Claude 컨텍스트 부담을 줄인다.
PPS_CHUNK_SIZE = 3000    # indexer에서 참조 (이 값 이하면 1청크)
PPS_TOP_K_CANDIDATES = 20
PPS_TOP_K_FINAL = 12

# 반환 콘텐츠 길이 제한 — 이를 초과하면 섹션 내부를 앞뒤 보존+중략으로 표시
PPS_CONTENT_LIMIT = 2500   # 반환 콘텐츠 길이 제한, 초과 시 중략 처리
_SECTION_RE = re.compile(r"(\[(?:안건명|질의요지|회답|이유|관련법령)\])")


def _truncate_content(text: str, limit: int = PPS_CONTENT_LIMIT) -> str:
    """길이 초과 시 섹션 단위로 중략 표시.

    섹션 구조([안건명]/[질의요지]/[회답]/[이유]/[관련법령])를 인식해,
    가장 긴 섹션 내부를 앞(1/3)·뒤(1/3) 보존 + '...(중략)...' 처리한다.
    한 섹션만으로도 limit를 넘으면 해당 섹션을 추가로 자른다.
    """
    if len(text) <= limit:
        return text

    # 섹션 분리: re.split(capturing)은 [앞부분, 태그1, 본문1, 태그2, 본문2, ...] 반환
    parts = _SECTION_RE.split(text)
    sections: list[tuple[str, str]] = []  # (태그, 본문)
    # parts[0]: 첫 태그 앞 텍스트 (보통 ""), parts[1::2]: 태그, parts[2::2]: 본문
    if parts[0]:
        sections.append(("", parts[0]))
    for i in range(1, len(parts) - 1, 2):
        tag = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        sections.append((tag, body))

    # 초과분 계산 — 가장 긴 섹션부터 줄인다
    excess = len(text) - limit
    sorted_sec = sorted(range(len(sections)), key=lambda j: len(sections[j][1]), reverse=True)

    for j in sorted_sec:
        tag, body = sections[j]
        body_len = len(body)
        if body_len <= 100:
            continue
        # 이 섹션을 줄여야 할 목표 크기(마커 오버헤드 30자 예약)
        target_body_len = max(120, body_len - excess - 30)
        keep = target_body_len // 2
        omit_chars = body_len - keep * 2
        omit_marker = f"\n...(중략 {omit_chars:,}자)...\n"
        new_body = body[:keep] + omit_marker + body[-keep:]
        excess -= (body_len - len(new_body))
        sections[j] = (tag, new_body)
        if excess <= 0:
            break

    return "".join(tag + body for tag, body in sections)


class PpsVectorSource(SearchSource):
    source_id = "pps_vector_db"

    def __init__(self):
        self._bm25: BM25Store | None = None
        self._dense: DenseStore | None = None
        self._id_to_idx: dict[str, int] = {}
        self._lock = threading.Lock()
        self._loaded = False

    def _ensure_loaded(self):
        with self._lock:
            if self._loaded:
                return
            col = settings.pps_bm25_collection
            bp = settings.pps_data_path
            self._bm25 = load_bm25(col, base_path=bp)
            if self._bm25:
                self._id_to_idx = {cid: i for i, cid in enumerate(self._bm25.ids)}
                logger.info("조달청 해석사례 BM25 로드: %d청크", len(self._bm25.ids))
                warmup_kiwi()
            else:
                logger.warning("조달청 해석사례 BM25 인덱스 없음 — build_pps_index.py를 실행하세요.")

            self._dense = load_dense(col, base_path=bp)
            if not self._dense:
                logger.warning("조달청 해석사례 Dense 인덱스 없음 — BM25만 사용합니다.")

            self._loaded = True

    def _dense_search(self, query: str) -> list[tuple[str, float]]:
        if not self._dense:
            return []
        try:
            q_vec = embed_query(query)
            return dense_search(self._dense, q_vec, top_k=PPS_TOP_K_CANDIDATES)
        except Exception as e:
            logger.warning("Dense 검색 실패 (BM25만 사용): %s", e)
            return []

    async def search(self, query: str, keywords: str) -> list[SearchResult]:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._ensure_loaded)

        if not self._bm25:
            return [SearchResult(
                source_id=self.source_id,
                title="조달청 해석사례 DB 미구축",
                content="조달청 해석사례 인덱스가 없습니다. scripts/build_pps_index.py를 실행하세요.",
            )]

        bm25 = self._bm25
        bm25_task = loop.run_in_executor(
            None, lambda: bm25_search(bm25, keywords, top_k=PPS_TOP_K_CANDIDATES)
        )
        dense_task = loop.run_in_executor(None, lambda: self._dense_search(query))
        bm25_hits, dense_hits = await asyncio.gather(bm25_task, dense_task)

        if dense_hits:
            top_ids = _rrf(bm25_hits, dense_hits, top_n=PPS_TOP_K_FINAL)
        else:
            top_ids = [cid for cid, _ in bm25_hits[:PPS_TOP_K_FINAL]]

        output = []
        for cid in top_ids:
            idx = self._id_to_idx.get(cid)
            if idx is None:
                continue
            meta = self._bm25.metadatas[idx]
            title = meta.get("title") or cid.rsplit("__c", 1)[0]
            reply_date = meta.get("reply_date", "")
            head = f"{title} (해석일자 {reply_date})" if reply_date else title
            output.append(SearchResult(
                source_id=self.source_id,
                title=head,
                content=_truncate_content(self._bm25.corpus[idx]),
                url=meta.get("url", ""),
                metadata=meta,
            ))
        return output
