# -*- coding: utf-8 -*-
"""售后实时聚合（只读）：从两张售后表读取记录，按店铺/SKU/月份聚合分子与原因。

本模块不写表、不推送、不调用 LLM，也不返回任何原始行或原文。
"""

from __future__ import annotations

import csv
import hashlib
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .bitable_board import fetch_records, fetch_fields, BoardError
from .aftersale import _reason_category
from .aftersale_teams import TEAM_MEMBERS
from .reason_detail import reason_subcategory

UTC8 = timezone(timedelta(hours=8), "Asia/Shanghai")

AFTERSALE_TABLE_1 = "tblxsG5v24ohZyF7"
AFTERSALE_TABLE_2 = "tblPYPZnDP06szoy"
SHOP_FIELD_1 = "店铺名称"
SHOP_FIELD_2 = "店铺账号"
REQUIRED_FIELDS = ("SKU", "易仓sku", "原文", "日期")

START_MONTH = "2026-04"

DEFAULT_MAPPING_PATH = Path(__file__).resolve().parent / "aftersale_sku_relation.csv"

# 精确账号别名（仅这些标签可映射到规范账号）
ACCOUNT_ALIASES = {
    "初颂": "RUIBAO_US_US",
    "RUIBAO_US_US": "RUIBAO_US_US",
    "香港": "KornerHK_US",
    "香港店": "KornerHK_US",
    "KornerHK_US": "KornerHK_US",
    "southwest": "southwest_US",
    "southwest_US": "southwest_US",
    "威卡斯": "Wekas_US",
    "Wekas_US": "Wekas_US",
    "麦吉天": "AmazonMJSKY_US_US",
    "买几天": "AmazonMJSKY_US_US",
    "AmazonMJSKY_US_US": "AmazonMJSKY_US_US",
    "浙捷": "kaifengzhejie_US",
    "kaifengzhejie_US": "kaifengzhejie_US",
    "贸易通": "maoyitong_US",
    "茂易通": "maoyitong_US",
    "maoyitong_US": "maoyitong_US",
    "行善": "xingshan_US",
    "行善店": "xingshan_US",
    "xingshan_US": "xingshan_US",
    "ruibao": "RUIBAO_US_US",
    "瑞宝": "RUIBAO_US_US",
}

# 明确不映射的标签
UNMAPPED_LABELS = frozenset({"2", "数字标签", "东玻"})

# 明确排除的店铺标签
EXCLUDED_SHOP_LABELS = frozenset({"禾逸钦"})

# 规范账号 -> Team
ACCOUNT_TEAM = {}
for _team, _members in TEAM_MEMBERS:
    for _acc in _members:
        ACCOUNT_TEAM[_acc] = _team

CANONICAL_ACCOUNTS = frozenset(ACCOUNT_TEAM.keys())

# 期望的 8 个账号（固定映射）
EXPECTED_ACCOUNTS = (
    "RUIBAO_US_US",
    "KornerHK_US",
    "southwest_US",
    "Wekas_US",
    "AmazonMJSKY_US_US",
    "kaifengzhejie_US",
    "maoyitong_US",
    "xingshan_US",
)

# 期望的三个 Team
EXPECTED_TEAMS = tuple(t for t, _m in TEAM_MEMBERS)

# T01 严格 token 边界（拉丁字母/数字边界）
_T01_TOKEN_RE = re.compile(r"T01(?![0-9])")
# 显示名：T01 后接非 ASCII 或分隔符，但不得是 T010
_T01_DISPLAY_RE = re.compile(r"T01(?![0-9])")


def _has_t01_token(text: str) -> bool:
    if not text:
        return False
    return bool(_T01_TOKEN_RE.search(text))


def _has_t01_display(text: str) -> bool:
    if not text:
        return False
    return bool(_T01_DISPLAY_RE.search(text))


def _is_t01(sku: str, display_name: str) -> bool:
    if _has_t01_token(sku):
        return True
    if _has_t01_display(display_name):
        return True
    return False


def _text(v) -> str:
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float)):
        return str(v).strip()
    return ""


def _canonical_account(label: str):
    s = _text(label)
    if not s:
        return None
    if s in UNMAPPED_LABELS:
        return None
    acc = ACCOUNT_ALIASES.get(s)
    if acc and acc in CANONICAL_ACCOUNTS:
        return acc
    return None


_DATE_FULL_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T].*)?$")
_DATE_SLASH_RE = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})(?:[ T].*)?$")


