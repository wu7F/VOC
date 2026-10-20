#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 9 · C1 骨架的测试（CI 入口）。

只测**纯函数与纯状态机**，不起服务、不跑内核 —— 毫秒级，可以进每次提交的流水线。
端到端（真起 uvicorn + 真跑七步）在 ``tools/_stage9_accept.py``。

三块：
1. **队列**（``web.store.Queue``）：claim 的原子性、失败保留、中断回收。
2. **状态合成**（``web.app._ui_status``）：**这是最该守的地方** ——
   闸门有 error 时必须是「完成（有质量问题）」而**不是**「失败」；
   退出码 3（等待人工回填）也**不是**失败。
3. **文件名与产物白名单**：上传名消毒、未知产物名一律 404。

``fastapi`` 不在时整体跳过（内核零依赖，不该被 Web 的依赖拖累）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
if str(PLATFORM) not in sys.path:
    sys.path.insert(0, str(PLATFORM))

from web import store as S  # noqa: E402
from web.worker import new_job_id, slugify  # noqa: E402

try:
    from web import app as APP
except Exception:  # noqa: BLE001 —— 没装 fastapi 就跳过（内核不受影响）
    APP = None


# ==========================================================================
# 队列
# ==========================================================================


class TestQueue(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.q = S.Queue(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_add_then_claim(self):
        self.q.add("j1", {"category": "A"})
        self.assertEqual([e["job_id"] for e in self.q.list()], ["j1"])
        e = self.q.claim_next()
        self.assertEqual(e["job_id"], "j1")
        self.assertEqual(e["state"], S.STATE_RUNNING)
        #: 已经 running 的不该被再认领一次
        self.assertIsNone(self.q.claim_next())

    def test_claim_is_fifo(self):
        for n in ("a", "b", "c"):
            self.q.add(n, {})
        self.assertEqual([self.q.claim_next()["job_id"] for _ in range(3)],
                         ["a", "b", "c"])

    def test_duplicate_job_id_rejected(self):
        self.q.add("j1", {})
        with self.assertRaises(ValueError):
            self.q.add("j1", {})

    def test_finish_row_stays_as_ledger(self):
        """跑完之行**留着**，当提交账本（C2 改了 C1 的「跑完即退场」）。

        为什么改：``jobs`` 表要记 ``owner`` —— 谁提交了什么。跑完就删行的话，
        job.json 里没有 owner 这个概念，多租户隔离到第二页就断，
        而且「这个人今天提交了多少任务」永远答不出来。

        代价与补偿：``queue.list()`` 现在也会返回 ``done`` 行，所以
        :func:`web.app._ui_status` **必须**跳过 ``entry.state == 'done'``
        （跑完之后状态一律归 ``job.json``）—— 那条由
        ``test_queue_states_win`` / ``test_clean_gate_is_plain_done`` 守着。
        """
        self.q.add("j1", {}, owner="ethan")
        self.q.claim_next()
        self.q.finish("j1", exit_code=0)

        entry = self.q.get("j1")
        self.assertIsNotNone(entry, "提交记录不该随完成而消失")
        self.assertEqual(entry["state"], S.STATE_DONE)
        self.assertEqual(entry["owner"], "ethan")
        #: 但它**不再是可认领的活任务** —— 这点不能变
        self.assertNotIn("j1", self.q.pending_ids())
        self.assertNotIn("j1", [e["job_id"] for e in self.q.list()
                                if e["state"] != S.STATE_DONE])

    def test_fail_keeps_entry_for_ui(self):
        """失败要**留着**，否则界面没法显示原因。"""
        self.q.add("j1", {})
        self.q.claim_next()
        self.q.fail("j1", "boom")
        lst = self.q.list()
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["state"], S.STATE_FAILED)
        self.assertEqual(lst[0]["error"], "boom")

    def test_reap_interrupted(self):
        """worker 死后，它认领的 running 项标成 interrupted（**不自动重跑**）。"""
        self.q.add("j1", {})
        self.q.claim_next()
        #: C2：worker 心跳停了才回收 —— 所以要先让心跳「过期」
        hit = self.q.reap_stale(older_than_s=0)
        self.assertEqual(hit, ["j1"])
        self.assertEqual(self.q.get("j1")["state"], S.STATE_INTERRUPTED)
        #: 不该被再次认领 —— 重跑由人决定
        self.assertIsNone(self.q.claim_next())

    def test_reap_does_not_touch_freshly_running_jobs(self):
        """C2 相对 C1 新增的保护：**正在干活的任务不能被回收**。

        C1 的 ``reap_interrupted()`` 一上来就把**所有** running 都标成 interrupted
        —— 那是单进程下的正确假设（队列里有 running ⇒ 上一轮肯定死了）。

        到了 C2（worker 在别人进程里）这条假设立刻变成事故：两个 worker 同时起来时，
        后起的那个会把同伴手里的活判死。所以改成**按心跳回收**：干活的人持续刷新
        ``heartbeat_at``，只有停了的才会落进回收网。

        这条是本文件里唯一一个「C1 没有、C2 才成立」的判据。
        """
        self.q.add("j1", {})
        self.q.claim_next(worker_id="worker-A")
        #: worker-A 正在干，心跳是新鲜的 —— 默认窗口（90s）下绝不能回收它
        self.assertEqual(self.q.reap_stale(), [])
        self.assertEqual(self.q.get("j1")["state"], S.STATE_RUNNING)

    def test_heartbeat_keeps_job_alive(self):
        """持续心跳 ⇒ 一直不被回收；心跳一停 ⇒ 立刻回收。"""
        self.q.add("j1", {})
        self.q.claim_next(worker_id="worker-B")
        self.q.heartbeat("worker-B", job_id="j1", host="h", pid=42)
        self.assertEqual(self.q.get("j1")["state"], S.STATE_RUNNING)
        self.assertEqual(self.q.reap_stale(older_than_s=0), ["j1"])

    def test_corrupt_queue_does_not_crash(self):
        """队列库坏了也不能让 Web 起不来（**这条判据从 C1 一路守到 C2**）。

        换了存储之后「烂文件」从 JSON 变成了 SQLite，坑的位置也变了：
        ``sqlite3.connect`` 是**惰性**的，文件不是数据库时它照样返回成功，
        要等第一次真正查询才抛 ``file is not a database`` —— 那样会把
        ``GET /api/health`` 也拖崩。所以 ``Queue._open`` 里的处理是：
        先读一次 schema 逼它现形，不行就把坏库**隔离**成 ``queue.db.corrupt-*``
        （留证据）再重建 —— 服务立刻可用，数据交给人去查。
        """
        #: q.path 现在指向 queue.db（不是 queue.json）
        self.q.add("j1", {})
        self.q.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.q.list(), [])
        #: 隔离之后还能用 —— 这才是「Web 起得来」的实质
        self.q.add("j2", {})
        self.assertEqual([e["job_id"] for e in self.q.list()], ["j2"])
        self.assertTrue(any(self.q.dir.glob("queue.db.corrupt-*")),
                        "坏库应当被隔离保留，而不是被静默删掉")

    def test_missing_queue_file_is_empty(self):
        self.assertEqual(self.q.list(), [])


