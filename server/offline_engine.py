# -*- coding: utf-8 -*-
"""内置离线 VOC 分析引擎。

不需要任何 API Key 即可产出完整的七维度标注结果：

    1. 把「讨论单元」拆成句子；
    2. 用 taxonomy 的正则词典匹配各维度标签（在句子窗口内判定，避免跨句误判）；
    3. 对命中句做情感判定（含否定词处理；电商语料以星级为先验）；
    4. 把命中的**原句逐字**作为证据（evidence），因此证据天然可溯源、不可编造。

该引擎是平台的默认分析后端。若用户配置了 LLM（`LLM_CONNECTOR_*`），
可切换到 LLM 后端以获得更细的语义理解；两者产出同一套数据契约，
因此前端与报告层完全复用。
"""

from __future__ import annotations

import re
from typing import Iterable

from . import taxonomy

# 句子切分：英文句末标点 / 中文句末标点 / 换行 / 分号
_SENT_SPLIT = re.compile(r"(?<=[.!?。！？；;])\s+|\n+")
_WS = re.compile(r"\s+")

# 预编译标签模式
_COMPILED: dict[str, list[re.Pattern]] = {}
for _dim, _spec in taxonomy.DIMENSIONS.items():
    for _key, _t in _spec["tags"].items():
        _COMPILED[_key] = [re.compile(p, re.I) for p in _t.get("patterns", [])]

