# -*- coding: utf-8 -*-
"""聚合层：把逐单元标注结果汇总为前端与报告消费的数据契约。

产出结构（单文件 `result.json`）：

    overview          数据概览与关键指标
    dimensions        七维度分布 + 证据（含购买状态汇总）
    purchaser_personas / non_purchaser_personas / persona_gap
    rating_gap        评分分层的人群差异（电商平台适用）
    features          功能满意度 + 画像×功能矩阵 + 品牌×功能矩阵
    brands            品牌分布与情感
    units             全部讨论单元（「数据原声」页的数据源）

设计要点：
- 满意度 = 正面 / (正面 + 负面)，**中性不计入分母**，使不同功能之间可比。
- 证据只保留逐字原句，且记录来源（post / comment / platform），可回溯到单元。
- 画像×功能交叉**仅在已购人群中统计**（未购人群的功能评价多为选购期预期，与真实
  体验不可比）。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

from . import taxonomy

PURCHASE_KEYS = ("purchased", "not_purchased", "unknown")

PLATFORM_KIND = {
    "Amazon": "ecommerce",
    "Wayfair": "ecommerce",
    "Reddit": "forum",
}

MAX_EVIDENCE_PER_TAG = 8
MAX_UNIT_TAGS = 40


def _dims_of(idx) -> dict:
    """按索引中实际出现的维度，还原 `{dim: {"label_cn":..., "tags": {...}}}`。

    LLM 引擎生成的标签集可能不含全部内置维度，因此不能假设固定的 7 维。
    """
    out: dict[str, dict] = {}
    for key, t in idx.tags.items():
        d = t["dim"]
        if d not in out:
            out[d] = {"label_cn": idx.dim_label(d), "label_en": "", "tags": {}}
        out[d]["tags"][key] = t
    out.setdefault("purchase_status",
                   {"label_cn": idx.dim_label("purchase_status"), "label_en": "", "tags": {}})
    return out


def _satisfaction(pos: int, neg: int) -> float | None:
    den = pos + neg
    if den == 0:
        return None
    return round(pos / den, 4)


def _evidence_list(items: list[dict], limit: int = MAX_EVIDENCE_PER_TAG) -> list[dict]:
    """从 (unit_id, quote, sentiment, source) 列表生成去重证据。"""
    out, seen = [], set()
    for it in items:
        q = (it.get("quote") or "").strip()
        if not q or q in seen:
            continue
        seen.add(q)
        out.append(it)
        if len(out) >= limit:
            break
    return out


def aggregate(units: list[dict], records: list[dict], *,
              category: str, platform: str, engine: str,
              brands: list[str] | None = None,
              tag_index=None) -> dict:
    units_by_id = {u["unit_id"]: u for u in units}
    # 标签来自「标签集」（内置词典或 LLM 生成），聚合层不假设具体的标签键
    idx = tag_index or taxonomy.index_from_taxonomy()
    tag_info = idx.tags

    # ---------------- 概览 ----------------
    purchase_counter = Counter(r.get("purchase_status", "unknown") for r in records)
    rating_counter = Counter()
    ratings = []
    dates = []
    for u in units:
        m = u.get("meta") or {}
        rt = m.get("rating")
        if rt is not None:
            try:
                rf = float(rt)
                ratings.append(rf)
                rating_counter[str(int(round(rf)))] += 1
            except (TypeError, ValueError):
                pass
        if m.get("date"):
            dates.append(str(m["date"]))

    total_comments = sum(len(u.get("comments") or []) for u in units)
    analyzed = len(records)

    # ---------------- 维度聚合 ----------------
    dim_items: dict[str, dict] = {}
    persona_by_purchase: dict[str, dict[str, int]] = {
        k: defaultdict(int) for k in PURCHASE_KEYS
    }
    # 评分分层（好评 4-5 / 中评 3 / 差评 1-2）
    persona_by_rating: dict[str, dict[str, int]] = {
        "high": defaultdict(int), "mid": defaultdict(int), "low": defaultdict(int),
    }
    rid2tier = {}
    for u in units:
        rt = (u.get("meta") or {}).get("rating")
        try:
            rf = float(rt) if rt is not None else None
        except (TypeError, ValueError):
            rf = None
        tier = "high" if (rf is not None and rf >= 4) else \
               "mid" if (rf is not None and rf == 3) else \
               "low" if (rf is not None and rf <= 2) else "unknown"
        rid2tier[u["unit_id"]] = tier

    for dim, spec in _dims_of(idx).items():
        tag_counter: Counter = Counter()
        evidence_pool: dict[str, list[dict]] = defaultdict(list)
        sent_pool: dict[str, Counter] = defaultdict(Counter)
        comment_ev = defaultdict(int)

        for rec in records:
            uid = rec.get("reddit_id")
            status = rec.get("purchase_status", "unknown")
            for key, ann in (rec.get("annotations") or {}).items():
                if ann.get("mentioned") != "yes":
                    continue
                info = tag_info.get(key)
                if not info or info["dim"] != dim:
                    continue
                tag_counter[key] += 1
                sent = (ann.get("sentiment") or "neutral").lower()
                sent_pool[key][sent] += 1
                src = ann.get("source") or ""
                if src == "comment":
                    comment_ev[key] += 1
                evidence_pool[key].append({
                    "unit_id": uid,
                    "quote": ann.get("original_text") or "",
                    "sentiment": sent,
                    "source": src,
                    "comment_id": ann.get("comment_id") or "",
                })
                if dim == "user_personas":
                    if status in PURCHASE_KEYS:
                        persona_by_purchase[status][key] += 1
                    tier = rid2tier.get(uid, "unknown")
                    if tier in persona_by_rating:
                        persona_by_rating[tier][key] += 1

        total = sum(tag_counter.values())
        items = []
        for key, c in tag_counter.most_common():
            t = spec["tags"][key]
            items.append({
                "key": key,
                "label_cn": t["label_cn"],
                "label_en": t.get("label_en", ""),
                "count": c,
                "percentage": round(c / total * 100, 1) if total else 0.0,
                "mention_rate": round(c / analyzed * 100, 1) if analyzed else 0.0,
                "positive": sent_pool[key].get("positive", 0),
                "negative": sent_pool[key].get("negative", 0),
                "neutral": sent_pool[key].get("neutral", 0),
                "comment_evidence": comment_ev.get(key, 0),
                "reason": t.get("desc") or "",
                "evidence": _evidence_list(evidence_pool.get(key, [])),
            })
        dim_items[dim] = {
            "key": dim,
            "label_cn": spec["label_cn"],
            "label_en": spec["label_en"],
            "total_hits": total,
            "items": items,
        }

    # 购买状态分布（含证据）
    ps_spec = {"label_cn": idx.dim_label("purchase_status"), "tags": {}}
    ps_items = []
    for key in ("purchased", "not_purchased"):
        c = purchase_counter.get(key, 0)
        if c == 0:
            continue
        ev = []
        for rec in records:
            if rec.get("purchase_status") != key:
                continue
            pe = rec.get("purchase_evidence") or {}
            if pe.get("original_text"):
                ev.append({
                    "unit_id": rec.get("reddit_id"),
                    "quote": pe["original_text"],
                    "sentiment": "",
                    "source": pe.get("source", ""),
                    "comment_id": pe.get("comment_id", ""),
                })
        ps_items.append({
            "key": key,
            "label_cn": idx.label(key),
            "count": c,
            "percentage": round(c / analyzed * 100, 1) if analyzed else 0.0,
            "reason": (idx.get(key) or {}).get("desc", ""),
            "evidence": _evidence_list(ev, 6),
        })

    # 购买状态分桶（用于人群拆分）
    def _persona_dist(bucket: dict[str, int]) -> list[dict]:
        tot = sum(bucket.values())
        out = []
        for key, c in sorted(bucket.items(), key=lambda x: -x[1]):
            t = idx.get(key)
            if not t or t["dim"] != "user_personas":
                continue
            out.append({
                "key": key,
                "label_cn": t["label_cn"],
                "label_en": t.get("label_en", ""),
                "count": c,
                "percentage": round(c / tot * 100, 1) if tot else 0.0,
            })
        return out

    purchaser_personas = _persona_dist(persona_by_purchase["purchased"])
    non_purchaser_personas = _persona_dist(persona_by_purchase["not_purchased"])
    gap = []
    pp = {x["key"]: x for x in purchaser_personas}
    np_ = {x["key"]: x for x in non_purchaser_personas}
    for key in set(pp) | set(np_):
        a = pp.get(key, {}).get("percentage", 0.0)
        b = np_.get(key, {}).get("percentage", 0.0)
        t = idx.get(key) or {}
        gap.append({
            "key": key,
            "label_cn": t.get("label_cn", key),
            "purchased_pct": a,
            "not_purchased_pct": b,
            "gap": round(a - b, 1),
        })
    gap.sort(key=lambda x: -abs(x["gap"]))

    rating_gap = []
    hd = _persona_dist(persona_by_rating["high"])
    ld = _persona_dist(persona_by_rating["low"])
    hh = {x["key"]: x for x in hd}
    ll = {x["key"]: x for x in ld}
    for key in set(hh) | set(ll):
        a = hh.get(key, {}).get("percentage", 0.0)
        b = ll.get(key, {}).get("percentage", 0.0)
        t = idx.get(key) or {}
        rating_gap.append({
            "key": key,
            "label_cn": t.get("label_cn", key),
            "high_pct": a,
            "low_pct": b,
            "gap": round(a - b, 1),
        })
    rating_gap.sort(key=lambda x: -abs(x["gap"]))

    # ---------------- 功能体验 ----------------
    feat_spec = {k: v for k, v in idx.tags.items() if v["dim"] == "features"}
    feat_counter: Counter = Counter()
    feat_sent: dict[str, Counter] = defaultdict(Counter)
    feat_ev: dict[str, list[dict]] = defaultdict(list)
    persona_feature: dict[str, dict] = defaultdict(lambda: defaultdict(Counter))
    brand_feature: dict[str, dict] = defaultdict(lambda: defaultdict(Counter))
    brand_counter: Counter = Counter()
    brand_sent: dict[str, Counter] = defaultdict(Counter)
    brand_purchase: dict[str, Counter] = defaultdict(Counter)

    uid2brands = {}
    for rec in records:
        uid = rec.get("reddit_id")
        unit = units_by_id.get(uid) or {}
        m = unit.get("meta") or {}
        uid2brands[uid] = m.get("brands") or []

    for rec in records:
        uid = rec.get("reddit_id")
        status = rec.get("purchase_status", "unknown")
        unit = units_by_id.get(uid) or {}
        personas = [k for k, a in (rec.get("annotations") or {}).items()
                    if a.get("mentioned") == "yes"
                    and (tag_info.get(k) or {}).get("dim") == "user_personas"]
        brands = uid2brands.get(uid, [])
        for b in brands:
            brand_counter[b] += 1
            brand_purchase[b][status] += 1

        for key, ann in (rec.get("annotations") or {}).items():
            info = tag_info.get(key)
            if not info:
                continue
            if info["dim"] not in ("features", "post_purchase_experience"):
                continue
            if ann.get("mentioned") != "yes":
                continue
            if info["dim"] == "features":
                feat_counter[key] += 1
                sent = (ann.get("sentiment") or "neutral").lower()
                feat_sent[key][sent] += 1
                feat_ev[key].append({
                    "unit_id": uid,
                    "quote": ann.get("original_text") or "",
                    "sentiment": sent,
                    "source": ann.get("source") or "",
                    "comment_id": ann.get("comment_id") or "",
                })
                for b in brands:
                    brand_feature[b][key]["total"] += 1
                    if sent == "positive":
                        brand_feature[b][key]["pos"] += 1
                    elif sent == "negative":
                        brand_feature[b][key]["neg"] += 1
                # 画像 × 功能交叉：以「有一手使用经验」的单元为分母
                # （功能评价须来自真实使用者，而非选购期的预期）
                if rec.get("has_experience"):
                    for pk in personas:
                        persona_feature[pk][key]["total"] += 1
                        if sent == "positive":
                            persona_feature[pk][key]["pos"] += 1
                        elif sent == "negative":
                            persona_feature[pk][key]["neg"] += 1
            # 品牌情感来自购后体验维度
            if info["dim"] == "post_purchase_experience" and brands:
                sent = (ann.get("sentiment") or "neutral").lower()
                for b in brands:
                    brand_sent[b][sent] += 1

    feature_items = []
    for key, c in feat_counter.most_common():
        s = feat_sent[key]
        pos, neg, neu = s.get("positive", 0), s.get("negative", 0), s.get("neutral", 0)
        t = feat_spec.get(key, {})
        feature_items.append({
            "key": key,
            "name": t.get("label_cn", key),
            "name_en": t.get("label_en", ""),
            "total": c,
            "positive": pos,
            "negative": neg,
            "neutral": neu,
            "satisfaction": _satisfaction(pos, neg),
            "mention_rate": round(c / analyzed * 100, 1) if analyzed else 0.0,
            "evidence": _evidence_list(feat_ev.get(key, [])),
        })

    def _matrix(src: dict) -> dict:
        out = {}
        for a, feats in src.items():
            row = {}
            for fk, cc in feats.items():
                sat = _satisfaction(cc["pos"], cc["neg"])
                if cc["total"] <= 0:
                    continue
                row[feat_spec.get(fk, {}).get("label_cn", fk)] = {
                    "total": cc["total"],
                    "satisfaction": sat,
                    "positive": cc["pos"],
                    "negative": cc["neg"],
                }
            if row:
                out[a] = row
        return out

    persona_label = {}
    for key, t in idx.tags.items():
        if t["dim"] == "user_personas":
            persona_label[key] = t["label_cn"]
    persona_matrix = {
        persona_label.get(k, k): v for k, v in _matrix(persona_feature).items()
    }
    brand_matrix = _matrix(brand_feature)

    # ---------------- 品牌 ----------------
    brand_total = sum(brand_counter.values())
    brand_items = []
    for b, c in brand_counter.most_common():
        s = brand_sent.get(b, Counter())
        pos, neg, neu = s.get("positive", 0), s.get("negative", 0), s.get("neutral", 0)
        p = brand_purchase.get(b, Counter())
        gp = (p.get("purchased", 0) + p.get("not_purchased", 0)) or 1
        ev = []
        for rec in records:
            if b in (uid2brands.get(rec.get("reddit_id")) or []):
                for key, ann in (rec.get("annotations") or {}).items():
                    info = tag_info.get(key)
                    if not info or info["dim"] != "post_purchase_experience":
                        continue
                    if ann.get("mentioned") != "yes":
                        continue
                    ev.append({
                        "unit_id": rec.get("reddit_id"),
                        "quote": ann.get("original_text") or "",
                        "sentiment": (ann.get("sentiment") or "neutral").lower(),
                        "source": ann.get("source") or "",
                        "comment_id": ann.get("comment_id") or "",
                    })
                break
        brand_items.append({
            "name": b,
            "count": c,
            "percentage": round(c / analyzed * 100, 1) if analyzed else 0.0,
            "positive": pos, "negative": neg, "neutral": neu,
            "satisfaction": _satisfaction(pos, neg),
            "purchased_count": p.get("purchased", 0),
            "not_purchased_count": p.get("not_purchased", 0),
            "purchased_share": round(p.get("purchased", 0) / gp * 100, 1),
            "evidence": _evidence_list(ev, 4),
        })

    # ---------------- 单元（数据原声） ----------------
    out_units = []
    for u in units:
        uid = u["unit_id"]
        rec = next((r for r in records if r.get("reddit_id") == uid), None)
        tags = []
        if rec:
            for key, ann in (rec.get("annotations") or {}).items():
                if ann.get("mentioned") != "yes":
                    continue
                info = tag_info.get(key)
                if not info:
                    continue
                if info["dim"] == "purchase_status":
                    continue
                tags.append({
                    "key": key,
                    "label_cn": info.get("label_cn") or idx.label(key),
                    "dim": info["dim"],
                    "dim_label": idx.dim_label(info["dim"]),
                    "quote": ann.get("original_text") or "",
                    "sentiment": ann.get("sentiment") or "",
                    "source": ann.get("source") or "",
                    "comment_id": ann.get("comment_id") or "",
                })
        tags.sort(key=lambda x: x["dim"])
        m = u.get("meta") or {}
        # 展示用评论：**不设额外上限**。单元的 comments 已在适配阶段按
        # 「每帖前 N 条」截断过，这里再截一次会导致第 21 条以后产生的证据
        # 在详情页看不到出处（引用无法自证）。
        comments = []
        for c in (u.get("comments") or []):
            comments.append({
                "comment_id": c.get("comment_id", ""),
                "author": c.get("author", ""),
                # 展示上限设得足够高（Reddit 长评可达数千字符），
                # 确保任何被引用的原句都能在详情中完整看到
                "body": (c.get("body") or "")[:8000],
            })
        out_units.append({
            "unit_id": uid,
            "title": u.get("title") or "",
            "body": (u.get("body") or "")[:4000],
            "url": m.get("url") or "",
            "platform": m.get("platform") or platform,
            "product": m.get("product") or "",
            "sku": m.get("sku") or "",
            "subreddit": m.get("subreddit") or "",
            "rating": m.get("rating"),
            "date": m.get("date") or "",
            "purchase_status": (rec or {}).get("purchase_status", "unknown"),
            "brands": m.get("brands") or [],
            "annotations": m.get("annotations") or [],
            "comment_count": len(u.get("comments") or []),
            "comment_total": m.get("comment_total"),
            "comments": comments,
            "tags": tags[:MAX_UNIT_TAGS],
        })

    avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None

    return {
        "category": category,
        "platform": platform,
        "platform_kind": PLATFORM_KIND.get(platform, "ecommerce"),
        "engine": engine,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overview": {
            "units_total": len(units),
            "units_analyzed": analyzed,
            "comments_total": total_comments,
            "units_with_comments": sum(1 for u in units if (u.get("comments") or [])),
            "date_min": min(dates) if dates else "",
            "date_max": max(dates) if dates else "",
            "avg_rating": avg_rating,
            "rating_dist": {k: rating_counter.get(k, 0) for k in ("1", "2", "3", "4", "5")},
            "purchase": {k: purchase_counter.get(k, 0) for k in PURCHASE_KEYS},
            "experience_units": sum(1 for r in records if r.get("has_experience")),
            "tag_hits": sum(d["total_hits"] for d in dim_items.values()),
            "products": len({(u.get("meta") or {}).get("sku") for u in units
                             if (u.get("meta") or {}).get("sku")}) or 1,
        },
        "dimensions": dim_items,
        "purchase_status": {"label_cn": ps_spec["label_cn"], "items": ps_items},
        "purchaser_personas": purchaser_personas,
        "non_purchaser_personas": non_purchaser_personas,
        "persona_gap": gap,
        "rating_gap": rating_gap,
        "features": {
            "items": feature_items,
            "persona_feature_matrix": persona_matrix,
            "brand_feature_matrix": brand_matrix,
        },
        "brands": {"items": brand_items, "total_mentions": brand_total},
        "units": out_units,
    }