def _parse_date_month(v):
    """接受完整 YYYY-MM-DD / YYYY/M/D（可带时间）；返回 YYYY-MM。"""
    s = _text(v)
    if not s:
        return None
    m = _DATE_FULL_RE.match(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = _DATE_SLASH_RE.match(s)
        if not m:
            return None
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    try:
        datetime(y, mo, d)
    except ValueError:
        return None
    return "%04d-%02d" % (y, mo)


def _prev_month(month: str):
    y, m = int(month[:4]), int(month[5:7])
    if m == 1:
        return "%04d-12" % (y - 1)
    return "%04d-%02d" % (y, m - 1)


def _load_mapping(path):
    """读取关系 CSV，返回账户精确键及显式通用账户键的目标集合。"""
    rows = {}
    generic_rows = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError("mapping header missing")
        required = {"seller_sku", "account", "warehouse_sku", "qty"}
        if not required.issubset({(h or "").strip() for h in reader.fieldnames}):
            raise ValueError("mapping header invalid")
        for row in reader:
            seller = _text(row.get("seller_sku"))
            account = _text(row.get("account"))
            wh = _text(row.get("warehouse_sku"))
            if not seller or not account or not wh:
                continue
            if account == "所有账户":
                generic_rows.setdefault(seller, set()).add(wh)
                continue
            canon = _canonical_account(account)
            if not canon:
                continue
            rows.setdefault((canon, seller), set()).add(wh)
    # Explicit "所有账户" rules are available only as a fallback when the exact
    # store has no relationship. Never override a store-specific row.
    rows[None] = generic_rows
    return rows


def _build_sales_map(sales_snapshot):
    """从完整、成功的看板快照构建销量映射；结构缺失时拒绝生成售后率。

    保留显式 0 与正数；缺失月份不出现（视为未知，不当作 0）。
    """
    if not isinstance(sales_snapshot, dict) or sales_snapshot.get("ok") is not True:
        raise ValueError("sales snapshot unavailable")
    teams = sales_snapshot.get("teams")
    if not isinstance(teams, list) or {t.get("name") for t in teams if isinstance(t, dict)} != set(EXPECTED_TEAMS):
        raise ValueError("sales team coverage invalid")
    found_accounts = []
    for team in teams:
        if not isinstance(team, dict) or not isinstance(team.get("shops"), list):
            raise ValueError("sales team rows invalid")
        for shop in team["shops"]:
            if not isinstance(shop, dict) or not isinstance(shop.get("skus"), list):
                raise ValueError("sales shop rows invalid")
            found_accounts.append(_text(shop.get("account")))
    if len(found_accounts) != len(EXPECTED_ACCOUNTS) or set(found_accounts) != set(EXPECTED_ACCOUNTS):
        raise ValueError("sales account coverage invalid")

    out = {}
    for team in teams:
        shops = team["shops"]
        for shop in shops:
            account = _text(shop.get("account"))
            if account not in CANONICAL_ACCOUNTS:
                raise ValueError("sales account invalid")
            skus = shop["skus"]
            for sku_item in skus:
                if not isinstance(sku_item, dict):
                    raise ValueError("sales sku row invalid")
                sku = _text(sku_item.get("sku"))
                if not sku:
                    raise ValueError("sales sku missing")
                sold = sku_item.get("sold")
                if not isinstance(sold, dict):
                    raise ValueError("sales months missing")
                months = {}
                for mon, qty in sold.items():
                    if not isinstance(mon, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", mon):
                        raise ValueError("sales month invalid")
                    if qty is None:
                        continue
                    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 0:
                        raise ValueError("sales quantity invalid")
                    months[mon] = qty
                key = (account, sku)
                if key in out:
                    raise ValueError("duplicate sales sku")
                if months:
                    out[key] = months
    return out


def _shift_month(event_month, sales_months):
    """返回可用的最近月份（含当月），否则 None。

    sales_months 为 None 表示该 (account, sku) 完全无销量记录 -> 未知。
    sales_months 为 dict 时，缺失月份视为未知（不当作 0），
    显式 0 或正数才视为已确认。
    """
    if sales_months is None:
        return None
    cur = event_month
    while cur >= START_MONTH:
        if cur in sales_months:
            qty = sales_months[cur]
            if qty > 0:
                return cur
            # 显式 0：继续向前找
        cur = _prev_month(cur)
    return None


def _empty_result(fetched_at, diagnostics):
    return {
        "connected": False,
        "realtime": False,
        "message": "售后实时数据读取失败，售后率显示“—”，不按 0% 计算。",
        "source_as_of": fetched_at,
        "start_month": START_MONTH,
        "end_month": None,
        "summary": {
            "numerator": None,
            "denominator": None,
            "rate": None,
        },
        "total": {"numerator": None, "denominator": None, "rate": None},
        "teams": [],
        "stores": [],
        "store_month": [],
        "sku_store_month": [],
        "reasons": {
            "connected": False,
            "items": None,
            "subcategory_items": None,
            "message": "售后原因数据暂不可用；未生成占比。",
        },
        "diagnostics": diagnostics,
    }


def _rate(n, d):
    if n is None or d is None or d <= 0:
        return None
    return round(n * 100.0 / d, 4)


def _rate_entry(n, d):
    return {"numerator": n, "denominator": d, "rate": _rate(n, d)}


def _diag(level, code, message):
    return {"level": level, "code": code, "message": message}


def build_aftersale_live(token, app_token, pair_map, sales_snapshot,
                         http=None, mapping_path=None, fetched_at=None):
    """只读聚合售后实时数据。失败时返回 connected=False 的安全结构。"""
    diagnostics = []
    if fetched_at is None:
        fetched_at = datetime.now(UTC8).isoformat()

    if not isinstance(pair_map, dict):
        diagnostics.append(_diag("error", "pair_map_invalid",
                                 "配对表不可用，售后实时数据未接入。"))
        return _empty_result(fetched_at, diagnostics)

    # 1. 读取映射 CSV
    path = Path(mapping_path) if mapping_path is not None else DEFAULT_MAPPING_PATH
    try:
        relation = _load_mapping(path)
    except FileNotFoundError:
        diagnostics.append(_diag("error", "mapping_missing",
                                 "SKU 关系映射文件缺失，售后实时数据未接入。"))
        return _empty_result(fetched_at, diagnostics)
    except (OSError, ValueError, csv.Error):
        diagnostics.append(_diag("error", "mapping_invalid",
                                 "SKU 关系映射文件无法解析，售后实时数据未接入。"))
        return _empty_result(fetched_at, diagnostics)

    # 2. 读取两张售后表（先校验字段，再读记录）
    sources = (
        (AFTERSALE_TABLE_1, SHOP_FIELD_1),
        (AFTERSALE_TABLE_2, SHOP_FIELD_2),
    )
    all_records = []
    seen_ids = set()
    for table_id, shop_field in sources:
        # 2a. 字段元数据校验
        try:
            field_meta = fetch_fields(
                token, app_token, table_id, http=http,
                required_fields={name: 1 for name in REQUIRED_FIELDS + (shop_field,)},
            )
        except BoardError:
            diagnostics.append(_diag("error", "source_failed",
                                     "售后源表字段读取失败，售后实时数据未接入。"))
            return _empty_result(fetched_at, diagnostics)
        except Exception:
            diagnostics.append(_diag("error", "source_failed",
                                     "售后源表字段读取失败，售后实时数据未接入。"))
            return _empty_result(fetched_at, diagnostics)
        if not isinstance(field_meta, dict):
            diagnostics.append(_diag("error", "source_incomplete",
                                     "售后源表字段结构异常，售后实时数据未接入。"))
            return _empty_result(fetched_at, diagnostics)
        field_names = set(field_meta.keys())
        for req in REQUIRED_FIELDS:
            if req not in field_names:
                diagnostics.append(_diag("error", "field_missing",
                                         "售后源表缺少必需字段，售后实时数据未接入。"))
                return _empty_result(fetched_at, diagnostics)
        if shop_field not in field_names:
            diagnostics.append(_diag("error", "field_missing",
                                     "售后源表缺少店铺字段，售后实时数据未接入。"))
            return _empty_result(fetched_at, diagnostics)

        # 2b. 读取记录
        try:
            records, _page_count = fetch_records(token, app_token, table_id, http=http)
        except BoardError:
            diagnostics.append(_diag("error", "source_failed",
                                     "售后源表读取失败，售后实时数据未接入。"))
            return _empty_result(fetched_at, diagnostics)
        except Exception:
            diagnostics.append(_diag("error", "source_failed",
                                     "售后源表读取失败，售后实时数据未接入。"))
            return _empty_result(fetched_at, diagnostics)
        if not isinstance(records, list):
            diagnostics.append(_diag("error", "source_incomplete",
                                     "售后源表返回结构异常，售后实时数据未接入。"))
            return _empty_result(fetched_at, diagnostics)
        for rec in records:
            if not isinstance(rec, dict):
                diagnostics.append(_diag("error", "source_incomplete",
                                         "售后源表记录结构异常，售后实时数据未接入。"))
                return _empty_result(fetched_at, diagnostics)
            rid = _text(rec.get("record_id"))
            if not rid:
                diagnostics.append(_diag("error", "record_id_missing",
                                         "售后源表记录缺少唯一标识，售后实时数据未接入。"))
                return _empty_result(fetched_at, diagnostics)
            source_record_key = (table_id, rid)
            if source_record_key in seen_ids:
                diagnostics.append(_diag("error", "record_id_duplicate",
                                         "售后源表记录标识重复，售后实时数据未接入。"))
                return _empty_result(fetched_at, diagnostics)
            seen_ids.add(source_record_key)
            fields = rec.get("fields")
            if not isinstance(fields, dict):
                fields = {}
            all_records.append((fields, shop_field))

    # 3. 构建销量精确映射
    sales_map = _build_sales_map(sales_snapshot)

    # 4. 逐条处理
    num_by_store_month_sku = {}   # (account, month, sku) -> count
    num_by_store_sku = {}         # (account, sku) -> count
    reason_by_store_sku = {}      # (account, sku, category) -> count
    reason_by_store_sku_month = {}  # (account, sku, month, category) -> count
    reason_detail_counter = {}      # (account, sku, month, category, subcategory) -> count
    max_month = None

    diag_counts = {
        "blank_sku": 0,
        "unknown_shop": 0,
        "excluded_shop": 0,
        "t01_excluded": 0,
        "invalid_date": 0,
        "missing_year": 0,
        "before_coverage": 0,
        "invalid_row_fields": 0,
        "no_pair": 0,
        "ambiguous_pair": 0,
        "no_sales_month": 0,
        "unknown_sales_month": 0,
    }

    for fields, shop_field in all_records:
        shop_label = _text(fields.get(shop_field))
        sku = _text(fields.get("SKU"))
        eccang_sku = _text(fields.get("易仓sku"))
        raw_text = fields.get("原文")
        date_val = fields.get("日期")
        # Non-empty business values of unexpected composite types are not flattened or guessed.
        if any(fields.get(name) is not None and not isinstance(fields.get(name), (str, int, float, bool))
               for name in REQUIRED_FIELDS + (shop_field,)):
            diag_counts["invalid_row_fields"] += 1
            continue

        # Future records may contain only 易仓 SKU; that is sufficient for exact direct matching.
        if not sku and not eccang_sku:
            diag_counts["blank_sku"] += 1
            continue

        if shop_label in EXCLUDED_SHOP_LABELS:
            diag_counts["excluded_shop"] += 1
            continue

        account = _canonical_account(shop_label)
        if not account:
            diag_counts["unknown_shop"] += 1
            continue

        # 先解析并映射SKU，再按仓库SKU/权威配对名称排除T01。
        date_str = _text(date_val)
        if date_str and not re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T].*)?$", date_str):
            diag_counts["missing_year"] += 1
            continue
        event_month = _parse_date_month(date_val)
        if not event_month:
            diag_counts["invalid_date"] += 1
            continue
        if event_month < START_MONTH:
            diag_counts["before_coverage"] += 1
            continue

        # 匹配 SKU
        matched_sku = None
        if eccang_sku:
            if eccang_sku in pair_map:
                matched_sku = eccang_sku
            else:
                diag_counts["no_pair"] += 1
                continue
        else:
            if not sku:
                diag_counts["blank_sku"] += 1
                continue
            candidates = relation.get((account, sku))
            if not candidates:
                # The uploaded relation table has an explicit "所有账户" bucket.
                # Apply it only when unique after intersecting the authoritative
                # SKU pairing table; ambiguous/missing targets remain excluded.
                generic_map = relation.get(None, {})
                generic_candidates = generic_map.get(sku, set())
                paired_generic = [wh for wh in generic_candidates if wh in pair_map]
                if len(paired_generic) == 1:
                    candidates = set(paired_generic)
                elif len(paired_generic) > 1:
                    diag_counts["ambiguous_pair"] += 1
                    continue
                else:
                    candidates = set()
            if not candidates:
                # Some known seller-SKU codes explicitly encode the T01 family;
                # exclude only this literal family marker, not arbitrary text.
                if "T01" in sku.upper() and not re.search(r"T01\d", sku.upper()):
                    diag_counts["t01_excluded"] += 1
                else:
                    diag_counts["no_pair"] += 1
                continue
            paired = [wh for wh in candidates if wh in pair_map]
            if len(paired) > 1:
                diag_counts["ambiguous_pair"] += 1
                continue
            if len(paired) == 0:
                if "T01" in sku.upper() and not re.search(r"T01\d", sku.upper()):
                    diag_counts["t01_excluded"] += 1
                else:
                    diag_counts["no_pair"] += 1
                continue
            matched_sku = paired[0]

        # 再次检查 T01（针对匹配后的 SKU）
        matched_entry = pair_map.get(matched_sku)
        if matched_entry is not None:
            dn = _text(matched_entry.get("display_name"))
            if _is_t01(matched_sku, dn):
                diag_counts["t01_excluded"] += 1
                continue

        # 月份对齐：仅当销量分母已知（显式 0 或正数）才计入分子
        sales_months = sales_map.get((account, matched_sku))
        if sales_months is None:
            diag_counts["unknown_sales_month"] += 1
            continue
        assigned_month = _shift_month(event_month, sales_months)
        if not assigned_month:
            diag_counts["no_sales_month"] += 1
            continue

        # 记录
        key_sm = (account, assigned_month, matched_sku)
        num_by_store_month_sku[key_sm] = num_by_store_month_sku.get(key_sm, 0) + 1
        key_s = (account, matched_sku)
        num_by_store_sku[key_s] = num_by_store_sku.get(key_s, 0) + 1

        raw_text_str = _text(raw_text)
        category = _reason_category(raw_text_str) or "未注明原因"
        subcategory = reason_subcategory(category, raw_text_str)
        key_r = (account, matched_sku, category)
        reason_by_store_sku[key_r] = reason_by_store_sku.get(key_r, 0) + 1
        key_rm = (account, matched_sku, assigned_month, category)
        reason_by_store_sku_month[key_rm] = reason_by_store_sku_month.get(key_rm, 0) + 1
        key_sm_detail = (account, matched_sku, assigned_month, category, subcategory)
        reason_detail_counter[key_sm_detail] = reason_detail_counter.get(key_sm_detail, 0) + 1

        if max_month is None or assigned_month > max_month:
            max_month = assigned_month

    # 5. 分母：当前销量快照，排除 T01，仅 pair_map 内 SKU
    #    显式 0 与正数都计入分母（0 表示确认无销量，rate 为 null）
    den_by_store_month_sku = {}
    den_by_store_sku = {}
    for (account, sku), months in sales_map.items():
        if sku not in pair_map:
            continue
        entry = pair_map.get(sku) or {}
        dn = _text(entry.get("display_name"))
        if _is_t01(sku, dn):
            continue
        for mon, qty in months.items():
            if mon < START_MONTH:
                continue
            key_sm = (account, mon, sku)
            den_by_store_month_sku[key_sm] = den_by_store_month_sku.get(key_sm, 0) + qty
            key_s = (account, sku)
            den_by_store_sku[key_s] = den_by_store_sku.get(key_s, 0) + qty

    # 6. 汇总
    # stores：固定 8 个账号
    stores_out = []
    for account in EXPECTED_ACCOUNTS:
        n = sum(v for (a, _s), v in num_by_store_sku.items() if a == account)
        d = sum(v for (a, _s), v in den_by_store_sku.items() if a == account)
        entry = _rate_entry(n, d)
        entry["account"] = account
        entry["team"] = ACCOUNT_TEAM.get(account)
        stores_out.append(entry)

    # teams：固定三个 Team
    teams_out = []
    for team in EXPECTED_TEAMS:
        n = sum(s["numerator"] for s in stores_out if s.get("team") == team)
        d = sum(s["denominator"] for s in stores_out if s.get("team") == team)
        entry = _rate_entry(n, d)
        entry["name"] = team
        teams_out.append(entry)

    # store_month
    sm_keys = set()
    for (a, m, _s) in num_by_store_month_sku:
        sm_keys.add((a, m))
    for (a, m, _s) in den_by_store_month_sku:
        sm_keys.add((a, m))
    store_month_out = []
    for (account, month) in sorted(sm_keys):
        n = sum(v for (a, m, _s), v in num_by_store_month_sku.items()
                if a == account and m == month)
        d = sum(v for (a, m, _s), v in den_by_store_month_sku.items()
                if a == account and m == month)
        entry = _rate_entry(n, d)
        entry["account"] = account
        entry["month"] = month
        entry["team"] = ACCOUNT_TEAM.get(account)
        store_month_out.append(entry)

    # sku_store_month
    ssm_keys = set()
    for (a, m, s) in num_by_store_month_sku:
        ssm_keys.add((a, m, s))
    for (a, m, s) in den_by_store_month_sku:
        ssm_keys.add((a, m, s))
    sku_store_month_out = []
    for (account, month, sku) in sorted(ssm_keys):
        n = num_by_store_month_sku.get((account, month, sku), 0)
        d = den_by_store_month_sku.get((account, month, sku), 0)
        entry = _rate_entry(n, d)
        entry["account"] = account
        entry["month"] = month
        entry["sku"] = sku
        entry["team"] = ACCOUNT_TEAM.get(account)
        sku_store_month_out.append(entry)

    # reasons：按 account+sku+month+category 输出，含 share
    reason_totals = {}
    for (a, s, m, cat), cnt in reason_by_store_sku_month.items():
        reason_totals[(a, s, m)] = reason_totals.get((a, s, m), 0) + cnt
    reasons_items = []
    for (account, sku, month, category), cnt in sorted(reason_by_store_sku_month.items()):
        total = reason_totals.get((account, sku, month), 0)
        share = round(cnt * 100.0 / total, 4) if total > 0 else None
        reasons_items.append({
            "account": account,
            "sku": sku,
            "month": month,
            "category": category,
            "count": cnt,
            "share": share,
        })

    # subcategory_items: additive read-only child breakdown; share is percent of PARENT category
    subcategory_items = []
    for (account, sku, month, category, subcategory), cnt in sorted(reason_detail_counter.items()):
        parent_total = reason_by_store_sku_month.get((account, sku, month, category), 0)
        sub_share = round(cnt * 100.0 / parent_total, 4) if parent_total > 0 else None
        subcategory_items.append({
            "account": account,
            "sku": sku,
            "month": month,
            "category": category,
            "subcategory": subcategory,
            "count": cnt,
            "share": sub_share,
        })

    # summary
    total_n = sum(num_by_store_sku.values())
    total_d = sum(den_by_store_sku.values())
    summary = _rate_entry(total_n, total_d)

    # diagnostics（仅计数，不暴露值）
    for code, count in diag_counts.items():
        if count > 0:
            diagnostics.append(_diag(
                "warning", code,
                "售后记录排除计数：%s=%d" % (code, count),
            ))

    return {
        "connected": True,
        "realtime": True,
        "message": "售后实时数据来自多维表格，只读聚合。",
        "source_as_of": fetched_at,
        "start_month": START_MONTH,
        "end_month": max_month,
        "summary": summary,
        "total": summary,
        "source": "feishu_live_read_only",
        "source_tables": [AFTERSALE_TABLE_1, AFTERSALE_TABLE_2],
        "mapping_version": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        "coverage": {
            "start_month": START_MONTH,
            "end_month": max_month,
            "team_count": len(teams_out),
            "store_count": len(stores_out),
            "store_month_count": len(store_month_out),
            "sku_store_month_count": len(sku_store_month_out),
        },
        "teams": teams_out,
        "stores": stores_out,
        "store_month": store_month_out,
        "sku_store_month": sku_store_month_out,
        "reasons": {
            "connected": True,
            "items": reasons_items,
            "subcategory_items": subcategory_items,
            "message": "原因占比按当前SKU、店铺和月份展示；只返回聚合类别和数量。",
        },
        "diagnostics": diagnostics,
    }


def load(token, app_token, pair_map, sales_snapshot, http=None,
         mapping_path=None, fetched_at=None):
    """最小集成入口：调用 build_aftersale_live 并返回结果。

    失败时 fail-closed：返回 connected=False 的结构，不抛出异常。
    """
    try:
        return build_aftersale_live(
            token=token,
            app_token=app_token,
            pair_map=pair_map,
            sales_snapshot=sales_snapshot,
            http=http,
            mapping_path=mapping_path,
            fetched_at=fetched_at,
        )
    except Exception:
        return {
            "connected": False,
            "realtime": False,
            "message": "售后实时数据加载失败，售后率显示“—”，不按 0% 计算。",
            "source_as_of": fetched_at,
            "start_month": START_MONTH,
            "end_month": None,
            "summary": {"numerator": None, "denominator": None, "rate": None},
            "teams": [],
            "stores": [],
            "store_month": [],
            "sku_store_month": [],
            "reasons": {"connected": False, "items": None, "subcategory_items": None},
            "diagnostics": [{
                "level": "error",
                "code": "load_failed",
                "message": "售后实时数据加载失败，售后率显示“—”，不按 0% 计算。",
            }],
        }