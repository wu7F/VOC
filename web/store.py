#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web/store.py · 任务队列（SQLite，**跨进程**）。

C2 相对 C1 改了什么，以及为什么
===============================

C1 的队列是「一个 ``queue.json`` + 一把 ``threading.RLock``」。那个设计在
**单进程**下是对的，而且刻意不引入数据库（理由见 C1 的 README：``job.json`` 已是权威
状态，不该给同一个事实造第二个副本）。

C2 要**独立 worker 进程**，那个设计立刻失效 —— 两处硬伤：

1. ``threading.RLock`` **跨进程无效**。两个进程同时「读 JSON → 改 → 写回」，
   后写的会把先写的整个覆盖掉（丢任务，或者同一个 job 被两个 worker 同时跑）。
2. ``queue.json`` 的每次写入都要走 ``atomic_replace``（写临时文件 → ``os.replace``），
   而本机 ``os.replace`` 在工作区内实测单次最慢 **11.68s**（Stage 9 咬出的真故障）。
   SQLite 的 rollback journal **提交时不做改名**（只写日志文件再落库），天然绕开这一条。

所以换成标准库 ``sqlite3``（**仍然零第三方依赖**）。它一次解决三件事：
跨进程原子性、免改名提交、以及「谁活着」这个新问题。

为什么不用 WAL（**这条是量出来的，不是拍脑袋**）
----------------------------------------------

WAL 在所有资料里都是「高并发」的推荐答案，但在这台机器 + 这个访问形状下它**是错的**。
用 ``tools/_probe_sqlite_mode.py`` 压 8 线程 × 40 job × 3 轮::

    journal_mode  连法      首轮认领            异常
    WAL           短连接    40/40               6 个（**3/3 轮都炸**）
    WAL           长连接    40/40               6 个（**3/3 轮都炸**）
    DELETE        短连接    40/40               0
    TRUNCATE      短连接    40/40               0

异常全是 ``sqlite3.OperationalError: attempt to write a readonly database``，
且**每一种组合里都发生在每个线程的第 1 次认领**（记录到的下标恒为 1）——
即多个连接**同时首次打开**一个 WAL 库、争着初始化 ``-shm``/``-wal`` 的那一刻。
先串行「预热」一条连接也没用（异常数 6/6 → 2/7/6），所以它不是「进程内第一次」的问题，
而是「并发首次打开」的问题。

再量单次认领+提交的延迟（250 次，DELETE 与 TRUNCATE 各跑一遍）::

    journal_mode   中位     p95     最大
    DELETE         58.5ms   111.5ms 295.0ms     ← 最快、尾巴最短
    TRUNCATE       63.7ms    86.4ms 659.2ms
    WAL           104.1ms   248.3ms 675.0ms

结论：**用 SQLite 的默认（rollback journal + ``synchronous=FULL``）**，
一条 ``journal_mode`` / ``synchronous`` 的 PRAGMA 都不设。

顺带记一个**被数据推翻的推理**：原本以为「TRUNCATE 把日志文件截零、不像 DELETE 那样
反复建删文件，应该更稳」—— 本机对「建文件/删文件」确实很敏感（Stage 9 的
``os.replace`` 11.68s 就是这么来的），但实测 DELETE 的中位与最大延迟都**更低**。
所以选型以实测为准。

代价说清楚：rollback journal 模式下读会挡写（反之亦然）。本队列的写频率是
「提交一次 + 每 10s 一次心跳」，单次 ~60ms，占用率远低于 1%，完全不成为瓶颈；
``busy_timeout`` 也兜住了并发争用。

三个刻意的设计选择
------------------

**1. 原子认领靠「``BEGIN IMMEDIATE`` + 守卫式 UPDATE」，不靠应用层读改写。**::

       BEGIN IMMEDIATE;                        -- 立刻拿写锁，两个进程在此排队
       SELECT ... WHERE state='queued' LIMIT 1;
       UPDATE ... SET state='running' WHERE job_id=? AND state='queued';
       COMMIT;

   ``UPDATE`` 上的 ``AND state='queued'`` 不是多余的 —— 它是**最后一道保险**：
   即使将来有人把 ``BEGIN IMMEDIATE`` 改坏（或换回默认的延迟事务），
   ``rowcount != 1`` 也能让这次认领失败退出，而不是把同一个 job 发出去两次。
   判据见 ``tests/test_web_queue.py::TestAtomicClaim``。

