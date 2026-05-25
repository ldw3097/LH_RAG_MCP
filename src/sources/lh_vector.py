"""
LH 규정 벡터DB 검색 소스 — Dense + BM25 하이브리드 (RRF 융합).

검색 흐름:
  1. Dense: ChromaDB 코사인 유사도로 상위 TOP_K_DENSE 후보 수집
  2. Sparse: BM25 스코어로 상위 TOP_K_SPARSE 후보 수집
  3. RRF(k=60)로 두 랭킹을 합산, 상위 TOP_K_FINAL 반환
"""

import asyncio
import logging
import threading

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

from src.config import settings
from src.sources.base import SearchResult, SearchSource
from crawler.bm25_index import BM25Store, load_bm25, bm25_search

logger = logging.getLogger(__name__)

TOP_K_DENSE = 30    # Dense 후보 수
TOP_K_SPARSE = 30   # BM25 후보 수
TOP_K_FINAL = 15    # 소스 후보 수 (server.py에서 재랭킹 후 최종 10개로 축소)
RRF_K = 60          # RRF 평활화 상수 (논문 기본값)


def _rrf_score(rank: int, k: int = RRF_K) -> float:
    return 1.0 / (k + rank + 1)


def _reciprocal_rank_fusion(
    dense_ids: list[str],
    sparse_ids: list[str],
) -> list[str]:
    """두 랭킹 리스트를 RRF로 합산해 정렬된 ID 목록을 반환합니다."""
    scores: dict[str, float] = {}
    for rank, cid in enumerate(dense_ids):
        scores[cid] = scores.get(cid, 0.0) + _rrf_score(rank)
    for rank, cid in enumerate(sparse_ids):
        scores[cid] = scores.get(cid, 0.0) + _rrf_score(rank)
    return sorted(scores, key=lambda x: scores[x], reverse=True)


class LHVectorSource(SearchSource):
    source_id = "lh_vector_db"

    def __init__(self):
        self._client: chromadb.PersistentClient | None = None
        self._collection: chromadb.Collection | None = None
        self._embed_fn: SentenceTransformerEmbeddingFunction | None = None
        self._bm25: BM25Store | None = None
        self._lock = threading.Lock()

    def _ensure_loaded(self):
        with self._lock:
            if self._collection is not None:
                return

        self._embed_fn = SentenceTransformerEmbeddingFunction(
            model_name=settings.embedding_model,
            trust_remote_code=True,
        )
        self._client = chromadb.PersistentClient(path=settings.chroma_path)
        try:
            self._collection = self._client.get_collection(
                name=settings.chroma_collection,
                embedding_function=self._embed_fn,
            )
            logger.info("LH 벡터DB 로드 완료: %d청크", self._collection.count())
        except Exception:
            logger.warning(
                "LH 벡터DB 컬렉션 '%s' 없음. build_index.py를 먼저 실행하세요.",
                settings.chroma_collection,
            )
            self._collection = None
            return

        self._bm25 = load_bm25()
        if self._bm25:
            logger.info("BM25 인덱스 로드 완료: %d청크", len(self._bm25.ids))
        else:
            logger.warning("BM25 인덱스 없음 — Dense 전용 검색으로 동작합니다.")

    async def search(self, query: str) -> list[SearchResult]:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._ensure_loaded)
        if self._collection is None:
            return [SearchResult(
                source_id=self.source_id,
                title="LH 규정 DB 미구축",
                content="LH 규정 벡터DB가 아직 구축되지 않았습니다. scripts/build_index.py를 실행하세요.",
            )]

        # ── 1. Dense 검색 ──────────────────────────────────────────────────
        collection = self._collection
        dense_result = await loop.run_in_executor(
            None,
            lambda: collection.query(
                query_texts=[query],
                n_results=TOP_K_DENSE,
                include=["documents", "metadatas", "distances"],
            ),
        )
        dense_ids: list[str] = dense_result["ids"][0]
        dense_docs: dict[str, str] = dict(
            zip(dense_ids, dense_result["documents"][0])
        )
        dense_metas: dict[str, dict] = dict(
            zip(dense_ids, dense_result["metadatas"][0])
        )
        dense_dists: dict[str, float] = dict(
            zip(dense_ids, dense_result["distances"][0])
        )

        # ── 2. BM25 검색 ───────────────────────────────────────────────────
        sparse_ids: list[str] = []
        if self._bm25:
            bm25 = self._bm25
            sparse_hits = await loop.run_in_executor(
                None, lambda: bm25_search(bm25, query, top_k=TOP_K_SPARSE)
            )
            sparse_ids = [cid for cid, _ in sparse_hits]

        # ── 3. RRF 융합 ────────────────────────────────────────────────────
        fused_ids = _reciprocal_rank_fusion(dense_ids, sparse_ids)[:TOP_K_FINAL]

        # ── 4. 청크 내용 조회 (BM25 전용 청크는 ChromaDB에서 추가 조회) ──
        missing = [cid for cid in fused_ids if cid not in dense_docs]
        if missing:
            extra = await loop.run_in_executor(
                None,
                lambda: collection.get(ids=missing, include=["documents", "metadatas"]),
            )
            for cid, doc, meta in zip(
                extra["ids"], extra["documents"], extra["metadatas"]
            ):
                dense_docs[cid] = doc
                dense_metas[cid] = meta

        # ── 5. 결과 조립 ───────────────────────────────────────────────────
        output = []
        for cid in fused_ids:
            doc = dense_docs.get(cid, "")
            meta = dense_metas.get(cid, {})
            dist = dense_dists.get(cid, 0.0)

            # Dense 결과 중 유사도가 너무 낮은 것은 BM25 전용 결과만 남김
            if cid in dense_dists and dist > 1.5:
                continue

            title = meta.get("title", "LH 규정")
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
