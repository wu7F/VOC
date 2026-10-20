#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 6 质量闸门的守门测试 —— 用**变异测试**证明闸门真的能拦住坏数据。

为什么用变异测试而不是「跑一遍看有没有报错」：校验器的失效方式和普通代码不同。
普通代码会抛异常；校验器失效时**安静地返回通过**，而那正是它存在的理由。
所以唯一可信的验收方式是：拿一份**全部通过**的产物，逐个注入一种缺陷，
断言**恰好是那一条检查项翻红**（不是"某处报了错"）。

这里的 21 个用例逐一对应 ``voc_core/validate.CHECKS`` 里的 21 条检查项。
新增检查项时请同时在此加一条变异用例 —— 没有变异用例的检查项等于没验过。
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voc_core.validate import CHECKS, Context, audit  # noqa: E402

VOC = ROOT / "voc.py"
DATA = ROOT.parent / "data" / "reddit_analysis"
REPORTS = ROOT.parent / "reports"
PY = sys.executable


# ==========================================================================
# 基线产物：一份**全部检查项都通过**的最小集合
# ==========================================================================

_POSTS = [
    {"reddit_id": "p1", "title": "Best glider ever",
     "summary_text": "I bought this glider and it is comfortable",
     "url": "https://www.reddit.com/r/baby/p1", "subreddit": "baby",
     "author": "alice", "published": "2026-01-01",
     "voc_bucket": "A_强相关VOC", "voc_score": "90",
     "mentioned_brands": "Babyletto"},
    {"reddit_id": "p2", "title": "Looking for a recliner",
     "summary_text": "Any recommendations for a nursery chair?",
     "url": "https://www.reddit.com/r/baby/p2", "subreddit": "baby",
     "author": "bob", "published": "2026-02-01",
     "voc_bucket": "B_可参考", "voc_score": "60",
     "mentioned_brands": ""},
]

_COMMENTS = [
    {"comment_id": "c1", "post_reddit_id": "p1",
     "body": "I love the swivel, used it daily. My Babyletto is great."},
]

_STATS = {
    "category": "测试品类", "raw_total": 3, "expired_count": 1,
    "after_date_clean": 2, "bucket_A": 1, "bucket_B": 1, "bucket_C": 0, "bucket_D": 0,
    "final_analysis": 2, "max_age_years": 2.0,
    "keywords": ["glider"], "brand_terms": ["Babyletto"],
    "comments_total": 1, "comments_posts_count": 1, "source_note": "",
}

_DIMENSIONS = {
    "category": "测试品类",
    "generated_at": "2026-01-01T00:00:00",
    "usage_scenarios": [
        {"label_cn": "客厅使用", "count": 1, "percentage": 50.0,
         "reason": "帖子提到客厅", "description": "描述",
         "evidence_posts": [{"reddit_id": "p1",
                             "evidence_quote": "I bought this glider and it is comfortable"}]},
    ],
    "pre_purchase_concerns": [
        {"label_cn": "选购问推荐", "count": 1, "percentage": 100.0, "reason": "r",
         "evidence_posts": [{"reddit_id": "p2",
                             "evidence_quote": "Any recommendations for a nursery chair?"}]},
    ],
    "post_purchase_experience": [
        {"label_cn": "舒适度好", "sentiment": "positive", "count": 1, "percentage": 100.0,
         "reason": "r",
         "evidence_posts": [{"reddit_id": "c1", "unit_type": "comment",
                             "post_reddit_id": "p1",
                             "evidence_quote": "I love the swivel, used it daily."}]},
    ],
    "unmet_needs": [
        {"label_cn": "需要更大尺码", "count": 1, "percentage": 100.0, "reason": "r",
         "evidence_posts": [{"reddit_id": "p1", "evidence_quote": "it is comfortable"}]},
    ],
    "user_personas_purchased": [
        {"label_cn": "新手父母", "count": 2, "post_count": 1, "comment_count": 1,
         "percentage": 66.7, "reason": "r",
         "evidence_posts": [
             {"reddit_id": "p1", "unit_type": "post",
              "evidence_quote": "I bought this glider"},
             {"reddit_id": "c1", "unit_type": "comment", "post_reddit_id": "p1",
              "evidence_quote": "My Babyletto is great."}]},
    ],
    "user_personas_non_purchased": [
        {"label_cn": "首次选购", "count": 1, "post_count": 1, "comment_count": 0,
         "percentage": 33.3, "reason": "r",
         "evidence_posts": [{"reddit_id": "p2", "unit_type": "post",
                             "evidence_quote": "Looking for a recliner"}]},
    ],
    "brand_mentions": [
        {"label_cn": "Babyletto", "count": 1, "percentage": 100.0,
         "sentiment_distribution": {"positive": 1, "negative": 0, "neutral": 0},
         "evidence_posts": [{"reddit_id": "c1", "unit_type": "comment",
                             "post_reddit_id": "p1",
                             "evidence_quote": "My Babyletto is great."}]},
    ],
    "purchase_status_distribution": [
        {"label_cn": "已购买者", "status": "purchased", "count": 2,
         "post_count": 1, "comment_count": 1, "percentage": 66.7},
        {"label_cn": "未购买者", "status": "non_purchased", "count": 1,
         "post_count": 1, "comment_count": 0, "percentage": 33.3},
    ],
    "unit_counts": {"post_units": 2, "comment_units": 1, "excluded_unknown_units": 0,
                    "persona_scope_rule": "人群画像 = 帖子 + 评论合并计数"},
}

