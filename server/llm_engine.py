# -*- coding: utf-8 -*-
"""LLM 分析引擎。

与内置离线引擎产出**完全相同的数据契约**，因此聚合层、前端、报告层完全复用，
用户可以在界面上切换用哪个引擎跑。

两个阶段：
    1. design_tagset()   依据品类 + 语料抽样，生成**品类专属**的七维度标签集；
    2. annotate_units()  用该标签集逐单元标注（支持批量 + 并发）。

**证据可信性**：LLM 给出的引用一律经过 `_locate()` 在原文中逐字回查 ——
找到则存**原文切片**（保证逐字），找不到则丢弃该标签并记入告警。
这样即使模型产生幻觉引用，也不会进入结果。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import llm, taxonomy

# 单单元送入 prompt 的文本上限（字符）
UNIT_CHAR_CAP = 7000
COMMENT_CHAR_CAP = 900


# ---------------------------------------------------------------------------
# 文本定位（逐字证据的关键）
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def _norm_with_map(text: str) -> tuple[str, list[int]]:
    """把文本归一化（折叠空白）并返回「归一化位置 → 原文位置」的映射。"""
    out_chars: list[str] = []
    idx_map: list[int] = []
    prev_ws = False
    for i, ch in enumerate(text):
        if ch.isspace():
            if prev_ws:
                continue
            out_chars.append(" ")
            idx_map.append(i)
            prev_ws = True
        else:
            out_chars.append(ch)
            idx_map.append(i)
            prev_ws = False
    return "".join(out_chars), idx_map


def locate_in(text: str, quote: str) -> str | None:
    """在 text 中定位 quote，返回**原文中的切片**（保证逐字）；找不到返回 None。"""
    if not text or not quote:
        return None
    q = quote.strip()
    if not q:
        return None
    if q in text:                      # 直接命中，最快路径
        return q
    nt, mp = _norm_with_map(text)
    nq, _ = _norm_with_map(q)
    nq = nq.strip()
    if not nq:
        return None
    pos = nt.find(nq)
    if pos < 0:
        return None
    start = mp[pos]
    end = mp[pos + len(nq) - 1] + 1
    return text[start:end]


def locate_in_unit(unit: dict, quote: str) -> tuple[str, str, str] | None:
    """在讨论单元（标题 + 正文 + 各条评论）中定位引用。

    返回 (原文字面量, source, comment_id)；找不到返回 None。
    """
    title = unit.get("title") or ""
    body = unit.get("body") or ""
    post_text = (title + "\n" + body).strip()
    if post_text:
        hit = locate_in(post_text, quote)
        if hit:
            return hit, "post", ""
    for c in (unit.get("comments") or []):
        cb = c.get("body") or ""
        hit = locate_in(cb, quote)
        if hit:
            return hit, "comment", c.get("comment_id") or ""
    return None


# ---------------------------------------------------------------------------
# 阶段一：标签集设计
# ---------------------------------------------------------------------------

DESIGN_SYSTEM = """你是资深的 VOC（用户之声）分析专家，为消费品做评论/论坛语料的标签体系设计。
你只输出 JSON，不输出任何解释文字。"""

DESIGN_USER = """品类：{category}
数据平台：{platform}（{kind_desc}）

下面是该品类真实语料的抽样（帖子标题 / 评论文本摘录）：

{corpus}

---
请基于以上语料，设计一套**该品类专属**的 VOC 标签体系，输出 JSON：

