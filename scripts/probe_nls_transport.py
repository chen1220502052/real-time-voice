"""Remote-only real NLS diagnosis, no voice server or raw credential logging.

Run using the existing runtime EnvironmentFile in an isolated transient unit.
Audio input must be a named test fixture; callbacks record metadata, not text.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import struct
import threading
import time
import wave
import socket
import ssl
import http.client
import base64
import re
from urllib.parse import urlparse

from backend.providers.asr_nls import AliyunNlsAsrVadAdapter, checked_transcriber_factory
from backend.providers.nls_auth import token_provider_from_environment, EcsRamRoleNlsTokenProvider


def probe(fixture):
    import certifi
    import nls
    os.environ.setdefault("WEBSOCKET_CLIENT_CA_BUNDLE", certifi.where())
    started = time.monotonic()
    report = {"fixture": fixture.name, "mode": "real_nls_without_vad_or_bot",
              "callbacks": [], "chunks": 0, "bytes": 0, "sdkStartedStates": {}, "errors": []}
    lock = threading.Lock()

    def record(kind, message=None):
        event = AliyunNlsAsrVadAdapter._parse_nls_message(message) if message else {}
        header, payload = event.get("header", {}), event.get("payload", {})
        with lock:
            report["callbacks"].append({"kind": kind, "atMs": round((time.monotonic()-started)*1000, 2),
                                        "name": header.get("name"), "status": header.get("status"),
                                        "payloadKeys": sorted(payload), "resultChars": len(str(payload.get("result", "")))})

    class Transcriber(checked_transcriber_factory(nls)):
        def _NlsSpeechTranscriber__tr_core_on_error(self, error, *args):
            detail = str(error)
            report["transportError"] = {"type": type(error).__name__,
                                        "certificateVerifyFailed": "CERTIFICATE_VERIFY_FAILED" in detail,
                                        "verifyCode": getattr(error, "verify_code", None),
                                        "httpStatus": getattr(error, "status_code", None)}
            report["transportError"]["responseHeaderNames"] = sorted((getattr(error, "resp_headers", None) or {}).keys())
            report["transportError"]["reasonCategories"] = [word for word in
                ("expired", "invalid", "missing", "denied", "forbidden") if word in detail.lower()]
            return super()._NlsSpeechTranscriber__tr_core_on_error(error, *args)

        def start(self, *args, **kwargs):
            result = super().start(*args, **kwargs)
            record("sdk_start_return")
            return result

        def send_audio(self, pcm):
            # SDK exposes no public readiness predicate; this introspection
            # is confined to the diagnostic subclass, never the adapter.
            ready = str(getattr(self, "_NlsSpeechTranscriber__start_flag", "unavailable"))
            with lock:
                report["chunks"] += 1
                report["bytes"] += len(pcm)
                report["sdkStartedStates"][ready] = report["sdkStartedStates"].get(ready, 0)+1
            return super().send_audio(pcm)

    class Adapter(AliyunNlsAsrVadAdapter):
        pass

    for method in ("_on_nls_start", "_on_nls_sentence_begin", "_on_nls_sentence_end",
                   "_on_nls_result_changed", "_on_nls_completed", "_on_nls_error", "_on_nls_close"):
        original = getattr(AliyunNlsAsrVadAdapter, method)
        def observe(self, *args, _method=method, _original=original):
            record(_method, args[0] if args else None)
            return _original(self, *args)
        setattr(Adapter, method, observe)
    adapter = Adapter(transcriber_factory=Transcriber)
    host = urlparse(adapter.url).hostname
    report["tlsChecks"] = {}
    for name, context in (("system", ssl.create_default_context()),
                          ("certifi", ssl.create_default_context(cafile=certifi.where()))):
        try:
            with socket.create_connection((host, 443), timeout=8) as connection:
                with context.wrap_socket(connection, server_hostname=host):
                    report["tlsChecks"][name] = {"ok": True}
        except Exception as error:
            report["tlsChecks"][name] = {"ok": False, "type": type(error).__name__,
                                        "verifyCode": getattr(error, "verify_code", None)}
    try:
        adapter._token_provider = token_provider_from_environment()
        token = adapter._token_provider.get_token() if adapter._token_provider else adapter.token
    except Exception as error:
        report.update(status="failed", errors=[getattr(error, 'code', type(error).__name__)])
        return report
    report["authentication"] = {"source": "ecs_ram_role" if isinstance(adapter._token_provider, EcsRamRoleNlsTokenProvider)
                                else "ram_token_minted_in_new_process" if adapter._token_provider else "configured_token",
                                "nonempty": bool(token)}
    if adapter._token_provider:
        report["authentication"]["validForSeconds"] = round(min(item[1] for item in adapter._token_provider._cache.values())-time.time())
    # Observe the gateway's rejected Upgrade response without dumping any
    # request headers/token or response body. No ASR task is started here.
    connection = http.client.HTTPSConnection(host, timeout=8, context=ssl.create_default_context(cafile=certifi.where()))
    try:
        connection.request("GET", urlparse(adapter.url).path or "/", headers={
            "Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode(), "X-NLS-Token": token})
        response = connection.getresponse()
        body = response.read(4096).decode("utf-8", errors="replace") if response.status != 101 else ""
        report["independentHandshake"] = {"httpStatus": response.status,
                                          "reasonCategories": [word for word in ("expired", "invalid", "missing", "denied", "forbidden", "token", "privilege") if word in body.lower()],
                                          "serviceCodes": re.findall(r"(?:Meta|Gateway):[A-Z_]+", body),
                                          "bodyLength": len(body)}
    except Exception as error:
        report["independentHandshake"] = {"errorType": type(error).__name__}
    finally:
        connection.close()
    with wave.open(str(fixture), "rb") as source:
        assert (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (16000, 1, 2)
        pcm = source.readframes(source.getnframes())
    samples = struct.unpack(f"<{len(pcm)//2}h", pcm)
    report["input"] = {"samples": len(samples), "peak": max(map(abs, samples)),
                       "rms": round((sum(sample*sample for sample in samples)/len(samples))**.5, 2)}
    try:
        adapter._start_recognition()
        # Reproduce the adapter's 100ms cadence and SDK 20ms subdivision.
        for offset in range(0, len(pcm), 3200):
            adapter._send_cloud_audio(pcm[offset:offset+3200].ljust(3200, b"\0"))
            time.sleep(.1)
        for _ in range(8):
            adapter._send_cloud_audio(b"\0"*3200)
            time.sleep(.1)
        text = adapter._stop_recognition()
        report["resultChars"] = len(text)
        report["status"] = "passed" if text else "empty_result"
    except Exception as error:
        report["status"] = "failed"
        report["errors"].append(getattr(error, 'code', type(error).__name__))
    finally:
        try:
            asyncio.run(adapter.reset())
        except Exception as error:
            report["errors"].append("cleanup_"+type(error).__name__)
    report["elapsedMs"] = round((time.monotonic()-started)*1000, 2)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = probe(args.fixture)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)+"\n"
    args.output.write_text(encoded)
    print(encoded)
    raise SystemExit(0 if result["status"] == "passed" else 1)
