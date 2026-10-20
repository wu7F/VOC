#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 8 变异验证：证明这套回归**真的守得住**退化，而不是看着绿。

思路（沿用 Stage 6 ``test_validate.py`` 的规矩）：在一个**好的**产物链上注入一处
特定改动，断言「恰好是那条判据翻红」。三个变异对应三种不同的退化形状，用来证明
两层判据**不冗余**：

* **M1 · 不明者重新计入「已购」**（改 ``status_stat`` 的桶名）
  真实的「购买状态不明」单元被算进已购桶：已购 4223 → 7700、排除桶 3477 → 0。
  期望：**锚点层**报 ``purchase_status.purchased`` 与 ``excluded_unknown_units``。

* **M2 · 画像归桶闸门写反**（``status != persona_map[dim]`` → ``==``）
  这正是路线图点名的那次改动（「移除购买状态不明者画像」）的**反向**：
  闸门本来是「桶不匹配就跳过」，写反后变成「桶匹配就跳过」，于是只有
  购买状态与画像桶**不一致**的单元才被计数，画像覆盖从 1284 掉到 0。
  分段轴计数与排除桶**完全不变**，动的只有画像覆盖。
  期望：**锚点层**报 ``metrics.persona_covered_units``。
  —— 有了这条，才说明「画像覆盖率」不只是个展示数字，而是被守着的判据。
  （踩坑：最初这处写成 ``and False`` 关掉闸门 —— **零反应**。判别实验证明闸门
  可达，但「关掉」在这条链上不等于「反向」；只有写反条件才真的改了计数。）

* **M3 · 证据截断**（品牌证据引文 400 字 → 200 字）
  计数、百分比、情感分布、关键结论**全都不变**，闸门也照样通过
  （截断后的前缀仍是原文逐字子串）。只有产物指纹会变。
  期望：**基线层**报 ``products``，而**锚点层一条都不报**。
  —— 专门用来证明「只有锚点是不够的」。

用法::

    python tools/_mutate_stage8.py            # 跑全部变异
    python tools/_mutate_stage8.py --mutation 1
    python tools/_mutate_stage8.py --keep     # 不还原（调试用，危险）

退出码：0 = 全部变异都被抓住 ｜ 1 = 有变异没被抓住（回归有洞）。

> 踩坑留痕：最初 M1 瞄在 ``axis_bucket_of`` 的兜底分支（``return unknown_bucket``）上，
> 结果**毫无反应** —— 真实标注里 ``ps_unknown`` 是**显式命中**（``mentioned=yes``）的，
> 走的是 ``len(hits) == 1`` 正常路径，那个兜底在这份数据上从不执行。
> 用「把该行改成 ``raise`` 看会不会崩」的判别实验才定位到。
> **教训：变异点必须先在真实数据上验证可达，否则会误判成「回归有洞」。**
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
if str(PLATFORM) not in sys.path:
    sys.path.insert(0, str(PLATFORM))

from voc_core import eval as EV  # noqa: E402

CASE_ID = "reddit-ng-consumer"
AGG = "voc_core/kernel/aggregate_annotations.py"

#: (编号, 说明, 相对路径, 原文, 改后, 期望的锚点违反键, 是否期望基线漂移)
MUTATIONS: list[tuple[int, str, str, str, str, list[str], bool]] = [
    (1, "不明者重新计入「已购」：unknown 单元改落进 purchased 桶",
     AGG,
     '            st = status_stat.setdefault(status, new_bucket())',
     '            st = status_stat.setdefault(\n'
     '                "purchased" if status == unknown_bid else status, new_bucket())  # MUTATION',
     ["purchase_status.purchased", "excluded_unknown_units"],
     True),

    (2, "画像归桶闸门写反：只累计**桶不匹配**的单元（画像覆盖 1284 → 0）",
     AGG,
     "            if use_gating and dim in persona_map and status != persona_map[dim]:\n"
     "                continue",
     "            if use_gating and dim in persona_map and status == persona_map[dim]:   # MUTATION\n"
     "                continue",
     ["persona_covered_units"],
     True),

    (3, "证据截断：品牌证据引文 400 字 → 200 字（计数与结论全不变）",
     AGG,
     "                    quote = txt[s0:e0].strip()[:400]",
     "                    quote = txt[s0:e0].strip()[:200]   # MUTATION",
     [],
     True),
]


