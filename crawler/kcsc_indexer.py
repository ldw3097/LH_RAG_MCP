"""
KCSC 건설기준 인덱스 구축.

흐름 (LH의 indexer.py 패턴 차용, RSS/PDF 대신 Open API 사용):
  1. crawl_to_cache()  — CodeList → 코드별 CodeViewer 조회 → JSON 캐시 저장
                         (updateDate 비교로 변경분만 API 호출)
  2. build_from_cache() — JSON 캐시 → BM25 + Dense + 인용 그래프 빌드

청킹은 lv1/lv2 헤더를 경계로 하위 섹션(lv3/lv4)을 한 덩어리로 합친다. KCSC API가
조문을 level 1~4의 평면 배열로 주는데, lv3/lv4를 낱개로 청킹하면 문맥 없는 미니청크가
되기 때문이다. 너무 짧은 그룹은 다음 그룹에 병합하고, CHUNK_SIZE 초과 시 분할한다.

청크 ID는 '{codeType}{code}__c{idx:04d}'로 문서 단위 키를 유지해 기존 증분 유틸
(get_all_title_keys, update_dense_incremental)을 그대로 재사용한다. 인용 그래프 노드는
청크보다 세밀한 조문 단위(lv3/lv4 label)로 두되, node_to_chunks가 부모 lv2 청크를
가리키도록 매핑해 1-hop 확장이 올바른 청크를 찾게 한다.
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
    extract_citations,
)

logger = logging.getLogger(__name__)

_CONCURRENCY = 5


def _cache_dir() -> Path:
    p = Path(settings.kcsc_data_path) / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _graph_path() -> Path:
    p = Path(settings.kcsc_data_path)
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

def _date_prefix(update_date: str) -> str:
    """ISO datetime 문자열 → YYYYMMDD 문자열. 예: '2025-12-29T14:48:25' → '20251229'."""
    return update_date[:10].replace("-", "") if update_date else ""


def _find_cache_file(meta: CodeMeta) -> Path | None:
    """기존 캐시 파일 위치: data/kcsc/{YYYYMMDD}_{doc_key}.json"""
    matches = list(_cache_dir().glob(f"*_{meta.doc_key}.json"))
    return matches[0] if matches else None


def _new_cache_path(meta: CodeMeta) -> Path:
    """새 캐시 파일 경로: data/kcsc/{YYYYMMDD}_{doc_key}.json"""
    return _cache_dir() / f"{_date_prefix(meta.update_date)}_{meta.doc_key}.json"


def _load_cached_date(meta: CodeMeta) -> str | None:
    """캐시 파일명에서 날짜(YYYYMMDD)를 읽습니다. 파일이 없으면 None."""
    f = _find_cache_file(meta)
    if f is None:
        return None
    return f.stem.split("_", 1)[0]  # "20251229_KDS111005" → "20251229"


def _save_cache(meta: CodeMeta, sections: list[Section], known_codes: set[str]) -> None:
    # 구버전 파일 삭제 (날짜 변경 시 파일명이 달라지므로)
    for old in _cache_dir().glob(f"*_{meta.doc_key}.json"):
        old.unlink()

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
    _new_cache_path(meta).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )


async def crawl_to_cache(
    code_type: str | None = None,
    limit: int | None = None,
) -> CrawlResult:
    """CodeList → 변경된 코드의 CodeViewer를 조회해 JSON 캐시로 저장합니다."""
    result = CrawlResult()
    client = KcscApiClient()
    try:
        all_codes = await client.fetch_code_list()
        known_codes = {c.doc_key for c in all_codes}   # 항상 전체 기준

        codes = all_codes
        if code_type:
            codes = [c for c in codes if c.code_type == code_type.upper()]
        if limit:
            codes = codes[:limit]

        to_fetch = [c for c in codes if _load_cached_date(c) != _date_prefix(c.update_date)]
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

def _iter_caches():
    for f in sorted(_cache_dir().glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("캐시 파싱 실패, 스킵: %s", f.name)
            continue
        yield data


def build_from_cache() -> BuildResult:
    """JSON 캐시 전체에서 BM25/Dense/그래프 인덱스를 빌드합니다.

    BM25는 캐시 전체를 매번 통째로 재빌드하므로 부분(타입별) 빌드를 지원하지 않는다.
    (특정 타입만 넘기면 다른 타입이 인덱스에서 사라진다.)
    """
    col = settings.kcsc_bm25_collection

    prev_bm25 = load_bm25(col, base_path=settings.kcsc_data_path)
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
    for data in _iter_caches():
        ctype = data["code_type"]
        code = data["code"]
        name = data.get("name", "")
        doc_key = f"{ctype}{code}"
        doc_node = f"{ctype}:{code}"
        update_date = data.get("update_date", "") or ""
        viewer_url = data.get("viewer_url", "")

        sections = data.get("sections", [])
        # lv2 경계로 하위 섹션을 집계해 청크 생성.
        # lv3/lv4 낱개 섹션(5~200자)을 그대로 청킹하면 문맥 없는 미니청크가 되므로
        # lv1/lv2 헤더를 만날 때마다 새 그룹을 열고 하위 텍스트를 모두 합친다.
        groups: list[tuple[dict | None, list[dict]]] = []
        cur_header: dict | None = None
        cur_children: list[dict] = []
        for s in sections:
            if s.get("level", 0) <= 2:
                if cur_header is not None or cur_children:
                    groups.append((cur_header, cur_children))
                cur_header = s
                cur_children = []
            else:
                cur_children.append(s)
        if cur_header is not None or cur_children:
            groups.append((cur_header, cur_children))

        # 너무 짧은 그룹은 다음 그룹의 앞에 병합한다.
        # (lv1 헤더만 있는 그룹, "기호의 정의\n내용 없음" 등)
        _MIN_CHUNK = 60
        merged: list[tuple[dict | None, list[dict]]] = []
        pending_prefix: list[str] = []   # 앞 그룹에서 넘어온 짧은 텍스트 조각
        for hdr, children in groups:
            parts: list[str] = []
            if hdr:
                h = f"{hdr.get('title','')}\n{hdr.get('text','')}".strip()
                if h:
                    parts.append(h)
            for c in children:
                t = c.get("text", "").strip()
                if t:
                    parts.append(t)
            body = "\n".join(parts).strip()
            if len(body) < _MIN_CHUNK:
                # 짧으면 다음 그룹 앞에 붙일 prefix로 쌓아둔다
                pending_prefix.append(body)
            else:
                if pending_prefix:
                    # 쌓아둔 짧은 텍스트를 이 그룹의 첫 자식으로 주입
                    prefix_sec = {"label": "", "level": 9,
                                  "title": "", "text": "\n".join(pending_prefix)}
                    merged.append((hdr, [prefix_sec] + children))
                    pending_prefix = []
                else:
                    merged.append((hdr, children))
        # 마지막까지 남은 prefix는 마지막 그룹에 붙이거나 독립 그룹으로
        if pending_prefix:
            if merged:
                last_hdr, last_children = merged[-1]
                suffix_sec = {"label": "", "level": 9,
                              "title": "", "text": "\n".join(pending_prefix)}
                merged[-1] = (last_hdr, last_children + [suffix_sec])
            else:
                merged.append((None, [{"label": "", "level": 9, "title": "",
                                       "text": "\n".join(pending_prefix)}]))

        idx = 0
        for (hdr, children) in merged:
            # ── 집계 텍스트 구성 ──────────────────────────────────────
            parts: list[str] = []
            hdr_label = hdr.get("label", "") if hdr else ""
            hdr_title = hdr.get("title", "") if hdr else ""
            hdr_node = f"{ctype}:{code}:{hdr_label}" if hdr_label else doc_node

            if hdr:
                header_body = f"{hdr_title}\n{hdr.get('text','') }".strip()
                if header_body:
                    parts.append(header_body)

            child_nodes: list[tuple[str, list[str]]] = []  # (node_id, citations)
            for c in children:
                t = c.get("text", "").strip()
                if t:
                    parts.append(t)
                cl = c.get("label", "")
                cn = f"{ctype}:{code}:{cl}" if cl else hdr_node
                child_nodes.append((cn, c.get("citations", [])))

            body = "\n".join(parts).strip()
            if not body:
                continue

            # ── 청크 저장 ─────────────────────────────────────────────
            pieces = [body] if len(body) <= CHUNK_SIZE else _split_fixed(body)
            for piece in pieces:
                cid = chunk_id(doc_key, idx)
                all_ids.append(cid)
                all_corpus.append(piece)
                all_metadatas.append({
                    "title": name,
                    "url": viewer_url,
                    "pub_date": update_date,
                    "code_type": ctype,
                    "code": code,
                    "full_code": data.get("full_code", ""),
                    "version": data.get("version", ""),
                    "section_label": hdr_label,
                    "section_title": hdr_title,
                    "level": hdr.get("level", 0) if hdr else 0,
                    "chunk_index": idx,
                    "node_id": hdr_node,
                })
                disp = f"{ctype} {code} {name}" + (f" §{hdr_label}" if hdr_label else "")
                _add_node_chunk(hdr_node, cid, disp)
                # 하위 노드도 이 청크를 가리키도록 등록
                for cn, _ in child_nodes:
                    if cn != hdr_node:
                        _add_node_chunk(cn, cid, disp)
                _add_node_chunk(doc_node, cid, f"{ctype} {code} {name}")
                idx += 1

            # ── 인용 엣지: hdr_node에 헤더+자식 인용을 모두 집계 ──────
            # hdr_node(lv2) 단위로 인용을 집계하면, 검색 시 BM25 메타의
            # node_id(hdr_node)만으로 해당 청크 전체의 인용을 조회할 수 있다.
            chunk_cites: list[str] = list(hdr.get("citations", []) if hdr else [])
            for _, cites in child_nodes:
                chunk_cites.extend(cites)
            for cite in chunk_cites:
                edges.setdefault(hdr_node, [])
                if cite not in edges[hdr_node]:
                    edges[hdr_node].append(cite)

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
    build_and_save(all_ids, all_corpus, all_metadatas, collection=col,
                   base_path=settings.kcsc_data_path)

    # 그래프 저장
    graph = KcscGraph(edges=edges, node_to_chunks=node_to_chunks, node_names=node_names)
    with _graph_path().open("wb") as f:
        pickle.dump(graph, f)
    n_edges = sum(len(v) for v in edges.values())
    logger.info("인용 그래프 저장: 노드 %d, 엣지 %d → %s",
                len(node_to_chunks), n_edges, _graph_path().name)

    # Dense 증분 갱신
    if settings.deepinfra_api_key:
        prev_dense = load_dense(col, base_path=settings.kcsc_data_path)
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
            update_dense_incremental(remove_keys, add_ids, add_corpus, collection=col,
                                     base_path=settings.kcsc_data_path)
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
    """crawl_to_cache(부분 크롤 가능) + build_from_cache(항상 전체 빌드) 순차 실행."""
    await crawl_to_cache(code_type=code_type, limit=limit)
    return build_from_cache()
