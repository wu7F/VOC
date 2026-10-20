#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web/worker.py · 串行 worker（可同进程、也可独立进程）。

为什么是串行
============

内核是**本地文件密集 + 长任务**的（单次 ``annotate`` 要给 7,662 条评论逐个写中间文件，
实测 162 秒），而且七步是严格链式的。并发跑多个 job 会同时抢磁盘与 LLM 配额，
语义没验证过 —— Stage 7 的批量模式当初也是**只做串行**，理由相同。

所以这里就是一个队列、一次一个 job。

C2 相对 C1 加的三件事（都是为了能**跨进程**跑）
==============================================

**1. 心跳。** C1 靠 ``threading.Thread.is_alive()`` 判断 worker 在不在；拆成独立进程后
Web 侧再没有那个对象可以问。改为每 ``heartbeat_s`` 秒往 ``workers`` 表刷一次时间戳，
Web 侧按「心跳是否新鲜」判定存活。

心跳必须在**独立线程**里刷：一个 job 要跑 175 秒，主线程整个被 ``run_job`` 占住，
在主循环里刷 = 跑长任务时心跳必然过期 = 自己的任务被自己回收。

**2. 日志落盘。** C1 的日志是内存里的 ``deque``，另一个进程读不到。
改为写 ``<state_dir>/logs/<job_id>.log``（批量刷盘，见 :mod:`web.logs`）。

**3. 崩溃回收靠心跳过期，不靠「启动时清场」。** C1 是 worker 启动时把所有 ``running``
标成 ``interrupted``；多 worker 下这会互相误伤。现在 ``reap_stale`` 只看心跳，
而且**在循环里周期性地做**（不是只在启动时做一次）—— 否则「worker 刚崩、进程管理器
立刻重启」这个场景要等到下次重启才被回收。

退出码语义（**最容易做错的地方**）
==================================

``run_job`` 的退出码不是「成功/失败」两分：

===== ============================================== ========================
退出码 含义                                             界面应该显示
===== ============================================== ========================
0     全部步骤跑完（闸门可能是 pass，也可能只是 warn）   完成
1     一般失败（某步报错）                              失败
2     输入 / 配置错误                                   失败（配置问题）
3     **等待人工回填**（LLM 兜底队列）—— 不是失败        待办
===== ============================================== ========================

另外 ``validate``（闸门）退出码 1 表示「这批产物有问题」，而**不是**「执行失败」——
它不阻断整体（Stage 7 定的），所以 job 状态仍是 ``done``、闸门结论单独记。

