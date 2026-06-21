"""국토안전관리원 건설안전 사고통계 진단 소스.

벡터 검색이 아니라 정제 레코드를 메모리에 적재해 질의마다 즉석 집계한다(37k행, <10ms).
예정 공사의 공종 소분류·작업프로세스·시설물 소분류를 받아, 과거 사고의
인적사고종류(대분류) 분포와 baseline 대비 lift를 반환한다.

백오프: n>=MIN_N 이 되는 첫 레벨을 채택.
  L1 = 공종소 + 작업프로세스 + 시설물소
  L2 = 공종소 + 작업프로세스
  L3 = 작업프로세스
표본이 끝내 부족하면 마지막(최대 표본) 레벨을 채택하고 low_sample 플래그.
작업프로세스가 없으면 baseline 분포만 반환한다.
"""

import json
import logging
import pickle
import threading
from collections import Counter
from pathlib import Path

from src.config import settings

logger = logging.getLogger(__name__)

MIN_N = 20                  # 채택 최소 표본
LIFT_HIGH = 1.2            # 이 배수 이상이면 '↑' 강조
LIFT_LOW = 0.8            # 이 배수 이하이면 '↓' 표시
SKIP_VALUE = "불명"         # 해당 분류 미지정 의사 (선택 필드)

# 레코드 튜플 인덱스
_GONGSO, _PROCESS, _FACILITY, _ACC = 0, 1, 2, 3


def _norm(v: str) -> str:
    """해석용 정규화 — 공백·'및' 제거."""
    return v.replace(" ", "").replace("및", "").strip()


