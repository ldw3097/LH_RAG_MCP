"""search_law 개선 전후 정확도 비교 평가 스크립트.

측정 항목:
  1. AND→0건 회복률: 복합 키워드를 넣었을 때 법령/행정규칙 블록에 결과가 나오는지
  2. 정확매칭 정밀도: 법령 블록 1위가 기대 법령명과 일치하는지
  3. 블록 구조: 조문/법령/행정규칙 블록이 모두 있는지
  4. 응답 지연: 검색 소요 시간

사용법:
    python scripts/eval_law_api.py
    python scripts/eval_law_api.py --verbose
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from src.context import law_oc_var
from src.sources.law_api import LawApiSource

DIVIDER = "=" * 72
SUB_DIV = "-" * 72

# ── 테스트 케이스 ─────────────────────────────────────────────────────────────
# format: (설명, query, keywords, 검사항목dict)
#
# 검사항목:
#   expected_law_top   : 법령 블록 1위 법령명에 포함되어야 할 문자열
#   expect_law_results : 법령 블록에 결과가 1개 이상 있어야 하는지 (AND→0건 회복)
#   expect_article     : 조문 블록에 결과가 있어야 하는지
#   expect_admrul      : 행정규칙 블록에 결과가 있어야 하는지 (선택적)

TEST_CASES = [
    # ── 정확매칭 품질 ──────────────────────────────────────────────────────────
    {
        "desc": "정확매칭: '민법' → 난민법·시행령보다 민법이 1위",
        "query": "민법 계약해제 손해배상",
        "keywords": "민법",
        "expected_law_top": "민법",
        "expect_law_results": True,
        "expect_article": True,
    },
    {
        "desc": "정확매칭: '국가계약법' 축약어 없이 정식명 검색",
        "query": "국가 계약 일반조건 지체상금",
        "keywords": "국가를 당사자로 하는 계약에 관한 법률",
        "expected_law_top": "국가를 당사자로 하는 계약에 관한 법률",
        "expect_law_results": True,
        "expect_article": True,
    },
    # ── AND→0건 회복 ───────────────────────────────────────────────────────────
    {
        "desc": "AND→0건 회복: 법령명+본문어 혼합 → 비법령명어 제거 후 결과",
        "query": "도급 사업주의 안전조치 의무 위반 처벌",
        "keywords": "산업안전보건법 도급 처벌 기준",
        "expected_law_top": "산업안전보건법",
        "expect_law_results": True,
        "expect_article": True,
    },
    {
        "desc": "AND→0건 회복: 공공주택+다수 부가어",
        "query": "공공주택 분양가 산정 기준 및 절차",
        "keywords": "공공주택특별법 분양가 기준 절차",
        "expected_law_top": "공공주택",
        "expect_law_results": True,
        "expect_article": True,
    },
    # ── 행정규칙(국토부) ───────────────────────────────────────────────────────
    {
        "desc": "행정규칙: 국토부 고시 검색",
        "query": "주택 분양가 상한제 적용 기준",
        "keywords": "분양가 상한제 고시",
        "expected_law_top": None,
        "expect_law_results": True,
        "expect_article": True,
        "expect_admrul": True,
    },
    # ── 건설·안전 도메인 ───────────────────────────────────────────────────────
    {
        "desc": "건설 도메인: 건축법 시행령 정확매칭",
        "query": "건축물 용도변경 허가 절차",
        "keywords": "건축법",
        "expected_law_top": "건축법",
        "expect_law_results": True,
        "expect_article": True,
    },
    {
        "desc": "AND→0건 회복: 부동산+다수 부가어",
        "query": "임대차 보증금 반환 소송 절차",
        "keywords": "주택임대차보호법 보증금 반환 소송",
        "expected_law_top": "주택임대차",
        "expect_law_results": True,
        "expect_article": True,
    },
]


def _extract_blocks(results):
    """SearchResult 리스트를 block 키로 분류."""
    blocks = {"조문": [], "법령": [], "행정규칙": []}
    for r in results:
        b = r.metadata.get("block", "조문")
        if b in blocks:
            blocks[b].append(r)
    return blocks


def _check(case: dict, blocks: dict, elapsed: float, verbose: bool) -> dict:
    """단일 케이스 검사. {passed, details} 반환."""
    checks = []
    passed_all = True

    # 1. 조문 블록
    if case.get("expect_article", False):
        ok = len(blocks["조문"]) > 0
        checks.append(("조문 블록 결과 있음", ok, f"{len(blocks['조문'])}건"))
        if not ok:
            passed_all = False

    # 2. 법령 블록 결과 수
    if case.get("expect_law_results", False):
        ok = len(blocks["법령"]) > 0
        checks.append(("법령 블록 결과 있음", ok, f"{len(blocks['법령'])}건"))
        if not ok:
            passed_all = False

    # 3. 법령 블록 1위 정확매칭
    expected_top = case.get("expected_law_top")
    if expected_top and blocks["법령"]:
        top_title = blocks["법령"][0].title
        ok = expected_top in top_title
        checks.append((f"법령 1위 ⊇ '{expected_top}'", ok, f"실제: {top_title!r}"))
        if not ok:
            passed_all = False

    # 4. 행정규칙 블록 (선택적)
    if case.get("expect_admrul", False):
        ok = len(blocks["행정규칙"]) > 0
        checks.append(("행정규칙 블록 결과 있음", ok, f"{len(blocks['행정규칙'])}건"))
        # 행정규칙은 soft check (없어도 FAIL 아님)

    return {"passed": passed_all, "checks": checks, "elapsed": elapsed}


async def run(verbose: bool = False):
    source = LawApiSource()
    token = law_oc_var.set("we-407bt")

    results_summary = []

    try:
        print(DIVIDER)
        print("search_law 정확도 평가 (로컬 코드 직접 호출)")
        print(DIVIDER)

        for i, case in enumerate(TEST_CASES, 1):
            print(f"\n[{i}/{len(TEST_CASES)}] {case['desc']}")
            print(f"  query   : {case['query']}")
            print(f"  keywords: {case['keywords']}")

            t0 = time.perf_counter()
            try:
                raw = await source.search(case["query"], case["keywords"])
                elapsed = time.perf_counter() - t0
            except Exception as e:
                elapsed = time.perf_counter() - t0
                print(f"  ✗ 예외: {e}  ({elapsed:.2f}s)")
                results_summary.append({"passed": False, "checks": [], "elapsed": elapsed})
                continue

            blocks = _extract_blocks(raw)
            eval_result = _check(case, blocks, elapsed, verbose)
            results_summary.append(eval_result)

            status = "✓ PASS" if eval_result["passed"] else "✗ FAIL"
            print(f"  {status}  ({elapsed:.2f}s)")
            print(f"  블록별 건수: 조문={len(blocks['조문'])} 법령={len(blocks['법령'])} 행정규칙={len(blocks['행정규칙'])}")

            for check_name, ok, detail in eval_result["checks"]:
                icon = "  ✓" if ok else "  ✗"
                print(f"  {icon} {check_name} — {detail}")

            if verbose:
                if blocks["법령"]:
                    print(f"\n  [법령 블록 상위 3건]")
                    for j, r in enumerate(blocks["법령"][:3], 1):
                        snippet = r.content[:80].replace("\n", " ")
                        print(f"    {j}. {r.title} | {snippet}…")
                if blocks["조문"]:
                    print(f"\n  [조문 블록 상위 2건]")
                    for j, r in enumerate(blocks["조문"][:2], 1):
                        snippet = r.content[:80].replace("\n", " ")
                        print(f"    {j}. {r.title} | {snippet}…")

    finally:
        law_oc_var.reset(token)
        await source.aclose()

    # ── 요약 ─────────────────────────────────────────────────────────────────
    print(f"\n{DIVIDER}")
    print("평가 요약")
    print(DIVIDER)
    pass_count = sum(1 for r in results_summary if r["passed"])
    avg_elapsed = sum(r["elapsed"] for r in results_summary) / len(results_summary)

    print(f"  PASS    : {pass_count} / {len(TEST_CASES)}")
    print(f"  FAIL    : {len(TEST_CASES) - pass_count} / {len(TEST_CASES)}")
    print(f"  평균 응답: {avg_elapsed:.2f}s")

    # 항목별 집계
    check_totals: dict[str, list[bool]] = {}
    for r in results_summary:
        for name, ok, _ in r["checks"]:
            check_totals.setdefault(name, []).append(ok)
    print()
    for name, oks in check_totals.items():
        pct = sum(oks) / len(oks) * 100
        print(f"  {name}: {sum(oks)}/{len(oks)} ({pct:.0f}%)")

    print(DIVIDER)
    return pass_count == len(TEST_CASES)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    ok = asyncio.run(run(verbose=args.verbose))
    sys.exit(0 if ok else 1)
