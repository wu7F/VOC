"""Synthetic end-to-end Team fields in the existing summary API."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import aftersale, feishu_sales
from server import app as api


ACCOUNTS = ["xingshan_US", "KornerHK_US", "southwest_US", "Wekas_US",
            "kaifengzhejie_US", "maoyitong_US", "AmazonMJSKY_US_US", "RUIBAO_US_US"]


class TeamApiTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)

    def report(self, name, month="2026-10"):
        root = self.root / name
        root.mkdir()
        settings = feishu_sales.save_settings(root,
            url="https://example.feishu.cn/sheets/SYNTHETIC?sheet=team")
        values = [feishu_sales.HEADERS] + [
            [account, "MR12345", month + "-01", qty]
            for account, qty in zip(ACCOUNTS, [100, 900, 1000, 10, 90, 10, 20, 30])]
        cache = feishu_sales.build_cache(values, settings, root)
        feishu_sales._save(root, feishu_sales._CACHE, cache)
        events = [{"店铺": "xingshan_US", "SKU": "MR12345", "日期": month + "-12",
                   "售后原因": "破损"} for _ in range(10)]
        with patch.object(aftersale.adapters, "read_table", return_value=(events, "synthetic")):
            return aftersale.run_aftersale(files=[root / "synthetic.csv"],
                platform="AmazonCs", category="", out_dir=root, data_dir=root)

    def summary(self, reports):
        recs = []
        for name, report in reports.items():
            root = self.root / ("saved-" + name)
            root.mkdir()
            (root / "result.json").write_text(json.dumps(report), encoding="utf-8")
            recs.append({"id": name, "platform": report["platform"], "status": "done"})
        class SyntheticStore:
            _data = {"datasets": recs}
            def dataset_dir(_, did):
                return self.root / ("saved-" + did)
        with patch.object(api, "store", SyntheticStore()):
            return api.api_aftersale_summary()

    def test_summary_uses_exact_team_inputs_and_isolates_wayfair(self):
        amazon = self.report("amazon")
        wayfair = deepcopy(amazon)
        wayfair["platform"] = "WayfairCs"
        wayfair["aftersale"]["platform"] = "WayfairCs"
        result = self.summary({"amazon": amazon, "wayfair": wayfair})
        self.assertEqual([row["team"] for row in result["teams"]],
                         ["Team 1", "Team 2", "Team 3"])
        self.assertEqual(result["teams"][0]["rate"], .005)
        self.assertEqual(result["teams"][0]["sales_qty"], 2000)
        self.assertEqual(result["teams"][0]["aftersale"], 10)
        self.assertEqual(result["teams"][1]["rate"], 0)

    def test_overlapping_reports_hide_team_rate_and_disjoint_months_merge(self):
        first = self.report("oct")
        overlap = self.summary({"oct": first, "copy": first})
        self.assertIsNone(overlap["teams"][0]["rate"])
        self.assertTrue(overlap["teams"][0]["rate_warning"])
        # Every summary call writes different saved directories.
        november = self.report("nov", "2026-11")
        merged = self.summary({"old": first, "new": november})
        self.assertEqual(merged["teams"][0]["rate"], .005)
        self.assertEqual(merged["teams"][0]["sales_qty"], 4000)

    def test_ambiguous_shop_mapping_disables_legacy_summary_rates_but_keeps_valid_teams(self):
        report = self.report("ambiguous")
        report["aftersale"]["sales"]["mapping_warning"] = "销量店名映射不唯一，暂不计算整体与产品合并率。"
        report["aftersale"]["overall_rate"] = None
        summary = self.summary({"ambiguous": report})
        self.assertIsNone(summary["overall"]["rate"])
        self.assertTrue(all(product["rate"] is None for product in summary["products"]))
        self.assertIn("店名映射不唯一", summary["rate_warning"])
        self.assertEqual(summary["teams"][0]["rate"], .005)

    def test_old_amazon_reports_require_reanalysis_and_other_platform_only_has_no_teams(self):
        old = self.report("legacy")
        old["aftersale"].pop("teams", None)
        result = self.summary({"legacy": old})
        self.assertTrue(result["teams"])
        self.assertTrue(all(row["rate"] is None for row in result["teams"]))
        self.assertTrue(all(row["rate_warning"] for row in result["teams"]))
        self.assertTrue(all(row["aftersale"] is None for row in result["teams"]))
        current = self.report("current")
        mixed = self.summary({"oldmix": old, "currentmix": current})
        self.assertIsNone(mixed["teams"][0]["aftersale"])
        self.assertIsNone(mixed["teams"][0]["rate_numerator"])
        wayfair = deepcopy(old)
        wayfair["platform"] = "WayfairCs"
        wayfair["aftersale"]["platform"] = "WayfairCs"
        self.assertEqual(self.summary({"wayfair": wayfair})["teams"], [])


if __name__ == "__main__":
    unittest.main()
