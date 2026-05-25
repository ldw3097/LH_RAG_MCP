"""
Dense 임베딩 인덱스 — DeepInfra API (Qwen3-Embedding-0.6B) 사용.

파일 구조:
  data/bm25/{collection}_dense.pkl  ← DenseStore(ids, embeddings)

빌드 타임: 청크 전체를 배치 임베딩 후 저장
쿼리 타임: 쿼리 1개 임베딩 → numpy 코사인 유사도 (로컬)
"""

import logging
import pickle
import sys
from pathlib import Path
from typing import NamedTuple

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings

logger = logging.getLogger(__name__)

DEEPINFRA_EMBED_URL = "https://api.deepinfra.com/v1/openai/embeddings"
BATCH_SIZE = 100

# Qwen3-Embedding 검색 태스크용 instruction prefix
QUERY_INSTRUCTION = (
    "Instruct: 한국 법령·규정 관련 문서를 검색합니다.\nQuery: "
)


class DenseStore(NamedTuple):
    ids: list[str]
    embeddings: np.ndarray  # (N, D) float32, L2 정규화 완료


def _index_path(collection: str) -> Path:
    p = Path(settings.bm25_path)
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{collection}_dense.pkl"


def load_dense(collection: str | None = None) -> DenseStore | None:
    """저장된 Dense 인덱스를 로드합니다. 없거나 형식이 다르면 None 반환."""
    col = collection or settings.bm25_collection
    path = _index_path(col)
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            store = pickle.load(f)
        if not isinstance(store, DenseStore):
            logger.warning("Dense 인덱스 형식 불일치 — build_index.py를 다시 실행하세요.")
            return None
        logger.info(
            "Dense 인덱스 로드: %d청크 (dim=%d, %s)",
            len(store.ids), store.embeddings.shape[1], path.name,
        )
        return store
    except Exception as e:
        logger.warning("Dense 인덱스 로드 실패 (%s)", e)
        return None


def _embed_texts(texts: list[str], api_key: str, model: str) -> np.ndarray:
    """텍스트 배치를 L2 정규화된 임베딩 벡터로 변환."""
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(
            DEEPINFRA_EMBED_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "input": texts},
        )
        resp.raise_for_status()
    items = sorted(resp.json()["data"], key=lambda x: x["index"])
    vecs = np.array([item["embedding"] for item in items], dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return vecs / norms


def build_and_save_dense(
    ids: list[str],
    corpus: list[str],
    collection: str | None = None,
) -> DenseStore:
    """청크 목록을 임베딩하고 Dense 인덱스를 저장합니다.

    DEEPINFRA_API_KEY가 없으면 ValueError를 발생시킵니다.
    호출 전에 settings.deepinfra_api_key 유무를 확인하세요.
    """
    api_key = settings.deepinfra_api_key
    model = settings.embedding_model
    if not api_key:
        raise ValueError("DEEPINFRA_API_KEY가 설정되지 않았습니다.")

    logger.info("Dense 인덱스 빌드 시작: %d청크, 모델=%s", len(ids), model)
    all_vecs: list[np.ndarray] = []
    for i in range(0, len(corpus), BATCH_SIZE):
        batch = corpus[i : i + BATCH_SIZE]
        vecs = _embed_texts(batch, api_key, model)
        all_vecs.append(vecs)
        logger.debug("임베딩 진행: %d/%d", min(i + BATCH_SIZE, len(corpus)), len(corpus))

    embeddings = np.concatenate(all_vecs, axis=0)
    store = DenseStore(ids=ids, embeddings=embeddings)

    col = collection or settings.bm25_collection
    path = _index_path(col)
    with path.open("wb") as f:
        pickle.dump(store, f)
    logger.info(
        "Dense 인덱스 저장: %d청크, dim=%d → %s",
        len(ids), embeddings.shape[1], path.name,
    )
    return store


def update_dense_incremental(
    remove_keys: set[str],
    add_ids: list[str],
    add_corpus: list[str],
    collection: str | None = None,
) -> DenseStore:
    """Dense 인덱스를 증분 갱신합니다.

    remove_keys 에 해당하는 기존 청크 임베딩을 제거하고,
    add_corpus 만 새로 임베딩하여 병합합니다.
    전체 재빌드 대비 API 호출 횟수가 대폭 줄어듭니다.
    """
    api_key = settings.deepinfra_api_key
    model = settings.embedding_model
    if not api_key:
        raise ValueError("DEEPINFRA_API_KEY가 설정되지 않았습니다.")

    existing = load_dense(collection)

    if existing and remove_keys:
        keep = [
            i for i, cid in enumerate(existing.ids)
            if cid.rsplit("__c", 1)[0] not in remove_keys
        ]
        kept_ids: list[str] = [existing.ids[i] for i in keep]
        kept_embs: np.ndarray = existing.embeddings[keep]
    elif existing:
        kept_ids = list(existing.ids)
        kept_embs = existing.embeddings
    else:
        kept_ids = []
        kept_embs = None

    if add_corpus:
        logger.info(
            "Dense 증분 임베딩: 신규 %d청크 (기존 유지 %d청크)",
            len(add_corpus), len(kept_ids),
        )
        new_vecs_parts: list[np.ndarray] = []
        for i in range(0, len(add_corpus), BATCH_SIZE):
            batch = add_corpus[i : i + BATCH_SIZE]
            new_vecs_parts.append(_embed_texts(batch, api_key, model))
            logger.debug("임베딩 진행: %d/%d", min(i + BATCH_SIZE, len(add_corpus)), len(add_corpus))
        new_vecs = np.concatenate(new_vecs_parts, axis=0)

        all_ids = kept_ids + add_ids
        all_embs = np.concatenate([kept_embs, new_vecs], axis=0) if kept_embs is not None else new_vecs
    else:
        all_ids = kept_ids
        all_embs = kept_embs if kept_embs is not None else np.empty((0, 1024), dtype=np.float32)

    store = DenseStore(ids=all_ids, embeddings=all_embs)
    col = collection or settings.bm25_collection
    path = _index_path(col)
    with path.open("wb") as f:
        pickle.dump(store, f)
    logger.info(
        "Dense 인덱스 저장: %d청크, dim=%d → %s",
        len(all_ids), all_embs.shape[1] if all_embs.size else 0, path.name,
    )
    return store


def embed_query(query: str) -> np.ndarray:
    """쿼리 1개를 L2 정규화된 임베딩 벡터로 변환합니다 (검색 타임)."""
    api_key = settings.deepinfra_api_key
    model = settings.embedding_model
    if not api_key:
        raise ValueError("DEEPINFRA_API_KEY가 설정되지 않았습니다.")
    return _embed_texts([QUERY_INSTRUCTION + query], api_key, model)[0]


def dense_search(
    store: DenseStore,
    query_vec: np.ndarray,
    top_k: int = 20,
) -> list[tuple[str, float]]:
    """L2 정규화된 벡터 내적(= 코사인 유사도)으로 상위 top_k 청크를 반환합니다."""
    scores: np.ndarray = store.embeddings @ query_vec
    top_idx = np.argsort(scores)[::-1][:top_k]
    return [(store.ids[i], float(scores[i])) for i in top_idx if scores[i] > 0]
