#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 7 编排层（``voc_core/pipeline.py``）测试。

覆盖五块：

1. 步骤切片 —— ``--only`` / ``--from`` / ``--to`` 与非法步骤名
2. 指纹幂等 —— 参数 / 输入内容 / 内核代码 任一变化都要失效；路径归一化后跨目录同指纹
3. 评论收窄 —— 把 SKILL.md 那条人肉步骤的账目算对
4. job.json 契约 —— 复现所需的字段齐备、密钥打码、命令可抄
5. 端到端两个分支 —— ``--dry-run`` 不写盘；人工兜底时退出码 3 且下游 blocked
6. 产物记录 —— ``outputs`` 靠**声明**而非差分（差分在确定性重跑时会记成空集）

运行::

    python -m unittest tests.test_pipeline -v
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voc_core import pipeline as P  # noqa: E402

SILENT = lambda *a, **k: None  # noqa: E731 - 测试里静音编排器的日志


def write_csv(path: Path, cols: list[str], rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def small_reddit_posts(path: Path, n: int = 6) -> Path:
    """一批很小的 Reddit 帖子（只有一半会命中关键词，好让分桶有内容）。"""
    cols = ["reddit_id", "title", "url", "subreddit", "author", "published",
            "updated", "summary_text", "query", "scope", "sort", "time_filter",
            "feed_url"]
    rows = []
    for i in range(n):
        hit = i % 2 == 0
        rows.append({
            "reddit_id": f"p{i}",
            "title": ("Nursery glider recliner review " if hit else "Off topic chat ") + str(i),
            "url": f"https://reddit.com/r/test/comments/p{i}/x/",
            "subreddit": "test",
            "author": f"user{i}",
            "published": "2026-07-01T10:00:00Z",
            "updated": "2026-07-01T10:00:00Z",
            "summary_text": ("The nursery glider recliner is comfortable and the fabric is soft."
                             if hit else "Totally unrelated content about something else."),
            "query": "nursery glider",
            "scope": "all",
            "sort": "relevance",
            "time_filter": "year",
            "feed_url": "",
        })
    return write_csv(path, cols, rows)


def small_reddit_comments(path: Path) -> Path:
    cols = ["comment_id", "post_reddit_id", "post_subreddit", "post_title",
            "author", "published", "body", "is_root_comment"]
    rows = [
        {"comment_id": "c0", "post_reddit_id": "p0", "post_subreddit": "test",
         "post_title": "t", "author": "a", "published": "2026-07-02T10:00:00Z",
         "body": "Our glider recliner squeaks after a month of use.", "is_root_comment": "True"},
        {"comment_id": "c1", "post_reddit_id": "p0", "post_subreddit": "test",
         "post_title": "t", "author": "b", "published": "2026-07-02T11:00:00Z",
         "body": "Same here, the swivel base wobbles.", "is_root_comment": "True"},
        {"comment_id": "c2", "post_reddit_id": "p1", "post_subreddit": "test",
         "post_title": "t", "author": "c", "published": "2026-07-02T12:00:00Z",
         "body": "Unrelated chatter about the weather today.", "is_root_comment": "True"},
        {"comment_id": "c3", "post_reddit_id": "p2", "post_subreddit": "test",
         "post_title": "t", "author": "d", "published": "2026-07-02T13:00:00Z",
         "body": "Recliner mechanism failed after six months.", "is_root_comment": "True"},
    ]
    return write_csv(path, cols, rows)


class TempJobCase(unittest.TestCase):
    """带临时目录 + 小数据集的基类。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="voc_s7_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.job_dir = self.tmp / "runs" / "job1"
        self.job = P.JobPaths(self.job_dir)
        self.posts = small_reddit_posts(self.tmp / "src" / "posts.csv")
        self.comments = small_reddit_comments(self.tmp / "src" / "comments.csv")

    def spec(self, **kw) -> P.JobSpec:
        base = dict(category="Nursery Glider", input=str(self.posts),
                    comments=str(self.comments), profile="reddit", mock=True,
                    keywords="nursery,glider,recliner,rocker",
                    brands="Babyletto,DaVinci")
        base.update(kw)
        return P.JobSpec(**base)


# --------------------------------------------------------------------------
# 1. 步骤切片
# --------------------------------------------------------------------------

class TestSelectSteps(unittest.TestCase):
    def test_default_is_all_seven(self):
        self.assertEqual(P.select_steps(), list(P.STEPS))
        self.assertEqual(len(P.STEPS), 7)

    def test_only_keeps_canonical_order(self):
        got = P.select_steps(only=["validate", "clean", "tags"])
        self.assertEqual(got, ["clean", "tags", "validate"])

    def test_from(self):
        self.assertEqual(P.select_steps(start="annotate"),
                         ["annotate", "aggregate", "report", "dashboard", "validate"])

    def test_to(self):
        self.assertEqual(P.select_steps(end="tags"), ["clean", "tags"])

    def test_from_to(self):
        self.assertEqual(P.select_steps(start="tags", end="aggregate"),
                         ["tags", "annotate", "aggregate"])

    def test_unknown_step_raises(self):
        for kw in ({"only": ["nope"]}, {"start": "nope"}, {"end": "nope"}):
            with self.assertRaises(P.PipelineError):
                P.select_steps(**kw)

    def test_first_and_last(self):
        self.assertEqual(P.select_steps(start="clean")[0], "clean")
        self.assertEqual(P.select_steps(end="validate")[-1], "validate")


# --------------------------------------------------------------------------
# 2. 指纹幂等
# --------------------------------------------------------------------------

class TestFingerprint(TempJobCase):
    def _fp(self, step: str, params: dict, inputs: dict) -> str:
        return P._fingerprint(step, params, inputs, self.job)

    def test_params_change_breaks(self):
        a = self._fp("clean", {"category": "A"}, {"x.csv": "h1"})
        b = self._fp("clean", {"category": "B"}, {"x.csv": "h1"})
        self.assertNotEqual(a, b)

    def test_input_content_change_breaks(self):
        a = self._fp("clean", {"category": "A"}, {"x.csv": "h1"})
        b = self._fp("clean", {"category": "A"}, {"x.csv": "h2"})
        self.assertNotEqual(a, b)

    def test_same_inputs_same_fp(self):
        a = self._fp("clean", {"category": "A"}, {"x.csv": "h1"})
        b = self._fp("clean", {"category": "A"}, {"x.csv": "h1"})
        self.assertEqual(a, b)

    def test_step_isolated(self):
        a = self._fp("clean", {}, {})
        b = self._fp("tags", {}, {})
        self.assertNotEqual(a, b)

    def test_absolute_and_relative_paths_normalize_alike(self):
        """同一条命令，job 目录写绝对路径或相对路径，指纹必须一致（换 checkout 目录也能续跑）。"""
        ab = self._fp("clean", {"out_dir": str(self.job.out)}, {})
        rel = self._fp("clean", {"out_dir": os.path.relpath(self.job.out)}, {})
        self.assertEqual(rel, ab)

    def test_external_path_collapses_to_ext(self):
        p = P._normalize_params({"input": r"C:\other\place\data.csv"}, self.job)
        self.assertTrue(p["input"].startswith("<ext>/"), p)

    def test_script_sha_is_content_hash(self):
        sha = P._script_sha("clean")
        self.assertEqual(len(sha), 40)
        self.assertNotEqual(P._script_sha("clean"), P._script_sha("tags"))

    def test_non_path_values_left_alone(self):
        p = P._normalize_params({"mock": True, "backend": "manual"}, self.job)
        self.assertEqual(p, {"mock": True, "backend": "manual"})


class TestIntactAndSnapshot(TempJobCase):
    def test_snapshot_skips_job_json(self):
        self.job.ensure()
        (self.job.root / "a.txt").write_text("x", encoding="utf-8")
        (self.job.root / P.JOB_FILE).write_text("{}", encoding="utf-8")
        snap = P._snapshot(self.job)
        self.assertIn("a.txt", snap)
        self.assertNotIn(P.JOB_FILE, snap)

    def test_intact_detects_manual_edit(self):
        self.job.ensure()
        p = self.job.out / "d.json"
        p.write_text('{"a":1}', encoding="utf-8")
        rec = {"out/d.json": P._sha1_file(p)}
        self.assertTrue(P._intact(self.job.root, rec))
        p.write_text('{"a":2}', encoding="utf-8")
        self.assertFalse(P._intact(self.job.root, rec))

    def test_intact_detects_missing(self):
        self.job.ensure()
        self.assertFalse(P._intact(self.job.root, {"out/nope.json": "deadbeef"}))

    def test_changed_only_new_or_modified(self):
        before = {"a": "1", "b": "2"}
        after = {"a": "1", "b": "3", "c": "9"}
        self.assertEqual(P._changed(before, after), {"b": "3", "c": "9"})


# --------------------------------------------------------------------------
# 3. 评论收窄
# --------------------------------------------------------------------------

class TestNarrowComments(TempJobCase):
    def test_filters_to_relevant_parents(self):
        rel = self.tmp / "rel.csv"
        write_csv(rel, ["reddit_id", "title"],
                  [{"reddit_id": "p0", "title": "a"}, {"reddit_id": "p2", "title": "b"}])
        out = self.tmp / "out" / "comments_relevant.csv"
        info = P.narrow_comments(rel, self.comments, out)
        self.assertTrue(info["applicable"])
        self.assertEqual(info["units_kept"], 2)
        self.assertEqual(info["comments_total"], 4)
        self.assertEqual(info["comments_kept"], 3)   # c0, c1(->p0) 与 c3(->p2)
        self.assertEqual(info["dropped"], 1)         # c2 挂在被排除的 p1 上
        with out.open(encoding="utf-8-sig", newline="") as f:
            kept = list(csv.DictReader(f))
        self.assertEqual([r["comment_id"] for r in kept], ["c0", "c1", "c3"])
        # 原列序与列名保持不变
        self.assertEqual(list(kept[0]), ["comment_id", "post_reddit_id", "post_subreddit",
                                         "post_title", "author", "published", "body",
                                         "is_root_comment"])

    def test_missing_file_raises(self):
        with self.assertRaises(P.PipelineError):
            P.narrow_comments(self.tmp / "nope.csv", self.comments, self.tmp / "o.csv")

    def test_no_relevant_units_yields_empty_body(self):
        rel = self.tmp / "rel.csv"
        write_csv(rel, ["reddit_id", "title"], [{"reddit_id": "zzz", "title": "x"}])
        out = self.tmp / "o.csv"
        info = P.narrow_comments(rel, self.comments, out)
        self.assertEqual(info["comments_kept"], 0)
        self.assertTrue(out.exists())


# --------------------------------------------------------------------------
# 3b. 品牌列重写（SKILL.md 第 310 行那条人肉步骤）
# --------------------------------------------------------------------------

class TestRewriteBrands(TempJobCase):
    def _rel(self, rows):
        return write_csv(self.tmp / "rel.csv",
                         ["reddit_id", "title", "mentioned_brands"], rows)

    def test_rewrites_from_thread(self):
        rel = self._rel([
            {"reddit_id": "p0", "title": "Nice chair", "mentioned_brands": ""},
            {"reddit_id": "p1", "title": "Babyletto glider", "mentioned_brands": "babyletto"},
            {"reddit_id": "p2", "title": "Off topic", "mentioned_brands": ""},
        ])
        com = write_csv(self.tmp / "c.csv", ["comment_id", "post_reddit_id", "body"], [
            {"comment_id": "c0", "post_reddit_id": "p0", "body": "we love our DaVinci crib"},
            {"comment_id": "c1", "post_reddit_id": "p0", "body": "unrelated chatter"},
            {"comment_id": "c2", "post_reddit_id": "p2", "body": "no brand mentioned"},
        ])
        info = P.rewrite_mentioned_brands(rel, com, ["Babyletto", "DaVinci"])
        self.assertTrue(info["applicable"])
        self.assertEqual(info["before_nonempty"], 1)
        self.assertEqual(info["after_nonempty"], 2, "标题 + 评论口径应把 p0 也捞进来")
        self.assertEqual(info["brands_hit"], 2)
        with rel.open(encoding="utf-8-sig", newline="") as f:
            got = {r["reddit_id"]: r["mentioned_brands"] for r in csv.DictReader(f)}
        self.assertEqual(got, {"p0": "DaVinci", "p1": "Babyletto", "p2": ""})

    def test_order_and_casing_follow_brand_terms(self):
        """输出顺序按 brand_terms、写法用标准大小写（与既有产物同构）。"""
        rel = self._rel([{"reddit_id": "p0",
                          "title": "davinci and BABYLETTO and la-z-boy rock",
                          "mentioned_brands": ""}])
        P.rewrite_mentioned_brands(rel, None, ["Babyletto", "La-Z-Boy", "DaVinci"])
        with rel.open(encoding="utf-8-sig", newline="") as f:
            val = next(csv.DictReader(f))["mentioned_brands"]
        self.assertEqual(val, "Babyletto,La-Z-Boy,DaVinci")

    def test_word_boundary_respected(self):
        rel = self._rel([{"reddit_id": "p0", "title": "the chitanese dialect",
                          "mentioned_brands": ""}])
        P.rewrite_mentioned_brands(rel, None, ["CHITA"])
        with rel.open(encoding="utf-8-sig", newline="") as f:
            self.assertEqual(next(csv.DictReader(f))["mentioned_brands"], "")

    def test_empty_brand_terms_not_applicable(self):
        rel = self._rel([{"reddit_id": "p0", "title": "x", "mentioned_brands": ""}])
        self.assertFalse(P.rewrite_mentioned_brands(rel, None, [])["applicable"])

    def test_missing_column_not_applicable(self):
        rel = write_csv(self.tmp / "r2.csv", ["reddit_id", "title"],
                        [{"reddit_id": "p0", "title": "x"}])
        info = P.rewrite_mentioned_brands(rel, None, ["CHITA"])
        self.assertFalse(info["applicable"])

    def test_preserves_other_columns_and_order(self):
        rel = write_csv(self.tmp / "r3.csv",
                        ["reddit_id", "title", "mentioned_brands", "bucket"],
                        [{"reddit_id": "p0", "title": "CHITA rocker",
                          "mentioned_brands": "", "bucket": "B"}])
        P.rewrite_mentioned_brands(rel, None, ["CHITA"])
        with rel.open(encoding="utf-8-sig", newline="") as f:
            rdr = csv.DictReader(f)
            self.assertEqual(rdr.fieldnames,
                             ["reddit_id", "title", "mentioned_brands", "bucket"])
            row = next(rdr)
        self.assertEqual(row["bucket"], "B")
        self.assertEqual(row["mentioned_brands"], "CHITA")


# --------------------------------------------------------------------------
# 4. job.json 契约
# --------------------------------------------------------------------------

class TestJobSpec(unittest.TestCase):
    def test_roundtrip(self):
        spec = P.JobSpec(category="C", input="i.csv", frame="b2b_role", mock=True,
                         llm={"backend": "manual"})
        back = P.JobSpec.from_dict(spec.to_dict())
        self.assertEqual(back.category, "C")
        self.assertEqual(back.frame, "b2b_role")
        self.assertEqual(back.llm, {"backend": "manual"})
        self.assertTrue(back.mock)

    def test_from_dict_ignores_unknown(self):
        spec = P.JobSpec.from_dict({"category": "C", "id": "x", "bogus": 1})
        self.assertEqual(spec.category, "C")

    def test_api_key_redacted_but_distinguishable(self):
        d1 = P.JobSpec(llm={"api_key": "sk-aaa"}).to_dict()
        d2 = P.JobSpec(llm={"api_key": "sk-bbb"}).to_dict()
        self.assertNotIn("sk-aaa", json.dumps(d1))
        self.assertIn("<redacted:", d1["llm"]["api_key"])
        self.assertNotEqual(d1["llm"]["api_key"], d2["llm"]["api_key"])

    def test_reproduce_command_contains_key_bits(self):
        spec = P.JobSpec(category="Nursery Glider", input="a.csv", comments="b.csv",
                         frame="consumer", mock=True, llm={"api_key": "sk-xxx"})
        cmd = spec.reproduce_command("runs/x")
        self.assertIn("voc.py run", cmd)
        self.assertIn('--category "Nursery Glider"', cmd)
        self.assertIn("--mock", cmd)
        self.assertIn("--api-key ***", cmd)
        self.assertNotIn("sk-xxx", cmd)
        self.assertIn('--job-dir "runs/x"', cmd)

    def test_reproduce_command_is_shell_parseable(self):
        """渲染出来的命令必须能被真正解析回参数 —— 否则「抄进终端就跑」不成立。"""
        import shlex
        spec = P.JobSpec(
            category="Nursery Glider Recliner",
            input=r"C:\data\reddit_posts.csv",
            comments=r"C:\data\reddit_comments.csv",
            keywords="nursery glider,glider,recliner",
            brands="Babyletto,La-Z-Boy,Pottery Barn Kids",
            frame="b2b_role", profile="reddit", mock=True)
        tokens = shlex.split(spec.reproduce_command(r"C:\runs\job1"))
        self.assertEqual(tokens[:3], ["python", "voc.py", "run"])
        self.assertIn("C:/data/reddit_posts.csv", tokens)     # 反斜杠已转正斜杠
        self.assertIn("Babyletto,La-Z-Boy,Pottery Barn Kids", tokens)
        self.assertIn("Nursery Glider Recliner", tokens)
        self.assertIn("C:/runs/job1", tokens)

        cli = P._voc_cli()
        ns = cli.build_parser().parse_args(tokens[2:])
        self.assertEqual(ns.cmd, "run")
        self.assertEqual(ns.category, "Nursery Glider Recliner")
        self.assertEqual(ns.frame, "b2b_role")
        self.assertTrue(ns.mock)

    def test_reproduce_omits_defaults(self):
        cmd = P.JobSpec(category="C", input="a.csv").reproduce_command()
        self.assertNotIn("--profile", cmd)
        self.assertNotIn("--artifact-names", cmd)


# --------------------------------------------------------------------------
# 5. 端到端：dry-run 与人工兜底
# --------------------------------------------------------------------------

class TestDryRun(TempJobCase):
    def test_dry_run_writes_nothing(self):
        r = P.run_job(self.spec(), self.job_dir, dry_run=True, log=SILENT)
        self.assertEqual(r.status, "dry_run")
        self.assertEqual(r.exit_code, P.EXIT_OK)
        self.assertFalse(self.job_dir.exists(), "dry-run 不应创建 job 目录")


class TestWaitingManual(TempJobCase):
    """LLM 落到人工兜底时：退出码 3、状态 waiting_manual、下游 blocked（Stage 4 的约定）。"""

    def test_manual_backend_blocks_downstream(self):
        queue = self.tmp / "llm_queue"
        spec = self.spec(mock=False)  # mock 走内置标签集，压根不碰后端，测不到兜底
        spec.llm = {"backend": "manual", "llm_queue": str(queue)}
        r = P.run_job(spec, self.job_dir, log=SILENT)
        self.assertEqual(r.exit_code, P.EXIT_PENDING, "退出码应为 3（等待人工回填，不是失败）")
        self.assertEqual(r.status, "waiting_manual")
        self.assertEqual(r.waiting, ["tags"])
        self.assertEqual(r.steps["clean"].status, "done")
        self.assertEqual(r.steps["tags"].status, "waiting_manual")
        for step in ("annotate", "aggregate", "report", "dashboard", "validate"):
            self.assertEqual(r.steps[step].status, "blocked", step)

        state = P.load_job(self.job_dir)
        self.assertEqual(state["status"], "waiting_manual")
        self.assertEqual(state["steps"]["tags"]["status"], "waiting_manual")
        # 队列里确实躺着待回填任务
        pend = list((queue / "pending").glob("*.json"))
        self.assertTrue(pend, "人工兜底队列应有待回填任务")

    def test_clean_outputs_are_hashed_into_job_json(self):
        r = P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        self.assertEqual(r.steps["clean"].status, "done")
        state = P.load_job(self.job_dir)
        outs = state["steps"]["clean"]["outputs"]
        self.assertTrue(outs, "clean 应记录产物")
        for rel, sha in outs.items():
            self.assertTrue((self.job_dir / rel).exists(), rel)
            self.assertEqual(len(sha), 40)


class TestJobJsonContract(TempJobCase):
    def test_reproduce_fields_present(self):
        P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        state = P.load_job(self.job_dir)
        for key in ("schema_version", "job_id", "created_at", "updated_at", "status",
                    "spec", "source_files", "steps_order", "steps", "python", "reproduce"):
            self.assertIn(key, state, f"job.json 缺 {key}")
        self.assertEqual(state["schema_version"], P.JOB_SCHEMA_VERSION)
        # 源文件原始路径留档（spec.input 已改写成 job 内路径）
        self.assertEqual(Path(state["source_files"]["input"]), self.posts)
        self.assertIn("voc.py run", state["reproduce"])
        self.assertEqual(state["steps_order"], ["clean"])

    def test_inputs_staged_into_job(self):
        P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        self.assertTrue((self.job.input / "posts.csv").exists())
        self.assertTrue((self.job.input / "comments.csv").exists())
        state = P.load_job(self.job_dir)
        # 落地后 spec.input 指向 job 内部（自包含）
        self.assertIn("input/", state["spec"]["input"].replace("\\", "/"))


class TestIdempotentRerun(TempJobCase):
    """重跑：没变的步骤跳过；改了参数（或产物）则重算。"""

    def test_second_run_skips(self):
        r1 = P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        self.assertEqual(r1.steps["clean"].status, "done")
        r2 = P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        self.assertEqual(r2.steps["clean"].status, "skipped")
        self.assertEqual(r2.skipped, ["clean"])
        self.assertEqual(r2.exit_code, P.EXIT_OK)

    def test_param_change_invalidates(self):
        P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        r2 = P.run_job(self.spec(keywords="completely,other,words"), self.job_dir,
                       only=["clean"], log=SILENT)
        self.assertEqual(r2.steps["clean"].status, "done", "换关键词应重算")

    def test_manual_edit_invalidates(self):
        P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        rel = P.resolve_artifact_path(self.job, "tagged")
        self.assertIsNotNone(rel)
        rel.write_text("reddit_id,title\np0,tampered\n", encoding="utf-8")
        r2 = P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        self.assertEqual(r2.steps["clean"].status, "done", "产物被手改应重算")

    def test_force_reruns(self):
        P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        r2 = P.run_job(self.spec(), self.job_dir, only=["clean"], force="all", log=SILENT)
        self.assertEqual(r2.steps["clean"].status, "done")

    def test_comments_narrowed_after_clean(self):
        r = P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        self.assertIn("评论收窄", r.steps["clean"].note)
        self.assertTrue(self.job.artifact("comments_relevant").exists())


class TestOutputRecording(TempJobCase):
    """产物**怎么记** —— 这一组守着一个真实的洞（v1 job.json 的失效模式）。

    v1 用「跑前/跑后差分」推断本步产物。差分只在产物**第一次出现**时管用：确定性
    步骤重跑（``--force``、或断点续跑）时字节完全相同，差分得到空集，``outputs``
    记成 ``{}`` —— 而 ``_intact({})`` 恒为真，于是「产物完好」永远成立，删光了也跳过。
    实测重现：``--force all`` 之后 ``clean`` 的产物记录从 7 项掉成 0 项。

    修法是**声明**（``STEP_OUTPUT_*``）而不是差分，并把 schema 升到 2 让旧记录作废。
    """

    def test_outputs_recorded_on_first_run(self):
        P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        outs = P.load_job(self.job_dir)["steps"]["clean"]["outputs"]
        self.assertIn("out/reddit_analysis_stats.json", outs)
        self.assertIn("out/reddit_posts_relevant.csv", outs)
        self.assertGreaterEqual(len(outs), 5, outs)

    def test_forced_rerun_keeps_outputs(self):
        """回归主测：强制重跑后产物清单必须原样保留（v1 这里会掉成空）。"""
        P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        first = P.load_job(self.job_dir)["steps"]["clean"]["outputs"]
        self.assertTrue(first)
        P.run_job(self.spec(), self.job_dir, only=["clean"], force="all", log=SILENT)
        again = P.load_job(self.job_dir)["steps"]["clean"]["outputs"]
        self.assertEqual(again, first, "强制重跑后产物清单必须原样保留")

    def test_deleted_output_forces_rerun(self):
        P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        victim = self.job.artifact("comments_relevant")
        self.assertTrue(victim.exists())
        victim.unlink()
        r = P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        self.assertEqual(r.steps["clean"].status, "done", "产物被删应重算而不是跳过")
        self.assertTrue(victim.exists(), "重算后产物应回来")

    def test_declared_output_missing_from_disk_is_not_recorded(self):
        """声明了但没产出的项不该记进清单（例如没给 --comments 时没有收窄评论表）。"""
        P.run_job(self.spec(comments=""), self.job_dir, only=["clean"], log=SILENT)
        outs = P.load_job(self.job_dir)["steps"]["clean"]["outputs"]
        self.assertNotIn("out/units_comments_relevant.csv", outs)

    def test_dir_output_uses_aggregate_hash(self):
        d = self.job.annot
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.json").write_text("{}", encoding="utf-8")
        outs = P._collect_outputs("annotate", self.job)
        self.assertEqual(list(outs), ["annot/"])
        self.assertEqual(len(outs["annot/"]), 40)
        self.assertTrue(P._intact(self.job.root, outs, {}))
        # 改一个子文件 → 聚合哈希变 → 判为不完整（文件产物走 stat 短路，目录不能）
        (d / "a.json").write_text('{"x": 1}', encoding="utf-8")
        self.assertFalse(P._intact(self.job.root, outs, {}))

    def test_dir_entries_get_no_stat_stamp(self):
        """目录条目不存 [size, mtime] 戳 —— 目录自身 stat 对子文件改动不敏感，
        存了会让 _intact 漏判。"""
        d = self.job.annot
        d.mkdir(parents=True, exist_ok=True)
        (d / "a.json").write_text("{}", encoding="utf-8")
        sr = P.StepRun(key="annotate")
        P._record_outputs(sr, self.job, P._collect_outputs("annotate", self.job))
        self.assertIn("annot/", sr.outputs)
        self.assertNotIn("annot/", sr.stamps)

    def test_empty_output_records_never_skip(self):
        """第二道保险：产物清单为空时，即使指纹一致也必须重跑。"""
        P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        state = P.load_job(self.job_dir)
        state["steps"]["clean"]["outputs"] = {}
        state["steps"]["clean"]["outputs_stat"] = {}
        self.job.job_file.write_text(json.dumps(state), encoding="utf-8")
        r = P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        self.assertEqual(r.steps["clean"].status, "done")

    def test_legacy_schema_state_is_discarded(self):
        P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        state = P.load_job(self.job_dir)
        state["schema_version"] = 1  # 模拟修复前写下的 job.json
        state["steps"]["clean"]["outputs"] = {}
        self.job.job_file.write_text(json.dumps(state), encoding="utf-8")
        r = P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        self.assertEqual(r.steps["clean"].status, "done", "旧版本记录应作废、整条重跑")
        self.assertEqual(P.load_job(self.job_dir)["schema_version"], P.JOB_SCHEMA_VERSION)


class TestListJobs(TempJobCase):
    def test_lists_after_run(self):
        P.run_job(self.spec(), self.job_dir, only=["clean"], log=SILENT)
        rows = P.list_jobs(self.job_dir.parent)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_id"], "job1")
        self.assertEqual(rows[0]["status"], "done")
        self.assertEqual(rows[0]["done"], 1)

    def test_empty_dir(self):
        self.assertEqual(P.list_jobs(self.tmp / "nothing"), [])


class TestRunBatch(TempJobCase):
    def test_batch_serial(self):
        runs = self.tmp / "runs"
        specs = [
            ("a", self.spec(category="Cat A")),
            ("b", self.spec(category="Cat B")),
        ]
        results, code = P.run_batch(specs, runs, only=["clean"], log=SILENT)
        self.assertEqual(code, P.EXIT_OK)
        self.assertEqual([r.job_dir.name for r in results], ["a", "b"])
        self.assertEqual(len(P.list_jobs(runs)), 2)

    def test_batch_keeps_going_on_failure(self):
        runs = self.tmp / "runs"
        bad = self.spec(input=str(self.tmp / "missing.csv"))
        specs = [("bad", bad), ("good", self.spec())]
        results, code = P.run_batch(specs, runs, only=["clean"], log=SILENT)
        self.assertEqual(len(results), 2)
        self.assertEqual(code, P.EXIT_FAIL)




# --------------------------------------------------------------------------
# 3c. 自动喂评论（主文件「帖+评」混装导出时，把拆出的评论半并入分析）
# --------------------------------------------------------------------------

class TestAutoFeedComments(TempJobCase):
    def _make_main_xlsx(self):
        # 非 CSV 主文件（触发 _convert_staged_inputs 的转换分支）
        self.job.ensure()
        xp = self.job.input / "bulk.xlsx"
        xp.write_bytes(b"PK\x03\x04fake")
        return xp

    def _fake_convert(self, write_comments):
        def fake(sp, job, spec, log, *, is_comments=False):
            if is_comments:
                # 显式评论文件：转换产物用一个**不与 auto-feed 撞名**的文件，
                # 以验证「显式优先、不被自动并覆盖」
                out = job.input / "explicit_comments.csv"
                out.write_text("x\n", encoding="utf-8")
                return out
            # 主文件：模拟 cmd_convert 写出 posts + 评论 两个半
            posts = job.input / "units_legacy_posts.csv"
            posts.write_text("reddit_id,title\n", encoding="utf-8")
            if write_comments:
                comments = job.input / "units_legacy_comments.csv"
                comments.write_text("post_reddit_id,body\n", encoding="utf-8")
            return posts
        return fake

    def test_auto_feed_when_comments_present(self):
        from unittest.mock import patch
        with patch.object(P, "_convert_one") as mc:
            mc.side_effect = self._fake_convert(write_comments=True)
            spec = P.JobSpec(input=str(self._make_main_xlsx()), comments="")
            out = P._convert_staged_inputs(spec, self.job, SILENT)
        self.assertEqual(out.input, str(self.job.input / "units_legacy_posts.csv"))
        self.assertEqual(out.comments, str(self.job.input / "units_legacy_comments.csv"),
                         "主文件拆出的评论半应被自动并入")

    def test_skip_when_explicit_comments(self):
        from unittest.mock import patch
        with patch.object(P, "_convert_one") as mc:
            mc.side_effect = self._fake_convert(write_comments=True)
            cx = self.job.input / "bulk_comments.xlsx"
            self.job.ensure()
            cx.write_bytes(b"PK")
            spec = P.JobSpec(input=str(self._make_main_xlsx()), comments=str(cx))
            out = P._convert_staged_inputs(spec, self.job, SILENT)
        self.assertEqual(out.comments, str(self.job.input / "explicit_comments.csv"),
                         "显式传了评论文件时不应被 auto-feed 覆盖")

    def test_skip_when_no_comments_csv(self):
        from unittest.mock import patch
        with patch.object(P, "_convert_one") as mc:
            mc.side_effect = self._fake_convert(write_comments=False)
            spec = P.JobSpec(input=str(self._make_main_xlsx()), comments="")
            out = P._convert_staged_inputs(spec, self.job, SILENT)
        self.assertEqual(out.comments, "", "没有评论半时不应伪造 comments")

if __name__ == "__main__":
    unittest.main(verbosity=2)
