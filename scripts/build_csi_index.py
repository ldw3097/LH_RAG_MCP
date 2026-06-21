"""국토안전관리원 건설안전 사고통계 인덱스 구축 스크립트.

CSV(cp949) → 정제 레코드 + baseline pkl. 벡터 인덱스가 아닌 통계 집계용 데이터다.

사용법:
    # 전체 빌드 (CSV 경로 지정)
    python scripts/build_csi_index.py --source "data/csi/raw/국토안전관리원_건설안전사고사례_20250630.csv"

    # 테스트용 부분 빌드 (N행)
    python scripts/build_csi_index.py --source <csv> --limit 1000

    # 기존 pkl 삭제 후 재빌드
    python scripts/build_csi_index.py --source <csv> --force
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from crawler.csi_indexer import build_from_csv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _default_source() -> str | None:
    """data/csi/raw/ 아래 첫 번째 CSV를 기본 소스로 사용."""
    raw_dir = Path(settings.csi_data_path) / "raw"
    if raw_dir.is_dir():
        for csv_file in sorted(raw_dir.glob("*.csv")):
            return str(csv_file)
    return None


def main():
    from dotenv import load_dotenv
    load_dotenv()

    parser = argparse.ArgumentParser(description="국토안전관리원 건설안전 사고통계 인덱스 구축")
    parser.add_argument("--source", type=str, default=None,
                        help="원본 CSV 경로 (기본값: data/csi/raw/*.csv 중 첫 파일)")
    parser.add_argument("--limit", type=int, default=None,
                        help="최대 적재 행수 (테스트용, 기본값: 전체)")
    parser.add_argument("--force", action="store_true",
                        help="기존 pkl 삭제 후 재빌드")
    args = parser.parse_args()

    source = args.source or _default_source()
    if not source:
        logger.error(
            "CSV 소스를 찾을 수 없습니다. --source <경로>를 지정하거나 "
            "data/csi/raw/ 아래에 CSV를 두세요."
        )
        sys.exit(1)

    if args.force:
        pkl = Path(settings.csi_data_path) / settings.csi_pkl_name
        if pkl.exists():
            pkl.unlink()
            logger.info("기존 인덱스 삭제: %s", pkl.name)

    summary = build_from_csv(source, limit=args.limit)
    logger.info("빌드 완료 — 행: %d, baseline 표본: %d",
                summary["row_count"], summary["baseline_total"])
    logger.info("사고유형(대분류) 분포:")
    for acc, cnt in summary["baseline_counts"].items():
        logger.info("  %6d  %s", cnt, acc)


if __name__ == "__main__":
    main()
