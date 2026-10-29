#!/usr/bin/env python3.11
"""Fail-fast environment checks for the local demo and real-model profiles."""

import importlib.util
import json
import os
import platform
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    project = Path(__file__).resolve().parent.parent
    checks = {
        "python": {"ok": sys.version_info[:2] == (3, 11), "value": platform.python_version()},
        "aiohttp": {"ok": importlib.util.find_spec("aiohttp") is not None},
        "web": {"ok": (project / "web" / "app.js").is_file()},
    }
    asr = os.getenv("ASR_BACKEND", "stub")
    tts = os.getenv("TTS_BACKEND", "demo")
    agent = os.getenv("AGENT_BACKEND", "demo")
    if asr in {"funasr", "dashscope", "nls"}:
        checks["funasr"] = {"ok": importlib.util.find_spec("funasr") is not None}
        cache = Path.home() / ".cache" / "modelscope" / "models"
        vad = Path(os.getenv("FUNASR_VAD_MODEL", "")) if os.getenv("FUNASR_VAD_MODEL") else cache / "iic--speech_fsmn_vad_zh-cn-16k-common-pytorch" / "snapshots" / "master"
        checks["fsmn_vad_model"] = {"ok": vad.is_dir(), "value": str(vad)}
    if asr == "funasr":
        cache = Path.home() / ".cache" / "modelscope" / "models"
        paraformer = Path(os.getenv("FUNASR_ASR_MODEL", "")) if os.getenv("FUNASR_ASR_MODEL") else cache / "iic--speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-online" / "snapshots" / "master"
        checks["paraformer_model"] = {"ok": paraformer.is_dir(), "value": str(paraformer)}
    if asr == "dashscope":
        checks["dashscope_sdk"] = {"ok": importlib.util.find_spec("dashscope") is not None}
        checks["dashscope_api_key"] = {
            "ok": bool(os.getenv("DASHSCOPE_API_KEY")),
            "value": "configured" if os.getenv("DASHSCOPE_API_KEY") else "missing",
        }
    if asr == "nls":
        checks["nls_sdk"] = {"ok": importlib.util.find_spec("nls") is not None}
        checks["nls_app_key"] = {
            "ok": bool(os.getenv("ALIYUN_NLS_APP_KEY")),
            "value": "configured" if os.getenv("ALIYUN_NLS_APP_KEY") else "missing",
        }
        manual_token = bool(os.getenv("ALIYUN_NLS_TOKEN"))
        automatic_refresh = bool(os.getenv("ALIYUN_AK_ID") and os.getenv("ALIYUN_AK_SECRET"))
        checks["nls_auth"] = {
            "ok": manual_token or automatic_refresh,
            "value": "automatic_refresh" if automatic_refresh else "manual_token" if manual_token else "missing",
        }
    if tts == "piper":
        model = project / os.getenv("PIPER_MODEL_PATH", "pretrained_models/piper/zh_CN-huayan-medium.onnx")
        checks["piper"] = {"ok": importlib.util.find_spec("piper") is not None}
        checks["piper_model"] = {"ok": model.is_file(), "value": str(model)}
    if agent in {"ollama", "english_chat"}:
        base_url = (os.getenv("LLM_BASE_URL") or os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
        model = os.getenv("LLM_MODEL") or os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b")
        api_key = os.getenv("LLM_API_KEY") or os.getenv("DEEPSEEK_API_KEY") or ""
        checks["llm_runtime"] = {"ok": bool(api_key) or shutil.which("ollama") is not None,
                                 "value": "hosted" if api_key else "local"}
        try:
            handlers = [urllib.request.ProxyHandler({})]
            if base_url.startswith("https://"):
                import certifi
                import ssl
                handlers.append(urllib.request.HTTPSHandler(
                    context=ssl.create_default_context(cafile=certifi.where())))
            opener = urllib.request.build_opener(*handlers)
            if api_key:
                request = urllib.request.Request(
                    base_url + ("/models" if base_url.endswith("/v1") else "/v1/models"), headers={"Authorization": f"Bearer {api_key}"})
            else:
                request = urllib.request.Request(base_url + "/api/tags")
            with opener.open(request, timeout=float(os.getenv("OLLAMA_CHECK_TIMEOUT_SECONDS", "10"))) as response:
                payload = json.load(response)
            if api_key:
                names = [item.get("id", "") for item in payload.get("data", [])]
            else:
                names = [item.get("name", "") for item in payload.get("models", [])]
            checks["ollama_service"] = {"ok": True, "value": base_url}
            checks["ollama_model"] = {"ok": model in names, "value": model}
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            checks["ollama_service"] = {"ok": False, "value": str(exc)}
            checks["ollama_model"] = {"ok": False, "value": model}
    free_gb = shutil.disk_usage(project).free / (1024 ** 3)
    checks["free_disk"] = {"ok": free_gb >= 2, "value": f"{free_gb:.1f} GiB"}
    result = {"ok": all(item["ok"] for item in checks.values()), "profile": {"asr": asr, "agent": agent, "tts": tts}, "checks": checks}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
