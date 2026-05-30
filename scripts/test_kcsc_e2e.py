"""KCSC 건설기준 검색 E2E 테스트.

다양한 질문 유형으로 search_construction_standards 도구의 실제 동작을 검증한다.
"""

import asyncio
import os
import sys
import textwrap
import time

# DeepInfra API 키가 없으면 BM25만 사용 (인용 확장에는 Dense 불필요)
# 실제 Dense 검색도 테스트하려면 .env 또는 환경변수에 DEEPINFRA_API_KEY를 설정한다.

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.sources.kcsc_vector import KCSCVectorSource

# ──────────────────────────────────────────────
# 테스트 케이스: (설명, query, keywords)
# ──────────────────────────────────────────────
TEST_CASES = [
    (
        "콘크리트 압축강도 시험",
        "콘크리트 압축강도 시험방법 및 기준",
        "압축강도 시험",
    ),
    (
        "철근 이음 길이",
        "철근 겹침이음 및 기계적이음 길이 규정",
        "철근 이음 겹침",
    ),
    (
        "지반 지지력 평가",
        "기초 지반의 지지력 산정 방법",
        "지반 지지력 기초",
    ),
    (
        "방수 공사 재료",
        "지하 구조물 방수 공사 재료 및 시공 기준",
        "방수 재료 시공",
    ),
    (
        "용접 검사 기준",
        "강구조물 용접부 비파괴 검사 기준",
        "용접 검사 비파괴",
    ),
    (
        "말뚝 재하 시험",
        "말뚝 재하시험 방법 및 지지력 확인",
        "말뚝 재하시험",
    ),
    (
        "거푸집 존치 기간",
        "콘크리트 거푸집 및 동바리 존치 기간 규정",
        "거푸집 존치 기간",
    ),
]

DIVIDER = "=" * 72
SUB_DIV = "-" * 72


def truncate(text: str, max_chars: int = 300) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"… (이하 {len(text) - max_chars}자 생략)"


async def run_tests():
    source = KCSCVectorSource()
    source._ensure_loaded()

    bm25_loaded = source._bm25 is not None
    dense_loaded = source._dense is not None
    graph_loaded = source._graph is not None

    print(DIVIDER)
    print("KCSC 건설기준 검색 E2E 테스트")
    print(DIVIDER)
    print(f"  BM25  인덱스: {'✓' if bm25_loaded else '✗ (없음)'}"
          + (f"  — {len(source._bm25.ids):,}청크" if bm25_loaded else ""))
    print(f"  Dense 인덱스: {'✓' if dense_loaded else '✗ (없음 — BM25만 사용)'}"
          + (f"  — {len(source._dense.ids):,}청크" if dense_loaded else ""))
    print(f"  인용 그래프:  {'✓' if graph_loaded else '✗ (없음)'}"
          + (f"  — 노드 {len(source._graph.node_to_chunks):,}, 엣지 {len(source._graph.edges):,}" if graph_loaded else ""))
    print()

    total_primary = 0
    total_citation = 0
    pass_count = 0

    for idx, (desc, query, keywords) in enumerate(TEST_CASES, 1):
        print(DIVIDER)
        print(f"[{idx}/{len(TEST_CASES)}] {desc}")
        print(f"  query   : {query}")
        print(f"  keywords: {keywords}")
        print(SUB_DIV)

        t0 = time.perf_counter()
        results = await source.search(query, keywords)
        elapsed = time.perf_counter() - t0

        primary = [r for r in results if r.metadata.get("via") != "citation"]
        citations = [r for r in results if r.metadata.get("via") == "citation"]

        total_primary += len(primary)
        total_citation += len(citations)

        if primary:
            pass_count += 1

        print(f"  결과: 1차={len(primary)}건, 인용확장={len(citations)}건  ({elapsed:.2f}s)")
        print()

        for i, r in enumerate(primary, 1):
            print(f"  [{i}] {r.title}")
            content_preview = truncate(r.content, 280)
            for line in textwrap.wrap(content_preview, width=68):
                print(f"      {line}")
            meta = r.metadata
            print(f"      → 코드: {meta.get('code_type','')} {meta.get('code','')}  "
                  f"node_id: {meta.get('node_id','')}")
            print()

        if citations:
            print("  ── 인용 참조 결과 ──────────────────────────────────────")
            for i, r in enumerate(citations, 1):
                print(f"  [인용{i}] {r.title}")
                content_preview = truncate(r.content, 200)
                for line in textwrap.wrap(content_preview, width=68):
                    print(f"          {line}")
                print()
        else:
            print("  (인용 참조 없음)")
            print()

    print(DIVIDER)
    print("테스트 요약")
    print(DIVIDER)
    print(f"  총 케이스  : {len(TEST_CASES)}건")
    print(f"  결과 있음  : {pass_count}건 / {len(TEST_CASES)}건")
    print(f"  평균 1차결과: {total_primary/len(TEST_CASES):.1f}건")
    print(f"  총 인용확장 : {total_citation}건 (평균 {total_citation/len(TEST_CASES):.1f}건)")

    if pass_count == len(TEST_CASES):
        print("\n  ✓ 전 케이스 PASS")
    else:
        fail = len(TEST_CASES) - pass_count
        print(f"\n  ✗ {fail}건 FAIL (결과 0건)")
    print(DIVIDER)


if __name__ == "__main__":
    asyncio.run(run_tests())
