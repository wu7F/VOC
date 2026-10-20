#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内置**分段轴分类器**定义（Stage 5）。

分段轴（segmentation axis）指的是「把一个单元判进哪个桶」的那个分类器维度。
消费品品类里它是**购买状态**（已购 / 未购 / 无法判断）；B2B 可以是**决策角色**；
订阅制产品可以是**付费档位**。轴本身可配，但**判定口径必须固定**——

为什么不把口径交给 LLM 自由生成：它是「归桶闸门」。每个单元先被判进某个桶，
聚合时只把该单元计入对应的人群画像桶，保证两桶严格互斥。口径一旦漂移
（这次把「刚下单还没收到」判成未购、下次判成已购），跨品类的份额数字就不再可比，
而且**不会有任何报错**——只是数字悄悄错了。

因此：标签键与五字段判定指令在这里硬编码，由 ``frames/*.yaml`` 按名字引用
（``segmentation_axis.classifier: purchase_status``）；LLM 只负责生成画像维度下面的
**标签值**（「价格敏感型」「空间受限型」…），不参与闸门口径。

要加一个新轴：在 ``CLASSIFIERS`` 里加一个条目，然后在 frame 里引用它。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["ClassifierDef", "CLASSIFIERS", "get_classifier", "classifier_names"]


@dataclass(frozen=True)
class ClassifierDef:
    """一个分类器：维度键 + 标签定义（5 字段结构，与生成型维度同构）。"""

    name: str
    dim_label: str
    #: 标签键 -> 五字段定义。字段沿用 annotate 阶段的通用契约
    #: （``thoughts`` / ``mentioned`` / ``original_text`` / ``<tk>_summary`` / ``sentiment``）
    tags: dict = field(default_factory=dict)
    #: 桶的展示顺序（在分布图与文案里的排列）
    bucket_order: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# purchase_status —— 消费品：按购买状态分桶
# --------------------------------------------------------------------------
# 判定口径的写法要点（照抄即可，别改）：
#  * thoughts 里给**可被原文验证的信号词**（'I bought'、'should I buy'），不要给抽象描述；
#  * 第三条（无法判断）必须写明与前两条**互斥**，否则模型会同时标 yes →
#    聚合层「恰好一个命中才生效」的闸门把该单元判成 unknown，样本白白损失；
#  * sentiment 统一 'n/a'：这是分类不是评价，情感维度在别处统计。
PURCHASE_STATUS_TAGS = {
    "ps_purchased": {
        "label_cn": "已购买者",
        "category": "user",
        "thoughts": (
            "判定该发言者**是否已经购买 / 已经拥有 / 正在使用**该品类产品。信号："
            "'I bought/got/ordered'、'my <产品>'、'mine'、'we have had it for ...'、"
            "'switched from ... to ...'、描述实际使用体验或故障、晒单 / 退货 / 售后经历。"
            "仅在明确能读出「已持有」时填 yes。"
        ),
        "mentioned": "Output 'yes' or 'no'. If 'no', leave all other fields blank.",
        "original_text": "Extract the exact phrase(s) that reveal the speaker already owns / bought the product.",
        "ps_purchased_summary": "发言者自述已购买/已拥有的具体线索，如 'my X' || 'bought it last month'；用 '||' 分隔",
        "sentiment": "n/a",
    },
    "ps_non_purchased": {
        "label_cn": "未购买者",
        "category": "user",
        "thoughts": (
            "判定该发言者**尚未购买、正在选购 / 比较 / 观望 / 求助推荐**。信号："
            "'should I buy'、'looking for'、'which one'、'any recommendations'、"
            "'is it worth it'、'thinking about getting'、'vs'、'budget'、'first time buyer'、"
            "'about to pull the trigger'。仅在明确能读出「还没买」时填 yes。"
        ),
        "mentioned": "Output 'yes' or 'no'. If 'no', leave all other fields blank.",
        "original_text": "Extract the exact phrase(s) that reveal the speaker has not bought the product yet.",
        "ps_non_purchased_summary": "发言者自述未购买/正在选购的具体线索，如 'should I buy' || 'looking for a X'；用 '||' 分隔",
        "sentiment": "n/a",
    },
    "ps_unknown": {
        "label_cn": "无法判断",
        "category": "user",
        "thoughts": (
            "文本过短、纯情绪附和（'this'、'following'）、或完全读不出持有状态时使用。"
            "与 ps_purchased / ps_non_purchased 互斥：能判出前两者之一时本标签必须为 no。"
        ),
        "mentioned": "Output 'yes' or 'no'. If 'no', leave all other fields blank.",
        "original_text": "Leave blank.",
        "ps_unknown_summary": "留空",
        "sentiment": "n/a",
    },
}

# --------------------------------------------------------------------------
# decision_role —— B2B / 组织采购：按决策角色分桶
# --------------------------------------------------------------------------
# 同一个「分段轴」抽象换个轴：这里问的不是「买了没」，而是
# 「发言者在这笔采购里是什么角色」。闸门逻辑一字不改
# （恰好一个标签命中才生效、命中 0 或 ≥2 判 unknown）。
#
# 注意第三个标签的语义差异：``dr_unclear`` 对应 purchase_status 里的
# ps_unknown，但**不叫** unknown —— 「说不清角色」和「说不清买没买」是两回事，
# 桶名要让人看懂分析结论里那句「另有 N 个单元无法判定…」到底在说什么。
DECISION_ROLE_TAGS = {
    "dr_decision_maker": {
        "label_cn": "决策者",
        "category": "user",
        "thoughts": (
            "判定该发言者**是否有权拍板 / 批准这笔采购**。信号："
            "'we decided to'、'I signed off'、'our team chose'、'I approved the budget'、"
            "'I'm the one who picks'、'we went with'、'I run this department'。"
            "仅在明确能读出「这个人说了算」时填 yes。"
        ),
        "mentioned": "Output 'yes' or 'no'. If 'no', leave all other fields blank.",
        "original_text": "Extract the exact phrase(s) that reveal the speaker holds decision authority.",
        "dr_decision_maker_summary": "发言者自述有决策权的具体线索，如 'I signed off on it' || 'we went with X'；用 '||' 分隔",
        "sentiment": "n/a",
    },
    "dr_influencer": {
        "label_cn": "影响者",
        "category": "user",
        "thoughts": (
            "判定该发言者**能影响选型但无权拍板**。信号："
            "'I recommended X to my boss'、'we evaluated'、'I put together the shortlist'、"
            "'IT wanted'、'my team prefers'、'I'd push for'。"
            "仅在明确能读出「有话语权但不是最终决定人」时填 yes。"
        ),
        "mentioned": "Output 'yes' or 'no'. If 'no', leave all other fields blank.",
        "original_text": "Extract the exact phrase(s) that reveal the speaker influences but does not own the decision.",
        "dr_influencer_summary": "发言者自述有建议权/评估权的具体线索，如 'I recommended' || 'we evaluated'；用 '||' 分隔",
        "sentiment": "n/a",
    },
    "dr_end_user": {
        "label_cn": "使用者",
        "category": "user",
        "thoughts": (
            "判定该发言者**是日常使用这套产品/服务的一线人员**，不参与选型决策。信号："
            "'we use it daily'、'as an operator'、'my daily driver at work'、"
            "'the team on the floor says'、抱怨实际操作体验。"
            "仅在明确能读出「是使用者而非采购方」时填 yes。"
        ),
        "mentioned": "Output 'yes' or 'no'. If 'no', leave all other fields blank.",
        "original_text": "Extract the exact phrase(s) that reveal the speaker uses the product day to day.",
        "dr_end_user_summary": "发言者自述为一线使用者的具体线索，如 'we use it daily' || 'as an operator'；用 '||' 分隔",
        "sentiment": "n/a",
    },
    "dr_unclear": {
        "label_cn": "角色不明",
        "category": "user",
        "thoughts": (
            "文本过短、纯情绪附和（'this'、'following'）、或完全读不出组织角色时使用。"
            "与 dr_decision_maker / dr_influencer / dr_end_user 互斥："
            "能判出前三者之一时本标签必须为 no。"
        ),
        "mentioned": "Output 'yes' or 'no'. If 'no', leave all other fields blank.",
        "original_text": "Leave blank.",
        "dr_unclear_summary": "留空",
        "sentiment": "n/a",
    },
}


CLASSIFIERS: dict[str, ClassifierDef] = {
    "purchase_status": ClassifierDef(
        name="purchase_status",
        dim_label="购买状态判定",
        tags=PURCHASE_STATUS_TAGS,
        bucket_order=("purchased", "non_purchased", "unknown"),
    ),
    "decision_role": ClassifierDef(
        name="decision_role",
        dim_label="决策角色判定",
        tags=DECISION_ROLE_TAGS,
        bucket_order=("decision_maker", "influencer", "end_user", "unclear"),
    ),
}


def classifier_names() -> list[str]:
    return sorted(CLASSIFIERS)


def get_classifier(name: str) -> ClassifierDef:
    """按名字取内置分类器。找不到时**列出可选项**再报错 —— 手打名字最容易错在拼写上。"""
    key = (name or "").strip()
    if key not in CLASSIFIERS:
        raise KeyError(
            f"未知的分类器 {name!r}；可用的有：{'、'.join(classifier_names())}。"
            f"（frames/*.yaml 的 segmentation_axis.classifier 引用这里的内置定义；"
            f"要新增轴就在 voc_core/classifiers.py 里加一个条目）")
    return CLASSIFIERS[key]
