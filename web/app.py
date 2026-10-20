#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web/app.py · C2 的 HTTP 层（FastAPI）。

**它仍然是薄的。** 分析逻辑一行都不在这里 —— 全部经由 ``voc_core.pipeline``：

========================================= ==================================
Web 关心的事                                调用的内核接口
========================================= ==================================
提交任务                                     ``JobSpec``
跑分析                                      ``pipeline.run_job(spec, job_dir)``
任务列表（**只读**）                          ``pipeline.list_jobs(runs_dir)``
任务详情（**只读**）                          ``pipeline.load_job(job_dir)``
报告                                         直接回吐 ``report/dashboard.html``
========================================= ==================================

C1 → C2 换了四样东西，**内核一行没动**：

======================== ============================================================
组件                      C1  →  C2
======================== ============================================================
队列                      ``queue.json`` + 线程锁  →  ``queue.db``（SQLite，跨进程原子认领）
跑 job 的人                 Web 进程内的线程   →   **独立 worker 进程**（默认 ``--worker spawn``）
跑完看到的日志              内存环形缓冲       →  ``<state>/logs/<job_id>.log``（落盘，跨进程可读）
谁能看什么                  没有这个概念       →  可选认证 + owner 隔离（``VOC_WEB_AUTH=1``）
上传落在哪                  本机 ``uploads/``  →  :class:`web.storage.Storage`（本机 or S3）
======================== ============================================================

启动::

    python web/serve.py                       # Web + 自动拉起一个 worker 子进程
    python web/serve.py --worker inproc       # 退回 C1：worker 跑在 Web 进程里
    python web/serve.py --worker off          # 只起 Web；worker 自己用 web.worker_main 起

环境变量（都有默认值，全部落在平台目录内）::

    VOC_WEB_RUNS_DIR     默认 <platform>/runs
    VOC_WEB_UPLOAD_DIR   默认 <platform>/uploads
    VOC_WEB_STATE_DIR    默认 <platform>/web_state
    VOC_WEB_WORKER_MODE  spawn（默认）/ inproc / off
    VOC_WEB_AUTH         1=开启认证 / 0=强制关闭 / 未设=有用户就开
    VOC_WEB_STORAGE      local（默认）/ s3
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

PLATFORM = Path(__file__).resolve().parents[1]
if str(PLATFORM) not in sys.path:
    sys.path.insert(0, str(PLATFORM))

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from voc_core import pipeline as P  # noqa: E402
from web import auth as AUTH  # noqa: E402
from web import logs as LOG  # noqa: E402
from web import storage as STORAGE  # noqa: E402
from web.store import (  # noqa: E402
    STATE_FAILED, STATE_INTERRUPTED, STATE_QUEUED, STATE_RUNNING, Queue,
)
from web.worker import Worker, new_job_id  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"

RUNS_DIR = Path(os.environ.get("VOC_WEB_RUNS_DIR") or PLATFORM / "runs")
UPLOAD_DIR = Path(os.environ.get("VOC_WEB_UPLOAD_DIR") or PLATFORM / "uploads")
STATE_DIR = Path(os.environ.get("VOC_WEB_STATE_DIR") or PLATFORM / "web_state")

#: 允许读回的产物（相对 ``<job>/report`` 或 ``<job>/out``）。**白名单**，
#: 不做任意路径拼接 —— 否则就是一个目录穿越洞。
REPORT_FILES = {
    "dashboard": ("report", "dashboard.html"),
    "insight": ("report", "insight_report.md"),
    "quality": ("report", "quality_report.json"),
    "dimensions": ("out", "dimensions.json"),
    "features": ("out", "features.json"),
}

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\u4e00-\u9fff-]+")

# ==========================================================================
# 运行模式
# ==========================================================================

WORKER_MODES = ("spawn", "off", "inproc")
WORKER_MODE = (os.environ.get("VOC_WEB_WORKER_MODE") or "spawn").strip().lower()
if WORKER_MODE not in WORKER_MODES:                      # 不认识的按默认来，别让服务起不来
    WORKER_MODE = "spawn"

queue = Queue(STATE_DIR)
app = FastAPI(title="VOC 洞察平台", version="0.2.0",
              description="通用 VOC 分析 —— Stage 9 C2（跨进程队列 / 独立 worker / 可选认证）")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# ==========================================================================
