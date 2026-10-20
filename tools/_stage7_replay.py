#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 7 验收 · 「从 job.json 复现」。

路线图的验收条件只有一句：``runs/<job_id>/job.json`` 能完整复现一次分析。

本脚本拿一个**已跑完**的 job，只读它的 ``job.json``：

1. 用 ``JobSpec.from_dict(state["spec"])`` 还原参数（不看原命令行）；
2. 在**新目录**里重跑（输入取自 ``<job>/input/``，即 job 自包含的那份数据）；
3. 逐产物比 sha1 —— 一致才算复现成功。

比较前会做一次**路径归一化**：产物里会印数据文件路径（``stats.json`` 的
``input_path``、数据概览的附录……），换了 job 目录必然不同 —— 那不是「不可复现」，
是「同一份分析在另一个目录」。把两边的 job 目录都替换成 ``<JOB>`` 再比才是要点。

注意反斜杠有**三种**写法要覆盖：原样、正斜杠、以及 JSON 里的转义双写
（``\\``）—— 漏掉最后一种时 ``.json`` 里的路径一个都替换不掉。

用法::

    python tools/_stage7_replay.py out/verify/stage7/reddit out/verify/stage7/replay
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
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


def _variants(p: Path) -> list[bytes]:
    """同一个目录路径在产物里可能出现的几种写法。"""
    s = str(p)
    return [v.encode("utf-8") for v in (
        s,                              # out\verify\stage7\reddit
        s.replace("\\", "/"),           # out/verify/stage7/reddit
        s.replace("/", "\\"),           # out\verify\stage7\reddit
        s.replace("\\", "\\\\"),        # out\\verify\\stage7\\reddit（JSON 转义）
        s.replace("/", "\\\\"),         # out\\verify\\stage7\\reddit
    )]


def norm_sha(p: Path, job_dirs: list[Path]) -> str:
    """把 job 目录出现的地方折叠成 ``<JOB>`` 再算哈希（两边的目录都折叠）。"""
    try:
        data = p.read_bytes()
    except OSError:
        return ""
    for d in job_dirs:
        for v in _variants(d):
            data = data.replace(v, b"<JOB>")
    return hashlib.sha1(data).hexdigest()


def main() -> int:
    _utf8_stdout()
    ap = argparse.ArgumentParser(description="从 job.json 复现一次分析")
    ap.add_argument("src_job")
    ap.add_argument("dst_job")
    ap.add_argument("--only", default="clean,tags",
                    help="复现哪几步（默认 clean,tags：clean 是纯计算、tags 是 mock LLM，"
                         "两者都确定 —— 足以证明『参数 + 数据 → 产物』可重放）")
    args = ap.parse_args()

    src = Path(args.src_job)
    dst = Path(args.dst_job)
    state = P.load_job(src)
    if state is None:
        print(f"✗ 没有 job.json：{src}", file=sys.stderr)
        return 2

    steps = [s for s in args.only.split(",") if s.strip()]
    print("=" * 72)
    print(f"从 job.json 复现 · {state.get('job_id')} · 只读 job.json 的参数")
    print("=" * 72)

    # 只从 job.json 还原参数 —— 不碰原始命令行
    spec = P.JobSpec.from_dict(state["spec"])
    print(f"spec.input      = {spec.input}")
    print(f"spec.category   = {spec.category}")
    print(f"spec.frame      = {spec.frame or '(consumer)'}")
    print(f"spec.mock       = {spec.mock}")
    print(f"复现步骤        = {steps}")

    if dst.exists():
        shutil.rmtree(dst)
    result = P.run_job(spec, dst, only=steps, log=lambda *a, **k: None)
    print(f"\n复现运行：status={result.status} exit={result.exit_code} "
          f"用时 {result.elapsed_s:.1f}s")

    dirs = [src, dst]

    def paired() -> list[tuple[str, Path, Path]]:
        pairs: list[tuple[str, Path, Path]] = []
        for sub in ("out", "tags", "report"):
            names: set[str] = set()
            for base in (src, dst):
                d = base / sub
                if d.is_dir():
                    names |= {p.name for p in d.glob("*")}
            for name in sorted(names):
                pairs.append((f"{sub}/{name}", src / sub / name, dst / sub / name))
        return pairs

    print(f"\n{'产物':34s} {'原 job':14s} {'复现':14s} 结论")
    print("-" * 72)
    checked = same = 0
    only_src: list[str] = []
    for name, a, b in paired():
        if not b.exists():
            only_src.append(name)          # 这一步没复现（不参与判定）
            continue
        if not a.exists():
            print(f"{name:34s} {'—':14s} {norm_sha(b, dirs)[:12]:14s} ✗ 复现多出")
            checked += 1
            continue
        ha, hb = norm_sha(a, dirs), norm_sha(b, dirs)
        ok = ha == hb
        checked += 1
        same += ok
        print(f"{name:34s} {ha[:12]:14s} {hb[:12]:14s} {'✓ 一致' if ok else '✗ 不同'}")
    print("-" * 72)
    if only_src:
        print(f"（未复现的步骤产物，不参与判定：{'、'.join(only_src)}）")
    print(f"结论：{'复现成功' if checked and same == checked else '复现有差异'}"
          f" —— {same}/{checked} 个产物一致（已按 <JOB> 归一化路径）")
    return 0 if checked and same == checked else 1


if __name__ == "__main__":
    sys.exit(main())
