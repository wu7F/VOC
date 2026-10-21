# -*- coding: utf-8 -*-
"""VOC 洞察平台 · 后端服务（FastAPI）。

启动：
    python 启动平台.py
或：
    uvicorn server.app:app --host 127.0.0.1 --port 8848
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import auth, llm, pipeline, aftersale as aftersale_module
from .paths import config_path, ensure_dirs, web_dir
from .store import Store, _LOCK as _STORE_LOCK


def jsonable_error(msg: str, **extra) -> dict:
    """构造结构化错误详情，供前端区分错误类型并展示细节。"""
    return {"msg": msg, **extra}

_PATHS = ensure_dirs()
BASE = _PATHS["root"]          # 数据根目录（产出 / 配置 / 演示数据都挂这里）
WEB = web_dir()                # 前端静态资源（打包后随 exe 走）
RUNS = _PATHS["runs"]
DATA = _PATHS["demo"]

store = Store(RUNS)
app = FastAPI(title="VOC 洞察平台", version="1.0")

# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------
# 说明：平台要放到公网，必须有这一层。规则——
#   · 未配置口令时：本机（127.0.0.1）照常使用，远程一律拒绝写操作
#   · 配置口令后：访客只读，管理员可增删改
# 静态资源与登录接口本身不拦截，否则会出现"进不去"的死循环。

PUBLIC_PATHS = {"/", "/index.html", "/app.js", "/style.css", "/login.css",
                "/vendor/echarts.min.js", "/favicon.ico",
                "/api/health", "/api/auth/login", "/api/auth/status",
                "/api/auth/me"}

def _is_write(path: str, method: str) -> bool:
    """写操作需要登录（所有账号权限相同，登录即可写）。"""
    return method in ("POST", "PUT", "PATCH", "DELETE")


_PATH_HINT = re.compile(r"[A-Za-z]:\\[^\s\"']+|/[A-Za-z0-9_.\-]+/[A-Za-z0-9_./\-]+")


def _public_error(e: Exception) -> str:
    """把异常信息里可能出现的绝对路径与堆栈剥掉，只留人能看懂的描述。"""
    msg = str(e).strip() or e.__class__.__name__
    msg = _PATH_HINT.sub("<路径>", msg)
    if len(msg) > 400:
        msg = msg[:400] + "…"
    return msg


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/vendor/"):
        return await call_next(request)

    client = request.client
    loopback = bool(client and client.host in ("127.0.0.1", "::1", "localhost"))

    # 一个账号都没有：本机全放行，远程一律拒绝写操作（不会裸奔）
    if not auth.auth_enabled():
        if loopback:
            request.state.email = "local@loopback"
            return await call_next(request)
        if _is_write(path, request.method):
            return JSONResponse(
                {"detail": "尚未配置任何账号，远程写操作被拒绝。"
                           "请在本机运行「账号管理.bat」创建账号后重启服务。"},
                status_code=401)
        request.state.email = None
        return await call_next(request)

    email = auth.read_email(request.cookies.get(auth.COOKIE_NAME))
    if not email:
        return JSONResponse({"detail": "请先登录"}, status_code=401)

    # 所有账号权限相同，登录即可写
    request.state.email = email
    return await call_next(request)


@app.get("/api/auth/me")
def api_auth_me(request: Request):
    """当前会话是谁。前端据此决定是否显示登录页。"""
    email = auth.read_email(request.cookies.get(auth.COOKIE_NAME))
    return {
        "auth_enabled": auth.auth_enabled(),
        "authenticated": bool(email),
        "email": email or "",
    }


class LoginReq(BaseModel):
    email: str = ""
    password: str = ""


@app.get("/api/auth/status")
def api_auth_status():
    """前端据此决定：显示登录页 / 是否已配置账号。"""
    accounts = []
    try:
        accounts = auth.account_list()
    except Exception:
        pass
    return {
        "auth_enabled": auth.auth_enabled(),
        "account_count": len(accounts),
    }


@app.post("/api/auth/login")
def api_auth_login(req: LoginReq, request: Request):
    """校验邮箱 + 口令，成功后下发签名会话 Cookie。"""
    ip = (request.client.host if request.client else "-") or "-"
    ok, wait = auth.check_rate(ip)
    if not ok:
        # 结构化详情：前端据此显示锁定倒计时
        raise HTTPException(429, jsonable_error(
            "尝试过于频繁，已临时锁定", retry_after=wait))

    email = auth.verify_login(req.email, req.password)
    if not email:
        auth.note_failure(ip)
        remaining = auth.remaining_attempts(ip)
        # 不区分"邮箱不存在"与"口令错误"，避免暴露账号注册情况；
        # 但把剩余可尝试次数回传，让前端给出"还可尝试 N 次"的提示
        raise HTTPException(401, jsonable_error(
            "邮箱或密码不正确", remaining=remaining))
    auth.clear_failures(ip)
    resp = JSONResponse({"ok": True, "email": email})
    resp.set_cookie(
        auth.COOKIE_NAME, auth.issue(email),
        max_age=auth.SESSION_TTL, httponly=True, samesite="lax",
        secure=False,  # 本地 http；走 HTTPS 由隧道终止
    )
    return resp


@app.post("/api/auth/logout")
def api_auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME)
    return resp


# ---------------------------------------------------------------------------
# 公网加固
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES = int(os.environ.get("VOC_MAX_UPLOAD_MB", "200")) * 1024 * 1024


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    """对外只给一句话，不泄漏绝对路径 / 堆栈；完整信息写终端与日志。"""
    tb = traceback.format_exc()
    print(f"[VOC] 未处理异常 {request.url.path}:\n{tb}", file=sys.stderr)
    return JSONResponse({"detail": "服务内部错误，请查看服务端日志"}, status_code=500)

PLATFORMS = [
    {
        "id": "Amazon",
        "name": "AMAZON",
        "label_cn": "Amazon",
        "group": "产品VOC",
        "kind": "ecommerce",
        "icon": "amazon",
        "color": "#FF9900",
        "desc": "上传 Amazon 评论导出（含评分、评论正文、ASIN）",
        "hint": "支持列名：Rating / Review text / ASIN / Review date / Product name 等常见别名",
    },
    {
        "id": "Wayfair",
        "name": "WAYFAIR",
        "label_cn": "Wayfair",
        "group": "产品VOC",
        "kind": "ecommerce",
        "icon": "wayfair",
        "color": "#7B189F",
        "desc": "上传 Wayfair 评论导出（含星级、Review text、Annotations 标注）",
        "hint": "若含 Annotations 列，平台会直接采用其打标结果提升功能维度准确度",
    },
    {
        "id": "Reddit",
        "name": "REDDIT",
        "label_cn": "Reddit",
        "group": "产品VOC",
        "kind": "forum",
        "icon": "reddit",
        "color": "#FF4500",
        "desc": "上传 Reddit 帖子 + 评论（支持导出文件或采集结果）",
        "hint": "支持帖子+评论混合文件（type=post/comment），自动取每帖前 N 条评论",
    },
    {
        "id": "AmazonCs",
        "name": "AMAZON 售后",
        "label_cn": "Amazon售后",
        "group": "售后VOC",
        "kind": "aftersale",
        "icon": "amazon",
        "color": "#FF7A00",
        "desc": "上传 Amazon 订单明细（含产品、是否售后、售后原因）",
        "hint": "列名示例：产品名/ASIN · 是否售后（是/否 或 退/换/仅退款）· 售后原因。系统按产品聚合售后率与原因分布。",
    },
    {
        "id": "WayfairCs",
        "name": "WAYFAIR 售后",
        "label_cn": "Wayfair售后",
        "group": "售后VOC",
        "kind": "aftersale",
        "icon": "wayfair",
        "color": "#9B27B0",
        "desc": "上传 Wayfair 订单明细（含产品、是否售后、售后原因）",
        "hint": "列名示例：产品名/SKU · 订单状态（退/换/正常）· 售后原因。系统按产品聚合售后率与原因分布。",
    },
]

# 售后 VOC 平台（走独立聚合管线，不走评论文本标注）
AFTERSALE_PLATFORMS = {p["id"] for p in PLATFORMS if p.get("kind") == "aftersale"}


# ---------------------------------------------------------------------------
# 静态资源
# ---------------------------------------------------------------------------

def _nocache() -> str:
    """前端资源一律不缓存。

    背景：Cloudflare 边缘按源站 `Cache-Control` 缓存静态资源。实测 app.js 曾被缓存
    `max-age=14400`（4 小时），导致改完代码前端拿到的还是旧版，表现为"改了没生效"。
    这里显式下发 no-cache，让浏览器与边缘每次都回源校验。
    静态资源只有几百 KB，回源开销可以忽略，正确性优先。
    """
    return "no-cache, no-store, must-revalidate"


def _index_html() -> bytes:
    """首页 HTML：注入构建号，让 app.js/style.css 的 URL 随代码变化而变化，
    即使某层缓存不理会 Cache-Control 也能强制穿透。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    b = _web_build()
    for asset in ("/app.js", "/style.css", "/login.css"):
        html = html.replace(f'{asset}"', f'{asset}?v={b}"')
    return html.encode("utf-8")


