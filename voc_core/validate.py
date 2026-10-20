#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 6 · 质量闸门（quality gate）。

把 ``reddit分析`` SKILL.md 里 20+ 条「人肉记住的经验」，变成每次运行都会跑的
可执行断言，产出 ``quality_report.json``。

**为什么需要它**：这套管线里绝大多数结论都不是算出来的，而是**被判定出来的**
—— 引文从原文里挑、标签由模型归、购买状态靠信号词判。这类环节不会抛异常，
只会**静默地错**。原技能把这些错误一条条写进 SKILL.md 让人记住，人换了、
模型换了、品类换了，记住的东西就失效了。闸门的意义是把「记住」变成「每次
都验一遍」。

设计原则
--------
1. **不抛异常**：校验器自己崩掉等于没有校验器。每个检查项单独 try/except，
   异常本身成为一条 fail finding。
2. **作用域显式**：不是所有 ``count`` 都需要证据（``purchase_status_distribution``
   是分布不是标签）。检查项用显式的维度名单，不做隐式猜测。
3. **坏数据必须被拦住并说清原因**：验收方式是**变异测试** —— 在好数据上逐个
   注错，断言恰好是那一条检查项翻红（见 ``tests/test_validate.py``）。
4. **口径可溯源**：每条 finding 带 ``source`` 字段，指向它在 SKILL.md 里的出处，
   这样翻红时能立刻读到当初为什么定这条规则。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voc_core.kernel_common import canonicalize_rows, guess_role, row_get  # noqa: E402
from voc_core.source_profile import get_profile  # noqa: E402

__all__ = [
    "CHECKS",
    "Context",
    "Finding",
    "audit",
    "build_context",
    "render_console",
    "run_checks",
    "SEV_ERROR",
    "SEV_WARN",
    "SEV_INFO",
]

SEV_ERROR = "error"
SEV_WARN = "warn"
SEV_INFO = "info"
SEV_ORDER = {SEV_ERROR: 0, SEV_WARN: 1, SEV_INFO: 2}

ST_PASS = "pass"
ST_FAIL = "fail"
ST_SKIP = "skip"

# --------------------------------------------------------------------------
# 契约常量（改契约时改这里，不要散在检查函数里）
# --------------------------------------------------------------------------

#: 有 ``count`` + ``label_cn`` + ``evidence_posts`` 的**标签维度**。
#: ``purchase_status_distribution`` 是分布（每段是计数不是标签），刻意不在列。
LABEL_DIMS: tuple[str, ...] = (
    "usage_scenarios",
    "pre_purchase_concerns",
    "post_purchase_experience",
    "unmet_needs",
    "user_personas_purchased",
    "user_personas_non_purchased",
    "brand_mentions",
)

#: 人群画像维度：证据必须带 ``unit_type``，评论证据必须带 ``post_reddit_id``。
PERSONA_DIMS: tuple[str, ...] = ("user_personas_purchased", "user_personas_non_purchased")

#: 体验类维度：证据不得是疑问句（"Any recommendations?" 不是体验陈述）。
EXPERIENCE_DIMS: tuple[str, ...] = ("post_purchase_experience", "unmet_needs")

#: 必须存在、否则看板整卡隐藏 / 报告缺披露的结构键。
REQUIRED_DIM_KEYS: tuple[str, ...] = ("purchase_status_distribution", "unit_counts")

FEATURE_REQUIRED: tuple[str, ...] = (
    "name", "reason", "total", "positive", "negative", "neutral", "satisfaction",
)

#: 历史字段名：用了它们看板「功能体验」页会整页空白且不报错。
FEATURE_LEGACY_NAMES: tuple[str, ...] = (
    "mention_count", "positive_count", "negative_count", "satisfaction_rate",
)

MATRIX_CELL_KEYS: tuple[str, ...] = ("total", "satisfaction", "evidence_posts")

#: ``label_cn`` 禁止出现的符号（SKILL.md 字段规则：禁括号、斜杠、特殊符号）。
LABEL_FORBIDDEN = re.compile(r"[（）()／/|｜\[\]【】{}<>《》#*~`\"']")

#: 机器统计出的维度（非模型归纳），豁免 label_cn 格式与 reason 要求。
#: 依据：品牌名是**专有名词**——``Carter's`` / ``Lowe's`` / ``McDonald's`` 自带撇号，
#: ``La-Z-Boy`` 自带连字符，``Crate & Barrel`` 自带 `&`；而 SKILL.md 那份字段规则
#: 是为「LLM 归纳出的中文标签」写的（防的是「家庭/亲子用户（为孩子做健康面包）」这种）。
#: 同理，``brand_mentions`` 由帖子 ``mentioned_brands`` 字段直接统计，没有 thoughts
#: 可供填 reason —— SKILL.md 的契约示例里它本来也不带 reason。
MACHINE_DIMS: frozenset[str] = frozenset({"brand_mentions"})

#: 只在某个源里成立的词。其他源的**可见输出**里出现即视为域名泄漏。
#: 这是 Stage 3「呈现边界」的可执行版 —— 新增源时往这里加一行。
SOURCE_EXCLUSIVE_VOCAB: dict[str, tuple[str, ...]] = {
    "reddit": ("子版块", "Subreddit", "subreddit", "Reddit", "reddit", "原帖", "RSS"),
}

#: 无日期占比超过此值 → 时间线不可用，必须在报告里显式声明。
DATE_MISSING_WARN = 0.80

#: 品牌在帖子级文本里的命中数低于讨论串口径的此比例 → 帖子级口径严重低估。
BRAND_THREAD_RATIO_WARN = 0.5

_SAMPLE_CAP = 5

_TS = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}")


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass
class Finding:
    """一条检查结果。``status=skip`` 表示输入缺失（不算通过也不算失败）。"""

    check: str
    title: str
    severity: str
    status: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    @property
    def failed(self) -> bool:
        return self.status == ST_FAIL

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "title": self.title,
            "severity": self.severity,
            "status": self.status,
            "message": self.message,
            "detail": self.detail,
            "source": self.source,
        }


