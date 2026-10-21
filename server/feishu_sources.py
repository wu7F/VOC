# -*- coding: utf-8 -*-
"""飞书数据源：把飞书表格变成平台的可自动刷新数据源。

一个「数据源」= 一张飞书表格 + 一个目标平台 + 同步策略。
每次同步会：拉取飞书数据 → 写成 CSV → 重跑售后分析 → 更新同一个数据集。
因此看板链接稳定（dataset id 不变），内容随飞书表格自动更新，不需要再上传文件。

持久化在 data/feishu_sources.json（与 datasets 分开，源是配置、数据集是产物）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from pathlib import Path

from . import feishu as feishu_module

_LOCK = threading.Lock()
_FILE = "feishu_sources.json"


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / _FILE


def _load(data_dir: Path) -> list[dict]:
    p = _path(data_dir)
    if not p.is_file():
        return []
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        srcs = d.get("sources") if isinstance(d, dict) else d
        return srcs if isinstance(srcs, list) else []
    except Exception:
        return []


def _save(data_dir: Path, sources: list[dict]) -> None:
    p = _path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"sources": sources}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, p)


def list_sources(data_dir: Path) -> list[dict]:
    with _LOCK:
        srcs = list(_load(data_dir))
    srcs.sort(key=lambda s: s.get("created_at") or "", reverse=True)
    return srcs


def get_source(data_dir: Path, sid: str) -> dict | None:
    for s in list_sources(data_dir):
        if s.get("id") == sid:
            return s
    return None


def upsert_source(data_dir: Path, *, sid: str | None = None, **fields) -> dict:
    with _LOCK:
        srcs = _load(data_dir)
        rec = next((s for s in srcs if s.get("id") == sid), None)
        if rec is None:
            rec = {
                "id": sid or ("fsrc_" + time.strftime("%Y%m%d_%H%M%S") + "_" + os.urandom(2).hex()),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_sync_at": None,
                "last_status": "",
                "last_message": "",
                "dataset_id": "",
            }
            srcs.insert(0, rec)
        for k, v in fields.items():
            if v is not None:
                rec[k] = v
        _save(data_dir, srcs)
        return rec


def delete_source(data_dir: Path, sid: str) -> bool:
    with _LOCK:
        srcs = _load(data_dir)
        rest = [s for s in srcs if s.get("id") != sid]
        if len(rest) == len(srcs):
            return False
        _save(data_dir, rest)
        return True


def due_for_sync(src: dict, interval_min: int = 30) -> bool:
    """是否到了该同步的时间。"""
    if not src.get("auto_sync", True):
        return False
    last = src.get("last_sync_at")
    if not last:
        return True
    iv = int(src.get("interval_min") or interval_min or 30)
    return (time.time() - float(last)) > iv * 60
