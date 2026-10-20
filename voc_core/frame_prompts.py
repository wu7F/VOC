#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把分析框架渲染成**提示词片段**（Stage 5）。

``design_tags`` 与 ``annotate_posts`` 的提示词里，原本硬编码着「哪 8 个维度、
按购买状态归桶、不要输出 purchase_status」这类内容 —— 那正是「分析什么」。
框架一换（例如 B2B 按决策角色分桶），这些句子就全错了，而且**错得很安静**：
模型照样输出合法 JSON，只是每一维都在回答错误的问题。

所以这一段的口径是：**提示词里凡是跟框架有关的部分，都由 frame 渲染**。

设计取舍：**长段落整段交给 YAML，而不是拆成十几个碎片变量**。
像 ``persona_requirements``（画像维度的特殊要求）这种段落，句式与维度键交织在
一起，拆碎后每加一个桶都得改三处模板，且极容易在拼接处多/少一个换行或空格
——而提示词错一个空格是**没有任何测试能发现**的那类错误（Stage 4 的教训）。
整段放在框架文件里，改动点集中、可逐字核对，代价是新框架要自己写一遍这段话。
"""

from __future__ import annotations

from voc_core.analysis_frame import AnalysisFrame

__all__ = ["prompt_parts", "annotate_rules"]


def _ordered_categories(frame: AnalysisFrame) -> list[tuple[str, str]]:
    """按维度声明顺序返回 ``[(category, 说明)]``（去重）。

    顺序有意义：提示词里 ``user（人群画像）/ scene（使用场景）/ …``
    这一串必须与原来一致，而「原来」就是维度声明顺序。
    """
    legend = frame.category_legend
    seen: list[str] = []
    for d in frame.active_dims:
        if d.category and d.category not in seen:
            seen.append(d.category)
    return [(c, legend.get(c, c)) for c in seen]


def _experience_labels(frame: AnalysisFrame) -> str:
    return " / ".join(d.label for d in frame.active_dims if d.experience)


def _distribution_labels(frame: AnalysisFrame) -> str:
    """分布类维度（不需要 reason 的那些）的组名，如「人群画像 / 使用场景 / 购买前关注点」。

    判定规则：某个 category 下**一个体验类维度都没有** → 它整体算分布类。
    """
    exp_cats = {d.category for d in frame.active_dims if d.experience}
    return " / ".join(desc for cat, desc in _ordered_categories(frame)
                      if cat not in exp_cats)


def _persona_rules(frame: AnalysisFrame) -> str:
    """标注规则里的画像分桶条件（每桶一行，第 2 行起缩进 3 空格）。

    标点是**语义**：中间几行用 ``；`` 收尾，最后一行用 ``。`` —— 后面要么接
    「旧维度」提醒（同一条规则里），要么就是这条规则的结束。全写成 ``；`` 会让
    提示词读起来是断句，而且与改造前不一致（提示词改动 = 模型输出改动）。
    """
    items = list(frame.persona_dims().items())
    lines: list[str] = []
    for i, (did, bid) in enumerate(items):
        b = frame.axis.bucket(bid) if frame.axis else None
        cond = (b.persona_condition if b else "") or "符合该桶条件"
        prefix = "7. " if i == 0 else "   "
        end = "。" if i == len(items) - 1 else "；"
        lines.append(f"{prefix}`{did}` 下的标签只在发言者**{cond}**时才可能标 yes{end}")
    return "\n".join(lines)


def _deprecated_persona_note(frame: AnalysisFrame) -> str:
    """旧画像维度的提醒（没有废弃维度时整句消失）。

    整句由框架配置（``segmentation_axis.prompt.deprecated_persona_note``）——
    它在说「读不出**是否购买**」，那是**轴的口径**，不是维度的通用属性：
    B2B 框架里这句话该说「读不出决策角色」。代码只负责补缩进（它要接在规则 7
    后面，第 2 行起缩进 3 空格）。
    """
    axis = frame.axis
    tpl = (axis.prompt.get("deprecated_persona_note") if axis else "") or ""
    dep = sorted(frame.deprecated_dims())
    if not tpl or not dep:
        return ""
    rendered = frame.render_prompt(tpl, deprecated_dims="、".join(f"`{d}`" for d in dep))
    return "\n" + "\n".join("   " + ln for ln in rendered.splitlines())


def prompt_parts(frame: AnalysisFrame, **ctx) -> dict:
    """渲染出全部提示词片段。

    ``ctx`` 需要调用方提供：``category``（品类名）、``unit``、``secondary``、
    ``secondary_speaker``（后三个来自源画像）。
    """
    axis = frame.axis
    ctx = dict(ctx)
    ctx.update({
        "dim_count": len(frame.generative_dims()),
        "sample_tags_per_dim": frame.sample_tags_per_dim,
        # ``summary_line`` 用的是简洁变量名 ``${n}``（与 design_tags.user 的旧写法一致）
        "n": frame.sample_tags_per_dim,
        "experience_labels": _experience_labels(frame),
        "dist_dim_labels": _distribution_labels(frame),
        "category_count": len(_ordered_categories(frame)),
        "category_legend": "/ ".join(f"{c}（{desc}）" for c, desc in _ordered_categories(frame)),
        "axis_dim": axis.dim if axis else "",
        "axis": axis.label if axis else "",
        "axis_dim_label": (frame.dim(axis.dim).label if (axis and frame.dim(axis.dim)) else ""),
        "bucket_tag_list": (" / ".join(t for b in axis.buckets for t in b.tags)
                            if axis else ""),
        "excluded_tag": (axis.excluded_bucket.tags[0]
                         if (axis and axis.excluded_bucket and axis.excluded_bucket.tags) else ""),
        "persona_rules": _persona_rules(frame),
        "deprecated_persona_note": _deprecated_persona_note(frame),
        "unit": ctx.get("unit", ""),
        "secondary": ctx.get("secondary", ""),
        "secondary_speaker": ctx.get("secondary_speaker", ""),
    })

    # 维度行：逐维度用各自的 prompt_line 渲染（YAML 里按维度配，含专属说明）
    lines = []
    for d in frame.generative_dims():
        if not d.prompt_line:
            continue
        lines.append(frame.render_prompt(d.prompt_line, dim_id=d.id, dim_label=d.label,
                                        dim_category=d.category, **ctx))
    dim_block = "\n".join(
        [x for x in (frame.prompt.get("dim_block_header"), "\n".join(lines)) if x])

    out = dict(frame.prompt)
    out["dim_block"] = dim_block
    for key in ("purpose_line", "axis_note", "summary_line", "experience_note",
                "category_note", "persona_requirements", "json_shape"):
        if key in frame.prompt:
            out[key] = frame.render_prompt(frame.prompt[key], **ctx)
    return out


def annotate_rules(frame: AnalysisFrame, **ctx) -> dict:
    """``annotate.system`` 里规则 6 / 7 的文本（跟轴与画像桶绑定）。

    规则 6 由框架配置（句式固定、只换了轴名与标签键）；
    规则 7 **由代码生成** —— 它的行数等于画像桶数，桶从 2 个变 3 个时
    句子结构就变了，写死模板反而更绕。
    """
    axis = frame.axis
    if axis is None:
        return {"annotate_axis_rule": "", "annotate_persona_rule": ""}
    base = {
        "axis_dim": axis.dim, "axis": axis.label,
        "axis_question": axis.prompt_axis_question,
        "bucket_tag_list": " / ".join(t for b in axis.buckets for t in b.tags),
        "excluded_tag": (axis.excluded_bucket.tags[0]
                         if (axis.excluded_bucket and axis.excluded_bucket.tags) else ""),
    }
    base.update(ctx)
    tpl = axis.prompt.get("annotate_axis_rule") or ""
    return {
        "annotate_axis_rule": frame.render_prompt(tpl, **base) if tpl else "",
        "annotate_persona_rule": _persona_rules(frame) + _deprecated_persona_note(frame),
    }
