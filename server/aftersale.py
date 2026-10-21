# -*- coding: utf-8 -*-
"""售后 VOC 分析管线（独立于评论文本分析）。

输入是**订单级明细表**（每行一笔订单），列大致为：
  · 产品（产品名 / ASIN / SKU / 型号）
  · 是否售后（是/否、退/换/仅退款、Return/Refund/1/0 等）
  · 售后原因（退换货原因、问题描述）
  · 子店铺（类型 / 店铺 / 店铺名 —— 亚马逊多店铺运营常见）

两种模式：
  · **明细模式**（detail）：能识别出「是否售后」列 → 总单数=行数，售后率=售后单/总单
  · **台账模式**（ledger）：只有「已发生售后」的登记台账，没有全量订单分母
    → 每行视作 1 条售后记录，**不计算售后率**（rate=None），看板会标注口径

本模块只做**聚合**，不做任何 LLM 文本标注：
  · 按 产品 聚合：总单数、售后单数、售后率
  · 按 子店铺 聚合：登记量、主要问题
  · 按 售后原因 聚合：分布占比
结果同时写入 result.json（供前端消费）与 report.md（供导出）。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import adapters
from .sku_merge import SkuGrouper

# 列名别名（小写匹配）：角色 -> 候选关键词
_COLUMN_ALIASES = {
    "product": ["产品", "商品", "product", "item", "asin", "sku", "型号", "名称", "title", "款式", "spu"],
    "flag": ["是否售后", "售后", "退换", "退换货", "是否退", "类型", "after", "return",
            "refund", "rma", "售后状态", "处理状态", "订单状态", "is_return", "是否退换",
            "具体事项", "事项", "服务类型", "处理类型"],
    "reason": ["原因", "售后原因", "退换原因", "理由", "reason", "cause", "问题描述", "备注", "说明",
               "内容要点", "具体事项", "事项", "客诉问题", "问题", "投诉内容"],
    "date": ["日期", "时间", "date", "order date", "下单", "created"],
    # 子店铺（亚马逊多店铺台账里叫「类型」或「店铺」）
    "shop": ["子店铺", "店铺", "店铺名", "分店", "类型", "shop", "store", "渠道", "品牌"],
}

# 非语义列（含这些词的一律不参与角色判定：截图/图片/邮箱/订单号…）
_NOISE_HINTS = ("截图", "图片", "照片", "附件", "链接", "网址", "邮箱", "邮件",
                "追踪号", "单号", "编号", "金额", "电话", "身份证")

# 标识列：绝不能当「是否售后」标志（订单号里含 1/0/退 等字符会误判）
_ID_LIKE = {"订单", "订单号", "订单编号", "order", "orderno", "orderid",
            "email", "邮箱", "邮箱地址", "客户邮箱"}

# 售后判定的正/负向关键词
_POS = ["退", "换", "售后", "return", "refund", "rma", "维修", "补发", "拒收", "是", "yes", "1", "true", "换货", "退货", "仅退款"]
_NEG = ["否", "no", "0", "false", "无", "正常", "非售后", "none", "完成", "已完结"]
# 明确「非售后」的复合词（要在 _POS 之前判定，但不影响「售后+对产品满意」这类组合）
_NEG_COMPOUND = ["对产品满意", "满意", "好评", "差评"]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip().lower())


def _is_noise(h: str) -> bool:
    """非语义列（含截图/图片/订单号等）不参与角色判定。"""
    n = _norm(h)
    if n in _ID_LIKE:
        return True
    return any(x in n for x in _NOISE_HINTS)


def _match_role(h: str, role: str) -> bool:
    """表头是否命中该角色的别名。"""
    hl = _norm(h)
    if not hl:
        return False
    for c in _COLUMN_ALIASES[role]:
        if c in hl:                      # 别名是表头的子串
            return True
    # 别名包含表头（短表头如 'asin'）—— 只在表头够长时才算，避免「类型」误命中「问题类型」
    if len(hl) >= 3:
        for c in _COLUMN_ALIASES[role]:
            if hl in c:
                return True
    return False


def _pick_columns(headers: list[str], role: str, exclude: set[str] | None = None) -> list[str]:
    """挑出该角色的所有候选列。"""
    skip = set(exclude or set())
    return [h for h in headers if h not in skip and not _is_noise(h) and _match_role(h, role)]


def _is_aftersale(val: str) -> bool | None:
    """判定是否售后。返回 None = 无法判定（未知取值，不当成非售后）。

    判定顺序很关键：
      非售后 / 不满意 / 差评  → False
      售后 / 退货 / 换货       → True（所以「售后+对产品满意」仍算售后）
      满意（无售后词）         → False
      是 / 否 / 正常 / 退 / 换 → True / False
    """
    v = _norm(val)
    if not v:
        return None
    # 1) 明确否定（含「非」前缀）
    if v.startswith("非") or "非售后" in v:
        return False
    # 2) 明确的售后动作词
    for w in ("售后", "退货", "换货", "退款", "补发", "维修", "return", "refund", "rma"):
        if w in v:
            return True
    # 3) 满意度类
    for w in _NEG_COMPOUND:
        if w in v:
            return False
    # 4) 通用否 / 正
    for neg in _NEG:
        if neg in v:
            return False
    for pos in _POS:
        if pos in v:
            return True
    return None


def _decidability(col: str, rows: list[dict]) -> tuple[int, int, int]:
    """某列作为「是否售后」标志的可信度。返回 (可判定数, 判为售后数, 有值数)。

    只统计**该列有值**的行——多 sheet 合并后前面的行可能来自没有这列的 sheet。
    """
    judged = 0
    yes = 0
    n = 0
    for r in rows:
        v = (r.get(col) or "").strip()
        if not v:
            continue
        n += 1
        d = _is_aftersale(v)
        if d is not None:
            judged += 1
            if d:
                yes += 1
        if n >= 300:
            break
    return judged, yes, n


def _first_nonempty(row: dict, cols: list[str]) -> str:
    """从多列里取第一个非空值。"""
    for c in cols:
        if not c:
            continue
        v = (row.get(c) or "").strip()
        if v:
            return v
    return ""


# 售后原因归一化：台账里的「内容要点」是自由文本（可达数百种），
# 归成大类才能看清问题结构。顺序即优先级。
_REASON_CATEGORIES = [
    ("破损损坏", ["破损", "损坏", "裂", "断", "散架", "划", "凹陷", "压坏", "摔", "掉漆", "变形"]),
    ("尺寸规格", ["尺寸", "大小", "偏小", "偏大", "规格", "型号", "不符", "小了", "大了", "短了"]),
    ("缺件漏发", ["丢件", "缺少", "缺件", "漏发", "少", "没送", "未送", "缺"]),
    ("功能故障", ["卡扣", "难关", "异响", "晃", "无法", "失灵", "故障", "不灵", "锁", "连接",
                  "按钮", "遥控", "升降", "折叠", "转不动", "卡住"]),
    ("物流配送", ["物流", "快递", "配送", "运输", "签收", "派送", "送货", "包装", "外箱"]),
    ("安装使用", ["安装", "组装", "说明书", "使用", "装不上", "不会装", "操作", "教程"]),
    ("要求退货", ["退货", "退款", "退单", "不要了", "不想要", "退掉"]),
    ("要求换货", ["换货", "换一个", "换新的", "换一件"]),
]


def _reason_category(text: str) -> str:
    """把自由文本的售后原因归到大类。"""
    t = _norm(text)
    if not t:
        return "未注明原因"
    for name, kws in _REASON_CATEGORIES:
        for kw in kws:
            if kw in t:
                return name
    return "其他问题"


def _classify_row(row: dict, cols: dict) -> tuple[str, bool | None]:
    """返回 (product, is_aftersale)。"""
    product = _first_nonempty(row, cols.get("product") or [])
    product = product or "未标注产品"
    flag = _first_nonempty(row, cols.get("flag") or [])
    return product, _is_aftersale(flag)


def run_aftersale(*, files: list[Path], platform: str, category: str,
                  out_dir: Path, progress=None, data_dir=None) -> dict:
    def p(pct: int, msg: str) -> None:
        if progress:
            progress(pct, msg)

    t0 = time.time()
    grouper = SkuGrouper(data_dir)
    p(5, "读取订单明细…")
    all_rows: list[dict] = []
    sources: list[str] = []
    for f in files:
        rows, src = adapters.read_table(f, multi_sheet=True)
        if rows:
            all_rows.extend(rows)
            sources.append(src)
    if not all_rows:
        raise ValueError("数据文件中没有可用记录。请确认文件含表头与数据行。")

    headers: list[str] = []
    for r in all_rows:
        for k in r.keys():
            if k not in headers:
                headers.append(k)

    # ---- 列角色分配 ----------------------------------------------------
    # 一个列只能承担一个角色，且要按「售后语义可判定率」择优：
    #   「类型」列在亚马逊台账里装的是**店铺名**（行善/香港店/威卡斯…），
    #   若直接当售后标志会把 800+ 行全判成"非售后"。
    flag_cands = _pick_columns(headers, "flag")
    scored: list[tuple[float, str, int, int]] = []
    for c in flag_cands:
        judged, yes, n = _decidability(c, all_rows)
        if n < 3:
            continue
        ratio = judged / n
        # 判为售后的比例也要有区分度：全是"是"或全是"否"都不能区分
        spread = min(yes, n - yes) / n
        scored.append((ratio * 0.6 + spread * 0.4, c, judged, n))
    scored.sort(reverse=True)
    flag_primary = ""
    flag_trusted = False
    if scored:
        best_score, best_col, best_judged, best_n = scored[0]
        # 至少 30% 的取值能判出语义，且有区分度 → 认定为可靠的售后标志列
        if best_judged >= 3 and best_judged / max(1, best_n) >= 0.3:
            flag_primary = best_col
            flag_trusted = True
    # 覆盖率：该列有值的行占全部行数的比例。偏低说明这份数据里只有部分 sheet
    # 才有这列（如多 sheet 台账合并），不能当成"每行都有售后标志"。
    flag_cov = 0.0
    if flag_primary:
        with_flag = sum(1 for r in all_rows if (r.get(flag_primary) or "").strip())
        flag_cov = with_flag / max(1, len(all_rows))

    used = {flag_primary} if flag_primary else set()
    reason_cols = _pick_columns(headers, "reason", exclude=used)
    used |= set(reason_cols)
    shop_cols = _pick_columns(headers, "shop", exclude=used)
    used |= set(shop_cols)
    product_cols = _pick_columns(headers, "product", exclude=used)
    if not product_cols:
        raise ValueError(
            "未能识别「产品」列。\n"
            "请确认表头包含类似：产品名 / ASIN / SKU / 型号 之类列。\n"
            f"当前识别到的列：{headers[:16]}")

    cols = {
        "product": product_cols,
        "flag": [flag_primary] if flag_primary else [],
        "reason": reason_cols,
        "shop": shop_cols,
    }

    # ---- 判定模式 -------------------------------------------------------
    # 明细模式（可算率）：有**可靠**的 flag 列，且它几乎覆盖了所有行
    #   —— 覆盖不全时（如多 sheet 台账只有部分 sheet 有这列），
    #      "没填 flag 值" 不等于 "不是售后"，算出来的率会误导。
    # 台账模式（不算率）：每行视作 1 条已发生的售后记录。
    #   若存在可靠的 flag 列，仍用它剔除**明确**标注为非售后的行。
    mode = "detail" if (flag_trusted and flag_cov >= 0.9) else "ledger"
    warnings: list[str] = []
    if mode == "ledger":
        if not flag_trusted and flag_cands:
            warnings.append(
                "未找到可靠的「是否售后」列（候选列：%s），已按**台账模式**处理："
                "只统计已发生的售后记录，不计算售后率。"
                % "、".join(flag_cands[:4]))
        elif flag_trusted:
            warnings.append(
                "「是否售后」列（%s）只覆盖了 %.0f%% 的记录，"
                "说明该表并非每行都登记了售后状态 —— 已按**台账模式**处理："
                "只统计已发生的售后记录，不计算售后率。"
                % (flag_primary, flag_cov * 100))

    if mode == "ledger":
        p(30, "按台账模式聚合（仅统计已发生售后，不计算售后率）…")
    else:
        p(30, "聚合各产品售后率与原因分布…")

    prod: dict[str, dict] = {}
    reasons: dict[str, int] = {}
    cats: dict[str, int] = {}
    total = 0
    aftersale = 0
    unknown = 0
    filtered_out = 0
    for row in all_rows:
        raw_product, is_as = _classify_row(row, cols)
        # 高度相似的 SKU 合并成产品族（如 MR221118BR-WKS / MR221118-BR-MJT-BJ-1）
        product = grouper.family(raw_product)
        if mode == "detail":
            # 明细模式：flag 为空/无法判定 = **未知**，既不计入分子也不计入分母
            if is_as is None:
                unknown += 1
                continue
            is_as = bool(is_as)
        else:
            # 台账模式：只剔除**明确**判定为非售后的行；无法判定的行照常计入
            if is_as is False:
                filtered_out += 1
                continue
            is_as = True
        total += 1
        if is_as:
            aftersale += 1
            reason = _first_nonempty(row, reason_cols)
            reason = reason or "未注明原因"
            reason = reason[:40]
            reasons[reason] = reasons.get(reason, 0) + 1
            cat = _reason_category(reason)
            cats[cat] = cats.get(cat, 0) + 1
        bucket = prod.setdefault(product, {"total": 0, "aftersale": 0, "cats": {},
                                           "skus": {}, "raw": raw_product})
        bucket["total"] += 1
        bucket["skus"][raw_product] = bucket["skus"].get(raw_product, 0) + 1
        if is_as:
            bucket["aftersale"] += 1
            bucket["cats"][cat] = bucket["cats"].get(cat, 0) + 1

    # 台账模式下产品级「售后率」恒为 1.0（每条记录都是售后），会误导 → 置 None
    products = sorted(
        ({"product": k, "total": v["total"], "aftersale": v["aftersale"],
          "top_category": (sorted(v.get("cats", {}).items(), key=lambda x: -x[1]) or [("", 0)])[0][0],
          "sku_count": len(v.get("skus", {})),
          "members": [{"sku": sk, "count": c}
                      for sk, c in sorted(v.get("skus", {}).items(), key=lambda x: -x[1])],
          "rate": (round(v["aftersale"] / v["total"], 4)
                   if v["total"] and mode == "detail" else None)}
         for k, v in prod.items()),
        key=lambda x: (-(x["rate"] or 0), -x["total"]))
    reason_list = sorted(({"reason": k, "count": v} for k, v in reasons.items()),
                         key=lambda x: -x["count"])
    rsum = sum(r["count"] for r in reason_list) or 1
    for r in reason_list:
        r["pct"] = round(r["count"] / rsum, 4)
        r["category"] = _reason_category(r["reason"])
    # 归一化后的大类分布
    csum = sum(cats.values()) or 1
    category_list = sorted(({"category": k, "count": v} for k, v in cats.items()),
                           key=lambda x: -x["count"])
    for c_ in category_list:
        c_["pct"] = round(c_["count"] / csum, 4)

    # 缺分母 → 售后率为 None（台账模式）
    overall_rate = round(aftersale / total, 4) if (total and mode == "detail") else None
    p(85, "生成售后洞察报告…")

    if mode == "ledger":
        headline = f"台账模式 · {total} 条售后登记 · 不计算售后率（缺全量订单分母）"
    else:
        headline = f"完成 · {total} 单 · 售后率 {overall_rate:.1%}"
    p(100, headline)

    result = {
        "report_type": "aftersale",
        "platform": platform,
        "category": category or "售后分析",
        "mode": mode,
        "overview": {
            "units_analyzed": total,
            "comments_total": 0,
            "tag_hits": aftersale,
            "avg_rating": None,
            "purchase": "",
            "date_min": "", "date_max": "",
        },
        "aftersale": {
            "platform": platform,
            "mode": mode,
            "total_orders": total,
            "aftersale_orders": aftersale,
            "overall_rate": overall_rate,
            "products": products,
            "reasons": reason_list,
            "categories": category_list,
            "filtered_out": filtered_out,
            "unknown": unknown,
        },
        "sources": sources,
        "warnings": warnings,
        "params": {"mode": mode,
                   "columns": {"product": product_cols, "flag": cols["flag"],
                               "reason": reason_cols, "shop": shop_cols}},
    }
    result["elapsed_sec"] = round(time.time() - t0, 2)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(_md(result), encoding="utf-8")
    return result


def _md(r: dict) -> str:
    a = r["aftersale"]
    ledger = a.get("mode") == "ledger"
    lines = [
        f"# 售后 VOC 分析 · {r['platform']}",
        "",
    ]
    if ledger:
        lines += [
            "> **口径说明：台账模式**。本表只登记了已发生售后的记录，"
            "没有全量订单数做分母，因此**不计算售后率**。",
            "",
            f"- 售后登记条数：**{a['total_orders']}**",
            f"- 涉及产品数：**{len(a['products'])}**",
        ]
    else:
        lines += [
            f"- 总订单数：**{a['total_orders']}**",
            f"- 售后订单数：**{a['aftersale_orders']}**",
            f"- 整体售后率：**{a['overall_rate']:.1%}**",
        ]
    if a.get("filtered_out"):
        lines.append(f"- 已剔除非售后记录：**{a['filtered_out']}** 条")
    lines += ["", "## 各产品售后率", "",
              "| 产品 | 总单数 | 售后单数 | 售后率 |", "|---|---|---|---|"]
    for p_ in a["products"]:
        if ledger:
            lines.append(f"| {p_['product']} | {p_['total']} | {p_['aftersale']} | — |")
        else:
            lines.append(f"| {p_['product']} | {p_['total']} | {p_['aftersale']} | {p_['rate']:.1%} |")
    if a.get("products"):
        lines += ["", "## SKU 合并明细", "",
                  "| 产品族 | 合并 SKU 数 | 售后条数 | 成员 SKU |", "|---|---|---|---|"]
        for p_ in a["products"]:
            if p_.get("sku_count", 1) <= 1:
                continue
            mem = ", ".join(m["sku"] for m in (p_.get("members") or [])[:6])
            if p_.get("sku_count", 0) > 6:
                mem += " …"
            lines.append(f"| {p_['product']} | {p_['sku_count']} | {p_['aftersale']} | {mem} |")
    lines += ["", "## 售后原因分布", ""]
    for rs in a["reasons"]:
        lines.append(f"- {rs['reason']}：{rs['count']}（{rs['pct']:.1%}）")
    return "\n".join(lines)