@dataclass
class Context:
    """一份被审计的产物集合。所有字段都可缺省 —— 缺什么就跳过什么检查。"""

    stats: dict[str, Any] = field(default_factory=dict)
    dimensions: dict[str, Any] = field(default_factory=dict)
    features: dict[str, Any] = field(default_factory=dict)
    tagged: list[dict] = field(default_factory=list)
    relevant: list[dict] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)
    report_text: str = ""
    dashboard_html: str = ""
    profile_name: str = ""
    #: ``--frame`` 兜底：产物里没有 ``analysis_frame`` 块（改造前产物）时才用得上。
    frame_name: str = ""
    #: 引文回溯的容错上限（比例）。默认 0 —— SKILL.md 写的是「必须逐字」。
    max_quote_miss_rate: float = 0.0
    paths: dict[str, str] = field(default_factory=dict)

    _unit_text: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _threads: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _built: bool = field(default=False, init=False, repr=False)
    #: 懒加载的分析框架（Stage 5）。None 表示「还没解析过」。
    _frame: Any = field(default=None, init=False, repr=False)

    # -- 语料索引 ----------------------------------------------------------

    def build_index(self) -> None:
        """建两套索引，供「引文逐字回溯」使用。

        * ``unit_text[unit_id]`` —— 单个单元自己的文本（帖子正文 / 评论正文）
        * ``threads[post_id]``  —— **讨论串**：帖子正文 + 该帖下所有评论

        ⚠️ 回溯必须用**讨论串**，不能用单元自身文本。实测（nursery，3343 条引文）：
        只查帖子正文时命中率 **1.8%**，查讨论串时 **100%**。原因是管线的口径就是
        「讨论串」—— 非画像维度的命中常发生在评论里，而证据仍挂在帖子单元上
        （SKILL.md Phase 2' 的「讨论串」口径）。按字面实现这条规则会把 98% 的
        正确证据判红，闸门就废了。
        """
        if self._built:
            return
        unit = self._unit_text
        threads: dict[str, list[str]] = defaultdict(list)

        for row in (*self.tagged, *self.relevant):
            uid = row_get(row, "reddit_id")
            if not uid:
                continue
            txt = "\n".join(filter(None, [row_get(row, "title"),
                                          row_get(row, "summary_text")]))
            unit[uid] = txt
            threads[uid].append(txt)

        for row in self.comments:
            cid = row_get(row, "comment_id")
            pid = row_get(row, "post_reddit_id")
            body = row_get(row, "body")
            if cid:
                unit[cid] = body
            if pid:
                threads[pid].append(body)

        self._threads = {k: _norm("\n".join(v)) for k, v in threads.items()}
        self._unit_text = {k: _norm(v) for k, v in unit.items()}
        self._built = True

    def haystack_for(self, reddit_id: str, unit_type: str,
                     post_reddit_id: str = "") -> str:
        """取一条证据可被回溯的文本范围（讨论串优先）。"""
        self.build_index()
        if unit_type == "comment" and post_reddit_id:
            return self._threads.get(post_reddit_id) or self._unit_text.get(reddit_id, "")
        return self._threads.get(reddit_id) or self._unit_text.get(reddit_id, "")

    @property
    def thread_index(self) -> dict[str, str]:
        self.build_index()
        return self._threads

    @property
    def unit_index(self) -> dict[str, str]:
        self.build_index()
        return self._unit_text

    def frame(self):
        """分析框架（Stage 5）：从 dimensions.json 的 analysis_frame 块还原。

        老产物没有这个块 → 内置 consumer，于是所有派生量与改造前的硬编码常量相同，
        老产物照样能过闸门。
        """
        if self._frame is None:
            from voc_core.analysis_frame import frame_from_product
            from voc_core.kernel_common import load_frame
            # 产物自描述优先；旧产物（没有 analysis_frame 块）才用 --frame；
            # 都没有则内置 consumer —— 与改造前的硬编码常量逐项相同。
            self._frame = (frame_from_product(self.dimensions)
                           or load_frame(self.frame_name or None))
        return self._frame

    def axis(self):
        return self.frame().axis

    def persona_dims(self) -> tuple[str, ...]:
        """人群画像维度清单（框架里声明了 persona_bucket 的维度）。"""
        return tuple(self.frame().persona_dim_ids()) or PERSONA_DIMS

    def axis_dist_key(self) -> str:
        """分段轴分布在 dimensions.json 里的键名。"""
        ax = self.axis()
        return (ax.distribution_key if ax else "") or "purchase_status_distribution"

    def axis_active_bucket_ids(self) -> tuple[str, ...]:
        """非排除桶的 id（分布图里允许出现的 status 取值）。"""
        ax = self.axis()
        if ax is None:
            return ("purchased", "non_purchased")
        return tuple(b.id for b in ax.active_buckets)

    def axis_label(self) -> str:
        ax = self.axis()
        return (ax.label if ax else "") or "购买状态"

    def label_dims(self) -> list[str]:
        """实际存在的标签维度：名单内的 + 任何符合形态的未知维度。

        **框架声明的分段轴分布键**（consumer 的 ``purchase_status_distribution``、
        B2B 的 ``decision_role_distribution``）不算标签维度 —— 它是桶计数的披露表，
        条目本来就没有 ``reason``。这个键必须 ``self.axis_dist_key()`` 动态排除：
        写死在常量里的话，换个框架它就会掉进下面的「未知维度」兜底分支，
        平白被要求给每条计数附着分类依据。
        """
        found = [k for k in LABEL_DIMS if isinstance(self.dimensions.get(k), list)]
        known = (set(LABEL_DIMS) | set(REQUIRED_DIM_KEYS)
                 | {self.axis_dist_key()}
                 | {"category", "generated_at", "analysis_frame"})
        for k, v in self.dimensions.items():
            if k in known or not isinstance(v, list) or not v:
                continue
            if all(isinstance(x, dict) for x in v) and any("count" in x for x in v):
                found.append(k)
        return found


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    severity: str
    source: str
    fn: Callable[[Context], tuple[str, str, dict]]
    needs: tuple[str, ...] = ()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _ok(msg: str, **detail: Any) -> tuple[str, str, dict]:
    return ST_PASS, msg, detail


def _bad(msg: str, **detail: Any) -> tuple[str, str, dict]:
    return ST_FAIL, msg, detail


def _skip(msg: str, **detail: Any) -> tuple[str, str, dict]:
    return ST_SKIP, msg, detail


def _cap(items: Sequence[Any]) -> list[Any]:
    return list(items[:_SAMPLE_CAP])


def _pct(n: float, d: float) -> str:
    return f"{n / d:.1%}" if d else "0.0%"


def _read_csv(path: Path) -> list[dict]:
    """读 CSV 并补上内核规范键（任意源列名都能被检查项读到）。"""
    if not path or not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return []
    return canonicalize_rows(rows, guess_role(list(rows[0].keys())))


def _visible(text: str) -> str:
    """剥掉 ``<script>`` / ``<style>`` —— 内嵌数据与 JS 注释不是用户可见文案。"""
    return re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", text or "", flags=re.S | re.I)


def _visible_text(html: str) -> str:
    """**真正的用户可见文本**：先剥脚本样式，再剥掉所有标签（含属性）。

    ⚠️ 必须连属性一起剥。``<div id="chart-subreddits-small">`` 里的
    ``subreddits`` 是 DOM 标识符 —— 用户看不到，不该按"文案泄漏"判红。
    """
    h = _visible(html)
    return re.sub(r"<[^>]*>", " ", h)


def _markup_tokens(html: str) -> str:
    """标签与属性区（DOM id / class 等机器标识符）。"""
    h = _visible(html)
    return " ".join(re.findall(r"<[^>]*>", h))


# ==========================================================================
# 一、数据层：清洗产物自洽
# ==========================================================================

def check_dup_unit_ids(ctx: Context) -> tuple[str, str, dict]:
    """同 id 重复行 —— 「755 行实际只有 490 帖」。不去重所有计数成倍虚高。"""
    rows = ctx.tagged or ctx.relevant
    if not rows:
        return _skip("没有可检查的清洗产物")
    ids = [row_get(r, "reddit_id") for r in rows]
    dup = {k: v for k, v in Counter(i for i in ids if i).items() if v > 1}
    if dup:
        return _bad(
            f"{len(dup)} 个 id 出现多次（共 {sum(dup.values()) - len(dup)} 行冗余）"
            "—— 计数、图表与证据都会成倍虚高",
            duplicates=_cap(sorted(dup.items(), key=lambda kv: -kv[1])),
            rows=len(rows), unique=len(set(ids)),
        )
    return _ok(f"{len(rows)} 行 / {len(set(ids))} 个唯一 id，无重复", rows=len(rows))


