#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 6 验收工具：往真实产物里**故意灌坏数据**，看闸门拦不拦得住、说不说得清。

用法::

    python tools/make_bad_data.py \
        --stats ../data/reddit_analysis/reddit_analysis_stats.json \
        --dimensions ../reports/reddit_analysis_dimensions.json \
        --features-json ../data/reddit_analysis/reddit_analysis_features.json \
        --tagged ../data/reddit_analysis/reddit_posts_tagged.csv \
        --comments ../data/reddit_analysis/reddit_comments.csv \
        --out-dir out/verify/bad_data

每一处注入都对应一种**真实发生过的**失效方式（不是为凑数编的），出处写在 ``CASES``
的 ``why`` 里。**每处注入都声明它应当命中的检查项**，跑完逐个核对是否真的命中了 ——
否则"灌坏数据能拦住"这句话就是没有证据的。

注入后的产物与质量报告一起落盘，方便人工复核「报的到底是不是我注的那个错」。

退出码：

* ``0`` = 所有注入都被预期的检查项拦住
* ``1`` = 有注入漏网（期望命中的检查项没红）
* ``2`` = **有注入根本没落下去**（输入缺项/路径写错），或输入不齐直接拒绝运行。
  「没跑」必须比「漏网」更显眼 —— 否则一次路径写错就会得到一份假的"验收通过"。
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voc_core.validate import audit, build_context, render_console  # noqa: E402


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _read_raw(path: str) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write_raw(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _first(items: Any) -> dict | None:
    return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None


def _pick(dims: dict, *keys: str) -> tuple[str, dict] | tuple[None, None]:
    for k in keys:
        it = _first(dims.get(k))
        if it and it.get("evidence_posts"):
            return k, it
    return None, None


def _real_question(comments: list[dict]) -> dict | None:
    """从真实语料里捞一条**以问号结尾的句子**（保证引文仍可逐字回溯）。"""
    for c in comments:
        body = (c.get("body") or "").strip()
        if "?" in body:
            frag = body[:body.index("?") + 1].strip()
            if len(frag) > 12:
                return {"reddit_id": c.get("comment_id", ""),
                        "unit_type": "comment",
                        "post_reddit_id": c.get("post_reddit_id", ""),
                        "evidence_quote": frag}
    return None


# --------------------------------------------------------------------------
# 注入：每个函数返回一句人类可读的说明
# --------------------------------------------------------------------------

def inj_quote_paraphrased(a: dict) -> str:
    k, it = _pick(a["dimensions"], "usage_scenarios", "pre_purchase_concerns", "unmet_needs")
    it["evidence_posts"][0]["evidence_quote"] = "这条引文被模型润色过了"
    return f"{k} 的首条引文改成原文里没有的话"


def inj_count_drift(a: dict) -> str:
    k, it = _pick(a["dimensions"], "usage_scenarios")
    it["count"] = 0
    return f"{k} 首条 count 改成 0（小于证据条数，逻辑不可能）"


def inj_interrogative(a: dict) -> str:
    k, it = _pick(a["dimensions"], "post_purchase_experience", "unmet_needs")
    q = _real_question(a["comments"])
    if not q:
        return "（语料里找不到问句，跳过）"
    it["evidence_posts"] = [q]
    it["count"] = 1
    return f"{k} 的体验证据换成真实语料里的疑问句：{q['evidence_quote'][:28]}…"


def inj_persona_split(a: dict) -> str:
    k, it = _pick(a["dimensions"], "user_personas_purchased")
    it["comment_count"] = (it.get("comment_count") or 0) + 3
    return f"{k} 首条 comment_count +3 → post_count + comment_count != count"


def inj_label_symbols(a: dict) -> str:
    k, it = _pick(a["dimensions"], "usage_scenarios")
    it["label_cn"] = "家庭/亲子（客厅）"
    return f"{k} 标签名塞进斜杠与括号"


def inj_percentage_scale(a: dict) -> str:
    k, it = _pick(a["dimensions"], "usage_scenarios")
    it["percentage"] = (it.get("percentage") or 1.0) * 100
    return f"{k} 首条占比再乘一次 100"


def inj_empty_evidence_id(a: dict) -> str:
    k, it = _pick(a["dimensions"], "usage_scenarios")
    it["evidence_posts"][0]["reddit_id"] = ""
    return f"{k} 首条证据 reddit_id 置空"


def inj_missing_excluded_unknown(a: dict) -> str:
    uc = a["dimensions"].get("unit_counts")
    if isinstance(uc, dict):
        uc.pop("excluded_unknown_units", None)
        return "unit_counts 删掉 excluded_unknown_units"
    return "（没有 unit_counts）"


def inj_feature_legacy_fields(a: dict) -> str:
    f = _first(a["features"].get("features"))
    f["satisfaction_rate"] = 20.3
    return "功能条目加了历史字段名 satisfaction_rate"


def inj_satisfaction_wrong(a: dict) -> str:
    f = _first(a["features"].get("features"))
    f["satisfaction"] = 0.99
    return "功能满意度改成与 positive/(p+n+neutral) 不符的值"


def inj_duplicate_units(a: dict) -> str:
    rows = a["tagged"]
    if len(rows) > 1:
        rows.append(copy.deepcopy(rows[1]))
        return "单元表插入一行重复 id"
    return "（单元表为空）"


def inj_control_char(a: dict) -> str:
    rows = a["tagged"]
    if rows:
        rows[0]["title"] = str(rows[0].get("title", "")) + "\b"
        return "首行标题混入退格符 U+0008"
    return "（单元表为空）"


def inj_presentation_leak(a: dict) -> str:
    a["report"] = ("# Wayfair 用户洞察分析报告\n\n"
                   "## 三、高频子版块\n\n"
                   "清洗后相关评论：`out/wayfair/analysis/reddit_posts_relevant.csv`\n")
    a["profile"] = "wayfair"
    return "Wayfair 画像的输出里出现「子版块」与 reddit_ 文件名"


#: (id, 注入函数, 应当命中的检查项, 对应的真实失效方式)
CASES: list[tuple[str, Callable[[dict], str], str, str]] = [
    ("quote_paraphrased", inj_quote_paraphrased, "evidence_quote_verbatim",
     "硬规则①失效：标注时用了手写正则或模型润色，引文与原文不再逐字一致"),
    ("count_drift", inj_count_drift, "count_matches_evidence",
     "硬规则③失效：后处理单独改 count 没同步证据，图表数字与下钻列表对不上"),
    ("interrogative", inj_interrogative, "interrogative_evidence",
     "质检铁律③失效：「Any recommendations?」被当成购买后体验证据"),
    ("persona_split", inj_persona_split, "persona_split_sums",
     "硬规则④失效：人群画像柱内堆叠段错位"),
    ("label_symbols", inj_label_symbols, "label_cn_format",
     "字段规则失效：标签名退化成「家庭/亲子（客厅）」"),
    ("percentage_scale", inj_percentage_scale, "percentage_range",
     "已修 bug 的回归：fmt_pct 对 0<v<=1 再 ×100，0.8% 显示成 80.0%"),
    ("empty_evidence_id", inj_empty_evidence_id, "evidence_ids_unit_type",
     "契约失效：空 reddit_id 让看板静默跳过该证据卡"),
    ("missing_excluded_unknown", inj_missing_excluded_unknown, "required_structure",
     "口径透明度底线：漏掉 excluded_unknown_units，读者以为画像覆盖了全部语料"),
    ("feature_legacy_fields", inj_feature_legacy_fields, "feature_contract",
     "features 契约失效：历史字段名让「功能体验」页整页空白且不报错"),
    ("satisfaction_wrong", inj_satisfaction_wrong, "feature_satisfaction",
     "满意度口径失效：分子分母漂移，散点图全落在 0% 或 100%"),
    ("duplicate_units", inj_duplicate_units, "dup_unit_ids",
     "Phase 2' 坑①：同一帖被多个搜索词各导出一行，不去重计数成倍虚高"),
    ("control_char", inj_control_char, "control_chars",
     "质检铁律②：\\b 落盘成退格符，正则永不命中而脚本看似正常"),
    ("presentation_leak", inj_presentation_leak, "presentation_vocabulary",
     "Stage 3 呈现边界失效：换源后仍残留 Reddit 专属词与 reddit_ 文件名"),
]


#: 验收必须齐备的输入。缺任何一项都会让对应注入**静默跳过**，而闸门照样输出
#: 一份"看起来跑过了"的报告 —— 这正是本阶段要消灭的假绿。
#: （真实踩坑：路径写错时脚本照常打印一大段结果、退出码仍是 0，看不出什么都没验。）
_REQUIRED_INPUTS: tuple[tuple[str, str], ...] = (
    ("--stats", "stats"),
    ("--dimensions", "dimensions"),
    ("--features-json", "features_json"),
    ("--tagged", "tagged"),
    ("--comments", "comments"),
)

#: 注入函数返回以此开头的描述 = **没能真正注入**（输入里没有可改的对象）
_NOT_INJECTED_PREFIX = "（"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage 6 验收：灌坏数据看闸门拦不拦得住")
    ap.add_argument("--stats", default="")
    ap.add_argument("--dimensions", default="")
    ap.add_argument("--features-json", dest="features_json", default="")
    ap.add_argument("--tagged", default="")
    ap.add_argument("--relevant", default="")
    ap.add_argument("--comments", default="")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir)

    # 前置闸门：输入缺一不可。否则注入静默跳过 → "全绿"，等于没验。
    missing = []
    for flag, dest in _REQUIRED_INPUTS:
        val = getattr(args, dest)
        if not val:
            missing.append(f"{flag} 未提供")
        elif not Path(val).exists():
            missing.append(f"{flag}={val} 文件不存在")
    if missing:
        print("✗ 验收输入不齐，拒绝运行（避免「没跑也算过」）：")
        for m in missing:
            print(f"    - {m}")
        print("\n  提示：真实产物在 <项目根>/data/reddit_analysis/，"
              "dimensions 在 <项目根>/reports/。")
        return 2  # 拒绝运行不留任何产物，避免把空目录误当"跑过了"

    out_dir.mkdir(parents=True, exist_ok=True)

    def load(p: str) -> dict:
        f = Path(p) if p else None
        return json.loads(f.read_text(encoding="utf-8")) if f and f.exists() else {}

    base: dict[str, Any] = {
        "dimensions": load(args.dimensions),
        "features": load(args.features_json),
        "tagged": _read_raw(args.tagged),
        "comments": _read_raw(args.comments),
        "report": "",
        "profile": "reddit",
    }
    if not base["dimensions"]:
        raise SystemExit("需要 --dimensions 才能灌入归属层的坏数据")

    print("=" * 74)
    print("Stage 6 验收 · 故意灌入坏数据")
    print("=" * 74)
    uninjected: list[tuple[str, str]] = []
    for name, fn, expects, why in CASES:
        desc = fn(base)
        if desc.startswith(_NOT_INJECTED_PREFIX):
            uninjected.append((name, desc))
        print(f"  [{name:24s}] {desc}")
        print(f"  {'':26s} 应当命中：{expects}")
        print(f"  {'':26s} 真实失效：{why}")

    # 落盘注入后的产物，便于人工复核
    (out_dir / "dimensions.bad.json").write_text(
        json.dumps(base["dimensions"], ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "features.bad.json").write_text(
        json.dumps(base["features"], ensure_ascii=False, indent=2), encoding="utf-8")
    if base["report"]:
        (out_dir / "report.bad.md").write_text(base["report"], encoding="utf-8")
    tagged_out = out_dir / "units_tagged.bad.csv"
    if base["tagged"]:
        _write_raw(tagged_out, base["tagged"])

    ns = SimpleNamespace(
        stats=args.stats,
        dimensions=str(out_dir / "dimensions.bad.json"),
        features_json=str(out_dir / "features.bad.json"),
        tagged=str(tagged_out) if base["tagged"] else "",
        relevant=args.relevant,
        comments=args.comments,
        report=str(out_dir / "report.bad.md") if base["report"] else "",
        dashboard="",
        profile=base["profile"] or "reddit",
        max_quote_miss_rate=0.0,
    )
    report = audit(build_context(ns))
    (out_dir / "quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(render_console(report))

    failed = {f["check"] for f in report["findings"] if f["status"] == "fail"}
    caught = [c for c in CASES if c[2] in failed]
    missed = [c for c in CASES if c[2] not in failed]
    print()
    print(f"注入 {len(CASES)} 处 → 闸门拦住 {len(caught)} 处"
          f"（另有 {len(failed) - len(caught)} 条检查项被连带触发）")
    print(f"被拦住的检查项：{', '.join(sorted(failed))}")
    if missed:
        print()
        print("⚠ 以下注入未被其预期检查项命中：")
        for name, _, expects, _ in missed:
            print(f"    {name} → 期望 {expects}")
    if uninjected:
        print()
        print("✗ 以下注入**没有真正落下去**（输入里没有可改的对象）—— 它们等于没验：")
        for name, desc in uninjected:
            print(f"    {name}: {desc}")
    print(f"\n注入后的产物与质量报告：{out_dir}")
    if uninjected:
        return 2  # 「没跑」不能算过，必须比「漏网」更显眼
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
