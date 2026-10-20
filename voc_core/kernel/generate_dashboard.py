#!/usr/bin/env python3
"""
Reddit Analysis Dashboard Generator
Reads analysis output files and generates a self-contained interactive HTML dashboard.

Usage:
  python3 generate_dashboard.py \
    --stats data/reddit_analysis/reddit_analysis_stats.json \
    --tagged data/reddit_analysis/reddit_posts_tagged.csv \
    --relevant data/reddit_analysis/reddit_posts_relevant.csv \
    --category "NAS Storage" \
    --queries "NAS,Synology" \
    --subreddits "synology,homelab" \
    --brands "Synology,QNAP" \
    --keywords "nas,storage" \
    --out reports/reddit_analysis_dashboard.html
"""

import argparse
import csv
import json
import sys
import os
from collections import Counter, defaultdict
from datetime import datetime

# --- 平台侧公共设施（去域名化：列名归一化 + 源画像） -------------------------
# 见 voc_core/kernel_common.py。此处是内核唯一的外部依赖。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kernel_common import (  # noqa: E402
    add_frame_argument,
    add_profile_argument,
    canonicalize_rows,
    load_frame,
    load_frame_from_product,
    load_profile,
)
from voc_core.analysis_frame import AnalysisFrame  # noqa: E402

#: 平台根目录（assets/ 等资源相对它定位，保证 vendor 后路径仍正确）
PLATFORM_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def parse_args():
    p = argparse.ArgumentParser(description="Generate Reddit analysis interactive dashboard")
    p.add_argument("--stats", required=True, help="Path to reddit_analysis_stats.json")
    p.add_argument("--tagged", required=True, help="Path to reddit_posts_tagged.csv")
    p.add_argument("--relevant", default=None, help="Path to reddit_posts_relevant.csv")
    p.add_argument("--comments", default=None, help="Path to reddit_comments.csv")
    p.add_argument("--category", required=True, help="Product category name")
    p.add_argument("--queries", default="", help="Comma-separated search queries used")
    p.add_argument("--subreddits", default="", help="Comma-separated subreddits used")
    p.add_argument("--brands", default="", help="Comma-separated brand list")
    p.add_argument("--keywords", default="", help="Comma-separated keywords")
    p.add_argument("--max-age-years", type=float, default=2.0, help="Max age years filter")
    p.add_argument("--out", default="reddit_analysis_dashboard.html", help="Output HTML path")
    p.add_argument("--title", default=None, help="Dashboard title override")
    add_profile_argument(p)
    add_frame_argument(p)
    default_logo = os.path.join(PLATFORM_ROOT, "assets", "logo.jpg")
    p.add_argument("--logo", default=default_logo, help="Path to logo image (embedded as base64 data URI). Note: the top-left header logo is never rendered; this asset is only kept for backward compatibility")
    p.add_argument("--no-logo", action="store_true", help="Do not render any logo in the dashboard header (remove top-left logo)")
    p.add_argument("--dimensions", default=None, help="Path to reddit_analysis_dimensions.json (output from Phase 4 LLM analysis)")
    p.add_argument("--features-json", default=None, help="Path to reddit_analysis_features.json (output from feature/experience analysis)")
    return p.parse_args()


def load_logo_as_datauri(path):
    """Read an image file and return a base64 data URI suitable for embedding in HTML."""
    import base64
    if not path or not os.path.exists(path):
        return ""
    try:
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext in ("jpg", "jpeg"):
            mime = "image/jpeg"
        elif ext == "png":
            mime = "image/png"
        elif ext == "gif":
            mime = "image/gif"
        elif ext == "svg":
            mime = "image/svg+xml"
        elif ext == "webp":
            mime = "image/webp"
        else:
            mime = f"image/{ext}"
        with open(path, "rb") as f:
            data = f.read()
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        print(f"Warning: failed to load logo {path}: {e}", file=sys.stderr)
        return ""