_FEATURES = {
    "category": "测试品类",
    "features": [
        {"name": "摇摆滑轨", "reason": "r", "total": 3, "positive": 1, "negative": 1,
         "neutral": 1, "satisfaction": 1 / 3,
         "evidence_posts": [{"reddit_id": "p1", "mention_count": 1}]},
    ],
    "brand_feature_matrix": {
        "Babyletto": {"摇摆滑轨": {"total": 1, "satisfaction": 1.0, "evidence_posts": []}},
    },
    "persona_feature_matrix": {
        "新手父母": {"摇摆滑轨": {"total": 1, "satisfaction": 0.5, "evidence_posts": []}},
    },
}

_CLEAN_HTML = '<div class="card"><div id="chart-channels-small"></div></div>'


def _audit(*, stats=None, dims=None, feats=None, tagged=None, comments=None,
           report="", dashboard="", profile="reddit", **kw) -> dict:
    """用基线产物跑一遍闸门；传参即覆盖对应部分（其余仍为基线）。"""
    ctx = Context(
        stats=copy.deepcopy(_STATS if stats is None else stats),
        dimensions=copy.deepcopy(_DIMENSIONS if dims is None else dims),
        features=copy.deepcopy(_FEATURES if feats is None else feats),
        tagged=copy.deepcopy(_POSTS if tagged is None else tagged),
        relevant=copy.deepcopy(_POSTS if tagged is None else tagged),
        comments=copy.deepcopy(_COMMENTS if comments is None else comments),
        report_text=report,
        dashboard_html=dashboard,
        profile_name=profile,
        **kw,
    )
    return audit(ctx)


def _failed(report: dict) -> set[str]:
    return {f["check"] for f in report["findings"] if f["status"] == "fail"}


def _by_check(report: dict, check: str) -> dict:
    return next(f for f in report["findings"] if f["check"] == check)


# ==========================================================================
# 0. 基线自检 —— 好数据必须全绿（否则下面的变异测试没有意义）
# ==========================================================================

class TestBaselineIsClean(unittest.TestCase):
    """先证明基线产物**全绿**。基线不绿的话，后面每个变异用例都失去意义。"""

    def test_all_checks_pass(self):
        r = _audit()
        self.assertEqual(_failed(r), set(), "基线产物不该有任何失败项")
        self.assertEqual(r["summary"]["verdict"], "pass")

    def test_every_check_id_is_covered_by_a_mutation_case(self):
        """每个检查项都要在本文件里出现一次变异用例 —— **没验过的检查项等于没写**。

        检查项长起来比用例快是这类闸门最典型的腐化方式：不断加规则，却没人证明
        新规则真的会拦住东西。这条断言把"加检查项就必须加变异用例"钉死。
        """
        src = Path(__file__).read_text(encoding="utf-8")
        missing = [c.id for c in CHECKS if c.id not in src]
        self.assertEqual(missing, [], f"这些检查项没有对应的变异用例：{missing}")


