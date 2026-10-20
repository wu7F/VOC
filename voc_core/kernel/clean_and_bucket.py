#!/usr/bin/env python3
"""
VOC 数据清洗、数据量统计与相关性分级（去域名化后的内核脚本）。

Pipeline:
  1. Read raw collected posts/reviews CSV (any source; column names are
     canonicalized by ``kernel_common.canonicalize_rows``).
  2. Clean: remove records older than N years (configurable via --max-age-years).
  3. Tag: generic relevance scoring using category keywords; bucket into A/B/C/D.
  4. Statistics: output total / date-cleaned / final-analysis data volumes.
  5. Write tagged CSV, relevant CSV, and summary CSV for LLM report generation.

源相关的差异全部由 ``--profile`` 注入（见 ``voc_core/source_profile.py``）：
未传时等同 Reddit（兼容基线），传 ``wayfair`` / ``amazon`` 即切换为电商口径。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- 平台侧公共设施（去域名化：列名归一化 + 源画像） -------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kernel_common import add_profile_argument, canonicalize_rows, load_profile  # noqa: E402
from naming import NAME_MODES, artifact_path  # noqa: E402

_P = None


def use_profile(profile) -> None:
    global _P
    _P = profile


def P():
    global _P
    if _P is None:
        _P = load_profile(None)
    return _P


def T(key: str, default: str = "") -> str:
    return P().term(key, default)


def TO(key: str) -> str:
    return P().opt(key)


def TN(key: str) -> list[str]:
    """取**列表型**术语（如 ``limitation_notes``）。

    列表不能用 ``term()`` 读（后者只认字符串）；空列表回落到 REDDIT 同名列表。
    """
    val = getattr(P(), key, None)
    if isinstance(val, list) and val:
        return [str(x) for x in val]
    fb = getattr(load_profile(None), key, None)
    return [str(x) for x in fb] if isinstance(fb, list) else []


def is_reddit() -> bool:
    """是否 Reddit 源。

    下面三处逻辑是 **Reddit 独有的**，对电商导出无意义甚至有害，必须按源关闭：

    * ``NOISE_SUBS`` / ``AD_LIKE_SUBS``：按 **subreddit 名**判断噪声与广告。
      电商导出的「站点」列只有 "Wayfair" / "Amazon" 这种值，拿它去比对
      Reddit 交易版块名单毫无意义。
    * RSS footer 剥离：Reddit RSS 的 "submitted by /u/..." 尾巴。
    * 输入必须含 ``reddit.com`` 链接的准入校验。
    """
    return (P().source or "").strip().lower() == "reddit"


# ---- Noise filtering (generic, not category-specific) ----

NOISE_SUBS = {
    "NatureofPredators", "HFY", "gamerdeals", "LaptopDealsUS",
    "AppGiveaway", "freelance_forhire", "BestofRedditorUpdates",
    "HaloInfinity", "GearsOfWar",
}


# ---- Reddit RSS footer stripping ----
# Reddit RSS <content> appends a footer such as:
#   "submitted by /u/USER to r/SUBREDDIT [link] [comments]"
# (HTML entities like &#32; may appear between tokens). This metadata is NOT
# part of the post body and must be excluded from the content used for
# annotation, so that only the title + main post text are analyzed.
def strip_footer(text: str) -> str:
    """Remove the Reddit RSS 'submitted by /u/...' footer from a post body.

    Handles both the full footer ("... [link] [comments]") and truncated forms
    where the footer is cut off after the username. The distinctive marker is the
    "submitted ... by /u/..." structure, which does not occur in normal post text.
    """
    if not text:
        return text
    low = text.lower()
    i = low.find("submitted")
    if i == -1:
        return text
    tail = low[i: i + 160]
    ui = tail.find("/u/")
    if ui == -1:
        return text
    # Footer structure requires 'by' between 'submitted' and '/u/'.
    bi = tail.rfind("by ", 0, ui)
    if bi == -1:
        return text
    # Cut from the start of 'submitted' and drop any trailing whitespace entities.
    body = text[:i].rstrip()
    body = re.sub(r"(&#32;|&nbsp;|\s)+$", "", body)
    return body
NOISE_TERMS = [
    "chapter", "fanfic", "story", "forhire", "[hiring]",
    "closed testing", "voice recording tasks",
]
AD_LIKE_SUBS = {
    "gamerdeals", "laptopdealsus", "appgiveaway", "deals",
    "dealsreddit", "promocodes", "coupons",
}
AD_LIKE_TERMS = [
    "promoted", "sponsored", "advertisement", "[ad]", "(ad)",
    "coupon", "promo code", "discount", "deal", "deals", "sale",
    "giveaway", "limited time offer", "buy now", "shop now",
]
PROMO_TERMS = [
    "review", "testing", "launched", "launch", "product hunt",
    "coupon", "deal", "discount", "sale", "affiliate",
]
INTENT_TERMS = [
    "recommend", "best", "alternative", "vs", "anyone", "what", "which",
    "help", "need", "looking for", "using", "tried", "compare",
    "subscription", "privacy", "export", "experience", "issue", "problem",
    "worth", "should i", "opinion", "thoughts", "review",
]

# ---- Feature/Experience analysis is now LLM-driven (Phase 4) ----


def split_terms(raw: str) -> list[str]:
    """把用户输入的词表切成干净列表。

    兼容半角/全角逗号、分号、顿号、换行 —— 中文输入法打出的「，」
    之前只按半角 "," 切，整串会被当成一个词，永远匹配不上（全 D 桶）。
    """
    if not raw:
        return []
    return [t.strip() for t in re.split(r"[,，;；、\n\r\t]+", raw) if t.strip()]


def has_any(text: str, patterns: list[str]) -> bool:
    return any(p in text for p in patterns)


def count_matches(text: str, patterns: list[str]) -> int:
    return sum(1 for p in patterns if p in text)


def parse_date(date_str: str) -> datetime | None:
    """Parse an ISO 8601-ish date string (works for both Reddit RSS and e-commerce exports)."""
    if not date_str:
        return None
    s = date_str.strip()
    # Try common ISO formats
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    # Fallback: try fromisoformat (Python 3.11+ handles 'Z')
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def is_within_years(date_str: str, max_age_years: float, now: datetime) -> bool:
    """Return True if the post date is within max_age_years of now."""
    dt = parse_date(date_str)
    if dt is None:
        # If date is unparseable, keep the post (conservative: don't discard unknown dates)
        return True
    cutoff = now - timedelta(days=max_age_years * 365.25)
    return dt >= cutoff


def tag_rows(rows: list[dict], keywords: list[str], brand_terms: list[str]) -> list[dict]:
    """
    Generic relevance tagging.

    - keywords: category-specific search terms (e.g. ["nas", "synology", "home server"])
    - brand_terms: known brand/competitor names to boost score

    Scoring:
      +2 per brand term match
      +1 per keyword match
      +2 if intent terms present (organic discussion signal)
      -5 if noise
      -1 if promo-like

    Buckets:
      A_强相关VOC: score >= 8, or (brand match + 2+ keywords), or (2+ brands + intent)
      B_可参考: score >= 3 and not noise
      C_推广/弱参考: promo-like and score < 8
      D_噪声/低相关: noise or score < 3
    """
    kw_lower = [k.lower() for k in keywords]
    brand_lower = [b.lower() for b in brand_terms]
    # subreddit 名单只对 Reddit 源有意义（电商的「站点」列只有 Wayfair/Amazon 这种值）
    sub_rule_on = is_reddit()
    out = []
    for row in rows:
        # 渠道名是否并入评分文本：Reddit 的 r/nursery 这类版块名是有信息量的；
        # 电商的「站点」列是**常量**（整份导出都写 Wayfair），并入只会让
        # `--brands wayfair` 这种常见入参把每一行都判成品牌命中，故排除。
        channel_field = [row.get("subreddit", "")] if sub_rule_on else []
        text = " ".join([
            row.get("title", ""),
            row.get("summary_text", ""),
            row.get("query", ""),
            *channel_field,
        ]).lower()

        kw_hits = count_matches(text, kw_lower)
        brand_hits = count_matches(text, brand_lower)
        _sub = row.get("subreddit") or ""
        ad_like = (sub_rule_on and _sub.lower() in AD_LIKE_SUBS) or has_any(text, AD_LIKE_TERMS)
        noise = (sub_rule_on and _sub in NOISE_SUBS) or has_any(text, NOISE_TERMS) or ad_like
        promo = has_any(text, PROMO_TERMS)
        organic = has_any(text, INTENT_TERMS) and not noise

        score = (
            brand_hits * 2
            + kw_hits
            + (2 if organic else 0)
            - (5 if noise else 0)
            - (1 if promo else 0)
        )

        if noise or score < 3:
            bucket = "D_噪声/低相关"
        elif promo and score < 8:
            bucket = "C_推广/弱参考"
        elif score >= 8 or (brand_hits >= 1 and kw_hits >= 2) or (brand_hits >= 2 and organic):
            bucket = "A_强相关VOC"
        else:
            bucket = "B_可参考"

        # Extract mentioned brands for this row
        mentioned_brands = [b for b in brand_lower if b in text]

        new = dict(row)
        new.update({
            "voc_score": str(score),
            "voc_bucket": bucket,
            "kw_hits": str(kw_hits),
            "brand_hits": str(brand_hits),
            "mentioned_brands": ";".join(mentioned_brands),
            "is_promo_like": str(promo),
            "is_ad_like": str(ad_like),
            "is_noise_rule": str(noise),
            "is_organic": str(organic),
        })
        out.append(new)
    return out


def bucket_rank(bucket: str) -> int:
    if bucket.startswith("A"):
        return 1
    if bucket.startswith("B"):
        return 2
    if bucket.startswith("C"):
        return 3
    return 4


def add_summary_columns(rows: list[dict]) -> list[dict]:
    counts = Counter(r["voc_bucket"] for r in rows)
    total = len(rows)
    core = counts.get("A_强相关VOC", 0) + counts.get("B_可参考", 0)
    out = []
    for row in rows:
        new = dict(row)
        new["bucket_rank"] = str(bucket_rank(row["voc_bucket"]))
        new["is_core_sample"] = str(row["voc_bucket"].startswith("A") or row["voc_bucket"].startswith("B"))
        new["sample_total"] = str(total)
        new["sample_A_strong_voc"] = str(counts.get("A_强相关VOC", 0))
        new["sample_B_reference"] = str(counts.get("B_可参考", 0))
        new["sample_C_promo_weak"] = str(counts.get("C_推广/弱参考", 0))
        new["sample_D_noise_low"] = str(counts.get("D_噪声/低相关", 0))
        new["sample_core_A_plus_B"] = str(core)
        out.append(new)
    return sorted(
        out,
        key=lambda x: (
            int(x["bucket_rank"]),
            -int(x.get("voc_score", "0") or 0),
            x.get("subreddit", ""),
            x.get("title", ""),
        ),
    )


def write_summary_csv(path: Path, stats: dict) -> None:
    """Write the data volume summary CSV."""
    unit = T("unit_noun")
    summary = [
        {"数据项": "采集总量", "数量": stats["raw_total"], "说明": f"去重后的{unit}总数（本次采集或用户上传）"},
        {"数据项": "两年以上旧数据", "数量": stats["expired_count"], "说明": f"发布时间超过 {stats['max_age_years']} 年的{unit}（已清洗）"},
        {"数据项": "日期清洗后剩余", "数量": stats["after_date_clean"], "说明": f"清洗两年以上旧数据后的{unit}数"},
        {"数据项": "噪声/低相关 (D)", "数量": stats["bucket_D"], "说明": "噪声/广告/低相关，不用于分析"},
        {"数据项": "推广/弱参考 (C)", "数量": stats["bucket_C"], "说明": "推广类或弱参考"},
        {"数据项": "强相关 VOC (A)", "数量": stats["bucket_A"], "说明": f"强相关{unit}，优先分析"},
        {"数据项": "可参考 (B)", "数量": stats["bucket_B"], "说明": f"可参考{unit}"},
        {"数据项": "最终分析数据量 (A+B)", "数量": stats["final_analysis"], "说明": "最终用于各维度分析的核心样本量"},
    ]
    # 本文件列名是硬编码的（不含 BOM 污染），保持 utf-8 以便与旧产物逐字一致
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["数据项", "数量", "说明"])
        writer.writeheader()
        writer.writerows(summary)


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    # utf-8-sig（带 BOM）与平台 io_utils.CSV_ENCODING 一致：Excel 打开中文不乱码；
    # 也正好复原旧版产物的字节（旧版读到 BOM 首列键后原样写回，效果等同）。
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_stats_json(path: Path, stats: dict) -> None:
    """Write statistics as JSON for easy parsing by WorkBuddy."""
    path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")


def source_table(rows: list[dict], limit: int = 20) -> str:
    """Generate a markdown source table from A-bucket rows."""
    picks = sorted(
        [r for r in rows if r["voc_bucket"].startswith("A")],
        key=lambda x: int(x["voc_score"]),
        reverse=True,
    )[:limit]
    lines = [f"| 标题 | {T('channel_noun')} | 相关性 | 分数 | 链接 |", "|---|---|---|---:|---|"]
    for row in picks:
        title = row.get("title", "").replace("|", "/")[:80]
        sub = row.get("subreddit", "")
        bucket = row.get("voc_bucket", "")
        score = row.get("voc_score", "0")
        url = (row.get("url") or "").strip()
        # 电商导出常没有商品链接 —— 空 url 不能再渲染成 [链接]()，那是给读者的死链。
        link = f"[链接]({url})" if url else "—"
        lines.append(f"| {title} | {TO('channel_prefix')}{sub} | {bucket} | {score} | {link} |")
    return "\n".join(lines)


def generate_data_report(
    stats: dict,
    tagged: list[dict],
    relevant: list[dict],
    report_path: Path,
    category: str,
    source_note: str = "",
) -> None:
    """
    Generate a data overview report with volume statistics and source links.
    This is NOT the final analysis report — it provides the data foundation
    for WorkBuddy to perform the six-dimension analysis.
    """
    bucket_counts = Counter(r["voc_bucket"] for r in tagged)
    brand_counter = Counter()
    for r in relevant:
        for b in r.get("mentioned_brands", "").split(";"):
            if b:
                brand_counter[b] += 1

    subreddits = Counter(r.get("subreddit", "") for r in relevant)
    queries = Counter(r.get("query", "") for r in relevant)

    limitation_block = "\n".join(f"- {n}" for n in TN("limitation_notes"))

    def pct(n, d):
        return f"{n / d * 100:.1f}%" if d else "0.0%"

    def brand_table(counter, denom, limit=15):
        # 数据报告用**全称**（「提及帖子数」），final report 用缩写（「提及帖数」）
        lines = [f"| 品牌/关键词 | 提及{T('unit_noun')}数 | 占比 |", "|---|---:|---:|"]
        for key, value in counter.most_common(limit):
            lines.append(f"| {key} | {value} | {pct(value, denom)} |")
        return "\n".join(lines)

    def simple_table(counter, denom, limit=15):
        lines = [f"| 项目 | {T('unit_noun')}数 | 占比 |", "|---|---:|---:|"]
        for key, value in counter.most_common(limit):
            lines.append(f"| {key} | {value} | {pct(value, denom)} |")
        return "\n".join(lines)

    # 全部键都为空（如电商导出没有「关键词」列）时不要输出一行空白表格 —— 改成「无有效数据」
    has_channels = any(k for k in subreddits)
    has_queries = any(k for k in queries)
    channel_table = (simple_table(subreddits, stats['final_analysis'])
                     if stats['final_analysis'] > 0 and has_channels else '无有效数据')
    query_table = (simple_table(queries, stats['final_analysis'], 10)
                   if stats['final_analysis'] > 0 and has_queries else '无有效数据')

    report = f"""# {category} {P().label} 数据分析报告

