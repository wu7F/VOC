# -*- coding: utf-8 -*-
"""Markdown 洞察报告生成。

严格基于聚合结果，不做任何推测性补全：数据不足的维度如实标注。
所有引用均为逐字原句并附来源链接。
"""

from __future__ import annotations

from . import taxonomy


def _fmt_pct(x) -> str:
    try:
        return f"{float(x):.1f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_sat(x) -> str:
    if x is None:
        return "-"
    try:
        return f"{float(x) * 100:.0f}%"
    except (TypeError, ValueError):
        return "-"


def _link(unit) -> str:
    url = (unit or {}).get("url") or ""
    uid = (unit or {}).get("unit_id") or ""
    if url:
        return f"[{uid}]({url})"
    return f"`{uid}`"


def _source_tag(ev: dict) -> str:
    s = ev.get("source") or ""
    if s == "comment":
        return "💬 评论"
    if s == "platform":
        return "🏷 平台标注"
    if s == "post":
        return "📄 正文"
    return ""


def _evidence_block(items: list[dict], unit_map: dict, n: int = 3) -> str:
    lines = []
    for ev in items[:n]:
        q = (ev.get("quote") or "").strip()
        if not q:
            continue
        if len(q) > 260:
            q = q[:260] + "…"
        u = unit_map.get(ev.get("unit_id"), {})
        lines.append(f"> {q}\n> — 来源：{_link(u)}　{_source_tag(ev)}")
    return "\n\n".join(lines)


def _dim_table(items: list[dict], with_rate: bool = True) -> str:
    if not items:
        return "_无数据_"
    if with_rate:
        out = ["| 标签 | 命中单元 | 占本维度 | 提及率 |", "|---|---:|---:|---:|"]
        for it in items:
            out.append(f"| {it['label_cn']} | {it['count']} | "
                       f"{_fmt_pct(it.get('percentage'))} | {_fmt_pct(it.get('mention_rate'))} |")
    else:
        out = ["| 标签 | 命中单元 | 占本群体 |", "|---|---:|---:|"]
        for it in items:
            out.append(f"| {it['label_cn']} | {it['count']} | {_fmt_pct(it.get('percentage'))} |")
    return "\n".join(out)