def read_stats(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def read_csv_safe(path, role="auto"):
    """读 CSV 并做列名归一化（``role`` 见 :mod:`kernel_common`）。

    归一化是**补键**而非改名：任意源的 ``unit_id`` / ``channel`` / ``text``
    会被翻译成内核认识的 ``reddit_id`` / ``subreddit`` / ``summary_text``，
    原键全部保留。对本来就是旧 schema 的输入而言这是恒等变换。
    """
    rows = []
    if not path or not os.path.exists(path):
        return rows
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except Exception:
        pass
    if rows and role:
        rows = canonicalize_rows(rows, role)
    return rows


def build_brand_frequencies(rows):
    counter = Counter()
    for row in rows:
        brands_str = (row.get("mentioned_brands") or "").strip()
        if not brands_str:
            continue
        for b in brands_str.split(";"):
            b = b.strip()
            if b:
                counter[b] += 1
    top = counter.most_common(15)
    labels = [x[0] for x in top]
    counts = [x[1] for x in top]
    return {"labels": labels, "counts": counts, "total_mentions": sum(counts)}


def build_subreddit_distribution(rows):
    counter = Counter()
    for row in rows:
        sr = (row.get("subreddit") or "").strip()
        if sr:
            counter[sr] += 1
    top = counter.most_common(10)
    return {"labels": [x[0] for x in top], "counts": [x[1] for x in top]}


def build_time_series(rows):
    months = Counter()
    for row in rows:
        pub = (row.get("published") or "").strip()
        if len(pub) >= 7:
            months[pub[:7]] += 1
    sorted_months = sorted(months.items())
    return {"labels": [x[0] for x in sorted_months], "counts": [x[1] for x in sorted_months]}


def build_bucket_breakdown(rows):
    """Break down each bucket by sub-category"""
    buckets = defaultdict(Counter)
    for row in rows:
        bucket = (row.get("voc_bucket") or "").strip()
        if bucket:
            sr = (row.get("subreddit") or "").strip()
            if sr:
                buckets[bucket][sr] += 1
    return {k: dict(v.most_common(5)) for k, v in buckets.items()}


def compute_concern_distribution(rows):
    """
    Simple keyword-based pre-purchase concern distribution.
    In a full implementation, this would be enriched by LLM analysis results.
    """
    concern_words = {
        "价格/性价比": ["price", "cost", "cheap", "expensive", "budget", "worth", "affordable", "value"],
        "功能/性能": ["feature", "performance", "speed", "powerful", "fast", "capacity", "spec"],
        "品牌口碑": ["brand", "reputation", "reliable", "quality", "review", "rated"],
        "社区推荐": ["recommend", "suggestion", "opinion", "thoughts", "advice", "community"],
        "兼容性": ["compatible", "compatibility", "works with", "supports", "connect", "integrat"],
        "易用性": ["easy", "simple", "user friendly", "intuitive", "setup", "plug and play"],
        "耐用性": ["durable", "last", "longevity", "warranty", "lifetime", "build quality"],
        "隐私安全": ["privacy", "security", "safe", "encrypt", "secure", "trust"],
    }
    result = {}
    total = len(rows)
    if total == 0:
        return {"labels": [], "counts": [], "percentages": []}

    for label, terms in concern_words.items():
        count = 0
        for row in rows:
            text = " ".join([
                (row.get("title") or "").lower(),
                (row.get("summary_text") or "").lower()
            ])
            if any(term in text for term in terms):
                count += 1
        result[label] = count

    sorted_items = sorted(result.items(), key=lambda x: -x[1])
    return {
        "labels": [x[0] for x in sorted_items if x[1] > 0],
        "counts": [x[1] for x in sorted_items if x[1] > 0],
        "percentages": [round(x[1] / total * 100, 1) for x in sorted_items if x[1] > 0],
        "total": total,
    }


def compute_sentiment_distribution(rows):
    """
    Simple keyword-based sentiment distribution for post-purchase experience.
    """
    positive_words = ["love", "great", "best", "excellent", "amazing", "perfect",
                      "happy", "recommend", "impressed", "fantastic", "solid", "awesome"]

    negative_words = ["issue", "problem", "disappointed", "broken", "bug", "waste",
                      "terrible", "horrible", "returned", "refund", "regret", "worst",
                      "bad", "poor", "fail", "error", "crash", "slow"]

    positive_count = 0
    negative_count = 0
    neutral_count = 0

    # Sub-category analysis for experience dimensions
    exp_dimensions = {
        "易用性": ["easy", "simple", "setup", "intuitive", "user friendly", "plug and play"],
        "可靠性": ["reliable", "stable", "dependable", "consistent", "solid", "trust"],
        "电池/续航": ["battery", "charge", "power", "lasts", "runtime"],
        "软件/固件": ["software", "firmware", "app", "update", "ui", "interface"],
        "客服/售后": ["support", "customer", "service", "warranty", "return", "replace"],
        "配件/扩展": ["accessories", "mount", "case", "cable", "adapter", "mod"],
    }

    exp_sentiment = {}
    for dim, terms in exp_dimensions.items():
        pos = 0
        neg = 0
        for row in rows:
            text = " ".join([
                (row.get("title") or "").lower(),
                (row.get("summary_text") or "").lower()
            ])
            if any(t in text for t in terms):
                if any(p in text for p in positive_words):
                    pos += 1
                if any(n in text for n in negative_words):
                    neg += 1
        exp_sentiment[dim] = {"positive": pos, "negative": neg}

    for row in rows:
        text = " ".join([
            (row.get("title") or "").lower(),
            (row.get("summary_text") or "").lower()
        ])
        has_pos = any(p in text for p in positive_words)
        has_neg = any(n in text for n in negative_words)
        if has_pos and not has_neg:
            positive_count += 1
        elif has_neg and not has_pos:
            negative_count += 1
        elif has_pos and has_neg:
            neutral_count += 1
        else:
            neutral_count += 1

    return {
        "positive": positive_count,
        "negative": negative_count,
        "neutral": neutral_count,
        "total": len(rows),
        "dimensions": exp_sentiment,
    }


def compute_user_persona(rows):
    """
    Simple keyword-based user persona extraction.
    """
    persona_words = {
        "技术爱好者": ["enthusiast", "tech", "hobbyist", "geek", "nerd", "diy", "build", "homelab", "homelabber"],
        "家庭用户": ["family", "home", "household", "kids", "parents", "apartment", "wife", "husband"],
        "专业用户": ["professional", "business", "work", "office", "company", "enterprise", "client"],
        "学生": ["student", "college", "university", "school", "dorm", "campus"],
        "创作者": ["creator", "photographer", "video", "editor", "designer", "artist", "music"],
        "隐私敏感用户": ["privacy", "self host", "selfhosted", "cloud", "google", "data"],
        "新手入门": ["beginner", "newbie", "first time", "starter", "entry level", "new to"],
    }

    result = {}
    total = len(rows)
    for label, terms in persona_words.items():
        count = 0
        for row in rows:
            text = " ".join([
                (row.get("title") or "").lower(),
                (row.get("summary_text") or "").lower()
            ])
            if any(t in text for t in terms):
                count += 1
        result[label] = count

    sorted_items = sorted(result.items(), key=lambda x: -x[1])
    cleaned = [(k, v) for k, v in sorted_items if v > 0]
    other = total - sum(v for _, v in cleaned)
    if other > 0:
        cleaned.append(("其他/未识别", other))

    return {
        "labels": [x[0] for x in cleaned],
        "counts": [x[1] for x in cleaned],
        "total": total,
    }


def compute_unmet_needs(rows):
    """
    Simple keyword-based unmet needs extraction.
    """
    need_words = {
        "缺少功能": ["missing", "lack", "wish", "want", "need", "would love", "hoping for"],
        "价格过高": ["expensive", "overpriced", "cost", "price", "cheaper", "affordable"],
        "隐私顾虑": ["privacy", "data", "cloud", "google", "spy", "tracking", "surveillance"],
        "订阅模式排斥": ["subscription", "monthly", "paywall", "license", "recurring"],
        "集成/生态问题": ["integration", "ecosystem", "connect", "sync", "platform", "compatibility"],
        "学习曲线": ["complicated", "confusing", "hard", "difficult", "learning", "overwhelming"],
        "缺少本地化": ["language", "region", "local", "country", "available in"],
        "缺少配件": ["accessories", "replacement", "part", "spare", "mount", "adapter"],
    }

    result = {}
    for label, terms in need_words.items():
        count = 0
        for row in rows:
            text = " ".join([
                (row.get("title") or "").lower(),
                (row.get("summary_text") or "").lower()
            ])
            if any(t in text for t in terms):
                count += 1
        result[label] = count

    sorted_items = sorted(result.items(), key=lambda x: -x[1])
    return {
        "labels": [x[0] for x in sorted_items if x[1] > 0],
        "counts": [x[1] for x in sorted_items if x[1] > 0],
        "total": len(rows),
    }

def _dim_to_labels_counts(dim_items):
    """Convert dimensions JSON items to {labels, counts, total, percentages} for charts.

    计数口径统一为关联帖集合（evidence_posts）的长度，与数据原声聚焦信息流的「关联 X 篇」一致；
    若某类别无 evidence_posts（未运行 link_posts），则回退到 LLM 的 count 字段。
    percentages 为该类别在维度内的占比（合计约 100%）。
    """
    raw = []
    for d in dim_items:
        eps = d.get("evidence_posts") or []
        # count 为权威统计口径（评论单元等场景下证据只保留抽样）；
        # 缺 count 时回退到关联证据长度。
        c = d.get("count") or len(eps)
        if c > 0:
            raw.append((d.get("label_cn", ""), c))
    raw.sort(key=lambda x: -x[1])
    labels = [x[0] for x in raw]
    counts = [x[1] for x in raw]
    total = sum(counts)
    percentages = [round(c / total * 100, 1) if total else 0.0 for c in counts]
    return {"labels": labels, "counts": counts, "total": total, "percentages": percentages}

def _dim_to_stacked(dim_items):
    """人群画像专用：把维度项转成「帖子 / 评论」堆叠数据。

    v4 起人群画像是「帖子单元 + 评论单元合并计数」（按购买状态分购买者 / 未购买者两桶），
    所以每个标签除了 count 还要给出
    post_count / comment_count，看板据此堆叠，一眼看出该人群信号主要来自发帖还是评论。
    老数据缺 post_count / comment_count 时，按证据条目的 unit_type 推断；再不行则全部记为帖子。
    """
    raw = []
    for d in dim_items:
        eps = d.get("evidence_posts") or []
        c = d.get("count") or len(eps)
        if c <= 0:
            continue
        pc = d.get("post_count")
        cc = d.get("comment_count")
        if pc is None or cc is None:
            pc = sum(1 for e in eps if (e.get("unit_type") or "post") != "comment")
            cc = sum(1 for e in eps if e.get("unit_type") == "comment")
            if pc + cc == 0 and c:
                pc = c
                cc = 0
        raw.append((d.get("label_cn", ""), c, int(pc or 0), int(cc or 0)))
    raw.sort(key=lambda x: -x[1])
    labels = [x[0] for x in raw]
    counts = [x[1] for x in raw]
    post_counts = [x[2] for x in raw]
    comment_counts = [x[3] for x in raw]
    total = sum(counts)
    percentages = [round(c / total * 100, 1) if total else 0.0 for c in counts]
    return {
        "labels": labels, "counts": counts,
        "postCounts": post_counts, "commentCounts": comment_counts,
        "total": total, "percentages": percentages,
    }


def _counts_to_stacked(d):
    """把关键词兜底的 {labels, counts, total} 补成堆叠形状（评论段为 0）。"""
    labels = d.get("labels", []) or []
    counts = d.get("counts", []) or []
    return {
        "labels": labels, "counts": counts,
        "postCounts": list(counts), "commentCounts": [0] * len(counts),
        "total": d.get("total", sum(counts)),
        "percentages": [0.0] * len(counts),
    }


def _dim_to_distribution(dim_items):
    """购买状态分布 -> {labels, counts, total}（环形图用）。"""
    labels, counts = [], []
    for d in dim_items:
        c = d.get("count", 0) or 0
        if c <= 0:
            continue
        labels.append(d.get("label_cn", ""))
        counts.append(c)
    return {"labels": labels, "counts": counts, "total": sum(counts)}


def _sentiment_from_dimensions(dim_items):
    """Convert post_purchase_experience dimensions to sentiment data.

    每类总数（堆叠柱总和）统一取关联帖集合长度，与数据原声聚焦信息流「关联 X 篇」一致；
    正/负/中性拆分沿用该类别的原情感标签。未运行 link_posts 时回退到 LLM count。
    """
    dims = {}
    for d in dim_items:
        label = d.get("label_cn", "")
        eps = d.get("evidence_posts") or []
        cnt = d.get("count") or len(eps)
        sent = d.get("sentiment", "mixed")
        if label:
            dims[label] = {
                "positive": cnt if sent == "positive" else 0,
                "negative": cnt if sent == "negative" else 0,
                "neutral": cnt if sent not in ("positive", "negative") else 0,
            }
    return {"dimensions": dims}


def js_safe(val):
    """Convert any Python value to a JS literal using json.dumps (handles all escaping).
    Also prevents </script> sequences from breaking the HTML script tag."""
    result = json.dumps(val, ensure_ascii=False)
    # Prevent </script> from prematurely closing the <script> tag
    # Replace </ with <\/ in any case variation
    import re
    result = re.sub(r'</(?=script>)', r'<\\/', result, flags=re.IGNORECASE)
    return result


def js_escape_dict(d):
    """Convert Python dict/list to a JS literal string. Delegates to js_safe for safety."""
    return js_safe(d)


def serialize_rows(rows):
    """Convert CSV rows to a JSON-safe list."""
    return [{k: (v or "") for k, v in row.items()} for row in rows]


def generate_html(data, category, title, queries, subreddits, brands, keywords, max_age_years, logo_uri="", dimensions=None, features_data=None, profile=None, frame=None):
    # --- 分析框架（Stage 5）----------------------------------------------
    # 看板里凡涉及「分段轴」的标题 / 副标题都由框架配置渲染。框架从 dimensions.json
    # 的 analysis_frame 块读入（design_tags 写的）；老产物没有这个块 → 内置 consumer，
    # 渲染结果与改造前的硬编码逐字相同。
    FR = frame if frame is not None else load_frame(None)
    AX = FR.axis
    _dist_key = (AX.distribution_key if AX else "") or "purchase_status_distribution"

    # --- 术语（去域名化的呈现边界） -------------------------------------
    # 用户可见文案一律从画像取。默认画像 = REDDIT，其每个字段都与改造前的
    # 硬编码字符串逐字相同 —— 因此未传 --profile 时输出与旧版完全一致。
    P = profile or load_profile(None)
    # 注意：secondary_noun / promo_channel_note / channel_prefix 属于「可空术语」——
    # 空串表示**该概念在某源里不存在**，因此**必须直接取属性**，不能走
    # term()（term 会把空值回落到 Reddit 的值，导致电商源凭空多出「评论」这一层）。
    T_UNIT = P.term("unit_noun")                    # 帖子 / 评论
    T_UNIT_COUNT = P.term("unit_count_phrase")      # 提及帖子数 / 提及评论数
    T_UNIT_DISCUSS = P.term("unit_discuss_phrase")  # 讨论帖子数
    T_SECOND = P.opt("secondary_noun")              # 评论（电商源为空串）
    T_MERGED = P.term("merged_count_phrase")        # 帖子 + 评论
    T_CHANNEL = P.term("channel_noun")              # 子版块 / 站点
    T_CHANNEL_SECTION = P.term("channel_section")   # 子版块分布 / 站点分布
    T_CHANNEL_FILTER = P.term("channel_filter")
    T_CHANNEL_SORT = P.term("channel_sort")
    T_THREAD = P.term("thread_noun")                # 原帖 / 商品
    T_DATASET = P.term("dataset_label")             # Reddit RSS / Wayfair 导出
    T_GENERATOR = P.term("generator_label")
    T_SPEAKER = P.term("secondary_speaker")         # 评论者 / 用户
    T_FEED_TITLE = P.term("feed_title")
    T_SAMPLING = P.term("sampling_note")            # 采样局限说明
    T_THREAD_REF = P.term("thread_ref_phrase")      # 在原帖 / 在商品
    P_REDDIT = (P.source or "").strip().lower() == "reddit"

    # --- 派生文案：按源能力决定「说哪句」 ---------------------------------
    # 电商源是单层评论，没有「评论单元无功能标注」这个问题，脚注要整句换掉
    T_MATRIX_NOTE = (
        f" | 仅已购{T_UNIT}入矩阵；{T_SECOND}单元无功能标注，不进矩阵"
        if T_SECOND else f" | 仅已购{T_UNIT}入矩阵"
    )
    # 推广类渠道说明是 Reddit 独有的（subreddit 交易版块），其他源整段隐藏
    _promo = P.opt("promo_channel_note")
    T_PROMO_HTML = f"<p>{_promo}</p>" if (P_REDDIT and _promo) else ""
    # 数据粒度说明：Reddit 有「帖子 vs 评论」两层，电商只有一层评论
    T_LEVEL_NOTE = (
        f"{T_UNIT}级数据不等同于全量 VOC（用户之声）。{T_SECOND}级数据可能包含更丰富的信号。"
        if T_SECOND else f"{T_UNIT}数据不等同于全量 VOC（用户之声），可能存在抽样偏差。"
    )
    # 数据说明表里的次级单元行（电商源改称「数据条数」）
    T_SECOND_LABEL = f"{T_SECOND}数" if T_SECOND else "数据条数"
    # 「帖子 / 评论」这类单元对举；单层源只留一个词
    T_UNIT_PAIR = f"{T_UNIT} / {T_SECOND}" if T_SECOND else T_UNIT
    # 原文外链文案
    T_LINK_LABEL = f"{P.label} 原文 ↗"
    # 渠道名前缀：Reddit 显示 r/xxx，其他源直接显示渠道名（可空术语，不走 term）
    T_CHANNEL_PREFIX = P.opt("channel_prefix") if P_REDDIT else ""
    # DOM id / 排序键里的渠道 slug。Reddit 注入回 "subreddit"/"subreddits" → 输出逐字不变；
    # 其他源用 "channel"/"channels"。渲染层看得见 id，所以它也得跟着源走。
    T_CH_SLUG = P.term("channel_slug")
    T_CH_SLUG_PL = P.term("channel_slug_plural")
    # 排序字段取值：数据列的键名仍是 subreddit（列名属数据层，由 kernel_common 归一化），
    # 所以非 Reddit 源要把 UI 上的 channel 映射回 subreddit。Reddit 即原表达式，一行不差。
    T_SORT_FIELD_EXPR = ("parts[0]" if P_REDDIT
                         else "({ channel: 'subreddit' })[parts[0]] || parts[0]")
    # 人群画像堆叠图的图例与 tooltip：电商源没有次级单元，图例只列一项
    T_LEGEND_JS = "[" + ", ".join(
        f"'{x}'" for x in ([T_UNIT] + ([T_SECOND] if T_SECOND else []))
    ) + "]"
    T_STACK_TOOLTIP_JS = (
        f"'<br/>{T_UNIT} ' + p + ' · {T_SECOND} ' + c" if T_SECOND
        else f"'<br/>{T_UNIT} ' + (p + c)"
    )

    # --- 分段轴文案（Stage 5）：全部取自框架配置 --------------------------
    # _ax_ctx 是渲染这些句子的公共变量；句子本体在 frames/*.yaml 的
    # segmentation_axis.texts / .dashboard 里，改文案不必动代码。
    _ax_ctx = {
        "unit": T_UNIT, "unit_char": P.term("unit_char"), "unit_pair": T_UNIT_PAIR,
        "secondary": T_SECOND, "secondary_speaker": T_SPEAKER, "merged": T_MERGED,
    }
    if AX is not None:
        # 先渲染一遍拿到 matrix_note，再按「有没有次级单元」选版重渲染一次 ——
        # matrix_subtitle 的尾部要拼 matrix_note，两句必须取同一版（否则单层源的
        # 副标题里会冒出一个不存在的「评论单元」）。
        _first = AX.render_dashboard(**_ax_ctx)
        _note_key = "matrix_note" if T_SECOND else "matrix_note_solo"
        AX_DASH = AX.render_dashboard(matrix_note=_first.get(_note_key, ""), **_ax_ctx)
        T_MATRIX_NOTE = AX_DASH.get("matrix_note") or T_MATRIX_NOTE
    else:
        AX_DASH = {}

    # 框架缺某条文案（或整个框架没有分段轴）时的回落：与改造前的硬编码**逐字相同**，
    # 于是「老产物 + 新代码」出出来的看板与改造前逐字节一致。
    _DASH_FB = {
        "framework_line": (
            "分析框架：购买状态分布 &rarr; 购买者人群画像 &rarr; 未购买者人群画像 &rarr; "
            "使用场景 &rarr; 购买前关注点 &rarr; 购买后体验 &rarr; 品牌分布 &rarr; 未满足需求"
            f" | 点击图表分类可查看支撑证据；人群画像为「{T_MERGED}」合并计数"),
        "axis_chart_title": "a. 购买状态分布（购买意向漏斗）",
        "axis_chart_subtitle": (
            f"每个发言单元（{T_UNIT_PAIR}）先判定「已购买 / 未购买 / 无法判断」："
            "已购计入购买者桶、未购计入未购买者桶，无法判断的单元不计入任何画像桶"),
        "matrix_title": "已购人群画像 × 功能 交叉分析热力图",
        "matrix_subtitle": (
            f"纵轴：TOP 8 <b>已购</b>画像标签（不含未购 / 状态不明人群） | 横轴：TOP 10 功能 | "
            f"单元格：{T_UNIT}粒度交集计数 / 满意度{T_MATRIX_NOTE}"),
    }

    def _dash(key: str) -> str:
        """取一条轴相关文案：框架有就用框架的，没有就回落改造前的硬编码。"""
        return AX_DASH.get(key) or _DASH_FB[key]

    stats = data["stats"]
    tagged = data["tagged"]
    relevant = data["relevant"]
    comments = data["comments"]

    # Metrics
    raw_total = stats.get("raw_total", len(tagged))
    expired = stats.get("expired_count", 0)
    after_clean = stats.get("after_date_clean", raw_total - expired)
    bucket_a = stats.get("bucket_A", 0)
    bucket_b = stats.get("bucket_B", 0)
    bucket_c = stats.get("bucket_C", 0)
    bucket_d = stats.get("bucket_D", 0)
    final_analysis = stats.get("final_analysis", bucket_a + bucket_b)

    # Compute distributions
    brands_data = data.get("brands", build_brand_frequencies(relevant if relevant else tagged))
    subreddits_data = data.get("subreddits", build_subreddit_distribution(tagged))
    timeline = data.get("timeline", build_time_series(tagged))
    concern_data = compute_concern_distribution(relevant if relevant else tagged)
    sentiment_data = compute_sentiment_distribution(relevant if relevant else tagged)
    # 人群画像数据：键是**画像维度 id**，清单由框架的桶派生（几个桶几份数据）。
    # 没有 dimensions 时，只有第一个桶吃「画像关键词兜底」，其余为空 → 卡片自动隐藏。
    _persona_dim_by_bucket = {bid: did for did, bid in FR.persona_dims().items()}
    _empty_stacked = {"labels": [], "counts": [], "postCounts": [], "commentCounts": [],
                      "total": 0, "percentages": []}
    persona_datas = {did: dict(_empty_stacked) for did in _persona_dim_by_bucket.values()}
    _first_persona_dim = next(iter(_persona_dim_by_bucket.values()), "")
    if _first_persona_dim:
        persona_datas[_first_persona_dim] = _counts_to_stacked(
            compute_user_persona(relevant if relevant else tagged))
    purchase_status_data = {"labels": [], "counts": [], "total": 0}
    needs_data = compute_unmet_needs(relevant if relevant else tagged)
    scenario_data = {"labels": [], "counts": [], "total": 0}
    bucket_breakdown = build_bucket_breakdown(tagged)

    # Override chart data from dimensions JSON when available (Phase 4 LLM output)
    brand_sent_from_dims = False
    if dimensions:
        # 人群画像：桶 → 画像维度由框架给出（消费品是购买者 / 未购买者两桶，
        # 帖子 + 评论合并计数，柱内按单元类型堆叠）
        for _did in _persona_dim_by_bucket.values():
            persona_datas[_did] = _dim_to_stacked(dimensions.get(_did, []))
        purchase_status_data = _dim_to_distribution(dimensions.get(_dist_key, []))
        concern_data = _dim_to_labels_counts(dimensions.get("pre_purchase_concerns", []))
        sentiment_data = _sentiment_from_dimensions(dimensions.get("post_purchase_experience", []))
        needs_data = _dim_to_labels_counts(dimensions.get("unmet_needs", []))
        scenario_data = _dim_to_labels_counts(dimensions.get("usage_scenarios", []))
        # Brand data: use dimensions for brand mention counts
        brand_dims = dimensions.get("brand_mentions", [])
        if brand_dims:
            brands_data = _dim_to_labels_counts(brand_dims)
            # Also build sentiment from dimensions
            brand_sent_list = [
                {
                    "name": bd.get("label_cn", ""),
                    "positive": bd.get("sentiment_distribution", {}).get("positive", 0),
                    "negative": bd.get("sentiment_distribution", {}).get("negative", 0),
                    "neutral": bd.get("sentiment_distribution", {}).get("neutral", 0),
                }
                for bd in brand_dims
                if bd.get("sentiment_distribution")
            ]
            # Mark that brand sentiment came from LLM dimensions so the
            # keyword-based fallback below does NOT overwrite it.
            brand_sent_from_dims = bool(brand_sent_list)

    # --- 人群画像卡片：**按框架的桶生成**（几个桶几张卡） ------------------
    # 卡片标题 / 副标题来自桶配置（bucket.dashboard.card_title / card_subtitle），
    # DOM id 由桶 id 派生 —— 框架换桶，看板自动跟着换，代码里没有「固定两张卡」的假设。
    # 数据为空的桶，renderStackedPersona 会把卡片隐藏（这就是「自动隐藏空模块」）。
    persona_cards = []
    for _b in (AX.active_buckets if AX is not None else ()):
        _did = _persona_dim_by_bucket.get(_b.id, "")
        _bd = _b.dashboard
        persona_cards.append({
            "cardId": f"card-persona-{_b.id}",
            "chartId": f"chart-persona-{_b.id}",
            "dim": _did,
            "title": _bd.get("card_title", ""),
            "subtitle": (AX.render(_bd["card_subtitle"], **_ax_ctx)
                         if _bd.get("card_subtitle") else ""),
            "data": persona_datas.get(_did) or dict(_empty_stacked),
        })
    PERSONA_CARDS_HTML = "\n\n".join(
        f'    <div class="card" id="{c["cardId"]}">\n'
        f'      <div class="card-title">{c["title"]}</div>\n'
        f'      <div class="card-subtitle">{c["subtitle"]}</div>\n'
        f'      <div id="{c["chartId"]}" class="chart-container"></div>\n'
        f'    </div>' for c in persona_cards)
    PERSONA_CARDS_JS = [{k: v for k, v in c.items()
                         if k in ("cardId", "chartId", "dim", "data")}
                        for c in persona_cards]

    # 嵌进 JS 的 dimensions：剔掉 analysis_frame —— 它是**管线产物**（还带着全部提示词
    # 模板，约 8 KB），不是图表数据，JS 侧一次都用不到；留在里面只会让看板变胖，
    # 并让「改造前后看板是否一致」的逐字节比对失去意义。
    _embedded_dimensions = {k: v for k, v in (dimensions or {}).items()
                            if k != "analysis_frame"}

    # 框架里比静态表多出来的维度（B2B 的决策角色画像等）补进下钻横幅的中文名表。
    #
    # **只为「真的会出现在下钻里」的维度注入** —— 下钻的 ``dimKey`` 只可能来自
    # ``dimensions.json`` 的列表键（点柱子 / 点热力图格子 → showEvidence），
    # 所以判据是「该维度是内嵌 dimensions 的列表键」。少这一条会把
    # ``purchase_status``（分类器）和 ``features``（走 features.json）也塞进去 ——
    # 它们谁也查不到 DIM_LABELS，是死条目，却会平白改掉 consumer 框架下的看板字节。
    _STATIC_DIM_LABELS = {"usage_scenarios", "user_personas", "user_personas_purchased",
                          "user_personas_non_purchased", "user_personas_comments",
                          "pre_purchase_concerns", "post_purchase_experience",
                          "brand_mentions", "unmet_needs"}
    _drill_dims = {k for k, v in _embedded_dimensions.items() if isinstance(v, list)}
    EXTRA_DIM_LABELS_JS = "".join(
        f",\n  {k}: '{v}'" for k, v in FR.labels().items()
        if k not in _STATIC_DIM_LABELS and k in _drill_dims)

    # 只嵌入「被证据引用」的评论，避免 1 万+ 条评论把看板撑大
    evidence_comments = {}
    if dimensions:
        need = set()
        for dim_items in dimensions.values():
            if isinstance(dim_items, list):
                for it in dim_items:
                    for ep in (it.get("evidence_posts") or []):
                        rid = str(ep.get("reddit_id") or "").strip()
                        if rid:
                            need.add(rid)
        if need and comments:
            cmap = {(r.get("comment_id") or "").strip(): r for r in comments}
            evidence_comments = {r: cmap[r] for r in need if r in cmap}

    # Build posts for detail pages
    all_posts = serialize_rows(tagged)
    relevant_posts = serialize_rows(relevant) if relevant else [
        p for p in all_posts if p.get("voc_bucket", "") in ("A_强相关VOC", "B_可参考")
    ]

    # Brand sentiment: for each brand, count positive/negative.
    # Only use the keyword-based heuristic when LLM dimensions did not
    # already provide brand sentiment data (otherwise we overwrite it).
    if not brand_sent_from_dims:
        brand_sentiment = {}
        pos_w = ["love", "great", "best", "excellent", "amazing", "perfect", "happy", "recommend", "impressed"]
        neg_w = ["issue", "problem", "disappointed", "broken", "bug", "waste", "terrible", "returned", "refund", "bad"]
        for row in (relevant if relevant else tagged):
            brands_str = (row.get("mentioned_brands") or "").strip()
            if not brands_str:
                continue
            text = " ".join([(row.get("title") or ""), (row.get("summary_text") or "")]).lower()
            has_pos = any(p in text for p in pos_w)
            has_neg = any(n in text for n in neg_w)
            for b in brands_str.split(";"):
                b = b.strip()
                if not b:
                    continue
                if b not in brand_sentiment:
                    brand_sentiment[b] = {"positive": 0, "negative": 0, "neutral": 0}
                if has_pos:
                    brand_sentiment[b]["positive"] += 1
                elif has_neg:
                    brand_sentiment[b]["negative"] += 1
                else:
                    brand_sentiment[b]["neutral"] += 1

        brand_sent_list = [
            {"name": k, "positive": v["positive"], "negative": v["negative"], "neutral": v["neutral"]}
            for k, v in sorted(brand_sentiment.items(), key=lambda x: -(x[1]["positive"] + x[1]["negative"] + x[1]["neutral"]))
        ]

    # Generate timestamp
    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    dashboard_title = title or f"{category} {P.label} 分析仪表盘"

    # Build HTML
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{dashboard_title}</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js">
</script>
<style>
:root {{
  --bg-primary: #ffffff;
  --bg-secondary: #f5f6f8;
  --bg-tertiary: #ebedf0;
  --bg-hover: #f3f4f6;
  --text-primary: #1a1a2e;
  --text-secondary: #6b7280;
  --text-tertiary: #9ca3af;
  --border-primary: #e5e7eb;
  --border-secondary: #d1d5db;
  --blue: #3b82f6;
  --blue-light: #eff6ff;
  --green: #10b981;
  --green-light: #ecfdf5;
  --amber: #f59e0b;
  --amber-light: #fffbeb;
  --purple: #8b5cf6;
  --purple-light: #f5f3ff;
  --red: #ef4444;
  --red-light: #fef2f2;
  --gray: #6b7280;
  --gray-light: #f9fafb;
  /* === Unified Chart Color Palette === */
  --chart-blue: #3b82f6;
  --chart-purple: #8b5cf6;
  --chart-green: #10b981;
  --chart-red: #ef4444;
  --chart-amber: #f59e0b;
  --chart-teal: #06b6d4;
  --chart-pink: #ec4899;
  --chart-indigo: #6366f1;
  --chart-cyan: #14b8a6;
  --chart-orange: #f97316;
  --chart-grey: #94a3b8;
  --chart-sentiment-pos: #10b981;
  --chart-sentiment-neg: #ef4444;
  --chart-sentiment-neu: #94a3b8;
  --radius-lg: 12px;
  --radius-md: 8px;
  --radius-sm: 6px;
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, system-ui, sans-serif;
  --font-mono: "SF Mono", Monaco, "Cascadia Code", monospace;
}}

*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

body {{
  font-family: var(--font-sans);
  background: var(--bg-secondary);
  color: var(--text-primary);
  font-size: 14px;
  line-height: 1.6;
  min-height: 100vh;
}}

header {{
  background: var(--bg-primary);
  border-bottom: 1px solid var(--border-primary);
  padding: 0 24px;
  position: sticky;
  top: 0;
  z-index: 100;
}}

.header-inner {{
  max-width: 1400px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 56px;
}}

.header-left {{
  display: flex;
  align-items: center;
  gap: 14px;
}}

.header-title {{
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
}}

.header-meta {{
  font-size: 12px;
  color: var(--text-secondary);
}}

nav.tabs {{
  display: flex;
  gap: 0;
  border-bottom: 1px solid var(--border-primary);
  background: var(--bg-primary);
  padding: 0 24px;
  position: sticky;
  top: 56px;
  z-index: 99;
}}

.tab-inner {{
  max-width: 1400px;
  margin: 0 auto;
  display: flex;
  gap: 0;
  width: 100%;
}}

.tab-btn {{
  padding: 12px 20px;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-secondary);
  background: none;
  border: none;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: all 0.15s;
  white-space: nowrap;
}}

