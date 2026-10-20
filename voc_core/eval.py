#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 8 · 评测与回归（护城河）。

回答一个问题：**改一次口径，5 分钟内知不知道有没有变差？**

为什么需要它
------------
Stage 6 的闸门守的是「产物**自洽**」—— count 与证据条数对齐、引文可回溯、
字段契约完整。它守不住「产物**仍然正确**」：

* 把 `excluded_unknown_units` 的算法改一改，产物照样自洽，但画像覆盖率悄悄
  从 55.6% 变成 3.8%（mock 侧的实测值），报告还是"通过"；
* 把某条正则改宽一点，标签还在、reason 还在、闸门全绿，只是最大的那条结论
  从「客厅通用座椅」漂成了别的东西。

这两类退化都**不抛异常**，也都**过得了闸门**。路线图里点名的那次改动
（"移除购买状态不明者画像"）正是这一类——它同时改变了
`purchase_status_distribution` 与 `excluded_unknown_units`。

设计
----
1. **golden set 是「真实输入 + 人工核对过的答案」**，不是「上次跑出来的快照」。
   归档的真实 LLM 标注（``annot/runs/ng2026``：154 帖 + 7662 评论）就是输入；
   `reports/reddit_analysis_dimensions.json` 就是当年人眼核对过的结论。
   两者之间的**确定性尾部**（aggregate → report → dashboard → validate）可以重跑，
   实测字段级完全复现（仅 `generated_at` 不同，aggregate 用时 ~10s）。

2. **两层判据，缺一不可**：
   * *锚点*（``expected.json``，人写的）：正确答案的**下界断言**——"这个数必须还是
     这个数"、"这条引文必须还能逐字回溯"。它守的是**正确性**。
   * *基线*（``baselines/<case>.json``，冻结的）：全量指标的**快照**。它守的是
     **不漂移**——任何一格数字变了都要有人看一眼，至于变好变坏由人判断。
   只有锚点会「红」，只有基线会「diff」；把两者混成一个数就没法改口径了。

3. **零依赖**：只用标准库，裸解释器 ``python -S -E`` 也能跑（Stage 5 起立的规矩）。

产物
----
* ``eval/golden/<case>/case.json``      —— 一个评测案（输入在哪、怎么跑）
* ``eval/golden/<case>/expected.json``  —— 人工核对的锚点
* ``eval/baselines/<case>.json``        —— 冻结的指标基线
* 跑一次：``python tools/regress.py``（或 ``python voc.py eval``）

退出码：0 = 锚点全过且无漂移 ｜ 1 = 有锚点违反或有漂移 ｜ 2 = 配置/输入错误。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

PLATFORM = Path(__file__).resolve().parents[1]
REPO_ROOT = PLATFORM.parent

__all__ = [
    "CaseRun",
    "GoldenCase",
    "MODE_ANNOT_TAIL",
    "MODE_PIPELINE",
    "check_anchors",
    "compare_metrics",
    "discover_cases",
    "evaluate",
    "extract_metrics",
    "load_baseline",
    "load_case",
    "render_console",
    "render_markdown",
    "run_case",
]

EVAL_SCHEMA_VERSION = 1
DEFAULT_GOLDEN_DIR = "eval/golden"
DEFAULT_BASELINES_DIR = "eval/baselines"
CASE_FILE = "case.json"
ANCHOR_FILE = "expected.json"

#: 评测案的两种驱动方式。
#: * ``annot-tail`` —— 从**归档的真实标注**起步，只跑确定性尾部。
#:   信号最高（真结论），但要求该案自带一份人工标注产物。
#: * ``pipeline``   —— 走 Stage 7 的编排器跑完 7 步（mock 后端）。
#:   信号来自编排层本身，确定性由 mock 保证。
MODE_ANNOT_TAIL = "annot-tail"
MODE_PIPELINE = "pipeline"
MODES = (MODE_ANNOT_TAIL, MODE_PIPELINE)

#: 闸门结论的好坏序（越小越好）。``empty`` = 全部检查项都因缺输入跳过。
VERDICT_ORDER = {"empty": -1, "pass": 0, "warn": 1, "fail": 2}

#: 「关键结论」只取这几个维度的 top-N 标签做指纹。
#: 品牌维度刻意不在列：它的排序本来就会被一个新品牌名搅动，属于噪声不是结论。
CONCLUSION_DIMS = (
    "usage_scenarios",
    "pre_purchase_concerns",
    "post_purchase_experience",
    "unmet_needs",
)
CONCLUSION_TOP_N = 3

#: 漂移的严重度分级（只影响报告的排布与颜色，不改变退出码 —— 有漂移就非零）。
_DRIFT_SEVERITY = {
    "axis": "high",
    "conclusions": "high",
    "dim_sizes": "high",
    "evidence_verbatim_rate": "high",
    "excluded_unknown_units": "high",
    "persona_covered_units": "high",
    "tag_covered_units": "high",
    "total_units": "high",
    "gate": "medium",
    "excluded_unknown_ratio": "medium",
    "persona_coverage": "medium",
    "tag_coverage": "medium",
    "products": "low",
}
_SEV_RANK = {"high": 0, "medium": 1, "low": 2}

SILENT: Callable[..., None] = lambda *a, **k: None  # noqa: E731

#: 默认只跑 ``core`` —— 「5 分钟内知道有没有变差」是 Stage 8 的目标，默认档必须够快。
#: ``extended`` 档是 mock 端到端案，单案 ~3 分钟（瓶颈是 annotate 要写 7816 个中间
#: 文件，其余六步合计仅 ~20s），留给改编排层时跑。
DEFAULT_TIER = "core"


