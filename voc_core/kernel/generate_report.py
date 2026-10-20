#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 5 洞察分析：将聚合产物（dimensions.json / features.json / stats.json）转为 Markdown 洞察报告。

严格遵循 references/report_template.md 的结构与硬性要求：
- 仅从已采集/导入并清洗后的数据生成，不得用网络搜索或编造数据替代。
- 每个维度引用 2-3 条代表性原声并附原文链接。
- 用户可见文案（来源名、计数单元名、渠道名）由 ``--profile`` 注入，
  空 profile 等价 Reddit（与改造前逐字一致）。
- 各维度（人群画像按购买状态分边 / 使用场景 / 购买前关注 / 购买后体验 / 功能体验 / 品牌 / 未满足需求）全覆盖，
  并新增「功能体验与满意度」一节（来自 features.json）。
"""
import argparse
import collections
import csv
import html
import json
import re
import os
import sys
from datetime import datetime

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

#: 当前源画像。由 main() 设置；未设置时按 Reddit（= 改造前行为）处理。
_P = None


def use_profile(profile) -> None:
    """设置本次运行使用的源画像。"""
    global _P
    _P = profile


# --------------------------------------------------------------------------
# 分析框架（Stage 5）：报告里凡涉及「分段轴」的句子都由框架配置渲染
# --------------------------------------------------------------------------
#: 当前分析框架。main() 从 dimensions.json 的 analysis_frame 块读入；老产物没有
#: 这个块时回落内置 consumer（渲染结果与改造前的硬编码逐字相同）。
_F: AnalysisFrame | None = None


def use_frame(f) -> None:
    global _F
    _F = f


def current_frame() -> AnalysisFrame:
    global _F
    if _F is None:
        _F = load_frame(None)
    return _F


def axis_ctx(**extra) -> dict:
    """渲染轴文案时的公共变量（各句自己再补 unit_char / n / share / note）。"""
    UN, SEC = T("unit_noun"), TO("secondary_noun")
    ctx = {
        "unit": UN,
        "secondary": SEC,
        "secondary_speaker": T("secondary_speaker"),
        "unit_char": T("unit_char"),
        "unit_pair": f"{UN} / {SEC}" if SEC else UN,
        "merged": f"{UN} + {SEC}" if SEC else UN,
        "scope": f"{UN}单元 + {SEC}单元合并计数" if SEC else f"以{UN}单元计数",
        "n": 0, "share": "", "note": "",
    }
    ctx.update(extra)
    return ctx


def axis_text(key: str, **extra) -> str:
    """渲染一条轴文案；框架里没有这条（或框架没有分段轴）时返回空串。

    返回空串时调用方回落到改造前的硬编码句子 —— 老产物（没有 analysis_frame 块）
    因此仍能出报告，且文案与改造前逐字相同。
    """
    fr = current_frame()
    if fr.axis is None or not fr.axis.texts.get(key):
        return ""
    return fr.axis.render_text(key, **axis_ctx(**extra))


def P():
    global _P
    if _P is None:
        _P = load_profile(None)
    return _P


def T(key: str, default: str = "") -> str:
    """取术语（用户可见文案统一入口）。"""
    return P().term(key, default)


def TO(key: str) -> str:
    """取**可空**术语（空=该源无此概念，不回落 Reddit）。"""
    return P().opt(key)


STOP = set(
    "the a an and or to of in on for is are was were be been it its with my our we you they i this that as at by from has have had not but if so he she them his her your their can will do does did what when which who how why out up off about into over after before than then there here just get got use using used me also more most some any all would could should may might need want looking best vs recommend will have per are cat cats dog dogs pet pets food feeder feeders auto automatic smart".split()
)


def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_relevant(path):
    # ⚠️ 必须 utf-8-sig：analyzer/ingest 写出的 CSV 带 BOM，用 utf-8 读会让 id 列名变成
    #    '\ufeffreddit_id'，id2url 全空 → 报告里所有来源链接与证据链接静默消失。
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    # 列名归一化：任意源的 unit_id / channel / text 会被补成内核认识的旧列名
    rows = canonicalize_rows(rows, "posts")
    id2url = {r.get("reddit_id", ""): r.get("url", "") for r in rows}
    return rows, id2url


def load_comment_index(path):
    """comment_id -> {post_reddit_id}：让评论证据也能给出可点击的原帖链接。"""
    idx = {}
    if not path or not os.path.exists(path):
        return idx
    for r in canonicalize_rows(csv.DictReader(open(path, encoding="utf-8")), "comments"):
        cid = (r.get("comment_id") or "").strip()
        if cid:
            idx[cid] = {"post_reddit_id": (r.get("post_reddit_id") or "").strip()}
    return idx


def unescape(s):
    return html.unescape(s) if isinstance(s, str) else s


def fmt_pct(p):
    """把 dimensions.json 的 percentage 渲染成百分比。

    ⚠️ 该字段契约就是 0–100（见 SKILL.md「Dimensions JSON 结构约定」），
    不要再对 <=1 的值二次 ×100——那会把 0.8% 显示成 80.0%。
    """
    if p is None:
        return "-"
    try:
        v = float(p)
    except Exception:
        return str(p)
    return f"{v:.1f}%"


def fmt_sent(sd):
    if not sd:
        return "-"
    return f"正面 {sd.get('positive', 0)} / 负面 {sd.get('negative', 0)} / 中性 {sd.get('neutral', 0)}"


def render_evidence(item, id2url, n=3, comment_index=None):
    eps = item.get("evidence_posts", []) or []
    lines = []
    for e in eps[:n]:
        rid = e.get("reddit_id", "")
        q = unescape(e.get("evidence_quote", "") or "").strip()
        if not q:
            continue
        if len(q) > 280:
            q = q[:280] + "…"
        url = id2url.get(rid, "")
        label = rid
        if not url and comment_index and rid in comment_index:
            # 评论证据：链接到所属帖子的评论锚点，便于核对原文
            purl = id2url.get(comment_index[rid].get("post_reddit_id", ""), "")
            if purl:
                url = purl.rstrip("/") + f"/_/{rid}/"
                label = f"评论 {rid}"
        link = f"[{label}]({url})" if url else label
        lines.append(f"> {q}\n> \u2014 来源：{link}")
    return "\n\n".join(lines)


def dim_table(items, with_sentiment=False, unit=None, with_split=False):
    """渲染维度表。

    ``with_split=True`` 时额外给出「主计数单元 / 次级计数单元」拆分列（人群画像
    用合并计数）。**单层源（电商评论）没有次级单元，拆分列自动省略** —— 否则
    会出现一列恒为 0 的「评论」，让读者误以为还有一层数据没统计。
    """
    unit = unit if unit is not None else T("unit_char")
    sec = TO("secondary_noun")
    split = bool(with_split and sec)
    head = f"| 标签 | 提及{unit}数 |"
    sep = "|---|---:|"
    if split:
        head += f" {T('unit_noun')} | {sec} |"
        sep += "---:|---:|"
    head += " 占比 |"
    sep += "---:|"
    if with_sentiment:
        head += " 情感倾向 |"
        sep += "---|"
    out = [head, sep]
    for it in sorted(items, key=lambda x: -x.get("count", 0)):
        row = f"| {it.get('label_cn','')} | {it.get('count',0)} |"
        if split:
            row += f" {it.get('post_count', 0)} | {it.get('comment_count', 0)} |"
        row += f" {fmt_pct(it.get('percentage'))} |"
        if with_sentiment:
            sd = it.get("sentiment_distribution")
            row += f" {fmt_sent(sd) if sd else it.get('sentiment','-')} |"
        out.append(row)
    return "\n".join(out)


def dim_block(title, items, id2url, with_sentiment=False, topn=6, unit=None, comment_index=None,
              with_split=False):
    unit = unit if unit is not None else T("unit_char")
    items = sorted(items or [], key=lambda x: -x.get("count", 0))
    if not items:
        return f"### {title}\n\n_数据不足，该维度无有效标签。_\n"
    out = [f"### {title}", "", dim_table(items, with_sentiment, unit, with_split), ""]
    for it in items[:topn]:
        cnt = it.get("count", 0)
        out.append(f"#### {it.get('label_cn','')}（{cnt} {unit}，{fmt_pct(it.get('percentage'))}）")
        if it.get("reason"):
            out.append(f"\n**归类依据**：{it['reason']}")
        if it.get("description") and it.get("description") != it.get("reason"):
            out.append(f"\n{it['description']}")
        if with_sentiment and it.get("sentiment"):
            out.append(f"\n**情感**：{it['sentiment']}")
        ev = render_evidence(it, id2url, comment_index=comment_index)
        if ev:
            out.append("\n**代表性原声**：\n")
            out.append(ev)
        out.append("")
    return "\n".join(out)


def feature_block(features, id2url):
    if not features:
        return "_数据不足，无法支撑功能体验分析。_"
    out = []
    out.append(f"| 功能维度 | 提及{T('unit_char')}数 | 正面 | 负面 | 中性 | 满意度 |")
    out.append("|---|---:|---:|---:|---:|---:|")
    for ft in sorted(features, key=lambda x: -x.get("total", 0)):
        sat = ft.get("satisfaction")
        sat_s = f"{sat:.2f}" if isinstance(sat, (int, float)) else str(sat)
        out.append(
            f"| {ft.get('name','')} | {ft.get('total',0)} | {ft.get('positive',0)} | {ft.get('negative',0)} | {ft.get('neutral',0)} | {sat_s} |"
        )
    out.append("")
    for ft in sorted(features, key=lambda x: -x.get("total", 0)):
        sat = ft.get("satisfaction")
        # satisfaction 契约是 0–1 浮点；直接插值会打印成 0.24475524475524477
        sat_s = f"{sat * 100:.0f}%" if isinstance(sat, (int, float)) else str(sat)
        out.append(f"### {ft.get('name','')}（满意度 {sat_s}）")
        if ft.get("reason"):
            out.append(f"\n{ft['reason']}")
        ev = render_evidence(ft, id2url)
        if ev:
            out.append("\n**代表性原声**：\n")
            out.append(ev)
        out.append("")
    return "\n".join(out)


def persona_feature_block(persona_matrix):
    """画像 × 功能矩阵（2026-10-06 新增）。空矩阵返回空串，报告里不出现空节。"""
    if not persona_matrix:
        return ""
    # 列取全部出现过的功能，按总提及量降序；行按行内总提及量降序
    feat_totals = {}
    for fmap in persona_matrix.values():
        for fk, cell in fmap.items():
            feat_totals[fk] = feat_totals.get(fk, 0) + (cell.get("total", 0) or 0)
    feats = [fk for fk, _ in sorted(feat_totals.items(), key=lambda x: -x[1])]
    out = []
    # 尾注按「有没有次级单元」选版（单层源不能出现「评论单元」），再拼进标题。
    # 两句都来自框架配置 —— 连「评论单元无功能标注不进矩阵」都是轴的口径。
    _second = TO("secondary_noun")
    _note = axis_text("matrix_note" if _second else "matrix_note_solo")
    _title = axis_text("report_matrix_title", note=_note)
    if not _title:
        _fb = (f"；{_second}单元无功能标注不进矩阵") if _second else ""
        _title = (f"**已购人群画像 × 功能体验矩阵**（单元格 = {T('unit_char')}粒度交集计数 / 满意度；"
                  f"画像轴 = **仅已购人群**画像标签——未购人群与购买状态不明人群均不入矩阵{_fb}）：")
    out.append(_title + "\n")
    out.append("| 用户画像 | " + " | ".join(feats) + " |")
    out.append("|---|" + "---:|" * len(feats))
    for persona, fmap in sorted(persona_matrix.items(),
                                key=lambda kv: -sum(c.get("total", 0) for c in kv[1].values())):
        cells = []
        for fk in feats:
            cell = fmap.get(fk, {})
            if not cell or not cell.get("total"):
                cells.append("—")
            else:
                sat = cell.get("satisfaction")
                sat_s = f"{sat * 100:.0f}%" if isinstance(sat, (int, float)) else "-"
                cells.append(f"{cell.get('total',0)}/{sat_s}")
        out.append(f"| {persona} | " + " | ".join(cells) + " |")
    out.append("")
    return "\n".join(out)


def brand_block(brand_mentions, matrix, id2url):
    if not brand_mentions:
        return "_数据不足，无品牌提及。_"
    out = []
    out.append(f"| 品牌/产品 | 提及{T('unit_char')}数 | 占比 | 情感分布（正/负/中） |")
    out.append("|---|---:|---:|---|")
    for b in sorted(brand_mentions, key=lambda x: -x.get("count", 0)):
        sd = b.get("sentiment_distribution", {})
        out.append(
            f"| {b.get('label_cn','')} | {b.get('count',0)} | {fmt_pct(b.get('percentage'))} | 正 {sd.get('positive',0)} / 负 {sd.get('negative',0)} / 中 {sd.get('neutral',0)} |"
        )
    out.append("")
    if matrix:
        out.append(f"**品牌 × 功能体验矩阵**（单元格 = 提及{T('unit_char')}数 / 主导情感缩写）：\n")
        feats = list(next(iter(matrix.values())).keys()) if matrix else []
        out.append("| 品牌 | " + " | ".join(feats) + " |")
        out.append("|---|" + "---:|" * len(feats))
        for brand, fmap in matrix.items():
            cells = []
            for fk in feats:
                cell = fmap.get(fk, {})
                if not cell or not cell.get("total"):
                    cells.append("\u2014")
                else:
                    sat = cell.get("satisfaction")
                    sat_s = f"{sat:.2f}" if isinstance(sat, (int, float)) else "-"
                    cells.append(f"{cell.get('total',0)}/{sat_s}")
            out.append(f"| {brand} | " + " | ".join(cells) + " |")
        out.append("")
    for b in sorted(brand_mentions, key=lambda x: -x.get("count", 0))[:5]:
        out.append(f"### {b.get('label_cn','')}（{b.get('count',0)} {T('unit_quant')}）")
        ev = render_evidence(b, id2url)
        if ev:
            out.append("\n**代表性原声**：\n")
            out.append(ev)
        out.append("")
    return "\n".join(out)


def appendix(rows, id2url, stats, args):
    out = []
    sub = collections.Counter(r.get("subreddit", "") for r in rows)
    out.append(f"**高频{T('channel_noun')}**：")
    out.append("、".join(f"{TO('channel_prefix')}{k} ({v})" for k, v in sub.most_common(8)))
    out.append("")
    c = collections.Counter()
    for r in rows:
        t = (r.get("title", "") + " " + (r.get("summary_text", "") or "")).lower()
        for w in re.findall(r"[a-z][a-z\-]+", t):
            if w not in STOP and len(w) > 3:
                c[w] += 1
    out.append("**高频关键词**：")
    out.append("、".join(f"{w}({n})" for w, n in c.most_common(20)))
    out.append("")
    out.append("**数据文件路径**：")
    out.append(f"- 原始{T('unit_noun')}：`{stats.get('input_path','')}`")
    out.append(f"- 清洗后相关{T('unit_noun')}：`{stats.get('relevant_path','')}`")
    out.append(f"- 标签集：`{stats.get('tagset_path', 'tags/tagset.json')}`")
    out.append(f"- 标注文件：`{getattr(args, 'run_dir', 'annot/runs/')}/`")
    out.append(f"- 维度 JSON：`{args.dimensions}`")
    out.append(f"- 功能 JSON：`{args.features_json}`")
    out.append("")
    out.append(f"**{T('link_section')}**（前 10 条）：")
    for r in rows[:10]:
        rid = r.get("reddit_id", "")
        url = r.get("url", "")
        if url:
            out.append(f"- [{rid}]({url}) \u2014 {r.get('title','')[:60]}")
    return "\n".join(out)


def core_findings(dim):
    def top(key, n=1, prefer=None, unit=None):
        unit = unit if unit is not None else T("unit_char")
        lst = dim.get(key, [])
        if not lst:
            return ""
        pool = lst
        if prefer:
            # 「痛点」类结论必须取负面情感的标签，否则会把正面体验误标成痛点
            picked = [i for i in lst if i.get("sentiment") == prefer]
            if picked:
                pool = picked
        it = sorted(pool, key=lambda x: -x.get("count", 0))[0]
        return f"{it.get('label_cn','')}（{it.get('count',0)} {unit}）"

    # 画像两条结论由框架的桶列表驱动（换框架就不再是「购买人群 / 未购人群」）。
    # 句子前缀取自桶配置 dashboard.core_finding；桶没配就不出这条（而不是留下空头）。
    _fr = current_frame()
    _persona_bullets = []
    if _fr.axis is not None:
        for _did, _bid in _fr.persona_dims().items():
            _b = _fr.axis.bucket(_bid)
            _name = ((_b.dashboard.get("core_finding") if _b else "")
                     or (_b.label_cn if _b else ""))
            if _name:
                _persona_bullets.append(f"{_name}：{top(_did, unit='单元')}")

    bullets = _persona_bullets + [
        f"主使用场景：{top('usage_scenarios')}",
        f"购前首要关注：{top('pre_purchase_concerns')}",
        f"购后最大痛点：{top('post_purchase_experience', prefer='negative')}",
        f"最强未满足需求：{top('unmet_needs')}",
    ]
    bm = dim.get("brand_mentions", [])
    if bm:
        b = sorted(bm, key=lambda x: -x.get("count", 0))[0]
        bullets.append(f"最高频品牌：{b.get('label_cn','')}（{b.get('count',0)} {T('unit_quant')}）")
    # 某维度无数据时 top() 返回空串，会渲染成「主使用场景：」这种空头结论，直接丢弃
    bullets = [b for b in bullets if not b.endswith("：")]
    return "\n".join(f"- {b}" for b in bullets)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dimensions", required=True)
    ap.add_argument("--features-json", required=True)
    ap.add_argument("--stats", required=True)
    ap.add_argument("--relevant", required=True)
    ap.add_argument("--category", default="")
    ap.add_argument("--comments", default="", help="评论 CSV（可选）：让评论证据也能生成可点击的原帖链接")
    ap.add_argument("--run-dir", default="annot/runs/hy3", help="标注中间文件目录（仅写进附录，便于回查）")
    ap.add_argument("--out", required=True)
    add_profile_argument(ap)
    add_frame_argument(ap)
    args = ap.parse_args()
    use_profile(load_profile(args.profile))

    dim = load_json(args.dimensions)
    # 框架从**上游产物**读（aggregate 已把 analysis_frame 写进 dimensions.json）；
    # 老产物没有这个块时回落到 --frame / 内置 consumer。
    use_frame(load_frame_from_product(dim, default=(args.frame or "")))
    _fr = current_frame()
    _axis = _fr.axis
    _dist_key = (_axis.distribution_key if _axis else "") or "purchase_status_distribution"
    feat = load_json(args.features_json)
    stats = load_json(args.stats)
    rows, id2url = load_relevant(args.relevant)
    comment_index = load_comment_index(args.comments)
    cat = args.category or dim.get("category", "")

    UN = T("unit_noun")      # 主计数单元名：帖子 / 评论
    SEC = TO("secondary_noun")  # 次级单元名：评论 / 空（电商单层）

    L = []
    L.append(f"# {P().label} 用户洞察分析报告\n")
    L.append(f"**品类**：{cat}  ")
    L.append(f"**生成时间**：{datetime.now().strftime('%Y-%m-%d %H:%M')}  ")
    unit_counts = dim.get("unit_counts") or {}
    # 单层源没有次级单元，合并计数的说明整句不出现
    src_note = (f"，以及这些{UN}下的{SEC}（人群画像维度与{UN}单元合并计数）"
                if (unit_counts.get("comment_units") and SEC) else "")
    # 上传模式（Phase 2'）必须如实标注数据来源，不能谎称是本次抓取：
    # 优先用 analyzer 落到 stats 的 source_note，其次用 dimensions 里的 source_note。
    user_note = (stats.get("source_note") or unit_counts.get("source_note") or "").strip()
    if user_note:
        L.append(f"**数据来源**：{user_note}；清洗后分析样本为 A+B 级{UN}"
                 f"{stats.get('final_analysis', '-')} 篇{src_note}。\n")
    else:
        L.append(f"**数据来源**：{T('data_source_phrase')}（经采集与清洗后的 A+B 级{UN}{src_note}）\n")

    L.append("## 一、数据概览\n")
    L.append("| 数据阶段 | 数量 | 说明 |")
    L.append("|---|---:|---|")
    L.append(f"| 采集总量 | {stats.get('raw_total','-')} | {T('raw_total_note')} |")
    L.append(f"| 两年以上旧数据（已清洗） | {stats.get('expired_count','-')} | 发布时间超过 {stats.get('max_age_years',2)} 年的{UN}（已过滤） |")
    L.append(f"| 日期清洗后剩余 | {stats.get('after_date_clean','-')} | 清洗旧数据后的{UN}数 |")
    L.append(f"| 噪声/低相关 (D) | {stats.get('bucket_D','-')} | 不用于分析 |")
    L.append(f"| 推广/弱参考 (C) | {stats.get('bucket_C','-')} | 默认不用于核心结论 |")
    L.append(f"| 强相关 VOC (A) | {stats.get('bucket_A','-')} | 优先分析 |")
    L.append(f"| 可参考 (B) | {stats.get('bucket_B','-')} | 辅助分析 |")
    L.append(f"| **最终分析数据量 (A+B)** | **{stats.get('final_analysis','-')}** | **各维度分析的核心样本** |")
    uc = unit_counts
    if SEC and (uc.get("comment_units") or stats.get("comments_total")):
        L.append(f"| {SEC}单元 | {uc.get('comment_units', stats.get('comments_total','-'))} | "
                 f"人群画像维度与{UN}单元**合并计数**（其余维度仍以{UN}为单位） |")
    L.append("")
    L.append("**数据清洗说明**：过滤两年以上旧数据、广告/推广过滤、相关性分级（A 强相关 / B 可参考 / C 推广弱参考 / D 噪声低相关）。")
    L.append("")
    _merged = f"{UN} + {SEC}" if SEC else UN
    # 「已排除的单元」那句是**轴的口径**（读不出的到底是「是否购买」还是「决策角色」），
    # 所以归框架配置；框架缺这条时回落到改造前的硬编码。
    _limit = axis_text("report_data_limit") or (
        "购买状态判为「无法判断」的单元（纯附和短评、泛泛围观、第三人称转述等）"
        "已从人群画像中排除、不在两桶之内——其数量在「二、人群画像」节披露，"
        "读画像结论时应知道这部分语料未被覆盖。")
    L.append(f"**数据局限性**：采样非穷举；除人群画像（{_merged}合并计数）外，其余维度仍以{UN}级为单位；"
             "同一单元可命中多个标签，各标签占比之和可超过 100%；"
             f"{_limit}")
    L.append("")
    L.append("### 核心发现\n")
    L.append(core_findings(dim))
    L.append("")

    L.append("## 二、人群画像\n")
    _intro = axis_text("report_persona_intro" if SEC else "report_persona_intro_solo")
    if not _intro:
        _intro = (
            f"本节不再区分「发帖人 / {T('secondary_speaker')}」，而是**按购买状态**分两个画像——购买者 / 未购买者，"
            f"且**{UN}单元与{SEC}单元合并计数**——因为{SEC}者往往本身就是该品类的使用者或潜在买家，"
            "拆成两套会人为割裂同一批人；购买状态无法判定的单元不计入任何画像桶（数量见下方披露）。"
        ) if SEC else (
            "本节**按购买状态**分两个画像——购买者 / 未购买者；"
            "购买状态无法判定的单元不计入任何画像桶（数量见下方披露）。")
    L.append(_intro)
    L.append("")

    uc = dim.get("unit_counts") or {}
    if uc:
        if SEC:
            L.append(f"**计数单元**：{UN} **{uc.get('post_units', '-')}** 个 + {SEC} "
                     f"**{uc.get('comment_units', '-')}** 条。")
        else:
            L.append(f"**计数单元**：{UN} **{uc.get('post_units', uc.get('comment_units', '-'))}** 个。")
        if uc.get("persona_scope_rule"):
            L.append(f"**统计口径**：{uc['persona_scope_rule']}")
        _unk = uc.get("excluded_unknown_units") or 0
        if _unk:
            _tot = (uc.get("post_units") or 0) + (uc.get("comment_units") or 0)
            _share = f"（占全部单元的 {_unk / _tot * 100:.1f}%）" if _tot else ""
            _line = axis_text("report_excluded_line", n=_unk, share=_share)
            if not _line:
                _line = (f"**已排除单元**：另有 **{_unk}** 个单元{_share}无法判定购买状态"
                         f"（纯附和短评、泛泛围观、第三人称转述等），未计入下表任何画像桶。")
            L.append(_line)
        L.append("")

    psd = dim.get(_dist_key, [])
    _dist_title = axis_text("report_distribution_title") or "购买状态分布（归桶依据）"
    _dist_col = axis_text("report_distribution_col") or "购买状态"
    if psd:
        L.append(f"### {_dist_title}\n")
        if SEC:  # 单层源没有拆分列，避免出现一列恒为 0 的「评论」
            L.append(f"| {_dist_col} | 单元数 | {UN} | {SEC} | 占比 |")
            L.append("|---|---:|---:|---:|---:|")
            for s in psd:
                L.append(f"| {s.get('label_cn','')} | {s.get('count',0)} | {s.get('post_count',0)} | "
                         f"{s.get('comment_count',0)} | {fmt_pct(s.get('percentage'))} |")
        else:
            L.append(f"| {_dist_col} | 单元数 | 占比 |")
            L.append("|---|---:|---:|")
            for s in psd:
                L.append(f"| {s.get('label_cn','')} | {s.get('count',0)} | "
                         f"{fmt_pct(s.get('percentage'))} |")
        L.append("")
        # 注意：本表只含已购 / 未购两段——「无法判断」的单元不进画像桶，故不进本表；
        # 其数量已在上方「已排除单元」一行披露。

    # 画像小节由框架的桶列表驱动：几个桶就几节（消费品两个购买状态、B2B 三个决策角色）。
    # 空维度不用特判 —— dim_block 自己会输出「_数据不足，该维度无有效标签。_」。
    for _did, _bid in _fr.persona_dims().items():
        _b = _axis.bucket(_bid) if _axis else None
        _label = ((_b.persona_label if _b else "") or (_b.label_cn if _b else "")
                  or _fr.labels().get(_did, _did))
        L.append(dim_block(_label, dim.get(_did, []), id2url,
                           unit="单元", topn=8, with_split=True, comment_index=comment_index))

    L.append("## 三、使用场景\n")
    L.append(dim_block("典型使用场景", dim.get("usage_scenarios", []), id2url))

    L.append("## 四、购买前关注点与顾虑\n")
    L.append(dim_block("购买前关注点", dim.get("pre_purchase_concerns", []), id2url))

    L.append("## 五、购买后体验及吐槽\n")
    L.append(dim_block("购买后体验", dim.get("post_purchase_experience", []), id2url, with_sentiment=True))

    L.append("## 六、功能体验与满意度\n")
    L.append(feature_block(feat.get("features", []), id2url))
    persona_matrix = feat.get("persona_feature_matrix", {})
    if persona_matrix:
        L.append("### 画像 × 功能交叉分析\n")
        L.append(persona_feature_block(persona_matrix))

    L.append("## 七、高频提及品牌\n")
    L.append(brand_block(dim.get("brand_mentions", []), feat.get("brand_feature_matrix", {}), id2url))

    L.append("## 八、未被满足的需求\n")
    L.append(dim_block("未被满足的需求", dim.get("unmet_needs", []), id2url, with_sentiment=True))

    L.append("## 九、附录\n")
    L.append(appendix(rows, id2url, stats, args))

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(f"[generate_report] 已生成洞察报告: {args.out}  ({len(L)} 段落)")


if __name__ == "__main__":
    main()
