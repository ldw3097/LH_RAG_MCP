"""
조달청 해석사례 인덱스 구축.

흐름 (KCSC의 kcsc_indexer.py 패턴 축약 — 인용 그래프 없음):
  1. crawl_to_cache()  — lawSearch.do 목록 → 건별 lawService.do 본문 조회 → JSON 캐시 저장
                         (데이터기준일시 비교로 변경분만 API 호출)
  2. build_from_cache() — JSON 캐시 → BM25 + Dense 빌드

한 해석사례 = 안건명 + 질의요지 + 회답 + 이유 + 관련법령을 합친 본문 1개. CHUNK_SIZE 초과 시
_split_fixed로 분할한다. 청크 ID는 '{법령해석일련번호}__c{idx:04d}'로 문서 단위 키를
유지해 기존 증분 유틸(get_all_title_keys, update_dense_incremental)을 재사용한다.
"""

import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from crawler.indexer import chunk_id, CHUNK_OVERLAP
from src.sources.pps_vector import PPS_CHUNK_SIZE
from crawler.bm25_index import build_and_save, get_all_title_keys, load_bm25
from crawler.dense_index import load_dense, update_dense_incremental
from crawler.pps_api import PpsApiClient, PpsCase, to_iso

logger = logging.getLogger(__name__)


def _split_pps(text: str) -> list[str]:
    """PPS_CHUNK_SIZE 기준으로 본문을 분할합니다.

    _split_fixed(CHUNK_SIZE=800)를 직접 호출하면 안 된다 — 그쪽은 LH/KCSC용 800자 기준이며
    PPS는 1건=1청크 원칙(PPS_CHUNK_SIZE=3000)을 유지해야 한다.
    """
    if len(text) <= PPS_CHUNK_SIZE:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start: start + PPS_CHUNK_SIZE])
        start += PPS_CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def _cache_dir() -> Path:
    p = Path(settings.pps_data_path) / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class CrawlResult:
    fetched: int = 0
    skipped_fresh: int = 0
    failed: int = 0


@dataclass
class BuildResult:
    docs: int = 0
    chunks: int = 0


# ── Phase 1: API → JSON 캐시 ───────────────────────────────────────────────────

def _date_prefix(date_str: str) -> str:
    """'2025.12.04' → '20251204'. 파싱 실패 시 '00000000'."""
    iso = to_iso(date_str)
    return iso.replace("-", "") if iso else "00000000"


def _find_cache_file(sid: str) -> Path | None:
    matches = list(_cache_dir().glob(f"*_{sid}.json"))
    return matches[0] if matches else None


def _cached_date(sid: str) -> str | None:
    f = _find_cache_file(sid)
    return f.stem.split("_", 1)[0] if f else None


def _save_cache(case: PpsCase) -> None:
    # 구버전 파일 삭제 (날짜 변경 시 파일명이 달라지므로)
    for old in _cache_dir().glob(f"*_{case.id}.json"):
        old.unlink()
    payload = {
        "id": case.id,
        "title": case.title,
        "reply_date": case.reply_date,
        "base_date": case.base_date,
        "question": case.question,
        "answer": case.answer,
        "reason": case.reason,
        "related_law": case.related_law,
        "org": case.org,
        "url": case.url,
    }
    path = _cache_dir() / f"{_date_prefix(case.base_date or case.reply_date)}_{case.id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


async def crawl_to_cache(limit: int | None = None) -> CrawlResult:
    """목록 → 변경된 건의 본문을 조회해 JSON 캐시로 저장합니다."""
    result = CrawlResult()
    client = PpsApiClient()
    try:
        if not settings.law_oc_default:
            logger.error("LAW_OC_DEFAULT 미설정 — ppsCgmExpc 호출에 OC 키가 필요합니다.")
            return result

        metas = await client.fetch_id_list(limit=limit)

        # 데이터기준일시 비교로 변경분만 선별
        to_fetch = [
            m for m in metas
            if _cached_date(m["id"]) != _date_prefix(m.get("base_date") or m.get("reply_date"))
        ]
        result.skipped_fresh = len(metas) - len(to_fetch)
        print(
            f"\n[크롤 계획]  대상 {len(metas)}건  →  "
            f"갱신 {len(to_fetch)}건  스킵(최신) {result.skipped_fresh}건\n"
        )
        if not to_fetch:
            return result

        from tqdm import tqdm

        cases = await client.fetch_details(to_fetch)
        for case in tqdm(cases, desc="본문 저장", unit="건", dynamic_ncols=True):
            _save_cache(case)
            result.fetched += 1
        result.failed = len(to_fetch) - len(cases)
    finally:
        await client.aclose()

    logger.info(
        "크롤 완료 — 저장 %d, 스킵 %d, 실패 %d",
        result.fetched, result.skipped_fresh, result.failed,
    )
    return result


