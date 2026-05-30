"""KCSC 건설기준 검색 소스 — BM25 + Dense 하이브리드 + 인용 그래프 1-hop 확장.

건설기준은 상호 인용이 활발하고 인용이 조문(절·항) 단위로 가는 경우가 많다.
1차 하이브리드 검색 결과의 각 조문이 인용하는 대상 조문을 그래프에서 1-hop 확장하여
함께 반환한다.
"""

import asyncio
import logging
import threading

from src.sources.base import SearchResult, SearchSource
from src.sources.lh_vector import _rrf, TOP_K_CANDIDATES, TOP_K_FINAL
from src.config import settings
from crawler.bm25_index import BM25Store, load_bm25, bm25_search, warmup_kiwi
from crawler.dense_index import DenseStore, load_dense, embed_query, dense_search
from crawler.kcsc_indexer import KcscGraph, load_graph

logger = logging.getLogger(__name__)

MAX_CITATION_HOPS = 3   # 1-hop 확장으로 추가할 최대 청크 수


class KCSCVectorSource(SearchSource):
    source_id = "kcsc_vector_db"

    def __init__(self, collection: str | None = None):
        self._collection = collection or settings.kcsc_bm25_collection
        self._bm25: BM25Store | None = None
        self._dense: DenseStore | None = None
        self._graph: KcscGraph | None = None
        self._id_to_idx: dict[str, int] = {}
        self._dense_idx: dict[str, int] = {}
        self._lock = threading.Lock()
        self._loaded = False

    def _ensure_loaded(self):
        with self._lock:
            if self._loaded:
                return
            self._bm25 = load_bm25(self._collection)
            if self._bm25:
                self._id_to_idx = {cid: i for i, cid in enumerate(self._bm25.ids)}
                logger.info("KCSC BM25 로드: %d청크", len(self._bm25.ids))
                warmup_kiwi()
            else:
                logger.warning("KCSC BM25 인덱스 없음 — build_kcsc_index.py를 실행하세요.")

            self._dense = load_dense(self._collection)
            if self._dense:
                self._dense_idx = {cid: i for i, cid in enumerate(self._dense.ids)}
            else:
                logger.warning("KCSC Dense 인덱스 없음 — BM25만 사용합니다.")

            self._graph = load_graph()
            if not self._graph:
                logger.warning("KCSC 인용 그래프 없음 — 1-hop 확장 비활성화.")

            self._loaded = True

    def _dense_search(self, q_vec):
        if not self._dense or q_vec is None:
            return []
        return dense_search(self._dense, q_vec, top_k=TOP_K_CANDIDATES)

    def _make_result(self, cid: str, via: str = "", citing: str = "") -> SearchResult | None:
        idx = self._id_to_idx.get(cid)
        if idx is None:
            return None
        meta = dict(self._bm25.metadatas[idx])
        title = meta.get("title") or cid
        label = meta.get("section_label")
        ctype = meta.get("code_type", "")
        code = meta.get("code", "")
        head = f"{ctype} {code} {title}" + (f" §{label}" if label else "")
        if via == "citation":
            meta["via"] = "citation"
            head = f"[인용 참조: {citing} → {head}]"
        return SearchResult(
            source_id=self.source_id,
            title=head,
            content=self._bm25.corpus[idx],
            url=meta.get("url", ""),
            metadata=meta,
        )

    def _best_chunk(self, chunk_ids: list[str], q_vec) -> str | None:
        """노드의 청크들 중 쿼리와 가장 잘 맞는 청크 1개. dense 없으면 첫 청크."""
        if not chunk_ids:
            return None
        if q_vec is None or not self._dense:
            return chunk_ids[0]
        best, best_score = chunk_ids[0], -1.0
        for cid in chunk_ids:
            di = self._dense_idx.get(cid)
            if di is None:
                continue
            score = float(self._dense.embeddings[di] @ q_vec)
            if score > best_score:
                best, best_score = cid, score
        return best

    def _expand_citations(self, primary_ids: list[str], q_vec) -> list[SearchResult]:
        """1차 결과 조문이 인용하는 대상 조문을 1-hop 확장합니다."""
        graph = self._graph
        if not graph:
            return []

        # 1차 결과의 조문 노드 / 문서 노드 집합
        primary_nodes: set[str] = set()
        primary_docs: set[str] = set()
        for cid in primary_ids:
            idx = self._id_to_idx.get(cid)
            if idx is None:
                continue
            meta = self._bm25.metadatas[idx]
            node = meta.get("node_id")
            if node:
                primary_nodes.add(node)
            ct, cd = meta.get("code_type"), meta.get("code")
            if ct and cd:
                primary_docs.add(f"{ct}:{cd}")

        # 인용 대상 수집 (citing_node → target_node)
        targets: list[tuple[str, str]] = []
        seen_targets: set[str] = set()
        for node in primary_nodes:
            for tgt in graph.edges.get(node, []):
                if tgt in seen_targets:
                    continue
                # 이미 1차 결과에 포함된 조문/문서면 스킵
                tgt_doc = ":".join(tgt.split(":")[:2])
                if tgt in primary_nodes or tgt_doc in primary_docs:
                    continue
                seen_targets.add(tgt)
                targets.append((node, tgt))

        # 대상 노드 → 대표 청크 선정
        scored: list[tuple[float, str, str]] = []  # (score, chunk_id, citing_node)
        added_cids: set[str] = set(primary_ids)
        for citing, tgt in targets:
            chunks = graph.node_to_chunks.get(tgt)
            if not chunks:
                # 조문 노드가 인덱스에 없으면 문서 노드로 폴백
                tgt_doc = ":".join(tgt.split(":")[:2])
                chunks = graph.node_to_chunks.get(tgt_doc)
            if not chunks:
                continue
            best = self._best_chunk([c for c in chunks if c not in added_cids], q_vec)
            if not best:
                continue
            added_cids.add(best)
            di = self._dense_idx.get(best) if self._dense else None
            score = float(self._dense.embeddings[di] @ q_vec) if (di is not None and q_vec is not None) else 0.0
            scored.append((score, best, graph.node_names.get(citing, citing)))

        scored.sort(key=lambda x: x[0], reverse=True)
        results: list[SearchResult] = []
        for _, cid, citing in scored[:MAX_CITATION_HOPS]:
            r = self._make_result(cid, via="citation", citing=citing)
            if r:
                results.append(r)
        return results

    async def search(self, query: str, keywords: str) -> list[SearchResult]:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._ensure_loaded)

        if not self._bm25:
            return [SearchResult(
                source_id=self.source_id,
                title="KCSC 건설기준 DB 미구축",
                content="KCSC 인덱스가 없습니다. scripts/build_kcsc_index.py를 실행하세요.",
            )]

        # 쿼리 임베딩 (Dense·인용 확장에서 공용)
        q_vec = None
        if self._dense:
            try:
                q_vec = await loop.run_in_executor(None, lambda: embed_query(query))
            except Exception as e:
                logger.warning("Dense 임베딩 실패 (BM25만 사용): %s", e)

        bm25 = self._bm25
        bm25_task = loop.run_in_executor(
            None, lambda: bm25_search(bm25, keywords, top_k=TOP_K_CANDIDATES)
        )
        dense_task = loop.run_in_executor(None, lambda: self._dense_search(q_vec))
        bm25_hits, dense_hits = await asyncio.gather(bm25_task, dense_task)

        if dense_hits:
            top_ids = _rrf(bm25_hits, dense_hits)
        else:
            top_ids = [cid for cid, _ in bm25_hits[:TOP_K_FINAL]]

        output: list[SearchResult] = []
        for cid in top_ids:
            r = self._make_result(cid)
            if r:
                output.append(r)

        # 인용 그래프 1-hop 확장
        citation_results = await loop.run_in_executor(
            None, lambda: self._expand_citations(top_ids, q_vec)
        )
        output.extend(citation_results)
        return output
