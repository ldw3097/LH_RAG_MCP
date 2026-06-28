import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.config import settings
from src.context import law_oc_var
from src.sources.law_api import LawApiSource
from src.sources.lh_vector import LHVectorSource
from src.sources.kcsc_vector import KCSCVectorSource
from src.sources.prec_api import PrecedentSource
from src.sources.pps_vector import PpsVectorSource
from src.sources.csi_stats import CsiStatsSource

SOURCE_LABELS = {
    "law_api": "국가법령정보센터",
    "lh_vector_db": "LH 규정",
    "kcsc_vector_db": "건설기준(KDS/KCS/LHCS)",
    "prec": "법원 판례",
    "pps_vector_db": "조달청 해석사례",
    "csi_stats": "건설안전 사고통계",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="LH RAG MCP",
    instructions=(
        "LH 임직원 업무 지원 서버입니다. "
        "법령·행정규칙(search_law), LH 사내 규정(search_lh_regulations), "
        "건설기준(search_construction_standards), 판례(search_precedents), "
        "조달청 유권해석(search_procurement_interpretations), "
        "건설안전 사고통계 진단(assess_construction_risk) 도구를 제공합니다. "
        "search_law로 목차를 확인한 뒤 특정 조문 전문이 필요한 경우 "
        "get_law_article(법령) 또는 get_admrul_article(행정규칙)로 후속 조회하세요. "
        "생략 가능하다고 명시되지 않은 인자는 필수값입니다. "
        "keywords 인자는 핵심 키워드를 공백으로 구분해 전달하세요. "
        "답변 생성시 반드시 출처를 명시하세요."
    ),
)

_sources = {
    "law_api": LawApiSource(),
    "lh_vector_db": LHVectorSource(),
    "kcsc_vector_db": KCSCVectorSource(),
    "prec": PrecedentSource(),
    "pps_vector_db": PpsVectorSource(),
    "csi_stats": CsiStatsSource(),
}


_KST = timezone(timedelta(hours=9))


def _get_client_ip(request: Request) -> str:
    """X-Forwarded-For에서 신뢰 홉 수(trust_proxy)에 따라 클라이언트 IP를 추출합니다."""
    n = settings.trust_proxy
    xff = request.headers.get("X-Forwarded-For", "")
    if n > 0 and xff:
        parts = [p.strip() for p in xff.split(",")]
        idx = max(0, len(parts) - n)
        return parts[idx]
    return (request.client.host if request.client else None) or "unknown"


def _truncate_response(text: str) -> str:
    limit = settings.max_response_chars
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n...(응답이 {len(text) - limit:,}자 초과하여 잘렸습니다)"


def _result_header(label: str, query: str, keywords: str) -> list[str]:
    return [f"검색어: {query}", f"키워드: {keywords}", f"검색 소스: {label}", ""]


async def _search_single(source_id: str, query: str, keywords: str) -> str:
    """단일 소스를 검색해 포맷된 결과 문자열을 반환합니다."""
    logger.info("검색 요청 [%s]: query=%s | keywords=%s", source_id, query, keywords)
    try:
        results = await _sources[source_id].search(query, keywords)
    except Exception as e:
        logger.error("소스 %s 검색 오류: %s", source_id, e)
        return "검색 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."

    if not results:
        return "관련 정보를 찾지 못했습니다. 다른 키워드로 다시 질문해 주세요."

    label = SOURCE_LABELS.get(source_id, source_id)
    logger.info("검색 완료 [%s]: %d개 결과", source_id, len(results))
    lines = _result_header(label, query, keywords)
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] [{label}] {r.to_text()}")
    return _truncate_response("\n".join(lines))