def check_bucket_accounting(ctx: Context) -> tuple[str, str, dict]:
    """分桶账目自洽：原始 ≥ 清洗后 ≥ A+B，且 A+B+C+D == 清洗后。"""
    st = ctx.stats
    if not st:
        return _skip("没有 stats.json")
    need = ("raw_total", "expired_count", "after_date_clean",
            "bucket_A", "bucket_B", "bucket_C", "bucket_D", "final_analysis")
    missing = [k for k in need if k not in st]
    if missing:
        return _bad(f"stats.json 缺少统计键：{missing}", missing=missing)
    buckets = st["bucket_A"] + st["bucket_B"] + st["bucket_C"] + st["bucket_D"]
    problems = []
    if st["raw_total"] < st["after_date_clean"]:
        problems.append(f"raw_total({st['raw_total']}) < after_date_clean({st['after_date_clean']})")
    if st["after_date_clean"] != buckets:
        problems.append(f"A+B+C+D={buckets} != after_date_clean({st['after_date_clean']})")
    if st["final_analysis"] != st["bucket_A"] + st["bucket_B"]:
        problems.append(
            f"final_analysis({st['final_analysis']}) != A+B({st['bucket_A'] + st['bucket_B']})")
    if problems:
        return _bad("分桶账目不平：" + "；".join(problems), problems=problems)
    return _ok(f"raw {st['raw_total']} → 清洗 {st['after_date_clean']} → 分析 "
               f"{st['final_analysis']}（A{st['bucket_A']}/B{st['bucket_B']}/"
               f"C{st['bucket_C']}/D{st['bucket_D']}）")


def check_date_missing_rate(ctx: Context) -> tuple[str, str, dict]:
    """日期缺失率 > 80% → 时间线不可用，报告必须显式声明。"""
    rows = ctx.tagged or ctx.relevant
    if not rows:
        return _skip("没有可检查的清洗产物")
    undated = sum(1 for r in rows if not row_get(r, "published"))
    rate = undated / len(rows)
    if rate > DATE_MISSING_WARN:
        return _bad(
            f"无日期占比 {rate:.1%}（{undated}/{len(rows)}）超过 {DATE_MISSING_WARN:.0%}"
            "—— 帖子时间线不可用，必须在报告中显式声明时间维度不可用，"
            "否则读者会把空时间线读成结论",
            undated=undated, rows=len(rows), rate=round(rate, 4),
            threshold=DATE_MISSING_WARN)
    return _ok(f"无日期 {undated}/{len(rows)} = {rate:.1%}，时间维度可用",
               rate=round(rate, 4))


def check_control_chars(ctx: Context) -> tuple[str, str, dict]:
    """控制字符污染 —— 典型是 ``\\b`` 被工具参数吞成退格符 U+0008，正则永不命中。"""
    hits: list[tuple[str, str, str]] = []
    for label, rows in (("tagged", ctx.tagged), ("comments", ctx.comments)):
        for i, row in enumerate(rows):
            for k, v in row.items():
                for ch in str(v):
                    if ord(ch) < 32 and ch not in "\t\n\r":
                        hits.append((label, f"#{i}:{k}", f"U+{ord(ch):04X}"))
    if hits:
        return _bad(f"{len(hits)} 处控制字符污染（最可能是 \\b 被吞成退格符）",
                    offenders=_cap(hits), total=len(hits))
    return _ok("清洗产物无控制字符污染")


def _brand_measurements(ctx: Context) -> dict[str, Any] | None:
    """品牌口径的三个对照量。返回 ``None`` 表示前置条件不足。

    * ``threads_with_brand``：语料中含任一品牌词的**讨论串**数（真值下界）
    * ``col_hits``：``mentioned_brands`` 列（帖级中间信号）命中的帖数；
      取 **relevant 优先**（下游读的就是它，见下方注释）
    * ``reported``：交付进报告的品牌计数之和（``dimensions.brand_mentions``）
    """
    idx = ctx.thread_index
    terms = [t for t in (ctx.stats.get("brand_terms") or []) if t]
    if not idx or not terms:
        return None
    lower = [t.lower() for t in terms]
    threads_with_brand = sum(
        1 for h in idx.values() if any(t in h.lower() for t in lower))

    # **优先 relevant**：下游统一读它（``aggregate_annotations`` 的 ``--relevant``
    # 是必填；``generate_dashboard`` 里写的是 ``relevant if relevant else tagged``），
    # 而 SKILL.md 说的「用讨论串重写 mentioned_brands」也发生在 relevant 上
    # （既有产物：tagged 5 帖=标题口径，relevant 133 帖=重写后）。判据必须和下游
    # 看同一个文件，否则会出现「闸门说某列陈旧、下游其实不读它」的假警报。
    col_hits = 0
    col_present = False
    for row in ctx.relevant or ctx.tagged:
        if not col_present and "mentioned_brands" in row:
            col_present = True
        blob = (row.get("mentioned_brands") or "").lower()
        if not blob:
            blob = _norm("\n".join(filter(None, [row_get(row, "title"),
                                                 row_get(row, "summary_text")]))).lower()
        if any(t in blob for t in lower):
            col_hits += 1

    items = ctx.dimensions.get("brand_mentions")
    items = items if isinstance(items, list) else []
    return {
        "threads_with_brand": threads_with_brand,
        "post_level_hits": col_hits,
        "mentioned_brands_column_present": col_present,
        "reported_brand_total": sum(int(b.get("count") or 0)
                                    for b in items if isinstance(b, dict)),
        "brands_reported": len(items),
        "distinct_evidence_units": len({
            (e.get("reddit_id") or "").strip()
            for b in items if isinstance(b, dict)
            for e in (b.get("evidence_posts") or [])
            if (e.get("reddit_id") or "").strip()}),
    }


def check_brand_undercount(ctx: Context) -> tuple[str, str, dict]:
    """交付出去的品牌数字是否被严重低估。

    不变量：每个品牌至少覆盖一条命中它的讨论串，故 ``Σcount ≥ 不重复讨论串数``。
    ``Σcount`` 更小 → 品牌章节必然低估。这是**交付口径**的检查。
    """
    m = _brand_measurements(ctx)
    if m is None:
        return _skip("缺少 brand_terms / 语料，无法比对品牌口径")
    if not m["brands_reported"]:
        return _skip("没有 dimensions.json（或其中没有 brand_mentions 章节），"
                     "无法检查交付口径", **m)
    if m["threads_with_brand"] and m["reported_brand_total"] < m["threads_with_brand"]:
        return _bad(
            f"品牌 Σcount={m['reported_brand_total']} 小于语料中命中品牌词的讨论串数 "
            f"{m['threads_with_brand']} —— 每个品牌至少覆盖一条命中它的讨论串，"
            "故和不可能更小；品牌章节被严重低估",
            **m)
    return _ok(f"品牌计数 Σcount={m['reported_brand_total']} 覆盖 {m['brands_reported']} 个品牌，"
               f"语料含品牌词讨论串 {m['threads_with_brand']} 条，交付口径正常", **m)