_NEG_COMPILED = [re.compile(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", re.I)
                 for w in taxonomy.NEGATORS]
_POS_COMPILED = [re.compile(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", re.I)
                 for w in taxonomy.POSITIVE_WORDS]
_NEGW_COMPILED = [re.compile(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", re.I)
                  for w in taxonomy.NEGATIVE_WORDS]


def split_sentences(text: str) -> list[str]:
    """把文本切成句子（保留原句字节，仅去掉首尾空白）。"""
    if not text:
        return []
    parts = _SENT_SPLIT.split(text)
    out = []
    for p in parts:
        s = p.strip()
        if len(s) >= 2:
            out.append(s)
    return out


def _has_negation(sentence: str) -> bool:
    return any(p.search(sentence) for p in _NEG_COMPILED)


def sentiment_of(sentence: str) -> str:
    """判定单句情感：positive / negative / neutral（含否定翻转）。"""
    if not sentence:
        return "neutral"
    pos = sum(1 for p in _POS_COMPILED if p.search(sentence))
    neg = sum(1 for p in _NEGW_COMPILED if p.search(sentence))

    # 否定词后紧跟正向词 → 翻转为负向（如 "not comfortable"）
    if _has_negation(sentence):
        # 若句中同时有明确的负向词，直接以负向计
        if neg > 0:
            return "negative"
        if pos > 0:
            return "negative"
    if pos > neg and pos > 0:
        return "positive"
    if neg > pos and neg > 0:
        return "negative"
    return "neutral"


def _rating_sentiment(rating) -> str:
    """电商星级 → 情感先验。"""
    try:
        r = float(rating)
    except (TypeError, ValueError):
        return ""
    if r >= 4:
        return "positive"
    if r <= 2:
        return "negative"
    return "neutral"


def match_tags(sentences: list[tuple[str, str, str]]) -> dict[str, dict]:
    """在句子集合上匹配全部标签。

    sentences: [(句子文本, 来源 source(post/review/comment), comment_id)]
    返回 {tag_key: {original_text, sentiment, source, comment_id, hit_count}}
    """
    hits: dict[str, dict] = {}
    for text, source, cid in sentences:
        for key, pats in _COMPILED.items():
            matched = False
            for p in pats:
                if p.search(text):
                    matched = True
                    break
            if not matched:
                continue
            sent = sentiment_of(text)
            cur = hits.get(key)
            if cur is None:
                hits[key] = {
                    "original_text": text,
                    "sentiment": sent,
                    "source": source,
                    "comment_id": cid,
                    "hit_count": 1,
                    "all": [text],
                }
            else:
                cur["hit_count"] += 1
                if text not in cur["all"]:
                    cur["all"].append(text)
                # 优先保留「有情感倾向」的那句作为主证据
                if cur["sentiment"] == "neutral" and sent != "neutral":
                    cur["original_text"] = text
                    cur["sentiment"] = sent
                    cur["source"] = source
                    cur["comment_id"] = cid
    return hits


def detect_purchase_status(sentences: list[tuple[str, str, str]]) -> tuple[str, dict | None]:
    """判定购买状态。purchased / not_purchased / unknown（互斥，purchased 优先）。"""
    purchased_ev = not_purchased_ev = None
    for text, source, cid in sentences:
        if purchased_ev is None:
            for p in _COMPILED["purchased"]:
                if p.search(text):
                    purchased_ev = {"original_text": text, "source": source, "comment_id": cid}
                    break
        if not_purchased_ev is None:
            for p in _COMPILED["not_purchased"]:
                if p.search(text):
                    not_purchased_ev = {"original_text": text, "source": source, "comment_id": cid}
                    break
    if purchased_ev:
        return "purchased", purchased_ev
    if not_purchased_ev:
        return "not_purchased", not_purchased_ev
    return "unknown", None


# 求助/征集式标题 —— 强烈暗示发帖人处于选购阶段
_SHOPPING_TITLE = re.compile(
    r"\b(recommend\w*|recs?\b|which|what|anyone|any\b|best\b|vs\.?\b|help me|advice|"
    r"thoughts|suggest\w*|looking for|in the market|worth it|should i|need\b|compare|"
    r"opinions|experience with|feedback)\b", re.I)


def detect_op_stance(title: str, body_head: str) -> tuple[str, dict | None]:
    """判定**发帖人**的购买立场（只依据标题与正文开头，不含评论）。

    论坛里「发帖人」与「回帖老用户」常常是两类人：前者多处于选购阶段，
    后者多为已购用户。若把评论一并纳入判定，几乎所有帖子都会被判为「已购」，
    使「已购 / 未购人群画像对比」失去意义。因此这里只看发帖人自己的文字。
    """
    title = (title or "").strip()
    text = f"{title} {body_head or ''}".strip()
    if not text:
        return "unknown", None

    # 1) 明确的已购自述（发帖人自称拥有）
    for p in _COMPILED["purchased"]:
        m = p.search(text)
        if m:
            return "purchased", {"original_text": _sentence_of(text, m.start()),
                                 "source": "post", "comment_id": ""}
    # 2) 明确的选购自述
    for p in _COMPILED["not_purchased"]:
        m = p.search(text)
        if m:
            return "not_purchased", {"original_text": _sentence_of(text, m.start()),
                                     "source": "post", "comment_id": ""}
    # 3) 疑问/征集式标题 → 处在选购阶段
    if title.endswith(("?", "？")) or _SHOPPING_TITLE.search(title):
        return "not_purchased", {"original_text": title, "source": "post", "comment_id": ""}
    return "unknown", None


def _sentence_of(text: str, pos: int) -> str:
    """取出包含指定位置的完整句子。"""
    start = max(text.rfind(".", 0, pos), text.rfind("!", 0, pos),
                text.rfind("?", 0, pos), text.rfind("\n", 0, pos)) + 1
    m = re.search(r"[.!?\n]", text[pos:])
    end = pos + (m.start() if m else 90)
    return text[start:end + 1].strip()[:240]


def annotate_unit(unit: dict) -> dict:
    """对单个讨论单元做完整标注，返回标准标注记录。

    unit 结构（由 adapters 产出）：
        {unit_id, title, body, comments:[{comment_id, author, body}],
         meta:{platform, product, rating, date, brands, annotations}}
    """
    meta = unit.get("meta") or {}
    sentences: list[tuple[str, str, str]] = []
    for s in split_sentences(unit.get("title") or ""):
        sentences.append((s, "post", ""))
    for s in split_sentences(unit.get("body") or ""):
        sentences.append((s, "post", ""))
    for c in unit.get("comments") or []:
        for s in split_sentences(c.get("body") or ""):
            sentences.append((s, "comment", c.get("comment_id") or ""))

    hits = match_tags(sentences)

    # 「一手使用经验」：整个单元（含评论）出现已购/在用自述。
    # 用于功能满意度的分母 —— 只有真正用过的人评价功能才有意义。
    unit_status, unit_ev = detect_purchase_status(sentences)
    has_experience = unit_status == "purchased"

    # 「购买状态」：论坛按**发帖人立场**判定（选购者 vs 已购用户），
    # 电商按评论者判定 —— Amazon / Wayfair 的评论只能来自真实买家，
    # 因此**一律视为已购买**，不因为文案里出现「还没买 / 打算买」这类句子就拆出「未购买人群」。
    default_ps = meta.get("default_purchase")
    if meta.get("platform") == "Reddit":
        op_status, op_ev = detect_op_stance(unit.get("title") or "", (unit.get("body") or "")[:600])
        if op_status != "unknown":
            status, status_ev = op_status, op_ev
        else:
            status, status_ev = unit_status, unit_ev
    else:
        if default_ps == "purchased":
            status = "purchased"
            status_ev = unit_ev if unit_ev and unit_status == "purchased" else None
        elif status == "unknown" and default_ps:
            status = default_ps
        else:
            status, status_ev = unit_status, unit_ev
        # 电商评论者都真实用过商品 → 一手使用经验分母即全部单元
        has_experience = True

    # 星级先验：电商评论若整体为负向，把该单元中「中性」的体验类证据下调为负向
    rating_sent = _rating_sentiment(meta.get("rating"))

    # 平台自带标注（Amazon/Wayfair）→ 功能标签的先验
    platform_ann = meta.get("annotations") or []

    annotations: dict[str, dict] = {}

    for key, h in hits.items():
        sent = h["sentiment"]
        info = taxonomy.all_tags().get(key, {})
        dim = info.get("dim", "")
        if (sent == "neutral" and rating_sent
                and dim in ("features", "post_purchase_experience")):
            sent = rating_sent
        annotations[key] = {
            "mentioned": "yes",
            "original_text": h["original_text"],
            "summary": h["original_text"][:200],
            "sentiment": sent,
            "thoughts": "rule-engine",
            "source": h["source"],
            "comment_id": h["comment_id"],
            "hit_count": h["hit_count"],
        }

    # 平台标注补充：导出文件自带 aspect 时映射到功能标签
    for raw in platform_ann:
        key = taxonomy.ANNOTATION_MAP.get(str(raw).strip().lower())
        if not key:
            continue
        if key in annotations:
            continue
        sent = "negative" if str(raw).strip().lower() in taxonomy.ANNOTATION_NEGATIVE else "neutral"
        if sent == "neutral" and rating_sent:
            sent = rating_sent
        annotations[key] = {
            "mentioned": "yes",
            "original_text": f"[平台标注] {raw}",
            "summary": str(raw),
            "sentiment": sent,
            "thoughts": "platform-annotation",
            "source": "platform",
            "comment_id": "",
            "hit_count": 1,
        }

    # 补全未命中的标签（保持契约完整：mentioned=no）
    for key in _COMPILED:
        if key in annotations:
            continue
        annotations[key] = {
            "mentioned": "no", "original_text": "", "summary": "",
            "sentiment": "", "thoughts": "rule-engine",
            "source": "", "comment_id": "", "hit_count": 0,
        }

    rec = {
        "unit_id": unit.get("unit_id"),
        "reddit_id": unit.get("unit_id"),  # 兼容下游命名
        "title": unit.get("title") or "",
        "url": meta.get("url") or "",
        "platform": meta.get("platform") or "",
        "product": meta.get("product") or "",
        "rating": meta.get("rating"),
        "date": meta.get("date") or "",
        "purchase_status": status,
        "purchase_evidence": status_ev,
        "has_experience": has_experience,
        "unit_status": unit_status,
        "comment_count_used": len(unit.get("comments") or []),
        "annotations": annotations,
    }
    return rec


def design_tagset(category: str) -> dict:
    """离线标签集：直接导出内置通用税务词典（保证与标注引擎同源）。"""
    dims = {}
    for dim, spec in taxonomy.DIMENSIONS.items():
        tags = {}
        for key, t in spec["tags"].items():
            tags[key] = {
                "label_cn": t["label_cn"],
                "label_en": t.get("label_en", ""),
                "thoughts": t.get("desc") or t["label_cn"],
                "mentioned": "yes/no",
                "original_text": "命中的原句（逐字）",
                f"{key}_summary": "命中的原句（逐字）",
                "sentiment": "positive/neutral/negative",
            }
        dims[dim] = {
            "label_cn": spec["label_cn"],
            "label_en": spec["label_en"],
            "tags": tags,
        }
    return {
        "category": category,
        "engine": "offline-rule-engine",
        "dimensions": dims,
    }
