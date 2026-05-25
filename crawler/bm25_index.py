"""
BM25 스파스 인덱스 — LH 규정 검색의 유일한 저장소.

파일 구조:
  data/bm25/{collection}.pkl  ← BM25Store(ids, corpus, metadatas, bm25)

갱신 단위:
  - sync_from_rss 완료 시 전체 인덱스를 재빌드 (수만 건 이하에서 충분히 빠름)
  - 문서 삭제/업데이트 시에도 재빌드
"""

import logging
import pickle
import sys
from datetime import datetime
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
        if token.tag.startswith(("NN", "VV", "VA", "VX", "SL", "XR")):
            tokens.append(token.form)
    return tokens or text.split()


def warmup_kiwi() -> None:
    """Kiwi 형태소 분석기를 미리 초기화합니다 (첫 검색 지연 방지)."""
    _get_kiwi()
    logger.info("Kiwi 사전 초기화 완료")


class BM25Store(NamedTuple):
    ids: list[str]         # chunk ID 목록 (title_key__cNNNN)
    corpus: list[str]      # 원문 청크 텍스트
    metadatas: list[dict]  # 청크별 메타데이터 (title, url, pub_date 등)
    bm25: object           # BM25Okapi 인스턴스


def _index_path(collection: str) -> Path:
    p = Path(settings.bm25_path)
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{collection}.pkl"


def load_bm25(collection: str | None = None) -> BM25Store | None:
    """저장된 BM25 인덱스를 로드합니다. 없거나 형식이 다르면 None 반환."""
    col = collection or settings.bm25_collection
    path = _index_path(col)
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            store = pickle.load(f)
        if not isinstance(store, BM25Store):
            logger.warning("BM25 인덱스 형식 불일치 — build_index.py를 다시 실행하세요.")
            return None
        logger.info("BM25 인덱스 로드: %d청크 (%s)", len(store.ids), path.name)
        return store
    except Exception as e:
        logger.warning("BM25 인덱스 로드 실패 (%s) — build_index.py를 다시 실행하세요.", e)
        return None


def build_and_save(
    ids: list[str],
    corpus: list[str],
    metadatas: list[dict],
    collection: str | None = None,
) -> BM25Store:
    """청크 목록으로 BM25 인덱스를 빌드하고 저장합니다."""
    from rank_bm25 import BM25Okapi

    tokenized = [tokenize(doc) for doc in corpus]
    bm25 = BM25Okapi(tokenized)
    store = BM25Store(ids=ids, corpus=corpus, metadatas=metadatas, bm25=bm25)

    col = collection or settings.bm25_collection
    path = _index_path(col)
    with path.open("wb") as f:
        pickle.dump(store, f)
    logger.info("BM25 인덱스 저장: %d청크 → %s", len(ids), path.name)
    return store


def get_stored_pub_date(store: BM25Store | None, title_key: str) -> datetime | None:
    """저장된 문서의 pub_date를 반환합니다. 없으면 None."""
    if store is None:
        return None
    prefix = title_key + "__"
    for i, cid in enumerate(store.ids):
        if cid.startswith(prefix):
            pub = store.metadatas[i].get("pub_date", "")
            return datetime.fromisoformat(pub) if pub else None
    return None


def get_all_title_keys(store: BM25Store | None) -> dict[str, datetime]:
    """저장된 모든 title_key → pub_date 매핑을 반환합니다."""
    if store is None:
        return {}
    result: dict[str, datetime] = {}
    for i, cid in enumerate(store.ids):
        # chunk ID: {title_key}__c{idx:04d}
        key = cid.rsplit("__c", 1)[0]
        if key not in result:
            pub = store.metadatas[i].get("pub_date", "")
            if pub:
                result[key] = datetime.fromisoformat(pub)
    return result


def remove_doc_chunks(
    ids: list[str],
    corpus: list[str],
    metadatas: list[dict],
    title_key: str,
) -> tuple[list[str], list[str], list[dict]]:
    """특정 문서(title_key)의 청크를 목록에서 제거합니다."""
    prefix = title_key + "__"
    keep = [i for i, cid in enumerate(ids) if not cid.startswith(prefix)]
    return (
        [ids[i] for i in keep],
        [corpus[i] for i in keep],
        [metadatas[i] for i in keep],
    )


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
