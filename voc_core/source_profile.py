#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""源画像（SourceProfile）—— 内核去域名化的唯一注入点。

设计原则
--------
内核（6,900 行）里有 219 处 ``reddit``、101 处 ``subreddit``。**逐处改写成
抽象概念是高风险低收益的伪重构**。真正的域名泄漏只有两个边界：

1. **数据边界**（列名）：``reddit_id`` / ``subreddit`` / ``summary_text`` …
2. **呈现边界**（用户可见文案）：``子版块分布`` / ``数据来源: Reddit RSS`` …

本模块负责第 2 个边界：把每个源**该怎么说人话**集中成一份画像，内核通过
``term("key")`` 取值。加一个新源 = 加一个 Profile，内核代码零改动。

默认值即现状
------------
``REDDIT`` 画像的每个字段都**逐字等于内核里现有的硬编码文案**。因此未传
``--profile`` 时行为与改造前完全一致（这是回归测试 ``test_kernel_regression``
的验收标准）。

用法
----
::

    from voc_core.source_profile import get_profile
    p = get_profile("wayfair")
    p.term("channel_noun")        # -> "站点"
    p.term("channel_section")     # -> "站点分布"
    p.term("dataset_label")       # -> "Wayfair PartReviews 导出"
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, ClassVar

__all__ = [
    "SourceProfile",
    "REDDIT",
    "AMAZON",
    "WAYFAIR",
    "GENERIC",
    "BUILTIN_PROFILES",
    "get_profile",
]


