"""LH 규정 검색 소스 — BM25 + Dense(Qwen3-Embedding) 하이브리드, RRF 결합."""

import asyncio
import logging
import threading

from src.sources.base import SearchResult, SearchSource
from crawler.bm25_index import BM25Store, load_bm25, bm25_search, warmup_kiwi
from crawler.dense_index import DenseStore, load_dense, embed_query, dense_search

logger = logging.getLogger(__name__)

TOP_K_CANDIDATES = 20  # BM25·Dense 각각 후보 수
TOP_K_FINAL = 7        # RRF 최종 반환 수
RRF_K = 60             # RRF 상수


def _rrf(
    bm25_hits: list[tuple[str, float]],
    dense_hits: list[tuple[str, float]],
    top_n: int = TOP_K_FINAL,
) -> list[str]:
    """Reciprocal Rank Fusion으로 두 랭킹을 결합합니다."""
    scores: dict[str, float] = {}
    for rank, (cid, _) in enumerate(bm25_hits):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, (cid, _) in enumerate(dense_hits):
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(scores, key=lambda c: scores[c], reverse=True)[:top_n]


class LHVectorSource(SearchSource):
    source_id = "lh_vector_db"

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
            self._bm25 = load_bm25()
            if self._bm25:
                self._id_to_idx = {cid: i for i, cid in enumerate(self._bm25.ids)}
                logger.info("BM25 인덱스 로드 완료: %d청크", len(self._bm25.ids))
                warmup_kiwi()
            else:
                logger.warning("BM25 인덱스 없음 — build_index.py를 먼저 실행하세요.")

            self._dense = load_dense()
            if not self._dense:
                logger.warning("Dense 인덱스 없음 — BM25만 사용합니다.")

            self._loaded = True

    def _dense_search(self, query: str) -> list[tuple[str, float]]:
        """쿼리를 임베딩 후 코사인 유사도 상위 후보를 반환합니다."""
        if not self._dense:
            return []
        try:
            q_vec = embed_query(query)
            return dense_search(self._dense, q_vec, top_k=TOP_K_CANDIDATES)
        except Exception as e:
            logger.warning("Dense 검색 실패 (BM25만 사용): %s", e)
            return []

    async def search(self, query: str) -> list[SearchResult]:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._ensure_loaded)

        if not self._bm25:
            return [SearchResult(
                source_id=self.source_id,
                title="LH 규정 DB 미구축",
                content="LH 규정 BM25 인덱스가 없습니다. scripts/build_index.py를 실행하세요.",
            )]

        bm25 = self._bm25
        id_to_idx = self._id_to_idx

        # BM25·Dense 병렬 실행
        bm25_task = loop.run_in_executor(
            None, lambda: bm25_search(bm25, query, top_k=TOP_K_CANDIDATES)
        )
        dense_task = loop.run_in_executor(None, lambda: self._dense_search(query))
        bm25_hits, dense_hits = await asyncio.gather(bm25_task, dense_task)

        # RRF 결합 (Dense 없으면 BM25 top-N만)
        if dense_hits:
            top_ids = _rrf(bm25_hits, dense_hits)
            logger.debug(
                "RRF: BM25 %d개 + Dense %d개 → top %d",
                len(bm25_hits), len(dense_hits), len(top_ids),
            )
        else:
            top_ids = [cid for cid, _ in bm25_hits[:TOP_K_FINAL]]

        output = []
        for cid in top_ids:
            idx = id_to_idx.get(cid)
            if idx is None:
                continue
            doc = bm25.corpus[idx]
            meta = bm25.metadatas[idx]
            title = meta.get("title") or cid.rsplit("__", 1)[0]
            url = meta.get("url", "")
            output.append(SearchResult(
                source_id=self.source_id,
                title=title,
                content=doc,
                url=url,
                metadata=meta,
            ))
        return output