@app.get("/")
def index():
    return Response(_index_html(), media_type="text/html; charset=utf-8",
                    headers={"Cache-Control": _nocache()})


@app.get("/favicon.ico")
def favicon():
    """避免浏览器控制台出现 favicon 404 噪声。"""
    return Response(status_code=204)


@app.get("/app.js")
def app_js():
    return FileResponse(WEB / "app.js", media_type="application/javascript",
                        headers={"Cache-Control": _nocache()})


@app.get("/style.css")
def style_css():
    return FileResponse(WEB / "style.css", media_type="text/css",
                        headers={"Cache-Control": _nocache()})


@app.get("/login.css")
def login_css():
    return FileResponse(WEB / "login.css", media_type="text/css",
                        headers={"Cache-Control": _nocache()})


@app.get("/vendor/echarts.min.js")
def echarts_js():
    return FileResponse(WEB / "vendor" / "echarts.min.js", media_type="application/javascript")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/platforms")
def api_platforms():
    return {"platforms": PLATFORMS}


def _web_build() -> int:
    """前端构建号 = web 静态文件的最新修改时间。

    前端启动时记下这个值；页面重新获得焦点时若发现服务端已变化，说明服务被重启、
    前端文件已更新，提示用户刷新，避免旧 JS 配新 HTML 的错位。
    """
    ts = 0.0
    for name in ("index.html", "app.js", "style.css"):
        try:
            ts = max(ts, (WEB / name).stat().st_mtime)
        except OSError:
            continue
    return int(ts)


