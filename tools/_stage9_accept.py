#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/_stage9_accept.py · Stage 9（C1 骨架）端到端验收。

做四件事：

1. **起服务** —— 用 venv 解释器拉起 uvicorn，runs/uploads/state 全部指向
   ``out/verify/stage9/``（不污染平台目录）。
2. **真跑一次** —— 用**真实 Reddit 导出**（299 帖 + 25,146 评论，mock 后端）经
   ``POST /api/jobs`` 提交，轮询到终态。
3. **断言六条**（可机械判定，见 ``CHECKS``）：
   任务出现在列表 / 七步跑完 / 闸门结论可读 / 报告能打开 / 产物白名单生效 /
   路径穿越被挡。
4. **退出码语义反证** —— 闸门有警告时状态必须是 ``done_with_warnings`` 而**不是** ``failed``；
   这是 Web 层最容易做错的地方（``validate`` 退出码 1 ≠ 执行失败）。

只依赖标准库（``urllib`` + 手写 multipart），与项目「零依赖」的取向一致。

用法::

    python tools/_stage9_accept.py                     # 默认 8099 端口
    python tools/_stage9_accept.py --port 8123
    python tools/_stage9_accept.py --posts <csv> --comments <csv>
    python tools/_stage9_accept.py --timeout 900
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
REPO = PLATFORM.parent
OUT = PLATFORM / "out" / "verify" / "stage9"

#: 本机约定：内核读 xlsx 要 openpyxl，只有这个 venv 有
VENV_PY = Path(r"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe")


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
        #: URLError（连接被拒）与 TimeoutError（服务还在启动、没来得及响应）都是 OSError
        #: 的子类，但**彼此不是子类** —— 只捕 URLError 会让启动期的超时直接穿出去，
        #: 把 wait_health 的轮询打断在第一次尝试上（实测踩过）。
        return 0, str(e).encode(), {}