def resolve_tier(all_tiers: bool, tier: str) -> str:
    """``--all`` > ``--tier X`` > 默认 ``core``。"""
    if all_tiers:
        return ""
    return tier or DEFAULT_TIER


# ==========================================================================
# 评测案
# ==========================================================================


@dataclass
class GoldenCase:
    """一个评测案：输入在哪、怎么跑、正确答案是什么。

    ``paths`` 是**驱动产物**用的实际文件路径（跑之前就存在）；
    ``anchors`` 是人工核对的上下界断言，见 :func:`check_anchors`。
    """

    id: str
    label: str = ""
    mode: str = MODE_ANNOT_TAIL
    category: str = ""
    frame: str = ""
    source: str = "auto"
    tier: str = "core"
    #: 该案实际读到的输入文件（路径已解析为绝对路径）。
    paths: dict[str, str] = field(default_factory=dict)
    #: ``pipeline`` 模式下的 JobSpec 载荷（``mock`` 默认 True）。
    spec: dict[str, Any] = field(default_factory=dict)
    #: 人工锚点。
    anchors: dict[str, Any] = field(default_factory=dict)
    #: case.json 所在目录。
    dir: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "mode": self.mode,
            "category": self.category,
            "frame": self.frame,
            "source": self.source,
            "tier": self.tier,
            "paths": dict(self.paths),
            "spec": dict(self.spec),
            "anchors": dict(self.anchors),
            "dir": self.dir,
        }


def _resolve_paths(raw: dict[str, Any], root: Path) -> dict[str, str]:
    """把 case.json 里的路径解析成绝对路径（已存在才收；相对路径按 ``root`` 展开）。"""
    out: dict[str, str] = {}
    for key, val in (raw or {}).items():
        if not val:
            continue
        p = Path(str(val))
        if not p.is_absolute():
            p = root / p
        out[key] = str(p)
    return out