{{
  "dimensions": {{
    "user_personas":         {{"label_cn": "人群画像",   "tags": {{"<英文键名>": {{"label_cn": "<中文标签>", "thoughts": "<什么文本应判为该标签>"}}}}}},
    "usage_scenarios":       {{"label_cn": "使用场景",   "tags": {{...}}}},
    "pre_purchase_concerns": {{"label_cn": "购买前关注点", "tags": {{...}}}},
    "post_purchase_experience": {{"label_cn": "购买后体验", "tags": {{...}}}},
    "features":              {{"label_cn": "功能体验",   "tags": {{...}}}},
    "unmet_needs":           {{"label_cn": "未满足需求", "tags": {{...}}}},
    "purchase_status":       {{"label_cn": "购买状态",   "tags": {{
        "purchased":     {{"label_cn": "已购买人群", "thoughts": "出现明确的第一人称已购买/已持有表述"}},
        "not_purchased": {{"label_cn": "未购买人群", "thoughts": "处于选购/调研阶段，尚未购买"}}
    }}}}
  }}
}}

硬性要求：
1. 维度键名必须与上面完全一致，七个都要有；`purchase_status` 只能有 `purchased` 和 `not_purchased`。
2. 除 `purchase_status` 外，每个维度给 4~12 个标签。
3. 标签键名用英文小写+下划线；`label_cn` 用中文，**不得含括号、斜杠、逗号等符号**。
4. `thoughts` 一句话说明判定标准，要具体到可操作（写清哪些文本信号应该命中）。
5. 标签必须来自上面的语料，体现该品类特有的关注点，不要照搬通用模板。
6. **`features`（功能体验）与 `unmet_needs`（未满足需求）必须紧贴该品类的具体产品属性**。"""


def build_corpus_sample(units: list[dict], max_units: int = 30,
                        per_unit_chars: int = 320) -> str:
    """把语料抽样成紧凑文本，供标签设计阶段阅读。"""
    lines = []
    step = max(1, len(units) // max_units)
    picked = units[::step][:max_units]
    for i, u in enumerate(picked, 1):
        t = (u.get("title") or "").strip().replace("\n", " ")
        b = (u.get("body") or "").strip().replace("\n", " ")
        lines.append(f"[{i}] {t}")
        if b:
            lines.append(f"    {b[:per_unit_chars]}")
        for c in (u.get("comments") or [])[:3]:
            cb = (c.get("body") or "").strip().replace("\n", " ")
            if cb:
                lines.append(f"    - 评论: {cb[:180]}")
    return "\n".join(lines)


def _slug(s: str, fallback: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", str(s or "")).strip("_").lower()
    return s[:40] or fallback


def design_tagset(units: list[dict], *, category: str, platform: str,
                  progress=None) -> dict:
    """用 LLM 生成品类专属标签集。失败抛 llm.LLMError。"""
    if progress:
        progress("正在用 LLM 设计品类专属标签体系…")
    kind_desc = "商品评论，一条评论即一个分析单元" if platform in ("Amazon", "Wayfair") \
        else "论坛讨论，一篇帖子 + 前若干条评论为一个分析单元"
    corpus = build_corpus_sample(units)
    content, usage = llm.chat(DESIGN_SYSTEM,
                              DESIGN_USER.format(category=category, platform=platform,
                                                 kind_desc=kind_desc, corpus=corpus),
                              json_mode=True, temperature=0.3, max_tokens=6000)
    data = _extract_json(content)
    dims = data.get("dimensions") or {}
    if not dims:
        raise llm.LLMError("LLM 未返回 dimensions 字段")

    # 归一化：维度键、标签键、必填字段
    out_dims: dict[str, dict] = {}
    for dim, spec in dims.items():
        dkey = _slug(dim, dim)
        tags_in = (spec or {}).get("tags") or {}
        tags: dict[str, dict] = {}
        for k, t in tags_in.items():
            if isinstance(t, str):
                t = {"label_cn": t}
            if not isinstance(t, dict):
                continue
            label = str(t.get("label_cn") or k).strip()
            # 标签名不得含符号（与离线引擎保持同一规范）
            label = re.sub(r"[（）()\[\]【】/、,，;；]", "", label)[:24]
            if not label:
                continue
            tags[_slug(k, f"t{len(tags)}")] = {
                "label_cn": label,
                "label_en": str(t.get("label_en") or "").strip(),
                "thoughts": str(t.get("thoughts") or t.get("desc") or label).strip()[:200],
            }
        if not tags:
            continue
        out_dims[dkey] = {
            "label_cn": str((spec or {}).get("label_cn") or
                            taxonomy.DIMENSIONS.get(dkey, {}).get("label_cn", dkey)),
            "label_en": str((spec or {}).get("label_en") or ""),
            "tags": tags,
        }

    # 购买状态必须存在且互斥
    out_dims["purchase_status"] = {
        "label_cn": "购买状态",
        "label_en": "Purchase status",
        "tags": {
            "purchased": {"label_cn": "已购买人群", "label_en": "Purchased",
                          "thoughts": "出现明确的第一人称已购买 / 已持有 / 正在使用表述"},
            "not_purchased": {"label_cn": "未购买人群", "label_en": "Not purchased",
                              "thoughts": "处于选购 / 调研 / 求推荐阶段，尚未购买"},
        },
    }
    # 补齐缺失的标准维度（用内置标签兜底，避免维度整块缺失）
    for d, spec in taxonomy.DIMENSIONS.items():
        if d not in out_dims and d != "purchase_status":
            out_dims[d] = {
                "label_cn": spec["label_cn"],
                "label_en": spec["label_en"],
                "tags": {k: {"label_cn": t["label_cn"], "label_en": t.get("label_en", ""),
                             "thoughts": t.get("desc", t["label_cn"])}
                         for k, t in spec["tags"].items()},
            }

    return {
        "category": category,
        "platform": platform,
        "engine": "llm",
        "model": llm.load_config().get("model", ""),
        "usage": usage,
        "dimensions": out_dims,
    }


# ---------------------------------------------------------------------------
# 阶段二：逐单元标注
# ---------------------------------------------------------------------------

ANNOTATE_SYSTEM = """你是严谨的 VOC 语料标注员。
规则：
1. 只能判断给定标签是否被提及；不要发明新标签。
2. 引用的原文必须**逐字**来自语料，不得改写、翻译或拼接；不得凭空生成。
3. 只输出 JSON，不输出任何解释文字。"""

ANNOTATE_USER = """## 标签集定义

