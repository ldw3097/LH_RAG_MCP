"""골든셋 라벨링 보조 — 한 질의를 넓게 검색해 후보를 사람이 검토하기 좋게 덤프한다.

운영 검색(5~7건)보다 훨씬 많은 후보(기본 40건)를 문서키와 함께 출력한다.
이 출력을 보고 해당 질의에 '반드시 검색돼야 하는 문서키'를 골라 eval/golden_set.jsonl 에
기록한다.

사용법:
    python scripts/eval_collect.py --source lh \
        --query "출장 여비는 어떻게 산정하나" --keywords "출장 여비 산정" --top 40

    # 여러 질의를 한 번에: --batch eval/queries.jsonl
    #   (각 줄: {"source": "...", "query": "...", "keywords": "..."})
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from scripts.eval_common import SourceIndex


def _preview(text: str, n: int = 180) -> str:
    t = " ".join(text.split())
    return t[:n] + ("…" if len(t) > n else "")


def collect_one(idx: SourceIndex, query: str, keywords: str, top: int, mode: str) -> None:
    print("=" * 100)
    print(f"[source={idx.source}] query={query!r} | keywords={keywords!r} | mode={mode}")
    print("-" * 100)
    cands = idx.retrieve(query, keywords, top_k=top, mode=mode)
    if not cands:
        print("(후보 없음)")
        return

    primary = [c for c in cands if not c.metadata.get("via")]
    citation = [c for c in cands if c.metadata.get("via") == "citation"]

    print(f"▶ 1차 결과 ({len(primary)}건)")
    for rank, c in enumerate(primary, 1):
        print(f"[{rank:>2}] 문서키: {c.doc_key}")
        print(f"     제목 : {c.title}")
        print(f"     본문 : {_preview(c.text)}")

    if citation:
        print(f"\n▶ 인용 확장 결과 ({len(citation)}건)")
        for rank, c in enumerate(citation, 1):
            print(f"[인용{rank:>2}] 문서키: {c.doc_key}")
            print(f"        제목 : {c.title}")
            print(f"        본문 : {_preview(c.text)}")

    # 라벨 붙이기 쉽도록 문서키 후보 목록을 끝에 한 줄로
    uniq_primary: list[str] = []
    uniq_citation: list[str] = []
    seen: set[str] = set()
    for c in primary:
        if c.doc_key not in seen:
            seen.add(c.doc_key)
            uniq_primary.append(c.doc_key)
    for c in citation:
        if c.doc_key not in seen:
            seen.add(c.doc_key)
            uniq_citation.append(c.doc_key)

    print("-" * 100)
    print("1차 문서키:")
    print(json.dumps(uniq_primary, ensure_ascii=False))
    if uniq_citation:
        print("인용 문서키:")
        print(json.dumps(uniq_citation, ensure_ascii=False))


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description="골든셋 라벨링용 후보 덤프")
    ap.add_argument("--source", choices=["lh", "kcsc", "pps"])
    ap.add_argument("--query", default="")
    ap.add_argument("--keywords", default="")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--mode", choices=["hybrid", "bm25", "dense"], default="hybrid")
    ap.add_argument("--batch", help="질의 묶음 JSONL (source/query/keywords)")
    args = ap.parse_args()

    if args.batch:
        rows = [
            json.loads(line)
            for line in Path(args.batch).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        cache: dict[str, SourceIndex] = {}
        for row in rows:
            src = row["source"]
            idx = cache.get(src) or cache.setdefault(src, SourceIndex(src))
            collect_one(
                idx, row.get("query", ""), row.get("keywords", ""), args.top, args.mode
            )
        return

    if not args.source:
        ap.error("--source 또는 --batch 중 하나는 필요합니다.")
    idx = SourceIndex(args.source)
    collect_one(idx, args.query, args.keywords, args.top, args.mode)


if __name__ == "__main__":
    main()