worker 的职责就是**把这套语义原样保留**、不压成一个 boolean。
"""

from __future__ import annotations

import os
import re
import socket
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any, Callable

from web.logs import WorkerLog

#: 保留多少行日志给**同进程**调用方（独立进程时界面读的是日志文件）
LOG_RING = 200

#: 心跳间隔（秒）
HEARTBEAT_S = 10.0
#: 心跳过期阈值 —— worker 连续错过这么多次心跳就认定它死了。
#: 取 3 倍心跳间隔：既能容忍偶发的调度延迟，又能让「崩溃后重启」在 ~30s 内被回收。
#: **必须 >= 3 × HEARTBEAT_S**，否则一个正常的 worker 会因为抖动被自己的同伴回收。
STALE_S = 30.0
#: 主循环里多久扫一次「心跳过期的 running 项」
REAP_EVERY_S = 30.0

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, fallback: str = "job", limit: int = 32) -> str:
    """把品类名变成文件名安全的片段。中文品类会退化成 fallback（文件名仍唯一，靠时间戳）。"""
    s = _SLUG_RE.sub("-", (text or "").strip().lower()).strip("-")
    if not s:
        s = fallback
    return s[:limit]


def new_job_id(category: str, *, now: float | None = None) -> str:
    """``<日期>-<时分秒>-<品类 slug>`` —— 时间戳保证唯一，slug 只为好认。"""
    t = time.localtime(now if now is not None else time.time())
    stamp = time.strftime("%Y%m%d-%H%M%S", t)
    return f"{stamp}-{slugify(category, fallback='analysis')}"


def default_worker_id() -> str:
    """``<主机名>:<pid>`` —— 跨机器/跨进程都唯一，出问题一眼看得出是谁。"""
    return f"{socket.gethostname()}:{os.getpid()}"


class Worker(threading.Thread):
    """串行 worker：``claim_next()`` → ``run_job()`` → 记账。

    既可以被 Web 进程以线程方式启动（``--worker inproc``），
    也可以通过 :mod:`web.worker_main` 跑成独立进程（**C2 的默认**）。

    :param queue: :class:`web.store.Queue`
    :param runs_dir: job 落地目录（``runs/``）
    :param worker_id: 身份标识，写进 ``jobs.worker`` / ``workers.worker_id``
    :param log_dir: 日志目录（``<state_dir>/logs``）；``None`` 则只留内存环形缓冲
    :param on_event: 可选回调 ``(event: str, payload: dict)``，同进程模式给界面做事件流
    :param once: 把队列里现有的 job 跑完就退出（给测试 / cron 用）
    """

    def __init__(self, queue: Any, runs_dir: str | Path, *,
                 worker_id: str = "",
                 log_dir: str | Path | None = None,
                 on_event: Callable[[str, dict], None] | None = None,
                 poll_s: float = 1.0,
                 heartbeat_s: float = HEARTBEAT_S,
                 stale_s: float = STALE_S,
                 once: bool = False) -> None:
        super().__init__(name="voc-worker", daemon=True)
        if stale_s < 3 * heartbeat_s:
            raise ValueError(
                f"stale_s({stale_s}s) 必须 >= 3 × heartbeat_s({heartbeat_s}s)："
                "否则一个正常干活的 worker 会因为一次调度抖动被同伴回收")
        self.queue = queue
        self.runs_dir = Path(runs_dir)
        self.worker_id = worker_id or default_worker_id()
        self.log_dir = Path(log_dir) if log_dir else None
        self.on_event = on_event or (lambda *a, **k: None)
        self.poll_s = float(poll_s)
        self.heartbeat_s = float(heartbeat_s)
        self.stale_s = float(stale_s)
        self.once = once

        self._stop = threading.Event()
        self._logs: dict[str, deque[str]] = {}
        self._lock = threading.Lock()
        self._results: dict[str, dict[str, Any]] = {}
        #: 当前正在跑的 job（心跳线程要替它刷心跳）
        self._current = ""
        self._hb_done = threading.Event()
        self._reaped: list[str] = []

    # -- 给同进程调用方的读取口 --------------------------------------------
    def log_lines(self, job_id: str, limit: int = 40) -> list[str]:
        """内存环形缓冲（**只对同进程有意义**；独立进程请读日志文件）。"""
        with self._lock:
            buf = self._logs.get(job_id)
            return list(buf)[-limit:] if buf else []

    def result_of(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            r = self._results.get(job_id)
            return dict(r) if r else None

    @property
    def reaped_at_startup(self) -> list[str]:
        """启动后第一次心跳扫描回收掉的 job（诊断用）。"""
        return list(self._reaped)

    # -- 心跳 --------------------------------------------------------------
    def _heartbeat_loop(self) -> None:
        while not self._hb_done.wait(self.heartbeat_s):
            if self._stop.is_set():
                return
            try:
                self.queue.heartbeat(self.worker_id, job_id=self._current,
                                     host=socket.gethostname(), pid=os.getpid())
            except Exception:  # noqa: BLE001 —— 心跳失败不该拖垮正在跑的分析
                pass

    def heartbeat_once(self) -> None:
        """同步刷一次心跳（登记自身；长任务开始/结束时调用）。"""
        try:
            self.queue.heartbeat(self.worker_id, job_id=self._current,
                                 host=socket.gethostname(), pid=os.getpid())
        except Exception:  # noqa: BLE001
            pass

    # -- 主循环 ------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()
        self._hb_done.set()

    def run(self) -> None:  # noqa: D102
        #: 先登记自己再回收 —— 否则冷启动时可能把自己刚认领的行当成「过期」清掉
        self.heartbeat_once()
        try:
            self._reaped = self.queue.reap_stale(older_than_s=self.stale_s)
        except Exception:  # noqa: BLE001
            self._reaped = []
        if self._reaped:
            self.on_event("reaped", {"job_ids": self._reaped})

        hb = threading.Thread(target=self._heartbeat_loop, name="voc-heartbeat",
                              daemon=True)
        hb.start()
        last_reap = time.monotonic()
        try:
            while not self._stop.is_set():
                entry = self.queue.claim_next(worker_id=self.worker_id)
                if entry is None:
                    if self.once:
                        break
                    #: 周期性回收「心跳过期的 running」—— 不能只在启动时做一次：
                    #: 「worker 刚崩、进程管理器立刻重启」时，那条 running 行还「很新」，
                    #: 启动时扫不到它，必须等它过期后再扫一次。
                    if time.monotonic() - last_reap >= REAP_EVERY_S:
                        last_reap = time.monotonic()
                        self._reap_now()
                    self._stop.wait(self.poll_s)
                    continue
                self._run_one(entry)
        finally:
            self._hb_done.set()
            hb.join(timeout=2.0)
            self._current = ""
            self.heartbeat_once()

    def _reap_now(self) -> list[str]:
        try:
            hit = self.queue.reap_stale(older_than_s=self.stale_s)
        except Exception:  # noqa: BLE001
            return []
        if hit:
            self._reaped.extend(hit)
            self.on_event("reaped", {"job_ids": hit})
        return hit

    def _run_one(self, entry: dict[str, Any]) -> None:
        from voc_core.pipeline import JobSpec, PipelineError, run_job

        job_id = entry["job_id"]
        job_dir = self.runs_dir / job_id
        spec = JobSpec.from_dict(entry.get("spec") or {})
        self._current = job_id

        wlog = WorkerLog(self.log_dir / f"{job_id}.log") if self.log_dir else None
        t0 = time.time()
        try:
            self._emit(job_id, f"开始 —— {job_id}（worker {self.worker_id}）", wlog)
            if wlog:
                wlog.flush()

            def log(*args: Any, **kwargs: Any) -> None:  # noqa: ANN401
                self._emit(job_id, " ".join(str(a) for a in args), wlog)

            try:
                result = run_job(spec, job_dir, log=log)
            except PipelineError as exc:  # 配置错误（文件缺失 / 识别不出源）→ 退出码 2
                msg = f"配置错误：{exc}"
                self._emit(job_id, f"配置错误 —— {exc}", wlog)
                self._emit(job_id, traceback.format_exc(limit=3), wlog)
                self.queue.fail(job_id, msg, exit_code=2)
                self._remember(job_id, status="failed", exit_code=2, error=msg,
                               elapsed_s=round(time.time() - t0, 2))
                self.on_event("failed", {"job_id": job_id, "error": msg})
                return
            except Exception as exc:  # noqa: BLE001 —— 任何异常都要落成界面能看的原因
                msg = f"{type(exc).__name__}: {exc}"
                self._emit(job_id, f"异常退出 —— {msg}", wlog)
                self._emit(job_id, traceback.format_exc(limit=3), wlog)
                self.queue.fail(job_id, msg)
                self._remember(job_id, status="failed", exit_code=1, error=msg,
                               elapsed_s=round(time.time() - t0, 2))
                self.on_event("failed", {"job_id": job_id, "error": msg})
                return

            code = int(result.exit_code)
            summary = {
                "status": result.status,
                "exit_code": code,
                "elapsed_s": round(result.elapsed_s or (time.time() - t0), 2),
                "waiting": list(result.waiting),
                "failed": list(result.failed),
                "skipped": list(result.skipped),
                "done": list(result.done),
            }
            self._emit(job_id, f"结束 —— status={result.status} exit={code} "
                               f"{summary['elapsed_s']}s", wlog)

            if code == 3:
                #: 等待人工回填 —— **不是失败**。队列项留着，界面显示成待办。
                self.queue.fail(job_id,
                                "等待人工回填（LLM 兜底队列）：回填后重跑同一条命令即续跑",
                                exit_code=3)
                self._remember(job_id, error="等待人工回填", **summary)
                self.on_event("waiting_manual", {"job_id": job_id, **summary})
            elif code != 0:
                self.queue.fail(job_id, f"管线返回退出码 {code}", exit_code=code)
                self._remember(job_id, error=f"退出码 {code}", **summary)
                self.on_event("failed", {"job_id": job_id, **summary})
            else:
                self.queue.finish(job_id, exit_code=code)
                self._remember(job_id, error="", **summary)
                self.on_event("done", {"job_id": job_id, **summary})
        finally:
            self._current = ""
            if wlog:
                wlog.close()

    # -- 内部 --------------------------------------------------------------
    def _emit(self, job_id: str, line: str, wlog: WorkerLog | None = None) -> None:
        with self._lock:
            buf = self._logs.setdefault(job_id, deque(maxlen=LOG_RING))
            for part in str(line).splitlines() or [""]:
                buf.append(part)
        if wlog is not None:
            wlog.write(line)

    def _remember(self, job_id: str, **kw: Any) -> None:
        with self._lock:
            self._results[job_id] = kw
