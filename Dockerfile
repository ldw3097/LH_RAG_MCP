FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 서버에 필요한 의존성만 설치 (docling 제외)
RUN uv pip install --system --no-cache \
    "fastmcp>=2.0.0" \
    "openai>=1.0.0" \
    "httpx>=0.27.0" \
    "chromadb>=0.5.0" \
    "sentence-transformers>=3.0.0" \
    "pydantic-settings>=2.0.0" \
    "tenacity>=8.0.0" \
    "python-dotenv>=1.0.0" \
    "kiwipiepy>=0.19.0" \
    "rank-bm25>=0.2.2" \
    && uv pip install --system --no-cache \
    torch --index-url https://download.pytorch.org/whl/cpu

COPY src/ src/
COPY crawler/bm25_index.py crawler/bm25_index.py

# 임베딩 모델 미리 다운로드 (cold start 방지)
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('jhgan/ko-sroberta-multitask', trust_remote_code=True)"

RUN mkdir -p /data/chroma /data/bm25 /data/markdown

ENV CHROMA_PATH=/data/chroma
ENV MARKDOWN_PATH=/data/markdown
ENV TORCH_DEVICE=cpu
ENV HOST=0.0.0.0
ENV PORT=8000

EXPOSE 8000

CMD ["python", "-m", "src.server"]
