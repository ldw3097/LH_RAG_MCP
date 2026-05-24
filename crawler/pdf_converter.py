"""
docling 기반 PDF 변환.

docling은 pdfium 백엔드로 디지털 PDF 텍스트를 읽고(한국어 공백 올바르게 복원),
필요한 페이지만 자동으로 OCR하며, 표 구조를 마크다운 테이블로 출력합니다.

싱글턴 패턴으로 모델을 프로세스당 1회 로드합니다.
비동기 컨텍스트에서는 asyncio.to_thread(convert_pdf, path)로 호출하세요.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings

logger = logging.getLogger(__name__)

_converter = None


def _get_converter():
    """DocumentConverter 싱글턴을 반환합니다. 최초 호출 시 모델을 로드합니다."""
    global _converter
    if _converter is not None:
        return _converter

    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import (
        AcceleratorDevice,
        AcceleratorOptions,
        PdfPipelineOptions,
    )

    # TORCH_DEVICE 설정을 docling AcceleratorDevice로 매핑
    _device_map = {
        "mps": AcceleratorDevice.MPS,
        "cuda": AcceleratorDevice.CUDA,
        "cpu": AcceleratorDevice.CPU,
    }
    device = _device_map.get(settings.torch_device, AcceleratorDevice.AUTO)

    pipeline_options = PdfPipelineOptions(
        accelerator_options=AcceleratorOptions(device=device),
        do_table_structure=True,   # 표를 마크다운 테이블로 변환
        do_ocr=True,               # 스캔 페이지 자동 OCR (디지털 페이지는 pdfium 텍스트 사용)
        force_backend_text=False,  # 디지털 PDF는 pdfium 텍스트 우선 사용
    )

    logger.info(
        "docling 모델 로딩 중 (device=%s) — 최초 실행 시 수십 초 소요됩니다.",
        device.value,
    )
    _converter = DocumentConverter(
        format_options={"pdf": PdfFormatOption(pipeline_options=pipeline_options)}
    )
    logger.info("docling 모델 로딩 완료")
    return _converter


def convert_pdf(path: Path) -> str:
    """PDF 파일을 마크다운 텍스트로 변환합니다 (동기 함수, blocking).

    비동기 컨텍스트에서 호출 시 ``asyncio.to_thread(convert_pdf, path)`` 사용.

    - 디지털 PDF: pdfium 텍스트 추출 (한국어 공백 자동 복원) + 표 구조 마크다운
    - 스캔 PDF: 자동 OCR 적용
    """
    converter = _get_converter()
    try:
        result = converter.convert(str(path))
        md = result.document.export_to_markdown()
        if not md.strip():
            logger.warning("docling 변환 결과 비어있음: %s", path.name)
            return ""
        logger.info("docling 변환 완료: %d자 — %s", len(md), path.name)
        return md
    except Exception as e:
        logger.warning("docling PDF 변환 실패 %s: %s", path.name, e)
        return ""
