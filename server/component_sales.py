# -*- coding: utf-8 -*-
"""Fixed approved warehouse component alias merge + minimum aggregation helper.

Audit mapping source:
  file_id: 3ad55f539cd2
  sha256: 177e237fbc3cc2a125dedda69f2fb8be76757cfbb9606ab5f3684cb45863d2c4
No runtime workbook dependency.
"""

from __future__ import annotations
import re

from . import shop_filter

# Authoritative fixed mapping. Do not infer from suffixes or descriptions.
COMPONENT_ALIASES: dict[str, str] = {
    "LAYJMZPG02CR-2R": "LAYJMZPG02CR-2L",
    "LAYJMZPG02DG-2R": "LAYJMZPG02DG-2L",
    "LAYJMZPG01LG-2R": "LAYJMZPG01LG-2L",
    "LAYJMZPG02CR-3R": "LAYJMZPG02CR-3L",
    "LAYJMZPG02CR-3M": "LAYJMZPG02CR-3L",
    "LAYJMZP240458LG-2R": "LAYJMZP240458LG-2L",
    "LAYJMZPG01LG-3M": "LAYJMZPG01LG-3L",
    "LAYJMZPG01LG-3R": "LAYJMZPG01LG-3L",
    "LAYJMZPG02CR-2RIGHT": "LAYJMZPG02CR-2L",
    "LAYJMZPG01CR-2R": "LAYJMZPG01CR-2L",
    "LAYJMZP0458LG-2KB": "LAYJMZP240458LG-2L",
    "LAYJMZP0458LG-2LT": "LAYJMZP240458LG-2L",
    "LAYJMZP0458LG-2RT": "LAYJMZP240458LG-2L",
    "LAYJMZPG02DG-2RIGHT": "LAYJMZPG02DG-2L",
    "LAYJMZPG02CR-3MIDDLE": "LAYJMZPG02CR-3L",
    "LAYJMZPG02CR-3RIGHT": "LAYJMZPG02CR-3L",
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
    # New paired LEFT products fold to the old canonical seat products.
    "LAYJMZPG02CR-2LEFT": "LAYJMZPG02CR-2L",
    "LAYJMZPG02DG-2LEFT": "LAYJMZPG02DG-2L",
    "LAYJMZPG02CR-3LEFT": "LAYJMZPG02CR-3L",
    "LAYJMZPG02DG-3LEFT": "LAYJMZPG02DG-3L",
    # DG corner package parts (moved out of seat groups).
    "LAYJMZPG02DG-2LT": "LAYJMZPG02DG-LT",
    "LAYJMZPG02DG-2MT": "LAYJMZPG02DG-LT",
    "LAYJMZPG02DG-2RT": "LAYJMZPG02DG-LT",
    "LAYJMZPG02DG-3MT": "LAYJMZPG02DG-LT",
}


# Explicit fixed required-component subsets for approved single-version products.
# Partial local coverage only; pending products keep observed-only minimum behavior.
REQUIRED_COMPONENTS: dict[str, tuple[str, ...]] = {
    "LAYJMZPG01CR-2L": ("LAYJMZPG01CR-2L", "LAYJMZPG01CR-2R"),
    "LAYJMZPG01CR-3L": ("LAYJMZPG01CR-3L", "LAYJMZPG01CR-3M", "LAYJMZPG01CR-3R"),
    "LAYJMZPG01LG-2L": ("LAYJMZPG01LG-2L", "LAYJMZPG01LG-2R"),
    "LAYJMZPG01LG-3L": ("LAYJMZPG01LG-3L", "LAYJMZPG01LG-3M", "LAYJMZPG01LG-3R"),
    "LAYJMZP0458LG-3MT": ("LAYJMZP0458LG-3MT", "LAYJMZP0458LG-3LT", "LAYJMZP0458LG-3RT", "LAYJMZP0458LG-3KB"),
    "LAYJHLR8005DG-LT": ("LAYJHLR8005DG-LT", "LAYJHLR8005DG-ME1", "LAYJHLR8005DG-ZJKB", "LAYJHLR8005DG-ZJDZ", "LAYJHLR8005DG-ME2", "LAYJHLR8005DG-RT", "LAYJHLR8005DG-CJ"),
    "LAYJMZP202641DBR-2LT": ("LAYJMZP202641DBR-2LT", "LAYJMZP202641DBR-2RT", "LAYJMZP202641DBR-AR"),
}


