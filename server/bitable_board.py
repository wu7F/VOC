# -*- coding: utf-8 -*-
"""只读多维表格看板：Amazon 销量矩阵（v2 四表口径）。

数据来源：
  - AMAZON TEAM 1 (tblQkF7Vw69SVAx7)
  - AMAZON TEAM 2 (tblaL84UEi5AtsZn)
  - AMAZON TEAM 3 (tblsuoDngHQbirFL)
  - SKU配对表 (tbl6n6QymoJXDciu)

销售记录只读，不写表，不触发同步，不推送。
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import feishu
from . import shop_filter
from . import component_sales

UTC8 = timezone(timedelta(hours=8), "Asia/Shanghai")

DEFAULT_APP_TOKEN = "EmsLbLeKSasY4ZsXhYocpwaInKj"
PAIR_TABLE_ID = "tbl6n6QymoJXDciu"
DEFAULT_TABLE_NAME = "AMAZON TEAM 1 + AMAZON TEAM 2 + AMAZON TEAM 3 + SKU配对表"  # 三Team+配对口径

# 三张 Team 表定义，账号名单为权威归属
TEAM_TABLES = {
    "Team 1": {
        "table_id": "tblQkF7Vw69SVAx7",
        "accounts": ["KornerHK_US", "xingshan_US", "southwest_US"],
    },
    "Team 2": {
        "table_id": "tblaL84UEi5AtsZn",
        "accounts": ["Wekas_US", "kaifengzhejie_US"],
    },
    "Team 3": {
        "table_id": "tblsuoDngHQbirFL",
        "accounts": ["maoyitong_US", "RUIBAO_US_US", "AmazonMJSKY_US_US"],
    },
}

REQUIRED_FIELDS = {
    "平台": 1,        # 文本
    "店铺账号": 1,    # 文本
    "仓库SKU": 1,     # 文本
    "销量": 2,        # 数字
    "月份（自然月）": 5,  # 日期
}

REQUIRED_PAIR_FIELDS = {
    "易仓SKU": 1,
    "SKU中文名称": 1,
    "WF Part Number （整个SKU）": 1,
}

# Ethan 确认的 9 个零销量 SKU（2026-04~11 缺失时显式 0）
CONFIRMED_ZERO_SKUS = {
    "LAYJ240424-BE",
    "LAYJMZP0430DG-2L",
    "LAYJMZP0458DG-KBT",
    "LAYJMZPG02DG-2L",
    "LAYJNP0454-DBR-2CJ",
    "LAYJNP0454-KQ-2CJ",
    "SR-YJPU240420BR",
    "YAR-PWSR240442-BE",
    "YJDXRPW0424BE-2LT",
}

# 重复 SKU 特例：仅当该 SKU 存在时应用主推行规则
DUP_SKU_SPECIAL = "SR-YJDXR240424-BE"
DUP_SKU_KEEP_NAME = "240424电动摇转款灯芯绒米色（主推）"
DUP_SKU_IGNORE_NAME = "240424电动摇转款灯芯绒米色新链接（主推）"

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 30.0

CACHE_VERSION = "v4_component_min"

# 本地确认快照（只读，非实时）。缺失文件时售后保持未接入。
AFTERSALE_SNAPSHOT_PATH = Path(__file__).resolve().parent / "aftersale_snapshot.json"
AFTERSALE_SNAPSHOT_VERSION = "confirmed_2026-11-20"


class BoardError(Exception):
    """面向用户的中文错误。"""


# ---------------------------------------------------------------- 基础工具

def _utc8(dt: datetime) -> datetime:
    """把时间归一化到 Asia/Shanghai UTC+8。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC8)
    return dt.astimezone(UTC8)


def _month_str(dt: datetime) -> str:
    return "%04d-%02d" % (dt.year, dt.month)


def _add_months(month: str, delta: int) -> str:
    year, mon = (int(x) for x in month.split("-"))
    idx = year * 12 + (mon - 1) + delta
    return "%04d-%02d" % (idx // 12, idx % 12 + 1)


def _months_between(start: str, end: str) -> list[str]:
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur = _add_months(cur, 1)
    return out


def _text_val(v) -> str:
    """文本字段只接受简单标量，避免把列表/对象误当成字符串。"""
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v).strip()
    return ""


def _parse_qty(v):
    """销量规范化：int/float(整数值)/纯数字字符串；0 保留；非法返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        if v < 0:
            return None
        return v
    if isinstance(v, float):
        if not math.isfinite(v) or v < 0 or not v.is_integer():
            return None
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            iv = int(s)
            if iv < 0:
                return None
            return iv
        except ValueError:
            pass
        try:
            f = float(s)
            if not math.isfinite(f) or f < 0 or not f.is_integer():
                return None
            return int(f)
        except ValueError:
            return None
    return None


def _parse_month(v):
    """日期字段是毫秒时间戳，按 UTC+8 转 YYYY-MM。"""
    if isinstance(v, bool):
        return None
    try:
        ms = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ms):
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, UTC8)
    except (OverflowError, OSError, ValueError):
        return None
    return _month_str(dt)


def _public_error(exc, secrets=()) -> str:
    """去掉绝对路径、抹掉密钥/令牌片段并截断到 300 字符。"""
    msg = str(exc)
    for s in secrets:
        if s:
            msg = msg.replace(s, "[已隐藏]")

    def _basename(p: str) -> str:
        return p.replace("\\", "/").rstrip("/").split("/")[-1]

    msg = re.sub(
        r"(?:[A-Za-z]:[\\/]|/)[^\s\"']+",
        lambda m: _basename(m.group(0)),
        msg,
    )
    return msg[:300]


# ---------------------------------------------------------------- 售后快照（只读）

_EXPECTED_TEAMS = ("Team 1", "Team 2", "Team 3")
_EXPECTED_TEAM_SET = frozenset(_EXPECTED_TEAMS)
_EXPECTED_STORE_TEAM = {
    "KornerHK_US": "Team 1",
    "southwest_US": "Team 1",
    "xingshan_US": "Team 1",
    "Wekas_US": "Team 2",
    "kaifengzhejie_US": "Team 2",
    "AmazonMJSKY_US_US": "Team 3",
    "RUIBAO_US_US": "Team 3",
    "maoyitong_US": "Team 3",
}
_EXPECTED_MONTHS = tuple(
    "2026-%02d" % m for m in range(4, 12)
)
_EXPECTED_MONTH_SET = frozenset(_EXPECTED_MONTHS)
_EXPECTED_COVERAGE_START = "2026-04"
_EXPECTED_COVERAGE_END = "2026-11"
_EXPECTED_STORE_MONTH_COUNT = 50
_EXPECTED_SKU_STORE_MONTH_COUNT = 497


def _load_aftersale_snapshot(path=None):
    """安全加载本地确认快照。缺失返回 None；读取/解析失败返回 sentinel dict。"""
    p = Path(path) if path is not None else AFTERSALE_SNAPSHOT_PATH
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    except OSError:
        return {"__snapshot_error__": "read_failed"}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {"__snapshot_error__": "parse_failed"}
    if not isinstance(data, dict):
        return {"__snapshot_error__": "shape_invalid"}
    return data


def _as_int(v):
    """快照数值规范化：仅接受有限整数/整值浮点/纯数字字符串；否则 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if not math.isfinite(v) or not v.is_integer():
            return None
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None
    return None