# ==========================================================================
# 1. 变异测试：逐条注错，断言**恰好**是那条检查项翻红
# ==========================================================================

class TestMutation(unittest.TestCase):
    """一条缺陷 → 一条检查项翻红。断言的是**集合相等**，因此同时证明了"不误报"。"""

    # -- 数据层 ----------------------------------------------------------

    def test_dup_unit_ids(self):
        rows = copy.deepcopy(_POSTS) + [copy.deepcopy(_POSTS[1])]
        r = _audit(tagged=rows)
        self.assertEqual(_failed(r), {"dup_unit_ids"})

    def test_bucket_accounting(self):
        st = copy.deepcopy(_STATS)
        st["bucket_D"] = 5          # A+B+C+D 不再等于 after_date_clean
        r = _audit(stats=st)
        self.assertEqual(_failed(r), {"bucket_accounting"})

    def test_date_missing_rate(self):
        rows = copy.deepcopy(_POSTS)
        for x in rows:
            x["published"] = ""     # 100% 无日期 → 时间线不可用
        r = _audit(tagged=rows)
        self.assertEqual(_failed(r), {"date_missing_rate"})

    def test_control_chars(self):
        rows = copy.deepcopy(_POSTS)
        rows[0]["title"] = "glider\bpattern"    # \b 被吞成退格符
        r = _audit(tagged=rows)
        self.assertEqual(_failed(r), {"control_chars"})

    def test_brand_undercount(self):
        d = copy.deepcopy(_DIMENSIONS)
        d["brand_mentions"][0]["count"] = 0
        d["brand_mentions"][0]["sentiment_distribution"] = {
            "positive": 0, "negative": 0, "neutral": 0}
        d["brand_mentions"][0]["evidence_posts"] = []
        r = _audit(dims=d)
        self.assertEqual(_failed(r), {"brand_undercount"})

    def test_brand_column_stale(self):
        rows = copy.deepcopy(_POSTS)
        for x in rows:
            x["mentioned_brands"] = ""   # 帖级列被清空，讨论串里仍有品牌
        r = _audit(tagged=rows)
        self.assertEqual(_failed(r), {"brand_column_stale"})

    # -- 归属层 ----------------------------------------------------------

    def test_evidence_quote_verbatim(self):
        d = copy.deepcopy(_DIMENSIONS)
        d["usage_scenarios"][0]["evidence_posts"][0]["evidence_quote"] = \
            "这句话不在原文里，是模型改写的"
        r = _audit(dims=d)
        self.assertEqual(_failed(r), {"evidence_quote_verbatim"})

    def test_count_matches_evidence(self):
        d = copy.deepcopy(_DIMENSIONS)
        d["usage_scenarios"][0]["count"] = 0    # count < 证据条数，逻辑不可能
        r = _audit(dims=d)
        self.assertEqual(_failed(r), {"count_matches_evidence"})

    def test_persona_split_sums(self):
        d = copy.deepcopy(_DIMENSIONS)
        d["user_personas_purchased"][0]["comment_count"] = 0   # 1 + 0 != 2
        r = _audit(dims=d)
        self.assertEqual(_failed(r), {"persona_split_sums"})

    def test_brand_sentiment_sums(self):
        d = copy.deepcopy(_DIMENSIONS)
        d["brand_mentions"][0]["sentiment_distribution"] = {
            "positive": 1, "negative": 1, "neutral": 1}        # 和 = 3 != count 1
        r = _audit(dims=d)
        self.assertEqual(_failed(r), {"brand_sentiment_sums"})

    def test_interrogative_evidence(self):
        """体验类证据换成疑问句 —— 引文仍在原文里，只有这一条翻红。"""
        d = copy.deepcopy(_DIMENSIONS)
        d["post_purchase_experience"][0]["evidence_posts"] = [
            {"reddit_id": "p2", "unit_type": "post",
             "evidence_quote": "Any recommendations for a nursery chair?"}]
        r = _audit(dims=d)
        self.assertEqual(_failed(r), {"interrogative_evidence"})

    def test_interrogative_evidence_allows_questions_elsewhere(self):
        """反例：换到「购买前关注点」就不该报 —— 那里本来就是问句。"""
        r = _audit()
        self.assertNotIn("interrogative_evidence", _failed(r))

    def test_evidence_ids_and_unit_type(self):
        d = copy.deepcopy(_DIMENSIONS)
        d["user_personas_purchased"][0]["evidence_posts"][0].pop("unit_type")
        r = _audit(dims=d)
        self.assertEqual(_failed(r), {"evidence_ids_unit_type"})

    def test_empty_evidence_id(self):
        """证据没有 id 是**两个**问题：看板会跳过它，且它无法被回溯。

        这里断言两条同时翻红 —— 不是缺陷，是「空 id 就是不可核查的」这件事的如实反映。
        """
        d = copy.deepcopy(_DIMENSIONS)
        d["usage_scenarios"][0]["evidence_posts"][0]["reddit_id"] = ""
        r = _audit(dims=d)
        self.assertEqual(_failed(r), {"evidence_ids_unit_type", "evidence_quote_verbatim"})

    def test_required_structure(self):
        d = copy.deepcopy(_DIMENSIONS)
        d.pop("unit_counts")
        r = _audit(dims=d)
        self.assertEqual(_failed(r), {"required_structure"})

    def test_label_cn_format(self):
        d = copy.deepcopy(_DIMENSIONS)
        d["usage_scenarios"][0]["label_cn"] = "家庭/亲子（客厅）"
        r = _audit(dims=d)
        self.assertEqual(_failed(r), {"label_cn_format"})

    def test_missing_reason(self):
        d = copy.deepcopy(_DIMENSIONS)
        d["usage_scenarios"][0]["reason"] = ""
        r = _audit(dims=d)
        self.assertEqual(_failed(r), {"label_cn_format"})

    def test_percentage_range(self):
        d = copy.deepcopy(_DIMENSIONS)
        d["usage_scenarios"][0]["percentage"] = 150.0   # 契约是 0–100
        r = _audit(dims=d)
        self.assertEqual(_failed(r), {"percentage_range"})

    # -- 功能层 ----------------------------------------------------------

    def test_feature_contract_missing_field(self):
        f = copy.deepcopy(_FEATURES)
        f["features"][0].pop("reason")
        r = _audit(feats=f)
        self.assertEqual(_failed(r), {"feature_contract"})

    def test_feature_contract_legacy_name(self):
        f = copy.deepcopy(_FEATURES)
        f["features"][0]["satisfaction_rate"] = 33.3    # 历史字段名 → 看板整页空白
        r = _audit(feats=f)
        self.assertEqual(_failed(r), {"feature_contract"})

    def test_feature_satisfaction(self):
        f = copy.deepcopy(_FEATURES)
        f["features"][0]["satisfaction"] = 0.9          # 应为 1/(1+1+1)
        r = _audit(feats=f)
        self.assertEqual(_failed(r), {"feature_satisfaction"})

    def test_feature_matrix_cell(self):
        f = copy.deepcopy(_FEATURES)
        f["brand_feature_matrix"]["Babyletto"]["摇摆滑轨"].pop("total")
        r = _audit(feats=f)
        self.assertEqual(_failed(r), {"feature_matrix"})

    # -- 呈现层 ----------------------------------------------------------

    def test_presentation_vocabulary(self):
        r = _audit(profile="wayfair", report="# 报告\n\n## 三、高频子版块\n")
        self.assertEqual(_failed(r), {"presentation_vocabulary"})

    def test_presentation_vocabulary_allows_own_source_words(self):
        """反例：Reddit 源自己的词不算泄漏。"""
        r = _audit(profile="reddit", report="# 报告\n\n## 三、高频子版块\n")
        self.assertEqual(_failed(r), set())

    def test_presentation_identifiers(self):
        r = _audit(profile="wayfair",
                   dashboard='<div id="chart-subreddits-small"></div>')
        self.assertEqual(_failed(r), {"presentation_identifiers"})

    def test_presentation_placeholders(self):
        r = _audit(profile="wayfair", dashboard='<p>[链接]()</p>')
        self.assertEqual(_failed(r), {"presentation_placeholders"})

    # -- 校验器自身的健壮性 ----------------------------------------------

    def test_validator_never_raises_on_garbage(self):
        """输入是垃圾时应当报错，而不是把校验器自己炸掉。"""
        r = _audit(stats={}, dims={"usage_scenarios": "不是列表"},
                   feats={"features": [None, 3]}, tagged=[{}, {}], comments=[{}])
        self.assertIsInstance(r["summary"]["verdict"], str)   # 没抛异常即达标
        self.assertTrue(any(f["status"] == "fail" for f in r["findings"]))

    def test_finding_carries_source_provenance(self):
        """每条失败都要能指出它在 SKILL.md 里的出处 —— 否则翻红时无从判断。"""
        r = _audit(tagged=copy.deepcopy(_POSTS) + [copy.deepcopy(_POSTS[0])])
        f = _by_check(r, "dup_unit_ids")
        self.assertEqual(f["status"], "fail")
        self.assertIn("SKILL.md", f["source"])