# Two independently complete packages for LAYJMZP240458LG-2L.
# Package A: LAYJMZP240458LG-2L / LAYJMZP240458LG-2R
# Package B: LAYJMZP0458LG-2LT / LAYJMZP0458LG-2RT / LAYJMZP0458LG-2KB
# Each package is evaluated separately; observed incomplete package yields 0,
# complete package yields its minimum; the two results are summed. An entirely
# absent package contributes nothing (never forces the other to zero).
#
# G02 seat/corner canonicals: each version is an independent package. Observed
# partial => 0, complete => minimum, entirely absent => no contribution; present
# version values are summed. Never mix colors or fill missing parts across versions.
MULTI_PACKAGE_COMPONENTS: dict[str, tuple[tuple[str, ...], ...]] = {
    "LAYJMZP240458LG-2L": (
        ("LAYJMZP240458LG-2L", "LAYJMZP240458LG-2R"),
        ("LAYJMZP0458LG-2LT", "LAYJMZP0458LG-2RT", "LAYJMZP0458LG-2KB"),
    ),
    "LAYJMZPG02CR-2L": (
        ("LAYJMZPG02CR-2L", "LAYJMZPG02CR-2R"),
        ("LAYJMZPG02CR-2LEFT", "LAYJMZPG02CR-2RIGHT"),
    ),
    "LAYJMZPG02DG-2L": (
        ("LAYJMZPG02DG-2L", "LAYJMZPG02DG-2R"),
        ("LAYJMZPG02DG-2LEFT", "LAYJMZPG02DG-2RIGHT"),
    ),
    "LAYJMZPG02CR-3L": (
        ("LAYJMZPG02CR-3L", "LAYJMZPG02CR-3M", "LAYJMZPG02CR-3R"),
        ("LAYJMZPG02CR-3LEFT", "LAYJMZPG02CR-3MIDDLE", "LAYJMZPG02CR-3RIGHT"),
    ),
    "LAYJMZPG02DG-3L": (
        ("LAYJMZPG02DG-3L", "LAYJMZPG02DG-3M", "LAYJMZPG02DG-3R"),
        ("LAYJMZPG02DG-3LEFT", "LAYJMZPG02DG-3MIDDLE", "LAYJMZPG02DG-3RIGHT"),
    ),
    "LAYJMZPG02DG-LT": (
        ("LAYJMZPG02DG-LT", "LAYJMZPG02DG-ME1", "LAYJMZPG02DG-ZJKB", "LAYJMZPG02DG-ZJDZ", "LAYJMZPG02DG-ME2", "LAYJMZPG02DG-RT", "LAYJMZPG02DG-CJ"),
        ("LAYJMZPG02DG-KBT", "LAYJMZPG02DG-2LT", "LAYJMZPG02DG-2MT", "LAYJMZPG02DG-ZJKBT", "LAYJMZPG02DG-ZJDZT", "LAYJMZPG02DG-3MT", "LAYJMZPG02DG-2RT", "LAYJMZPG02DG-CJT"),
    ),
}


# Explicit product rollups: new paired LEFT products fold to the old canonical.
# Only applied when the canonical exists in pair_map.
PRODUCT_ROLLUPS: dict[str, str] = {
    "LAYJMZPG02CR-2LEFT": "LAYJMZPG02CR-2L",
    "LAYJMZPG02DG-2LEFT": "LAYJMZPG02DG-2L",
    "LAYJMZPG02CR-3LEFT": "LAYJMZPG02CR-3L",
    "LAYJMZPG02DG-3LEFT": "LAYJMZPG02DG-3L",
}


# Exact G02 warehouse prefixes whose BACKREST / AR suffix components are ignored
# for completeness, minimum and base-product sales.
_G02_IGNORE_PREFIXES = ("LAYJMZPG02CR-", "LAYJMZPG02DG-")
_G02_IGNORE_SUFFIX_RE = re.compile(r"^(?:BACKREST|AR|2AR|3AR)$")


# 8018 family detection: exact family number 8018 with digit boundaries.
_8018_RE = re.compile(r"(?<!\d)8018(?!\d)")


def is_8018_product(sku: str | None, display_name: str | None = None) -> bool:
    """Return True when the SKU or authoritative display name belongs to the 8018 family.

    Matches the exact family number 8018 with digit boundaries in either the
    original warehouse SKU or the paired display name. Does not match 18018 or
    80180.
    """
    for value in (sku, display_name):
        if not value or not isinstance(value, str):
            continue
        if _8018_RE.search(value):
            return True
    return False


def is_ignored_g02_component(sku: str | None) -> bool:
    """Return True for exact G02 BACKREST / AR suffix components that must be ignored."""
    if not sku or not isinstance(sku, str):
        return False
    for prefix in _G02_IGNORE_PREFIXES:
        if sku.startswith(prefix):
            suffix = sku[len(prefix):]
            if _G02_IGNORE_SUFFIX_RE.match(suffix):
                return True
    return False


def _merge_group(by_orig: dict[str, list[dict]], diagnostics: list[dict],
                 account: str, canonical: str, month) -> dict:
    """Merge one (account, canonical, month) group into a single row."""
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
        return merged

    packages = MULTI_PACKAGE_COMPONENTS.get(canonical)
    if packages is not None:
        total = 0
        for package in packages:
            present = [sku for sku in package if sku in by_orig]
            if not present:
                # Entirely absent package contributes nothing.
                continue
            if any(sku not in by_orig for sku in package):
                # Observed incomplete package yields 0.
                continue
            pkg_qty = min(by_orig[sku][0].get("qty") for sku in package)
            total += pkg_qty
        merged["qty"] = total
        merged["invalid"] = False
        return merged

    required = REQUIRED_COMPONENTS.get(canonical)
    if required is not None and any(sku not in by_orig for sku in required):
        merged["qty"] = 0
        merged["invalid"] = False
    else:
        merged["qty"] = min(qty_values)
        merged["invalid"] = False
    return merged


def merge_component_rows(rows: list[dict], pair_map: dict | None, diagnostics: list[dict]) -> list[dict]:
    """Return normalized rows with admitted aliases merged to canonical products by minimum quantity.

    Only component rows whose canonical target exists in pair_map are merged.
    Unrelated rows pass through unchanged (copied). No missing-component synthesis.
    For targets in REQUIRED_COMPONENTS, an observed group with any required original
    SKU absent yields qty 0; invalid/duplicate observations still take precedence.
    For targets in MULTI_PACKAGE_COMPONENTS, each package is evaluated separately.
    """
    filtered = [r for r in rows if not is_ignored_g02_component(r.get("sku"))]

    if pair_map is None:
        return [dict(r) for r in filtered]

    active_targets = {target for target in COMPONENT_ALIASES.values() if target in pair_map}
    if not active_targets:
        return [dict(r) for r in filtered]

    groups: dict[tuple, dict[str, list[dict]]] = {}
    unrelated: list[dict] = []
    for r in filtered:
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
        out.append(_merge_group(by_orig, diagnostics, account, canonical, month))

    return unrelated + out