def _rate(numerator, denominator):
    """仅当分子分母均有效且分母>0 时计算百分比；否则 None。"""
    n = _as_int(numerator)
    d = _as_int(denominator)
    if n is None or d is None or d <= 0:
        return None
    return round(n * 100.0 / d, 4)


def _rate_entry(numerator, denominator):
    """构造 {numerator, denominator, rate} 三元组，缺失保持 None。"""
    n = _as_int(numerator)
    d = _as_int(denominator)
    return {
        "numerator": n,
        "denominator": d,
        "rate": _rate(n, d),
    }


def exclude_t01_rows(numerator_rows, denominator_rows):
    """纯本地合成辅助：从分子/分母行列表中排除 is_t01 为真的行，保持顺序。"""
    num_out = [r for r in numerator_rows if not r.get("is_t01")]
    den_out = [r for r in denominator_rows if not r.get("is_t01")]
    return num_out, den_out


_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _valid_month(v):
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not _MONTH_RE.match(s):
        return None
    return s


def _text_val(v):
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return None


def _disconnected(message=None, diagnostics=None):
    return {
        "connected": False,
        "realtime": False,
        "message": message or "售后分子和原因尚未接入多维表格,售后率显示“—”,不按 0% 计算",
        "reasons": {"connected": False, "items": None},
        "total": None,
        "teams": [],
        "stores": [],
        "store_month": [],
        "sku_store_month": [],
        "t01_excluded": {"team1": None, "team2": None, "team3": None, "total": None},
        "coverage": {
            "start_month": None,
            "end_month": None,
            "team_count": 0,
            "store_count": 0,
            "store_month_count": 0,
            "sku_store_month_count": 0,
        },
        "diagnostics": list(diagnostics) if diagnostics else [],
    }