def check_brand_column_stale(ctx: Context) -> tuple[str, str, dict]:
    """``mentioned_brands`` 列是不是陈旧的地雷。

    该列若没在标注阶段用讨论串重写，就只反映帖子标题级信号。它**不是**交付口径，
    但下游脚本（或下一次跑批）会拿它统计品牌 —— SKILL.md 记的正是这个坑。
    """
    m = _brand_measurements(ctx)
    if m is None:
        return _skip("缺少 brand_terms / 语料，无法比对品牌口径")
    if not m["mentioned_brands_column_present"]:
        return _skip("清洗产物里没有 mentioned_brands 列", **m)
    t = m["threads_with_brand"]
    if t and m["post_level_hits"] * 2 < t:
        return _bad(
            f"`mentioned_brands` 列只覆盖 {m['post_level_hits']} 帖，讨论串口径覆盖 {t} 帖"
            f"（仅 {_pct(m['post_level_hits'], t)}）—— 该列陈旧，未按讨论串重写。"
            "谁拿它统计品牌谁就会严重低估（SKILL.md 2026-09-30 补充③）",
            ratio_threshold=BRAND_THREAD_RATIO_WARN, **m)
    return _ok(f"`mentioned_brands` 列覆盖 {m['post_level_hits']} 帖 / 讨论串 {t} 帖，"
               "帖级口径无系统性低估", **m)


# ==========================================================================
# 二、归属层：dimensions.json
# ==========================================================================

def check_evidence_quote_verbatim(ctx: Context) -> tuple[str, str, dict]:
    """引文逐字回溯 —— 硬规则①。回溯范围是**讨论串**（见 ``build_index``）。"""
    if not ctx.dimensions:
        return _skip("没有 dimensions.json")
    if not (ctx.tagged or ctx.relevant or ctx.comments):
        return _skip("没有语料 CSV，无法回溯引文")

    total = miss = 0
    offenders: list[dict] = []
    for dim in ctx.label_dims():
        for it in ctx.dimensions.get(dim) or []:
            if not isinstance(it, dict):
                continue
            for ev in it.get("evidence_posts") or []:
                q = ev.get("evidence_quote") or ""
                if not _norm(q):
                    continue
                total += 1
                uid = (ev.get("reddit_id") or "").strip()
                hay = ctx.haystack_for(uid, ev.get("unit_type") or "post",
                                       (ev.get("post_reddit_id") or "").strip())
                if _norm(q) in hay:
                    continue
                miss += 1
                if len(offenders) < _SAMPLE_CAP * 2:
                    offenders.append({
                        "dim": dim, "label": it.get("label_cn"), "reddit_id": uid,
                        "reason": "语料里没有该单元" if not hay else "文本对不上（疑被改写）",
                        "quote": _norm(q)[:120],
                    })
    if not total:
        return _skip("dimensions.json 里没有任何带 evidence_quote 的证据")
    rate = miss / total
    if rate > ctx.max_quote_miss_rate:
        return _bad(
            f"{miss}/{total} 条引文无法在讨论串里逐字命中（未命中率 {rate:.2%}，"
            f"上限 {ctx.max_quote_miss_rate:.2%}）—— 引文必须逐字取自原文，不得改写",
            total=total, missed=miss, miss_rate=round(rate, 4),
            limit=ctx.max_quote_miss_rate, offenders=_cap(offenders))
    return _ok(f"{total} 条引文全部可在讨论串里逐字命中", total=total, missed=miss)


def check_count_matches_evidence(ctx: Context) -> tuple[str, str, dict]:
    """``count`` 与证据条数对齐 —— 硬规则③。

    ``count < len(evidence)`` 逻辑上不可能（错）；``count > len(evidence)``
    是万级语料下允许的**抽样**（警告，需在看板标注）。
    """
    if not ctx.dimensions:
        return _skip("没有 dimensions.json")
    impossible: list[dict] = []
    sampled: list[dict] = []
    for dim in ctx.label_dims():
        for it in ctx.dimensions.get(dim) or []:
            if not isinstance(it, dict) or "count" not in it:
                continue
            c = it.get("count")
            n = len(it.get("evidence_posts") or [])
            rec = {"dim": dim, "label": it.get("label_cn"), "count": c, "evidence": n}
            if not isinstance(c, int) or c < n:
                impossible.append(rec)
            elif c > n:
                sampled.append(rec)
    if impossible:
        return _bad(
            f"{len(impossible)} 处 count 少于证据条数（逻辑不可能）—— "
            "图表数字与下钻列表会不一致",
            offenders=_cap(impossible), total=len(impossible))
    if sampled:
        return _skip(
            f"{len(sampled)} 处 count > 证据条数（抽样模式；看板须标注「下钻展示其中 N 条抽样证据」）",
            offenders=_cap(sampled))
    return _ok("所有标签维度 count == len(evidence_posts)，图表与下钻完全对齐")


def check_persona_split_sums(ctx: Context) -> tuple[str, str, dict]:
    """``post_count + comment_count == count`` —— 硬规则④（看板堆叠段不错位）。"""
    if not ctx.dimensions:
        return _skip("没有 dimensions.json")
    dims = [d for d in ctx.persona_dims() if d in ctx.dimensions]
    if not dims:
        return _skip("dimensions.json 里没有人群画像维度")
    bad: list[dict] = []
    for dim in dims:
        for it in ctx.dimensions.get(dim) or []:
            p, c, t = it.get("post_count"), it.get("comment_count"), it.get("count")
            if p is None or c is None:
                bad.append({"dim": dim, "label": it.get("label_cn"),
                            "why": "缺 post_count / comment_count"})
            elif p + c != t:
                bad.append({"dim": dim, "label": it.get("label_cn"),
                            "why": f"{p} + {c} != {t}"})
    if bad:
        return _bad(f"{len(bad)} 处画像条目 post_count + comment_count != count",
                    offenders=_cap(bad), total=len(bad))
    return _ok(f"{len(dims)} 个画像维度全部满足 post_count + comment_count == count")


def check_brand_sentiment_sums(ctx: Context) -> tuple[str, str, dict]:
    """品牌情感分布之和 == count —— 硬规则④。"""
    if not ctx.dimensions:
        return _skip("没有 dimensions.json")
    items = ctx.dimensions.get("brand_mentions")
    if not isinstance(items, list) or not items:
        return _skip("dimensions.json 里没有 brand_mentions")
    bad: list[dict] = []
    for it in items:
        sd = it.get("sentiment_distribution") or {}
        s = sum(v for v in sd.values() if isinstance(v, (int, float)))
        if s != it.get("count"):
            bad.append({"label": it.get("label_cn"), "count": it.get("count"),
                        "sentiment_sum": s, "distribution": sd})
    if bad:
        return _bad(f"{len(bad)} 个品牌的情感分布之和 != count",
                    offenders=_cap(bad), total=len(bad))
    return _ok(f"{len(items)} 个品牌的情感分布之和均等于 count")


