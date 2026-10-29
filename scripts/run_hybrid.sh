#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ -f .env.local ]]; then
  set -a
  source .env.local
  set +a
fi
export ASR_BACKEND="${ASR_BACKEND:-nls}"
export ASR_DEVICE="${ASR_DEVICE:-cpu}"
export AGENT_BACKEND="${AGENT_BACKEND:-english_chat}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:0.5b}"
export TTS_BACKEND="${TTS_BACKEND:-piper}"

python3.11 scripts/check_environment.py
python3.11 scripts/check_port.py
echo "混合模式：本地 FSMN-VAD + 阿里云 NLS + 年级聊天 Bot（可配置本地/远程 LLM）+ 本地 Piper"
exec python3.11 -m backend.app