@mcp.tool()
async def search_law(query: str, keywords: str) -> str:
    """
    국가법령정보센터에서 대한민국 법령(법률·시행령·시행규칙) 조문과 행정규칙(고시·훈령·예규)을 검색합니다.

    Args:
        query: 자연어로 요약한 질의.
        keywords: 핵심 키워드 (예: "주택임대차보호법 보증금 우선변제").
    """
    logger.info("검색 요청 [law_api]: query=%s | keywords=%s", query, keywords)
    try:
        results = await _sources["law_api"].search(query, keywords)
    except Exception as e:
        logger.error("law_api 검색 오류: %s", e)
        return "검색 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."

    if not results:
        return "관련 정보를 찾지 못했습니다. 다른 키워드로 다시 질문해 주세요."

    # block별로 그룹핑
    blocks: dict[str, list] = {"조문": [], "법령": [], "행정규칙": []}
    for r in results:
        block = r.metadata.get("block", "조문")
        if block in blocks:
            blocks[block].append(r)

    label = SOURCE_LABELS["law_api"]
    lines = _result_header(label, query, keywords)
    total = 0
    block_headers = {
        "조문": "■ AI 의미검색 (조문)",
        "법령": "■ 키워드 검색 (법령)",
        "행정규칙": "■ 키워드 검색 (행정규칙)",
    }
    for block_key, header in block_headers.items():
        items = blocks[block_key]
        if not items:
            continue
        lines.append(header)
        for i, r in enumerate(items, 1):
            lines.append(f"[{i}] {r.to_text()}")
            total += 1
        lines.append("")

    logger.info("검색 완료 [law_api]: %d개 결과", total)
    return _truncate_response("\n".join(lines))


@mcp.tool()
async def search_lh_regulations(query: str, keywords: str) -> str:
    """
    LH(한국토지주택공사) 사내 규정을 검색합니다.

    인사·보수·직제·감사·보안 등 LH 임직원과 관련된 내부 규정·시행세칙을 검색합니다. 
    국가 법령이나 대외 정책이 아닌 LH 내부 업무 절차·복무·조직 운영에 관한 질문에 사용하세요.

    Args:
        query: 자연어로 요약한 질의.
        keywords: 핵심 키워드 (예: "연차 휴가 복무").
    """
    return await _search_single("lh_vector_db", query, keywords)


_CATEGORY_MAP: dict[str, set[str] | None] = {
    "design":       {"KDS"},
    "construction": {"KCS", "LHCS"},
    "all":          None,
}


@mcp.tool()
async def search_construction_standards(query: str, keywords: str, category: str = "all") -> str:
    """
    건설기준(KDS·KCS·LHCS)을 검색합니다.

    구조·지반·토목·건축·시공 방법, 재료·품질 기준, 설계 하중·안전율 등 기술적 건설기준에 관한 질문에 사용하세요. 

    Args:
        query: 자연어로 요약한 질의.
        keywords: 핵심 키워드 (예: "옹벽 토압 안정성 설계").
        category: 검색 범위.
            "design"       — KDS(설계기준)만. 설계 계산·하중·안전율 등 설계 단계 질문.
            "construction" — KCS(표준시방서)·LHCS(LH 전문시방서)만. 공법·재료·시공 절차 질문.
            "all"          — 전체 검색 (기본값).
    """
    code_types = _CATEGORY_MAP.get(category)
    logger.info("검색 요청 [kcsc_vector_db]: query=%s | keywords=%s | category=%s", query, keywords, category)
    try:
        results = await _sources["kcsc_vector_db"].search(query, keywords, code_types=code_types)
    except Exception as e:
        logger.error("소스 kcsc_vector_db 검색 오류: %s", e)
        return "검색 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    if not results:
        return "관련 정보를 찾지 못했습니다. 다른 키워드로 다시 질문해 주세요."
    label = SOURCE_LABELS["kcsc_vector_db"]
    logger.info("검색 완료 [kcsc_vector_db]: %d개 결과", len(results))
    lines = _result_header(label, query, keywords)
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] [{label}] {r.to_text()}")
    return _truncate_response("\n".join(lines))


