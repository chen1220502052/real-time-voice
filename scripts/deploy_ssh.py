#!/usr/bin/env python3
"""Deployment transport; credentials stay in the ignored local .env.deploy.

Run with .local/deploy-venv/bin/python. Host keys must already be trusted in
~/.ssh/known_hosts; unknown or changed keys are never accepted by this helper.
"""

import argparse
from contextlib import contextmanager
from pathlib import Path
import sys
import time

import paramiko


def connect(config):
    """Try SSH first; use the independent local RAM identity if ECS is stopped."""
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    def attempt():
        client.connect(
            hostname=config['DEPLOY_HOST'], port=int(config.get('DEPLOY_PORT', '22')),
            username=config.get('DEPLOY_USER', 'root'), password=config['DEPLOY_PASSWORD'],
            look_for_keys=False, allow_agent=False, timeout=10, auth_timeout=10,
            banner_timeout=10)
        client.get_transport().set_keepalive(20)
        return client
    try:
        return attempt()
    except (OSError, paramiko.ssh_exception.NoValidConnectionsError):
        # Do not mistake an authentication or changed-host-key failure for shutdown.
        try:
            from ecs_power import ensure_running
        except ImportError:
            from scripts.ecs_power import ensure_running
        ensure_running()
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                return attempt()
            except (OSError, paramiko.ssh_exception.NoValidConnectionsError):
                time.sleep(3)
        client.close()
        raise RuntimeError('ECS is Running but SSH did not become ready.')


@contextmanager
def maintenance_lease(client):
    """A separate SSH channel holds a shared lock for exec, SFTP or local tests."""
    transport = client.get_transport()
    channel = transport.open_session(timeout=10)
    channel.exec_command('/usr/bin/python3 /opt/real-time-voice/scripts/ecs_idle_guard.py lease')
    channel.settimeout(10)
    try:
        line = channel.makefile('rb').readline()
        if line.strip() != b'READY':
            raise RuntimeError('Could not acquire the ECS deployment/test lease.')
        yield
    finally:
        channel.shutdown_write()
        channel.close()


def read_config(path: Path) -> dict[str, str]:
    if path.stat().st_mode & 0o077:
        raise ValueError("Credential file must have mode 0600")
    config = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            config[key.strip().removeprefix("export ").strip()] = value
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["exec", "upload", "download"])
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / ".env.deploy")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    config = read_config(args.config)
    if not config.get("DEPLOY_PASSWORD"):
        raise ValueError("DEPLOY_PASSWORD has not been filled in")
    client = connect(config)
    try:
        with maintenance_lease(client):
            return perform(client, args)
    finally:
        client.close()


def perform(client, args):
    # Keep all operations within the caller's maintenance lease.
    if args.action == "exec":
        command = " ".join(args.paths) if args.paths else sys.stdin.read()
        if not command.strip():
            raise ValueError("Remote command is empty")
        _, stdout, stderr = client.exec_command(command, timeout=args.timeout)
        channel = stdout.channel
        # Drain both streams while the command runs, avoiding full SSH
        # channel windows on long package installs.
        import select
        while True:
            if channel.recv_ready():
                sys.stdout.buffer.write(channel.recv(65536))
                sys.stdout.buffer.flush()
            if channel.recv_stderr_ready():
                sys.stderr.buffer.write(channel.recv_stderr(65536))
                sys.stderr.buffer.flush()
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            select.select([channel], [], [], 0.2)
        return channel.recv_exit_status()
    if len(args.paths) != 2:
        raise ValueError("Supply source and destination paths")
    with client.open_sftp() as sftp:
        if args.action == "upload":
            sftp.put(args.paths[0], args.paths[1])
            sftp.chmod(args.paths[1], 0o600)
        else:
            sftp.get(args.paths[0], args.paths[1])
            Path(args.paths[1]).chmod(0o600)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Never include configuration values in an authentication error.
        if type(exc).__name__ == 'PowerUnavailable':
            print(str(exc), file=sys.stderr)
        else:
            print(f"Deployment connection failed ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(1)