def check_interrogative_evidence(ctx: Context) -> tuple[str, str, dict]:
    """疑问句混入体验证据 —— "Any recommendations?" 不是体验陈述。"""
    if not ctx.dimensions:
        return _skip("没有 dimensions.json")
    bad: list[dict] = []
    for dim in EXPERIENCE_DIMS:
        for it in ctx.dimensions.get(dim) or []:
            for ev in it.get("evidence_posts") or []:
                q = _norm(ev.get("evidence_quote") or "")
                if q.endswith(("?", "？")):
                    bad.append({"dim": dim, "label": it.get("label_cn"),
                                "reddit_id": ev.get("reddit_id"), "quote": q[:100]})
    if bad:
        return _bad(
            f"{len(bad)} 条体验 / 需求类证据是疑问句（以 ？ 结尾）—— "
            "下钻会展示出一堆「求推荐」，不是体验证据",
            offenders=_cap(bad), total=len(bad))
    return _ok("体验类与需求类证据均为陈述句")


def check_evidence_ids_and_unit_type(ctx: Context) -> tuple[str, str, dict]:
    """证据 id 不得为空；画像证据必须带 ``unit_type``，评论证据带 ``post_reddit_id``。"""
    if not ctx.dimensions:
        return _skip("没有 dimensions.json")
    no_id: list[dict] = []
    no_type: list[dict] = []
    no_parent: list[dict] = []
    for dim in ctx.label_dims():
        for it in ctx.dimensions.get(dim) or []:
            for ev in it.get("evidence_posts") or []:
                uid = (ev.get("reddit_id") or "").strip()
                rec = {"dim": dim, "label": it.get("label_cn"),
                       "quote": _norm(ev.get("evidence_quote") or "")[:60]}
                if not uid:
                    no_id.append(rec)          # 空 id 会被看板直接跳过（证据卡消失）
                    continue
                if dim not in ctx.persona_dims():
                    continue
                ut = ev.get("unit_type")
                if ut not in ("post", "comment"):
                    no_type.append({**rec, "reddit_id": uid, "unit_type": ut})
                elif ut == "comment" and not (ev.get("post_reddit_id") or "").strip():
                    no_parent.append({**rec, "reddit_id": uid})
    problems = []
    if no_id:
        problems.append(f"{len(no_id)} 条证据 reddit_id 为空（看板会静默跳过该证据卡）")
    if no_type:
        problems.append(f"{len(no_type)} 条画像证据缺 unit_type（看板堆叠段错位）")
    if no_parent:
        problems.append(f"{len(no_parent)} 条评论证据缺 post_reddit_id（评论卡无法回链原帖）")
    if problems:
        return _bad("；".join(problems), empty_id=_cap(no_id),
                    missing_unit_type=_cap(no_type), missing_parent=_cap(no_parent))
    return _ok("证据 id 与 unit_type / post_reddit_id 齐备")


def check_required_structure(ctx: Context) -> tuple[str, str, dict]:
    """分段轴分布键 + ``unit_counts.excluded_unknown_units`` 必须存在。

    分布键名跟着框架走（消费品 ``purchase_status_distribution``、
    B2B ``decision_role_distribution``）；``excluded_unknown_units`` 则是**产物契约键**，
    换框架也不改名 —— 它只表示「进了被排除桶、因而没被任何画像桶统计」的单元数。
    """
    if not ctx.dimensions:
        return _skip("没有 dimensions.json")
    dist_key = ctx.axis_dist_key()
    required = (dist_key, "unit_counts")
    missing = [k for k in required if k not in ctx.dimensions]
    uc = ctx.dimensions.get("unit_counts") or {}
    if "unit_counts" in ctx.dimensions and "excluded_unknown_units" not in uc:
        missing.append("unit_counts.excluded_unknown_units")
    if missing:
        return _bad(
            "缺少结构键：" + "、".join(missing)
            + f" —— 缺前者{ctx.axis_label()}环形图整卡隐藏，缺后者报告没有「已排除 N 个单元」的披露",
            missing=missing)
    psd = ctx.dimensions.get(dist_key) or []
    statuses = [s.get("status") for s in psd if isinstance(s, dict)]
    allowed = ctx.axis_active_bucket_ids()
    extra = [s for s in statuses if s not in allowed]
    if extra:
        return _bad(f"{dist_key} 出现非分布桶状态：{extra}（允许：{'、'.join(allowed)}）",
                    statuses=statuses)
    return _ok(f"结构键齐备（{ctx.axis_label()} {len(psd)} 段，"
               f"已排除未知单元 {uc.get('excluded_unknown_units')}）")


def check_label_cn_format(ctx: Context) -> tuple[str, str, dict]:
    """``label_cn`` 禁括号 / 斜杠 / 特殊符号；``reason`` 必填。

    机器统计维度（``brand_mentions``）豁免：品牌是专有名词，格式与 reason 都不适用。
    """
    if not ctx.dimensions:
        return _skip("没有 dimensions.json")
    bad_label: list[dict] = []
    no_reason: list[dict] = []
    for dim in ctx.label_dims():
        if dim in MACHINE_DIMS:
            continue
        for it in ctx.dimensions.get(dim) or []:
            lc = it.get("label_cn") or ""
            if not lc.strip():
                bad_label.append({"dim": dim, "label_cn": lc, "why": "为空"})
            elif LABEL_FORBIDDEN.search(lc):
                bad_label.append({"dim": dim, "label_cn": lc, "why": "含禁止符号"})
            if not (it.get("reason") or "").strip():
                no_reason.append({"dim": dim, "label_cn": lc})
    problems = []
    if bad_label:
        problems.append(f"{len(bad_label)} 个 label_cn 含禁止符号或为空")
    if no_reason:
        problems.append(f"{len(no_reason)} 个标签缺 reason（报告里每条结论都要有归类依据）")
    if problems:
        return _bad("；".join(problems), bad_labels=_cap(bad_label),
                    missing_reason=_cap(no_reason))
    return _ok("label_cn 格式合规，reason 齐备")


def check_percentage_range(ctx: Context) -> tuple[str, str, dict]:
    """``percentage`` 契约是 0–100。曾对 0<v<=1 再 ×100 → 0.8% 显示成 80.0%。"""
    if not ctx.dimensions:
        return _skip("没有 dimensions.json")
    bad: list[dict] = []
    for dim in ctx.label_dims():
        for it in ctx.dimensions.get(dim) or []:
            p = it.get("percentage")
            if p is None:
                continue
            if not isinstance(p, (int, float)) or not (0 <= p <= 100):
                bad.append({"dim": dim, "label": it.get("label_cn"), "percentage": p})
    if bad:
        return _bad(f"{len(bad)} 处 percentage 越界（契约是 0–100，不要启发式 ×100）",
                    offenders=_cap(bad), total=len(bad))
    return _ok("percentage 全部落在 [0, 100]")


# ==========================================================================
# 三、功能层：features.json
# ==========================================================================