def _build_aftersale_summary(snapshot):
    """把确认快照整理为公开只读结构。缺失/非法返回未接入占位。"""
    if snapshot is None:
        return _disconnected()

    diagnostics = []

    def _fail(msg):
        diagnostics.append(msg)
        out = _disconnected()
        out["diagnostics"] = diagnostics
        return out

    if not isinstance(snapshot, dict):
        return _fail("snapshot not an object")

    if "__snapshot_error__" in snapshot:
        return _fail("snapshot unavailable")

    source = snapshot.get("source")
    if not isinstance(source, dict):
        return _fail("source missing or invalid")

    coverage_start = _valid_month(source.get("coverage_start"))
    coverage_end = _valid_month(source.get("coverage_end"))
    if coverage_start != _EXPECTED_COVERAGE_START:
        return _fail("coverage_start mismatch")
    if coverage_end != _EXPECTED_COVERAGE_END:
        return _fail("coverage_end mismatch")

    team_totals_raw = snapshot.get("team_totals")
    stores_raw = snapshot.get("stores")
    store_month_raw = snapshot.get("store_month")
    sku_store_month_raw = snapshot.get("sku_store_month")
    for name, val in (
        ("team_totals", team_totals_raw),
        ("stores", stores_raw),
        ("store_month", store_month_raw),
        ("sku_store_month", sku_store_month_raw),
    ):
        if not isinstance(val, list):
            return _fail(f"{name} missing or not a list")

    # ---- team_totals ----
    teams_out = []
    t01_by_team = {}
    seen_teams = set()
    for t in team_totals_raw:
        if not isinstance(t, dict):
            return _fail("team_totals row not a dict")
        name = _text_val(t.get("team"))
        if not name:
            return _fail("team_totals row missing team")
        if name not in _EXPECTED_TEAM_SET:
            return _fail(f"unexpected team: {name}")
        if name in seen_teams:
            return _fail(f"duplicate team: {name}")
        seen_teams.add(name)
        n = _as_int(t.get("numerator"))
        d = _as_int(t.get("denominator"))
        if n is None or d is None:
            return _fail(f"team_totals invalid counts for {name}")
        if n < 0 or d < 0:
            return _fail(f"team_totals negative counts for {name}")
        t01 = _as_int(t.get("excluded_t01_sales"))
        if t01 is None or t01 < 0:
            return _fail(f"team_totals invalid excluded_t01_sales for {name}")
        entry = _rate_entry(n, d)
        entry["name"] = name
        teams_out.append(entry)
        t01_by_team[name] = t01

    if seen_teams != _EXPECTED_TEAM_SET:
        return _fail("team set mismatch")

    # ---- stores ----
    stores_out = []
    seen_stores = set()
    store_team = {}
    for s in stores_raw:
        if not isinstance(s, dict):
            return _fail("stores row not a dict")
        account = _text_val(s.get("store"))
        if not account:
            return _fail("stores row missing store")
        if account in seen_stores:
            return _fail(f"duplicate store: {account}")
        seen_stores.add(account)
        team = _text_val(s.get("team"))
        expected_team = _EXPECTED_STORE_TEAM.get(account)
        if expected_team is None:
            return _fail(f"unexpected store: {account}")
        if team != expected_team:
            return _fail(f"stores row wrong team for {account}")
        n = _as_int(s.get("numerator"))
        d = _as_int(s.get("denominator"))
        if n is None or d is None or n < 0 or d < 0:
            return _fail(f"stores invalid counts for {account}")
        entry = _rate_entry(n, d)
        entry["account"] = account
        entry["team"] = team
        stores_out.append(entry)
        store_team[account] = team

    if seen_stores != set(_EXPECTED_STORE_TEAM.keys()):
        return _fail("store set mismatch")

    # ---- store_month ----
    store_month_out = []
    seen_sm = set()
    sm_sum = {}
    for r in store_month_raw:
        if not isinstance(r, dict):
            return _fail("store_month row not a dict")
        account = _text_val(r.get("store"))
        month = _valid_month(r.get("month"))
        if not account or not month:
            return _fail("store_month row missing store/month")
        if account not in store_team:
            return _fail(f"store_month unknown store: {account}")
        team = _text_val(r.get("team"))
        if team != store_team[account]:
            return _fail(f"store_month team mismatch for {account}")
        key = (account, month)
        if key in seen_sm:
            return _fail(f"duplicate store_month: {account}/{month}")
        seen_sm.add(key)
        if month not in _EXPECTED_MONTH_SET:
            return _fail(f"store_month month out of coverage: {month}")
        n = _as_int(r.get("numerator"))
        d = _as_int(r.get("denominator"))
        if n is None or d is None or n < 0 or d < 0:
            return _fail(f"store_month invalid counts for {account}/{month}")
        entry = _rate_entry(n, d)
        entry["account"] = account
        entry["month"] = month
        entry["team"] = team
        store_month_out.append(entry)
        sm_sum[key] = (n, d)

    # ---- sku_store_month ----
    sku_store_month_out = []
    seen_ssm = set()
    ssm_sum = {}
    for r in sku_store_month_raw:
        if not isinstance(r, dict):
            return _fail("sku_store_month row not a dict")
        account = _text_val(r.get("store"))
        month = _valid_month(r.get("month"))
        sku = _text_val(r.get("sku"))
        if not account or not month or not sku:
            return _fail("sku_store_month row missing store/month/sku")
        if account not in store_team:
            return _fail(f"sku_store_month unknown store: {account}")
        team = _text_val(r.get("team"))
        if team != store_team[account]:
            return _fail(f"sku_store_month team mismatch for {account}")
        key = (account, month, sku)
        if key in seen_ssm:
            return _fail(f"duplicate sku_store_month: {account}/{month}/{sku}")
        seen_ssm.add(key)
        if month not in _EXPECTED_MONTH_SET:
            return _fail(f"sku_store_month month out of coverage: {month}")
        n = _as_int(r.get("numerator"))
        d = _as_int(r.get("denominator"))
        if n is None or d is None or n < 0 or d < 0:
            return _fail(f"sku_store_month invalid counts for {account}/{month}/{sku}")
        entry = _rate_entry(n, d)
        entry["account"] = account
        entry["month"] = month
        entry["sku"] = sku
        entry["team"] = team
        sku_store_month_out.append(entry)
        prev = ssm_sum.get((account, month), (0, 0))
        ssm_sum[(account, month)] = (prev[0] + n, prev[1] + d)

    # ---- coverage: months must be exactly expected set across both tables ----
    sm_months = {k[1] for k in seen_sm}
    ssm_months = {k[1] for k in seen_ssm}
    if sm_months != _EXPECTED_MONTH_SET:
        return _fail("store_month month coverage mismatch")
    if ssm_months != _EXPECTED_MONTH_SET:
        return _fail("sku_store_month month coverage mismatch")

    if len(store_month_out) != _EXPECTED_STORE_MONTH_COUNT:
        return _fail("store_month row count mismatch")
    if len(sku_store_month_out) != _EXPECTED_SKU_STORE_MONTH_COUNT:
        return _fail("sku_store_month row count mismatch")

    # ---- every SKU-store-month key must have matching store-month key ----
    for (account, month, _sku) in seen_ssm:
        if (account, month) not in seen_sm:
            return _fail(f"sku_store_month missing store_month key: {account}/{month}")

    # ---- every store-month key must have SKU rows ----
    ssm_keys = {(k[0], k[1]) for k in seen_ssm}
    for key in seen_sm:
        if key not in ssm_keys:
            return _fail(f"store_month missing sku rows: {key[0]}/{key[1]}")

    # ---- dimension sums must match ----
    team_sum = {}
    for t in teams_out:
        team_sum[t["name"]] = (t["numerator"], t["denominator"])
    store_sum = {}
    for s in stores_out:
        prev = store_sum.get(s["team"], (0, 0))
        store_sum[s["team"]] = (prev[0] + s["numerator"], prev[1] + s["denominator"])
    for team, (n, d) in team_sum.items():
        if store_sum.get(team) != (n, d):
            return _fail(f"stores sum mismatch for team {team}")

    # store_month sums must match stores
    sm_by_store = {}
    for (account, _month), (n, d) in sm_sum.items():
        prev = sm_by_store.get(account, (0, 0))
        sm_by_store[account] = (prev[0] + n, prev[1] + d)
    for s in stores_out:
        if sm_by_store.get(s["account"]) != (s["numerator"], s["denominator"]):
            return _fail(f"store_month sum mismatch for store {s['account']}")

    # sku_store_month sums must match store_month
    for key, (n, d) in ssm_sum.items():
        if sm_sum.get(key) != (n, d):
            return _fail(f"sku_store_month sum mismatch for {key[0]}/{key[1]}")

    # ---- totals ----
    total_n = sum(t["numerator"] for t in teams_out)
    total_d = sum(t["denominator"] for t in teams_out)
    total = _rate_entry(total_n, total_d)

    t01_team1 = t01_by_team.get("Team 1")
    t01_team2 = t01_by_team.get("Team 2")
    t01_team3 = t01_by_team.get("Team 3")
    t01_total = None
    if all(v is not None for v in (t01_team1, t01_team2, t01_team3)):
        t01_total = t01_team1 + t01_team2 + t01_team3

    return {
        "connected": True,
        "realtime": False,
        "message": "售后数据来自本地确认快照（非实时），分母可能与销量看板不同。",
        "source": "local_confirmed_snapshot",
        "source_label": _text_val(source.get("label")),
        "source_as_of": _text_val(source.get("snapshot_date")),
        "fetched_at": _text_val(source.get("snapshot_date")),
        "coverage": {
            "start_month": coverage_start,
            "end_month": coverage_end,
            "team_count": len(teams_out),
            "store_count": len(stores_out),
            "store_month_count": len(store_month_out),
            "sku_store_month_count": len(sku_store_month_out),
        },
        "total": total,
        "teams": teams_out,
        "stores": stores_out,
        "store_month": store_month_out,
        "sku_store_month": sku_store_month_out,
        "t01_excluded": {
            "team1": t01_team1,
            "team2": t01_team2,
            "team3": t01_team3,
            "total": t01_total,
        },
        "reasons": {"connected": False, "items": None},
    }

