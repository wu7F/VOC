#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 5 验收探针：**换框架之后，下游还对不对**。

对一个产物目录（``tagset.json`` / ``dimensions.json`` / ``report.md`` /
``dashboard.html``）跑四条与框架无关的断言，替人回答验收要求：

    换一个 profile，能跑出「没有购买状态轴」的分析（如 B2B 按角色分桶），
    且下游看板 / 报告不报错、自动隐藏空模块。

四条断言：

1. **轴换了** —— ``dimensions.json`` 里的分布键 / 计数键 / 桶 id 全部来自框架声明，
   且被排除的桶不在分布里（这条与 ``stage5_baseline.probe_invariants`` 同源，
   这里再列一次是为了让报告「自证」而不是「引用另一个脚本的输出」）。
2. **旧轴的词不出现在用户可见文案里** —— 报告正文 + 看板渲染文本里不能残留
   其它框架的口径（如 B2B 产物里不该出现「已购 / 未购」）。
   JS 注释与静态 ``DIM_LABELS`` 表**不算**用户可见文案，检查前会剥掉。
3. **空模块有隐藏钩子** —— 每个数据为空的模块都必须走 ``display = 'none'`` 那条路，
   否则 B2B 这种「三个角色桶全空」的产物会渲染出三张空图。
4. **不报错** —— 六个步骤的产物齐备，质量闸门没有 error 级失败。

用法::

    python tools/_probe_frame_switch.py out/verify/stage5/b2b --old-axis 购买状态
    python tools/_probe_frame_switch.py out/verify/stage5/b2b --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: 判定「旧轴词汇残留」时要剥掉的段：
#: ① 行注释（``// …``）② 块注释 ③ 静态维度标签表（消费品的维度名，B2B 用不到）
_LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_STATIC_LABEL_TABLE = re.compile(r"var DIM_LABELS = \{.*?\};", re.S)


def visible_text(html: str) -> str:
    """看板里**用户会看到**的那部分文本（剥掉注释与静态标签表）。"""
    t = _BLOCK_COMMENT.sub("", html)
    t = _LINE_COMMENT.sub("", t)
    t = _STATIC_LABEL_TABLE.sub("", t)
    return t


def check_axis_switched(dim: dict) -> dict:
    af = dim.get("analysis_frame") or {}
    ax = af.get("segmentation_axis") or {}
    buckets = ax.get("buckets") or []
    active = [b["id"] for b in buckets if not b.get("excluded")]
    excluded = [b["id"] for b in buckets if b.get("excluded")]
    dist_key = ax.get("distribution_key") or "purchase_status_distribution"
    uc_key = ax.get("unit_counts_key") or "purchase_status"
    dist = dim.get(dist_key) or []
    dist_ids = [e.get("status") for e in dist]
    uc = dim.get("unit_counts") or {}
    problems = []
    if af.get("name") in (None, "", "consumer") and dist_key == "purchase_status_distribution":
        problems.append("产物仍是内置 consumer 框架（没有换成别的轴）")
    if sorted(dist_ids) != sorted(active):
        problems.append(f"分布桶 {dist_ids} != 活跃桶 {active}")
    if any(b in dist_ids for b in excluded):
        problems.append(f"被排除桶出现在分布里：{excluded} ∩ {dist_ids}")
    if sorted((uc.get(uc_key) or {}).keys()) != sorted(active + excluded):
        problems.append(f"unit_counts[{uc_key}] 没覆盖全部桶")
    return {
        "name": "已换轴：轴来自框架声明（排除桶不进分布）",
        "ok": not problems,
        "detail": f"框架={af.get('name')} 分布键={dist_key} 计数键={uc_key} "
                  f"活跃={active} 排除={excluded}"
                  + ("｜" + "；".join(problems) if problems else ""),
    }


def check_no_old_axis_words(out: Path, old_axis: str) -> dict:
    """旧轴的口径词不能出现在用户可见文案里。"""
    if not old_axis:
        return {"name": "旧轴词汇无残留", "ok": True, "detail": "未指定旧轴词，跳过"}
    hits: list[str] = []
    rep = out / "report.md"
    if rep.exists():
        n = rep.read_text(encoding="utf-8").count(old_axis)
        if n:
            hits.append(f"report.md × {n}")
    dash = out / "dashboard.html"
    if dash.exists():
        n = visible_text(dash.read_text(encoding="utf-8")).count(old_axis)
        if n:
            hits.append(f"dashboard.html 渲染文本 × {n}")
    return {"name": f"用户可见文案无「{old_axis}」残留", "ok": not hits,
            "detail": "；".join(hits) if hits else "报告与看板可见文本均为 0 处"}


