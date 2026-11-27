# -*- coding: utf-8 -*-
"""Fixed approved warehouse component alias merge + minimum aggregation helper.

Audit mapping source:
  file_id: 3ad55f539cd2
  sha256: 177e237fbc3cc2a125dedda69f2fb8be76757cfbb9606ab5f3684cb45863d2c4
No runtime workbook dependency.
"""

from __future__ import annotations
from . import shop_filter

# Authoritative fixed mapping. Do not infer from suffixes or descriptions.
COMPONENT_ALIASES: dict[str, str] = {
    "LAYJMZP8018CR-2R": "LAYJMZP8018CR-2L",
    "LAYJMZPG02CR-2R": "LAYJMZPG02CR-2L",
    "LAYJMZPG01LG-2R": "LAYJMZPG01LG-2L",
    "LAYJMZPG02CR-3R": "LAYJMZPG02CR-3L",
    "LAYJMZPG02CR-3M": "LAYJMZPG02CR-3L",
    "LAYJMZP8018CR-3M": "LAYJMZP8018CR-3L",
    "LAYJMZP8018CR-3R": "LAYJMZP8018CR-3L",
    "LAYJMZP240458LG-2R": "LAYJMZP240458LG-2L",
    "LAYJCR8018DG-2R": "LAYJMZP8018DG-2L",
    "LAYJCR8018DG-2L": "LAYJMZP8018DG-2L",
    "LAYJMZPG02CR-BACKREST": "LAYJMZPG02CR",
    "LAYJMZPG01LG-3M": "LAYJMZPG01LG-3L",
    "LAYJMZPG01LG-3R": "LAYJMZPG01LG-3L",
    "LAYJMZP8018DG-3R": "LAYJMZP8018DG-3L",
    "LAYJMZP8018DG-3M": "LAYJMZP8018DG-3L",
    "LAYJMZPG02CR-2RIGHT": "LAYJMZPG02CR-2L",
    "LAYJMZPG01CR-2R": "LAYJMZPG01CR-2L",
    "LAYJMZPG02DG-BACKREST": "LAYJMZPG02DG",
    "LAYJMZP8018BK-2R": "LAYJMZP8018BK-2L",
    "LAYJMZP0458LG-2KB": "LAYJMZP240458LG-2L",
    "LAYJMZP0458LG-2LT": "LAYJMZP240458LG-2L",
    "LAYJMZP0458LG-2RT": "LAYJMZP240458LG-2L",
    "LAYJMZPG02DG-2RIGHT": "LAYJMZPG02DG-2L",
    "LAYJMZP8018DG-2R": "LAYJMZP8018DG-2L",
    "LAYJMZPG02CR-3MIDDLE": "LAYJMZPG02CR-3L",
    "LAYJMZPG02CR-3RIGHT": "LAYJMZPG02CR-3L",
    "LAYJMZP8018BK-3M": "LAYJMZP8018BK-3L",
    "LAYJMZP8018BK-3R": "LAYJMZP8018BK-3L",
    "LAYJMZP202641DBR-2RT": "LAYJMZP202641DBR-2LT",
    "LAYJMZP202641DBR-AR": "LAYJMZP202641DBR-2LT",
    "LAYJMZPG02DG-3M": "LAYJMZPG02DG-3L",
    "LAYJMZPG02DG-3R": "LAYJMZPG02DG-3L",
    "LAYJMZPG02DG-CJ": "LAYJMZPG02DG-LT",
    "LAYJMZPG02DG-ME1": "LAYJMZPG02DG-LT",
    "LAYJMZPG02DG-ME2": "LAYJMZPG02DG-LT",
    "LAYJMZPG02DG-RT": "LAYJMZPG02DG-LT",
    "LAYJMZPG02DG-ZJDZ": "LAYJMZPG02DG-LT",
    "LAYJMZPG02DG-ZJKB": "LAYJMZPG02DG-LT",
    "LAYJMZPG02DG-2LT": "LAYJMZPG02DG-2L",
    "LAYJMZPG02DG-2MT": "LAYJMZPG02DG-2L",
    "LAYJMZPG02DG-2RT": "LAYJMZPG02DG-2L",
    "LAYJMZPG02DG-3MT": "LAYJMZPG02DG-3L",
    "LAYJMZPG02DG-CJT": "LAYJMZPG02DG-LT",
    "LAYJMZPG02DG-KBT": "LAYJMZPG02DG-LT",
    "LAYJMZPG02DG-ZJDZT": "LAYJMZPG02DG-LT",
    "LAYJMZPG02DG-ZJKBT": "LAYJMZPG02DG-LT",
    "LAYJMZPG01CR-3M": "LAYJMZPG01CR-3L",
    "LAYJMZPG01CR-3R": "LAYJMZPG01CR-3L",
    "LAYJMZPG02DG-3MIDDLE": "LAYJMZPG02DG-3L",
    "LAYJMZPG02DG-3RIGHT": "LAYJMZPG02DG-3L",
    "LAYJMZP0458LG-3KB": "LAYJMZP0458LG-3MT",
    "LAYJMZP0458LG-3LT": "LAYJMZP0458LG-3MT",
    "LAYJMZP0458LG-3RT": "LAYJMZP0458LG-3MT",
    "LAYJHLR8005DG-CJ": "LAYJHLR8005DG-LT",
    "LAYJHLR8005DG-ME1": "LAYJHLR8005DG-LT",
    "LAYJHLR8005DG-ME2": "LAYJHLR8005DG-LT",
    "LAYJHLR8005DG-RT": "LAYJHLR8005DG-LT",
    "LAYJHLR8005DG-ZJDZ": "LAYJHLR8005DG-LT",
    "LAYJHLR8005DG-ZJKB": "LAYJHLR8005DG-LT",
}


