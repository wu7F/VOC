#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VOC 平台 · 统一数据契约（地基）。

本模块定义**与数据源无关**的最小分析单元 ``Unit``，取代此前散落在各来源
导入脚本里的三套互不通用的字段（reddit posts / reddit comments / amazon reviews）。

所有适配器（``voc_core.adapters``）负责把任意来源的导出文件归一化为
``list[Unit]``；下游的清洗、标注、聚合、呈现只认这一套结构，因此**新增数据源
不需要改内核**。

设计要点
--------
1. ``source`` 说明"数据从哪来"，``source_type`` 说明"它是什么"
   （post / comment / review）。两者分开，才能表达"评论型 review"这类混合形态。
2. ``rating`` 可空 —— 有星级的来源（亚马逊 / Wayfair）启用星级分布模块，
   没有的（Reddit）自动隐藏该模块，不需要另写一套流程。
3. ``thread_id`` 把原先口头的"讨论串"概念固化下来：一条讨论串 = 一个 root
   单元 + 其下全部单元。Reddit 上是帖子与评论，电商上就是商品与它下面的评论。
4. ``extra`` 透传原始多余字段（score / 变体 / 站点等），不丢信息。
5. ``legacy_row()`` 输出旧 schema，使新适配器可以**先与现有内核共存**，
   逐步迁移而不是一次性重写。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

SOURCE_REDDIT = "reddit"
SOURCE_AMAZON = "amazon"
SOURCE_WAYFAIR = "wayfair"
KNOWN_SOURCES = (SOURCE_REDDIT, SOURCE_AMAZON, SOURCE_WAYFAIR)

TYPE_POST = "post"
TYPE_COMMENT = "comment"
TYPE_REVIEW = "review"
KNOWN_TYPES = (TYPE_POST, TYPE_COMMENT, TYPE_REVIEW)

#: 对外 CSV 交换格式的列顺序（稳定，勿随意调整）
UNIT_FIELDS = [
    "unit_id", "source", "source_type", "thread_id", "parent_unit_id",
    "title", "text", "author", "created_at", "lang",
    "rating", "product_id", "product_name", "brand",
    "channel", "url", "verified", "helpful_votes", "extra",
]

#: 原生带星级的来源 —— 决定看板/报告是否渲染星级分布
RATED_SOURCES = (SOURCE_AMAZON, SOURCE_WAYFAIR)


# --------------------------------------------------------------------------
# 单元
# --------------------------------------------------------------------------