## 一、数据概览

### 1.1 数据量统计

| 数据阶段 | 数量 | 说明 |
|---|---:|---|
| 采集总量 | {stats['raw_total']} | {T('raw_total_note')} |
| 两年以上旧数据（已清洗） | {stats['expired_count']} | 发布时间超过 {stats['max_age_years']} 年的{T('unit_noun')} |
| 日期清洗后剩余 | {stats['after_date_clean']} | 清洗旧数据后的{T('unit_noun')}数 |
| 噪声/低相关 (D) | {stats['bucket_D']} | 噪声/广告/低相关，不用于分析 |
| 推广/弱参考 (C) | {stats['bucket_C']} | 推广类或弱参考 |
| 强相关 VOC (A) | {stats['bucket_A']} | 强相关{T('unit_noun')} |
| 可参考 (B) | {stats['bucket_B']} | 可参考{T('unit_noun')} |
| **最终分析数据量 (A+B)** | **{stats['final_analysis']}** | **最终用于各维度分析的核心样本量** |

### 1.2 数据清洗说明

- 数据来源：{source_note if source_note else T('collection_note')}
- 清洗规则：过滤发布时间超过 {stats['max_age_years']} 年的{T('unit_noun')}，仅保留近两年数据
- 相关性分级：A=强相关VOC，B=可参考，C=推广/弱参考，D=噪声/低相关
- 最终分析仅使用 A+B 级别数据

