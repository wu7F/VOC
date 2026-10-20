#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 8 · 评测回归器（命令行入口）。

与 ``voc.py eval`` 同一套内核（:mod:`voc_core.eval`），单独留一个脚本是为了
**不依赖 voc.py 的参数解析** —— CI 里直接 ``python tools/regress.py`` 就能跑，
这也是本仓库 Stage 4/5/6 验收脚本的老规矩。

用法::

    python tools/regress.py                     # 比对基线（有漂移则退出码 1）
    python tools/regress.py --case reddit-ng-consumer
    python tools/regress.py --tier extended     # 只跑扩展案
    python tools/regress.py --freeze            # 首次建立基线
    python tools/regress.py --update            # 已确认改动，接受新答案
    python tools/regress.py --markdown out/verify/stage8/regress.md
    python tools/regress.py --list              # 只列出评测案

退出码：0 = 锚点全过且无漂移 ｜ 1 = 锚点违反或有漂移 ｜ 2 = 配置/输入错误。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
if str(PLATFORM) not in sys.path:
    sys.path.insert(0, str(PLATFORM))

from voc_core import eval as EV  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="regress",
        description="Stage 8 评测回归：改口径之后，结论有没有漂",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "两层判据：\n"
            "  锚点（eval/golden/<案>/expected.json）—— 人写的下界断言，违反即失败\n"
            "  基线（eval/baselines/<案>.json）      —— 冻结的指标快照，变了就列出来\n\n"
            "退出码：0 通过 ｜ 1 违反/漂移 ｜ 2 配置错误"
        ),
    )
    ap.add_argument("--case", default="", help="只跑这些案（逗号分隔 id）")
    ap.add_argument("--tier", default="",
                    help="只跑该 tier（core / extended）；默认 core") 
    ap.add_argument("--all", action="store_true",
                    help="跑全部 tier（含 ~3 分钟/案的 mock 端到端案）")
    ap.add_argument("--golden-dir", dest="golden_dir", default=EV.DEFAULT_GOLDEN_DIR)
    ap.add_argument("--baselines-dir", dest="baselines_dir",
                    default=EV.DEFAULT_BASELINES_DIR)
    ap.add_argument("--work-root", dest="work_root", default="out/verify/stage8")
    ap.add_argument("--freeze", action="store_true", help="写基线（首次建立用）")
    ap.add_argument("--update", action="store_true",
                    help="覆盖基线（已确认改动、要接受新答案时用）")
    ap.add_argument("--markdown", default="", help="把 Markdown 对比表写到该路径")
    ap.add_argument("--json", action="store_true", help="stdout 直接打印 JSON")
    ap.add_argument("--list", action="store_true", help="只列出评测案后退出")
    ap.add_argument("--keep-going", action="store_true", default=True,
                    help="某个案跑不通也继续跑后面的（默认开）")
    args = ap.parse_args(argv)

    try:
        cases = EV.discover_cases(
            args.golden_dir,
            only=[s for s in (args.case or "").split(",") if s.strip()],
            tier=EV.resolve_tier(args.all, args.tier))
    except (FileNotFoundError, ValueError) as exc:
        print(f"配置错误：{exc}")
        return 2

    if not cases:
        print("没有可跑的评测案。")
        return 2

    if args.list:
        print(f"{'id':26s} {'tier':9s} {'mode':12s} {'框架':12s} 说明")
        print("-" * 96)
        for c in cases:
            print(f"{c.id:26s} {c.tier:9s} {c.mode:12s} {c.frame or '-':12s} "
                  f"{c.label}")
        print(f"\n共 {len(cases)} 个评测案。")
        return 0

    report = EV.evaluate(cases, baselines_dir=args.baselines_dir,
                         work_root=args.work_root,
                         freeze=args.freeze, update=args.update,
                         keep_going=args.keep_going)

    if args.markdown:
        p = Path(args.markdown)
        if not p.is_absolute():
            p = PLATFORM / p
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(EV.render_markdown(report), encoding="utf-8")
        print(f"\nMarkdown 对比表：{p}")

    if args.json:
        import json
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print()
        print(EV.render_console(report))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
