"""
LH 규정 RSS 기반 문서 수집기.

RSS 피드에서 문서 목록을 가져오고, 각 게시글 페이지에서
첨부파일을 찾아 텍스트를 추출합니다.
"""

import asyncio
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://www.lh.or.kr"

# RSS pubDate 형식: "2026-04-28 14:02"
_PUB_DATE_FMT = "%Y-%m-%d %H:%M"


@dataclass
class RssItem:
    title: str
    link: str
    pub_date: datetime
    description: str = ""
    author: str = ""


def parse_rss_feed(content: str) -> list[RssItem]:
    """RSS XML 문자열을 파싱하여 RssItem 목록을 반환합니다."""
    import feedparser

    feed = feedparser.parse(content)
    items = []
    for entry in feed.entries:
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        if not title or not link:
            continue

        pub_date_str = entry.get("published", entry.get("updated", "")).strip()
        pub_date = _parse_pub_date(pub_date_str, entry)

        items.append(RssItem(
            title=title,
            link=link,
            pub_date=pub_date,
            description=entry.get("summary", ""),
            author=entry.get("author", ""),
        ))
    return items


def _parse_pub_date(pub_date_str: str, entry) -> datetime:
    """다양한 날짜 형식을 처리합니다. 파싱 실패 시 datetime.min 반환."""
    # LH RSS 형식: "2026-04-28 14:02"
    try:
        return datetime.strptime(pub_date_str, _PUB_DATE_FMT)
    except ValueError:
        pass
    # feedparser가 이미 파싱한 struct_time 형식
    try:
        from calendar import timegm
        return datetime.utcfromtimestamp(timegm(entry.published_parsed))
    except Exception:
        pass
    # 파싱 불가 → 항상 업데이트 대상으로 처리
    return datetime.min


class LHDocumentFetcher:
    """LH 규정 게시글 페이지에서 첨부파일을 찾아 텍스트를 추출합니다."""

    def __init__(self):
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0 (compatible; LH-RAG-Bot/1.0)"},
            follow_redirects=True,
            verify=False,   # LH 사이트 SSL 인증서 체인 문제 우회 (공공기관 사이트 흔함)
        )

    async def fetch_text(self, page_url: str) -> str:
        """게시글 URL → 첨부파일 탐색 → 텍스트 추출."""
        try:
            resp = await self._client.get(page_url)
            resp.raise_for_status()
        except Exception as e:
            logger.warning("페이지 로드 실패 %s: %s", page_url, e)
            return ""

        soup = BeautifulSoup(resp.text, "html.parser")
        file_url = self._find_attachment_url(soup)
        if not file_url:
            logger.warning("첨부파일 없음: %s", page_url)
            return ""

        return await self._download_and_extract(file_url)

    def _find_attachment_url(self, soup: BeautifulSoup) -> str | None:
        """페이지에서 첨부파일 다운로드 URL을 탐색합니다.

        우선순위:
        1. 실제 다운로드 패턴 (boardDownload, fileDown 등) — 미리보기 제외
        2. href에 파일 확장자가 직접 포함된 링크 중 미리보기(Preview) 제외
        """
        # LH 게시판: "다운로드" 버튼은 boardDownload.es, 파일 다운로드는 fileDown 등
        # "바로보기(attachApiPreview)" 는 HTML 뷰어이므로 제외
        _PREVIEW = re.compile(r"preview", re.IGNORECASE)
        _DOWNLOAD_PATTERNS = [
            "boardDownload", "fileDown", "boardFile",
            "atchFile", "FileDown", "fileDownload",
        ]
        for pattern in _DOWNLOAD_PATTERNS:
            link = soup.find("a", href=re.compile(pattern, re.IGNORECASE))
            if link and not _PREVIEW.search(link["href"]):
                return _abs_url(link["href"])

        # 직접 확장자 링크 (미리보기 URL 제외)
        for ext in (".hwpx", ".hwp", ".pdf"):
            for link in soup.find_all("a", href=re.compile(rf".*{re.escape(ext)}", re.IGNORECASE)):
                if not _PREVIEW.search(link["href"]):
                    return _abs_url(link["href"])

        return None

    async def _download_and_extract(self, file_url: str) -> str:
        try:
            resp = await self._client.get(file_url)
            resp.raise_for_status()
        except Exception as e:
            logger.warning("파일 다운로드 실패 %s: %s", file_url, e)
            return ""

        content_type = resp.headers.get("content-type", "").lower()
        content_disposition = resp.headers.get("content-disposition", "")
        suffix = _guess_suffix(file_url, content_type, content_disposition)
        logger.debug("다운로드 파일 형식: %s (url=%s, ct=%s, cd=%s)",
                     suffix, file_url, content_type, content_disposition)

        if suffix == ".bin":
            logger.warning("파일 형식 판별 불가, 스킵: %s", file_url)
            return ""

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(resp.content)
            tmp_path = Path(f.name)

        try:
            if suffix == ".pdf":
                return await asyncio.to_thread(_extract_pdf, tmp_path)
            if suffix in (".hwp", ".hwpx"):
                return await asyncio.to_thread(_extract_hwp, tmp_path)
            return ""
        finally:
            tmp_path.unlink(missing_ok=True)

    async def aclose(self):
        await self._client.aclose()


