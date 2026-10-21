# -*- coding: utf-8 -*-
"""飞书表格数据源（电子表格 Sheets / 多维表格 Base）。

用于把飞书里的售后订单明细直接喂进 VOC 平台的售后 VOC 管线，
省掉「导出 Excel → 再上传」这一趟手工活。

支持两类链接：
  · 电子表格   https://xxx.feishu.cn/sheets/<spreadsheet_token>?sheet=<sheet_id>
  · 多维表格   https://xxx.feishu.cn/base/<app_token>?table=<table_id>

鉴权：飞书开放平台自建应用的 app_id / app_secret 换 tenant_access_token。
  · 优先读环境变量 VOC_FEISHU_APP_ID / VOC_FEISHU_APP_SECRET（生产推荐）
  · 其次读 data/feishu_config.json（网页上填过一次就存下）
只用标准库 urllib，不引入新依赖。
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_OPEN = "https://open.feishu.cn/open-apis"
_TOKEN_TTL = 5400  # 飞书 token 有效期 2h，这里留 30 分钟余量

# 进程内 token 缓存：{app_id: (token, expire_ts)}
_TOKENS: dict[str, tuple[str, float]] = {}


class FeishuError(Exception):
    """面向用户的中文错误。"""


# ---------------------------------------------------------------- 链接解析

def parse_feishu_url(raw: str) -> dict:
    """从链接（或裸 token）解析出类型与各 id。

    返回 {"kind": "sheets"|"bitable", "token": ..., "sheet_id": ..., "table_id": ...}
    """
    s = (raw or "").strip()
    if not s:
        raise FeishuError("请填写飞书表格链接。")

    if "://" not in s:
        # 允许直接贴 token：shtcnXXXX（表格）/ bascnXXXX（多维表格）
        if s.startswith("bascn"):
            return {"kind": "bitable", "token": s, "table_id": "", "sheet_id": ""}
        if s.startswith("sht") or s.startswith("shtcn"):
            return {"kind": "sheets", "token": s, "sheet_id": "", "table_id": ""}
        raise FeishuError("无法识别该链接，请粘贴飞书表格的完整网址。")

    try:
        u = urllib.parse.urlparse(s)
    except Exception:
        raise FeishuError("链接格式不正确。")
    q = urllib.parse.parse_qs(u.query)
    path = u.path or ""

    # 多维表格 Base
    m = re.search(r"/base/([A-Za-z0-9]+)", path)
    if m:
        return {
            "kind": "bitable",
            "token": m.group(1),
            "table_id": (q.get("table") or [""])[0],
            "sheet_id": "",
        }

    # 电子表格 Sheets
    m = re.search(r"/sheets/([A-Za-z0-9]+)", path)
    if m:
        return {
            "kind": "sheets",
            "token": m.group(1),
            # 飞书链接里 sheet= 是 sheet_id，table= 有时也指 sheet_id
            "sheet_id": (q.get("sheet") or q.get("table") or [""])[0],
            "table_id": "",
        }

    # 维基 / 文档 / drive 文件夹等，本模块不支持
    raise FeishuError(
        "只支持「电子表格」和「多维表格」链接。\n"
        "请在飞书里点开表格 → 分享 → 复制链接，形如：\n"
        "  https://xxx.feishu.cn/sheets/xxxxxxxx  （电子表格）\n"
        "  https://xxx.feishu.cn/base/xxxxxxxx   （多维表格）")


# ---------------------------------------------------------------- HTTP

def _http_json(url: str, *, method: str = "GET", body: dict | None = None,
               headers: dict | None = None, timeout: int = 30) -> dict:
    data = None
    hdrs = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "ignore")
            j = json.loads(raw)
            msg = j.get("msg") or raw[:200]
        except Exception:
            msg = "HTTP %s" % e.code
        raise FeishuError("飞书接口返回错误：%s" % msg)
    except urllib.error.URLError as e:
        raise FeishuError("无法连接飞书服务器：%s" % (e.reason,))
    except Exception as e:
        raise FeishuError("请求飞书接口失败：%s" % e)

    try:
        j = json.loads(raw)
    except Exception:
        raise FeishuError("飞书返回了非 JSON 内容（可能被网关拦截）：%s" % raw[:200])
    if j.get("code") not in (0, None):
        raise FeishuError("飞书接口报错(%s)：%s" % (j.get("code"), j.get("msg")))
    return j


# ---------------------------------------------------------------- 鉴权

def get_tenant_token(app_id: str, app_secret: str) -> str:
    app_id = (app_id or "").strip()
    app_secret = (app_secret or "").strip()
    if not app_id or not app_secret:
        raise FeishuError("缺少飞书应用凭证（app_id / app_secret）。")

    hit = _TOKENS.get(app_id)
    if hit and hit[1] > time.time():
        return hit[0]

    j = _http_json(
        _OPEN + "/auth/v3/tenant_access_token/internal",
        method="POST",
        body={"app_id": app_id, "app_secret": app_secret},
    )
    token = j.get("tenant_access_token")
    if not token:
        raise FeishuError("未取到 tenant_access_token，请检查 app_id / app_secret 是否正确。")
    _TOKENS[app_id] = (token, time.time() + _TOKEN_TTL)
    return token


# ---------------------------------------------------------------- 取数

def _flat(v) -> str:
    """把多维表格的字段值压成一行字符串。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        parts = []
        for x in v:
            if isinstance(x, dict):
                for k in ("text", "name", "value", "full_name", "email", "id"):
                    if x.get(k) is not None:
                        parts.append(str(x[k]))
                        break
                else:
                    parts.append(json.dumps(x, ensure_ascii=False))
            else:
                parts.append(str(x))
        return " / ".join(p for p in parts if p)
    if isinstance(v, dict):
        for k in ("text", "name", "value", "full_name"):
            if v.get(k) is not None:
                return str(v[k])
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _sheets_of(token: str, spreadsheet_token: str) -> list[dict]:
    j = _http_json(
        "%s/sheets/v3/spreadsheets/%s/sheets/query" % (_OPEN, spreadsheet_token),
        headers={"Authorization": "Bearer " + token},
    )
    return (j.get("data") or {}).get("sheets") or []


