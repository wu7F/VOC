# -*- coding: utf-8 -*-
"""分析管线编排：从原始文件到前端可直接消费的结果。

    读取文件 → 平台适配 → （分级/清洗）→ 逐单元标注 → 聚合 → 报告

每个阶段都会通过 `progress(pct, message)` 回调上报进度，前端据此显示进度条。
整个流程是**确定性**的（无随机性），同一份输入重复运行结果一致。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import adapters, aggregator, llm, llm_engine, offline_engine, report, taxonomy

PLATFORM_KIND = {"Amazon": "ecommerce", "Wayfair": "ecommerce", "Reddit": "forum"}


def _default_brands(platform: str) -> str:
    """内置的常用品牌清单（用户可在界面覆盖）。

    只收录**产品品牌**，不收录零售商（Wayfair / Amazon / Costco 等）——
    零售商名字出现在评论文中会污染品牌维度。
    用 `|` 合并同一品牌的多种拼写。
    """
    common = ("La-Z-Boy|Lazy Boy|La Z Boy|Lazboy, Monte|Monte Design, Dutailier|Dutalier, "
              "Chita|Chita Living, Il Tutto, Babyletto, Dream on Me, "
              "Pottery Barn|PB Kids|Pottery Barn Kids, Storkcraft, DaVinci, Namesake, "
              "Nurture&|Nurture and, Delta Children|Delta, IKEA, Ashley, "
              "Graco, Evenflo, Nuna, BabyBjörn|Baby Bjorn, Maxi-Cosi, "
              "Uppababy|UppaBaby, West Elm, Structube, Oeuf, Nursery Works, "
              "Babyletto, Soho Baby, Million Dollar Baby, Child Craft, "
              "Eames, Herman Miller, Article, Burrow, Joybird, Albany Park")
    if platform in ("Amazon", "Wayfair"):
        return common + ", Novogratz, Zinus, Yaheetech, Tangkula, Giantex, Homall, " \
                         "Flash Furniture, Christopher Knight, Andover Mills"
    return common


def _clean_date(s: str) -> str:
    return (s or "").strip()[:10]


def _diagnose_no_units(files_rows, platform: str) -> str:
    """当解析不出任何讨论单元时，给出可操作的诊断信息（而不是一句笼统的失败提示）。"""
    lines = ["未能从文件中解析出任何有效讨论单元。", ""]
    for _f, rows, src in files_rows:
        if not rows:
            lines.append(f"· {src}：文件为空或无数据行")
            continue
        cols = list(rows[0].keys())
        lines.append(f"· {src}：{len(rows)} 行，识别到列 {cols[:14]}"
                     + ("…" if len(cols) > 14 else ""))
        if platform == "Reddit":
            if adapters.has_type_column(rows):
                posts, comments = adapters._split_reddit_files(rows)
                lines.append(f"    含 type 列：帖子行 {len(posts)}、评论行 {len(comments)}")
                if not posts:
                    lines.append("    → 未找到 type=post 的行。若该文件只有评论，"
                                 "请同时上传帖子文件。")
            elif adapters.looks_like_comments(rows):
                lines.append("    → 判定为纯评论文件，但没有配套的帖子文件，"
                             "无法组成「帖子 + 评论」讨论单元。请一并上传帖子文件。")
            else:
                lines.append("    → 判定为帖子文件，但未能提取到标题/正文。"
                             "请确认存在 title / content / selftext / body 之类的内容列。")
        else:
            lines.append("    → 未能找到评论正文列（如 Review text / Review Body / Content）。"
                         "请确认至少有一列承载评论文本。")
    lines.append("")
    lines.append("平台可识别的列名：")
    if platform == "Reddit":
        lines.append("  reddit_id / id · title · content / selftext / body · subreddit · "
                     "author · published / date · comment_id · post_reddit_id · type")
    else:
        lines.append("  ASIN / SKU / Supplier part number · Product name · Rating / Star Rating · "
                     "Review text / Review Body / Content · Review date · Store name · Annotations")
    return "\n".join(lines)


def run_analysis(*, files: list[Path], platform: str, category: str,
                 brands: str = "", comment_limit: int = 20,
                 out_dir: Path, progress=None,
                 engine: str = "auto", threads: int = 4, batch_size: int = 4) -> dict:
    """执行完整分析。返回结果字典（同时写入 out_dir/result.json）。

    engine: "auto" = 配了 LLM 就用 LLM、失败自动回退离线；
            "llm"  = 强制 LLM（失败即报错，不静默降级）；
            "offline" = 强制内置离线引擎。
    """
    def p(pct: int, msg: str) -> None:
        if progress:
            progress(pct, msg)

    t0 = time.time()
    kind = PLATFORM_KIND.get(platform, "ecommerce")

    # ---- 1. 读取 ----
    p(5, "读取数据文件…")
    files_rows: list[tuple[Path, list[dict], str]] = []
    for f in files:
        rows, src = adapters.read_table(f)
        files_rows.append((f, rows, src))
    total_rows = sum(len(r) for _, r, _ in files_rows)
    if total_rows == 0:
        raise ValueError("数据文件中没有可用记录。")
    sources = [src for _, _, src in files_rows]

    # ---- 2. 适配 ----
    p(20, "识别字段与平台结构…")
    brand_map = adapters.merge_brand_aliases(
        adapters.parse_brand_list(brands) or adapters.parse_brand_list(_default_brands(platform))
    )
    if platform == "Reddit":
        # 支持「帖子 + 评论」分文件上传，或单文件混合（type=post/comment）
        units = adapters.adapt_reddit_multifile(
            [(src, rows) for _, rows, src in files_rows],
            comment_limit=comment_limit, brand_map=brand_map)
    else:
        all_rows = [r for _, rows, _ in files_rows for r in rows]
        units = adapters.adapt_auto(all_rows, platform, brand_map=brand_map,
                                   comment_limit=comment_limit, category=category)
    if not units:
        raise ValueError(_diagnose_no_units(files_rows, platform))

    if kind == "ecommerce":
        for u in units:
            u["meta"]["default_purchase"] = "purchased"

    # 时间窗口清洗（两年）—— 有日期时生效
    dated = [u for u in units if (u.get("meta") or {}).get("date")]
    if dated and len(dated) >= max(5, len(units) * 0.3):
        cutoff = datetime.now() - timedelta(days=365 * 2)
        kept = []
        for u in units:
            d = _clean_date((u.get("meta") or {}).get("date"))
            if not d:
                kept.append(u)
                continue
            try:
                if datetime.strptime(d, "%Y-%m-%d") >= cutoff:
                    kept.append(u)
            except ValueError:
                kept.append(u)
        if len(kept) >= 5:
            units = kept

    p(32, f"归一化完成：{len(units)} 个讨论单元"
          + (f"（含 {sum(len(u['comments']) for u in units)} 条评论）"
             if kind == "forum" else ""))

    # ---- 3. 标注（选定引擎） ----
    engine_name = "offline-rule-engine"
    tagset = offline_engine.design_tagset(category)
    warnings: list[str] = []
    records: list[dict] = []
    tag_index = taxonomy.index_from_taxonomy()

    want_llm = engine in ("llm", "auto")
    if want_llm and not llm.is_configured():
        if engine == "llm":
            raise ValueError(
                "选择了 LLM 引擎，但未配置：请在 llm_config.json 或环境变量 "
                "LLM_CONNECTOR_BASE_URL / API_KEY / MODEL 中填写。")
        warnings.append("未配置 LLM，已使用内置离线引擎。")
        want_llm = False

    if want_llm:
        # 先用（带缓存的）健康检查挡一道：后端不可用时直接走内置引擎，
        # 不把每个单元的请求都浪费在一次注定失败的调用上
        h = llm.health()
        if not h.get("ok"):
            reason = h.get("message", "")
            if engine == "llm":
                raise ValueError(
                    f"LLM 不可用：{reason}\n\n"
                    "当前选择的是「强制 LLM」模式，因此不会自动降级。\n"
                    "· 改用「自动」模式即可在后端不可用时回退到内置引擎；\n"
                    "· 或先修复 LLM 配置（llm_config.json / LLM_CONNECTOR_*）后重试。")
            warnings.append(f"LLM 不可用，已使用内置离线引擎。原因：{reason}")
            p(40, warnings[-1])
            want_llm = False

    if want_llm:
        try:
            p(36, "LLM：正在设计品类专属标签体系…")
            tagset = llm_engine.design_tagset(units, category=category, platform=platform)
            (out_dir).mkdir(parents=True, exist_ok=True)
            (out_dir / "tags.json").write_text(
                json.dumps(tagset, ensure_ascii=False, indent=2), encoding="utf-8")
            n_tags = sum(len(d.get("tags") or {}) for d in tagset["dimensions"].values())
            p(42, f"LLM：标签体系就绪（{len(tagset['dimensions'])} 维度 / {n_tags} 标签），开始标注…")

            def _llm_progress(msg: str):
                m = re.search(r"(\d+)/(\d+)", msg)
                if m:
                    frac = int(m.group(1)) / max(1, int(m.group(2)))
                    p(42 + int(40 * frac), msg)
                else:
                    p(50, msg)

            records, llm_stats = llm_engine.annotate_units(
                units, tagset, batch_size=batch_size, threads=threads,
                progress=_llm_progress)
            if not records:
                raise llm.LLMError("LLM 未产出任何标注记录")
            if llm_stats.get("dropped_quote") or llm_stats.get("missing_unit"):
                warnings.append(
                    f"LLM 标注中丢弃了 {llm_stats.get('dropped_quote', 0)} 条无法在原文定位的引用"
                    f"（已按证据逐字校验规则剔除），{llm_stats.get('missing_unit', 0)} 个单元未返回结果。")
            engine_name = f"llm:{llm.load_config().get('model', '')}"
            tag_index = taxonomy.index_from_tagset(tagset)
            p(82, "LLM 标注完成")
        except llm.LLMError as e:
            if engine == "llm":
                raise ValueError(f"LLM 分析失败：{e}") from e
            warnings.append(f"LLM 不可用，已自动回退到内置离线引擎。原因：{e}")
            p(40, warnings[-1])
            records = []
            tag_index = taxonomy.index_from_taxonomy()

    if not records:
        n = len(units)
        step = max(1, n // 40)
        records = []
        for i, u in enumerate(units):
            records.append(offline_engine.annotate_unit(u))
            if i % step == 0:
                p(45 + int(35 * i / n), f"内置引擎标注 {i + 1}/{n}…")
        engine_name = "offline-rule-engine"
        if tagset.get("engine") != "offline-rule-engine":
            tagset = offline_engine.design_tagset(category)
        (out_dir).mkdir(parents=True, exist_ok=True)
        (out_dir / "tags.json").write_text(
            json.dumps(tagset, ensure_ascii=False, indent=2), encoding="utf-8")
    p(86, "标注完成，正在聚合维度…")

    # ---- 4. 聚合 ----
    result = aggregator.aggregate(units, records, category=category,
                                 platform=platform, engine=engine_name,
                                 brands=list(brand_map.keys()),
                                 tag_index=tag_index)
    result["sources"] = sources
    result["warnings"] = warnings
    result["params"] = {
        "comment_limit": comment_limit,
        "brands_input": brands,
        "brand_list": list(brand_map.keys()),
        "time_window_years": 2,
        "engine_requested": engine,
        "llm_config": llm.public_config(),
    }
    p(90, "生成洞察报告…")

    # ---- 5. 报告 ----
    md = report.build_report(result, dataset_name=", ".join(sources))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(md, encoding="utf-8")
    # 原始单元留档，便于复核
    (out_dir / "units.jsonl").write_text(
        "\n".join(json.dumps(u, ensure_ascii=False) for u in units), encoding="utf-8")

    result["elapsed_sec"] = round(time.time() - t0, 2)
    (out_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    p(100, f"分析完成，用时 {result['elapsed_sec']:.1f}s")
    return result
