#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 8 · 评测回归的测试（CI 入口）。

分三层，速度从快到慢：

1. **比对逻辑**（毫秒）—— 漂移判定、锚点校验的边界。这些是纯函数，
   用构造出来的指标字典喂，不碰任何文件。
2. **评测案契约**（毫秒）—— golden set 自身完整：每个案有 case.json、
   声明的输入路径存在、mode 合法。
3. **端到端回归**（~15s）—— 真跑一遍 ``reddit-ng-consumer``（用归档的真实
   标注），断言**人工锚点全过**且**相对冻结基线零漂移**。这条就是 CI 里
   「退化能自动发现」的执行体；归档输入不在时（换机器/裁剪过仓库）自动跳过。

变异验证（证明这些测试真的守得住）见 ``tools/_mutate_stage8.py``。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
if str(PLATFORM) not in sys.path:
    sys.path.insert(0, str(PLATFORM))

from voc_core import eval as EV  # noqa: E402

#: 端到端回归用的案（最省钱、信号最高的一条）。
CORE_CASE = "reddit-ng-consumer"


def _metrics(**over: object) -> dict:
    """一份最小可用的指标字典，测试里按需覆写某一格。"""
    base: dict = {
        "schema": EV.EVAL_SCHEMA_VERSION,
        "case": "unit",
        "mode": EV.MODE_ANNOT_TAIL,
        "total_units": 7816,
        "units": {"post": 154, "comment": 7662},
        "axis": {"key": "purchase_status_distribution", "label": "购买状态",
                 "counts": {"purchased": 4223, "non_purchased": 116}},
        "excluded_unknown_units": 3477,
        "excluded_unknown_ratio": 0.4449,
        "persona_covered_units": 1284,
        "persona_coverage": 0.164278,
        "tag_covered_units": 2149,
        "tag_coverage": 0.274949,
        "evidence_total": 3433,
        "evidence_missed": 0,
        "evidence_verbatim_rate": 1.0,
        "dim_sizes": {"usage_scenarios": 7},
        "conclusions": {"usage_scenarios": [["客厅通用座椅", 88]]},
        "quote_anchors": [],
        "gate": {"verdict": "pass", "passed": 21, "failed": 0, "skipped": 0,
                 "failed_checks": []},
        "products": {"dimensions": "aaa", "features": "bbb"},
    }
    base.update(over)
    return base


class TestCompareMetrics(unittest.TestCase):
    """漂移判定：一致不报，变了要报，且报在正确的键上。"""

    def test_identical_metrics_have_no_drift(self):
        m = _metrics()
        self.assertEqual(EV.compare_metrics(m, dict(m)), [])

    def test_ignores_meta_keys(self):
        """schema / case / mode 是元信息，不参与漂移判定。"""
        a, b = _metrics(), _metrics()
        b.update(schema=99, case="别的", mode=EV.MODE_PIPELINE)
        self.assertEqual(EV.compare_metrics(a, b), [])

    def test_single_number_change_is_reported(self):
        a = _metrics()
        b = _metrics(excluded_unknown_units=7287)
        drift = EV.compare_metrics(a, b)
        self.assertEqual([d["key"] for d in drift], ["excluded_unknown_units"])
        self.assertEqual(drift[0]["baseline"], 3477)
        self.assertEqual(drift[0]["current"], 7287)
        self.assertEqual(drift[0]["severity"], "high")

    def test_conclusion_drift_is_high_severity(self):
        a = _metrics()
        b = _metrics(conclusions={"usage_scenarios": [["夜间喂养与哄睡", 74]]})
        drift = EV.compare_metrics(a, b)
        self.assertEqual([d["key"] for d in drift], ["conclusions"])
        self.assertEqual(drift[0]["severity"], "high")

    def test_nested_dict_change_is_detected(self):
        a = _metrics()
        b = _metrics(axis={"key": "purchase_status_distribution", "label": "购买状态",
                           "counts": {"purchased": 294, "non_purchased": 235}})
        self.assertEqual([d["key"] for d in EV.compare_metrics(a, b)], ["axis"])

    def test_drift_sorted_high_severity_first(self):
        a = _metrics()
        b = _metrics(excluded_unknown_units=1, gate={"verdict": "fail", "passed": 0,
                                                     "failed": 3, "skipped": 0,
                                                     "failed_checks": ["x"]},
                     products={"dimensions": "zzz", "features": "bbb"})
        sevs = [d["severity"] for d in EV.compare_metrics(a, b)]
        self.assertEqual(sevs, sorted(sevs, key=lambda s: EV._SEV_RANK[s]))
        self.assertEqual(sevs[0], "high")

    def test_new_metric_key_is_flagged_not_silently_ignored(self):
        """基线里没有、现在多了的键也要露头（新增指标得让人看见）。"""
        a = _metrics()
        b = _metrics(shiny_new_metric=1)
        drift = EV.compare_metrics(a, b)
        self.assertEqual([d["key"] for d in drift], ["shiny_new_metric"])
        self.assertIn("新增指标", drift[0].get("note", ""))

    def test_metric_missing_from_current_is_skipped(self):
        """当前指标缺了某一格 → 不在这里报（产物缺失由 run.ok 负责）。"""
        a = _metrics()
        b = _metrics()
        del b["tag_coverage"]
        self.assertEqual(EV.compare_metrics(a, b), [])


