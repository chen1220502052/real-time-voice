#!/usr/bin/env python3
"""Copy required app credentials to the designated ECS via verified SSH.

Secrets are written directly to a root-only remote environment file, never
placed in the bundle, command-line arguments, or printed to the console.
Existing remote configuration is not overwritten.
"""

import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

import paramiko

from deploy_ssh import read_config


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    target = read_config(root / ".env.deploy")
    source = read_config(root / ".env.local")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:8765/health", timeout=5) as response:
        health = json.load(response)
    pid = int(health["pid"])
    process = subprocess.check_output(["ps", "eww", "-p", str(pid), "-o", "command="], text=True)
    effective = {}
    for key in ("OLLAMA_BASE_URL", "OLLAMA_MODEL", "LLM_SYSTEM_PROMPT"):
        # Multiword system prompts cannot safely be parsed from ps output.
        if key == "LLM_SYSTEM_PROMPT":
            if source.get(key):
                effective[key] = source[key]
            continue
        match = re.search(r"(?:^|\s)" + key + r"=([^\s]+)", process)
        if match:
            effective[key] = match.group(1)
        elif source.get(key):
            effective[key] = source[key]
    base = effective.get("OLLAMA_BASE_URL", "")
    if not base.startswith("https://"):
        raise ValueError("Current app must use an HTTPS hosted LLM for this deployment")
    config = {
        "HOST": "127.0.0.1", "PORT": "8765", "ASR_BACKEND": "nls", "ASR_DEVICE": "cpu",
        "AGENT_BACKEND": "ollama", "TTS_BACKEND": "piper", "MAX_CONCURRENT_SESSIONS": "1",
        "FUNASR_VAD_MODEL": "/opt/real-time-voice/pretrained_models/fsmn-vad",
        "PIPER_MODEL_PATH": "/opt/real-time-voice/pretrained_models/piper/zh_CN-huayan-medium.onnx",
        "PIPER_MODEL_PATH_EN": "/opt/real-time-voice/pretrained_models/piper/en_US-lessac-medium.onnx",
        "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2",
        "PYTHONUNBUFFERED": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "VAD_MAX_END_SILENCE_MS": "600", "NLS_MAX_SENTENCE_SILENCE_MS": "600",
        **effective,
    }
    for key in ("ALIYUN_NLS_APP_KEY", "ALIYUN_AK_ID", "ALIYUN_AK_SECRET", "ALIYUN_NLS_TOKEN",
                "DEEPSEEK_API_KEY", "LLM_API_KEY", "ALIYUN_NLS_URL"):
        if source.get(key):
            config[key] = source[key]
    if not config.get("ALIYUN_NLS_APP_KEY") or not (config.get("ALIYUN_NLS_TOKEN") or (config.get("ALIYUN_AK_ID") and config.get("ALIYUN_AK_SECRET"))):
        raise ValueError("NLS credentials are incomplete")
    if not (config.get("DEEPSEEK_API_KEY") or config.get("LLM_API_KEY")):
        raise ValueError("Hosted LLM credentials are missing")
    lines = []
    for key, value in config.items():
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError("Multiline environment values are not supported")
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{escaped}"\n')
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(target["DEPLOY_HOST"], port=int(target.get("DEPLOY_PORT", "22")),
                       username=target.get("DEPLOY_USER", "root"), password=target["DEPLOY_PASSWORD"],
                       look_for_keys=False, allow_agent=False, timeout=15, auth_timeout=15)
        with client.open_sftp() as sftp:
            with sftp.open("/etc/real-time-voice.env", "wx") as stream:
                sftp.chmod("/etc/real-time-voice.env", 0o600)
                stream.write("".join(lines))
        print("Runtime configuration transferred over SSH (root-only; secret values omitted).")
        print("NLS authentication: renewable" if config.get("ALIYUN_AK_ID") and config.get("ALIYUN_AK_SECRET") else "NLS authentication: temporary token; refresh credentials required before sustained use.")
    finally:
        client.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Runtime configuration transfer failed ({type(error).__name__})", file=sys.stderr)
        raise SystemExit(1)
