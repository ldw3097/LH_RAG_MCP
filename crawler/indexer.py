"""
BM25 + Dense 인덱스 동기화 로직.

흐름:
  1. crawl_to_markdown()  — RSS → markdown 저장 (인덱스 미변경)
                            중복 판단 기준: data/markdown/ 파일의 날짜
  2. sync_indexes_from_markdown()  — markdown dir ↔ BM25/Dense 정합성 검증
                                     추가·수정·삭제를 계산 후 인덱스 재빌드

sync_from_rss()는 두 단계를 순서대로 호출하는 편의 함수입니다.
"""

import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from crawler.lh_crawler import LHDocumentFetcher, RssItem, parse_rss_feed
from crawler.bm25_index import (
    build_and_save, get_all_title_keys, load_bm25, remove_doc_chunks,
)
from crawler.dense_index import update_dense_incremental

_markdown_dir: Path | None = None


def _get_markdown_dir() -> Path:
    global _markdown_dir
    if _markdown_dir is None:
        _markdown_dir = Path(settings.markdown_path)
        _markdown_dir.mkdir(parents=True, exist_ok=True)
    return _markdown_dir


def _save_markdown(title_key: str, pub_date: datetime, text: str) -> Path:
    """data/markdown/{YYMMDD}_{title_key}.md 에 저장.
    같은 title_key의 구버전 파일을 먼저 삭제합니다.
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
    t = title.strip()
    return t.endswith(_SKIP_SUFFIXES) or bool(_SKIP_PATTERNS.search(t))


logger = logging.getLogger(__name__)

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100


@dataclass
class CrawlResult:
    fetched: int = 0           # 새로 다운로드한 문서 수
    skipped_fresh: int = 0     # markdown 기준 이미 최신
    skipped_filter: int = 0    # 예고/안내문 필터로 제외
    failed: int = 0


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped: int = 0
    failed: int = 0


# ── ID / 청크 유틸리티 ─────────────────────────────────────────────────────────

def title_to_key(title: str) -> str:
    """제목을 BM25 ID 접두어로 변환합니다 (최대 80자).

    공백·특수문자를 제거하여 '경영심의회운영규정'과 '경영심의회 운영규정'을
    동일 키로 매핑합니다.
    """
    safe = re.sub(r"[^\w가-힣]", "", title.strip())
    return safe[:80]


def chunk_id(title_key: str, idx: int) -> str:
    return f"{title_key}__c{idx:04d}"


def chunk_text(text: str) -> list[str]:
    """마크다운을 헤딩 단위로 청킹합니다."""
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
    return [p.strip() for p in _H2_RE.split(text) if p.strip()]


def _split_by_h1(text: str) -> list[str]:
    return [p.strip() for p in _H1_RE.split(text) if p.strip()]


def _split_by_paragraph(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]


def _split_fixed(text: str) -> list[str]:
    if len(text) <= CHUNK_SIZE:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + CHUNK_SIZE])
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


# ── Phase 1: RSS → markdown ────────────────────────────────────────────────────

async def _fetch_rss_items(rss_url: str) -> list[RssItem]:
    async with httpx.AsyncClient(
        timeout=15.0,
        verify=False,
        headers={"User-Agent": "Mozilla/5.0 (compatible; LH-RAG-Bot/1.0)"},
    ) as client:
        resp = await client.get(rss_url)
        resp.raise_for_status()
    return parse_rss_feed(resp.text)


async def crawl_to_markdown(
    rss_url: str,
    limit: int | None = None,
) -> CrawlResult:
    """RSS 피드에서 신규·변경 문서를 다운로드하여 markdown으로 저장합니다.

    중복 판단 기준은 data/markdown/ 디렉토리의 파일 날짜입니다.
    인덱스(BM25/Dense)는 건드리지 않습니다.
    """
    result = CrawlResult()
    md_dir = _get_markdown_dir()

    logger.info("RSS 수신 중: %s", rss_url)
    try:
        items = await _fetch_rss_items(rss_url)
    except Exception as e:
        logger.error("RSS 수신 실패: %s", e)
        return result

    if limit:
        items = items[:limit]

    # title_key별 최신 항목만 유지 (RSS에 동일 규정의 개정 이력이 모두 포함됨)
    # RSS는 최신순 정렬이므로 처음 나온 항목이 최신
    seen_keys: dict[str, RssItem] = {}
    for item in items:
        if _should_skip(item.title):
            result.skipped_filter += 1
            continue
        key = title_to_key(item.title)
        if key not in seen_keys:
            seen_keys[key] = item

    to_fetch: list[RssItem] = []
    for key, item in seen_keys.items():
        # markdown 파일 기준으로 최신 여부 판단
        existing = list(md_dir.glob(f"*_{key}.md"))
        if existing:
            date_str = existing[0].stem.split("_", 1)[0]
            try:
                stored_date = datetime.strptime(date_str, "%y%m%d")
                if stored_date >= item.pub_date:
                    result.skipped_fresh += 1
                    continue
            except ValueError:
                pass
        to_fetch.append(item)

    n_new = sum(1 for item in to_fetch if not list(md_dir.glob(f"*_{title_to_key(item.title)}.md")))
    n_upd = len(to_fetch) - n_new
    print(
        f"\n[크롤 계획]  전체 {len(items)}건"
        f"  →  신규 {n_new}건  업데이트 {n_upd}건  "
        f"스킵 {result.skipped_fresh}건  제외(예고/안내문) {result.skipped_filter}건\n"
    )

    if not to_fetch:
        return result

    from tqdm import tqdm

    fetcher = LHDocumentFetcher()
    try:
        pbar = tqdm(to_fetch, desc="크롤링", unit="건", dynamic_ncols=True)
        for item in pbar:
            pbar.set_postfix_str(item.title[:30])
            text = await fetcher.fetch_text(item.link)
            if not text:
                logger.warning("텍스트 추출 실패, 스킵: %s", item.title)
                result.failed += 1
                continue
            key = title_to_key(item.title)
            _save_markdown(key, item.pub_date, text)
            result.fetched += 1
            await asyncio.sleep(0.3)
    finally:
        await fetcher.aclose()

    logger.info(
        "크롤 완료 — 저장: %d, 스킵(최신): %d, 스킵(필터): %d, 실패: %d",
        result.fetched, result.skipped_fresh, result.skipped_filter, result.failed,
    )
    return result


# ── Phase 2: markdown → BM25 + Dense 정합성 검증 ──────────────────────────────

def sync_indexes_from_markdown(markdown_dir: Path | None = None) -> SyncResult:
    """markdown 디렉토리를 source of truth로 삼아 BM25/Dense 인덱스를 갱신합니다.

    - 추가: markdown에 있지만 인덱스에 없는 문서
    - 업데이트: markdown 날짜가 인덱스보다 최신인 문서
    - 삭제: 인덱스에 있지만 markdown 파일이 없는 문서
    """
    md_dir = markdown_dir or _get_markdown_dir()

    # markdown 상태: {title_key: (date, path)}
    md_state: dict[str, tuple[datetime, Path]] = {}
    for f in sorted(md_dir.glob("*.md")):
        stem = f.stem
        parts = stem.split("_", 1)
        if len(parts) != 2:
            logger.warning("파일명 형식 불일치, 스킵: %s", f.name)
            continue
        date_str, title_key = parts
        try:
            date = datetime.strptime(date_str, "%y%m%d")
        except ValueError:
            logger.warning("날짜 파싱 실패, 스킵: %s", f.name)
            continue
        md_state[title_key] = (date, f)

    # BM25 상태: {title_key: date}
    existing = load_bm25()
    bm25_state = get_all_title_keys(existing)

    # 차이 계산
    to_add = {k for k in md_state if k not in bm25_state}
    to_update = {
        k for k in md_state
        if k in bm25_state and md_state[k][0] > bm25_state[k]
    }
    to_remove = {k for k in bm25_state if k not in md_state}

    result = SyncResult(
        added=len(to_add),
        updated=len(to_update),
        removed=len(to_remove),
    )

    if not (to_add or to_update or to_remove):
        logger.info("인덱스 최신 상태 — 변경 없음 (%d문서)", len(md_state))
        return result

    print(
        f"\n[인덱스 정합성]  추가 {len(to_add)}건  "
        f"업데이트 {len(to_update)}건  삭제 {len(to_remove)}건\n"
    )

    # 현재 인덱스에서 시작
    all_ids: list[str] = list(existing.ids) if existing else []
    all_corpus: list[str] = list(existing.corpus) if existing else []
    all_metadatas: list[dict] = list(existing.metadatas) if existing else []

    # 삭제·업데이트 대상 청크 제거
    for key in to_remove | to_update:
        all_ids, all_corpus, all_metadatas = remove_doc_chunks(
            all_ids, all_corpus, all_metadatas, key
        )

    # 추가·업데이트 대상 청크 삽입 (Dense 증분 업데이트용으로 별도 추적)
    from tqdm import tqdm

    new_ids: list[str] = []
    new_corpus: list[str] = []

    for key in tqdm(to_add | to_update, desc="청킹", unit="건", dynamic_ncols=True):
        date, path = md_state[key]
        text = path.read_text(encoding="utf-8")
        chunks = chunk_text(text)
        pub_date_iso = date.isoformat()
        for i, chunk in enumerate(chunks):
            cid = chunk_id(key, i)
            all_ids.append(cid)
            all_corpus.append(chunk)
            all_metadatas.append({
                "title": key,
                "url": "",
                "pub_date": pub_date_iso,
                "chunk_index": i,
                "total_chunks": len(chunks),
            })
            new_ids.append(cid)
            new_corpus.append(chunk)

    logger.info("BM25 인덱스 재빌드 중... (%d청크)", len(all_ids))
    build_and_save(all_ids, all_corpus, all_metadatas)
    logger.info("BM25 인덱스 재빌드 완료")

    if settings.deepinfra_api_key:
        # 변경된 청크만 임베딩 (전체 재빌드 대신 증분 업데이트)
        update_dense_incremental(
            remove_keys=to_remove | to_update,
            add_ids=new_ids,
            add_corpus=new_corpus,
        )
    else:
        logger.info("DEEPINFRA_API_KEY 미설정 — Dense 인덱스 스킵")

    return result


# ── 편의 함수 (기존 API 유지) ─────────────────────────────────────────────────

async def sync_from_rss(
    rss_url: str,
    limit: int | None = None,
) -> SyncResult:
    """crawl_to_markdown + sync_indexes_from_markdown 를 순서대로 실행합니다."""
    await crawl_to_markdown(rss_url, limit=limit)
    return sync_indexes_from_markdown()


def build_from_markdown(markdown_dir: Path | None = None) -> SyncResult:
    """markdown 디렉토리에서 인덱스를 동기화합니다 (네트워크 없음).

    완전 재빌드가 필요하면 먼저 data/bm25/*.pkl 을 삭제하세요.
    """
    return sync_indexes_from_markdown(markdown_dir)
