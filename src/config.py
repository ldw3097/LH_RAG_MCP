from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM 라우터 (OpenAI 호환 엔드포인트 어디든 사용 가능)
    router_api_key: str = ""
    router_base_url: str = "https://api.deepinfra.com/v1/openai"
    router_model: str = "Qwen/Qwen2.5-7B-Instruct"

    # 법제처 API 키: 서버 기본값 (없으면 요청별 URL 파라미터 law_oc 필수)
    law_oc_default: str = ""

    # ChromaDB
    chroma_path: str = "./data/chroma"
    chroma_collection: str = "lh_regulations"

    # 변환된 마크다운 캐시 경로
    markdown_path: str = "./data/markdown"

    # 임베딩 모델
    embedding_model: str = "jhgan/ko-sroberta-multitask"

    # LH 크롤링
    lh_regulations_url: str = "https://www.lh.or.kr/board.es?mid=a10108020000&bid=0055"
    lh_rss_url: str = ""

    # PDF 변환 (marker)
    torch_device: str = "mps"   # mps | cuda | cpu

    # MCP 서버 인증
    mcp_api_key: str = ""
    host: str = "0.0.0.0"
    port: int = 8000


settings = Settings()

# 소스 레지스트리: 새 소스 추가 시 여기에만 등록
SOURCE_REGISTRY: dict[str, str] = {
    "law_api": (
        "국가법령정보센터 - 법률, 시행령, 시행규칙, 판례, 행정규칙, 헌재결정례 등 "
        "대한민국 국가 법령 전반. 법적 근거, 법령 조문, 판례 조회에 활용."
    ),
    "lh_vector_db": (
        "LH 한국토지주택공사 내부 규정집 - 업무지침, 시행세칙, 내규, 사규. "
        "LH 내부 절차, 기준, 서식, 조직 운영에 관한 질문에 활용."
    ),
}
