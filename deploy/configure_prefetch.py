"""Root-only config release; preserve code, credentials and duplex tuning."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request


def updated_config(source, release, enabled):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", release) or enabled not in {"0", "1"}:
        raise ValueError("Invalid configuration release")
    keys = {"GIT_COMMIT", "DUPLEX_PREFETCH_ENABLED"}
    lines = [line for line in source.splitlines() if line.split("=", 1)[0].strip() not in keys]
    return "\n".join(lines + ["GIT_COMMIT=" + release, "DUPLEX_PREFETCH_ENABLED=" + enabled]) + "\n"


def health():
    return json.load(urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=3))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True)
    parser.add_argument("--expected-release", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--enabled", choices=["0", "1"], required=True)
    args = parser.parse_args()
    config = Path("/etc/real-time-voice.env")
    source = config.read_text()
    replacement = updated_config(source, args.release, args.enabled)
    evidence = json.loads(args.evidence.read_text())
    if evidence.get("status") != "passed" or evidence.get("physicalOutput") is not None:
        raise RuntimeError("Expected a successful, explicitly non-physical acceptance report")
    if args.enabled == "1":
        summary = evidence["summary"]
        if (summary["pairs"] < 8 or not summary["arms"]["on"]["hits"]
                or summary["pairedSavingsMs"]["fixtureSignalEndToFirstPacketMs"]["median"] <= 0):
            raise RuntimeError("Insufficient evidence of prefetch latency benefit")
        if set(evidence.get("multiturn", {})) != {"off", "on"} or set(evidence.get("interruptions", {})) != {"off", "on"}:
            raise RuntimeError("Missing on/off multi-turn or interruption guardrails")
        for result in evidence["interruptions"].values():
            if result["errors"] or result["lateOldAudio"] or len(result["interruptions"]) < 2:
                raise RuntimeError("Interruption guardrail failed")
        for result in evidence["multiturn"].values():
            if result["errors"] or len(result["asrFinals"]) < 4:
                raise RuntimeError("Multi-turn guardrail failed")
        if not evidence.get("commands") or not evidence.get("recovery"):
            raise RuntimeError("Missing command/idempotency/state recovery guardrails")
    root = Path("/opt/real-time-voice")
    for name, digest in evidence["sourceHashes"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError("Deployed source changed since acceptance; refuse switch")

    def idle():
        state = health()
        if not state.get("ok") or state.get("gitCommit") != args.expected_release or state["resources"]["inUse"]:
            raise RuntimeError("Unexpected release or active trial; do not interrupt it")

    idle()
    backup = Path("/var/backups/real-time-voice") / args.release
    backup.mkdir(parents=True, mode=0o700, exist_ok=False)
    shutil.copy2(config, backup / "runtime.env")
    (backup / "runtime.env").chmod(0o600)
    shutil.copy2(args.evidence, backup / "acceptance.json")
    (backup / "acceptance.json").chmod(0o600)
    if config.read_text() != source:
        raise RuntimeError("Configuration changed concurrently; refuse switch")
    idle()

    def command(action):
        subprocess.run(["systemctl", action, "real-time-voice"], check=True)

    stopped = False
    try:
        temporary = config.with_name(config.name + "." + args.release)
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(replacement)
        stopped = True
        command("stop")
        os.replace(temporary, config)
        command("start")
        for _ in range(120):
            try:
                state = health()
                if (state.get("ok") and state.get("gitCommit") == args.release
                        and state.get("textBotPreparation") == (args.enabled == "1")):
                    print(json.dumps({"release": args.release, "prefetchEnabled": state["textBotPreparation"],
                                      "backup": str(backup)}), flush=True)
                    return
            except (OSError, ValueError):
                pass
            time.sleep(1)
        raise RuntimeError("New configuration health check timed out")
    except Exception:
        if stopped:
            command("stop")
            shutil.copy2(backup / "runtime.env", config)
            command("start")
        raise


if __name__ == "__main__":
    main()
