"""
KCSC 건설기준 인덱스 구축.

흐름 (LH의 indexer.py 패턴 차용, RSS/PDF 대신 Open API 사용):
  1. crawl_to_cache()  — CodeList → 코드별 CodeViewer 조회 → JSON 캐시 저장
                         (updateDate 비교로 변경분만 API 호출)
  2. build_from_cache() — JSON 캐시 → BM25 + Dense + 인용 그래프 빌드

조문(Section) 단위로 청킹하며, 청크 ID는 '{codeType}{code}__c{idx:04d}'로 문서 단위 키를
유지해 기존 증분 유틸(get_all_title_keys, update_dense_incremental)을 그대로 재사용한다.
"""

import asyncio
import json
import logging
import pickle
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from crawler.indexer import CHUNK_SIZE, chunk_id, _split_fixed
from crawler.bm25_index import build_and_save, get_all_title_keys, load_bm25
from crawler.dense_index import load_dense, update_dense_incremental
from crawler.kcsc_api import (
    CodeMeta, KcscApiClient, Section,
    extract_citations, sections_to_markdown,
)

logger = logging.getLogger(__name__)

_CONCURRENCY = 5


def _cache_dir() -> Path:
    p = Path("./data/kcsc")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _md_dir() -> Path:
    p = Path(settings.markdown_path) / "kcsc"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _graph_path() -> Path:
    p = Path(settings.bm25_path)
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{settings.kcsc_bm25_collection}_graph.pkl"


class KcscGraph(NamedTuple):
    edges: dict[str, list[str]]            # 출발 노드 → 인용 노드 목록
    node_to_chunks: dict[str, list[str]]   # 노드 id → chunk_id 목록
    node_names: dict[str, str]             # 노드 id → 표시 라벨


@dataclass
class CrawlResult:
    fetched: int = 0
    skipped_fresh: int = 0
    failed: int = 0


@dataclass
class BuildResult:
    docs: int = 0
    chunks: int = 0
    edges: int = 0


# ── Phase 1: API → JSON 캐시 ───────────────────────────────────────────────────

def _cache_file(meta: CodeMeta) -> Path:
    return _cache_dir() / f"{meta.code_type}_{meta.code}.json"


def _load_cached_date(meta: CodeMeta) -> str | None:
    f = _cache_file(meta)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8")).get("update_date")
    except Exception:
        return None


def _save_cache(meta: CodeMeta, sections: list[Section], known_codes: set[str]) -> None:
    # 조문별 인용 추출 (그래프 출발 노드를 조문 단위로 두기 위함)
    payload = {
        "code_type": meta.code_type,
        "code": meta.code,
        "full_code": meta.full_code,
        "name": meta.name,
        "version": meta.version,
        "update_date": meta.update_date,
        "viewer_url": meta.viewer_url,
        "sections": [
            {
                "label": s.label,
                "level": s.level,
                "title": s.title,
                "text": s.text,
                "citations": sorted(
                    extract_citations(f"{s.title}\n{s.text}", meta, known_codes)
                ),
            }
            for s in sections
        ],
    }
    _cache_file(meta).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    # 디버그/감사용 마크다운
    md = sections_to_markdown(meta, sections)
    (_md_dir() / f"{meta.code_type}_{meta.code}.md").write_text(md, encoding="utf-8")


async def crawl_to_cache(
    code_type: str | None = None,
    limit: int | None = None,
) -> CrawlResult:
    """CodeList → 변경된 코드의 CodeViewer를 조회해 JSON 캐시로 저장합니다."""
    result = CrawlResult()
    client = KcscApiClient()
    try:
        codes = await client.fetch_code_list()
        if code_type:
            codes = [c for c in codes if c.code_type == code_type.upper()]
        known_codes = {c.doc_key for c in codes}
        if limit:
            codes = codes[:limit]

        to_fetch = [c for c in codes if _load_cached_date(c) != c.update_date]
        result.skipped_fresh = len(codes) - len(to_fetch)
        print(
            f"\n[크롤 계획]  대상 {len(codes)}건  →  "
            f"갱신 {len(to_fetch)}건  스킵(최신) {result.skipped_fresh}건\n"
        )
        if not to_fetch:
            return result

        from tqdm import tqdm

        sem = asyncio.Semaphore(_CONCURRENCY)
        pbar = tqdm(total=len(to_fetch), desc="CodeViewer", unit="건", dynamic_ncols=True)

        async def _work(meta: CodeMeta):
            async with sem:
                try:
                    sections = await client.fetch_sections(meta)
                    if not sections:
                        logger.warning("본문 없음, 스킵: %s %s", meta.code_type, meta.code)
                        result.failed += 1
                    else:
                        _save_cache(meta, sections, known_codes)
                        result.fetched += 1
                except Exception as e:
                    logger.warning("조회 실패 %s %s: %s", meta.code_type, meta.code, e)
                    result.failed += 1
                finally:
                    pbar.update(1)

        await asyncio.gather(*(_work(c) for c in to_fetch))
        pbar.close()
    finally:
        await client.aclose()

    logger.info(
        "크롤 완료 — 저장 %d, 스킵 %d, 실패 %d",
        result.fetched, result.skipped_fresh, result.failed,
    )
    return result


