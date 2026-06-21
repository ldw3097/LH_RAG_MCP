"""국토안전관리원 건설안전 사고사례 적재 — CSV → 정제 레코드 pkl.

벡터 인덱서가 아니다. 통계 집계용으로 4개 필드(공종 소분류·작업프로세스·시설물 소분류·
인적사고종류 대분류)만 추출·정규화해 컴팩트 레코드 리스트로 만들고, baseline 분포를
사전계산해 pickle로 저장한다.

원본 CSV는 cp949 인코딩이다 (HWP→PDF 계열이 아닌 행정 통계 CSV).
"""

import csv
import json
import logging
import pickle
import re
from collections import Counter
from pathlib import Path

from src.config import settings

logger = logging.getLogger(__name__)

# 통계에서 제외할 노이즈 값 (입력 부실 / 분류불가)
NOISE = {"미입력", "기타", "없음", "분류불능", ""}

# CSV 컬럼명 → 레코드 필드
COL_GONGSO = "공종(소분류)"
COL_PROCESS = "작업프로세스"
COL_FACILITY = "시설물 소분류"
COL_ACC_MAJOR = "인적사고종류(대분류)"

# 입력 필드(파라미터명) → 레코드 튜플 인덱스
INPUT_FIELDS = {"work_subtype": 0, "work_process": 1, "facility_subtype": 2}

# '불명' = 해당 분류를 특정하지 않겠다는 의사. 선택 필드(공종소·시설물소)에만 허용.
SKIP_VALUE = "불명"
SKIP_ALLOWED_FIELDS = ("work_subtype", "facility_subtype")


def _norm(v: str | None) -> str | None:
    """공백 제거 후 노이즈면 None, 아니면 정제 문자열."""
    s = (v or "").strip()
    return None if s in NOISE else s


def _alias(v: str) -> str:
    """정식 카테고리명 → 짧은 별칭. 괄호·'및'·공백 제거 후 '공사'/'작업' 접미사 절단."""
    a = re.sub(r"\(.*?\)", "", v)
    a = a.replace(" 및 ", "").replace(" ", "")
    for suf in ("공사", "작업"):
        # 접미사를 떼고 2글자 이상 남을 때만 절단 (단일 글자 별칭 방지)
        if a.endswith(suf) and len(a) - len(suf) >= 2:
            a = a[: -len(suf)]
    return a


def _build_vocab(records: list[tuple]) -> dict[str, dict[str, str]]:
    """입력 필드별 {정식명: 별칭} 맵. 별칭 충돌 시 해당 정식명은 자기 자신을 별칭으로 둔다."""
    vocab: dict[str, dict[str, str]] = {}
    for field, idx in INPUT_FIELDS.items():
        canons = sorted({r[idx] for r in records if r[idx]})
        alias_to_canons: dict[str, list[str]] = {}
        for c in canons:
            alias_to_canons.setdefault(_alias(c), []).append(c)
        field_map: dict[str, str] = {}
        for c in canons:
            a = _alias(c)
            field_map[c] = a if len(alias_to_canons[a]) == 1 else c
        # 선택 필드에는 '불명'(미지정 의사)을 값으로 추가
        if field in SKIP_ALLOWED_FIELDS:
            field_map[SKIP_VALUE] = SKIP_VALUE
        vocab[field] = field_map
    return vocab


def _pkl_path() -> Path:
    return Path(settings.csi_data_path) / settings.csi_pkl_name


def _vocab_path() -> Path:
    return Path(settings.csi_data_path) / "csi_vocab.json"


def build_from_csv(csv_path: str, limit: int | None = None) -> dict:
    """CSV를 읽어 정제 레코드 + baseline을 pickle로 저장하고 요약 dict를 반환한다."""
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV 파일을 찾을 수 없습니다: {csv_path}")

    records: list[tuple[str | None, str | None, str | None, str | None]] = []
    row_count = 0
    with path.open(encoding="cp949") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if limit is not None and row_count >= limit:
                break
            row_count += 1
            records.append((
                _norm(row.get(COL_GONGSO)),
                _norm(row.get(COL_PROCESS)),
                _norm(row.get(COL_FACILITY)),
                _norm(row.get(COL_ACC_MAJOR)),
            ))

    # baseline: 작업프로세스 유효 & 타깃(인적사고 대분류) 유효인 레코드의 대분류 분포
    baseline_counts: Counter[str] = Counter()
    for _gongso, process, _facility, acc in records:
        if process is not None and acc is not None:
            baseline_counts[acc] += 1
    baseline_total = sum(baseline_counts.values())

    payload = {
        "records": records,
        "baseline_counts": dict(baseline_counts),
        "baseline_total": baseline_total,
        "source": str(path),
        "row_count": row_count,
    }

    out = _pkl_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("wb") as f:
        pickle.dump(payload, f)

    # 입력 필드별 어휘(정식명↔별칭) — 도구 설명·입력 해석에 사용
    vocab = _build_vocab(records)
    with _vocab_path().open("w", encoding="utf-8") as f:
        json.dump(vocab, f, ensure_ascii=False, indent=1)

    logger.info(
        "CSI 적재 완료 — 행 %d, baseline 표본 %d, 사고유형 %d종 → %s",
        row_count, baseline_total, len(baseline_counts), out,
    )
    logger.info(
        "어휘 저장 — 공종소 %d · 작업프로세스 %d · 시설물소 %d → %s",
        len(vocab["work_subtype"]), len(vocab["work_process"]),
        len(vocab["facility_subtype"]), _vocab_path(),
    )
    return {
        "row_count": row_count,
        "baseline_total": baseline_total,
        "baseline_counts": dict(baseline_counts.most_common()),
        "pkl_path": str(out),
        "vocab_path": str(_vocab_path()),
    }