# ---------------------------------------------------------------- 飞书取数

def fetch_fields(token: str, app_token: str, table_id: str, http=None,
                 required_fields=None) -> dict:
    """分页读完整字段清单，校验必需字段名与类型后返回字段名->字段对象。"""
    http = http or feishu._http_json
    required = required_fields if required_fields is not None else REQUIRED_FIELDS
    base = ("%s/bitable/v1/apps/%s/tables/%s/fields"
            % (feishu._OPEN, urllib.parse.quote(app_token, safe=""),
               urllib.parse.quote(table_id, safe="")))
    items: list[dict] = []
    page_token = ""
    seen_tokens: set[str] = set()

    for _ in range(100):
        url = base + "?page_size=100"
        if page_token:
            url += "&page_token=" + urllib.parse.quote(page_token, safe="")
        j = http(url, headers={"Authorization": "Bearer " + token})
        data = j.get("data") or {}
        items.extend(data.get("items") or [])
        if data.get("has_more") is False:
            break
        next_token = data.get("page_token") or ""
        if not next_token:
            raise BoardError("字段分页未结束但缺少 page_token。")
        if next_token in seen_tokens:
            raise BoardError("字段分页 page_token 重复，终止读取避免死循环。")
        seen_tokens.add(next_token)
        page_token = next_token
    else:
        raise BoardError("字段分页超过安全上限。")

    field_map = {f.get("field_name"): f for f in items if f.get("field_name")}
    missing = [name for name in required if name not in field_map]
    wrong: list[str] = []
    for name, expected_type in required.items():
        if name in field_map:
            actual_type = field_map[name].get("type")
            if actual_type != expected_type:
                wrong.append("%s(实际类型 %s, 期望 %s)" % (name, actual_type, expected_type))
    if missing or wrong:
        parts = []
        if missing:
            parts.append("缺少字段：" + ", ".join(missing))
        if wrong:
            parts.append("字段类型漂移：" + ", ".join(wrong))
        raise BoardError("字段漂移诊断：" + "；".join(parts))
    return field_map


def fetch_records(token: str, app_token: str, table_id: str, http=None):
    """分页读完整记录。返回 (records, page_count)。"""
    http = http or feishu._http_json
    base = ("%s/bitable/v1/apps/%s/tables/%s/records"
            % (feishu._OPEN, urllib.parse.quote(app_token, safe=""),
               urllib.parse.quote(table_id, safe="")))
    items: list[dict] = []
    page_token = ""
    seen_tokens: set[str] = set()

    for page_count in range(1, 401):
        url = base + "?page_size=500"
        if page_token:
            url += "&page_token=" + urllib.parse.quote(page_token, safe="")
        j = http(url, headers={"Authorization": "Bearer " + token})
        data = j.get("data") or {}
        items.extend(data.get("items") or [])
        if data.get("has_more") is False:
            return items, page_count
        next_token = data.get("page_token") or ""
        if not next_token:
            raise BoardError("记录分页未结束但缺少 page_token。")
        if next_token in seen_tokens:
            raise BoardError("记录分页 page_token 重复，终止读取避免死循环。")
        seen_tokens.add(next_token)
        page_token = next_token
    raise BoardError("记录超过安全上限（400 页 × 500 条），拒绝继续避免静默截断。")


# ---------------------------------------------------------------- 配对表

