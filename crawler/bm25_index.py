"""
BM25 스파스 인덱스 — ChromaDB Dense 검색과 함께 하이브리드 검색에 사용.

파일 구조:
  data/bm25/{collection}.pkl  ← {ids: [...], corpus: [...], bm25: BM25Okapi}

갱신 단위:
  - upsert_chunks 호출마다 전체 인덱스를 재빌드 (청크 수가 수만 건 이하에서 충분히 빠름)
  - 문서 삭제 시에도 재빌드
"""

import logging
import pickle
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings

logger = logging.getLogger(__name__)

_kiwi = None


def _get_kiwi():
    global _kiwi
    if _kiwi is None:
        from kiwipiepy import Kiwi
        _kiwi = Kiwi()
        logger.info("Kiwi 형태소 분석기 로드 완료")
    return _kiwi


def tokenize(text: str) -> list[str]:
    """kiwipiepy로 명사/동사/형용사 어간 추출."""
    kiwi = _get_kiwi()
    tokens = []
    for token in kiwi.tokenize(text):
        # 명사(NN*), 동사 어간(VV/VA/VX), 외국어(SL)만 사용
        if token.tag.startswith(("NN", "VV", "VA", "VX", "SL", "XR")):
            tokens.append(token.form)
    return tokens or text.split()   # 폴백: 공백 분리


class BM25Store(NamedTuple):
    ids: list[str]          # chunk ID 목록 (ChromaDB ID와 동일)
    corpus: list[str]       # 원문 청크 텍스트
    bm25: object            # BM25Okapi 인스턴스


def _index_path(collection: str) -> Path:
    p = Path(settings.chroma_path).parent / "bm25"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{collection}.pkl"


def load_bm25(collection: str | None = None) -> BM25Store | None:
    """저장된 BM25 인덱스를 로드합니다. 없으면 None 반환."""
    col = collection or settings.chroma_collection
    path = _index_path(col)
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            store = pickle.load(f)
        logger.info("BM25 인덱스 로드: %d청크 (%s)", len(store.ids), path.name)
        return store
    except Exception as e:
        logger.warning("BM25 인덱스 로드 실패: %s", e)
        return None


def build_and_save(
    ids: list[str],
    corpus: list[str],
    collection: str | None = None,
) -> BM25Store:
    """청크 목록으로 BM25 인덱스를 빌드하고 저장합니다."""
    from rank_bm25 import BM25Okapi

    tokenized = [tokenize(doc) for doc in corpus]
    bm25 = BM25Okapi(tokenized)
    store = BM25Store(ids=ids, corpus=corpus, bm25=bm25)

    col = collection or settings.chroma_collection
    path = _index_path(col)
    with path.open("wb") as f:
        pickle.dump(store, f)
    logger.info("BM25 인덱스 저장: %d청크 → %s", len(ids), path.name)
    return store


def rebuild_from_chroma(collection_name: str | None = None) -> BM25Store:
    """ChromaDB 컬렉션 전체를 읽어 BM25 인덱스를 재빌드합니다."""
    import chromadb
    from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

    col_name = collection_name or settings.chroma_collection
    embed_fn = SentenceTransformerEmbeddingFunction(
        model_name=settings.embedding_model,
        trust_remote_code=True,
    )
    client = chromadb.PersistentClient(path=settings.chroma_path)
    col = client.get_collection(name=col_name, embedding_function=embed_fn)

    result = col.get(include=["documents"])
    ids: list[str] = result["ids"]
    corpus: list[str] = result["documents"]
    return build_and_save(ids, corpus, col_name)


def bm25_search(
    store: BM25Store,
    query: str,
    top_k: int = 20,
) -> list[tuple[str, float]]:
    """BM25 스코어 기준 상위 top_k 청크를 반환합니다.

    Returns:
        [(chunk_id, score), ...] 내림차순 정렬
    """
    tokens = tokenize(query)
    scores = store.bm25.get_scores(tokens)
    ranked = sorted(
        enumerate(scores), key=lambda x: x[1], reverse=True
    )[:top_k]
    return [(store.ids[i], float(s)) for i, s in ranked if s > 0]