@app.get("/api/health")
def api_health():
    return {"ok": True, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "build": _web_build()}


@app.get("/api/engine")
def api_engine():
    """当前分析引擎配置（密钥打码）。"""
    cfg = llm.public_config()
    return {"llm": cfg, "engines": [
        {"id": "auto", "label": "自动", "desc": "已配置 LLM 则用 LLM，失败自动回退内置引擎"},
        {"id": "llm", "label": "LLM", "desc": "强制使用 LLM（失败即报错，不静默降级）"},
        {"id": "offline", "label": "内置引擎", "desc": "本地词典规则，无需联网，约 100 单元/秒"},
    ]}


@app.post("/api/engine/test")
def api_engine_test():
    """LLM 连通性自检（会真的发一次最小请求）。"""
    return llm.preflight()


@app.get("/api/datasets")
def api_datasets():
    return {"datasets": store.list()}


@app.get("/api/datasets/{did}")
def api_dataset(did: str):
    rec = store.get(did)
    if not rec:
        raise HTTPException(404, "数据集不存在")
    return rec


@app.get("/api/datasets/{did}/result")
def api_result(did: str):
    f = store.dataset_dir(did) / "result.json"
    if not f.exists():
        raise HTTPException(404, "结果尚未生成")
    return json.loads(f.read_text(encoding="utf-8"))


