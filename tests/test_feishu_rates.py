"""Synthetic sales-table precedence, zero-event coverage and summary safeguards."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import aftersale, eccang_sales, feishu_sales, shop_filter
from server.aftersale_rates import report_rate, combine_rates
from server.sku_merge import SkuGrouper

URL = "https://example.feishu.cn/sheets/shtSynthetic?sheet=sheetSynthetic"


class FeishuRateTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.settings = feishu_sales.save_settings(self.root, url=URL)
        self.values = [feishu_sales.HEADERS,
                       ["Shop A", "MR221118BR", "2026-10-01", 100],
                       ["Shop A", "MR221118BR", "2026-11-01", 50],
                       ["Shop B", "MR12345", "2026-10-01", 200],
                       ["Shop Zero", "MR99999", "2026-10-01", 0]]
        self.cache = feishu_sales.build_cache(self.values, self.settings, self.root)
        self.save_cache(self.cache)

    def save_cache(self, cache):
        feishu_sales._save(self.root, feishu_sales._CACHE, cache)

    @staticmethod
    def event(shop="Shop A", sku="MR221118BR", date="2026-10-20", **extra):
        return {"店铺": shop, "SKU": sku, "日期": date, "售后原因": "破损", **extra}

    def analyze(self, rows=None, platform="AmazonCs"):
        rows = [self.event()] if rows is None else rows
        with patch.object(aftersale.adapters, "read_table", return_value=(rows, "synthetic")):
            return aftersale.run_aftersale(files=[self.root / "synthetic.csv"], platform=platform,
                category="", out_dir=self.root / "report", data_dir=self.root)["aftersale"]

    def test_table_priority_zero_event_shops_products_missing_shops_no_fallback(self):
        # No Eccang cache/configuration is consulted when this platform has a table.
        with patch.object(eccang_sales, "load", side_effect=AssertionError("Eccang consulted")):
            a = self.analyze([self.event(), self.event(shop="Missing"),
                              self.event(date="2026-12-01")])
        self.assertEqual(a["rate_basis"], "feishu_qty")
        self.assertEqual(a["overall_rate"], round(1 / 350, 4))
        self.assertEqual(a["sales"]["numerator_used"], 1)
        self.assertEqual(a["sales"]["qty_used"], 350)
        shops = {s["shop"]: s for s in a["shops"]}
        self.assertEqual(shops["Shop A"]["sales_qty"], 150)
        self.assertEqual(shops["Shop B"]["rate"], 0)
        self.assertEqual(shops["Shop B"]["records"], 0)
        self.assertIsNone(shops["Missing"]["rate"])
        self.assertIsNone(shops["Shop Zero"]["rate"])
        family = SkuGrouper(self.root).family("MR12345")
        self.assertEqual(next(p for p in a["products"] if p["product"] == family)["rate"], 0)
        self.assertEqual(a["sales"]["no_denom"], 2)

    def test_failed_read_disables_rates_even_with_complete_eccang_cache(self):
        failed = deepcopy(self.cache)
        failed.update(complete=False, error="Synthetic permission failure")
        self.save_cache(failed)
        with patch.object(eccang_sales, "load", side_effect=AssertionError("Eccang fallback")):
            a = self.analyze()
        self.assertEqual(a["rate_basis"], "none")
        self.assertIsNone(a["overall_rate"])
        self.assertTrue(all(s["rate"] is None for s in a["shops"]))

    def test_all_zero_events_table_and_whitelist_scope(self):
        a = self.analyze([])
        self.assertEqual(a["overall_rate"], 0)
        self.assertEqual(a["sales"]["qty_used"], 350)
        shop_filter.save(self.root, ["Shop B"])
        a = self.analyze([self.event()])
        self.assertEqual(a["overall_rate"], 0)
        self.assertEqual(a["sales"]["qty_used"], 200)
        self.assertEqual([s["shop"] for s in a["shops"]], ["Shop B"])

    def test_detail_mode_remains_table_internal_and_platform_scope_isolated(self):
        a = self.analyze([self.event(**{"是否售后": "是"}),
                          self.event(**{"是否售后": "否"})])
        self.assertEqual(a["mode"], "detail")
        self.assertEqual(a["rate_basis"], "detail")
        self.assertEqual(a["overall_rate"], .5)
        a = self.analyze(platform="WayfairCs")
        self.assertEqual(a["rate_basis"], "none")
        self.assertNotIn("Shop B", [s["shop"] for s in a["shops"]])

    def test_summary_accepts_table_basis_rejects_overlap_and_mixed_basis(self):
        a = self.analyze()
        part = report_rate(a)
        self.assertEqual(part["basis"], "feishu_qty")
        self.assertEqual(combine_rates([part])["rate"], a["overall_rate"])
        self.assertIsNone(combine_rates([part, part])["rate"])
        self.assertTrue(combine_rates([part, part])["rate_warning"])
        other = deepcopy(part)
        other["basis"] = "eccang_qty"
        self.assertIsNone(combine_rates([part, other])["rate"])
        incomplete = deepcopy(a)
        incomplete["sales"]["source_id"] = ""
        self.assertEqual(report_rate(incomplete)["basis"], "none")
        other = deepcopy(part)
        for d in other["denominators"]:
            d["source_id"] = "other-worksheet"
        self.assertIsNotNone(combine_rates([part, other])["rate"])


if __name__ == "__main__":
    unittest.main()
