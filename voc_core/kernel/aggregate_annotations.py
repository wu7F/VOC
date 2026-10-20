#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""聚合 —— 把逐条语义标注结果收敛为看板契约的 dimensions.json / features.json。
纯本地计算，不调 LLM。

**分段轴（segmentation axis，Stage 5）**：本脚本不再写死「购买状态」。
单元的归桶轴来自 ``--tagset`` 里的 ``analysis_frame`` 块（由 design_tags 写入，
见 ``voc_core/analysis_frame.py``）：消费品框架按购买状态分桶、B2B 框架按决策角色
分桶、订阅制可以按付费档位分桶。凡是原来写死 ``purchase_status`` 的地方，
现在都从 ``frame.axis`` 派生（``distribution_key`` / ``unit_counts_key`` /
``buckets`` / ``matrix_bucket`` / ``persona_dims()``）。

归桶闸门（**口径固定，不随框架变**）：
  * 每个单元先由分段轴分类器判进**恰好一个**桶——命中 0 个或 ≥2 个标签都判为
    轴上的「被排除桶」（宁可不计入任何画像桶，也不要把人放错桶）；
  * 每个画像维度只累计「桶匹配」的单元（映射表由 ``frame.persona_dims()`` 给出）；
  * **被排除桶的单元不计入任何画像桶**，数量写进
    ``unit_counts.excluded_unknown_units``，供报告做一行披露——不静默丢数据。
    该键名是**产物契约**（Stage 6 闸门按它取值），换框架也不会变。
  * 各桶严格互斥；每个画像标签同时给出 `count` / `post_count` / `comment_count`，
    看板按单元类型堆叠。

输出自带 ``analysis_frame`` 元数据，下游（报告 / 看板 / 质量闸门）据此渲染
跟轴有关的标题与口径说明——不必各自再挂一个 ``--frame`` 参数。

其余维度沿用原规则：
- 每标签 count = 该维度下 mentioned=yes 的帖数；percentage = 维度内占比（合计 100%）。
- evidence_posts = mentioned=yes 且含 original_text 的帖（带 reddit_id / evidence_quote / mention_count）。
- 情感分布由 sentiment 统计；features 的 positive/negative/neutral 与 satisfaction 由 sentiment 重算。
- brand_mentions 与 brand_feature_matrix 由帖子 mentioned_brands 字段精确统计（不经逐条标注）。
- persona_feature_matrix（画像×功能矩阵）为帖粒度交集：每篇帖子先由 purchase_status 判状态，
  **只有判为「已购」的帖子才入矩阵**（只取已购一桶：未购桶样本量通常极小、且其关心点与
  已购体验不同源，混在一张热力图里会稀释可读性；状态不明者是噪声量尺，同样不进矩阵）；
  再与该帖命中的功能标签相交；单元格满意度 = 该帖该功能标注的情感分布。评论单元无功能标注，不进矩阵。

运行：
  python3 aggregate_annotations.py --run-dir annot/runs/<run> \
      --relevant data/reddit_analysis/reddit_posts_relevant.csv \
      --tagset tags/reddit_tagset.json \
      --dimensions reports/reddit_analysis_dimensions.json \
      --features data/reddit_analysis/reddit_analysis_features.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# --- 平台侧公共设施（去域名化：列名归一化 + 源画像） -------------------------
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

#: 当前源画像（由 main 设置；未设置即 Reddit = 改造前行为）
_P = None


def use_profile(profile) -> None:
    global _P
    _P = profile


def T(key: str, default: str = "") -> str:
    global _P
    if _P is None:
        _P = load_profile(None)
    return _P.term(key, default)


def TO(key: str) -> str:
    global _P
    if _P is None:
        _P = load_profile(None)
    return _P.opt(key)


#: 当前分析框架（由 main 设置；未设置即内置 consumer = 改造前行为）
_F: AnalysisFrame | None = None


def use_frame(f: AnalysisFrame | None) -> None:
    global _F
    _F = f


def current_frame() -> AnalysisFrame:
    """当前分析框架。没有显式设置过就加载内置 ``consumer``。

    内置 consumer 的维度与分段轴与平台化之前**逐字节等价**，所以「忘了传框架」
    不会悄悄改变平台化之前的行为。
    """
    global _F
    if _F is None:
        _F = load_frame(None)
    return _F