def fetch_pairing(token: str, app_token: str, http=None) -> dict:
    """读取并校验 SKU 配对表，返回 {sku: {sku, display_name, wf_part_number}}。

    若 SR-YJDXR240424-BE 不存在则跳过该特例，不阻断其他产品。
    若存在则必须精确匹配主推行且忽略新链接行，冲突则抛出异常。
    """
    http = http or feishu._http_json
    fetch_fields(token, app_token, PAIR_TABLE_ID, http=http,
                 required_fields=REQUIRED_PAIR_FIELDS)
    records, page_count = fetch_records(token, app_token, PAIR_TABLE_ID, http=http)

    raw_items: list[dict] = []
    for rec in records:
        fields = rec.get("fields") or {}
        sku = _text_val(fields.get("易仓SKU"))
        cn = _text_val(fields.get("SKU中文名称"))
        wf = _text_val(fields.get("WF Part Number （整个SKU）"))
        if not sku or not cn or not wf:
            raise BoardError("配对表存在空SKU/缺失必需字段，不能生成可信新分母。")
        raw_items.append({"sku": sku, "display_name": cn, "wf_part_number": wf})

    pair_map: dict[str, dict] = {}
    dup_special_items: list[dict] = []

    for item in raw_items:
        sku = item["sku"]
        if sku == DUP_SKU_SPECIAL:
            dup_special_items.append(item)
            continue
        if sku in pair_map:
            raise BoardError("配对表存在重复易仓SKU：%s" % sku)
        pair_map[sku] = item

    # 特例处理
    if dup_special_items:
        # 忽略所有包含“新链接”的行
        non_ignored = [
            item for item in dup_special_items
            if item["display_name"] != DUP_SKU_IGNORE_NAME
        ]
        # 主推行必须唯一
        keep_items = [
            item for item in non_ignored
            if item["display_name"] == DUP_SKU_KEEP_NAME
        ]
        if len(keep_items) != 1:
            # 0 个主推行或 >1 个主推行均视为冲突
            raise BoardError(
                "配对表 %s 主推行规则冲突：期望唯一主推行“%s”，实际 %d 行。"
                % (DUP_SKU_SPECIAL, DUP_SKU_KEEP_NAME, len(keep_items))
            )
        if len(non_ignored) != 1:
            # 存在除主推行以外的未知配对
            raise BoardError(
                "配对表 %s 存在未知配对行，请人工确认。" % DUP_SKU_SPECIAL
            )
        pair_map[DUP_SKU_SPECIAL] = keep_items[0]

    return pair_map, len(records), page_count


# ---------------------------------------------------------------- 规范化（保留旧公开签名）

def normalize(records, *, today=None):
    """逐行规范化。返回 (rows, diagnostics)。"""
    rows: list[dict] = []
    diagnostics: list[dict] = []

    for idx, rec in enumerate(records, 1):
        fields = rec.get("fields") or {}
        platform = _text_val(fields.get("平台"))
        account = _text_val(fields.get("店铺账号"))
        sku = _text_val(fields.get("仓库SKU"))
        display_name = _text_val(fields.get("店铺名称"))
        month = _parse_month(fields.get("月份（自然月）"))

        if not platform or not account or not sku or not month:
            diagnostics.append({
                "level": "warning",
                "code": "invalid_row",
                "message": "第 %d 行平台/店铺账号/SKU/月份缺失或非法，未参与统计。" % idx,
            })
            continue

        qty = _parse_qty(fields.get("销量"))
        invalid = qty is None
        if invalid:
            diagnostics.append({
                "level": "warning",
                "code": "invalid_qty",
                "message": "account=%s month=%s SKU=%s 销量非法，该格置空。"
                           % (account, month, sku),
            })

        rows.append({
            "platform": platform,
            "account": account,
            "sku": sku,
            "month": month,
            "qty": qty,
            "invalid": invalid,
            "display_name": display_name,
            "_source_team": rec.get("_source_team"),
        })

    return rows, diagnostics


# ---------------------------------------------------------------- 聚合函数（扩展签名，生产路径真实调用）

def _build_shop(account: str, account_rows: list[dict], months: list[str],
                diagnostics: list[dict], pair_map: dict | None = None,
                confirmed_zero_skus: set[str] | None = None) -> dict:
    """构建单个店铺的快照。

    account_rows 已经保证属于此账号且仅包含配对 SKU（若 pair_map 提供）。
    在构造 sku_map 前会调用 component_sales.merge_component_rows 将已准入的组件别名
    规范化为 canonical 产品行；确认零 SKU 将按规则补缺，但不计入店铺级月汇总真实已知量。
    """
    account_rows = component_sales.merge_component_rows(account_rows, pair_map, diagnostics)
    # 取 display_name
    display_name = ""
    for r in account_rows:
        dn = r.get("display_name") or ""
        if dn:
            display_name = dn
            break
    if not display_name:
        display_name = account

    # 按 SKU 聚合真实记录
    sku_map: dict[str, dict[str, list[dict]]] = {}
    for r in account_rows:
        sku = r["sku"]
        mon = r["month"]
        sku_map.setdefault(sku, {}).setdefault(mon, []).append(r)

    real_months_bad: dict[str, bool] = {}

    # 找出所有应出现在 skus 列表中的 SKU：
    # 1) 有真实记录的 SKU
    # 2) 确认零 SKU（仅当 pair_map 提供且该 SKU 在 pair_map 中存在）
    all_skus = set(sku_map.keys())
    if confirmed_zero_skus and pair_map is not None:
        for sku in confirmed_zero_skus:
            if sku in pair_map:
                all_skus.add(sku)

    sku_items: list[dict] = []
    missing_months: list[str] = []

    for sku in sorted(all_skus):
        sold: dict[str, int | None] = {}
        is_confirmed_zero = confirmed_zero_skus is not None and sku in confirmed_zero_skus

        for mon in months:
            entries = sku_map.get(sku, {}).get(mon, [])
            if len(entries) > 1:
                # 重复键：该月不完整
                sold[mon] = None
                diagnostics.append({
                    "level": "error",
                    "code": "duplicate_key",
                    "message": "account=%s month=%s SKU=%s 重复 %d 行，该月置空。"
                               % (account, mon, sku, len(entries)),
                })
                # 店铺月汇总也标记为不完整
                real_months_bad[mon] = True
                if mon not in missing_months:
                    missing_months.append(mon)
            elif len(entries) == 1 and entries[0].get("invalid"):
                sold[mon] = None
                real_months_bad[mon] = True
                if mon not in missing_months:
                    missing_months.append(mon)
            elif len(entries) == 1:
                sold[mon] = entries[0].get("qty")
            else:
                # 无记录
                if is_confirmed_zero and pair_map is not None and "2026-04" <= mon <= "2026-11":
                    sold[mon] = 0
                else:
                    sold[mon] = None

        # 构建 sku 条目
        display = ""
        wf = ""
        if pair_map and sku in pair_map:
            display = pair_map[sku].get("display_name", "")
            wf = pair_map[sku].get("wf_part_number", "")
        sku_items.append({
            "sku": sku,
            "display_name": display,
            "wf_part_number": wf,
            "sold": sold,
        })

    # 计算店铺真实月汇总
    months_vals: dict[str, int | None] = {}
    for mon in months:
        if real_months_bad.get(mon):
            months_vals[mon] = None
            if mon not in missing_months:
                missing_months.append(mon)
            continue
        # 仅累加真实记录（任何有真实记录的 SKU 在该月的有效值）
        total = None
        any_known = False
        for sku, month_entries in sku_map.items():
            entries = month_entries.get(mon, [])
            if len(entries) == 1 and not entries[0].get("invalid"):
                val = entries[0].get("qty")
                if val is not None:
                    any_known = True
                    total = (total or 0) + val
        months_vals[mon] = total if any_known else None

    units_known = sum(v for v in months_vals.values() if v is not None)
    return {
        "account": account,
        "display_name": display_name,
        "months": months_vals,
        "units_known": units_known,
        "missing_months": missing_months,
        "skus": sku_items,
    }




