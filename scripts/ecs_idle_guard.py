#!/usr/bin/env python3
"""ECS idle shutdown and shared maintenance leases (stdlib only, Linux)."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import uuid

RUNTIME = Path('/run/ecs-power')
ACCESS = Path('/var/log/nginx/ecs-activity.log')
IDLE_SECONDS = 1800


def recent_access(path=ACCESS):
    """Only nginx's filtered successful business requests enter this file."""
    with path.open('rb') as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(max(0, size - 1024 * 1024))
        lines = stream.read().splitlines()
    latest = 0.0
    for line in lines:
        try:
            latest = max(latest, float(json.loads(line)['t']))
        except (ValueError, KeyError, TypeError):
            continue
    # A nonempty, unparseable log is not evidence of idleness.
    if size and not latest:
        raise ValueError('Activity log has no valid timestamps')
    # Rotation can replace the log with an empty file just after real traffic.
    # Treat file creation/write time as activity too, to avoid an early stop.
    return max(latest, path.stat().st_mtime)


def can_stop(now, last_activity, active_connections, in_use, healthy):
    if not healthy:
        return False, 'health_unavailable'
    if active_connections or in_use:
        return False, 'active_request_or_session'
    if now - last_activity < IDLE_SECONDS:
        return False, 'recent_activity'
    return True, 'idle_30_minutes'


def activity():
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o755)
    (RUNTIME / 'activity').touch()


def lease():
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o755)
    with (RUNTIME / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        activity()
        print('READY', flush=True)
        try:
            # The SSH channel holds stdin open until the deployment/test ends.
            while sys.stdin.buffer.read(1):
                pass
        finally:
            activity()


def health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8765/health', timeout=5) as response:
        return json.load(response)


def connections():
    result = subprocess.run(
        ['ss', '-Hnt', 'state', 'established', '( sport = :8765 or sport = :18765 )'],
        capture_output=True, text=True, check=True, timeout=5)
    return bool(result.stdout.strip())


def cloud_api(action, params):
    import aliyun_instance_ctl as ctl
    key, secret, token = ctl.load_credentials()
    return ctl.call(action, key, secret, ctl.DEFAULT_REGION,
                    'oos.cn-hongkong.aliyuncs.com', params,
                    security_token=token, api_version='2019-06-01')


def request_stop():
    import aliyun_instance_ctl as ctl
    template = {
        'FormatVersion': 'OOS-2019-06-01',
        'Description': 'Stop this ECS after 30 minutes without access or maintenance',
        'Parameters': {'role': {'Type': 'String'}},
        'RamRole': '{{ role }}',
        'Tasks': [{'Name': 'stopIdleInstance', 'Action': 'ACS::ECS::StopInstance',
                   'Properties': {'regionId': ctl.DEFAULT_REGION,
                                  'instanceId': ctl.DEFAULT_INSTANCE,
                                  'stoppedMode': 'StopCharging'}}],
    }
    result = cloud_api('StartExecution', {
        'TemplateContent': json.dumps(template),
        'Parameters': json.dumps({'role': 'ecs-night-scheduler-role'}),
        'ClientToken': str(uuid.uuid4()),
        'Description': 'ECS idle guard: 30 minutes idle, economical stop',
    })
    execution = result['Execution']['ExecutionId']
    (RUNTIME / 'pending.json').write_text(json.dumps({'id': execution, 'time': time.time()}))
    print('StopCharging requested: ' + execution, flush=True)


def check(dry_run=False):
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o755)
    with (RUNTIME / 'lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('keep_running: deployment_or_test_lease')
            return
        pending = RUNTIME / 'pending.json'
        if pending.exists() and not dry_run:
            data = json.loads(pending.read_text())
            result = cloud_api('ListExecutions', {'ExecutionId': data['id']})
            executions = result.get('Executions', [])
            if not executions or executions[0]['Status'] not in ('Failed', 'Cancelled'):
                print('keep_running: stop_already_requested ' + data['id'])
                return
            pending.unlink()
        now = time.time()
        boot = now - float(Path('/proc/uptime').read_text().split()[0])
        marker = RUNTIME / 'activity'
        last = max(boot, marker.stat().st_mtime if marker.exists() else boot,
                   recent_access())
        state = health()
        allowed, reason = can_stop(now, last, connections(),
                                   state.get('resources', {}).get('inUse', 0), state.get('ok') is True)
        print(json.dumps({'stop': allowed, 'reason': reason,
                          'idleSeconds': round(now-last), 'dryRun': dry_run}), flush=True)
        if allowed and not dry_run:
            # Recheck both traffic and business sessions just before submitting.
            state = health()
            if (connections() or not state.get('ok') or state.get('resources', {}).get('inUse', 0)
                    or time.time() - max(last, recent_access()) < IDLE_SECONDS):
                return
            request_stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['lease', 'touch', 'check'])
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.command == 'lease':
        lease()
    elif args.command == 'touch':
        activity()
    else:
        check(args.dry_run)


if __name__ == '__main__':
    try:
        main()
    except (Exception, SystemExit) as error:
        if isinstance(error, SystemExit) and error.code in (0, None):
            raise
        # Fail closed; do not print signed URLs or credentials.
        print('Idle shutdown skipped: ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
