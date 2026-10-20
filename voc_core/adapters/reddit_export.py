#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reddit 批量导出适配器（帖子 + 评论混装）。

第三方"关键词搜索导出"（如 ``批量导出_13617条.xlsx``，12 列
``author, authorUrl, content, date, likes, dislikes, unvoted, url, id,
parentId, type, postId``）**把帖子和评论放在同一个文件里**，靠 ``type`` 列区分：

* ``type=post``    → 帖子。``content`` 就是标题（长内容时标题后直接拼接了正文，
  按首个句末标点切开，前半存 ``title``、后半存 ``text``）。
* ``type=comment`` → 评论。``postId`` 指向所属帖子，``id`` 形如 ``t1_xxxx``。

**为什么必须单独有这么一个适配器**：``reddit_comments`` 只取评论，
遇到这种文件会把 ``type=post`` 的行**静默丢掉**——用户以为导入成功，
实际帖子和它们的正文全没了。形态识别（看 ``type`` 列的**值**，而不只是列名）
是这个适配器与 ``reddit_comments`` 的分水岭。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..io_utils import collapse_ws, pick, strip_prefixes
from ..schema import SOURCE_REDDIT, Unit
from .base import Adapter
from .reddit_comments import FIELD_ALIASES as COMMENT_ALIASES
from .reddit_comments import RedditCommentsAdapter
from .reddit_posts import _ensure_reddit_url, _subreddit_from_url

#: type 列的别名
TYPE_ALIASES = ["type", "kind", "item_type", "类型"]
#: 帖子形态的取值
POST_TYPE_VALUES = {"post", "t3", "submission", "link"}
COMMENT_TYPE_VALUES = {"comment", "t1"}


class RedditExportAdapter(Adapter):
    adapter_id = "reddit_export"
    source = SOURCE_REDDIT
    label = "Reddit 批量导出（帖子+评论混装）"
    produces = "unit_type=post 与 comment 混合（同一文件按 type 列拆分）"

    # -- 识别 -------------------------------------------------------------

    def detect(self, path: Path) -> float:
        cols = self.sniff_columns(path)
        name = self.filename_hint(path)
        if not cols:
            return 0.0

        score = 0.0
        if "postid" in cols:
            score += 0.35
        if "parentid" in cols:
            score += 0.25
        if cols & {"content", "body", "text"}:
            score += 0.1
        # 关键判据：type 列的值里真的有 post —— 说明是混装导出
        has_post_rows = bool(self.sniff_values(path, TYPE_ALIASES) & POST_TYPE_VALUES)
        if has_post_rows:
            score += 0.3
        if "批量导出" in name or "reddit" in name:
            score += 0.2
        return max(0.0, min(score, 1.0))

    # -- 归一化 -----------------------------------------------------------

    def normalize(self, path: Path, **opts: Any) -> list[Unit]:
        rows = self.read_source(path)
        return self.normalize_rows(
            rows,
            default_channel=str(opts.get("default_subreddit", opts.get("default_channel", ""))),
            source_note=str(opts.get("source_note", "")),
        )

    def normalize_rows(
        self,
        rows: list[dict[str, Any]],
        *,
        default_channel: str = "",
        source_note: str = "",
    ) -> list[Unit]:
        post_rows: list[dict[str, Any]] = []
        comment_rows: list[dict[str, Any]] = []

        for row in rows:
            t = pick(row, TYPE_ALIASES).strip().lower()
            if t in COMMENT_TYPE_VALUES:
                comment_rows.append(row)
            elif t in POST_TYPE_VALUES:
                post_rows.append(row)
            else:
                # 没写 type 的行：有 parentId/postId 的当评论，否则当帖子
                if pick(row, COMMENT_ALIASES["parent_unit_id"]):
                    comment_rows.append(row)
                else:
                    post_rows.append(row)

        # 先出帖子（评论的 title / channel 要靠它们补齐）
        posts = self._normalize_posts(post_rows, default_channel, source_note)

        post_meta = {
            p.unit_id: {
                "title": p.title,
                "channel": p.channel,
                "text": p.text,
            }
            for p in posts
        }

        comments = RedditCommentsAdapter().normalize_rows(
            comment_rows, post_meta=post_meta, source_note=source_note,
        )
        # 评论标题从所属帖子补齐（原文件不重复存标题）
        for c in comments:
            if not c.title:
                c.title = post_meta.get(c.thread_id, {}).get("title", "")
            if not c.channel:
                c.channel = post_meta.get(c.thread_id, {}).get("channel", "")

        self.last_stats = {
            "rows": len(rows),
            "posts": len(posts),
            "comments": len(comments),
        }
        return posts + comments

    # -- 帖子行 → Unit -----------------------------------------------------

    def _normalize_posts(
        self,
        rows: list[dict[str, Any]],
        default_channel: str,
        source_note: str,
    ) -> list[Unit]:
        out: list[Unit] = []
        seen: set[str] = set()

        for row in rows:
            used: set[str] = set()
            raw_content = collapse_ws(pick(row, COMMENT_ALIASES["text"], mark_used=used))
            if not raw_content:
                continue

            # content 是"标题（+可能拼接的正文）"，按首个句末标点切开
            title, body = _split_title_body(raw_content)

            pid = strip_prefixes(pick(row, COMMENT_ALIASES["thread_id"], mark_used=used)
                                 or pick(row, COMMENT_ALIASES["unit_id"], mark_used=used), "t3_", "t1_")
            if not pid:
                pid = "p_" + str(abs(hash(raw_content[:80])) % (10 ** 12))
            if pid in seen:
                continue
            seen.add(pid)

            url = pick(row, COMMENT_ALIASES["url"], mark_used=used)
            channel = _subreddit_from_url(url) or default_channel

            extra: dict[str, Any] = {}
            if source_note:
                extra["source_note"] = source_note
            for k, v in row.items():
                key = str(k).strip()
                if key in used or key.lower() in {"", "nan"}:
                    continue
                s = collapse_ws(v)
                if s:
                    extra[key] = s

            out.append(Unit(
                unit_id=pid,
                source=SOURCE_REDDIT,
                source_type="post",
                thread_id=pid,
                title=title,
                text=body,
                author=pick(row, COMMENT_ALIASES["author"], mark_used=used),
                created_at=collapse_ws(pick(row, COMMENT_ALIASES["created_at"], mark_used=used)),
                channel=channel,
                url=_ensure_reddit_url(url, channel, pid),
                helpful_votes=None,
                extra=extra,
            ).normalize())

        return out


#: 句末标点（中英）
_SENTENCE_END = re.compile(r"[.!?。！？]")


def _split_title_body(content: str) -> tuple[str, str]:
    """把 ``content`` 拆成标题与正文。

    第三方导出把标题和正文拼在一个字段里，中间没有分隔符。启发式：在**前 120 个
    字符内**找第一个句末标点，之前作为标题、之后作为正文；找不到就整体当标题
    （绝大多数 Reddit 标题没有句末标点，且长度有限）。
    """
    if not content:
        return "", ""
    window = content[:120]
    m = _SENTENCE_END.search(window)
    if m and m.end() < len(content) - 1:
        title = content[: m.end()].strip()
        body = content[m.end():].strip()
        # 标题过长则说明这不是"标题+正文"而是纯正文，不做拆分
        if len(title) <= 120:
            return title, body
    return content, ""
