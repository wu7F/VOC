"""Offline Sheets writer regression; all rows, HTTP and credentials are synthetic."""
from copy import deepcopy
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import feishu_sales_writer as writer
from server import feishu
from server.sales_lock import try_lock

URL = "https://example.feishu.cn/sheets/shtSynthetic?sheet=SheetSynthetic"
H = writer.HEADERS


def row(shop="ShopA", sku="SKU1", day="2026-10-01", qty=1):
    return dict(zip(H, [shop, sku, day, qty]))


class Sheet:
    def __init__(self, values=None, grid=20):
        self.values = deepcopy(values or [])
        self.grid = grid
        self.revision = 1
        self.calls = []
        self.write_count = 0
        self.fail_after_apply = 0
        self.bad_receipt = False
        self.change_revision = False
        self.merges = []

    def http(self, url, *, method="GET", body=None, **kwargs):
        self.calls.append((method, url, deepcopy(body)))
        if url.endswith("/sheets/query"):
            return {"code": 0, "data": {"sheets": [{
                "sheet_id": "SheetSynthetic", "resource_type": "sheet",
                "grid_properties": {"row_count": self.grid, "column_count": 4},
                "merges": self.merges}]}}
        if method == "GET":
            rng = unquote(urlparse(url).path.split("/values/")[1])
            match = re.fullmatch(r"SheetSynthetic!A(\d+):D(\d+)", rng)
            assert match, rng
            start, end = map(int, match.groups())
            assert end <= self.grid
            vals = deepcopy(self.values[start - 1:end])
            while vals and all(c == "" for c in vals[-1]):
                vals.pop()
            # 飞书 ToString 常把数值按字符串返回。
            vals = [[str(c) if isinstance(c, int) else c for c in r] for r in vals]
            revision = self.revision + int(self.change_revision and start > 1)
            return {"code": 0, "data": {"revision": revision, "valueRange": {"values": vals}}}
        if method == "POST" and url.endswith("/dimension_range"):
            count = body["dimension"]["length"]
            assert 0 < count <= 5000
            self.grid += count
            return {"code": 0, "data": {"addCount": count}}
        assert method == "PUT" and url.endswith("/values"), (method, url)
        values = deepcopy(body["valueRange"]["values"])
        assert len(values) <= 5000
        match = re.fullmatch(r"SheetSynthetic!A(\d+):D(\d+)", body["valueRange"]["range"])
        start, end = map(int, match.groups())
        assert end - start + 1 == len(values)
        assert end <= self.grid
        self.values.extend([["", "", "", ""] for _ in range(end - len(self.values))])
        self.values[start - 1:end] = values
        self.revision += 1
        self.write_count += 1
        if self.write_count == self.fail_after_apply:
            raise feishu.FeishuError("synthetic response lost after server commit")
        return {"code": 0, "data": {"updatedRows": len(values),
                "updatedCells": 0 if self.bad_receipt else len(values) * 4}}


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="voc-stage4-writer-")
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.sheet = Sheet()
        self.patches = [
            patch.object(feishu, "load_config", return_value={"app_id": "synthetic", "app_secret": "synthetic"}),
            patch.object(feishu, "get_tenant_token", return_value="synthetic"),
            patch.object(feishu, "_http_json", side_effect=lambda *a, **kw: self.sheet.http(*a, **kw)),
            patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden in synthetic tests")),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def write(self, rows):
        return writer.write_rows(self.data_dir, rows, url=URL)

    def read(self):
        return writer.read_sheet(self.data_dir, url=URL)

    def active(self):
        return [r for r in self.sheet.values[1:] if any(c != "" for c in r)]

    def test_create_replay_and_update_preserve_other_dates(self):
        self.assertTrue(self.write([row(qty=4), row(day="2026-10-02", qty=7)])["ok"])
        calls = self.sheet.write_count
        self.assertEqual(self.write([row(qty=4), row(day="2026-10-02", qty=7)])["changed_rows"], 0)
        self.assertEqual(self.sheet.write_count, calls)
        self.write([row(qty=9)])
        self.assertEqual(self.active(), [["ShopA", "SKU1", "2026-10-01", 9],
                                         ["ShopA", "SKU1", "2026-10-02", 7]])

    def test_daily_replacement_removes_stale_sku_and_duplicate_rows(self):
        self.sheet.values = [H, ["ShopA", "SKU1", "2026-10-01", 2],
            ["ShopA", "SKU1", "2026-10-01", 2],
            ["ShopA", "OLD", "2026-10-01", 3],
            ["ShopB", "OTHER", "2026-10-01", 8],
            ["ShopA", "SKU1", "2026-09-30", 6]]
        self.write([row(sku="sku1", qty=5), row(sku="NEW", qty=4)])
        self.assertCountEqual(self.active(), [["ShopA", "SKU1", "2026-10-01", 5],
            ["ShopA", "NEW", "2026-10-01", 4], ["ShopB", "OTHER", "2026-10-01", 8],
            ["ShopA", "SKU1", "2026-09-30", 6]])

    def test_response_lost_then_replay_recovers_without_duplicates(self):
        self.sheet.values = [H, ["ShopA", "OLD", "2026-10-01", 2]]
        self.sheet.fail_after_apply = 1
        with self.assertRaises(feishu.FeishuError):
            self.write([row(qty=5)])
        self.assertTrue(writer._pending_path(self.data_dir, URL).exists())
        with self.assertRaisesRegex(feishu.FeishuError, "未确认完成"):
            self.read()
        self.sheet.fail_after_apply = 0
        self.write([row(qty=5)])
        self.assertEqual(self.active(), [["ShopA", "SKU1", "2026-10-01", 5]])
        self.assertFalse(writer._pending_path(self.data_dir, URL).exists())
        self.assertEqual(self.read()[-1], ["ShopA", "SKU1", "2026-10-01", "5"])

    def test_partial_multi_batch_failure_clears_on_replay(self):
        rows = [row(sku="SKU%04d" % i, qty=i) for i in range(1200)]
        self.sheet.fail_after_apply = 2
        with self.assertRaises(feishu.FeishuError):
            self.write(rows)
        self.sheet.fail_after_apply = 0
        self.write(rows)
        self.assertEqual(len(self.active()), 1200)
        self.assertEqual(len({tuple(r[:3]) for r in self.active()}), 1200)

    def test_empty_input_preserves_history_and_empty_table_header(self):
        self.write([])
        self.assertEqual(self.sheet.values, [H])
        self.write([row(qty=2)])
        self.write([])
        self.assertEqual(len(self.active()), 1)

    def test_explicit_empty_daily_partition_clears_only_target_day(self):
        self.sheet.values = [H, ["ShopA", "SKU1", "2026-10-01", 3],
                             ["ShopA", "SKU1", "2026-10-02", 5],
                             ["ShopB", "SKU1", "2026-10-01", 7]]
        result = writer.write_rows(self.data_dir, [], url=URL, replace_days=[(" shopa ", "2026/10/1")])
        self.assertEqual(result["replaced_days"], 1)
        self.assertEqual(self.active(), [["ShopA", "SKU1", "2026-10-02", 5],
                                         ["ShopB", "SKU1", "2026-10-01", 7]])
        with self.assertRaises(feishu.FeishuError):
            writer.write_rows(self.data_dir, [], url=URL, replace_days=[("ShopA", "invalid")])

    def test_invalid_incoming_and_remote_rows_never_write(self):
        invalid = [row(qty=-1), row(qty=1.5), row(qty=True), row(qty="NaN"),
                   row(qty=""), row(sku=""), row(day="2026-02-30"), row(shop="=FORMULA")]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(feishu.FeishuError):
                self.write([raw])
        with self.assertRaisesRegex(feishu.FeishuError, "重复"):
            self.write([row(), row(shop="shopa", sku="sku1")])
        self.sheet.values = [H, ["ShopA", "SKU1", "2026-10-01", "invalid"]]
        with self.assertRaisesRegex(feishu.FeishuError, "第 2 行"):
            self.write([row()])
        self.assertEqual(self.sheet.write_count, 0)

    def test_ambiguous_target_and_noncanonical_header_rejected(self):
        for url in ("https://example.feishu.cn/sheets/shtSynthetic",
                    "https://example.feishu.cn/base/baseSynthetic?table=TableSynthetic",
                    "https://example.feishu.cn/wiki/wikiSynthetic"):
            with self.subTest(url=url), self.assertRaises(feishu.FeishuError):
                writer.write_rows(self.data_dir, [row()], url=url)
        self.sheet.values = [["说明", "SKU", "日期", "销量件数"]]
        with self.assertRaisesRegex(feishu.FeishuError, "第一行"):
            self.write([row()])
        self.assertEqual(self.sheet.write_count, 0)

    def test_full_read_crosses_5000_rows_and_internal_blank(self):
        self.sheet.grid = 6500
        self.sheet.values = [H] + [["", "", "", ""] for _ in range(6000)]
        self.sheet.values[6000] = ["ShopA", "TAIL", "2026-10-31", 9]
        values = self.read()
        self.assertEqual(len(values), 6001)
        self.assertEqual(values[6000][-1], "9")
        self.assertEqual(values[100], ["", "", "", ""])
        self.assertEqual(len([c for c in self.sheet.calls if "/values/" in c[1]]), 7)

    def test_revision_change_and_malformed_receipt_fail_closed(self):
        self.sheet.grid = 1500
        self.sheet.change_revision = True
        with self.assertRaisesRegex(feishu.FeishuError, "被修改"):
            self.read()
        self.sheet.change_revision = False
        self.sheet.bad_receipt = True
        with self.assertRaisesRegex(feishu.FeishuError, "回执不完整"):
            self.write([row()])
        self.assertTrue(writer._pending_path(self.data_dir, URL).exists())

    def test_write_readback_mismatch_leaves_pending(self):
        original = self.sheet.http
        changed = False
        def concurrent_edit(*args, **kwargs):
            nonlocal changed
            if self.sheet.write_count and kwargs.get("method", "GET") == "GET" and not changed:
                self.sheet.values[1][3] = 999
                changed = True
            return original(*args, **kwargs)
        with patch.object(feishu, "_http_json", side_effect=concurrent_edit):
            with self.assertRaisesRegex(feishu.FeishuError, "写后核对"):
                self.write([row()])
        self.assertTrue(writer._pending_path(self.data_dir, URL).exists())

    def test_sheet_lock_prevents_interleaved_read_and_write(self):
        with try_lock(self.data_dir, "feishu-sales-write") as acquired:
            self.assertTrue(acquired)
            with self.assertRaises(feishu.FeishuError):
                self.write([row()])
            with self.assertRaises(feishu.FeishuError):
                self.read()
        self.assertFalse(self.sheet.calls)

    def test_date_serial_and_case_normalization(self):
        self.assertEqual(writer.normalize_day(46326), "2026-10-31")
        self.assertEqual(writer.normalize_day("2026/10/1"), "2026-10-01")
        self.assertEqual(writer.normalize_day("2026-11-01T00:05:00+08:00"), "2026-11-01")
        self.sheet.values = [H, [" ShopA ", "sku1", "2026/10/1", "1"]]
        self.write([row(shop="shopa", sku="SKU1", qty=2)])
        self.assertEqual(len(self.active()), 1)
        self.assertEqual(self.active()[0][-1], 2)


if __name__ == "__main__":
    unittest.main()