@mcp.tool()
async def search_precedents(query: str, law_name: str = "", keywords: str = "") -> str:
    """
    법원 판례를 검색합니다.

    Args:
        query: 자연어 문장으로 요약한 질의.
        law_name: 관련 법령 정식명칭. 생략 가능.
        keywords: 쟁점과 직결된 핵심 용어 (예: "직접지급합의 묵시적합의해지").
    """
    if not law_name and not keywords:
        return "law_name 또는 keywords 중 하나 이상을 전달해야 합니다."
    logger.info("검색 요청 [prec]: query=%s | law_name=%s | keywords=%s", query, law_name, keywords)
    try:
        results = await _sources["prec"].search(query, law_name=law_name, keywords=keywords)
    except Exception as e:
        logger.error("소스 prec 검색 오류: %s", e)
        return "검색 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    if not results:
        hint = law_name or keywords
        return f"'{hint}' 관련 판례를 찾지 못했습니다. 다른 키워드로 시도해주세요."
    label = SOURCE_LABELS["prec"]
    logger.info("검색 완료 [prec]: %d개 결과", len(results))
    lines = [f"검색어: {query}", f"법령: {law_name}", f"키워드: {keywords}", f"검색 소스: {label}", ""]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] [{label}] {r.to_text()}")
    return _truncate_response("\n".join(lines))


@mcp.tool()
async def search_procurement_interpretations(query: str, keywords: str) -> str:
    """
    조달청 계약법규 해석사례(유권해석)를 검색합니다.

    입찰·낙찰자 선정, 계약 체결·관리, 하자담보 등 국가계약법규에 관한 조달청 유권해석이 필요할 때 사용하세요.
    2022년 이후 사례만 수록됩니다.

    Args:
        query: 자연어로 요약한 질의.
        keywords: 핵심 키워드 (예: "물가변동 계약금액 조정").
    """
    return await _search_single("pps_vector_db", query, keywords)


_CSI_LEVEL_LABELS = {
    "L1": "공종소분류+작업프로세스+시설물소분류",
    "L2": "공종소분류+작업프로세스",
    "L3": "작업프로세스",
    "baseline": "전체(작업프로세스 입력 자료)",
}

_CSI_DELTA_MARK = 3.0   # 기준선 대비 ±이 %p 이상이면 ↑/↓ 강조


def _csi_tool_description() -> str:
    """csi_vocab.json의 별칭 목록을 읽어 도구 설명을 동적 구성한다 (없으면 일반 설명)."""
    base = (
        "예정 공사의 잠재 사고유형을 과거 건설안전 사고통계로 진단합니다. "
        "국토안전관리원 건설안전 사고사례(2019~2025, 약 3.7만건)에서 입력한 공사 조건과 "
        "같은 과거 사고들을 모아 인적사고종류(넘어짐·떨어짐·물체에 맞음·끼임 등)의 "
        "분포와, 기준선 대비 각 사고유형이 얼마나 더 발생하는지 통계를 제공합니다. \n"
        "표본이 부족하면 변수를 단계적으로 떨어뜨려(공종+작업+시설물 → 공종+작업 → 작업) "
        "집계하며, 어느 수준으로 집계했는지 표기합니다.\n\n"
        "각 인자에는 아래 목록의 값 중 하나를 넣으세요. "
    )
    try:
        import json
        from pathlib import Path
        vpath = Path(settings.csi_data_path) / "csi_vocab.json"
        vocab = json.loads(vpath.read_text(encoding="utf-8"))
        field_titles = {
            "work_process": "work_process(작업프로세스, 필수)",
            "work_subtype": "work_subtype(공종 소분류, 선택)",
            "facility_subtype": "facility_subtype(시설물 소분류, 선택)",
        }
        for field, title in field_titles.items():
            values = sorted(vocab.get(field, {}).values())
            if values:
                base += f"\n[{title}] {', '.join(values)}\n"
    except Exception as e:
        logger.warning("csi 어휘 로드 실패 — 도구 설명에 별칭 목록 미포함: %s", e)
    return base


