import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import bitable_board as bb

UTC8 = timezone(timedelta(hours=8), "Asia/Shanghai")

# 实际表 ID（来自规格）
TEAM1_TABLE_ID = "tblQkF7Vw69SVAx7"
TEAM2_TABLE_ID = "tblaL84UEi5AtsZn"
TEAM3_TABLE_ID = "tblsuoDngHQbirFL"
PAIR_TABLE_ID = "tbl6n6QymoJXDciu"
DEFAULT_TABLE_ID = "tblqPTlFEa3PahaN"  # 旧表 ID，当前 get_board 使用


def local_ms(y, m, d, hh=0, mm=0):
    dt = datetime(y, m, d, hh, mm, tzinfo=UTC8)
    return int(dt.timestamp() * 1000)


def month_ms(month_str):
    """月份字符串转该月1日0点的毫秒时间戳"""
    y, m = map(int, month_str.split("-"))
    return local_ms(y, m, 1)


def fields_response(field_items):
    return {
        "code": 0,
        "data": {
            "items": field_items,
            "has_more": False,
            "page_token": "",
        },
    }


def records_response(items, has_more=False, page_token=""):
    return {
        "code": 0,
        "data": {
            "items": items,
            "has_more": has_more,
            "page_token": page_token,
        },
    }


def rec(fields):
    return {"record_id": "r", "fields": fields}


# 公共字段（销量表）
AMAZON_FIELDS = [
    {"field_id": "f1", "field_name": "平台", "type": 1},
    {"field_id": "f2", "field_name": "店铺账号", "type": 1},
    {"field_id": "f3", "field_name": "店铺名称", "type": 1},
    {"field_id": "f4", "field_name": "仓库SKU", "type": 1},
    {"field_id": "f5", "field_name": "销量", "type": 2},
    {"field_id": "f6", "field_name": "月份（自然月）", "type": 5},
]

# 配对表字段
PAIR_FIELDS = [
    {"field_id": "p1", "field_name": "易仓SKU", "type": 1},
    {"field_id": "p2", "field_name": "SKU中文名称", "type": 1},
    {"field_id": "p3", "field_name": "WF Part Number （整个SKU）", "type": 1},
]


def amazon_row(account, sku, qty, month, platform="Amazon", display_name=None):
    """构造一条销量记录"""
    fields = {
        "平台": platform,
        "店铺账号": account,
        "仓库SKU": sku,
        "销量": qty,
        "月份（自然月）": month_ms(month),
    }
    if display_name:
        fields["店铺名称"] = display_name
    return rec(fields)


def pair_row(yc_sku, cn_name, wf_part):
    return rec({
        "易仓SKU": yc_sku,
        "SKU中文名称": cn_name,
        "WF Part Number （整个SKU）": wf_part,
    })


class FakeHttp:
    """根据 table_id 路由响应。每个表可配置 fields 响应列表和 records 响应列表。"""

    def __init__(self, table_responses):
        # table_responses: {table_id: {"fields": [...], "records": [...]}}
        self.table_responses = table_responses
        self.calls = []

    def _extract_table_id(self, url):
        # URL 类似 https://.../apps/{app_token}/tables/{table_id}/fields?...
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


class FailHttp:
    def __init__(self, exc):
        self.exc = exc

    def __call__(self, *args, **kwargs):
        raise self.exc


def make_default_valid_fake():
    """为旧 DEFAULT_TABLE_ID 提供空有效响应，使旧代码能够运行到返回快照"""
    return FakeHttp({
        DEFAULT_TABLE_ID: {
            "fields": [fields_response(AMAZON_FIELDS)],
            "records": [records_response([])],
        }
    })


