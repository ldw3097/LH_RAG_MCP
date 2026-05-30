"""
국가건설기준센터(KCSC) Open API 클라이언트.

LH 규정과 달리 KCSC는 공식 Open API(JSON)를 제공하므로 크롤링/PDF 변환이 불필요하다.
  - CodeList:   전체 코드 목록(KDS/KCS/LHCS)
  - CodeViewer: 코드별 목차 항목(조문) 배열 + HTML 본문

본문은 목차(label="3.2" 등) 단위로 구조화되어 오므로, 청킹·그래프 노드를 조문 단위로 맞춘다.
"""

import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings

logger = logging.getLogger(__name__)

# 인덱싱 대상 코드 타입
WANTED_TYPES = ("KDS", "KCS", "LHCS")

# 인용으로 인정하는 코드 타입 (본문에서 다른 기준을 인용할 때 쓰는 접두어)
_CITATION_TYPES = ("KDS", "KCS", "LHCS", "EXCS", "SMCS")

# LHCS 전용 8자리 패턴 (2-2-2-2). 6자리 패턴보다 먼저 적용한다.
_LHCS8_RE = re.compile(
    r"LHCS\s*"
    r"(?P<code>\d{2}\s*\d{2}\s*\d{2}\s*\d{2})"
    r"(?:\s*(?:의|,)?\s*\(?(?P<label>\d+(?:\.\d+)*)\)?)?"
)

# 6자리 패턴 — KDS/KCS/EXCS/SMCS 전용 및 LHCS 폴백
_CITATION_RE = re.compile(
    r"(?P<type>" + "|".join(_CITATION_TYPES) + r")\s*"
    r"(?P<code>\d{2}\s*\d{2}\s*\d{2})"
    r"(?:\s*(?:의|,)?\s*\(?(?P<label>\d+(?:\.\d+)*)\)?)?"
)


@dataclass
class CodeMeta:
    """CodeList 항목."""
    code_type: str       # KDS | KCS | LHCS
    code: str            # "142010"
    full_code: str       # "2010114010"
    name: str            # "파형강판 암거"
    version: str         # "2025"
    update_date: str     # ISO datetime 문자열

    @property
    def doc_key(self) -> str:
        """청크 ID 접두어 / Dense 증분 단위. 예: 'KCS142010'."""
        return f"{self.code_type}{self.code}"

    @property
    def node_id(self) -> str:
        """문서 레벨 그래프 노드. 예: 'KCS:142010'."""
        return f"{self.code_type}:{self.code}"

    @property
    def viewer_url(self) -> str:
        return f"https://www.kcsc.re.kr/StandardCode/Viewer/{self.code_type}/{self.code}"


@dataclass
class Section:
    """CodeViewer 목차 항목 = 조문."""
    label: str           # "3.2" (없을 수 있음)
    level: int
    title: str           # "3.2 재료"
    text: str            # HTML 제거된 본문

    def node_id(self, meta: CodeMeta) -> str:
        """조문 그래프 노드. label 있으면 'KCS:142010:3.2', 없으면 문서 레벨."""
        if self.label:
            return f"{meta.code_type}:{meta.code}:{self.label}"
        return meta.node_id


def _strip_html(html: str) -> str:
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(separator="\n")
    # 과도한 빈 줄 정리
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _norm_code(raw: str) -> str:
    """'14 20 10' → '142010'."""
    return re.sub(r"\s+", "", raw)


