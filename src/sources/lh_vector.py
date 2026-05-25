"""
LH 규정 검색 소스 — BM25 스파스 검색 전용 (kiwipiepy 형태소 분석).

Dense(ChromaDB + SentenceTransformer) 검색은 비활성화:
fly.io shared CPU에서 임베딩 추론이 39초로 실용 불가.
BM25는 조문 번호·정확 키워드 검색에 강하며 ML 모델 불필요.

메타데이터(title, url, doc_type 등)는 ChromaDB에서 조회.
ChromaDB 없으면 BM25 corpus에서 직접 제공 (제목은 chunk ID에서 파싱).
"""

import asyncio
import logging
import threading

import chromadb

from src.config import settings
from src.sources.base import SearchResult, SearchSource
from crawler.bm25_index import BM25Store, load_bm25, bm25_search

logger = logging.getLogger(__name__)

TOP_K_BM25 = 15    # BM25 후보 수 (server.py에서 재랭킹 후 최종 10개로 축소)


class LHVectorSource(SearchSource):
    source_id = "lh_vector_db"

    def __init__(self):
        self._client: chromadb.PersistentClient | None = None
        self._collection = None
        self._bm25: BM25Store | None = None
        self._lock = threading.Lock()
        self._loaded = False

    def _ensure_loaded(self):
        with self._lock:
            if self._loaded:
                return

            self._client = chromadb.PersistentClient(path=settings.chroma_path)
            try:
                # embedding_function 없이 열기 — get()으로 메타데이터만 조회
                self._collection = self._client.get_collection(
                    name=settings.chroma_collection,
                )
                logger.info("LH ChromaDB 로드 완료: %d청크", self._collection.count())
            except Exception:
                logger.warning(
                    "LH ChromaDB 컬렉션 '%s' 없음 — BM25 corpus에서 직접 조회합니다.",
                    settings.chroma_collection,
                )

            self._bm25 = load_bm25()
            if self._bm25:
                logger.info("BM25 인덱스 로드 완료: %d청크", len(self._bm25.ids))
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

        # ── 1. BM25 검색 ───────────────────────────────────────────────────
        bm25 = self._bm25
        sparse_hits = await loop.run_in_executor(
            None, lambda: bm25_search(bm25, query, top_k=TOP_K_BM25)
        )
        if not sparse_hits:
            return []

        top_ids = [cid for cid, _ in sparse_hits]

        # ── 2. 메타데이터 조회 ─────────────────────────────────────────────
        if self._collection is not None:
            collection = self._collection
            extra = await loop.run_in_executor(
                None,
                lambda: collection.get(ids=top_ids, include=["documents", "metadatas"]),
            )
            id_to_doc = dict(zip(extra["ids"], extra["documents"]))
            id_to_meta = dict(zip(extra["ids"], extra["metadatas"]))
        else:
            # ChromaDB 없으면 BM25 corpus에서 직접 조회
            id_to_idx = {cid: i for i, cid in enumerate(bm25.ids)}
            id_to_doc = {cid: bm25.corpus[id_to_idx[cid]] for cid in top_ids if cid in id_to_idx}
            id_to_meta = {}

        # ── 3. 결과 조립 ───────────────────────────────────────────────────
        output = []
        for cid in top_ids:
            doc = id_to_doc.get(cid, "")
            meta = id_to_meta.get(cid, {})
            # title 없으면 chunk ID에서 파싱: "{title_key}__c{idx}"
            title = meta.get("title") or cid.rsplit("__", 1)[0]
            url = meta.get("url", "")
            doc_type = meta.get("doc_type", "")
            effective_date = meta.get("effective_date", "")
            header = f"{doc_type} | 시행: {effective_date}" if doc_type else ""
            output.append(SearchResult(
                source_id=self.source_id,
                title=title,
                content=f"{header}\n{doc}" if header else doc,
                url=url,
                metadata=meta,
            ))
        return output
