"""
LH 규정 인덱스 구축 스크립트.

사용법:
    # RSS에서 증분 동기화 (기본)
    python scripts/build_index.py
    python scripts/build_index.py --limit 5

    # 기존 마크다운 캐시에서 재빌드 (LH 사이트 접속 없음)
    python scripts/build_index.py --from-markdown

    # 청킹 로직 변경 등 완전 재빌드가 필요할 때
    python scripts/build_index.py --from-markdown --force
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from crawler.indexer import sync_from_rss, sync_indexes_from_markdown

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def build_rss(rss_url: str, limit: int | None = None):
    if not rss_url:
        logger.error("RSS URL이 없습니다. --rss 옵션 또는 .env의 LH_RSS_URL을 설정하세요.")
        sys.exit(1)
    result = await sync_from_rss(rss_url, limit=limit)
    logger.info(
        "완료 — 신규: %d, 업데이트: %d, 삭제: %d, 실패: %d",
        result.added, result.updated, result.removed, result.failed,
    )


def main():
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="LH 규정 인덱스 구축")
    parser.add_argument(
        "--from-markdown",
        action="store_true",
        help="LH 사이트 접속 없이 data/markdown/ 캐시에서 재빌드",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="--from-markdown과 함께 사용. 기존 인덱스를 완전히 삭제 후 재빌드",
    )
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

    if args.from_markdown:
        if args.force:
            bm25_dir = Path(settings.bm25_path)
            for pkl in bm25_dir.glob("*.pkl"):
                pkl.unlink()
                logger.info("인덱스 삭제: %s", pkl.name)
        result = sync_indexes_from_markdown()
        logger.info(
            "완료 — 신규: %d, 업데이트: %d, 삭제: %d",
            result.added, result.updated, result.removed,
        )
    else:
        asyncio.run(build_rss(args.rss, limit=args.limit))


if __name__ == "__main__":
    main()