def persona_scope_rule(f: AnalysisFrame | None = None) -> str:
    """人群画像口径说明（写进 dimensions.json，最终出现在报告里）。

    句子本体在 ``frames/*.yaml`` 的 ``segmentation_axis.texts.scope_rule`` ——
    它说的是**轴的口径**（「每个单元先由 purchase_status 判为已购 / 未购 / 无法判断」），
    换框架就该换句子。代码只负责算 ``${scope}`` 这一个变量：

    单层源（电商评论）没有次级单元，措辞要相应退化为「以评论单元计数」——
    否则报告会声称存在一个根本不存在的「评论单元」。
    """
    fr = f or current_frame()
    un, sec = T("unit_noun"), TO("secondary_noun")
    scope = f"{un}单元 + {sec}单元合并计数" if sec else f"以{un}单元计数"
    axis = fr.axis
    if axis is None or not axis.texts.get("scope_rule"):
        # 没有分段轴的框架：没有任何桶，口径说明退化为一句纯计数说明。
        return f"人群画像 = {scope}。"
    return axis.render_text("scope_rule", scope=scope)


def bucket_map(f: AnalysisFrame | None = None) -> dict[str, str]:
    """``{分类器标签键: 桶 id}`` —— 由框架**显式声明**（``buckets[].tags``）。

    这是归桶闸门的映射表本体。刻意不按键名猜：猜错的后果是把人放进错的桶，
    而份额数字错了**不会有任何报错**。
    """
    fr = f or current_frame()
    out: dict[str, str] = {}
    if fr.axis is None:
        return out
    for b in fr.axis.buckets:
        for tk in b.tags:
            out[tk] = b.id
    return out

# 品牌情感判定词（用于把「品牌所在句」判成正 / 负 / 中性）
POS_WORDS = [re.compile(p, re.I) for p in [
    r"\blove\b", r"\bloved\b", r"\bgreat\b", r"\bbest\b", r"\bworth\s+it\b", r"\bperfect\b",
    r"\bcomfortab", r"\bcomfy\b", r"\bquiet\b", r"\bsilent\b", r"\bsmooth\b", r"\bawesome\b",
    r"\bamazing\b", r"\brecommend\b", r"\bglad\b", r"\bfavorite\b", r"\bnice\b", r"\bworks\s+great\b",
    r"\bstill\s+going\s+strong\b", r"\bmust\s+have\b", r"\bbest\s+purchase\b",
]]
NEG_WORDS = [re.compile(p, re.I) for p in [
    r"\bhate\b", r"\bawful\b", r"\bterrible\b", r"\bworst\b", r"\bbroke\b", r"\bbroken\b", r"\bproblem",
    r"\bissue", r"\bannoy", r"\bfrustrat", r"\bdisappoint", r"\bcheaply\s+made\b", r"\bflimsy\b",
    r"\bnot\s+working\b", r"\bregret\b", r"\bfell\s+apart\b", r"\bnot\s+worth\b", r"\breturned\b",
    r"\bstain", r"\bsmell", r"\bhard\s+to\s+clean\b", r"\btoo\s+heavy\b", r"\brecall", r"\bhazard",
]]


def load_tagset(path: str):
    ts = json.loads(Path(path).read_text(encoding="utf-8")).get("tagset", {})
    flat = {}
    for dk, dv in ts.get("dimensions", {}).items():
        for tk, td in dv.get("tags", {}).items():
            flat[tk] = {
                "dim": dk,
                "label_cn": td.get("label_cn", tk),
                "category": td.get("category"),
                "thoughts": td.get("thoughts", ""),
                "reason_hint": td.get(f"{tk}_reason", "") or td.get("thoughts", ""),
            }
    return ts, flat


