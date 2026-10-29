#!/usr/bin/env python3.11
"""Fail fast with listener details when the web port is already occupied."""

import os
import socket
import subprocess


def main() -> int:
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8765"))
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError:
        details = ""
        try:
            details = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                check=False, capture_output=True, text=True, timeout=2,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        print(f"端口 {host}:{port} 已被占用，拒绝启动另一个实例。")
        if details:
            print(details)
        print("请先停止上面的旧进程，或使用 PORT 环境变量选择其他端口。")
        return 1
    finally:
        probe.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
