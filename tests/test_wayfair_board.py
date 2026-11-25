"""Independent examples for the approved Wayfair read-only board contract.

These expectations were written from the approved source/normalization contract,
before reading application candidate code. No real source data or network is used.
"""
import copy
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import wayfair_board as wf
from server import bitable_board as bb


TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")
NOW = datetime(2026, 11, 25, 13, 0, tzinfo=TAIPEI)
SHOP = "Wayfair玺家"
SALES_TABLE = "tblqdIy1im66p76r"
AFTER_TABLE = "tbleZkN9hVYq48g0"
PAIR_TABLE = "tbl6n6QymoJXDciu"
SPECIAL = "SR-YJDXR240424-BE"
SPECIAL_NAME = "240424电动摇转款灯芯绒米色（主推）"
SPECIAL_NEW_NAME = "240424电动摇转款灯芯绒米色新链接（主推）"

# Counter spellings are a mechanical contract binding, not business assertions.
# Root may bind these to the final public names without changing expected counts.
COUNT_KEYS = {
    "invalid_qty": "sales_qty_invalid",
    "invalid_sales_date": "sales_date_invalid",
    "other_shop": "sales_other_shop",
    "sales_t01": "sales_t01_excluded_rows",
    "invalid_after_date": "after_date_invalid",
    "after_t01": "after_t01_excluded",
    "no_sales_month": "after_no_sales_month_rows",
    "shifted": "after_shifted_rows",
    "duplicate_extra": "after_duplicate_extra_rows",
    "duplicate_involved": "after_duplicate_involved_rows",
    "duplicate_groups": "after_duplicate_groups",
}


def ms(year=2026, month=4, day=1, hour=0, minute=0):
    return int(datetime(year, month, day, hour, minute, tzinfo=TAIPEI).timestamp() * 1000)


def rec(fields, marker="private-record-id"):
    return {"record_id": marker, "fields": fields}


def sale(sku="WF-A", qty=1, *, month=4, shop=SHOP, date=None):
    return rec({"店铺": shop, "订单日期": ms(month=month) if date is None else date,
                "SKU": sku, "订单购买数量": qty})


def after(sku="WF-A", *, date="2026-04-10 12:00", order="", reason="质量问题", action="退货"):
    return rec({"客诉产生时间": date, "SKU": sku, "订单号": order,
                "售后原因": reason, "原因": action})


def pair(sku="A", wf_sku="WF-A", name="产品甲"):
    return rec({"易仓SKU": sku, "SKU中文名称": name,
                "WF Part Number （整个SKU）": wf_sku})


def build(sales=(), aftersales=(), pairs=None, *, now=NOW):
    return wf.build_snapshot(list(sales), list(aftersales),
                             [pair()] if pairs is None else list(pairs),
                             now=now, source_meta={"table_name": "synthetic readonly contract"})


def shop(snap):
    shops = [s for group in snap["teams"] for s in group["shops"]]
    assert len(shops) == 1
    return shops[0]


def sku_row(snap, sku="A"):
    return next(s for s in shop(snap)["skus"] if s["sku"] == sku)


def monthly(snap, sku="A", month="2026-04"):
    return next(r for r in snap["aftersale"]["sku_store_month"]
                if r["sku"] == sku and r["month"] == month)


def count(snap, semantic):
    return snap["counts"][COUNT_KEYS[semantic]]


