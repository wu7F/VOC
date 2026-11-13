"""Strict sales worksheet snapshots, independent of Eccang credentials.

One dedicated worksheet has exactly 店铺/SKU/日期/销量件数. The complete
validated snapshot is the authority for its selected platform; missing shops
and failed reads never fall back to another denominator.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

from . import feishu, shop_filter
from .sales_lock import try_lock
from .sku_merge import SkuGrouper

_SETTINGS = "feishu_sales_settings.json"
_CACHE = "feishu_sales.json"
_LOCK = threading.RLock()
_DEFAULT = {"url": "", "write_back": False, "auto_sync": False,
            "platform": "AmazonCs"}
HEADERS = ["店铺", "SKU", "日期", "销量件数"]


def load_settings(data_dir) -> dict:
    p = Path(data_dir) / _SETTINGS if data_dir else None
    if p is None or not p.is_file():
        return dict(_DEFAULT)
    saved = json.loads(p.read_text(encoding="utf-8"))
    return {k: saved.get(k, v) for k, v in _DEFAULT.items()}


def source_id(settings: dict) -> str:
    if not settings.get("url"):
        return ""
    info = feishu.parse_feishu_url(settings["url"])
    identity = [info["kind"], info["token"], info.get("sheet_id", "")]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def _save(data_dir, name: str, content: dict) -> None:
    p = Path(data_dir) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_name(p.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, p)


def save_settings(data_dir, *, url: str = "", write_back: bool = False,
                  auto_sync: bool = False, platform: str = "AmazonCs") -> dict:
    url = url.strip()
    if platform not in ("AmazonCs", "WayfairCs"):
        raise ValueError("请选择销量表对应的售后平台。")
    if url:
        info = feishu.parse_feishu_url(url)
        if info["kind"] != "sheets" or not info.get("sheet_id"):
            raise ValueError("销量表请使用带 sheet 参数的飞书电子表格工作表链接。")
    if (write_back or auto_sync) and not url:
        raise ValueError("启用写入或自动同步前，请配置销量工作表链接。")
    if write_back and platform != "AmazonCs":
        raise ValueError("现有易仓同步只提供 Amazon 销量，不能写入其他平台。")
    if auto_sync and not write_back:
        raise ValueError("每月自动同步需要启用易仓写入。")
    saved = {"url": url, "write_back": bool(write_back),
             "auto_sync": bool(auto_sync), "platform": platform}
    with _LOCK:
        _save(data_dir, _SETTINGS, saved)
    return saved


def empty_cache() -> dict:
    return {"version": 1, "complete": False, "synced_at": 0, "source_id": "",
            "months": [], "accounts": [], "qty": {}, "qty_by_family": {},
            "shop_names": {}, "units": 0, "row_count": 0, "error": ""}


def load(data_dir) -> dict:
    settings = load_settings(data_dir)
    if not settings["url"]:
        return empty_cache()
    from .feishu_sales_writer import has_pending
    if has_pending(data_dir, url=settings["url"]):
        result = empty_cache()
        result["error"] = "上次销量写表未确认完成，请先重跑易仓写表。"
        return result
    p = Path(data_dir) / _CACHE
    if not p.is_file():
        return empty_cache()
    try:
        cache = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return empty_cache()
    if (not isinstance(cache, dict) or cache.get("source_id") != source_id(settings)
            or cache.get("platform") != settings["platform"]):
        return empty_cache()
    return cache


def is_complete(cache: dict) -> bool:
    return bool(cache.get("complete") is True and cache.get("source_id")
                and cache.get("synced_at", 0) > 0)


def invalidate(data_dir, reason: str = "销量表正在写入") -> None:
    """Called under the worksheet lock before the first remote mutation."""
    settings = load_settings(data_dir)
    pending = empty_cache()
    pending.update(source_id=source_id(settings), platform=settings["platform"], error=reason)
    _save(data_dir, _CACHE, pending)


def build_cache(values: list[list], settings: dict, data_dir=None) -> dict:
    """Validate every occupied row before exposing any quantity as a denominator."""
    from .feishu_sales_writer import normalize_row, key_for
    if not values or list(values[0]) != HEADERS:
        raise ValueError("销量表首行须依次为：店铺、SKU、日期、销量件数。")
    result = empty_cache()
    result["source_id"] = source_id(settings)
    result["platform"] = settings["platform"]
    seen = set()
    grouper = SkuGrouper(data_dir)
    for raw in values[1:]:
        if not any(v not in (None, "") for v in raw):
            continue
        if len(raw) > 4 and any(v not in (None, "") for v in raw[4:]):
            raise ValueError("销量表只允许四个数据列。")
        row = normalize_row(dict(zip(HEADERS, list(raw) + [""] * (4 - len(raw)))))
        key = key_for(row)
        if key in seen:
            raise ValueError("销量表存在重复的店铺、SKU、日期组合，请清理后重试。")
        seen.add(key)
        shop = shop_filter.norm(row["店铺"])
        month = row["日期"][:7]
        family = grouper.family(row["SKU"])
        qty = row["销量件数"]
        result["shop_names"].setdefault(shop, row["店铺"])
        monthly = result["qty"].setdefault(shop, {})
        monthly[month] = monthly.get(month, 0) + qty
        families = result["qty_by_family"].setdefault(shop, {}).setdefault(month, {})
        families[family] = families.get(family, 0) + qty
        result["units"] += qty
        result["row_count"] += 1
    result["accounts"] = sorted(result["qty"])
    result["months"] = sorted({m for q in result["qty"].values() for m in q})
    result.update(complete=True, synced_at=time.time())
    return result


def sync(data_dir) -> dict:
    """Refresh a single worksheet; failure invalidates the former snapshot."""
    from .feishu_sales_writer import read_sheet
    with _LOCK:
        settings = load_settings(data_dir)
        with try_lock(data_dir, "feishu-sales-write") as acquired:
            if not acquired:
                return status(data_dir)
            pending = empty_cache()
            pending["source_id"] = source_id(settings)
            pending["platform"] = settings["platform"]
            _save(data_dir, _CACHE, pending)
            try:
                if not settings["url"]:
                    raise ValueError("尚未配置飞书销量表。")
                values = read_sheet(data_dir, url=settings["url"], locked=True)
                pending = build_cache(values, settings, data_dir)
            except Exception:
                # Provider error text may contain account or request details.
                pending["error"] = "销量表读取或校验失败；请检查四列表头、日期/件数、重复行及应用访问权限。"
            _save(data_dir, _CACHE, pending)
    return status(data_dir)


def status(data_dir) -> dict:
    settings = load_settings(data_dir)
    cache = load(data_dir)
    cfg = feishu.load_config(data_dir)
    return {"settings": settings, "configured": bool(settings["url"]),
            "credentials_configured": bool(cfg.get("app_id") and cfg.get("app_secret")),
            "complete": is_complete(cache), "synced_at": cache.get("synced_at", 0),
            "units": cache.get("units", 0), "months": cache.get("months", []),
            "row_count": cache.get("row_count", 0), "error": cache.get("error", "")}