def build_snapshot(rows, diagnostics, *, now, source_meta,
                   teams: dict | None = None,
                   pair_map: dict | None = None,
                   confirmed_zero_skus: set[str] | None = None,
                   aftersale_summary: dict | None = None) -> dict:
    """按 Team/店铺/SKU/月份构建只读快照。

    teams 可选，默认 TEAM_TABLES（真实8账号）。
    pair_map 可选，若提供则只统计配对 SKU 并添加 wf/display_name；
    未提供则保留旧行为（不过滤 SKU）。
    confirmed_zero_skus 可选，提供则进行确认零补缺。
    """
    now = _utc8(now)
    current_month = _month_str(now)
    fetched_at = now.isoformat()

    diag = list(diagnostics or [])
    teams_def = teams if teams is not None else TEAM_TABLES

    # 固定月份轴：2026-04 至当前月
    months = _months_between("2026-04", current_month)

    # 预处理 rows：过滤平台并检查月份范围
    amazon_rows: list[dict] = []
    other_counts: dict[str, int] = {}
    for r in rows:
        platform = r.get("platform", "").strip()
        if platform.lower() != "amazon":
            other_counts[platform] = other_counts.get(platform, 0) + 1
            continue
        month = r.get("month")
        if month is None or month < "2026-04" or month > current_month:
            diag.append({
                "level": "warning",
                "code": "month_out_of_range",
                "message": "account=%s month=%s 不在统计范围 2026-04~%s，未纳入。"
                           % (r.get("account", "?"), month, current_month),
            })
            continue
        amazon_rows.append(r)

    for platform, count in sorted(other_counts.items()):
        diag.append({
            "level": "info",
            "code": "other_platform",
            "message": "平台 %s 有 %d 行，未并入 Amazon 看板。" % (platform, count),
        })

    # 构建账号到 team 的映射（用于权威归属校验）
    account_team: dict[str, str] = {}
    account_canonical: dict[str, str] = {}
    for team_name, info in teams_def.items():
        for acc in info["accounts"]:
            norm = shop_filter.norm(acc)
            account_team[norm] = team_name
            account_canonical[norm] = acc

    # 按账号分组（只保留归属正确的行）
    rows_by_account: dict[str, list[dict]] = {}
    unmapped_rows: list[dict] = []
    unpaired_by_team: dict[str, dict] = {}  # team -> {"units": int|None, "skus": set}
    unpaired_sku_totals_by_team: dict[str, dict[str, dict]] = {}  # team -> sku -> totals
    merged_sku_totals_by_team: dict[str, dict[str, dict[str, object]]] = {}  # team -> alias sku -> audit totals

    for r in amazon_rows:
        acc_norm = shop_filter.norm(r["account"])
        # 检查账号是否属于预期 team（如果 r 带有 _source_team）
        expected_team = r.get("_source_team")
        if expected_team is not None:
            # 该记录来自某个 Team 表，需要校验
            allowed_accounts = teams_def.get(expected_team, {}).get("accounts", [])
            allowed_norms = {shop_filter.norm(a) for a in allowed_accounts}
            if acc_norm not in allowed_norms:
                # 账号不属于该表
                if acc_norm in account_team:
                    correct_team = account_team[acc_norm]
                    diag.append({
                        "level": "warning",
                        "code": "wrong_table",
                        "message": "错表：账号 %s 属于 %s，但出现在 %s 表中，不并入。"
                                   % (account_canonical[acc_norm], correct_team, expected_team),
                    })
                else:
                    diag.append({
                        "level": "warning",
                        "code": "unmapped_account",
                        "message": "账号 %s 不在任何 Team 名单中，不并入任何 Team。"
                                   % (r["account"],),
                    })
                unmapped_rows.append(r)
                continue
        else:
            # 旧调用没有 _source_team，仅按名单归类
            if acc_norm not in account_team:
                diag.append({
                    "level": "warning",
                    "code": "unmapped_account",
                    "message": "账号 %s 不在任何 Team 名单中，不并入任何 Team。" % (r["account"],),
                })
                unmapped_rows.append(r)
                continue

        team_name = account_team[acc_norm]
        # 组件别名：仅当 pair_map 提供且目标在 pair_map 中才并入
        alias_target = component_sales.COMPONENT_ALIASES.get(r["sku"]) if pair_map is not None else None
        if alias_target is not None and alias_target in pair_map:
            merged_totals = merged_sku_totals_by_team.setdefault(team_name, {})
            audit = merged_totals.setdefault(r["sku"], {
                "target_sku": alias_target,
                "known_qty": 0,
                "has_unknown": False,
            })
            if r.get("invalid"):
                audit["has_unknown"] = True
            else:
                qty = r.get("qty")
                if qty is not None:
                    audit["known_qty"] = audit["known_qty"] + qty
            rows_by_account.setdefault(acc_norm, []).append(r)
            continue

        # 配对过滤：若 pair_map 提供，只保留配对 SKU
        if pair_map is not None and r["sku"] not in pair_map:
            # 未配对 SKU 计入未配对统计，区分已知/未知
            unpaired = unpaired_by_team.setdefault(team_name, {
                "known_qty": 0,
                "has_unknown": False,
                "skus": set(),
            })
            sku_totals = unpaired_sku_totals_by_team.setdefault(team_name, {})
            sku_total = sku_totals.setdefault(r["sku"], {
                "known_qty": 0,
                "has_unknown": False,
            })
            if r.get("invalid"):
                unpaired["has_unknown"] = True
                sku_total["has_unknown"] = True
                diag.append({
                    "level": "warning",
                    "code": "unpaired_invalid",
                    "message": "account=%s SKU=%s 未配对且销量无效，件数未知。" % (r["account"], r["sku"]),
                })
            else:
                qty = r.get("qty")
                if qty is not None:
                    unpaired["known_qty"] = unpaired.get("known_qty", 0) + qty
                    sku_total["known_qty"] = sku_total["known_qty"] + qty
            unpaired["skus"].add(r["sku"])
            continue

        # 正确归属且配对通过（或未启用配对过滤）
        rows_by_account.setdefault(acc_norm, []).append(r)

    # 构建 teams 结果
    teams_output: list[dict] = []
    global_unpaired_by_team: dict[str, dict] = {}

    for team_name, info in teams_def.items():
        allowed_accounts = info["accounts"]
        shops: list[dict] = []
        team_unpaired_data = unpaired_by_team.get(team_name, {
            "known_qty": 0,
            "has_unknown": False,
            "skus": set(),
        })
        team_known_qty = team_unpaired_data.get("known_qty", 0)
        team_has_unknown = team_unpaired_data.get("has_unknown", False)
        team_skus_sorted = sorted(team_unpaired_data.get("skus", set()))
        if team_skus_sorted:
            units_text = "未知" if team_has_unknown else str(team_known_qty)
            diag.append({
                "level": "warning",
                "code": "unpaired_summary",
                "message": "%s 未配对 SKU 清单 %s；件数 %s"
                           % (team_name, "、".join(team_skus_sorted), units_text),
            })

        for acc in allowed_accounts:
            norm = shop_filter.norm(acc)
            acc_rows = rows_by_account.get(norm)
            if acc_rows is None:
                # 无记录店铺也要添加确认零条目（_build_shop 会处理）
                shop = _build_shop(acc, [], months, diag,
                                   pair_map=pair_map,
                                   confirmed_zero_skus=confirmed_zero_skus)
                shops.append(shop)
                diag.append({
                    "level": "warning",
                    "code": "no_rows",
                    "message": "Team 成员 %s 在表中完全没有记录。" % acc,
                })
            else:
                shop = _build_shop(acc, acc_rows, months, diag,
                                   pair_map=pair_map,
                                   confirmed_zero_skus=confirmed_zero_skus)
                shops.append(shop)

        unpaired_units = team_known_qty if not team_has_unknown else None
        unpaired_info = {
            "known_qty": team_known_qty,
            "has_unknown": team_has_unknown,
            "skus": team_skus_sorted,
        }
        unpaired_sku_totals = []
        for sku, totals in sorted(unpaired_sku_totals_by_team.get(team_name, {}).items()):
            known_qty = totals["known_qty"]
            has_unknown = totals["has_unknown"]
            unpaired_sku_totals.append({
                "sku": sku,
                "units": known_qty if not has_unknown else None,
                "known_qty": known_qty,
                "has_unknown": has_unknown,
            })
        merged_sku_totals = []
        for sku, totals in sorted(merged_sku_totals_by_team.get(team_name, {}).items()):
            known_qty = totals["known_qty"]
            has_unknown = totals["has_unknown"]
            merged_sku_totals.append({
                "sku": sku,
                "target_sku": totals["target_sku"],
                "units": known_qty if not has_unknown else None,
                "known_qty": known_qty,
                "has_unknown": has_unknown,
            })
        teams_output.append({
            "name": team_name,
            "shops": shops,
            "unpaired_units": unpaired_units,
            "unpaired_skus": team_skus_sorted,
            "unpaired": unpaired_info,
            "unpaired_sku_totals": unpaired_sku_totals,
            "merged_sku_totals": merged_sku_totals,
        })
        global_unpaired_by_team[team_name] = {
            "units": unpaired_units,
            "has_unknown": team_has_unknown,
        }

    # 构建 unmapped 信息
    unmapped: list[dict] = []
    for r in unmapped_rows:
        # 查找 display_name
        display_name = r.get("display_name") or r["account"]
        unmapped.append({
            "account": r["account"],
            "display_name": display_name,
            "rows": 1,  # 每个 unmapped 行独立
            "units": r.get("qty") if r.get("qty") is not None and not r.get("invalid") else 0,
        })

    # 源信息
    source = copy.deepcopy(source_meta or {})
    source.setdefault("table_name", DEFAULT_TABLE_NAME)
    source.setdefault("row_count", 0)
    source.setdefault("page_count", 0)

    # 售后：优先使用调用方传入的只读实时聚合；否则保留固定快照用于离线回归。
    if aftersale_summary is None:
        aftersale_summary = _build_aftersale_summary(_load_aftersale_snapshot())

    # 产品目录：基于已提供的 pair_map，序列化为只读数组。
    products: list[dict] = []
    if pair_map is not None:
        from . import aftersale_live
        zero_skus = confirmed_zero_skus or set()
        for sku in pair_map:
            pair = pair_map[sku]
            display_name = pair.get("display_name") or ""
            products.append({
                "sku": sku,
                "display_name": display_name,
                "wf_part_number": pair.get("wf_part_number") or "",
                "confirmed_zero": sku in zero_skus,
                "t01": aftersale_live._is_t01(sku, display_name),
            })

    return {
        "version": 2,
        "ok": True,
        "fetched_at": fetched_at,
        "months": months,
        "current_month": current_month,
        "current_month_partial": True,
        "source": source,
        "teams": teams_output,
        "unmapped": unmapped,
        "other_platforms": [{"platform": p, "rows": c} for p, c in sorted(other_counts.items())],
        "products": products,
        "aftersale": aftersale_summary,
        "wayfair": {
            "connected": False,
            "message": "Wayfair 销量尚未接入",
        },
        "diagnostics": diag,
        "stale": False,
        "error": None,
        "last_success_at": fetched_at,
        "unpaired": {
            "total_units": None if any(
                item.get("has_unknown") for item in global_unpaired_by_team.values()
            ) else sum(
                item.get("units", 0) for item in global_unpaired_by_team.values()
                if item.get("units") is not None
            ),
            "by_team": global_unpaired_by_team,
        },
    }