class WayfairSalesContractTests(unittest.TestCase):
    def test_orders_add_purchase_quantities_instead_of_counting_rows_or_component_minimum(self):
        snap = build([sale(qty="1"), sale(qty="2"), sale(qty=3)])
        self.assertEqual(sku_row(snap)["sold"]["2026-04"], 6)
        self.assertEqual(shop(snap)["months"]["2026-04"], 6)
        self.assertEqual(shop(snap)["units_known"], 6)
        self.assertEqual(snap["aftersale"]["total"]["denominator"], 6)

    def test_only_trimmed_exact_xijia_shop_is_admitted(self):
        snap = build([sale(qty=2), sale(qty=3, shop=" Wayfair玺家 "),
                      sale(qty=20, shop="WayfairSouthwest"),
                      sale(qty=30, shop="wayfair玺家"), sale(qty=40, shop="玺家")])
        self.assertEqual(shop(snap)["account"], SHOP)
        self.assertEqual(shop(snap)["units_known"], 5)
        self.assertEqual(count(snap, "other_shop"), 3)

    def test_taipei_midnight_boundary_includes_april_not_march(self):
        april_start_utc = int(datetime(2026, 3, 31, 16, tzinfo=timezone.utc).timestamp() * 1000)
        may_start_utc = int(datetime(2026, 4, 30, 16, tzinfo=timezone.utc).timestamp() * 1000)
        snap = build([sale(qty=70, date=april_start_utc - 1),
                      sale(qty=2, date=april_start_utc), sale(qty=3, date=may_start_utc)])
        self.assertEqual(sku_row(snap)["sold"]["2026-04"], 2)
        self.assertEqual(sku_row(snap)["sold"]["2026-05"], 3)
        self.assertEqual(shop(snap)["units_known"], 5)
        self.assertEqual(snap["time_zone"], "Asia/Taipei")
        self.assertEqual(snap["months"][0], "2026-04")
        self.assertNotIn("2026-03", snap["months"])

    def test_invalid_quantities_are_ignored_and_do_not_poison_valid_orders(self):
        invalid = [None, "", -1, "-2", 1.5, True, "garbage", math.nan, math.inf]
        rows = [sale(qty=v) for v in invalid] + [sale(qty="1"), sale(qty=2),
                sale(qty=3.0), sale(qty="4.0"), sale(qty=0)]
        snap = build(rows)
        self.assertEqual(shop(snap)["units_known"], 10)
        self.assertEqual(sku_row(snap)["sold"]["2026-04"], 10)
        self.assertEqual(count(snap, "invalid_qty"), 9)

    def test_invalid_dates_are_not_coerced_to_current_month(self):
        bad = [sale(qty=90, date=True), sale(qty=80, date="not-a-date")]
        missing = sale(qty=70)
        missing["fields"]["订单日期"] = None
        snap = build(bad + [missing, sale(qty=2)])
        self.assertEqual(shop(snap)["units_known"], 2)
        self.assertEqual(count(snap, "invalid_sales_date"), 3)

    def test_explicit_zero_and_absent_sales_have_distinct_values(self):
        snap = build([sale(qty=0)])
        self.assertEqual(sku_row(snap)["sold"]["2026-04"], 0)
        self.assertIsNone(sku_row(snap)["sold"].get("2026-05"))
        self.assertIsNone(monthly(snap)["rate"])
        self.assertEqual(monthly(snap)["denominator"], 0)
        self.assertFalse(any(p["confirmed_zero"] for p in snap["products"]))

    def test_distinct_wf_identifiers_for_special_canonical_are_both_counted(self):
        pairs = [pair(SPECIAL, SPECIAL, SPECIAL_NAME),
                 pair(SPECIAL, "240424PW-BE", SPECIAL_NEW_NAME)]
        snap = build([sale(SPECIAL, 4), sale("240424PW-BE", 3)], pairs=pairs)
        self.assertEqual(sku_row(snap, SPECIAL)["sold"]["2026-04"], 7)
        products = [p for p in snap["products"] if p["sku"] == SPECIAL]
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["display_name"], SPECIAL_NAME)
        self.assertIn(SPECIAL, products[0]["wf_part_number"])
        self.assertIn("240424PW-BE", products[0]["wf_part_number"])

    def test_exact_case_sensitive_matching_never_fuzzily_pairs_a_suffix(self):
        snap = build([sale(" WF-A ", 2), sale("wf-a", 5), sale("WF-A-2R", 7)])
        self.assertEqual(shop(snap)["units_known"], 2)
        self.assertEqual(snap["unpaired"]["total_units"], 12)
        self.assertEqual([(x["sku"], x["units"]) for x in snap["unpaired"]["sales"]],
                         [("WF-A-2R", 7), ("wf-a", 5)])

    def test_conflicting_wf_key_and_unrecognized_canonical_name_conflicts_fail_closed(self):
        conflicts = [
            [pair("A", "WF-X", "甲"), pair("B", "WF-X", "乙")],
            [pair("A", "WF-A", "甲"), pair("A", "WF-OTHER", "另一个中文名")],
        ]
        for pairs in conflicts:
            with self.subTest(pairs=pairs):
                with self.assertRaises(bb.BoardError):
                    build([sale()], pairs=pairs)

    def test_t01_excluded_from_every_denominator_but_t010_is_kept(self):
        pairs = [pair(), pair("SRT01-BR", "WF-T", "T01 产品"),
                 pair("SRT010-BR", "WF-T010", "T010 产品")]
        snap = build([sale(qty=2), sale("WF-T", 50), sale("WF-T010", 3),
                      sale("UNKNOWN-T01-BR", 70)], pairs=pairs)
        self.assertEqual(shop(snap)["units_known"], 5)
        self.assertEqual(snap["aftersale"]["total"]["denominator"], 5)
        self.assertEqual({p["sku"] for p in snap["products"]}, {"A", "SRT010-BR"})
        self.assertEqual(snap["unpaired"]["total_units"], 0)
        self.assertEqual(count(snap, "sales_t01"), 2)

    def test_month_axis_is_not_hardcoded_to_november(self):
        snap = build([sale(qty=2, month=12)])
        self.assertIn("2026-12", snap["months"])
        self.assertEqual(sku_row(snap)["sold"]["2026-12"], 2)


