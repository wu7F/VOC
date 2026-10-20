#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 5 提示词零回归工具。

改造提示词模板之前先冻结「真实发出去的提示词」，改造后逐字节比对。
**为什么要用 --backend manual 而不是读模板文件**：
    模板文件是"原料"，真正影响模型输出的是**渲染之后**的成品。直接从队列里取
    （``pending/<task_id>.json`` 里的 ``system`` / ``user``）拿到的是走完全部
    代码路径的最终文本 —— 包括变量插值、维度块拼接、评论块预算截断。

用法::

    python tools/stage5_prompt_baseline.py --tag before          # 冻结
    python tools/stage5_prompt_baseline.py --tag after --compare out/verify/stage5/prompt_before/manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
DATA = PLATFORM.parent / "data" / "reddit_analysis"
CATEGORY = "Nursery Glider Recliner"


def run(cmd, cwd) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)


def collect(py: str, out: Path, frame: str = "") -> dict:
    """跑 tags / annotate 的 manual 后端，把队列里的提示词抓出来。"""
    fr = ["--frame", frame] if frame else []
    rel = str(DATA / "reddit_posts_relevant.csv")
    cmt = str(DATA / "reddit_comments.csv")
    res: dict = {}

    # 先要一个**已存在**的标签集：annotate 的提示词由标签集驱动，
    # 而 tags 走 manual 时不会产出 tagset.json（任务入队后即退出码 3）。
    # 用 --mock 生成即可 —— 标签集的内容在这里不重要，要的是「同一份标签集
    # 在改造前后是否渲染出同一段提示词」。
    print("[prep] 用 --mock 生成一份标签集给 annotate 用")
    tagset = out / "tagset_mock.json"
    r = run([py, "voc.py", "tags", "--category", CATEGORY, "--relevant", rel,
             "--comments", cmt, "--out", str(tagset), "--mock", *fr], PLATFORM)
    if r.returncode != 0:
        print("  !! 失败：", (r.stdout or "")[-800:], (r.stderr or "")[-800:])

    for step, cmd in (
        ("tags", [py, "voc.py", "tags", "--category", CATEGORY, "--relevant", rel,
                  "--comments", cmt, "--out", str(out / "tagset.json"),
                  "--backend", "manual", "--llm-queue", str(out / "q_tags"), *fr]),
        ("annotate", [py, "voc.py", "annotate", "--tagset", str(tagset),
                      "--relevant", rel, "--comments", cmt,
                      "--run-dir", str(out / "annot"),
                      "--backend", "manual", "--llm-queue", str(out / "q_annot"), *fr]),
    ):
        print(f"[{step}] 跑 manual 后端（预期退出码 3）…")
        r = run(cmd, PLATFORM)
        print(f"  退出码 {r.returncode}（3 = 等待人工回填，正常）")
        qdir = out / ("q_tags" if step == "tags" else "q_annot") / "pending"
        tasks = sorted(qdir.glob("*.json")) if qdir.is_dir() else []
        print(f"  队列任务 {len(tasks)} 条")
        blobs = []
        for t in tasks:
            d = json.loads(t.read_text(encoding="utf-8"))
            blobs.append({"task_id": t.stem, "system": d.get("system", ""),
                          "user": d.get("user", "")})
        res[step] = {"count": len(blobs), "tasks": blobs}
    return res


def digest(blobs: list[dict]) -> dict:
    out = {}
    for b in blobs:
        for part in ("system", "user"):
            key = f"{part}"
            out.setdefault(key, []).append(
                hashlib.sha1(b[part].encode("utf-8")).hexdigest()[:16])
    # system 在所有任务里相同 → 折叠成一个
    for k, v in out.items():
        out[k] = sorted(set(v))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Stage 5 提示词零回归")
    ap.add_argument("--tag", default="before")
    ap.add_argument("--frame", default="")
    ap.add_argument("--out-root", default="out/verify/stage5")
    ap.add_argument("--compare", default="")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)

    out = (PLATFORM / args.out_root / f"prompt_{args.tag}").resolve()
    if out.exists() and not args.keep:
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    data = collect(sys.executable, out, args.frame)
    manifest = {"tag": args.tag, "frame": args.frame or "(consumer)", "steps": {}}
    for step, info in data.items():
        blobs = info["tasks"]
        manifest["steps"][step] = {
            "count": info["count"],
            "system_sha1": sorted({hashlib.sha1(b["system"].encode("utf-8")).hexdigest()
                                   for b in blobs}),
            "user_sha1": [hashlib.sha1(b["user"].encode("utf-8")).hexdigest() for b in blobs],
        }
        # 留全文供人肉核对第一条
        if blobs:
            (out / f"{step}.system.txt").write_text(blobs[0]["system"], encoding="utf-8")
            (out / f"{step}.user.first.txt").write_text(blobs[0]["user"], encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    print(f"\n已写 {out / 'manifest.json'}")
    for step, m in manifest["steps"].items():
        print(f"  {step}: 任务 {m['count']} 条 | system sha1 {[s[:12] for s in m['system_sha1']]} "
              f"| user 条数 {len(m['user_sha1'])}")

    if args.compare:
        base = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        print(f"\n=== 与 {args.compare} 比对 ===")
        for step in sorted(set(base["steps"]) | set(manifest["steps"])):
            b = base["steps"].get(step)
            n = manifest["steps"].get(step)
            if not b or not n:
                print(f"  {step}: 只在一边存在")
                continue
            if b["system_sha1"] != n["system_sha1"]:
                print(f"  ! {step}.system 变了：{b['system_sha1']} -> {n['system_sha1']}")
            else:
                print(f"  = {step}.system 一致（{n['system_sha1'][0][:12]}）")
            if b["user_sha1"] != n["user_sha1"]:
                nb, nn = len(b["user_sha1"]), len(n["user_sha1"])
                diff = [i for i, (x, y) in enumerate(zip(b["user_sha1"], n["user_sha1"])) if x != y]
                print(f"  ! {step}.user 变了：{nb} → {nn} 条，前若干差异位置 {diff[:5]}")
            else:
                print(f"  = {step}.user 一致（{len(n['user_sha1'])} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