.tab-btn:hover {{ color: var(--text-primary); }}
.tab-btn.active {{
  color: var(--blue);
  border-bottom-color: var(--blue);
}}

main {{
  max-width: 1400px;
  margin: 0 auto;
  padding: 24px;
}}

.page {{ display: none; }}
.page.active {{ display: block; }}

.metrics-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}}

.metric-card {{
  background: var(--bg-primary);
  border: 1px solid var(--border-primary);
  border-radius: var(--radius-lg);
  padding: 20px;
}}

.metric-label {{
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 6px;
}}

.metric-value {{
  font-size: 28px;
  font-weight: 700;
  color: var(--text-primary);
}}

.metric-sub {{
  font-size: 11px;
  color: var(--text-tertiary);
  margin-top: 4px;
}}

.card {{
  background: var(--bg-primary);
  border: 1px solid var(--border-primary);
  border-radius: var(--radius-lg);
  padding: 24px;
  margin-bottom: 20px;
}}

.card-title {{
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 4px;
}}

.card-subtitle {{
  font-size: 12px;
  color: var(--text-secondary);
  margin-bottom: 16px;
}}

.chart-container {{
  width: 100%;
  height: 380px;
}}

.chart-sm {{ height: 300px; }}
.chart-xs {{ height: 280px; }}

.grid-2 {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
}}

.grid-3 {{
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 20px;
}}

@media (max-width: 900px) {{
  .grid-2, .grid-3 {{ grid-template-columns: 1fr; }}
}}

.pipeline-container {{
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 0;
}}

.pipeline-step {{
  text-align: center;
  min-width: 90px;
}}

.pipeline-box {{
  background: var(--blue-light);
  border: 1px solid #bfdbfe;
  border-radius: var(--radius-lg);
  padding: 8px 16px;
  min-width: 90px;
}}

