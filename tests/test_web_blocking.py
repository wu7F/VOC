#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 9 · 「阻塞 I/O 不许做在 asyncio 事件循环上」。

这是从一次**真实故障**里抽出来的不变量，不是假想的洁癖
=====================================================

现象
----

第一次 C1 端到端验收（``tools/_stage9_accept.py``）的日志里，提交后**第一次**状态
响应出现在 **119.6s**：::

    [ 119.6s] running 0/7
    [ 125.7s] running 1/7
    [ 301.2s] running 3/7
    [ 335.0s] done 7/7

定位
----

``tools/_p39_dump.py`` 用 ``faulthandler`` 定时把所有线程的调用栈打进日志，
一次就抓到了现场：::

    线程 0x0000c5dc（事件循环）
      web/store.py:192 in _write
      web/store.py:97  in add
      web/app.py:203   in create_job          ← 当时是 async def
      ... uvicorn h11_impl run_asgi ...
      voc_core/pipeline.py:1449 in atomic_replace   ← 卡在 os.replace 这一行

    线程 0x0000e098（worker）
      web/store.py:102 in claim_next          ← 等同一把 Queue._lock

即：``POST /api/jobs`` 声明成 ``async def``，里面却做着**同步阻塞的文件 I/O**。

放大它的两个本机因素
--------------------

1. ``os.replace`` 在工作区内会**秒级阻塞**：``tools/_p39_lockprobe2.py`` 实测
   20 次里最慢一次 **11.68s**，而同样的代码在系统临时目录里最慢只要 0.01s。
2. ``atomic_replace`` 会**重试 10 次**（退避合计 13.75s）。

两者相乘 ≈ 87 秒 —— 这与「handler 在 13:55:20 就把 6MB 写完了，客户端却到
13:56:47 才拿到响应」的观测完全吻合。而且因为卡的是**事件循环**，
期间连 ``GET /api/health`` 都超时：整个服务不可用。

修法
----

端点改成**同步 ``def``**，由 FastAPI 丢进线程池执行。阻塞的只是那个线程。

修复后的同一份验收（真实数据 + 真跑七步）::

    [   0.3s] queued 0/7      ← 提交 0.3s 就返回（原来 119.6s）
    [ 175.6s] done 7/7        ← 总耗时从 335.0s 降到 175.6s

本文件把这条不变量钉住：**任何 ``/api`` 端点都不许是协程函数**。
"""

from __future__ import annotations

import inspect
import sys
import unittest
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
if str(PLATFORM) not in sys.path:
    sys.path.insert(0, str(PLATFORM))

try:
    from web import app as APP
except Exception:  # noqa: BLE001 —— 没装 fastapi 就跳过（内核零依赖）
    APP = None


@unittest.skipIf(APP is None, "fastapi 不在本环境")
class TestNoBlockingOnEventLoop(unittest.TestCase):
    """``/api`` 端点必须都是同步 ``def`` —— 它们全都要读写本地磁盘。"""

    def _api_endpoints(self):
        for route in APP.app.routes:
            fn = getattr(route, "endpoint", None)
            path = getattr(route, "path", "") or ""
            if fn is None or not path.startswith("/api"):
                continue
            yield path, fn

    def test_every_api_endpoint_is_sync(self):
        bad = [f"{path} -> {fn.__name__}"
               for path, fn in self._api_endpoints()
               if inspect.iscoroutinefunction(fn)]
        self.assertEqual(
            bad, [],
            "这些端点会把阻塞 I/O 做在事件循环上（会冻住整个服务，应为同步 def）："
            + ", ".join(bad))

    def test_create_job_is_sync(self):
        self.assertFalse(
            inspect.iscoroutinefunction(APP.create_job),
            "create_job 又变回 async def 了 —— 它要落盘上传文件并调用 queue.add"
            "（→ atomic_replace → os.replace，本机实测单次可卡 ~12s、重试 10 次），"
            "一次提交就能冻住整个服务约 87 秒")

    def test_at_least_one_api_endpoint_seen(self):
        """防止上面的检查因为「一个端点都没匹配上」而空转通过。"""
        self.assertGreaterEqual(len(list(self._api_endpoints())), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
