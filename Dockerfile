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
    "rank-bm25>=0.2.2" \
    "numpy>=1.24.0" \
    "beautifulsoup4>=4.12.0"

COPY src/ src/
COPY crawler/__init__.py crawler/__init__.py
COPY crawler/bm25_index.py crawler/bm25_index.py
COPY crawler/dense_index.py crawler/dense_index.py
COPY crawler/indexer.py crawler/indexer.py
COPY crawler/kcsc_api.py crawler/kcsc_api.py
COPY crawler/kcsc_indexer.py crawler/kcsc_indexer.py
COPY crawler/lh_crawler.py crawler/lh_crawler.py

RUN mkdir -p /data/lh_regulation /data/kcsc

ENV MARKDOWN_PATH=/data/lh_regulation/markdown
ENV BM25_PATH=/data/lh_regulation
ENV KCSC_DATA_PATH=/data/kcsc
ENV HOST=0.0.0.0
ENV PORT=8000

EXPOSE 8000

CMD ["python", "-m", "src.server"]
