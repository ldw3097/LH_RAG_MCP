"""파라미터 그리드 서치 — TOP_K_FINAL / TOP_K_CANDIDATES / RRF_K.

사용법:
    python scripts/tune_params.py                        # hybrid, nDCG@10 최적화
    python scripts/tune_params.py --metric recall@5      # 다른 지표로 최적화
    python scripts/tune_params.py --mode bm25            # BM25 단독 튜닝
"""

import argparse
import sys
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

import src.sources.lh_vector as lh_mod
import src.sources.kcsc_vector as kcsc_mod
import src.sources.pps_vector as pps_mod
from scripts.eval_common import SourceIndex
from scripts.eval_retrieval import evaluate

ROOT = Path(__file__).parent.parent
DEFAULT_GOLDEN = ROOT / "eval" / "golden_set.jsonl"

GRID = {
    "top_k_final":  [7, 10, 15, 20],
    "candidates_k": [10, 20, 30],
    "rrf_k":        [20, 60, 100],
}

# 지표 키 → evaluate() summary 딕셔너리 키
_METRIC_KEYS = {
    "recall@5":  "recall@5",
    "recall@10": "recall@10",
    "mrr":       "mrr",
    "ndcg@10":   "ndcg@10",
}


def _patch(top_k_final: int, candidates_k: int, rrf_k: int) -> None:
    """모든 소스 모듈의 파라미터를 일괄 monkeypatch."""
    # RRF_K — _rrf()가 모듈 변수를 런타임에 참조하므로 패치 즉시 반영됨
    lh_mod.RRF_K = rrf_k

    # TOP_K_FINAL / CANDIDATES
    lh_mod.TOP_K_FINAL = top_k_final
    lh_mod.TOP_K_CANDIDATES = candidates_k
    kcsc_mod.KCSC_TOP_K_PRIMARY = top_k_final
    pps_mod.PPS_TOP_K_FINAL = top_k_final
    pps_mod.PPS_TOP_K_CANDIDATES = candidates_k


def run_grid(
    golden_path: Path,
    mode: str,
    metric: str,
    combo_slice: list[int] | None = None,
) -> list[dict]:
    ks = [5, 10]
    results = []
    all_combos = list(product(GRID["top_k_final"], GRID["candidates_k"], GRID["rrf_k"]))
    if combo_slice:
        combos = all_combos[combo_slice[0]:combo_slice[1]]
    else:
        combos = all_combos
    n = len(combos)

    print(f"그리드 탐색: {n}개 조합 (mode={mode}, metric={metric})"
          + (f" [slice {combo_slice[0]}:{combo_slice[1]}]" if combo_slice else ""))
    print("인덱스 로딩 중...", flush=True)
    indexes = {src: SourceIndex(src) for src in ("lh", "kcsc", "pps")}
    print("인덱스 로딩 완료\n", flush=True)

    for i, (top_k, cands, rrf) in enumerate(combos, 1):
        _patch(top_k, cands, rrf)
        r = evaluate(golden_path, mode=mode, ks=ks, indexes=indexes)
        overall = r["summary"].get("__overall__", {})
        score = overall.get(_METRIC_KEYS[metric], 0.0)

        row = {
            "top_k_final": top_k,
            "candidates_k": cands,
            "rrf_k": rrf,
            "score": score,
            "recall@5": overall.get("recall@5", 0.0),
            "recall@10": overall.get("recall@10", 0.0),
            "mrr": overall.get("mrr", 0.0),
            "ndcg@10": overall.get("ndcg@10", 0.0),
        }
        results.append(row)
        print(
            f"[{i:>3}/{n}] top_k={top_k:>2} cands={cands:>2} rrf_k={rrf:>3}"
            f"  {metric}={score:.4f}"
            f"  R@5={row['recall@5']:.4f} R@10={row['recall@10']:.4f}"
            f"  MRR={row['mrr']:.4f} nDCG={row['ndcg@10']:.4f}"
        )

    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="파라미터 그리드 서치")
    ap.add_argument("--golden", default=str(DEFAULT_GOLDEN))
    ap.add_argument("--mode", choices=["hybrid", "bm25", "dense"], default="hybrid")
    ap.add_argument(
        "--metric",
        choices=list(_METRIC_KEYS),
        default="ndcg@10",
        help="최적화 기준 지표 (기본: ndcg@10)",
    )
    ap.add_argument(
        "--slice",
        nargs=2,
        type=int,
        metavar=("START", "END"),
        default=None,
        help="조합 인덱스 범위 [START, END) 만 실행 (병렬 분할용)",
    )
    args = ap.parse_args()

    results = run_grid(Path(args.golden), args.mode, args.metric, combo_slice=args.slice)

    # 슬라이스 모드: 결과를 JSON으로 저장하고 종료 (집계는 상위에서)
    if args.slice:
        import json
        out_path = ROOT / "eval" / "results" / f"tune_{args.slice[0]}_{args.slice[1]}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n슬라이스 결과 저장: {out_path}")
        return

    results.sort(key=lambda r: r["score"], reverse=True)
    best = results[0]

    print("\n" + "=" * 70)
    print(f"최적 조합 (기준: {args.metric})")
    print(f"  top_k_final  = {best['top_k_final']}")
    print(f"  candidates_k = {best['candidates_k']}")
    print(f"  rrf_k        = {best['rrf_k']}")
    print(f"  {args.metric} = {best['score']:.4f}")
    print()

    print("상위 5개:")
    header = f"{'top_k':>6} {'cands':>6} {'rrf_k':>6}  {'R@5':>7} {'R@10':>7} {'MRR':>7} {'nDCG':>7}"
    print(header)
    print("-" * len(header))
    for r in results[:5]:
        print(
            f"{r['top_k_final']:>6} {r['candidates_k']:>6} {r['rrf_k']:>6}"
            f"  {r['recall@5']:>7.4f} {r['recall@10']:>7.4f}"
            f"  {r['mrr']:>7.4f} {r['ndcg@10']:>7.4f}"
        )


if __name__ == "__main__":
    main()