@app.get("/api/datasets/{did}/report")
def api_report(did: str):
    f = store.dataset_dir(did) / "report.md"
    if not f.exists():
        raise HTTPException(404, "报告尚未生成")
    return PlainTextResponse(f.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")


@app.delete("/api/datasets/{did}")
def api_delete(did: str):
    if not store.remove(did):
        raise HTTPException(404, "数据集不存在")
    return {"ok": True}


class DeleteManyReq(BaseModel):
    ids: list[str] = []


@app.post("/api/datasets/delete")
def api_delete_many(req: DeleteManyReq):
    """批量删除所选记录。"""
    if not req.ids:
        raise HTTPException(400, "未提供要删除的记录 id")
    n = store.remove_many(req.ids)
    return {"ok": True, "deleted": n, "requested": len(req.ids)}


# ---------------------------------------------------------------------------
# 磁盘残留清理（删除报告后，产出目录与上传原件一律不留）
# ---------------------------------------------------------------------------

@app.get("/api/maintenance/status")
def api_maintenance_status():
    """统计索引里查不到的产出目录与无主上传原件。"""
    return store.disk_report()


@app.post("/api/maintenance/cleanup")
def api_maintenance_cleanup():
    """清除孤立产出目录与无主上传原件。

    只会删除**不在 `runs/_index.json` 里**的 `ds_*` 目录，以及不再被任何记录引用的
    上传原件；正在排队/运行的任务目录在索引中，因此不会被误删。
    """
    return store.cleanup()


@app.get("/api/datasets/{did}/events")
async def api_events(did: str):
    """SSE 进度推送。"""
    async def gen():
        last = None
        for _ in range(3600):
            rec = store.get(did)
            if not rec:
                yield f"event: error\ndata: {json.dumps({'message': '数据集不存在'})}\n\n"
                return
            snap = {"status": rec["status"], "progress": rec["progress"],
                    "message": rec["message"], "error": rec.get("error", ""),
                    "engine_used": rec.get("engine_used", ""),
                    "warnings": rec.get("warnings", [])}
            if snap != last:
                yield f"data: {json.dumps(snap, ensure_ascii=False)}\n\n"
                last = snap
            if rec["status"] in ("done", "error"):
                return
            await asyncio.sleep(0.4)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _run_job(did: str, files: list[str], platform: str, category: str,
             brands: str, comment_limit: int, engine: str = "auto") -> None:
    def progress(pct: int, msg: str) -> None:
        store.update(did, progress=max(0, min(100, int(pct))), message=msg)

    # 售后 VOC：走独立聚合管线（订单明细 → 售后率/原因分布），不走 LLM 文本标注
    if platform in AFTERSALE_PLATFORMS:
        return _run_aftersale_job(did, files, platform, category, progress)

    try:
        store.update(did, status="running", progress=2, message="开始分析…", error="")
        out = store.dataset_dir(did)
        result = pipeline.run_analysis(
            files=[Path(f) for f in files],
            platform=platform,
            category=category,
            brands=brands,
            comment_limit=comment_limit,
            out_dir=out,
            progress=progress,
            engine=engine,
        )
        store.update(
            did, status="done", progress=100,
            message=f"完成 · {result['overview']['units_analyzed']} 个单元 · {result['elapsed_sec']}s",
            elapsed=result["elapsed_sec"],
            engine_used=result.get("engine", ""),
            warnings=result.get("warnings", []),
            overview={
                "units": result["overview"]["units_analyzed"],
                "comments": result["overview"]["comments_total"],
                "tag_hits": result["overview"]["tag_hits"],
                "avg_rating": result["overview"]["avg_rating"],
                "purchase": result["overview"]["purchase"],
            },
        )
    except Exception as e:  # noqa: BLE001
        # 面向用户只展示可读原因；完整堆栈写入日志文件与终端，便于排查
        tb = traceback.format_exc()
        print(f"[VOC] 数据集 {did} 分析失败:\n{tb}", file=sys.stderr)
        try:
            (store.dataset_dir(did)).mkdir(parents=True, exist_ok=True)
            ((store.dataset_dir(did)) / "error.log").write_text(tb, encoding="utf-8")
        except Exception:
            pass
        store.update(did, status="error", progress=100, message="分析失败", error=_public_error(e))


def _run_aftersale_job(did: str, files: list[str], platform: str, category: str,
                      progress) -> None:
    try:
        store.update(did, status="running", progress=2, message="开始售后分析…", error="")
        out = store.dataset_dir(did)
        result = aftersale_module.run_aftersale(
            files=[Path(f) for f in files],
            platform=platform,
            category=category,
            out_dir=out,
            progress=progress,
        )
        store.update(
            did, status="done", progress=100,
            message=f"完成 · {result['aftersale']['total_orders']} 单 · "
                    f"售后率 {result['aftersale']['overall_rate']:.1%} · {result['elapsed_sec']}s",
            elapsed=result["elapsed_sec"],
            engine_used="aftersale-aggregate",
            warnings=result.get("warnings", []),
            overview={
                "units": result["overview"]["units_analyzed"],
                "comments": 0,
                "tag_hits": result["overview"]["tag_hits"],
                "avg_rating": None,
                "purchase": "",
            },
        )
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        print(f"[VOC] 数据集 {did} 售后分析失败:\n{tb}", file=sys.stderr)
        try:
            (store.dataset_dir(did)).mkdir(parents=True, exist_ok=True)
            ((store.dataset_dir(did)) / "error.log").write_text(tb, encoding="utf-8")
        except Exception:
            pass
        store.update(did, status="error", progress=100, message="分析失败", error=_public_error(e))


@app.get("/api/aftersale/summary")
def api_aftersale_summary():
    """跨平台聚合售后指标：各平台、各平台各产品的售后率，以及合并后的售后原因分布。

    用于售后VOC 父板块的总览看板（数据来自所有 aftersale 类型且已完成的报告）。
    """
    roll = {"platforms": {}, "products": {}, "reasons": {}}
    with _STORE_LOCK:
        recs = [d for d in store._data["datasets"]
                if d.get("platform") in AFTERSALE_PLATFORMS and d.get("status") == "done"]
    for rec in recs:
        p = rec["platform"]
        rp = (store.dataset_dir(rec["id"]) / "result.json")
        if not rp.exists():
            continue
        try:
            r = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            continue
        a = r.get("aftersale")
        if not a:
            continue
        pf = roll["platforms"].setdefault(p, {"platform": p, "total": 0, "aftersale": 0})
        pf["total"] += a["total_orders"]
        pf["aftersale"] += a["aftersale_orders"]
        for pr in a["products"]:
            key = f"{p}||{pr['product']}"
            bucket = roll["products"].setdefault(key, {
                "platform": p, "product": pr["product"], "total": 0, "aftersale": 0})
            bucket["total"] += pr["total"]
            bucket["aftersale"] += pr["aftersale"]
        for rs in a["reasons"]:
            roll["reasons"][rs["reason"]] = roll["reasons"].get(rs["reason"], 0) + rs["count"]

    def _rate(b):
        b["rate"] = round(b["aftersale"] / b["total"], 4) if b["total"] else 0.0
        return b

    platforms = sorted((_rate(v) for v in roll["platforms"].values()),
                      key=lambda x: -x["rate"])
    products = sorted((_rate(v) for v in roll["products"].values()),
                     key=lambda x: (x["platform"], -x["rate"], -x["total"]))
    rsum = sum(roll["reasons"].values()) or 1
    reasons = sorted(({"reason": k, "count": v, "pct": round(v / rsum, 4)}
                     for k, v in roll["reasons"].items()), key=lambda x: -x["count"])
    tot = sum(p["total"] for p in platforms)
    aft = sum(p["aftersale"] for p in platforms)
    return {
        "platforms": platforms,
        "products": products,
        "reasons": reasons,
        "overall": {
            "total": tot, "aftersale": aft,
            "rate": round(aft / tot, 4) if tot else 0.0,
        },
        "datasets": len(recs),
    }


@app.post("/api/upload")
async def api_upload(
    files: list[UploadFile] = File(...),
    platform: str = Form("Amazon"),
    category: str = Form(""),
    brands: str = Form(""),
    comment_limit: int = Form(20),
    dataset_name: str = Form(""),
    engine: str = Form("auto"),
):
    if not files:
        raise HTTPException(400, "请至少上传一个数据文件")
    saved: list[str] = []
    names: list[str] = []
    for f in files:
        content = await f.read()
        if not content:
            continue
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                413,
                f"文件「{f.filename}」超过单文件上限 "
                f"{MAX_UPLOAD_BYTES // 1024 // 1024}MB，请拆分后再上传")
        saved.append(store.save_upload(f.filename or "upload.csv", content))
        names.append(f.filename or "upload.csv")
    if not saved:
        raise HTTPException(400, "上传的文件为空")

    engine = engine if engine in ("auto", "llm", "offline") else "auto"
    name = dataset_name.strip() or f"{platform} · {category or '未命名品类'}"
    rec = store.add(name=name, platform=platform, category=category.strip() or "未命名品类",
                    brands=brands.strip(), comment_limit=int(comment_limit),
                    files=names, engine=engine, uploads=saved)
    t = threading.Thread(target=_run_job, args=(rec["id"], saved, platform,
                                                rec["category"], rec["brands"],
                                                int(comment_limit), engine), daemon=True)
    t.start()
    return {"id": rec["id"], "name": rec["name"]}