# 单例：认证 / 存储 / inproc worker / spawn 出来的子进程
# ==========================================================================

_cache: dict[str, Any] = {}
_worker_proc: subprocess.Popen | None = None
_worker_log_file: Any = None          #: 保持句柄打开，免得被 GC 关掉
_worker_proc_started: float = 0.0


def get_auth() -> AUTH.Auth:
    """认证器。懒构造 —— 否则 import 本模块就会在盘上落 ``secret.key``。"""
    if "auth" not in _cache:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _cache["auth"] = AUTH.Auth(STATE_DIR)
    return _cache["auth"]


def get_storage() -> STORAGE.Storage:
    """对象存储后端。失败时**明着抛**（配了 S3 却写本机，这种错要换机器那天才炸）。"""
    if "storage" not in _cache:
        _cache["storage"] = STORAGE.storage_from_env(UPLOAD_DIR)
    return _cache["storage"]


def get_worker() -> Worker:
    """同进程 worker（只有 ``--worker inproc`` 用得上）。"""
    if "worker" not in _cache:
        _cache["worker"] = Worker(queue, RUNS_DIR, log_dir=LOG.log_dir(STATE_DIR))
    return _cache["worker"]


def set_auth(auth: AUTH.Auth) -> None:
    """测试注入点：换掉认证器（同时也被 :func:`get_auth` 之后的调用看到）。"""
    _cache["auth"] = auth


def set_storage(storage: STORAGE.Storage) -> None:
    _cache["storage"] = storage


#: 兼容 C1 的调用方/测试（``APP.worker``）—— inproc 模式下拿到的是真 worker
class _LazyWorkerProxy:  # pragma: no cover —— 只是个取用的壳
    def __getattr__(self, name: str) -> Any:
        return getattr(get_worker(), name)

    def is_alive(self) -> bool:
        return get_worker().is_alive()


worker = _LazyWorkerProxy()


# ==========================================================================
# 认证
# ==========================================================================


def _current_user(request: Request) -> dict[str, Any] | None:
    """当前登录者。**未启用认证时返回一个 admin** —— 本机单用户 = C1 的行为，不该变。"""
    a = get_auth()
    #: **每次判定都要先看一眼用户表有没有变**。服务通常比第一个用户早起来，
    #: 不 reload 的话管理员建完人，服务会一直停在免登录（详见 ``Auth.maybe_reload``）。
    a.maybe_reload()
    if not a.enabled:
        return {"username": "", "role": AUTH.ROLE_ADMIN}
    return a.read(request.cookies.get(AUTH.COOKIE_NAME, ""))


def _require_user(request: Request) -> dict[str, Any]:
    u = _current_user(request)
    if u is None:
        raise HTTPException(401, "需要登录")
    return u


def _may_view(request: Request, owner: str) -> bool:
    """能否看某个 job（admin 看全部，user 只看自己的）。"""
    return get_auth().can_see(_current_user(request), owner)


def _owner_of(request: Request) -> str:
    return str(_current_user(request).get("username") or "")


# ==========================================================================
# worker 存活判定
# ==========================================================================


def _worker_alive() -> bool:
    """Web 与 worker 不同进程，``thread.is_alive()`` 就用不上了 —— 换三个来源交叉判定。

    1. spawn 出来的子进程还在（``poll() is None``）
    2. 数据库里有心跳新鲜的 worker 行（**这一条覆盖「worker 起在别的机器上」**）
    3. 兜底：管道/fork 都失效时的最后一次手段
    """
    if WORKER_MODE == "inproc":
        return get_worker().is_alive()
    if _worker_proc is not None and _worker_proc.poll() is None:
        return True
    try:
        return any(w.get("alive") for w in queue.workers())
    except Exception:  # noqa: BLE001 —— 队列坏了也得回“活着与否”，不能让探活页炸
        return False


@app.get("/api/workers")
def list_workers() -> dict[str, Any]:
    """所有登记过的 worker + 心跳是否新鲜（C2 才有这张表，用来看「谁还在干活」）。"""
    _ = _require_user
    return {"workers": queue.workers(), "mode": WORKER_MODE}


# ==========================================================================
# 生命周期
# ==========================================================================