@dataclass
class SourceProfile:
    """一个数据源的「词汇表 + 能力表」。

    字段分两类：

    * **术语**（``*_noun`` / ``*_label``）：写进看板与报告的**用户可见**字样。
      渠道名（Reddit 的「子版块」、Wayfair 的「站点」）与计数单元名
      （Reddit 的「帖子」、电商的「评论」）差异最大，是最容易露馅的地方。
    * **能力**（``has_rating`` / ``has_threads`` / ``has_author`` …）：决定
      哪些模块该出现。Reddit 没有星级 → 星级模块自动隐藏；电商没有
      「发帖人 vs 评论者」之分 → 相应文案降级。

    约定：任何字段为空字符串时，``term()`` 会回落到 ``REDDIT`` 的同名字段，
    保证「只覆盖差异、其余继承现状」。
    """

    # -- 标识 --------------------------------------------------------------
    source: str = "reddit"
    label: str = "Reddit"

    # -- 渠道（Reddit=子版块 / 电商=站点或商品） ----------------------------
    #: 单数渠道名，如「子版块」「站点」「商品」
    channel_noun: str = "子版块"
    #: 渠道分布卡片标题，如「子版块分布」
    channel_section: str = "子版块分布"
    #: 筛选下拉里的渠道项，如「子版块: 全部」
    channel_filter: str = "子版块"
    #: 排序项，如「排序: 子版块」
    channel_sort: str = "子版块"
    #: 表格/图表列头，如「子版块」
    channel_column: str = "子版块"
    #: 渠道在 **DOM id / 排序键** 里的英文单数形（Reddit=subreddit，其他=channel）
    channel_slug: str = "subreddit"
    #: 同上，复数形（用于 ``chart-<slug_plural>-small``）
    channel_slug_plural: str = "subreddits"

    # -- 计数单元（Reddit=帖子 / 电商=评论） --------------------------------
    #: 主计数单元名（看板里「帖子时间分布」的「帖子」）
    unit_noun: str = "帖子"
    #: 报告表头「提及 X 数」里的短名（Reddit=帖 / 电商=评论）
    unit_char: str = "帖"
    #: 报告里「N X」的量词短语（Reddit=帖 / 电商=条评论）
    unit_quant: str = "帖"
    #: 主计数单元的量词短语，如「提及帖子数」「提及评论数」
    unit_count_phrase: str = "提及帖子数"
    #: 讨论帖子数
    unit_discuss_phrase: str = "讨论帖子数"
    #: 次级单元（Reddit 的评论；电商没有，留空则该概念消失）
    secondary_noun: str = "评论"
    #: 次级单元的发言者（如「评论者」）
    secondary_speaker: str = "评论者"
    #: 「帖子 + 评论」这类合并计数的描述短语
    merged_count_phrase: str = "帖子 + 评论"

    # -- 讨论串（Reddit=原帖 / 电商=商品） ----------------------------------
    #: 讨论串名，如「原帖」「商品」
    thread_noun: str = "原帖"
    #: 讨论串页面切分说明，如「以标题与摘要为主体的原始帖子信息流」
    feed_title: str = "以标题与摘要为主体的原始帖子信息流"

    # -- 数据来源与出处 ----------------------------------------------------
    #: 「数据来源: Reddit RSS」里的来源名
    dataset_label: str = "Reddit RSS"
    #: 页脚生成者短语（含动作，模板里直接跟时间戳：
    #: ``{generator_label} | <时间> | 数据来源: {dataset_label}``）
    generator_label: str = "Reddit 分析技能自动生成"
    #: 报告里「代表性 Reddit 来源链接」
    link_section: str = "代表性 Reddit 来源链接"
    #: 报告数据来源句，如「Reddit（经采集与清洗后的 A+B 级帖子）」
    data_source_phrase: str = "Reddit"
    #: 采集量口径说明，如「Reddit 采集去重后的帖子总数」
    raw_total_note: str = "Reddit 采集去重后的帖子总数"
    #: 「数据来源」行在未显式传 ``--source-note`` 时的默认说明
    collection_note: str = "Reddit 登录态 RSS 搜索采样"
    #: 数据局限性条目（每条渲染成一行 ``- …``）；空列表 = 该段整段不出现
    limitation_notes: list[str] = field(
        default_factory=lambda: [
            "Reddit RSS 为采样数据，非穷举",
            "帖子级数据不包含完整评论树",
        ]
    )

    # -- 采样与噪声说明（数据说明页） --------------------------------------
    #: 采样局限句
    sampling_note: str = "Reddit RSS 为采样数据，非穷举。实际帖子数量可能更多。"
    #: 推广类渠道说明（电商源一般无此概念）
    promo_channel_note: str = (
        "同时过滤来自 gamerdeals、LaptopDealsUS、AppGiveaway 等交易/推广类 Subreddit 的帖子。"
    )
    #: 「在《原帖》下的发言」里的讨论串名（不含「在」，由模板补）
    thread_ref_phrase: str = "原帖"
    #: 「数据局限性」最后一条：相关性与噪声的口径说明
    #: （Reddit 是搜索采样 →「搜索结果含噪声」；电商是静态导出，无「搜索」一说）
    relevance_caveat: str = "搜索结果含噪声，定量分析仅基于 A/B 相关候选"
    #: 渠道名前缀：Reddit 的 ``r/`` 是专属写法，其他源留空
    channel_prefix: str = "r/"

    # -- 能力开关 ----------------------------------------------------------
    #: 是否有客观星级（电商有，Reddit 无）→ 决定星级模块是否启用
    has_rating: bool = False
    #: 是否有「发帖人 + 评论者」双层结构（电商是单层评论）
    has_threads: bool = True
    #: 是否有作者字段
    has_author: bool = True
    #: 平台自身是否已对商品分渠道（Reddit 的 subreddit / 电商的 marketplace）
    has_channel: bool = True

    # -- 自由扩展 ----------------------------------------------------------
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    def term(self, key: str, default: str | None = None) -> str:
        """取术语。空值回落到 REDDIT 同名字段，再回落到 ``default``。

        ⚠️ 不要用它读「可空术语」（``secondary_noun`` / ``promo_channel_note`` /
        ``channel_prefix``）—— 对它们来说**空串是有意义的值**（表示该源没有这个
        概念），回落会让电商源凭空多出「评论」这一层。请改用 :meth:`opt`。
        """
        val = getattr(self, key, None)
        if isinstance(val, str) and val.strip():
            return val
        fallback = getattr(REDDIT, key, None)
        if isinstance(fallback, str) and fallback.strip():
            return fallback
        return default if default is not None else ""

    #: 允许为空的术语——空串表示「该源不存在此概念」，取值**不得回落**。
    #: 必须标 ClassVar：否则会被当成 dataclass 字段，混进 to_dict()/asdict()
    #: 并让 json 序列化炸掉（frozenset 不可序列化）。
    OPTIONAL_TERMS: ClassVar[frozenset[str]] = frozenset(
        {"secondary_noun", "promo_channel_note", "channel_prefix"}
    )

    def opt(self, key: str) -> str:
        """取**可空**术语：空值不回落到 REDDIT（空即「无此概念」）。"""
        val = getattr(self, key, "")
        return val.strip() if isinstance(val, str) else ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return p

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceProfile":
        """从 dict 构造；未知键进 ``extra``（向前兼容）。"""
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known and k != "extra"}
        # 旧导出（在 slug 字段加入之前生成的 json）没有 channel_slug —— 若非 Reddit 源，
        # 必须落到中性 id，否则看板仍会漏出 `chart-subreddits-small`。
        if "channel_slug" not in kwargs and str(kwargs.get("source", "")).lower() != "reddit":
            kwargs["channel_slug"] = "channel"
            kwargs["channel_slug_plural"] = "channels"
        extra = dict(data.get("extra") or {})
        extra.update({k: v for k, v in data.items() if k not in known})
        kwargs["extra"] = extra
        return cls(**kwargs)


