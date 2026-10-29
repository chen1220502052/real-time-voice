#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export ASR_BACKEND="${ASR_BACKEND:-funasr}"
export ASR_DEVICE="${ASR_DEVICE:-cpu}"
export AGENT_BACKEND="${AGENT_BACKEND:-ollama}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen2.5:0.5b}"
export TTS_BACKEND="${TTS_BACKEND:-piper}"

python3.11 scripts/check_environment.py
python3.11 scripts/check_port.py
echo "真实模型预热需要一些时间；完成后请访问 http://localhost:8765"
exec python3.11 -m backend.app