def guess_bucket_of_tag(tk: str, label_cn: str = "", unknown_bucket: str = "unknown") -> str:
    """兜底：框架的 ``buckets[].tags`` 没覆盖到该标签键时，按键名 / 中文名猜桶。

    **正常路径是** :func:`bucket_map` **的显式映射**；这里只服务于「手工改过的旧
    标签集」——它多了一个框架里没声明的分类器标签。保留的是平台化之前那套购买
    状态启发式，因此只认得出 purchased / non_purchased；认不出来一律落到
    ``unknown_bucket``：宁可排除，也别把人放进错的桶。
    """
    k = (tk or "").lower()
    lab = label_cn or ""
    if "non" in k or "未购" in lab or "尚未" in lab:
        return "non_purchased"
    if "unknown" in k or "无法" in lab or "不清" in lab:
        return unknown_bucket
    if "purchased" in k or "已购" in lab or "购买" in lab or "拥有" in lab:
        return "purchased"
    return unknown_bucket


def axis_bucket_of(rec: dict, bucket_tags: list[tuple[str, str]],
                   unknown_bucket: str = "unknown") -> str:
    """判定单个单元落进哪个桶（**恰好一个**命中才生效）。

    ``bucket_tags`` 为 ``[(tag_key, bucket_id)]``。恰好一个标签 ``mentioned=yes``
    时取其桶；0 个或 ≥2 个命中都判为 ``unknown_bucket``——宁可不计入画像桶，
    也不要把人放错桶。

    这段闸门逻辑**不随框架变**：消费品按购买状态、B2B 按决策角色，判定规则一致，
    变的只是「标签 → 桶」的映射表。
    """
    hits = []
    anns = rec.get("annotations") or {}
    for tk, bid in bucket_tags:
        ann = anns.get(tk, {}) or {}
        if str(ann.get("mentioned", "")).lower() == "yes":
            hits.append(bid)
    if len(hits) == 1:
        return hits[0]
    return unknown_bucket


# 兼容别名（平台化之前的名字；语义已从「购买状态」泛化为「分段轴」）
status_of_tag = guess_bucket_of_tag
purchase_status_of = axis_bucket_of


_GATE_PREFIX = re.compile(r"^\s*【判定前置】[^；]*；\s*")


def display_reason(s: str) -> str:
    """去掉标签 thoughts 里的「【判定前置】…；」前缀。

    这个前缀是写给标注模型看的归桶闸门说明（见 design_tags 注入逻辑），
    进了 dimensions.json 就会在看板「归类原因」与报告「归类依据」里露出内部指令腔，
    对读报告的人是噪音，因此在聚合出口统一剥掉。
    """
    if not s:
        return ""
    return _GATE_PREFIX.sub("", s).strip() or s.strip()


def load_runs(run_dir: str) -> list[dict]:
    recs = []
    for p in Path(run_dir).glob("*.json"):
        recs.append(json.loads(p.read_text(encoding="utf-8")))
    return recs


def mention_count_of(ann: dict) -> int:
    if str(ann.get("mentioned", "")).lower() != "yes":
        return 0
    ot = ann.get("original_text", "") or ""
    if not ot.strip():
        return 1
    return len([x for x in ot.split("||") if x.strip()])


def sentiment_label(d: dict) -> str:
    pos, neg = d["pos"], d["neg"]
    if pos > 0 and neg > 0:
        return "mixed"
    if pos > 0:
        return "positive"
    if neg > 0:
        return "negative"
    return "neutral"


def parse_brands(s: str) -> list[str]:
    if not s:
        return []
    return [b.strip() for b in s.replace(";", ",").split(",") if b.strip()]