def check_feature_contract(ctx: Context) -> tuple[str, str, dict]:
    """功能必填字段齐备，且不得使用历史字段名。"""
    if not ctx.features:
        return _skip("没有 features.json")
    feats = ctx.features.get("features")
    if not isinstance(feats, list) or not feats:
        return _bad("features.json 缺少 features 数组（看板功能体验页会整页空白）")
    missing: list[dict] = []
    legacy: list[dict] = []
    for i, f in enumerate(feats):
        if not isinstance(f, dict):
            missing.append({"index": i, "why": "不是对象"})
            continue
        gaps = [k for k in FEATURE_REQUIRED if k not in f]
        if gaps:
            missing.append({"index": i, "name": f.get("name"), "missing": gaps})
        olds = [k for k in FEATURE_LEGACY_NAMES if k in f]
        if olds:
            legacy.append({"index": i, "name": f.get("name"), "legacy_fields": olds})
    problems = []
    if missing:
        problems.append(f"{len(missing)} 个功能条目缺必填字段")
    if legacy:
        problems.append(
            f"{len(legacy)} 个功能条目用了历史字段名（会导致「功能体验」页整页空白而不报错）")
    if problems:
        return _bad("；".join(problems), missing_fields=_cap(missing),
                    legacy_fields=_cap(legacy))
    return _ok(f"{len(feats)} 个功能条目字段合规")


def check_feature_satisfaction(ctx: Context) -> tuple[str, str, dict]:
    """``satisfaction == positive / (positive + negative + neutral)`` 且 ∈ [0,1] 。"""
    if not ctx.features:
        return _skip("没有 features.json")
    feats = ctx.features.get("features")
    if not isinstance(feats, list) or not feats:
        return _skip("没有可检查的功能条目")
    bad: list[dict] = []
    over: list[dict] = []
    for f in feats:
        if not isinstance(f, dict):
            continue
        s = f.get("satisfaction")
        p, n, nu = (f.get("positive") or 0), (f.get("negative") or 0), (f.get("neutral") or 0)
        if s is not None:
            if not isinstance(s, (int, float)) or not (0 <= s <= 1):
                bad.append({"name": f.get("name"), "why": f"satisfaction 越界：{s}"})
                continue
            denom = p + n + nu
            if denom:
                calc = p / denom
                if abs(s - calc) > 1e-6:
                    bad.append({"name": f.get("name"),
                                "why": f"satisfaction={s:.6f} != {p}/({p}+{n}+{nu})={calc:.6f}"})
        if isinstance(f.get("total"), (int, float)) and f["total"] < p + n + nu:
            over.append({"name": f.get("name"), "total": f.get("total"),
                         "sentiment_sum": p + n + nu})
    if bad:
        return _bad(f"{len(bad)} 个功能的满意度违反契约（0–1 且 = 正面/有情感命中）",
                    offenders=_cap(bad), total=len(bad))
    if over:
        return _bad(
            f"{len(over)} 个功能的 positive+negative+neutral 超过 total —— "
            "total 是提及次数、情感计数是「帖 × 标签」，前者不应更小",
            offenders=_cap(over))
    return _ok("功能满意度全部满足 0–1 且公式自洽")


def check_feature_matrix(ctx: Context) -> tuple[str, str, dict]:
    """两个矩阵的单元格结构：``{total, satisfaction, evidence_posts}``。"""
    if not ctx.features:
        return _skip("没有 features.json")
    problems: list[dict] = []
    checked = 0
    for key in ("brand_feature_matrix", "persona_feature_matrix"):
        m = ctx.features.get(key)
        if not isinstance(m, dict) or not m:
            continue
        for row, cells in m.items():
            if not isinstance(cells, dict):
                problems.append({"matrix": key, "row": row, "why": "行不是对象"})
                continue
            for fn, cell in cells.items():
                checked += 1
                if not isinstance(cell, dict):
                    problems.append({"matrix": key, "row": row, "feature": fn,
                                     "why": "单元格不是对象"})
                    continue
                gaps = [k for k in MATRIX_CELL_KEYS if k not in cell]
                if gaps:
                    problems.append({"matrix": key, "row": row, "feature": fn,
                                     "missing": gaps})
                    continue
                s = cell.get("satisfaction")
                if s is not None and (not isinstance(s, (int, float)) or not (0 <= s <= 1)):
                    problems.append({"matrix": key, "row": row, "feature": fn,
                                     "why": f"satisfaction 越界：{s}"})
    if not checked and not problems:
        return _skip("features.json 里没有矩阵（看板对应卡片会自动隐藏）")
    if problems:
        return _bad(f"{len(problems)}/{checked} 个矩阵单元格结构不合契约"
                    "（缺字段会让热力图空白）",
                    offenders=_cap(problems), total=len(problems), checked=checked)
    return _ok(f"{checked} 个矩阵单元格结构合规")


# ==========================================================================
# 四、呈现层
# ==========================================================================

def _scan_vocab(text: str, src: str) -> list[dict]:
    leaks: list[dict] = []
    for other, words in SOURCE_EXCLUSIVE_VOCAB.items():
        if other == src:
            continue
        for w in words:
            n = text.count(w)
            if n:
                i = text.find(w)
                leaks.append({"word": w, "from": other, "count": n,
                              "context": _norm(text[max(0, i - 60):i + 60])})
    return leaks


def check_presentation_vocabulary(ctx: Context) -> tuple[str, str, dict]:
    """**用户可见文案**里不得残留其他源的专属词（Stage 3 呈现边界的自动版）。"""
    src = (ctx.profile_name or "").strip().lower()
    if not src:
        return _skip("未指定源画像（--profile），跳过词汇泄漏检查")
    if not (ctx.dashboard_html or ctx.report_text):
        return _skip("没有提供看板 / 报告文本")
    visible = _visible_text(ctx.dashboard_html) + "\n" + (ctx.report_text or "")
    leaks = _scan_vocab(visible, src)
    if leaks:
        return _bad(
            f"用户可见文案残留 {len(leaks)} 个其他源的专属词（源画像 = {src}）—— "
            "文案层没走 SourceProfile 注入",
            offenders=_cap(leaks), total=len(leaks), profile=src)
    return _ok(f"源画像 {src} 的可见文案无其他源词汇泄漏")


def check_presentation_identifiers(ctx: Context) -> tuple[str, str, dict]:
    """DOM id / class 等**机器标识符**里残留旧源词汇 —— 用户看不到，但是模板没洗干净的痕迹。"""
    src = (ctx.profile_name or "").strip().lower()
    if not src:
        return _skip("未指定源画像（--profile），跳过标识符检查")
    if not ctx.dashboard_html:
        return _skip("没有提供看板 HTML")
    leaks = _scan_vocab(_markup_tokens(ctx.dashboard_html), src)
    if leaks:
        return _bad(
            f"看板的 DOM 标识符里残留 {len(leaks)} 个旧源词汇（源画像 = {src}）"
            "—— 不污染用户可见文案，但说明模板仍带着源专属 id，换源时容易踩到",
            offenders=_cap(leaks), total=len(leaks), profile=src)
    return _ok(f"看板 DOM 标识符无旧源词汇残留（源画像 = {src}）")


def check_presentation_placeholders(ctx: Context) -> tuple[str, str, dict]:
    """未替换的模板占位符 / 空链接 —— 典型的静默失败。"""
    if not (ctx.dashboard_html or ctx.report_text):
        return _skip("没有提供看板 / 报告文本")
    visible = _visible_text(ctx.dashboard_html)
    text = ctx.report_text or ""
    needle = visible + "\n" + text
    patterns = {
        "未替换占位符": re.compile(r"\{[A-Za-z_][A-Za-z0-9_]{2,}\}|None|nan%"),
        "空链接": re.compile(r"\]\(\s*\)|href=\"\""),
    }
    hits: list[dict] = []
    for name, pat in patterns.items():
        for m in list(pat.finditer(needle))[:_SAMPLE_CAP]:
            hits.append({"kind": name, "match": m.group(0)[:40],
                         "context": _norm(needle[max(0, m.start() - 40):m.start() + 60])})
    if hits:
        return _bad(f"{len(hits)} 处疑似未替换占位符 / 空链接",
                    offenders=_cap(hits), total=len(hits))
    return _ok("无未替换占位符与空链接")