# ── Phase 2: JSON 캐시 → BM25 + Dense + 그래프 ─────────────────────────────────

def _iter_caches(code_type: str | None = None):
    for f in sorted(_cache_dir().glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("캐시 파싱 실패, 스킵: %s", f.name)
            continue
        if code_type and data.get("code_type") != code_type.upper():
            continue
        yield data


def build_from_cache(code_type: str | None = None) -> BuildResult:
    """JSON 캐시에서 BM25/Dense/그래프 인덱스를 빌드합니다."""
    col = settings.kcsc_bm25_collection

    prev_bm25 = load_bm25(col)
    prev_dates = get_all_title_keys(prev_bm25)  # {doc_key: datetime}

    all_ids: list[str] = []
    all_corpus: list[str] = []
    all_metadatas: list[dict] = []
    new_dates: dict[str, datetime] = {}

    edges: dict[str, list[str]] = {}
    node_to_chunks: dict[str, list[str]] = {}
    node_names: dict[str, str] = {}

    def _add_node_chunk(node: str, cid: str, name: str):
        node_to_chunks.setdefault(node, []).append(cid)
        node_names.setdefault(node, name)

    docs = 0
    for data in _iter_caches(code_type):
        ctype = data["code_type"]
        code = data["code"]
        name = data.get("name", "")
        doc_key = f"{ctype}{code}"
        doc_node = f"{ctype}:{code}"
        update_date = data.get("update_date", "") or ""
        viewer_url = data.get("viewer_url", "")

        sections = data.get("sections", [])
        # 조문 → 청크 (과대 섹션만 분할)
        idx = 0
        for s in sections:
            label = s.get("label", "")
            title = s.get("title", "")
            text = s.get("text", "")
            body = f"{title}\n{text}".strip() if title else text
            if not body:
                continue
            pieces = [body] if len(body) <= CHUNK_SIZE else _split_fixed(body)
            section_node = f"{ctype}:{code}:{label}" if label else doc_node
            for piece in pieces:
                cid = chunk_id(doc_key, idx)
                all_ids.append(cid)
                all_corpus.append(piece)
                all_metadatas.append({
                    "title": name,
                    "url": viewer_url,
                    "pub_date": update_date,   # 증분 유틸 호환 (updateDate)
                    "code_type": ctype,
                    "code": code,
                    "full_code": data.get("full_code", ""),
                    "version": data.get("version", ""),
                    "section_label": label,
                    "section_title": title,
                    "level": s.get("level", 0),
                    "chunk_index": idx,
                    "node_id": section_node,
                })
                disp = f"{ctype} {code} {name}" + (f" §{label}" if label else "")
                _add_node_chunk(section_node, cid, disp)
                if section_node != doc_node:
                    _add_node_chunk(doc_node, cid, f"{ctype} {code} {name}")
                idx += 1

            # 조문 단위 인용 엣지: 이 조문 노드 → 인용 대상 노드들
            cites = s.get("citations", [])
            if cites:
                bucket = edges.setdefault(section_node, [])
                for tgt in cites:
                    if tgt not in bucket:
                        bucket.append(tgt)

        if update_date:
            try:
                new_dates[doc_key] = datetime.fromisoformat(update_date)
            except ValueError:
                pass
        docs += 1

    if not all_ids:
        logger.warning("빌드할 캐시가 없습니다. 먼저 crawl_to_cache를 실행하세요.")
        return BuildResult()

    logger.info("BM25 빌드: %d문서 %d청크", docs, len(all_ids))
    build_and_save(all_ids, all_corpus, all_metadatas, collection=col)

    # 그래프 저장
    graph = KcscGraph(edges=edges, node_to_chunks=node_to_chunks, node_names=node_names)
    with _graph_path().open("wb") as f:
        pickle.dump(graph, f)
    n_edges = sum(len(v) for v in edges.values())
    logger.info("인용 그래프 저장: 노드 %d, 엣지 %d → %s",
                len(node_to_chunks), n_edges, _graph_path().name)

    # Dense 증분 갱신
    if settings.deepinfra_api_key:
        prev_dense = load_dense(col)
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
            update_dense_incremental(remove_keys, add_ids, add_corpus, collection=col)
        else:
            logger.info("Dense 변경 없음")
    else:
        logger.info("DEEPINFRA_API_KEY 미설정 — Dense 인덱스 스킵")

    return BuildResult(docs=docs, chunks=len(all_ids), edges=n_edges)


def load_graph() -> KcscGraph | None:
    path = _graph_path()
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            graph = pickle.load(f)
        if not isinstance(graph, KcscGraph):
            logger.warning("그래프 형식 불일치 — 재빌드가 필요합니다.")
            return None
        return graph
    except Exception as e:
        logger.warning("그래프 로드 실패 (%s)", e)
        return None


# ── 편의 함수 ──────────────────────────────────────────────────────────────────

async def sync(code_type: str | None = None, limit: int | None = None) -> BuildResult:
    """crawl_to_cache + build_from_cache 순차 실행."""
    await crawl_to_cache(code_type=code_type, limit=limit)
    return build_from_cache(code_type=code_type)
