#!/usr/bin/env python3
"""Run a local deployment/test command while holding an ECS maintenance lease."""
import subprocess
import sys

from deploy_ssh import connect, maintenance_lease, read_config
from pathlib import Path


def main():
    args = sys.argv[1:]
    if args and args[0] == '--':
        args = args[1:]
    if not args:
        raise SystemExit('Usage: ecs_use.py -- COMMAND [ARGUMENTS...]')
    config = read_config(Path(__file__).resolve().parent.parent / '.env.deploy')
    client = connect(config)
    try:
        with maintenance_lease(client):
            return subprocess.call(args)
    finally:
        client.close()


if __name__ == '__main__':
    raise SystemExit(main())