# ==========================================================================
# 注册表
# ==========================================================================

CHECKS: list[Check] = [
    # -- 数据层 ----------------------------------------------------------
    Check("dup_unit_ids", "单元 id 无重复", SEV_ERROR,
          "SKILL.md Phase 2'：755 行实际只有 490 帖，不去重所有计数成倍虚高",
          check_dup_unit_ids, needs=("tagged",)),
    Check("bucket_accounting", "分桶账目自洽", SEV_ERROR,
          "SKILL.md Phase 3：docs 要求 raw ≥ 清洗 ≥ A+B，且 A+B+C+D = 清洗后",
          check_bucket_accounting, needs=("stats",)),
    Check("date_missing_rate", "日期缺失率可控", SEV_ERROR,
          "SKILL.md 2026-09-30 复跑补充②：476/755 空日期 → 必须在报告里声明时间维度不可用",
          check_date_missing_rate, needs=("tagged",)),
    Check("control_chars", "无控制字符污染", SEV_ERROR,
          "SKILL.md 质检铁律②：\\b 被工具参数吞成退格符 U+0008，正则永不命中",
          check_control_chars, needs=("tagged",)),
    Check("brand_undercount", "品牌交付口径未低估", SEV_ERROR,
          "SKILL.md 2026-09-30 补充③：品牌只在评论里出现，帖子级口径会严重低估；Σcount ≥ 命中品牌的讨论串数",
          check_brand_undercount, needs=("stats", "corpus")),
    Check("brand_column_stale", "mentioned_brands 列未陈旧", SEV_WARN,
          "SKILL.md 2026-09-30 补充③：须用讨论串口径重写 mentioned_brands 列，否则下游统计严重低估",
          check_brand_column_stale, needs=("stats", "tagged")),

    # -- 归属层 ----------------------------------------------------------
    Check("evidence_quote_verbatim", "引文逐字回溯（讨论串）", SEV_ERROR,
          "SKILL.md 硬规则①：引文必须逐字取自原文；回溯范围＝讨论串（实测 100% vs 帖子级 1.8%）",
          check_evidence_quote_verbatim, needs=("dimensions", "corpus")),
    Check("count_matches_evidence", "count 与证据条数对齐", SEV_ERROR,
          "SKILL.md 硬规则③：count = len(evidence_posts)，保证图表与下钻一致",
          check_count_matches_evidence, needs=("dimensions",)),
    Check("persona_split_sums", "画像拆分列求和等于 count", SEV_ERROR,
          "SKILL.md 硬规则④：post_count + comment_count == count",
          check_persona_split_sums, needs=("dimensions",)),
    Check("brand_sentiment_sums", "品牌情感分布之和等于 count", SEV_ERROR,
          "SKILL.md 硬规则④：brand_mentions 的情感分布之和必须等于 count",
          check_brand_sentiment_sums, needs=("dimensions",)),
    Check("interrogative_evidence", "体验证据非疑问句", SEV_ERROR,
          "SKILL.md 质检铁律③：别让疑问句冒充证据",
          check_interrogative_evidence, needs=("dimensions",)),
    Check("evidence_ids_unit_type", "证据 id 与 unit_type 齐备", SEV_ERROR,
          "SKILL.md 硬规则④：评论证据要带 post_reddit_id；空 id 会让证据卡被跳过",
          check_evidence_ids_and_unit_type, needs=("dimensions",)),
    Check("required_structure", "必备结构键存在", SEV_ERROR,
          "SKILL.md Phase 4.6：purchase_status_distribution 只含两段 + unit_counts.excluded_unknown_units",
          check_required_structure, needs=("dimensions",)),
    Check("label_cn_format", "标签名格式与 reason", SEV_ERROR,
          "SKILL.md 字段规则：label_cn 禁括号 / 斜杠 / 特殊符号，reason 必填",
          check_label_cn_format, needs=("dimensions",)),
    Check("percentage_range", "占比落在 0–100", SEV_ERROR,
          "SKILL.md bug 清单：fmt_pct 曾对 0<v<=1 再 ×100，0.8% 显示成 80.0%",
          check_percentage_range, needs=("dimensions",)),

    # -- 功能层 ----------------------------------------------------------
    Check("feature_contract", "功能条目字段契约", SEV_ERROR,
          "SKILL.md features 契约：禁用 mention_count / satisfaction_rate 等历史字段名",
          check_feature_contract, needs=("features",)),
    Check("feature_satisfaction", "功能满意度公式自洽", SEV_ERROR,
          "SKILL.md 满意度口径：satisfaction = positive ÷ (positive+negative+neutral)，取值 0–1",
          check_feature_satisfaction, needs=("features",)),
    Check("feature_matrix", "热力图矩阵单元格结构", SEV_ERROR,
          "SKILL.md features 契约：矩阵单元格 {total, satisfaction, evidence_posts} 缺一即空白",
          check_feature_matrix, needs=("features",)),

    # -- 呈现层 ----------------------------------------------------------
    Check("presentation_vocabulary", "可见文案无跨源词汇泄漏", SEV_ERROR,
          "Stage 3 呈现边界：非本源输出不得出现「子版块 / Subreddit / Reddit / 原帖 / RSS」",
          check_presentation_vocabulary, needs=()),
    Check("presentation_identifiers", "DOM 标识符无旧源残留", SEV_WARN,
          "Stage 3 呈现边界的边角：模板里 chart-subreddits 这类 id 用户看不到，但是换源时的雷",
          check_presentation_identifiers, needs=()),
    Check("presentation_placeholders", "无未替换占位符 / 空链接", SEV_WARN,
          "SKILL.md bug 清单：BOM 导致来源链接静默渲染成 [](url)",
          check_presentation_placeholders, needs=()),
]


# ==========================================================================
# 执行
# ==========================================================================

_NEEDS_ALIAS = {
    "tagged": ("tagged", "relevant"),
    "corpus": ("tagged", "relevant", "comments"),
}


def _available(ctx: Context, needs: Sequence[str]) -> bool:
    for n in needs:
        for key in _NEEDS_ALIAS.get(n, (n,)):
            if getattr(ctx, key, None):
                break
        else:
            return False
    return True


def run_checks(ctx: Context, only: Iterable[str] | None = None,
               skip: Iterable[str] | None = None) -> list[Finding]:
    """跑一遍所有检查项。**任何检查项抛异常都不会中断整体**。"""
    only_s = set(only or ())
    skip_s = set(skip or ())
    out: list[Finding] = []
    for c in CHECKS:
        if only_s and c.id not in only_s:
            continue
        if c.id in skip_s:
            continue
        if c.needs and not _available(ctx, c.needs):
            out.append(Finding(c.id, c.title, c.severity, ST_SKIP,
                               f"输入缺失（需要 {', '.join(c.needs)}），跳过", {}, c.source))
            continue
        try:
            status, msg, detail = c.fn(ctx)
        except Exception as exc:  # noqa: BLE001 - 校验器必须比被校验对象更耐操
            status, msg, detail = ST_FAIL, f"检查项自身异常：{type(exc).__name__}: {exc}", {
                "exception": f"{type(exc).__name__}: {exc}"}
        out.append(Finding(c.id, c.title, c.severity, status, msg, detail, c.source))
    return out


