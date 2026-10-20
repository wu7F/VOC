#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**一次性**基线冻结：把改造前**内嵌在代码里**的提示词原样导出。

为什么需要这一步（Stage 4 的方法论，与 Stage 3 的「逐字不变」同一套）：

    把提示词从 Python 字符串搬到 ``prompts/*.md``，是**纯搬家**，不是重写。
    但提示词里有大量不可见细节——行尾空格、空行数量、``{tk}_summary`` 这种
    占位符与真占位符混在一起。靠肉眼复刻必然走样，而提示词走样的后果是
    **模型输出悄悄变了**，没有任何测试会发现。

所以先在改造前把两份产物落盘：

* ``templates/`` —— 模板**原文**（把 ``{var}`` 换成 ``${var}`` 后的文本）。
  搬到 ``prompts/`` 时直接复制这些文件，保证模板本身字节一致。
* ``rendered/`` —— 用固定 fixture 变量渲染出来的**成品提示词**。
  改造后由 ``tests/test_llm.py::TestPromptZeroRegression`` 逐字节比对，
  连同 ``templates/`` 一起，证明搬家零回归。

改造完成后本脚本即失效（内核里不再有这些常量），保留它只是留个证据链。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "voc_core" / "kernel"))

import annotate_posts as ap  # noqa: E402
import design_tags as dt  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "prompts_golden"
TEMPLATES = OUT / "templates"
RENDERED = OUT / "rendered"

# --------------------------------------------------------------------------
# fixture：固定输入。必须覆盖「条件行」——即带 reason 词表的标签与不带的标签
# 都要出现，否则测不出 annotate.tag_block 的条件拼接。
# --------------------------------------------------------------------------

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

#: 覆盖：带 reason 的体验类标签、不带 reason 的分布类标签、两个维度
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


def w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"  {path.relative_to(ROOT)}  ({len(text)} 字符)")


def to_template(text: str, names: tuple[str, ...]) -> str:
    """把真占位符 ``{name}`` 改写成 ``${name}``；其余花括号（如 ``{tk}_summary``）原样保留。

    坑（踩过一次）：交替分支**必须**用 ``(?:...)`` 包起来，且 ``\\{`` / ``\\}`` 要
    各自贴在**整个交替**外面。写成 ``\\{a|b\\}`` 会被解析为「``{a`` **或** ``b}``」，
    于是裸单词 ``category`` 也会被匹配 → 产出 ``{${ategor}}`` 这种废模板。
    包上 ``(?:)`` 之后，花括号本身就把 ``{n}`` 与 ``{nsample}`` 区分开了。
    """
    pattern = re.compile(r"\{(?:" + "|".join(sorted(names, key=len, reverse=True)) + r")\}")
    return pattern.sub(lambda m: "${" + m.group(0)[1:-1] + "}", text)


def _assert_equivalent(name: str, template: str, fmt_args: dict, fmt_text: str) -> None:
    """自检：用 ``string.Template`` 渲染模板，必须与改造前 ``.format()`` 的结果**逐字相同**。

    没有这一步，``to_template`` 写错了也不会有人发现 —— 而错误的模板会被
    原样搬进 ``prompts/``，直到模型输出变烂才暴露。
    """
    from string import Template

    got = Template(template).substitute(fmt_args)
    if got != fmt_text:
        for i, (a, b) in enumerate(zip(got.splitlines(), fmt_text.splitlines())):
            if a != b:
                raise AssertionError(
                    f"{name}: 模板自检失败，首个差异在第 {i + 1} 行\n"
                    f"  template → {a!r}\n  .format() → {b!r}")
        raise AssertionError(f"{name}: 模板自检失败（行数不同）")
    if "{${" in template:
        raise AssertionError(name + ": 模板里出现 '{${' 说明替换写坏了")
    print(f"  ✓ 自检通过：{name}（Template 渲染 == 原 .format() 结果）")


def main() -> int:
    print("冻结改造前基线 →", OUT.relative_to(ROOT))

    # ---- design_tags：三个常量都是普通字符串（非 f-string）----
    w(TEMPLATES / "design_tags.system.zh.txt", dt.DESIGN_SYSTEM)

    user_tpl = to_template(dt.DESIGN_USER, ("category", "nsample", "samples", "comment_block", "n"))
    user_args = dict(category=CATEGORY, nsample=len(POSTS), n=dt.SAMPLE_TAGS_PER_DIM,
                     samples=SAMPLES, comment_block=COMMENT_BLOCK_RENDERED)
    user_rendered = dt.DESIGN_USER.format(**user_args)
    _assert_equivalent("design_tags.user", user_tpl, user_args, user_rendered)
    w(TEMPLATES / "design_tags.user.zh.txt", user_tpl)

    cb_tpl = to_template(dt.COMMENT_BLOCK, ("ncomments", "nposts", "comments"))
    cb_args = dict(ncomments=2, nposts=1, comments="- c1: I love the swivel\n- c2: mine squeaks")
    _assert_equivalent("design_tags.comment_block", cb_tpl, cb_args, dt.COMMENT_BLOCK.format(**cb_args))
    w(TEMPLATES / "design_tags.comment_block.zh.txt", cb_tpl)

    w(RENDERED / "design_tags.system.zh.txt", dt.DESIGN_SYSTEM)
    w(RENDERED / "design_tags.user.zh.txt", user_rendered)

    # ---- annotate：SYSTEM 是常量；user / batch 是过程拼装，只能冻渲染结果 ----
    w(TEMPLATES / "annotate.system.zh.txt", ap.ANNOTATE_SYSTEM)
    w(RENDERED / "annotate.system.zh.txt", ap.ANNOTATE_SYSTEM)
    w(RENDERED / "annotate.user.zh.txt", ap.build_user_prompt(POSTS[0], FLAT))
    w(RENDERED / "annotate.batch.user.zh.txt", ap.build_batch_user_prompt(POSTS, FLAT))

    print("\n完成。注意：annotate 的 user / batch 是过程拼装，没有模板原文可导出，")
    print("模板需要手写，但**渲染结果**已冻结，测试会逐字节比对。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
