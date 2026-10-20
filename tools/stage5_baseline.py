#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 5（维度框架配置化）的零回归基线工具。

用 mock 后端把整条链路跑一遍，把每个产物**归一化掉时间戳之后**算 sha1，
冻结成 ``baseline.json``。改造之后重跑同一个脚本并 ``--compare``，
就能一眼看出「哪一步的产物变了一个字节」。

为什么必须归一化时间戳：
    报告与看板里都写「生成时间」，两次运行必然差几秒。不归一化的话
    每次比对都是红的，等于没有基线。（Stage 6 的洞察报告回归测试
    踩过同一个坑，那里是逐行白名单，这里直接做字符串替换。）

为什么用 ``--mock``：
    它是确定性的（内核规则 / 内置示例标签集），不消耗 LLM 额度，
    也不会因为模型版本变化让基线失真。Stage 5 要证明的是
    **「换个框架，代码行为按预期变；不换框架，一个字节都不变」**，
    这跟 LLM 的内容质量无关。

用法::

    python tools/stage5_baseline.py --tag baseline
    python tools/stage5_baseline.py --tag after --compare <baseline.json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
DATA = PLATFORM.parent / "data" / "reddit_analysis"

#: 时间戳归一化：``2026-10-07 13:30`` / ``2026-10-07T13:30:12`` / ``…:12.914857`` → ``<TS>``
_TS = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?")

#: 本次运行的产物根目录（绝对路径）。产物里会回显它，两次跑 tag 不同 → 必须抹掉。
_OUT_ROOT: Path | None = None

CATEGORY = "Nursery Glider Recliner"


def norm(text: str) -> str:
    """抹平时间戳与本次产物路径（行尾统一，避免 CRLF/LF 差异）。

    这两样是**回显出来的运行环境**，不是分析结论。不抹掉的话，
    同一个 tag 跑两次的哈希也永远不同（Stage 6 的洞察报告回归测试
    踩过同一个坑，那里用逐行白名单，这里直接做字符串替换）。
    """
    s = text.replace("\r\n", "\n")
    if _OUT_ROOT is not None:
        # 三种写法都要覆盖：原样（C:\a\b）、正斜杠（C:/a/b）、
        # **JSON 里的转义形态**（C:\\a\\b）—— 最后一种最容易漏，
        # 漏了就是「同一个 tag 跑两次 quality_report.json 也不相等」。
        for form in (str(_OUT_ROOT),
                     str(_OUT_ROOT).replace("\\", "/"),
                     str(_OUT_ROOT).replace("\\", "\\\\")):
            s = s.replace(form, "<OUT>")
    return _TS.sub("<TS>", s)


def _strip_frame_meta(raw: str) -> str:
    """去掉产物里新增的 ``analysis_frame`` 块，得到「语义视图」。

    Stage 5 给产物加了**框架元数据** —— 它是**新增**，不是改动。若逐字节比对时
    把它算进去，8 个产物会全线飘红，反而看不出「分析结论有没有变」。

    所以每个产物算两份哈希：

    * ``sha1``    —— 原样（元数据算在内）；
    * ``sha1_nf`` —— 去掉 ``analysis_frame`` 块、JSON 重新序列化后的。

    ``sha1_nf`` 一致 = **除了新增的元数据，一个字节都没变**。
    非 JSON 产物（报告 / 看板 / CSV）没有这个块，两份哈希相同。
    """
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 - 不是 JSON 就原样返回
        return raw

    def drop(obj):
        if isinstance(obj, dict):
            obj.pop("analysis_frame", None)
            for v in obj.values():
                drop(v)
        elif isinstance(obj, list):
            for v in obj:
                drop(v)

    drop(data)
    return json.dumps(data, ensure_ascii=False, indent=2)


def sha_of_file(path: Path) -> dict:
    """返回 ``{sha1, chars, sha1_nf, chars_nf}``（都已归一化时间戳与产物路径）。"""
    raw = path.read_text(encoding="utf-8", errors="replace")
    n = norm(raw)
    nf = norm(_strip_frame_meta(raw))
    return {
        "sha1": hashlib.sha1(n.encode("utf-8")).hexdigest(), "chars": len(n),
        "sha1_nf": hashlib.sha1(nf.encode("utf-8")).hexdigest(), "chars_nf": len(nf),
    }


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(str(c) for c in cmd[1:]))
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env)
    if r.returncode != 0:
        print(f"    !! 退出码 {r.returncode}")
        print("    stdout:", (r.stdout or "")[-2000:])
        print("    stderr:", (r.stderr or "")[-2000:])
    return r


