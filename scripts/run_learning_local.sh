#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env.local ]]; then
  set -a
  source .env.local
  set +a
fi
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8765}"
export LLM_PROVIDER="${LLM_PROVIDER:-ollama}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3.5:9b}"
export VOICE_CHAT_MODEL="${VOICE_CHAT_MODEL:-qwen2.5:3b}"
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-30m}"
export OLLAMA_NATIVE_BASE_URL="${OLLAMA_NATIVE_BASE_URL:-http://127.0.0.1:11434}"
export VOICE_ENABLED="${VOICE_ENABLED:-1}"
export ASR_BACKEND="${ASR_BACKEND:-parakeet}"
export TTS_BACKEND="${TTS_BACKEND:-kokoro}"
export AGENT_BACKEND="${AGENT_BACKEND:-english_chat}"
export TTS_CPU_THREADS="${TTS_CPU_THREADS:-4}"
export LOCAL_ASR_LANGUAGE="${LOCAL_ASR_LANGUAGE:-en}"
export LOCAL_VAD_END_MS="${LOCAL_VAD_END_MS:-500}"
exec "${PYTHON_BIN:-.venv/bin/python}" -m backend.app