class WayfairAftersaleContractTests(unittest.TestCase):
    def test_each_aftersale_row_counts_one_even_when_same_order_and_sku_repeat(self):
        rows = [after(order="same-order"), after(order="same-order"),
                after(order="another-order")]
        snap = build([sale(qty=10)], rows)
        self.assertEqual(snap["aftersale"]["total"]["numerator"], 3)
        self.assertEqual(monthly(snap)["numerator"], 3)
        self.assertAlmostEqual(monthly(snap)["rate"], 30, places=2)
        self.assertEqual(count(snap, "duplicate_extra"), 1)
        self.assertEqual(count(snap, "duplicate_involved"), 2)
        self.assertEqual(count(snap, "duplicate_groups"), 1)

    def test_duplicate_audit_is_before_pairing_and_t01_exclusions(self):
        rows = ([after(order="o1") for _ in range(3)] +
                [after("UNKNOWN", order="o2") for _ in range(2)] +
                [after("WF-T", order="o3") for _ in range(2)])
        snap = build([sale(qty=10)], rows,
                     pairs=[pair(), pair("T01-A", "WF-T", "T01 产品")])
        self.assertEqual(snap["aftersale"]["total"]["numerator"], 3)
        self.assertEqual(count(snap, "duplicate_extra"), 4)
        self.assertEqual(count(snap, "duplicate_involved"), 7)
        self.assertEqual(count(snap, "duplicate_groups"), 3)
        self.assertEqual(snap["unpaired"]["aftersale"], [{"sku": "UNKNOWN", "rows": 2}])
        self.assertEqual(count(snap, "after_t01"), 2)

    def test_missing_order_number_is_not_a_duplicate_key_and_sku_is_part_of_key(self):
        rows = [after(order=""), after(order=""), after(order="shared"),
                after("WF-B", order="shared")]
        snap = build([sale(qty=10), sale("WF-B", 10)], rows,
                     pairs=[pair(), pair("B", "WF-B", "产品乙")])
        self.assertEqual(snap["aftersale"]["total"]["numerator"], 4)
        self.assertEqual(count(snap, "duplicate_extra"), 0)
        self.assertEqual(count(snap, "duplicate_involved"), 0)
        self.assertEqual(count(snap, "duplicate_groups"), 0)

    def test_rolls_backward_across_missing_and_zero_months_and_reasons_follow_assigned_month(self):
        rows = [after(date="2026-06-01 09:00", reason="质量问题（破损）"),
                after(date="2026-05-20", reason="物流问题—破损"),
                after(date="2026-04-10", reason="无理由（与描述不符）"),
                after(date="2026-07-11", reason="")]
        snap = build([sale(qty=10), sale(qty=0, month=5), sale(qty=5, month=7)], rows)
        self.assertEqual(monthly(snap)["numerator"], 3)
        self.assertEqual(monthly(snap, month="2026-05")["numerator"], 0)
        self.assertIsNone(monthly(snap, month="2026-05")["rate"])
        self.assertEqual(monthly(snap, month="2026-07")["numerator"], 1)
        self.assertFalse(any(r["month"] == "2026-06" for r in snap["aftersale"]["sku_store_month"]))
        self.assertEqual(count(snap, "shifted"), 2)
        primary = [r for r in snap["aftersale"]["reasons"]["items"] if r["month"] == "2026-04"]
        self.assertEqual({r["category"]: r["count"] for r in primary},
                         {"质量问题": 1, "物流问题": 1, "无理由": 1})
        for r in primary:
            self.assertAlmostEqual(r["share"], 100 / 3, places=2)
        children = [r for r in snap["aftersale"]["reasons"]["subcategory_items"]
                    if r["month"] == "2026-04"]
        self.assertEqual({(r["category"], r["subcategory"]) for r in children},
                         {("质量问题", "破损"), ("物流问题", "破损"), ("无理由", "与描述不符")})
        self.assertTrue(all(r["count"] == 1 and r["share"] == 100 for r in children))

    def test_cannot_roll_forward_cross_product_or_before_april(self):
        snap = build([sale(qty=10), sale("WF-B", 20, month=7)],
                     [after("WF-B", date="2026-06-10"),
                      after("WF-C", date="2026-04-10")],
                     pairs=[pair(), pair("B", "WF-B", "乙"), pair("C", "WF-C", "丙")])
        self.assertEqual(snap["aftersale"]["total"]["numerator"], 0)
        self.assertEqual(count(snap, "no_sales_month"), 2)
        self.assertEqual(count(snap, "shifted"), 0)

    def test_only_valid_fully_dated_april_onward_rows_enter_numerator_and_duplicate_audit(self):
        rows = [after(date=None, order="excluded"), after(date="04-10", order="excluded"),
                after(date="2026-02-30", order="excluded"),
                after(date="2026-03-31 23:59", order="excluded"),
                after(date="2026-04-01 00:00", order="valid")]
        snap = build([sale(qty=10)], rows)
        self.assertEqual(snap["aftersale"]["total"]["numerator"], 1)
        self.assertEqual(count(snap, "invalid_after_date"), 3)
        self.assertEqual(count(snap, "duplicate_extra"), 0)

    def test_positive_sales_without_aftersales_is_true_zero_not_unknown(self):
        snap = build([sale(qty=4)], [])
        self.assertEqual(monthly(snap)["numerator"], 0)
        self.assertEqual(monthly(snap)["rate"], 0)
        self.assertEqual({k: snap["aftersale"]["total"][k]
                          for k in ("numerator", "denominator", "rate")},
                         {"numerator": 0, "denominator": 4, "rate": 0})

    def test_reason_action_field_is_ignored_and_two_level_aggregate_sums_match_numerator(self):
        rows = [after(reason="质量问题（断裂）", action="退款"),
                after(reason="质量问题（不起作用）", action="替换件"),
                after(reason="破损", action="退货")]
        snap = build([sale(qty=10)], rows)
        reasons = snap["aftersale"]["reasons"]
        self.assertEqual(sum(r["count"] for r in reasons["items"]), 3)
        self.assertEqual(sum(r["count"] for r in reasons["subcategory_items"]), 3)
        categories = {r["category"]: r["count"] for r in reasons["items"]}
        self.assertEqual(categories, {"质量问题": 2, "破损损坏": 1})
        for r in reasons["subcategory_items"]:
            self.assertNotIn(r["subcategory"], {"退款", "替换件", "退货"})
            expected = 50 if r["category"] == "质量问题" else 100
            self.assertAlmostEqual(r["share"], expected, places=2)

    def test_public_shape_never_returns_private_order_record_source_or_original_fields(self):
        row = after(order="PRIVATE-ORDER-DO-NOT-RETURN")
        row["record_id"] = "PRIVATE-RECORD-DO-NOT-RETURN"
        row["fields"].update({"订单号_hash": "PRIVATE-HASH-DO-NOT-RETURN",
                              "SourceID": "PRIVATE-SOURCE-DO-NOT-RETURN",
                              "原文": "PRIVATE-CUSTOMER-MAIL-DO-NOT-RETURN"})
        source = sale(qty=10)
        source["fields"]["订单编号"] = "PRIVATE-SALES-ORDER-DO-NOT-RETURN"
        snap = build([source], [row])
        serialized = json.dumps(snap, ensure_ascii=False)
        self.assertNotIn("PRIVATE-", serialized)
        forbidden = {"record_id", "订单号", "订单号_hash", "订单编号", "SourceID", "原文"}
        def visit(value):
            if isinstance(value, dict):
                self.assertFalse(forbidden.intersection(value))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(snap)

    def test_pure_build_does_not_mutate_input_and_totals_agree_at_every_level(self):
        sales = [sale(qty=2), sale(qty=3, month=5), sale("WF-B", 7)]
        rows = [after(reason="质量问题"), after("WF-B", reason="物流问题")]
        pairs = [pair(), pair("B", "WF-B", "乙")]
        before = copy.deepcopy((sales, rows, pairs))
        snap = build(sales, rows, pairs)
        self.assertEqual((sales, rows, pairs), before)
        self.assertEqual(snap["platform"], "Wayfair")
        self.assertEqual(snap["aftersale"]["teams"], [])
        self.assertEqual(snap["aftersale"]["total"]["denominator"], 12)
        self.assertEqual(snap["aftersale"]["total"]["numerator"], 2)
        for field in ("denominator", "numerator"):
            expected = snap["aftersale"]["total"][field]
            for section in ("stores", "store_month", "sku_store_month"):
                self.assertEqual(sum(r[field] for r in snap["aftersale"][section]), expected)


