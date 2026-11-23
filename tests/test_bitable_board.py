import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import app as api
from server import bitable_board as bb

UTC8 = timezone(timedelta(hours=8), "Asia/Shanghai")

TEAM1_TABLE_ID = "tblQkF7Vw69SVAx7"
TEAM2_TABLE_ID = "tblaL84UEi5AtsZn"
TEAM3_TABLE_ID = "tblsuoDngHQbirFL"
PAIR_TABLE_ID = "tbl6n6QymoJXDciu"

AMAZON_FIELDS = [
    {"field_id": "f1", "field_name": "平台", "type": 1},
    {"field_id": "f2", "field_name": "店铺账号", "type": 1},
    {"field_id": "f3", "field_name": "店铺名称", "type": 1},
    {"field_id": "f4", "field_name": "仓库SKU", "type": 1},
    {"field_id": "f5", "field_name": "销量", "type": 2},
    {"field_id": "f6", "field_name": "月份（自然月）", "type": 5},
]

PAIR_FIELDS = [
    {"field_id": "p1", "field_name": "易仓SKU", "type": 1},
    {"field_id": "p2", "field_name": "SKU中文名称", "type": 1},
    {"field_id": "p3", "field_name": "WF Part Number （整个SKU）", "type": 1},
]

def fields_response(field_items):
    return {"code": 0, "data": {"items": field_items, "has_more": False, "page_token": ""}}

class RoutedFakeHttp:
    def __init__(self, table_responses):
        self.table_responses = table_responses
        self.calls = []

    def _extract_table_id(self, url):
        parts = url.split("/tables/")
        if len(parts) < 2:
            raise AssertionError("URL does not contain table id: " + url)
        table_id = parts[1].split("/")[0].split("?")[0]
        return table_id

    def __call__(self, url, method="GET", body=None, headers=None, timeout=30):
        self.calls.append((url, method, headers or {}))
        table_id = self._extract_table_id(url)
        endpoint = "fields" if "/fields" in url else "records"
        resp_list = self.table_responses.get(table_id, {}).get(endpoint, [])
        if not resp_list:
            raise AssertionError(f"no fake response for {url}")
        resp = resp_list.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp

def make_valid_board_fake():
    team1_records = [rec({
        "平台": "Amazon",
        "店铺账号": "xingshan_US",
        "仓库SKU": "S",
        "销量": "1",
        "月份（自然月）": local_ms(2026, 4, 1),
        "店铺名称": "杭州行善_US",
    })]
    pair_records = [rec({
        "易仓SKU": "S",
        "SKU中文名称": "中文名",
        "WF Part Number （整个SKU）": "WF-S",
    })]
    return RoutedFakeHttp({
        TEAM1_TABLE_ID: {
            "fields": [valid_fields()],
            "records": [record_page(team1_records, False)],
        },
        TEAM2_TABLE_ID: {
            "fields": [valid_fields()],
            "records": [record_page([], False)],
        },
        TEAM3_TABLE_ID: {
            "fields": [valid_fields()],
            "records": [record_page([], False)],
        },
        PAIR_TABLE_ID: {
            "fields": [fields_response(PAIR_FIELDS)],
            "records": [record_page(pair_records, False)],
        },
    })

def add_valid_board_responses(fake):
    team1_records = [rec({
        "平台": "Amazon",
        "店铺账号": "xingshan_US",
        "仓库SKU": "S",
        "销量": "1",
        "月份（自然月）": local_ms(2026, 4, 1),
        "店铺名称": "杭州行善_US",
    })]
    pair_records = [rec({
        "易仓SKU": "S",
        "SKU中文名称": "中文名",
        "WF Part Number （整个SKU）": "WF-S",
    })]
    fake.table_responses[TEAM1_TABLE_ID]["fields"].append(valid_fields())
    fake.table_responses[TEAM1_TABLE_ID]["records"].append(record_page(team1_records, False))
    fake.table_responses[TEAM2_TABLE_ID]["fields"].append(valid_fields())
    fake.table_responses[TEAM2_TABLE_ID]["records"].append(record_page([], False))
    fake.table_responses[TEAM3_TABLE_ID]["fields"].append(valid_fields())
    fake.table_responses[TEAM3_TABLE_ID]["records"].append(record_page([], False))
    fake.table_responses[PAIR_TABLE_ID]["fields"].append(fields_response(PAIR_FIELDS))
    fake.table_responses[PAIR_TABLE_ID]["records"].append(record_page(pair_records, False))


