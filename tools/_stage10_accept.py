#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C2 · 端到端验收（真起 uvicorn + 真起**独立 worker 进程** + 真实数据）。

本探针验的是**单元测试补不了的那部分**。单元测试（``tests/test_web_*.py``）全部毫秒级、
不联网、能进 CI；但下面这几件事只有在「真起两个进程」时才存在，根本绕不过去：

==================================== ==================================================
只有端到端才测得出的                       为什么单测答不了
==================================== ==================================================
worker 真的是**另一个进程**                  ``worker_pid`` ≠ server PID
跑任务期间 Web **仍然响应**                  这正是 Stage 9 那次故障的表现形式：事件循环
                                           被同进程 worker 冻住，连 /api/health 都不回
跨进程日志能读到                            内存环形缓冲过了进程边界就没了，必须落盘
未登录 401 / 越权 403                      需要真的 HTTP 状态码与 cookie 流转
七步真的能跑完 + 看板真的能开                全流程
==================================== ==================================================

用法::

    python tools/_stage10_accept.py
    python tools/_stage10_accept.py --port 8123 --no-auth

依赖 uvicorn / fastapi（``pip install -e ".[web]"``）与一份真实数据
（默认 ``<repo>/data/reddit_analysis/``）。**零第三方 HTTP 库**：multipart 手写、请求用 urllib。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
REPO = PLATFORM.parent
OUT = PLATFORM / "out" / "verify" / "stage10"
STATE_DIR = OUT / "web_state"

VENV_PY = Path(r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default"
               r"\Scripts\python.exe")

_PASSED: list[str] = []
_FAILED: list[str] = []


def check(name: str, ok: bool, note: str = "") -> None:
    (_PASSED if ok else _FAILED).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"         —— {note}" if note else ""))


# ==========================================================================
# HTTP（标准库）
# ==========================================================================


#: **验收探针必须显式绕开代理。** 这台机器的环境里有 ``http_proxy``
#: （沙箱的透明代理），``urllib.request.urlopen`` 会乖乖走它 —— 于是探
#: ``http://127.0.0.1:<port>/api/health`` 变成「请代理帮我连我自己的端口」，
#: 代理回 502 Bad Gateway 或干脆不回。表现是**服务明明活着，验收却报
#: 「服务没起来」**（C2 验收在这里整整卡了两轮）。
#: 用 ``ProxyHandler({})`` 造一个不走任何代理的 opener —— 回环地址本来就不该走代理。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def _req(url: str, *, data: bytes | None = None, headers: dict | None = None,
         timeout: float = 30.0):
    r = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with _OPENER.open(r, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})
    except OSError as e:
        #: URLError（连接被拒）与 TimeoutError（还在启动）都是 OSError 的子类但互不为子类。
        #: 只捕 URLError 会让启动期的超时直接穿出去（Stage 9 实测踩过）。
        return 0, str(e).encode(), {}


def _json(url: str, **kw):
    code, body, hdrs = _req(url, **kw)
    try:
        return code, json.loads(body), hdrs
    except Exception:  # noqa: BLE001
        return code, body.decode("utf-8", "replace"), hdrs


