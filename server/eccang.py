# -*- coding: utf-8 -*-
"""易仓科技（ECCANG ERP）开放平台客户端。

用途：拉订单明细 → 聚合「SKU × 店铺 × 月」销量，给售后率提供分母。

接口约定（官方文档 open.eccang.com）：
    POST http://openapi-web.eccang.com/openApi/api/unity
    Content-Type: application/json
    公共参数：app_key / service_id / interface_method / biz_content /
              sign / sign_type=MD5 / nonce_str / charset=UTF-8 /
              timestamp(毫秒) / version=v1.0.0
    签名：按字段名排序拼 `k=v&...`（跳过空值与 sign 本身），末尾直接拼 app_secret，
          再取 32 位小写 MD5。

凭证来源（优先级从高到低）：
    1) 环境变量 VOC_ECCANG_APP_KEY / VOC_ECCANG_APP_SECRET / VOC_ECCANG_SERVICE_ID
    2) data/eccang_config.json   {"app_key":..., "app_secret":..., "service_id":...}
**不要把凭证写进代码或提交到仓库。**
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API_URL = "http://openapi-web.eccang.com/openApi/api/unity"
_CFG = "eccang_config.json"

# 易仓有 QPS 限制（实测连续调用会 429「请求频率超限」），
# 这里做全局节流 + 429 指数退避重试。
_MIN_GAP = 0.7          # 两次请求最小间隔（秒）
_last_call = [0.0]
_MAX_RETRY = 4


class EccangError(RuntimeError):
    pass


def _throttle():
    gap = time.time() - _last_call[0]
    if gap < _MIN_GAP:
        time.sleep(_MIN_GAP - gap)
    _last_call[0] = time.time()


# ---------------------------------------------------------------- 凭证

def load_config(data_dir) -> dict:
    """读取易仓凭证。返回 {app_key, app_secret, service_id, source}。"""
    import os
    env = {
        "app_key": os.environ.get("VOC_ECCANG_APP_KEY", "").strip(),
        "app_secret": os.environ.get("VOC_ECCANG_APP_SECRET", "").strip(),
        "service_id": os.environ.get("VOC_ECCANG_SERVICE_ID", "").strip(),
    }
    if env["app_key"] and env["app_secret"] and env["service_id"]:
        env["source"] = "env"
        return env

    out = {"app_key": "", "app_secret": "", "service_id": "", "source": ""}
    if data_dir:
        p = Path(data_dir) / _CFG
        if p.is_file():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                d = {}
            out = {
                "app_key": str(d.get("app_key", "")).strip(),
                "app_secret": str(d.get("app_secret", "")).strip(),
                "service_id": str(d.get("service_id", "")).strip(),
                "source": "file",
            }
    for k in ("app_key", "app_secret", "service_id"):
        if env[k]:
            out[k] = env[k]
            out["source"] = out["source"] or "env"
    return out


def save_config(data_dir, app_key: str, app_secret: str, service_id: str) -> dict:
    p = Path(data_dir) / _CFG
    p.parent.mkdir(parents=True, exist_ok=True)
    d = {"app_key": app_key.strip(), "app_secret": app_secret.strip(),
         "service_id": service_id.strip()}
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        import os
        os.chmod(p, 0o600)
    except Exception:
        pass
    return load_config(data_dir)


# ---------------------------------------------------------------- 签名 / 请求

def make_sign(params: dict, secret: str) -> str:
    items = sorted((k, v) for k, v in params.items()
                   if k != "sign" and v is not None and str(v) != "")
    raw = "&".join("%s=%s" % (k, v) for k, v in items) + secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def call(method: str, biz: dict | None = None, *, cfg: dict | None = None,
         timeout: int = 40) -> dict:
    """调用一个易仓接口，返回 data 部分（失败抛 EccangError，带可读原因）。"""
    c = cfg or {}
    app_key = c.get("app_key", "")
    secret = c.get("app_secret", "")
    service_id = c.get("service_id", "")
    if not app_key or not secret:
        raise EccangError("缺少易仓 app_key / app_secret。")
    if not service_id:
        raise EccangError(
            "缺少易仓「授权服务ID」(service_id)。\n"
            "获取位置：home.eccang.com → 应用管理 → 授权状态 → 已授权 → 服务ID\n"
            "（https://home.eccang.com/#/company/serve/serve-manage）")

    params = {
        "app_key": app_key,
        "service_id": service_id,
        "interface_method": method,
        "biz_content": json.dumps(biz or {}, ensure_ascii=False, separators=(",", ":")),
        "sign_type": "MD5",
        "charset": "UTF-8",
        "nonce_str": uuid.uuid4().hex,
        "timestamp": int(time.time() * 1000),
        "version": "v1.0.0",
    }
    params["sign"] = make_sign(params, secret)
    body = json.dumps(params, ensure_ascii=False).encode("utf-8")

    last_err = None
    for attempt in range(_MAX_RETRY):
        _throttle()
        req = urllib.request.Request(API_URL, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "ignore")
            break
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                # 限流：指数退避后重试
                time.sleep(2 ** (attempt + 1))
                continue
            raise EccangError("易仓接口 HTTP %s：%s" % (e.code, e.read()[:300]))
        except Exception as e:
            raise EccangError("连不上易仓接口：%r" % (e,))
    else:
        raise EccangError(
            "易仓接口持续限流（HTTP 429，已重试 %d 次）。\n"
            "易仓对请求频率有上限，拉取大批量订单时请放慢速度或缩小时间范围。\n"
            "最后一次错误：%r" % (_MAX_RETRY, last_err))

    try:
        j = json.loads(raw)
    except Exception:
        raise EccangError("易仓返回了非 JSON：%s" % raw[:300])

    code = str(j.get("code", ""))
    if code and code not in ("200", "0", "success", "common.success"):
        msg = j.get("message") or j.get("msg") or raw[:200]
        if "授权服务id" in str(msg) or "service" in str(msg).lower():
            raise EccangError(
                "易仓要求「授权服务ID」：请在 home.eccang.com → 应用管理 → 授权状态 → "
                "已授权 里复制服务ID 填进来。原始返回：%s" % msg)
        raise EccangError("易仓接口报错（%s）：%s" % (code, msg))
    return j.get("data") if isinstance(j.get("data"), (dict, list)) else j


def test_connection(cfg: dict) -> dict:
    """连通性自检：先拿仓库列表（最轻的接口），确认凭证/签名/服务ID 都对。"""
    steps = []
    try:
        data = call("getWarehouseList", {"page": 1, "pageSize": 5}, cfg=cfg)
        steps.append({"step": "getWarehouseList", "ok": True, "data": data})
    except EccangError as e:
        steps.append({"step": "getWarehouseList", "ok": False, "error": str(e)})
        return {"ok": False, "steps": steps}
    try:
        data = call("getOrderList",
                    {"page": 1, "pageSize": 1, "getDetail": 1}, cfg=cfg)
        steps.append({"step": "getOrderList", "ok": True, "data": data})
    except EccangError as e:
        steps.append({"step": "getOrderList", "ok": False, "error": str(e)})
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


# ---------------------------------------------------------------- 拉订单 / 聚合销量

def fetch_orders(cfg: dict, *, date_from: str, date_to: str,
                 page_size: int = 100, max_pages: int = 200,
                 progress=None) -> list[dict]:
    """按「下单时间」区间分页拉订单（含明细）。返回订单列表。

    时间格式：YYYY-MM-DD HH:MM:SS
    """
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        # 实测：参数必须是 snake_case（page_size / get_detail），
        # 驼峰 pageSize/getDetail 会被忽略（pageSize 固定回 50 条、明细不返回）
        biz = {
            "page": page, "page_size": page_size,
            "get_detail": 1, "get_address": 0,
            "condition": {
                "created_date_after": date_from,
                "created_date_before": date_to,
            },
        }
        try:
            data = call("getOrderList", biz, cfg=cfg)
        except EccangError:
            biz["condition"] = {"createdDateAfter": date_from,
                                "createdDateBefore": date_to}
            data = call("getOrderList", biz, cfg=cfg)

        rows = _pick_rows(data)
        if not rows:
            break
        out.extend(rows)
        if progress:
            progress(len(out), page)
        if len(rows) < page_size:
            break
    return out


def _unwrap(data):
    """易仓把真实业务数据放在 biz_content 里，而且是一个 **JSON 字符串**。

    实测返回形如：
        {"code":"200","message":"Success",...,
         "biz_content":"{\"total\":\"285586\",\"data\":[{...}]}"}
    所以要二次解析。部分接口也可能直接给 dict，两种情况都兼容。
    """
    if isinstance(data, str):
        try:
            return json.loads(data)
        except Exception:
            return data
    if isinstance(data, dict):
        bc = data.get("biz_content")
        if isinstance(bc, str):
            try:
                return json.loads(bc)
            except Exception:
                pass
        elif isinstance(bc, (dict, list)):
            return bc
    return data


def _pick_rows(data) -> list[dict]:
    """从各种可能的返回结构里挖出订单数组。"""
    data = _unwrap(data)
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for k in ("data", "list", "records", "rows", "items", "result", "orders"):
        v = data.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, (dict, str)):
            got = _pick_rows(v)
            if got:
                return got
    return []


def aggregate_sales(orders: list[dict]) -> dict:
    """订单 → {by_sku_shop: {(sku, shop, month): qty}, by_sku: {...}}。

    店铺取「平台账号」(userAccount / platformAccount) 或店铺名；
    日期取「下单时间」(createdDate) 或「仓库发货时间」。
    """
    by_ss: dict[tuple[str, str, str], int] = {}
    by_sku: dict[tuple[str, str], int] = {}
    sample = None
    for od in orders:
        shop = _first(od, ("user_account", "userAccount", "platform_user_name",
                           "shop_name", "store_name", "shopName"))
        created = _first(od, ("date_create", "date_create_platform",
                              "createdDate", "platform_paid_date"))
        month = _month(created)
        items = od.get("order_details") or od.get("orderDetails") or \
                od.get("details") or od.get("items") or []
        if sample is None:
            sample = {"order_keys": sorted(od.keys())[:24],
                      "detail_sample": (items[0] if items else None)}
        for it in items:
            sku = _first(it, ("product_sku", "productSku", "sku",
                              "warehouse_sku", "warehouseSku"))
            if not sku:
                continue
            try:
                qty = int(float(_first(it, ("product_num", "productNum",
                                            "quantity", "qty")) or 1))
            except Exception:
                qty = 1
            if month:
                by_ss[(sku, shop or "", month)] = by_ss.get((sku, shop or "", month), 0) + qty
            by_sku[(sku, month or "")] = by_sku.get((sku, month or ""), 0) + qty
    return {
        "orders": len(orders),
        "by_sku_shop": [{"sku": k[0], "shop": k[1], "month": k[2], "qty": v}
                        for k, v in by_ss.items()],
        "by_sku": [{"sku": k[0], "month": k[1], "qty": v}
                   for k, v in by_sku.items()],
        "sample": sample,
    }


def _first(d: dict, keys) -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _month(s: str) -> str:
    s = (s or "").strip()
    return s[:7] if len(s) >= 7 else ""
