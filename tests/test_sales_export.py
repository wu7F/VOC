"""Synthetic-only Eccang → daily SKU totals → mocked Feishu writeback checks."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import eccang_sales as es, feishu_sales, feishu_sales_writer
from server.sales_lock import try_lock


class SalesExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="voc-stage4-export-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        es._CANCEL.clear()
        es._SYNC["running"] = False
        self.settings = {"url": "https://synthetic.feishu.cn/sheets/SYNTHETIC?sheet=sheet1",
                         "write_back": True}
        self.requests = []
        self.exports = []
        self.replace_days = []
        self.map = {"合成店铺": ["SYNTH_ACCOUNT"]}

    @staticmethod
    def order(code="SYNTH-1", sku="SYNTH-SKU-A", qty=2, day="2026-10-20"):
        return {"order_code": code, "date_create": day + " 10:20:00", "product_count": qty,
                "order_details": [{"product_sku_org": sku, "qty": qty}]}

    def run_sync(self, orders, *, full=False, enabled=True, failure=None, cancel=False,
                 writer_error=None, receipt=None, payload=None):
        self.requests = []
        self.exports = []
        self.replace_days = []
        def call(method, biz, **kwargs):
            self.requests.append(deepcopy(biz))
            if failure:
                raise es.eccang.EccangError("synthetic source failure")
            if cancel:
                es._CANCEL.set()
            if payload is not None:
                return deepcopy(payload)
            return {"total": len(orders), "data": deepcopy(orders)}
        def write(data_dir, rows, *, url, replace_days=None):
            self.exports.append(deepcopy(rows))
            self.replace_days.append(deepcopy(replace_days))
            self.assertEqual(Path(data_dir), self.root)
            self.assertEqual(url, self.settings["url"])
            if writer_error:
                raise RuntimeError("synthetic write failed after uncertain response")
            return {"ok": True} if receipt is None else receipt
        cfg = {"app_key": "SYNTHETIC", "app_secret": "SYNTHETIC", "service_id": "SYNTHETIC"}
        with (patch.object(feishu_sales, "load_settings", return_value=dict(self.settings, write_back=enabled)),
              patch.object(es.eccang, "load_config", return_value=cfg),
              patch.object(es.eccang, "call", side_effect=call),
              patch.object(es, "load_account_map", return_value=self.map),
              patch.object(feishu_sales_writer, "write_rows", side_effect=write)):
            result = es.sync(self.root, full=full)
        return es.load(self.root), result

    def test_aggregates_raw_sku_by_day_and_natural_month(self):
        orders = [self.order(qty=2), self.order("SYNTH-2", qty=3),
                  self.order("SYNTH-3", "SYNTH-SKU-B", 4, "2026-11-01")]
        cache, result = self.run_sync(orders, full=True)
        self.assertTrue(es.is_complete(cache))
        self.assertEqual(cache["qty"]["SYNTH_ACCOUNT"], {"2026-10": 5, "2026-11": 4})
        self.assertEqual(self.exports, [[
            {"店铺": "合成店铺", "SKU": "SYNTH-SKU-A", "日期": "2026-10-20", "销量件数": 5},
            {"店铺": "合成店铺", "SKU": "SYNTH-SKU-B", "日期": "2026-11-01", "销量件数": 4}]])
        self.assertTrue(all(request["get_detail"] == 1 for request in self.requests))
        self.assertEqual(result["writeback"]["status"], "ok")
        self.assertFalse(result["sync"]["error"])

    def test_increment_accumulates_day_without_duplicates_and_full_replaces_quantity(self):
        self.run_sync([self.order()], full=True)
        self.run_sync([self.order(), self.order("SYNTH-2", qty=3)])
        self.assertEqual(len(self.exports[0]), 1)
        self.assertEqual(self.exports[0][0]["销量件数"], 5)
        self.run_sync([self.order(), self.order("SYNTH-2", qty=3)])
        self.assertEqual(self.exports[0][0]["销量件数"], 5)
        cache, _ = self.run_sync([self.order(qty=1)], full=True)
        self.assertEqual(self.exports[0][0]["销量件数"], 1)
        self.assertEqual(cache["qty"]["SYNTH_ACCOUNT"]["2026-10"], 1)

    def test_sku_case_is_normalized_before_aggregation(self):
        self.run_sync([self.order(sku="synth-sku-a"), self.order("SYNTH-2", qty=3)], full=True)
        self.assertEqual(self.exports[0], [
            {"店铺": "合成店铺", "SKU": "SYNTH-SKU-A", "日期": "2026-10-20", "销量件数": 5}])

    def test_old_family_cache_requires_daily_history_rebuild(self):
        old = es.empty_cache()
        old.update(complete=True, synced_at=123, accounts=["SYNTH_ACCOUNT"],
                   done_accounts=["SYNTH_ACCOUNT"], with_detail=True,
                   order_index={"SYNTH-1": {"a": "SYNTH_ACCOUNT", "m": "2026-10"}},
                   qty={"SYNTH_ACCOUNT": {"2026-10": 999}}, sys_cursor="2026-10-19 00:00:00")
        es._save(self.root, old)
        cache, _ = self.run_sync([self.order()])
        self.assertEqual(cache["qty"]["SYNTH_ACCOUNT"]["2026-10"], 2)
        self.assertEqual(self.requests[0]["condition"]["date_create_sys_start"], "2026-01-01 00:00:00")
        self.assertEqual(cache["daily_version"], 1)

    def test_deleted_last_sku_day_is_cleared_and_scope_change_does_not_clear_old_days(self):
        self.run_sync([self.order()], full=True)
        cache, result = self.run_sync([], full=True, writer_error=True)
        self.assertEqual(self.exports, [[]])
        self.assertEqual(self.replace_days, [[("合成店铺", "2026-10-20")]])
        self.assertEqual(cache["writeback"]["status"], "error")
        self.run_sync([], full=True)
        self.assertEqual(self.replace_days, [[("合成店铺", "2026-10-20")]])
        self.map = {"另一个合成店铺": ["SYNTH_ACCOUNT"]}
        self.run_sync([], full=True)
        self.assertEqual(self.replace_days, [[]])

    def test_reenable_after_untracked_sync_rebuilds_daily_history(self):
        self.run_sync([self.order()], full=True)
        cache, _ = self.run_sync([self.order("SYNTH-2", qty=3)], enabled=False)
        self.assertFalse(self.exports)
        self.assertNotIn("daily_version", cache)
        cache, _ = self.run_sync([self.order(), self.order("SYNTH-2", qty=3)])
        self.assertEqual(self.exports[0][0]["销量件数"], 5)
        self.assertEqual(self.requests[0]["condition"]["date_create_sys_start"], "2026-01-01 00:00:00")

    def test_missing_or_invalid_export_data_never_reaches_writer(self):
        cases = []
        for field, value in (("order_details", []), ("date_create", "2026-02-30 12:00:00"),
                             ("order_code", ""), ("product_count", 999)):
            cases.append(dict(self.order(), **{field: value}))
        cases.append(dict(self.order(), order_details=[{"qty": 2}]))
        for qty in (None, -1, "1.5", "NaN", True):
            cases.append(dict(self.order(), order_details=[{"product_sku_org": "SYNTH-SKU-A", "qty": qty}]))
        for i, order in enumerate(cases):
            with self.subTest(case=i):
                cache, result = self.run_sync([order], full=True)
                self.assertFalse(self.exports)
                self.assertFalse(es.is_complete(cache))
                self.assertTrue(result["sync"]["error"])
                self.assertEqual(result["writeback"]["status"], "error")

    def test_source_failure_and_late_cancel_never_write(self):
        for options in ({"failure": True}, {"cancel": True}):
            with self.subTest(options=options):
                cache, result = self.run_sync([self.order()], full=True, **options)
                self.assertFalse(self.exports)
                self.assertFalse(es.is_complete(cache))
                self.assertEqual(result["writeback"]["status"], "blocked")

    def test_malformed_or_truncated_page_never_writes(self):
        for payload in ({}, {"total": 1, "data": {}}, {"total": 10, "data": [self.order()]},
                        {"total": "invalid", "data": []}):
            with self.subTest(payload=payload):
                cache, result = self.run_sync([], full=True, payload=payload)
                self.assertFalse(self.exports)
                self.assertFalse(es.is_complete(cache))
                self.assertTrue(result["sync"]["error"])

    def test_write_failure_remains_visible_and_retries_even_with_no_new_orders(self):
        cache, result = self.run_sync([self.order()], full=True, writer_error=True)
        self.assertTrue(es.is_complete(cache))
        self.assertEqual(result["writeback"]["status"], "error")
        self.assertTrue(result["sync"]["error"])
        failed_rows = deepcopy(self.exports)
        cache, result = self.run_sync([self.order()])
        self.assertEqual(self.exports, failed_rows)
        self.assertEqual(result["writeback"]["status"], "ok")
        self.assertFalse(result["sync"]["error"])
        self.assertEqual(cache["orders"], 1)

    def test_missing_success_receipt_is_write_failure(self):
        _, result = self.run_sync([self.order()], full=True, receipt={})
        self.assertEqual(result["writeback"]["status"], "error")
        self.assertTrue(result["sync"]["error"])

    def test_zero_quantity_and_empty_complete_snapshot_are_safe(self):
        cache, _ = self.run_sync([self.order(qty=0)], full=True)
        self.assertEqual(self.exports[0][0]["销量件数"], 0)
        self.assertEqual(cache["qty"]["SYNTH_ACCOUNT"]["2026-10"], 0)
        _, result = self.run_sync([], full=True)
        self.assertEqual(self.exports, [[]])
        self.assertEqual(result["writeback"]["status"], "ok")

    def test_multiple_accounts_for_one_shop_merge_into_one_daily_key(self):
        self.map = {"合成店铺": ["SYNTH_ACCOUNT", "SYNTH_ACCOUNT_B"]}
        # Source order keys include account; order_code is globally unique in the existing source contract.
        def make_call(method, biz, **kwargs):
            account = biz["condition"]["user_account_list"][0]
            return {"total": 1, "data": [self.order(account)]}
        cfg = {"app_key": "SYNTHETIC", "app_secret": "SYNTHETIC", "service_id": "SYNTHETIC"}
        with (patch.object(feishu_sales, "load_settings", return_value=self.settings),
              patch.object(es.eccang, "load_config", return_value=cfg),
              patch.object(es.eccang, "call", side_effect=make_call),
              patch.object(es, "load_account_map", return_value=self.map),
              patch.object(feishu_sales_writer, "write_rows", return_value={"ok": True}) as writer):
            es.sync(self.root, full=True)
        self.assertEqual(writer.call_args.args[1], [
            {"店铺": "合成店铺", "SKU": "SYNTH-SKU-A", "日期": "2026-10-20", "销量件数": 4}])

    def test_process_lock_blocks_a_second_sync_before_external_calls(self):
        with try_lock(self.root, "eccang-sales") as acquired:
            self.assertTrue(acquired)
            _, result = self.run_sync([self.order()])
        self.assertFalse(self.requests)
        self.assertFalse(self.exports)
        self.assertTrue(result["sync"]["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