class WayfairReasonContractTests(unittest.TestCase):
    def test_approved_aliases_and_whitespace(self):
        cases = {
            " Mis infor ": ("信息/描述不符", "未注明"),
            "Misinfor": ("信息/描述不符", "未注明"),
            "Misinformation": ("信息/描述不符", "未注明"),
            "Mis-information": ("信息/描述不符", "未注明"),
            "different on website": ("信息/描述不符", "未注明"),
            "defective": ("质量问题", "未注明"),
            "defect": ("质量问题", "未注明"),
            "warranty": ("保修", "未注明"),
            "保修": ("保修", "未注明"),
            "破损": ("破损损坏", "未注明"),
            "不需要": ("不再需要", "未注明"),
            "不再需要": ("不再需要", "未注明"),
            "不符期待": ("不符合期待", "未注明"),
            "不舒服": ("舒适度问题", "未注明"),
            "面料脏": ("面料/外观问题", "污渍"),
            "产品有污渍": ("面料/外观问题", "污渍"),
            "面料质量不佳": ("面料/外观问题", "未注明"),
            "质量问题 ": ("质量问题", "未注明"),
            "": ("未注明", "未注明"),
            "   ": ("未注明", "未注明"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(tuple(wf.reason_parts(text)), expected)

    def test_explicit_chinese_parent_does_not_get_reclassified_by_its_detail(self):
        cases = {
            "无理由（与描述不符）": ("无理由", "与描述不符"),
            "无理由（different on website）": ("无理由", "与网站描述不符"),
            "质量问题（破损）": ("质量问题", "破损"),
            "物流问题—破损": ("物流问题", "破损"),
            "质量问题-断裂": ("质量问题", "断裂"),
            " defective ( damage ) ": ("质量问题", "破损"),
            "破损—底座扶手连接处断开": ("破损损坏", "底座扶手连接处断开"),
            "质量问题（）": ("质量问题", "未注明"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(tuple(wf.reason_parts(text)), expected)

    def test_new_unknown_reason_is_retained_not_guessed_from_damage_keyword(self):
        self.assertEqual(tuple(wf.reason_parts("package damage")), ("package damage", "未注明"))
        self.assertEqual(tuple(wf.reason_parts("新原因—新细项")), ("新原因", "新细项"))


SALES_FIELDS = [("店铺", 1), ("订单日期", 5), ("SKU", 1), ("订单购买数量", 1)]
AFTER_FIELDS = [("客诉产生时间", 1), ("SKU", 1), ("售后原因", 1), ("订单号", 1)]
PAIR_FIELDS = [("易仓SKU", 1), ("SKU中文名称", 1), ("WF Part Number （整个SKU）", 1)]


class ReadonlyHttp:
    """Feishu-shaped fields/records responses; any non-GET call fails the test."""
    def __init__(self):
        self.calls = []
        self.records = {SALES_TABLE: [sale(qty=3)], AFTER_TABLE: [after()], PAIR_TABLE: [pair()]}
        self.fields = {SALES_TABLE: SALES_FIELDS[:], AFTER_TABLE: AFTER_FIELDS[:], PAIR_TABLE: PAIR_FIELDS[:]}
        self.fail_tables = set()
        self.paginated = False
        self.missing_page_token = False
        self.failure_message = "upstream failure includes private-app-secret and private-tenant-token"

    def __call__(self, url, **kwargs):
        if kwargs.get("method", "GET") != "GET":
            raise AssertionError("A source table write was attempted")
        parsed = urlparse(url)
        parts = parsed.path.split("/")
        table = parts[parts.index("tables") + 1]
        resource = parts[-1]
        if table not in self.records or resource not in {"fields", "records"}:
            raise AssertionError("Unexpected source/table endpoint")
        query = parse_qs(parsed.query)
        page = query.get("page_token", [""])[0]
        self.calls.append((table, resource, page))
        if table in self.fail_tables:
            raise RuntimeError(self.failure_message)
        items = ([{"field_name": name, "type": kind} for name, kind in self.fields[table]]
                 if resource == "fields" else copy.deepcopy(self.records[table]))
        has_more, next_token = False, ""
        if self.paginated and table == SALES_TABLE:
            split = 2 if resource == "fields" else 1
            if not page:
                items, has_more, next_token = items[:split], True, "p2"
            else:
                if page != "p2":
                    raise AssertionError("Unexpected page token")
                items = items[split:]
        if self.missing_page_token and table == AFTER_TABLE and resource == "records":
            has_more, next_token = True, ""
        return {"code": 0, "data": {"items": items, "has_more": has_more, "page_token": next_token}}


class WayfairCacheContractTests(unittest.TestCase):
    def get(self, http, cache, *, refresh=False, app_id="contract-app", secret="private-app-secret", now=NOW):
        with patch("server.feishu.get_tenant_token", return_value="private-tenant-token"):
            return wf.get_board(app_id, secret, refresh=refresh, now_fn=lambda: now, http=http, cache=cache)

    def test_complete_get_pagination_and_text_or_number_quantity_schema(self):
        for qty_type in (1, 2):
            with self.subTest(qty_type=qty_type):
                http = ReadonlyHttp()
                http.fields[SALES_TABLE][-1] = ("订单购买数量", qty_type)
                http.records[SALES_TABLE] = [sale(qty=1), sale(qty=2)]
                http.paginated = True
                result = self.get(http, {})
                self.assertTrue(result["ok"])
                self.assertEqual(shop(result)["units_known"], 3)
                self.assertEqual({table for table, _, _ in http.calls},
                                 {SALES_TABLE, AFTER_TABLE, PAIR_TABLE})
                self.assertIn((SALES_TABLE, "fields", "p2"), http.calls)
                self.assertIn((SALES_TABLE, "records", "p2"), http.calls)

    def test_cache_has_thirty_second_ttl_and_returns_deep_copies(self):
        http, cache = ReadonlyHttp(), {}
        with patch("time.time", return_value=1000):
            first = self.get(http, cache)
        self.assertTrue(first["ok"])
        calls = len(http.calls)
        first["teams"][0]["shops"][0]["months"]["2026-04"] = -90
        first["products"][0]["display_name"] = "MUTATED"
        http.records[SALES_TABLE] = [sale(qty=9)]
        with patch("time.time", return_value=1029):
            cached = self.get(http, cache)
        self.assertEqual(len(http.calls), calls)
        self.assertEqual(shop(cached)["units_known"], 3)
        self.assertEqual(shop(cached)["months"]["2026-04"], 3)
        self.assertEqual(cached["products"][0]["display_name"], "产品甲")
        with patch("time.time", return_value=1031):
            refreshed = self.get(http, cache)
        self.assertGreater(len(http.calls), calls)
        self.assertEqual(shop(refreshed)["units_known"], 9)

    def test_explicit_refresh_does_not_use_fresh_cache(self):
        http, cache = ReadonlyHttp(), {}
        with patch("time.time", return_value=1000):
            self.assertEqual(shop(self.get(http, cache))["units_known"], 3)
            http.records[SALES_TABLE] = [sale(qty=8)]
            result = self.get(http, cache, refresh=True)
        self.assertEqual(shop(result)["units_known"], 8)

    def test_aftersale_read_failure_retains_entire_old_coherent_snapshot(self):
        http, cache = ReadonlyHttp(), {}
        old = self.get(http, cache)
        self.assertTrue(old["ok"])
        original = copy.deepcopy(old)
        http.records[SALES_TABLE] = [sale(qty=99)]
        http.fail_tables.add(AFTER_TABLE)
        result = self.get(http, cache, refresh=True, now=NOW + timedelta(hours=1))
        self.assertTrue(result["ok"])
        self.assertTrue(result["stale"])
        self.assertTrue(result["error"])
        self.assertEqual(result["fetched_at"], original["fetched_at"])
        self.assertEqual(result["last_success_at"], original["last_success_at"] or original["fetched_at"])
        self.assertEqual(result["products"], original["products"])
        self.assertEqual(result["teams"], original["teams"])
        self.assertEqual(result["aftersale"]["total"], original["aftersale"]["total"])
        self.assertEqual(result["aftersale"]["source_as_of"], original["aftersale"]["source_as_of"])
        self.assertEqual(result["aftersale"]["sales_source_as_of"], original["aftersale"]["sales_source_as_of"])
        self.assertNotIn("private-app-secret", json.dumps(result))
        self.assertNotIn("private-tenant-token", json.dumps(result))
        # A failed refresh must not poison the known-good cached data.
        http.fail_tables.clear()
        with patch("time.time", return_value=0):
            cached = self.get(http, cache)
        self.assertFalse(cached["stale"])
        self.assertEqual(shop(cached)["units_known"], 3)

    def test_schema_drift_and_conflicting_mapping_refresh_keep_same_good_snapshot(self):
        for broken in ("schema", "mapping"):
            with self.subTest(broken=broken):
                http, cache = ReadonlyHttp(), {}
                old = self.get(http, cache)
                if broken == "schema":
                    http.fields[SALES_TABLE][2] = ("SKU", 2)
                else:
                    http.records[PAIR_TABLE].append(pair("B", "WF-A", "乙"))
                result = self.get(http, cache, refresh=True)
                self.assertTrue(result["stale"])
                self.assertEqual(result["teams"], old["teams"])
                self.assertEqual(result["aftersale"]["total"], old["aftersale"]["total"])

    def test_incomplete_pagination_cannot_be_presented_or_cached_as_success(self):
        http, cache = ReadonlyHttp(), {}
        http.missing_page_token = True
        result = self.get(http, cache)
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])
        http.missing_page_token = False
        http.records[SALES_TABLE] = [sale(qty=7)]
        result = self.get(http, cache)
        self.assertTrue(result["ok"])
        self.assertEqual(shop(result)["units_known"], 7)

    def test_first_failure_has_safe_error_and_no_cross_credential_cache_fallback(self):
        http, cache = ReadonlyHttp(), {}
        good = self.get(http, cache)
        self.assertTrue(good["ok"])
        http.fail_tables.add(SALES_TABLE)
        http.failure_message = "upstream failure includes different-secret and private-tenant-token"
        result = self.get(http, cache, app_id="different-app", secret="different-secret")
        self.assertFalse(result["ok"])
        self.assertFalse(result["stale"])
        serialized = json.dumps(result)
        self.assertNotIn("private-app-secret", serialized)
        self.assertNotIn("private-tenant-token", serialized)
        self.assertNotIn("different-secret", serialized)

    def test_cache_identity_separates_base_override(self):
        http, cache = ReadonlyHttp(), {}
        with patch.dict(os.environ, {"VOC_BITABLE_APP_TOKEN": "contract-base-one"}):
            self.assertTrue(self.get(http, cache)["ok"])
        http.fail_tables.add(SALES_TABLE)
        with patch.dict(os.environ, {"VOC_BITABLE_APP_TOKEN": "contract-base-two"}):
            result = self.get(http, cache)
        self.assertFalse(result["ok"])
        self.assertFalse(result["stale"])


if __name__ == "__main__":
    unittest.main()