def load_case(case_dir: Path, *, root: Path = REPO_ROOT) -> GoldenCase:
    """从 ``<case_dir>/case.json`` 读一个评测案，并挂上 ``expected.json`` 的锚点。"""
    case_dir = Path(case_dir)
    meta_path = case_dir / CASE_FILE
    if not meta_path.exists():
        raise FileNotFoundError(f"评测案缺少 {CASE_FILE}：{meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    anchor_path = case_dir / ANCHOR_FILE
    anchors = (json.loads(anchor_path.read_text(encoding="utf-8"))
               if anchor_path.exists() else {})

    mode = str(meta.get("mode") or MODE_ANNOT_TAIL)
    if mode not in MODES:
        raise ValueError(f"未知的 case mode：{mode!r}（可选 {'/'.join(MODES)}）")

    return GoldenCase(
        id=str(meta.get("id") or case_dir.name),
        label=str(meta.get("label") or ""),
        mode=mode,
        category=str(meta.get("category") or ""),
        frame=str(meta.get("frame") or ""),
        source=str(meta.get("source") or "auto"),
        tier=str(meta.get("tier") or "core"),
        paths=_resolve_paths(meta.get("paths") or {}, root),
        spec=dict(meta.get("spec") or {}),
        anchors=anchors,
        dir=str(case_dir),
    )


def discover_cases(golden_dir: Path | str = DEFAULT_GOLDEN_DIR, *,
                   root: Path = REPO_ROOT,
                   only: Iterable[str] = (),
                   tier: str = "") -> list[GoldenCase]:
    """扫描 golden 目录。``only`` 给出 id 名单时按名单过滤（并保序）。"""
    gd = Path(golden_dir)
    if not gd.is_absolute():
        gd = PLATFORM / gd
    if not gd.is_dir():
        raise FileNotFoundError(f"golden 目录不存在：{gd}")

    want = [s for s in only if s]
    cases: list[GoldenCase] = []
    for child in sorted(gd.iterdir()):
        if not child.is_dir():
            continue
        if not (child / CASE_FILE).exists():
            continue
        if want:
            #: 点名的案一律跑，**不被 tier 过滤** —— 否则 ``--case <extended案>``
            #: 会被默认的 core 档静默吞掉，报「找不到评测案」，很费解。
            if child.name not in want:
                continue
        case = load_case(child, root=root)
        if not want and tier and case.tier != tier:
            continue
        cases.append(case)

    if want:
        missing = [w for w in want if all(c.id != w for c in cases)]
        if missing:
            raise ValueError(f"找不到评测案：{', '.join(missing)}")
        cases.sort(key=lambda c: want.index(c.id))
    return cases


# ==========================================================================
# 跑评测案
# ==========================================================================


@dataclass
class CaseRun:
    """一次评测案的执行结果（产物路径 + 指标 + 账目）。"""

    case: GoldenCase
    ok: bool
    work_dir: str
    products: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0
    commands: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def id(self) -> str:
        return self.case.id


#: ``annot-tail`` 模式下，产物在 work 目录里的规范键 → 文件名。
TAIL_PRODUCTS = {
    "dimensions": "dimensions.json",
    "features": "features.json",
    "report": "report.md",
    "dashboard": "dashboard.html",
    "quality": "quality_report.json",
}

#: ``pipeline`` 模式下，规范键 → job 目录内的候选相对路径（按序取第一个存在的）。
PIPELINE_PRODUCTS = {
    "dimensions": ("out/dataset_dimensions.json",),
    "features": ("out/dataset_features.json",),
    "stats": ("out/{source}_analysis_stats.json", "out/dataset_stats.json"),
    "tagged": ("out/{source}_posts_tagged.csv", "out/units_tagged.csv"),
    "relevant": ("out/{source}_posts_relevant.csv", "out/units_relevant.csv"),
    "comments": ("out/units_comments_relevant.csv",),
    "report": ("report/insight_report.md",),
    "dashboard": ("report/dashboard.html",),
    "quality": ("report/quality_report.json",),
}


#: 瞬时文件锁的指纹。本机实测：杀软实时扫描 / 备份服务会短暂扣住刚写出的文件，
#: 让内核脚本在覆盖写时崩掉；重试一两次就过去了。**只识别这一类**，别的失败照旧。
_TRANSIENT_LOCK = re.compile(
    r"PermissionError|WinError 5\b|Errno 13|拒绝访问|另一个程序正在使用|"
    r"being used by another process|WinError 32\b", re.I)


def _run(cmd: list[str], *, cwd: Path, log: Callable[..., None],
         _tries: int = 6) -> int:
    """跑一条命令；瞬时文件锁自动退避重试（最多 ``_tries`` 次）。"""
    log(f"    $ {' '.join(Path(c).name if c.startswith(str(cwd)) else c for c in cmd)}")
    code = 0
    for attempt in range(max(1, _tries)):
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        code = proc.returncode
        if code == 0:
            return 0
        tail = "\n".join((proc.stdout or "").splitlines()[-15:])
        err = "\n".join((proc.stderr or "").splitlines()[-15:])
        blob = f"{tail}\n{err}"
        if attempt + 1 < _tries and _TRANSIENT_LOCK.search(blob):
            log(f"    … exit={code}，识别为瞬时文件锁，重试（{attempt + 2}/{_tries}）")
            time.sleep(min(5.0, 0.5 * (2 ** attempt)))
            continue
        log(f"    ! exit={code}\n{tail}\n{err}")
        break
    return code


def _kernel(script: str) -> str:
    return str(PLATFORM / "voc_core" / "kernel" / script)


def run_case(case: GoldenCase, work_root: Path | str, *,
             log: Callable[..., None] = print,
             force: bool = True) -> CaseRun:
    """跑一个评测案，把产物落到 ``<work_root>/<case.id>/``。

    两种模式共用同一个「产物规范键」表（:data:`TAIL_PRODUCTS` /
    :data:`PIPELINE_PRODUCTS`），所以 :func:`extract_metrics` 对两者一视同仁。
    """
    t0 = time.time()
    if case.mode == MODE_ANNOT_TAIL:
        return _run_annot_tail(case, work_root, log=log, t0=t0)
    return _run_pipeline(case, work_root, log=log, t0=t0, force=force)


def _run_annot_tail(case: GoldenCase, work_root: Path | str, *,
                    log: Callable[..., None], t0: float) -> CaseRun:
    work = Path(work_root) / case.id
    work.mkdir(parents=True, exist_ok=True)
    paths = case.paths

    required = ("annot_dir", "tagset", "relevant", "comments", "stats", "tagged")
    missing = [k for k in required if not paths.get(k)]
    if missing:
        return CaseRun(case, False, str(work), error=f"case 缺少输入：{', '.join(missing)}")
    for key in required:
        if not Path(paths[key]).exists():
            return CaseRun(case, False, str(work), error=f"输入不存在：{key} = {paths[key]}")

    dims = work / TAIL_PRODUCTS["dimensions"]
    feats = work / TAIL_PRODUCTS["features"]
    report = work / TAIL_PRODUCTS["report"]
    dash = work / TAIL_PRODUCTS["dashboard"]
    quality = work / TAIL_PRODUCTS["quality"]

    profile = case.source or "auto"
    common_frame = ["--frame", case.frame] if case.frame else []

    steps: list[tuple[str, list[str]]] = [
        ("aggregate", [sys.executable, _kernel("aggregate_annotations.py"),
                       "--run-dir", paths["annot_dir"],
                       "--relevant", paths["relevant"],
                       "--tagset", paths["tagset"],
                       "--dimensions", str(dims),
                       "--features", str(feats),
                       "--category", case.category,
                       "--comments-csv", paths["comments"],
                       "--profile", profile, *common_frame]),
        ("report", [sys.executable, _kernel("generate_report.py"),
                    "--dimensions", str(dims),
                    "--features-json", str(feats),
                    "--stats", paths["stats"],
                    "--relevant", paths["relevant"],
                    "--category", case.category,
                    "--comments", paths["comments"],
                    "--run-dir", paths["annot_dir"],
                    "--out", str(report)]),
        ("dashboard", [sys.executable, _kernel("generate_dashboard.py"),
                       "--stats", paths["stats"],
                       "--tagged", paths["tagged"],
                       "--relevant", paths["relevant"],
                       "--comments", paths["comments"],
                       "--category", case.category,
                       "--dimensions", str(dims),
                       "--features-json", str(feats),
                       "--out", str(dash)]),
        ("validate", [sys.executable, str(PLATFORM / "voc_core" / "validate.py"),
                      "--stats", paths["stats"],
                      "--dimensions", str(dims),
                      "--features-json", str(feats),
                      "--tagged", paths["tagged"],
                      "--relevant", paths["relevant"],
                      "--comments", paths["comments"],
                      "--report", str(report),
                      "--dashboard", str(dash),
                      "--profile", profile,
                      "--out", str(quality)]),
    ]

    cmds: list[str] = []
    for name, cmd in steps:
        log(f"  · {name}")
        cmds.append(" ".join(cmd))
        code = _run(cmd, cwd=PLATFORM, log=log)
        # 闸门退出码 1 = 产物有问题（不是执行失败），产物已写出，继续。
        if code != 0 and name != "validate":
            return CaseRun(case, False, str(work), commands=cmds,
                           error=f"{name} 失败（exit={code}）",
                           elapsed_s=time.time() - t0)

    products = {
        "dimensions": str(dims), "features": str(feats), "report": str(report),
        "dashboard": str(dash), "quality": str(quality),
        "stats": paths["stats"], "tagged": paths["tagged"],
        "relevant": paths["relevant"], "comments": paths["comments"],
    }
    return CaseRun(case, True, str(work), products=products,
                   elapsed_s=time.time() - t0, commands=cmds)


def _run_pipeline(case: GoldenCase, work_root: Path | str, *,
                  log: Callable[..., None], t0: float,
                  force: bool) -> CaseRun:
    from voc_core import pipeline as P  # 延迟导入：annot-tail 案用不到编排器

    job_dir = Path(work_root) / case.id
    payload = dict(case.spec)
    payload.setdefault("category", case.category)
    payload.setdefault("frame", case.frame)
    payload.setdefault("source", case.source)
    payload.setdefault("mock", True)

    # spec 里的相对路径按 repo 根展开（与 case.paths 同一套规矩）。
    for key in ("input", "comments"):
        val = payload.get(key)
        if val and not Path(str(val)).is_absolute():
            payload[key] = str(REPO_ROOT / str(val))

    spec = P.JobSpec.from_dict(payload)
    log(f"  · pipeline（{payload.get('source') or 'auto'} / "
        f"{payload.get('frame') or '默认框架'} / "
        f"{'mock' if payload.get('mock') else '真后端'}）")
    res = P.run_job(spec, job_dir, force="all" if force else "", log=log)

    source_key = ""
    try:
        state = json.loads((job_dir / P.JOB_FILE).read_text(encoding="utf-8"))
        source_key = str((state.get("source_profile") or {}).get("name") or "")
    except Exception:  # noqa: BLE001 - 名字取不到只是候选路径退化，不该中断评测
        pass
    if not source_key:
        source_key = str(case.source if case.source not in ("", "auto") else "reddit")

    products: dict[str, str] = {}
    for kind, candidates in PIPELINE_PRODUCTS.items():
        for rel in candidates:
            p = job_dir / rel.format(source=source_key)
            if p.exists():
                products[kind] = str(p)
                break

    ok = res.exit_code in (P.EXIT_OK,) and bool(products.get("dimensions"))
    err = ""
    if not ok:
        err = (f"编排失败：exit={res.exit_code} status={res.status} "
               f"failed={list(res.failed or [])}")
    return CaseRun(case, ok, str(job_dir), products=products,
                   elapsed_s=time.time() - t0, error=err)


# ==========================================================================
# 指标
# ==========================================================================


def _load_json(path: str) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 坏 JSON 由闸门那条链负责报错，这里退化成空
        return {}
    return data if isinstance(data, dict) else {}


def _canon_sha(obj: Any) -> str:
    """对 JSON 载荷取规范指纹：去掉 ``generated_at`` 这类运行环境回显。"""
    def scrub(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: scrub(v) for k, v in sorted(o.items())
                    if k not in ("generated_at", "run_dir", "annot_dir")}
        if isinstance(o, list):
            return [scrub(x) for x in o]
        return o
    blob = json.dumps(scrub(obj), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _sum_counts(items: Any) -> int:
    if not isinstance(items, list):
        return 0
    return sum(int(x.get("count") or 0) for x in items if isinstance(x, dict))


def _conclusions(dims: dict[str, Any]) -> dict[str, list[list[Any]]]:
    """关键结论指纹：每个维度取 count 最高的 N 个标签（``[label_cn, count]``）。

    只记**标签名 + 计数**，不记证据——证据会随语料浮动，标签漂移才是结论漂移。
    """
    out: dict[str, list[list[Any]]] = {}
    for dim in CONCLUSION_DIMS:
        items = [x for x in (dims.get(dim) or []) if isinstance(x, dict)]
        items.sort(key=lambda x: int(x.get("count") or 0), reverse=True)
        out[dim] = [[str(x.get("label_cn") or ""), int(x.get("count") or 0)]
                    for x in items[:CONCLUSION_TOP_N]]
    return out


def extract_metrics(case: GoldenCase, products: dict[str, str]) -> dict[str, Any]:
    """从产物里抽指标。三个来源：dimensions / quality_report / 语料（引文回溯）。"""
    from voc_core import validate as V

    dims = _load_json(products.get("dimensions", ""))
    feats = _load_json(products.get("features", ""))
    quality = _load_json(products.get("quality", ""))

    unit_counts = dims.get("unit_counts") or {}
    post_units = int(unit_counts.get("post_units") or 0)
    comment_units = int(unit_counts.get("comment_units") or 0)
    total_units = post_units + comment_units
    excluded = int(unit_counts.get("excluded_unknown_units") or 0)

    # -- 分段轴分布（消费品的 purchase_status / B2B 的 decision_role）--------
    ctx = V.Context(dimensions=dims, frame_name=case.frame or "",
                    profile_name=case.source or "")
    axis_key = ctx.axis_dist_key()
    axis_rows = [x for x in (dims.get(axis_key) or []) if isinstance(x, dict)]
    axis_counts = {str(x.get("status") or x.get("label_cn") or i): int(x.get("count") or 0)
                   for i, x in enumerate(axis_rows)}

    # -- 画像覆盖 / 标签密度 -----------------------------------------------
    #: 两个都是「Σ 计数 / 全部单元」。同一个单元可以在一个维度里命中多个标签，
    #: 所以它们不是严格的「去重覆盖率」，而是**密度**——路线图要的是「有基线、
    #: 一变就看得见」，不是统计上无懈可击。画像那项沿用路线图的叫法。
    persona_units = sum(_sum_counts(dims.get(d)) for d in ctx.persona_dims())
    tag_units = sum(_sum_counts(dims.get(d)) for d in V.LABEL_DIMS
                    if d not in ctx.persona_dims())
    ratio = lambda n: round(n / total_units, 6) if total_units else 0.0  # noqa: E731

    # -- 语料索引：引文可核查率与标杆引文都要它 -----------------------------
    corpus_ctx = None
    try:
        corpus_ctx = V.Context(
            dimensions=dims,
            tagged=_read_rows(products.get("tagged", "")),
            relevant=_read_rows(products.get("relevant", "")),
            comments=_read_rows(products.get("comments", "")),
            frame_name=case.frame or "",
        )
    except Exception:  # noqa: BLE001 - 语料读不进来只影响引文类指标
        corpus_ctx = None

    # -- 引文可核查率（直接复用 Stage 6 那条检查，口径不许有第二份实现）------
    verbatim_total = verbatim_missed = 0
    verbatim_rate = 0.0
    if corpus_ctx is not None:
        try:
            _, _, detail = V.check_evidence_quote_verbatim(corpus_ctx)
            verbatim_total = int(detail.get("total") or 0)
            verbatim_missed = int(detail.get("missed") or 0)
            if verbatim_total:
                verbatim_rate = round(1 - verbatim_missed / verbatim_total, 6)
        except Exception:  # noqa: BLE001
            pass

    # -- 标杆引文（人工从结论里挑的那几条）：标签还在、原文还能逐字对上 ------
    quote_checks: list[dict[str, Any]] = []
    for a in (case.anchors.get("quote_anchors") or []):
        dim = str(a.get("dim") or "")
        label = str(a.get("label_cn") or "")
        quote = str(a.get("quote") or "")
        label_present = any(
            isinstance(x, dict) and str(x.get("label_cn") or "") == label
            for x in (dims.get(dim) or []))
        traceable = False
        if corpus_ctx is not None and quote:
            needle = V._norm(quote)
            try:
                traceable = any(needle in hay
                                for hay in corpus_ctx.thread_index.values())
            except Exception:  # noqa: BLE001
                traceable = False
        quote_checks.append({"dim": dim, "label_cn": label, "quote": quote,
                             "label_present": label_present, "traceable": traceable})

    # -- 闸门结论 -----------------------------------------------------------
    summary = quality.get("summary") or {}
    gate = {
        "verdict": str(summary.get("verdict") or "missing"),
        "passed": int(summary.get("passed") or 0),
        "failed": int(summary.get("failed") or 0),
        "skipped": int(summary.get("skipped") or 0),
        "failed_checks": sorted(
            str(f.get("check")) for f in (quality.get("findings") or [])
            if isinstance(f, dict) and f.get("status") == "fail"),
    }

    return {
        "schema": EVAL_SCHEMA_VERSION,
        "case": case.id,
        "mode": case.mode,
        "frame": str((dims.get("analysis_frame") or {}).get("name") or case.frame or ""),
        "total_units": total_units,
        "units": {"post": post_units, "comment": comment_units},
        "axis": {"key": axis_key, "label": ctx.axis_label(), "counts": axis_counts},
        "excluded_unknown_units": excluded,
        "excluded_unknown_ratio": ratio(excluded),
        "persona_covered_units": persona_units,
        "persona_coverage": ratio(persona_units),
        "tag_covered_units": tag_units,
        "tag_coverage": ratio(tag_units),
        "evidence_total": verbatim_total,
        "evidence_missed": verbatim_missed,
        "evidence_verbatim_rate": verbatim_rate,
        "dim_sizes": {k: len(v) for k, v in dims.items()
                      if isinstance(v, list) and v and isinstance(v[0], dict)
                      and k not in ("purchase_status_distribution",
                                    "decision_role_distribution")},
        "conclusions": _conclusions(dims),
        "quote_anchors": quote_checks,
        "gate": gate,
        "products": {
            "dimensions": _canon_sha(dims),
            "features": _canon_sha(feats),
        },
    }


def _read_rows(path: str, limit: int = 0) -> list[dict[str, Any]]:
    """读一个 CSV 成 dict 行（引文回溯用）。只在需要时才调。"""
    if not path:
        return []
    import csv
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    with p.open(encoding="utf-8-sig", newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            if limit and i >= limit:
                break
            rows.append(dict(row))
    return rows


# ==========================================================================
# 比对
# ==========================================================================


def _diff_value(key: str, base: Any, cur: Any) -> dict[str, Any] | None:
    """比较一格指标。返回 None 表示一致。"""
    if base == cur:
        return None
    return {
        "key": key,
        "severity": _DRIFT_SEVERITY.get(key, "medium"),
        "baseline": base,
        "current": cur,
    }


def compare_metrics(baseline: dict[str, Any],
                    current: dict[str, Any]) -> list[dict[str, Any]]:
    """逐格比对指标，返回漂移清单（按严重度排序，再按键名）。

    只比**指标键**，不比 ``schema`` / ``case`` 这类元信息 —— 那些由调用方管。
    """
    keys = [k for k in baseline
            if k not in ("schema", "case", "mode")]
    drift: list[dict[str, Any]] = []
    for key in sorted(keys):
        # 新增的指标键（老基线没有）不算漂移，但也提示一句。
        if key not in current:
            continue
        item = _diff_value(key, baseline.get(key), current.get(key))
        if item:
            drift.append(item)
    # 基线里没有、现在多了的键（一般来自新增指标）也值得看一眼
    for key in sorted(set(current) - set(baseline)):
        if key in ("schema", "case", "mode"):
            continue
        drift.append({"key": key, "severity": "low",
                      "baseline": None, "current": current[key],
                      "note": "新增指标键（基线里没有）"})
    drift.sort(key=lambda d: (_SEV_RANK.get(d["severity"], 9), d["key"]))
    return drift


def _anchors_from_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """把一格的指标整理成「锚点形状」，供 ``--freeze-anchors`` 起草人工答案。"""
    return {
        "metrics": {k: metrics.get(k) for k in
                    ("persona_covered_units", "tag_covered_units", "evidence_total")},
        "total_units": metrics.get("total_units"),
        "purchase_status": dict((metrics.get("axis") or {}).get("counts") or {}),
        "excluded_unknown_units": metrics.get("excluded_unknown_units"),
        "top_labels": {dim: [pair[0] for pair in pairs]
                       for dim, pairs in (metrics.get("conclusions") or {}).items()},
        "evidence_verbatim_rate_min": metrics.get("evidence_verbatim_rate"),
        "gate_verdict_in": [metrics.get("gate", {}).get("verdict")],
        "gate_failed_max": metrics.get("gate", {}).get("failed"),
    }


#: 通用标量锚点（``expected.json`` 的 ``metrics`` 块）的「一句话解释」。
#: 加新指标时只要往这里补一行，不必改校验代码。
_ANCHOR_NOTES = {
    "persona_covered_units": "画像覆盖的单元数变了 —— 「购买状态不明者」是否又被算进了画像？",
    "tag_covered_units": "标签覆盖的单元数变了 —— 标签命中或语料口径动了",
    "evidence_total": "证据总条数变了 —— 引文抽样或标签命中口径动了",
    "excluded_unknown_units": "排除桶规模变了 —— 画像覆盖率会跟着漂",
    "total_units": "语料单元数变了 —— 上游清洗 / 收窄口径动了",
    "persona_coverage": "画像覆盖率变了",
    "tag_coverage": "标签密度变了",
}


def check_anchors(anchors: dict[str, Any],
                  metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """校验人工锚点。返回违反清单（空 = 全过）。

    锚点是**下界式**断言：写进去的每一项都必须成立；没写的不管。
    支持的形式见 :func:`_anchors_from_metrics`。
    """
    bad: list[dict[str, Any]] = []

    def fail(key: str, want: Any, got: Any, note: str = "") -> None:
        bad.append({"key": key, "expected": want, "actual": got, "note": note})

    if not anchors:
        return bad

    if "total_units" in anchors and anchors["total_units"] != metrics.get("total_units"):
        fail("total_units", anchors["total_units"], metrics.get("total_units"),
             "语料单元数变了 —— 上游清洗/收窄口径动了")

    want_ps = anchors.get("purchase_status") or {}
    got_ps = (metrics.get("axis") or {}).get("counts") or {}
    for key, want in want_ps.items():
        got = got_ps.get(key)
        if want != got:
            fail(f"purchase_status.{key}", want, got,
                 "分段轴归桶变了 —— 正是「移除购买状态不明者画像」那类改动会碰的地方")

    if ("excluded_unknown_units" in anchors
            and anchors["excluded_unknown_units"] != metrics.get("excluded_unknown_units")):
        fail("excluded_unknown_units", anchors["excluded_unknown_units"],
             metrics.get("excluded_unknown_units"),
             "排除桶规模变了 —— 画像覆盖率会跟着漂")

    for dim, want_labels in (anchors.get("top_labels") or {}).items():
        got_labels = [pair[0] for pair in (metrics.get("conclusions") or {}).get(dim, [])]
        for i, want in enumerate(want_labels):
            got = got_labels[i] if i < len(got_labels) else None
            if want != got:
                fail(f"top_labels.{dim}[{i}]", want, got,
                     "关键结论漂移 —— 最该有人看一眼的那种退化")

    floor = anchors.get("evidence_verbatim_rate_min")
    if floor is not None:
        got = metrics.get("evidence_verbatim_rate") or 0.0
        if got + 1e-9 < float(floor):
            fail("evidence_verbatim_rate", f"≥ {floor}", got,
                 "引文可核查率跌破下限 —— 证据被改写或语料索引退化")

    for i, q in enumerate(metrics.get("quote_anchors") or []):
        tag = f"quote_anchors[{i}]"
        if not q.get("label_present"):
            fail(f"{tag}.label_cn", q.get("label_cn"), None,
                 f"标杆引文所挂的结论「{q.get('label_cn')}」在 {q.get('dim')} 里消失了")
        if not q.get("traceable"):
            fail(f"{tag}.quote", "可在讨论串里逐字命中", "命中失败",
                 f"标杆引文无法逐字回溯（{q.get('dim')} / {q.get('label_cn')}）—— "
                 f"原文被改动或语料索引退化")

    for key, want in (anchors.get("metrics") or {}).items():
        got = metrics.get(key)
        if want != got:
            fail(key, want, got, _ANCHOR_NOTES.get(key, ""))

    allowed = anchors.get("gate_verdict_in")
    if allowed:
        got = (metrics.get("gate") or {}).get("verdict")
        if got not in allowed:
            fail("gate.verdict", f"∈ {allowed}", got, "闸门结论变差")

    cap = anchors.get("gate_failed_max")
    if cap is not None:
        got = int((metrics.get("gate") or {}).get("failed") or 0)
        if got > int(cap):
            fail("gate.failed", f"≤ {cap}", got, "闸门失败项变多")

    return bad


# ==========================================================================
# 基线存取
# ==========================================================================


def baseline_path(case_id: str, baselines_dir: Path | str = DEFAULT_BASELINES_DIR) -> Path:
    bd = Path(baselines_dir)
    if not bd.is_absolute():
        bd = PLATFORM / bd
    return bd / f"{case_id}.json"


def load_baseline(case_id: str,
                  baselines_dir: Path | str = DEFAULT_BASELINES_DIR) -> dict[str, Any]:
    p = baseline_path(case_id, baselines_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def save_baseline(case_id: str, metrics: dict[str, Any],
                  baselines_dir: Path | str = DEFAULT_BASELINES_DIR) -> Path:
    p = baseline_path(case_id, baselines_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    return p


# ==========================================================================
# 编排
# ==========================================================================


def evaluate(cases: Sequence[GoldenCase], *,
             baselines_dir: Path | str = DEFAULT_BASELINES_DIR,
             work_root: Path | str = "out/verify/stage8",
             freeze: bool = False,
             update: bool = False,
             keep_going: bool = True,
             log: Callable[..., None] = print) -> dict[str, Any]:
    """跑全部评测案，出对比表。

    * ``freeze=True`` —— **写**基线（首次建立用）。不判漂移。
    * ``update=True`` —— 无论有没有漂移都覆盖基线（已确认改动、要接受新答案时用）。
    * 两者都 False   —— 只读基线比对，漂移即非零退出。
    """
    wr = Path(work_root)
    if not wr.is_absolute():
        wr = PLATFORM / wr
    wr.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for i, case in enumerate(cases):
        if i and keep_going is False:
            break
        log(f"\n[{i + 1}/{len(cases)}] {case.id} —— {case.label or case.mode}")
        run = run_case(case, wr, log=log)
        entry: dict[str, Any] = {
            "id": case.id, "label": case.label, "mode": case.mode,
            "tier": case.tier, "ok": run.ok, "error": run.error,
            "elapsed_s": round(run.elapsed_s, 2), "work_dir": run.work_dir,
            "drift": [], "violations": [],
        }
        if not run.ok:
            log(f"    ! 未跑通：{run.error}")
            results.append(entry)
            continue

        metrics = extract_metrics(case, run.products)
        # 基线里回显的 case/mode 是元信息，比对前先剔除
        base = load_baseline(case.id, baselines_dir)
        base_cmp = {k: v for k, v in base.items() if k not in ("case", "mode")}
        cur_cmp = {k: v for k, v in metrics.items() if k not in ("case", "mode")}

        entry["violations"] = check_anchors(case.anchors, metrics)
        if base_cmp and not (freeze or update):
            entry["drift"] = compare_metrics(base_cmp, cur_cmp)
        entry["metrics"] = metrics

        if freeze or update:
            p = save_baseline(case.id, metrics, baselines_dir)
            entry["baseline_written"] = str(p)

        n_bad = len(entry["violations"]) + len(entry["drift"])
        log(f"    {'✓' if not n_bad else '✗'} 用时 {run.elapsed_s:.1f}s "
            f"｜锚点违反 {len(entry['violations'])} ｜漂移 {len(entry['drift'])}"
            + (f" ｜基线已写 {entry['baseline_written']}"
               if entry.get("baseline_written") else ""))
        results.append(entry)

    hard = [r for r in results if not r["ok"]]
    viol = sum(len(r["violations"]) for r in results)
    drift = sum(len(r["drift"]) for r in results)
    exit_code = 1 if (hard or viol or drift) else 0

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "schema": EVAL_SCHEMA_VERSION,
        "mode": ("freeze" if freeze else "update" if update else "check"),
        "work_root": str(wr),
        "exit_code": exit_code,
        "totals": {
            "cases": len(results),
            "failed_runs": len(hard),
            "violations": viol,
            "drift": drift,
        },
        "cases": results,
    }


# ==========================================================================
# 呈现
# ==========================================================================

_ICON = {"high": "🔴", "medium": "🟠", "low": "⚪"}


def _fmt(v: Any, width: int = 34) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False,
                                                separators=(",", ":"))
    s = " ".join(str(s).split())
    return s if len(s) <= width else s[:width - 1] + "…"


def render_console(report: dict[str, Any]) -> str:
    t = report["totals"]
    lines = [
        "=" * 78,
        f"Stage 8 评测回归 ｜ 模式 {report['mode']} ｜ 案 {t['cases']} ｜ "
        f"跑不通 {t['failed_runs']} ｜ 锚点违反 {t['violations']} ｜ 漂移 {t['drift']}",
        "=" * 78,
    ]
    for case in report["cases"]:
        head = f"{case['id']}  ({case['mode']}, {case['elapsed_s']}s)"
        if not case["ok"]:
            lines.append(f"✗ {head}\n    {case['error']}")
            continue
        flag = "✓" if not (case["violations"] or case["drift"]) else "✗"
        m = case.get("metrics") or {}
        gate = (m.get("gate") or {}).get("verdict", "?")
        lines.append(
            f"{flag} {head}\n"
            f"    单元 {m.get('total_units')} ｜ 画像覆盖 {m.get('persona_coverage')} "
            f"｜ 标签密度 {m.get('tag_coverage')} ｜ 排除未知 {m.get('excluded_unknown_ratio')} "
            f"｜ 引文可核查 {m.get('evidence_verbatim_rate')} ｜ 闸门 {gate}")
        for v in case["violations"]:
            lines.append(f"    ✗ 锚点 {v['key']}：期望 {_fmt(v['expected'])} "
                         f"实得 {_fmt(v['actual'])}")
            if v.get("note"):
                lines.append(f"        └ {v['note']}")
        if case["drift"]:
            lines.append(f"    ── 漂移 {len(case['drift'])} 格 ──")
            for d in case["drift"][:20]:
                lines.append(f"    {_ICON.get(d['severity'], '·')} {d['key']:<28s} "
                             f"{_fmt(d['baseline'], 30):<30s} → {_fmt(d['current'], 30)}")
            if len(case["drift"]) > 20:
                lines.append(f"    … 另有 {len(case['drift']) - 20} 格")
        if case.get("baseline_written"):
            lines.append(f"    基线已写：{case['baseline_written']}")
    lines.append("-" * 78)
    lines.append("结论：" + ("通过（锚点全过、无漂移）" if not report["exit_code"]
                            else "不通过 —— 见上"))
    return "\n".join(lines)


def render_markdown(report: dict[str, Any]) -> str:
    t = report["totals"]
    L = [
        "# Stage 8 评测回归报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 模式：`{report['mode']}`",
        f"- 案情：{t['cases']} 个 ｜ 跑不通 {t['failed_runs']} ｜ "
        f"锚点违反 {t['violations']} ｜ 漂移 {t['drift']}",
        f"- 结论：**{'通过' if not report['exit_code'] else '不通过'}**",
        "",
        "## 指标总览",
        "",
        "| 评测案 | 单元 | 画像覆盖 | 标签密度 | 排除未知 | 引文可核查 | 闸门 | 用时 |",
        "|---|---:|---:|---:|---:|---:|---|---:|",
    ]
    for c in report["cases"]:
        m = c.get("metrics") or {}
        if not c["ok"]:
            L.append(f"| `{c['id']}` | — | — | — | — | — | 跑不通 | {c['elapsed_s']}s |")
            continue
        L.append(
            f"| `{c['id']}` | {m.get('total_units')} | {m.get('persona_coverage')} | "
            f"{m.get('tag_coverage')} | {m.get('excluded_unknown_ratio')} | "
            f"{m.get('evidence_verbatim_rate')} | {(m.get('gate') or {}).get('verdict')} | "
            f"{c['elapsed_s']}s |")

    for c in report["cases"]:
        L += ["", f"## {c['id']}", ""]
        if not c["ok"]:
            L.append(f"未跑通：{c['error']}")
            continue
        m = c.get("metrics") or {}
        L += [
            f"分段轴：`{m.get('axis', {}).get('key')}`"
            f"（{m.get('axis', {}).get('label')}） "
            + "、".join(f"{k}={v}" for k, v in (m.get("axis") or {}).get("counts", {}).items()),
            "",
            f"单元总数 **{m.get('total_units')}**（帖 {m.get('units', {}).get('post')} / "
            f"评论 {m.get('units', {}).get('comment')}）；"
            f"排除无法判定 **{m.get('excluded_unknown_units')}**；"
            f"画像覆盖 **{m.get('persona_covered_units')}**；"
            f"标签覆盖 **{m.get('tag_covered_units')}**；"
            f"证据 **{m.get('evidence_total')}** 条（未命中 {m.get('evidence_missed')}）。",
            "",
        ]
        L += ["| 关键结论 | top 1 | top 2 | top 3 |", "|---|---|---|---|"]
        for dim, pairs in (m.get("conclusions") or {}).items():
            cells = [f"{p[0]}（{p[1]}）" for p in pairs] + [""] * (3 - len(pairs))
            L.append(f"| `{dim}` | " + " | ".join(cells[:3]) + " |")
        L.append("")

        if c["violations"]:
            L += ["### 锚点违反", "", "| 锚点 | 期望 | 实得 | 说明 |", "|---|---|---|---|"]
            for v in c["violations"]:
                L.append(f"| `{v['key']}` | `{_fmt(v['expected'], 60)}` | "
                         f"`{_fmt(v['actual'], 60)}` | {v.get('note', '')} |")
            L.append("")
        if c["drift"]:
            L += ["### 指标漂移", "", "| 严重度 | 指标 | 基线 | 当前 |", "|---|---|---|---|"]
            for d in c["drift"]:
                L.append(f"| {d['severity']} | `{d['key']}` | `{_fmt(d['baseline'], 60)}` | "
                         f"`{_fmt(d['current'], 60)}` |")
            L.append("")
        if c.get("baseline_written"):
            L.append(f"基线已写：`{c['baseline_written']}`")
            L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    """``voc eval`` 的入口。真正的参数在 voc.py 里，这里只做转发与打印。"""
    import argparse
    ap = argparse.ArgumentParser(prog="voc eval", description="Stage 8 评测回归")
    ap.add_argument("--case", default="", help="只跑这些案（逗号分隔 id）")
    ap.add_argument("--tier", default="",
                    help="只跑该 tier（core / extended）；默认 core") 
    ap.add_argument("--all", action="store_true",
                    help="跑全部 tier（含 ~3 分钟/案的 mock 端到端案）")
    ap.add_argument("--golden-dir", default=DEFAULT_GOLDEN_DIR)
    ap.add_argument("--baselines-dir", default=DEFAULT_BASELINES_DIR)
    ap.add_argument("--work-root", default="out/verify/stage8")
    ap.add_argument("--freeze", action="store_true", help="写基线（首次建立）")
    ap.add_argument("--update", action="store_true", help="覆盖基线（接受新答案）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--markdown", default="", help="把 Markdown 报告写到该路径")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(PLATFORM))
    cases = discover_cases(args.golden_dir,
                           only=[s for s in args.case.split(",") if s.strip()],
                           tier=resolve_tier(args.all, args.tier))
    if not cases:
        print("没有可跑的评测案。")
        return 2

    report = evaluate(cases, baselines_dir=args.baselines_dir,
                      work_root=args.work_root,
                      freeze=args.freeze, update=args.update)

    if args.markdown:
        p = Path(args.markdown)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(render_markdown(report), encoding="utf-8")
        print(f"Markdown 报告：{p}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_console(report))
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