{tagdefs}

## 待标注讨论单元（共 {n} 个）

{units}

## 输出要求

对**每一个**单元 id 输出结果：

{{
  "<单元id>": {{
    "purchase_status": "purchased | not_purchased | unknown",
    "tags": {{
      "<标签键>": {{
        "quote": "<原文逐字引用，1-2 句；必须能在语料中找到>",
        "source": "post | comment",
        "sentiment": "positive | negative | neutral"
      }}
    }}
  }}
}}

规则：
- `tags` 里只放**确实被提及**的标签；未提及的标签直接省略，不要写出来。
- `quote` 必须逐字来自该单元语料，否则该标签会被丢弃。
- `source` 填 `post` 表示来自标题/正文，`comment` 表示来自评论。
- `purchase_status` 三选一：有明确第一人称已购/在用表述填 `purchased`；
  明确处于选购阶段填 `not_purchased`；无法判断填 `unknown`。
- 情感以引用句的语气为准；中立陈述填 `neutral`。"""


def _render_tagdefs(tagset: dict) -> str:
    lines = []
    for dim, spec in (tagset.get("dimensions") or {}).items():
        if dim == "purchase_status":
            continue
        lines.append(f"### {spec.get('label_cn', dim)}（{dim}）")
        for k, t in (spec.get("tags") or {}).items():
            lines.append(f"- {k} = {t.get('label_cn')}　判定：{t.get('thoughts', '')}")
        lines.append("")
    lines.append("### 购买状态（purchase_status）")
    lines.append("- purchased / not_purchased / unknown（见下方说明）")
    return "\n".join(lines)


def _render_unit(u: dict) -> str:
    t = (u.get("title") or "").strip()
    b = (u.get("body") or "").strip()
    parts = [f"id={u['unit_id']}", f"标题: {t}"]
    if b:
        parts.append(f"正文: {b[:UNIT_CHAR_CAP]}")
    cs = u.get("comments") or []
    if cs:
        parts.append(f"评论（{len(cs)} 条）:")
        for c in cs:
            cb = (c.get("body") or "").strip().replace("\n", " ")
            parts.append(f"  [cid={c.get('comment_id', '')}] {cb[:COMMENT_CHAR_CAP]}")
    text = "\n".join(parts)
    return text[:UNIT_CHAR_CAP]


def _extract_json(content: str) -> dict:
    """从模型输出里稳健地取出 JSON 对象。"""
    if not content:
        raise llm.LLMError("LLM 返回空内容")
    s = content.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(s[i:j + 1])
        except json.JSONDecodeError as e:
            raise llm.LLMError(f"LLM 输出不是合法 JSON：{e}")
    raise llm.LLMError("LLM 输出中未找到 JSON 对象")


def _make_record(unit: dict, obj: dict, tag_index, stats: Counter) -> dict:
    """把 LLM 的稀疏输出转成标准标注记录（含证据逐字回查）。"""
    unit_id = unit["unit_id"]
    tags_out = obj.get("tags") or {}
    ps = str(obj.get("purchase_status") or "unknown").strip().lower()
    if ps not in ("purchased", "not_purchased", "unknown"):
        ps = "unknown"

    annotations: dict[str, dict] = {}
    purchase_ev = None
    for key, raw in tags_out.items():
        if not isinstance(raw, dict):
            continue
        info = tag_index.get(key)
        if not info:
            stats["unknown_tag"] += 1
            continue
        quote = str(raw.get("quote") or "").strip()
        if not quote:
            stats["empty_quote"] += 1
            continue
        hit = locate_in_unit(unit, quote)
        if not hit:
            # 引用无法在原文定位 → 丢弃，绝不写入编造内容
            stats["dropped_quote"] += 1
            continue
        literal, src, cid = hit
        sent = str(raw.get("sentiment") or "neutral").strip().lower()
        if sent not in ("positive", "negative", "neutral"):
            sent = "neutral"
        if info["dim"] == "purchase_status":
            if purchase_ev is None:
                purchase_ev = {"original_text": literal, "source": src, "comment_id": cid}
            continue
        if key in annotations:
            annotations[key]["hit_count"] += 1
            continue
        annotations[key] = {
            "mentioned": "yes",
            "original_text": literal,
            "summary": literal[:200],
            "sentiment": sent,
            "thoughts": "llm",
            "source": src,
            "comment_id": cid,
            "hit_count": 1,
        }
        stats["kept"] += 1

    # purchase_status 由模型判定；若模型给了引用则作为证据
    status_ev = purchase_ev
    if status_ev is None and ps != "unknown":
        # 退一步：用该单元任意一条已保留证据作为状态证据（不伪造文本）
        first = next(iter(annotations.values()), None)
        if first:
            status_ev = {"original_text": first["original_text"],
                         "source": first["source"], "comment_id": first["comment_id"]}

    # 与内置引擎保持形态一致：把购买状态也记成一条标注（证据为真实引用）
    if ps in ("purchased", "not_purchased") and tag_index.get(ps) and ps not in annotations:
        ev = status_ev or {}
        annotations[ps] = {
            "mentioned": "yes",
            "original_text": ev.get("original_text", ""),
            "summary": "",
            "sentiment": "",
            "thoughts": "llm",
            "source": ev.get("source", ""),
            "comment_id": ev.get("comment_id", ""),
            "hit_count": 1 if ev.get("original_text") else 0,
        }

    # 未命中的标签补全（保持契约完整）
    for key in tag_index.tags:
        if key in annotations:
            continue
        annotations[key] = {
            "mentioned": "no", "original_text": "", "summary": "",
            "sentiment": "", "thoughts": "llm", "source": "",
            "comment_id": "", "hit_count": 0,
        }

    default_ps = (unit.get("meta") or {}).get("default_purchase")
    # Amazon / Wayfair 的评论只能来自真实买家 —— 即便模型从文案里读出「还没买」也一律判为已购，
    # 避免凭空造出一批并不存在的「未购买人群」。
    if default_ps == "purchased":
        ps = "purchased"
    elif ps == "unknown" and default_ps:
        ps = default_ps
    has_experience = any(
        a["mentioned"] == "yes" and k != "purchase_status"
        for k, a in annotations.items()
        if (tag_index.get(k) or {}).get("dim") == "features") or ps == "purchased"
    if default_ps == "purchased":
        has_experience = True

    return {
        "unit_id": unit_id,
        "reddit_id": unit_id,
        "title": unit.get("title") or "",
        "url": (unit.get("meta") or {}).get("url") or "",
        "platform": (unit.get("meta") or {}).get("platform") or "",
        "product": (unit.get("meta") or {}).get("product") or "",
        "rating": (unit.get("meta") or {}).get("rating"),
        "date": (unit.get("meta") or {}).get("date") or "",
        "purchase_status": ps,
        "purchase_evidence": status_ev,
        "has_experience": has_experience,
        "comment_count_used": len(unit.get("comments") or []),
        "annotations": annotations,
    }


def annotate_units(units: list[dict], tagset: dict, *, batch_size: int = 4,
                   threads: int = 4, progress=None) -> tuple[list[dict], dict]:
    """批量 + 并发标注。返回 (记录列表, 统计)。"""
    tag_index = taxonomy.index_from_tagset(tagset)
    tagdefs = _render_tagdefs(tagset)
    stats: Counter = Counter()
    records: dict[str, dict] = {}
    total = len(units)
    done = 0

    batches = [units[i:i + batch_size] for i in range(0, total, batch_size)]

    def run_batch(batch):
        body = "\n\n".join(_render_unit(u) for u in batch)
        content, _ = llm.chat(ANNOTATE_SYSTEM,
                              ANNOTATE_USER.format(tagdefs=tagdefs, n=len(batch), units=body),
                              json_mode=True, temperature=0, max_tokens=8000)
        return _extract_json(content)

    def handle(batch, data, local_stats):
        out = []
        for u in batch:
            obj = data.get(u["unit_id"])
            if not isinstance(obj, dict):
                local_stats["missing_unit"] += 1
                obj = {"purchase_status": "unknown", "tags": {}}
            out.append(_make_record(u, obj, tag_index, local_stats))
        return out

    def one(u):
        content, _ = llm.chat(ANNOTATE_SYSTEM,
                              ANNOTATE_USER.format(tagdefs=tagdefs, n=1, units=_render_unit(u)),
                              json_mode=True, temperature=0, max_tokens=8000)
        data = _extract_json(content)
        obj = data.get(u["unit_id"])
        if not isinstance(obj, dict):
            cands = [v for v in data.values() if isinstance(v, dict)]
            obj = cands[0] if cands else {"purchase_status": "unknown", "tags": {}}
        return _make_record(u, obj, tag_index, stats)

    lock = __import__("threading").Lock()

    def work(batch):
        nonlocal done
        local = Counter()
        try:
            data = run_batch(batch)
            recs = handle(batch, data, local)
        except llm.LLMError:
            recs = []
            for u in batch:          # 整批失败 → 逐条补标
                try:
                    recs.append(one(u))
                except llm.LLMError as e:
                    local["failed"] += 1
                    recs.append(_make_record(u, {}, tag_index, local))
        with lock:
            stats.update(local)
            for r in recs:
                records[r["unit_id"]] = r
            done += len(batch)
            if progress:
                progress(f"LLM 标注 {done}/{total}…")

    with ThreadPoolExecutor(max_workers=max(1, threads)) as ex:
        futs = [ex.submit(work, b) for b in batches]
        for f in as_completed(futs):
            f.result()

    ordered = [records[u["unit_id"]] for u in units if u["unit_id"] in records]
    return ordered, dict(stats)