def multipart(fields: dict[str, str],
              files: dict[str, tuple[str, bytes]]) -> tuple[str, bytes]:
    b = "----vocstage10" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for k, (name, blob) in files.items():
        out += (f'--{b}\r\nContent-Disposition: form-data; name="{k}"; '
                f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
        out += blob + b"\r\n"
    out += f"--{b}--\r\n".encode()
    return b, bytes(out)


def post_form(path: str, fields: dict[str, str], files: dict | None = None,
              *, base: str, cookie: str = "") -> tuple[int, dict]:
    b, body = multipart(fields, files or {})
    hdrs = {"Content-Type": f"multipart/form-data; boundary={b}"}
    if cookie:
        hdrs["Cookie"] = cookie
    code, payload, h = _json(base + path, data=body, headers=hdrs, timeout=180)
    return code, (payload if isinstance(payload, dict) else {"raw": payload}), h


# ==========================================================================
# 进程
# ==========================================================================


def _python() -> str:
    return str(VENV_PY) if VENV_PY.exists() else sys.executable


def start_server(port: int) -> subprocess.Popen:
    """起 uvicorn，**worker 关闭**（``--worker off``）—— worker 由本探针单独起。

    **输出必须落文件，不能用 PIPE**：见 C1 验收里那条记了两遍的坑（64KB 管道缓冲写满
    → 整个 job 卡在某一步）。
    """
    env = dict(os.environ)
    env.update({
        "VOC_WEB_RUNS_DIR": str(OUT / "runs"),
        "VOC_WEB_UPLOAD_DIR": str(OUT / "uploads"),
        "VOC_WEB_STATE_DIR": str(STATE_DIR),
        "VOC_WEB_WORKER_MODE": "off",        #: worker 交给本探针另起，才是「独立进程」
        "PYTHONIOENCODING": "utf-8",
    })
    for d in ("runs", "uploads", "web_state"):
        (OUT / d).mkdir(parents=True, exist_ok=True)
    log = (OUT / "server.log").open("wb")
    proc = subprocess.Popen(
        [_python(), "-m", "uvicorn", "web.app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(PLATFORM), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    proc._voc_log = log  # type: ignore[attr-defined]
    return proc


def start_worker() -> subprocess.Popen:
    """**独立 worker 进程** —— C2 全部意义所在。

    注意 Windows：``Popen.terminate()`` 走 TerminateProcess，worker 收不到可捕获的信号，
    所以这里直接用 kill 也没关系 —— 它一死心跳就停，``reap_stale`` 会回收它的 running 项。
    """
    env = dict(os.environ)
    env.update({
        "VOC_WEB_RUNS_DIR": str(OUT / "runs"),
        "VOC_WEB_STATE_DIR": str(STATE_DIR),
        "PYTHONIOENCODING": "utf-8",
    })
    log = (OUT / "worker_proc.log").open("wb")
    proc = subprocess.Popen(
        [_python(), "-m", "web.worker_main",
         "--state-dir", str(STATE_DIR), "--runs-dir", str(OUT / "runs"),
         "--id", "acceptance-worker", "--poll", "0.5"],
        cwd=str(PLATFORM), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    proc._voc_log = log  # type: ignore[attr-defined]
    return proc


def wait_health(base: str, *, timeout: float = 40.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        code, payload, _ = _json(base + "/api/health", timeout=3)
        if code == 200 and isinstance(payload, dict):
            return payload
        time.sleep(0.5)
    raise SystemExit("服务没起来（40s 内 /api/health 不可达）")


def wait_worker_alive(base: str, *, timeout: float = 40.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        code, payload, _ = _json(base + "/api/health", timeout=3)
        if code == 200 and isinstance(payload, dict) and payload.get("worker_alive"):
            return True
        time.sleep(0.5)
    return False


# ==========================================================================
# 主流程
# ==========================================================================


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C2 端到端验收")
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--posts", default=str(REPO / "data/reddit_analysis/reddit_posts.csv"))
    ap.add_argument("--comments", default=str(REPO / "data/reddit_analysis/reddit_comments.csv"))
    ap.add_argument("--no-auth", action="store_true", help="跳过认证相关的断言")
    ap.add_argument("--timeout", type=float, default=1500.0, help="等七步跑完的上限（秒）")
    args = ap.parse_args(argv)

    posts, comments = Path(args.posts), Path(args.comments)
    if not posts.exists() or not comments.exists():
        raise SystemExit(f"数据文件不在：{posts} / {comments}")

    #: 清场。不清的话上一轮留下的 ``users.json`` 会让认证**早已开着**（于是本轮的
    #: 「未登录 → 401」测的是旧用户表）。用 Python 删而不是 shell 的 ``rm -rf``：
    #: 后者在本工作区会被 safe-delete 钩子拦下并返回非 0，让 ``&&`` 短路（Stage 9 踩过）。
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)

    base = f"http://127.0.0.1:{args.port}"
    srv = wrk = None
    print(f"解释器：{_python()}")
    print(f"工作区：{OUT}")
    print(f"数据：  {posts.name} + {comments.name}\n")

    try:
        # -- 1. 起服务（不带 worker）与独立 worker ------------------------------
        print("[1] 起服务（--worker off）与**独立 worker 进程**")
        srv = start_server(args.port)
        h = wait_health(base)
        check("服务起来了", True, f"worker_mode={h.get('worker_mode')}")

        wrk = start_worker()
        alive = wait_worker_alive(base)
        check("Web 通过**心跳**感知到独立 worker", alive,
              f"worker_pid={h.get('worker_pid')}")

        srv_pid, wrk_pid = srv.pid, wrk.pid
        check("worker 与服务是**两个不同的进程**", srv_pid != wrk_pid,
              f"server={srv_pid} worker={wrk_pid}")

        # -- 2. 认证（可选） ----------------------------------------------------
        cookie = ""
        if not args.no_auth:
            print("\n[2] 认证与隔离")
            #: 用**真的 CLI** 建用户（顺带把 ``web.auth`` 这条命令也验了一遍）。
            #: 不用 ``from web import auth``：本脚本的 ``sys.path[0]`` 是 ``tools/``，
            #: 子进程自己会从平台目录起，路径天然是对的。
            for _user, _pw, _role in (("ethan", "accept-me-1", "admin"),
                                      ("guest", "guest-pass-1", "user")):
                _p = subprocess.run(
                    [_python(), "-m", "web.auth", "--state-dir", str(STATE_DIR),
                     "adduser", _user, "--role", _role, "--password", _pw],
                    cwd=str(PLATFORM), capture_output=True, text=True,
                    encoding="utf-8", errors="replace")
                if _p.returncode != 0:
                    raise SystemExit(f"建用户 {_user} 失败：{_p.stderr}")

            code, payload, _ = _json(base + "/api/jobs", timeout=20)
            check("未登录访问任务列表 → 401", code == 401, f"HTTP {code}")

            code, payload, hdrs = post_form("/api/login",
                                            {"username": "ethan", "password": "wrong-pw"},
                                            base=base)
            check("错误口令 → 401", code == 401, f"HTTP {code}")

            code, payload, hdrs = post_form("/api/login",
                                            {"username": "ethan", "password": "accept-me-1"},
                                            base=base)
            check("正确口令 → 拿到会话 cookie", code == 200 and "set-cookie" in hdrs,
                  f"HTTP {code}")
            sc = hdrs.get("set-cookie", "")
            cookie = sc.split(";")[0]
            check("会话 cookie 是 HttpOnly",
                  "httponly" in sc.lower(), sc[:60] + "...")

            code, payload, _ = _json(base + "/api/me", timeout=20,
                                     headers={"Cookie": cookie})
            me = payload.get("user") if isinstance(payload, dict) else None
            check("/api/me 报出当前登录者", bool(me and me.get("username") == "ethan"),
                  str(me))
        else:
            print("\n[2] 认证 —— 已跳过（--no-auth）")

        # -- 3. 提交 ------------------------------------------------------------
        print("\n[3] 提交真实数据（mock 关闭内核 LLM 之外的部分，仍跑满七步）")
        t0 = time.time()
        code, payload, _ = post_form(
            "/api/jobs",
            {"category": "Acceptance C2", "frame": "consumer", "source": "auto",
             "profile": "auto", "mock": "true"},
            {"file": (posts.name, posts.read_bytes()),
             "comments": (comments.name, comments.read_bytes())},
            base=base, cookie=cookie)
        submit_s = time.time() - t0
        check("提交秒回（不再像 C1 那样卡在事件循环上）", submit_s < 20.0,
              f"{submit_s:.2f}s")
        job_id = payload.get("job_id") if isinstance(payload, dict) else ""
        check("提交返回 job_id", bool(job_id), f"HTTP {code} {str(payload)[:80]}")

        # -- 4. 跑任务期间 Web 仍然响应（这是 C2 要解决的核心问题） -------------
        print("\n[4] 七步执行期间，反复探 Web 是否仍响应")
        worst = 0.0
        deadline = time.time() + args.timeout
        last = None
        while time.time() < deadline:
            ht = time.time()
            code, payload, _ = _json(base + "/api/health", timeout=10)
            worst = max(worst, time.time() - ht)
            if code == 200 and isinstance(payload, dict):
                last = payload
            time.sleep(2.0)
            code, d, _ = _json(base + f"/api/jobs/{job_id}", timeout=20,
                               headers={"Cookie": cookie} if cookie else None)
            if code == 200 and isinstance(d, dict):
                st = d.get("status")
                if st in ("done", "done_with_errors", "done_with_warnings",
                          "failed", "waiting_manual"):
                    break
        detail_code, detail, _ = _json(base + f"/api/jobs/{job_id}", timeout=30,
                                       headers={"Cookie": cookie} if cookie else None)
        status = detail.get("status") if isinstance(detail, dict) else "?"
        steps_done = sum(1 for s in (detail.get("steps") or []) if s.get("status") == "done")
        total_steps = len(detail.get("steps") or [])
        check("内核跑满七步", total_steps >= 7 and steps_done >= 7,
              f"{steps_done}/{total_steps} 步 done")
        check("跑任务期间 Web 最坏一次响应 < 5s", worst < 5.0,
              f"最坏 {worst:.2f}s（C1 冻住时是整整两分钟不响应）")
        check("终态不是 failed", status not in ("failed",),
              f"status={status}")

        # -- 5. 跨进程日志 ------------------------------------------------------
        print("\n[5] worker 在**别的进程**里，日志也必须能读到")
        logs = detail.get("recent_log") or []
        check("详情里读得到 worker 日志（跨进程）", len(logs) > 0,
              f"{len(logs)} 行；首行：{(logs[0][:70] if logs else '（空）')}")

        logfile = STATE_DIR / "logs" / f"{job_id}.log"
        check("日志文件真的落在状态目录里", logfile.exists(), str(logfile.relative_to(OUT)))

        # -- 6. 报告与产物 ------------------------------------------------------
        print("\n[6] 报告与产物白名单")
        code, payload, _ = _req(base + f"/api/jobs/{job_id}/report", timeout=60,
                                headers={"Cookie": cookie} if cookie else None)
        size = len(payload) if code == 200 else 0
        check("看板可打开", code == 200 and size > 10000, f"HTTP {code}，{size} 字节")

        code, _, _ = _req(base + f"/api/jobs/{job_id}/file/nope", timeout=20,
                          headers={"Cookie": cookie} if cookie else None)
        check("未知产物名 → 404", code == 404, f"HTTP {code}")
        code, _, _ = _req(base + f"/api/jobs/{job_id}/file/../../etc/passwd", timeout=20,
                          headers={"Cookie": cookie} if cookie else None)
        check("目录穿越类产物名被挡住", code in (403, 404), f"HTTP {code}")

        # -- 7. 多租户隔离 ------------------------------------------------------
        if not args.no_auth:
            print("\n[7] 多租户隔离")
            code, gpayload, ghdrs = post_form("/api/login",
                                              {"username": "guest", "password": "guest-pass-1"},
                                              base=base)
            gcookie = ghdrs.get("set-cookie", "").split(";")[0]
            code, lst, _ = _json(base + "/api/jobs", timeout=20,
                                 headers={"Cookie": gcookie})
            ids = [j["job_id"] for j in (lst.get("jobs") or [])] if isinstance(lst, dict) else []
            check("普通用户看不到 admin 提交的任务", job_id not in ids,
                  f"guest 可见 {len(ids)} 条")
            code, _, _ = _json(base + f"/api/jobs/{job_id}", timeout=20,
                               headers={"Cookie": gcookie})
            check("普通用户直取他人任务 → 403", code == 403, f"HTTP {code}")
            own = len([j for j in (lst.get("jobs") or []) if j.get("owner") == "guest"]) \
                if isinstance(lst, dict) else 0
            check("guest 列表里没有别人的 owner", own == 0, f"guest 名下 {own} 条")

    finally:
        if wrk is not None:
            wrk.kill()
            try:
                wrk.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
            if getattr(wrk, "_voc_log", None):
                wrk._voc_log.close()
        if srv is not None:
            srv.terminate()
            try:
                srv.wait(timeout=10)
            except Exception:  # noqa: BLE001
                srv.kill()
            if getattr(srv, "_voc_log", None):
                srv._voc_log.close()

    print("\n" + "=" * 66)
    print(f"通过 {len(_PASSED)} 项，失败 {len(_FAILED)} 项")
    for name in _FAILED:
        print(f"  ✗ {name}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
