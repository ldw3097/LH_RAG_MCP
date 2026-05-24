"""
PDF 텍스트 추출.

전략 (속도 우선):
1. pdftext — 디지털 생성 PDF의 경우 0.3초 이내에 올바른 띄어쓰기로 추출.
   폰트 크기를 분석해 ## / # 헤딩을 마크다운으로 표현합니다.
2. marker — 스캔 PDF 또는 pdftext 품질이 낮은 경우 OCR로 폴백합니다.
   최초 호출 시 surya 모델을 로드하므로 수 분이 소요될 수 있습니다.
"""

import logging
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings

logger = logging.getLogger(__name__)

# pdftext 텍스트 밀도가 이 값 미만(chars/page)이면 scanned으로 판단 → marker 폴백
_MIN_CHARS_PER_PAGE = 80

# marker 싱글턴 (최초 호출 시 초기화)
_marker_converter = None


# ── pdftext 기반 추출 (1차) ────────────────────────────────────────────────────

def _extract_with_pdftext(path: Path) -> str:
    """pdftext로 텍스트를 추출하고 마크다운 형태로 반환합니다.

    - 폰트 크기 중앙값을 기준으로 # / ## 헤딩을 붙입니다.
    - 페이지 사이는 빈 줄로 구분합니다.
    """
    from pdftext.extraction import dictionary_output

    try:
        pages = dictionary_output(str(path), sort=True)
    except Exception as e:
        logger.warning("pdftext 추출 실패 %s: %s", path.name, e)
        return ""

    all_lines: list[tuple[float, str]] = []  # (font_size, text)
    page_breaks: list[int] = []  # all_lines 인덱스

    for page_data in pages:
        page_start = len(all_lines)
        for block in page_data.get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(s["text"] for s in spans).strip()
                if not text:
                    continue
                max_size = max(
                    (s["font"]["size"] for s in spans if s.get("font")),
                    default=0.0,
                )
                all_lines.append((max_size, text))
        if len(all_lines) > page_start:  # 페이지에 내용이 있었으면 구분자 표시
            page_breaks.append(len(all_lines))

    if not all_lines:
        return ""

    # 본문 폰트 크기 추정 (중앙값)
    sizes = [s for s, _ in all_lines if s > 0]
    if sizes:
        body_size = statistics.median(sizes)
    else:
        body_size = 10.0

    output: list[str] = []
    prev_break = 0
    break_set = set(page_breaks)

    for idx, (size, text) in enumerate(all_lines):
        # 헤딩 판별: body 대비 크기 비율로 결정
        if size > body_size * 1.4:
            output.append(f"# {text}")
        elif size > body_size * 1.15:
            output.append(f"## {text}")
        else:
            output.append(text)

        # 페이지 경계 → 빈 줄 삽입
        if (idx + 1) in break_set:
            output.append("")

    return "\n".join(output)


def _text_density(text: str, page_count: int) -> float:
    """페이지당 평균 글자 수를 반환합니다."""
    if page_count <= 0:
        return 0.0
    return len(text.replace("\n", "").replace(" ", "")) / page_count


def _get_page_count(path: Path) -> int:
    """pymupdf로 페이지 수를 빠르게 가져옵니다."""
    try:
        import pymupdf
        doc = pymupdf.open(str(path))
        count = doc.page_count
        doc.close()
        return count
    except Exception:
        return 1


# ── marker 기반 추출 (2차, scanned PDF용) ────────────────────────────────────

def _get_marker_converter():
    """marker PdfConverter 싱글턴을 반환합니다 (최초 호출 시 모델 로드)."""
    global _marker_converter
    if _marker_converter is None:
        logger.info(
            "marker 모델 로딩 중 (device=%s) — 최초 실행 시 수 분 소요됩니다.",
            settings.torch_device,
        )
        from marker.converters.pdf import PdfConverter
        from marker.models import create_model_dict

        models = create_model_dict(device=settings.torch_device)
        _marker_converter = PdfConverter(artifact_dict=models)
        logger.info("marker 모델 로딩 완료")
    return _marker_converter


def _extract_with_marker(path: Path) -> str:
    """marker로 PDF를 마크다운으로 변환합니다 (OCR 포함)."""
    converter = _get_marker_converter()
    try:
        result = converter(str(path))
        if not result.markdown.strip():
            logger.warning("marker 변환 결과 비어있음: %s", path.name)
            return ""
        return result.markdown
    except Exception as e:
        logger.warning("marker PDF 변환 실패 %s: %s", path.name, e)
        return ""


# ── 공개 인터페이스 ───────────────────────────────────────────────────────────

def convert_pdf(path: Path) -> str:
    """PDF 파일을 마크다운 텍스트로 변환합니다 (동기 함수, blocking).

    비동기 컨텍스트에서 호출 시 ``asyncio.to_thread(convert_pdf, path)`` 사용.

    처리 흐름:
    1. pdftext로 빠르게 텍스트 추출 (< 1초)
    2. 페이지당 글자 수가 충분하면 → pdftext 결과 반환
    3. 부족하면(스캔본 의심) → marker OCR 폴백
    """
    page_count = _get_page_count(path)

    # 1차: pdftext
    text = _extract_with_pdftext(path)
    density = _text_density(text, page_count)
    logger.debug(
        "pdftext 추출: %d자 (%.1f chars/page) — %s",
        len(text), density, path.name,
    )

    if density >= _MIN_CHARS_PER_PAGE:
        logger.info(
            "pdftext 사용 (%.0f chars/page, %d자): %s",
            density, len(text), path.name,
        )
        return text

    # 2차: marker (scanned PDF)
    logger.info(
        "pdftext 품질 부족 (%.0f chars/page) → marker OCR 폴백: %s",
        density, path.name,
    )
    return _extract_with_marker(path)