class KcscApiClient:
    """KCSC Open API 비동기 클라이언트."""

    def __init__(self, api_key: str | None = None, base: str | None = None):
        self._key = api_key or settings.kcsc_api_key
        self._base = (base or settings.kcsc_api_base).rstrip("/")
        if not self._key:
            raise ValueError(
                "KCSC_API_KEY가 설정되지 않았습니다. .env에 KCSC_API_KEY를 등록하세요."
            )
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; LH-RAG-Bot/1.0)"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_code_list(self) -> list[CodeMeta]:
        """전체 코드 목록을 조회하고 KDS/KCS/LHCS만 반환합니다."""
        url = f"{self._base}/CodeList"
        resp = await self._client.get(url, params={"key": self._key})
        resp.raise_for_status()
        data = resp.json()
        result: list[CodeMeta] = []
        for item in data:
            ctype = item.get("codeType", "")
            if ctype not in WANTED_TYPES:
                continue
            result.append(CodeMeta(
                code_type=ctype,
                code=str(item.get("code", "")),
                full_code=str(item.get("fullCode", "")),
                name=item.get("name", ""),
                version=str(item.get("version", "")),
                update_date=item.get("updateDate", "") or "",
            ))
        logger.info("CodeList 조회: 전체 %d건 중 대상 %d건", len(data), len(result))
        return result

    async def fetch_sections(self, meta: CodeMeta) -> list[Section]:
        """CodeViewer를 조회하여 조문(Section) 목록을 반환합니다."""
        url = f"{self._base}/CodeViewer/{meta.code_type}/{meta.code}"
        resp = await self._client.get(url, params={"key": self._key})
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return []
        items = data[0].get("list") or []
        sections: list[Section] = []
        for it in items:
            text = _strip_html(it.get("contents", ""))
            title = (it.get("title") or "").strip()
            if not text and not title:
                continue
            # API placeholder 제거
            if text in ("내용 없음", "내용없음", "내용 없음.", "내용없음."):
                text = ""
            if not text and not title:
                continue
            sections.append(Section(
                label=(it.get("label") or "").strip().rstrip("."),
                level=int(it.get("level") or 0),
                title=title,
                text=text,
            ))
        return sections


def extract_citations(text: str, self_meta: CodeMeta, known_codes: set[str]) -> set[str]:
    """본문에서 인용 노드 id 집합을 추출합니다.

    2-pass 방식:
      Pass 1 — LHCS 8자리 패턴(_LHCS8_RE): known_codes에 존재하면 채택하고
               해당 매치 시작 위치를 기록한다.
      Pass 2 — 6자리 패턴(_CITATION_RE): LHCS 타입이고 Pass 1에서 이미 처리된
               위치이면 스킵 (8자리가 우선). known_codes에 없는 8자리 시도는
               자동으로 6자리 폴백된다.

    Args:
        text: 검사할 텍스트(조문 본문/제목).
        self_meta: 현재 문서(자기 인용 제외용).
        known_codes: 존재하는 코드 집합 {"KCS142010", ...} (codeType+code).

    Returns:
        인용 노드 id 집합. 절 번호가 있으면 'KCS:142010:3.2', 없으면 'KCS:142010'.
    """
    found: set[str] = set()
    lhcs8_positions: set[int] = set()

    # Pass 1: LHCS 8자리
    for m in _LHCS8_RE.finditer(text):
        code = _norm_code(m.group("code"))
        doc_key = f"LHCS{code}"
        if doc_key not in known_codes or doc_key == self_meta.doc_key:
            continue
        lhcs8_positions.add(m.start())
        label = m.group("label")
        if label:
            found.add(f"LHCS:{code}:{label}")
        else:
            found.add(f"LHCS:{code}")

    # Pass 2: 6자리 (LHCS는 Pass 1 위치 제외)
    for m in _CITATION_RE.finditer(text):
        if m.group("type") == "LHCS" and m.start() in lhcs8_positions:
            continue
        ctype = m.group("type")
        code = _norm_code(m.group("code"))
        doc_key = f"{ctype}{code}"
        if doc_key not in known_codes or doc_key == self_meta.doc_key:
            continue
        label = m.group("label")
        if label:
            found.add(f"{ctype}:{code}:{label}")
        else:
            found.add(f"{ctype}:{code}")

    return found


def sections_to_markdown(meta: CodeMeta, sections: list[Section]) -> str:
    """디버그/감사용 전체 문서 마크다운을 조립합니다."""
    lines = [f"# {meta.code_type} {meta.code} {meta.name} ({meta.version})", ""]
    for s in sections:
        heading = "#" * min(max(s.level, 1) + 1, 6)
        lines.append(f"{heading} {s.title}".rstrip())
        if s.text:
            lines.append(s.text)
        lines.append("")
    return "\n".join(lines)
