#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
export ASR_BACKEND="${ASR_BACKEND:-stub}"
export AGENT_BACKEND="${AGENT_BACKEND:-demo}"
export TTS_BACKEND="${TTS_BACKEND:-demo}"

python3.11 scripts/check_environment.py
python3.11 scripts/check_port.py
echo "演示台启动后请访问 http://localhost:8765"
exec python3.11 -m backend.app
