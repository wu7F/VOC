#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reddit 帖子适配器。

迁移自 ``reddit分析/scripts/ingest_data.py``，行为保持一致：
字段别名映射、从 URL 推导 subreddit、epoch 转 ISO、由 subreddit + id 补全
标准评论链接、按 id 去重并合并多搜索词命中记录。

**给通用平台的一处有意放宽**：原脚本会把"补不出 reddit.com 链接"的行直接丢弃
（因为老 analyzer 强制校验 URL 含 reddit.com）。新适配器改为**保留该单元**——
通用平台的分析不该被来源链接缺失阻断，只在 ``extra`` 里记 ``url_missing``。
降级为老 schema 时若确实需要链接，仍会走同一套补全逻辑。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable

from ..io_utils import (
    collapse_ws,
    epoch_to_iso,
    pick,
    strip_prefixes,
)
from ..schema import SOURCE_REDDIT, TYPE_POST, Unit
from .base import Adapter

FIELD_ALIASES = {
    "unit_id": ["reddit_id", "id", "post_id", "name", "fullname", "t3_id", "postid"],
    "title": ["title", "title_text", "post_title", "headline"],
    "url": ["url", "link", "permalink", "post_url", "comments_url", "comments", "url_path"],
    "channel": ["subreddit", "subreddit_name", "sub", "subreddit_id", "community"],
    "author": ["author", "username", "user", "reddit_username", "reddit_author"],
    "created_at": ["published", "created", "created_utc", "created_at", "datetime",
                   "timestamp", "time", "date", "post_date"],
    "updated": ["updated", "edited", "edited_utc", "last_edit"],
    "text": ["summary_text", "selftext", "self_text", "body", "text", "content",
             "description", "post_body", "raw_text"],
    "query": ["query", "search_query", "keyword", "q", "search_term"],
}

ALL_ALIASES = {a for v in FIELD_ALIASES.values() for a in v}

#: 原生 schema 字段名（透传多余字段时排除）
NATIVE = {"reddit_id", "title", "url", "subreddit", "author", "published", "updated",
          "summary_text", "query", "scope", "sort", "time_filter", "feed_url"}


