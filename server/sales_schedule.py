"""Opt-in monthly import inside the VOC service, with durable retry state."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib
import json
import os
import time

from . import eccang_sales, feishu_sales
from .sales_lock import try_lock

_FILE = "feishu_sales_schedule.json"
_CST = timezone(timedelta(hours=8))


def status(data_dir):
    path = Path(data_dir) / _FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {"error": "月度同步状态文件无法读取，请检查后重试。"}


def _save(data_dir, value):
    path = Path(data_dir) / _FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def tick(data_dir, *, now=None):
    """Once each Shanghai calendar month. Failures retry after one hour."""
    cfg = feishu_sales.load_settings(data_dir)
    if not (cfg.get("url") and cfg.get("write_back") and cfg.get("auto_sync")):
        return {"skipped": "disabled"}
    now = time.time() if now is None else now
    month = datetime.fromtimestamp(now, _CST).strftime("%Y-%m")
    target = hashlib.sha256(cfg["url"].encode()).hexdigest()
    with try_lock(data_dir, "feishu-sales-monthly") as acquired:
        if not acquired:
            return {"skipped": "busy"}
        st = status(data_dir)
        if st.get("target") != target:
            st = {"target": target}
        if st.get("last_month") == month:
            return {"skipped": "already_complete", **st}
        if now - st.get("last_attempt_at", 0) < 3600:
            return {"skipped": "retry_later", **st}
        if eccang_sales.status(data_dir).get("sync", {}).get("running"):
            return {"skipped": "eccang_busy", **st}
        st.update(last_attempt_at=now, error="")
        _save(data_dir, st)
        try:
            # Rebuild the history to carry corrections and deleted SKUs, then upsert.
            out = eccang_sales.sync(data_dir, full=True, with_detail=True)
            if (not out.get("complete") or out.get("sync", {}).get("error")
                    or out.get("writeback", {}).get("status") != "ok"):
                raise RuntimeError(out.get("sync", {}).get("error") or "易仓销量未完整同步。")
            read = feishu_sales.sync(data_dir)
            if not read.get("complete"):
                raise RuntimeError(read.get("error") or "飞书销量回读未完成。")
            st.update(last_month=month, completed_at=now, error="")
        except Exception:
            # Provider error strings may contain account data; expose only a safe summary.
            st["error"] = "月度销量同步未完成，一小时后自动重试；可查看销量同步状态。"
        _save(data_dir, st)
        return st
