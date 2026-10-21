# -*- coding: utf-8 -*-
"""售后 VOC 分析管线（独立于评论文本分析）。

输入是**订单级明细表**（每行一笔订单），列大致为：
  · 产品（产品名 / ASIN / SKU / 型号）
  · 是否售后（是/否、退/换/仅退款、Return/Refund/1/0 等）
  · 售后原因（退换货原因、问题描述）

本模块只做**聚合**，不做任何 LLM 文本标注：
  · 按 产品 聚合：总单数、售后单数、售后率
  · 按 售后原因 聚合：分布占比
结果同时写入 result.json（供前端消费）与 report.md（供导出）。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import adapters

# 列名别名（小写匹配）：角色 -> 候选关键词
_COLUMN_ALIASES = {
    "product": ["产品", "商品", "product", "item", "asin", "sku", "型号", "名称", "title", "款式", "spu"],
    "flag": ["是否售后", "售后", "退换", "退换货", "是否退", "类型", "after", "return",
            "refund", "rma", "售后状态", "处理状态", "订单状态", "is_return", "是否退换"],
    "reason": ["原因", "售后原因", "退换原因", "理由", "reason", "cause", "问题描述", "备注", "说明"],
    "date": ["日期", "时间", "date", "order date", "下单", "created"],
}

# 售后判定的正/负向关键词
_POS = ["退", "换", "售后", "return", "refund", "rma", "维修", "补发", "拒收", "是", "yes", "1", "true", "换货", "退货", "仅退款"]
_NEG = ["否", "no", "0", "false", "无", "正常", "非售后", "none", "完成", "已完结"]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip().lower())


def _pick_column(headers: list[str], role: str) -> str | None:
    """从表头里按别名挑一个最像的列为某角色。"""
    low = [_norm(h) for h in headers]
    cands = _COLUMN_ALIASES[role]
    # 1) 完全包含某别名
    for h, hl in zip(headers, low):
        for c in cands:
            if c in hl:
                return h
    # 2) 别名包含表头（短表头，例如 'asin'）
    for h, hl in zip(headers, low):
        if not hl:
            continue
        for c in cands:
            if hl in c:
                return h
    return None


def _is_aftersale(val: str) -> bool | None:
    """返回 None 表示无法判定（视作非售后，但不计入原因）。"""
    v = _norm(val)
    if not v:
        return None
    for neg in _NEG:
        if neg in v:
            return False
    for pos in _POS:
        if pos in v:
            return True
    return False


def _classify_row(row: dict, cols: dict) -> tuple[str | None, bool | None]:
    """返回 (product, is_aftersale)。"""
    product = (row.get(cols["product"]) or "").strip() if cols.get("product") else ""
    product = product or "未标注产品"
    flag = (row.get(cols["flag"]) or "") if cols.get("flag") else ""
    return product, _is_aftersale(flag)


def run_aftersale(*, files: list[Path], platform: str, category: str,
                  out_dir: Path, progress=None) -> dict:
    def p(pct: int, msg: str) -> None:
        if progress:
            progress(pct, msg)

    t0 = time.time()
    p(5, "读取订单明细…")
    all_rows: list[dict] = []
    sources: list[str] = []
    for f in files:
        rows, src = adapters.read_table(f)
        if rows:
            all_rows.extend(rows)
            sources.append(src)
    if not all_rows:
        raise ValueError("数据文件中没有可用记录。请确认文件含表头与数据行。")

    headers = list(all_rows[0].keys())
    cols = {
        "product": _pick_column(headers, "product"),
        "flag": _pick_column(headers, "flag"),
        "reason": _pick_column(headers, "reason"),
    }
    if not cols["product"] or not cols["flag"]:
        raise ValueError(
            "未能识别「产品」或「是否售后」列。\n"
            "请确认表头包含类似：产品名/ASIN/SKU，以及 是否售后/退换货/订单状态 之类列。\n"
            f"当前识别到的列：{headers[:16]}")
    p(30, "聚合各产品售后率与原因分布…")

    prod: dict[str, dict] = {}
    reasons: dict[str, int] = {}
    total = 0
    aftersale = 0
    for row in all_rows:
        product, is_as = _classify_row(row, cols)
        total += 1
        if is_as:
            aftersale += 1
            reason = (row.get(cols["reason"]) or "").strip() if cols.get("reason") else ""
            reason = reason or "未注明原因"
            reason = reason[:40]
            reasons[reason] = reasons.get(reason, 0) + 1
        bucket = prod.setdefault(product, {"total": 0, "aftersale": 0})
        bucket["total"] += 1
        if is_as:
            bucket["aftersale"] += 1

    products = sorted(
        ({"product": k, "total": v["total"], "aftersale": v["aftersale"],
          "rate": round(v["aftersale"] / v["total"], 4) if v["total"] else 0.0}
         for k, v in prod.items()),
        key=lambda x: (-x["rate"], -x["total"]))
    reason_list = sorted(({"reason": k, "count": v} for k, v in reasons.items()),
                         key=lambda x: -x["count"])
    rsum = sum(r["count"] for r in reason_list) or 1
    for r in reason_list:
        r["pct"] = round(r["count"] / rsum, 4)

    overall_rate = round(aftersale / total, 4) if total else 0.0
    p(85, "生成售后洞察报告…")

    result = {
        "report_type": "aftersale",
        "platform": platform,
        "category": category or "售后分析",
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
            "total_orders": total,
            "aftersale_orders": aftersale,
            "overall_rate": overall_rate,
            "products": products,
            "reasons": reason_list,
        },
        "sources": sources,
        "warnings": [],
        "params": {"mode": "aftersale", "columns": {k: (v or "") for k, v in cols.items()}},
    }
    result["elapsed_sec"] = round(time.time() - t0, 2)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(_md(result), encoding="utf-8")
    p(100, f"售后分析完成 · {total} 单 · 售后率 {overall_rate:.1%}")
    return result


def _md(r: dict) -> str:
    a = r["aftersale"]
    lines = [
        f"# 售后 VOC 分析 · {r['platform']}",
        "",
        f"- 总订单数：**{a['total_orders']}**",
        f"- 售后订单数：**{a['aftersale_orders']}**",
        f"- 整体售后率：**{a['overall_rate']:.1%}**",
        "",
        "## 各产品售后率",
        "",
        "| 产品 | 总单数 | 售后单数 | 售后率 |",
        "|---|---|---|---|",
    ]
    for p_ in a["products"]:
        lines.append(f"| {p_['product']} | {p_['total']} | {p_['aftersale']} | {p_['rate']:.1%} |")
    lines += ["", "## 售后原因分布", ""]
    for rs in a["reasons"]:
        lines.append(f"- {rs['reason']}：{rs['count']}（{rs['pct']:.1%}）")
    return "\n".join(lines)
