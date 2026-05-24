"""
ChromaDB 증분 동기화 로직.

제목(title)을 기본 키로 사용하며, RSS pubDate가 저장된 것보다
최신인 경우에만 청크를 삭제 후 재삽입합니다.
"""

import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import chromadb
import httpx
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

# 프로젝트 루트를 경로에 추가 (직접 실행 시)
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from crawler.lh_crawler import LHDocumentFetcher, RssItem, parse_rss_feed
from crawler.bm25_index import build_and_save

_markdown_dir: Path | None = None


def _get_markdown_dir() -> Path:
    global _markdown_dir
    if _markdown_dir is None:
        _markdown_dir = Path(settings.markdown_path)
        _markdown_dir.mkdir(parents=True, exist_ok=True)
    return _markdown_dir


def _save_markdown(title_key: str, pub_date: datetime, text: str) -> Path:
    """변환된 마크다운을 data/markdown/{YYMMDD}_{title_key}.md 에 저장합니다.

    같은 title_key의 기존 파일(날짜가 다른 구버전)을 먼저 삭제합니다.
    """
    md_dir = _get_markdown_dir()
    for old in md_dir.glob(f"*_{title_key}.md"):
        old.unlink()
    date_str = pub_date.strftime("%y%m%d")
    path = md_dir / f"{date_str}_{title_key}.md"
    path.write_text(text, encoding="utf-8")
    return path


_SKIP_SUFFIXES = ("예고", "안내문")
_SKIP_PATTERNS = re.compile(
    r"(일부개정|전부개정|개정안|개정\(안\)|규정\s*제\d+호|개정\s*시행)$"
)


def _should_skip(title: str) -> bool:
    """다음 경우 인덱싱에서 제외합니다.
    - 제목 끝이 '예고' 또는 '안내문'
    - 개정안·개정(안)·일부개정·규정 제N호 공고 형식
    """
    t = title.strip()
    if t.endswith(_SKIP_SUFFIXES):
        return True
    if _SKIP_PATTERNS.search(t):
        return True
    return False

logger = logging.getLogger(__name__)

CHUNK_SIZE = 800       # 조문 단위 청킹 시 상한 (자)
CHUNK_OVERLAP = 100   # 고정 크기 폴백 사용 시 겹침


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0


# ── ID / 청크 유틸리티 ─────────────────────────────────────────────────────────

def title_to_key(title: str) -> str:
    """제목을 ChromaDB ID 접두어로 변환합니다 (최대 80자).

    공백·특수문자를 제거(언더스코어 치환 아님)하여
    '경영심의회운영규정'과 '경영심의회 운영규정'을 동일 키로 매핑합니다.
    RSS 업데이트 시 제목 표기가 미세하게 바뀌어도 중복 저장을 방지합니다.
    """
    safe = re.sub(r"[^\w가-힣]", "", title.strip())
    return safe[:80]


def chunk_id(title_key: str, idx: int) -> str:
    """청크 고유 ID: {title_key}__c{idx:04d}"""
    return f"{title_key}__c{idx:04d}"


def chunk_text(text: str) -> list[str]:
    """marker 마크다운 출력을 헤딩 단위로 청킹합니다.

    우선순위:
    1. ## 헤딩 기준 (조문 단위 — marker가 '제N조'를 ## 로 변환)
    2. # 헤딩 기준 (장·편 단위)
    3. 빈줄(단락) 기준
    4. 고정 크기 폴백 (CHUNK_SIZE)

    각 청크가 CHUNK_SIZE를 초과하면 고정 크기로 재분할합니다.
    """
    for splitter in (_split_by_h2, _split_by_h1, _split_by_paragraph):
        chunks = splitter(text)
        if len(chunks) >= 2:
            break
    else:
        return _split_fixed(text)

    result = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        if len(chunk) > CHUNK_SIZE:
            result.extend(_split_fixed(chunk))
        else:
            result.append(chunk)
    return result or [text]


# ── 분할 전략 ──────────────────────────────────────────────────────────────────

_H2_RE = re.compile(r"(?=^## )", re.MULTILINE)
_H1_RE = re.compile(r"(?=^# )", re.MULTILINE)


def _split_by_h2(text: str) -> list[str]:
    """'## ' 헤딩 기준 분할 (조문 단위)."""
    parts = _H2_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def _split_by_h1(text: str) -> list[str]:
    """'# ' 헤딩 기준 분할 (장·편 단위)."""
    parts = _H1_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def _split_by_paragraph(text: str) -> list[str]:
    """빈줄(\\n\\n) 기준 단락 분할."""
    parts = re.split(r"\n{2,}", text)
    return [p.strip() for p in parts if p.strip()]


def _split_fixed(text: str) -> list[str]:
    """고정 크기 분할 (폴백)."""
    if len(text) <= CHUNK_SIZE:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + CHUNK_SIZE])
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


# ── ChromaDB 헬퍼 ──────────────────────────────────────────────────────────────

def get_collection() -> chromadb.Collection:
    embed_fn = SentenceTransformerEmbeddingFunction(
        model_name=settings.embedding_model,
        trust_remote_code=True,
    )
    client = chromadb.PersistentClient(path=settings.chroma_path)
    return client.get_or_create_collection(
        name=settings.chroma_collection,
        embedding_function=embed_fn,
        metadata={"hnsw:space": "cosine"},
    )