**2. 队列从「只记 pending」扩成「提交账本」（submission ledger）。**
C1 跑完就从队列里删掉（状态交给 ``job.json``）。C2 **必须留痕**：多租户要记
``owner``，而 owner 必须在 job 完成之后还能查到 —— 否则跑完的任务谁都认不出是谁的。
但这**没有**推翻 C1 的「单一事实源」原则，区别只是「留什么」：

============================ ==========================================
``jobs.state``               谁说了算
============================ ==========================================
``queued`` / ``running`` /    队列（``job.json`` 这时还不存在）
``interrupted`` / ``failed``
``done``                      **``job.json``** —— 队列行不参与状态合成
============================ ==========================================

``web.app._ui_status`` 已经天然满足这条（它只认 queued/running/interrupted/failed，
其余一律落到 ``job.json``）。这让「状态是算出来的，不是记出来的」在 C2 继续成立。

**3. 存活判定靠心跳，不靠 ``is_alive()``。**
worker 拆成独立进程后，Web 进程再也没有一个线程对象可以问 ``is_alive()``。
改为 ``workers`` 表 + 心跳时间戳：worker 每 ``heartbeat_s`` 秒刷新一次自己（以及
当前在跑的 job），Web 侧把「心跳新鲜的 worker」当成活着。

这同时解决了**崩溃回收**：C1 靠 worker 启动时把所有 ``running`` 标成 ``interrupted``，
但那样多 worker 会互相误伤。现在 ``reap_stale`` 只回收**心跳过期**的行 ——
正在干活的同伴不会被碰。

迁移
----

首次打开时若同目录存在 C1 的 ``queue.json`` 且新表为空，会导入旧数据并把文件改名为
``queue.json.migrated``（**不删**，留证）。已经在跑的 job 会以 ``queued`` 重新入队 ——
内核自带断点续跑（Stage 7），重跑同一条命令即从断点继续。
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

#: 队列项状态。注意与 ``job.json`` 的 ``status`` **不是同一套** ——
#: 这里只描述「提交生命周期」，job 内部的七步状态仍由 job.json 说了算。
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_FAILED = "failed"
STATE_INTERRUPTED = "interrupted"
STATE_DONE = "done"

#: 还没结束的状态（这些才参与界面状态合成）
LIVE_STATES = (STATE_QUEUED, STATE_RUNNING, STATE_FAILED, STATE_INTERRUPTED)

PUBLIC_STATES = (STATE_QUEUED, STATE_RUNNING, STATE_FAILED, STATE_INTERRUPTED, STATE_DONE)

#: 默认的心跳过期阈值：worker 每 10s 刷一次，90s 没动静就认定它死了
DEFAULT_STALE_S = 90.0
#: Web 侧判定「这个 worker 活着」的新鲜度
DEFAULT_FRESH_S = 45.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT    NOT NULL UNIQUE,
    spec         TEXT    NOT NULL DEFAULT '{}',
    owner        TEXT    NOT NULL DEFAULT '',
    state        TEXT    NOT NULL,
    priority     INTEGER NOT NULL DEFAULT 0,
    attempts     INTEGER NOT NULL DEFAULT 0,
    submitted_at TEXT    NOT NULL DEFAULT '',
    started_at   TEXT    NOT NULL DEFAULT '',
    finished_at  TEXT    NOT NULL DEFAULT '',
    heartbeat_at TEXT    NOT NULL DEFAULT '',
    worker       TEXT    NOT NULL DEFAULT '',
    exit_code    INTEGER,
    error        TEXT    NOT NULL DEFAULT ''
);
-- 认领时的取件顺序完全由这个索引决定（priority 降序 → seq 升序）
CREATE INDEX IF NOT EXISTS ix_jobs_pick ON jobs(state, priority DESC, seq);
-- 列表按 owner 过滤（多租户）
CREATE INDEX IF NOT EXISTS ix_jobs_owner ON jobs(owner, seq DESC);

