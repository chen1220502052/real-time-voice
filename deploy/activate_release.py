"""Root-only ECS release installer. Preserve credentials and keep a rollback."""

import json
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request


def command(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)


def release_settings(release, prefetch=None, nls_auth=None, connection_capacity=None):
    settings = {"GIT_COMMIT": release}
    if prefetch is not None:
        if prefetch not in {"0", "1"}:
            raise ValueError("Prefetch setting must be 0 or 1")
        settings["DUPLEX_PREFETCH_ENABLED"] = prefetch
    if nls_auth is not None:
        if nls_auth != "ecs_ram_role":
            raise ValueError("Only the explicit ECS RAM role migration is supported")
        settings["ALIYUN_NLS_AUTH_MODE"] = nls_auth
        settings["PYTHONPATH"] = "/opt/real-time-voice/runtime_deps/nls_role"
    if connection_capacity is not None:
        if connection_capacity not in {'2', '3'}:
            raise ValueError('Only validated two/three-call browser access migration is supported')
        settings.update(BROWSER_AUTH_ENABLED='1', BROWSER_AUTH_COOKIE_SECURE='1',
                        BROWSER_AUTH_KEY_FILE='/var/lib/real-time-voice/browser-auth.key',
                        MAX_CONCURRENT_SESSIONS=connection_capacity, LOG_AUDIO_SPANS='0')
    return settings


