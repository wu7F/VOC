#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reddit 评论适配器。

迁移自 ``reddit分析/scripts/ingest_comments.py``：列名别名、混合编码处理
（在 ``io_utils.read_text_auto`` 里统一实现）、``type == comment`` 过滤、
``is_root_comment`` 判定（parentId 空或 t3_ 开头 = 直接回复帖子）、按 id 去重。

第三方"关键词搜索导出"（``批量导出_<N>条.xlsx``）也走这里：那份数据里
``type=comment`` 的行是评论，``postId`` 指向所属帖子，``id`` 形如 ``t1_xxxx``。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from ..io_utils import collapse_ws, epoch_to_iso, pick, strip_prefixes, to_int
from ..schema import SOURCE_REDDIT, TYPE_COMMENT, Unit
from .base import Adapter

FIELD_ALIASES = {
    "unit_id": ["comment_id", "id", "name", "fullname", "t1_id"],
    "thread_id": ["post_reddit_id", "postId", "post_id", "link_id", "submission_id",
                  "thread_id", "postid"],
    "text": ["body", "content", "text", "comment", "comment_body", "selftext",
             "正文", "评论", "内容"],
    "author": ["author", "username", "user", "reddit_author", "作者"],
    "created_at": ["published", "date", "created", "created_utc", "created_at",
                   "datetime", "time", "时间", "日期"],
    "parent_unit_id": ["parentId", "parent_id", "parent", "父级"],
    "type": ["type", "kind", "item_type", "类型"],
    "url": ["url", "permalink", "link", "post_url", "url_path"],
    "helpful_votes": ["score", "likes", "ups", "upvotes", "points", "点赞"],
    "channel": ["post_subreddit", "subreddit", "subreddit_name", "sub", "community"],
}

NATIVE = {"comment_id", "post_reddit_id", "post_subreddit", "post_title",
          "author", "published", "body", "is_root_comment"}


class RedditCommentsAdapter(Adapter):
    adapter_id = "reddit_comments"
    source = SOURCE_REDDIT
    label = "Reddit 评论导出"
    produces = "unit_type=comment（评论正文，挂在所属帖子下）"

    def detect(self, path: Path) -> float:
        cols = self.sniff_columns(path)
        name = self.filename_hint(path)
        if not cols:
            return 0.0
        score = 0.0
        # camelCase 的 postId / parentId 是第三方 Reddit 导出的强特征
        if "postid" in cols:
            score += 0.45
        if "parentid" in cols:
            score += 0.15
        if cols & {"body", "comment_body"}:
            score += 0.2
        if cols & {"subreddit", "subreddit_name"}:
            score += 0.15
        if "reddit" in name or "comment" in name:
            score += 0.2

        # 让路：若 type 列的值里出现 post，说明这是**帖子+评论混装**导出，
        # 应交由 reddit_export 处理 —— 本适配器只取评论，会静默丢掉全部帖子。
        # 明确扣分而不是靠运气比大小，避免"看起来导入成功、实际丢一半数据"。
        if self.sniff_values(path, FIELD_ALIASES["type"]) & {"post", "t3", "submission"}:
            score -= 0.5
        return max(0.0, min(score, 1.0))

    def normalize(self, path: Path, **opts: Any) -> list[Unit]:
        rows = self.read_source(path)
        post_meta: dict[str, dict[str, str]] = opts.get("posts_meta") or {}
        return self.normalize_rows(
            rows,
            post_meta=post_meta,
            only_matching_posts=bool(opts.get("only_matching_posts", False)),
            source_note=str(opts.get("source_note", "")),
        )

    def normalize_rows(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        post_meta: dict[str, dict[str, str]] | None = None,
        only_matching_posts: bool = False,
        source_note: str = "",
    ) -> list[Unit]:
        post_meta = post_meta or {}
        units: list[Unit] = []
        seen: set[str] = set()
        stats = {"rows": 0, "skipped_type": 0, "skipped_empty": 0,
                 "skipped_dup": 0, "skipped_nopost": 0}

        for row in rows:
            stats["rows"] += 1
            used: set[str] = set()

            rtype = pick(row, FIELD_ALIASES["type"], mark_used=used).lower()
            if rtype and rtype not in ("comment", "t1"):
                stats["skipped_type"] += 1
                continue

            text = collapse_ws(pick(row, FIELD_ALIASES["text"], mark_used=used))
            if not text:
                stats["skipped_empty"] += 1
                continue

            thread_id = strip_prefixes(pick(row, FIELD_ALIASES["thread_id"], mark_used=used), "t3_")
            uid = strip_prefixes(pick(row, FIELD_ALIASES["unit_id"], mark_used=used), "t1_")
            if not uid:
                uid = "c_" + str(abs(hash(thread_id + text[:80])) % (10 ** 12))
            if uid in seen:
                stats["skipped_dup"] += 1
                continue
            if only_matching_posts and post_meta and thread_id not in post_meta:
                stats["skipped_nopost"] += 1
                continue
            seen.add(uid)

            parent_raw = pick(row, FIELD_ALIASES["parent_unit_id"], mark_used=used)
            parent = strip_prefixes(parent_raw, "t3_", "t1_")
            is_root = (not parent_raw) or parent_raw.startswith("t3_")

            meta = post_meta.get(thread_id, {})
            url = pick(row, FIELD_ALIASES["url"], mark_used=used)
            # 渠道优先级：行内列 → 帖子元数据 → 从 URL 推导。
            # 行内优先很重要 —— "淘宝式导出"里 url 常整列缺失，只靠它会让 channel 全空。
            channel = (pick(row, FIELD_ALIASES["channel"], mark_used=used)
                       or meta.get("channel", "")
                       or _subreddit_from_url(url))

            created_raw = pick(row, FIELD_ALIASES["created_at"], mark_used=used)
            created = epoch_to_iso(created_raw) if re.fullmatch(r"\s*[\d.]+\s*", created_raw or "") else created_raw

            extra: dict[str, Any] = {"is_root_comment": "True" if is_root else "False"}
            if source_note:
                extra["source_note"] = source_note
            for k, v in row.items():
                key = str(k).strip()
                if key in used or key.lower() in {"", "nan"} or key in NATIVE:
                    continue
                s = collapse_ws(v)
                if s:
                    extra[key] = s

            units.append(Unit(
                unit_id=uid,
                source=SOURCE_REDDIT,
                source_type=TYPE_COMMENT,
                thread_id=thread_id,
                parent_unit_id="" if is_root else parent,   # 根评论无父单元
                title=meta.get("title", ""),
                text=text,
                author=pick(row, FIELD_ALIASES["author"], mark_used=used),
                created_at=created,
                channel=channel,
                url=url,
                helpful_votes=to_int(pick(row, FIELD_ALIASES["helpful_votes"], mark_used=used)),
                extra=extra,
            ).normalize())

        self.last_stats = stats
        return units


def _subreddit_from_url(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"reddit\.com/r/([A-Za-z0-9_]+)", url, re.I)
    if m:
        return m.group(1)
    m = re.match(r"\s*/?r/([A-Za-z0-9_]+)", url, re.I)
    return m.group(1) if m else ""