def audit(ctx: Context, only: Iterable[str] | None = None,
          skip: Iterable[str] | None = None) -> dict[str, Any]:
    """跑闸门并组装 ``quality_report.json`` 的载荷。"""
    findings = run_checks(ctx, only, skip)
    counts = Counter(f.status for f in findings)
    by_sev = Counter(f.severity for f in findings if f.failed)
    if by_sev.get(SEV_ERROR):
        verdict = "fail"
    elif by_sev.get(SEV_WARN):
        verdict = "warn"
    elif counts.get(ST_SKIP) == len(findings):
        verdict = "empty"
    else:
        verdict = "pass"
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": ctx.profile_name or "",
        "inputs": {k: v for k, v in ctx.paths.items() if v},
        "summary": {
            "total": len(findings),
            "passed": counts.get(ST_PASS, 0),
            "failed": counts.get(ST_FAIL, 0),
            "skipped": counts.get(ST_SKIP, 0),
            "failed_by_severity": dict(by_sev),
            "verdict": verdict,
        },
        "findings": [f.to_dict() for f in findings],
    }


_ICON = {ST_PASS: "✓", ST_FAIL: "✗", ST_SKIP: "·"}
_VERDICT_CN = {"pass": "通过", "warn": "通过（有警告）", "fail": "不通过", "empty": "无输入"}


def render_console(report: dict) -> str:
    s = report["summary"]
    lines = [
        "=" * 72,
        f"质量闸门 · {report.get('source') or '(未指定源)'}   "
        f"通过 {s['passed']} / 失败 {s['failed']} / 跳过 {s['skipped']}",
        "=" * 72,
    ]
    worst = {SEV_ERROR: 0, SEV_WARN: 1, SEV_INFO: 2}
    for f in sorted(report["findings"], key=lambda x: (x["status"] != ST_FAIL,
                                                       worst.get(x["severity"], 3))):
        icon = _ICON.get(f["status"], "?")
        sev = {"error": "错误", "warn": "警告", "info": "提示"}.get(f["severity"], "")
        lines.append(f"  {icon} [{sev}] {f['title']}")
        lines.append(f"      {f['message']}")
        if f["status"] == ST_FAIL and f.get("source"):
            lines.append(f"      依据：{f['source']}")
    lines.append("-" * 72)
    lines.append(f"结论：{_VERDICT_CN.get(s['verdict'], s['verdict'])}")
    return "\n".join(lines)


# ==========================================================================
# 输入装配 + CLI
# ==========================================================================

def build_context(args) -> Context:
    def _load_json(p: str) -> dict:
        if not p:
            return {}
        f = Path(p)
        if not f.exists():
            return {}
        try:
            return json.loads(f.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _load_text(p: str) -> str:
        if not p:
            return ""
        f = Path(p)
        return f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""

    ctx = Context(
        stats=_load_json(args.stats),
        dimensions=_load_json(args.dimensions),
        features=_load_json(args.features_json),
        tagged=_read_csv(Path(args.tagged)) if args.tagged else [],
        relevant=_read_csv(Path(args.relevant)) if args.relevant else [],
        comments=_read_csv(Path(args.comments)) if args.comments else [],
        report_text=_load_text(args.report),
        dashboard_html=_load_text(args.dashboard),
        profile_name=(args.profile or "").strip(),
        frame_name=(getattr(args, "frame", "") or "").strip(),
        max_quote_miss_rate=args.max_quote_miss_rate,
    )
    ctx.paths = {
        k: v for k, v in {
            "stats": args.stats, "dimensions": args.dimensions,
            "features": args.features_json, "tagged": args.tagged,
            "relevant": args.relevant, "comments": args.comments,
            "report": args.report, "dashboard": args.dashboard,
        }.items() if v
    }
    return ctx


def _resolve_profile_name(spec: str) -> str:
    """``auto`` → 空（由调用方决定）；内置名 / JSON 路径 → 源名。"""
    s = (spec or "").strip()
    if not s or s.lower() == "auto":
        return ""
    try:
        return get_profile(s).source
    except Exception:  # noqa: BLE001
        return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="validate",
        description="Stage 6 质量闸门：把 SKILL.md 的经验条目变成可执行断言",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "退出码：0 = 通过（含仅警告）；1 = 有 error 级失败；2 = 参数错误\n"
            "输入可以只给一部分（如只给 dimensions + comments），缺输入的检查项会标为 skip。"
        ),
    )
    ap.add_argument("--stats", default="", help="数据集统计 JSON")
    ap.add_argument("--dimensions", default="", help="维度 JSON")
    ap.add_argument("--features-json", dest="features_json", default="", help="功能体验 JSON")
    ap.add_argument("--tagged", default="", help="清洗后单元 CSV")
    ap.add_argument("--relevant", default="", help="A/B 单元 CSV")
    ap.add_argument("--comments", default="", help="次级单元（评论）CSV")
    ap.add_argument("--report", default="", help="Markdown 报告（做呈现层检查）")
    ap.add_argument("--dashboard", default="", help="看板 HTML（做呈现层检查）")
    ap.add_argument("--profile", default="auto", help="源画像：内置名 / JSON 路径 / auto")
    ap.add_argument("--only", default="", help="只跑这些检查项（逗号分隔 id）")
    ap.add_argument("--skip", default="", help="跳过这些检查项（逗号分隔 id）")
    ap.add_argument("--max-quote-miss-rate", dest="max_quote_miss_rate", type=float,
                    default=0.0, help="引文回溯允许的未命中率（默认 0，即必须逐字）")
    ap.add_argument("--fail-on", choices=(SEV_ERROR, SEV_WARN, SEV_INFO), default=SEV_ERROR,
                    help="到这个级别就返回非零退出码（默认 error）")
    ap.add_argument("--out", default="", help="quality_report.json 的写入路径")
    ap.add_argument("--json", action="store_true", help="stdout 直接打印 JSON")
    ap.add_argument("--list-checks", action="store_true", help="列出全部检查项后退出")
    args = ap.parse_args(argv)

    if args.list_checks:
        print(f"{'id':32s} {'级别':4s} 说明")
        for c in CHECKS:
            print(f"{c.id:32s} {c.severity:4s} {c.title}")
            print(f"{'':32s}      依据：{c.source}")
        print(f"\n共 {len(CHECKS)} 项。")
        return 0

    args.profile = _resolve_profile_name(args.profile)
    ctx = build_context(args)
    report = audit(ctx,
                   only=[s for s in args.only.split(",") if s.strip()],
                   skip=[s for s in args.skip.split(",") if s.strip()])

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_console(report))
        if args.out:
            print(f"\n质量报告：{args.out}")

    threshold = SEV_ORDER[args.fail_on]
    worst = min((SEV_ORDER.get(f["severity"], 3)
                 for f in report["findings"] if f["status"] == ST_FAIL), default=99)
    return 1 if worst <= threshold else 0


if __name__ == "__main__":
    raise SystemExit(main())