# ── 유틸리티 ──────────────────────────────────────────────────────────────────

def _abs_url(href: str) -> str:
    return href if href.startswith("http") else BASE_URL + href


def _guess_suffix(url: str, content_type: str, content_disposition: str = "") -> str:
    """URL, Content-Disposition, Content-Type 순서로 파일 확장자를 추론합니다.

    LH 다운로드 URL은 fileDown.es?... 형태라 URL에 확장자가 없는 경우가 많습니다.
    Content-Disposition 헤더의 filename에서 확장자를 추출하는 것이 가장 신뢰도가 높습니다.
    """
    _SUPPORTED = (".pdf", ".hwpx", ".hwp")

    # 1순위: URL 경로에 확장자가 직접 포함된 경우
    clean_url = url.lower().split("?")[0]
    for ext in _SUPPORTED:
        if clean_url.endswith(ext):
            return ext

    # 2순위: 쿼리 파라미터의 file_name/fileName 값 (LH: attachApiPreview.es?file_name=xxx.pdf)
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(url).query)
    for param in ("file_name", "fileName", "filename", "FILENAME", "orgFileName"):
        values = qs.get(param, [])
        if values:
            fname = values[0].lower()
            for ext in _SUPPORTED:
                if fname.endswith(ext):
                    return ext

    # 2순위: Content-Disposition 헤더의 filename
    #   형식 예시:
    #     attachment; filename="취업규칙.pdf"
    #     attachment; filename*=UTF-8''%EC%B7%A8%EC%97%85%EA%B7%9C%EC%B9%99.pdf
    if content_disposition:
        # filename*= (RFC 5987, percent-encoded)
        m = re.search(r"filename\*=[^']*''(.+)", content_disposition, re.IGNORECASE)
        if m:
            from urllib.parse import unquote
            fname = unquote(m.group(1).strip())
            for ext in _SUPPORTED:
                if fname.lower().endswith(ext):
                    return ext

        # filename= (일반)
        m = re.search(r'filename=["\']?([^"\';\s]+)', content_disposition, re.IGNORECASE)
        if m:
            fname = m.group(1).strip().strip("\"'")
            for ext in _SUPPORTED:
                if fname.lower().endswith(ext):
                    return ext

    # 3순위: Content-Type
    if "pdf" in content_type:
        return ".pdf"
    if "hwp" in content_type or "hangul" in content_type:
        return ".hwp"

    return ".bin"


def _extract_pdf(path: Path) -> str:
    """docling으로 PDF를 마크다운으로 변환합니다.

    pdfium 백엔드로 한국어 공백을 올바르게 복원하고, 표 구조를 마크다운 테이블로 출력합니다.
    스캔 페이지는 자동 OCR을 적용합니다.
    asyncio.to_thread()를 통해 이벤트 루프 블로킹 없이 호출하세요.
    """
    from crawler.pdf_converter import convert_pdf
    return convert_pdf(path)


def _extract_hwp(path: Path) -> str:
    """LibreOffice로 HWP/HWPX → TXT 변환. 미설치 시 빈 문자열 반환."""
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            subprocess.run(
                ["libreoffice", "--headless", "--convert-to", "txt:Text",
                 "--outdir", tmp_dir, str(path)],
                capture_output=True, timeout=30, check=True,
            )
            txt_file = Path(tmp_dir) / (path.stem + ".txt")
            if txt_file.exists():
                return txt_file.read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        logger.warning("LibreOffice 미설치 — HWP 파일 스킵: %s", path.name)
    except Exception as e:
        logger.warning("HWP 변환 실패 %s: %s", path.name, e)
    return ""