def aggregate(recs: list[dict], flat: dict, relevant_csv: str,
              comment_recs: list[dict] | None = None, gating: bool = True,
              comments_csv: str = "", frame: AnalysisFrame | None = None):
    f = frame or current_frame()
    axis = f.axis
    # ---- 下面这些全部从框架派生：换框架时这个函数一行都不用改 ----
    persona_map = f.persona_dims()          # {画像维度 id: 桶 id}（归桶闸门的映射表）
    deprecated = f.deprecated_dims()        # 声明成 deprecated 的维度：一律不产出
    exp_dims = f.experience_dims()          # 要算情感分布的体验类维度
    # 进 features.json 的「功能体验」维度。是模块契约（看板整页按它渲染），
    # 因此可配但不随框架自由变动：默认仍是 features。
    feature_dim = str((f.modules or {}).get("features_dim") or "features")
    axis_dim = axis.dim if axis else ""
    unknown_bid = (axis.unknown_bucket if axis else "") or "unknown"
    # 产物键名跟着轴走 —— B2B 产物里不该出现一个叫 purchase_status_distribution 的键
    dist_key = (axis.distribution_key if axis else "") or "purchase_status_distribution"
    uc_key = (axis.unit_counts_key if axis else "") or "purchase_status"

    dim_tags: dict = defaultdict(list)
    for tk, info in flat.items():
        dim_tags[info["dim"]].append(tk)

    # 标签 -> 桶：先用框架的显式声明，框架没覆盖到的标签才走启发式兜底
    tag_bucket = bucket_map(f)
    status_tags: list[tuple[str, str]] = [
        (tk, tag_bucket.get(tk)
             or guess_bucket_of_tag(tk, flat[tk].get("label_cn", ""), unknown_bid))
        for tk in (dim_tags.get(axis_dim, []) if axis_dim else [])
    ]
    has_status = bool(status_tags)
    use_gating = gating and has_status

    def new_bucket():
        return {"count": 0, "post_count": 0, "comment_count": 0,
                "eps": [], "pos": 0, "neg": 0, "neu": 0, "mc": 0}

    agg = defaultdict(new_bucket)
    # 分段轴分布：按单元类型分别统计（桶清单与桶 id 都来自框架）
    status_stat: dict = {b.id: new_bucket() for b in (axis.buckets if axis else ())}

    def feed_unit(rec: dict, unit_type: str):
        status = axis_bucket_of(rec, status_tags, unknown_bid) if use_gating else unknown_bid
        if use_gating:
            st = status_stat.setdefault(status, new_bucket())
            st["count"] += 1
            st["post_count" if unit_type == "post" else "comment_count"] += 1
        anns = rec.get("annotations") or {}
        for tk, info in flat.items():
            dim = info["dim"]
            # 已废弃维度（旧标签集里的 user_personas_unknown 等）一律不计入任何产出
            if dim in deprecated:
                continue
            # 归桶闸门：每个画像维度只累计「桶匹配」的单元
            if use_gating and dim in persona_map and status != persona_map[dim]:
                continue
            ann = anns.get(tk, {}) or {}
            mc = mention_count_of(ann)
            if mc <= 0:
                continue
            d = agg[tk]
            d["count"] += 1
            d["post_count" if unit_type == "post" else "comment_count"] += 1
            d["mc"] += mc
            ep = {
                "reddit_id": rec.get("reddit_id"),
                "evidence_quote": ann.get("original_text", ""),
                "mention_count": mc,
                "summary": ann.get("summary", ""),
                "unit_type": unit_type,
            }
            if unit_type == "comment":
                ep["post_reddit_id"] = rec.get("post_reddit_id", "")
            d["eps"].append(ep)
            s = (ann.get("sentiment") or "").lower()
            if s == "positive":
                d["pos"] += 1
            elif s == "negative":
                d["neg"] += 1
            elif s == "neutral":
                d["neu"] += 1

    for rec in recs:
        feed_unit(rec, "post")
    for rec in (comment_recs or []):
        feed_unit(rec, "comment")

    dims_out = {}
    for dk, tags in dim_tags.items():
        if dk == feature_dim or (axis_dim and dk == axis_dim):
            continue  # 功能维度进 features.json；分段轴维度是分类器，不单独出图
        if dk in deprecated:
            continue  # 已废弃的画像维度：不出现在任何产出里
        total_dim = sum(agg[tk]["count"] for tk in tags) or 1
        lst = []
        for tk in tags:
            d = agg[tk]
            item = {
                "label_cn": flat[tk]["label_cn"],
                "count": d["count"],
                "percentage": round(d["count"] / total_dim * 100, 1),
                "reason": display_reason(flat[tk].get("thoughts", "")),
                "description": display_reason(flat[tk].get("thoughts", "")),
                "evidence_posts": d["eps"],
            }
            if dk in persona_map:
                # 帖子 / 评论合并计数：给看板堆叠与报告拆分用
                item["post_count"] = d["post_count"]
                item["comment_count"] = d["comment_count"]
            if dk in exp_dims:
                item["sentiment"] = sentiment_label(d)
                item["sentiment_distribution"] = {"positive": d["pos"], "negative": d["neg"], "neutral": d["neu"]}
            lst.append(item)
        dims_out[dk] = sorted(lst, key=lambda x: x["count"], reverse=True)

    # 品牌维度（来自 mentioned_brands 字段）
    # ⚠️ 必须用 utf-8-sig：本技能自己的 analyzer/ingest 写出的 CSV 带 BOM，
    #    用 utf-8 读会让首列名变成 '\ufeffreddit_id'，于是 id2brands 的键全为 None
    #    → 品牌证据的 reddit_id 全丢、品牌×功能矩阵整块为空。
    brand_counter = defaultdict(int)
    brand_posts = defaultdict(list)
    rows = canonicalize_rows(
        list(csv.DictReader(Path(relevant_csv).open(encoding="utf-8-sig"))), "posts")
    id2brands = {r.get("reddit_id", ""): parse_brands(r.get("mentioned_brands", "")) for r in rows}
    id2text = {r.get("reddit_id", ""): " ".join(
        [r.get("title", "") or "", r.get("summary_text", "") or ""]).strip() for r in rows}
    # 淘宝式导出/评论级数据：品牌几乎只出现在评论正文里。
    # 传入 --comments-csv 时把评论正文并入该帖可见文本，品牌情感才能按上下文判出正负。
    if comments_csv:
        try:
            for cr in canonicalize_rows(
                    csv.DictReader(Path(comments_csv).open(encoding="utf-8-sig")), "comments"):
                rid = cr.get("post_reddit_id", "") or ""
                if rid:
                    id2text[rid] = (id2text.get(rid, "") + " " + (cr.get("body", "") or "")).strip()
        except FileNotFoundError:
            print(f"[aggregate] 警告：--comments-csv 文件不存在：{comments_csv}，品牌情感仅用标题/正文判定")
    for r in rows:
        for b in parse_brands(r.get("mentioned_brands", "")):
            brand_counter[b] += 1
            brand_posts[b].append(r.get("reddit_id"))
    brand_total = sum(brand_counter.values()) or 1
    brand_mentions = []
    for b, c in sorted(brand_counter.items(), key=lambda x: x[1], reverse=True):
        # 品牌情感：在该帖可见文本里找品牌所在句，按上下文词判正负（三项之和 == count）
        sent = {"positive": 0, "negative": 0, "neutral": 0}
        eps = []
        for rid in brand_posts[b]:
            txt = id2text.get(rid, "") or ""
            quote = ""
            verdict = "neutral"
            if txt:
                # 文本与品牌名都要归一小写再找，否则 "IKEA" 在小写文本里永远找不到（引文空、情感全中性）
                i = txt.lower().find(b.lower())
                if i >= 0:
                    s0 = max(txt.rfind(".", 0, i), txt.rfind("!", 0, i), txt.rfind("?", 0, i)) + 1
                    e0 = len(txt)
                    for sep in ".!?":
                        j = txt.find(sep, i)
                        if j != -1:
                            e0 = min(e0, j + 1)
                    quote = txt[s0:e0].strip()[:400]
                    pos = sum(1 for c2 in POS_WORDS if c2.search(quote))
                    neg = sum(1 for c2 in NEG_WORDS if c2.search(quote))
                    verdict = "positive" if pos > neg else ("negative" if neg > pos else "neutral")
            sent[verdict] += 1
            eps.append({"reddit_id": rid, "evidence_quote": quote, "mention_count": 1})
        brand_mentions.append({
            "label_cn": b,
            "count": c,
            "percentage": round(c / brand_total * 100, 1),
            "sentiment_distribution": sent,
            "evidence_posts": eps,
        })
    dims_out["brand_mentions"] = brand_mentions

    # ---- 分段轴分布（帖子 + 评论合并计数）----
    # 键名与桶清单都跟着框架走（B2B 产物里不该出现一个叫 purchase_status_distribution 的键）。
    status_labels = {}
    for tk, st in status_tags:
        status_labels.setdefault(st, flat[tk]["label_cn"])
    # 分布图只画**非排除**桶：「无法判断」这类被排除桶的单元不进任何画像桶，
    # 因此也不进分布图分母（它只以 unit_counts.excluded_unknown_units 的形式披露）。
    dist_buckets = axis.active_buckets if axis else ()
    st_total = sum((status_stat.get(b.id) or {}).get("count", 0) for b in dist_buckets)
    distribution = []
    for b in dist_buckets:
        v = status_stat.get(b.id) or new_bucket()
        distribution.append({
            "label_cn": status_labels.get(b.id) or b.label_cn,
            "status": b.id,
            "count": v["count"],
            "post_count": v["post_count"],
            "comment_count": v["comment_count"],
            "percentage": round(v["count"] / st_total * 100, 1) if st_total else 0.0,
        })
    dims_out[dist_key] = distribution

    # 计数单元说明：帖子单元 + 评论单元（人群画像维度合并计数）
    excluded_units = (status_stat.get(unknown_bid) or {}).get("count", 0)
    dims_out["unit_counts"] = {
        "post_units": len(recs),
        "comment_units": len(comment_recs or []),
        "comment_units_all": len(comment_recs or []),
        "status_gating": use_gating,
        "persona_scope_rule": persona_scope_rule(f),
        # ``excluded_unknown_units`` 是**产物契约键**（Stage 6 闸门按它取值）：
        # 换框架后它表示「进了被排除桶、因而没被任何画像桶统计」的单元数。
        "excluded_unknown_units": excluded_units,
        "excluded_unknown_note": (
            axis.render_text("excluded_note") if axis is not None else ""
        ),
        uc_key: {
            b.id: {"posts": (status_stat.get(b.id) or {}).get("post_count", 0),
                   "comments": (status_stat.get(b.id) or {}).get("comment_count", 0),
                   "total": (status_stat.get(b.id) or {}).get("count", 0)}
            for b in (axis.buckets if axis else ())
        },
    }

    # features.json
    feat_tags = dim_tags.get(feature_dim, [])
    ftotal = sum(agg[tk]["mc"] for tk in feat_tags) or 1
    features = []
    brand_feat_matrix = defaultdict(lambda: defaultdict(lambda: {"total": 0, "pos": 0, "neg": 0, "neu": 0, "eps": []}))
    for tk in feat_tags:
        d = agg[tk]
        satisfaction = (d["pos"] / (d["pos"] + d["neg"] + d["neu"])) if (d["pos"] + d["neg"] + d["neu"]) > 0 else None
        features.append({
            "name": flat[tk]["label_cn"],
            "reason": flat[tk].get("thoughts", ""),
            "total": d["mc"],
            "positive": d["pos"],
            "negative": d["neg"],
            "neutral": d["neu"],
            "satisfaction": satisfaction,
            "evidence_posts": d["eps"],
        })
    # 品牌 × 功能矩阵：帖粒度交集（仅帖子单元，评论单元无 mentioned_brands）
    for rec in recs:
        brands = id2brands.get(rec.get("reddit_id"), [])
        if not brands:
            continue
        feats_mentioned = [tk for tk in feat_tags if mention_count_of(rec.get("annotations", {}).get(tk, {})) > 0]
        for b in brands:
            for tk in feats_mentioned:
                cell = brand_feat_matrix[b][flat[tk]["label_cn"]]
                cell["total"] += 1
                s = (rec.get("annotations", {}).get(tk, {}).get("sentiment") or "").lower()
                if s == "positive":
                    cell["pos"] += 1
                elif s == "negative":
                    cell["neg"] += 1
                elif s == "neutral":
                    cell["neu"] += 1
                cell["eps"].append({"reddit_id": rec.get("reddit_id"), "mention_count": 1})
    brand_feature_matrix = {}
    for b, fmap in brand_feat_matrix.items():
        brand_feature_matrix[b] = {}
        for fname, cell in fmap.items():
            sat = (cell["pos"] / (cell["pos"] + cell["neg"] + cell["neu"])) if (cell["pos"] + cell["neg"] + cell["neu"]) > 0 else None
            brand_feature_matrix[b][fname] = {"total": cell["total"], "satisfaction": sat, "evidence_posts": cell["eps"]}

    # 画像 × 功能矩阵：帖粒度交集（仅帖子单元，评论单元无 features 标注）。
    # 画像轴只取框架声明的 ``matrix_bucket`` 一桶——每篇帖子先由分段轴判桶，判中才入矩阵
    # （其余桶不进矩阵：样本量小且关注点与主桶不同源；被排除桶是噪声量尺，同样不进）。
    # 只剩一桶，行键直接用画像标签中文名，不再加「已购·」前缀。
    matrix_bid = axis.matrix_bucket if axis else ""
    matrix_persona_dim = next(
        (did for did, bid in persona_map.items() if bid == matrix_bid), "")
    persona_feat_matrix = defaultdict(lambda: defaultdict(lambda: {"total": 0, "pos": 0, "neg": 0, "neu": 0, "eps": []}))
    for rec in recs:
        # 矩阵的桶判定与 --no-status-gating 解耦：兜底模式关掉的只是画像归桶闸门，
        # 矩阵「哪个桶入矩阵」的语义仍依赖分类器，否则兜底模式下矩阵会整块为空。
        status = axis_bucket_of(rec, status_tags, unknown_bid) if has_status else None
        pdim = matrix_persona_dim if status == matrix_bid else ""
        if not pdim:
            continue
        anns = rec.get("annotations") or {}
        personas = [tk for tk in dim_tags.get(pdim, [])
                    if mention_count_of(anns.get(tk, {}) or {}) > 0]
        if not personas:
            continue
        feats_mentioned = [tk for tk in feat_tags if mention_count_of(anns.get(tk, {})) > 0]
        if not feats_mentioned:
            continue
        for tk_p in personas:
            for tk_f in feats_mentioned:
                cell = persona_feat_matrix[flat[tk_p]["label_cn"]][flat[tk_f]["label_cn"]]
                cell["total"] += 1
                s = (anns.get(tk_f, {}).get("sentiment") or "").lower()
                if s == "positive":
                    cell["pos"] += 1
                elif s == "negative":
                    cell["neg"] += 1
                elif s == "neutral":
                    cell["neu"] += 1
                cell["eps"].append({"reddit_id": rec.get("reddit_id"), "mention_count": 1})
    persona_feature_matrix = {}
    for p, fmap in persona_feat_matrix.items():
        persona_feature_matrix[p] = {}
        for fname, cell in fmap.items():
            sat = (cell["pos"] / (cell["pos"] + cell["neg"] + cell["neu"])) if (cell["pos"] + cell["neg"] + cell["neu"]) > 0 else None
            persona_feature_matrix[p][fname] = {"total": cell["total"], "satisfaction": sat, "evidence_posts": cell["eps"]}

    return dims_out, {"category": "", "features": features, "brand_feature_matrix": brand_feature_matrix,
                      "persona_feature_matrix": persona_feature_matrix}