# --------------------------------------------------------------------------
# 内置画像
# --------------------------------------------------------------------------
# 注：REDDIT 的每个字段都与内核现有硬编码文案**逐字一致**，是兼容基线。

REDDIT = SourceProfile(
    source="reddit",
    label="Reddit",
    channel_noun="子版块",
    channel_section="子版块分布",
    channel_filter="子版块",
    channel_sort="子版块",
    channel_column="子版块",
    channel_slug="subreddit",
    channel_slug_plural="subreddits",
    unit_noun="帖子",
    unit_char="帖",
    unit_quant="帖",
    unit_count_phrase="提及帖子数",
    unit_discuss_phrase="讨论帖子数",
    secondary_noun="评论",
    secondary_speaker="评论者",
    merged_count_phrase="帖子 + 评论",
    thread_noun="原帖",
    feed_title="以标题与摘要为主体的原始帖子信息流",
    dataset_label="Reddit RSS",
    generator_label="Reddit 分析技能自动生成",
    link_section="代表性 Reddit 来源链接",
    data_source_phrase="Reddit",
    raw_total_note="Reddit 采集去重后的帖子总数",
    sampling_note="Reddit RSS 为采样数据，非穷举。实际帖子数量可能更多。",
    promo_channel_note=(
        "同时过滤来自 gamerdeals、LaptopDealsUS、AppGiveaway 等交易/推广类 Subreddit 的帖子。"
    ),
    thread_ref_phrase="原帖",
    relevance_caveat="搜索结果含噪声，定量分析仅基于 A/B 相关候选",
    channel_prefix="r/",
    has_rating=False,
    has_threads=True,
)

AMAZON = SourceProfile(
    source="amazon",
    label="亚马逊",
    channel_noun="站点",
    channel_section="站点分布",
    channel_filter="站点",
    channel_sort="站点",
    channel_column="站点",
    channel_slug="channel",
    channel_slug_plural="channels",
    unit_noun="评论",
    unit_char="评论",
    unit_quant="条评论",
    unit_count_phrase="提及评论数",
    unit_discuss_phrase="讨论评论数",
    secondary_noun="",  # 电商无「评论的评论」这层
    secondary_speaker="用户",
    merged_count_phrase="评论",
    thread_noun="商品",
    feed_title="以标题与正文为主体的原始评论信息流",
    dataset_label="亚马逊评论导出",
    generator_label="VOC 洞察平台自动生成",
    link_section="代表性评论来源链接",
    data_source_phrase="亚马逊",
    raw_total_note="亚马逊评论去重后的总数",
    collection_note="卖家后台／第三方工具导出的评论数据",
    limitation_notes=[
        "评论数据来自卖家后台／第三方工具导出，非平台全量",
        "评论为单品维度，不含品类整体分布",
    ],
    sampling_note="评论数据来自卖家后台／第三方工具导出，非平台全量。",
    promo_channel_note="",  # 电商无 subreddit 交易版块概念 → 置空并由 T_PROMO_HTML 整段隐藏
    thread_ref_phrase="商品",
    relevance_caveat="导出内容含低相关样本，定量分析仅基于 A/B 相关候选",
    channel_prefix="",
    has_rating=True,
    has_threads=False,
    has_channel=False,  # 亚马逊导出常无 marketplace 列，站点靠国家列推导
)