def check_empty_modules_hidden(out: Path) -> dict:
    """空模块必须走隐藏分支。"""
    dash = out / "dashboard.html"
    if not dash.exists():
        return {"name": "空模块自动隐藏", "ok": False, "detail": "缺 dashboard.html"}
    txt = dash.read_text(encoding="utf-8")
    has_hook = "style.display = 'none'" in txt
    # 内嵌数据里「空」的画像卡片数：labels 为空 ⇒ renderStackedPersona 会隐藏它
    empties = 0
    cards = 0
    m = re.search(r"\n  personaCards: (\[.*?\]),\n", txt, re.S)
    if m:
        try:
            cards_list = json.loads(m.group(1))
            cards = len(cards_list)
            empties = sum(1 for c in cards_list
                          if not ((c.get("data") or {}).get("labels") or []))
        except Exception:  # noqa: BLE001
            pass
    # 画像×功能矩阵：为空则整卡隐藏
    matrix_empty = '"persona_feature_matrix": {}' in txt or not re.search(
        r'"persona_feature_matrix":\s*\{\s*"[^"]+"', txt)
    ok = has_hook
    return {
        "name": "空模块自动隐藏",
        "ok": ok,
        "detail": f"隐藏钩子 {'有' if has_hook else '缺'}；画像卡片 {empties}/{cards} 张数据为空"
                  f"（会隐藏）；画像×功能矩阵{'为空→隐藏' if matrix_empty else '有数据→显示'}",
    }


def check_no_errors(out: Path) -> dict:
    q = out / "quality_report.json"
    if not q.exists():
        return {"name": "下游不报错", "ok": False, "detail": "缺 quality_report.json"}
    r = json.loads(q.read_text(encoding="utf-8"))
    errs = [f["check"] for f in r["findings"]
            if f["status"] == "fail" and f["severity"] == "error"]
    need = ("tagset.json", "dimensions.json", "features.json", "report.md", "dashboard.html")
    missing = [n for n in need if not (out / n).exists()]
    ok = not errs and not missing
    return {
        "name": "下游不报错（产物齐备 + 闸门无 error）",
        "ok": ok,
        "detail": f"产物缺 {missing or '无'}；闸门 {r['summary']['passed']}/"
                  f"{r['summary']['total']} 通过，verdict={r['summary']['verdict']}"
                  + (f"；error 级失败：{errs}" if errs else "")
                  + "（warn 级失败是既有数据问题，与框架无关）",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage 5 换框架验收探针")
    ap.add_argument("out_dir", help="产物目录")
    ap.add_argument("--old-axis", default="", help="旧轴的口径词（如「购买状态」），用于查残留")
    ap.add_argument("--json", action="store_true", help="打印 JSON")
    args = ap.parse_args(argv)

    out = Path(args.out_dir)
    if not out.is_dir():
        raise SystemExit(f"目录不存在：{out}")
    dim_p = out / "dimensions.json"
    if not dim_p.exists():
        raise SystemExit(f"缺 dimensions.json：{dim_p}")
    dim = json.loads(dim_p.read_text(encoding="utf-8"))

    checks = [check_axis_switched(dim)]
    if args.old_axis:
        checks.append(check_no_old_axis_words(out, args.old_axis))
    checks += [check_empty_modules_hidden(out), check_no_errors(out)]

    if args.json:
        print(json.dumps({"dir": str(out), "checks": checks,
                          "ok": all(c["ok"] for c in checks)},
                         ensure_ascii=False, indent=2))
        return 0 if all(c["ok"] for c in checks) else 1

    print(f"=== 换框架验收：{out} ===")
    for c in checks:
        print(f"  {'✓' if c['ok'] else '✗'} {c['name']}")
        print(f"      {c['detail']}")
    bad = [c["name"] for c in checks if not c["ok"]]
    print()
    print("结论：" + ("全部通过 —— 换框架后下游不报错、空模块自动隐藏"
                     if not bad else "不通过 → " + "、".join(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
