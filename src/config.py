from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 법제처 API 키: 서버 기본값 (없으면 요청별 URL 파라미터 law_oc 필수)
    law_oc_default: str = ""

    # BM25 인덱스
    bm25_path: str = "./data/bm25"
    bm25_collection: str = "lh_regulations"

    # 변환된 마크다운 캐시 경로
    markdown_path: str = "./data/markdown"

    # LH 크롤링
    lh_regulations_url: str = "https://www.lh.or.kr/board.es?mid=a10108020000&bid=0055"
    lh_rss_url: str = ""

    # PDF 변환 (docling)
    torch_device: str = "mps"   # mps | cuda | cpu

    # Dense 임베딩 (DeepInfra API)
    deepinfra_api_key: str = ""
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"

    # MCP 서버 인증
    mcp_api_key: str = ""
    host: str = "0.0.0.0"
    port: int = 8000


settings = Settings()