def local_ms(y, m, d, hh=0, mm=0):
    dt = datetime(y, m, d, hh, mm, tzinfo=UTC8)
    return int(dt.timestamp() * 1000)


def valid_fields():
    return {
        "code": 0,
        "data": {
            "items": [
                {"field_id": "f1", "field_name": "平台", "type": 1},
                {"field_id": "f2", "field_name": "店铺账号", "type": 1},
                {"field_id": "f3", "field_name": "仓库SKU", "type": 1},
                {"field_id": "f4", "field_name": "销量", "type": 2},
                {"field_id": "f5", "field_name": "月份（自然月）", "type": 5},
                {"field_id": "f6", "field_name": "店铺名称", "type": 1},
            ],
            "has_more": False,
            "page_token": "",
        },
    }


def record_page(items, has_more, page_token=""):
    return {"code": 0, "data": {"items": items, "has_more": has_more, "page_token": page_token}}


def rec(fields):
    return {"record_id": "r", "fields": fields}


class FakeHttp:
    def __init__(self):
        self.calls = []
        self.pages = []

    def add(self, response):
        self.pages.append(response)

    def __call__(self, url, method="GET", body=None, headers=None, timeout=30):
        self.calls.append((url, method, headers or {}))
        if not self.pages:
            raise AssertionError("no fake response for %s" % url)
        return self.pages.pop(0)


class FailHttp:
    def __init__(self, exc):
        self.exc = exc

    def __call__(self, *args, **kwargs):
        raise self.exc


class AlwaysMoreHttp:
    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return {"code": 0, "data": {"items": [], "has_more": True, "page_token": str(self.calls)}}


