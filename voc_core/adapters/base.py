#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""适配器协议与注册表。

一个数据源 = 一个适配器文件。新增来源（Trustpilot / App Store / 客服工单…）
只需实现 ``Adapter`` 的两个方法并注册，**内核不需要任何改动**。

协议刻意做得极小：

* ``detect(path) -> float`` —— 给这份文件"像不像我这个源"打 0–1 置信度，
  支持 ``--source auto`` 自动路由。
* ``normalize(path, **opts) -> list[Unit]`` —— 归一化为统一契约。

``EcommerceReviewAdapter`` 把电商评论共有的逻辑（星级解析、已验证购买、
评论 id 兜底、ASIN/SKU 推导链接）沉淀成基类，亚马逊与 Wayfair 各自只覆盖
**字段别名表**与 **detect 特征词**。
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterable

from ..io_utils import (
    collapse_ws,
    epoch_to_iso,
    norm_key,
    pick,
    read_rows,
    read_xlsx_rows,
    to_bool,
    to_int,
)
from ..schema import (
    SOURCE_AMAZON,
    SOURCE_REDDIT,
    SOURCE_WAYFAIR,
    TYPE_COMMENT,
    TYPE_POST,
    TYPE_REVIEW,
    Unit,
)


class Adapter(ABC):
    """数据源适配器协议。"""

    #: 适配器标识（注册表 key）。一个源可有多个适配器 ——
    #: 例如 Reddit 的帖子导出与评论导出形态完全不同，分别是
    #: ``reddit_posts`` / ``reddit_comments``。
    adapter_id: str = ""
    #: 数据源标识（写入 Unit.source）：reddit / amazon / wayfair
    source: str = ""
    #: 人类可读名称（日志与前端下拉用）
    label: str = ""
    #: 该适配器产出的单元形态说明（文档用）
    produces: str = ""

    @abstractmethod
    def detect(self, path: Path) -> float:
        """返回该文件属于本源的置信度（0–1）。"""

    @abstractmethod
    def normalize(self, path: Path, **opts: Any) -> list[Unit]:
        """把文件归一化为 Unit 列表。"""

    # -- 供子类复用 -------------------------------------------------------

    @staticmethod
    def read_source(path: str | Path, limit: int | None = None) -> list[dict[str, Any]]:
        """按后缀读取源文件：csv / jsonl / json / xlsx。

        第三方导出经常直接给 ``.xlsx``（如 ``批量导出_<N>条.xlsx``），
        因此读取入口必须统一处理，否则每个适配器都要各写一遍。
        ``limit`` 供识别阶段采样，避免为了看表头而读 12 MB 全量。
        """
        p = Path(path)
        suf = p.suffix.lower()
        if suf in (".xlsx", ".xlsm"):
            return read_xlsx_rows(p, limit=limit)
        if suf == ".xls":
            raise SystemExit(
                f"不支持旧版 .xls（{p.name}）。请先用 Excel 另存为 .xlsx 或 CSV 再导入。"
            )
        return read_rows(p, limit=limit)

    @staticmethod
    def sniff_values(path: Path, aliases: Iterable[str], limit: int = 300) -> set[str]:
        """采样某列（按别名定位）出现过的值（小写、去空白）。

        用于**形态识别**，而不只是列名识别。典型场景：区分「纯评论导出」与
        「帖子 + 评论混装导出」—— 两者的列名几乎一样，唯一区别是后者的
        ``type`` 列里会出现 ``post`` 值。只看列名无法分辨。
        """
        try:
            rows = Adapter.read_source(path, limit=limit)
        except Exception:  # noqa: BLE001 - 识别阶段不允许抛异常
            return set()
        vals: set[str] = set()
        for r in rows:
            v = pick(r, aliases)
            if v:
                vals.add(v.strip().lower())
        return vals

    @staticmethod
    def sniff_columns(path: Path) -> set[str]:
        """读表头（经 ``norm_key`` 归一化），供 detect 判断。读不到返回空集。

        归一化后 ``Review Title`` / ``review-title`` / ``review_title`` 都变成
        ``review_title``，因此 detect 的列名判断与 pick 的别名匹配口径一致。
        """
        try:
            rows = Adapter.read_source(path, limit=50)   # 只需表头，采样即可
        except Exception:  # noqa: BLE001 - detect 阶段不允许抛异常
            return set()
        if not rows:
            return set()
        cols: set[str] = set()
        for r in rows[:1]:
            for k in r:
                nk = norm_key(k)
                if nk:
                    cols.add(nk)
        return cols

    @staticmethod
    def filename_hint(path: Path) -> str:
        return path.name.lower()


