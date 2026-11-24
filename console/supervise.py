"""控制台看护进程：server.py 退出就自动拉起，避免"打不开了"。

不要双击它；用 Start-Process -WindowStyle Hidden 起，日志写到 state/console.*.log。
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"
PY = sys.executable
sys.path.insert(0, str(HERE.parent / 'src'))
import settings


def port_open() -> bool:
    s = socket.socket()
    s.settimeout(1.0)
    try:
        s.connect(("127.0.0.1", settings.load()["console"]["port"]))
        return True
    except OSError:
        return False
    finally:
        s.close()


def main() -> None:
    # [2026-11-06] 单实例：已经在跑就直接退出，避免开机脚本重复拉两份互相抢端口
    if port_open():
        print("[supervise] 控制台已在运行，退出", flush=True)
        return
    while True:
        if port_open():
            time.sleep(5)
            continue
        p = subprocess.Popen([PY, str(SERVER)], cwd=str(HERE.parent))
        code = p.wait()
        print(f"[supervise] server.py 退出 code={code}，3 秒后重启", flush=True)
        time.sleep(3)


if __name__ == "__main__":
    main()