def build_pipeline(py: str, out: Path, *, frame: str = "") -> dict:
    """跑完整链路，返回 {相对路径: 说明}。"""
    out.mkdir(parents=True, exist_ok=True)
    rel = str(DATA / "reddit_posts_relevant.csv")
    cmt = str(DATA / "reddit_comments.csv")
    cmt_rel = str(DATA / "reddit_comments_relevant.csv")
    stats = str(DATA / "reddit_analysis_stats.json")
    tagged = str(DATA / "reddit_posts_tagged.csv")

    tagset = out / "tagset.json"
    annot = out / "annot"
    dims = out / "dimensions.json"
    feats = out / "features.json"
    report = out / "report.md"
    dash = out / "dashboard.html"
    quality = out / "quality_report.json"

    fr = ["--frame", frame] if frame else []

    print("[1/6] tags")
    run([py, "voc.py", "tags", "--category", CATEGORY, "--relevant", rel,
         "--comments", cmt, "--out", str(tagset), "--mock", *fr], PLATFORM)

    print("[2/6] annotate")
    run([py, "voc.py", "annotate", "--tagset", str(tagset), "--relevant", rel,
         "--comments", cmt, "--run-dir", str(annot),
         "--export", str(out / "units_tagged.csv"),
         "--export-comments", str(out / "units_comments_tagged.csv"),
         "--mock", *fr], PLATFORM)

    print("[3/6] aggregate")
    run([py, str(PLATFORM / "voc_core" / "kernel" / "aggregate_annotations.py"),
         "--run-dir", str(annot), "--relevant", rel,
         "--comments-csv", cmt_rel,
         "--tagset", str(tagset), "--dimensions", str(dims),
         "--features", str(feats), "--category", CATEGORY], PLATFORM)

    print("[4/6] report")
    run([py, "voc.py", "report", "--dimensions", str(dims), "--features-json", str(feats),
         "--stats", stats, "--relevant", rel, "--comments", cmt_rel,
         "--category", CATEGORY, "--out", str(report)], PLATFORM)

    print("[5/6] dashboard")
    run([py, "voc.py", "dashboard", "--stats", stats, "--tagged", tagged,
         "--relevant", rel, "--comments", cmt_rel,
         "--dimensions", str(dims), "--features-json", str(feats),
         "--category", CATEGORY, "--out", str(dash), "--no-logo"], PLATFORM)

    print("[6/6] validate")
    run([py, "voc.py", "validate", "--stats", stats, "--dimensions", str(dims),
         "--features-json", str(feats), "--tagged", tagged, "--relevant", rel,
         "--comments", cmt_rel, "--report", str(report), "--dashboard", str(dash),
         "--out", str(quality)], PLATFORM)

    return {
        "tagset.json": "标签集（维度框架的载体）",
        "annot/": "逐条标注中间结果",
        "dimensions.json": "聚合维度（下游看板/报告的输入）",
        "features.json": "功能体验与两个矩阵",
        "report.md": "Markdown 洞察报告",
        "dashboard.html": "交互式看板",
        "quality_report.json": "质量闸门结果",
        "units_tagged.csv": "逐单元标注导出",
    }


def snapshot(out: Path, manifest: dict) -> dict:
    """对目录里所有产物算哈希。annot/ 只算文件名+内容哈希的**汇总**。"""
    files: dict[str, dict] = {}
    for key in manifest:
        p = out / key
        if p.is_dir():
            entries = []
            blob_nf: list[str] = []
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    h = sha_of_file(f)
                    name = str(f.relative_to(out))
                    entries.append({"name": name, "sha1": h["sha1"], "chars": h["chars"]})
                    blob_nf.append(f"{name}:{h['sha1_nf']}")
            blob = "\n".join(f"{e['name']}:{e['sha1']}" for e in entries)
            files[key] = {
                "kind": "dir", "files": len(entries),
                "sha1": hashlib.sha1(blob.encode("utf-8")).hexdigest(),
                "sha1_nf": hashlib.sha1("\n".join(blob_nf).encode("utf-8")).hexdigest(),
                "chars": sum(e["chars"] for e in entries),
                "notes": manifest[key],
            }
        elif p.exists():
            h = sha_of_file(p)
            files[key] = {"kind": "file", **h, "notes": manifest[key]}
        else:
            files[key] = {"kind": "missing", "notes": manifest[key]}
    return files


def _nf(entry: dict) -> str:
    """取「语义视图」哈希：新基线有 ``sha1_nf``；旧基线只有 ``sha1``。

    旧基线的产物里还没有 ``analysis_frame`` 块，所以它的 ``sha1`` 本身就是
    语义视图 —— 直接拿来当参照，于是**跨改造前后也能比**。
    """
    return entry.get("sha1_nf") or entry.get("sha1") or ""