.pipeline-box.c-blue {{ background: #eff6ff; border-color: #bfdbfe; }}
.pipeline-box.c-green {{ background: #ecfdf5; border-color: #a7f3d0; }}
.pipeline-box.c-amber {{ background: #fffbeb; border-color: #fde68a; }}
.pipeline-box.c-purple {{ background: #f5f3ff; border-color: #ddd6fe; }}

.pipeline-number {{
  font-size: 22px;
  font-weight: 800;
}}

.pipeline-label {{
  font-size: 11px;
  color: var(--text-secondary);
  margin-top: 2px;
}}

.pipeline-arrow {{
  font-size: 18px;
  color: var(--text-tertiary);
  display: flex;
  flex-direction: column;
  align-items: center;
}}

.pipeline-loss {{
  font-size: 10px;
  color: var(--red);
}}

.bucket-card {{
  background: var(--bg-primary);
  border: 1px solid var(--border-primary);
  border-radius: var(--radius-lg);
  padding: 10px 14px;
  text-align: center;
  flex: 1;
  min-width: 110px;
}}

.bucket-card .bucket-label {{
  font-size: 11px;
  color: var(--text-secondary);
  margin-bottom: 4px;
}}

.bucket-card .bucket-value {{
  font-size: 22px;
  font-weight: 700;
}}

.bucket-card .bucket-pct {{
  font-size: 11px;
  color: var(--text-tertiary);
  margin-top: 2px;
}}

.progress-bar {{
  height: 4px;
  background: var(--bg-tertiary);
  border-radius: 2px;
  margin-top: 6px;
  overflow: hidden;
}}

.progress-fill {{
  height: 100%;
  border-radius: 3px;
  transition: width 0.3s;
}}

.filters-bar {{
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: 16px;
}}

.filters-bar input,
.filters-bar select {{
  padding: 8px 12px;
  border: 1px solid var(--border-secondary);
  border-radius: var(--radius-sm);
  font-size: 13px;
  background: var(--bg-primary);
  color: var(--text-primary);
  min-width: 140px;
}}

.filters-bar input:focus,
.filters-bar select:focus {{
  outline: none;
  border-color: var(--blue);
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
}}

.data-table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}}

.data-table thead {{
  position: sticky;
  top: 0;
  z-index: 1;
}}

.data-table th {{
  background: var(--bg-secondary);
  padding: 10px 14px;
  text-align: left;
  font-weight: 600;
  font-size: 12px;
  color: var(--text-secondary);
  border-bottom: 1px solid var(--border-primary);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}}

.data-table th:hover {{ color: var(--text-primary); }}

.data-table td {{
  padding: 10px 14px;
  border-bottom: 1px solid var(--border-primary);
  max-width: 400px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}

.data-table tbody tr {{
  cursor: pointer;
  transition: background 0.1s;
}}

.data-table tbody tr:hover {{
  background: var(--blue-light);
}}

.data-table tbody tr.expanded {{
  background: var(--purple-light);
}}

/* ======== Data Raw Feed (数据原声) ======== */
.feed {{
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin-top: 4px;
}}
.post-card {{
  border: 1px solid var(--border-primary);
  border-radius: var(--radius-md);
  padding: 16px 18px;
  background: var(--bg-primary);
  transition: border-color 0.15s, box-shadow 0.15s;
}}
.post-card:hover {{
  border-color: var(--blue);
  box-shadow: 0 2px 10px rgba(59, 130, 246, 0.08);
}}
.post-card-title {{
  font-size: 16px;
  font-weight: 700;
  line-height: 1.45;
  color: var(--text-primary);
  margin-bottom: 8px;
  word-break: break-word;
}}
.post-card-summary {{
  font-size: 14px;
  line-height: 1.75;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-word;
}}
.post-card-summary.clamped {{
  display: -webkit-box;
  -webkit-line-clamp: 8;
  -webkit-box-orient: vertical;
  overflow: hidden;
}}
.post-card-more {{
  display: inline-block;
  margin-top: 6px;
  color: var(--blue);
  font-size: 13px;
  cursor: pointer;
  user-select: none;
}}
.post-card-more:hover {{ text-decoration: underline; }}
.post-card-meta {{
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 14px;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed var(--border-primary);
  font-size: 12px;
  color: var(--text-secondary);
}}
.post-card-meta .meta-item {{ white-space: nowrap; }}
.post-card-meta .meta-link {{
  color: var(--blue);
  text-decoration: none;
  margin-left: auto;
  font-weight: 600;
}}
.post-card-meta .meta-link:hover {{ text-decoration: underline; }}
.feed-empty {{
  padding: 40px 16px;
  text-align: center;
  color: var(--text-secondary);
  font-size: 14px;
}}

/* ======== Explorer Focus Banner (图表下钻融合至数据原声) ======== */
.explorer-focus {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  background: var(--blue-light);
  border: 1px solid #bfdbfe;
  border-radius: 10px;
  padding: 12px 16px;
  margin-bottom: 14px;
}}
.explorer-focus-info {{ min-width: 0; }}
.explorer-focus-title {{ font-size: 15px; font-weight: 700; color: var(--text-primary); }}
.explorer-focus-sub {{ font-size: 12px; color: var(--text-secondary); margin-top: 2px; }}
.explorer-focus-clear {{
  flex-shrink: 0;
  background: var(--bg-primary);
  border: 1px solid var(--border-primary);
  border-radius: 8px;
  padding: 6px 12px;
  font-size: 13px;
  color: var(--text-primary);
  cursor: pointer;
}}
.explorer-focus-clear:hover {{ background: var(--bg-hover); }}
.explorer-focus-note {{
  background: #eff6ff;
  border-left: 3px solid #3b82f6;
  padding: 10px 14px;
  border-radius: 0 8px 8px 0;
  font-size: 13px;
  color: #1e3a8a;
  line-height: 1.6;
  margin-bottom: 14px;
  white-space: pre-wrap;
  word-break: break-word;
}}
.post-card-mention {{
  display: inline-block;
  margin-left: 8px;
  padding: 1px 8px;
  border-radius: 999px;
  background: #ede9fe;
  color: #6d28d9;
  border: 1px solid #c4b5fd;
  font-size: 12px;
  font-weight: 700;
  vertical-align: middle;
}}
.post-card-quote {{
  background: #fef3c7;
  border-left: 3px solid #f59e0b;
  padding: 8px 12px;
  border-radius: 0 6px 6px 0;
  font-size: 13px;
  color: #92400e;
  line-height: 1.6;
  font-style: italic;
  margin-top: 8px;
}}
.post-card-quote::before {{
  content: "\1F4AC 原文线索: ";
  font-style: normal;
  font-weight: 600;
  font-size: 12px;
  color: #b45309;
}}
/* 原文线索在帖子正文（标题/摘要）中的高亮锚点 */
mark.quote-hit {{
  background: #fde68a;
  color: #78350f;
  padding: 0 1px;
  border-radius: 3px;
  font-weight: 600;
}}

.badge {{
  display: inline-block;
  padding: 2px 10px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 500;
  white-space: nowrap;
}}

.badge-a {{ background: #f5f3ff; color: #7c3aed; }}
.badge-b {{ background: #ecfdf5; color: #059669; }}
.badge-c {{ background: #fffbeb; color: #d97706; }}
.badge-d {{ background: #f3f4f6; color: #6b7280; }}
.badge-comment {{ background: #eef2ff; color: #4f46e5; }}

.pagination {{
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  margin-top: 16px;
}}

.pagination button {{
  padding: 6px 12px;
  border: 1px solid var(--border-primary);
  border-radius: var(--radius-sm);
  background: var(--bg-primary);
  color: var(--text-primary);
  font-size: 13px;
  cursor: pointer;
}}

.pagination button:hover {{ background: var(--bg-secondary); }}
.pagination button.active {{ background: var(--blue); color: white; border-color: var(--blue); }}
.pagination button:disabled {{ opacity: 0.5; cursor: not-allowed; }}

.row-count {{
  font-size: 12px;
  color: var(--text-secondary);
  margin-right: 12px;
}}

.methodology-section {{
  margin-bottom: 24px;
}}

.methodology-section h3 {{
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 8px;
}}

.methodology-section p,
.methodology-section li {{
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.8;
}}

.tag-list {{
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}}

.tag-item {{
  background: var(--blue-light);
  color: var(--blue);
  padding: 4px 12px;
  border-radius: 16px;
  font-size: 12px;
}}

.empty-state {{
  text-align: center;
  padding: 48px;
  color: var(--text-tertiary);
}}

footer {{
  text-align: center;
  padding: 32px;
  font-size: 12px;
  color: var(--text-tertiary);
}}
</style>
</head>
<body>

<header>
  <div class="header-inner">
    <div class="header-left">
      <div>
        <div class="header-title">{dashboard_title}</div>
        <div class="header-meta">生成时间: {gen_time} | 品类: {category}</div>
      </div>
    </div>
  </div>
</header>

<nav class="tabs">
  <div class="tab-inner" id="tabNav">
    <button class="tab-btn active" data-page="page-overview">数据看板</button>
    <button class="tab-btn" data-page="page-analysis">分析结果</button>
    <button class="tab-btn" data-page="page-features">功能体验</button>
    <button class="tab-btn" data-page="page-brands">品牌满意度</button>
    <button class="tab-btn" data-page="page-explorer">数据原声</button>
    <button class="tab-btn" data-page="page-appendix">数据说明</button>
  </div>
</nav>

<main>
  <!-- Page 1: Overview -->
  <div class="page active" id="page-overview">
    <div class="card">
      <div class="card-title">数据管道与分级分析</div>
      <div class="card-subtitle">采集 → 过期过滤 → 日期清洗 → 相关性分级（A/B/C/D 四级分桶），点击桶卡片可跳转查看详情</div>
      <div class="pipeline-container">
        <div class="pipeline-step">
          <div class="pipeline-box c-blue">
            <div class="pipeline-number" style="color: var(--blue)">{raw_total}</div>
            <div class="pipeline-label">采集总量</div>
          </div>
        </div>
        <div class="pipeline-arrow">
          <span>&rarr;</span>
          <span class="pipeline-loss">-{expired} 过期</span>
        </div>
        <div class="pipeline-step">
          <div class="pipeline-box c-amber">
            <div class="pipeline-number" style="color: var(--amber)">{expired}</div>
            <div class="pipeline-label">过期数据</div>
          </div>
        </div>
        <div class="pipeline-arrow">
          <span>&rarr;</span>
          <span class="pipeline-loss">剩余 {after_clean}</span>
        </div>
        <div class="pipeline-step">
          <div class="pipeline-box c-green">
            <div class="pipeline-number" style="color: var(--green)">{after_clean}</div>
            <div class="pipeline-label">清洗后剩余</div>
          </div>
        </div>
        <div class="pipeline-arrow">
          <span>&rarr;</span>
          <span class="pipeline-loss">-{bucket_c + bucket_d} 噪声</span>
        </div>
        <div class="pipeline-step">
          <div class="pipeline-box c-purple">
            <div class="pipeline-number" style="color: var(--purple)">{final_analysis}</div>
            <div class="pipeline-label">最终分析 (A+B)</div>
          </div>
        </div>
      </div>
      <div style="display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--border-primary);">
        <div class="bucket-card" onclick="filterBucket('A_强相关VOC')" style="cursor: pointer;">
          <div class="bucket-label">A 强相关 VOC</div>
          <div class="bucket-value" style="color: #7c3aed;">{bucket_a}</div>
          <div class="bucket-pct">{round(bucket_a / max(after_clean, 1) * 100, 1)}%</div>
          <div class="progress-bar"><div class="progress-fill" style="width: {round(bucket_a / max(after_clean, 1) * 100)}%; background: #8b5cf6;"></div></div>
        </div>
        <div class="bucket-card" onclick="filterBucket('B_可参考')" style="cursor: pointer;">
          <div class="bucket-label">B 可参考</div>
          <div class="bucket-value" style="color: #059669;">{bucket_b}</div>
          <div class="bucket-pct">{round(bucket_b / max(after_clean, 1) * 100, 1)}%</div>
          <div class="progress-bar"><div class="progress-fill" style="width: {round(bucket_b / max(after_clean, 1) * 100)}%; background: #10b981;"></div></div>
        </div>
        <div class="bucket-card" onclick="filterBucket('C_推广/弱参考')" style="cursor: pointer;">
          <div class="bucket-label">C 推广/弱参考</div>
          <div class="bucket-value" style="color: #d97706;">{bucket_c}</div>
          <div class="bucket-pct">{round(bucket_c / max(after_clean, 1) * 100, 1)}%</div>
          <div class="progress-bar"><div class="progress-fill" style="width: {round(bucket_c / max(after_clean, 1) * 100)}%; background: #f59e0b;"></div></div>
        </div>
        <div class="bucket-card" onclick="filterBucket('D_噪声/低相关')" style="cursor: pointer;">
          <div class="bucket-label">D 噪声/低相关</div>
          <div class="bucket-value" style="color: #6b7280;">{bucket_d}</div>
          <div class="bucket-pct">{round(bucket_d / max(after_clean, 1) * 100, 1)}%</div>
          <div class="progress-bar"><div class="progress-fill" style="width: {round(bucket_d / max(after_clean, 1) * 100)}%; background: #9ca3af;"></div></div>
        </div>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <div class="card-title">{T_UNIT}时间分布</div>
        <div id="chart-timeline" class="chart-container chart-sm"></div>
      </div>
      <div class="card">
        <div class="card-title">{T_CHANNEL_SECTION} (Top 10)</div>
        <div id="chart-{T_CH_SLUG_PL}-small" class="chart-container chart-sm"></div>
      </div>
    </div>

  </div>

  <!-- Page 3: Analysis -->
  <div class="page" id="page-analysis">
    <div class="card">
      <div class="card-title">分析维度概览</div>
      <div class="card-subtitle">{_dash("framework_line")}</div>
    </div>

    <div class="card" id="card-purchase-status">
      <div class="card-title">{_dash("axis_chart_title")}</div>
      <div class="card-subtitle">{_dash("axis_chart_subtitle")}</div>
      <div id="chart-purchase-status" class="chart-container chart-sm"></div>
    </div>

{PERSONA_CARDS_HTML}

    <div class="card">
      <div class="card-title">e. 使用场景分布</div>
      <div class="card-subtitle">用户在何种场景下使用该产品（如户外、室内、外出/旅行等），点击柱子可查看支撑证据</div>
      <div id="chart-scenario" class="chart-container"></div>
    </div>

    <div class="grid-2">
      <div class="card">
        <div class="card-title">f. 购买前关注点</div>
        <div class="card-subtitle">用户购买前最关心的因素（多标签，可超100%）</div>
        <div id="chart-concerns" class="chart-container"></div>
      </div>
      <div class="card">
        <div class="card-title">g. 购买后体验 (情感分布)</div>
        <div class="card-subtitle">正面 vs 负面 vs 中性体验占比</div>
        <div id="chart-sentiment" class="chart-container"></div>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <div class="card-title">h. 品牌提及频次</div>
        <div class="card-subtitle">Top 品牌被讨论次数排行</div>
        <div id="chart-brands-bar" class="chart-container"></div>
      </div>
      <div class="card">
        <div class="card-title">i. 未被满足的需求</div>
        <div class="card-subtitle">各需求类别的讨论热度分布，点击柱子可查看支撑证据</div>
        <div id="chart-unmet-needs" class="chart-container"></div>
      </div>
    </div>
  </div>

  <!-- Page 4: Feature/Experience Analysis -->
  <div class="page" id="page-features">
    <div class="card">
      <div class="card-title">功能体验分析</div>
      <div class="card-subtitle">基于 LLM 语义分析的功能/体验标注，统计各功能提及次数与满意度。满意度 = 正面/(正面+负面)，中性不参与计算。</div>
    </div>

    <div class="card">
      <div class="card-title">功能体验满意度散点图</div>
      <div class="card-subtitle">横轴：提及次数 | 纵轴：满意度 | 仅显示有情感数据的功能 | 点击圆点查看证据</div>
      <div id="chart-feature-scatter" class="chart-container"></div>
    </div>

    <div class="card">
      <div class="card-title">品牌 × 功能 交叉分析热力图</div>
      <div class="card-subtitle">纵轴：TOP 5 品牌 | 横轴：TOP 10 功能 | 单元格：提及量 / 满意度 | 色块：蓝色渐变=提及量（深=多） | 字体：绿色≥80%满意，红色＜80%</div>
      <div id="chart-brand-feature-heatmap" class="chart-container"></div>
    </div>

    <div class="card" id="card-persona-feature-heatmap">
      <div class="card-title">{_dash("matrix_title")}</div>
      <div class="card-subtitle">{_dash("matrix_subtitle")}</div>
      <div id="chart-persona-feature-heatmap" class="chart-container"></div>
    </div>

    <div class="card">
      <div class="card-title">功能体验详细统计</div>
      <div class="card-subtitle">所有功能的提及次数、正/负/中性分布和满意度</div>
      <div style="max-height: 500px; overflow: auto;">
        <table class="data-table" id="feature-stats-table">
          <thead>
            <tr>
              <th>功能/体验</th>
              <th>提及次数</th>
              <th>正面</th>
              <th>负面</th>
              <th>中性</th>
              <th>满意度</th>
              <th>归类原因</th>
            </tr>
          </thead>
          <tbody id="feature-stats-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Page 4: Brand Satisfaction -->
  <div class="page" id="page-brands">
    <div class="card">
      <div class="card-title">品牌满意度</div>
      <div class="card-subtitle">品牌提及频次排行与情感倾向分布</div>
      <div id="chart-brand-sentiment" class="chart-container"></div>
    </div>
  </div>

  <!-- Page 5: Data Voices -->
  <div class="page" id="page-explorer">
    <div class="card">
      <div class="card-title">数据原声</div>
      <div class="card-subtitle">{T_FEED_TITLE}，每条均展示完整标题与尽可能多的摘要内容，支持搜索、筛选与排序</div>
      <div class="filters-bar" id="explorer-filters">
        <input type="text" id="explorer-search" placeholder="搜索标题或摘要关键词..." oninput="renderExplorerTable()">
        <select id="explorer-bucket" onchange="renderExplorerTable()">
          <option value="">相关性: 全部 (A+B)</option>
          <option value="A_强相关VOC">A 强相关 VOC</option>
          <option value="B_可参考">B 可参考</option>
          <option value="C_推广/弱参考">C 推广/弱参考</option>
          <option value="D_噪声/低相关">D 噪声/低相关</option>
        </select>
        <select id="explorer-brand" onchange="renderExplorerTable()">
          <option value="">品牌: 全部</option>
        </select>
        <select id="explorer-{T_CH_SLUG}" onchange="renderExplorerTable()">
          <option value="">{T_CHANNEL_FILTER}: 全部</option>
        </select>
        <select id="explorer-sort" onchange="sortExplorerTable(this.value)">
          <option value="natural:desc">排序: 关联顺序（默认）</option>
          <option value="published:desc">排序: 发布日期 (新→旧)</option>
          <option value="voc_score:desc">排序: 相关性分数 (高→低)</option>
          <option value="title:asc">排序: 标题 (A→Z)</option>
          <option value="{T_CH_SLUG}:asc">排序: {T_CHANNEL_SORT}</option>
        </select>
        <span class="row-count" id="explorer-count"></span>
      </div>
      <div class="explorer-focus" id="explorer-focus" style="display:none;">
        <div class="explorer-focus-info">
          <div class="explorer-focus-title" id="explorer-focus-title"></div>
          <div class="explorer-focus-sub" id="explorer-focus-sub"></div>
        </div>
        <button class="explorer-focus-clear" onclick="clearExplorerFocus()">✕ 返回全部原声</button>
      </div>
      <div class="explorer-focus-note" id="explorer-focus-note" style="display:none;"></div>
      <div id="explorer-feed" class="feed"></div>
      <div class="pagination" id="explorer-pagination"></div>
    </div>
  </div>

  <!-- Page 6: Data Description -->
  <div class="page" id="page-appendix">
    <div class="card">
      <div class="card-title">搜索策略</div>
      <div class="methodology-section" id="methodology-search-strategy"></div>
    </div>

    <div class="card">
      <div class="card-title">数据清洗方法</div>
      <div class="methodology-section">
        <h3>日期过滤</h3>
        <p>过滤发布时间超过 {max_age_years} 年的{T_UNIT}。如果日期字段无法解析，保守保留该{T_UNIT}。</p>
        <h3>广告/推广过滤</h3>
        <p>过滤包含 promoted、sponsored、coupon、discount、deal、giveaway 等广告关键词的{T_UNIT}。</p>
        {T_PROMO_HTML}
        <h3>相关性分级</h3>
        <p>基于品类关键词和品牌词进行评分：品牌词匹配+2分，品类关键词匹配+1分，意图词匹配+2分，噪声-5分。</p>
        <ul>
          <li>A 强相关VOC: 分数 >= 8，或品牌匹配 + 2+ 关键词</li>
          <li>B 可参考: 分数 >= 3 且非噪声</li>
          <li>C 推广/弱参考: 推广类且分数 < 8</li>
          <li>D 噪声/低相关: 噪声或分数 < 3</li>
        </ul>
      </div>
    </div>

    <div class="card">
      <div class="card-title">数据局限性</div>
      <div class="methodology-section">
        <ul>
          <li>{T_SAMPLING}</li>
          <li>{T_LEVEL_NOTE}</li>
          <li>分析维度中的百分比基于关键词匹配，为多标签统计，总和可能超过 100%。</li>
          <li>品牌提及频次仅反映讨论热度，不等于市场份额。</li>
          <li>日期清洗过滤了 {max_age_years} 年以上的数据，更早的趋势未被纳入。</li>
          <li>C/D 级数据不应用于核心结论。</li>
        </ul>
      </div>
    </div>

    <div class="card">
      <div class="card-title">数据统计</div>
      <div class="methodology-section">
        <table style="border-collapse: collapse; width: 100%; max-width: 600px;">
          <tr><td style="padding: 6px 12px; border-bottom: 1px solid var(--border-primary); font-weight: 500;">采集总量</td><td style="padding: 6px 12px; border-bottom: 1px solid var(--border-primary);">{raw_total}</td></tr>
          <tr><td style="padding: 6px 12px; border-bottom: 1px solid var(--border-primary); font-weight: 500;">过期数据</td><td style="padding: 6px 12px; border-bottom: 1px solid var(--border-primary);">{expired}</td></tr>
          <tr><td style="padding: 6px 12px; border-bottom: 1px solid var(--border-primary); font-weight: 500;">A 强相关VOC</td><td style="padding: 6px 12px; border-bottom: 1px solid var(--border-primary);">{bucket_a}</td></tr>
          <tr><td style="padding: 6px 12px; border-bottom: 1px solid var(--border-primary); font-weight: 500;">B 可参考</td><td style="padding: 6px 12px; border-bottom: 1px solid var(--border-primary);">{bucket_b}</td></tr>
          <tr><td style="padding: 6px 12px; border-bottom: 1px solid var(--border-primary); font-weight: 500;">C 推广/弱参考</td><td style="padding: 6px 12px; border-bottom: 1px solid var(--border-primary);">{bucket_c}</td></tr>
          <tr><td style="padding: 6px 12px; border-bottom: 1px solid var(--border-primary); font-weight: 500;">D 噪声/低相关</td><td style="padding: 6px 12px; border-bottom: 1px solid var(--border-primary);">{bucket_d}</td></tr>
          <tr><td style="padding: 6px 12px; border-bottom: 1px solid var(--border-primary); font-weight: 500;">{T_SECOND_LABEL}</td><td style="padding: 6px 12px; border-bottom: 1px solid var(--border-primary);">{len(comments)}</td></tr>
          <tr><td style="padding: 6px 12px; border-bottom: 1px solid var(--border-primary); font-weight: 500;">生成时间</td><td style="padding: 6px 12px; border-bottom: 1px solid var(--border-primary);">{gen_time}</td></tr>
        </table>
      </div>
    </div>
  </div>
</main>

<footer>
  由 {T_GENERATOR} | {gen_time} | 数据来源: {T_DATASET}
</footer>

<script>
// ======== Embedded Data ========
const EMBEDDED = {{
  stats: {js_escape_dict(stats)},
  allPosts: {js_escape_dict(all_posts)},
  relevantPosts: {js_escape_dict(relevant_posts)},
  commentsCount: {len(comments)},
  brands: {js_escape_dict(brands_data)},
  subreddits: {js_escape_dict(subreddits_data)},
  timeline: {js_escape_dict(timeline)},
  concernData: {js_escape_dict(concern_data)},
  sentimentData: {js_escape_dict(sentiment_data)},
  personaCards: {js_escape_dict(PERSONA_CARDS_JS)},
  purchaseStatusData: {js_escape_dict(purchase_status_data)},
  evidenceComments: {js_escape_dict(evidence_comments)},
  needsData: {js_escape_dict(needs_data)},
  scenarioData: {js_escape_dict(scenario_data)},
  brandSentiment: {js_escape_dict(brand_sent_list)},
  dimensions: {js_escape_dict(_embedded_dimensions)},
  featuresData: {js_escape_dict(features_data or {})},
  rawTotal: {raw_total},
  expired: {expired},
  afterClean: {after_clean},
  bucketA: {bucket_a},
  bucketB: {bucket_b},
  bucketC: {bucket_c},
  bucketD: {bucket_d},
  finalAnalysis: {final_analysis},
  category: {js_safe(category)},
  maxAgeYears: {max_age_years},
}};

// ======== 全局：反转义 HTML 实体（与 Python html.unescape 对齐，单次扫描避免二次转义） ========
var ENT_MAP = {{amp:'&', lt:'<', gt:'>', quot:'"', apos:"'", nbsp:' ', rsquo:'’', lsquo:'‘', ldquo:'“', rdquo:'”', ndash:'–', mdash:'—', hellip:'…'}};
function unescapeHtml(s) {{
  if (!s) return s;
  return String(s).replace(/&(#(?:x[0-9a-f]+|[0-9]+)|[a-z]+);/gi, function(m, g1) {{
    var low = g1.toLowerCase();
    if (low.charAt(0) === '#') {{
      var code = low.charAt(1) === 'x' ? parseInt(low.substring(2), 16) : parseInt(low.substring(1), 10);
      if (!isNaN(code)) return String.fromCharCode(code);
      return m;
    }}
    return ENT_MAP[low] !== undefined ? ENT_MAP[low] : m;
  }});
}}

// ======== Page Navigation ========
document.querySelectorAll('.tab-btn').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    var pageId = this.dataset.page;
    document.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    document.querySelectorAll('.page').forEach(function(p) {{ p.classList.remove('active'); }});
    this.classList.add('active');
    var pageEl = document.getElementById(pageId);
    if (pageEl) {{
      pageEl.classList.add('active');
      // Trigger chart resize and table render
      if (pageId === 'page-overview') {{ renderOverviewCharts(); }}
      if (pageId === 'page-analysis') {{ renderAnalysisCharts(); }}
      if (pageId === 'page-features') {{ renderFeatureCharts(); }}
      if (pageId === 'page-explorer') {{ renderExplorerTable(); }}
      if (pageId === 'page-brands') {{ renderBrandCharts(); }}
    }}
  }});
}});

// ======== Unified Color Palette ========
var COLORS = {{
  blue: '#3b82f6', purple: '#8b5cf6', green: '#10b981', red: '#ef4444',
  amber: '#f59e0b', teal: '#06b6d4', pink: '#ec4899', indigo: '#6366f1',
  cyan: '#14b8a6', orange: '#f97316', grey: '#94a3b8',
  bar: '#3b82f6',         // primary bar color
  barHover: '#2563eb',    // bar hover/emphasis
  list: ['#3b82f6', '#8b5cf6', '#10b981', '#f59e0b', '#06b6d4', '#ec4899', '#6366f1', '#14b8a6', '#f97316', '#ef4444'],
  sentimentPos: '#10b981', sentimentNeg: '#ef4444', sentimentNeu: '#94a3b8',
  scatterHigh: '#10b981', scatterMid: '#f59e0b', scatterLow: '#ef4444'
}};

// ======== Chart Helpers ========
function createChart(containerId) {{
  var el = document.getElementById(containerId);
  if (!el) return null;
  var existing = echarts.getInstanceByDom(el);
  if (existing) existing.dispose();
  return echarts.init(el);
}}

// ======== Page 1: Overview Charts ========
function renderOverviewCharts() {{
  renderTimeline();
  renderSubredditsSmall();
}}

function renderTimeline() {{
  var chart = createChart('chart-timeline');
  if (!chart) return;
  var d = EMBEDDED.timeline;
  chart.setOption({{
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'shadow' }} }},
    xAxis: {{ type: 'category', data: d.labels, axisLabel: {{ rotate: 45, fontSize: 10 }} }},
    yAxis: {{ type: 'value', name: '{T_UNIT}数' }},
    series: [{{
      type: 'bar', data: d.counts, barMaxWidth: 22,
      itemStyle: {{ color: COLORS.bar, borderRadius: [4, 4, 0, 0] }},
      emphasis: {{ itemStyle: {{ color: COLORS.barHover }} }}
    }}],
    grid: {{ left: 50, right: 20, top: 10, bottom: 50 }}
  }});
}}

function renderSubredditsSmall() {{
  var chart = createChart('chart-{T_CH_SLUG_PL}-small');
  if (!chart) return;
  var d = EMBEDDED.subreddits;
  chart.setOption({{
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'shadow' }} }},
    xAxis: {{ type: 'value', name: '{T_UNIT}数' }},
    yAxis: {{ type: 'category', data: d.labels.slice().reverse(), axisLabel: {{ fontSize: 11 }} }},
    series: [{{
      type: 'bar', data: d.counts.slice().reverse(), barMaxWidth: 22,
      itemStyle: {{ color: COLORS.bar, borderRadius: [0, 4, 4, 0] }}
    }}],
    grid: {{ left: 120, right: 30, top: 10, bottom: 20 }}
  }});
}}

// ======== Page 3: Analysis Charts ========
function renderAnalysisCharts() {{
  renderPurchaseStatus();
  renderPersonaCards();
  renderScenario();
  renderConcerns();
  renderSentiment();
  renderBrandsBar();
  renderUnmetNeeds();
}}

// ======== 人群画像（v5）：帖子 + 评论合并计数，柱内按单元类型堆叠 ========
// 画像卡张数由框架的**活跃桶**决定（consumer 两张、B2B 三张……），代码里没有固定张数；
// 每张卡走同一个渲染器，只是数据键与下钻维度键不同；该桶无数据（或被排除）时整卡隐藏。
function renderStackedPersona(chartId, cardId, d, dimKey) {{
  var card = document.getElementById(cardId);
  var chart = createChart(chartId);
  if (!d || !d.labels || d.labels.length === 0) {{
    if (card) card.style.display = 'none';
    return;
  }}
  if (card) card.style.display = '';
  if (!chart) return;
  var names = d.labels.slice().reverse();
  var pv = (d.postCounts || []).slice().reverse();
  var cv = (d.commentCounts || []).slice().reverse();
  var total = d.total || d.counts.reduce(function(a, b) {{ return a + b; }}, 0);
  var pct = (d.percentages || []).slice().reverse();
  chart.setOption({{
    legend: {{ data: {T_LEGEND_JS}, top: 0, right: 10, itemWidth: 12, itemHeight: 8, textStyle: {{ fontSize: 11 }} }},
    tooltip: {{
      trigger: 'axis', axisPointer: {{ type: 'shadow' }},
      formatter: function(ps) {{
        var i = ps[0].dataIndex;
        var p = pv[i] || 0, c = cv[i] || 0;
        var line = ps[0].name + '：共 ' + (p + c) + ' 个单元';
        if (pct[i] != null) line += '（占该画像 ' + pct[i] + '%）';
        line += {T_STACK_TOOLTIP_JS};
        return line;
      }}
    }},
    xAxis: {{ type: 'value', name: '单元数' }},
    yAxis: {{ type: 'category', data: names, axisLabel: {{ fontSize: 11 }} }},
    series: [
      {{ name: '{T_UNIT}', type: 'bar', stack: 'unit', data: pv, barMaxWidth: 22,
        itemStyle: {{ color: COLORS.bar }},
        emphasis: {{ itemStyle: {{ color: COLORS.barHover }} }} }},
      {{ name: '{T_SECOND}', type: 'bar', stack: 'unit', data: cv, barMaxWidth: 22,
        itemStyle: {{ color: COLORS.teal, borderRadius: [0, 4, 4, 0] }},
        emphasis: {{ itemStyle: {{ color: '#0891b2' }} }} }}
    ],
    grid: {{ left: 140, right: 50, top: 34, bottom: 20 }}
  }});
  chart.off('click');
  chart.on('click', function(params) {{
    showEvidence(dimKey, params.name);
  }});
}}

// 画像卡片按框架的桶渲染：几张卡由 EMBEDDED.personaCards 决定，代码里没有固定张数。
// 某个桶没有数据时，renderStackedPersona 会把那张卡隐藏 —— 空模块自动消失。
function renderPersonaCards() {{
  (EMBEDDED.personaCards || []).forEach(function(pc) {{
    renderStackedPersona(pc.chartId, pc.cardId, pc.data, pc.dim);
  }});
}}

// 分段轴分布（consumer = 购买状态、B2B = 决策角色…）：各活跃桶的单元占比，决定人群画像的归桶
function renderPurchaseStatus() {{
  var card = document.getElementById('card-purchase-status');
  var chart = createChart('chart-purchase-status');
  var d = EMBEDDED.purchaseStatusData;
  if (!d || !d.labels || d.labels.length === 0 || !d.total) {{
    if (card) card.style.display = 'none';
    return;
  }}
  if (card) card.style.display = '';
  if (!chart) return;
  var data = d.labels.map(function(l, i) {{ return {{ name: l, value: d.counts[i] }}; }});
  var palette = ['#3b82f6', '#f59e0b', '#94a3b8'];
  chart.setOption({{
    tooltip: {{ trigger: 'item', formatter: function(p) {{ return p.name + '：' + p.value + ' 个单元（' + p.percent + '%）'; }} }},
    legend: {{ orient: 'vertical', right: 10, top: 'center', textStyle: {{ fontSize: 11 }} }},
    series: [{{
      type: 'pie', radius: ['42%', '68%'], center: ['38%', '50%'], data: data,
      label: {{ show: true, fontSize: 11, formatter: '{{b}}\\n{{c}}' }},
      itemStyle: {{ borderColor: '#fff', borderWidth: 2 }},
      color: palette
    }}]
  }});
}}

function renderScenario() {{
  var chart = createChart('chart-scenario');
  if (!chart) return;
  var d = EMBEDDED.scenarioData;
  if (!d || !d.labels || d.labels.length === 0) {{
    chart.setOption({{
      title: {{ text: '当前数据不足以识别使用场景类别', left: 'center', top: 'center', textStyle: {{ color: '#9ca3af', fontSize: 14 }} }}
    }});
    return;
  }}
  var total = d.total || d.counts.reduce(function(a, b) {{ return a + b; }}, 0);
  var names = d.labels.slice().reverse();
  var vals = d.counts.slice().reverse();
  chart.setOption({{
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'shadow' }},
      formatter: function(ps) {{ return ps[0].name + ': ' + ps[0].value + ' 帖 (' + (ps[0].value / total * 100).toFixed(1) + '%)'; }}
    }},
    xAxis: {{ type: 'value', name: '{T_UNIT}数' }},
    yAxis: {{ type: 'category', data: names, axisLabel: {{ fontSize: 11 }} }},
    series: [{{
      type: 'bar', data: vals, barMaxWidth: 22,
      itemStyle: {{ color: COLORS.bar, borderRadius: [0, 4, 4, 0] }},
      emphasis: {{ itemStyle: {{ color: COLORS.barHover }} }}
    }}],
    grid: {{ left: 140, right: 50, top: 10, bottom: 20 }}
  }});
  chart.off('click');
  chart.on('click', function(params) {{
    showEvidence('usage_scenarios', params.name);
  }});
}}