class TestCheckAnchors(unittest.TestCase):
    """锚点：写进去的必须成立，没写的不限制。"""

    def test_empty_anchors_never_fail(self):
        self.assertEqual(EV.check_anchors({}, _metrics()), [])

    def test_matching_anchors_pass(self):
        anchors = {
            "total_units": 7816,
            "purchase_status": {"purchased": 4223, "non_purchased": 116},
            "excluded_unknown_units": 3477,
            "top_labels": {"usage_scenarios": ["客厅通用座椅"]},
            "evidence_verbatim_rate_min": 0.98,
            "gate_verdict_in": ["pass", "warn"],
            "gate_failed_max": 0,
        }
        self.assertEqual(EV.check_anchors(anchors, _metrics()), [])

    def test_total_units_mismatch(self):
        bad = EV.check_anchors({"total_units": 7816}, _metrics(total_units=7000))
        self.assertEqual([v["key"] for v in bad], ["total_units"])

    def test_axis_counts_mismatch_is_named_per_bucket(self):
        """分段轴是最该守的地方 —— 报错要指名道姓说到哪个桶。"""
        bad = EV.check_anchors({"purchase_status": {"purchased": 4223, "non_purchased": 116}},
                               _metrics(axis={"key": "purchase_status_distribution",
                                              "label": "购买状态",
                                              "counts": {"purchased": 294,
                                                         "non_purchased": 235}}))
        self.assertEqual(sorted(v["key"] for v in bad),
                         ["purchase_status.non_purchased", "purchase_status.purchased"])
        self.assertIn("购买状态不明", bad[0]["note"])

    def test_unwritten_bucket_is_not_checked(self):
        """只锚了 purchased，就只查 purchased —— non_purchased 变了不报。"""
        bad = EV.check_anchors({"purchase_status": {"purchased": 4223}},
                               _metrics(axis={"key": "purchase_status_distribution",
                                              "label": "购买状态",
                                              "counts": {"purchased": 4223,
                                                         "non_purchased": 999}}))
        self.assertEqual(bad, [])

    def test_top_label_position_matters(self):
        bad = EV.check_anchors({"top_labels": {"usage_scenarios": ["客厅通用座椅"]}},
                               _metrics(conclusions={"usage_scenarios":
                                                     [["夜间喂养与哄睡", 88]]}))
        self.assertEqual([v["key"] for v in bad], ["top_labels.usage_scenarios[0]"])
        self.assertIn("关键结论漂移", bad[0]["note"])

    def test_top_label_missing_position_is_a_violation(self):
        """锚了两个，实得一个 → 第二个算违反，不是静默通过。"""
        bad = EV.check_anchors({"top_labels": {"usage_scenarios": ["客厅通用座椅", "夜间喂养与哄睡"]}},
                               _metrics(conclusions={"usage_scenarios": [["客厅通用座椅", 88]]}))
        self.assertEqual([v["key"] for v in bad], ["top_labels.usage_scenarios[1]"])
        self.assertIsNone(bad[0]["actual"])

    def test_verbatim_floor_is_inclusive(self):
        """下界是闭区间：正好等于下限算通过，跌破才报。"""
        floor = {"evidence_verbatim_rate_min": 0.98}
        self.assertEqual(EV.check_anchors(floor, _metrics(evidence_verbatim_rate=0.98)), [])
        self.assertEqual(
            EV.check_anchors(floor, _metrics(evidence_verbatim_rate=1.0)), [])
        bad = EV.check_anchors(floor, _metrics(evidence_verbatim_rate=0.5))
        self.assertEqual([v["key"] for v in bad], ["evidence_verbatim_rate"])

    def test_gate_verdict_and_fail_cap(self):
        worse = _metrics(gate={"verdict": "fail", "passed": 18, "failed": 3,
                               "skipped": 0, "failed_checks": ["a", "b", "c"]})
        bad = EV.check_anchors({"gate_verdict_in": ["pass", "warn"], "gate_failed_max": 0},
                               worse)
        self.assertEqual(sorted(v["key"] for v in bad),
                         ["gate.failed", "gate.verdict"])

    def test_quote_anchor_not_traceable_is_a_violation(self):
        m = _metrics(quote_anchors=[{"dim": "usage_scenarios", "label_cn": "客厅通用座椅",
                                     "quote": "……", "label_present": True,
                                     "traceable": False}])
        bad = EV.check_anchors({"quote_anchors": [{}]}, m)
        self.assertEqual([v["key"] for v in bad], ["quote_anchors[0].quote"])

    def test_quote_anchor_label_disappeared_is_a_violation(self):
        m = _metrics(quote_anchors=[{"dim": "usage_scenarios", "label_cn": "客厅通用座椅",
                                     "quote": "x", "label_present": False,
                                     "traceable": True}])
        bad = EV.check_anchors({"quote_anchors": [{}]}, m)
        self.assertEqual([v["key"] for v in bad], ["quote_anchors[0].label_cn"])