WAYFAIR = SourceProfile(
    source="wayfair",
    label="Wayfair",
    channel_noun="站点",
    channel_section="站点分布",
    channel_filter="站点",
    channel_sort="站点",
    channel_column="站点",
    channel_slug="channel",
    channel_slug_plural="channels",
    unit_noun="评论",
    unit_char="评论",
    unit_quant="条评论",
    unit_count_phrase="提及评论数",
    unit_discuss_phrase="讨论评论数",
    secondary_noun="",
    secondary_speaker="用户",
    merged_count_phrase="评论",
    thread_noun="料号",
    feed_title="以正文为主体的原始评论信息流",  # PartReviews 导出无评论标题列
    dataset_label="Wayfair PartReviews 导出",
    generator_label="VOC 洞察平台自动生成",
    link_section="代表性评论来源链接",
    data_source_phrase="Wayfair",
    raw_total_note="Wayfair 评论去重后的总数",
    collection_note="Wayfair PartReviews 导出（按料号）",
    limitation_notes=[
        "PartReviews 导出为单品维度（Supplier part number），非平台全量",
        "导出无评论标题列，正文即用户原话",
    ],
    sampling_note="评论导出为单品维度（Supplier part number），非平台全量。",
    promo_channel_note="",
    thread_ref_phrase="料号",
    relevance_caveat="导出内容含低相关样本，定量分析仅基于 A/B 相关候选",
    channel_prefix="",
    has_rating=True,
    has_threads=False,
    has_channel=True,  # PartReviews 导出含 Store name 列，恒为 Wayfair
)

#: 兜底画像：不确定源时用它，术语最中性
GENERIC = SourceProfile(
    source="generic",
    label="VOC 数据",
    channel_noun="渠道",
    channel_section="渠道分布",
    channel_filter="渠道",
    channel_sort="渠道",
    channel_column="渠道",
    channel_slug="channel",
    channel_slug_plural="channels",
    unit_noun="条目",
    unit_char="条",
    unit_quant="条",
    unit_count_phrase="提及条目数",
    unit_discuss_phrase="讨论条目数",
    secondary_noun="",
    secondary_speaker="用户",
    merged_count_phrase="条目",
    thread_noun="主题",
    feed_title="原始数据信息流",
    dataset_label="VOC 导出",
    generator_label="VOC 洞察平台自动生成",
    link_section="代表性来源链接",
    data_source_phrase="导入数据",
    raw_total_note="导入去重后的条目总数",
    collection_note="用户导入的已有 VOC 数据",
    limitation_notes=["数据来自导入文件，非平台全量"],
    sampling_note="数据来自导出文件，非平台全量。",
    promo_channel_note="",
    thread_ref_phrase="主题",
    relevance_caveat="数据含低相关样本，定量分析仅基于 A/B 相关候选",
    channel_prefix="",
    has_rating=False,
    has_threads=False,
    has_channel=False,
)

BUILTIN_PROFILES: dict[str, SourceProfile] = {
    p.source: p for p in (REDDIT, AMAZON, WAYFAIR, GENERIC)
}


def get_profile(spec: str | Path | dict | None) -> SourceProfile:
    """按源名 / JSON 文件路径 / dict 取画像。

    :param spec:
        - ``None`` 或 ``""`` → 返回 ``REDDIT``（保证旧调用行为不变）
        - 内置名 ``reddit`` / ``amazon`` / ``wayfair`` / ``generic``
        - ``.json`` 文件路径（由 ``voc.py`` 从适配器源名生成）
        - 已解析的 dict
    """
    if spec is None or (isinstance(spec, str) and not spec.strip()):
        return REDDIT
    if isinstance(spec, dict):
        return SourceProfile.from_dict(spec)
    s = str(spec).strip()
    key = s.lower()
    if key in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[key]
    p = Path(s)
    if p.suffix.lower() == ".json" and p.exists():
        return SourceProfile.from_dict(json.loads(p.read_text(encoding="utf-8")))
    # 未知源名：派生一个「继承 Reddit 词汇但改标识」的画像，而不是报错 ——
    # 内核必须永远能跑出东西（宁可文案不完美，不可中断分析）。
    # 但 DOM id 用**中性形**：继承 Reddit 文案是兼容考虑，漏出 `chart-subreddits` 不是。
    return SourceProfile(source=key or "generic", label=s or "VOC 数据",
                         channel_slug="channel", channel_slug_plural="channels")
