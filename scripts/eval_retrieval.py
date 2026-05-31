"""검색 정확도 평가 — golden_set.jsonl 로 Recall@k / MRR / nDCG@k 계산.

각 질의를 해당 소스의 하이브리드(또는 bm25/dense) 검색에 태워, 결과의 문서키 순위가
정답 문서키(relevant)를 얼마나 잘 포함·상위 배치하는지 측정한다.

사용법:
    python scripts/eval_retrieval.py                 # hybrid, golden_set.jsonl
    python scripts/eval_retrieval.py --mode bm25     # BM25 단독
    python scripts/eval_retrieval.py --mode dense    # Dense 단독
    python scripts/eval_retrieval.py --golden eval/golden_set.jsonl --ks 5 10
"""

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from scripts.eval_common import SourceIndex

ROOT = Path(__file__).parent.parent
DEFAULT_GOLDEN = ROOT / "eval" / "golden_set.jsonl"
RESULTS_DIR = ROOT / "eval" / "results"

# source 키 정규화 (golden 에 source_id 가 와도 받아줌)
_SRC_ALIAS = {
    "lh_vector_db": "lh",
    "kcsc_vector_db": "kcsc",
    "pps_vector_db": "pps",
}


def _norm_source(s: str) -> str:
    return _SRC_ALIAS.get(s, s)


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    hit = sum(1 for key in ranked[:k] if key in relevant)
    return hit / len(relevant)


def mrr(ranked: list[str], relevant: set[str]) -> float:
    for i, key in enumerate(ranked, 1):
        if key in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(
        1.0 / math.log2(i + 1)
        for i, key in enumerate(ranked[:k], 1)
        if key in relevant
    )
    ideal_hits = min(len(relevant), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    return dcg / idcg if idcg else 0.0


def evaluate(
    golden_path: Path,
    mode: str,
    ks: list[int],
    indexes: dict[str, "SourceIndex"] | None = None,
) -> dict:
    rows = [
        json.loads(line)
        for line in golden_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        raise SystemExit(f"골든셋이 비어 있습니다: {golden_path}")

    max_k = max(ks)
    _indexes: dict[str, SourceIndex] = indexes if indexes is not None else {}
    per_query: list[dict] = []
    by_source: dict[str, list[dict]] = defaultdict(list)

    for row in rows:
        src = _norm_source(row["source"])
        relevant = set(row.get("relevant", []))
        if not relevant:
            print(f"[건너뜀] 정답 없음: {row.get('id')}", file=sys.stderr)
            continue
        idx = _indexes.get(src) or _indexes.setdefault(src, SourceIndex(src))
        ranked = idx.retrieve_doc_keys(
            row.get("query", ""), row.get("keywords", ""), top_k=max_k, mode=mode
        )
        metrics = {f"recall@{k}": recall_at_k(ranked, relevant, k) for k in ks}
        metrics["mrr"] = mrr(ranked, relevant)
        metrics[f"ndcg@{max_k}"] = ndcg_at_k(ranked, relevant, max_k)
        rec = {
            "id": row.get("id"),
            "source": src,
            "found": [k for k in ranked[:max_k] if k in relevant],
            "metrics": metrics,
        }
        per_query.append(rec)
        by_source[src].append(metrics)

    def _avg(dicts: list[dict]) -> dict:
        if not dicts:
            return {}
        keys = dicts[0].keys()
        return {k: round(sum(d[k] for d in dicts) / len(dicts), 4) for k in keys}

    summary = {src: _avg(ms) for src, ms in by_source.items()}
    summary["__overall__"] = _avg([m for ms in by_source.values() for m in ms])
    return {
        "mode": mode,
        "ks": ks,
        "n_queries": len(per_query),
        "summary": summary,
        "per_query": per_query,
    }


def _print_table(summary: dict, ks: list[int], max_k: int) -> None:
    cols = [f"recall@{k}" for k in ks] + ["mrr", f"ndcg@{max_k}"]
    header = f"{'source':<14}" + "".join(f"{c:>12}" for c in cols)
    print(header)
    print("-" * len(header))
    for src in sorted(summary, key=lambda s: (s == "__overall__", s)):
        m = summary[src]
        if not m:
            continue
        name = "전체" if src == "__overall__" else src
        print(f"{name:<14}" + "".join(f"{m.get(c, 0):>12.4f}" for c in cols))


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description="검색 정확도 평가")
    ap.add_argument("--golden", default=str(DEFAULT_GOLDEN))
    ap.add_argument("--mode", choices=["hybrid", "bm25", "dense"], default="hybrid")
    ap.add_argument("--ks", type=int, nargs="+", default=[5, 10])
    ap.add_argument("--no-save", action="store_true", help="결과 JSON 저장 안 함")
    args = ap.parse_args()

    result = evaluate(Path(args.golden), args.mode, sorted(args.ks))
    print(f"\n=== 검색 평가 (mode={args.mode}, n={result['n_queries']}) ===")
    _print_table(result["summary"], sorted(args.ks), max(args.ks))

    if not args.no_save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = RESULTS_DIR / f"{ts}_{args.mode}.json"
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n결과 저장: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
