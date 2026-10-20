#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wayfair 评论适配器。

Wayfair 与亚马逊同属"电商评论"，因此复用 ``EcommerceReviewAdapter`` 的
星级解析 / 已验证购买 / id 兜底逻辑，只覆盖字段别名与识别特征。

真实样本（已校准）
------------------
2026-10 拿到一份 Wayfair 官方导出（``0424电动 评论.xlsx``，sheet 名
``PartReviews``），实测列结构如下 —— 别名表已按它坐实：

==================================  ==================================
原始列名                            映射
==================================  ==================================
``Supplier part number``            ``product_id``（同时作为 ``thread_id``）
``Launch date``                     ``extra["Launch date"]``
``Product name``                    ``product_name``
``Class name``                      ``extra["Class name"]``（品类）
``Review date``                     ``created_at``
``Rating``                          ``rating``（1–5）
``Review text``                     ``text``（无标题列 → ``title`` 留空）
``Store name``                      ``channel``（恒为 ``Wayfair``）
``Annotations``                     ``extra["platform_annotations"]``
==================================  ==================================

两点值得记：

1. **没有评论 id 列**。``_derive_id`` 会对 (商品|星级|标题|正文) 做确定性哈希，
   因此重复导入结果稳定、去重有效。副作用是**文本与星级完全相同的两条评论会被
   合并**——在真实数据里概率极低，可接受。
2. **``Annotations`` 是 Wayfair 平台自己打的维度标签**（``Material - Comfort`` /
   ``Assembly - Difficulty`` / ``Time - Delay`` … 实测 21 种）。**但实测证明它是
   有偏采样，绝不能当"参考答案"**：

   =========  ==========  ==========
   星级        带平台标签   合计
   =========  ==========  ==========
   5 星        36 (64%)    56
   4 星        5           11
   3 星        1           7
   2 星        0           3
   1 星        **0**       **18**
   =========  ==========  ==========

   即平台标注**系统性地不覆盖差评**。该样本里 24 条提及"到货破损 / 断裂 /
   缺件"（占 25%），其中只有 2 条被平台标注 —— 而这正是本产品最大的负面来源。

   → 因此这里只把原值结构化存入 ``extra["platform_annotations"]`` 作为**原始留档**，
   **明确禁止**用它校准或评测本平台的维度框架（照它校准会把同一个盲区继承进产品）。
   本平台自建的维度必须自带"到货状态 / 破损"一类，且证据一律直接从原文采。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .base import EcommerceReviewAdapter
from ..io_utils import norm_key
from ..schema import SOURCE_WAYFAIR, Unit


