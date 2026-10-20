#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 7 收尾清理：把一次性补丁脚本归档后删除，并清掉验收留下的临时目录。

这些脚本都是**一次性**的：它们的效果已经烘进已验证通过的
``voc.py`` / ``voc_core/pipeline.py`` / ``tests/test_pipeline.py``，
再跑一遍只会二次改写甚至报错，留在 ``tools/`` 里是噪音。

先打包到 ``out/verify/stage7/_archive/stage7-patches.zip`` 再删 —— 可逆。
"""

from __future__ import annotations

import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # voc-platform/tools
ROOT = HERE.parent                              # voc-platform

# 一次性补丁 / 摸底盘（效果已在终版里验证）
PATCHES = [
    "_p34_build_parser.py", "_p34_build_parser2.py",
    "_p35_fix_pipeline.py", "_p35b_fix_req.py", "_p35c_fix_loadjob.py",
    "_p35d_reproduce.py", "_p35e_fixes.py", "_p35f_patch.py",
    "_p35g_postcode.py", "_p35h_both_files.py", "_p35i_brand_judge.py",
    "_p35j_exact_inputs.py", "_p35k_exact_inputs.py", "_p35l_fast_skip.py",
    "_p35m_fast_intact.py", "_p35n_fast_intact.py", "_p35o_quoted_cmd.py",
    "_p35p_quoted_cmd.py",
    "_p36_cli_run.py", "_p37_fix_tests.py",
    "_probe_brands.py", "_probe_brands2.py",
]

# 验收途中的临时目录（dry-run 输出 / 包导入探针）
SCRATCH_DIRS = [
    ROOT / "out" / "verify" / "stage7" / "_dry",
    ROOT / "out" / "verify" / "stage7" / "_wtest",
]


def main() -> int:
    archive_dir = ROOT / "out" / "verify" / "stage7" / "_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    zpath = archive_dir / "stage7-patches.zip"

    existing = [HERE / n for n in PATCHES if (HERE / n).exists()]
    missing = [n for n in PATCHES if not (HERE / n).exists()]

    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in existing:
            zf.write(p, p.name)

    print(f"归档 {len(existing)} 个脚本 -> {zpath.relative_to(ROOT)}"
          f"（{zpath.stat().st_size} 字节）")
    if missing:
        print(f"（{len(missing)} 个原本就不在：{', '.join(missing)}）")

    # 验证归档可读，再删原件
    with zipfile.ZipFile(zpath) as zf:
        bad = zf.testzip()
        if bad:
            print(f"归档损坏：{bad}", file=sys.stderr)
            return 1
        assert len(zf.namelist()) == len(existing)

    removed = 0
    for p in existing:
        try:
            p.unlink()
            removed += 1
        except OSError as exc:  # noqa: BLE001
            print(f"删除失败 {p.name}：{exc}", file=sys.stderr)
    print(f"删除脚本 {removed}/{len(existing)}")

    dirs_removed = 0
    for d in SCRATCH_DIRS:
        if d.exists():
            shutil.rmtree(d)
            dirs_removed += 1
            print(f"删除临时目录 {d.relative_to(ROOT)}")
    print(f"删除临时目录 {dirs_removed}/{len(SCRATCH_DIRS)}")

    left = sorted(p.name for p in HERE.glob("_p3*.py"))
    if left:
        print(f"⚠️ tools/ 仍残留：{left}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
