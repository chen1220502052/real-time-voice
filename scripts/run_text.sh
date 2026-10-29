#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env.local ]]; then
  set -a
  source .env.local
  set +a
fi
export VOICE_ENABLED=0
export AGENT_BACKEND=english_chat
export PORT="${PORT:-8870}"
echo "文字聊天：http://127.0.0.1:${PORT}/text.html（不加载 ASR、VAD 或 TTS）"
echo "单元写作：http://127.0.0.1:${PORT}/writing.html"
exec "${PYTHON_BIN:-python3.11}" -m backend.app
