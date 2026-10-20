#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C2 · SQLite 队列的测试（CI 入口，毫秒级 / 秒级，不起服务）。

这里守的是 C1 → C2 **换掉的那个东西**：``queue.json`` + ``threading.RLock``。

C1 那个写法在单进程下是对的；到了「worker 跑在别人进程里」立刻失效，失效方式是
**静默的**（最糟的一种）：

    A 进程 read-modify-write  ─┐
                               ├→ 后写的把先写的**整个覆盖**（丢任务）
    B 进程 read-modify-write  ─┘

更隐蔽的是第二种：两个 worker 拿到**同一个** job —— 同一个 job 目录被并写，
落出来的看板是两份半截结果摞起来的。这种失效很难被发现：产物看起来「跑完了」，
只有顺着日志数一遍才会发现它跑了两遍。

最值得看的两个用例是 :class:`TestCrossProcessClaim` 与
:class:`TestReapSafety` —— 它们各自对应上面两种失效方式。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
if str(PLATFORM) not in sys.path:
    sys.path.insert(0, str(PLATFORM))

from web import store as S  # noqa: E402


def _raw_exec(db: Path, sql: str, params: tuple = ()) -> None:
    """直接改库里一处 field（测试要造「心跳过期」这种状态，必须下到 SQL）。

    **必须显式 close**：``with sqlite3.connect(...) as c`` 只提交/回滚事务，
    **不关闭连接** —— Windows 上手柄不松开，``tearDown`` 删临时目录时会抛
    ``PermissionError: [WinError 32]``（不看这条 traceback 的话会以为是 SQLite 的错）。
    """
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        conn.execute(sql, params)
    finally:
        conn.close()


# ==========================================================================
# 认领：并行正确性（C1 在这里会双/漏）
# ==========================================================================