class TestCaseLoading(unittest.TestCase):
    """golden set 自身的完整性。"""

    def test_discovers_three_cases(self):
        ids = [c.id for c in EV.discover_cases()]
        self.assertEqual(set(ids), {"reddit-ng-consumer", "pipeline-mock-consumer",
                                    "pipeline-mock-b2b"})

    def test_only_filter_keeps_order(self):
        cases = EV.discover_cases(only=["pipeline-mock-b2b", "reddit-ng-consumer"])
        self.assertEqual([c.id for c in cases],
                         ["pipeline-mock-b2b", "reddit-ng-consumer"])

    def test_only_filter_rejects_unknown_id(self):
        with self.assertRaises(ValueError):
            EV.discover_cases(only=["不存在的案"])

    def test_tier_filter(self):
        core = [c.id for c in EV.discover_cases(tier="core")]
        self.assertIn("reddit-ng-consumer", core)
        self.assertNotIn("pipeline-mock-b2b", core)

    def test_bad_mode_is_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "x"
            d.mkdir()
            (d / EV.CASE_FILE).write_text(
                json.dumps({"id": "x", "mode": "飞"}),
                encoding="utf-8")
            with self.assertRaises(ValueError):
                EV.load_case(d)

    def test_missing_case_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                EV.load_case(Path(td))

    def test_anchors_are_attached(self):
        case = next(c for c in EV.discover_cases(only=[CORE_CASE]))
        self.assertEqual(case.anchors.get("total_units"), 7816)
        self.assertTrue(case.anchors.get("quote_anchors"))

    def test_relative_paths_resolve_against_repo_root(self):
        case = next(c for c in EV.discover_cases(only=[CORE_CASE]))
        self.assertTrue(Path(case.paths["annot_dir"]).is_absolute())
        self.assertEqual(Path(case.paths["annot_dir"]).name, "ng2026")


class TestGoldenInputsPresent(unittest.TestCase):
    """声明的输入还在不在。不在就跳过（换机器/裁剪仓库时不该红）。"""

    def test_declared_annotation_inputs_exist(self):
        case = next(c for c in EV.discover_cases(only=[CORE_CASE]))
        missing = [k for k, v in case.paths.items() if not Path(v).exists()]
        if missing:
            self.skipTest(f"归档输入不在本机：{', '.join(missing)}")


class TestCoreCaseRegression(unittest.TestCase):
    """CI 的执行体：真跑一遍核心案，锚点全过 + 相对冻结基线零漂移。"""

    @classmethod
    def setUpClass(cls):
        case = next(c for c in EV.discover_cases(only=[CORE_CASE]))
        missing = [k for k, v in case.paths.items() if not Path(v).exists()]
        if missing:
            raise unittest.SkipTest(f"归档输入不在本机：{', '.join(missing)}")
        cls.case = case
        #: 属性名**绝不能**叫 ``run`` —— 那会覆盖 ``unittest.TestCase.run``，
        #: runner 一调 ``self.run(result)`` 就报 "CaseRun object is not callable"。
        cls.case_run = EV.run_case(case, PLATFORM / "out/verify/stage8",
                                   log=EV.SILENT)

    def test_case_runs(self):
        self.assertTrue(self.case_run.ok, self.case_run.error)

    def test_anchors_all_hold(self):
        metrics = EV.extract_metrics(self.case, self.case_run.products)
        bad = EV.check_anchors(self.case.anchors, metrics)
        self.assertEqual(bad, [], f"锚点违反：{json.dumps(bad, ensure_ascii=False)}")

    def test_no_drift_against_frozen_baseline(self):
        base = EV.load_baseline(self.case.id)
        self.assertTrue(base, f"基线缺失：{EV.baseline_path(self.case.id)}")
        metrics = EV.extract_metrics(self.case, self.case_run.products)
        base_cmp = {k: v for k, v in base.items() if k not in ("case", "mode")}
        cur_cmp = {k: v for k, v in metrics.items() if k not in ("case", "mode")}
        drift = EV.compare_metrics(base_cmp, cur_cmp)
        self.assertEqual(drift, [], f"指标漂移：{json.dumps(drift, ensure_ascii=False)[:800]}")

    def test_gate_not_worse_than_baseline(self):
        """闸门结论只许变好：基线是 pass，就不许出现 fail。"""
        base = EV.load_baseline(self.case.id) or {}
        was = (base.get("gate") or {}).get("verdict")
        if was is None:
            self.skipTest("基线里没有闸门结论")
        metrics = EV.extract_metrics(self.case, self.case_run.products)
        now = (metrics.get("gate") or {}).get("verdict")
        self.assertLessEqual(EV.VERDICT_ORDER.get(now, 9),
                             EV.VERDICT_ORDER.get(was, 9),
                             f"闸门变差：{was} → {now}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
