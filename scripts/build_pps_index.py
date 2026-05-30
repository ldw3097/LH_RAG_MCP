"""
조달청 해석사례 인덱스 구축 스크립트 (법제처 OPEN API ppsCgmExpc).

사용법:
    # 전체 빌드 (~864건)
    python scripts/build_pps_index.py

    # 테스트용 부분 빌드 (N건)
    python scripts/build_pps_index.py --limit 20

    # 캐시 재사용 (API 호출 없이 인덱스만 재빌드)
    python scripts/build_pps_index.py --from-cache

    # 기존 인덱스 PKL 삭제 후 재빌드
    python scripts/build_pps_index.py --force
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from crawler.pps_indexer import crawl_to_cache, build_from_cache

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def build(limit: int | None, from_cache: bool):
    if not settings.law_oc_default and not from_cache:
        logger.error(
            "LAW_OC_DEFAULT가 설정되지 않았습니다. .env에 법제처 API 키를 등록하세요."
        )
        sys.exit(1)

    if not from_cache:
        crawl = await crawl_to_cache(limit=limit)
        logger.info(
            "크롤 완료 — 저장: %d, 스킵(최신): %d, 실패: %d",
            crawl.fetched, crawl.skipped_fresh, crawl.failed,
        )

    result = build_from_cache()
    logger.info("인덱스 빌드 완료 — 문서: %d, 청크: %d", result.docs, result.chunks)


def main():
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="조달청 해석사례 인덱스 구축")
    parser.add_argument("--limit", type=int, default=None,
                        help="최대 적재 건수 (테스트용, 기본값: 전체)")
    parser.add_argument("--from-cache", action="store_true",
                        help="API 호출 없이 기존 JSON 캐시에서 인덱스만 재빌드")
    parser.add_argument("--force", action="store_true",
                        help="기존 인덱스 PKL 삭제 후 재빌드")
    args = parser.parse_args()

    if args.force:
        data_dir = Path(settings.pps_data_path)
        for pkl in data_dir.glob("*.pkl"):
            pkl.unlink()
            logger.info("인덱스 삭제: %s", pkl.name)

    asyncio.run(build(args.limit, args.from_cache))


if __name__ == "__main__":
    main()
