#!/usr/bin/env python3
"""Local on-demand ECS startup. Cloud credentials must exist outside the ECS."""
import argparse
import os
from pathlib import Path
import time

try:
    import aliyun_instance_ctl as ctl
except ImportError:
    from scripts import aliyun_instance_ctl as ctl


class PowerUnavailable(RuntimeError):
    pass


def credentials():
    path = Path.home() / '.config/aliyun-ecs.env'
    if not (os.getenv('ALIYUN_ACCESS_KEY_ID') and os.getenv('ALIYUN_ACCESS_KEY_SECRET')):
        if not path.exists():
            raise PowerUnavailable(
                'ECS is unreachable and no local cloud identity is configured. '
                'Start it in the Alibaba Cloud console, or configure the dedicated '
                'RAM startup credentials in ~/.config/aliyun-ecs.env (mode 0600).')
        if path.stat().st_mode & 0o077:
            raise PowerUnavailable('Local ECS credential file must have mode 0600.')
    return ctl.load_credentials()


def ensure_running(timeout=300):
    key, secret, token = credentials()
    def api(action, params):
        return ctl.call(action, key, secret, ctl.DEFAULT_REGION,
                        ctl.PUBLIC_ENDPOINT.format(region=ctl.DEFAULT_REGION),
                        params, security_token=token)
    def status():
        result = api('DescribeInstanceStatus', {'InstanceId.1': ctl.DEFAULT_INSTANCE})
        statuses = result.get('InstanceStatuses', {}).get('InstanceStatus', [])
        if not statuses:
            raise PowerUnavailable('The configured ECS instance was not found.')
        return statuses[0]['Status']
    deadline = time.monotonic() + timeout
    requested = False
    while time.monotonic() < deadline:
        state = status()
        if state == 'Running':
            return
        if state == 'Stopped' and not requested:
            api('StartInstance', {'InstanceId': ctl.DEFAULT_INSTANCE})
            requested = True
            print('ECS startup requested; waiting for Running.', flush=True)
        elif state not in ('Stopped', 'Stopping', 'Starting', 'Pending'):
            raise PowerUnavailable('Cannot start ECS in state: ' + state)
        time.sleep(5)
    raise PowerUnavailable('Timed out waiting for ECS startup.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['start'])
    parser.parse_args()
    ensure_running()
