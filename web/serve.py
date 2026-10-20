#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VOC洞察平台 · Web 服务启动脚本

用法：
    python web/serve.py                          # 默认 127.0.0.1:8848，自动打开浏览器
    python web/serve.py --host 127.0.0.1 --port 8080
    python web/serve.py --port 8080 --no-browser
    python web/serve.py --data-dir D:\\VOCData    # 指定数据目录（产出/配置/演示数据）

本文件位于 web/ 目录，需要把项目根目录加入 sys.path 才能 import server 包。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# web/ 的上一级即项目根目录（server/、data/、runs/ 都在这一层）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fix_console() -> None:
    """Windows 控制台默认 GBK，遇到 ⚠ / ✓ 这类字符会直接抛 UnicodeEncodeError
    把整个服务带崩。这里只放宽编码错误处理：中文照常显示，生僻字符降级为 ? 而不是崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def pick_port(preferred: int, host: str = "127.0.0.1", tries: int = 20) -> int:
    """端口被占用时自动向后顺延，最多尝试 tries 个。"""
    for p in range(preferred, preferred + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise SystemExit(f"未找到可用端口（{preferred}~{preferred + tries - 1}），请用 --port 指定。")


def main() -> int:
    _fix_console()
    ap = argparse.ArgumentParser(
        description="VOC洞察平台 本地服务",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1（仅本机可访问）")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8848")),
                    help="监听端口，默认 8848；云平台常用环境变量 PORT")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    ap.add_argument("--data-dir", default="", help="数据目录（产出/配置/演示数据），默认见 README")
    args = ap.parse_args()

    # 数据目录要在 import server 之前定好（server.paths 在导入时就会解析）
    if args.data_dir:
        os.environ["VOC_DATA_DIR"] = args.data_dir

    try:
        import uvicorn
    except ImportError:
        print("缺少依赖 uvicorn，请先安装：\n  pip install fastapi uvicorn python-multipart openpyxl pandas")
        return 1

    from server.paths import data_root, is_frozen

    port = pick_port(args.port, args.host)
    url = f"http://{args.host}:{port}"
    if args.host not in ("127.0.0.1", "localhost"):
        url = f"http://{args.host}:{port}"

    print("=" * 58)
    print("  VOC洞察平台 · Voice of Customer Insight Platform")
    print("=" * 58)
    print(f"  服务地址 : {url}")
    print(f"  数据目录 : {data_root()}")
    print(f"  运行方式 : {'打包 exe' if is_frozen() else 'Python 源码'}")
    print("  平台板块 : AMAZON  /  WAYFAIR  /  REDDIT")
    print("  报告记录 : %s/#records" % url)
    if args.host not in ("127.0.0.1", "localhost"):
        print("  ⚠ 已监听非本机地址，请确认已设置登录密码且防火墙已放行")
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