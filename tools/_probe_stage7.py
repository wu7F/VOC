#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 7 验收探针 —— 替人回答路线图的验收条件。

路线图对 Stage 7 的验收只有一句：

    ``runs/<job_id>/job.json`` 能完整复现一次分析（给定参数与数据）。

这句可以拆成四条**可机械判定**的断言，本探针逐条验：

1. **契约完整** —— job.json 里复现所需的字段一个不少
   （spec / source_files / reproduce / steps_order / steps / python / schema_version）
2. **产物自洽** —— steps[*].outputs 里记的 sha1 与磁盘上的一致（说明记的是真指纹）
3. **输入留痕** —— steps[*].inputs 记的 sha1 与磁盘一致（说明上游产物确实被吃进去了）
4. **命令可用** —— ``reproduce`` 那一行能被 CLI 解析器原样接受，且参数与 spec 对得上
5. **路径相对化** —— 指纹里的路径都折叠成了 ``<job>/…`` / ``<platform>/…``
   （这是「换台机器/换个 checkout 目录也能复现」的前提）

用法::

    python tools/_probe_stage7.py runs/2026-10-08-nursery
    python tools/_probe_stage7.py out/verify/stage7/reddit --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voc_core import pipeline as P  # noqa: E402

def _utf8_stdout() -> None:
    """Windows 控制台默认 GBK，✓/✗ 与中文会炸 —— 裸解释器 ``-S -E`` 下也得能用。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - 环境不支持就算了
                pass


def _mark(ok: bool) -> str:
    """终端实在吃不下 UTF-8 时退化成 ASCII，不能因为「打个勾」把探针搞崩。"""
    try:
        ("✓" if ok else "✗").encode(sys.stdout.encoding or "utf-8")
    except (UnicodeEncodeError, LookupError):
        return "[OK]" if ok else "[!!]"
    return "✓" if ok else "✗"


_utf8_stdout()
OK_ = _mark(True)
NO_ = _mark(False)

STATUSES = {"done", "skipped", "failed", "waiting_manual", "blocked", "pending"}


def sha1_file(p: Path) -> str:
    try:
        with p.open("rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return ""


def check_contract(state: dict) -> dict:
    need = ("schema_version", "job_id", "created_at", "updated_at", "status",
            "spec", "source_files", "reproduce", "python", "steps_order", "steps")
    missing = [k for k in need if k not in state]
    problems = [f"缺字段 {k}" for k in missing]
    if state.get("schema_version") != P.JOB_SCHEMA_VERSION:
        problems.append(f"schema_version={state.get('schema_version')}")
    if list(state.get("steps_order") or []) != list(P.STEPS):
        problems.append(f"steps_order 与 STEPS 不一致：{state.get('steps_order')}")
    if not state.get("reproduce", "").startswith("python voc.py run"):
        problems.append("reproduce 不是 `voc.py run` 命令")
    return {"name": "契约完整：job.json 带齐复现所需字段",
            "ok": not problems, "detail": f"字段 {len(need)} 项，缺失 {len(missing)}", "problems": problems}


def _digest(p: Path, rel: str) -> str:
    """按条目类型取指纹：目录条目（``rel`` 以 ``/`` 结尾）用聚合哈希，文件用 sha1。"""
    return P._dir_sha(p) if rel.endswith("/") else sha1_file(p)


def check_outputs(state: dict, job_dir: Path) -> dict:
    checked = miss = bad = 0
    problems: list[str] = []
    for step, rec in (state.get("steps") or {}).items():
        for rel, sha in (rec.get("outputs") or {}).items():
            p = job_dir / rel
            if not p.exists():
                miss += 1
                problems.append(f"{step}: 产物消失 {rel}")
                continue
            checked += 1
            if _digest(p, rel) != sha:
                bad += 1
                problems.append(f"{step}: 产物被改 {rel}")
    return {"name": "产物自洽：记录的 sha1 与磁盘一致",
            "ok": not problems, "detail": f"核对 {checked} 个产物，缺失 {miss}，内容不符 {bad}",
            "problems": problems[:8]}


def check_inputs(state: dict, job_dir: Path) -> dict:
    checked = bad = 0
    problems: list[str] = []
    for step, rec in (state.get("steps") or {}).items():
        for rel, sha in (rec.get("inputs") or {}).items():
            p = job_dir / rel
            if not p.exists():
                problems.append(f"{step}: 输入丢失 {rel}")
                continue
            checked += 1
            if _digest(p, rel) != sha:
                bad += 1
                problems.append(f"{step}: 输入已变 {rel}")
    return {"name": "输入留痕：每步吃进的产物都被记了指纹",
            "ok": not problems, "detail": f"核对 {checked} 条输入指纹，不符 {bad}",
            "problems": problems[:8]}


def check_reproduce(state: dict) -> dict:
    """把 reproduce 命令丢给真正的解析器 —— 它接受，就说明参数合法且完整。"""
    cmd = state.get("reproduce", "")
    problems: list[str] = []
    if not cmd:
        return {"name": "命令可用：reproduce 能被 CLI 解析", "ok": False,
                "detail": "reproduce 为空", "problems": ["空"]}
    import shlex
    try:
        tokens = shlex.split(cmd)
    except ValueError as exc:
        return {"name": "命令可用：reproduce 能被 CLI 解析", "ok": False,
                "detail": f"引号不配对：{exc}", "problems": [cmd]}
    argv = tokens[2:] if tokens[:2] == ["python", "voc.py"] else tokens
    try:
        cli = P._voc_cli()
        parser = cli.build_parser()
        ns = parser.parse_args(argv)
    except SystemExit as exc:
        return {"name": "命令可用：reproduce 能被 CLI 解析", "ok": False,
                "detail": f"解析失败（SystemExit {exc.code}）", "problems": [cmd]}
    except Exception as exc:  # noqa: BLE001
        return {"name": "命令可用：reproduce 能被 CLI 解析", "ok": False,
                "detail": f"{type(exc).__name__}: {exc}", "problems": [cmd]}

    if ns.cmd != "run":
        problems.append(f"子命令是 {ns.cmd}，应为 run")
    spec = state.get("spec") or {}
    for attr, key in (("category", "category"), ("frame", "frame"), ("mock", "mock")):
        if getattr(ns, attr, None) != spec.get(key):
            problems.append(f"{attr}: 命令={getattr(ns, attr, None)!r} spec={spec.get(key)!r}")
    return {"name": "命令可用：reproduce 能被 CLI 解析", "ok": not problems,
            "detail": "解析通过，关键参数与 spec 一致", "problems": problems}


def check_relativized(state: dict, job_dir: Path) -> dict:
    """指纹里的路径必须已折叠成占位符前缀，否则换机器就对不上。"""
    bad: list[str] = []
    total = 0
    for step, rec in (state.get("steps") or {}).items():
        for k, v in (rec.get("params") or {}).items():
            if not isinstance(v, str) or not v:
                continue
            looks_path = ("/" in v or "\\" in v) and (
                v.startswith(("<job>", "<platform>", "<ext>")) or "." in Path(v).name)
            if not looks_path:
                continue
            total += 1
            if not v.startswith(("<job>", "<platform>", "<ext>")):
                if Path(v).is_absolute() or v.startswith(job_dir.name):
                    bad.append(f"{step}.{k}={v}")
    return {"name": "路径相对化：指纹里的路径已折叠成 <job>/<platform>/<ext>",
            "ok": not bad, "detail": f"检查 {total} 个路径参数，未折叠 {len(bad)}",
            "problems": bad[:8]}


def check_statuses(state: dict) -> dict:
    steps = state.get("steps") or {}
    order = state.get("steps_order") or []
    unknown = [k for k in steps if k not in order]
    bad = [f"{k}={v.get('status')}" for k, v in steps.items()
           if v.get("status") not in STATUSES]
    return {"name": "状态合法：每步状态用词统一、无游离步骤",
            "ok": not bad and not unknown,
            "detail": f"{len(steps)} 步："
                      + "，".join(f"{k}={steps[k].get('status')}" for k in order if k in steps),
            "problems": bad + [f"游离步骤 {k}" for k in unknown]}


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 7 验收探针")
    ap.add_argument("job_dir")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    job_dir = Path(args.job_dir)
    state = P.load_job(job_dir)
    if state is None:
        print(f"{NO_} 没有 job.json：{job_dir}", file=sys.stderr)
        return 2

    checks = [
        check_contract(state),
        check_outputs(state, job_dir),
        check_inputs(state, job_dir),
        check_reproduce(state),
        check_relativized(state, job_dir),
        check_statuses(state),
    ]

    if args.json:
        print(json.dumps({"job_dir": str(job_dir), "checks": checks,
                          "ok": all(c["ok"] for c in checks)},
                         ensure_ascii=False, indent=2))
        return 0 if all(c["ok"] for c in checks) else 1

    print("=" * 72)
    print(f"Stage 7 验收 · {state.get('job_id')} · 状态 {state.get('status')}")
    print("=" * 72)
    for c in checks:
        print(f"{OK_ if c['ok'] else NO_} {c['name']}")
        print(f"    {c['detail']}")
        for p in c.get("problems") or []:
            print(f"      · {p}")
    passed = sum(1 for c in checks if c["ok"])
    print("-" * 72)
    print(f"通过 {passed}/{len(checks)}")
    print(f"复现命令：{state.get('reproduce')}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