def build_report(result: dict, dataset_name: str = "") -> str:
    ov = result.get("overview", {})
    dims = result.get("dimensions", {})
    feats = result.get("features", {})
    brands = result.get("brands", {})
    platform = result.get("platform", "")
    category = result.get("category", "")
    kind = result.get("platform_kind", "ecommerce")
    unit_map = {u["unit_id"]: u for u in result.get("units", [])}

    L: list[str] = []
    L.append(f"# {category} · {platform} VOC 洞察报告\n")
    L.append(f"**分析平台：** {platform}　|　**品类：** {category}　|　**生成时间：** {result.get('generated_at','')}")
    src = "用户上传的电商评论数据" if kind == "ecommerce" else "Reddit 帖子与评论"
    L.append(f"**数据来源：** {src}{('（' + dataset_name + '）') if dataset_name else ''}")
    L.append(f"**分析引擎：** {'内置离线规则引擎' if result.get('engine') == 'offline-rule-engine' else result.get('engine','')}\n")

    # 一、数据概览
    L.append("## 一、数据概览\n")
    L.append("| 指标 | 数值 |")
    L.append("|---|---:|")
    L.append(f"| 讨论单元总数 | {ov.get('units_total', 0)} |")
    L.append(f"| 纳入分析单元 | {ov.get('units_analyzed', 0)} |")
    if ov.get("comments_total"):
        L.append(f"| 纳入评论数 | {ov.get('comments_total', 0)} |")
        L.append(f"| 含评论的单元 | {ov.get('units_with_comments', 0)} |")
    if ov.get("avg_rating") is not None:
        L.append(f"| 平均星级 | {ov.get('avg_rating')} |")
        rd = ov.get("rating_dist") or {}
        L.append("| 星级分布 | " + "　".join(f"{k}★ {rd.get(k,0)}" for k in ("1","2","3","4","5")) + " |")
    if ov.get("date_min"):
        L.append(f"| 时间范围 | {ov.get('date_min')} ~ {ov.get('date_max')} |")
    L.append(f"| 标签命中总数 | {ov.get('tag_hits', 0)} |")
    if ov.get("experience_units") is not None:
        L.append(f"| 有一手使用经验的单元 | {ov.get('experience_units', 0)} |")
    L.append("")
    L.append("**购买状态分布**（互斥）\n")
    L.append("| 状态 | 单元数 | 占比 |")
    L.append("|---|---:|---:|")
    for it in result.get("purchase_status", {}).get("items", []):
        L.append(f"| {it['label_cn']} | {it['count']} | {_fmt_pct(it['percentage'])} |")
    unk = ov.get("purchase", {}).get("unknown", 0)
    if unk:
        L.append(f"| 状态不明 | {unk} | {_fmt_pct(unk / max(ov.get('units_analyzed',1),1) * 100)} |")
    L.append("")
    if kind == "ecommerce":
        L.append("> 说明：电商平台评论者本身即为购买者，因此「已购买人群」≈ 全部评论者；"
                 "未购买样本主要来自评论中「我还没买 / 只是看看」等表述。"
                 "针对电商数据，请重点参考后文的**评分分层人群差异**。\n")
    else:
        L.append("> 说明：论坛中的「发帖人」与「回帖老用户」常是两类人 —— 前者多处于选购阶段，"
                 "后者多为已购用户。因此本报告的购买状态按**发帖人立场**判定（选购者 / 已购用户），"
                 "评论中老用户的声音则贡献到各维度的画像与功能评价中。\n")

    # 二、人群画像
    L.append("## 二、人群画像\n")
    L.append("### 2.1 已购买人群画像\n")
    pp = result.get("purchaser_personas", [])
    if pp:
        L.append(_dim_table(pp, with_rate=False))
    else:
        L.append("_本次数据中未识别出明确的已购买人群画像信号。_")
    L.append("")
    L.append("### 2.2 未购买人群画像\n")
    np_ = result.get("non_purchaser_personas", [])
    if np_:
        L.append(_dim_table(np_, with_rate=False))
    else:
        L.append("_本次数据中未识别出明确的未购买人群画像信号。_")
    L.append("")

    gap = result.get("persona_gap", [])
    if gap:
        L.append("### 2.3 两类人群画像差异\n")
        L.append("正 gap 表示该画像在**已购人群**中更突出，负 gap 表示在**未购人群**中更突出。\n")
        L.append("| 画像标签 | 已购占比 | 未购占比 | 差异 |")
        L.append("|---|---:|---:|---:|")
        for g in gap[:10]:
            L.append(f"| {g['label_cn']} | {_fmt_pct(g['purchased_pct'])} | {_fmt_pct(g['not_purchased_pct'])} | {g['gap']:+.1f} |")
        L.append("")

    rgap = result.get("rating_gap", [])
    if kind == "ecommerce" and rgap:
        L.append("### 2.4 评分分层人群差异（好评 4-5★ vs 差评 1-2★）\n")
        L.append("正 gap 表示该画像在**好评**中更突出，负 gap 表示在**差评**中更突出。\n")
        L.append("| 画像标签 | 好评占比 | 差评占比 | 差异 |")
        L.append("|---|---:|---:|---:|")
        for g in rgap[:10]:
            L.append(f"| {g['label_cn']} | {_fmt_pct(g['high_pct'])} | {_fmt_pct(g['low_pct'])} | {g['gap']:+.1f} |")
        L.append("")

    # 三~六 普通维度
    order = [
        ("usage_scenarios", "三、使用场景"),
        ("pre_purchase_concerns", "四、购买前关注点"),
        ("post_purchase_experience", "五、购买后体验"),
    ]
    for key, title in order:
        d = dims.get(key, {})
        L.append(f"## {title}\n")
        items = d.get("items", [])
        if not items:
            L.append("_数据不足，无法支撑该维度分析。_")
            L.append("")
            continue
        L.append(_dim_table(items))
        L.append("")
        for it in items[:3]:
            if not it.get("evidence"):
                continue
            L.append(f"**{it['label_cn']}** — {it.get('reason','')}")
            L.append("")
            blk = _evidence_block(it["evidence"], unit_map, 2)
            if blk:
                L.append(blk)
                L.append("")
        L.append("")

    # 六、功能体验
    L.append("## 六、功能体验与满意度\n")
    fitems = feats.get("items", [])
    if not fitems:
        L.append("_数据不足，无法支撑功能体验分析。_")
        L.append("")
    else:
        L.append("满意度 = 正面 /（正面 + 负面），中性评价不计入分母。\n")
        L.append("| 功能维度 | 提及单元 | 正面 | 负面 | 中性 | 满意度 |")
        L.append("|---|---:|---:|---:|---:|---:|")
        for ft in sorted(fitems, key=lambda x: -x["total"]):
            L.append(f"| {ft['name']} | {ft['total']} | {ft['positive']} | {ft['negative']} | {ft['neutral']} | {_fmt_sat(ft['satisfaction'])} |")
        L.append("")
        for ft in sorted(fitems, key=lambda x: -(x["total"]))[:5]:
            if not ft.get("evidence"):
                continue
            L.append(f"### {ft['name']}（满意度 {_fmt_sat(ft['satisfaction'])}）")
            L.append("")
            blk = _evidence_block(ft["evidence"], unit_map, 2)
            if blk:
                L.append(blk)
            L.append("")

    # 七、交叉分析
    L.append("## 七、购买人群 × 功能满意度交叉分析\n")
    pm = feats.get("persona_feature_matrix", {})
    if not pm:
        L.append("_本次数据中有使用经验的人群样本不足以支撑交叉分析。_")
        L.append("")
    else:
        L.append("仅统计**有一手使用经验**的单元 —— 功能满意度须来自真实使用者，"
                 "选购期的预期不纳入计算。\n")
        low_pairs = []
        for persona, row in pm.items():
            for fname, cell in row.items():
                sat = cell.get("satisfaction")
                if sat is not None and cell["total"] >= 2:
                    low_pairs.append((sat, persona, fname, cell))
        low_pairs.sort(key=lambda x: x[0])
        if low_pairs:
            L.append("**满意度最低的组合**（样本数 ≥ 2）：\n")
            L.append("| 人群 | 功能 | 提及 | 满意度 |")
            L.append("|---|---|---:|---:|")
            for sat, persona, fname, cell in low_pairs[:8]:
                L.append(f"| {persona} | {fname} | {cell['total']} | {_fmt_sat(sat)} |")
            L.append("")

    # 八、品牌
    L.append("## 八、高频提及品牌\n")
    bitems = brands.get("items", [])
    if not bitems:
        L.append("_未识别出品牌提及（可在分析配置中补充品牌清单以提升识别率）。_")
        L.append("")
    else:
        L.append("| 品牌 | 提及单元 | 提及率 | 正面 | 负面 | 满意度 | 已购占比 |")
        L.append("|---|---:|---:|---:|---:|---:|---:|")
        for b in bitems[:15]:
            L.append(f"| {b['name']} | {b['count']} | {_fmt_pct(b['percentage'])} | "
                     f"{b['positive']} | {b['negative']} | {_fmt_sat(b['satisfaction'])} | "
                     f"{_fmt_pct(b['purchased_share'])} |")
        L.append("")

    # 九、未满足需求
    L.append("## 九、未被满足的需求\n")
    d = dims.get("unmet_needs", {})
    items = d.get("items", [])
    if not items:
        L.append("_本次数据中未识别出明确的未满足需求信号。_")
        L.append("")
    else:
        L.append(_dim_table(items))
        L.append("")
        for it in items[:4]:
            if not it.get("evidence"):
                continue
            L.append(f"**{it['label_cn']}** — {it.get('reason','')}")
            L.append("")
            blk = _evidence_block(it["evidence"], unit_map, 2)
            if blk:
                L.append(blk)
                L.append("")

    # 十、附录
    L.append("## 十、附录\n")
    L.append("**统计口径说明**\n")
    if kind == "ecommerce":
        L.append("- 1 条评论 = 1 个讨论单元；标题取评论文首句，其余为正文。")
        L.append("- 星级仅作为情感判定的先验，功能情感仍以评论文本为准。")
        L.append("- 平台自带标注（Annotations）作为功能维度的补充信号。")
    else:
        L.append("- 1 篇帖子 + 该帖前若干条评论 = 1 个讨论单元。")
        L.append("- 评论来源的证据会标注 💬；正文来源标注 📄。")
    L.append("- 标签占比为**多标签统计**，同一单元可命中多个标签，合计可超过 100%。")
    L.append("- 满意度公式为 正面/(正面+负面)，中性不计入分母。")
    L.append("")
    L.append("**局限性**\n")
    L.append("- 内置离线引擎基于词典与规则，覆盖高频表达；长尾、反讽、隐含语义可能漏判。")
    L.append("- 若需更高语义精度，可配置 LLM 连接器后切换到 LLM 标注后端。")
    L.append("- 采样非穷举；结论用于方向性判断，不替代定量市场研究。")
    L.append("")
    return "\n".join(L)