@dataclass
class Unit:
    """一个最小分析单元（帖子 / 评论 / 评论型 review）。"""

    unit_id: str
    source: str
    source_type: str
    text: str = ""
    title: str = ""
    thread_id: str = ""
    parent_unit_id: str = ""
    author: str = ""
    created_at: str = ""          # ISO 8601 (UTC) 或空
    lang: str = ""                # zh / en / ""（未知）—— 供中英双语分析
    rating: int | None = None     # 1-5，无星级来源为 None
    product_id: str = ""          # ASIN / SKU / 商品 id
    product_name: str = ""
    brand: str = ""
    channel: str = ""             # subreddit / marketplace / 站点
    url: str = ""
    verified: bool | None = None  # 已验证购买（电商来源）；Reddit 为 None
    helpful_votes: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # -- 派生属性 ---------------------------------------------------------

    @property
    def has_rating(self) -> bool:
        return self.rating is not None

    @property
    def is_thread_root(self) -> bool:
        """是否是一条讨论串的根（帖子 / 商品评论的载体）。"""
        return self.source_type in (TYPE_POST, TYPE_REVIEW) or not self.parent_unit_id

    @property
    def body(self) -> str:
        """正文（旧 schema 里的 summary_text / content / body）。"""
        return self.text

    # -- 清洗 -------------------------------------------------------------

    def normalize(self) -> "Unit":
        """就地规范化：去空白、剥离 Reddit 的 t3_/t1_、u/ 前缀。

        :return: self（便于链式调用）
        """
        def clean(v: Any) -> str:
            return " ".join(str(v or "").split())

        self.unit_id = clean(self.unit_id)
        self.thread_id = _strip_prefixes(clean(self.thread_id), "t3_", "t1_")
        self.parent_unit_id = _strip_prefixes(clean(self.parent_unit_id), "t3_", "t1_")
        self.title = clean(self.title)
        self.text = clean(self.text)
        self.author = _strip_prefixes(clean(self.author), "u/", "/u/")
        self.created_at = clean(self.created_at)
        self.lang = clean(self.lang).lower()[:8]
        self.product_id = clean(self.product_id)
        self.product_name = clean(self.product_name)
        self.brand = clean(self.brand)
        self.channel = _strip_prefixes(clean(self.channel), "r/", "/r/")
        self.url = clean(self.url)
        self.source = clean(self.source).lower()
        self.source_type = clean(self.source_type).lower()

        # 根单元自身即讨论串锚点
        if self.is_thread_root and not self.thread_id:
            self.thread_id = self.unit_id
        return self

    # -- 校验 -------------------------------------------------------------

    def validate(self) -> list[str]:
        """返回问题列表（空列表 = 通过）。不抛异常，便于批量收集。"""
        errs: list[str] = []
        if not self.unit_id:
            errs.append("unit_id 为空")
        if not self.source:
            errs.append("source 为空")
        if self.source not in KNOWN_SOURCES:
            errs.append(f"未知 source: {self.source}")
        if self.source_type not in KNOWN_TYPES:
            errs.append(f"未知 source_type: {self.source_type}")
        if not self.text and not self.title:
            errs.append("title 与 text 同时为空（无内容可分析）")
        if self.rating is not None and not (1 <= int(self.rating) <= 5):
            errs.append(f"rating 越界: {self.rating}")
        if self.source in RATED_SOURCES and self.rating is None:
            # 电商来源缺星级常见（部分导出无星级列），仅作提示不算硬错误
            pass
        return errs

    # -- 序列化 -----------------------------------------------------------

    def to_row(self) -> dict[str, str]:
        """转为 CSV 行（全部值字符串化，extra 以 JSON 存储）。"""
        row: dict[str, str] = {}
        for k in UNIT_FIELDS:
            v = getattr(self, k)
            if k == "extra":
                row[k] = json.dumps(v, ensure_ascii=False) if v else ""
            elif k == "rating":
                row[k] = "" if v is None else str(int(v))
            elif k == "verified":
                row[k] = "" if v is None else ("true" if v else "false")
            elif k == "helpful_votes":
                row[k] = "" if v is None else str(int(v))
            else:
                row[k] = "" if v is None else str(v)
        return row

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Unit":
        """从 CSV 行还原（宽容解析，坏值降级为默认）。"""
        def g(*names: str) -> str:
            for n in names:
                v = row.get(n)
                if v is not None and str(v).strip() != "":
                    return str(v).strip()
            return ""

        rating_raw = g("rating")
        try:
            rating = int(float(rating_raw)) if rating_raw else None
        except ValueError:
            rating = None

        votes_raw = g("helpful_votes")
        try:
            votes = int(float(votes_raw)) if votes_raw else None
        except ValueError:
            votes = None

        verified_raw = g("verified").lower()
        if verified_raw in ("true", "yes", "1", "是", "y", "vp"):
            verified: bool | None = True
        elif verified_raw in ("false", "no", "0", "否", "n"):
            verified = False
        else:
            verified = None

        extra_raw = g("extra")
        if extra_raw:
            try:
                extra = json.loads(extra_raw)
            except json.JSONDecodeError:
                extra = {}
        else:
            extra = {}

        return cls(
            unit_id=g("unit_id"),
            source=g("source"),
            source_type=g("source_type"),
            text=g("text"),
            title=g("title"),
            thread_id=g("thread_id"),
            parent_unit_id=g("parent_unit_id"),
            author=g("author"),
            created_at=g("created_at"),
            lang=g("lang"),
            rating=rating,
            product_id=g("product_id"),
            product_name=g("product_name"),
            brand=g("brand"),
            channel=g("channel"),
            url=g("url"),
            verified=verified,
            helpful_votes=votes,
            extra=extra,
        )

    # -- 兼容层 -----------------------------------------------------------

    def legacy_row(self) -> dict[str, str]:
        """输出旧管线（6,900 行内核）可直接消费的行。

        渐进迁移的关键：新适配器产出的 Unit 可以随时降级成老 schema，
        因此**不需要一次性重写下游**。三种形态各自对应原导入脚本的输出：

        * reddit + post    → ``ingest_data.py`` 的 reddit_posts.csv
        * reddit + comment → ``ingest_comments.py`` 的 reddit_comments.csv
        * amazon/wayfair   → ``ingest_reviews.py`` 的 amazon_reviews.csv
        """
        if self.source == SOURCE_REDDIT and self.source_type == TYPE_COMMENT:
            return {
                "comment_id": self.unit_id,
                "post_reddit_id": self.thread_id,
                "post_subreddit": self.channel,
                "post_title": self.title,
                "author": self.author,
                "published": self.created_at,
                "body": self.text,
                "is_root_comment": "True" if not self.parent_unit_id else "False",
            }
        if self.source in RATED_SOURCES or self.source_type == TYPE_REVIEW:
            return {
                "review_id": self.unit_id,
                "title": self.title,
                "content": self.text,
                "rating": "" if self.rating is None else str(int(self.rating)),
                "asin": self.product_id,
                "product": self.product_name,
                "brand": self.brand,
                "author": self.author,
                "date": self.created_at,
                "url": self.url,
                "verified": "" if self.verified is None else ("true" if self.verified else "false"),
                "helpful_votes": "" if self.helpful_votes is None else str(int(self.helpful_votes)),
                "country": self.channel,
                "variant": str(self.extra.get("variant", "")),
                "source": self.source,
            }
        # 默认按 reddit post
        return {
            "reddit_id": self.unit_id,
            "title": self.title,
            "url": self.url,
            "subreddit": self.channel,
            "author": self.author,
            "published": self.created_at,
            "updated": str(self.extra.get("updated", "")),
            "summary_text": self.text,
            "query": str(self.extra.get("query", "")),
            "scope": self.channel,
            "sort": "",
            "time_filter": "",
            "feed_url": self.url,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------

def _strip_prefixes(v: str, *prefixes: str) -> str:
    for p in prefixes:
        if v.lower().startswith(p.lower()):
            return v[len(p):]
    return v


def legacy_fields(units: Iterable[Unit]) -> list[str]:
    """推断 legacy 行应使用的列顺序（按单元形态取并集）。"""
    seen: list[str] = []
    for u in units:
        for k in u.legacy_row():
            if k not in seen:
                seen.append(k)
    return seen


def normalize_all(units: Iterable[Unit]) -> list[Unit]:
    """批量规范化并剔除无内容单元，返回新列表。"""
    out: list[Unit] = []
    for u in units:
        u.normalize()
        if u.text or u.title:
            out.append(u)
    return out


def summarize(units: list[Unit]) -> dict[str, Any]:
    """产出数据规模统计，供看板「数据说明」页与质检报告使用。"""
    by_source: dict[str, int] = {}
    by_type: dict[str, int] = {}
    rated: dict[str, int] = {}
    lang: dict[str, int] = {}
    dated = 0
    threads: set[str] = set()

    for u in units:
        by_source[u.source] = by_source.get(u.source, 0) + 1
        by_type[u.source_type] = by_type.get(u.source_type, 0) + 1
        if u.rating is not None:
            key = str(u.rating)
            rated[key] = rated.get(key, 0) + 1
        if u.lang:
            lang[u.lang] = lang.get(u.lang, 0) + 1
        if u.created_at:
            dated += 1
        if u.thread_id:
            threads.add(u.thread_id)

    return {
        "total_units": len(units),
        "threads": len(threads),
        "by_source": by_source,
        "by_source_type": by_type,
        "rating_distribution": dict(sorted(rated.items(), key=lambda kv: -int(kv[0]))),
        "lang_distribution": lang,
        "dated_units": dated,
        "undated_ratio": round(1 - dated / len(units), 4) if units else 0.0,
    }
