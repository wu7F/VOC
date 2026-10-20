#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针：框架渲染出的提示词 vs 改造前冻结的 golden 渲染稿，逐字节比对。

Stage 5 的硬指标是：**不换框架时，模型收到的每一个字都不变**。
这份探针就是它的可执行证据（之后会并进 tests/test_frame.py）。

fixture 与 ``tools/_freeze_prompts_baseline.py`` 保持一致 —— 基线是在那组
固定输入下冻的，换成别的输入就比不出结论。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "voc_core" / "kernel"))

from voc_core.analysis_frame import load_frame  # noqa: E402

dt = importlib.import_module("design_tags")
ap = importlib.import_module("annotate_posts")

GOLD = ROOT / "tests" / "fixtures" / "prompts_golden" / "rendered"

CATEGORY = "Nursery Glider Recliner"
SAMPLES = "[1] 标题: Best glider ever\n正文: I bought this glider and it is comfortable"
COMMENT_BLOCK_RENDERED = (
    "\n\n## 各帖的评论片段（共 2 条，覆盖 1 个帖子）\n"
    "**注意：以下内容全部是「评论片段，非发帖人自述」**—（fixture 截断）\n\n"
    "- c1: I love the swivel\n- c2: mine squeaks"
)
POSTS = [
    {"reddit_id": "p1", "title": "Best glider ever",
     "summary": "I bought this glider and it is comfortable", "mentioned_brands": "Babyletto", "_seq": 0},
    {"reddit_id": "p2", "title": "Looking for a recliner",
     "summary": "Any recommendations for a nursery chair?", "mentioned_brands": "", "_seq": 1},
]
FLAT = {
    "nursery_glider": {
        "dim": "features",
        "def": {
            "category": "feature",
            "thoughts": "用户提到摇篮椅的摇摆功能",
            "mentioned": "Output 'yes' or 'no'. If 'no', leave all other fields blank.",
            "original_text": "抽取打标对应的原文片段",
            "nursery_glider_summary": "具体功能点",
            "nursery_glider_reason": ["省力", "哄睡快"],
            "sentiment": "positive / neutral / negative",
        },
    },
    "budget_shopper": {
        "dim": "user_personas_non_purchased",
        "def": {
            "category": "user",
            "thoughts": "仅当发言者尚未购买时才标 yes",
            "mentioned": "Output 'yes' or 'no'. If 'no', leave all other fields blank.",
            "original_text": "抽取打标对应的原文片段",
            "budget_shopper_summary": "预算区间与比较维度",
            "sentiment": "n/a",
        },
    },
}

frame = load_frame(None)
system, user = dt.build_design_prompts(
    frame, category=CATEGORY, nsample=len(POSTS), samples=SAMPLES,
    comment_block=COMMENT_BLOCK_RENDERED, unit="帖子", secondary="评论",
    secondary_speaker="评论者", lang="zh")
annotate_sys = ap.build_annotate_system(frame, unit="帖子", secondary="评论",
                                        secondary_speaker="评论者", lang="zh")

cases = {
    "design_tags.system.zh.txt": system,
    "design_tags.user.zh.txt": user,
    "annotate.system.zh.txt": annotate_sys,
    "annotate.user.zh.txt": ap.build_user_prompt(POSTS[0], FLAT),
    "annotate.batch.user.zh.txt": ap.build_batch_user_prompt(POSTS, FLAT),
}

bad = 0
for name, got in cases.items():
    want = (GOLD / name).read_text(encoding="utf-8")
    if got == want:
        print(f"  = {name}  逐字节一致（{len(got)} 字符）")
        continue
    bad += 1
    print(f"  ! {name}  不一致：期望 {len(want)} 字符 / 实际 {len(got)} 字符")
    gm, wm = got.splitlines(), want.splitlines()
    for i in range(max(len(gm), len(wm))):
        a = gm[i] if i < len(gm) else "<缺行>"
        b = wm[i] if i < len(wm) else "<多行>"
        if a != b:
            print(f"    第 {i+1} 行：")
            print(f"      期望: {b!r}")
            print(f"      实际: {a!r}")
            break

print(f"\n不一致条数：{bad} / {len(cases)}")
sys.exit(1 if bad else 0)
