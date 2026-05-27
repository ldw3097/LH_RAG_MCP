#!/bin/bash
# session-start.sh — LH RAG MCP 클라우드 개발 환경 초기화
# 원격 환경(Claude Code on the web)에서만 실행됩니다.

set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

echo "🚀 LH RAG MCP 개발 환경 초기화 중..."

# uv 설치 (없는 경우)
if ! command -v uv &> /dev/null; then
  echo "📦 uv 설치 중..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  echo "export PATH=\"$HOME/.local/bin:$PATH\"" >> "$CLAUDE_ENV_FILE"
fi

echo "✅ uv 버전: $(uv --version)"

# 의존성 설치 (개발 의존성 포함)
cd "$CLAUDE_PROJECT_DIR"
echo "📦 Python 의존성 설치 중..."
uv sync --extra dev

# PYTHONPATH 설정
echo "export PYTHONPATH=\"$CLAUDE_PROJECT_DIR\"" >> "$CLAUDE_ENV_FILE"

echo "✅ 환경 초기화 완료!"