### 1.3 数据局限性

{limitation_block}
{' - 已采集' + TO('secondary_noun') + '数据：' + str(stats.get('comments_total', 0)) + ' 条' + TO('secondary_noun') + '，覆盖 ' + str(stats.get('comments_posts_count', 0)) + ' 个' + T('unit_noun') if stats.get('comments_total') else ''}
- 主题百分比为多标签，总和可能超过 100%
- {T('relevance_caveat')}

## 二、高频提及品牌

{brand_table(brand_counter, stats['final_analysis']) if stats['final_analysis'] > 0 else '无有效数据'}

## 三、高频{T('channel_noun')}

{channel_table}

## 四、高频搜索关键词

{query_table}

## 附录：{T('link_section')}（A 级强相关）

{source_table(relevant) if relevant else '无有效数据'}

## 附录：数据文件路径

- 原始数据：`{stats.get('input_path', '')}`
- 打标数据：`{stats.get('tagged_path', '')}`
- 分析数据（A+B）：`{stats.get('relevant_path', '')}`
- 统计摘要：`{stats.get('summary_path', '')}`
- 统计JSON：`{stats.get('stats_json_path', '')}`
{' - ' + TO('secondary_noun') + '数据：`' + str(stats.get('comments_csv_path', '')) + '`' if stats.get('comments_csv_path') else ''}
"""

    report_path.write_text(report, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="VOC data cleaning, volume statistics, and relevance tagging")
    parser.add_argument("--input", default="data/reddit_analysis/reddit_posts.csv",
                        help="Path to the raw collected CSV (posts/reviews)")
    parser.add_argument("--out-dir", default="data/reddit_analysis", help="Output directory")
    parser.add_argument("--report", default="", help="Path for the data report markdown")
    parser.add_argument("--category", default="Product", help="Product category name for the report title")
    parser.add_argument(
        "--keywords", default="",
        help="Comma-separated category keywords for relevance scoring (e.g. 'nas,synology,home server')",
    )
    parser.add_argument(
        "--brands", default="",
        help="Comma-separated brand/competitor names for brand detection (e.g. 'synology,qnap,truenas')",
    )
    parser.add_argument(
        "--max-age-years", type=float, default=2.0,
        help="Maximum record age in years; older records are filtered out (default: 2.0)",
    )
    parser.add_argument(
        "--comments", default="",
        help="Path to a secondary/reply CSV (optional). Enables secondary-unit statistics in the data report.",
    )
    parser.add_argument(
        "--source-note", default="",
        help="数据来源说明（可选），覆盖数据报告中的「数据来源」行。上传模式可传入如「用户上传的已有 VOC 数据」。",
    )
    add_profile_argument(parser)
    parser.add_argument(
        "--artifact-names", dest="artifact_names", choices=NAME_MODES, default="auto",
        help=("产物命名：auto=Reddit 保旧名(reddit_posts_*)、其他源用中性名(units_*)，"
              "legacy=强制旧名，neutral=强制中性名（默认 auto）"),
    )
    args = parser.parse_args()
    use_profile(load_profile(args.profile))

    in_path = Path(args.input)
    if not in_path.exists():
        raise SystemExit(f"Input CSV not found. Collect or export the data first: {in_path}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Read raw data（列名归一化：任意源的 unit_id / channel / text 都认）
    # 读输入一律 utf-8-sig：口径同 generate_dashboard / generate_report，
    # 避免 BOM 黏在首个列名上变成 "\ufeffreddit_id"
    rows = canonicalize_rows(
        list(csv.DictReader(in_path.open(encoding="utf-8-sig"))), "posts")
    if not rows:
        raise SystemExit("Input CSV is empty. Refusing to generate analysis without successful collection.")

    # 准入校验：Reddit 源必须带 reddit.com 链接（防止把无关 CSV 误当采集结果）。
    # 电商导出常无 URL（Wayfair PartReviews 就没有），因此该规则仅对 Reddit 生效。
    if is_reddit() and not any("reddit.com" in (r.get("url", "") or "") for r in rows):
        raise SystemExit("Input CSV does not appear to contain Reddit source URLs. Refusing to generate analysis.")

    raw_total = len(rows)

    # ---- Step 1: Date-based cleaning (filter posts older than max_age_years) ----
    now = datetime.now(timezone.utc)
    date_cleaned = []
    expired_count = 0
    for row in rows:
        pub = row.get("published", "") or row.get("updated", "")
        if is_within_years(pub, args.max_age_years, now):
            date_cleaned.append(row)
        else:
            expired_count += 1

    after_date_clean = len(date_cleaned)

    # ---- Step 1.5: Strip Reddit RSS footer metadata from post bodies ----
    # The footer ("submitted by /u/... to r/... [link] [comments]") is not part of
    # the post body and must be excluded from the content used for annotation, so
    # that only the title + main post text are analyzed. The raw CSV on disk is
    # left untouched; this only cleans the in-memory rows before tagging.
    # 非 Reddit 源没有这种尾巴 —— 硬跑反而可能误删正文（正文里真出现 "submitted"
    # 这类词时会被当成 footer 截断）。
    if is_reddit():
        for row in date_cleaned:
            if row.get("summary_text"):
                row["summary_text"] = strip_footer(row["summary_text"])

    # ---- Step 2: Relevance tagging ----
    keywords = split_terms(args.keywords)
    brand_terms = split_terms(args.brands)

    # If no keywords provided, extract from the query field in the data
    if not keywords:
        query_set = set()
        for row in date_cleaned:
            q = row.get("query", "").strip().strip('"').lower()
            if q:
                query_set.add(q)
        keywords = list(query_set)[:20]

    tagged = tag_rows(date_cleaned, keywords, brand_terms)
    tagged = add_summary_columns(tagged)
    relevant = [r for r in tagged if r["voc_bucket"].startswith("A") or r["voc_bucket"].startswith("B")]

    # ---- Step 3 (optional): Load and count comments ----
    comments_total = 0
    comments_posts_count = 0
    if args.comments:
        comments_path = Path(args.comments)
        if comments_path.exists():
            comment_rows = canonicalize_rows(
                list(csv.DictReader(comments_path.open(encoding="utf-8-sig"))), "comments")
            comments_total = len(comment_rows)
            comments_posts_count = len(set(r.get("post_reddit_id", "") for r in comment_rows))

    bucket_counts = Counter(r["voc_bucket"] for r in tagged)
    stats = {
        "category": args.category,
        "raw_total": raw_total,
        "expired_count": expired_count,
        "after_date_clean": after_date_clean,
        "bucket_A": bucket_counts.get("A_强相关VOC", 0),
        "bucket_B": bucket_counts.get("B_可参考", 0),
        "bucket_C": bucket_counts.get("C_推广/弱参考", 0),
        "bucket_D": bucket_counts.get("D_噪声/低相关", 0),
        "final_analysis": len(relevant),
        "max_age_years": args.max_age_years,
        "keywords": keywords,
        "brand_terms": brand_terms,
        "comments_total": comments_total,
        "comments_posts_count": comments_posts_count,
        "comments_csv_path": args.comments if args.comments else "",
        # 数据来源说明必须落到 stats：否则 generate_report.py 拿不到，
        # 上传模式（Phase 2'）的洞察报告会谎称数据是本次抓取的。
        "source_note": args.source_note or "",
    }

    # ---- Write outputs ----
    # 产物命名也参与去域名化：auto = Reddit 保旧名（真·零回归）、其他源用中性名。
    # 起因：产物路径会印进数据报告附录，跑 Wayfair 却出现 `reddit_posts_relevant.csv`，
    # 被 Stage 6 闸门的 presentation_vocabulary 判为错误级失败。
    name_mode = getattr(args, "artifact_names", "auto")
    is_rd = is_reddit()
    tagged_path = artifact_path(out_dir, "tagged", name_mode, is_rd)
    relevant_path = artifact_path(out_dir, "relevant", name_mode, is_rd)
    summary_path = artifact_path(out_dir, "summary", name_mode, is_rd)
    stats_json_path = artifact_path(out_dir, "stats", name_mode, is_rd)
    report_path = (Path(args.report) if args.report
                   else artifact_path(out_dir, "data_report", name_mode, is_rd))

    stats["input_path"] = str(in_path)
    stats["tagged_path"] = str(tagged_path)
    stats["relevant_path"] = str(relevant_path)
    stats["summary_path"] = str(summary_path)
    stats["stats_json_path"] = str(stats_json_path)

    write_csv(tagged_path, tagged)
    write_csv(relevant_path, relevant)
    write_summary_csv(summary_path, stats)
    write_stats_json(stats_json_path, stats)
    generate_data_report(stats, tagged, relevant, report_path, args.category, args.source_note)

    # Print stats for WorkBuddy to parse
    print(f"=== Data Volume Statistics ===")
    print(f"raw_total={raw_total}")
    print(f"expired_count={expired_count}")
    print(f"after_date_clean={after_date_clean}")
    print(f"bucket_A={stats['bucket_A']}")
    print(f"bucket_B={stats['bucket_B']}")
    print(f"bucket_C={stats['bucket_C']}")
    print(f"bucket_D={stats['bucket_D']}")
    print(f"final_analysis={stats['final_analysis']}")
    if comments_total:
        print(f"comments_total={comments_total}")
        print(f"comments_posts_count={comments_posts_count}")
    print(f"=== Output Files ===")
    print(f"summary={summary_path}")
    print(f"tagged={tagged_path}")
    print(f"relevant={relevant_path}")
    print(f"stats_json={stats_json_path}")
    print(f"report={report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