def _format_csi(result: dict) -> str:
    """assess() 결과 dict를 한국어 텍스트 블록으로 포맷."""
    if not result.get("loaded"):
        return ("건설안전 사고통계 데이터가 없습니다. "
                "scripts/build_csi_index.py를 실행하세요.")

    err = result.get("error")
    if err:
        return (
            f"입력한 {err['field']} 값 '{err['value']}'을(를) 인식하지 못했습니다.\n"
            f"다음 값 중 하나로 다시 호출하세요:\n"
            f"{', '.join(err['valid_aliases'])}"
        )

    inp = result["input"]
    inp_str = " / ".join(
        f"{k}={v}" for k, v in (
            ("공종", inp.get("공종소분류")),
            ("작업프로세스", inp.get("작업프로세스")),
            ("시설물", inp.get("시설물소분류")),
        ) if v
    ) or "(입력 없음)"

    lines = ["[건설안전 사고유형 진단]", f"입력: {inp_str}"]
    level_label = _CSI_LEVEL_LABELS.get(result["level"], result["level"])
    sample_note = " · 표본 부족(참고용)" if result.get("low_sample") else ""
    if result["level"] == "baseline":
        lines.append(f"집계수준: 전체 사고 평균 (작업프로세스 미입력 — 조합 통계 불가){sample_note}")
    else:
        lines.append(f"집계수준: {result['level']} ({level_label}) · 표본 n={result['n']}{sample_note}")
    lines.append(f"전체 사고 평균: 작업프로세스가 기재된 사고 {result['baseline_total']:,}건 기준")
    lines.append("")

    if not result["distribution"]:
        lines.append("해당 조건의 사고 기록을 찾지 못했습니다.")
        return "\n".join(lines)

    lines.append("사고유형(인적사고종류 대분류) 분포 — 전체 사고 평균 대비 증감(%p):")
    for d in result["distribution"]:
        base = d["baseline_pct"]
        delta = d["pct"] - base
        arrow = " ↑" if delta >= _CSI_DELTA_MARK else (" ↓" if delta <= -_CSI_DELTA_MARK else "")
        comp = f"전체 평균 {base:.1f}% → {delta:+.1f}%p{arrow}"
        lines.append(
            f"  {d['type']}  {d['pct']:.1f}% ({d['count']}건)   {comp}"
        )
    lines.append("")
    lines.append("출처: 국토안전관리원 건설안전사고사례(2019~2025)")
    return "\n".join(lines)


@mcp.tool(description=_csi_tool_description())
async def assess_construction_risk(
    work_process: str,
    work_subtype: str = "",
    facility_subtype: str = "",
) -> str:
    logger.info(
        "진단 요청 [csi]: 공종=%s | 작업=%s | 시설=%s",
        work_subtype, work_process, facility_subtype,
    )
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: _sources["csi_stats"].assess(work_subtype, work_process, facility_subtype),
        )
    except Exception as e:
        logger.error("csi 진단 오류: %s", e)
        return "진단 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    return _truncate_response(_format_csi(result))


@mcp.tool()
async def get_law_article(law_name: str, article: str) -> str:
    """
    법령의 특정 조문 전문을 조회합니다.

    search_law로 목차를 확인한 뒤 원하는 조문을 상세 조회하는 2-step 플로우에 사용하세요.

    Args:
        law_name: 법령명
        article:  조문 식별자 ("제3조", "제7조의2" 형식)
    """
    logger.info("조문 조회 [law]: law_name=%s | article=%s", law_name, article)
    try:
        result = await _sources["law_api"].get_law_article(law_name, article)
    except Exception as e:
        logger.error("조문 조회 오류 [law]: %s", e)
        return "조회 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    return _truncate_response(result)


