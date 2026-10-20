#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web/serve.py · 起服务。

**必须用带 openpyxl 的解释器**（内核读 xlsx 时延迟导入它）。本机约定::

    C:\\Users\\Administrator\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe

用法::

    python web/serve.py                       # 127.0.0.1:8080，自动拉起一个 worker 子进程
    python web/serve.py --host 0.0.0.0        # 局域网可访问
    python web/serve.py --worker inproc       # 退回 C1：worker 跑在 Web 进程里（调试用）
    python web/serve.py --worker off          # 只起 Web；worker 自己单独跑 web.worker_main
    python web/serve.py --no-auth             # 就算有用户也免登录（纯内网临时用）
    python web/serve.py --reload              # 开发热重载

为什么默认 spawn
================

Stage 9 的一条实测：worker 跑在 Web 进程里时，内核单次 ``annotate`` 要给数千条评论
逐个写中间文件，本机一次 ``os.replace`` 能卡到 ~12s —— 那次把**整个事件循环**冻住了
约 87s，连 ``/api/health`` 都不响应。拆进程之后：

* Web 的响应延迟不再跟着内核的磁盘抖动一起抖；
* worker 崩了 Web 不会陪葬（死了就 reap，重跑从断点续跑）；
* 想加机器就再加一个 ``python -m web.worker_main``，两边只通过 ``queue.db`` 说话。

代价是「Web 不知道 worker 线程还活着」—— 改看**心跳**（见 ``app._worker_alive``）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
if str(PLATFORM) not in sys.path:
    sys.path.insert(0, str(PLATFORM))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="VOC 洞察平台 · Web 服务（C2）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--worker", choices=("spawn", "inproc", "off"),
                    default=os.environ.get("VOC_WEB_WORKER_MODE") or "spawn",
                    help="worker 形态：spawn=独立子进程（默认）/ inproc=同进程线程 / off=不起")
    ap.add_argument("--auth", choices=("on", "off"), default=None,
                    help="认证开关；不传则按 VOC_WEB_AUTH 与是否有用户推断")
    ap.add_argument("--no-auth", action="store_true",
                    help="强制免登录（等价 --auth off，纯内网临时用）")
    ap.add_argument("--storage", choices=("local", "s3"), default=None,
                    help="对象存储后端（默认 local）")
    ap.add_argument("--reload", action="store_true", help="开发热重载")
    args = ap.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        print("缺 uvicorn。装：pip install -e \".[web]\"", file=sys.stderr)
        return 2

    #: 这几个必须在 ``import web.app`` **之前**就绪 —— 它在模块级就读了这几个变量
    os.environ["VOC_WEB_WORKER_MODE"] = args.worker
    if args.no_auth:
        args.auth = "off"
    if args.auth:
        os.environ["VOC_WEB_AUTH"] = "1" if args.auth == "on" else "0"
    if args.storage:
        os.environ["VOC_WEB_STORAGE"] = args.storage

    if args.reload:
        target: object = "web.app:app"
    else:
        from web.app import app as target  # type: ignore[assignment]

    print(f"平台目录：{PLATFORM}")
    print(f"解释器：  {sys.executable}")
    print(f"worker：  {args.worker}"
          f"{'（自动拉起子进程）' if args.worker == 'spawn' else ''}")
    print(f"认证：    {'开' if (args.auth or 'on') == 'on' else '按用户表推断'}")
    print(f"存储：    {args.storage or 'local'}")
    print(f"打开：    http://{args.host}:{args.port}/")
    uvicorn.run(target, host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
