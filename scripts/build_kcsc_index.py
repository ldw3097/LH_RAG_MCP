"""
KCSC 건설기준 인덱스 구축 스크립트.

사용법:
    # 전체 빌드 (KDS + KCS + LHCS)
    python scripts/build_kcsc_index.py

    # 유형 지정
    python scripts/build_kcsc_index.py --type KDS
    python scripts/build_kcsc_index.py --type KCS

    # 테스트용 부분 빌드 (유형별 N개)
    python scripts/build_kcsc_index.py --limit 5

    # 캐시 재사용 (API 호출 없이 인덱스만 재빌드)
    python scripts/build_kcsc_index.py --from-cache
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from crawler.kcsc_indexer import crawl_to_cache, build_from_cache

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def build(code_type: str | None, limit: int | None, from_cache: bool):
    if not settings.kcsc_api_key:
        logger.error(
            "KCSC_API_KEY가 설정되지 않았습니다. .env에 KCSC_API_KEY를 등록하세요."
        )
        sys.exit(1)

    if not from_cache:
        crawl = await crawl_to_cache(code_type=code_type, limit=limit)
        logger.info(
            "크롤 완료 — 저장: %d, 스킵(최신): %d, 실패: %d",
            crawl.fetched, crawl.skipped_fresh, crawl.failed,
        )

    result = build_from_cache()
    logger.info(
        "인덱스 빌드 완료 — 문서: %d, 청크: %d, 인용 엣지: %d",
        result.docs, result.chunks, result.edges,
    )


def main():
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="KCSC 건설기준 인덱스 구축")
    parser.add_argument(
        "--type",
        choices=["KDS", "KCS", "LHCS"],
        default=None,
        help="크롤할 코드 유형 (기본값: 전체). 인덱스 빌드는 항상 캐시 전체 대상.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="유형별 최대 코드 수 (테스트용, 기본값: 전체)",
    )
    parser.add_argument(
        "--from-cache",
        action="store_true",
        help="API 호출 없이 기존 JSON 캐시에서 인덱스만 재빌드",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="기존 인덱스 PKL 삭제 후 재빌드",
    )
    args = parser.parse_args()

    if args.force:
        data_dir = Path(settings.kcsc_data_path)
        for pkl in data_dir.glob("*.pkl"):
            pkl.unlink()
            logger.info("인덱스 삭제: %s", pkl.name)

    asyncio.run(build(args.type, args.limit, args.from_cache))


if __name__ == "__main__":
    main()