# --------------------------------------------------------------------------
# 电商评论基类
# --------------------------------------------------------------------------

class EcommerceReviewAdapter(Adapter):
    """电商评论适配器基类（亚马逊 / Wayfair / 通用电商导出）。

    子类需要覆盖：
      * ``source`` / ``label``
      * ``FIELD_ALIASES``  字段别名表（各自平台的导出列名不同）
      * ``detect_tokens``  文件名与列名里的特征词
    """

    FIELD_ALIASES: dict[str, list[str]] = {
        "unit_id": ["review_id", "review-id", "id", "评论id", "review uuid", "uuid", "reviewid"],
        "title": ["title", "review_title", "review title", "评论标题", "headline",
                  "summary", "title_text"],
        "text": ["content", "body", "review_content", "review content", "review_text",
                 "review text", "text", "评论内容", "评论", "body_text", "评论正文",
                 "content_full", "review_body", "comment"],
        "rating": ["rating", "star", "stars", "star_rating", "星级", "评分",
                   "review_rating", "rating_score", "review rating", "打分",
                   "overall_rating", "score"],
        "product_id": ["asin", "asin1", "parent_asin", "parent asin", "商品asin",
                       "sku", "product_id", "productid", "item_id", "itemid",
                       "product sku", "商品id", "产品id"],
        "product_name": ["product_title", "product title", "product_name", "product name",
                         "product", "商品名称", "商品标题", "item_title", "item title",
                         "商品名", "产品名称", "product_description"],
        "brand": ["brand", "brand_name", "brand name", "品牌", "manufacturer",
                  "品牌/brand", "seller_brand", "vendor"],
        "author": ["author", "reviewer", "reviewer_name", "reviewer name", "profile_name",
                   "用户名", "customer", "客户名", "nickname", "评论人", "user_name",
                   "username", "customer_name"],
        "created_at": ["date", "review_date", "review date", "reviewed_at", "reviewed at",
                       "评论时间", "评论日期", "datetime", "timestamp", "created_at",
                       "created", "time", "post_date", "submission_time", "submit_date",
                       "review_time", "reviewtime"],
        "url": ["url", "review_url", "review url", "link", "链接", "评论链接",
                "product_url", "permalink"],
        "verified": ["verified", "verified_purchase", "verified purchase",
                     "is_verified", "是否真实购买", "vp", "verified_purchase_flag",
                     "verified_purchaser", "verified_buyer", "verified_owner"],
        "helpful_votes": ["helpful_votes", "helpful votes", "helpful_count", "helpful",
                          "有用票数", "有用投票", "helpful_votes_count", "helpful_vote_count",
                          "upvotes", "likes", "helpful_yes"],
        "channel": ["country", "marketplace", "站点", "site", "marketplace_id",
                    "country_code", "国家", "region", "locale"],
        "variant": ["variant", "style", "variation", "变体", "型号", "规格",
                    "variant_info", "product_variant", "color", "size", "finish"],
    }

    #: 文件名 / 列名里出现即加分
    detect_tokens: tuple[str, ...] = ()
    #: 该源的特征列（出现即强烈加分）
    signature_columns: tuple[str, ...] = ()

    def detect(self, path: Path) -> float:
        cols = self.sniff_columns(path)
        name = self.filename_hint(path)
        if not cols:
            return 0.0

        score = 0.0
        # 特征列命中（强信号）
        for c in self.signature_columns:
            if norm_key(c) in cols:
                score += 0.35
        # 文件名特征词
        for t in self.detect_tokens:
            if t.lower() in name:
                score += 0.3
        # 通用电商评论形态（弱信号）。
        # 刻意压低：亚马逊与 Wayfair 的导出列名高度重叠（都有正文 + 星级），
        # 若把"通用形态"给足分数，两个适配器会打成平手并随机命中一个。
        # 压到 0.2 总和之后，无特征文件会低于 detect_best 的 0.3 阈值 →
        # 明确返回"识别不出"，提示用户显式指定来源，而不是猜一个。
        has_text = bool(cols & {norm_key(a) for a in self.FIELD_ALIASES["text"]})
        has_rating = bool(cols & {norm_key(a) for a in self.FIELD_ALIASES["rating"]})
        if has_text:
            score += 0.1
        if has_rating:
            score += 0.1
        return min(score, 1.0)

    def normalize(self, path: Path, **opts: Any) -> list[Unit]:
        rows = self.read_source(path)
        return self.normalize_rows(rows, source_note=str(opts.get("source_note", "")),
                                   default_brand=str(opts.get("default_brand", "")),
                                   default_channel=str(opts.get("default_channel", "")))

    # -- 共用实现 ---------------------------------------------------------

    def normalize_rows(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        source_note: str = "",
        default_brand: str = "",
        default_channel: str = "",
    ) -> list[Unit]:
        out: list[Unit] = []
        seen: set[str] = set()
        rows_total = 0
        skipped_no_text = 0          # 打了星但没写文字（纯评分评论）
        rating_only: dict[str, int] = {}
        skipped_empty = 0            # 完全空行
        skipped_dup = 0              # 与已有单元 id 相同

        for row in rows:
            rows_total += 1
            used: set[str] = set()
            text = collapse_ws(pick(row, self.FIELD_ALIASES["text"], mark_used=used))
            title = collapse_ws(pick(row, self.FIELD_ALIASES["title"], mark_used=used))
            if not text and not title:
                # 无正文可分析 → 不进分析单元。但**必须计数**：
                # 实测 Wayfair 官方导出里存在"只打星、不写字"的评论，
                # 静默丢弃会让星级分布与平台后台对不上，且用户无从察觉
                # （与"混合导出静默丢帖子"是同一类事故）。
                r0 = to_int(pick(row, self.FIELD_ALIASES["rating"], mark_used=used))
                if r0 is not None and 1 <= r0 <= 5:
                    skipped_no_text += 1
                    rating_only[str(r0)] = rating_only.get(str(r0), 0) + 1
                else:
                    skipped_empty += 1
                continue

            rating = to_int(pick(row, self.FIELD_ALIASES["rating"], mark_used=used))
            if rating is not None and not (1 <= rating <= 5):
                rating = None

            product_id = pick(row, self.FIELD_ALIASES["product_id"], mark_used=used)
            uid_raw = pick(row, self.FIELD_ALIASES["unit_id"], mark_used=used)
            uid = self._derive_id(uid_raw, product_id, rating, title, text)
            if uid in seen:
                skipped_dup += 1
                continue
            seen.add(uid)

            created = self.parse_date(pick(row, self.FIELD_ALIASES["created_at"], mark_used=used))

            url = pick(row, self.FIELD_ALIASES["url"], mark_used=used)
            if not url and product_id:
                url = self.product_url(product_id)

            channel = (pick(row, self.FIELD_ALIASES["channel"], mark_used=used)
                       or default_channel)
            brand = pick(row, self.FIELD_ALIASES["brand"], mark_used=used) or default_brand

            extra: dict[str, Any] = {}
            variant = pick(row, self.FIELD_ALIASES["variant"], mark_used=used)
            if variant:
                extra["variant"] = variant
            for k, v in row.items():
                key = str(k).strip()
                if key in used or key.lower() in {"", "nan"}:
                    continue
                s = collapse_ws(v)
                if s:
                    extra[key] = s
            if source_note:
                extra["source_note"] = source_note

            out.append(Unit(
                unit_id=uid,
                source=self.source,
                source_type=TYPE_REVIEW,
                thread_id=product_id or uid,   # 同一商品的评论归为一条"讨论串"
                title=title,
                text=text,
                author=pick(row, self.FIELD_ALIASES["author"], mark_used=used),
                created_at=created,
                rating=rating,
                product_id=product_id,
                product_name=collapse_ws(pick(row, self.FIELD_ALIASES["product_name"], mark_used=used)),
                brand=brand,
                channel=channel,
                url=url,
                verified=to_bool(pick(row, self.FIELD_ALIASES["verified"], mark_used=used)),
                helpful_votes=to_int(pick(row, self.FIELD_ALIASES["helpful_votes"], mark_used=used)),
                extra=extra,
            ))

        # 导入台账：让"少了 N 条"永远可解释，而不是黑箱
        self.last_stats: dict[str, Any] = {
            "rows_total": rows_total,
            "kept": len(out),
            "skipped_no_text": skipped_no_text,
            "skipped_empty": skipped_empty,
            "skipped_duplicate": skipped_dup,
            "rating_only_distribution": dict(
                sorted(rating_only.items(), key=lambda kv: -int(kv[0]))
            ),
        }
        return [u.normalize() for u in out]

    @staticmethod
    def _derive_id(raw_id: str, product_id: str, rating: int | None, title: str, text: str) -> str:
        """评论 id 兜底：对 (商品|星级|标题|正文) 做确定性哈希。

        确定性很关键 —— 同一份数据重复导入必须得到同样的 id，否则去重失效、
        所有计数会成倍虚高。
        """
        if raw_id:
            raw_id = re.sub(r"^[rt]\d+_", "", raw_id.strip())
            if raw_id:
                return raw_id
        h = hashlib.md5(
            f"{product_id}|{rating}|{title}|{text}".encode("utf-8")
        ).hexdigest()
        return "rv_" + h[:12]

    @staticmethod
    def parse_date(raw: str) -> str:
        """把电商导出的日期规范化为 ISO 8601（UTC，带 Z）。

        实测各平台格式差异很大：Wayfair 官方导出是 ``2025-11-27``，亚马逊后台
        是 ``2025/11/27``，中文导出是 ``2025年11月27日``，第三方工具常给 Unix 秒。

        **只做无歧义的转换**：``11/27/2025`` 这类美式写法（月/日）与欧式（日/月）
        无法从字面区分，猜错会把时间整体错位，因此原样保留而不是硬转。
        纯日期统一补 ``T00:00:00Z``，让下游的时间线/月度分布可以直接按同一格式解析
        —— 不补的话 ``2025-11-27`` 与 ``2026-01-01T00:00:00Z`` 两种形态会让
        "按天/按月聚合"这类逻辑各写一遍。
        """
        v = collapse_ws(raw)
        if not v:
            return ""

        # Unix 秒 / 毫秒（阈值放到 9 位，避免把 "2025" 这类年份误当时间戳）
        if re.fullmatch(r"\d{9,13}(\.\d+)?", v):
            return epoch_to_iso(v)

        # 2025-11-27 / 2025/11/27 / 2025.11.27（后可带时间）
        m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", v)
        if m:
            y, mo, d = (int(g) for g in m.groups())
            tm = re.match(r"[T ]?(\d{1,2}):(\d{2})(?::(\d{2}))?", v[m.end():].strip())
            if tm:
                hh, mi, ss = int(tm.group(1)), tm.group(2), tm.group(3) or "00"
                stamp = f"T{hh:02d}:{mi}:{ss}"
            else:
                stamp = "T00:00:00"
            return f"{y:04d}-{mo:02d}-{d:02d}{stamp}Z"

        # 2025年11月27日
        m = re.match(r"^(\d{4})年(\d{1,2})月(\d{1,2})日?", v)
        if m:
            y, mo, d = (int(g) for g in m.groups())
            return f"{y:04d}-{mo:02d}-{d:02d}T00:00:00Z"

        return v

    def product_url(self, product_id: str) -> str:
        """由商品 id 推导评论页链接；子类覆盖。"""
        return ""


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------