class CsiStatsSource:
    source_id = "csi_stats"

    def __init__(self):
        self._records: list[tuple] = []
        self._baseline_counts: dict[str, int] = {}
        self._baseline_total: int = 0
        self._vocab: dict[str, dict[str, str]] = {}      # field → {정식명: 별칭}
        self._resolvers: dict[str, dict[str, str]] = {}  # field → {키: 정식명}
        self._lock = threading.Lock()
        self._loaded = False

    def _ensure_loaded(self):
        with self._lock:
            if self._loaded:
                return
            # 데이터/어휘 로드는 어떤 실패든 서버 전체를 죽이지 않도록 방어한다.
            # (파일 누락·손상 시 경고만 남기고 빈 상태로 — csi 도구만 '데이터 없음' 반환)
            try:
                path = Path(settings.csi_data_path) / settings.csi_pkl_name
                if not path.exists():
                    logger.warning(
                        "건설안전 사고통계 데이터 없음 — scripts/build_csi_index.py를 실행하세요."
                    )
                    return
                with path.open("rb") as f:
                    payload = pickle.load(f)
                self._records = payload["records"]
                self._baseline_counts = payload["baseline_counts"]
                self._baseline_total = payload["baseline_total"]

                vpath = Path(settings.csi_data_path) / "csi_vocab.json"
                if vpath.exists():
                    with vpath.open(encoding="utf-8") as f:
                        self._vocab = json.load(f)
                    self._resolvers = {
                        field: self._build_resolver(fmap)
                        for field, fmap in self._vocab.items()
                    }
                else:
                    logger.warning("건설안전 사고통계 어휘(csi_vocab.json) 없음 — 입력 해석 제한됨.")
                logger.info(
                    "건설안전 사고통계 로드: %d행, baseline 표본 %d, 어휘 %d필드",
                    payload.get("row_count", len(self._records)),
                    self._baseline_total, len(self._vocab),
                )
            except Exception as e:
                logger.error("건설안전 사고통계 로드 실패 — csi 도구 비활성: %s", e)
                self._records = []
                self._vocab = {}
                self._resolvers = {}
            finally:
                self._loaded = True

    @staticmethod
    def _build_resolver(fmap: dict[str, str]) -> dict[str, str]:
        """{정식명: 별칭} → {정식명·별칭·정규화형 키: 정식명} 역인덱스."""
        resolver: dict[str, str] = {}
        for canon, alias in fmap.items():
            for key in (canon, _norm(canon), alias, _norm(alias)):
                resolver[key] = canon
        return resolver

    def _resolve(self, field: str, value: str) -> tuple[str | None, bool]:
        """입력값 → (정식명, 인식성공). 빈 값이면 (None, True). 미인식이면 (None, False)."""
        v = (value or "").strip()
        if not v:
            return None, True
        resolver = self._resolvers.get(field, {})
        for key in (v, _norm(v)):
            if key in resolver:
                return resolver[key], True
        # 부분일치 폴백 (정규화 기준, 유일할 때만)
        nv = _norm(v)
        cands = {c for c in self._vocab.get(field, {}) if nv and (nv in _norm(c) or _norm(c) in nv)}
        if len(cands) == 1:
            return next(iter(cands)), True
        return None, False

    def _aggregate(self, predicate) -> Counter:
        """predicate(record)가 참이고 타깃 유효인 레코드의 대분류 분포."""
        counts: Counter[str] = Counter()
        for r in self._records:
            if r[_ACC] is not None and predicate(r):
                counts[r[_ACC]] += 1
        return counts

    def assess(self, work_subtype: str, work_process: str, facility_subtype: str) -> dict:
        self._ensure_loaded()

        result = {
            "loaded": bool(self._records),
            "level": "baseline",
            "n": 0,
            "low_sample": False,
            "baseline_total": self._baseline_total,
            "distribution": [],
            "input": {"공종소분류": None, "작업프로세스": None, "시설물소분류": None},
            "error": None,
        }
        if not self._records:
            return result

        # 입력값 → 정식명 해석. 비어있지 않은데 인식 실패하면 유효 별칭과 함께 반환.
        labels = {"work_subtype": "공종소분류", "work_process": "작업프로세스",
                  "facility_subtype": "시설물소분류"}
        resolved: dict[str, str | None] = {}
        for field, raw in (("work_subtype", work_subtype),
                           ("work_process", work_process),
                           ("facility_subtype", facility_subtype)):
            canon, ok = self._resolve(field, raw)
            if not ok:
                result["error"] = {
                    "field": labels[field],
                    "value": (raw or "").strip(),
                    "valid_aliases": sorted(self._vocab.get(field, {}).values()),
                }
                return result
            # '불명' = 해당 분류 미지정 → 빈 값과 동일하게 백오프에서 제외
            resolved[field] = None if canon == SKIP_VALUE else canon

        gongso = resolved["work_subtype"]
        process = resolved["work_process"]
        facility = resolved["facility_subtype"]
        result["input"] = {"공종소분류": gongso, "작업프로세스": process, "시설물소분류": facility}

        # 적용 가능한 백오프 레벨 (구체적 → 일반). 모든 레벨은 작업프로세스를 요구한다.
        levels: list[tuple[str, callable]] = []
        if gongso and process and facility:
            levels.append((
                "L1", lambda r: r[_PROCESS] == process and r[_GONGSO] == gongso
                and r[_FACILITY] == facility,
            ))
        if gongso and process:
            levels.append((
                "L2", lambda r: r[_PROCESS] == process and r[_GONGSO] == gongso,
            ))
        if process:
            levels.append(("L3", lambda r: r[_PROCESS] == process))

        chosen_level = None
        chosen_counts: Counter = Counter()
        last_level = None
        last_counts: Counter = Counter()
        for name, pred in levels:
            counts = self._aggregate(pred)
            n = sum(counts.values())
            last_level, last_counts = name, counts
            if n >= MIN_N:
                chosen_level, chosen_counts = name, counts
                break

        if chosen_level is None:
            # 어느 레벨도 MIN_N 미달 → 최대표본(=가장 일반) 레벨 채택, 플래그
            if last_level is not None and sum(last_counts.values()) > 0:
                chosen_level, chosen_counts = last_level, last_counts
                result["low_sample"] = True
            else:
                # 작업프로세스 미입력 등 → baseline 분포만
                chosen_level = "baseline"
                chosen_counts = Counter(self._baseline_counts)

        n = sum(chosen_counts.values())
        result["level"] = chosen_level
        result["n"] = n

        dist = []
        for acc, cnt in chosen_counts.most_common():
            pct = 100.0 * cnt / n if n else 0.0
            base_cnt = self._baseline_counts.get(acc, 0)
            base_pct = 100.0 * base_cnt / self._baseline_total if self._baseline_total else 0.0
            lift = (pct / base_pct) if base_pct > 0 else None
            dist.append({
                "type": acc, "count": cnt, "pct": pct,
                "baseline_pct": base_pct, "lift": lift,
            })
        result["distribution"] = dist
        return result
