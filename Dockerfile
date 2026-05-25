FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN uv pip install --system --no-cache \
    "fastmcp>=2.0.0" \
    "httpx>=0.27.0" \
    "pydantic-settings>=2.0.0" \
    "tenacity>=8.0.0" \
    "python-dotenv>=1.0.0" \
    "kiwipiepy>=0.19.0" \
    "rank-bm25>=0.2.2"

COPY src/ src/
COPY crawler/bm25_index.py crawler/bm25_index.py

RUN mkdir -p /data/bm25 /data/markdown

ENV MARKDOWN_PATH=/data/markdown
ENV HOST=0.0.0.0
ENV PORT=8000

EXPOSE 8000

CMD ["python", "-m", "src.server"]