# ── Phase 2: JSON 캐시 → BM25 + Dense ──────────────────────────────────────────

def _iter_caches():
    for f in sorted(_cache_dir().glob("*.json")):
        try:
            yield json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("캐시 파싱 실패, 스킵: %s", f.name)


def _case_body(data: dict) -> str:
    parts = [f"[안건명] {data.get('title','')}"]
    for label, key in (("질의요지", "question"), ("회답", "answer"),
                       ("이유", "reason"), ("관련법령", "related_law")):
        v = (data.get(key) or "").strip()
        if v:
            parts.append(f"[{label}]\n{v}")
    return "\n\n".join(parts)


def build_from_cache() -> BuildResult:
    """JSON 캐시 전체에서 BM25/Dense 인덱스를 빌드합니다."""
    col = settings.pps_bm25_collection
    bp = settings.pps_data_path

    prev_bm25 = load_bm25(col, base_path=bp)
    prev_dates = get_all_title_keys(prev_bm25)  # {id: datetime}

    all_ids: list[str] = []
    all_corpus: list[str] = []
    all_metadatas: list[dict] = []
    new_dates: dict[str, datetime] = {}

    docs = 0
    for data in _iter_caches():
        sid = str(data.get("id", "")).strip()
        if not sid:
            continue
        body = _case_body(data).strip()
        if not body:
            continue

        pub_iso = to_iso(data.get("base_date") or data.get("reply_date") or "")
        pieces = _split_pps(body)
        for idx, piece in enumerate(pieces):
            all_ids.append(chunk_id(sid, idx))
            all_corpus.append(piece)
            all_metadatas.append({
                "title": data.get("title", ""),
                "url": data.get("url", ""),
                "pub_date": pub_iso,
                "id": sid,
                "reply_date": data.get("reply_date", ""),
                "org": data.get("org", ""),
                "chunk_index": idx,
            })

        if pub_iso:
            try:
                new_dates[sid] = datetime.fromisoformat(pub_iso)
            except ValueError:
                pass
        docs += 1

    if not all_ids:
        logger.warning("빌드할 캐시가 없습니다. 먼저 crawl_to_cache를 실행하세요.")
        return BuildResult()

    logger.info("BM25 빌드: %d문서 %d청크", docs, len(all_ids))
    build_and_save(all_ids, all_corpus, all_metadatas, collection=col, base_path=bp)

    # Dense 증분 갱신
    if settings.deepinfra_api_key:
        prev_dense = load_dense(col, base_path=bp)
        dense_keys = (
            {cid.rsplit("__c", 1)[0] for cid in prev_dense.ids} if prev_dense else set()
        )
        changed = {k for k in new_dates if prev_dates.get(k) != new_dates[k]}
        missing = {k for k in new_dates if k not in dense_keys}
        to_embed = changed | missing
        removed = set(prev_dates) - set(new_dates)
        remove_keys = changed | removed

        add_ids: list[str] = []
        add_corpus: list[str] = []
        for cid, doc in zip(all_ids, all_corpus):
            if cid.rsplit("__c", 1)[0] in to_embed:
                add_ids.append(cid)
                add_corpus.append(doc)

        if add_ids or remove_keys:
            logger.info("Dense 증분: 임베딩 %d청크, 제거 키 %d", len(add_ids), len(remove_keys))
            update_dense_incremental(remove_keys, add_ids, add_corpus, collection=col, base_path=bp)
        else:
            logger.info("Dense 변경 없음")
    else:
        logger.info("DEEPINFRA_API_KEY 미설정 — Dense 인덱스 스킵")

    return BuildResult(docs=docs, chunks=len(all_ids))


async def sync(limit: int | None = None) -> BuildResult:
    """crawl_to_cache + build_from_cache 순차 실행."""
    await crawl_to_cache(limit=limit)
    return build_from_cache()