CREATE TABLE IF NOT EXISTS workers (
    worker_id    TEXT PRIMARY KEY,
    host         TEXT NOT NULL DEFAULT '',
    pid          INTEGER NOT NULL DEFAULT 0,
    started_at   TEXT NOT NULL DEFAULT '',
    heartbeat_at TEXT NOT NULL DEFAULT '',
    current_job  TEXT NOT NULL DEFAULT '',
    jobs_done    INTEGER NOT NULL DEFAULT 0,
    jobs_failed  INTEGER NOT NULL DEFAULT 0
);
"""

#: 每个进程只在第一次打开某个库时建表（``CREATE TABLE IF NOT EXISTS`` 也要走一次写事务）
_SCHEMA_READY: set[str] = set()
_SCHEMA_LOCK = threading.Lock()


class QueueUnavailable(Exception):
    """队列库不可用（坏库隔离后仍打不开）。

    **读操作**会按空值降级；**写操作**必须让它穿出去 ——
    让任务「以为排在队里、其实从没进去过」比报错更糟。
    """


def _now() -> str:
    """秒级 ISO 时间。**刻意用 ``T`` 分隔** —— 字典序即时间序，可直接在 SQL 里比大小。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _ts_ago(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - seconds))


class Queue:
    """SQLite 提交队列。**可被多个进程同时安全使用。**

    :param state_dir: 放 ``queue.db`` 的目录（通常是 ``<platform>/web_state/``）
    :param busy_timeout_s: SQLite 拿不到锁时的等待上限（超了抛 ``sqlite3.OperationalError``）。

        **这个值必须远小于「服务看起来挂了」的阈值。** 单机争用量出来是几十毫秒
        （见 ``tools/_probe_sqlite_mode.py``），给它 15s 毫无收益，代价却是
        ``GET /api/health`` 原地等 15s —— 探活 3s 超时的客户端直接判「服务没起来」，
        而它其实只是排队等一把马上就放的锁。这是 Stage 9 那条教训的同一面：
        **请求路径上的阻塞，不管因为什么，都会表现为服务不可用。**
    """

    def __init__(self, state_dir: str | Path, *,
                 db_name: str = "queue.db",
                 busy_timeout_s: float = 2.0,
                 migrate_json: bool = True) -> None:
        self.dir = Path(state_dir)
        self.db_name = db_name
        self.path = self.dir / db_name
        self.busy_timeout_s = float(busy_timeout_s)
        self._migrate_json = migrate_json
        #: 兼容 C1 的调用方（``tests`` 里用过 ``q.path``）—— 那个 ``path`` 现在指向 DB。
        self.json_legacy = self.dir / "queue.json"
        #: 建表 + 捡起 C1 的 queue.json，**提前到构造时**做一次，别让第一个 HTTP 请求
        #: 去付这笔写事务的钱。失败不算数 —— 请求路径上还会再试（见 _ensure_schema）。
        #: 迁移也放这里：``_ensure_schema`` 有「表在就直接返回」的快路径，
        #: 它不能顺带负责捡旧文件（同一个进程里第二个 Queue 实例就捡不到了）。
        try:
            with self._conn() as conn:
                self._ensure_schema(conn)
                if self._migrate_json:
                    self._import_legacy_json(conn)
        except Exception:  # noqa: BLE001
            pass

    # -- 连接 --------------------------------------------------------------
    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """短连接：每次操作开一个、用完就关。

        为什么不复用长连接：**跨进程**场景下连接不能共享；而且本机的写操作频率很低
        （提交一次 / 每 10s 一次心跳），开连接的开销完全不是瓶颈，
        换来的是「不会拿着一个陈旧的快照读到别的进程已经改过的行」。
        """
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    def _open(self) -> sqlite3.Connection:
        """开一条**验过得能用**的连接：建库 → 试探着读一次 → 不行就隔离坏库重试。

        为什么要「试探着读一次」：``sqlite3.connect`` 是**惰性**的 —— 文件根本不是
        数据库（被截断、被别的程序覆盖、落盘写到一半断电）时它照样返回成功，要等到
        第一次真正查询才抛 ``DatabaseError: file is not a database``。

        不处理会怎样：这个异常会从 ``queue.list()`` 一路穿出去，**整个 Web 起不来**
        （``GET /api/health`` 都要调队列）。它正是 C1 那条判据
        「``queue.json`` 烂了也不能让 Web 起不来」想守的东西 —— 换了存储以后这条
        判据**依然成立**，所以这里必须接住。
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            conn = sqlite3.connect(str(self.path),
                                   timeout=self.busy_timeout_s,
                                   isolation_level=None)  #: 自己管事务
            conn.row_factory = sqlite3.Row
            try:
                conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_s * 1000)}")
                #: 这一步才真正去读文件头 —— 坏库在这里现形
                conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
                self._ensure_schema(conn)
                return conn
            except sqlite3.DatabaseError as exc:
                conn.close()
                if attempt or not self._quarantine_corrupt():
                    raise QueueUnavailable(
                        f"队列库打不开（已重试）：{self.path} —— {exc}") from exc
        raise QueueUnavailable(f"队列库打不开：{self.path}")

    def _quarantine_corrupt(self) -> bool:
        """把打不开的库**挪开**而不是删掉，然后让出一个新库的位置。

        挪而不是删：写到一半断电出来的库里可能还有值得捞的提交记录，而且它是
        一次事故的**证据**。挪到 ``queue.db.corrupt-<时间戳>``，服务立刻可用，
        数据留给人去查。

        :returns: 是否成功腾出了位置（False = 压根没能rename，比如权限问题）
        """
        try:
            if not self.path.exists():
                return False
        except OSError:
            return False
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            #: 这条 **os.replace** 落在罕见的恢复路径上（不是每次写都走），
            #: 即使在本机偶尔慢到秒级也无所谓 —— 前提是不能让服务起不来。
            os.replace(self.path, dest)
        except OSError as exc:
            print(f"[store] 坏库挪不走（{exc}）：{self.path}", file=sys.stderr, flush=True)
            return False
        print(f"[store] 队列库不是有效的 SQLite，已隔离到 {dest.name}；"
              "用空库继续（原文件保留可查）", file=sys.stderr, flush=True)
        with _SCHEMA_LOCK:
            _SCHEMA_READY.discard(str(self.path.resolve()))
        return True

    def _quiet(self, default: Any, fn: Callable[[], Any], what: str) -> Any:
        """读操作的兜底：库坏了就把界面饿着，但**别把服务拖下水**。

        只在**读**上用。写操作（认领/标记失败/记账）宁可失败也不能降级 ——
        静默降级会让人以为任务排在队里、其实从没进去过。
        """
        try:
            return fn()
        except (sqlite3.DatabaseError, QueueUnavailable) as exc:
            print(f"[store] 读 {what} 失败，按空值降级：{exc}", file=sys.stderr, flush=True)
            return default

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """建表。**整个初始化都在进程内的锁里**，一个进程只做一次。

        这里**刻意不设任何 journal_mode / synchronous** —— 用 SQLite 自己的默认值
        （rollback journal + `synchronous=FULL`）。这不是偷懒，是量出来的：见下面
        「为什么不用 WAL」。
        """
        key = str(self.path.resolve())
        with _SCHEMA_LOCK:
            if key in _SCHEMA_READY:
                return
            #: **先看一眼表在不在 —— 这是读，不是写。** 别一上来就 executescript：
            #: ``CREATE TABLE IF NOT EXISTS`` 也是**写事务**，而每个新连接都要走一遍
            #: 这里。C2 验收就栽在它上面 —— 服务刚起、库还是空的，探活脚本每 0.5s
            #: 打一次 ``/api/health``，线程池里几十个请求同时挤进 executescript
            #: 互相等锁，``/api/health`` 40s 不通（表现是「服务没起来」，其实它活着）。
            try:
                have = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='jobs'").fetchone()
            except sqlite3.DatabaseError:
                have = None
            if have is not None:
                _SCHEMA_READY.add(key)
                return
            last: Exception | None = None
            for attempt in range(6):
                try:
                    conn.executescript(_SCHEMA)
                    if self._migrate_json:
                        self._import_legacy_json(conn)
                    last = None
                    break
                except sqlite3.OperationalError as exc:  # 别的进程正在开同一个新库
                    last = exc
                    time.sleep(0.05 * (attempt + 1))
            if last is not None:
                raise last
            _SCHEMA_READY.add(key)

    def _import_legacy_json(self, conn: sqlite3.Connection) -> None:
        """把 C1 的 ``queue.json`` 一次性导入（只在表为空时）。原文件改名保留。"""
        src = self.json_legacy
        if not src.exists():
            return
        n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        if n:
            return
        try:
            items = json.loads(src.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 —— 旧文件烂了就跳过，别拦着服务启动
            return
        if not isinstance(items, list):
            return
        for e in items:
            if not isinstance(e, dict) or not e.get("job_id"):
                continue
            conn.execute(
                "INSERT OR IGNORE INTO jobs (job_id, spec, owner, state, priority,"
                " attempts, submitted_at, started_at, finished_at, error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(e["job_id"]),
                 json.dumps(e.get("spec") or {}, ensure_ascii=False),
                 str(e.get("owner") or ""),
                 #: C1 里 ``done`` 的项是被删掉的；若真有残留就当已完成，不再重跑
                 str(e.get("state") or STATE_QUEUED),
                 0, 0,
                 str(e.get("submitted_at") or _now()),
                 str(e.get("started_at") or ""),
                 str(e.get("finished_at") or ""),
                 str(e.get("error") or "")))
        try:
            src.replace(self.dir / "queue.json.migrated")
        except OSError:
            pass

    # -- 读 ----------------------------------------------------------------
    #: 这几个读方法的实现都搬到 ``_xxx_body`` 里，外层由 ``_quiet`` 包一层降级。
    #: 理由见 :meth:`_quiet` —— 队列坏了界面可以空着，**服务不能跟着起不来**。
    def list(self, *, owner: str | None = None) -> list[dict[str, Any]]:
        """全部队列行（新的在前）。``owner`` 给了就只返回那个 owner 的。

        C2：跑完的行**留着**（提交账本，要记 owner），所以这里也会返回 ``done`` 的行。
        界面状态合成（:func:`web.app._ui_status`）会跳过它们 —— 跑完以后状态一律
        归 ``job.json`` 管。
        """
        return self._quiet([], lambda: self._list_body(owner=owner), "队列列表")

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self._quiet(None, lambda: self._get_body(job_id), f"任务 {job_id}")

    def pending_ids(self) -> set[str]:
        return self._quiet(set(), self._pending_ids_body, "未完成任务集合")

    def stats(self) -> dict[str, int]:
        return self._quiet({s: 0 for s in PUBLIC_STATES} | {"total": 0},
                           self._stats_body, "队列计数")

    def _list_body(self, *, owner: str | None = None) -> list[dict[str, Any]]:
        with self._conn() as c:
            if owner is None:
                rows = c.execute("SELECT * FROM jobs ORDER BY seq DESC").fetchall()
            else:
                rows = c.execute("SELECT * FROM jobs WHERE owner=? ORDER BY seq DESC",
                                 (owner,)).fetchall()
            return [_entry(r) for r in rows]

    def _get_body(self, job_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return _entry(r) if r else None

    def _pending_ids_body(self) -> set[str]:
        """**不含 ``done``** —— 跑完的项由 ``runs/<id>/job.json`` 提供状态。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT job_id FROM jobs WHERE state IN (?,?,?,?)",
                LIVE_STATES).fetchall()
            return {r["job_id"] for r in rows}

    def _stats_body(self) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state").fetchall()
        out = {s: 0 for s in PUBLIC_STATES}
        for r in rows:
            out[r["state"]] = int(r["n"])
        out["total"] = sum(out[s] for s in PUBLIC_STATES)
        return out

    # -- 写 ----------------------------------------------------------------
    def add(self, job_id: str, spec: dict[str, Any], *,
            owner: str = "", priority: int = 0) -> dict[str, Any]:
        with self._conn() as c:
            try:
                c.execute(
                    "INSERT INTO jobs (job_id, spec, owner, state, priority,"
                    " submitted_at) VALUES (?,?,?,?,?,?)",
                    (job_id, json.dumps(spec or {}, ensure_ascii=False), owner,
                     STATE_QUEUED, int(priority), _now()))
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"job_id 已存在：{job_id}") from exc
            r = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return _entry(r)

    def claim_next(self, *, worker_id: str = "") -> dict[str, Any] | None:
        """原子地取下一个 queued 项并标成 running。没有就返回 ``None``。

        跨进程安全：``BEGIN IMMEDIATE`` 立刻拿写锁，两个 worker 会在这里排队，
        而不是各自读到同一行。
        """
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute(
                    "SELECT job_id FROM jobs WHERE state=? "
                    "ORDER BY priority DESC, seq ASC LIMIT 1",
                    (STATE_QUEUED,)).fetchone()
                if row is None:
                    c.execute("ROLLBACK")
                    return None
                now = _now()
                cur = c.execute(
                    "UPDATE jobs SET state=?, started_at=?, heartbeat_at=?, worker=?,"
                    " attempts=attempts+1 WHERE job_id=? AND state=?",
                    (STATE_RUNNING, now, now, worker_id, row["job_id"], STATE_QUEUED))
                if cur.rowcount != 1:  # pragma: no cover —— 见文件头「设计选择 1」
                    c.execute("ROLLBACK")
                    return None
                e = c.execute("SELECT * FROM jobs WHERE job_id=?",
                              (row["job_id"],)).fetchone()
                c.execute("COMMIT")
                return _entry(e)
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    c.execute("ROLLBACK")
                raise

    def finish(self, job_id: str, *, exit_code: int) -> None:
        """跑完了 —— 行**留着**（提交账本，含 owner），状态交给 ``job.json``。"""
        with self._conn() as c:
            wid = _worker_of(c, job_id)
            cur = c.execute("UPDATE jobs SET state=?, finished_at=?, exit_code=?,"
                            " heartbeat_at='', error='' WHERE job_id=? AND state!=?",
                            (STATE_DONE, _now(), int(exit_code), job_id, STATE_DONE))
            #: 只在**真的发生状态跃迁**时计数 —— 重复 finish 不该把统计刷成两倍
            if cur.rowcount:
                self._bump(c, wid, "jobs_done")

    def fail(self, job_id: str, error: str, *, exit_code: int | None = None) -> None:
        """跑炸了 / 等待人工回填 —— 行留着，好让界面显示原因（**不自动重试**）。"""
        with self._conn() as c:
            wid = _worker_of(c, job_id)
            cur = c.execute("UPDATE jobs SET state=?, error=?, finished_at=?,"
                            " heartbeat_at='', exit_code=COALESCE(?, exit_code)"
                            " WHERE job_id=? AND state!=?",
                            (STATE_FAILED, error, _now(), exit_code, job_id, STATE_FAILED))
            if cur.rowcount:
                self._bump(c, wid, "jobs_failed")

    @staticmethod
    def _bump(c: sqlite3.Connection, worker_id: str, col: str) -> None:
        """给认领这个 job 的 worker 计数。

        用 UPSERT 而不是 UPDATE：worker 可能还没来得及登记心跳就被要求在极早期收尾
        （例如输入文件缺失，``run_job`` 秒退）—— 那时 ``workers`` 里还没有它的行，
        纯 UPDATE 会静默丢计数。
        """
        if not worker_id or col not in ("jobs_done", "jobs_failed"):
            return
        c.execute(
            "INSERT INTO workers (worker_id, started_at, heartbeat_at, " + col + ")"
            f" VALUES (?,?,?,1) ON CONFLICT(worker_id) DO UPDATE SET {col}={col}+1",
            (worker_id, _now(), _now()))

    def remove(self, job_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
            return cur.rowcount > 0

    def prune(self, *, older_than_days: float = 30.0) -> int:
        """删掉太久以前**已结束**的提交记录（只动账本，不碰 ``runs/``）。"""
        cutoff = _ts_ago(older_than_days * 86400)
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM jobs WHERE state NOT IN (?,?,?) AND finished_at != ''"
                " AND finished_at < ?",
                (STATE_QUEUED, STATE_RUNNING, STATE_INTERRUPTED, cutoff))
            return int(cur.rowcount or 0)

    # -- 心跳与存活 --------------------------------------------------------
    def heartbeat(self, worker_id: str, *, job_id: str = "",
                  host: str = "", pid: int = 0) -> None:
        """刷新 worker 自身心跳（``job_id`` 给了就同时刷新那个 job 的心跳）。"""
        now = _now()
        with self._conn() as c:
            c.execute(
                "INSERT INTO workers (worker_id, host, pid, started_at, heartbeat_at,"
                " current_job) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(worker_id) DO UPDATE SET"
                "   heartbeat_at=excluded.heartbeat_at,"
                "   current_job=excluded.current_job,"
                "   host=CASE WHEN excluded.host!='' THEN excluded.host ELSE host END,"
                "   pid =CASE WHEN excluded.pid!=0    THEN excluded.pid  ELSE pid  END",
                (worker_id, host, int(pid), now, now, job_id))
            if job_id:
                c.execute("UPDATE jobs SET heartbeat_at=? WHERE job_id=? AND state=?",
                          (now, job_id, STATE_RUNNING))

    def workers(self, *, fresh_s: float = DEFAULT_FRESH_S) -> list[dict[str, Any]]:
        """所有登记过的 worker + ``alive`` 判定（心跳是否新鲜）。"""
        return self._quiet([], lambda: self._workers_body(fresh_s=fresh_s), "worker 表")

    def _workers_body(self, *, fresh_s: float = DEFAULT_FRESH_S) -> list[dict[str, Any]]:
        cutoff = _ts_ago(fresh_s)
        with self._conn() as c:
            rows = c.execute("SELECT * FROM workers ORDER BY heartbeat_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["alive"] = bool(d.get("heartbeat_at") and d["heartbeat_at"] >= cutoff)
            out.append(d)
        return out

    def reap_stale(self, *, older_than_s: float = DEFAULT_STALE_S,
                   reason: str = "") -> list[str]:
        """把「``running`` 但心跳过期」的项标成 ``interrupted``（**不自动重跑**）。

        心跳是跨进程判定的关键：正在干活的 worker 会持续刷新 ``heartbeat_at``，
        所以只有**死掉的** worker 留下的行会被回收 —— 重启一个 worker 不会误伤同伴。
        """
        cutoff = _ts_ago(older_than_s)
        msg = reason or (f"worker 心跳超过 {older_than_s:.0f}s 未刷新（进程大概率已死）；"
                         "可重跑 —— 内核自带断点续跑")
        with self._conn() as c:
            #: 用 ``<=`` 而不是 ``<``：时间戳是**秒级**的。用 ``<`` 时
            #: ``older_than_s=0``（「全部回收」，测试与手动抢救都靠它）会取不到任何行 ——
            #: cutoff 正好等于心跳的那一秒。语义上「恰好 N 秒没刷新」**已经**过期了，
            #: 所以边界要算进去。对默认的 90s 窗口无副作用：活着的 worker 每 10s 刷一次，
            #: 实际心跳永远不会靠近 cutoff。
            rows = c.execute(
                "SELECT job_id FROM jobs WHERE state=? AND (heartbeat_at='' OR"
                " heartbeat_at <= ?)", (STATE_RUNNING, cutoff)).fetchall()
            ids = [r["job_id"] for r in rows]
            if ids:
                qs = ",".join("?" * len(ids))
                c.execute(f"UPDATE jobs SET state=?, error=?, finished_at=?,"
                          f" heartbeat_at='' WHERE job_id IN ({qs})",
                          (STATE_INTERRUPTED, msg, _now(), *ids))
            return ids

    def reap_interrupted(self, *, stale_s: float = DEFAULT_STALE_S) -> list[str]:
        """兼容 C1 的旧名（C1 无参把所有 running 都回收）。语义已升级为**按心跳回收**。"""
        return self.reap_stale(older_than_s=stale_s)

    # -- 旧接口兼容 --------------------------------------------------------
    def _read(self) -> list[dict[str, Any]]:  # C1 内部方法名，测试里用到过
        return self.list()

    def _write(self, items: list[dict[str, Any]]) -> None:  # pragma: no cover
        raise NotImplementedError("C2 起队列由 SQLite 管；请用 add/finish/fail/remove")


def _worker_of(c: sqlite3.Connection, job_id: str) -> str:
    r = c.execute("SELECT worker FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return (r["worker"] if r else "") or ""


def _entry(row: sqlite3.Row) -> dict[str, Any]:
    """DB 行 → 界面/调用方用的字典（``spec`` 解回 dict）。"""
    d = dict(row)
    try:
        d["spec"] = json.loads(d.get("spec") or "{}")
    except Exception:  # noqa: BLE001
        d["spec"] = {}
    return d