class BitableBoardV2Tests(unittest.TestCase):
    def _build_multi_table_fake(self):
        """构造包含三 Team 表和配对表数据的 FakeHttp，同时为 DEFAULT_TABLE_ID 提供空响应避免旧代码崩溃"""
        # Team1 表：xingshan_US 有两条记录：一条配对 SKU 销量1，一条未配对 SKU 销量1（同单）
        team1_records = [
            amazon_row("xingshan_US", "PAIRED-SKU-1", "1", "2026-04", display_name="杭州行善_US"),
            amazon_row("xingshan_US", "UNPAIRED-PART", "1", "2026-04", display_name="杭州行善_US"),
        ]
        team2_records = [
            amazon_row("Wekas_US", "PAIRED-SKU-2", "2", "2026-04", display_name="Wekas_US"),
        ]
        team3_records = [
            amazon_row("maoyitong_US", "PAIRED-SKU-3", "3", "2026-04", display_name="maoyitong_US"),
        ]
        # 配对表：包含正常配对，以及重复的 SR-YJDXR240424-BE 两行
        pair_records = [
            pair_row("PAIRED-SKU-1", "产品一中文名", "WF-1"),
            pair_row("PAIRED-SKU-2", "产品二中文名", "WF-2"),
            pair_row("PAIRED-SKU-3", "产品三中文名", "WF-3"),
            pair_row("SR-YJDXR240424-BE", "240424电动摇转款灯芯绒米色（主推）", "WF-DUP-1"),
            pair_row("SR-YJDXR240424-BE", "240424电动摇转款灯芯绒米色新链接（主推）", "WF-DUP-2"),
        ]
        fake = FakeHttp({
            TEAM1_TABLE_ID: {
                "fields": [fields_response(AMAZON_FIELDS)],
                "records": [records_response(team1_records)],
            },
            TEAM2_TABLE_ID: {
                "fields": [fields_response(AMAZON_FIELDS)],
                "records": [records_response(team2_records)],
            },
            TEAM3_TABLE_ID: {
                "fields": [fields_response(AMAZON_FIELDS)],
                "records": [records_response(team3_records)],
            },
            PAIR_TABLE_ID: {
                "fields": [fields_response(PAIR_FIELDS)],
                "records": [records_response(pair_records)],
            },
            DEFAULT_TABLE_ID: {
                "fields": [fields_response(AMAZON_FIELDS)],
                "records": [records_response([])],
            },
        })
        return fake

    def test_three_team_tables_and_pairing_table_are_read(self):
        fake = self._build_multi_table_fake()
        cache = {}
        now = datetime(2026, 12, 1, tzinfo=UTC8)
        with patch.object(bb.feishu, "get_tenant_token", return_value="token"):
            snap = bb.get_board("id", "secret", refresh=True, http=fake, cache=cache,
                                now_fn=lambda: now)
        # 断言三个 team 均存在
        team_names = [t["name"] for t in snap.get("teams", [])]
        self.assertIn("Team 1", team_names)
        self.assertIn("Team 2", team_names)
        self.assertIn("Team 3", team_names)
        # 断言配对表的数据被使用：产品显示名应该是中文
        team1 = next(t for t in snap["teams"] if t["name"] == "Team 1")
        xingshan_shop = next(s for s in team1["shops"] if s["account"] == "xingshan_US")
        paired_sku = next(s for s in xingshan_shop["skus"] if s["sku"] == "PAIRED-SKU-1")
        self.assertEqual(paired_sku.get("display_name"), "产品一中文名")

    def test_matched_sku_total_is_1_not_2(self):
        fake = self._build_multi_table_fake()
        cache = {}
        now = datetime(2026, 12, 1, tzinfo=UTC8)
        with patch.object(bb.feishu, "get_tenant_token", return_value="token"):
            snap = bb.get_board("id", "secret", refresh=True, http=fake, cache=cache,
                                now_fn=lambda: now)
        team1 = next(t for t in snap["teams"] if t["name"] == "Team 1")
        xingshan_shop = next(s for s in team1["shops"] if s["account"] == "xingshan_US")
        # 2026-04 应只计配对 SKU 销量 1，不能把未配对部件销量 1 累加为 2
        self.assertEqual(xingshan_shop["months"]["2026-04"], 1)

    def test_duplicate_pair_takes_known_chinese_name_and_not_new_link(self):
        fake = self._build_multi_table_fake()
        # 在 Team1 表中添加一条 SR-YJDXR240424-BE 的记录，销量 5，月份 2026-05
        # 需要修改 fake 中 Team1 的 records，但由于 fake 已经构建，我们可以重新构建一个
        fake = self._build_multi_table_fake_with_dup_sku()
        cache = {}
        now = datetime(2026, 12, 1, tzinfo=UTC8)
        with patch.object(bb.feishu, "get_tenant_token", return_value="token"):
            snap = bb.get_board("id", "secret", refresh=True, http=fake, cache=cache,
                                now_fn=lambda: now)
        team1 = next(t for t in snap["teams"] if t["name"] == "Team 1")
        xingshan_shop = next(s for s in team1["shops"] if s["account"] == "xingshan_US")
        dup_sku_item = next(s for s in xingshan_shop["skus"] if s["sku"] == "SR-YJDXR240424-BE")
        self.assertIn("display_name", dup_sku_item)
        self.assertEqual(dup_sku_item["display_name"], "240424电动摇转款灯芯绒米色（主推）")
        self.assertNotIn("新链接", dup_sku_item["display_name"])

    def _build_multi_table_fake_with_dup_sku(self):
        fake = self._build_multi_table_fake()
        # 在 Team1 的 records 里添加一条 dup SKU 记录，需要重新构造 Team1 的 records 响应
        # 由于 FakeHttp 已经 built，简单方法：直接修改它的 table_responses 中的 records 列表
        team1_records = fake.table_responses[TEAM1_TABLE_ID]["records"][0]["data"]["items"]
        team1_records.append(amazon_row("xingshan_US", "SR-YJDXR240424-BE", "5", "2026-05"))
        # 重新打包响应
        fake.table_responses[TEAM1_TABLE_ID]["records"] = [records_response(team1_records)]
        return fake

    def test_unpaired_sku_diagnostic_and_count(self):
        fake = self._build_multi_table_fake()
        cache = {}
        now = datetime(2026, 12, 1, tzinfo=UTC8)
        with patch.object(bb.feishu, "get_tenant_token", return_value="token"):
            snap = bb.get_board("id", "secret", refresh=True, http=fake, cache=cache,
                                now_fn=lambda: now)
        # 应包含未配对 SKU 诊断
        unpaired_diag = [d for d in snap.get("diagnostics", []) if "未配对" in d.get("message", "")]
        self.assertTrue(unpaired_diag, "应存在未配对 SKU 诊断")
        # 未配对件数应为 1（UNPAIRED-PART 销量 1）
        # 我们可检查诊断消息中包含件数 1
        self.assertIn("1", unpaired_diag[0]["message"])

    def test_wrong_table_account_diagnostic_and_not_moved(self):
        # 构造 Team2 表中错误地出现了 xingshan_US 记录
        fake = self._build_multi_table_fake()
        # 在 Team2 表记录中添加一条 xingshan_US 记录
        team2_records = fake.table_responses[TEAM2_TABLE_ID]["records"][0]["data"]["items"]
        team2_records.append(amazon_row("xingshan_US", "PAIRED-SKU-1", "9", "2026-04"))
        fake.table_responses[TEAM2_TABLE_ID]["records"] = [records_response(team2_records)]
        cache = {}
        now = datetime(2026, 12, 1, tzinfo=UTC8)
        with patch.object(bb.feishu, "get_tenant_token", return_value="token"):
            snap = bb.get_board("id", "secret", refresh=True, http=fake, cache=cache,
                                now_fn=lambda: now)
        team2 = next(t for t in snap["teams"] if t["name"] == "Team 2")
        accounts_in_team2 = [s["account"] for s in team2["shops"]]
        self.assertNotIn("xingshan_US", accounts_in_team2)
        # 应有诊断
        diag_msgs = [d["message"] for d in snap.get("diagnostics", [])]
        self.assertTrue(any("错表" in m or "陌生" in m for m in diag_msgs))
        # 真实 Team1 的 xingshan_US 销量仍为 1，不应被错误记录加成 10
        team1 = next(t for t in snap["teams"] if t["name"] == "Team 1")
        xingshan_shop = next(s for s in team1["shops"] if s["account"] == "xingshan_US")
        self.assertEqual(xingshan_shop["months"].get("2026-04"), 1)

    def test_nine_confirmed_zero_skus_show_zero(self):
        # 构造一个包含确认零 SKU 的配对表，但销量表无该 SKU 记录
        zero_sku = "LAYJ240424-BE"
        pair_records = [
            pair_row(zero_sku, "确认零中文名", "WF-ZERO"),
            pair_row("PAIRED-SKU-1", "产品一中文名", "WF-1"),
        ]
        team1_records = []  # 无销量记录
        fake = FakeHttp({
            TEAM1_TABLE_ID: {
                "fields": [fields_response(AMAZON_FIELDS)],
                "records": [records_response(team1_records)],
            },
            TEAM2_TABLE_ID: {
                "fields": [fields_response(AMAZON_FIELDS)],
                "records": [records_response([])],
            },
            TEAM3_TABLE_ID: {
                "fields": [fields_response(AMAZON_FIELDS)],
                "records": [records_response([])],
            },
            PAIR_TABLE_ID: {
                "fields": [fields_response(PAIR_FIELDS)],
                "records": [records_response(pair_records)],
            },
            DEFAULT_TABLE_ID: {
                "fields": [fields_response(AMAZON_FIELDS)],
                "records": [records_response([])],
            },
        })
        cache = {}
        now = datetime(2026, 12, 15, tzinfo=UTC8)  # 当前月 2026-12，确认零在范围内，但12月本身应为None
        with patch.object(bb.feishu, "get_tenant_token", return_value="token"):
            snap = bb.get_board("id", "secret", refresh=True, http=fake, cache=cache,
                                now_fn=lambda: now)
        team1 = next(t for t in snap["teams"] if t["name"] == "Team 1")
        # 由于 Team1 表没有记录，但根据规格，xingshan_US 是 Team1 成员，应显示其店铺且 SKU 包含确认零 SKU
        # 但当前实现可能不会为无记录店铺创建 SKU 条目，因此会失败
        # 我们检查 xingshan_US 店铺的 skus 中是否含有该 SKU 且 sold 为 0
        xingshan_shop = next(s for s in team1["shops"] if s["account"] == "xingshan_US")
        zero_sku_item = next((s for s in xingshan_shop.get("skus", []) if s["sku"] == zero_sku), None)
        self.assertIsNotNone(zero_sku_item, "确认零 SKU 应出现在店铺 SKU 列表中")
        if zero_sku_item:
            # 月份 2026-04 到 2026-11 应显示 0（显式零）
            for mon in ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11"]:
                self.assertEqual(zero_sku_item["sold"].get(mon), 0, f"月份 {mon} 应为 0")
            # 12月（当前月）在轴内但确认零应仍为 None，因为真实销量优先，无真实销量则 None
            self.assertIsNone(zero_sku_item["sold"].get("2026-12"))

    def test_new_real_row_overrides_confirmed_zero(self):
        zero_sku = "LAYJ240424-BE"
        pair_records = [
            pair_row(zero_sku, "确认零中文名", "WF-ZERO"),
            pair_row("PAIRED-SKU-1", "产品一中文名", "WF-1"),
        ]
        team1_records = [
            amazon_row("xingshan_US", zero_sku, "7", "2026-05"),  # 新真实销量 7
        ]
        fake = FakeHttp({
            TEAM1_TABLE_ID: {
                "fields": [fields_response(AMAZON_FIELDS)],
                "records": [records_response(team1_records)],
            },
            TEAM2_TABLE_ID: {
                "fields": [fields_response(AMAZON_FIELDS)],
                "records": [records_response([])],
            },
            TEAM3_TABLE_ID: {
                "fields": [fields_response(AMAZON_FIELDS)],
                "records": [records_response([])],
            },
            PAIR_TABLE_ID: {
                "fields": [fields_response(PAIR_FIELDS)],
                "records": [records_response(pair_records)],
            },
            DEFAULT_TABLE_ID: {
                "fields": [fields_response(AMAZON_FIELDS)],
                "records": [records_response([])],
            },
        })
        cache = {}
        now = datetime(2026, 12, 15, tzinfo=UTC8)
        with patch.object(bb.feishu, "get_tenant_token", return_value="token"):
            snap = bb.get_board("id", "secret", refresh=True, http=fake, cache=cache,
                                now_fn=lambda: now)
        team1 = next(t for t in snap["teams"] if t["name"] == "Team 1")
        xingshan_shop = next(s for s in team1["shops"] if s["account"] == "xingshan_US")
        zero_sku_item = next(s for s in xingshan_shop["skus"] if s["sku"] == zero_sku)
        # 2026-05 有真实记录，应显示 7，而不是人工0
        self.assertEqual(zero_sku_item["sold"]["2026-05"], 7)
        # 其他月份如 2026-04 应仍为 0
        self.assertEqual(zero_sku_item["sold"]["2026-04"], 0)
        # 2026-12 应 None（真实销量优先，但无真实销量）
        self.assertIsNone(zero_sku_item["sold"].get("2026-12"))

    def test_pairing_table_read_failure_blocks_new_snapshot(self):
        # 配对表读取失败（抛出异常），应导致 get_board 返回错误或回退
        fake = self._build_multi_table_fake()
        # 设置配对表响应为异常
        fake.table_responses[PAIR_TABLE_ID]["fields"] = [RuntimeError("pair table boom")]
        fake.table_responses[PAIR_TABLE_ID]["records"] = [RuntimeError("pair table boom")]
        cache = {}
        now = datetime(2026, 12, 1, tzinfo=UTC8)
        with patch.object(bb.feishu, "get_tenant_token", return_value="token"):
            snap = bb.get_board("id", "secret", refresh=True, http=fake, cache=cache,
                                now_fn=lambda: now)
        # 当前实现不会读取配对表，所以不会失败，但未来实现应该失败或回退
        # 我们断言快照不包含可信的新分母，即 ok 为 False 或 stale 为 True
        # 当前实现会成功返回（因为不读配对表），所以断言失败
        self.assertTrue(snap.get("ok") is False or snap.get("stale") is True,
                        "配对表失败应阻断新快照或回退旧快照")


if __name__ == "__main__":
    unittest.main()