class WayfairReviewsAdapter(EcommerceReviewAdapter):
    adapter_id = "wayfair_reviews"
    source = SOURCE_WAYFAIR
    label = "Wayfair 评论导出"
    produces = "unit_type=review（含星级；无星级时自动降级为无评分分析）"

    #: 独家特征列 —— 实测仅见于 Wayfair 官方 ``PartReviews`` 导出。
    #: 三个全中即 1.0，因此这类文件能被稳定识别，不依赖文件名。
    unique_columns = ("supplier_part_number", "annotations", "class_name")

    #: 通用电商列名 —— 其他平台也可能出现，故只给低权重，避免误吞亚马逊文件。
    common_columns = ("store_name", "launch_date")

    detect_tokens = ("wayfair", "way fair")

    #: 在基类别名表基础上，补 Wayfair 实测列名
    FIELD_ALIASES = {
        **EcommerceReviewAdapter.FIELD_ALIASES,
        "unit_id": EcommerceReviewAdapter.FIELD_ALIASES["unit_id"] + [
            "review_key", "review id",
        ],
        "text": EcommerceReviewAdapter.FIELD_ALIASES["text"] + [
            "review_body", "review_description", "customer_review", "comments",
        ],
        "title": EcommerceReviewAdapter.FIELD_ALIASES["title"] + [
            "review_headline", "subject",
        ],
        "rating": EcommerceReviewAdapter.FIELD_ALIASES["rating"] + [
            "star_rating_value", "review_score",
        ],
        # 实测：Wayfair 用「供应商料号」而非 ASIN 标识商品。
        # 缺了这几个别名，product_id 会为空 → thread_id 退化成每条评论一个
        # "讨论串"，聚合层的"商品分布"会完全失真（1 个商品被拆成 N 个）。
        "product_id": EcommerceReviewAdapter.FIELD_ALIASES["product_id"] + [
            "supplier_part_number", "supplier part number", "part_number", "part number",
            "supplier_sku", "manufacturer_part_number", "product_sku", "sku_id",
            "item_number",
        ],
        "author": EcommerceReviewAdapter.FIELD_ALIASES["author"] + [
            "reviewer_nickname", "nickname", "display_name", "customer_nickname",
        ],
        "created_at": EcommerceReviewAdapter.FIELD_ALIASES["created_at"] + [
            "submission_time", "submit_time", "date_submitted",
        ],
        "verified": EcommerceReviewAdapter.FIELD_ALIASES["verified"] + [
            "verified_owner", "is_verified_purchaser",
        ],
        "helpful_votes": EcommerceReviewAdapter.FIELD_ALIASES["helpful_votes"] + [
            "helpful_count", "was_this_helpful", "helpful_yes_count",
        ],
        # 实测：渠道信息在 ``Store name``（值为 "Wayfair"），不在 country/site，
        # 且官方导出**没有 URL 列**，因此 email/`product_url` 无从推导。
        "channel": EcommerceReviewAdapter.FIELD_ALIASES["channel"] + [
            "store_name", "store name", "store", "retailer", "sales_channel",
        ],
        "variant": EcommerceReviewAdapter.FIELD_ALIASES["variant"] + [
            "option", "selected_options", "finish_color",
        ],
    }

    # -- 识别 -------------------------------------------------------------

    def detect(self, path: Path) -> float:
        """识别这份文件是不是 Wayfair 导出。

        与基类的差别在于**不依赖文件名**：用户的导出常叫
        ``0424电动 评论.xlsx``，既无 "wayfair" 字样，官方列名也可能被工具改写成
        通用名（``Review Date`` / ``Rating`` / ``Review Text``）。
        因此加了一道**取值采样**：Wayfair 官方导出的 ``Store name`` 列值恒为
        ``Wayfair``，只要渠道列里出现该词就足以定性。
        """
        cols = self.sniff_columns(path)
        if not cols:
            return 0.0

        score = 0.0
        hit_unique = sum(1 for c in self.unique_columns if c in cols)
        if hit_unique:
            score += min(0.35 * hit_unique, 0.75)
        score += 0.15 * sum(1 for c in self.common_columns if c in cols)

        # 取值采样（形态识别）—— 列名被改写成通用名时唯一可靠的信号
        if score < 1.0:
            vals = self.sniff_values(path, self.FIELD_ALIASES["channel"], limit=200)
            if any("wayfair" in v for v in vals):
                score += 0.5

        name = self.filename_hint(path)
        if "wayfair" in name or "way fair" in name:
            score += 0.3

        # 通用电商形态（弱信号，与基类一致地压低，避免与亚马逊打成平手）
        if cols & {norm_key(a) for a in self.FIELD_ALIASES["text"]}:
            score += 0.1
        if cols & {norm_key(a) for a in self.FIELD_ALIASES["rating"]}:
            score += 0.1

        return min(score, 1.0)

    # -- 归一化 -----------------------------------------------------------

    def normalize(self, path: Path, **opts: Any) -> list[Unit]:
        rows = self.read_source(path)
        return self.normalize_rows(
            rows,
            source_note=str(opts.get("source_note", "")),
            default_brand=str(opts.get("default_brand", "")),
            # 源本身就是 Wayfair：即使导出没有 Store name 列，渠道也不该为空
            default_channel=str(opts.get("default_channel", "") or "Wayfair"),
        )

    def normalize_rows(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        source_note: str = "",
        default_brand: str = "",
        default_channel: str = "",
    ) -> list[Unit]:
        units = super().normalize_rows(
            rows,
            source_note=source_note,
            default_brand=default_brand,
            default_channel=default_channel,
        )
        for u in units:
            self._lift_platform_annotations(u)
        return units

    @staticmethod
    def _lift_platform_annotations(u: Unit) -> None:
        """把平台的 ``Annotations`` 列拆成结构化列表。

        原始值形如 ``"Material - Comfort, Quality/Price, Assembly - Difficulty"``。
        平台标注的维度名与本平台的维度定义不同构，因此**只作为先验留存**，
        不覆盖任何分析结果字段。
        """
        raw = ""
        for k in ("Annotations", "annotations"):
            v = u.extra.get(k)
            if v:
                raw = str(v)
                break
        if not raw:
            return
        parts = [p.strip() for p in raw.replace("；", ",").replace(";", ",").split(",")]
        u.extra["platform_annotations"] = [p for p in parts if p]

    def product_url(self, product_id: str) -> str:
        """Wayfair 商品链接含不可推导的 slug，因此不臆造 URL。

        实测官方导出**不带 URL 列**，拼一个错链接比没有链接更糟 ——
        留空，由看板降级为纯文本展示。
        """
        return ""