_REGISTRY: dict[str, Adapter] = {}


def register(adapter: Adapter) -> Adapter:
    """注册适配器（按 adapter_id 索引）。"""
    if not adapter.adapter_id:
        raise ValueError(f"适配器 {type(adapter).__name__} 缺少 adapter_id")
    _REGISTRY[adapter.adapter_id] = adapter
    return adapter


def get(name: str) -> Adapter:
    """按 adapter_id 取适配器。

    也接受**源标识**（如 ``amazon``），前提是该源只有一个适配器；
    有歧义时（如 ``reddit`` 同时对应帖子与评论）会明确报错并列出候选。
    """
    key = (name or "").strip().lower()
    if key in _REGISTRY:
        return _REGISTRY[key]
    by_source = [a for a in _REGISTRY.values() if a.source == key]
    if len(by_source) == 1:
        return by_source[0]
    if len(by_source) > 1:
        raise KeyError(
            f"'{name}' 对应多个适配器，请指明其一："
            + ", ".join(a.adapter_id for a in by_source)
        )
    raise KeyError(
        f"未知适配器 '{name}'。可用：{', '.join(sorted(_REGISTRY))}"
    )


def available() -> list[Adapter]:
    """返回全部已注册适配器（用于前端下拉与 --help）。"""
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]


def detect_best(path: str | Path) -> tuple[Adapter | None, dict[str, float]]:
    """自动识别文件属于哪个适配器。

    :return: (最佳适配器, {adapter_id: 置信度})；全部低于阈值时最佳为 None。
    """
    p = Path(path)
    scores = {a.adapter_id: round(a.detect(p), 3) for a in available()}
    if not scores:
        return None, {}
    best = max(scores, key=lambda k: scores[k])
    if scores[best] < 0.3:
        return None, scores
    return _REGISTRY[best], scores


# 源码里显式引用，避免 lint 认为常量未使用（同时作为文档锚点）
_KNOWN_SOURCE_LABELS = {
    SOURCE_REDDIT: "Reddit",
    SOURCE_AMAZON: "亚马逊",
    SOURCE_WAYFAIR: "Wayfair",
}
_KNOWN_TYPES = (TYPE_POST, TYPE_COMMENT, TYPE_REVIEW)