@app.on_event("startup")
def _startup() -> None:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG.log_dir(STATE_DIR).mkdir(parents=True, exist_ok=True)

    if WORKER_MODE == "inproc":
        w = get_worker()
        if not w.is_alive():
            w.start()
    elif WORKER_MODE == "spawn":
        #: 见 spawn_worker 的说明：本机开那个日志文件可能**阻塞**十几秒。
        #: 在启动钩子里干 = 整个 Web 起不来 —— 所以丢到后台线程。
        threading.Thread(target=spawn_worker, name="worker-boot",
                         daemon=True).start()


@app.on_event("shutdown")
def _shutdown() -> None:
    if WORKER_MODE == "inproc":
        get_worker().stop()
    stop_worker()


_worker_boot_lock = threading.Lock()


def _open_worker_log(logdir: Path) -> tuple[Any, Path | None]:
    """开 worker 子进程要用的 stdout 日志文件。

    **本机（Windows）有瞬时文件锁**：杀软会扣住刚被写过的同名文件几秒，
    ``open(..., 'ab')`` 会抛 ``PermissionError [WinError 13]``，实测还会原地
    **阻塞十几秒**（同一个坑在 ``os.replace`` 上是 11.68s）。开工前它在启动钩子里，
    于是「服务起不来」。

    所以：退避重试几次；还不行就换个名字；再不行就返回 ``None``，
    stdout 去 DEVNULL。**少一份日志可以，服务起不来不行。**
    """
    delay = 0.3
    for attempt in range(4):
        stamp = int(time.time())
        name = "worker_proc.log" if attempt == 0 else "worker_proc.%d-%d.log" % (stamp, attempt)
        target = logdir / name
        try:
            return target.open("ab"), target
        except OSError as exc:
            print("[app] worker 日志打不开（%s），%.1fs 后重试" % (exc, delay),
                  file=sys.stderr, flush=True)
            time.sleep(delay)
            delay *= 2
    print("[app] worker 日志始终打不开，改用 DEVNULL 拉起子进程",
          file=sys.stderr, flush=True)
    return None, None


def spawn_worker() -> subprocess.Popen | None:
    """拉起一个 ``python -m web.worker_main`` 子进程。

    **stdout 必须重定向到文件，绝不能用 PIPE。** C1 踩过：内核往 stdout 大量回显，
    而干活的是另一个进程，PIPE 的 64KB 缓冲写满就阻塞，现象是
    「中间件全写完了但步骤永不返回」（实测挂死 9 分钟）。

    本函数会**阻塞**（见 :func:`_open_worker_log`），所以启动钩子里不直接调它，
    而是丢到后台线程。
    """
    global _worker_proc, _worker_log_file, _worker_proc_started
    with _worker_boot_lock:
        if _worker_proc is not None and _worker_proc.poll() is None:
            return _worker_proc

        logdir = LOG.log_dir(STATE_DIR)
        logdir.mkdir(parents=True, exist_ok=True)
        #: 用二进制句柄，别让子进程走 unicode 编码器
        fh, _logpath = _open_worker_log(logdir)
        env = dict(os.environ)
        env["VOC_WEB_STATE_DIR"] = str(STATE_DIR)
        env["VOC_WEB_RUNS_DIR"] = str(RUNS_DIR)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "web.worker_main",
                 "--state-dir", str(STATE_DIR), "--runs-dir", str(RUNS_DIR)],
                cwd=str(PLATFORM),
                env=env,
                stdout=(fh if fh is not None else subprocess.DEVNULL),
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            )
        except OSError as exc:  # 拉不起来：降级成“没 worker”，Web 照样能响应
            print("[app] worker 子进程起不来（%s）—— Web 仍可用，job 会停在 queued" % (exc,),
                  file=sys.stderr, flush=True)
            if fh is not None:
                fh.close()
            return None
        _worker_proc = proc
        _worker_log_file = fh
        _worker_proc_started = time.time()
        return proc


def stop_worker() -> None:
    """尝试优雅地收掉 spawn 出来的 worker。

    Windows 上 :meth:`Popen.terminate` 是 ``TerminateProcess`` —— worker 拿不到信号。
    没关系：它一死心跳就停，``reap_stale`` 30s 后会把它认领的 running 项回收成
    ``interrupted``，重跑即从断点续跑（内核自带续跑）。
    """
    global _worker_proc, _worker_log_file
    if _worker_proc is None:
        return
    try:
        if _worker_proc.poll() is None:
            _worker_proc.terminate()
            try:
                _worker_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _worker_proc.kill()
    except Exception:  # noqa: BLE001
        pass
    finally:
        _worker_proc = None
        if _worker_log_file is not None:
            try:
                _worker_log_file.close()
            except Exception:  # noqa: BLE001
                pass
            _worker_log_file = None
        if _worker_log_file is not None:
            try:
                _worker_log_file.close()
            except Exception:  # noqa: BLE001
                pass
            _worker_log_file = None


