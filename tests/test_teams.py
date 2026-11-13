"""Synthetic account/alias coverage and exact Team numerator regressions."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import aftersale, eccang_sales, feishu_sales, shop_filter
from server.aftersale_teams import TEAM_MEMBERS, combine_team_reports

URL = "https://example.feishu.cn/sheets/synthetic?sheet=synthetic"
ACCOUNTS = [a for _, members in TEAM_MEMBERS for a in members]


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = feishu_sales.save_settings(self.root, url=URL)
        self.values = [feishu_sales.HEADERS] + [[a, "MR12345", "2026-10-01", 100] for a in ACCOUNTS]
        self.write_table()

    def write_table(self):
        cache = feishu_sales.build_cache(self.values, self.settings, self.root)
        feishu_sales._save(self.root, feishu_sales._CACHE, cache)
        return cache

    @staticmethod
    def event(shop="xingshan_US", date="2026-10-20", **more):
        return {"店铺": shop, "SKU": "MR12345", "日期": date, "售后原因": "破损", **more}

    def analyze(self, rows=None, platform="AmazonCs"):
        rows = [self.event()] if rows is None else rows
        with patch.object(aftersale.adapters, "read_table", return_value=(rows, "synthetic")):
            return aftersale.run_aftersale(files=[self.root / "synthetic.csv"],
                platform=platform, category="", out_dir=self.root / "report", data_dir=self.root)["aftersale"]

    def test_three_exact_teams_and_weighted_rate(self):
        self.values[1][3] = 1
        self.values[2][3] = 999
        self.values[3][3] = 1000
        self.write_table()
        a = self.analyze([self.event()] * 1 + [self.event("KornerHK_US")] * 9)
        self.assertEqual([(g["team"], g["members"]) for g in a["teams"]],
                         [(name, list(members)) for name, members in TEAM_MEMBERS])
        t = a["teams"][0]
        self.assertEqual(t["rate_numerator"], 10)
        self.assertEqual(t["sales_qty"], 2000)
        self.assertEqual(t["rate"], .005)
        self.assertEqual(t["aftersale"], 10)
        self.assertNotEqual(t["rate"], round((1 + 9 / 999) / 3, 4))
        self.assertEqual(a["teams"][1]["rate"], 0)

    def test_feishu_chinese_ledger_and_account_table_match(self):
        before = feishu_sales.load(self.root)
        a = self.analyze([self.event("行善"), self.event("香港"), self.event("香港店"), self.event("初颂")])
        self.assertEqual(a["teams"][0]["rate_numerator"], 3)
        self.assertEqual(a["teams"][0]["sales_qty"], 300)
        self.assertEqual(a["teams"][0]["rate"], .01)
        self.assertEqual(a["teams"][2]["rate_numerator"], 1)
        self.assertEqual(a["sales"]["numerator_used"], 4)
        self.assertEqual(a["sales"]["numerator_used"], sum(t["rate_numerator"] for t in a["teams"]))
        self.assertEqual(len(a["shops"]), 8)
        hong_kong = next(s for s in a["shops"] if s["shop"] == "KornerHK_US")
        self.assertEqual(hong_kong["aftersale"], 2)
        self.assertEqual(hong_kong["sales_qty"], 100)
        self.assertEqual(hong_kong["rate"], .02)
        self.assertEqual(a["overall_rate"], .005)
        self.assertEqual(feishu_sales.load(self.root), before)

    def test_feishu_account_ledger_and_chinese_table_match(self):
        names = {accounts[0]: name for name, accounts in eccang_sales.DEFAULT_ACCOUNT_MAP.items()}
        for row in self.values[1:]:
            row[0] = names[row[0]]
        self.write_table()
        a = self.analyze([self.event(), self.event("RUIBAO_US_US")])
        self.assertEqual(a["teams"][0]["rate"], round(1 / 300, 4))
        self.assertEqual(a["teams"][2]["rate"], round(1 / 300, 4))
        self.assertEqual(len(a["shops"]), 8)
        self.assertEqual(a["sales"]["numerator_used"], 2)
        self.assertEqual(a["sales"]["numerator_used"], sum(t["rate_numerator"] for t in a["teams"]))
        self.assertEqual(a["overall_rate"], round(2 / 800, 4))

    def test_feishu_whitelist_uses_confirmed_identity_for_rows_and_zero_event_sources(self):
        labels = ["行善", "香港店", "SOUTHWEST", "威卡斯", "浙捷", "茂易通", "麦吉天", "初颂"]
        for table_names, whitelist in ((ACCOUNTS, labels), (labels, ACCOUNTS)):
            with self.subTest(table_names=table_names):
                self.values = [feishu_sales.HEADERS] + [
                    [name, "MR12345", "2026-10-01", 100] for name in table_names]
                self.write_table()
                shop_filter.save(self.root, whitelist)
                a = self.analyze([self.event("行善"), self.event("KornerHK_US"),
                                  self.event("香港"), self.event("初颂")])
                self.assertEqual(a["total_orders"], 4)
                self.assertEqual(a["sales"]["numerator_used"], 4)
                self.assertEqual(a["sales"]["qty_used"], 800)
                self.assertEqual(len(a["shops"]), 8)
                self.assertEqual(a["shop_whitelist"]["missing"], [])
                self.assertEqual(a["shop_whitelist"]["excluded_records"], 0)
                self.assertTrue(all(t["rate"] is not None for t in a["teams"]))
                self.assertEqual(a["teams"][1]["rate"], 0)

    def test_feishu_identity_join_is_not_applied_to_other_platform_or_detail(self):
        rows = [self.event("行善"), self.event("香港")]
        self.settings = feishu_sales.save_settings(self.root, url=URL, platform="WayfairCs")
        self.write_table()
        a = self.analyze(rows, "WayfairCs")
        self.assertEqual(a["sales"]["numerator_used"], 0)
        self.assertIn("行善", {shop["shop"] for shop in a["shops"]})
        self.settings = feishu_sales.save_settings(self.root, url=URL)
        self.write_table()
        a = self.analyze([self.event("行善", **{"是否售后": "是"}),
                          self.event("行善", **{"是否售后": "否"})])
        self.assertEqual(a["mode"], "detail")
        self.assertEqual([shop["shop"] for shop in a["shops"]], ["行善"])
        self.assertEqual(a["overall_rate"], .5)

    def test_missing_member_keeps_registration_and_blocks_group_rate(self):
        self.values = [row for row in self.values if row[0] != "RUIBAO_US_US"]
        self.write_table()
        a = self.analyze([self.event("RUIBAO_US_US")])
        t = a["teams"][2]
        self.assertEqual(t["missing_shops"], ["RUIBAO_US_US"])
        self.assertEqual(t["aftersale"], 1)
        self.assertIsNone(t["sales_qty"])
        self.assertIsNone(t["rate"])
        self.assertEqual(t["shops"][2]["status"], "missing")

    def test_zero_sales_is_distinct_from_missing_and_all_zero_has_no_rate(self):
        for row in self.values[1:]:
            row[3] = 0
        self.write_table()
        t = self.analyze([])["teams"][0]
        self.assertEqual(t["sales_qty"], 0)
        self.assertIsNone(t["rate"])
        self.assertEqual(t["missing_shops"], [])
        self.assertTrue(all(s["status"] == "zero_sales" for s in t["shops"]))
        t = self.analyze()["teams"][0]
        self.assertIsNone(t["sales_qty"])
        self.assertIn("同店同月", t["rate_warning"])

    def test_failed_cache_never_uses_stale_quantities(self):
        c = self.write_table()
        c["complete"] = False
        feishu_sales._save(self.root, feishu_sales._CACHE, c)
        t = self.analyze()["teams"][0]
        self.assertIsNone(t["rate"])
        self.assertIsNone(t["sales_qty"])
        self.assertTrue(all(s["status"] == "unavailable" for s in t["shops"]))

    def test_unconfigured_erp_display_name_never_guessed(self):
        self.values[-1][0] = "02ruibao_US"
        self.write_table()
        t = self.analyze([self.event("02ruibao_US")])["teams"][2]
        self.assertEqual(t["missing_shops"], ["RUIBAO_US_US"])
        self.assertIn("02ruibao_US", t["unmatched_shops"])
        self.assertIsNone(t["rate"])

    def test_ambiguous_alias_is_not_assigned_to_first_account(self):
        mapping = {**eccang_sales.DEFAULT_ACCOUNT_MAP, "行善": ["xingshan_US", "Wekas_US"]}
        with patch.object(eccang_sales, "load_account_map", return_value=mapping):
            a = self.analyze([self.event("行善")])
        self.assertIn("行善", a["teams"][0]["unmatched_shops"])
        self.assertEqual(a["teams"][0]["rate_numerator"], 0)
        for team in a["teams"][:2]:
            self.assertIsNone(team["rate"])
            self.assertIsNone(team["sales_qty"])
            self.assertIn("配置映射不唯一", team["rate_warning"])
        self.assertEqual(a["teams"][2]["rate"], 0)
        self.assertIsNone(a["overall_rate"])
        self.assertIn("来源映射不唯一", a["sales"]["mapping_warning"])
        self.assertTrue(all(p["rate"] is None for p in a["products"]))
        self.assertTrue(all(s["rate"] is None for s in a["shops"]))
        self.assertTrue(all(p["rate"] is None for s in a["shops"] for p in s["products"]))
        report = (self.root / "report/report.md").read_text(encoding="utf-8")
        self.assertIn("来源映射不唯一", report)
        self.assertNotIn("尚未同步易仓", report)
        self.assertNotIn("整体售后率：**0.0%", report)
        self.assertNotIn("| 0.0% |", report)

    def test_duplicate_sales_aliases_do_not_double_denominator(self):
        self.values.append(["香港", "MR12345", "2026-10-01", 100])
        self.write_table()
        a = self.analyze([self.event("香港店")])
        t = a["teams"][0]
        self.assertIsNone(t["rate"])
        self.assertIsNone(t["sales_qty"])
        self.assertIn("多个销量店名", t["rate_warning"])
        self.assertIn("2026-10", t["rate_warning"])
        self.assertIsNone(a["overall_rate"])
        self.assertIn("KornerHK_US/2026-10", a["sales"]["mapping_warning"])
        self.assertTrue(a["sales"]["complete"])
        self.assertEqual(a["teams"][1]["rate"], 0)
        self.assertEqual(a["teams"][2]["rate"], 0)
        report = (self.root / "report/report.md").read_text(encoding="utf-8")
        self.assertIn("来源映射不唯一", report)
        self.assertNotIn("尚未同步易仓", report)

    def test_confirmed_sales_aliases_in_disjoint_months_share_one_account(self):
        self.values[2][0] = "香港"
        self.values.append(["香港店", "MR12345", "2026-11-01", 300])
        self.write_table()
        rows = [self.event("香港")] + [self.event("香港店", "2026-11-20")] * 3
        team = self.analyze(rows)["teams"][0]
        self.assertEqual(team["rate_numerator"], 4)
        self.assertEqual(team["sales_qty"], 600)
        self.assertEqual(team["rate"], round(4 / 600, 4))
        hong_kong = next(shop for shop in team["shops"] if shop["account"] == "KornerHK_US")
        self.assertEqual(hong_kong["shop_names"], ["香港", "香港店"])
        self.assertEqual(hong_kong["sales_qty"], 400)
        self.assertEqual(hong_kong["rate_numerator"], 4)
        self.assertEqual(hong_kong["rate"], .01)
        self.assertEqual([(d["account"], d["month"], d["qty"])
                          for d in hong_kong["rate_denominators"]],
                         [("KornerHK_US", "2026-10", 100), ("KornerHK_US", "2026-11", 300)])

    def test_changed_sales_alias_between_reports_still_blocks_same_account_month(self):
        first = self.analyze([self.event("香港")])
        self.values[2][0] = "香港店"
        self.write_table()
        second = self.analyze([self.event("香港店")])
        team = combine_team_reports([first, second])[0]
        self.assertIsNone(team["rate"])
        self.assertIsNone(team["sales_qty"])
        self.assertIn("重叠账号月份", team["rate_warning"])

    def test_month_or_date_gap_does_not_drop_numerator_and_show_zero(self):
        for date in ("2026-11-20", ""):
            with self.subTest(date=date):
                a = self.analyze([self.event(date=date)])
                t = a["teams"][0]
                self.assertEqual(t["aftersale"], 1)
                self.assertIsNone(t["rate"])
                self.assertIn("缺日期或同店同月", t["rate_warning"])
                self.assertNotIn("mapping_warning", a["sales"])

    def test_whitelist_preserved_and_excluded_member_is_visible(self):
        shop_filter.save(self.root, ["xingshan_US", "KornerHK_US"])
        t = self.analyze()["teams"][0]
        self.assertEqual(t["excluded_shops"], ["southwest_US"])
        self.assertEqual(t["shops"][2]["status"], "excluded")
        self.assertIsNone(t["rate"])

    def test_detail_mode_uses_internal_counts_and_wayfair_is_not_grouped(self):
        rows = [self.event(a, **{"是否售后": "是" if i == 0 else "否"})
                for i, a in enumerate(ACCOUNTS)]
        a = self.analyze(rows)
        self.assertEqual(a["mode"], "detail")
        self.assertEqual(a["teams"][0]["rate_basis"], "detail")
        self.assertEqual(a["teams"][0]["sales_qty"], 3)
        self.assertEqual(a["teams"][0]["rate"], round(1 / 3, 4))
        self.assertEqual(self.analyze(rows, "WayfairCs")["teams"], [])

    def test_eccang_alias_events_share_one_account_month_denominator(self):
        feishu_sales.save_settings(self.root)
        c = eccang_sales.empty_cache()
        c.update(complete=True, synced_at=123, accounts=ACCOUNTS, done_accounts=ACCOUNTS,
                 months=["2026-10"], qty={a: {"2026-10": 100} for a in ACCOUNTS})
        eccang_sales._save(self.root, c)
        t = self.analyze([self.event("香港"), self.event("香港店"), self.event("行善")])["teams"][0]
        self.assertEqual(t["sales_qty"], 300)
        self.assertEqual(t["rate_numerator"], 3)
        self.assertEqual(t["rate"], .01)
        self.assertEqual(len(t["rate_denominators"]), 3)

    def test_eccang_stale_quantities_outside_completed_account_scope_are_not_used(self):
        feishu_sales.save_settings(self.root)
        cache = eccang_sales.empty_cache()
        cache.update(complete=True, synced_at=123, accounts=ACCOUNTS[:-1],
                     done_accounts=ACCOUNTS[:-1], months=["2026-10"],
                     qty={a: {"2026-10": 100} for a in ACCOUNTS})
        eccang_sales._save(self.root, cache)
        team = self.analyze([self.event("行善")])["teams"][2]
        self.assertEqual(team["missing_shops"], ["RUIBAO_US_US"])
        self.assertIsNone(team["rate"])

    def test_report_merge_exact_disjoint_and_overlap_or_legacy_blocked(self):
        first = self.analyze()
        for row in self.values[1:]:
            row[2] = "2026-11-01"
        self.write_table()
        second = self.analyze([self.event(date="2026-11-10")] * 2)
        merged = combine_team_reports([first, second])
        self.assertEqual(merged[0]["rate_numerator"], 3)
        self.assertEqual(merged[0]["sales_qty"], 600)
        self.assertEqual(merged[0]["rate"], .005)
        self.assertIsNone(combine_team_reports([first, first])[0]["rate"])
        self.assertIn("重叠", combine_team_reports([first, first])[0]["rate_warning"])
        legacy = deepcopy(first)
        del legacy["teams"]
        self.assertIsNone(combine_team_reports([first, legacy])[0]["rate"])
        self.assertIn("重新分析", combine_team_reports([first, legacy])[0]["rate_warning"])
        for g in combine_team_reports([first, legacy]):
            self.assertIsNone(g["records"])
            self.assertIsNone(g["aftersale"])
            self.assertIsNone(g["rate_numerator"])
            self.assertTrue(all(m["aftersale"] is None for m in g["shops"]))
        corrupted = deepcopy(first)
        corrupted["teams"][0]["rate_denominators"][0]["qty"] += 1
        self.assertIsNone(combine_team_reports([corrupted])[0]["rate"])

    def erp_cache(self):
        feishu_sales.save_settings(self.root)
        c = eccang_sales.empty_cache()
        c.update(complete=True, synced_at=123, accounts=ACCOUNTS, done_accounts=ACCOUNTS,
                 months=["2026-09", "2026-10"],
                 qty={a: {"2026-09": 900, "2026-10": 100} for a in ACCOUNTS})
        return c

    def test_eccang_scope_uses_report_months_not_entire_cached_year(self):
        c = self.erp_cache()
        eccang_sales._save(self.root, c)
        t = self.analyze()["teams"][0]
        self.assertEqual(t["sales_qty"], 300)
        self.assertEqual(t["rate"], round(1 / 300, 4))
        self.assertEqual({d["month"] for d in t["rate_denominators"]}, {"2026-10"})
        del c["qty"]["southwest_US"]["2026-10"]
        eccang_sales._save(self.root, c)
        t = self.analyze()["teams"][0]
        self.assertIsNone(t["sales_qty"])
        self.assertIsNone(t["rate"])
        self.assertIn("缺少本次月份", t["rate_warning"])

    def test_eccang_unknown_scope_cannot_show_a_zero_rate(self):
        eccang_sales._save(self.root, self.erp_cache())
        t = self.analyze([self.event(date="")])["teams"][0]
        self.assertIsNone(t["sales_qty"])
        self.assertIsNone(t["rate"])
        self.assertIn("月份范围", t["rate_warning"])

    def test_eccang_conflicting_exact_order_account_blocks_both_teams(self):
        c = self.erp_cache()
        c["order_index"] = {"SYNTH-1": {"a": "Wekas_US", "m": "2026-10"}}
        eccang_sales._save(self.root, c)
        teams = self.analyze([self.event("行善", **{"订单号": "SYNTH-1"})])["teams"]
        self.assertEqual(teams[0]["aftersale"], 1)
        self.assertEqual(teams[1]["aftersale"], 0)
        for t in teams[:2]:
            self.assertIsNone(t["rate"])
            self.assertIn("店铺名与订单账号不一致", t["rate_warning"])

    def test_exact_order_account_outside_teams_does_not_fall_back_to_shop_name(self):
        cache = self.erp_cache()
        cache["order_index"] = {"SYNTH-OUTSIDE": {"a": "OUTSIDE_TEAM", "m": "2026-10"}}
        eccang_sales._save(self.root, cache)
        team = self.analyze([self.event("行善", **{"订单号": "SYNTH-OUTSIDE"})])["teams"][0]
        self.assertIsNone(team["rate"])
        self.assertIsNone(team["sales_qty"])
        self.assertIn("店铺名与订单账号不一致", team["rate_warning"])

    def test_eccang_unknown_shop_exact_account_blocks_target_rate(self):
        c = self.erp_cache()
        c["order_index"] = {"SYNTH-1": {"a": "RUIBAO_US_US", "m": "2026-10"}}
        eccang_sales._save(self.root, c)
        t = self.analyze([self.event("未标注店铺", **{"订单号": "SYNTH-1"})])["teams"][2]
        self.assertIsNone(t["rate"])
        self.assertIn("未标注店铺", t["unmatched_shops"])
        self.assertIn("店铺名与订单账号不一致", t["rate_warning"])

    def test_merge_genuine_zero_quantity_remains_zero_not_missing(self):
        for row in self.values[1:]:
            row[3] = 0
        self.write_table()
        a = self.analyze([])
        t = combine_team_reports([a])[0]
        self.assertEqual(t["sales_qty"], 0)
        self.assertIsNone(t["rate"])
        self.assertIn("合计为 0", t["rate_warning"])
        self.assertTrue(all(m["sales_qty"] == 0 and m["status"] == "zero_sales" for m in t["shops"]))

    def test_changed_worksheet_does_not_allow_overlapping_account_months(self):
        a = self.analyze()
        b = deepcopy(a)
        for t in b["teams"]:
            for d in t["rate_denominators"]:
                d["source_id"] = "another-synthetic-worksheet"
        t = combine_team_reports([a, b])[0]
        self.assertIsNone(t["rate"])
        self.assertIsNone(t["sales_qty"])
        self.assertIn("重叠账号月份", t["rate_warning"])


if __name__ == "__main__":
    unittest.main()