@mcp.tool()
async def get_admrul_article(admrul_name: str, article: str, ministry: str = "") -> str:
    """
    행정규칙(고시·훈령·예규)의 특정 조문 전문을 조회합니다.

    search_law로 목차를 확인한 뒤 원하는 조문을 상세 조회하는 2-step 플로우에 사용하세요.

    Args:
        admrul_name: 행정규칙명
        article:     조문 식별자 ("제5조", "제7조의2" 형식)
        ministry:    소관부처명. 생략하면 전 부처 검색.
    """
    logger.info("조문 조회 [admrul]: admrul_name=%s | article=%s | ministry=%s", admrul_name, article, ministry)
    try:
        result = await _sources["law_api"].get_admrul_article(admrul_name, article, ministry)
    except Exception as e:
        logger.error("조문 조회 오류 [admrul]: %s", e)
        return "조회 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
    return _truncate_response(result)


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    """Content-Length 기준으로 요청 바디 크기를 제한합니다."""

    async def dispatch(self, request: Request, call_next):
        max_bytes = settings.max_request_body_kb * 1024
        cl = request.headers.get("content-length")
        if cl and int(cl) > max_bytes:
            return JSONResponse({"error": "Request body too large"}, status_code=413)
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """IP당 5분 버킷·일별 버킷 두 단계 rate limit을 적용합니다."""

    _EXEMPT_PATHS = {"/", "/health"}

    def __init__(self, app):
        super().__init__(app)
        self._5min: dict[str, dict] = {}
        self._daily: dict[str, dict] = {}
        self._last_cleanup: float = 0.0

    def _next_midnight_kst(self) -> float:
        now_kst = datetime.now(_KST)
        midnight = (now_kst + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.timestamp()

    def _cleanup(self, now: float) -> None:
        if now - self._last_cleanup < 600:
            return
        self._last_cleanup = now
        for buckets in (self._5min, self._daily):
            expired = [ip for ip, b in buckets.items() if now >= b["reset_at"]]
            for ip in expired:
                del buckets[ip]

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self._EXEMPT_PATHS:
            return await call_next(request)

        if (
            settings.rate_limit_bypass_key
            and request.headers.get("X-Rate-Limit-Bypass") == settings.rate_limit_bypass_key
        ):
            return await call_next(request)

        now = time.time()
        self._cleanup(now)
        ip = _get_client_ip(request)

        # 5분 버킷
        b5 = self._5min.get(ip)
        if not b5 or now >= b5["reset_at"]:
            b5 = {"count": 0, "reset_at": now + 300}
            self._5min[ip] = b5
        b5["count"] += 1
        if b5["count"] > settings.rate_limit_per_5min:
            retry_after = int(b5["reset_at"] - now)
            return JSONResponse(
                {"error": "Too many requests. Try again later.", "retry_after": retry_after},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        # 일별 버킷
        bd = self._daily.get(ip)
        if not bd or now >= bd["reset_at"]:
            bd = {"count": 0, "reset_at": self._next_midnight_kst()}
            self._daily[ip] = bd
        bd["count"] += 1
        if bd["count"] > settings.rate_limit_daily:
            retry_after = int(bd["reset_at"] - now)
            return JSONResponse(
                {"error": "Daily request limit exceeded. Try again tomorrow.", "retry_after": retry_after},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Bearer 토큰으로 MCP 서버 접근을 제한합니다."""

    async def dispatch(self, request: Request, call_next):
        if not settings.mcp_api_key:
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {settings.mcp_api_key}":
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


class LawOcMiddleware(BaseHTTPMiddleware):
    """URL 파라미터 law_oc를 요청 컨텍스트에 주입합니다.

    사용자는 MCP 서버 URL에 자신의 법제처 API 키를 포함합니다:
        https://your-server.com/mcp?law_oc=YOUR_KEY
    """

    async def dispatch(self, request: Request, call_next):
        law_oc = request.query_params.get("law_oc", settings.law_oc_default)
        token = law_oc_var.set(law_oc)
        try:
            return await call_next(request)
        finally:
            law_oc_var.reset(token)


def main():
    if not settings.law_oc_default:
        logger.warning(
            "LAW_OC_DEFAULT 미설정 — 법령 API는 요청 URL의 ?law_oc= 파라미터가 필수입니다."
        )

    # 첫 요청 전 BM25 인덱스·kiwipiepy 사전 로딩 (ML 모델 없음 — 수초 이내)
    logger.info("BM25 인덱스 사전 로딩 시작...")
    _sources["lh_vector_db"]._ensure_loaded()
    _sources["kcsc_vector_db"]._ensure_loaded()
    _sources["pps_vector_db"]._ensure_loaded()
    _sources["csi_stats"]._ensure_loaded()
    logger.info("BM25 인덱스 사전 로딩 완료")

    app = mcp.http_app(transport="streamable-http")
    # 미들웨어는 역순으로 실행되므로 안쪽부터 등록
    # 실행 순서: MaxBodySize → RateLimit → ApiKey → LawOc → 툴
    app.add_middleware(LawOcMiddleware)
    app.add_middleware(ApiKeyMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(MaxBodySizeMiddleware)

    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