# ==========================================================================
# 页面
# ==========================================================================


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))


# ==========================================================================
# 元信息（表单要用的枚举 + 前端要显示的模式）
# ==========================================================================


@app.get("/api/meta")
def meta(request: Request) -> dict[str, Any]:
    from voc_core import adapters as A
    from voc_core import analysis_frame as AF

    try:
        frames = AF.available_frames()
    except Exception:  # noqa: BLE001
        frames = ["consumer"]
    try:
        sources = sorted(A.available())
    except Exception:  # noqa: BLE001
        sources = ["auto"]
    a = get_auth()
    u = _current_user(request)
    return {
        "frames": frames,
        "sources": ["auto", *sources],
        "profiles": ["auto", "reddit", "amazon", "wayfair"],
        "runs_dir": str(RUNS_DIR),
        "upload_dir": str(UPLOAD_DIR),
        "python": sys.executable,
        "worker_mode": WORKER_MODE,
        "auth_enabled": a.enabled,
        "storage": get_storage().describe(),
        "me": ({"username": u["username"], "role": u.get("role", "")} if u else None),
    }


# ==========================================================================
# 登录 / 登出
# ==========================================================================


# --- 登录限流：防爆破，公网部署必需 ----------------------------------------
#: 失败计数与锁定都落在内存里（单实例足够；多实例可换 Redis）。
_LOGIN_FAIL: dict[str, list[float]] = {}
_LOGIN_LOCK: dict[str, float] = {}
_LOGIN_MAX = 8          # 失败达到此数触发锁定
_LOGIN_WINDOW = 300.0   # 失败计数窗口（秒）
_LOGIN_LOCK_S = 300     # 锁定时长（秒）