def get_stored_pub_date(
    collection: chromadb.Collection, title_key: str
) -> datetime | None:
    """저장된 첫 번째 청크의 pub_date를 반환합니다. 없으면 None."""
    try:
        result = collection.get(
            ids=[chunk_id(title_key, 0)], include=["metadatas"]
        )
        if result["metadatas"]:
            stored = result["metadatas"][0].get("pub_date", "")
            if stored:
                return datetime.fromisoformat(stored)
    except Exception:
        pass
    return None


def delete_existing_chunks(collection: chromadb.Collection, title: str) -> None:
    """동일 title의 모든 청크를 삭제합니다."""
    collection.delete(where={"title": {"$eq": title}})


def upsert_chunks(
    collection: chromadb.Collection,
    item: RssItem,
    chunks: list[str],
) -> None:
    key = title_to_key(item.title)
    pub_date_iso = item.pub_date.isoformat()
    n = len(chunks)
    collection.upsert(
        ids=[chunk_id(key, i) for i in range(n)],
        documents=chunks,
        metadatas=[
            {
                "title": item.title,
                "url": item.link,
                "pub_date": pub_date_iso,
                "chunk_index": i,
                "total_chunks": n,
            }
            for i in range(n)
        ],
    )


# ── 핵심 동기화 함수 ───────────────────────────────────────────────────────────

async def _fetch_rss_items(rss_url: str) -> list[RssItem]:
    async with httpx.AsyncClient(
        timeout=15.0,
        verify=False,
        headers={"User-Agent": "Mozilla/5.0 (compatible; LH-RAG-Bot/1.0)"},
    ) as client:
        resp = await client.get(rss_url)
        resp.raise_for_status()
    return parse_rss_feed(resp.text)


async def sync_from_rss(
    rss_url: str,
    limit: int | None = None,
) -> SyncResult:
    """RSS 피드를 기준으로 ChromaDB를 증분 동기화합니다.

    동작 방식:
    - RSS 항목의 title을 기본 키로 사용
    - 저장된 pub_date보다 RSS pub_date가 최신이면 청크 전체 교체
    - 동일하거나 오래된 경우 스킵
    - limit: 처리할 최대 항목 수 (None이면 전체)
    """
    result = SyncResult()

    logger.info("RSS 동기화 시작: %s", rss_url)
    try:
        items = await _fetch_rss_items(rss_url)
    except Exception as e:
        logger.error("RSS 수신 실패: %s", e)
        return result

    if not items:
        logger.warning("RSS 항목 없음")
        return result

    if limit:
        items = items[:limit]

    # ── 사전 스캔: 처리 대상 분류 ─────────────────────────────────────────
    collection = get_collection()

    class _Plan:
        def __init__(self, item: RssItem, is_update: bool):
            self.item = item
            self.is_update = is_update

    to_process: list[_Plan] = []
    n_skip_filter = 0
    n_skip_fresh = 0

    for item in items:
        if _should_skip(item.title):
            n_skip_filter += 1
            continue
        key = title_to_key(item.title)
        stored_date = get_stored_pub_date(collection, key)
        if stored_date is not None and stored_date >= item.pub_date:
            n_skip_fresh += 1
            continue
        to_process.append(_Plan(item, is_update=(stored_date is not None)))

    n_new = sum(1 for p in to_process if not p.is_update)
    n_upd = sum(1 for p in to_process if p.is_update)
    print(
        f"\n[인덱싱 계획]  전체 {len(items)}건"
        f"  →  신규 {n_new}건  업데이트 {n_upd}건  "
        f"스킵 {n_skip_fresh}건  제외(예고/안내문) {n_skip_filter}건\n"
    )
    result.skipped = n_skip_fresh + n_skip_filter

    if not to_process:
        logger.info("처리할 문서 없음 — 모두 최신 상태입니다.")
        return result

    # ── 처리 루프 ─────────────────────────────────────────────────────────
    from tqdm import tqdm

    fetcher = LHDocumentFetcher()
    try:
        pbar = tqdm(to_process, desc="인덱싱", unit="건", dynamic_ncols=True)
        for plan in pbar:
            item = plan.item
            pbar.set_postfix_str(item.title[:30])

            text = await fetcher.fetch_text(item.link)
            if not text:
                logger.warning("텍스트 추출 실패, 스킵: %s", item.title)
                result.failed += 1
                continue

            key = title_to_key(item.title)
            _save_markdown(key, item.pub_date, text)

            if plan.is_update:
                delete_existing_chunks(collection, item.title)
            upsert_chunks(collection, item, chunk_text(text))

            result.updated += 1 if plan.is_update else 0
            result.added += 0 if plan.is_update else 1

            await asyncio.sleep(0.3)  # 서버 부하 방지

    finally:
        await fetcher.aclose()

    # 신규·업데이트가 있을 때만 BM25 인덱스 재빌드
    if result.added or result.updated:
        logger.info("BM25 인덱스 재빌드 중...")
        all_chunks = collection.get(include=["documents"])
        build_and_save(all_chunks["ids"], all_chunks["documents"])
        logger.info("BM25 인덱스 재빌드 완료")

    logger.info(
        "동기화 완료 — 신규: %d, 업데이트: %d, 스킵: %d, 실패: %d",
        result.added, result.updated, result.skipped, result.failed,
    )
    return result
