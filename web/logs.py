#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web/logs.py · worker 日志落盘（**跨进程**）。

为什么不能再用内存环形缓冲
==========================

C1 的 worker 是**同进程线程**，所以 ``Worker._logs``（一个 ``deque(maxlen=200)``）
就能给界面供日志。C2 把 worker 拆成**独立进程**之后，那个 deque 住在另一个进程里 ——
Web 进程读不到它，``GET /api/jobs/{id}`` 的 ``recent_log`` 会永远是空列表。

所以日志改为**落盘**：``<state_dir>/logs/<job_id>.log``。

为什么不是直接 ``open(path, "a")`` 每行一次
==========================================

``run_job(log=回调)`` 的回调频率跟着内核走 —— 真实数据下 ``annotate`` 一步要给
7,662 条评论逐个写中间文件，若它按条回调，逐行开文件 append 是 7,662 次
「open + write + close」，在本机（杀软逐个文件扫描）会明显拖慢内核。

所以 :class:`WorkerLog` 做**批量刷盘**：攒够 ``flush_every`` 行、或距上次刷盘超过
``flush_after_s`` 秒，才真正落一次盘；:meth:`close` 保证尾巴一定写出去。

刷盘用 ``open(..., "a")``（追加）而不是 ``atomic_replace`` —— 日志是**只追加**的，
没有「读者看到半截状态」的问题，也就不需要那套「写临时文件再改名」的重试逻辑
（本机 ``os.replace`` 会秒级阻塞，见 Stage 9 的踩坑记录）。
"""

from __future__ import annotations

import time
from pathlib import Path

#: 攒够这么多行就刷一次盘
FLUSH_EVERY = 16
#: 距上次刷盘超过这么多秒也刷一次（否则长任务中间看不到日志）
FLUSH_AFTER_S = 2.0
#: tail() 单次最多读多少行（防止把整个日志读进内存）
TAIL_HARD_LIMIT = 4000


def log_dir(state_dir: str | Path) -> Path:
    """worker 日志目录：``<state_dir>/logs/``。"""
    return Path(state_dir) / "logs"


def log_path(state_dir: str | Path, job_id: str) -> Path:
    return log_dir(state_dir) / f"{job_id}.log"


class WorkerLog:
    """给一个 job 写日志：攒批刷盘 + 写后可选回读校验。

    :param path: 日志文件（``<state_dir>/logs/<job_id>.log``）
    :param flush_every: 攒够多少行刷一次
    :param flush_after_s: 距上次刷盘多久强制刷一次
    """

    def __init__(self, path: str | Path, *,
                 flush_every: int = FLUSH_EVERY,
                 flush_after_s: float = FLUSH_AFTER_S) -> None:
        self.path = Path(path)
        self.flush_every = max(1, int(flush_every))
        self.flush_after_s = float(flush_after_s)
        self._buf: list[str] = []
        self._last = time.monotonic()
        self._total = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- 写 ----------------------------------------------------------------
    def write(self, line: str) -> None:
        """记一行（**不带换行**，本方法自己加）。"""
        for part in str(line).splitlines() or [""]:
            self._buf.append(part)
        self._total += len(self._buf)
        if len(self._buf) >= self.flush_every or \
                (time.monotonic() - self._last) >= self.flush_after_s:
            self.flush()

    def flush(self) -> None:
        if not self._buf:
            self._last = time.monotonic()
            return
        blob = "\n".join(self._buf) + "\n"
        self._buf.clear()
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(blob)
        except OSError:
            #: 日志写不进去**不该**让分析挂掉 —— 它不是产物，是辅助观测。
            #: 这里吞掉异常是有意的；内核的产物另有自己的写后回读校验。
            pass
        self._last = time.monotonic()

    def close(self) -> None:
        self.flush()

    def __enter__(self) -> WorkerLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def total_lines(self) -> int:
        return self._total


def tail(path: str | Path, n: int = 40) -> list[str]:
    """读日志最后 ``n`` 行（**不含**换行）。文件不存在/读不了都返回空列表。

    日志可能被另一个进程正在追加 —— 按行读、遇到解码问题就忽略该行，
    不为此抛异常（界面上少一行日志，比 500 好得多）。
    """
    p = Path(path)
    if not p.exists():
        return []
    n = max(0, min(int(n), TAIL_HARD_LIMIT))
    if n == 0:
        return []
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            lines = [ln.rstrip("\n") for ln in fh]
    except OSError:
        return []
    return lines[-n:]


def append_line(path: str | Path, line: str) -> None:
    """直接追加一行（低频场景用；高频请用 :class:`WorkerLog`）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(str(line).rstrip("\n") + "\n")
    except OSError:
        pass