@app.post("/api/datasets/{did}/rerun")
def api_rerun(did: str):
    rec = store.get(did)
    if not rec:
        raise HTTPException(404, "数据集不存在")
    # 重新定位上次上传的文件（按文件名匹配 _uploads 目录）
    up = store.uploads
    found: list[str] = []
    for n in rec["files"]:
        cand = sorted(up.glob(f"*_{n}"), key=lambda p: p.stat().st_mtime, reverse=True)
        if cand:
            found.append(str(cand[0]))
    if not found:
        raise HTTPException(400, "原始文件已不存在，无法重跑，请重新上传")
    store.update(did, status="queued", progress=0, message="排队中…", error="")
    t = threading.Thread(target=_run_job, args=(did, found, rec["platform"],
                                                rec["category"], rec["brands"],
                                                rec["comment_limit"],
                                                rec.get("engine", "auto")), daemon=True)
    t.start()
    return {"ok": True}


# ---------------------------------------------------------------------------
# PDF 导出（把前端组装好的自包含 HTML 交给本地浏览器渲染成真实 PDF）
# ---------------------------------------------------------------------------

# 依次尝试的浏览器可执行文件；均不存在时再回退到 PATH 查找
_CHROME_CANDIDATES = [
    os.environ.get("VOC_CHROME_PATH", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def _find_browser() -> str | None:
    for c in _CHROME_CANDIDATES:
        if c and Path(c).exists():
            return c
    for name in ("google-chrome", "chrome", "chromium", "chromium-browser", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    return None


class ExportPdfReq(BaseModel):
    html: str = ""
    name: str = "VOC分析报告"


@app.get("/api/export/available")
def api_export_available():
    """前端据此决定按钮文案：本地引擎可用 → 直接下载 PDF；不可用 → 走浏览器打印。"""
    b = _find_browser()
    return {"pdf_engine": bool(b), "browser": Path(b).name if b else ""}


@app.post("/api/export/pdf")
def api_export_pdf(req: ExportPdfReq):
    """接收完整 HTML 文档，用无头浏览器渲染为 PDF 并作为附件返回。

    前端已完成全部工作（六板块 DOM 渲染 + 图表转 PNG 内联），
    因此这里拿到的是一份**自包含静态 HTML**，无需等待任何网络/脚本，
    Chrome 的 --print-to-pdf 可以直接稳定出图。
    """
    html = (req.html or "").strip()
    if len(html) < 200:
        raise HTTPException(400, "导出内容为空，请先在结果页打开一份分析")

    browser = _find_browser()
    if not browser:
        raise HTTPException(503, "本机未找到 Chrome / Edge，无法在服务端生成 PDF，请改用浏览器打印")

    safe = "".join(ch for ch in (req.name or "VOC分析报告") if ch not in '\\/:*?"<>|\r\n\t').strip()
    safe = (safe or "VOC分析报告")[:80]

    tmp = Path(tempfile.mkdtemp(prefix="voc_pdf_"))
    html_path = tmp / "doc.html"
    pdf_path = tmp / "doc.pdf"
    profile = tmp / "profile"
    html_path.write_text(html, encoding="utf-8")

    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-first-run",
        "--no-pdf-header-footer",
        "--disable-extensions",
        "--hide-scrollbars",
        f"--user-data-dir={profile}",          # 隔离用户目录，避免抢占用户已开的浏览器实例
        "--virtual-time-budget=8000",
        f"--print-to-pdf={pdf_path}",
        html_path.as_uri(),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(504, "PDF 生成超时（浏览器无响应），请重试或改用浏览器打印")
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(500, f"调用浏览器失败：{e}")

    if not pdf_path.exists() or pdf_path.stat().st_size < 1000:
        detail = (proc.stderr or b"").decode("utf-8", "ignore")[-400:]
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(500, f"PDF 生成失败：{detail or '未产出文件'}")

    # 用后台任务在响应发送完毕后清理临时目录（含隔离 profile）
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"{safe}.pdf",
        background=BackgroundTask(shutil.rmtree, tmp, True),
    )


# ---------------------------------------------------------------------------
# 演示数据（全部来自用户真实文件，未做任何编造）
# ---------------------------------------------------------------------------

DEMOS = [
    {
        "id": "wayfair",
        "platform": "Wayfair",
        "category": "Nursery Recliner",
        "label": "Wayfair 婴儿房躺椅评论",
        "files": ["wayfair_recliner_reviews.csv"],
        "desc": "97 条真实评论 · 含星级与平台标注",
    },
    {
        "id": "reddit",
        "platform": "Reddit",
        "category": "Nursery Recliner",
        "label": "Reddit 婴儿房躺椅讨论",
        "files": ["reddit_nursery_posts.csv", "reddit_nursery_comments.csv"],
        "desc": "101 篇帖子 + 1817 条评论",
    },
]


@app.get("/api/demo")
def api_demo():
    out = []
    for d in DEMOS:
        if all((DATA / f).exists() for f in d["files"]):
            out.append(d)
    return {"demos": out}


@app.post("/api/demo/{demo_id}")
def api_run_demo(demo_id: str, category: str = Form("")):
    d = next((x for x in DEMOS if x["id"] == demo_id), None)
    if not d:
        raise HTTPException(404, "演示数据不存在")
    paths = [DATA / f for f in d["files"]]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        raise HTTPException(404, f"演示数据文件缺失：{', '.join(missing)}")
    cat = category or d["category"]
    rec = store.add(name=f"演示 · {d['label']}", platform=d["platform"], category=cat,
                    brands="", comment_limit=20, files=[p.name for p in paths],
                    engine="offline")
    t = threading.Thread(target=_run_job,
                         args=(rec["id"], [str(p) for p in paths], d["platform"], cat, "", 20,
                               "offline"),
                         daemon=True)
    t.start()
    return {"id": rec["id"], "name": rec["name"]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8848)