function renderConcerns() {{
  var chart = createChart('chart-concerns');
  if (!chart) return;
  var d = EMBEDDED.concernData;
  chart.setOption({{
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'shadow' }}, formatter: function(ps) {{ var di = ps[0].dataIndex; var origIdx = d.percentages.length - 1 - di; return ps[0].name + ': ' + ps[0].value + ' 帖 (' + (d.percentages[origIdx] != null ? d.percentages[origIdx] : '?') + '%)'; }} }},
    xAxis: {{ type: 'value', name: '{T_UNIT}数', max: d.total }},
    yAxis: {{ type: 'category', data: d.labels.slice().reverse(), axisLabel: {{ fontSize: 11 }} }},
    series: [{{
      type: 'bar', data: d.counts.slice().reverse(), barMaxWidth: 22,
      itemStyle: {{ color: COLORS.bar, borderRadius: [0, 4, 4, 0] }},
      emphasis: {{ itemStyle: {{ color: COLORS.barHover }} }}
    }}],
    grid: {{ left: 100, right: 50, top: 10, bottom: 20 }}
  }});
  chart.off('click');
  chart.on('click', function(params) {{
    showEvidence('pre_purchase_concerns', params.name);
  }});
}}

function renderSentiment() {{
  var chart = createChart('chart-sentiment');
  if (!chart) return;
  var d = EMBEDDED.sentimentData;
  var dims = d.dimensions;
  var labels = Object.keys(dims);
  var posVals = labels.map(function(l) {{ return dims[l].positive; }});
  var negVals = labels.map(function(l) {{ return dims[l].negative; }});
  chart.setOption({{
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'shadow' }} }},
    legend: {{ data: ['正面', '负面'] }},
    xAxis: {{ type: 'value', name: '提及次数' }},
    yAxis: {{ type: 'category', data: labels.slice().reverse(), axisLabel: {{ fontSize: 11 }} }},
    series: [
      {{ name: '正面', type: 'bar', stack: 'total', barMaxWidth: 22, data: posVals.slice().reverse(), itemStyle: {{ color: COLORS.sentimentPos }} }},
      {{ name: '负面', type: 'bar', stack: 'total', barMaxWidth: 22, data: negVals.slice().reverse(), itemStyle: {{ color: COLORS.sentimentNeg }} }}
    ],
    grid: {{ left: 100, right: 50, top: 30, bottom: 20 }}
  }});
  chart.off('click');
  chart.on('click', function(params) {{
    showEvidence('post_purchase_experience', params.name);
  }});
}}

function renderBrandsBar() {{
  var chart = createChart('chart-brands-bar');
  if (!chart) return;
  var d = EMBEDDED.brands;
  chart.setOption({{
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'shadow' }} }},
    xAxis: {{ type: 'value', name: '{T_UNIT_COUNT}' }},
    yAxis: {{ type: 'category', data: d.labels.slice().reverse(), axisLabel: {{ fontSize: 11 }} }},
    series: [{{
      type: 'bar', data: d.counts.slice().reverse(), barMaxWidth: 22,
      itemStyle: {{ color: COLORS.bar, borderRadius: [0, 4, 4, 0] }},
      emphasis: {{ itemStyle: {{ color: COLORS.barHover }} }}
    }}],
    grid: {{ left: 120, right: 50, top: 10, bottom: 20 }}
  }});
  chart.off('click');
  chart.on('click', function(params) {{
    showEvidence('brand_mentions', params.name);
  }});
}}

function renderUnmetNeeds() {{
  var chart = createChart('chart-unmet-needs');
  if (!chart) return;
  var d = EMBEDDED.needsData;
  if (!d.labels || d.labels.length === 0) {{
    chart.setOption({{
      title: {{ text: '当前数据不足以识别未满足需求类别', left: 'center', top: 'center', textStyle: {{ color: '#9ca3af', fontSize: 14 }} }}
    }});
    return;
  }}

  // Build data sorted by count descending
  var items = [];
  for (var i = 0; i < d.labels.length; i++) {{
    items.push({{ label: d.labels[i], count: d.counts[i] }});
  }}
  items.sort(function(a, b) {{ return b.count - a.count; }});

  var labels = items.map(function(x) {{ return x.label; }});
  var counts = items.map(function(x) {{ return x.count; }});
  var total = d.total || counts.reduce(function(a, b) {{ return a + b; }}, 0);

  chart.setOption({{
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'shadow' }},
      formatter: function(ps) {{ return ps[0].name + ': ' + ps[0].value + ' 帖 (' + (ps[0].value / total * 100).toFixed(1) + '%)'; }}
    }},
    grid: {{ left: 140, right: 50, top: 10, bottom: 20 }},
    xAxis: {{ type: 'value', name: '{T_UNIT}数', axisLabel: {{ fontSize: 11 }} }},
    yAxis: {{ type: 'category', data: labels, axisLabel: {{ fontSize: 11 }}, inverse: true }},
    series: [{{
      type: 'bar', data: counts, barMaxWidth: 22,
      itemStyle: {{ color: COLORS.bar, borderRadius: [0, 4, 4, 0] }},
      emphasis: {{ itemStyle: {{ color: COLORS.barHover }} }}
    }}]
  }});

  chart.off('click');
  chart.on('click', function(params) {{
    if (params.componentType === 'series') {{
      showEvidence('unmet_needs', params.name);
    }}
  }});
}}

// ======== Page 5: Brand Charts ========
function renderBrandCharts() {{
  renderBrandSentiment();
}}

function renderBrandSentiment() {{
  var chart = createChart('chart-brand-sentiment');
  if (!chart) return;
  var bs = EMBEDDED.brandSentiment.slice(0, 10);
  chart.setOption({{
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'shadow' }} }},
    legend: {{ data: ['正面', '中性', '负面'] }},
    xAxis: {{ type: 'value', name: '{T_UNIT_DISCUSS}' }},
    yAxis: {{ type: 'category', data: bs.map(function(x) {{ return x.name; }}).reverse(), axisLabel: {{ fontSize: 11 }} }},
    series: [
      {{ name: '正面', type: 'bar', stack: 'total', barMaxWidth: 22, data: bs.map(function(x) {{ return x.positive; }}).reverse(), itemStyle: {{ color: COLORS.sentimentPos }} }},
      {{ name: '中性', type: 'bar', stack: 'total', barMaxWidth: 22, data: bs.map(function(x) {{ return x.neutral; }}).reverse(), itemStyle: {{ color: COLORS.sentimentNeu }} }},
      {{ name: '负面', type: 'bar', stack: 'total', barMaxWidth: 22, data: bs.map(function(x) {{ return x.negative; }}).reverse(), itemStyle: {{ color: COLORS.sentimentNeg }} }}
    ],
    grid: {{ left: 100, right: 50, top: 30, bottom: 20 }}
  }});
  chart.on('click', function(params) {{
    if (params.componentType === 'series') {{
      var revIdx = bs.length - 1 - params.dataIndex;
      if (revIdx >= 0 && revIdx < bs.length) {{
        var brandName = bs[revIdx].name;
        navigateToExplorer(brandName, '');
      }}
    }}
  }});
}}

// ======== Feature/Experience Charts ========
function renderFeatureCharts() {{
  renderFeatureScatter();
  renderBrandFeatureHeatmap();
  renderPersonaFeatureHeatmap();
  renderFeatureStatsTable();
}}