def fetch_sheets_rows(token: str, spreadsheet_token: str, sheet_id: str = "") -> tuple[list[list], str]:
    """读电子表格，返回 (二维数组, 用到的 sheet 名)。"""
    target = sheet_id
    title = ""
    if not target:
        sheets = _sheets_of(token, spreadsheet_token)
        if not sheets:
            raise FeishuError("该电子表格里没有可读的工作表。")
        target = sheets[0].get("sheet_id") or ""
        title = sheets[0].get("title") or ""
    else:
        try:
            for s in _sheets_of(token, spreadsheet_token):
                if s.get("sheet_id") == target:
                    title = s.get("title") or ""
                    break
        except FeishuError:
            pass

    rng = urllib.parse.quote(target, safe="")
    j = _http_json(
        "%s/sheets/v2/spreadsheets/%s/values/%s" % (_OPEN, spreadsheet_token, rng),
        headers={"Authorization": "Bearer " + token},
    )
    values = ((j.get("data") or {}).get("valueRange") or {}).get("values") or []
    # 飞书会跳过完全空白的行，这里补齐列数，保证每行列数一致
    width = max((len(r) for r in values), default=0)
    values = [list(r) + [""] * (width - len(r)) for r in values]
    return values, title or target


def fetch_bitable_rows(token: str, app_token: str, table_id: str = "") -> tuple[list[list], str]:
    """读多维表格记录，返回 (二维数组, 表名)。"""
    tables = []
    if not table_id:
        j = _http_json(
            "%s/bitable/v1/apps/%s/tables?page_size=100" % (_OPEN, app_token),
            headers={"Authorization": "Bearer " + token},
        )
        tables = (j.get("data") or {}).get("items") or []
        if not tables:
            raise FeishuError("该多维表格里没有可读的数据表。")
        table_id = tables[0].get("table_id") or ""
        title = tables[0].get("name") or ""
    else:
        title = ""
        try:
            j = _http_json(
                "%s/bitable/v1/apps/%s/tables?page_size=100" % (_OPEN, app_token),
                headers={"Authorization": "Bearer " + token},
            )
            for t in (j.get("data") or {}).get("items") or []:
                if t.get("table_id") == table_id:
                    title = t.get("name") or ""
                    break
        except FeishuError:
            pass

    # 拉全部记录（分页）
    items: list[dict] = []
    page_token = ""
    for _ in range(50):  # 最多 50 页 × 500 条 = 25000 条
        url = ("%s/bitable/v1/apps/%s/tables/%s/records?page_size=500"
               % (_OPEN, app_token, table_id))
        if page_token:
            url += "&page_token=" + urllib.parse.quote(page_token, safe="")
        j = _http_json(url, headers={"Authorization": "Bearer " + token})
        data = j.get("data") or {}
        items.extend(data.get("items") or [])
        if not data.get("has_more"):
            break
        page_token = data.get("page_token") or ""
        if not page_token:
            break

    # 所有字段名（保持首次出现顺序）
    headers: list[str] = []
    for it in items:
        for k in (it.get("fields") or {}).keys():
            if k not in headers:
                headers.append(k)
    rows = [headers]
    for it in items:
        f = it.get("fields") or {}
        rows.append([_flat(f.get(h)) for h in headers])
    return rows, title or table_id


