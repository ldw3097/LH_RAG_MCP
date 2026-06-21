from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 법제처 API 키: 서버 기본값 (없으면 요청별 URL 파라미터 law_oc 필수)
    law_oc_default: str = ""

    # LH 규정 인덱스 / 마크다운 경로
    bm25_path: str = "./data/lh_regulation"
    bm25_collection: str = "lh_regulations"
    markdown_path: str = "./data/lh_regulation/markdown"

    # LH 크롤링
    lh_regulations_url: str = "https://www.lh.or.kr/board.es?mid=a10108020000&bid=0055"
    lh_rss_url: str = ""

    # PDF 변환 (docling)
    torch_device: str = "mps"   # mps | cuda | cpu

    # Dense 임베딩 (DeepInfra API)
    deepinfra_api_key: str = ""
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    embedding_batch_model: str = "Qwen/Qwen3-Embedding-0.6B-batch"

    # KCSC 건설기준 (국가건설기준센터 Open API)
    kcsc_api_key: str = ""
    kcsc_api_base: str = "https://kcsc.re.kr/OpenApi"
    kcsc_data_path: str = "./data/kcsc"
    kcsc_bm25_collection: str = "kcsc_standards"

    # 조달청 해석사례 (법제처 OPEN API ppsCgmExpc — 인증키는 law_oc_default 재사용)
    pps_data_path: str = "./data/pps"
    pps_bm25_collection: str = "pps_interpretations"

    # 국토안전관리원 건설안전 사고통계 (CSV → 정제 레코드 pkl, 통계 집계용)
    csi_data_path: str = "./data/csi"
    csi_pkl_name: str = "csi_accidents.pkl"

    # MCP 서버 인증
    mcp_api_key: str = ""
    host: str = "0.0.0.0"
    port: int = 8000

    # 과다 사용 방지
    rate_limit_per_5min: int = 30       # 5분 버킷 최대 요청 수
    rate_limit_daily: int = 800         # 일 버킷 최대 요청 수 (자정 KST 기준 초기화)
    max_request_body_kb: int = 100      # 요청 바디 크기 상한 (KB)
    max_response_chars: int = 150_000   # 응답 텍스트 길이 상한 (chars)
    trust_proxy: int = 1                # XFF 신뢰 홉 수
    rate_limit_bypass_key: str = ""     # 설정 시 X-Rate-Limit-Bypass 헤더로 우회 가능


settings = Settings()