function renderFeatureScatter() {{
  var chart = createChart('chart-feature-scatter');
  if (!chart) return;
  var fd = EMBEDDED.featuresData;
  var features = (fd && fd.features) ? fd.features : [];
  // Only show features with sentiment data (satisfaction !== null) and mentions > 0
  var dataPoints = features.filter(function(f) {{
    return f.total > 0 && f.satisfaction !== null;
  }});
  if (dataPoints.length === 0) {{
    chart.setOption({{
      title: {{ text: '暂无足够情感数据', left: 'center', top: 'center', textStyle: {{ color: '#9ca3af', fontSize: 14 }} }}
    }});
    return;
  }}

  var scatterData = dataPoints.map(function(f) {{
    return {{
      value: [f.total, f.satisfaction],
      name: f.name
    }};
  }});

  chart.setOption({{
    tooltip: {{
      trigger: 'item',
      formatter: function(p) {{
        return '<b>' + p.name + '</b><br/>提及次数: ' + p.value[0] +
               '<br/>满意度: ' + (p.value[1] * 100).toFixed(1) + '%' +
               '<br/><span style=\\"color:#3b82f6;font-size:11px\\">点击查看证据</span>';
      }}
    }},
    xAxis: {{
      type: 'value', name: '提及次数',
      nameLocation: 'middle', nameGap: 30,
      axisLabel: {{ fontSize: 11 }}
    }},
    yAxis: {{
      type: 'value', name: '满意度',
      min: 0, max: 1,
      axisLabel: {{ formatter: function(v) {{ return (v * 100).toFixed(0) + '%'; }}, fontSize: 11 }}
    }},
    series: [{{
      type: 'scatter',
      data: scatterData,
      symbolSize: 24,
      itemStyle: {{ color: COLORS.bar, opacity: 0.75 }},
      emphasis: {{
        scale: 1.5,
        itemStyle: {{ opacity: 1 }},
        label: {{ show: true, formatter: '{{b}}', position: 'top', fontSize: 12 }}
      }},
      label: {{
        show: true,
        formatter: '{{b}}',
        position: 'top',
        fontSize: 11,
        color: '#374151'
      }},
    }}],
    grid: {{ left: 60, right: 40, top: 40, bottom: 50 }}
  }});

  // Click handler: fuse the clicked feature into 数据原声 (focus feed)
  chart.off('click');
  chart.on('click', function(params) {{
    if (params.seriesType === 'scatter') {{
      showFeatureEvidence(params.name);
    }}
  }});
}}

function renderBrandFeatureHeatmap() {{
  var chart = createChart('chart-brand-feature-heatmap');
  if (!chart) return;
  var fd = EMBEDDED.featuresData;
  var matrix = (fd && fd.brand_feature_matrix) ? fd.brand_feature_matrix : {{}};
  var features = (fd && fd.features) ? fd.features : [];

  var topFeatures = features
    .filter(function(f) {{ return f.total > 0; }})
    .slice(0, 10)
    .map(function(f) {{ return f.name; }});

  var brandScores = [];
  Object.keys(matrix).forEach(function(brand) {{
    var total = 0;
    topFeatures.forEach(function(f) {{
      total += (matrix[brand][f] && matrix[brand][f].total) || 0;
    }});
    brandScores.push({{ name: brand, total: total }});
  }});
  brandScores.sort(function(a, b) {{ return b.total - a.total; }});
  var topBrands = brandScores.slice(0, 5).map(function(b) {{ return b.name; }});

  if (topBrands.length === 0 || topFeatures.length === 0) {{
    chart.setOption({{
      title: {{ text: '暂无品牌×功能交叉数据', left: 'center', top: 'center', textStyle: {{ color: '#9ca3af', fontSize: 14 }} }}
    }});
    return;
  }}

  // Build heatmap data with per-cell labels + satisfaction lookup
  var heatData = [];
  var satMap = {{}};
  var maxMentions = 0;

  topFeatures.forEach(function(fname, fi) {{
    topBrands.forEach(function(brand, bi) {{
      var cell = matrix[brand] && matrix[brand][fname];
      var total = (cell && cell.total) || 0;
      var sat = (cell && cell.satisfaction !== null) ? cell.satisfaction : null;
      if (sat !== null && total > 0) {{
        var satPct = (sat * 100).toFixed(0);
        var labelColor = sat >= 0.8 ? '#10b981' : '#ef4444';
        heatData.push({{
          value: [fi, bi, total],
          label: {{
            show: true,
            formatter: total + '次\\n' + satPct + '%',
            color: labelColor,
            fontSize: 12,
            fontWeight: 'bold',
            textShadowColor: 'rgba(255,255,255,0.7)',
            textShadowBlur: 3
          }}
        }});
        satMap[fi + ',' + bi] = sat;
        if (total > maxMentions) maxMentions = total;
      }}
    }});
  }});

  if (heatData.length === 0) {{
    chart.setOption({{
      title: {{ text: '暂无品牌×功能交叉数据', left: 'center', top: 'center', textStyle: {{ color: '#9ca3af', fontSize: 14 }} }}
    }});
    return;
  }}

  chart.setOption({{
    tooltip: {{
      position: 'top',
      formatter: function(p) {{
        var feature = topFeatures[p.value[0]];
        var brand = topBrands[p.value[1]];
        var mentions = p.value[2];
        var sat = satMap[p.value[0] + ',' + p.value[1]];
        var satStr = sat !== undefined ? (sat * 100).toFixed(0) + '%' : 'N/A';
        return '<b>' + brand + '</b> × <b>' + feature + '</b><br/>' +
               '提及量: ' + mentions + ' 次<br/>' +
               '满意度: ' + satStr +
               '<br/><span style=\\"color:#8b5cf6;font-size:11px\\">点击查看证据</span>';
      }}
    }},
    grid: {{ left: 120, right: 50, top: 10, bottom: 80 }},
    xAxis: {{
      type: 'category',
      data: topFeatures,
      axisLabel: {{ rotate: 35, fontSize: 10, interval: 0 }},
      position: 'bottom'
    }},
    yAxis: {{
      type: 'category',
      data: topBrands,
      axisLabel: {{ fontSize: 11 }}
    }},
    visualMap: {{
      min: 0,
      max: maxMentions,
      calculable: true,
      orient: 'horizontal',
      left: 'center',
      bottom: 0,
      inRange: {{ color: ['#bfdbfe', '#1d4ed8'] }}
    }},
    series: [{{
      type: 'heatmap',
      data: heatData,
      emphasis: {{
        itemStyle: {{
          shadowBlur: 10,
          shadowColor: 'rgba(0, 0, 0, 0.3)'
        }}
      }}
    }}]
  }});

  chart.off('click');
  chart.on('click', function(params) {{
    if (params.seriesType === 'heatmap') {{
      var feature = topFeatures[params.value[0]];
      var brand = topBrands[params.value[1]];
      showBrandFeatureEvidence(brand, feature);
    }}
  }});
}}

function renderPersonaFeatureHeatmap() {{
  var chart = createChart('chart-persona-feature-heatmap');
  if (!chart) return;
  var fd = EMBEDDED.featuresData;
  var matrix = (fd && fd.persona_feature_matrix) ? fd.persona_feature_matrix : {{}};
  var features = (fd && fd.features) ? fd.features : [];

  // 无数据时整卡隐藏（与分段轴分布图的隐藏策略一致）
  if (Object.keys(matrix).length === 0) {{
    var cardEl = document.getElementById('card-persona-feature-heatmap');
    if (cardEl) cardEl.style.display = 'none';
    return;
  }}

  var topFeatures = features
    .filter(function(f) {{ return f.total > 0; }})
    .slice(0, 10)
    .map(function(f) {{ return f.name; }});

  var personaScores = [];
  Object.keys(matrix).forEach(function(persona) {{
    var total = 0;
    topFeatures.forEach(function(f) {{
      total += (matrix[persona][f] && matrix[persona][f].total) || 0;
    }});
    personaScores.push({{ name: persona, total: total }});
  }});
  personaScores.sort(function(a, b) {{ return b.total - a.total; }});
  var topPersonas = personaScores.slice(0, 8).map(function(b) {{ return b.name; }});

  if (topPersonas.length === 0 || topFeatures.length === 0) {{
    chart.setOption({{
      title: {{ text: '暂无画像×功能交叉数据', left: 'center', top: 'center', textStyle: {{ color: '#9ca3af', fontSize: 14 }} }}
    }});
    return;
  }}

  var heatData = [];
  var satMap = {{}};
  var maxMentions = 0;

  topFeatures.forEach(function(fname, fi) {{
    topPersonas.forEach(function(persona, pi) {{
      var cell = matrix[persona] && matrix[persona][fname];
      var total = (cell && cell.total) || 0;
      var sat = (cell && cell.satisfaction !== null) ? cell.satisfaction : null;
      if (sat !== null && total > 0) {{
        var satPct = (sat * 100).toFixed(0);
        var labelColor = sat >= 0.8 ? '#10b981' : '#ef4444';
        heatData.push({{
          value: [fi, pi, total],
          label: {{
            show: true,
            formatter: total + '次\\n' + satPct + '%',
            color: labelColor,
            fontSize: 12,
            fontWeight: 'bold',
            textShadowColor: 'rgba(255,255,255,0.7)',
            textShadowBlur: 3
          }}
        }});
        satMap[fi + ',' + pi] = sat;
        if (total > maxMentions) maxMentions = total;
      }}
    }});
  }});

  if (heatData.length === 0) {{
    chart.setOption({{
      title: {{ text: '暂无画像×功能交叉数据', left: 'center', top: 'center', textStyle: {{ color: '#9ca3af', fontSize: 14 }} }}
    }});
    return;
  }}

  chart.setOption({{
    tooltip: {{
      position: 'top',
      formatter: function(p) {{
        var feature = topFeatures[p.value[0]];
        var persona = topPersonas[p.value[1]];
        var mentions = p.value[2];
        var sat = satMap[p.value[0] + ',' + p.value[1]];
        var satStr = sat !== undefined ? (sat * 100).toFixed(0) + '%' : 'N/A';
        return '<b>' + persona + '</b> × <b>' + feature + '</b><br/>' +
               '提及量: ' + mentions + ' 次<br/>' +
               '满意度: ' + satStr +
               '<br/><span style=\\"color:#8b5cf6;font-size:11px\\">点击查看证据</span>';
      }}
    }},
    grid: {{ left: 160, right: 50, top: 10, bottom: 80 }},
    xAxis: {{
      type: 'category',
      data: topFeatures,
      axisLabel: {{ rotate: 35, fontSize: 10, interval: 0 }},
      position: 'bottom'
    }},
    yAxis: {{
      type: 'category',
      data: topPersonas,
      axisLabel: {{ fontSize: 11 }}
    }},
    visualMap: {{
      min: 0,
      max: maxMentions,
      calculable: true,
      orient: 'horizontal',
      left: 'center',
      bottom: 0,
      inRange: {{ color: ['#fde68a', '#b45309'] }}
    }},
    series: [{{
      type: 'heatmap',
      data: heatData,
      emphasis: {{
        itemStyle: {{
          shadowBlur: 10,
          shadowColor: 'rgba(0, 0, 0, 0.3)'
        }}
      }}
    }}]
  }});

  chart.off('click');
  chart.on('click', function(params) {{
    if (params.seriesType === 'heatmap') {{
      var feature = topFeatures[params.value[0]];
      var persona = topPersonas[params.value[1]];
      showPersonaFeatureEvidence(persona, feature);
    }}
  }});
}}

function renderFeatureStatsTable() {{
  var fd = EMBEDDED.featuresData;
  var features = (fd && fd.features) ? fd.features : [];
  var tbody = document.getElementById('feature-stats-tbody');
  var html = '';
  features.forEach(function(f) {{
    var satDisplay = f.satisfaction !== null ? (f.satisfaction * 100).toFixed(1) + '%' : 'N/A';
    var satColor = f.satisfaction !== null
      ? (f.satisfaction >= 0.7 ? 'color:#10b981;font-weight:600' : f.satisfaction >= 0.4 ? 'color:#f59e0b;font-weight:600' : 'color:#ef4444;font-weight:600')
      : 'color:var(--text-tertiary)';
    html += '<tr>';
    html += '<td><strong>' + escapeHtml(f.name) + '</strong></td>';
    html += '<td>' + f.total + '</td>';
    html += '<td style="color:#10b981">' + f.positive + '</td>';
    html += '<td style="color:#ef4444">' + f.negative + '</td>';
    html += '<td style="color:#6b7280">' + f.neutral + '</td>';
    html += '<td style="' + satColor + '">' + satDisplay + '</td>';
    html += '<td style="font-size:12px;color:var(--text-secondary);max-width:260px;line-height:1.4;">' + escapeHtml(f.reason || '') + '</td>';
    html += '</tr>';
  }});
  if (html === '') {{
    html = '<tr><td colspan="7" style="text-align:center;color:#9ca3af;padding:24px;">暂无功能体验数据</td></tr>';
  }}
  tbody.innerHTML = html;
}}

// ======== Data Raw Feed (数据原声) ========
var explorerSortField = 'published';
var explorerSortDir = 'desc';
var explorerPage = 1;
var explorerPageSize = 30;
// 图表下钻聚焦状态：非空时，数据原声页仅展示该结果关联的原始帖子，实现「下钻即融合」
var explorerFocus = null;

// 维度键 → 中文标签（用于聚焦横幅标题）
var DIM_LABELS = {{
  usage_scenarios: '使用场景',
  user_personas: '人群画像',
  user_personas_purchased: '购买者人群画像',
  user_personas_non_purchased: '未购买者人群画像',
  user_personas_comments: '评论者人群画像',
  pre_purchase_concerns: '购买前关注点',
  post_purchase_experience: '购买后体验',
  brand_mentions: '品牌分布',
  unmet_needs: '未满足需求'{EXTRA_DIM_LABELS_JS}
}};

// 读取当前筛选条件（搜索 / 相关性 / 品牌 / 子版块）
function getExplorerFilterCriteria() {{
  var rawSearch = (document.getElementById('explorer-search') || {{value: ''}}).value.trim();
  var keywords = rawSearch ? rawSearch.toLowerCase().split(/\\s+/).filter(function(k) {{ return k.length > 0; }}) : [];
  var bucket = (document.getElementById('explorer-bucket') || {{value: ''}}).value;
  var brand = (document.getElementById('explorer-brand') || {{value: ''}}).value.trim();
  var subreddit = (document.getElementById('explorer-{T_CH_SLUG}') || {{value: ''}}).value.trim();
  return {{ keywords: keywords, bucket: bucket, brand: brand, subreddit: subreddit }};
}}

// 单条帖子是否匹配筛选条件
function matchExplorerCriteria(p, c) {{
  if (c.keywords.length > 0) {{
    var text = ((p.title || '') + ' ' + (p.summary_text || '')).toLowerCase();
    for (var i = 0; i < c.keywords.length; i++) {{ if (text.indexOf(c.keywords[i]) === -1) return false; }}
  }}
  if (c.bucket && (p.voc_bucket || '') !== c.bucket) return false;
  if (c.brand) {{
    var brands = (p.mentioned_brands || '').toLowerCase();
    if (brands.indexOf(c.brand.toLowerCase()) === -1) return false;
  }}
  if (c.subreddit && (p.subreddit || '').toLowerCase() !== c.subreddit.toLowerCase()) return false;
  return true;
}}

function getExplorerFilteredPosts() {{
  var posts = EMBEDDED.relevantPosts.length > 0 ? EMBEDDED.relevantPosts : EMBEDDED.allPosts;
  var c = getExplorerFilterCriteria();
  return posts.filter(function(p) {{ return matchExplorerCriteria(p, c); }});
}}

// 评论证据映射：把 comment_id -> 评论行（来自红线证据里以评论为单元的下钻）
function buildCommentMap() {{
  if (window.__commentMap) return window.__commentMap;
  var m = {{}};
  var src = EMBEDDED.evidenceComments || {{}};
  for (var k in src) {{ if (Object.prototype.hasOwnProperty.call(src, k)) m[k] = src[k]; }}
  window.__commentMap = m;
  return m;
}}

// 将证据条目（含 reddit_id / mention_count / evidence_quote）解析为「完整帖子/评论 + 提及次数 + 原文线索」
// 先按帖子 id 解析；解析不到时回退到评论（评论作为独立计数单元的场景）。
function resolveEvidenceItems(posts) {{
  var pm = buildPostMap();
  var cm = buildCommentMap();
  var items = [];
  for (var i = 0; i < posts.length; i++) {{
    var ep = posts[i];
    var rid = (ep.reddit_id || '').trim();
    var full = pm[rid] || null;
    var isComment = false;
    if (!full && cm[rid]) {{
      var c = cm[rid];
      var parent = pm[c.post_reddit_id] || null;
      full = {{
        reddit_id: c.comment_id || rid,
        title: '{T_SPEAKER} @' + (c.author || '匿名') + ' 在{T_THREAD_REF}《' + ((parent && parent.title) || c.post_title || '') + '》下的发言',
        summary_text: c.body || '',
        subreddit: (parent && parent.subreddit) || c.post_subreddit || '',
        voc_bucket: c.is_root_comment === 'False' ? '评论·回复' : '评论',
        voc_score: '',
        mentioned_brands: '',
        author: c.author || '',
        published: c.published || '',
        url: (parent && parent.url) || ''
      }};
      isComment = true;
    }}
    if (!full) continue;
    items.push({{ post: full, mention: ep.mention_count || 1, quote: ep.evidence_quote || '', isComment: isComment }});
  }}
  return items;
}}