# Explicit fixed required-component subsets for approved single-version products.
# Partial local coverage only; pending products keep observed-only minimum behavior.
REQUIRED_COMPONENTS: dict[str, tuple[str, ...]] = {
    "LAYJMZP8018CR-2L": ("LAYJMZP8018CR-2L", "LAYJMZP8018CR-2R"),
    "LAYJMZP8018BK-2L": ("LAYJMZP8018BK-2L", "LAYJMZP8018BK-2R"),
    "LAYJMZP8018CR-3L": ("LAYJMZP8018CR-3L", "LAYJMZP8018CR-3M", "LAYJMZP8018CR-3R"),
    "LAYJMZP8018BK-3L": ("LAYJMZP8018BK-3L", "LAYJMZP8018BK-3M", "LAYJMZP8018BK-3R"),
    "LAYJMZP8018DG-3L": ("LAYJMZP8018DG-3L", "LAYJMZP8018DG-3M", "LAYJMZP8018DG-3R"),
    "LAYJMZPG01CR-2L": ("LAYJMZPG01CR-2L", "LAYJMZPG01CR-2R"),
    "LAYJMZPG01CR-3L": ("LAYJMZPG01CR-3L", "LAYJMZPG01CR-3M", "LAYJMZPG01CR-3R"),
    "LAYJMZPG01LG-2L": ("LAYJMZPG01LG-2L", "LAYJMZPG01LG-2R"),
    "LAYJMZPG01LG-3L": ("LAYJMZPG01LG-3L", "LAYJMZPG01LG-3M", "LAYJMZPG01LG-3R"),
    "LAYJMZP0458LG-3MT": ("LAYJMZP0458LG-3MT", "LAYJMZP0458LG-3LT", "LAYJMZP0458LG-3RT", "LAYJMZP0458LG-3KB"),
    "LAYJHLR8005DG-LT": ("LAYJHLR8005DG-LT", "LAYJHLR8005DG-ME1", "LAYJHLR8005DG-ZJKB", "LAYJHLR8005DG-ZJDZ", "LAYJHLR8005DG-ME2", "LAYJHLR8005DG-RT", "LAYJHLR8005DG-CJ"),
}


def merge_component_rows(rows: list[dict], pair_map: dict | None, diagnostics: list[dict]) -> list[dict]:
    """Return normalized rows with admitted aliases merged to canonical products by minimum quantity.

    Only component rows whose canonical target exists in pair_map are merged.
    Unrelated rows pass through unchanged (copied). No missing-component synthesis.
    For targets in REQUIRED_COMPONENTS, an observed group with any required original
    SKU absent yields qty 0; invalid/duplicate observations still take precedence.
    """
    if pair_map is None:
        return [dict(r) for r in rows]

    active_targets = {target for target in COMPONENT_ALIASES.values() if target in pair_map}
    if not active_targets:
        return [dict(r) for r in rows]

    groups: dict[tuple, dict[str, list[dict]]] = {}
    unrelated: list[dict] = []
    for r in rows:
        sku = r.get("sku")
        target = COMPONENT_ALIASES.get(sku)
        if target and target in active_targets:
            canonical = target
        elif sku in active_targets:
            canonical = sku
        else:
            unrelated.append(dict(r))
            continue

        key = (shop_filter.norm(r.get("account") or ""), canonical, r.get("month"))
        by_orig = groups.setdefault(key, {})
        by_orig.setdefault(sku, []).append(r)

    out: list[dict] = []
    for (account, canonical, month), by_orig in groups.items():
        qty_values: list[int] = []
        invalid = False
        for orig_sku, entries in by_orig.items():
            if len(entries) != 1:
                invalid = True
                diagnostics.append({
                    "level": "error",
                    "code": "duplicate_key",
                    "message": "account=%s month=%s SKU=%s 重复 %d 行，该月置空。"
                               % (account, month, orig_sku, len(entries)),
                })
            else:
                entry = entries[0]
                if entry.get("invalid") or entry.get("qty") is None:
                    invalid = True
                else:
                    qty_values.append(entry.get("qty"))

        first_list = next(iter(by_orig.values()))
        base = first_list[0]
        merged = dict(base)
        merged["sku"] = canonical
        if invalid or not qty_values:
            merged["qty"] = None
            merged["invalid"] = True
        else:
            required = REQUIRED_COMPONENTS.get(canonical)
            if required is not None and any(sku not in by_orig for sku in required):
                merged["qty"] = 0
                merged["invalid"] = False
            else:
                merged["qty"] = min(qty_values)
                merged["invalid"] = False
        out.append(merged)

    return unrelated + out
