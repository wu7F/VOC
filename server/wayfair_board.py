# -*- coding: utf-8 -*-
"""Wayfair 飞书多维表格看板数据源（只读、独立于 Amazon）。"""

from __future__ import annotations

import copy
import math
import os
import re
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from . import feishu
from .aftersale_live import _is_t01
from .bitable_board import (
    DUP_SKU_IGNORE_NAME,
    DUP_SKU_KEEP_NAME,
    DUP_SKU_SPECIAL,
    BoardError,
    fetch_fields,
    fetch_records,
)

# ---------------------------------------------------------------------------
# Wayfair 数据源常量
# ---------------------------------------------------------------------------

WAYFAIR_BASE_APP_TOKEN = "EmsLbLeKSasY4ZsXhYocpwaInKj"
SALES_TABLE_ID = "tblqdIy1im66p76r"
AFTER_TABLE_ID = "tbleZkN9hVYq48g0"
PAIR_TABLE_ID = "tbl6n6QymoJXDciu"
POLICY_VERSION = "wayfair_policy_2026-04_v1"

_UTC8 = timezone(timedelta(hours=8))
_START_DATE = date(2026, 4, 1)
_START_YM = "2026-04"
_TEAM_NAME = "Wayfair"
_ACCOUNT_NAME = "Wayfair玺家"

_CACHE_TTL = 30
_CACHE = {}
_CACHE_LOCK = threading.Lock()

_PATH_HINT = re.compile(r"[A-Za-z]:\\[^\s\"']+|/[A-Za-z0-9_.\-]+/[A-Za-z0-9_./\-]+")


# ---------------------------------------------------------------------------
# 基础读取/清洗工具
# ---------------------------------------------------------------------------

def _to_utc8(dt):
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_UTC8)
    return dt.astimezone(_UTC8)


def _text_val(v):
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        if not math.isfinite(v):
            return ""
        return str(v).strip()
    return ""