def probe_axis_text(out: Path) -> dict:
    """抽几段「轴相关」的可见文案，方便人眼确认换了框架之后确实变了。

    哈希只能告诉你「变了」，这些摘要告诉你「变成什么了」——
    验收要求是「下游不报错、自动隐藏空模块」，那得看见标题与结构。
    """
    res: dict = {}
    dims_p = out / "dimensions.json"
    if dims_p.exists():
        d = json.loads(dims_p.read_text(encoding="utf-8"))
        res["dimension_keys"] = sorted(k for k, v in d.items() if isinstance(v, list))
        uc = d.get("unit_counts") or {}
        res["persona_scope_rule"] = uc.get("persona_scope_rule", "")
        res["status_gating"] = uc.get("status_gating")
        res["axis_block"] = d.get("analysis_frame", {}).get("axis", {})
        res["excluded_unknown_units"] = uc.get("excluded_unknown_units")
    rep = out / "report.md"
    if rep.exists():
        txt = rep.read_text(encoding="utf-8")
        res["report_headings"] = [ln for ln in txt.splitlines() if ln.startswith("#")]
        res["report_mentions_purchase_status"] = txt.count("购买状态")
    dash = out / "dashboard.html"
    if dash.exists():
        txt = dash.read_text(encoding="utf-8")
        res["dashboard_mentions_purchase_status"] = txt.count("购买状态")
        res["dashboard_has_hidden_hook"] = "card.style.display = 'none'" in txt
    return res


#: 看板里「结构指纹」的比对字段。**刻意不含** cardId / chartId / persona_card_dims ——
#: 这三样在 Stage 5 里**本来就该变**（从写死两张卡改成按框架的桶派生）。
_SHAPE_FIELDS = ("persona_card_count", "persona_card_titles", "persona_card_subtitles",
                 "embedded_dimension_keys")


def probe_dashboard(out: Path) -> dict:
    """看板**结构指纹** —— 让「画像卡片按桶生成」可机器验证。

    ``sha1`` 只能告诉你「看板变了」。而 Stage 5 的看板**本来就该变**：画像卡片
    从写死的两张（有购买者 / 未购买者）改成「按框架的活跃桶生成，几张由框架决定」。
    所以要有一份**结构证据**，说明变的是「怎么生成」而不是「长什么样」：

    * ``persona_card_titles`` / ``persona_card_subtitles`` —— 可见外观（DOM 顺序）
    * ``persona_card_count`` —— 卡片张数（应等于框架的活跃桶数）
    * ``embedded_dimension_keys`` —— 内嵌 ``dimensions`` 的键。**必须已剔除**
      ``analysis_frame``（那份元数据只给上游看，不该塞进看板给前端）；
      剩下的键与改造前逐字一致。
    * ``persona_card_ids`` / ``persona_card_dims`` —— 只记录、不当回归判据。
      改造前写死 ``card-persona`` / ``card-persona-non``，改造后按桶派生
      ``card-persona-<桶id>``；仓库里没有任何外部引用（只有看板自身的 JS），
      改名安全。
    * ``css_classes_sha`` —— 看板用到的 CSS 类清单哈希。**换框架也不该变**，
      是跨框架的不变量。
    """
    res: dict = {}
    p = out / "dashboard.html"
    if not p.exists():
        return res
    txt = p.read_text(encoding="utf-8")

    cards = [{"cardId": m.group(1), "title": m.group(2),
              "subtitle": m.group(3), "chartId": m.group(4)}
             for m in re.finditer(
                 r'<div class="card" id="(card-persona[^"]*)">\s*'
                 r'<div class="card-title">([^<]*)</div>\s*'
                 r'<div class="card-subtitle">([^<]*)</div>\s*'
                 r'<div id="([^"]+)"', txt)]
    res["persona_card_count"] = len(cards)
    res["persona_card_titles"] = [c["title"] for c in cards]
    res["persona_card_subtitles"] = [c["subtitle"] for c in cards]
    res["persona_card_ids"] = [c["cardId"] for c in cards]
    res["persona_chart_ids"] = [c["chartId"] for c in cards]

    # 改造后：一张卡一个 dim（内嵌 personaCards[].dim）。改造前没有这个字段，
    # 所以只在有的时候记录 —— 缺失不算失败，比对时按「基线未记录」处理。
    m = re.search(r"\n  personaCards: (\[.*?\]),\n", txt, re.S)
    if m:
        try:
            res["persona_card_dims"] = [c.get("dim", "") for c in json.loads(m.group(1))]
        except Exception:  # noqa: BLE001
            res["persona_card_dims"] = []

    m = re.search(r"\n  dimensions: (\{.*?\}),\n  [a-zA-Z]", txt, re.S)
    if m:
        try:
            res["embedded_dimension_keys"] = sorted(json.loads(m.group(1)).keys())
        except Exception:  # noqa: BLE001
            res["embedded_dimension_keys"] = []
    res["embedded_has_analysis_frame"] = "analysis_frame" in (m.group(1) if m else "")

    cls = sorted({x for c in re.findall(r'class="([^"]+)"', txt) for x in c.split()})
    res["css_classes_sha"] = hashlib.sha1("\n".join(cls).encode("utf-8")).hexdigest()
    res["css_classes_count"] = len(cls)
    return res