// 渲染单张帖子卡片（数据原声统一外观）；聚焦模式下附带 ×N 提及徽标与原文线索
function renderPostCard(p, idx, opts) {{
  opts = opts || {{}};
  var bucketClass = '';
  var bucketLabel = p.voc_bucket || '';
  if (bucketLabel && bucketLabel.indexOf('评论') === 0) bucketClass = 'badge-comment';
  else if (bucketLabel && bucketLabel.charAt(0) === 'A') bucketClass = 'badge-a';
  else if (bucketLabel && bucketLabel.charAt(0) === 'B') bucketClass = 'badge-b';
  else if (bucketLabel && bucketLabel.charAt(0) === 'C') bucketClass = 'badge-c';
  else bucketClass = 'badge-d';

  var title = p.title || '(无标题)';
  var summary = p.summary_text || '(无摘要)';
  // 反转义，使 CSV 中的 HTML 实体（如 &#39;）正确显示，且与翻译表键对齐
  var titleRaw = unescapeHtml(title);
  var summaryRaw = unescapeHtml(summary);
  // 聚焦模式下展示原文线索时，正文（标题/摘要）中对应的文字需高亮展示；
  // 同时取消摘要截断，确保高亮内容可见。
  // 关键：原文线索 quote 由 || 分隔不连续片段、片段内由 … 省略；把标题与摘要拼回整体
  // 做精确匹配，再把命中区间投影回各自字段，保证高亮连续。
  var hl = opts.quote ? highlightQuoteInPost(titleRaw, summaryRaw, opts.quote) : null;
  var renderedTitle = hl ? hl.title : escapeHtml(titleRaw);
  var renderedSummary = hl ? hl.summary : escapeHtml(summaryRaw);
  var longSummary = summaryRaw.length > 500 && !opts.quote;

  var h = '<div class="post-card">';
  h += '<div class="post-card-title">' + renderedTitle;
  if (opts.mention && opts.mention > 1) {{
    h += ' <span class="post-card-mention" title="该帖在此条目下被提及 ' + opts.mention + ' 次">×' + opts.mention + '</span>';
  }}
  h += '</div>';
  h += '<div class="post-card-summary' + (longSummary ? ' clamped' : '') + '" id="exp-sum-' + idx + '">' + renderedSummary + '</div>';
  if (longSummary) {{
    h += '<a class="post-card-more" id="exp-tog-' + idx + '" onclick="toggleSummary(\\'exp\\',' + idx + ')">展开全文</a>';
  }}
  if (opts.quote) {{
    // 原文线索按 || 切分为不连续片段，逐段展示，可兼容跨段落的多段原文
    String(opts.quote).split('||').forEach(function(seg) {{
      var segRaw = unescapeHtml(seg.trim());
      if (!segRaw) return;
      h += '<div class="post-card-quote">' + escapeHtml(segRaw) + '</div>';
    }});
  }}
  h += '<div class="post-card-meta">';
  h += '<span class="badge ' + bucketClass + '">' + escapeHtml(bucketLabel) + '</span>';
  h += '<span class="meta-item">{T_CHANNEL_PREFIX}' + escapeHtml(p.subreddit || '?') + '</span>';
  h += '<span class="meta-item">分数 ' + (p.voc_score || 'N/A') + '</span>';
  if (p.mentioned_brands && p.mentioned_brands.trim()) h += '<span class="meta-item">品牌 ' + escapeHtml(p.mentioned_brands) + '</span>';
  if (p.author) h += '<span class="meta-item">@' + escapeHtml(p.author) + '</span>';
  h += '<span class="meta-item">' + escapeHtml((p.published || '').substring(0, 10)) + '</span>';
  if (p.url) h += '<a class="meta-link" href="' + escapeHtml(p.url) + '" target="_blank" rel="noopener">{T_LINK_LABEL}</a>';
  h += '</div>';
  h += '</div>';
  return h;
}}

// ======== 原文线索高亮（数据原声聚焦） ========
// 将「原文线索」(quote) 对应的文字在帖子标题/摘要正文中高亮展示，让用户一眼看到线索在原文中的位置。
//
// 关键：quote 是「标题 + 换行 + 摘要」的子串（见 link_posts.extract_snippet）。
// 此处把标题与摘要拼回整体做精确匹配，再把命中区间按边界投影回各自字段。
// 仅做整段精确匹配；当 quote 中间含省略号「…」（或连续三个英文点「...」，代表被省略的原文）时，
// 按省略号拆成多段、各段分别精确匹配，使被省略部分两侧的真实片段都能独立高亮（不强行连成一段）；
// 某段仍无法在原文匹配（如 LLM 改写/散词）则该段不高亮（不做单词级回退）。
//
// 归一化：原文线索与帖子正文的 Unicode 标点形态常不一致（如直撇号 ' 与弯撇号 ’、
// 弯引号 “” 、破折号 –— 、不间断空格），导致精确匹配失败。匹配前统一归一为 ASCII 等价字符。
// 映射为一对一字符映射，故归一后的匹配位置对原始文本仍然有效，展示时仍使用原文真实字符。
function normalizeForMatch(str) {{
  return String(str)
    .replace(/[’‘]/g, "'")      // 弯单撇号/引号 -> 直撇号
    .replace(/[“”]/g, '"')      // 弯双引号 -> 直双引号
    .replace(/[–—]/g, '-')      // 破折号/连接号 -> 连字符
    .replace(/\u00a0/g, ' ');   // 不间断空格 -> 普通空格
}}
function highlightQuoteInPost(title, summary, quote) {{
  if (!quote) return {{ title: escapeHtml(title || ''), summary: escapeHtml(summary || '') }};
  var t = String(title || '');
  var s = String(summary || '');
  var q = unescapeHtml(String(quote)).replace(/^[\\s…]+/, '').replace(/[\\s…]+$/, '');
  if (q.length < 3) return {{ title: escapeHtml(t), summary: escapeHtml(s) }};

  var sep = '\\n';
  var combined = t + sep + s;
  var combinedN = normalizeForMatch(combined).toLowerCase();
  var titleLen = t.length, sepLen = sep.length, sumBase = titleLen + sepLen;

  // 按省略号拆成多段分别精确匹配（含 \\n→空格 兜底）；「...」先归一为「…」
  var normQ = q.replace(/\\.\\.\\./g, '…');
  // 先按 || 切分为不连续片段，每段再按省略号（… 或 ...）切分，逐段精确匹配
  var parts = [];
  normQ.split('||').forEach(function(seg) {{
    seg.split('…').forEach(function(p) {{
      p = p.trim();
      if (p.length >= 3) parts.push(p);
    }});
  }});
  if (parts.length === 0) return {{ title: escapeHtml(t), summary: escapeHtml(s) }};

  var ranges = [];  // combined 坐标下的命中区间（每项含 start/end）
  parts.forEach(function(part) {{
    var needle = normalizeForMatch(part);
    var pos = combinedN.indexOf(needle.toLowerCase());
    if (pos < 0 && needle.indexOf('\\n') >= 0) {{
      var needleSp = normalizeForMatch(needle.replace(/\\n/g, ' '));
      var p2 = combinedN.indexOf(needleSp.toLowerCase());
      if (p2 >= 0) {{ pos = p2; needle = needleSp; }}
    }}
    if (pos >= 0) ranges.push({{ start: pos, end: pos + needle.length }});
  }});

  if (ranges.length === 0) {{
    // 没有任何片段能在原文匹配（如 LLM 改写/散词）→ 不高亮，避免单词级错误高亮
    return {{ title: escapeHtml(t), summary: escapeHtml(s) }};
  }}

  // 将各命中区间投影到 title / summary 两段，分别做多区间高亮
  var titleHtml = highlightRangesInField(t, 0, titleLen, ranges);
  var summaryHtml = highlightRangesInField(s, sumBase, combined.length, ranges);
  return {{ title: titleHtml, summary: summaryHtml }};
}}

// 在单个字段内对多个命中区间（combined 坐标）高亮：投影到本地偏移、合并重叠区间、其余转义
function highlightRangesInField(fieldText, base, end, ranges) {{
  var local = [];
  ranges.forEach(function(r) {{
    var a = Math.max(r.start, base), b = Math.min(r.end, end);
    if (a < b) local.push({{ start: a - base, end: b - base }});
  }});
  if (local.length === 0) return escapeHtml(fieldText);
  local.sort(function(x, y) {{ return x.start - y.start; }});
  var merged = [local[0]];
  for (var i = 1; i < local.length; i++) {{
    var last = merged[merged.length - 1];
    // 仅合并重叠区间；相邻（被省略部分）不强行连成一块，保留两段独立高亮
    if (local[i].start < last.end) last.end = Math.max(last.end, local[i].end);
    else merged.push(local[i]);
  }}
  var out = '', cursor = 0;
  merged.forEach(function(m) {{
    out += escapeHtml(fieldText.slice(cursor, m.start));
    out += '<mark class="quote-hit">' + escapeHtml(fieldText.slice(m.start, m.end)) + '</mark>';
    cursor = m.end;
  }});
  out += escapeHtml(fieldText.slice(cursor));
  return out;
}}

function renderExplorerPagination(totalPages) {{
  var pagDiv = document.getElementById('explorer-pagination');
  var pagHtml = '';
  if (totalPages > 1) {{
    pagHtml += '<button ' + (explorerPage <= 1 ? 'disabled' : '') + ' onclick="explorerPage=' + (explorerPage - 1) + ';renderExplorerTable();">‹ 上一页</button>';
    for (var pg = 1; pg <= totalPages; pg++) {{
      pagHtml += '<button class="' + (pg === explorerPage ? 'active' : '') + '" onclick="explorerPage=' + pg + ';renderExplorerTable();">' + pg + '</button>';
    }}
    pagHtml += '<button ' + (explorerPage >= totalPages ? 'disabled' : '') + ' onclick="explorerPage=' + (explorerPage + 1) + ';renderExplorerTable();">下一页 ›</button>';
  }}
  pagDiv.innerHTML = pagHtml;
}}

// 切换到数据原声页
function switchToExplorer() {{
  document.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
  document.querySelectorAll('.page').forEach(function(p) {{ p.classList.remove('active'); }});
  document.querySelector('.tab-btn[data-page="page-explorer"]').classList.add('active');
  document.getElementById('page-explorer').classList.add('active');
}}

// 聚焦数据原声页：仅展示某图表结果关联的原始帖子
function focusExplorer(title, sub, note, items) {{
  explorerFocus = {{ title: title, sub: sub, note: note || '', items: items }};
  // 清除其它筛选条件，避免与聚焦结果混淆
  var s = document.getElementById('explorer-search'); if (s) s.value = '';
  var b = document.getElementById('explorer-bucket'); if (b) b.value = '';
  var br = document.getElementById('explorer-brand'); if (br) br.value = '';
  var sb = document.getElementById('explorer-{T_CH_SLUG}'); if (sb) sb.value = '';
  var so = document.getElementById('explorer-sort'); if (so) so.value = 'natural:desc';
  explorerSortField = 'natural';
  explorerSortDir = 'desc';
  explorerPage = 1;
  switchToExplorer();
  renderExplorerTable();
}}

// 清除聚焦，返回全部原声
function clearExplorerFocus() {{
  explorerFocus = null;
  var f = document.getElementById('explorer-focus'); if (f) f.style.display = 'none';
  var n = document.getElementById('explorer-focus-note'); if (n) {{ n.style.display = 'none'; n.innerHTML = ''; }}
  explorerSortField = 'published';
  explorerSortDir = 'desc';
  var so = document.getElementById('explorer-sort'); if (so) so.value = 'published:desc';
  explorerPage = 1;
  renderExplorerTable();
}}

// 聚焦模式下的信息流渲染
function renderExplorerFocus() {{
  document.getElementById('explorer-focus').style.display = 'flex';
  document.getElementById('explorer-focus-title').textContent = explorerFocus.title;
  document.getElementById('explorer-focus-sub').textContent = explorerFocus.sub;
  var noteEl = document.getElementById('explorer-focus-note');
  if (explorerFocus.note) {{ noteEl.style.display = 'block'; noteEl.textContent = explorerFocus.note; }}
  else {{ noteEl.style.display = 'none'; noteEl.innerHTML = ''; }}

  var c = getExplorerFilterCriteria();
  var items = explorerFocus.items.filter(function(it) {{ return matchExplorerCriteria(it.post, c); }});

  items.sort(function(a, b) {{
    if (explorerSortField === 'natural') return 0;
    var va = (a.post[explorerSortField] || '').toString();
    var vb = (b.post[explorerSortField] || '').toString();
    if (!isNaN(va) && !isNaN(vb) && va !== '' && vb !== '') {{ va = parseFloat(va); vb = parseFloat(vb); }}
    var cmp = va < vb ? -1 : va > vb ? 1 : 0;
    return explorerSortDir === 'asc' ? cmp : -cmp;
  }});

  var totalPages = Math.ceil(items.length / explorerPageSize);
  explorerPage = Math.min(explorerPage, totalPages);
  if (explorerPage < 1) explorerPage = 1;
  var start = (explorerPage - 1) * explorerPageSize;
  var page = items.slice(start, start + explorerPageSize);

  var feed = document.getElementById('explorer-feed');
  var html = '';
  if (page.length === 0) {{
    html = '<div class="feed-empty">该结果暂无关联的原始{T_UNIT}，或当前筛选条件下无匹配。</div>';
  }}
  for (var i = 0; i < page.length; i++) {{
    html += renderPostCard(page[i].post, start + i, {{ mention: page[i].mention, quote: page[i].quote }});
  }}
  feed.innerHTML = html;

  var shownFrom = items.length === 0 ? 0 : (start + 1);
  var shownTo = start + page.length;
  document.getElementById('explorer-count').textContent = '共 ' + items.length + ' 条关联{T_UNIT}，当前显示第 ' + shownFrom + '–' + shownTo + ' 条';

  renderExplorerPagination(totalPages);
}}

function sortExplorerTable(val) {{
  var parts = (val || 'published:desc').split(':');
  explorerSortField = {T_SORT_FIELD_EXPR} || 'published';
  explorerSortDir = parts[1] || 'desc';
  explorerPage = 1;
  renderExplorerTable();
}}

function renderExplorerTable() {{
  if (explorerFocus) {{ renderExplorerFocus(); return; }}
  var filtered = getExplorerFilteredPosts();
  filtered.sort(function(a, b) {{
    var va = (a[explorerSortField] || '').toString();
    var vb = (b[explorerSortField] || '').toString();
    if (!isNaN(va) && !isNaN(vb) && va !== '' && vb !== '') {{ va = parseFloat(va); vb = parseFloat(vb); }}
    var cmp = va < vb ? -1 : va > vb ? 1 : 0;
    return explorerSortDir === 'asc' ? cmp : -cmp;
  }});

  var totalPages = Math.ceil(filtered.length / explorerPageSize);
  explorerPage = Math.min(explorerPage, totalPages);
  if (explorerPage < 1) explorerPage = 1;
  var start = (explorerPage - 1) * explorerPageSize;
  var page = filtered.slice(start, start + explorerPageSize);

  var feed = document.getElementById('explorer-feed');
  var html = '';
  if (page.length === 0) {{
    html = '<div class="feed-empty">没有符合筛选条件的{T_UNIT}，请调整搜索关键词或筛选条件。</div>';
  }}
  for (var i = 0; i < page.length; i++) {{
    html += renderPostCard(page[i], start + i, {{}});
  }}
  feed.innerHTML = html;

  var shownFrom = filtered.length === 0 ? 0 : (start + 1);
  var shownTo = start + page.length;
  document.getElementById('explorer-count').textContent = '共 ' + filtered.length + ' 条，当前显示第 ' + shownFrom + '–' + shownTo + ' 条';

  renderExplorerPagination(totalPages);
}}

// ======== Summary inline expand/collapse ========
function toggleSummary(prefix, idx) {{
  var textEl = document.getElementById(prefix + '-sum-' + idx);
  var toggleEl = document.getElementById(prefix + '-tog-' + idx);
  if (!textEl || !toggleEl) return;
  var isClamped = textEl.classList.contains('clamped');
  if (isClamped) {{
    textEl.classList.remove('clamped');
    toggleEl.textContent = '收起';
  }} else {{
    textEl.classList.add('clamped');
    toggleEl.textContent = '展开全文';
  }}
}}

