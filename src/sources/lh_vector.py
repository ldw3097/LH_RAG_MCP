"""LH 규정 검색 소스 — BM25(kiwipiepy) 검색."""

import asyncio
import logging
import threading

from src.sources.base import SearchResult, SearchSource
from crawler.bm25_index import BM25Store, load_bm25, bm25_search, warmup_kiwi

logger = logging.getLogger(__name__)

TOP_K_BM25 = 7


class LHVectorSource(SearchSource):
    source_id = "lh_vector_db"

    def __init__(self):
        self._bm25: BM25Store | None = None
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
            self._loaded = True

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

        sparse_hits = await loop.run_in_executor(
            None, lambda: bm25_search(bm25, query, top_k=TOP_K_BM25)
        )
        if not sparse_hits:
            return []

        output = []
        for cid, _ in sparse_hits:
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