def shape(probe: dict) -> str:
    """把**框架无关的结构指纹**折算成一个哈希（跨改造前后、跨框架都能比）。"""
    if not probe:
        return ""
    canon = json.dumps({k: probe.get(k) for k in _SHAPE_FIELDS},
                       ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()


def probe_invariants(out: Path) -> dict:
    """换框架的**不变量**断言 —— 让「下游不报错、自动隐藏空模块」可机器验证。

    全部从产物**自己声明的框架**（``analysis_frame.segmentation_axis``）推出来，
    所以 consumer 与 b2b 两套框架跑同一份断言。核心几条：

    * **排除桶不出现在分布里** —— 桶被标了 ``excluded: true``，就不能出现在
      ``<distribution_key>`` 中（看板/报告要「自动隐藏空模块」）；
    * **全桶计数仍在披露** —— ``unit_counts[<unit_counts_key>]`` 保留**所有**桶的
      计数，排除桶不静默消失；
    * **排除计数自洽** —— ``excluded_unknown_units`` 等于排除桶的 ``total``；
    * **总量自洽** —— 分布各桶之和 + 排除量 == 帖子单元 + 评论单元；
    * **画像维度只挂活跃桶** —— 带 ``persona_bucket`` 的维度不得挂在排除桶上。
    """
    dims_p = out / "dimensions.json"
    if not dims_p.exists():
        return {}
    d = json.loads(dims_p.read_text(encoding="utf-8"))
    af = d.get("analysis_frame") or {}
    ax = af.get("segmentation_axis") or {}
    if not ax:
        # 改造前的基线产物里还没有框架元数据 —— 不变量**无从断言**，
        # 报「不适用」而不是「失败」，否则比对时会误报一堆红。
        return {"ok": True, "applicable": False, "passed": 0, "total": 0,
                "active_buckets": [], "excluded_buckets": [],
                "distribution_key": "", "unit_counts_key": "", "checks": []}
    uc = d.get("unit_counts") or {}
    buckets = ax.get("buckets") or []
    unknown_bid = ax.get("unknown_bucket") or "unknown"
    dist_key = ax.get("distribution_key") or "purchase_status_distribution"
    uc_key = ax.get("unit_counts_key") or "purchase_status"

    active = [b["id"] for b in buckets if not b.get("excluded")]
    excluded = [b["id"] for b in buckets if b.get("excluded")]
    dist = d.get(dist_key) or []
    dist_ids = [e.get("bucket") or e.get("status") for e in dist]
    all_counts = uc.get(uc_key) or {}
    persona_buckets = [x.get("persona_bucket") for x in (af.get("dimensions") or [])
                       if x.get("persona_bucket")]
    #: ``post_units`` / ``comment_units`` 是**整数**（不是 {total: n} 结构）。
    total = int(uc.get("post_units") or 0) + int(uc.get("comment_units") or 0)

    checks = [
        {"name": "排除桶不出现在分布里",
         "ok": all(b not in dist_ids for b in excluded),
         "detail": f"排除 {excluded} / 分布仅 {dist_ids}"},
        {"name": "分布条目 = 活跃桶（不多不少）",
         "ok": sorted(dist_ids) == sorted(active),
         "detail": f"分布 {sorted(dist_ids)} vs 活跃桶 {sorted(active)}"},
        {"name": "全桶计数仍披露（排除桶不静默消失）",
         "ok": sorted(all_counts.keys()) == sorted(active + excluded),
         "detail": f"unit_counts[{uc_key}] = {sorted(all_counts.keys())}"},
        {"name": "排除计数自洽",
         "ok": uc.get("excluded_unknown_units") == (all_counts.get(unknown_bid) or {}).get("total"),
         "detail": f"excluded_unknown_units={uc.get('excluded_unknown_units')}"
                   f" vs {unknown_bid}.total={(all_counts.get(unknown_bid) or {}).get('total')}"},
        {"name": "总量自洽（分布之和 + 排除量 = 单元总数）",
         "ok": sum(e.get("count", 0) for e in dist) + (uc.get("excluded_unknown_units") or 0) == total,
         "detail": f"{sum(e.get('count', 0) for e in dist)}"
                   f" + {uc.get('excluded_unknown_units')} == {total}"},
        {"name": "画像维度只挂活跃桶",
         "ok": all(b in active for b in persona_buckets),
         "detail": f"画像桶 {persona_buckets} / 活跃桶 {active}"},
    ]
    failed = [c for c in checks if not c["ok"]]
    return {"ok": not failed, "applicable": True, "passed": len(checks) - len(failed),
            "total": len(checks),
            "active_buckets": active, "excluded_buckets": excluded,
            "distribution_key": dist_key, "unit_counts_key": uc_key,
            "checks": checks}


#: 看板里「按框架生成」的那几块。掩掉之后，**其余部分必须与改造前逐字节一致**。
#: 左 = 改造前（写死两张卡），右 = 改造后（按框架的桶派生）。两边掩码数量可以不等
#: （两张卡的两个渲染函数 + 两个调用点 → 一个循环 + 一个调用点）。
_DASH_MASKS: list[tuple[str, str]] = [
    (r'    <div class="card" id="card-persona">[\s\S]*?'
     r'<div id="chart-persona" class="chart-container"></div>\n    </div>\n',
     r'    <div class="card" id="card-persona-purchased">[\s\S]*?'
     r'<div id="chart-persona-purchased" class="chart-container"></div>\n    </div>\n'),
    (r'    <div class="card" id="card-persona-non">[\s\S]*?'
     r'<div id="chart-persona-non" class="chart-container"></div>\n    </div>\n',
     r'    <div class="card" id="card-persona-non_purchased">[\s\S]*?'
     r'<div id="chart-persona-non_purchased" class="chart-container"></div>\n    </div>\n'),
    (r'  personaData: \{[^\n]*\n  personaNonPurchasedData: \{[^\n]*\n',
     r'  personaCards: \[[^\n]*\n'),
    (r'(?!)', r'// 画像卡片按框架的桶渲染：[\s\S]*?空模块自动消失。\n'),  # 新增注释
    (r'function renderPersonaPurchased\(\)[\s\S]*?\n\}\n\n',
     r'function renderPersonaCards\(\)[\s\S]*?\n\}\n\n'),
    (r'function renderPersonaNonPurchased\(\)[\s\S]*?\n\}\n\n', r'(?!)'),
    (r'  renderPersonaPurchased\(\);\n  renderPersonaNonPurchased\(\);\n',
     r'  renderPersonaCards\(\);\n'),
    # 三处**开发者向注释**也会被烤进 HTML。原文写死了 consumer 的「两张图 /
    # 购买者 / 未购买者」，Stage 5 之后张数由框架的活跃桶决定，注释已失真 → 同步改写。
    (r'// 两张图（购买者 / 未购买者）共用同一渲染器[\s\S]*?故无第三张图。\n',
     r'// 画像卡张数由框架的\*\*活跃桶\*\*决定[\s\S]*?整卡隐藏。\n'),
    (r'// 购买状态分布：已购 / 未购 / 无法判断的单元占比（决定人群画像的归桶）\n',
     r'// 分段轴分布（consumer = 购买状态、B2B = 决策角色…）[\s\S]*?决定人群画像的归桶\n'),
    (r'  // 无数据时整卡隐藏（与购买状态环形图的隐藏策略一致）\n',
     r'  // 无数据时整卡隐藏（与分段轴分布图的隐藏策略一致）\n'),
]


def mask_persona_regions(text: str, *, framed: bool) -> tuple[str, int]:
    """把「按框架生成画像卡片」的那几块**整块抠掉**。

    ``framed=True``（改造后：按桶派生）／``False``（改造前：写死两张卡）。
    返回 ``(掩码后文本, 成功掩掉的块数)``。

    为什么需要它：改造把两条内嵌数据合并成一条、把两个渲染函数合成一个循环，
    **行数变了**。这时候逐行 zip 会让后面所有行整体错位，看起来「771 行都变了」，
    完全掩盖真相。抠掉预期会变的那几块之后再比，才看得出「有没有别的改动」。

    ``tests/test_debranding.py`` 的跨内核逐行比对直接复用这个函数。
    """
    NEVER = r"(?!)"
    n = 0
    for pat_base, pat_new in _DASH_MASKS:
        pat = pat_new if framed else pat_base
        if pat == NEVER:
            continue
        text, k = re.subn(pat, "", text)
        n += k
    return text, n


def dashboard_masked_diff(base_dir: Path, new_dir: Path) -> dict:
    """**掩码差分** —— 证明看板的改动被严格限定在「画像卡片」那几块。

    逐字节哈希只会说「394 KB 的页面变了」；行级 diff 会骗人：改造把两条内嵌数据
    合并成一条，之后所有行整体错位，看起来「771 行都变了」。

    所以换个判据：把**预期会变的那 5 处**（两张卡的 div、内嵌画像数据、两个渲染
    函数、调用点）整块抠掉，剩下 39 万字符必须**逐字节一致**。抠掉之后还完全相同
    ⇒ 改动没有溢出到别的模块（图表、导航、筛选、证据下钻都没被碰到）。

    ``(?!)`` 是「永不匹配」的空掩码：新版把两个函数合并成一个，旧版的第二条
    掩码在新版里自然不该命中，掩掉 0 处是**正确**的。
    """
    res: dict = {"ok": False, "regions": 0, "sha1_base": "", "sha1_new": ""}
    pb, pn = base_dir / "dashboard.html", new_dir / "dashboard.html"
    if not (pb.exists() and pn.exists()):
        return res

    def load(p: Path) -> str:
        return norm(p.read_text(encoding="utf-8")).replace("\r\n", "\n")

    base, new = load(pb), load(pn)
    NEVER = r"(?!)"
    # 先做**掩码写入校验**：掩码写错会「静默地什么都没掩掉」，于是后面必然不等 ——
    # 但原因是掩码错，不是产物错。所以每个非空掩码都要求恰好命中一次。
    for pat_base, pat_new in _DASH_MASKS:
        if pat_base != NEVER and len(re.findall(pat_base, base)) != 1:
            return {**res, "error": f"基线侧掩码命中 {len(re.findall(pat_base, base))} 次"
                                    f"（应为 1）：{pat_base[:48]}…"}
        if pat_new != NEVER and len(re.findall(pat_new, new)) != 1:
            return {**res, "error": f"新侧掩码命中 {len(re.findall(pat_new, new))} 次"
                                    f"（应为 1）：{pat_new[:48]}…"}
    base, _ = mask_persona_regions(base, framed=False)
    new, _ = mask_persona_regions(new, framed=True)

    hb = hashlib.sha1(base.encode("utf-8")).hexdigest()
    hn = hashlib.sha1(new.encode("utf-8")).hexdigest()
    res.update({"regions": len(_DASH_MASKS), "sha1_base": hb, "sha1_new": hn,
                "chars_base": len(base), "chars_new": len(new), "ok": hb == hn})
    return res


def diff(base: dict, new: dict) -> list[str]:
    """逐产物比对。三种结论：一致 / 只多了框架元数据 / 真的变了。"""
    lines = []
    for key in sorted(set(base) | set(new)):
        b, n = base.get(key), new.get(key)
        if b is None or n is None:
            lines.append(f"  ✗ {key}: 只在一边存在")
            continue
        if _nf(b) == _nf(n):
            if b.get("sha1") == n.get("sha1"):
                lines.append(f"  = {key}: 逐字节一致 {n.get('sha1','')[:8]}"
                             f"（{n.get('chars')} 字符）")
            else:
                lines.append(f"  ≈ {key}: 语义一致，仅新增 analysis_frame 元数据"
                             f"（{b.get('chars')} → {n.get('chars')} 字符）")
        else:
            lines.append(f"  ✗ {key}: 变了 —— 语义哈希 {_nf(b)[:8]} → {_nf(n)[:8]}"
                         f"（chars {b.get('chars')} → {n.get('chars')}）")
    return lines


def diff_shape(base: dict, new: dict) -> list[str]:
    """比对两边的看板结构指纹，给出**人眼可核**的诊断。

    三类结论：

    * ``= 结构一致`` —— 框架无关的那几项逐字相同；
    * ``≈ 结构按框架变`` —— 卡片张数 / 标题 / 副标题变了，但变的方向是
      「按桶生成」：CSS 类清单不变（没有偷偷改样式）、内嵌 dimensions 的键
      不变（没漏字段）、新框架的桶数与卡片数对得上；
    * ``✗ 结构异常`` —— CSS 类清单变了，或卡片张数与活跃桶数对不上。
    """
    if not base or not new:
        return []
    lines: list[str] = []
    b, n = base, new
    same_css = b.get("css_classes_sha") == n.get("css_classes_sha")
    n_active = n.get("active_buckets")
    cards_ok = (n.get("persona_card_count") == n_active) if n_active is not None else True
    same_embed = b.get("embedded_dimension_keys") == n.get("embedded_dimension_keys")

    identical = (shape(b) == shape(n) and same_css)
    if identical:
        lines.append(f"  = dashboard 结构: 与基线一致（{n.get('persona_card_count')} 张卡 / "
                     f"{n.get('css_classes_count')} 个 CSS 类 / 内嵌 dimensions "
                     f"{len(n.get('embedded_dimension_keys') or [])} 键）")
        old_ids = b.get("persona_card_ids") or []
        new_ids = n.get("persona_card_ids") or []
        if old_ids != new_ids:
            lines.append(f"      卡片 id: {old_ids} → {new_ids}")
            lines.append("      （改造前写死两张卡的 id，改造后按桶派生；仓库内无外部引用，"
                         "看板自身 JS 已同步改名）")
        dims = n.get("persona_card_dims")
        if dims and not b.get("persona_card_dims"):
            lines.append(f"      新增内嵌绑定: personaCards[].dim = {dims}"
                         "（改造前散在 JS 里写死，现在随卡片一起下发）")
        return lines

    ok = same_css and cards_ok and same_embed
    lines.append(f"  {'≈' if ok else '✗'} dashboard 结构: "
                 f"{'按框架的桶生成（预期变化）' if ok else '异常，见下'}")
    lines.append(f"      卡片数 {b.get('persona_card_count')} → {n.get('persona_card_count')}"
                 f"（活跃桶 {n_active}）{'✓' if cards_ok else ' ✗ 对不上'}")
    for i in range(max(len(b.get("persona_card_titles") or []),
                       len(n.get("persona_card_titles") or []))):
        bt = (b.get("persona_card_titles") or [None] * 9)[i]
        nt = (n.get("persona_card_titles") or [None] * 9)[i]
        bs = (b.get("persona_card_subtitles") or [None] * 9)[i]
        ns = (n.get("persona_card_subtitles") or [None] * 9)[i]
        mark = "=" if (bt == nt and bs == ns) else "~"
        lines.append(f"      {mark} [{i}] {bt!r}")
        if bt != nt:
            lines.append(f"            → {nt!r}")
        if bs != ns:
            lines.append(f"          副标题 → {ns!r}")
    old_ids = b.get("persona_card_ids") or []
    new_ids = n.get("persona_card_ids") or []
    if old_ids != new_ids:
        lines.append(f"      id: {old_ids} → {new_ids}（按桶派生，仓库内无外部引用）")
    dims = n.get("persona_card_dims")
    if dims:
        lines.append(f"      绑定维度: {dims}"
                     + ("" if b.get("persona_card_dims") else "（基线未记录此字段）"))
    lines.append(f"      {'=' if same_embed else '✗'} 内嵌 dimensions 键 "
                 f"{len(b.get('embedded_dimension_keys') or [])} → "
                 f"{len(n.get('embedded_dimension_keys') or [])}"
                 + ("" if same_embed else f" ✗ {n.get('embedded_dimension_keys')}"))
    lines.append(f"      {'=' if same_css else '✗'} CSS 类清单 "
                 f"{b.get('css_classes_count')} → {n.get('css_classes_count')}"
                 + ("" if same_css else " ✗ 样式被改动了"))
    lines.append(f"      {'=' if not n.get('embedded_has_analysis_frame') else '✗'} "
                 f"看板内嵌 dimensions 已剔除 analysis_frame")
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Stage 5 零回归基线")
    ap.add_argument("--tag", default="baseline", help="本次运行的标签（决定输出目录名）")
    ap.add_argument("--out-root", default="out/verify/stage5", help="测试产物根目录")
    ap.add_argument("--frame", default="", help="传给内核的分析框架（留空 = 内置 consumer）")
    ap.add_argument("--compare", default="", help="与给定的 baseline.json 比对")
    ap.add_argument("--keep", action="store_true", help="不清理已有产物目录")
    args = ap.parse_args(argv)

    py = sys.executable
    global _OUT_ROOT
    out = (PLATFORM / args.out_root / args.tag).resolve()
    _OUT_ROOT = out
    if out.exists() and not args.keep:
        shutil.rmtree(out)
    manifest = build_pipeline(py, out, frame=args.frame)
    files = snapshot(out, manifest)
    dash_probe = probe_dashboard(out)
    invariants = probe_invariants(out)
    # 把框架的活跃桶数并进看板指纹 —— `diff_shape` 要用它判断「卡片数 == 桶数」。
    dash_probe["active_buckets"] = (len(invariants.get("active_buckets") or [])
                                    if invariants.get("applicable") else None)
    payload = {"tag": args.tag, "frame": args.frame or "(内置 consumer)",
               "category": CATEGORY, "files": files,
               "axis_probe": probe_axis_text(out),
               "dashboard_probe": dash_probe, "invariants": invariants}
    (out / "baseline.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    print(f"\n已写 {out / 'baseline.json'}")
    for k, v in files.items():
        flag = "!" if v.get("kind") == "missing" else " "
        print(f" {flag} {k:24s} {v.get('kind',''):8s} {v.get('sha1','')[:12]:12s} {v.get('chars','')}")

    if invariants:
        if invariants.get("applicable"):
            print(f"\n=== 换框架不变量（{invariants['passed']}/{invariants['total']} 通过）===")
            print(f"    活跃桶 {invariants['active_buckets']} / 排除桶 {invariants['excluded_buckets']}"
                  f" / 分布键 {invariants['distribution_key']} / 计数键 {invariants['unit_counts_key']}")
            for c in invariants["checks"]:
                print(f"  {'✓' if c['ok'] else '✗'} {c['name']}：{c['detail']}")
        else:
            print("\n=== 换框架不变量：不适用（产物里还没有 analysis_frame 元数据）===")

    if dash_probe:
        print("\n=== 看板结构指纹 ===")
        print(f"    画像卡片 {dash_probe.get('persona_card_count')} 张 / "
              f"CSS 类 {dash_probe.get('css_classes_count')} 个 / "
              f"内嵌 dimensions {len(dash_probe.get('embedded_dimension_keys') or [])} 键")
        for i, t in enumerate(dash_probe.get("persona_card_titles") or []):
            print(f"      [{i}] {t}  ← id={((dash_probe.get('persona_card_ids') or []) + [''])[i]}"
                  f" dim={((dash_probe.get('persona_card_dims') or []) + [''])[i]}")

    if args.compare:
        cmp_path = Path(args.compare)
        base = json.loads(cmp_path.read_text(encoding="utf-8"))
        base_dash = base.get("dashboard_probe") or {}
        backfilled = False
        if not base_dash and (cmp_path.parent / "dashboard.html").exists():
            # 旧基线冻结时还没有结构指纹。看板指纹只依赖 dashboard.html 本身，
            # 所以可以直接从基线目录**补算** —— 不用重跑整条链路。
            base_dash = probe_dashboard(cmp_path.parent)
            backfilled = True
        base_inv = base.get("invariants") or {}
        if not base_inv.get("applicable") and (cmp_path.parent / "dimensions.json").exists():
            base_inv = probe_invariants(cmp_path.parent)
        print(f"\n=== 与 {args.compare} 比对（{base.get('tag')} / frame={base.get('frame')}）===")
        for ln in diff(base["files"], files):
            print(ln)
        semantic = [k for k in files if _nf(base["files"].get(k, {})) != _nf(files[k])]
        raw = [k for k in files if base["files"].get(k, {}).get("sha1") != files[k].get("sha1")]
        print(f"\n语义变化项：{len(semantic)} / {len(files)}"
              + (f"  →  {'、'.join(semantic)}" if semantic else "（分析结论一个字节都没变）"))
        print(f"逐字节变化项：{len(raw)} / {len(files)}"
              + (f"  →  {'、'.join(raw)}" if raw else "")
              + ("（多出来的都只是新增的 analysis_frame 元数据）" if raw and not semantic else ""))

        print("\n=== 看板结构比对 ==="
              + ("（基线冻结时还没有结构指纹，已从基线产物补算）" if backfilled else ""))
        for ln in diff_shape(base_dash, dash_probe):
            print(ln)
        md = dashboard_masked_diff(cmp_path.parent, out)
        if md.get("error"):
            print(f"  ✗ 掩码差分无法进行：{md['error']}")
        elif md["sha1_base"]:
            mark = "=" if md["ok"] else "✗"
            print(f"  {mark} 掩码差分：掩掉「画像卡片」相关的 {md['regions']} 处后，"
                  f"剩余 {md['chars_new']} 字符"
                  + ("逐字节一致" if md["ok"] else "**不一致**"))
            print(f"      sha1 {md['sha1_base'][:16]} vs {md['sha1_new'][:16]}")
            if md["ok"]:
                print("      → 改动没有溢出到别的区块（图表 / 导航 / 筛选 / 证据下钻都没被碰）")
        if base_inv:
            print(f"\n=== 不变量回归（基线 {base_inv.get('passed')}/{base_inv.get('total')}"
                  f" → 本次 {invariants.get('passed')}/{invariants.get('total')}）===")
            if not base_inv.get("applicable"):
                print("  基线的产物里还没有 analysis_frame（改造之前），无从断言；"
                      "以本次为准。")
            lost = [c["name"] for c in (base_inv.get("checks") or []) if not c["ok"]]
            print("  " + ("基线里本来就有失败的：" + "、".join(lost) if lost
                          else "基线全部通过；本次若也全通过则无回归"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
