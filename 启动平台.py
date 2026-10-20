#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VOC洞察平台 · 一键启动

双击本文件即可启动本地服务并自动打开浏览器。
    python 启动平台.py            # 默认端口 8848
    python 启动平台.py --port 9000
    python 启动平台.py --no-browser
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def pick_port(preferred: int) -> int:
    for p in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise SystemExit("未找到可用端口，请用 --port 指定。")


def main() -> int:
    ap = argparse.ArgumentParser(description="VOC洞察平台 本地服务")
    ap.add_argument("--port", type=int, default=8848)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("缺少依赖 uvicorn，请先安装：\n  pip install fastapi uvicorn python-multipart openpyxl pandas")
        return 1

    port = pick_port(args.port)
    url = f"http://{args.host}:{port}"

    print("=" * 58)
    print("  VOC洞察平台 · Voice of Customer Insight Platform")
    print("=" * 58)
    print(f"  服务地址 : {url}")
    print(f"  工作目录 : {ROOT}")
    print(f"  平台板块 : AMAZON  /  WAYFAIR  /  REDDIT")
    print("  停止服务 : 在本窗口按 Ctrl+C")
    print("=" * 58)

    if not args.no_browser:
        def _open():
            time.sleep(1.2)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    from server.app import app
    uvicorn.run(app, host=args.host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
