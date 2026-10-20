#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""亚马逊评论适配器。

迁移自 ``amazon-voc-analysis/scripts/ingest_reviews.py``，沿用那份别名表
（已覆盖卖家精灵 / Helium 10 / JungleScout / 亚马逊后台 / 官方评论导出）。
星级解析、已验证购买判定、review_id 确定性哈希兜底都在
``EcommerceReviewAdapter`` 里共用。
"""

from __future__ import annotations

from .base import EcommerceReviewAdapter
from ..schema import SOURCE_AMAZON


class AmazonReviewsAdapter(EcommerceReviewAdapter):
    adapter_id = "amazon_reviews"
    source = SOURCE_AMAZON
    label = "亚马逊评论导出"
    produces = "unit_type=review（含 1-5 星、已验证购买标记）"

    #: 出现即强烈指示亚马逊
    signature_columns = ("asin", "asin1", "parent_asin", "helpful_votes",
                         "verified_purchase", "product_reviews")

    detect_tokens = ("amazon", "亚马逊", "卖家精灵", "sellersprite", "seller sprite",
                     "helium", "junglescout", "jungle scout", "asin")

    def product_url(self, product_id: str) -> str:
        """由 ASIN 推导亚马逊评论页链接。"""
        if not product_id or product_id.upper() == "UNKNOWN":
            return ""
        return f"https://www.amazon.com/product-reviews/{product_id}/"