# ==========================================================================
# 文件名
# ==========================================================================


class TestNaming(unittest.TestCase):
    def test_slugify_basic(self):
        self.assertEqual(slugify("Nursery Glider Recliner"), "nursery-glider-recliner")

    def test_slugify_chinese_falls_back(self):
        """中文品类 slug 退化成 fallback —— 唯一性靠时间戳，slug 只为好认。"""
        self.assertEqual(slugify("婴儿摇椅"), "job")

    def test_job_id_is_time_prefixed_and_fs_safe(self):
        jid = new_job_id("Nursery Glider Recliner", now=0)
        self.assertRegex(jid, r"^\d{8}-\d{6}-nursery-glider-recliner$")
        self.assertNotIn(":", jid)
        self.assertNotIn(" ", jid)

    @unittest.skipIf(APP is None, "fastapi 不在本环境")
    def test_safe_upload_name_strips_dirs_and_separators(self):
        """上传名消毒：路径分隔符与奇怪字符全部换掉（防目录穿越写文件）。"""
        self.assertEqual(APP._safe("../../etc/passwd"), "passwd")
        self.assertEqual(APP._safe("C:\\x\\y\\a b.csv"), "a_b.csv")
        self.assertEqual(APP._safe(""), "upload")
        self.assertNotIn("/", APP._safe("a/b/c.csv"))


