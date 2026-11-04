# -*- coding: utf-8 -*-
"""售后店铺白名单：分析时只保留指定店铺，其余记录直接排除。

配置在 data/shop_whitelist.json：
    {
      "enabled": true,
      "shops": ["威卡斯", "麦吉天", ...],
      "note": "可选备注"
    }

匹配规则：**大小写不敏感、忽略首尾空格**的精确匹配。
不配这个文件（或 enabled=false、shops 为空）时 = 不过滤，全部店铺都算。
"""

from __future__ import annotations

import json
from pathlib import Path

_FILE = "shop_whitelist.json"


def norm(s: str) -> str:
    """店铺名归一化：去首尾空格 + 转小写。"""
    return str(s or "").strip().lower()


def load(data_dir) -> tuple[set[str] | None, list[str]]:
    """返回 (归一化后的白名单集合 | None, 原始名单)。None 表示不过滤。"""
    if not data_dir:
        return None, []
    p = Path(data_dir) / _FILE
    if not p.is_file():
        return None, []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None, []
    if isinstance(d, list):
        d = {"enabled": True, "shops": d}
    if not d.get("enabled", True):
        return None, []
    raw = [str(s).strip() for s in (d.get("shops") or []) if str(s).strip()]
    if not raw:
        return None, []
    return {norm(s) for s in raw}, raw


def save(data_dir, shops: list[str], enabled: bool = True) -> dict:
    """写入白名单（供界面/脚本调用）。"""
    p = Path(data_dir) / _FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = [str(s).strip() for s in (shops or []) if str(s).strip()]
    p.write_text(json.dumps({"enabled": bool(enabled), "shops": clean},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    return {"enabled": bool(enabled), "shops": clean}
