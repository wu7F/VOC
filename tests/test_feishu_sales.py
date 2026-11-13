"""Synthetic strict worksheet snapshot and credential-independent read tests."""
import json
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import feishu_sales, feishu
from server.sku_merge import SkuGrouper

URL = "https://example.feishu.cn/sheets/shtSynthetic?sheet=sheetSynthetic"


class FeishuSalesTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.settings = feishu_sales.save_settings(self.root, url=URL)

    def values(self):
        return [feishu_sales.HEADERS, ["Shop A", "MR221118BR", "2026-10-01", 3],
                ["Shop A", "MR221118-BR", "2026-10-02", "7"],
                ["Shop A", "MR221118BR", "2026-11-01", 4],
                ["Shop B", "MR12345", "2026-10-31", 0]]

    def test_daily_skus_aggregate_calendar_months_and_families(self):
        cache = feishu_sales.build_cache(self.values(), self.settings, self.root)
        family = SkuGrouper(self.root).family("MR221118BR")
        self.assertEqual(cache["qty"]["shop a"], {"2026-10": 10, "2026-11": 4})
        self.assertEqual(cache["qty_by_family"]["shop a"]["2026-10"][family], 10)
        self.assertEqual(cache["units"], 14)
        self.assertTrue(feishu_sales.is_complete(cache))

    def test_bad_rows_and_duplicate_daily_keys_reject_entire_snapshot(self):
        cases = [
            [feishu_sales.HEADERS, ["Shop A", "MR221118BR", "2026-02-30", 2]],
            [feishu_sales.HEADERS, ["Shop A", "MR221118BR", "2026-10-01", -1]],
            [feishu_sales.HEADERS, ["Shop A", "MR221118BR", "2026-10-01", 1.5]],
            [feishu_sales.HEADERS, ["Shop A", "MR221118BR", "2026-10-01", "NaN"]],
            [feishu_sales.HEADERS, ["Shop A", "MR221118BR", "2026-10-01", "inf"]],
            [feishu_sales.HEADERS, ["Shop A", "", "2026-10-01", 2]],
            [feishu_sales.HEADERS, ["Shop A", "MR221118BR", "2026-10-01", 2],
             ["shop a", "mr221118br", "2026-10-01", 3]],
            [["店铺", "SKU", "日期", "件数"], ["Shop A", "MR221118BR", "2026-10-01", 2]],
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaises((ValueError, feishu.FeishuError)):
                feishu_sales.build_cache(values, self.settings, self.root)

    def test_read_needs_only_existing_feishu_config_and_failure_invalidates_cache(self):
        with patch("server.feishu_sales_writer.read_sheet", return_value=self.values()) as read:
            result = feishu_sales.sync(self.root)
            self.assertTrue(result["complete"])
            read.assert_called_once_with(self.root, url=URL, locked=True)
        with patch("server.feishu_sales_writer.read_sheet", side_effect=RuntimeError("PRIVATE-DETAIL")):
            result = feishu_sales.sync(self.root)
        self.assertFalse(result["complete"])
        self.assertEqual(result["units"], 0)
        self.assertNotIn("PRIVATE-DETAIL", json.dumps(result))

    def test_target_or_platform_change_invalidates_cached_quantities(self):
        with patch("server.feishu_sales_writer.read_sheet", return_value=self.values()):
            feishu_sales.sync(self.root)
        feishu_sales.save_settings(self.root, url=URL.replace("sheetSynthetic", "sheetOther"))
        self.assertFalse(feishu_sales.status(self.root)["complete"])
        feishu_sales.save_settings(self.root, url=URL, platform="WayfairCs")
        self.assertFalse(feishu_sales.status(self.root)["complete"])

    def test_pending_write_blocks_previously_complete_snapshot(self):
        from server.feishu_sales_writer import _pending_path
        with patch("server.feishu_sales_writer.read_sheet", return_value=self.values()):
            feishu_sales.sync(self.root)
        self.assertTrue(feishu_sales.status(self.root)["complete"])
        marker = _pending_path(self.root, URL)
        marker.write_text("synthetic pending write", encoding="utf-8")
        self.assertFalse(feishu_sales.status(self.root)["complete"])
        marker.unlink()
        feishu_sales.invalidate(self.root)
        self.assertFalse(feishu_sales.status(self.root)["complete"])

    def test_settings_require_explicit_worksheet_and_amazon_write_scope(self):
        cases = [{"url": URL.split("?")[0]}, {"url": URL, "auto_sync": True},
                 {"url": URL, "write_back": True, "platform": "WayfairCs"}]
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                feishu_sales.save_settings(self.root, **fields)


if __name__ == "__main__":
    unittest.main()