# ---------------------------------------------------------------- 缓存入口

def get_board(app_id, app_secret, *, refresh=False, now_fn=None, http=None, cache=None):
    """只读看板快照，带 30 秒内存缓存与失败回退。正式路径只读新四表。"""
    now = now_fn() if now_fn else datetime.now(UTC8)
    now = _utc8(now)

    app_token = os.environ.get("VOC_BITABLE_APP_TOKEN", DEFAULT_APP_TOKEN)

    cache = cache if cache is not None else _CACHE
    # 缓存身份包含四表及业务口径版本
    key = (
        app_id, app_secret, app_token,
        TEAM_TABLES["Team 1"]["table_id"],
        TEAM_TABLES["Team 2"]["table_id"],
        TEAM_TABLES["Team 3"]["table_id"],
        PAIR_TABLE_ID,
        CACHE_VERSION,
    )

    def _fresh(entry) -> bool:
        return bool(entry and time.time() < entry.get("expires", 0))

    if not refresh:
        entry = cache.get(key)
        if _fresh(entry):
            return copy.deepcopy(entry["snapshot"])

    with _CACHE_LOCK:
        if not refresh:
            entry = cache.get(key)
            if _fresh(entry):
                return copy.deepcopy(entry["snapshot"])

        token = None
        try:
            token = feishu.get_tenant_token(app_id, app_secret)

            # 1. 读取三张 Team 表
            all_team_records: list[dict] = []
            total_row_count = 0
            total_page_count = 0
            table_details: list[dict] = []

            for team_name, info in TEAM_TABLES.items():
                table_id = info["table_id"]
                fetch_fields(token, app_token, table_id, http=http,
                             required_fields=REQUIRED_FIELDS)
                records, page_count = fetch_records(token, app_token, table_id, http=http)
                # 为每条记录添加来源 team 标识（用于归属校验）
                for rec in records:
                    rec["_source_team"] = team_name
                all_team_records.extend(records)
                total_row_count += len(records)
                total_page_count += page_count
                table_details.append({
                    "table_name": "AMAZON " + team_name,
                    "table_id": table_id,
                    "row_count": len(records),
                    "page_count": page_count,
                })

            # 2. 读取配对表，一次调用返回 pair_map、行数、页数
            pair_map, pair_row_count, pair_page_count = fetch_pairing(token, app_token, http=http)
            table_details.append({
                "table_name": "SKU配对表",
                "table_id": PAIR_TABLE_ID,
                "row_count": pair_row_count,
                "page_count": pair_page_count,
            })

            # 3. 规范化所有 Team 记录（使用 normalize 获得 rows 和诊断）
            rows, diagnostics = normalize(all_team_records)

            # 4. 构建快照
            source_meta = {
                "table_name": "AMAZON TEAM 1 + AMAZON TEAM 2 + AMAZON TEAM 3 + SKU配对表",
                "row_count": total_row_count,
                "page_count": total_page_count,
                "pairing_row_count": pair_row_count,
                "pairing_page_count": pair_page_count,
                "table_details": table_details,
            }
            snapshot = build_snapshot(
                rows,
                diagnostics,
                now=now,
                source_meta=source_meta,
                teams=TEAM_TABLES,
                pair_map=pair_map,
                confirmed_zero_skus=CONFIRMED_ZERO_SKUS,
            )

            # 售后数据与销量从同一轮只读快照聚合，失败不以旧固定数据冒充实时。
            from . import aftersale_live
            live_aftersale = aftersale_live.load(
                token, app_token, pair_map, snapshot, http=http,
                fetched_at=snapshot["fetched_at"],
            )
            live_aftersale["source_as_of"] = snapshot["fetched_at"]
            live_aftersale["sales_source_as_of"] = snapshot["fetched_at"]
            if live_aftersale.get("connected") is not True:
                old = cache.get(key)
                old_snapshot = old.get("snapshot") if old else None
                old_aftersale = (old_snapshot or {}).get("aftersale") if old_snapshot else None
                if isinstance(old_aftersale, dict) and old_aftersale.get("connected") is True:
                    snap = copy.deepcopy(old_snapshot)
                    aftersale = snap.get("aftersale") or {}
                    aftersale["stale"] = True
                    aftersale["error"] = "售后源刷新失败，正在显示上次成功数据。"
                    snap["stale"] = True
                    snap["error"] = "售后源刷新失败，正在显示上次成功数据。"
                    snap["last_success_at"] = (
                        old_snapshot.get("last_success_at") or
                        old_snapshot.get("fetched_at")
                    )
                    return snap
            snapshot["aftersale"] = live_aftersale
            # Cache only a fresh, fully validated successful sales snapshot. If
            # the aftersale refresh failed and its own result is stale/unknown,
            # retain the previous cache entry (when any) rather than replacing a
            # known-good aftersale result with the failed refresh.
            if not snapshot.get("stale"):
                cache[key] = {"snapshot": copy.deepcopy(snapshot),
                              "expires": time.time() + _CACHE_TTL}
            return snapshot
        except Exception as exc:
            err = _public_error(exc, secrets=tuple(x for x in (app_secret, token) if x))
            entry = cache.get(key)
            if entry and entry.get("snapshot"):
                snap = copy.deepcopy(entry["snapshot"])
                snap["stale"] = True
                snap["error"] = err
                snap["last_success_at"] = snap.get("last_success_at") or snap.get("fetched_at")
                return snap
            return {"ok": False, "error": err, "last_success_at": None, "stale": False}