def main():
    release = sys.argv[1]
    settings = release_settings(release, (sys.argv[4] if sys.argv[4] != '-' else None) if len(sys.argv) > 4 else None,
                                (sys.argv[5] if sys.argv[5] != '-' else None) if len(sys.argv) > 5 else None,
                                sys.argv[6] if len(sys.argv) > 6 else None)
    parts = ['backend'] if len(sys.argv) > 3 and sys.argv[3] == 'backend-only' else ['backend', 'web']
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", release):
        raise ValueError("Invalid release")
    root = Path("/opt/real-time-voice")
    config = Path("/etc/real-time-voice.env")
    bundle = Path("/var/tmp") / (release + ".tar.gz")
    backup = Path("/var/backups/real-time-voice") / release
    health = json.load(urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=5))
    if health.get("resources", {}).get("inUse", 0):
        raise RuntimeError("Active trial session; retry after it ends")
    if len(sys.argv) > 2:
        expected = json.loads(Path(sys.argv[2]).read_text())
        for relative, digest in expected.items():
            if relative == '@nginx-https':
                if hashlib.sha256(Path('/etc/nginx/sites-available/real-time-voice-https').read_bytes()).hexdigest() != digest:
                    raise RuntimeError('Nginx configuration changed since validation')
                continue
            target = (root / relative).resolve()
            if not target.is_relative_to(root) or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise RuntimeError("Remote files changed since validation; activation cancelled")
    if "ALIYUN_NLS_AUTH_MODE" in settings and (root / "runtime_deps").exists():
        parts.append("runtime_deps")
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    nginx = Path('/etc/nginx/sites-available/real-time-voice-https')
    log_override = Path('/etc/systemd/system/real-time-voice.service.d/connection-logging.conf')
    browser_migration = 'BROWSER_AUTH_ENABLED' in settings
    new_browser_files = [root/name for name in ('backend/browser_access.py','web/connection.js') if not (root/name).exists()]
    if browser_migration:
        shutil.copy2(nginx, backup/'nginx-https.conf')
        if log_override.exists():
            shutil.copy2(log_override, backup/'connection-logging.conf')
    command("tar", "-czf", str(backup / "app.tar.gz"), "-C", str(root), *parts)
    shutil.copy2(config, backup / "runtime.env")
    os.chmod(backup / "runtime.env", 0o600)
    config_lines = config.read_text().splitlines()
    if "ALIYUN_NLS_AUTH_MODE" in settings:
        existing_path = next((line.split("=", 1)[1].strip().strip('"').strip("'")
                              for line in config_lines if line.startswith("PYTHONPATH=")), "")
        if existing_path and existing_path != settings["PYTHONPATH"]:
            raise RuntimeError("Existing custom Python path; review before role migration")
    state_setting = next((line.split("=", 1)[1].strip().strip('"').strip("'") for line in config_lines
                          if line.split("=", 1)[0].strip() == "BOT_STATE_DB"), "")
    if state_setting and Path(state_setting).is_file():
        with sqlite3.connect(state_setting) as source, sqlite3.connect(backup / "bot-state.sqlite3") as destination:
            source.backup(destination)
        os.chmod(backup / "bot-state.sqlite3", 0o600)
    print("Backup ready: " + str(backup), flush=True)
    stopped = False
    try:
        # Recheck after the backup too; never stop a newly started trial.
        health = json.load(urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=5))
        if health.get("resources", {}).get("inUse", 0):
            raise RuntimeError("A trial started during backup; activation cancelled")
        state_folder = Path("/var/lib/real-time-voice/bot")
        if not state_folder.exists():
            state_folder.mkdir(mode=0o700)
            owner = subprocess.check_output(["systemctl", "show", "real-time-voice", "-p", "User", "--value"], text=True).strip()
            shutil.chown(state_folder, user=owner or "root")
        stopped = True
        command("systemctl", "stop", "real-time-voice")
        command("tar", "-xzf", str(bundle), "--no-same-owner", "--no-same-permissions", "-C", str(root))
        if browser_migration:
            key_file = Path(settings['BROWSER_AUTH_KEY_FILE'])
            if not key_file.exists():
                descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'wb') as stream:
                    stream.write(secrets.token_bytes(48))
                shutil.chown(key_file, user='catvoice', group='catvoice')
            if key_file.is_symlink() or len(key_file.read_bytes()) < 32 or key_file.stat().st_mode & 0o077:
                raise RuntimeError('Browser signing key is missing or not private')
            log_override.parent.mkdir(parents=True, exist_ok=True)
            log_override.write_text('[Service]\nLogRateLimitBurst=2000\n')
            command('systemctl', 'daemon-reload')
        if "ALIYUN_NLS_AUTH_MODE" in settings and not (root / "runtime_deps/nls_role/alibabacloud_credentials").is_dir():
            raise RuntimeError("Validated role credential runtime dependencies are missing")
        # Preserve provider/model and duplex tuning. Only the explicitly
        # requested role migration removes obsolete NLS static credentials.
        if not state_setting:
            settings["BOT_STATE_DB"] = str(state_folder / "state.sqlite3")
        if not any(line.startswith("ACCOUNT_DB=") for line in config_lines):
            settings["ACCOUNT_DB"] = "/var/lib/real-time-voice/accounts.sqlite3"
        lines = config.read_text().splitlines()
        removed = set(settings)
        if "ALIYUN_NLS_AUTH_MODE" in settings:
            removed.update({"ALIYUN_NLS_TOKEN", "ALIYUN_AK_ID", "ALIYUN_AK_SECRET"})
        output = [line for line in lines if line.split("=", 1)[0].strip() not in removed]
        output += [key + "=" + value for key, value in settings.items()]
        temporary = config.with_name(config.name + "." + release)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write("\n".join(output) + "\n")
        os.replace(temporary, config)
        command("systemctl", "start", "real-time-voice")
        print("Service restarting; waiting for ASR/TTS warmup.", flush=True)
        for _ in range(120):
            try:
                health = json.load(urllib.request.urlopen("http://127.0.0.1:8765/health", timeout=2))
                if (health.get("ok") and health.get("gitCommit") == release
                        and ("DUPLEX_PREFETCH_ENABLED" not in settings or
                             health.get("textBotPreparation") == (settings["DUPLEX_PREFETCH_ENABLED"] == "1"))):
                    if browser_migration:
                        if health.get('browserAuth') not in {'cookie', 'account'} or health['resources']['capacity'] != int(settings['MAX_CONCURRENT_SESSIONS']):
                            raise RuntimeError('Browser authentication/capacity activation mismatch')
                        shutil.copy2(root/'deploy/nginx-https.conf', nginx)
                        command('nginx', '-t')
                        command('systemctl', 'reload', 'nginx')
                    print(json.dumps(health, ensure_ascii=False), flush=True)
                    return
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(1)
        raise RuntimeError("New release health check timed out")
    except Exception:
        if not stopped:
            raise
        print("Activation failed; restoring previous application and configuration.", flush=True)
        command("systemctl", "stop", "real-time-voice")
        command("tar", "-xzf", str(backup / "app.tar.gz"), "-C", str(root))
        if browser_migration:
            for path in new_browser_files:
                if path.is_file():
                    path.unlink()
        shutil.copy2(backup / "runtime.env", config)
        if browser_migration:
            shutil.copy2(backup/'nginx-https.conf', nginx)
            if (backup/'connection-logging.conf').exists():
                shutil.copy2(backup/'connection-logging.conf', log_override)
            elif log_override.exists():
                log_override.unlink()
            command('systemctl', 'daemon-reload')
            command('nginx', '-t')
            command('systemctl', 'reload', 'nginx')
        command("systemctl", "start", "real-time-voice")
        raise


if __name__ == "__main__":
    main()
