#!/usr/bin/env python3
"""Compatibility entry point; use verified transport and ECS maintenance leases."""
import sys
from deploy_ssh import main

if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit("Usage: ssh_deploy.py 'REMOTE COMMAND'")
    sys.argv = [sys.argv[0], 'exec', *sys.argv[1:]]
    raise SystemExit(main())