# ==========================================================================
# 状态合成（**本文件最该守的**）
# ==========================================================================


@unittest.skipIf(APP is None, "fastapi 不在本环境")
class TestUiStatus(unittest.TestCase):
    """把「队列 × job.json × 闸门」折成一个界面词。做错了用户就会以为分析崩了。"""

    def ui(self, job=None, entry=None, gate=None):
        return APP._ui_status(job, entry, gate)

    # -- 队列态优先 -------------------------------------------------------
    def test_queue_states_win(self):
        for st in (S.STATE_QUEUED, S.STATE_RUNNING, S.STATE_INTERRUPTED):
            self.assertEqual(self.ui(job={"status": "done"}, entry={"state": st}), st)

    # -- 关键语义 ---------------------------------------------------------
    def test_gate_errors_are_not_failure(self):
        """闸门有 error → 「完成（有质量问题）」。**不是 failed**。

        ``validate`` 退出码 1 表示「产物有问题」，不是「执行失败」（Stage 7 定的）。
        显示成失败会让用户去重跑，白烧一次 LLM 额度。
        """
        self.assertEqual(
            self.ui(job={"status": "done"},
                    gate={"errors": 2, "warnings": 0, "passed": 19, "total": 21}),
            "done_with_errors")

    def test_gate_warnings_are_not_failure(self):
        self.assertEqual(
            self.ui(job={"status": "done"},
                    gate={"errors": 0, "warnings": 1, "passed": 20, "total": 21}),
            "done_with_warnings")

    def test_clean_gate_is_plain_done(self):
        self.assertEqual(
            self.ui(job={"status": "done"},
                    gate={"errors": 0, "warnings": 0, "passed": 21, "total": 21}),
            "done")

    def test_exit_code_3_is_waiting_not_failed(self):
        """退出码 3 = 等待人工回填 —— **不是失败**。"""
        self.assertEqual(
            self.ui(entry={"state": S.STATE_FAILED, "error": "等待人工回填（LLM 兜底队列）…"}),
            "waiting_manual")

    def test_job_waiting_manual_maps_through(self):
        self.assertEqual(self.ui(job={"status": "waiting_manual"}), "waiting_manual")

    def test_real_failure_still_fails(self):
        self.assertEqual(self.ui(job={"status": "failed"}), S.STATE_FAILED)
        self.assertEqual(
            self.ui(entry={"state": S.STATE_FAILED, "error": "管线返回退出码 1"}),
            S.STATE_FAILED)

    def test_running_job_with_partial_steps_is_not_done(self):
        """跑了一半的 job（job.json status=running）不能被当成完成。"""
        self.assertNotIn(self.ui(job={"status": "running"}), ("done", "done_with_warnings"))

    def test_unknown_job(self):
        self.assertEqual(self.ui(), "unknown")


# ==========================================================================
# 产物白名单
# ==========================================================================


@unittest.skipIf(APP is None, "fastapi 不在本环境")
class TestArtifactWhitelist(unittest.TestCase):
    def test_kinds_point_inside_job(self):
        for kind, (sub, fname) in APP.REPORT_FILES.items():
            self.assertIn(sub, ("report", "out"), f"{kind} 跑到 job 目录外了")
            self.assertNotIn("..", fname)
            self.assertNotIn("/", fname)

    def test_dashboard_is_served(self):
        self.assertEqual(APP.REPORT_FILES["dashboard"], ("report", "dashboard.html"))

    def test_python_resolves_platform(self):
        self.assertTrue((APP.PLATFORM / "voc.py").exists(), "PLATFORM 解析错了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
