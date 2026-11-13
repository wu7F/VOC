"""Aggregate exact report rate inputs without reconstructing them from rounded rates."""
from __future__ import annotations


def report_rate(aftersale: dict, product: dict | None = None) -> dict:
    """Detail uses its table; ledger requires verified external rate components.

    Old ledger reports have no eligible-event count or denominator identities.
    Their registration counts remain usable, but their rates cannot be merged.
    """
    row = product if product is not None else aftersale
    if aftersale.get("mode", "detail") == "detail":
        return {"basis": "detail",
                "numerator": row.get("aftersale" if product is not None else "aftersale_orders", 0),
                "denominator": row.get("total" if product is not None else "total_orders", 0),
                "denominators": []}

    sales = aftersale.get("sales") or {}
    if sales.get("mapping_warning"):
        return {"basis": "none", "warning": sales["mapping_warning"]}
    basis = aftersale.get("rate_basis")
    if basis not in ("eccang_qty", "feishu_qty") or sales.get("complete") is not True:
        return {"basis": "none", "warning": "销量分母未确认完整，暂不计算汇总率。"}
    numerator = row.get("rate_numerator") if product is not None else sales.get("numerator_used")
    denominators = row.get("rate_denominators") if product is not None else sales.get("denominators")
    denominator = row.get("sales_qty") if product is not None else sales.get("qty_used")
    if numerator is None or denominators is None or denominator is None:
        return {"basis": "none", "warning": "历史报告缺少有效分子或分母明细，需重新分析后计算汇总率。"}
    if sum(d["qty"] for d in denominators) != denominator:
        return {"basis": "none", "warning": "报告的分母明细不一致，暂不计算汇总率。"}
    if basis == "feishu_qty" and (not sales.get("source_id") or
            any(d.get("source_id") != sales["source_id"] for d in denominators)):
        return {"basis": "none", "warning": "销量表报告缺少来源标识，需重新分析后计算汇总率。"}
    return {"basis": basis, "numerator": numerator,
            "denominator": denominator, "denominators": denominators}


def combine_rates(parts: list[dict]) -> dict:
    """Combine disjoint exact inputs; overlapping snapshots cannot prove event uniqueness."""
    result = {"rate": None, "sales_qty": 0, "rate_warning": ""}
    if not parts:
        return result
    invalid = next((p for p in parts if p["basis"] == "none"), None)
    if invalid:
        result["rate_warning"] = invalid["warning"]
        return result
    bases = {p["basis"] for p in parts}
    if len(bases) != 1:
        result["rate_warning"] = "表内订单、易仓销量与飞书销量表口径不同，暂不计算合并率。"
        return result
    external = bool(bases & {"eccang_qty", "feishu_qty"})
    if external:
        seen = set()
        for p in parts:
            for d in p["denominators"]:
                key = (p["basis"], d.get("source_id", ""), d["account"], d["month"])
                if key in seen:
                    result["rate_warning"] = "报告包含重叠账号月份，无法确认售后事件是否重复，暂不计算合并率。"
                    return result
                seen.add(key)
    denominator = sum(p["denominator"] for p in parts)
    if external:
        result["sales_qty"] = denominator
    if denominator:
        result["rate"] = round(sum(p["numerator"] for p in parts) / denominator, 4)
    return result
