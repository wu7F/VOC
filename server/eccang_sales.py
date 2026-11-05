# -*- coding: utf-8 -*-
"""易仓销量分母：按「店铺账号 × 自然月 × 产品族」聚合销量件数。

给售后 VOC 提供真实分母：
    售后率 = 当月售后事件数 ÷ 该店铺当月销量件数(qty)

数据源：易仓开放平台 `getOrderList`（实测覆盖全部店铺，明细带 SKU 与 qty）。
缓存：data/eccang_sales.json —— 首次全量拉，之后按 `date_create_sys` 游标增量拉，
      并用 order_code 去重，避免重复计数。

关于「中文店铺 → 易仓账号」：
  飞书台账里的「类型」是中文店名（初颂/威卡斯/浙捷…），易仓里是英文账号。
  DEFAULT_ACCOUNT_MAP 是实测校准过的映射（用台账订单号反查 getOrderList 得到）；
  分析时**优先用订单号精确反查** order_index，映射表只作兜底。
  可在 data/shop_account_map.json 里覆盖。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import eccang
from .sku_merge import SkuGrouper

_CACHE = "eccang_sales.json"
_MAP = "shop_account_map.json"

# 实测校准：用飞书台账里的订单号反查易仓 getOrderList 得到（2026-11-05）
# 例：初颂 111-9745637-1079466 -> RUIBAO_US_US（ERP 显示名 02ruibao_US）
DEFAULT_ACCOUNT_MAP = {
    "SOUTHWEST": ["southwest_US"],
    "初颂": ["RUIBAO_US_US"],
    "威卡斯": ["Wekas_US"],
    "麦吉天": ["AmazonMJSKY_US_US"],
    "浙捷": ["kaifengzhejie_US"],
    "茂易通": ["maoyitong_US"],
    "行善": ["xingshan_US"],
    "香港": ["KornerHK_US"],
    "香港店": ["KornerHK_US"],
}

# 同步状态（进程内，供 API 查询进度）。
# 进度按「页」实时刷新：一个店铺常有几十页，只按店铺刷新会长时间停在同一数字，
# 看起来像卡死。last_tick 用于判断线程是否真的还在推进。
_SYNC = {"running": False, "progress": 0, "message": "", "started_at": 0,
         "finished_at": 0, "error": "", "full": False,
         "account": "", "shop_label": "", "shop_no": 0, "shop_total": 0,
         "page": 0, "pages": 0, "fetched": 0, "last_tick": 0,
         "cancelled": False}
_SYNC_LOCK = threading.Lock()
_CANCEL = threading.Event()
PAGE_SIZE = 100   # 易仓硬上限：page_size>100 会返回 getOrderListConError


# ---------------------------------------------------------------- 配置 / 缓存

def load_account_map(data_dir) -> dict:
    """读店铺→账号映射（可被 data/shop_account_map.json 覆盖）。"""
    out = {k: list(v) for k, v in DEFAULT_ACCOUNT_MAP.items()}
    if data_dir:
        p = Path(data_dir) / _MAP
        if p.is_file():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    for k, v in d.items():
                        if k.startswith("_"):
                            continue
                        if isinstance(v, str):
                            out[str(k)] = [v.strip()] if v.strip() else []
                        elif isinstance(v, list):
                            out[str(k)] = [str(x).strip() for x in v if str(x).strip()]
            except Exception:
                pass
    return out


def accounts_for_shops(data_dir, shops: list[str] | None = None) -> list[str]:
    """白名单店铺 → 需要拉销量的易仓账号（去重、保持顺序）。"""
    m = load_account_map(data_dir)
    names = shops if shops is not None else list(m.keys())
    out: list[str] = []
    for s in names:
        key = str(s or "").strip()
        for acc in m.get(key) or m.get(key.lower()) or []:
            if acc and acc not in out:
                out.append(acc)
    return out


def empty_cache() -> dict:
    return {"version": 1, "synced_at": 0, "sys_cursor": "",
            "months": [], "accounts": [], "qty": {}, "qty_by_family": {},
            "order_index": {}, "orders": 0, "units": 0, "errors": []}


def load(data_dir) -> dict:
    if not data_dir:
        return empty_cache()
    p = Path(data_dir) / _CACHE
    if not p.is_file():
        return empty_cache()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else empty_cache()
    except Exception:
        return empty_cache()


def _save(data_dir, cache: dict) -> None:
    p = Path(data_dir) / _CACHE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    os.replace(tmp, p)


def status(data_dir) -> dict:
    """给前端看的状态摘要（不回传 order_index 全量）。"""
    c = load(data_dir)
    cfg = eccang.load_config(data_dir)
    with _SYNC_LOCK:
        st = dict(_SYNC)
    now = time.time()
    if st.get("running"):
        st["elapsed_sec"] = int(now - (st.get("started_at") or now))
        st["stalled_sec"] = int(now - (st.get("last_tick") or st.get("started_at") or now))
    else:
        st["elapsed_sec"] = 0
        st["stalled_sec"] = 0
    qty = c.get("qty") or {}
    units = sum(sum(m.values()) for m in qty.values() if isinstance(m, dict))
    return {
        "configured": bool(cfg.get("app_key") and cfg.get("app_secret")
                           and cfg.get("service_id")),
        "cred_source": cfg.get("source", ""),
        "synced_at": c.get("synced_at", 0),
        "months": c.get("months", []),
        "accounts": c.get("accounts", []),
        "done_accounts": c.get("done_accounts", []),
        "with_detail": bool(c.get("with_detail")),
        "orders": c.get("orders", 0),
        "units": units or c.get("units", 0),
        "order_index_size": len(c.get("order_index") or {}),
        "errors": (c.get("errors") or [])[-5:],
        "sync": st,
        "account_map": load_account_map(data_dir),
        "pending_accounts": [a for a in accounts_for_shops(data_dir)
                             if a not in set(c.get("done_accounts") or [])],
    }


def lookup_order(cache: dict, order_no: str) -> dict | None:
    """用飞书台账里的订单号精确反查该订单属于哪个易仓账号 / 哪个月。"""
    code = str(order_no or "").strip().upper()
    if not code:
        return None
    idx = cache.get("order_index") or {}
    hit = idx.get(code)
    if hit:
        return hit
    # 台账里偶尔会给订单号加行号后缀（如 114-...-9463-1），易仓里没有
    m = re.match(r"^(.*)-\d{1,2}$", code)
    return idx.get(m.group(1)) if m else None


def qty_for(cache: dict, account: str, month: str, family: str = "") -> int:
    """某账号某自然月的销量件数；给了 family 则取该产品族的件数。"""
    if not account or not month:
        return 0
    if family:
        return int(((cache.get("qty_by_family") or {}).get(account, {})
                    .get(month, {}) or {}).get(family, 0) or 0)
    return int(((cache.get("qty") or {}).get(account, {}) or {}).get(month, 0) or 0)


# ---------------------------------------------------------------- 同步

def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _month(s: str) -> str:
    s = str(s or "").strip()
    if len(s) >= 7 and s[4:5] == "-" and s[:4].isdigit():
        return s[:7]
    return ""


def _detail_items(order: dict) -> list[dict]:
    return order.get("order_details") or order.get("details") or []


def _item_sku(it: dict) -> str:
    for k in ("product_sku_org", "product_sku", "platform_sku", "sku",
              "warehouse_sku", "product_sku_list"):
        v = str(it.get(k) or "").strip()
        if v:
            return v.split("*")[0]
    for w in it.get("warehouse_sku_list") or []:
        v = str(w.get("warehouse_sku") or "").strip() if isinstance(w, dict) else ""
        if v:
            return v
    return ""


def _item_qty(it: dict) -> int:
    for k in ("qty", "product_sku_org_qty", "product_num", "quantity"):
        try:
            v = int(float(str(it.get(k) or "0")))
            if v > 0:
                return v
        except Exception:
            pass
    return 1


def sync(data_dir, *, full: bool = False, progress=None,
         shops: list[str] | None = None, max_pages: int = 400,
         with_detail: bool = False, since: str = "2026-01-01 00:00:00") -> dict:
    """同步易仓销量。full=True 全量重拉；否则从 sys_cursor 增量拉。

    with_detail=False（默认）用订单顶层 product_count 作销量件数，
    接口快很多；True 会拉订单明细并按产品族拆分销量（慢约 3 倍）。
    返回 status(data_dir)。重复调用会直接返回当前状态（不会并发拉）。
    """
    with _SYNC_LOCK:
        if _SYNC["running"]:
            return status(data_dir)
        now = time.time()
        _SYNC.update(running=True, progress=0, message="准备中…",
                     started_at=now, last_tick=now, finished_at=0,
                     error="", full=bool(full), cancelled=False,
                     account="", shop_label="", shop_no=0, shop_total=0,
                     page=0, pages=0, fetched=0)
    _CANCEL.clear()
    try:
        return _sync_inner(data_dir, full=full, progress=progress,
                           shops=shops, max_pages=max_pages,
                           with_detail=with_detail, since=since)
    except Exception as e:  # noqa: BLE001
        with _SYNC_LOCK:
            _SYNC.update(running=False, progress=100, finished_at=time.time(),
                         last_tick=time.time(), message="同步失败",
                         error=str(e)[:400])
        c = load(data_dir)
        errs = list(c.get("errors") or [])
        errs.append({"at": time.time(), "error": str(e)[:300]})
        c["errors"] = errs[-10:]
        try:
            _save(data_dir, c)
        except Exception:
            pass
        return status(data_dir)


def _sync_inner(data_dir, *, full, progress, shops, max_pages,
                with_detail, since) -> dict:
    def p(pct: int, msg: str, **kw) -> None:
        with _SYNC_LOCK:
            _SYNC.update(progress=max(0, min(100, int(pct))), message=msg,
                         last_tick=time.time(), **kw)
        if progress:
            progress(pct, msg)

    def _elapsed() -> str:
        s = int(time.time() - t_start)
        return "%d分%02d秒" % (s // 60, s % 60)

    cfg = eccang.load_config(data_dir)
    if not (cfg.get("app_key") and cfg.get("app_secret") and cfg.get("service_id")):
        raise RuntimeError(
            "尚未配置易仓凭证（app_key / app_secret / service_id）。"
            "请在服务器的 data/eccang_config.json 或环境变量 "
            "VOC_ECCANG_APP_KEY / VOC_ECCANG_APP_SECRET / VOC_ECCANG_SERVICE_ID 中配置。")

    # 易仓对 QPS 很敏感，连续翻页必须放慢
    eccang._MIN_GAP = 1.7

    accounts = accounts_for_shops(data_dir, shops)
    if not accounts:
        raise RuntimeError("没有可同步的店铺账号（店铺→账号映射为空）。")
    labels: dict[str, str] = {}
    for _shop, _accs in load_account_map(data_dir).items():
        for _acc in _accs:
            labels.setdefault(_acc, _shop)

    cache = load(data_dir)
    if not cache.get("order_index"):
        cache = empty_cache()
        full = True
    done = set(cache.get("done_accounts") or [])
    if full and done and all(a in done for a in accounts):
        # 上次已全部完成，这次显式 full → 从头重拉
        cache, done = empty_cache(), set()
    qty = cache.setdefault("qty", {})
    qfam = cache.setdefault("qty_by_family", {})
    index = cache.setdefault("order_index", {})
    errors: list[dict] = []

    now = time.time()
    t_start = now
    if full:
        start = since or "2026-01-01 00:00:00"
    else:
        cur = str(cache.get("sys_cursor") or "")
        start = cur or _fmt(now - 3 * 86400)
    until = _fmt(now + 3600)   # 留一点余量，避免边界漏单

    grouper = SkuGrouper(data_dir)
    seen = set(index.keys())
    added = 0
    # 全量时跳过已完成的账号（断点续传）；增量时所有账号都要刷新
    pending = [a for a in accounts if a not in done] if full else list(accounts)
    total_accounts = len(pending) or 1

    all_ok = True
    cancelled = False
    for ai, acc in enumerate(pending):
        label = labels.get(acc, acc)
        base_pct = int(5 + 85 * ai / total_accounts)
        p(base_pct, "开始同步 %s（%d/%d 店）" % (label, ai + 1, total_accounts),
          account=acc, shop_label=label, shop_no=ai + 1,
          shop_total=total_accounts, page=0, pages=0, fetched=added)
        acc_qty = qty.setdefault(acc, {})
        acc_fam = qfam.setdefault(acc, {})
        acc_added = 0
        pages_hint = 0
        finished_ok = True
        reached_end = False
        last_page = 0
        for page in range(1, max_pages + 1):
            if _CANCEL.is_set():
                cancelled, finished_ok = True, False
                break
            last_page = page
            biz = {
                "page": page, "page_size": PAGE_SIZE,
                "get_detail": 1 if with_detail else 0,
                "condition": {
                    "platform": "AMAZON",
                    "user_account_list": [acc],
                    "date_create_sys_start": start,
                    "date_create_sys_end": until,
                },
            }
            try:
                data = eccang._unwrap(eccang.call("getOrderList", biz, cfg=cfg,
                                                  timeout=35))
            except eccang.EccangError as e:
                errors.append({"at": time.time(), "account": acc, "page": page,
                               "error": str(e)[:200]})
                finished_ok = False
                break
            rows = data.get("data") if isinstance(data, dict) else None
            rows = rows if isinstance(rows, list) else []
            try:
                total_hint = int(float(str(data.get("total") or 0)))
            except Exception:
                total_hint = 0
            if total_hint > 0:
                pages_hint = max(1, (total_hint + PAGE_SIZE - 1) // PAGE_SIZE)
            frac = min(0.98, page / (pages_hint or max_pages))
            # 每拉完一页就刷新进度 + 心跳，避免大店铺几十页停在同一数字
            p(int(5 + 85 * (ai + frac) / total_accounts),
              "同步 %s %d/%d 店 · 第 %d%s 页 · 已拉 %d 单 · 已用 %s" %
              (label, ai + 1, total_accounts, page,
               ("/%d" % pages_hint) if pages_hint else "", added, _elapsed()),
              account=acc, shop_label=label, shop_no=ai + 1,
              shop_total=total_accounts, page=page, pages=pages_hint,
              fetched=added)
            if not rows:
                reached_end = True
                break
            for od in rows:
                code = str(od.get("order_code") or od.get("refrence_no") or "").strip().upper()
                if code and code in seen:
                    continue
                month = _month(od.get("date_create") or od.get("date_create_platform")
                               or od.get("platform_paid_date"))
                if code:
                    index[code] = {"a": acc, "m": month}
                    seen.add(code)
                added += 1
                acc_added += 1
                if not month:
                    continue
                m_qty = acc_qty.setdefault(month, 0)
                m_fam = acc_fam.setdefault(month, {})
                items = _detail_items(od) if with_detail else []
                if items:
                    for it in items:
                        sku = _item_sku(it)
                        n = _item_qty(it)
                        m_qty += n
                        if sku:
                            fam = grouper.family(sku)
                            m_fam[fam] = m_fam.get(fam, 0) + n
                else:
                    # 不带明细时，订单顶层的 product_count 就是该单的商品件数
                    try:
                        m_qty += max(0, int(float(str(od.get("product_count") or 0))))
                    except Exception:
                        pass
                acc_qty[month] = m_qty
            if len(rows) < PAGE_SIZE:
                reached_end = True
                break
        if last_page >= max_pages and not reached_end and not cancelled:
            errors.append({"at": time.time(), "account": acc,
                           "error": "达到最大页数上限，可能未拉全"})
            finished_ok = False
        # 只有完整拉完的店铺才标记完成：中途失败/中止的店铺下次会自动重试，
        # 否则会被永久跳过，分母缺一块却查不出来。
        if finished_ok:
            done.add(acc)
            p(int(5 + 85 * (ai + 1) / total_accounts),
              "%s 完成 · %d 单" % (label, acc_added),
              account=acc, shop_label=label, shop_no=ai + 1,
              shop_total=total_accounts, page=last_page, pages=pages_hint,
              fetched=added)
        else:
            all_ok = False
            errors.append({"at": time.time(), "account": acc,
                           "error": "用户中止" if cancelled else
                                    "该店铺未拉完，下次同步自动重试（已拉部分不会重复计数）"})
        # 每完成一个账号就落盘：首次全量较久，中途重启/中止可断点续传
        cache["done_accounts"] = sorted(done)
        cache["orders"] = len(index)
        cache["units"] = sum(sum(v.values()) for v in qty.values() if isinstance(v, dict))
        try:
            _save(data_dir, cache)
        except Exception:
            pass
        if cancelled:
            break

    finished = time.time()
    months = sorted({m for v in qty.values() if isinstance(v, dict) for m in v})
    cache.update({
        "version": 1,
        "months": months,
        "accounts": accounts,
        "done_accounts": sorted(done),
        "with_detail": bool(cache.get("with_detail")) or bool(with_detail),
        "orders": len(index),
        "units": sum(sum(v.values()) for v in qty.values() if isinstance(v, dict)),
        "errors": (cache.get("errors") or [])[-5:] + errors,
    })
    # 只有全部成功才推进游标 / 标记完成时间，避免半途数据被当成完整分母
    if all_ok and not cancelled:
        cache["synced_at"] = finished
        cache["sys_cursor"] = _fmt(finished - 7200)  # 增量游标往前留 2 小时
    cache["errors"] = cache["errors"][-10:]
    _save(data_dir, cache)
    with _SYNC_LOCK:
        cur_pct = _SYNC["progress"]
    if all_ok and not cancelled:
        p(100, "销量同步完成：%d 个账号 / %d 个月 / 新增 %d 单" %
          (len(accounts), len(months), added))
        with _SYNC_LOCK:
            _SYNC.update(running=False, progress=100, finished_at=finished)
    else:
        msg = ("已中止 · 已完成店铺已保存，点「继续同步」接着跑" if cancelled else
               "部分店铺未拉完 · 已保存进度，下次同步自动重试")
        p(cur_pct, msg)
        with _SYNC_LOCK:
            _SYNC.update(running=False, finished_at=finished, error=msg,
                         cancelled=bool(cancelled))
    return status(data_dir)


def cancel(data_dir) -> dict:
    """中止当前同步（在当前页请求返回后生效，最长约 35 秒）。

    已完成店铺的销量会保留，下次同步自动跳过并从中断的店铺继续。
    """
    with _SYNC_LOCK:
        running = bool(_SYNC["running"])
    if running:
        _CANCEL.set()
        return {"ok": True, "message": "已发出中止请求，等待当前页返回…",
                "sync": status(data_dir)["sync"]}
    return {"ok": True, "message": "当前没有正在运行的同步",
            "sync": status(data_dir)["sync"]}


def start_sync(data_dir, *, full: bool = False, shops: list[str] | None = None,
               with_detail: bool = False, since: str = "2026-01-01 00:00:00") -> dict:
    """后台线程启动同步，立即返回当前状态（不阻塞 HTTP 请求）。"""
    with _SYNC_LOCK:
        if _SYNC["running"]:
            return status(data_dir)
    t = threading.Thread(target=sync,
                         kwargs={"data_dir": data_dir, "full": full, "shops": shops,
                                 "with_detail": with_detail, "since": since},
                         daemon=True)
    t.start()
    # 给线程一点时间把 running 置起来
    time.sleep(0.05)
    return status(data_dir)
