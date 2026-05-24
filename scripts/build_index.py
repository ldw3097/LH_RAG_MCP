"""
LH 규정 벡터 인덱스 구축 스크립트 (RSS 기반 증분 동기화).

처음 실행하거나 수동으로 동기화할 때 사용합니다.
이미 최신인 문서는 스킵하고 변경된 문서만 업데이트합니다.

사용법:
    python scripts/build_index.py
    python scripts/build_index.py --rss https://www.lh.or.kr/rss.es?mid=...
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from crawler.indexer import sync_from_rss

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def build(rss_url: str, limit: int | None = None):
    if not rss_url:
        logger.error(
            "RSS URL이 없습니다. --rss 옵션 또는 .env의 LH_RSS_URL을 설정하세요."
        )
        sys.exit(1)
    result = await sync_from_rss(rss_url, limit=limit)
    logger.info(
        "완료 — 신규: %d, 업데이트: %d, 스킵: %d, 실패: %d",
        result.added, result.updated, result.skipped, result.failed,
    )


def main():
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="LH 규정 벡터 인덱스 구축 (RSS 기반)")
    parser.add_argument(
        "--rss",
        default=settings.lh_rss_url,
        help="LH 규정 RSS 피드 URL (기본값: .env의 LH_RSS_URL)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="처리할 최대 문서 수 (테스트용, 기본값: 전체)",
    )
    args = parser.parse_args()
    asyncio.run(build(args.rss, limit=args.limit))


if __name__ == "__main__":
    main()
