#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/_probe_sqlite_mode.py · 哪种 journal_mode 能扛住「多线程短连接」？

背景
====
C2 的队列换成 SQLite 后，8 线程 × 短连接（每次操作开一个连接、用完就关）的压测里
``BEGIN IMMEDIATE`` 会随机报::

    sqlite3.OperationalError: attempt to write a readonly database

短连接是**刻意的**（跨进程场景不能共享连接；本机写频率很低，开连接的开销不是瓶颈）。
所以问题在 journal_mode 的选择，不在连接策略。

怀疑：WAL 模式下「最后一个连接关闭」会触发一次 checkpoint 并**删掉 -wal/-shm**，
而别的线程正在开/关/事务中 —— Windows 的文件删除语义让这个清理与使用撞在一起。

本探针把四种组合各压一遍，用数据决定，而不是猜：
  A. WAL      + 短连接（当前实现）
  B. DELETE   + 短连接（SQLite 默认）
  C. TRUNCATE + 短连接
  D. WAL      + 线程内长连接（永不关，-wal/-shm 不会被删）

用法::

    python tools/_probe_sqlite_mode.py --threads 8 --jobs 40 --rounds 3
"""

from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    worker TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_pick ON jobs(state, seq);
"""


def _claim_short(db: Path, mode: str) -> str | None:
    """短连接认领：开 → BEGIN IMMEDIATE → 取件 → 关。"""
    c = sqlite3.connect(str(db), timeout=15.0, isolation_level=None)
    try:
        c.execute("PRAGMA busy_timeout=15000")
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT job_id FROM jobs WHERE state='queued'"
                        " ORDER BY seq ASC LIMIT 1").fetchone()
        if row is None:
            c.execute("ROLLBACK")
            return None
        cur = c.execute("UPDATE jobs SET state='running' WHERE job_id=? AND state='queued'",
                        (row[0],))
        if cur.rowcount != 1:
            c.execute("ROLLBACK")
            return None
        c.execute("COMMIT")
        return row[0]
    except Exception:
        raise
    finally:
        c.close()


_TLS = threading.local()


def _claim_long(db: Path, mode: str) -> str | None:
    """线程内长连接认领（连接永不关；WAL 的 -wal/-shm 因此不会被删）。"""
    c = getattr(_TLS, "conn", None)
    if c is None:
        c = sqlite3.connect(str(db), timeout=15.0, isolation_level=None)
        c.execute("PRAGMA busy_timeout=15000")
        _TLS.conn = c
    c.execute("BEGIN IMMEDIATE")
    row = c.execute("SELECT job_id FROM jobs WHERE state='queued'"
                    " ORDER BY seq ASC LIMIT 1").fetchone()
    if row is None:
        c.execute("ROLLBACK")
        return None
    cur = c.execute("UPDATE jobs SET state='running' WHERE job_id=? AND state='queued'",
                    (row[0],))
    if cur.rowcount != 1:
        c.execute("ROLLBACK")
        return None
    c.execute("COMMIT")
    return row[0]


def run_case(label: str, mode: str, *, threads: int, jobs: int,
             rounds: int, long_conn: bool) -> dict:
    catch: list[str] = []
    setup_fail: list[str] = []
    for r in range(rounds):
        td = Path(tempfile.mkdtemp())
        db = td / "q.db"
        c = sqlite3.connect(str(db), isolation_level=None)
        c.execute(f"PRAGMA journal_mode={mode}")
        c.executescript(DDL)
        c.executemany("INSERT INTO jobs (job_id, state) VALUES (?, 'queued')",
                      [(f"j{i:03d}",) for i in range(jobs)])
        c.commit()
        #: 让「建库连接」退出 —— WAL 下这会触发一次清理，正是要压的地方
        c.close()

        got: list[str] = []
        errs: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(threads)
        fn = _claim_long if long_conn else _claim_short

        def body() -> None:
            barrier.wait()
            while True:
                try:
                    jid = fn(db, mode)
                except Exception:
                    with lock:
                        errs.append(traceback.format_exc(limit=2))
                    return
                if jid is None:
                    return
                with lock:
                    got.append(jid)

        ts = [threading.Thread(target=body) for _ in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

        cnt = collections.Counter(got)
        dups = sum(1 for v in cnt.values() if v > 1)
        catch.append(f"round{r}: 认领 {len(got)}/{jobs} 重复 {dups} 异常 {len(errs)}"
                     + (f"  ← {errs[0].strip().splitlines()[-1][:70]}" if errs else ""))
        if errs:
            setup_fail.append(errs[0])
        if _TLS.__dict__.get("conn"):
            _TLS.conn.close()
            _TLS.conn = None

    bad = sum(1 for line in catch if "异常 0" not in line)
    print(f"\n── {label} ──  （{mode}{' + 长连接' if long_conn else ' + 短连接'}）")
    for line in catch:
        print("   " + line)
    print(f"   结论：{'FAIL' if bad else 'OK'}（{bad}/{rounds} 轮出现异常）")
    return {"label": label, "mode": mode, "long_conn": long_conn,
            "bad_rounds": bad, "rounds": rounds, "detail": catch}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="journal_mode 选型探针")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--jobs", type=int, default=40)
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args(argv)

    print("=" * 78)
    print(f"journal_mode 选型：{args.threads} 线程 × {args.jobs} 个 job × {args.rounds} 轮")
    print("=" * 78)

    rows = [
        run_case("A 当前实现", "WAL", threads=args.threads, jobs=args.jobs,
                 rounds=args.rounds, long_conn=False),
        run_case("B 退默认", "DELETE", threads=args.threads, jobs=args.jobs,
                 rounds=args.rounds, long_conn=False),
        run_case("C 截断日志", "TRUNCATE", threads=args.threads, jobs=args.jobs,
                 rounds=args.rounds, long_conn=False),
        run_case("D WAL+长连接", "WAL", threads=args.threads, jobs=args.jobs,
                 rounds=args.rounds, long_conn=True),
    ]

    print("\n" + "=" * 78)
    print("汇总（异常轮数越少越稳）")
    print("=" * 78)
    for r in rows:
        print(f"  {r['label']:12s} {r['mode']:9s}"
              f"{'长连接' if r['long_conn'] else '短连接'}   "
              f"{'FAIL' if r['bad_rounds'] else 'OK  '}  {r['bad_rounds']}/{r['rounds']} 轮有异常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
