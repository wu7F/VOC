#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""适配器注册表入口。

导入本包即完成全部内置适配器的注册。新增数据源时在此追加一行即可。
"""

from __future__ import annotations

from .base import (
    Adapter,
    EcommerceReviewAdapter,
    available,
    detect_best,
    get,
    register,
)
from .amazon_reviews import AmazonReviewsAdapter
from .reddit_comments import RedditCommentsAdapter
from .reddit_export import RedditExportAdapter
from .reddit_posts import RedditPostsAdapter
from .wayfair_reviews import WayfairReviewsAdapter

#: 内置适配器清单（新增源在这里加一行）
BUILTIN: tuple[Adapter, ...] = (
    RedditPostsAdapter(),
    RedditCommentsAdapter(),
    RedditExportAdapter(),
    AmazonReviewsAdapter(),
    WayfairReviewsAdapter(),
)

for _adapter in BUILTIN:
    register(_adapter)

__all__ = [
    "Adapter",
    "EcommerceReviewAdapter",
    "AmazonReviewsAdapter",
    "RedditCommentsAdapter",
    "RedditExportAdapter",
    "RedditPostsAdapter",
    "WayfairReviewsAdapter",
    "BUILTIN",
    "available",
    "detect_best",
    "get",
    "register",
]
