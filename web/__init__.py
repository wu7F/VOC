#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web · Stage 9 C1 骨架：FastAPI 薄层 + 单进程串行 worker。

模块分工::

    app.py      HTTP 层（路由 / 上传 / 回吐报告）—— 不含任何分析逻辑
    store.py    提交队列（一个 queue.json，**无数据库**）
    worker.py   后台串行 worker —— 唯一调 pipeline.run_job 的地方
    static/     最小前端：上传 + 任务列表 + 报告 iframe

依赖：``fastapi`` / ``uvicorn`` / ``python-multipart``（``pip install -e ".[web]"``）。
**内核本身仍然零依赖** —— 这个包只是壳。
"""

from __future__ import annotations

__all__ = ["app", "store", "worker"]