def _parse_qty(v):
    """数量字段可能是文本数字或 number。只接受有限非负整数，拒绝布尔/小数/负数/空/NaN/Infinity。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if not math.isfinite(v):
            return None
        if v < 0 or v != int(v):
            return None
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            d = Decimal(s)
        except InvalidOperation:
            return None
        if not d.is_finite():
            return None
        if d < 0:
            return None
        if d != d.to_integral_value():
            return None
        return int(d)
    return None


def _parse_date_millis(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        if not math.isfinite(v) or v != int(v):
            return None
        ms = int(v)
    elif isinstance(v, str):
        s = v.strip()
        if not re.fullmatch(r"-?\d+", s):
            return None
        try:
            ms = int(s)
        except ValueError:
            return None
    else:
        return None
    if ms < 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone(_UTC8)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_after_date(v):
    if v is None or isinstance(v, bool):
        return None
    s = str(v).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=_UTC8)
        except ValueError:
            continue
    return None


def _month_from_dt(dt):
    return dt.strftime("%Y-%m")


def _month_tuple(ym):
    return int(ym[:4]), int(ym[5:7])


def _add_months(ym, delta):
    y, m = _month_tuple(ym)
    total = y * 12 + (m - 1) + delta
    new_y, new_m = divmod(total, 12)
    return "%04d-%02d" % (new_y, new_m + 1)


def _generate_months(start_ym, end_ym):
    months = []
    cur = start_ym
    while cur <= end_ym:
        months.append(cur)
        cur = _add_months(cur, 1)
    return months


def _rate(numerator, denominator):
    if denominator is None or denominator == 0:
        return None
    return round(numerator / denominator * 100, 6)


def _public_error(exc, secrets=()):
    msg = str(exc).strip() or exc.__class__.__name__
    for s in secrets:
        if s:
            msg = msg.replace(str(s), "<凭证>")
    msg = _PATH_HINT.sub("<路径>", msg)
    if len(msg) > 400:
        msg = msg[:400] + "…"
    return msg


# ---------------------------------------------------------------------------
# 售后原因精确归并
# ---------------------------------------------------------------------------

def _canon_form(v):
    return re.sub(r"\s+", "", v.lower())


def _is_cjk(ch):
    return 0x4E00 <= ord(ch) <= 0x9FFF


def _split_reason(text):
    s = text.strip()
    if not s:
        return "", ""

    # 先拆中英文括号
    for i, ch in enumerate(s):
        if ch in ("（", "("):
            parent = s[:i].strip()
            opening = ch
            closing = "）" if opening == "（" else ")"
            tail = s[i + 1:]
            j = tail.find(closing)
            detail = tail[:j].strip() if j != -1 else tail.strip()
            return parent, detail

    # 再拆破折号
    if "—" in s:
        i = s.index("—")
        return s[:i].strip(), s[i + 1:].strip()

    # 中文字符后的连字符才作为一级/二级分隔，避免拆 Mis-information
    if "-" in s:
        i = s.index("-")
        if i > 0 and _is_cjk(s[i - 1]):
            return s[:i].strip(), s[i + 1:].strip()

    return s, ""


_PRIMARY_ALIASES = {
    _canon_form("Mis infor"): ("信息/描述不符", None),
    _canon_form("Misinfor"): ("信息/描述不符", None),
    _canon_form("Misinformation"): ("信息/描述不符", None),
    _canon_form("Mis-information"): ("信息/描述不符", None),
    _canon_form("misinformation"): ("信息/描述不符", None),
    _canon_form("different on website"): ("信息/描述不符", None),
    _canon_form("defective"): ("质量问题", None),
    _canon_form("defect"): ("质量问题", None),
    _canon_form("warranty"): ("保修", None),
    _canon_form("保修"): ("保修", None),
    _canon_form("破损"): ("破损损坏", None),
    _canon_form("不再需要"): ("不再需要", None),
    _canon_form("不需要"): ("不再需要", None),
    _canon_form("不符合期待"): ("不符合期待", None),
    _canon_form("不符期待"): ("不符合期待", None),
    _canon_form("不舒服"): ("舒适度问题", None),
    _canon_form("面料脏"): ("面料/外观问题", "污渍"),
    _canon_form("产品有污渍"): ("面料/外观问题", "污渍"),
    _canon_form("面料质量不佳"): ("面料/外观问题", None),
    _canon_form("面料问题"): ("面料/外观问题", None),
    _canon_form("颜色问题"): ("颜色问题", None),
    _canon_form("连接问题"): ("连接问题", None),
}

_PASSTHROUGH = {"质量问题", "物流问题", "配件问题", "无理由"}

_DETAIL_ALIASES = {
    _canon_form("damage"): "破损",
    _canon_form("different on website"): "与网站描述不符",
}


def reason_parts(text):
    """返回 (primary, secondary)，仅使用售后原因字段并按已批准规则归并。"""
    if text is None:
        text = ""
    parent_raw, detail_raw = _split_reason(text)
    explicit_secondary = None

    if not parent_raw:
        primary = "未注明"
    else:
        key = _canon_form(parent_raw)
        if key in _PRIMARY_ALIASES:
            primary, explicit_secondary = _PRIMARY_ALIASES[key]
        elif parent_raw in _PASSTHROUGH:
            primary = parent_raw
        else:
            primary = parent_raw

    if not detail_raw:
        secondary = explicit_secondary or "未注明"
    else:
        detail_key = _canon_form(detail_raw)
        secondary = _DETAIL_ALIASES.get(detail_key, detail_raw)

    return primary, secondary


def _reason_primary_raw(text):
    parent_raw, _ = _split_reason(text)
    return parent_raw


def _is_known_reason(parent_raw):
    if not parent_raw:
        return True
    return _canon_form(parent_raw) in _PRIMARY_ALIASES or parent_raw in _PASSTHROUGH


# ---------------------------------------------------------------------------
# T01 兼容调用
# ---------------------------------------------------------------------------

def _is_t01_product(sku, display_name=""):
    return bool(_is_t01(sku, display_name))


# ---------------------------------------------------------------------------
# 配对表构建
# ---------------------------------------------------------------------------

PAIR_REQUIRED_FIELDS = {
    "易仓SKU": 1,
    "SKU中文名称": 1,
    "WF Part Number （整个SKU）": 1,
}


def _build_pair_products(pair_records):
    grouped = defaultdict(list)

    for rec in pair_records:
        fields = rec.get("fields") or {}
        sku = _text_val(fields.get("易仓SKU"))
        cn = _text_val(fields.get("SKU中文名称"))
        wf = _text_val(fields.get("WF Part Number （整个SKU）"))
        if not sku or not cn or not wf:
            raise BoardError("Wayfair 配对表存在空 SKU/中文名称/WF Part Number。")
        grouped[sku].append((cn, wf))

    products = {}
    wf_to_canonical = {}

    for sku, rows in grouped.items():
        cn_values = [cn for cn, _ in rows]

        if sku == DUP_SKU_SPECIAL:
            keep_rows = [r for r in rows if r[0] == DUP_SKU_KEEP_NAME]
            if len(keep_rows) != 1:
                raise BoardError(
                    "Wayfair 配对表 %s 主推行规则冲突：期望唯一主推行“%s”，实际 %d 行。"
                    % (DUP_SKU_SPECIAL, DUP_SKU_KEEP_NAME, len(keep_rows))
                )
            unknown_special_rows = [
                r for r in rows
                if r[0] not in (DUP_SKU_KEEP_NAME, DUP_SKU_IGNORE_NAME)
            ]
            if unknown_special_rows:
                raise BoardError("Wayfair 配对表 %s 存在未知特殊行，请人工确认。" % DUP_SKU_SPECIAL)
            display_name = DUP_SKU_KEEP_NAME
        else:
            distinct_cn = set(cn_values)
            if len(distinct_cn) != 1:
                raise BoardError(
                    "Wayfair 配对表易仓SKU %s 的中文名称冲突：%s。"
                    % (sku, "、".join(sorted(distinct_cn)))
                )
            display_name = distinct_cn.pop()

        wf_keys = []
        for _cn, wf in rows:
            previous = wf_to_canonical.get(wf)
            if previous is not None and previous != sku:
                raise BoardError(
                    "Wayfair 配对表 WF Part Number %s 同时映射到 %s 和 %s。"
                    % (wf, previous, sku)
                )
            wf_to_canonical[wf] = sku
            wf_keys.append(wf)

        wf_keys = sorted(set(wf_keys))
        products[sku] = {
            "sku": sku,
            "display_name": display_name,
            "wf_part_number": wf_keys,
            "t01": _is_t01_product(sku, display_name),
        }

    return products, wf_to_canonical


# ---------------------------------------------------------------------------
# 查看板快照
# ---------------------------------------------------------------------------

def build_snapshot(sales_records, after_records, pair_records, *, now, source_meta=None):
    """纯函数：把飞书记录转换为 Wayfair 看板快照。"""
    now = _to_utc8(now)
    now_ym = _month_from_dt(now)

    products, wf_to_canonical = _build_pair_products(pair_records)

    counts = defaultdict(int)
    sales_by_sku_month = defaultdict(lambda: defaultdict(int))
    month_observed_matched = set()
    unpaired_sales = defaultdict(lambda: {"units": 0, "rows": 0})

    # ------------------------------------------------------ 销售
    for rec in sales_records:
        fields = rec.get("fields") or {}
        shop = _text_val(fields.get("店铺"))
        if shop != _ACCOUNT_NAME:
            counts["sales_other_shop"] += 1
            continue

        wf_sku = _text_val(fields.get("SKU"))
        if not wf_sku:
            counts["sales_invalid_sku_rows"] += 1
            continue

        dt = _parse_date_millis(fields.get("订单日期"))
        if dt is None:
            counts["sales_date_invalid"] += 1
            continue
        if dt.date() < _START_DATE:
            counts["sales_before_start"] += 1
            continue

        qty = _parse_qty(fields.get("订单购买数量"))
        if qty is None:
            counts["sales_qty_invalid"] += 1
            continue

        month = _month_from_dt(dt)
        canonical = wf_to_canonical.get(wf_sku)

        if canonical:
            prod = products[canonical]
            if prod["t01"]:
                counts["sales_t01_excluded_units"] += qty
                counts["sales_t01_excluded_rows"] += 1
                continue
            sales_by_sku_month[canonical][month] += qty
            month_observed_matched.add(month)
        else:
            if _is_t01_product(wf_sku, ""):
                counts["sales_t01_excluded_units"] += qty
                counts["sales_t01_excluded_rows"] += 1
                continue
            unpaired_sales[wf_sku]["units"] += qty
            unpaired_sales[wf_sku]["rows"] += 1
            counts["sales_unpaired_rows"] += 1
            counts["sales_unpaired_units"] += qty

    # ------------------------------------------------------ 售后日期解析与重复审计
    dated_aftersale_rows = []
    for rec in after_records:
        fields = rec.get("fields") or {}
        dt = _parse_after_date(fields.get("客诉产生时间"))
        if dt is None:
            counts["after_date_invalid"] += 1
            continue
        if dt.date() < _START_DATE:
            counts["after_before_start"] += 1
            continue

        wf_sku = _text_val(fields.get("SKU"))
        if not wf_sku:
            counts["after_invalid_sku_rows"] += 1
            continue

        order_no = _text_val(fields.get("订单号"))
        reason_raw = _text_val(fields.get("售后原因"))
        row = {
            "dt": dt,
            "sku": wf_sku,
            "order": order_no,
            "reason": reason_raw,
        }
        dated_aftersale_rows.append(row)

        # 诊断新写法（未批准一级）
        raw_parent = _reason_primary_raw(reason_raw)
        if raw_parent and not _is_known_reason(raw_parent):
            counts["reason_unknown_rows"] += 1

    # 重复审计：只做诊断，不改变逐行计数
    duplicate_groups = defaultdict(list)
    for row in dated_aftersale_rows:
        if row["order"] and row["sku"]:
            duplicate_groups[(row["order"], row["sku"])].append(row)
    for group_rows in duplicate_groups.values():
        if len(group_rows) > 1:
            counts["after_duplicate_groups"] += 1
            counts["after_duplicate_involved_rows"] += len(group_rows)
            counts["after_duplicate_extra_rows"] += len(group_rows) - 1

    # ------------------------------------------------------ 售后匹配/回退/原因
    after_counts_by_sku_month = defaultdict(lambda: defaultdict(int))
    after_reasons = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(int))))
    unpaired_after = defaultdict(lambda: {"rows": 0})

    for row in dated_aftersale_rows:
        wf_sku = row["sku"]
        canonical = wf_to_canonical.get(wf_sku)

        if canonical:
            prod = products[canonical]
            if prod["t01"]:
                counts["after_t01_excluded"] += 1
                continue
        else:
            if _is_t01_product(wf_sku, ""):
                counts["after_t01_excluded"] += 1
                continue
            unpaired_after[wf_sku]["rows"] += 1
            counts["after_unmatched_rows"] += 1
            continue

        event_month = _month_from_dt(row["dt"])
        sku_sales = sales_by_sku_month.get(canonical, {})

        if sku_sales.get(event_month, 0) > 0:
            assigned_month = event_month
        else:
            previous_positive = [
                m for m in sku_sales
                if m < event_month and sku_sales.get(m, 0) > 0
            ]
            if not previous_positive:
                counts["after_no_sales_month_rows"] += 1
                continue
            assigned_month = max(previous_positive)

        if assigned_month != event_month:
            counts["after_shifted_rows"] += 1

        after_counts_by_sku_month[canonical][assigned_month] += 1
        primary, secondary = reason_parts(row["reason"])
        after_reasons[canonical][assigned_month][primary][secondary] += 1

    # ------------------------------------------------------ 销售月份范围
    observed_sales_months = set()
    for sku_months in sales_by_sku_month.values():
        observed_sales_months.update(sku_months.keys())

    max_sales_ym = max(observed_sales_months, default=_START_YM)
    end_ym = now_ym if now_ym >= _START_YM else _START_YM
    if max_sales_ym > end_ym:
        end_ym = max_sales_ym
    months = _generate_months(_START_YM, end_ym)

    # ------------------------------------------------------ team/shops/skus
    store_month_units = defaultdict(int)
    for sku_months in sales_by_sku_month.values():
        for m, units in sku_months.items():
            store_month_units[m] += units

    shop_months = {}
    for m in months:
        shop_months[m] = store_month_units.get(m)

    team_skus = []
    products_output = []
    for sku in sorted(products):
        prod = products[sku]
        if prod["t01"]:
            continue

        sold_map = {}
        sku_sales = sales_by_sku_month.get(sku, {})
        for m in months:
            sold_map[m] = sku_sales.get(m)

        team_skus.append({
            "sku": sku,
            "display_name": prod["display_name"],
            "wf_part_number": ", ".join(prod["wf_part_number"]),
            "sold": sold_map,
        })

        products_output.append({
            "sku": sku,
            "display_name": prod["display_name"],
            "wf_part_number": ", ".join(prod["wf_part_number"]),
            "t01": False,
            "confirmed_zero": False,
        })

    unpaired_sales_sorted = [
        {"sku": sku, "units": data["units"], "rows": data["rows"]}
        for sku, data in sorted(
            unpaired_sales.items(),
            key=lambda kv: (-kv[1]["units"], -kv[1]["rows"], kv[0]),
        )
    ]
    unpaired_after_sorted = [
        {"sku": sku, "rows": data["rows"]}
        for sku, data in sorted(
            unpaired_after.items(),
            key=lambda kv: (-kv[1]["rows"], kv[0]),
        )
    ]
    total_unpaired_units = sum(
        data["units"] for data in unpaired_sales.values()
    )

    team = {
        "name": _TEAM_NAME,
        "shops": [{
            "account": _ACCOUNT_NAME,
            "display_name": _ACCOUNT_NAME,
            "months": shop_months,
            "units_known": sum(store_month_units.values()),
            "missing_months": [
                m for m in months if m not in month_observed_matched
            ],
            "skus": team_skus,
        }],
        "unpaired_units": total_unpaired_units,
        "unpaired_skus": sorted(unpaired_sales.keys()),
        "unpaired_sku_totals": unpaired_sales_sorted,
        "merged_sku_totals": [],
    }

    unpaired = {
        "total_units": total_unpaired_units,
        "sales": unpaired_sales_sorted,
        "aftersale": unpaired_after_sorted,
    }

    # ------------------------------------------------------ aftersale 汇总
    total_num = sum(
        sum(month_counts.values())
        for month_counts in after_counts_by_sku_month.values()
    )
    total_den = sum(
        sum(month_units.values())
        for month_units in sales_by_sku_month.values()
    )
    total_rate = _rate(total_num, total_den)

    total = {
        "numerator": total_num,
        "denominator": total_den,
        "rate": total_rate,
    }
    summary = copy.deepcopy(total)

    stores = [{
        "account": _ACCOUNT_NAME,
        "numerator": total_num,
        "denominator": total_den,
        "rate": total_rate,
    }]

    store_month = []
    for m in sorted(set(store_month_units.keys())):
        den = store_month_units.get(m, 0)
        num = sum(
            after_counts_by_sku_month[sku].get(m, 0)
            for sku in after_counts_by_sku_month
        )
        store_month.append({
            "account": _ACCOUNT_NAME,
            "month": m,
            "numerator": num,
            "denominator": den,
            "rate": _rate(num, den),
        })

    sku_store_month = []
    for sku in sorted(sales_by_sku_month):
        prod = products.get(sku)
        if not prod or prod["t01"]:
            continue
        for m in sorted(sales_by_sku_month[sku]):
            den = sales_by_sku_month[sku][m]
            num = after_counts_by_sku_month.get(sku, {}).get(m, 0)
            sku_store_month.append({
                "account": _ACCOUNT_NAME,
                "team": _TEAM_NAME,
                "sku": sku,
                "month": m,
                "numerator": num,
                "denominator": den,
                "rate": _rate(num, den),
            })

    reason_items = []
    subcategory_items = []
    for sku in sorted(after_counts_by_sku_month):
        for m in sorted(after_counts_by_sku_month[sku]):
            total_reasons = after_counts_by_sku_month[sku][m]
            primary_map = after_reasons.get(sku, {}).get(m, {})
            for primary in sorted(primary_map):
                secondary_map = primary_map[primary]
                primary_count = sum(secondary_map.values())
                reason_items.append({
                    "account": _ACCOUNT_NAME,
                    "sku": sku,
                    "month": m,
                    "category": primary,
                    "count": primary_count,
                    "share": _rate(primary_count, total_reasons),
                })
                for secondary in sorted(secondary_map):
                    sub_count = secondary_map[secondary]
                    subcategory_items.append({
                        "account": _ACCOUNT_NAME,
                        "sku": sku,
                        "month": m,
                        "category": primary,
                        "subcategory": secondary,
                        "count": sub_count,
                        "share": _rate(sub_count, primary_count),
                    })

    aftersale = {
        "connected": True,
        "realtime": True,
        "message": (
            "售后行均计入 Wayfair玺家；当月销量为0时按规则向前回退至最近有正销量月份。"
            "一级和二级均来自售后原因字段的已批准归并。"
        ),
        "source_as_of": now.isoformat(),
        "sales_source_as_of": now.isoformat(),
        "total": total,
        "summary": summary,
        "teams": [],
        "stores": stores,
        "store_month": store_month,
        "sku_store_month": sku_store_month,
        "reasons": {
            "connected": True,
            "items": reason_items,
            "subcategory_items": subcategory_items,
            "message": "一级和二级均来自售后原因字段的已批准归并",
        },
    }

    # ------------------------------------------------------ diagnostics/counts
    diagnostics = []
    diag_codes = [
        ("sales_other_shop", "非 Wayfair玺家 店铺销售行"),
        ("sales_date_invalid", "销售日期缺失/无效"),
        ("sales_before_start", "销售日期早于 2026-04"),
        ("sales_qty_invalid", "订单购买数量缺失/无效"),
        ("sales_invalid_sku_rows", "销售 SKU 缺失/无效"),
        ("sales_t01_excluded_rows", "T01 系列销售行（已排除）"),
        ("sales_t01_excluded_units", "T01 系列销售数量（已排除）"),
        ("sales_unpaired_rows", "销售 SKU 未配对"),
        ("sales_unpaired_units", "未配对销售数量"),
        ("after_date_invalid", "售后日期缺失/无效"),
        ("after_before_start", "售后日期早于 2026-04"),
        ("after_invalid_sku_rows", "售后 SKU 缺失/无效"),
        ("after_t01_excluded", "T01 系列售后行（已排除）"),
        ("after_unmatched_rows", "售后 SKU 未配对"),
        ("after_no_sales_month_rows", "无可回退正销量月份售后行（未计入）"),
        ("after_shifted_rows", "售后原因向前回退月份"),
        ("after_duplicate_groups", "售后重复组数（订单号+WF SKU）"),
        ("after_duplicate_involved_rows", "售后重复涉及行数"),
        ("after_duplicate_extra_rows", "售后重复额外行数"),
        ("reason_unknown_rows", "售后原因未批准写法（待归并）"),
    ]
    counts_dict = {}
    for code, message in diag_codes:
        count = int(counts.get(code) or 0)
        if count > 0:
            diagnostics.append({
                "code": code,
                "message": message,
                "count": count,
            })
        counts_dict[code] = count
    counts_dict["sales_unpaired_units"] = total_unpaired_units
    counts_dict["sales_unpaired_rows"] = sum(
        data["rows"] for data in unpaired_sales.values()
    )

    source = {
        "table_name": source_meta.get("table_name", "") if source_meta else "",
        "row_count": source_meta.get("row_count") if source_meta else 0,
        "page_count": source_meta.get("page_count") if source_meta else 0,
        "table_details": source_meta.get("table_details", []) if source_meta else [],
    }

    snapshot = {
        "platform": _TEAM_NAME,
        "time_zone": "Asia/Taipei",
        "ok": True,
        "version": 2,
        "fetched_at": now.isoformat(),
        "last_success_at": now.isoformat(),
        "stale": False,
        "error": None,
        "months": months,
        "current_month": now_ym,
        "current_month_partial": True,
        "source": source,
        "teams": [team],
        "products": products_output,
        "aftersale": aftersale,
        "unpaired": unpaired,
        "diagnostics": diagnostics,
        "counts": counts_dict,
    }
    return snapshot


# ---------------------------------------------------------------------------
# 公开 get_board
# ---------------------------------------------------------------------------

SALES_FIXED_FIELDS = {
    "店铺": 1,
    "订单日期": 5,
    "SKU": 1,
}
AFTER_REQUIRED_FIELDS = {
    "客诉产生时间": 1,
    "SKU": 1,
    "售后原因": 1,
    "订单号": 1,
}


def get_board(app_id, app_secret, *, refresh=False, now_fn=None, http=None, cache=None):
    """只读 Wayfair 看板快照，带独立 30 秒内存缓存与失败回退。"""
    now = _to_utc8(now_fn() if now_fn else datetime.now(_UTC8))
    app_token = os.environ.get("VOC_BITABLE_APP_TOKEN", WAYFAIR_BASE_APP_TOKEN)

    cache = cache if cache is not None else _CACHE
    key = (
        app_id, app_secret, app_token,
        SALES_TABLE_ID, AFTER_TABLE_ID, PAIR_TABLE_ID,
        POLICY_VERSION,
    )

    def _fresh(entry):
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

            sales_fields = fetch_fields(
                token, app_token, SALES_TABLE_ID, http=http,
                required_fields=SALES_FIXED_FIELDS,
            )
            qty_field = sales_fields.get("订单购买数量")
            if not qty_field:
                raise BoardError("Wayfair 销售表缺少字段：订单购买数量。")
            if qty_field.get("type") not in (1, 2):
                raise BoardError(
                    "Wayfair 销售表订单购买数量字段类型漂移：实际类型 %s，期望 1 或 2。"
                    % qty_field.get("type")
                )

            fetch_fields(
                token, app_token, AFTER_TABLE_ID, http=http,
                required_fields=AFTER_REQUIRED_FIELDS,
            )
            fetch_fields(
                token, app_token, PAIR_TABLE_ID, http=http,
                required_fields=PAIR_REQUIRED_FIELDS,
            )

            sales_records, sales_pages = fetch_records(
                token, app_token, SALES_TABLE_ID, http=http
            )
            after_records, after_pages = fetch_records(
                token, app_token, AFTER_TABLE_ID, http=http
            )
            pair_records, pair_pages = fetch_records(
                token, app_token, PAIR_TABLE_ID, http=http
            )

            source_meta = {
                "table_name": "WAYFAIR 销售表 + 售后表 + SKU配对表",
                "row_count": len(sales_records) + len(after_records) + len(pair_records),
                "page_count": sales_pages + after_pages + pair_pages,
                "table_details": [
                    {
                        "table_name": "Wayfair 销售表",
                        "table_id": SALES_TABLE_ID,
                        "row_count": len(sales_records),
                        "page_count": sales_pages,
                    },
                    {
                        "table_name": "Wayfair 售后表",
                        "table_id": AFTER_TABLE_ID,
                        "row_count": len(after_records),
                        "page_count": after_pages,
                    },
                    {
                        "table_name": "Wayfair SKU配对表",
                        "table_id": PAIR_TABLE_ID,
                        "row_count": len(pair_records),
                        "page_count": pair_pages,
                    },
                ],
            }

            snapshot = build_snapshot(
                sales_records,
                after_records,
                pair_records,
                now=now,
                source_meta=source_meta,
            )

            cache[key] = {
                "snapshot": copy.deepcopy(snapshot),
                "expires": time.time() + _CACHE_TTL,
            }
            return copy.deepcopy(snapshot)

        except Exception as exc:
            err = _public_error(exc, secrets=(
                app_secret, token,
            ))
            entry = cache.get(key)
            if entry and entry.get("snapshot"):
                snap = copy.deepcopy(entry["snapshot"])
                snap["stale"] = True
                snap["error"] = err
                snap["last_success_at"] = (
                    snap.get("last_success_at") or snap.get("fetched_at")
                )
                return snap
            return {
                "ok": False,
                "error": err,
                "last_success_at": None,
                "stale": False,
            }