def main() -> None:
    ap = argparse.ArgumentParser(
        description="聚合逐条标注 -> 看板 JSON（人群画像按分段轴归桶、帖子+评论合并计数）")
    ap.add_argument("--run-dir", required=True, help="标注中间文件目录")
    ap.add_argument("--relevant", required=True, help="reddit_posts_relevant.csv")
    ap.add_argument("--tagset", required=True, help="标签集 JSON")
    ap.add_argument("--dimensions", required=True, help="输出 dimensions.json")
    ap.add_argument("--features", required=True, help="输出 features.json")
    ap.add_argument("--category", default="", help="品类名（写入 JSON）")
    ap.add_argument("--comments-run-dir", default="", help="评论标注中间文件目录（默认 <run-dir>/comments）")
    ap.add_argument("--comments-csv", default="",
                    help="评论正文 CSV（reddit_comments_relevant.csv），用于品牌所在句情感判定")
    ap.add_argument("--comments-tagset", default="", help="【已废弃·忽略】v3 起评论与主标签集合并")
    ap.add_argument("--no-status-gating", action="store_true",
                    help="关闭分段轴归桶（人群画像标签命中即计数，不做桶校验；仅作兜底）")
    add_profile_argument(ap)
    add_frame_argument(ap)
    args = ap.parse_args()
    use_profile(load_profile(args.profile))

    ts, flat = load_tagset(args.tagset)
    # 框架从**上游产物**读：design_tags 已把 analysis_frame 写进 tagset.json。
    # 老产物没有这个块时回落到 --frame，再没有就内置 consumer（= 改造前行为）。
    fr = load_frame_from_product({"tagset": ts}, default=(args.frame or ""))
    if fr is None:
        fr = load_frame(None)
    use_frame(fr)
    recs = load_runs(args.run_dir)

    comment_recs = []
    c_dir = Path(args.comments_run_dir) if args.comments_run_dir else Path(args.run_dir) / "comments"
    if c_dir.exists():
        comment_recs = load_runs(str(c_dir))
        print(f"[aggregate] 评论单元标注={len(comment_recs)}（目录 {c_dir}）")
    else:
        print(f"[aggregate] 未找到评论标注目录 {c_dir}，人群画像仅统计帖子单元。")

    has_persona = any(info["dim"] in fr.persona_dims() for info in flat.values())
    if not has_persona:
        print("[aggregate] 警告：标签集缺少人群画像维度"
              f"（{'、'.join(fr.persona_dim_ids()) or '框架未定义任何画像桶'}），"
              "人群画像将按旧口径（帖子 / 评论分开）输出。")
    dep_dims = fr.deprecated_dims()
    n_deprecated = sum(1 for info in flat.values() if info["dim"] in dep_dims)
    if n_deprecated:
        print(f"[aggregate] 已忽略 {n_deprecated} 个废弃标签（{'、'.join(sorted(dep_dims))} "
              f"维度）：该维度不再产出。")
    if args.comments_tagset:
        print("[aggregate] --comments-tagset 已废弃并忽略：评论单元现与主标签集合并。")

    dims, feat = aggregate(recs, flat, args.relevant, comment_recs, gating=not args.no_status_gating,
                           comments_csv=args.comments_csv, frame=fr)
    axis = fr.axis
    dims["category"] = args.category
    # 产物自描述：下游（报告 / 看板 / 质量闸门）从这里读框架，不必各自再挂 --frame
    dims["analysis_frame"] = fr.to_metadata()
    dims["generated_at"] = datetime.now().isoformat()
    feat["category"] = args.category

    dpath = Path(args.dimensions)
    dpath.parent.mkdir(parents=True, exist_ok=True)
    dpath.write_text(json.dumps(dims, ensure_ascii=False, indent=2), encoding="utf-8")
    fpath = Path(args.features)
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(json.dumps(feat, ensure_ascii=False, indent=2), encoding="utf-8")

    # 分布键由框架声明（consumer=``purchase_status_distribution``，
    # B2B=``decision_role_distribution``）—— 这里**不能**写死，也不能引用
    # ``aggregate()`` 里的同名局部变量（那个作用域外面看不见）。
    dist_key = (axis.distribution_key if axis else "") or "purchase_status_distribution"
    psd = dims.get(dist_key, [])
    ps_str = "、".join(f"{d['label_cn']} {d['count']}（帖 {d['post_count']} / 评论 {d['comment_count']}）"
                       for d in psd)
    n_eps = sum(len(it.get("evidence_posts") or [])
                for dk, lst in dims.items() if isinstance(lst, list)
                for it in lst if isinstance(it, dict))
    dim_keys = [k for k, v in dims.items() if isinstance(v, list)]
    print(f"[aggregate] 帖子单元={len(recs)} 评论单元={len(comment_recs)} "
          f"维度键={len(dim_keys)} 证据总数={n_eps} 功能数={len(feat['features'])} "
          f"画像×功能矩阵行数={len(feat.get('persona_feature_matrix', {}))}")
    if ps_str:
        print(f"[aggregate] {(axis.label if axis else '分段轴')}分布：{ps_str}")
    uc = dims.get("unit_counts", {})
    if uc.get("excluded_unknown_units"):
        print(f"[aggregate] 已排除无法判定{(axis.label if axis else '分段轴')}的单元 "
              f"{uc['excluded_unknown_units']} 个（不进任何画像桶）")
    print(f"[aggregate] 已写: {dpath}\n           {fpath}")


if __name__ == "__main__":
    main()