def _client_ip(request: Request) -> str:
    """取真实客户端 IP：优先 X-Forwarded-For（Cloudflare / Render 反代会带）。"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _login_state(host: str) -> dict:
    """返回当前限流态：锁定中 {locked,retry_after} 或 {locked:False,remaining}。"""
    now = time.time()
    unlock = _LOGIN_LOCK.get(host, 0.0)
    if unlock > now:
        return {"locked": True, "retry_after": int(unlock - now) + 1}
    fails = [t for t in _LOGIN_FAIL.get(host, []) if now - t < _LOGIN_WINDOW]
    _LOGIN_FAIL[host] = fails
    return {"locked": False, "remaining": max(0, _LOGIN_MAX - len(fails))}


def _login_note_fail(host: str) -> dict:
    """记一次失败，返回更新后的限流态（可能进入锁定）。"""
    now = time.time()
    fails = _LOGIN_FAIL.setdefault(host, [])
    fails.append(now)
    fails[:] = [t for t in fails if now - t < _LOGIN_WINDOW]
    if len(fails) >= _LOGIN_MAX:
        _LOGIN_LOCK[host] = now + _LOGIN_LOCK_S
        return {"locked": True, "retry_after": int(_LOGIN_LOCK_S)}
    return {"locked": False, "remaining": max(0, _LOGIN_MAX - len(fails))}


def _login_clear(host: str) -> None:
    _LOGIN_FAIL.pop(host, None)
    _LOGIN_LOCK.pop(host, None)


@app.post("/api/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)) -> JSONResponse:
    """口令换 cookie。**认证关闭时返回 400**；失败带「剩余尝试次数」；连续失败临时锁定。

    错误统一用 ``{"detail": {...}}`` 包裹，前端取 ``detail.msg / detail.remaining
    / detail.retry_after``。
    """
    a = get_auth()
    a.maybe_reload()
    if not a.enabled:
        raise HTTPException(400, detail={"msg": "本实例未启用认证"})
    host = _client_ip(request)
    st = _login_state(host)
    if st["locked"]:
        raise HTTPException(429, detail={
            "msg": "尝试次数过多，请 %d 秒后再试" % st["retry_after"],
            "retry_after": st["retry_after"],
        })
    #: 用户名/口令错统一口径（防枚举账号），但用邮箱当账号名
    token = a.login(username.strip(), password)
    if not token:
        note = _login_note_fail(host)
        if note["locked"]:
            raise HTTPException(429, detail={
                "msg": "尝试次数过多，请 %d 秒后再试" % note["retry_after"],
                "retry_after": note["retry_after"],
            })
        raise HTTPException(401, detail={
            "msg": "邮箱或密码不正确", "remaining": note["remaining"],
        })
    _login_clear(host)
    resp = JSONResponse({"ok": True, "username": username.strip()})
    resp.set_cookie(
        AUTH.COOKIE_NAME, token, httponly=True, samesite="lax",
        secure=a.cookie_secure, max_age=a.ttl_s,
        path="/",
    )
    return resp


@app.post("/api/logout")
def logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(AUTH.COOKIE_NAME, path="/")
    return resp


@app.get("/api/me")
def me(request: Request) -> dict[str, Any]:
    u = _current_user(request)
    return {"auth_enabled": get_auth().enabled,
            "user": ({"username": u["username"], "role": u.get("role", "")} if u else None)}


# ==========================================================================
# 任务
# ==========================================================================


def _safe(name: str) -> str:
    base = Path(name or "upload").name
    cleaned = _SAFE_NAME.sub("_", base).strip("_") or "upload"
    return cleaned[:120]


#: **必须是同步 ``def``，不能是 ``async def``。**
#: 端点里要落盘（6MB 上传 + 队列写入），而本机磁盘 I/O 实测会卡到 ~12s —— 写在
#: 事件循环上就是**整个服务**（连 /api/health）一起冻住约 87s（Stage 9 现场转储）。
#: 同步 def 会被 FastAPI 丢进线程池，阻塞的只是那个线程。
#: 这条由 ``tests/test_web_blocking.py`` 钉着 —— 别改回 async。
@app.post("/api/jobs")
def create_job(
    request: Request,
    file: UploadFile = File(..., description="主单元源数据（xlsx / csv / json）"),
    comments: UploadFile | None = File(None, description="次级单元（评论）源数据，可选"),
    category: str = Form(""),
    frame: str = Form("consumer"),
    source: str = Form("auto"),
    profile: str = Form("auto"),
    keywords: str = Form(""),
    brands: str = Form(""),
    source_note: str = Form(""),
    title: str = Form(""),
    max_age_years: str = Form(""),
    mock: bool = Form(True),
) -> JSONResponse:
    """收上传文件 → 落存储 → 建 JobSpec → 入队。

    注意 ``JobSpec.input`` 是**外部路径**：内核会在 ``run_job`` 里把它复制进
    ``<job>/input/``（job 自包含，Stage 7 的验收条件）。所以这里只管把文件放到
    一个稳定的位置，不碰 job 内部布局。

    C2 新增：``owner`` 进队列行（此后就算 job 跑完，这条提交记录也还在，
    并且只对 owner / admin 可见）。
    """
    user = _require_user(request)
    if not file.filename:
        raise HTTPException(400, "缺少主数据文件")

    job_id = _unique_job_id(category or title or "analysis")
    store = get_storage()

    def _save(uf: UploadFile, tag: str) -> str:
        key = STORAGE.safe_key(f"{job_id}/{tag}__{_safe(uf.filename or '')}")
        data = uf.file.read()
        store.put_bytes(key, data)
        #: 内核只能读本地路径 —— S3 后端在这里把对象拉回缓存目录
        return str(store.ensure_local(key))

    input_path = _save(file, "main")
    comments_path = _save(comments, "comments") if (comments and comments.filename) else ""

    age: float | None = None
    if max_age_years.strip():
        try:
            age = float(max_age_years)
        except ValueError as exc:
            raise HTTPException(400, f"max_age_years 不是数字：{max_age_years!r}") from exc

    spec = P.JobSpec(
        category=category.strip(),
        input=input_path,
        comments=comments_path,
        source=source or "auto",
        keywords=keywords.strip(),
        brands=brands.strip(),
        source_note=source_note.strip(),
        frame=frame.strip(),
        profile=profile or "auto",
        max_age_years=age,
        mock=bool(mock),
        title=title.strip(),
    )
    owner = _owner_of(request) if get_auth().enabled else ""
    queue.add(job_id, spec.to_dict(redact=False), owner=owner)
    return JSONResponse({"job_id": job_id, "status": STATE_QUEUED,
                         "input": input_path, "owner": owner}, status_code=201)


def _unique_job_id(base: str) -> str:
    """同一秒提交两次会撞名 —— 加后缀而不是覆盖。C2：副本必须在队列里也查得到。"""
    job_id = new_job_id(base)
    n = 1
    while queue.get(job_id) or (RUNS_DIR / job_id).exists():
        n += 1
        job_id = f"{new_job_id(base)}-{n}"
    return job_id


def _gate_of(job_dir: Path, job: dict[str, Any]) -> dict[str, Any] | None:
    """从闸门产物读结论（``validate`` 退出码 1 表示「产物有问题」，不是「执行失败」）。"""
    p = job_dir / "report" / "quality_report.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    s = data.get("summary") or {}
    by = s.get("failed_by_severity") or {}
    return {
        "total": s.get("total"),
        "passed": s.get("passed"),
        "failed": s.get("failed"),
        "skipped": s.get("skipped"),
        "errors": int(by.get("error") or 0),
        "warnings": int(by.get("warning") or 0),
    }


def _ui_status(job: dict[str, Any] | None, entry: dict[str, Any] | None,
               gate: dict[str, Any] | None) -> str:
    """把「队列状态 × job.json 状态 × 闸门结论」折成一个界面用的词。

    **关键**：闸门报 error 级发现时仍是 ``done``（Stage 7：闸门是出口检查、不阻断），
    界面上要显示成「完成（有质量问题）」，而不是「失败」。

    C2 补充说明：``queue.db`` 里的行**跑完后留着**（提交账本，要记 owner），
    所以 ``entry.state == done`` 是正常的 —— 它不参与状态合成（下面没 matcher），
    状态此后一律由 ``job.json`` 说。
    """
    if entry:
        st = entry.get("state")
        if st in (STATE_QUEUED, STATE_RUNNING, STATE_INTERRUPTED):
            return st
        if st == STATE_FAILED:
            #: 退出码 3 是「等待人工回填」—— 不是失败
            if "人工回填" in (entry.get("error") or ""):
                return "waiting_manual"
            return STATE_FAILED
    if not job:
        return "unknown"
    status = job.get("status") or "?"
    if status == "waiting_manual":
        return "waiting_manual"
    if status == "failed":
        return STATE_FAILED
    if status in ("done", "skipped"):
        if gate and gate.get("errors"):
            return "done_with_errors"
        if gate and gate.get("warnings"):
            return "done_with_warnings"
        return "done"
    return status


@app.get("/api/jobs")
def list_all_jobs(request: Request) -> dict[str, Any]:
    """任务列表 = ``runs/`` 里的 job.json（权威状态） ∪ 队列里的提交记录。

    C2：**按 owner 过滤** —— 普通用户只看自己提交的；admin 看全部。
    """
    user = _require_user(request)
    a = get_auth()
    entries = {e["job_id"]: e for e in queue.list()}   # done 行也在这（账本）
    sums = {j["job_id"]: j for j in P.list_jobs(RUNS_DIR)}

    rows: list[dict[str, Any]] = []
    for jid in sorted(set(sums) | set(entries)):
        entry = entries.get(jid)
        #: 队列里记的 owner 就是提交者；job.json 里没有这个概念
        owner = (entry or {}).get("owner") or ""
        if not a.can_see(user, owner):
            continue
        job = sums.get(jid)
        gate = _gate_of(RUNS_DIR / jid, job) if job else None
        steps = (job or {}).get("done"), (job or {}).get("total")
        rows.append({
            "job_id": jid,
            "owner": owner,
            "category": (job or {}).get("category") or (entry or {}).get("spec", {}).get("category", ""),
            "frame": (job or {}).get("frame") or (entry or {}).get("spec", {}).get("frame", ""),
            "status": _ui_status(job, entry, gate),
            "done": steps[0] or 0,
            "total": steps[1] or len(P.STEPS),
            "gate": gate,
            "submitted_at": (entry or {}).get("submitted_at", ""),
            "updated_at": (job or {}).get("updated_at", ""),
            "error": (entry or {}).get("error", ""),
            "has_report": (RUNS_DIR / jid / "report" / "dashboard.html").exists(),
            "mtime": (job or {}).get("mtime", 0.0),
        })
    rows.sort(key=lambda r: r["mtime"] or 0, reverse=True)
    return {"jobs": rows, "runs_dir": str(RUNS_DIR)}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str, request: Request) -> dict[str, Any]:
    job_dir = RUNS_DIR / job_id
    entry = queue.get(job_id)
    #: **先看权限再看内容** —— 没权限时连「存不存在」都不该告诉对方
    if entry is not None and not _may_view(request, entry.get("owner") or ""):
        raise HTTPException(403, "这个任务不属于你")
    if entry is None and not _may_view(request, ""):
        #: 命令行直接跑出来的 job（队里没有记录，owner 为空）—— 普通用户不该看到
        raise HTTPException(403, "这个任务不属于你")

    job = P.load_job(job_dir)
    if job is None and entry is None:
        raise HTTPException(404, f"没有这个任务：{job_id}")

    gate = _gate_of(job_dir, job or {})
    steps: list[dict[str, Any]] = []
    if job:
        order = job.get("steps_order") or list(P.STEPS)
        for key in order:
            s = (job.get("steps") or {}).get(key) or {}
            steps.append({
                "key": key,
                "label": P.STEP_LABELS.get(key, key),
                "status": s.get("status", "pending"),
                "exit_code": s.get("exit_code", 0),
                "elapsed_s": s.get("elapsed_s", 0.0),
                "note": s.get("note", ""),
                "outputs": len(s.get("outputs") or {}),
            })

    #: C2：worker 大概率在**别的进程**，内存环形缓冲读不到 —— 改读落盘的日志
    logpath = LOG.log_path(STATE_DIR, job_id)
    recent = LOG.tail(logpath, 40) if logpath.exists() else []
    if not recent and WORKER_MODE == "inproc":
        recent = get_worker().log_lines(job_id, limit=40)

    return {
        "job_id": job_id,
        "owner": (entry or {}).get("owner", ""),
        "status": _ui_status(job, entry, gate),
        "job_status": (job or {}).get("status", ""),
        "spec": (job or {}).get("spec") or (entry or {}).get("spec", {}),
        "steps": steps,
        "gate": gate,
        "error": (entry or {}).get("error", ""),
        "elapsed_s": round(sum(s["elapsed_s"] or 0 for s in steps), 1),
        "recent_log": recent,
        "worker_result": get_worker().result_of(job_id) if WORKER_MODE == "inproc" else None,
        "reproduce": P.JobSpec.from_dict(
            (job or {}).get("spec") or (entry or {}).get("spec") or {}
        ).reproduce_command(f"runs/{job_id}"),
        "files": [k for k, (sub, fn) in REPORT_FILES.items()
                  if (job_dir / sub / fn).exists()],
    }


def _serve(request: Request, job_id: str, kind: str) -> FileResponse:
    pair = REPORT_FILES.get(kind)
    if pair is None:
        raise HTTPException(404, f"未知产物：{kind}（可用：{', '.join(REPORT_FILES)}）")
    entry = queue.get(job_id)
    if entry is not None and not _may_view(request, entry.get("owner") or ""):
        raise HTTPException(403, "这个任务不属于你")
    if entry is None and not _may_view(request, ""):
        raise HTTPException(403, "这个任务不属于你")
    sub, fname = pair
    p = RUNS_DIR / job_id / sub / fname
    if not p.exists():
        raise HTTPException(404, f"产物还没生成：{job_id}/{fname}")
    media = {".html": "text/html; charset=utf-8",
             ".json": "application/json; charset=utf-8"}.get(p.suffix, "text/markdown; charset=utf-8")
    #: 看板是自包含 HTML（唯一外部依赖是 echarts CDN），直接回吐即可
    return FileResponse(p, media_type=media)


@app.get("/api/jobs/{job_id}/report")
def job_report(job_id: str, request: Request) -> FileResponse:
    return _serve(request, job_id, "dashboard")


@app.get("/api/jobs/{job_id}/file/{kind}")
def job_file(job_id: str, kind: str, request: Request) -> FileResponse:
    return _serve(request, job_id, kind)


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "worker_mode": WORKER_MODE,
        "worker_alive": _worker_alive(),
        "worker_pid": (_worker_proc.pid if _worker_proc and _worker_proc.poll() is None else None),
        "auth_enabled": get_auth().enabled,
        "storage": get_storage().describe().get("scheme", "local"),
        "counts": queue.stats(),
    }