# ==========================================================================
# 2. 端到端：CLI 退出码 + 真实产物
# ==========================================================================

class TestValidateCli(unittest.TestCase):

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess:
        env = dict(os.environ, PYTHONHASHSEED="0", PYTHONIOENCODING="utf-8")
        return subprocess.run([PY, str(VOC), *argv], capture_output=True,
                              text=True, encoding="utf-8", env=env)

    def test_list_checks(self):
        r = self._run(["validate", "--list-checks"])
        self.assertEqual(r.returncode, 0)
        for c in CHECKS:
            self.assertIn(c.id, r.stdout)

    @unittest.skipUnless(DATA.exists(), "缺少真实数据")
    def test_real_reddit_artifacts_pass(self):
        """真实 Reddit 产物应通过（可以有警告，但不得有 error）。"""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "quality.json"
            r = self._run([
                "validate",
                "--stats", str(DATA / "reddit_analysis_stats.json"),
                "--dimensions", str(REPORTS / "reddit_analysis_dimensions.json"),
                "--out", str(out), "--json",
            ])
            self.assertEqual(r.returncode, 0, r.stdout[-800:] + r.stderr[-400:])
            rep = json.loads(out.read_text(encoding="utf-8"))
            errors = [f["check"] for f in rep["findings"]
                      if f["status"] == "fail" and f["severity"] == "error"]
            self.assertEqual(errors, [], f"真实产物不该有 error 级失败：{errors}")
            # 引文回溯必须真的跑了（不是被跳过），且命中率高
            quote = next(f for f in rep["findings"] if f["check"] == "evidence_quote_verbatim")
            self.assertEqual(quote["status"], "pass")
            self.assertGreater(quote["detail"]["total"], 100)

    @unittest.skipUnless(DATA.exists(), "缺少真实数据")
    def test_bad_data_is_blocked_with_exit_code_1(self):
        """验收核心：故意灌坏数据 → 退出码 1 + 说清是哪条、为什么。"""
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            dims = json.loads(
                (REPORTS / "reddit_analysis_dimensions.json").read_text(encoding="utf-8"))
            # 注错 1：把引文改成原文里没有的话
            dims["usage_scenarios"][0]["evidence_posts"][0]["evidence_quote"] = "伪造的引文"
            # 注错 2：把占比改到契约外
            dims["usage_scenarios"][0]["percentage"] = 999
            (d / "dims.json").write_text(json.dumps(dims, ensure_ascii=False), encoding="utf-8")

            out = d / "quality.json"
            r = self._run([
                "validate",
                "--stats", str(DATA / "reddit_analysis_stats.json"),
                "--dimensions", str(d / "dims.json"),
                "--out", str(out),
            ])
            self.assertEqual(r.returncode, 1, "坏数据必须让闸门返回非零退出码")
            self.assertIn("不通过", r.stdout)
            rep = json.loads(out.read_text(encoding="utf-8"))
            failed = _failed(rep)
            self.assertIn("evidence_quote_verbatim", failed)
            self.assertIn("percentage_range", failed)
            # 「说清原因」：消息里要能读到未命中率与越界值
            q = _by_check(rep, "evidence_quote_verbatim")
            self.assertIn("未命中率", q["message"])
            self.assertTrue(q["detail"]["offenders"])
            self.assertEqual(q["detail"]["offenders"][0]["quote"], "伪造的引文")


