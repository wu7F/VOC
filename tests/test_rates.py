"""Synthetic P1 regressions: incomplete denominators and summary parity.

Set VOC_TEST_SRC to test the original handoff; defaults to voc-stage2/src.
No provider requests, accounts, credentials or production cache are used.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


STAGE2 = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = STAGE2 / "src" if (STAGE2 / "src" / "server").is_dir() else STAGE2
SOURCE = Path(os.environ.get("VOC_TEST_SRC", str(DEFAULT_SOURCE))).resolve()
sys.path.insert(0, str(SOURCE))
_IMPORT_DATA = tempfile.TemporaryDirectory(prefix="voc-rate-import-")
os.environ["VOC_DATA_DIR"] = _IMPORT_DATA.name
from server import aftersale, app as app_module, eccang_sales  # noqa: E402
from server.sku_merge import SkuGrouper  # noqa: E402
from server.store import Store  # noqa: E402


OBSERVATIONS: dict = {"source": str(SOURCE), "synthetic_only": True, "cases": []}


def tearDownModule():
    output = os.environ.get("VOC_RATE_EVIDENCE")
    if output:
        p = Path(output)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(OBSERVATIONS, ensure_ascii=False, indent=2), encoding="utf-8")
    _IMPORT_DATA.cleanup()


class RateRegressions(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="voc-rate-test-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.store = Store(self.root / "runs")
        self.addCleanup(patch.stopall)
        patch.object(app_module, "store", self.store).start()

    def cache(self, *, family_qty: int = 0) -> dict:
        family = SkuGrouper(self.root).family("MR221118BR")
        c = eccang_sales.empty_cache()
        c.update(
            complete=True,
            synced_at=1,
            accounts=["southwest_US"],
            done_accounts=["southwest_US"],
            qty={"southwest_US": {"2026-10": 100}},
            qty_by_family={"southwest_US": {"2026-10": {family: family_qty}}},
            months=["2026-10"],
        )
        return c

    @staticmethod
    def event(index: int, *, date: str = "2026-10-20", shop: str = "SOUTHWEST", flag=None):
        row = {"产品": "MR221118BR", "店铺": shop, "售后原因": "破损",
               "日期": date, "订单号": f"SYNTH-{index}"}
        if flag is not None:
            row["是否售后"] = flag
        return row

    def report(self, rows: list[dict], cache: dict, label="Synthetic report") -> dict:
        rec = self.store.add(name=label, platform="AmazonCs", category="", brands="",
                             comment_limit=20, files=["synthetic.csv"])
        with (patch.object(aftersale.adapters, "read_table", return_value=(rows, "synthetic")),
              patch.object(eccang_sales, "load", return_value=deepcopy(cache))):
            result = aftersale.run_aftersale(files=[self.root / "synthetic.csv"],
                platform="AmazonCs", category="", out_dir=self.store.dataset_dir(rec["id"]),
                data_dir=self.root)
        self.store.update(rec["id"], status="done")
        return result

    def observed(self, case: str, **values):
        OBSERVATIONS["cases"].append({"case": case, **values})

    def reset_reports(self):
        self.store = Store(self.root / f"runs-{len(OBSERVATIONS['cases'])}")
        app_module.store = self.store

    def test_incomplete_sales_cache_is_not_a_denominator(self):
        """P1: partial and cancelled caches must stay registration-only."""
        cases = []
        initial = self.cache()
        initial.update(complete=False, synced_at=0, done_accounts=[])
        cases.append(("initial_partial", initial))
        cancelled = self.cache()
        cancelled.update(complete=False, synced_at=0, done_accounts=[], errors=[{"error": "synthetic cancel"}])
        cases.append(("cancelled_partial", cancelled))
        interrupted_increment = self.cache()
        interrupted_increment.update(complete=False, errors=[{"error": "synthetic incremental failure"}])
        cases.append(("partial_with_old_success_timestamp", interrupted_increment))
        legacy = self.cache()
        legacy.pop("complete")
        cases.append(("legacy_without_explicit_completion", legacy))
        unfinished_accounts = self.cache()
        unfinished_accounts.update(done_accounts=[])
        cases.append(("completion_flag_but_pending_account", unfinished_accounts))
        no_success = self.cache()
        no_success["synced_at"] = 0
        cases.append(("completion_flag_without_success_timestamp", no_success))
        other_scope = self.cache()
        other_scope.update(accounts=["Wekas_US"], done_accounts=["Wekas_US"])
        other_scope["qty"]["Wekas_US"] = {"2026-10": 200}
        cases.append(("completed_scope_does_not_cover_event_account", other_scope))
        cases.append(("complete_control", self.cache()))

        for label, cache in cases:
            with self.subTest(cache=label):
                a = self.report([self.event(i) for i in range(3)], cache)["aftersale"]
                self.observed(label, mode=a["mode"], rate_basis=a["rate_basis"],
                    rate=a["overall_rate"], qty_used=a["sales"]["qty_used"],
                    product_rates=[p["rate"] for p in a["products"]],
                    shop_rates=[s["rate"] for s in a["shops"]])
                self.assertEqual(a["mode"], "ledger")
                if label == "complete_control":
                    self.assertEqual(a["rate_basis"], "eccang_qty")
                    self.assertEqual(a["overall_rate"], .03)
                    self.assertEqual(a["sales"]["qty_used"], 100)
                else:
                    if label == "completed_scope_does_not_cover_event_account":
                        self.assertIn(a["rate_basis"], ("none", "eccang_qty"))
                    else:
                        self.assertEqual(a["rate_basis"], "none")
                    self.assertIsNone(a["overall_rate"])
                    self.assertEqual(a["sales"]["qty_used"], 0)
                    self.assertTrue(all(p["rate"] is None for p in a["products"]))
                    self.assertTrue(all(s["rate"] is None for s in a["shops"]))

    def test_summary_preserves_report_numerator_and_rate_basis(self):
        """P1: summary rates use validated source components, never raw count fallback."""
        for family_qty in (0, 20):
            with self.subTest(case="single_report", product_denominator=family_qty):
                self.reset_reports()
                a = self.report([self.event(1), self.event(2, date="")],
                                self.cache(family_qty=family_qty))["aftersale"]
                merged = app_module.api_aftersale_summary()
                expected_product_rate = .05 if family_qty else None
                self.observed(f"single_report_family_qty_{family_qty}",
                    report_rate=a["overall_rate"], summary_rate=merged["overall"]["rate"],
                    report_product_rate=a["products"][0]["rate"],
                    summary_product_rate=merged["products"][0]["rate"])
                self.assertEqual(a["overall_rate"], .01)
                self.assertEqual(merged["overall"]["rate"], a["overall_rate"])
                self.assertEqual(a["products"][0]["rate"], expected_product_rate)
                self.assertEqual(merged["products"][0]["rate"], expected_product_rate)
                self.assertEqual(a["sales"]["numerator_used"], 1)
                self.assertEqual(a["sales"]["denominators"],
                                 [{"account": "southwest_US", "month": "2026-10", "qty": 100}])

        with self.subTest(case="detail_mode_uses_input_orders"):
            self.reset_reports()
            a = self.report([self.event(1, flag="是"), self.event(2, flag="否"),
                             self.event(3, flag="否")], self.cache(family_qty=20))["aftersale"]
            merged = app_module.api_aftersale_summary()
            self.observed("detail_with_external_cache", report_rate=a["overall_rate"],
                summary_rate=merged["overall"]["rate"], report_basis=a["rate_basis"],
                report_product_rate=a["products"][0]["rate"],
                summary_product_rate=merged["products"][0]["rate"])
            self.assertEqual(a["mode"], "detail")
            self.assertEqual(a["rate_basis"], "detail")
            self.assertEqual(a["overall_rate"], .3333)
            self.assertEqual(a["products"][0]["rate"], .3333)
            self.assertEqual(merged["overall"]["rate"], .3333)
            self.assertEqual(merged["products"][0]["rate"], .3333)

        with self.subTest(case="overlapping_snapshots_are_not_assumed_disjoint"):
            self.reset_reports()
            self.report([self.event(1)], self.cache(family_qty=20))
            self.report([self.event(2)], self.cache(family_qty=20))
            merged = app_module.api_aftersale_summary()
            self.observed("overlapping_account_month", summary_rate=merged["overall"]["rate"],
                product_rate=merged["products"][0]["rate"], rate_warning=merged.get("rate_warning"))
            self.assertIsNone(merged["overall"]["rate"])
            self.assertIsNone(merged["products"][0]["rate"])
            self.assertTrue(merged.get("rate_warning"))
            self.assertEqual(merged["overall"]["aftersale"], 2)

        with self.subTest(case="nonoverlapping_accounts_can_be_combined"):
            self.reset_reports()
            cache = self.cache(family_qty=20)
            family = SkuGrouper(self.root).family("MR221118BR")
            cache["accounts"].append("Wekas_US")
            cache["done_accounts"].append("Wekas_US")
            cache["qty"]["Wekas_US"] = {"2026-10": 200}
            cache["qty_by_family"]["Wekas_US"] = {"2026-10": {family: 50}}
            self.report([self.event(1)], cache)
            self.report([self.event(2, shop="威卡斯")], cache)
            merged = app_module.api_aftersale_summary()
            self.observed("nonoverlapping_accounts", summary_rate=merged["overall"]["rate"],
                product_rate=merged["products"][0]["rate"],
                sales_qty=merged["overall"]["sales_qty"])
            self.assertEqual(merged["overall"]["rate"], round(2 / 300, 4))
            self.assertEqual(merged["products"][0]["rate"], round(2 / 70, 4))

        with self.subTest(case="legacy_external_report_without_components_is_unknown"):
            self.reset_reports()
            result = self.report([self.event(1), self.event(2, date="")], self.cache(family_qty=20))
            a = result["aftersale"]
            a["sales"].pop("numerator_used", None)
            a["sales"].pop("denominators", None)
            for p in a["products"]:
                p.pop("rate_numerator", None)
                p.pop("rate_denominators", None)
            record = self.store.list()[0]
            (self.store.dataset_dir(record["id"]) / "result.json").write_text(
                json.dumps(result, ensure_ascii=False), encoding="utf-8")
            merged = app_module.api_aftersale_summary()
            self.observed("legacy_external_missing_components", summary_rate=merged["overall"]["rate"],
                product_rate=merged["products"][0]["rate"])
            self.assertIsNone(merged["overall"]["rate"])
            self.assertIsNone(merged["products"][0]["rate"])

        with self.subTest(case="mixed_denominator_kinds_cannot_be_combined"):
            self.reset_reports()
            cache = self.cache(family_qty=20)
            self.report([self.event(1)], cache)
            self.report([self.event(2, flag="是"), self.event(3, flag="否"),
                         self.event(4, flag="否")], cache)
            merged = app_module.api_aftersale_summary()
            self.observed("mixed_detail_and_external", summary_rate=merged["overall"]["rate"],
                product_rate=merged["products"][0]["rate"])
            self.assertIsNone(merged["overall"]["rate"])
            self.assertIsNone(merged["products"][0]["rate"])

        with self.subTest(case="legacy_detail_mode_overrides_mislabelled_external_basis"):
            self.reset_reports()
            result = self.report([self.event(1, flag="是"), self.event(2, flag="否"),
                                  self.event(3, flag="否")], self.cache(family_qty=20))
            # The original handoff labelled detail reports eccang_qty whenever
            # any external cache existed, despite using input orders overall.
            result["aftersale"]["rate_basis"] = "eccang_qty"
            record = self.store.list()[0]
            (self.store.dataset_dir(record["id"]) / "result.json").write_text(
                json.dumps(result, ensure_ascii=False), encoding="utf-8")
            merged = app_module.api_aftersale_summary()
            self.observed("legacy_detail_mislabelled_basis", summary_rate=merged["overall"]["rate"],
                product_rate=merged["products"][0]["rate"])
            self.assertEqual(merged["overall"]["rate"], .3333)
            self.assertEqual(merged["products"][0]["rate"], .3333)


if __name__ == "__main__":
    unittest.main(verbosity=2)
