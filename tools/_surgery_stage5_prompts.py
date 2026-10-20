#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性手术：把提示词里「跟分析框架有关」的内容抽进 frames/consumer.yaml。

**为什么必须一次性做完、且有自检**：
    ``design_tags.system`` 与 ``design_tags.user`` 里硬编码着 8 个维度、
    「按购买状态分边」、「不要输出 purchase_status」以及完整的 JSON 结构示例
    —— 那就是「分析什么」。换框架（B2B 按决策角色分桶）时这些句子必须跟着换，
    否则模型会照样输出合法 JSON、只是每一条都在回答错误的问题。

**逐字节保证**：本脚本从**原模板文件**里按行切出段落（而不是让我手抄一遍），
    UTF-8 逐行搬运 + 只把维度行/统计数字替换成 ``${变量}``。跑完立刻用
    consumer 框架回渲一遍，与 ``out/verify/stage5/prompt_before/`` 里
    冻结的真实提示词比对；不一致就报错退出（不写文件）。

用法::

    python tools/_surgery_stage5_prompts.py --check     # 只看会改成什么（安全）
    python tools/_surgery_stage5_prompts.py --apply      # 真改
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
PROMPTS = PLATFORM / "prompts"
FRAMES = PLATFORM / "frames"


def lines_of(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").rstrip("\n").split("\n")


def yaml_block(text: str, indent: int) -> str:
    """把多行文本写成 YAML 字面块（``key: |`` 之后的内容，已加缩进）。"""
    pad = " " * indent
    return "\n".join(pad + ln if ln else "" for ln in text.split("\n"))


DIM_LINE = re.compile(r"^- (\w+)（(.+?)）(.*)$")


def varify_dim_line(line: str) -> str:
    """``- usage_scenarios（使用场景，category=scene）`` → 变量版。

    只替换 ``id（label，category=x`` 这一段，后面的专属说明（「——只描述**已购买**…」）
    原样保留 —— 每个维度的话不一样，那正是要留在框架文件里的东西。
    """
    m = DIM_LINE.match(line)
    if not m:
        raise SystemExit(f"维度行不符合预期，拒绝自动改写：{line!r}")
    dim_id, label_cat, rest = m.group(1), m.group(2), m.group(3)
    lm = re.match(r"^(.+?)，category=(\w+)(.*)$", label_cat)
    if not lm:
        raise SystemExit(f"维度行缺少 category=，拒绝自动改写：{line!r}")
    label, cat, tail = lm.group(1), lm.group(2), lm.group(3)
    return f"- ${{dim_id}}（${{dim_label}}，category=${{dim_category}}{tail}）{rest}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写文件")
    ap.add_argument("--check", action="store_true", help="只打印改法")
    args = ap.parse_args(argv)
    dry = not args.apply

    # ---------------------------------------------------------------- system
    sys_lines = lines_of(PROMPTS / "design_tags.system.zh.md")
    # 行号以 1 起；下面都在 0-based 上切
    keep_sys = sys_lines[:8]                     # 1–8 行：开头 + 5 个字段说明
    exp_note = sys_lines[8]                      # 9：体验类维度那句
    cat_note = sys_lines[10]                     # 11：category 取值那句
    persona_req = "\n".join(sys_lines[12:24])    # 13–24：画像维度特殊要求
    json_head = sys_lines[25]                    # 26：只输出合法 JSON，结构：
    json_shape = "\n".join(sys_lines[26:])       # 27–43：JSON 结构示例
    assert "体验类维度" in exp_note, exp_note[:40]
    assert "category" in cat_note and "5 类" in cat_note, cat_note[:60]
    assert "人群画像的两个维度" in persona_req, persona_req[:40]
    assert "只输出合法 JSON" in json_head, json_head[:40]
    assert json_shape.startswith("{"), json_shape[:40]

    exp_note_t = exp_note.replace(
        "体验类维度（购买后体验 / 功能体验 / 未满足需求）",
        "体验类维度（${experience_labels}）").replace(
        "分布类维度（人群画像 / 使用场景 / 购买前关注点）",
        "分布类维度（${dist_dim_labels}）")
    assert "${experience_labels}" in exp_note_t and "${dist_dim_labels}" in exp_note_t

    cat_note_t = cat_note.replace(
        "以下 5 类之一：", "以下 ${category_count} 类之一：").replace(
        "建议 3 个左右", "建议 ${sample_tags_per_dim} 个左右")
    cat_note_t = re.sub(r"取值严格为以下 \$\{category_count\} 类之一：(.*?)。",
                        r"取值严格为以下 ${category_count} 类之一：${category_legend}。",
                        cat_note_t)
    assert "${category_legend}" in cat_note_t, cat_note_t[:120]

    new_sys = "\n".join(keep_sys) + "\n\n" + "\n\n".join(
        ["${experience_note}", "${category_note}", "${persona_requirements}"]) + \
        "\n\n" + json_head + "\n${json_shape}"

    # ------------------------------------------------------------------ user
    usr_lines = lines_of(PROMPTS / "design_tags.user.zh.md")
    purpose = usr_lines[4]                       # 5：分析目的那句
    dim_head = usr_lines[6]                      # 7：## 维度框架（…）
    dim_rows = usr_lines[7:14]                   # 8–14：7 个维度行
    axis_note = "\n".join(usr_lines[15:18])      # 16–18：注意：… 段
    summary = usr_lines[22]                      # 23：请输出完整的标签集 JSON（7 个维度…）
    assert purpose.startswith("围绕「"), purpose[:30]
    assert dim_head.startswith("## 维度框架"), dim_head
    assert len(dim_rows) == 7, dim_rows
    assert axis_note.startswith("注意："), axis_note[:20]
    assert summary.startswith("请输出完整的标签集 JSON"), summary[:30]

    dim_rows_t = [varify_dim_line(r) for r in dim_rows]
    summary_t = summary.replace("（7 个维度都要有标签", "（${dim_count} 个维度都要有标签")
    assert "${dim_count}" in summary_t, summary_t

    axis_note_t = (axis_note
                   .replace("`purchase_status`（购买状态判定）",
                            "`${axis_dim}`（${axis_dim_label}）")
                   .replace("帖子（发帖人自述）", "${unit}（发帖人自述）")
                   .replace("评论（评论者发言）", "${secondary}（${secondary_speaker}发言）"))
    for must in ("${axis_dim}", "${axis_dim_label}", "${unit}", "${secondary}",
                 "${secondary_speaker}"):
        assert must in axis_note_t, f"{must} 未替换成功：{axis_note_t}"

    new_usr = ("## 品类\n${category}\n\n## 分析目的\n${purpose_line}\n\n"
               "${dim_block}\n\n${axis_note}\n\n"
               "## A+B 样本帖子（供归纳标签值参考，前 ${nsample} 条）\n"
               "${samples}\n${comment_block}\n${summary_line}")

    # --------------------------------------------------------------- annotate
    ann_lines = lines_of(PROMPTS / "annotate.system.zh.md")
    ann_keep = ann_lines[:8]                     # 1–8 行：开头 + 规则 1–5
    rule6 = "\n".join(ann_lines[8:11])           # 9–11：规则 6
    assert rule6.startswith("6. "), rule6[:20]
    assert "purchase_status" in rule6 and "ps_unknown" in rule6
    rule6_t = (rule6
               .replace("`purchase_status` 维度（ps_purchased / ps_non_purchased / ps_unknown）",
                        "`${axis_dim}` 维度（${bucket_tag_list}）")
               .replace("先判断发言者是否已购买该品类产品", "先判断发言者${axis_question}")
               .replace("才给 ps_unknown 标 yes", "才给 ${excluded_tag} 标 yes"))
    for must in ("${axis_dim}", "${bucket_tag_list}", "${axis_question}", "${excluded_tag}"):
        assert must in rule6_t, f"规则 6 的 {must} 未替换成功：{rule6_t}"
    new_ann = "\n".join(ann_keep) + "\n${annotate_axis_rule}\n${annotate_persona_rule}"

    # ------------------------------------------------------------------ report
    print("=" * 70)
    print("将写入：")
    print(f"  prompts/design_tags.system.zh.md   {len(new_sys)} 字符")
    print(f"  prompts/design_tags.user.zh.md     {len(new_usr)} 字符")
    print(f"  prompts/annotate.system.zh.md      {len(new_ann)} 字符")
    print(f"并将向 frames/consumer.yaml 注入 prompt 块 / prompt_line / persona_condition")
    print("=" * 70)
    if dry:
        print("\n--check 模式，不写文件。加 --apply 真正执行。")
        for nm, t in (("system", new_sys), ("user", new_usr), ("annotate", new_ann)):
            print(f"\n----- {nm} -----\n{t}")

    if dry:
        return 0

    # ------------------------------------------------- 往 consumer.yaml 注入
    # **先把所有断言跑完再写文件**：上一次 --apply 因为维度缩进写错（4 空格
    # vs 实际 2 空格）在写的半途炸掉，三个模板已经被覆盖了 —— 只能从
    # Stage 4 的 golden fixture 里恢复。写文件是最后一步，前面全是校验。
    yml = (FRAMES / "consumer.yaml").read_text(encoding="utf-8")

    # 1) 各维度加 prompt_line（按 generative 维度顺序对齐原文 7 行）
    for dim_id, row in zip(["user_personas_purchased", "user_personas_non_purchased",
                            "usage_scenarios", "pre_purchase_concerns",
                            "post_purchase_experience", "features", "unmet_needs"], dim_rows_t):
        pat = re.compile(rf"^(  - id: {dim_id}\n)", re.M)
        assert pat.search(yml), f"consumer.yaml 里找不到维度 {dim_id}"
        yml = pat.sub(lambda m: m.group(1) + f"    prompt_line: '{row}'\n", yml, count=1)

    # 2) 画像桶加 persona_condition（标注规则 7 那句「只在发言者**已购买**时…」）
    for bid, cond in (("purchased", "已购买"), ("non_purchased", "尚未购买")):
        pat = re.compile(rf"^(    - id: {bid}\n(?:.+\n)*?      persona_short: .+\n)", re.M)
        m = pat.search(yml)
        assert m, f"consumer.yaml 里找不到桶 {bid}"
        yml = pat.sub(lambda mm: mm.group(1) + f"      persona_condition: {cond}\n",
                      yml, count=1)

    # 3) axis 加 prompt_axis_question + prompt 块
    axis_prompt = (
        "  prompt_axis_question: 是否已购买该品类产品\n"
        "  prompt:\n"
        "    annotate_axis_rule: |\n"
        + yaml_block(rule6_t, 6) + "\n"
    )
    anchor = "  texts:\n"
    assert anchor in yml
    yml = yml.replace(anchor, axis_prompt + anchor, 1)

    # 4) frame 级 prompt 块 + category_legend
    top_prompt = (
        "category_legend:\n"
        "  user: 人群画像\n"
        "  scene: 使用场景\n"
        "  pre_purchase: 购买前关注点\n"
        "  post_purchase: 购买后体验\n"
        "  feature: 功能体验 / 未满足需求\n\n"
        "prompt:\n"
        "  purpose_line: '" + purpose + "'\n"
        "  dim_block_header: " + dim_head + "\n"
        f"  summary_line: '{summary_t}'\n"
        f"  experience_note: {exp_note_t}\n"
        f"  category_note: {cat_note_t}\n"
        "  persona_requirements: |\n"
        + yaml_block(persona_req, 4) + "\n"
        "  axis_note: |\n"
        + yaml_block(axis_note_t, 4) + "\n"
        "  json_shape: |\n"
        + yaml_block(json_shape, 4) + "\n\n"
    )
    anchor2 = "\nmodules:\n"
    assert anchor2 in yml
    yml = yml.replace(anchor2, "\n" + top_prompt + "modules:\n", 1)

    # 全部校验通过，才动盘
    (PROMPTS / "design_tags.system.zh.md").write_text(new_sys + "\n", encoding="utf-8")
    (PROMPTS / "design_tags.user.zh.md").write_text(new_usr + "\n", encoding="utf-8")
    (PROMPTS / "annotate.system.zh.md").write_text(new_ann + "\n", encoding="utf-8")
    (FRAMES / "consumer.yaml").write_text(yml, encoding="utf-8")
    print("\n已写入 4 个文件。下一步跑零回归自检。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