@unittest.skipUnless(
    DATA.exists() and (REPORTS / "reddit_analysis_dimensions.json").exists(),
    "缺少真实产物，跳过坏数据验收演示",
)
class TestBadDataDemo(unittest.TestCase):
    """验收流程本身也要被守住：每处注入都必须被它**声明的**检查项拦住。

    ``tools/make_bad_data.py`` 是路线图里写明的验收方式（"故意灌一份坏数据，闸门
    能拦住并说清原因"）。如果哪天它悄悄漏掉几处，这条测试会先红。
    """

    def test_all_injections_are_caught_by_their_expected_check(self):
        sys.path.insert(0, str(ROOT / "tools"))
        from make_bad_data import CASES  # noqa: E402

        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ, PYTHONHASHSEED="0", PYTHONIOENCODING="utf-8")
            r = subprocess.run([
                PY, str(ROOT / "tools" / "make_bad_data.py"),
                "--stats", str(DATA / "reddit_analysis_stats.json"),
                "--dimensions", str(REPORTS / "reddit_analysis_dimensions.json"),
                "--features-json", str(DATA / "reddit_analysis_features.json"),
                "--tagged", str(DATA / "reddit_posts_tagged.csv"),
                "--comments", str(DATA / "reddit_comments.csv"),
                "--out-dir", td,
            ], capture_output=True, text=True, encoding="utf-8", env=env)
            self.assertEqual(r.returncode, 0,
                             "有注入漏网：\n" + r.stdout[-1500:] + r.stderr[-400:])
            self.assertNotIn("未被其预期检查项命中", r.stdout)
            # 「没跑」不能算过：每处注入都必须真的落下去
            self.assertNotIn("没有真正落下去", r.stdout,
                             "有注入没落下去却仍判通过：\n" + r.stdout[-1500:])

            rep = json.loads((Path(td) / "quality_report.json").read_text(encoding="utf-8"))
            caught = _failed(rep)
            for name, _fn, expects, _why in CASES:
                self.assertIn(expects, caught, f"注入 {name} 未被 {expects} 拦住")

    def test_missing_input_refuses_to_run(self):
        """输入路径写错时必须**拒绝运行**，而不是静默跳过注入后报"全绿"。

        这是真实踩过的坑：把 ``data/reddit_analysis/...`` 当成 vue-platform 下的相对
        路径，文件其实在项目根 —— 脚本照常打印一大段结果、退出码 0，看不出什么都没验。
        """
        with tempfile.TemporaryDirectory() as td:
            bogus = Path(td) / "does-not-exist.json"
            out = Path(td) / "out"
            r = subprocess.run([
                PY, str(ROOT / "tools" / "make_bad_data.py"),
                "--stats", str(bogus),
                "--dimensions", str(REPORTS / "reddit_analysis_dimensions.json"),
                "--features-json", str(bogus),
                "--tagged", str(bogus),
                "--comments", str(bogus),
                "--out-dir", str(out),
            ], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("拒绝运行", r.stdout)
            self.assertFalse(out.exists(), "拒绝运行时不应留下任何产物目录")


if __name__ == "__main__":
    unittest.main(verbosity=2)