class RedditPostsAdapter(Adapter):
    adapter_id = "reddit_posts"
    source = SOURCE_REDDIT
    label = "Reddit 帖子导出"
    produces = "unit_type=post（帖子标题 + 正文）"

    def detect(self, path: Path) -> float:
        cols = self.sniff_columns(path)
        name = self.filename_hint(path)
        if not cols:
            return 0.0
        score = 0.0
        # Reddit 帖子特征列
        if cols & {"subreddit", "subreddit_name", "sub"}:
            score += 0.5
        if cols & {"selftext", "summary_text", "post_title", "title_text"}:
            score += 0.2
        if cols & {"reddit_id", "post_id", "permalink"}:
            score += 0.2
        # 评论形态的列出现在这里要压分（交给 reddit_comments）
        if cols & {"postid", "parentid"}:
            score -= 0.25
        if "reddit" in name:
            score += 0.2
        if "comment" in name:
            score -= 0.3
        return max(0.0, min(score, 1.0))

    def normalize(self, path: Path, **opts: Any) -> list[Unit]:
        rows = self.read_source(path)
        return self.normalize_rows(
            rows,
            default_channel=str(opts.get("default_subreddit", opts.get("default_channel", ""))),
            query_default=str(opts.get("query", "")),
            category=str(opts.get("category", "")),
            source_note=str(opts.get("source_note", "")),
        )

    def normalize_rows(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        default_channel: str = "",
        query_default: str = "",
        category: str = "",
        source_note: str = "",
    ) -> list[Unit]:
        rows = list(rows)   # 防止调用方传迭代器时被统计逻辑二次消费
        units: list[Unit] = []
        best: dict[str, Unit] = {}
        order: list[str] = []
        merged = 0

        for row in rows:
            used: set[str] = set()
            title = collapse_ws(pick(row, FIELD_ALIASES["title"], mark_used=used))
            text = collapse_ws(pick(row, FIELD_ALIASES["text"], mark_used=used))
            if not title and not text:
                continue

            url_raw = pick(row, FIELD_ALIASES["url"], mark_used=used)
            channel = (strip_prefixes(pick(row, FIELD_ALIASES["channel"], mark_used=used), "r/")
                       or _subreddit_from_url(url_raw) or default_channel)
            author = pick(row, FIELD_ALIASES["author"], mark_used=used)

            created_raw = pick(row, FIELD_ALIASES["created_at"], mark_used=used)
            created = epoch_to_iso(created_raw) if re.fullmatch(r"\s*[\d.]+\s*", created_raw or "") else created_raw
            updated_raw = pick(row, FIELD_ALIASES["updated"], mark_used=used)
            updated = epoch_to_iso(updated_raw) if re.fullmatch(r"\s*[\d.]+\s*", updated_raw or "") else updated_raw

            uid = self._derive_id(
                pick(row, FIELD_ALIASES["unit_id"], mark_used=used),
                url_raw, title, channel, created,
            )
            url = _ensure_reddit_url(url_raw, channel, uid)

            extra: dict[str, Any] = {}
            query = pick(row, FIELD_ALIASES["query"], mark_used=used) or query_default or category
            if query:
                extra["query"] = query
            if updated:
                extra["updated"] = updated
            if source_note:
                extra["source_note"] = source_note
            for k, v in row.items():
                key = str(k).strip()
                if key in used or key.lower() in {"", "nan"} or key in NATIVE:
                    continue
                s = collapse_ws(v)
                if s:
                    extra[key] = s

            u = Unit(
                unit_id=uid,
                source=SOURCE_REDDIT,
                source_type=TYPE_POST,
                thread_id=uid,
                title=title,
                text=text,
                author=author,
                created_at=created,
                product_name="",           # Reddit 无商品概念，品类的载体是 query
                channel=channel,
                url=url,
                extra=extra,
            ).normalize()

            # 同一帖被多个搜索词各导出一行 → 合并，保留信息量最大的一行
            if uid in best:
                merged += 1
                cur = best[uid]
                if len(u.text) > len(cur.text):
                    cur.text = u.text
                for attr in ("title", "author", "created_at", "channel", "url"):
                    if not getattr(cur, attr) and getattr(u, attr):
                        setattr(cur, attr, getattr(u, attr))
                qs = [q for q in (cur.extra.get("query", ""), u.extra.get("query", "")) if q]
                seen_q: list[str] = []
                for q in "||".join(qs).split("||"):
                    q = q.strip()
                    if q and q not in seen_q:
                        seen_q.append(q)
                if len(seen_q) > 1:
                    cur.extra["matched_queries"] = "||".join(seen_q)
                continue

            best[uid] = u
            order.append(uid)

        units = [best[k] for k in order]
        self.last_stats = {"rows": len(rows), "kept": len(units), "merged_duplicates": merged}
        return units

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _derive_id(raw_id: str, url: str, title: str, channel: str, created: str) -> str:
        rid = strip_prefixes(raw_id, "t3_", "t1_")
        if rid:
            return rid
        if url:
            m = re.search(r"/comments/([a-z0-9]+)", url, re.I)
            if m:
                return m.group(1)
            seg = [s for s in url.rstrip("/").split("/") if s]
            if seg and re.fullmatch(r"[a-z0-9_]{5,}", seg[-1]):
                return seg[-1]
        h = hashlib.md5(f"{title}|{channel}|{created}".encode("utf-8")).hexdigest()
        return "up_" + h[:12]


def _subreddit_from_url(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"reddit\.com/r/([A-Za-z0-9_]+)", url, re.I)
    if m:
        return m.group(1)
    m = re.match(r"\s*/?r/([A-Za-z0-9_]+)", url, re.I)
    return m.group(1) if m else ""


def _ensure_reddit_url(url: str, channel: str, uid: str) -> str:
    """补全为含 reddit.com 的标准链接（老内核强制校验这一点）。"""
    if url and "reddit.com" in url:
        return url
    if url and not url.lower().startswith("http"):
        return "https://www.reddit.com" + (url if url.startswith("/") else "/" + url)
    if channel and uid:
        return f"https://www.reddit.com/r/{channel}/comments/{uid}/"
    return ""