def multipart(fields: dict[str, str],
              files: dict[str, tuple[str, bytes]]) -> tuple[str, bytes]:
    b = "----vocstage9" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for k, (name, blob) in files.items():
        out += (f'--{b}\r\nContent-Disposition: form-data; name="{k}"; '
                f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
        out += blob + b"\r\n"
    out += f"--{b}--\r\n".encode()
    return b, bytes(out)


# ==========================================================================
# 服务
# ==========================================================================


def start_server(port: int) -> subprocess.Popen:
    """起 uvicorn。

    **输出必须落到文件，不能用 ``stdout=PIPE``。** 内核各步骤会往 stdout 打大量回显，
    而管道缓冲区只有 ~64KB；没人读它 → 写满即阻塞 → 整个 job 卡死在某一半，
    现象是「中间件文件全写完了但步骤不返回」（实测踩过：annotate 写完 7,662 个文件后
    挂死 9 分钟）。落文件既没有缓冲上限，又能事后排查。
    """
    py = VENV_PY if VENV_PY.exists() else Path(sys.executable)
    env = dict(os.environ)
    env.update({
        "VOC_WEB_RUNS_DIR": str(OUT / "runs"),
        "VOC_WEB_UPLOAD_DIR": str(OUT / "uploads"),
        "VOC_WEB_STATE_DIR": str(OUT / "web_state"),
        "PYTHONIOENCODING": "utf-8",
    })
    for d in ("runs", "uploads", "web_state"):
        (OUT / d).mkdir(parents=True, exist_ok=True)
    #: 瞬时文件锁：打开也可能失败（不是进程占着 —— ``ps`` 里一个 python 都没有）。
    #: 退避重试，实在打不开就换名字（日志换个名不影响任何断言）。
    for _i in range(10):
        _p = OUT / ("server.log" if _i == 0 else f"server({_i + 1}).log")
        try:
            log = _p.open("wb")
            break
        except OSError:
            time.sleep(0.2 * (_i + 1))
    else:
        raise SystemExit(f"server.log 打不开（工作区被瞬时锁住）：{OUT / 'server.log'}")
    proc = subprocess.Popen(
        [str(py), "-m", "uvicorn", "web.app:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(PLATFORM), env=env,
        stdout=log, stderr=subprocess.STDOUT,
    )
    proc._voc_log = log  # type: ignore[attr-defined]  #: 跟着进程一起关
    return proc


def wait_health(base: str, *, timeout: float = 30.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        code, body, _ = _req(f"{base}/api/health", timeout=3)
        if code == 200:
            return json.loads(body)
        time.sleep(0.5)
    raise SystemExit("服务没起来（30s 内 /api/health 不可达）")


def wait_worker_alive(base: str, *, timeout: float = 40.0) -> bool:
    """等 worker 真的活过来。

    C2 把 worker 从「Web 进程内的线程」换成了**独立子进程**，而且它是**后台线程**
    拉起的（启动钩子里开日志文件会被本机瞬时锁扣住，见 ``web.app.spawn_worker``）——
    所以「Web 能响应」和「worker 已就绪」之间差着几秒，得等，不能一上来就断言。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        code, body, _ = _req(f"{base}/api/health", timeout=3)
        if code == 200:
            payload = json.loads(body)
            if payload.get("worker_alive"):
                return True
        time.sleep(0.5)
    return False


# ==========================================================================
# 断言
# ==========================================================================


class Checks:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def ok(self, name: str, cond: bool, detail: str = "") -> bool:
        self.rows.append((name, bool(cond), detail))
        print(f"   {'PASS' if cond else 'FAIL'}  {name}" + (f"  —— {detail}" if detail else ""))
        return bool(cond)

    def report(self) -> bool:
        n = sum(1 for _, ok, _ in self.rows if ok)
        print("\n" + "=" * 78)
        print(f"Stage 9 验收：{n}/{len(self.rows)} 条断言通过")
        print("=" * 78)
        for name, ok, detail in self.rows:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  —— {detail}" if detail else ""))
        return n == len(self.rows)


def write_text_retry(path: Path, text: str, *, attempts: int = 10) -> str:
    """写文本文件，失败就换个名字再试 —— 本机会对刚写过的同名文件瞬时上锁。

    :returns: 实际写到的路径（可能带 ``(2)`` 后缀 —— 内容一样，只是换了个名）。
    """
    for i in range(attempts):
        target = path if i == 0 else path.with_name(
            f"{path.stem}({i + 1}){path.suffix}")
        try:
            target.write_text(text, encoding="utf-8")
            if target.read_text(encoding="utf-8") == text:
                return str(target)
        except OSError:
            time.sleep(0.2 * (i + 1))
    print(f"  ⚠ 验收记录写不进去：{path}（不影响上面的断言结论）")
    return str(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage 9 C1 骨架端到端验收")
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--timeout", type=float, default=900.0, help="等 job 跑完的上限（秒）")
    ap.add_argument("--posts", default=str(REPO / "data/reddit_analysis/reddit_posts.csv"))
    ap.add_argument("--comments", default=str(REPO / "data/reddit_analysis/reddit_comments.csv"))
    ap.add_argument("--category", default="Nursery Glider Recliner")
    ap.add_argument("--keep-server", action="store_true", help="跑完不杀服务（手工看界面）")
    args = ap.parse_args(argv)

    base = f"http://127.0.0.1:{args.port}"
    posts, comments = Path(args.posts), Path(args.comments)
    for p in (posts, comments):
        if not p.exists():
            raise SystemExit(f"输入不存在：{p}")

    print("=" * 78)
    print("Stage 9 · C1 骨架端到端验收（真实数据 + mock 后端）")
    print("=" * 78)
    print(f"输入：{posts.name}（{posts.stat().st_size:,} 字节）"
          f" + {comments.name}（{comments.stat().st_size:,} 字节）")

    proc = start_server(args.port)
    ck = Checks()
    try:
        health = wait_health(base)
        #: worker 是后台线程另起的（见 wait_worker_alive 的说明）—— 等它就绪再断言
        if not health.get("worker_alive"):
            health = wait_health(base) if wait_worker_alive(base) else health
        print(f"服务已起：worker_alive={health.get('worker_alive')} "
              f"queued={health.get('queued')} python={health.get('python', '')}")
        ck.ok("服务起来且 worker 存活", health.get("worker_alive") is True,
              f"queued={health.get('queued')}")

        # ---- 元信息 ----
        code, body, _ = _req(f"{base}/api/meta")
        meta = json.loads(body) if code == 200 else {}
        ck.ok("GET /api/meta 返回框架与源枚举",
               code == 200 and bool(meta.get("frames")) and bool(meta.get("sources")),
               f"frames={meta.get('frames')} sources={len(meta.get('sources') or [])} 个")

        # ---- 提交 ----
        fields = {
            "category": args.category,
            "frame": "consumer",
            "source": "auto",
            "profile": "reddit",
            "keywords": "nursery glider,glider,recliner",
            "brands": "Babyletto,Davinci,Delta Children",
            "max_age_years": "2",
            "mock": "true",
        }
        files = {"file": (posts.name, posts.read_bytes()),
                 "comments": (comments.name, comments.read_bytes())}
        boundary, blob = multipart(fields, files)
        t_submit = time.time()
        code, body, _ = _req(f"{base}/api/jobs", data=blob,
                             headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                             timeout=180)
        if code != 201:
            print(f"提交失败：{code} {body[:400]!r}")
            ck.ok("POST /api/jobs 接受上传", False, f"HTTP {code}")
            ck.report()
            return 1
        job_id = json.loads(body)["job_id"]
        ck.ok("POST /api/jobs 接受上传", True, f"job_id={job_id}")

        # ---- 轮询 ----
        print(f"\n已提交 {job_id}，轮询中（上限 {args.timeout:.0f}s）…")
        terminal = {"done", "done_with_warnings", "done_with_errors",
                    "failed", "waiting_manual", "interrupted"}
        last = ""
        row: dict = {}
        while time.time() - t_submit < args.timeout:
            code, body, _ = _req(f"{base}/api/jobs/{job_id}")
            if code == 200:
                row = json.loads(body)
                st = f"{row.get('status')} {sum(1 for s in row.get('steps') or [] if s['status'] in ('done','skipped'))}/7"
                if st != last:
                    print(f"   [{time.time() - t_submit:6.1f}s] {st}")
                    last = st
                if row.get("status") in terminal:
                    break
            time.sleep(3)
        elapsed = time.time() - t_submit
        print(f"终态：{row.get('status')}（{elapsed:.1f}s）")

        # ---- 断言 ----
        print("\n── 断言 ──")
        ck.ok("任务跑到终态（不是超时）", row.get("status") in terminal,
              f"status={row.get('status')} 用时 {elapsed:.1f}s")
        steps = row.get("steps") or []
        done_steps = [s for s in steps if s["status"] in ("done", "skipped")]
        ck.ok("七步全部有结果（done/skipped）", len(done_steps) == 7,
              f"{len(done_steps)}/7：" + ",".join(f"{s['key']}={s['status']}" for s in steps))
        ck.ok("闸门结论可读", bool(row.get("gate")),
              json.dumps(row.get("gate"), ensure_ascii=False) if row.get("gate") else "无")

        #: —— 关键语义：闸门有问题 ≠ 任务失败 ——
        st_now = row.get("status")
        ck.ok("闸门警告不被显示成「失败」",
               st_now in ("done", "done_with_warnings", "done_with_errors"),
               f"status={st_now}（若为 failed 则说明 Web 层把 validate 的 exit 1 当成了执行失败）")

        code, body, _ = _req(f"{base}/api/jobs/{job_id}/report")
        is_dash = b"echarts" in body or b"<!DOCTYPE html" in body[:200]
        ck.ok("GET /report 回吐看板", code == 200 and is_dash and len(body) > 50_000,
              f"HTTP {code}，{len(body):,} 字节")

        code, body, _ = _req(f"{base}/api/jobs/{job_id}/file/quality")
        q = json.loads(body) if code == 200 else {}
        ck.ok("GET /file/quality 回吐闸门 JSON",
               code == 200 and "summary" in q,
               f"HTTP {code}，summary={json.dumps(q.get('summary'), ensure_ascii=False)[:90] if q else '—'}")

        code, _, _ = _req(f"{base}/api/jobs/{job_id}/file/../../../../etc/passwd")
        code2, _, _ = _req(f"{base}/api/jobs/{job_id}/file/whatever")
        ck.ok("未知/穿越类产物名被白名单挡住", code == 404 and code2 == 404,
              f"traversal→{code}，unknown→{code2}")

        code, body, _ = _req(f"{base}/api/jobs")
        lst = json.loads(body).get("jobs") or []
        ck.ok("任务出现在列表且带闸门摘要",
               any(j["job_id"] == job_id and j.get("gate") for j in lst),
               f"列表 {len(lst)} 条")

        code, _, _ = _req(f"{base}/")
        ck.ok("首页可打开", code == 200, f"HTTP {code}")

        # ---- 复现命令（内核自带，用于交叉验证） ----
        print("\n复现命令：")
        print("  " + (row.get("reproduce") or "—"))

        # ---- 落验收记录 ----
        OUT.mkdir(parents=True, exist_ok=True)
        rec = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "job_id": job_id,
            "elapsed_s": round(elapsed, 1),
            "status": row.get("status"),
            "gate": row.get("gate"),
            "steps": [{"key": s["key"], "status": s["status"], "elapsed_s": s["elapsed_s"]}
                      for s in steps],
            "checks": [{"name": n, "pass": ok, "detail": d} for n, ok, d in ck.rows],
            "reproduce": row.get("reproduce"),
        }
        #: 带退避地写 —— 本机杀软会**瞬时扣住**刚写过的同名文件，
        #: 直接 write_text 会抛 PermissionError [WinError 13]。这一行在所有断言
        #: 跑完之后执行，崩在这里会让探针非 0 退出，看着像验收失败（其实不是）。
        _saved = write_text_retry(
            OUT / "acceptance.json",
            json.dumps(rec, ensure_ascii=False, indent=2))
        print(f"\n验收记录：{_saved}")
        return 0 if ck.report() else 1
    finally:
        if args.keep_server:
            print(f"\n--keep-server：服务留在 {base}（PID {proc.pid}）")
        else:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