class BitableBoardTests(unittest.TestCase):
    def test_fetch_fields_valid(self):
        f = FakeHttp()
        f.add(valid_fields())
        fields = bb.fetch_fields("tok", "app", "tbl", http=f)
        self.assertEqual(fields["平台"]["type"], 1)

    def test_fetch_fields_missing_required_field(self):
        resp = valid_fields()
        resp["data"]["items"] = [x for x in resp["data"]["items"] if x["field_name"] != "销量"]
        f = FakeHttp()
        f.add(resp)
        with self.assertRaises(bb.BoardError) as caught:
            bb.fetch_fields("tok", "app", "tbl", http=f)
        self.assertIn("字段漂移诊断", str(caught.exception))
        self.assertIn("销量", str(caught.exception))

    def test_fetch_fields_wrong_type(self):
        resp = valid_fields()
        for x in resp["data"]["items"]:
            if x["field_name"] == "销量":
                x["type"] = 1
        f = FakeHttp()
        f.add(resp)
        with self.assertRaises(bb.BoardError) as caught:
            bb.fetch_fields("tok", "app", "tbl", http=f)
        self.assertIn("字段类型漂移", str(caught.exception))

    def test_fetch_records_pagination_complete_3_pages(self):
        f = FakeHttp()
        f.add(record_page([rec({"平台": "Amazon"})], True, "p1"))
        f.add(record_page([rec({"平台": "Amazon"}), rec({"平台": "Amazon"})], True, "p2"))
        f.add(record_page([rec({"平台": "Amazon"})], False))
        items, pages = bb.fetch_records("tok", "app", "tbl", http=f)
        self.assertEqual(pages, 3)
        self.assertEqual(len(items), 4)

    def test_fetch_records_has_more_without_token_raises(self):
        f = FakeHttp()
        f.add(record_page([], True, ""))
        with self.assertRaises(bb.BoardError):
            bb.fetch_records("tok", "app", "tbl", http=f)

    def test_fetch_records_duplicate_page_token_raises(self):
        f = FakeHttp()
        f.add(record_page([], True, "dup"))
        f.add(record_page([], True, "dup"))
        with self.assertRaises(bb.BoardError) as caught:
            bb.fetch_records("tok", "app", "tbl", http=f)
        self.assertIn("重复", str(caught.exception))

    def test_fetch_records_over_page_limit_raises(self):
        with self.assertRaises(bb.BoardError) as caught:
            bb.fetch_records("tok", "app", "tbl", http=AlwaysMoreHttp())
        self.assertIn("安全上限", str(caught.exception))

    def test_normalize_sales_string_and_zero(self):
        rows, diag = bb.normalize([rec({
            "平台": "Amazon",
            "店铺账号": "xingshan_US",
            "仓库SKU": "S-1",
            "销量": " 1 ",
            "月份（自然月）": local_ms(2026, 4, 1),
            "店铺名称": "杭州行善_US",
        }), rec({
            "平台": "Amazon",
            "店铺账号": "xingshan_US",
            "仓库SKU": "S-ZERO",
            "销量": "0",
            "月份（自然月）": local_ms(2026, 4, 1),
            "店铺名称": "杭州行善_US",
        })])
        self.assertEqual(diag, [])
        self.assertEqual(rows[0]["qty"], 1)
        self.assertEqual(rows[1]["qty"], 0)
        self.assertFalse(rows[0]["invalid"])

    def test_invalid_qty_keeps_row_but_null_and_diagnostic(self):
        rows, diag = bb.normalize([rec({
            "平台": "Amazon",
            "店铺账号": "xingshan_US",
            "仓库SKU": "S",
            "销量": "-1",
            "月份（自然月）": local_ms(2026, 4, 1),
        })])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["invalid"])
        self.assertIsNone(rows[0]["qty"])
        self.assertTrue(any(d["code"] == "invalid_qty" for d in diag))

    def test_month_utc8_boundary(self):
        utc_ms = int(datetime(2026, 11, 30, 16, 30, tzinfo=timezone.utc).timestamp() * 1000)
        rows, _ = bb.normalize([rec({
            "平台": "Amazon",
            "店铺账号": "xingshan_US",
            "仓库SKU": "S",
            "销量": "1",
            "月份（自然月）": utc_ms,
        })])
        self.assertEqual(rows[0]["month"], "2026-12")

    def _amazon_row(self, account="xingshan_US", sku="SKU", qty="1", month="2026-04"):
        return {
            "platform": "Amazon",
            "account": account,
            "sku": sku,
            "month": month,
            "qty": bb._parse_qty(qty),
            "invalid": bb._parse_qty(qty) is None,
            "display_name": account,
        }

    def _source_meta(self):
        return {"table_name": "AMAZON销量数据", "row_count": 1, "page_count": 1}

    def test_duplicate_key_null_and_diagnostic(self):
        rows = [
            self._amazon_row(sku="DUP", qty="1"),
            self._amazon_row(sku="DUP", qty="2"),
        ]
        now = datetime(2026, 12, 1, tzinfo=UTC8)
        snap = bb.build_snapshot(rows, [], now=now, source_meta=self._source_meta())
        shop = next(s for s in snap["teams"][0]["shops"] if s["account"] == "xingshan_US")
        self.assertIsNone(shop["skus"][0]["sold"]["2026-04"])
        self.assertTrue(any(d["code"] == "duplicate_key" for d in snap["diagnostics"]))

    def test_month_axis_not_before_earliest(self):
        now = datetime(2026, 12, 1, tzinfo=UTC8)
        snap = bb.build_snapshot([self._amazon_row(month="2026-04")], [], now=now,
                                 source_meta=self._source_meta())
        self.assertEqual(snap["months"][0], "2026-04")
        self.assertNotIn("2026-01", snap["months"])

    def test_missing_month_null(self):
        now = datetime(2026, 6, 1, tzinfo=UTC8)
        snap = bb.build_snapshot([self._amazon_row(month="2026-04")], [], now=now,
                                 source_meta=self._source_meta())
        shop = next(s for s in snap["teams"][0]["shops"] if s["account"] == "xingshan_US")
        self.assertIsNone(shop["months"]["2026-05"])

    def test_invalid_month_appears_in_missing_months(self):
        rows = [
            self._amazon_row(sku="A", qty="1", month="2026-04"),
            self._amazon_row(sku="B", qty="-1", month="2026-05"),
        ]
        now = datetime(2026, 6, 1, tzinfo=UTC8)
        snap = bb.build_snapshot(rows, [], now=now, source_meta=self._source_meta())
        shop = next(s for s in snap["teams"][0]["shops"] if s["account"] == "xingshan_US")
        self.assertIsNone(shop["months"]["2026-05"])
        self.assertIn("2026-05", shop["missing_months"])

    def test_non_amazon_not_included(self):
        row = self._amazon_row()
        row["platform"] = "Wayfair"
        now = datetime(2026, 12, 1, tzinfo=UTC8)
        snap = bb.build_snapshot([row], [], now=now, source_meta=self._source_meta())
        self.assertEqual(snap["other_platforms"], [{"platform": "Wayfair", "rows": 1}])
        self.assertFalse(any("Wayfair" == shop["account"] for t in snap["teams"] for shop in t["shops"]))

    def test_unmapped_account_not_in_teams_and_diagnostic(self):
        row = self._amazon_row(account="unknown_account")
        now = datetime(2026, 12, 1, tzinfo=UTC8)
        snap = bb.build_snapshot([row], [], now=now, source_meta=self._source_meta())
        self.assertFalse(any(shop["account"] == "unknown_account" for t in snap["teams"] for shop in t["shops"]))
        self.assertEqual(snap["unmapped"][0]["account"], "unknown_account")
        self.assertTrue(any(d["code"] == "unmapped_account" for d in snap["diagnostics"]))

    def test_team_member_without_rows_still_listed(self):
        row = self._amazon_row(account="xingshan_US")
        now = datetime(2026, 12, 1, tzinfo=UTC8)
        snap = bb.build_snapshot([row], [], now=now, source_meta=self._source_meta())
        team1 = snap["teams"][0]
        self.assertIn("southwest_US", [s["account"] for s in team1["shops"]])
        self.assertTrue(any(d["code"] == "no_rows" for d in snap["diagnostics"]))

    def test_aftersale_confirmed_snapshot_connected_wayfair_unconnected(self):
        now = datetime(2026, 12, 1, tzinfo=UTC8)
        snap = bb.build_snapshot([], [], now=now, source_meta=self._source_meta())
        self.assertTrue(snap["aftersale"]["connected"])
        self.assertEqual(snap["aftersale"]["source"], "local_confirmed_snapshot")
        self.assertFalse(snap["aftersale"]["realtime"])
        self.assertFalse(snap["wayfair"]["connected"])

    def test_failure_falls_back_to_last_success_snapshot(self):
        f1 = make_valid_board_fake()
        cache = {}
        fixed_now = datetime(2026, 12, 15, 10, 0, tzinfo=UTC8)
        with patch.object(bb.feishu, "get_tenant_token", return_value="tok"):
            snap1 = bb.get_board("id", "secret", refresh=True, http=f1, cache=cache,
                                 now_fn=lambda: fixed_now)
        with patch.object(bb.feishu, "get_tenant_token", return_value="tok"):
            snap2 = bb.get_board("id", "secret", refresh=True,
                                 http=FailHttp(Exception("boom /tmp/secret_path.py")),
                                 cache=cache, now_fn=lambda: fixed_now)
        self.assertTrue(snap2["stale"])
        self.assertEqual(snap2["fetched_at"], snap1["fetched_at"])
        self.assertEqual(snap2["last_success_at"], snap1["last_success_at"])
        self.assertIn("boom", snap2["error"])

    def test_no_last_success_error_shape(self):
        cache = {}
        fixed_now = datetime(2026, 12, 15, 10, 0, tzinfo=UTC8)
        with patch.object(bb.feishu, "get_tenant_token", return_value="tok"):
            snap = bb.get_board("id", "secret", refresh=True,
                                http=FailHttp(Exception("bad")),
                                cache=cache, now_fn=lambda: fixed_now)
        self.assertFalse(snap["ok"])
        self.assertEqual(snap["last_success_at"], None)
        self.assertFalse(snap["stale"])

    def test_cache_30_seconds_and_refresh_forces_reread(self):
        f = make_valid_board_fake()
        cache = {}
        fixed_now = datetime(2026, 12, 15, 10, 0, tzinfo=UTC8)
        with patch.object(bb.feishu, "get_tenant_token", return_value="tok"), \
             patch.object(bb.time, "time", return_value=1000.0):
            bb.get_board("id", "secret", http=f, cache=cache, now_fn=lambda: fixed_now)
            self.assertEqual(len(f.calls), 9)
            bb.get_board("id", "secret", http=f, cache=cache, now_fn=lambda: fixed_now)
            self.assertEqual(len(f.calls), 9)
            add_valid_board_responses(f)
            bb.get_board("id", "secret", refresh=True, http=f, cache=cache,
                         now_fn=lambda: fixed_now)
            self.assertEqual(len(f.calls), 18)

    def test_sales_snapshot_cached_when_aftersale_source_fails(self):
        from server import aftersale_live
        f = make_valid_board_fake()
        cache = {}
        fixed_now = datetime(2026, 12, 15, 10, 0, tzinfo=UTC8)
        failed_aftersale = {
            "connected": False, "realtime": False,
            "summary": {"numerator": None, "denominator": None, "rate": None},
            "teams": [], "stores": [], "store_month": [], "sku_store_month": [],
            "reasons": {"connected": False, "items": None},
        }
        with patch.object(bb.feishu, "get_tenant_token", return_value="tok"), \
             patch.object(bb.time, "time", return_value=1000.0), \
             patch.object(aftersale_live, "load", return_value=failed_aftersale):
            first = bb.get_board("id", "secret", refresh=True, http=f,
                                 cache=cache, now_fn=lambda: fixed_now)
        self.assertTrue(first["ok"])
        self.assertFalse(first["aftersale"]["connected"])
        self.assertTrue(cache)
        with patch.object(bb.feishu, "get_tenant_token", return_value="tok"):
            fallback = bb.get_board("id", "secret", refresh=True,
                                    http=FailHttp(Exception("sales source failed")),
                                    cache=cache, now_fn=lambda: fixed_now)
        self.assertTrue(fallback["stale"])
        self.assertFalse(fallback["aftersale"]["connected"])
        self.assertEqual(fallback["fetched_at"], first["fetched_at"])

    def test_json_response_never_contains_secret_or_token(self):
        f = make_valid_board_fake()
        cache = {}
        fixed_now = datetime(2026, 12, 15, 10, 0, tzinfo=UTC8)
        with patch.object(bb.feishu, "get_tenant_token", return_value="secret-token"):
            snap = bb.get_board("id", "app-secret", http=f, cache=cache,
                                now_fn=lambda: fixed_now)
        text = json.dumps(snap, ensure_ascii=False)
        self.assertNotIn("app-secret", text)
        self.assertNotIn("secret-token", text)


class RouteTests(unittest.TestCase):
    def test_board_route_returns_board_json(self):
        expected = {"ok": True}
        with patch.object(api, "_feishu_creds", return_value=("id", "sec", "env")), \
             patch.object(api.bitable_board, "get_board", return_value=expected) as gb:
            resp = api.api_bitable_board()
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(json.loads(resp.body), expected)
            gb.assert_called_once_with("id", "sec", refresh=False)

    def test_board_route_missing_credentials_returns_ok_false(self):
        with patch.object(api, "_feishu_creds", return_value=("", "", "none")), \
             patch.object(api.bitable_board, "get_board") as gb:
            resp = api.api_bitable_board()
            self.assertEqual(resp.status_code, 200)
            body = json.loads(resp.body)
            self.assertFalse(body["ok"])
            self.assertIn("未配置飞书应用凭证", body["error"])
            gb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