// ======== Brand/Subreddit filter populate ========
function populateFilters() {{
  var allBrands = new Set();
  var allSubreddits = new Set();
  EMBEDDED.allPosts.forEach(function(p) {{
    var brands = (p.mentioned_brands || '').split(';');
    brands.forEach(function(b) {{ if (b.trim()) allBrands.add(b.trim()); }});
    if (p.subreddit) allSubreddits.add(p.subreddit);
  }});

  var sortedBrands = Array.from(allBrands).sort();
  var sortedSR = Array.from(allSubreddits).sort();

  ['source-brand', 'explorer-brand'].forEach(function(id) {{
    var sel = document.getElementById(id);
    if (!sel) return;
    for (var i = 1; i < sel.options.length; i++) sel.remove(i);
    sortedBrands.forEach(function(b) {{
      var opt = document.createElement('option');
      opt.value = b;
      opt.textContent = b;
      sel.appendChild(opt);
    }});
  }});

  ['source-{T_CH_SLUG}', 'explorer-{T_CH_SLUG}'].forEach(function(id) {{
    var sel = document.getElementById(id);
    if (!sel) return;
    for (var i = 1; i < sel.options.length; i++) sel.remove(i);
    sortedSR.forEach(function(s) {{
      var opt = document.createElement('option');
      opt.value = s;
      opt.textContent = 'r/' + s;
      sel.appendChild(opt);
    }});
  }});
}}

// ======== Navigation Helpers ========
// 仅清除聚焦状态（不触发重渲染），供其它导航入口复用
function resetExplorerFocus() {{
  explorerFocus = null;
  var f = document.getElementById('explorer-focus'); if (f) f.style.display = 'none';
  var n = document.getElementById('explorer-focus-note'); if (n) {{ n.style.display = 'none'; n.innerHTML = ''; }}
  explorerSortField = 'published';
  explorerSortDir = 'desc';
  var so = document.getElementById('explorer-sort'); if (so) so.value = 'published:desc';
}}

function navigateToExplorer(keyword, bucketVal) {{
  resetExplorerFocus();
  document.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
  document.querySelector('.tab-btn[data-page="page-explorer"]').classList.add('active');
  document.querySelectorAll('.page').forEach(function(p) {{ p.classList.remove('active'); }});
  document.getElementById('page-explorer').classList.add('active');
  if (keyword) document.getElementById('explorer-search').value = keyword;
  if (bucketVal) document.getElementById('explorer-bucket').value = bucketVal;
  explorerPage = 1;
  renderExplorerTable();
}}

// ======== Chart drill-down → 数据原声 (fusion) ========
var postMap = null;
function buildPostMap() {{
  if (postMap) return postMap;
  postMap = {{}};
  EMBEDDED.allPosts.forEach(function(p) {{
    var rid = (p.reddit_id || '').trim();
    if (rid) postMap[rid] = p;
  }});
  return postMap;
}}

// 汇总关联帖子数与总提及次数（用于聚焦横幅副标题）
function evidenceSummary(posts) {{
  var n = posts.length;
  var m = 0;
  for (var i = 0; i < posts.length; i++) {{ m += (posts[i].mention_count || 1); }}
  return {{ posts: n, mentions: m }};
}}

// 分析结果图表下钻：跳转到数据原声页并聚焦该维度分类关联的原始帖子
function showEvidence(dimKey, labelCn) {{
  var dims = EMBEDDED.dimensions;
  if (!dims || !dims[dimKey]) {{
    focusExplorer('分析结果', '暂无数据', '当前无 LLM 分析数据，请先运行 Phase 4 分析生成维度文件后重新生成看板。', []);
    return;
  }}
  var categories = dims[dimKey];
  var matched = null;
  for (var i = 0; i < categories.length; i++) {{
    if (categories[i].label_cn === labelCn) {{ matched = categories[i]; break; }}
  }}
  if (!matched) {{
    var dl = DIM_LABELS[dimKey] || dimKey;
    focusExplorer(dl, labelCn, '未找到「' + labelCn + '」的证据数据。', []);
    return;
  }}
  var posts = matched.evidence_posts || [];
  var summ = evidenceSummary(posts);
  var dimLabel = DIM_LABELS[dimKey] || dimKey;
  var title = dimLabel + ' › ' + matched.label_cn;
  var items = resolveEvidenceItems(posts);
  // 人群画像是「帖子 + 评论」混合证据，计数单位不能按维度键一刀切，按实际解析结果决定
  var nCmt = items.filter(function(it) {{ return it.isComment; }}).length;
  var nPost = items.length - nCmt;
  var unitStr, unitWord;
  if (nPost && nCmt) {{ unitStr = ' 个单元'; unitWord = '个'; }}
  else if (nCmt) {{ unitStr = ' 条{T_SECOND}'; unitWord = '条'; }}
  else {{ unitStr = ' 篇{T_UNIT}'; unitWord = '篇'; }}
  var declared = matched.count || summ.posts;
  var sub = '关联 ' + declared + unitStr + ' · 共 ' + summ.mentions + ' 次提及';
  if (declared > summ.posts) sub += '（下钻展示其中 ' + summ.posts + unitWord + '抽样证据）';
  if (nPost && nCmt) sub += '（帖 ' + nPost + ' · 评论 ' + nCmt + '）';
  var note = '';
  if (matched.reason) note += '归类原因：' + matched.reason;
  if (matched.description) note += (note ? '\\n' : '') + matched.description;
  focusExplorer(title, sub, note, items);
}}

// 功能体验散点下钻：聚焦该功能关联的原始帖子
function showFeatureEvidence(featureName) {{
  var fd = EMBEDDED.featuresData;
  var features = (fd && fd.features) ? fd.features : [];
  var matched = null;
  for (var i = 0; i < features.length; i++) {{
    if (features[i].name === featureName) {{ matched = features[i]; break; }}
  }}
  if (!matched) {{
    focusExplorer('功能体验', featureName, '未找到「' + featureName + '」的功能体验数据。', []);
    return;
  }}
  var posts = matched.evidence_posts || [];
  var summ = evidenceSummary(posts);
  var satStr = matched.satisfaction !== null ? (matched.satisfaction * 100).toFixed(0) + '%' : 'N/A';
  var title = '功能体验 › ' + matched.name;
  var sub = '关联 ' + summ.posts + ' 篇{T_UNIT} · 共 ' + summ.mentions + ' 次提及 | 正向 ' + matched.positive + ' · 负向 ' + matched.negative + ' · 中性 ' + matched.neutral + ' · 满意度 ' + satStr;
  var items = resolveEvidenceItems(posts);
  focusExplorer(title, sub, matched.reason || '', items);
}}

// Brand × Feature evidence drill-down (heatmap cell clicks)
function showBrandFeatureEvidence(brandName, featureName) {{
  var fd = EMBEDDED.featuresData;
  var matrix = (fd && fd.brand_feature_matrix) ? fd.brand_feature_matrix : {{}};
  var cell = (matrix[brandName] && matrix[brandName][featureName]) || {{}};
  var satStr = cell.satisfaction !== null ? (cell.satisfaction * 100).toFixed(0) + '%' : 'N/A';

  var features = (fd && fd.features) ? fd.features : [];
  var matchedFeature = null;
  for (var i = 0; i < features.length; i++) {{
    if (features[i].name === featureName) {{ matchedFeature = features[i]; break; }}
  }}

  var evidencePosts = (matchedFeature && matchedFeature.evidence_posts) ? matchedFeature.evidence_posts : [];
  // 仅保留同时提及该品牌的帖子
  var brandPosts = [];
  var pm = buildPostMap();
  for (var j = 0; j < evidencePosts.length; j++) {{
    var rid = (evidencePosts[j].reddit_id || '').trim();
    var fullPost = pm[rid] || null;
    var postBrands = fullPost ? (fullPost.mentioned_brands || '').toLowerCase() : '';
    if (postBrands.indexOf(brandName.toLowerCase()) >= 0) {{ brandPosts.push(evidencePosts[j]); }}
  }}

  var title = '品牌×功能 › ' + brandName + ' × ' + featureName;
  var sub = '该品牌此维度关联 ' + brandPosts.length + ' 篇{T_UNIT} · 提及 ' + (cell.total || 0) + ' 次 · 满意度 ' + satStr;
  if (brandPosts.length === 0) {{
    sub += '（未找到该品牌专属{T_UNIT}，已回退展示该功能全部关联{T_UNIT}）';
    brandPosts = evidencePosts;
  }}
  var items = resolveEvidenceItems(brandPosts);
  focusExplorer(title, sub, '', items);
}}

// Persona × Feature evidence drill-down (heatmap cell clicks)
function showPersonaFeatureEvidence(personaName, featureName) {{
  var fd = EMBEDDED.featuresData;
  var matrix = (fd && fd.persona_feature_matrix) ? fd.persona_feature_matrix : {{}};
  var cell = (matrix[personaName] && matrix[personaName][featureName]) || {{}};
  var satStr = cell.satisfaction !== null && cell.satisfaction !== undefined ? (cell.satisfaction * 100).toFixed(0) + '%' : 'N/A';

  // 矩阵单元格的 evidence_posts 本身就是该画像×功能交集的帖子，直接用，无需二次过滤
  var evidencePosts = (cell.evidence_posts) ? cell.evidence_posts : [];

  var title = '画像×功能 › ' + personaName + ' × ' + featureName;
  var sub = '该画像此维度关联 ' + evidencePosts.length + ' 篇{T_UNIT} · 提及 ' + (cell.total || 0) + ' 次 · 满意度 ' + satStr;
  var items = resolveEvidenceItems(evidencePosts);
  focusExplorer(title, sub, '', items);
}}

function filterBucket(bucketVal) {{
  resetExplorerFocus();
  document.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
  document.querySelectorAll('.page').forEach(function(p) {{ p.classList.remove('active'); }});
  var targetPage = document.getElementById('page-explorer');
  document.querySelector('.tab-btn[data-page="page-explorer"]').classList.add('active');
  targetPage.classList.add('active');
  document.getElementById('explorer-bucket').value = bucketVal;
  explorerPage = 1;
  renderExplorerTable();
}}

function escapeHtml(str) {{
  return String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}

// ======== Handle hash navigation ========
function handleHash() {{
  var hash = window.location.hash.replace('#', '');
  if (!hash) return;
  var pageId = 'page-' + hash.split('?')[0];
  var btn = document.querySelector('.tab-btn[data-page="' + pageId + '"]');
  if (btn) btn.click();
}}

// ======== Init ========
function renderMethodologySearchStrategy() {{
  var container = document.getElementById('methodology-search-strategy');
  if (!container) return;

  var queries = {{}};
  EMBEDDED.allPosts.forEach(function(p) {{
    if (p.query) queries[p.query] = (queries[p.query] || 0) + 1;
  }});
  var topQueries = Object.keys(queries).sort(function(a,b){{ return queries[b] - queries[a]; }});
  var queryTags = topQueries.map(function(q) {{ return '<span class=\"tag-item\">' + escapeHtml(q) + '</span>'; }}).join('');

  var subData = EMBEDDED.subreddits;
  var subTags = '';
  if (subData && subData.labels) {{
    var maxSubs = Math.min(subData.labels.length, 15);
    for (var i = 0; i < maxSubs; i++) {{
      subTags += '<span class=\"tag-item\">{T_CHANNEL_PREFIX}' + escapeHtml(subData.labels[i]) + '</span>';
    }}
  }}

  var brandTerms = (EMBEDDED.stats && EMBEDDED.stats.brand_terms) ? EMBEDDED.stats.brand_terms : [];
  var brandTags = brandTerms.map(function(b) {{ return '<span class=\"tag-item\">' + escapeHtml(b) + '</span>'; }}).join('');

  var kws = (EMBEDDED.stats && EMBEDDED.stats.keywords) ? EMBEDDED.stats.keywords : [];
  var kwTags = kws.map(function(k) {{ return '<span class=\"tag-item\">' + escapeHtml(k) + '</span>'; }}).join('');

  var html = '';
  if (queryTags) html += '<p>搜索关键词 (' + topQueries.length + ' 组): <span class=\"tag-list\">' + queryTags + '</span></p>';
  if (subTags) html += '<p>搜索{T_CHANNEL} (TOP ' + Math.min(subData.labels.length, 15) + '): <span class=\"tag-list\">' + subTags + '</span></p>';
  if (brandTags) html += '<p>品牌列表 (' + brandTerms.length + ' 个): <span class=\"tag-list\">' + brandTags + '</span></p>';
  if (kwTags) html += '<p>品类关键词 (' + kws.length + ' 个): <span class=\"tag-list\">' + kwTags + '</span></p>';
  container.innerHTML = html;
}}

function init() {{
  populateFilters();
  renderOverviewCharts();
  renderMethodologySearchStrategy();
  handleHash();
}}
window.addEventListener('hashchange', handleHash);
window.addEventListener('resize', function() {{
  var activePage = document.querySelector('.page.active');
  if (!activePage) return;
  if (activePage.id === 'page-overview') renderOverviewCharts();
  if (activePage.id === 'page-analysis') renderAnalysisCharts();
  if (activePage.id === 'page-brands') renderBrandCharts();
}});
init();
</script>
</body>
</html>"""

    return html


def main():
    args = parse_args()

    # 源画像（决定用户可见文案与模块开关；空 → Reddit，保持旧行为）
    profile = load_profile(args.profile)

    # Read data files（role 决定补哪些旧列名：posts / comments）
    stats = read_stats(args.stats)
    tagged = read_csv_safe(args.tagged, "posts")
    relevant = read_csv_safe(args.relevant, "posts") if args.relevant else []
    comments = read_csv_safe(args.comments, "comments") if args.comments else []

    if not tagged:
        print("ERROR: No tagged posts found. Aborting dashboard generation.", file=sys.stderr)
        sys.exit(1)

    # Infer stats if JSON is incomplete
    if not stats:
        raw_total = len(tagged)
        # Count buckets
        bucket_counts = Counter()
        for row in tagged:
            bucket = (row.get("voc_bucket") or "").strip()
            if bucket:
                bucket_counts[bucket] += 1

        stats = {
            "raw_total": raw_total,
            "expired_count": 0,
            "after_date_clean": raw_total,
            "bucket_A": bucket_counts.get("A_强相关VOC", 0),
            "bucket_B": bucket_counts.get("B_可参考", 0),
            "bucket_C": bucket_counts.get("C_推广/弱参考", 0),
            "bucket_D": bucket_counts.get("D_噪声/低相关", 0),
        }
        stats["final_analysis"] = stats["bucket_A"] + stats["bucket_B"]

    data = {
        "stats": stats,
        "tagged": tagged,
        "relevant": relevant,
        "comments": comments,
        "brands": build_brand_frequencies(relevant if relevant else tagged),
        "subreddits": build_subreddit_distribution(tagged),
        "timeline": build_time_series(tagged),
    }

    dimensions = None
    if args.dimensions:
        try:
            with open(args.dimensions, "r") as f:
                dims = json.load(f)
                dimensions = dims
                print(f"  Dimensions loaded: {sum(len(v) for v in dims.values() if isinstance(v, list))} categories across 5 dimensions")
        except Exception as e:
            print(f"Warning: failed to load dimensions JSON {args.dimensions}: {e}", file=sys.stderr)

    # 分析框架（Stage 5）：从产物读（aggregate 已把 analysis_frame 写进 dimensions.json）；
    # 老产物没有这个块时回落到 --frame / 内置 consumer。
    frame = load_frame_from_product(dimensions, default=(args.frame or ""))

    features_data = None
    if args.features_json:
        try:
            with open(args.features_json, "r") as f:
                features_data = json.load(f)
                feat_count = len(features_data.get("features", []))
                brand_count = len(features_data.get("brand_feature_matrix", {}))
                persona_count = len(features_data.get("persona_feature_matrix", {}))
                print(f"  Features loaded: {feat_count} features, {brand_count} brands, {persona_count} persona rows")
        except Exception as e:
            print(f"Warning: failed to load features JSON {args.features_json}: {e}", file=sys.stderr)

    title = args.title
    queries = args.queries
    subreddits = args.subreddits
    brands = args.brands
    keywords = args.keywords
    logo_uri = "" if args.no_logo else load_logo_as_datauri(args.logo)

    html = generate_html(data, args.category, title, queries, subreddits, brands, keywords, args.max_age_years, logo_uri, dimensions, features_data, profile, frame)

    out_dir = os.path.dirname(os.path.abspath(args.out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)

    file_size_kb = os.path.getsize(args.out) / 1024
    print(f"Dashboard generated: {args.out} ({file_size_kb:.1f} KB)")
    print(f"  Source profile: {profile.source} ({profile.label})")
    print(f"  Pages: 6 (数据看板, 分析结果, 功能体验, 品牌满意度, 数据原声, 数据说明)")
    print(f"  Posts: {len(tagged)} total, {len(relevant) if relevant else stats.get('final_analysis', 0)} relevant")
    print(f"  Comments: {len(comments)} loaded")


if __name__ == "__main__":
    main()
