"""
RSS 감시 데몬.

설정된 간격으로 RSS를 폴링하고 sync_from_rss를 호출합니다.
변경 감지와 증분 업데이트는 sync_from_rss 내부의 pubDate 비교가 처리합니다.
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from crawler.indexer import sync_from_rss
from src.config import settings

logger = logging.getLogger(__name__)


async def watch(rss_url: str, interval_seconds: int = 3600):
    """RSS 피드를 interval_seconds마다 동기화합니다 (기본 1시간)."""
    logger.info("RSS 감시 시작: %s (간격: %ds)", rss_url, interval_seconds)
    while True:
        result = await sync_from_rss(rss_url)
        if result.added or result.updated:
            logger.info(
                "변경 반영 완료 — 신규: %d, 업데이트: %d",
                result.added, result.updated,
            )
        else:
            logger.info("변경 없음 (스킵: %d건)", result.skipped)
        await asyncio.sleep(interval_seconds)


if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="LH RSS 감시 데몬")
    parser.add_argument(
        "--url",
        default=settings.lh_rss_url,
        help="LH 규정 RSS 피드 URL (기본값: .env의 LH_RSS_URL)",
    )
    parser.add_argument("--interval", type=int, default=3600, help="폴링 주기(초), 기본 3600")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    asyncio.run(watch(args.url, args.interval))