class TestClaim(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.q = S.Queue(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _fill(self, n: int) -> None:
        for i in range(n):
            self.q.add(f"job-{i:03d}", {"i": i})

    def test_claim_is_fifo_by_insertion_order(self):
        """FIFO 由 ``seq AUTOINCREMENT`` 保证 —— 不能靠 ``ROWID``/物理顺序。"""
        self._fill(5)
        got = []
        while True:
            e = self.q.claim_next(worker_id="w")
            if e is None:
                break
            got.append(e["job_id"])
        self.assertEqual(got, [f"job-{i:03d}" for i in range(5)])

    def test_claimed_item_is_not_claimable_again(self):
        self._fill(1)
        self.assertIsNotNone(self.q.claim_next(worker_id="w1"))
        self.assertIsNone(self.q.claim_next(worker_id="w2"))

    def test_higher_priority_is_picked_first(self):
        self.q.add("low", {}, priority=0)
        self.q.add("high", {}, priority=10)
        self.assertEqual(self.q.claim_next(worker_id="w")["job_id"], "high")

    def test_owner_is_kept_after_finish(self):
        """owner 存在队列行里 —— job.json 没有这个概念。"""
        self.q.add("j1", {}, owner="ethan")
        self.q.claim_next(worker_id="w")
        self.q.finish("j1", exit_code=0)
        self.assertEqual(self.q.get("j1")["owner"], "ethan")
        self.assertEqual([e["job_id"] for e in self.q.list(owner="ethan")], ["j1"])
        self.assertEqual(self.q.list(owner="someone-else"), [])

    def test_finish_is_idempotent(self):
        """同一次完成被记两次账的话，worker 的工作量统计就废了。"""
        self.q.add("j1", {})
        self.q.claim_next(worker_id="w1")
        self.q.finish("j1", exit_code=0)
        self.q.finish("j1", exit_code=0)
        ws = [w for w in self.q.workers() if w["worker_id"] == "w1"]
        self.assertEqual(ws[0]["jobs_done"], 1)

    def test_busy_timeout_is_configured(self):
        """争不到锁时要等（而不是立刻抛）—— 本机磁盘 I/O 经常抖到秒级。"""
        self.assertGreaterEqual(self.q.busy_timeout_s, 1.0)

    def test_busy_timeout_is_bounded_so_health_cannot_hang(self):
        """上界同样要钉住：**等锁不能等到「服务看起来挂了」。**

        C2 端到端验收在这里翻过车：`busy_timeout` 给的是 15s，队列一被别的进程
        占住，`GET /api/health` 就原地等 15s —— 探活 3s 超时的客户端（验收脚本就是）
        直接判「服务没起来」，而它其实只是排队等一把马上就放的锁。

        单机争用实测是几十毫秒量级（``tools/_probe_sqlite_mode.py``：DELETE 中位 58ms），
        所以上界给到 5s 已经很宽松，再大就是把一次可降级的读变成一次假死。
        """
        self.assertLessEqual(self.q.busy_timeout_s, 5.0)


# ==========================================================================
# 跨进程：C2 存在的理由
# ==========================================================================

#: 子进程跑的认领器。**写成临时文件**而不是 ``python -c`` ——
#: 后者要把整段代码塞进命令行引号里，在 Windows 上 quoting 会咬人。
_CHILD = textwrap.dedent(
    """
    import sys, json
    sys.path.insert(0, {platform!r})
    from web.store import Queue
    q = Queue(sys.argv[1])
    got = []
    while True:
        e = q.claim_next(worker_id=sys.argv[2])
        if e is None:
            break
        got.append(e["job_id"])
    print(json.dumps(got))
    """
)


@unittest.skipIf(sys.platform.startswith("win") and not sys.executable,
                 "没有可用的解释器")
class TestCrossProcessClaim(unittest.TestCase):
    """**本文件最该守的一条**：两个进程同时抢，不能重复、不能丢。

    C1 的 ``threading.RLock`` 在这里帮不上任何忙 —— 锁的作用域到不了另一个进程。
    SQLite 的 ``BEGIN IMMEDIATE`` + 守卫式 ``UPDATE ... WHERE state='queued'`` 才行。
    """

    N_JOBS = 40
    N_WORKERS = 4

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.q = S.Queue(self._td.name)
        for i in range(self.N_JOBS):
            self.q.add(f"job-{i:03d}", {"i": i})

    def tearDown(self):
        self._td.cleanup()

    def _spawn(self, wid: str) -> subprocess.Popen:
        helper = Path(self._td.name) / f"child_{wid}.py"
        helper.write_text(_CHILD.format(platform=str(PLATFORM)), encoding="utf-8")
        return subprocess.Popen(
            [sys.executable, str(helper), self._td.name, wid],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_two_processes_never_claim_the_same_job(self):
        procs = [self._spawn(f"w{i}") for i in range(self.N_WORKERS)]
        outs = [p.communicate(timeout=180) for p in procs]

        claimed: list[str] = []
        for i, (out, err) in enumerate(outs):
            self.assertEqual(procs[i].returncode, 0,
                             f"worker w{i} 崩了：{err[:400]}")
            claimed.extend(json.loads(out.strip() or "[]"))

        total = len(claimed)
        uniq = len(set(claimed))
        self.assertEqual(uniq, total,
                         f"有 {total - uniq} 个 job 被**两个进程同时认领** —— "
                         "同一个 job 目录会被并写，产物是两份半截结果摞起来的")
        self.assertEqual(sorted(set(claimed)),
                         sorted(f"job-{i:03d}" for i in range(self.N_JOBS)),
                         "有任务丢了（read-modify-write 覆盖的经典症状）")

    def test_concurrent_writes_do_not_lose_submissions(self):
        """一边 submitting 一边 claiming：**提交的行不能被后来的写覆盖掉**。

        这是 C1 那条硬伤的直接对照：``queue.json`` 是整文件覆盖写，
        两个进程同时「读→改→写」必丢。这里要断言的是**一条都没少**
        （至于这 60 个是不是全被认领走，取决于子进程退出的时机，不去断言）。
        """
        procs = [self._spawn(f"c{i}") for i in range(2)]
        #: 父进程趁着子进程在认领，继续往同一个库里提交
        for i in range(self.N_JOBS, self.N_JOBS + 20):
            self.q.add(f"job-{i:03d}", {"i": i})

        claimed: list[str] = []
        for p in procs:
            out, err = p.communicate(timeout=180)
            self.assertEqual(p.returncode, 0, f"child 崩了：{err[:400]}")
            claimed.extend(json.loads(out.strip() or "[]"))

        #: ① 并发下不能出现重复认领（同一个 job 被跑两遍）
        self.assertEqual(len(claimed), len(set(claimed)), "并发下出现了重复认领")

        #: ② 60 条提交**一条都不能少** —— 这才是「没冲突写丢失」的实质证据
        self.assertEqual(sorted(e["job_id"] for e in self.q.list()),
                         sorted(f"job-{i:03d}" for i in range(self.N_JOBS + 20)))

        #: ③ 没被认领走的那些，仍然乖乖是 queued（没被半截写坏）
        unclaimed = set(f"job-{i:03d}" for i in range(self.N_JOBS + 20)) - set(claimed)
        for jid in sorted(unclaimed):
            self.assertEqual(self.q.get(jid)["state"], S.STATE_QUEUED)


# ==========================================================================
# 心跳与回收：C2 新有的（C1 没有这个概念）
# ==========================================================================


class TestReapSafety(unittest.TestCase):
    """C1 的 ``reap_interrupted()`` 一上来就把**所有** running 都判死。

    单进程下这是对的（队里有 running ⇒ 上一轮肯定死了）；C2 里 worker 在别人进程，
    这条假设会变成事故：**后起的 worker 把同伴手里的活判死**。改成按心跳回收。
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.q = S.Queue(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_live_worker_keeps_its_job(self):
        self.q.add("j1", {})
        self.q.claim_next(worker_id="worker-A")
        self.q.heartbeat("worker-A", job_id="j1", host="h", pid=11)
        #: 默认窗口下，正在干活的不能被回收 —— 这是 C2 相对 C1 新增的保护
        self.assertEqual([], self.q.reap_stale())
        self.assertEqual(self.q.get("j1")["state"], S.STATE_RUNNING)

    def test_dead_heartbeat_is_reaped(self):
        self.q.add("j1", {})
        self.q.claim_next(worker_id="worker-B")
        hit = self.q.reap_stale(older_than_s=0)
        self.assertEqual(hit, ["j1"])
        #: 必须是 interrupted 而不是 failed —— 重跑是**人的决定**，不是自动策略
        self.assertEqual(self.q.get("j1")["state"], S.STATE_INTERRUPTED)

    def test_only_dead_workers_rows_are_reaped(self):
        """两个 worker 同时干活：回收只动心跳停了的那个，同伴不受牵连。

        C1 做不到这条 —— 它没有心跳，只能「running 一律判死」。
        """
        self.q.add("live", {})
        self.q.add("dead", {})
        self.q.claim_next(worker_id="busy")
        self.q.claim_next(worker_id="died")
        #: 手工把「dead」的心跳改写成很久以前 —— 模拟那个 worker 进程已经没了
        _raw_exec(self.q.path, "UPDATE jobs SET heartbeat_at='2000-01-01T00:00:00'"
                               " WHERE job_id='dead'")
        #: 用默认窗口（不是 0）：``older_than_s=0`` 等于「全部判死」，测不出这条保护
        hit = self.q.reap_stale()
        self.assertEqual(hit, ["dead"])
        self.assertEqual(self.q.get("dead")["state"], S.STATE_INTERRUPTED)
        self.assertEqual(self.q.get("live")["state"], S.STATE_RUNNING)

    def test_worker_rows_report_alive_by_freshness(self):
        self.q.heartbeat("w-old", host="h", pid=2)
        _raw_exec(self.q.path, "UPDATE workers SET heartbeat_at='2000-01-01T00:00:00'"
                               " WHERE worker_id='w-old'")
        self.q.heartbeat("w-now", host="h", pid=3)
        table = {w["worker_id"]: w["alive"] for w in self.q.workers()}
        self.assertFalse(table["w-old"], "心跳停了的 worker 不能报活着")
        self.assertTrue(table["w-now"])


# ==========================================================================
# 迁移与容错
# ==========================================================================


class TestMigrationAndResilience(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.q = S.Queue(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_legacy_queue_json_is_imported_and_kept(self):
        """C1 的 queue.json 要能被一次性捡起来，原文件留证。"""
        legacy = Path(self._td.name) / "queue.json"
        legacy.write_text(json.dumps([
            {"job_id": "old-1", "spec": {"category": "A"}, "state": "queued",
             "submitted_at": "2026-10-01T00:00:00", "attempts": 1},
            {"job_id": "old-2", "spec": {}, "state": "failed",
             "submitted_at": "2026-10-01T00:00:00", "error": "boom"},
        ], ensure_ascii=False), encoding="utf-8")
        q2 = S.Queue(self._td.name)
        self.assertEqual(sorted(e["job_id"] for e in q2.list()), ["old-1", "old-2"])
        self.assertTrue((Path(self._td.name) / "queue.json.migrated").exists(),
                        "旧文件必须改名保留 —— 不能删")

    def test_corrupt_db_is_quarantined_not_silently_deleted(self):
        """坏库→隔离成 queue.db.corrupt-*，服务继续可用。"""
        self.q.add("j1", {})
        self.q.path.write_text("not a database at all", encoding="utf-8")
        self.assertEqual(self.q.list(), [])                    # 不崩
        self.q.add("j2", {})                                   # 隔离后还能用
        self.assertEqual([e["job_id"] for e in self.q.list()], ["j2"])
        leftovers = list(Path(self._td.name).glob("queue.db.corrupt-*"))
        self.assertTrue(leftovers, "坏库应保留证据，不能静默删掉")

    def test_reads_degrade_when_db_stays_broken(self):
        """**读**操作降级为空，绝不能把 Web 拖下水（``/api/health`` 要用它）。"""
        self.q.add("j1", {})
        self.q.path.write_bytes(b"\x00" * 4096)
        self.assertEqual(self.q.list(), [])
        self.assertIsNone(self.q.get("j1"))
        self.assertEqual(self.q.pending_ids(), set())
        self.assertEqual(self.q.stats()["total"], 0)
        self.assertEqual(self.q.workers(), [])

    def test_prune_removes_only_old_finished_rows(self):
        """清理只动**早就跑完的**行 —— 排队中/运行中的任务一个都不能少。"""
        #: running：正在干活
        self.q.add("running", {})
        self.q.claim_next(worker_id="w")
        #: queued：还没轮到
        self.q.add("queued", {})
        #: done 但很老：这才是该清的
        self.q.add("ancient", {})
        e = self.q.claim_next(worker_id="w")
        self.assertEqual(e["job_id"], "queued", "取件顺序应与插入顺序一致")
        self.q.finish("queued", exit_code=0)
        self.q.fail("ancient", "老失败", exit_code=1)
        _raw_exec(self.q.path,
                  "UPDATE jobs SET finished_at='2000-01-01T00:00:00'"
                  " WHERE job_id='queued'")

        n = self.q.prune(older_than_days=1)
        self.assertEqual(n, 1, "只应清掉那条 2000 年就跑完的")

        left = sorted(e["job_id"] for e in self.q.list())
        self.assertIn("running", left, "运行中的任务被清理掉了 —— 最糟的一种丢活")
        self.assertIn("ancient", left, "失败记录不该被静默清掉（界面要靠它显示原因）")
        self.assertNotIn("queued", left)


if __name__ == "__main__":
    unittest.main(verbosity=2)
