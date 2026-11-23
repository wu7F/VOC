import json
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import bitable_board as bb


SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "server" / "aftersale_snapshot.json"


def load_snapshot():
    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class AftersaleSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.snap = load_snapshot()
        self.summary = bb._build_aftersale_summary(self.snap)

    def test_connected_and_not_realtime(self):
        self.assertTrue(self.summary["connected"])
        self.assertFalse(self.summary["realtime"])
        self.assertEqual(self.summary["source"], "local_confirmed_snapshot")
        self.assertFalse(self.summary["reasons"]["connected"])
        self.assertIsNone(self.summary["reasons"]["items"])

    def test_coverage_counts(self):
        cov = self.summary["coverage"]
        self.assertEqual(cov["team_count"], 3)
        self.assertEqual(cov["store_count"], 8)
        self.assertEqual(cov["store_month_count"], 50)
        self.assertEqual(cov["sku_store_month_count"], 497)

    def test_team_rates_exact(self):
        teams = {t["name"]: t for t in self.summary["teams"]}
        self.assertEqual(teams["Team 1"]["numerator"], 410)
        self.assertEqual(teams["Team 1"]["denominator"], 4127)
        self.assertAlmostEqual(teams["Team 1"]["rate"], 9.9346, places=4)
        self.assertEqual(teams["Team 2"]["numerator"], 291)
        self.assertEqual(teams["Team 2"]["denominator"], 2194)
        self.assertAlmostEqual(teams["Team 2"]["rate"], 13.2634, places=4)
        self.assertEqual(teams["Team 3"]["numerator"], 64)
        self.assertEqual(teams["Team 3"]["denominator"], 867)
        self.assertAlmostEqual(teams["Team 3"]["rate"], 7.3818, places=4)

    def test_total_rate_exact(self):
        total = self.summary["total"]
        self.assertEqual(total["numerator"], 765)
        self.assertEqual(total["denominator"], 7188)
        self.assertAlmostEqual(total["rate"], 10.6427, places=4)

    def test_store_rates_exact(self):
        stores = {s["account"]: s for s in self.summary["stores"]}
        expected = {
            "KornerHK_US": (317, 3115),
            "southwest_US": (4, 104),
            "xingshan_US": (89, 908),
            "Wekas_US": (226, 1552),
            "kaifengzhejie_US": (65, 642),
            "AmazonMJSKY_US_US": (1, 5),
            "RUIBAO_US_US": (56, 762),
            "maoyitong_US": (7, 100),
        }
        for account, (n, d) in expected.items():
            self.assertIn(account, stores)
            self.assertEqual(stores[account]["numerator"], n)
            self.assertEqual(stores[account]["denominator"], d)

    def test_named_month_sku_row(self):
        # 从快照中取一条真实 SKU×店铺×月记录进行核对
        raw = self.snap["sku_store_month"]
        self.assertTrue(raw)
        sample = raw[0]
        found = None
        for r in self.summary["sku_store_month"]:
            if (r["account"] == sample["store"] and r["month"] == sample["month"]
                    and r["sku"] == sample["sku"]):
                found = r
                break
        self.assertIsNotNone(found)
        self.assertEqual(found["numerator"], sample["numerator"])
        self.assertEqual(found["denominator"], sample["denominator"])

    def test_denominator_zero_vs_missing(self):
        self.assertIsNone(bb._rate(0, 0))
        self.assertIsNone(bb._rate(5, 0))
        self.assertIsNone(bb._rate(None, 10))
        self.assertIsNone(bb._rate(5, None))
        self.assertEqual(bb._rate(0, 10), 0.0)
        self.assertEqual(bb._rate(5, 10), 50.0)

    def test_explicit_zero_numerator(self):
        entry = bb._rate_entry(0, 100)
        self.assertEqual(entry["numerator"], 0)
        self.assertEqual(entry["denominator"], 100)
        self.assertEqual(entry["rate"], 0.0)

    def test_missing_snapshot_placeholder(self):
        summary = bb._build_aftersale_summary(None)
        self.assertFalse(summary["connected"])
        self.assertFalse(summary["reasons"]["connected"])
        self.assertIsNone(summary["reasons"]["items"])

    def test_t01_exclusion_metadata(self):
        t01 = self.summary["t01_excluded"]
        self.assertEqual(t01["team1"], 151)
        self.assertEqual(t01["team2"], 0)
        self.assertEqual(t01["team3"], 1)
        self.assertEqual(t01["total"], 152)

    def test_t01_both_sides_excluded_synthetic(self):
        numerator_rows = [
            {"count": 2, "is_t01": False},
            {"count": 1, "is_t01": True},
        ]
        denominator_rows = [
            {"sales": 20, "is_t01": False},
            {"sales": 3, "is_t01": True},
        ]
        num_out, den_out = bb.exclude_t01_rows(numerator_rows, denominator_rows)
        self.assertEqual(sum(r["count"] for r in num_out), 2)
        self.assertEqual(sum(r["sales"] for r in den_out), 20)
        self.assertEqual(bb._rate(2, 20), 10.0)

    def assert_fail_closed(self, snapshot, label):
        summary = bb._build_aftersale_summary(snapshot)
        self.assertFalse(summary["connected"], label)
        self.assertIsNone(summary.get("total"), label)
        self.assertEqual(summary.get("teams"), [], label)
        self.assertEqual(summary.get("stores"), [], label)
        self.assertTrue(summary.get("diagnostics"), label)
        self.assertNotIn("/synthetic", repr(summary), label)

    def test_missing_required_team_fails_closed(self):
        snap = load_snapshot()
        snap["team_totals"] = [t for t in snap["team_totals"] if t["team"] != "Team 3"]
        self.assert_fail_closed(snap, "missing Team 3 must not produce a partial total")

    def test_missing_required_store_fails_closed(self):
        snap = load_snapshot()
        snap["stores"] = [s for s in snap["stores"] if s["store"] != "maoyitong_US"]
        self.assert_fail_closed(snap, "missing required store must not produce a partial total")

    def test_duplicate_team_fails_closed(self):
        snap = load_snapshot()
        snap["team_totals"].append(dict(snap["team_totals"][0]))
        self.assert_fail_closed(snap, "duplicate Team must not be double counted")

    def test_duplicate_store_fails_closed(self):
        snap = load_snapshot()
        snap["stores"].append(dict(snap["stores"][0]))
        self.assert_fail_closed(snap, "duplicate store must not be double counted")

    def test_duplicate_store_month_fails_closed(self):
        snap = load_snapshot()
        snap["store_month"].append(dict(snap["store_month"][0]))
        self.assert_fail_closed(snap, "duplicate store-month key must be rejected")

    def test_duplicate_sku_store_month_fails_closed(self):
        snap = load_snapshot()
        snap["sku_store_month"].append(dict(snap["sku_store_month"][0]))
        self.assert_fail_closed(snap, "duplicate SKU-store-month key must be rejected")

    def test_unexpected_store_fails_closed(self):
        snap = load_snapshot()
        snap["stores"][0]["store"] = "unregistered_US"
        self.assert_fail_closed(snap, "unexpected store must be rejected")

    def test_store_month_aggregate_mismatch_fails_closed(self):
        snap = load_snapshot()
        snap["store_month"][0]["denominator"] += 1
        self.assert_fail_closed(snap, "store-month/store sums must reconcile")

    def test_sku_aggregate_mismatch_fails_closed(self):
        snap = load_snapshot()
        snap["sku_store_month"][0]["denominator"] += 1
        self.assert_fail_closed(snap, "SKU/store-month sums must reconcile")

    def test_negative_counts_fail_closed(self):
        snap = load_snapshot()
        snap["team_totals"][0]["numerator"] = -410
        self.assert_fail_closed(snap, "negative numerator must not be rendered as a valid rate")

    def test_malformed_month_fails_closed(self):
        snap = load_snapshot()
        snap["store_month"][0]["month"] = "2026-99"
        self.assert_fail_closed(snap, "invalid calendar month must be rejected")

    def test_missing_coverage_month_fails_closed(self):
        snap = load_snapshot()
        snap["store_month"] = [r for r in snap["store_month"] if r["month"] != "2026-11"]
        self.assert_fail_closed(snap, "coverage gap must not be presented as complete")

    def test_inconsistent_aggregate_sums_fail_closed(self):
        snap = load_snapshot()
        snap["team_totals"][0]["numerator"] += 1
        self.assert_fail_closed(snap, "team/store aggregate mismatch must be rejected")

    def test_invalid_json_file_is_fail_closed_with_diagnostic(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json") as f:
            f.write("{ invalid json")
            f.flush()
            loaded = bb._load_aftersale_snapshot(f.name)
        summary = bb._build_aftersale_summary(loaded)
        self.assertFalse(summary["connected"])
        self.assertIsNone(summary.get("total"))
        self.assertTrue(summary.get("diagnostics"))

    def test_snapshot_read_error_is_fail_closed_with_diagnostic(self):
        with mock.patch("builtins.open", side_effect=OSError("synthetic read failure")):
            loaded = bb._load_aftersale_snapshot("/synthetic/unreadable.json")
        summary = bb._build_aftersale_summary(loaded)
        self.assertFalse(summary["connected"])
        self.assertIsNone(summary.get("total"))
        self.assertTrue(summary.get("diagnostics"))

    def test_store_month_row_count_mismatch_fails_closed(self):
        snap = json.loads(json.dumps(load_snapshot()))
        snap["store_month"].pop(0)
        self.assert_fail_closed(snap, "50 store-month rows are required")

    def test_sku_store_month_row_count_mismatch_fails_closed(self):
        snap = json.loads(json.dumps(load_snapshot()))
        snap["sku_store_month"].pop(0)
        self.assert_fail_closed(snap, "497 SKU-store-month rows are required")

    def test_sales_present_aftersale_missing_is_null(self):
        # 有销量但无售后记录：售后率应为 None，不是 0
        snap = {
            "source": {},
            "team_totals": [],
            "stores": [],
            "store_month": [],
            "sku_store_month": [],
        }
        summary = bb._build_aftersale_summary(snap)
        self.assertEqual(summary["sku_store_month"], [])
        # 缺失键 -> None
        self.assertIsNone(bb._rate(None, 100))


if __name__ == "__main__":
    unittest.main()
