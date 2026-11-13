"""Offline P2 regressions for detail-mode inheritance and small order tables."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = BASE if (BASE / "server").is_dir() else BASE / "p2"
SOURCE = Path(os.environ.get("VOC_TEST_SRC", str(DEFAULT_SOURCE))).resolve()
sys.path.insert(0, str(SOURCE))
from server import aftersale, eccang_sales as es  # noqa: E402
from server.sku_merge import SkuGrouper  # noqa: E402

OBSERVATIONS = {"source": str(SOURCE), "synthetic_only": True, "cases": []}


def tearDownModule():
    output = os.environ.get("VOC_P2_BACKEND_EVIDENCE")
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(OBSERVATIONS, ensure_ascii=False, indent=2), encoding="utf-8")


class P2BackendRegressions(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="voc-p2-backend-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.account, self.month = "ACCOUNT_A", "2026-10"
        self.family = SkuGrouper(self.root).family("MR221118BR")
        self.requests = []
        self.writes = []
        es._CANCEL.clear()
        es._SYNC["running"] = False

    def cache(self, detail=True):
        c = es.empty_cache()
        c.update(complete=True, synced_at=123, sys_cursor="2026-10-19 00:00:00",
                 with_detail=detail, accounts=[self.account], done_accounts=[self.account],
                 qty={self.account: {self.month: 10}},
                 qty_by_family={self.account: {self.month: {self.family: 10} if detail else {}}},
                 order_index={"SYNTH-OLD": {"a": self.account, "m": self.month}})
        return c

    @staticmethod
    def order(code, qty):
        return {"order_code": code, "date_create": "2026-10-20 00:00:00", "product_count": qty,
                "order_details": [{"product_sku_org": "MR221118BR", "qty": qty}]}

    def sync(self, rows, *, full=False, detail=False, fail=False):
        save = es._save
        def capture(_, value):
            self.writes.append(deepcopy(value))
            save(self.root, value)
        def call(method, biz, **kwargs):
            self.requests.append(deepcopy(biz))
            if fail:
                raise es.eccang.EccangError("synthetic page failure")
            return {"total": len(rows), "data": deepcopy(rows)}
        cfg = {"app_key": "SYNTHETIC", "app_secret": "SYNTHETIC", "service_id": "SYNTHETIC"}
        with (patch.object(es.eccang, "load_config", return_value=cfg),
              patch.object(es.eccang, "call", side_effect=call),
              patch.object(es, "accounts_for_shops", return_value=[self.account]),
              patch.object(es, "_save", side_effect=capture)):
            es.sync(self.root, full=full, with_detail=detail)
        return es.load(self.root)

    def observe_sync(self, name, cache):
        value = {"case": name, "request_get_detail": [r["get_detail"] for r in self.requests],
                 "cache_with_detail": cache.get("with_detail"),
                 "shop_qty": es.qty_for(cache, self.account, self.month),
                 "family_qty": es.qty_for(cache, self.account, self.month, self.family),
                 "complete": cache.get("complete"),
                 "all_writes_keep_detail": all(c.get("with_detail") for c in self.writes)}
        OBSERVATIONS["cases"].append(value)

    def test_product_denominators_inherit_detail_mode(self):
        """An enabled product denominator must advance with every later sync."""
        for name in ("default_increment", "repeated_increment", "full_rebuild", "legacy_rebuild",
                     "enable_detail_rebuild", "failed_first_detail_resume", "default_shop_only"):
            with self.subTest(case=name), tempfile.TemporaryDirectory(prefix="voc-p2-sync-") as tmp:
                self.root = Path(tmp)
                self.requests, self.writes = [], []
                cache = self.cache(detail=name not in ("enable_detail_rebuild", "default_shop_only"))
                if name == "legacy_rebuild":
                    cache.pop("complete")
                es._save(self.root, cache)
                if name == "failed_first_detail_resume":
                    es._save(self.root, es.empty_cache())
                    self.sync([], detail=True, fail=True)
                    c = self.sync([self.order("SYNTH-NEW", 5)])
                    expected = 5
                elif name in ("full_rebuild", "legacy_rebuild", "enable_detail_rebuild"):
                    c = self.sync([self.order("SYNTH-OLD", 10), self.order("SYNTH-NEW", 5)],
                                  full=name == "full_rebuild", detail=name == "enable_detail_rebuild")
                    expected = 15
                else:
                    c = self.sync([self.order("SYNTH-NEW", 5)])
                    expected = 15
                    if name == "repeated_increment":
                        c = self.sync([self.order("SYNTH-NEXT", 2)])
                        expected = 17
                self.observe_sync(name, c)
                self.assertTrue(es.is_complete(c))
                self.assertEqual(es.qty_for(c, self.account, self.month), expected)
                if name == "default_shop_only":
                    self.assertFalse(c["with_detail"])
                    self.assertEqual(es.qty_for(c, self.account, self.month, self.family), 0)
                    self.assertEqual([r["get_detail"] for r in self.requests], [0])
                else:
                    self.assertTrue(c["with_detail"])
                    self.assertEqual(es.qty_for(c, self.account, self.month, self.family), expected)
                    self.assertTrue(all(r["get_detail"] == 1 for r in self.requests))
                    self.assertTrue(all(w.get("with_detail") is True for w in self.writes))
                    if name in ("full_rebuild", "legacy_rebuild", "enable_detail_rebuild"):
                        self.assertTrue(all(r["condition"]["date_create_sys_start"] == "2026-01-01 00:00:00"
                                            for r in self.requests))

    def test_small_explicit_flags_are_order_detail(self):
        """Explicit flags work on one/two rows without trusting ambiguous shop columns."""
        cases = [
            ("two_boolean_flags", "是否售后", ["是", "否"], "detail", 1, .5),
            ("one_yes", "是否售后", ["是"], "detail", 1, 1.0),
            ("one_no", "是否售后", ["否"], "detail", 0, 0.0),
            ("same_yes", "是否售后", ["是", "是"], "detail", 2, 1.0),
            ("same_no", "是否售后", ["否", "否"], "detail", 0, 0.0),
            ("english_flags", "is_return", ["true", "false"], "detail", 1, .5),
            ("explicit_unknown", "是否售后", ["是", "待确认"], "ledger", 2, None),
            ("ambiguous_boolean_type", "类型", ["是", "否"], "ledger", 2, None),
            ("shop_type", "类型", ["行善", "香港店"], "ledger", 2, None),
        ]
        for name, column, values, mode, count, rate in cases:
            with self.subTest(case=name):
                rows = [{"产品": "MR221118BR", column: value, "售后原因": "破损"} for value in values]
                with (patch.object(aftersale.adapters, "read_table", return_value=(rows, "synthetic")),
                      patch.object(es, "load", return_value=es.empty_cache())):
                    a = aftersale.run_aftersale(files=[self.root/"synthetic.csv"], platform="AmazonCs", category="",
                        out_dir=self.root/name, data_dir=self.root)["aftersale"]
                OBSERVATIONS["cases"].append({"case": name, "mode": a["mode"],
                    "total_orders": a["total_orders"], "aftersale_orders": a["aftersale_orders"],
                    "overall_rate": a["overall_rate"]})
                self.assertEqual(a["mode"], mode)
                self.assertEqual(a["aftersale_orders"], count)
                self.assertEqual(a["overall_rate"], rate)


if __name__ == "__main__":
    unittest.main(verbosity=2)
