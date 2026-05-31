"""검색 평가 공용 헬퍼.

골든셋 라벨링(eval_collect)과 지표 계산(eval_retrieval)이 동일한 검색 파이프라인을
공유하도록 한다. 운영 소스의 search()는 반환 건수가 캡(cap)돼 있고 _rrf의
기본 인자가 정의 시점에 바인딩돼 폭을 넓히기 어렵다. 따라서 여기서는 BM25/Dense/RRF
프리미티브를 직접 호출해 임의의 top_k로 후보를 뽑는다.

KCSC는 운영과 동일하게 인용 그래프 1-hop 확장을 포함한다.

문서(조문) 단위 안정키:
  - lh   : metadata["title"]            (규정명)
  - kcsc : metadata["node_id"]          (= "code:label", 조문 단위) — 없으면 code
  - pps  : metadata["id"]               (법령해석일련번호)
청크 ID는 모두 "{base}__c{idx:04d}" 형식이라 재인덱싱 시 인덱스 위치가 바뀌므로,
정답 대조는 위 문서키 집합으로 한다.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.sources.lh_vector import _rrf
import src.sources.kcsc_vector as kcsc_mod
from crawler.bm25_index import BM25Store, load_bm25, bm25_search, warmup_kiwi
from crawler.dense_index import DenseStore, load_dense, embed_query, dense_search
from crawler.kcsc_indexer import KcscGraph, load_graph

# 운영 코드(lh_vector.TOP_K_CANDIDATES)와 동일하게 맞춤
CANDIDATES_K = 20


# source 키 → (BM25 collection, base_path)
SOURCES: dict[str, tuple[str, str]] = {
    "lh": (settings.bm25_collection, settings.bm25_path),
    "kcsc": (settings.kcsc_bm25_collection, settings.kcsc_data_path),
    "pps": (settings.pps_bm25_collection, settings.pps_data_path),
}


@dataclass
class Candidate:
    chunk_id: str
    doc_key: str       # 문서(조문) 단위 안정키
    title: str
    text: str
    score: float       # RRF(또는 단일 검색) 점수
    metadata: dict


def doc_key(source: str, meta: dict, chunk_id: str) -> str:
    """결과 1건을 문서(조문) 단위 안정키로 매핑."""
    if source == "lh":
        return meta.get("title") or chunk_id.rsplit("__c", 1)[0]
    if source == "kcsc":
        return (
            meta.get("node_id")
            or meta.get("code")
            or chunk_id.rsplit("__c", 1)[0]
        )
    if source == "pps":
        return str(meta.get("id") or chunk_id.rsplit("__c", 1)[0])
    return chunk_id.rsplit("__c", 1)[0]


class SourceIndex:
    """소스별 BM25/Dense 인덱스를 로드해 검색 프리미티브를 노출."""

    def __init__(self, source: str):
        if source not in SOURCES:
            raise ValueError(f"알 수 없는 source: {source} (가능: {list(SOURCES)})")
        self.source = source
        collection, base_path = SOURCES[source]
        self.bm25: BM25Store | None = load_bm25(collection, base_path=base_path)
        if self.bm25 is None:
            raise SystemExit(
                f"[{source}] BM25 인덱스가 없습니다. 먼저 build 스크립트를 실행하세요."
            )
        self._id_to_idx = {cid: i for i, cid in enumerate(self.bm25.ids)}
        warmup_kiwi()
        self.dense: DenseStore | None = load_dense(collection, base_path=base_path)

        # KCSC 전용: 인용 그래프 + dense 인덱스 맵
        self._graph: KcscGraph | None = None
        self._dense_idx: dict[str, int] = {}
        if source == "kcsc":
            self._graph = load_graph()
            if self.dense:
                self._dense_idx = {cid: i for i, cid in enumerate(self.dense.ids)}

    def _make_candidate(self, chunk_id: str, score: float) -> "Candidate | None":
        idx = self._id_to_idx.get(chunk_id)
        if idx is None:
            return None
        meta = self.bm25.metadatas[idx]
        return Candidate(
            chunk_id=chunk_id,
            doc_key=doc_key(self.source, meta, chunk_id),
            title=meta.get("title") or chunk_id.rsplit("__c", 1)[0],
            text=self.bm25.corpus[idx],
            score=score,
            metadata=meta,
        )

    def _best_chunk(self, chunk_ids: list[str], q_vec) -> "str | None":
        if not chunk_ids:
            return None
        if q_vec is None or not self.dense:
            return chunk_ids[0]
        best, best_score = chunk_ids[0], -1.0
        for cid in chunk_ids:
            di = self._dense_idx.get(cid)
            if di is None:
                continue
            score = float(self.dense.embeddings[di] @ q_vec)
            if score > best_score:
                best, best_score = cid, score
        return best

    def _expand_citations(self, primary_ids: list[str], q_vec) -> list["Candidate"]:
        """KCSC 전용: 1차 결과가 인용하는 조문을 1-hop 확장."""
        graph = self._graph
        if not graph:
            return []

        primary_nodes: set[str] = set()
        primary_docs: set[str] = set()
        for cid in primary_ids:
            idx = self._id_to_idx.get(cid)
            if idx is None:
                continue
            meta = self.bm25.metadatas[idx]
            node = meta.get("node_id")
            if node:
                primary_nodes.add(node)
            ct, cd = meta.get("code_type"), meta.get("code")
            if ct and cd:
                primary_docs.add(f"{ct}:{cd}")

        targets: list[tuple[str, str]] = []
        seen_targets: set[str] = set()
        for node in primary_nodes:
            for tgt in graph.edges.get(node, []):
                if tgt in seen_targets:
                    continue
                tgt_doc = ":".join(tgt.split(":")[:2])
                if tgt in primary_nodes or tgt_doc in primary_docs:
                    continue
                seen_targets.add(tgt)
                targets.append((node, tgt))

        scored: list[tuple[float, str]] = []
        added_cids: set[str] = set(primary_ids)
        for _, tgt in targets:
            chunks = graph.node_to_chunks.get(tgt)
            if not chunks:
                tgt_doc = ":".join(tgt.split(":")[:2])
                chunks = graph.node_to_chunks.get(tgt_doc)
            if not chunks:
                continue
            best = self._best_chunk([c for c in chunks if c not in added_cids], q_vec)
            if not best:
                continue
            added_cids.add(best)
            di = self._dense_idx.get(best) if self.dense else None
            score = float(self.dense.embeddings[di] @ q_vec) if (di is not None and q_vec is not None) else 0.0
            scored.append((score, best))

        scored.sort(key=lambda x: x[0], reverse=True)
        out: list[Candidate] = []
        for _, cid in scored[:kcsc_mod.MAX_CITATION_HOPS]:
            c = self._make_candidate(cid, 0.0)
            if c:
                c.metadata = dict(c.metadata)  # 원본 dict 보호
                c.metadata["via"] = "citation"
                out.append(c)
        return out

    def retrieve(
        self,
        query: str,
        keywords: str,
        top_k: int,
        mode: str = "hybrid",
        candidates_k: int = CANDIDATES_K,
    ) -> list["Candidate"]:
        """mode: hybrid | bm25 | dense. 청크 단위 상위 top_k 후보 반환."""
        bm25_hits = (
            bm25_search(self.bm25, keywords, top_k=candidates_k)
            if mode in ("hybrid", "bm25")
            else []
        )
        q_vec = None
        dense_hits: list[tuple[str, float]] = []
        if mode in ("hybrid", "dense") and self.dense is not None:
            try:
                q_vec = embed_query(query)
                dense_hits = dense_search(self.dense, q_vec, top_k=candidates_k)
            except Exception as e:  # noqa: BLE001
                print(f"[경고] Dense 검색 실패 ({self.source}): {e}", file=sys.stderr)

        if mode == "bm25":
            ranked = [cid for cid, _ in bm25_hits[:top_k]]
        elif mode == "dense":
            ranked = [cid for cid, _ in dense_hits[:top_k]]
        else:  # hybrid
            if dense_hits:
                ranked = _rrf(bm25_hits, dense_hits, top_n=top_k)
            else:
                ranked = [cid for cid, _ in bm25_hits[:top_k]]

        out: list[Candidate] = []
        for cid in ranked:
            c = self._make_candidate(cid, 0.0)
            if c:
                out.append(c)

        # KCSC: 인용 그래프 1-hop 확장
        if self.source == "kcsc":
            out.extend(self._expand_citations(ranked, q_vec))

        return out

    def retrieve_doc_keys(
        self, query: str, keywords: str, top_k: int, mode: str = "hybrid"
    ) -> list[str]:
        """문서키 기준 순위 리스트(중복 문서키는 최상위 1회만).

        KCSC의 경우 1차 결과(top_k개)에 citation 결과를 추가로 붙인다.
        citation은 top_k 상한에 포함되지 않아 골든셋에서 citation 경유 정답도 평가된다.
        """
        candidates = self.retrieve(query, keywords, top_k=top_k, mode=mode)

        # 1차 결과(citation 아닌 것) → top_k 제한 적용
        primary = [c for c in candidates if not c.metadata.get("via")]
        citation = [c for c in candidates if c.metadata.get("via") == "citation"]

        seen: set[str] = set()
        keys: list[str] = []
        for c in primary:
            if c.doc_key in seen:
                continue
            seen.add(c.doc_key)
            keys.append(c.doc_key)
            if len(keys) >= top_k:
                break

        # citation은 top_k 이후에 추가 (상한 없음)
        for c in citation:
            if c.doc_key not in seen:
                seen.add(c.doc_key)
                keys.append(c.doc_key)

        return keys
