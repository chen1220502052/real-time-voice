"""Build backend-only release from the tested remote prompt candidate."""

import hashlib
import json
from pathlib import Path
import re
import sys
import tarfile


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ("backend", "web") for p in (root / folder).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and not p.name.startswith("._")}


def main():
    release, candidate = sys.argv[1], Path(sys.argv[2]).resolve()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", release):
        raise ValueError("Invalid release")
    if candidate.parent != Path("/var/tmp") or not candidate.name.startswith("rtv-prompt-opt."):
        raise ValueError("Expected an isolated prompt candidate")
    root = Path("/opt/real-time-voice")
    baseline, tested = hashes(root), hashes(candidate)
    changed = sorted(k for k, v in tested.items() if baseline.get(k) != v)
    allowed = {"backend/english_chat.py", "backend/chat_materials.py",
               "backend/providers/llm.py", "backend/vad_prefetch.py"}
    if set(baseline) != set(tested) or set(changed) != allowed:
        raise RuntimeError("Unexpected source changes; review before deployment")
    if json.loads((candidate / "prompt-ab.json").read_text())["status"] != "passed":
        raise RuntimeError("Prompt benchmark has not passed")
    voice = json.loads((candidate / "voice.json").read_text())
    interrupt = json.loads((candidate / "interrupt.json").read_text())
    if voice["status"] != "passed" or voice["errors"] or interrupt["errors"] or interrupt["lateOldAudio"]:
        raise RuntimeError("Real voice or interruption acceptance failed")
    for name in ("commands", "recovery"):
        if not json.loads((candidate / (name + ".json")).read_text()).get("checks"):
            raise RuntimeError("Command/recovery acceptance missing")
    manifest = Path("/var/tmp") / (release + "-expected.json")
    bundle = Path("/var/tmp") / (release + ".tar.gz")
    if manifest.exists() or bundle.exists():
        raise RuntimeError("Do not overwrite an existing release")
    manifest.write_text(json.dumps(baseline, indent=2) + "\n")
    manifest.chmod(0o600)

    def clean(info):
        return None if "__pycache__" in Path(info.name).parts or Path(info.name).name.startswith("._") else info

    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(candidate / "backend", arcname="backend", filter=clean)
    bundle.chmod(0o600)
    print(json.dumps({"release": release, "bundleSha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                      "changedFiles": changed, "candidateHashes": tested}))


if __name__ == "__main__":
    main()
