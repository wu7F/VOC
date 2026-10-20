#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web/worker_main.py · **独立 worker 进程**（C2 的默认形态）。

为什么要把 worker 拆出 Web 进程
===============================

C1 的 worker 是 Web 进程里的一个**线程**，代价在 Stage 9 已经量出来过：

1. **抢 CPU / 抢事件循环。** 内核单次 ``annotate`` 要给 7,662 条评论逐个写中间文件
   （实测 162s）。同进程跑，Web 的响应延迟跟着内核的磁盘抖动一起抖。
2. **stdout 会挂死。** 内核各步骤往 stdout 大量回显；如果 Web 端 stdout 接的是
   没人读的管道，64KB 缓冲写满即阻塞 —— 现象是「中间件文件全写完了但步骤永不返回」
   （C1 验收时实测挂死 9 分钟）。拆进程后两边 stdout 各归各的，这个坑从根上消失。
3. **崩一起崩。** 内核被 OOM / 段错误带走时，Web 不该跟着陪葬。

拆开之后 Web 与 worker 只通过**两个东西**通信，都是共享文件：

    queue.db    谁该跑什么、谁跑完了（跨进程原子认领，见 web/store.py）
    logs/*.log  跑到哪了（见 web/logs.py）

用法::

    python -m web.worker_main                          # 一直跑，poll 队列
    python -m web.worker_main --once                   # 把现有队列跑完就退（测试/cron）
    python -m web.worker_main --id gpu-01:99           # 指定身份
    python -m web.worker_main --prune-days 30          # 启动时顺手清 30 天前的提交记录

环境变量（与 ``web/app.py`` 同名，默认值也一致 —— 两边必须看到同一个队列）::

    VOC_WEB_STATE_DIR   默认 <platform>/web_state
    VOC_WEB_RUNS_DIR    默认 <platform>/runs

停止
----

``SIGINT``（**Ctrl+C**）与 ``SIGTERM`` 都是**优雅退出**：置停止标志 → 当前这个 job
跑完 → 退。不打断正在跑的 job —— 内核没有协作式中断点，硬杀只会留下半截中间文件。

> Windows 上 ``Popen.terminate()`` 走的是 ``TerminateProcess``，**杀不掉也拦不住**
> （收不到可捕获的信号）。这没关系：worker 一死心跳就停，30s 后
> ``reap_stale`` 会把它的 running 项回收成 ``interrupted``，重跑即从断点续跑。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
if str(PLATFORM) not in sys.path:
    sys.path.insert(0, str(PLATFORM))

from web.logs import log_dir  # noqa: E402
from web.store import Queue  # noqa: E402
from web.worker import Worker, default_worker_id  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="web.worker_main",
        description="VOC 洞察平台 · 独立 worker 进程（串行跑 job）")
    ap.add_argument("--state-dir",
                    default=os.environ.get("VOC_WEB_STATE_DIR") or str(PLATFORM / "web_state"))
    ap.add_argument("--runs-dir",
                    default=os.environ.get("VOC_WEB_RUNS_DIR") or str(PLATFORM / "runs"))
    ap.add_argument("--id", default="", help=f"worker 身份（默认 {default_worker_id()}）")
    ap.add_argument("--once", action="store_true", help="把现有队列跑完就退出")
    ap.add_argument("--poll", type=float, default=1.0, help="空队列时的轮询间隔（秒）")
    ap.add_argument("--heartbeat", type=float, default=None, help="心跳间隔（秒），默认 10")
    ap.add_argument("--prune-days", type=float, default=0.0,
                    help="启动时清掉 N 天前**已结束**的提交记录（0 = 不清）")
    args = ap.parse_args(argv)

    state_dir = Path(args.state_dir)
    runs_dir = Path(args.runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    queue = Queue(state_dir)
    if args.prune_days > 0:
        n = queue.prune(older_than_days=args.prune_days)
        print(f"[worker] 已清理 {n} 条 {args.prune_days:.0f} 天前的提交记录", flush=True)

    kw = {}
    if args.heartbeat:
        kw["heartbeat_s"] = args.heartbeat
        kw["stale_s"] = max(30.0, 3 * args.heartbeat)

    worker = Worker(queue, runs_dir, worker_id=args.id or default_worker_id(),
                    log_dir=log_dir(state_dir), once=args.once, poll_s=args.poll, **kw)

    def _graceful(signum: int, _frame: object) -> None:
        print(f"\n[worker] 收到信号 {signum}：当前任务跑完后退出", flush=True)
        worker.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _graceful)
        except (ValueError, OSError, AttributeError):  # pragma: no cover
            pass  #: 非主线程 / 平台不支持该信号 —— 只是失去优雅退出，不影响功能

    print(f"[worker] 启动 {worker.worker_id}", flush=True)
    print(f"[worker]   state_dir={state_dir}", flush=True)
    print(f"[worker]   runs_dir ={runs_dir}", flush=True)
    print(f"[worker]   once={args.once} poll={args.poll}s heartbeat={worker.heartbeat_s}s"
          f" stale={worker.stale_s}s", flush=True)

    worker.run()  #: 直接跑在主线程：信号处理器才有机会执行

    print("[worker] 退出", flush=True)
    print(f"[worker] 期间回收的过期任务：{worker.reaped_at_startup or '无'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
