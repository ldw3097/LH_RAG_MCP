import logging

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

from src.config import settings
from src.sources.base import SearchResult, SearchSource

logger = logging.getLogger(__name__)

TOP_K = 5


class LHVectorSource(SearchSource):
    source_id = "lh_vector_db"

    def __init__(self):
        self._client: chromadb.PersistentClient | None = None
        self._collection: chromadb.Collection | None = None
        self._embed_fn: SentenceTransformerEmbeddingFunction | None = None

    def _ensure_loaded(self):
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
            logger.info(
                "LH 벡터DB 로드 완료: %d개 문서", self._collection.count()
            )
        except Exception:
            logger.warning(
                "LH 벡터DB 컬렉션 '%s' 없음. build_index.py를 먼저 실행하세요.",
                settings.chroma_collection,
            )
            self._collection = None

    async def search(self, query: str) -> list[SearchResult]:
        self._ensure_loaded()
        if self._collection is None:
            return [SearchResult(
                source_id=self.source_id,
                title="LH 규정 DB 미구축",
                content="LH 규정 벡터DB가 아직 구축되지 않았습니다. scripts/build_index.py를 실행하세요.",
            )]

        results = self._collection.query(
            query_texts=[query],
            n_results=TOP_K,
            include=["documents", "metadatas", "distances"],
        )

        output = []
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        for doc, meta, dist in zip(docs, metas, distances):
            # 유사도가 너무 낮은 결과는 제외 (거리 > 1.5)
            if dist > 1.5:
                continue
            title = meta.get("title", "LH 규정")
            doc_type = meta.get("doc_type", "")
            effective_date = meta.get("effective_date", "")
            url = meta.get("url", "")
            header = f"{doc_type} | 시행: {effective_date}" if doc_type else ""
            output.append(SearchResult(
                source_id=self.source_id,
                title=title,
                content=f"{header}\n{doc}" if header else doc,
                url=url,
                metadata=meta,
            ))
        return output