def fetch_rows(*, url: str, app_id: str, app_secret: str) -> dict:
    """入口：解析链接 → 鉴权 → 取数。返回 {headers, rows, kind, title, source}"""
    info = parse_feishu_url(url)
    token = get_tenant_token(app_id, app_secret)
    if info["kind"] == "sheets":
        values, title = fetch_sheets_rows(token, info["token"], info.get("sheet_id", ""))
    else:
        values, title = fetch_bitable_rows(token, info["token"], info.get("table_id", ""))

    # 去掉尾部全空行
    while values and not any(str(c).strip() for c in values[-1]):
        values.pop()
    if not values:
        raise FeishuError("表格里没有读到任何内容。")

    headers = [str(c).strip() for c in values[0]]
    rows = values[1:]
    return {
        "kind": info["kind"],
        "title": title,
        "headers": headers,
        "rows": rows,
        "row_count": len(rows),
        "source": url,
    }


# ---------------------------------------------------------------- 转 CSV

def to_csv(headers: list[str], rows: list[list]) -> bytes:
    """把二维数组转成 UTF-8(BOM) CSV，直接喂给现有的 read_table 适配器。"""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(headers)
    for r in rows:
        w.writerow(["" if c is None else str(c) for c in r])
    return buf.getvalue().encode("utf-8-sig")


# ---------------------------------------------------------------- 凭证配置

def _config_path(data_dir: Path) -> Path:
    return Path(data_dir) / "feishu_config.json"


def load_config(data_dir: Path) -> dict:
    """读取已保存的凭证：环境变量优先，其次配置文件。"""
    cfg = {"app_id": os.environ.get("VOC_FEISHU_APP_ID", "").strip(),
           "app_secret": os.environ.get("VOC_FEISHU_APP_SECRET", "").strip(),
           "source": "env" if os.environ.get("VOC_FEISHU_APP_ID") else ""}
    p = _config_path(data_dir)
    if p.is_file():
        try:
            saved = json.loads(p.read_text(encoding="utf-8"))
            if not cfg["app_id"]:
                cfg["app_id"] = (saved.get("app_id") or "").strip()
                cfg["source"] = "file"
            if not cfg["app_secret"]:
                cfg["app_secret"] = (saved.get("app_secret") or "").strip()
                if cfg["source"] == "":
                    cfg["source"] = "file"
        except Exception:
            pass
    return cfg


def save_config(data_dir: Path, app_id: str, app_secret: str) -> dict:
    """把凭证存到 data 目录（仅当环境变量未配置时才需要）。"""
    app_id = (app_id or "").strip()
    app_secret = (app_secret or "").strip()
    if not app_id or not app_secret:
        raise FeishuError("app_id 和 app_secret 都要填写。")
    p = _config_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"app_id": app_id, "app_secret": app_secret},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return load_config(data_dir)