def sha1_bytes(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def apply_mutation(path: Path, old: str, new: str) -> bytes:
    """写入变异（含回读校验），返回**原始字节**供还原。"""
    original = path.read_bytes()
    src = original.decode("utf-8")
    if new in src:
        raise SystemExit(f"变异似乎已存在：{path}")
    if src.count(old) != 1:
        raise SystemExit(f"锚点命中 {src.count(old)} 次（期望 1）：{path}")
    out = src.replace(old, new, 1)
    compile(out, str(path), "exec")
    tmp = path.with_name(path.name + ".newfile")
    tmp.write_text(out, encoding="utf-8")
    os.remove(path)
    os.rename(tmp, path)
    #: 本机覆盖写会偶发静默失败 —— 写后必须回读，否则「变异没生效」会被误读成
    #: 「回归有洞」，白查半天。
    if path.read_text(encoding="utf-8") != out:
        raise SystemExit(f"变异写入未生效：{path}")
    return original


def restore(path: Path, original: bytes, digest: str) -> bool:
    tmp = path.with_name(path.name + ".restore")
    tmp.write_bytes(original)
    os.remove(path)
    os.rename(tmp, path)
    return sha1_bytes(path.read_bytes()) == digest


def run_case() -> dict:
    """在变异后的代码上跑一次核心案，返回报告。"""
    cases = EV.discover_cases(only=[CASE_ID])
    return EV.evaluate(cases, work_root=PLATFORM / "out/verify/stage8",
                       log=EV.SILENT)


def check(mut_id: int, desc: str, rel: str, old: str, new: str,
          want_anchors: list[str], want_drift: bool, keep: bool) -> bool:
    path = PLATFORM / rel
    digest = sha1_bytes(path.read_bytes())
    print(f"\n── 变异 {mut_id} ── {desc}")
    print(f"   目标 {rel}（sha1 {digest[:12]}）")

    original = apply_mutation(path, old, new)
    try:
        report = run_case()
    finally:
        if keep:
            print("   ! --keep：不还原，请手工恢复")
        else:
            if not restore(path, original, digest):
                raise SystemExit("还原失败，源文件可能已损坏")
            print("   还原：✓ sha1 一致")

    entry = report["cases"][0]
    got_anchors = sorted(v["key"] for v in entry["violations"])
    got_drift = sorted(d["key"] for d in entry["drift"])
    print(f"   回归退出码 {report['exit_code']} ｜ 锚点违反 {got_anchors or '无'}")
    print(f"   漂移键 {got_drift or '无'}")

    ok = True
    if not entry["ok"]:
        print(f"   ✗ 案子没跑通：{entry['error']}")
        ok = False
    for key in want_anchors:
        if key not in got_anchors:
            print(f"   ✗ 期望锚点违反 {key}，但没有")
            ok = False
    if want_anchors:
        extra = [k for k in got_anchors if k not in want_anchors]
        if extra:
            print(f"   · 另有锚点违反（不算失败，供参考）：{extra}")
    if want_drift and not got_drift:
        print("   ✗ 期望基线漂移，但没有")
        ok = False
    if not want_drift and got_drift:
        print(f"   ✗ 期望零漂移，但报了 {got_drift}")
        ok = False
    if report["exit_code"] == 0:
        print("   ✗ 回归退出码为 0（退化没被拦住）")
        ok = False
    print(f"   {'✓ 被抓住' if ok else '✗ 没被抓住'}")
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage 8 变异验证")
    ap.add_argument("--mutation", type=int, default=0, help="只跑该编号（默认全部）")
    ap.add_argument("--keep", action="store_true", help="不还原（调试用）")
    args = ap.parse_args(argv)

    picked = [m for m in MUTATIONS if not args.mutation or m[0] == args.mutation]
    if not picked:
        print("没有匹配的变异。")
        return 2

    print("=" * 78)
    print("Stage 8 变异验证：注入退化，断言回归把它抓住")
    print("=" * 78)

    results = []
    for mid, desc, rel, old, new, want_a, want_d in picked:
        results.append(check(mid, desc, rel, old, new, want_a, want_d, args.keep))

    print("\n" + "-" * 78)
    n_ok = sum(1 for r in results if r)
    print(f"结论：{n_ok}/{len(results)} 个变异被回归抓住"
          + ("（回归有效）" if n_ok == len(results) else "（** 回归有洞 **）"))
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
