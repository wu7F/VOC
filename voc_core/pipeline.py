#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 7 · 编排与任务化：把六步串成一条可重入的线。

设计要点（都是 Stage 1–6 的结论反过来决定的）
=============================================

**1. 不写状态机。**
路线图原文设想的是「``job.json`` 记阶段状态，某步失败后从该步继续」。但 Stage 4 的
回看（路线图末尾「三个最容易踩的坑」第 3 条）已经证明那不是最省事的做法：``task_id``
内容寻址让「重跑同一条命令」本身就等价于「从断点继续」。这里把这个结论推到底 ——

    阶段是否需要重跑 = f(参数, 输入产物内容, 内核代码内容)

算成一个 sha1 指纹存进 ``job.json``：指纹一致且产物仍在 → 跳过；任何一项变了 →
该步及其**下游**自动失效（因为上游产物是下游的输入，内容一进指纹表就跟着变）。
于是没有 ``pending/running/done`` 三态字段，没有重试计数器，也没有「状态与实际不符」
这类 bug —— 状态是**算出来的**，不是**记出来的**。

**2. 参数拼装只有一份实现。**
``voc.py`` 的 ``cmd_*`` 被直接复用：从 ``build_parser()`` 取该步的默认
:class:`argparse.Namespace`，只覆盖编排器真正要决定的那几项（路径、品类……）。
编排器**不重写**「哪个 flag 对应哪个内核参数」——那种重复是漂移的温床，而
Stage 3/5 的教训正是「同一个知识存了两份，改一份漏一份」。

**3. 输入先落地，job 自包含。**
源数据复制进 ``<job>/input/``，后续所有步骤只读 job 内部。因此 ``job.json`` 里记的
全是 job 内相对路径，换台机器、换个年份都能原样复现 —— 这正是 Stage 7 的验收条件
（「``runs/<job_id>/job.json`` 能完整复现一次分析」）。

**4. 退出码 3 不是失败。**
标签设计与逐条标注可能落到人工兜底（Stage 4）。此时 job 状态记 ``waiting_manual``，
下游标 ``blocked``，命令返回 3。回填完**重跑同一条命令**即从该步继续。

目录布局
--------

::

    runs/<job_id>/
      job.json        状态（算出来的）、参数、每步指纹与产物
      input/          源数据副本（自包含的关键）
      out/            清洗产物 + 聚合 JSON
      tags/           tagset.json
      annot/          逐条标注中间件（续传依据）
      report/         看板 + 洞察报告 + 质量报告

与路线图示意的一处差异：路线图把 ``units_relevant.csv`` 画在 ``input/`` 下，实际它
是 ``clean`` 的**产物**，故落在 ``out/``；``input/`` 只放「外部输入 + 其副本」。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

__all__ = [
    "STEPS",
    "STEP_LABELS",
    "JOB_SCHEMA_VERSION",
    "JOB_FILE",
    "JobSpec",
    "StepRun",
    "JobResult",
    "PipelineError",
    "JobPaths",
    "run_job",
    "run_batch",
    "load_job",
    "list_jobs",
    "select_steps",
    "narrow_comments",
    "rewrite_mentioned_brands",
    "EXIT_OK",
    "EXIT_FAIL",
    "EXIT_CONFIG",
    "EXIT_PENDING",
]

PLATFORM_ROOT = Path(__file__).resolve().parent.parent

#: 与全项目统一的退出码约定（Stage 4 定下）
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CONFIG = 2
EXIT_PENDING = 3

#: :: 2 —— ``steps[*].outputs`` 的含义变了：v1 记的是「跑前/跑后**差分**出的变化
#: 文件」，v2 记的是「本步**声明**的产物清单」。v1 有个要命的洞：确定性步骤重跑时
#: 产物字节不变 → 差分是空集 → ``outputs`` 记成 ``{}`` → ``_intact()`` 从此平凡为真，
#: 产物删光了也照样跳过。升版本号让旧 job.json 自动作废、整条重跑。
JOB_SCHEMA_VERSION = 2
JOB_FILE = "job.json"

#: 七步的顺序（依赖是严格链式：后一步吃前一步的产物）
STEPS: tuple[str, ...] = (
    "clean", "tags", "annotate", "aggregate", "report", "dashboard", "validate",
)

STEP_LABELS: dict[str, str] = {
    "clean": "清洗 / 分桶 / 统计",
    "tags": "标签设计",
    "annotate": "逐条标注",
    "aggregate": "聚合维度与功能",
    "report": "Markdown 洞察报告",
    "dashboard": "交互式看板",
    "validate": "质量闸门",
}

#: 每一步调用的内核脚本（``validate`` 走 Python API，故为 ``None``）；
#: 指纹里会带上脚本内容的哈希 —— 改了内核代码，所有阶段的指纹自动失效。
STEP_SCRIPTS: dict[str, str | None] = {
    "clean": "clean_and_bucket.py",
    "tags": "design_tags.py",
    "annotate": "annotate_posts.py",
    "aggregate": "aggregate_annotations.py",
    "report": "generate_report.py",
    "dashboard": "generate_dashboard.py",
    "validate": None,
}

#: ``validate`` 不走内核脚本，用这个模块的内容哈希代表「代码版本」
VALIDATE_MODULE = "voc_core/validate.py"

#: 带**编排器侧后处理**的步骤：这些步骤的产物不只由内核脚本决定，还由本模块里的
#: 后处理代码决定（例如 ``clean`` 的「评论收窄」「品牌列重写」）。改了后者，指纹
#: 必须跟着失效 —— 否则代码升级了、产物还是旧的，而系统认为"没变"。
POST_PROCESSED_STEPS = frozenset({"clean"})

#: 编排器**自己**定的产物名（不走 naming 的 legacy/neutral 判定）。
#: 这样 pipeline 内部自洽，不会因为源不同而漂移；``clean`` 的产物名仍交给内核
#: （``--artifact-names auto``），并用 ``naming.resolve_artifact`` 解析回来。
ARTIFACTS: dict[str, str] = {
    "tagset": "tags/tagset.json",
    "source_profile": "out/source_profile.json",
    "annotated": "out/units_annotated.csv",
    "annotated_comments": "out/units_comments_annotated.csv",
    "comments_relevant": "out/units_comments_relevant.csv",
    "dimensions": "out/dataset_dimensions.json",
    "features": "out/dataset_features.json",
    "insight_report": "report/insight_report.md",
    "dashboard": "report/dashboard.html",
    "quality_report": "report/quality_report.json",
    #: 目录型产物（逐条标注的中间件）。记成**一个**聚合条目，见 :func:`_collect_outputs`
    "annot_dir": "annot",
}

#: 目录骨架
LAYOUT: tuple[str, ...] = ("input", "out", "tags", "annot", "report")

#: 哪些步骤会调 LLM（它们的 Namespace 里才有 ``--backend`` 等）
LLM_STEPS = frozenset({"tags", "annotate"})

#: 每步真正**读**的**文件**参数（其余是输出路径或开关）。
#:
#: 为什么不用「跑前快照里的一切」：那会把**下游**产物也算成本步的输入 —— 第一次跑完
#: 下游后上游的指纹就变了，幂等当场失效（实测「执行 7 步，跳过 0 步」）。谁读什么，
#: 编排器本来就清楚（见 :func:`_ns_for`），声明出来最省事也最准。
STEP_INPUT_ATTRS: dict[str, tuple[str, ...]] = {
    "clean": ("input", "comments"),
    "tags": ("relevant", "comments"),
    "annotate": ("tagset", "relevant", "comments"),
    "aggregate": ("relevant", "tagset", "comments_csv"),
    "report": ("dimensions", "features_json", "stats", "relevant", "comments"),
    "dashboard": ("stats", "tagged", "relevant", "comments", "dimensions",
                  "features_json"),
    "validate": ("stats", "dimensions", "features_json", "tagged", "relevant",
                 "comments", "report", "dashboard"),
}

#: 每步真正**读**的**目录**参数（逐文件算哈希）—— 标注中间件既是续传依据也是输入。
STEP_INPUT_DIRS: dict[str, tuple[str, ...]] = {
    "annotate": ("run_dir",),
    "aggregate": ("run_dir",),
}

#: 每步**产出**的文件 —— 第一类：内核按 ``naming`` 命名的产物（在 ``<job>/out`` 里，
#: 名字随源画像变，故用 :func:`resolve_artifact_path` 先中性名后历史名地解析）。
#:
#: 为什么是**声明**而不是「跑前/跑后差分」：差分只在产物第一次出现时管用。确定性
#: 步骤重跑（``--force``、或断点续跑）时产物字节完全相同，差分得到空集，于是
#: ``outputs`` 记成 ``{}``，``_intact()`` 从此平凡为真 —— 产物删了也照样「完好」。
#: 这个洞实测重现过：``--force all`` 跑一次，``clean`` 的产物记录从 7 项掉成 0 项。
STEP_OUTPUT_KINDS: dict[str, tuple[str, ...]] = {
    # clean 的产物名由内核按 ``--artifact-names`` 决定，故走命名解析
    "clean": ("stats", "tagged", "relevant", "summary", "data_report"),
    "aggregate": ("dimensions", "features"),
}

#: 第二类：编排器**自己**定的产物（:data:`ARTIFACTS` 的键）
STEP_OUTPUT_PATHS: dict[str, tuple[str, ...]] = {
    "clean": ("source_profile", "comments_relevant"),
    "tags": ("tagset",),
    "annotate": ("annotated", "annotated_comments"),
    "report": ("insight_report",),
    "dashboard": ("dashboard",),
    "validate": ("quality_report",),
}

#: 第三类：目录型产物（存 :class:`JobPaths` 的属性名），只记一个聚合哈希
STEP_OUTPUT_DIRS: dict[str, tuple[str, ...]] = {
    "annotate": ("annot",),
}

#: LLM 参数名（与 ``voc.add_llm_cli_arguments`` 的 dest 一致）
LLM_KEYS: tuple[str, ...] = (
    "backend", "model", "base_url", "api_key", "temperature", "lang",
    "llm_config", "llm_record", "llm_replay", "llm_queue",
)


class PipelineError(RuntimeError):
    """编排层面的错误（输入缺失、步骤名写错……）—— 对应退出码 2。"""


# --------------------------------------------------------------------------
# 参数（可复现的全部依据）
# --------------------------------------------------------------------------

@dataclass
class JobSpec:
    """一次分析的**全部**输入参数。

    这是 ``job.json`` 里 ``spec`` 块的原型。只要它 + ``input/`` 里的数据 + 内核代码
    三样都在，这次分析就能原样重放 —— Stage 7 的验收条件说的就是这件事。
    """

    category: str = ""
    #: 主单元源数据（外部路径；落地后指向 ``<job>/input/``）
    input: str = ""
    #: 次级单元（评论）源数据，可选
    comments: str = ""
    source: str = "auto"
    keywords: str = ""
    brands: str = ""
    source_note: str = ""
    #: 分析框架（Stage 5）：consumer / b2b_role / …yaml
    frame: str = ""
    #: 源画像（Stage 3）：reddit / amazon / wayfair / auto
    profile: str = "auto"
    artifact_names: str = "auto"
    max_age_years: float | None = None
    #: 用 mock 后端跑 tags/annotate（不消耗额度，用于验收与自检）
    mock: bool = False
    title: str = ""
    #: LLM 后端参数（与 ``--backend`` 等同名）
    llm: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        d = {
            "category": self.category,
            "input": self.input,
            "comments": self.comments,
            "source": self.source,
            "keywords": self.keywords,
            "brands": self.brands,
            "source_note": self.source_note,
            "frame": self.frame,
            "profile": self.profile,
            "artifact_names": self.artifact_names,
            "max_age_years": self.max_age_years,
            "mock": self.mock,
            "title": self.title,
            "llm": _redact_llm(self.llm) if redact else dict(self.llm),
        }
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> JobSpec:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (d or {}).items() if k in known})

    def reproduce_command(self, job_dir: str = "") -> str:
        """渲染成一条等价命令 —— 直接抄进终端就能重放这个 job。

        路径统一转**正斜杠**再加双引号：这样同一条命令在 bash / PowerShell / cmd 里
        都能直接粘贴（Windows 也认正斜杠，而反斜杠会被 POSIX 风格的解析器当转义符
        吃掉 —— ``"out\verify"`` 会变成 ``outverify``）。
        """
        def q(v: Any) -> str:
            return '"' + str(v).replace("\\", "/") + '"'

        parts = ["python voc.py run"]
        if self.input:
            parts.append(f"--input {q(self.input)}")
        if self.comments:
            parts.append(f"--comments {q(self.comments)}")
        if self.category:
            parts.append(f"--category {q(self.category)}")
        if self.source and self.source != "auto":
            parts.append(f"--source {q(self.source)}")
        if self.keywords:
            parts.append(f"--keywords {q(self.keywords)}")
        if self.brands:
            parts.append(f"--brands {q(self.brands)}")
        if self.source_note:
            parts.append(f"--source-note {q(self.source_note)}")
        if self.frame:
            parts.append(f"--frame {q(self.frame)}")
        if self.profile and self.profile != "auto":
            parts.append(f"--profile {q(self.profile)}")
        if self.artifact_names and self.artifact_names != "auto":
            parts.append(f"--artifact-names {q(self.artifact_names)}")
        if self.max_age_years is not None:
            parts.append(f"--max-age-years {self.max_age_years}")
        if self.mock:
            parts.append("--mock")
        if self.title:
            parts.append(f"--title {q(self.title)}")
        for k in LLM_KEYS:
            v = self.llm.get(k)
            if v in (None, "", []):
                continue
            if k == "api_key":
                parts.append("--api-key ***")
            elif k == "temperature":
                parts.append(f"--temperature {v}")
            else:
                parts.append(f"--{k.replace('_', '-')} {q(v)}")
        if job_dir:
            parts.append(f"--job-dir {q(job_dir)}")
        return " ".join(parts)


def _redact_llm(llm: dict[str, Any]) -> dict[str, Any]:
    """``api_key`` 不进 job.json 明文；但留哈希前 12 位，好让指纹能区分不同密钥。"""
    out = dict(llm or {})
    key = out.get("api_key") or ""
    if key:
        out["api_key"] = f"<redacted:{_sha1_text(str(key))[:12]}>"
    return out


# --------------------------------------------------------------------------
# job 目录
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class JobPaths:
    """job 目录里各处的绝对路径。"""

    root: Path

    @property
    def input(self) -> Path:
        return self.root / "input"

    @property
    def out(self) -> Path:
        return self.root / "out"

    @property
    def tags(self) -> Path:
        return self.root / "tags"

    @property
    def annot(self) -> Path:
        return self.root / "annot"

    @property
    def report(self) -> Path:
        return self.root / "report"

    @property
    def job_file(self) -> Path:
        return self.root / JOB_FILE

    def ensure(self) -> None:
        for d in LAYOUT:
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)

    def artifact(self, key: str) -> Path:
        """按 :data:`ARTIFACTS` 取产物路径。"""
        return self.root / ARTIFACTS[key]

    def rel(self, p: str | Path) -> str:
        """转成 job 内相对路径（正斜杠）。job 外的路径原样返回。"""
        try:
            rp = Path(p).resolve()
        except OSError:  # pragma: no cover - 极端路径
            return str(p)
        try:
            return rp.relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return str(p)

    def rel_or_placeholder(self, p: str | Path) -> str:
        """同 :meth:`rel`，但 job 外 / 平台内的路径折叠成 ``<ext>`` / ``<platform>``。"""
        s = str(p)
        if not s:
            return ""
        inner = self.rel(s)
        if inner != s:
            return f"<job>/{inner}"
        try:
            rp = Path(s).resolve()
        except OSError:  # pragma: no cover
            return s
        try:
            rp.relative_to(PLATFORM_ROOT.resolve())
            return f"<platform>/{rp.relative_to(PLATFORM_ROOT.resolve()).as_posix()}"
        except ValueError:
            pass
        return f"<ext>/{rp.name}"


def resolve_artifact_path(job: JobPaths, kind: str) -> Path | None:
    """在 ``<job>/out`` 里找内核按 naming 命名的产物（tagged / relevant / stats…）。

    先中性名后历史名，与 ``naming.resolve_artifact`` 同一套规则；找不到返回 ``None``。
    """
    from voc_core.naming import resolve_artifact

    return resolve_artifact(job.out, kind)


# --------------------------------------------------------------------------
# 步骤结果
# --------------------------------------------------------------------------

@dataclass
class StepRun:
    """一个阶段的执行记录（写进 ``job.json.steps[key]``）。"""

    key: str
    status: str = "pending"
    exit_code: int = 0
    elapsed_s: float = 0.0
    script: str = ""
    #: 只记录「与默认值不同」的参数，路径已归一化成 ``<job>/…``
    params: dict[str, Any] = field(default_factory=dict)
    #: 跑前就存在的输入：相对路径 → sha1
    inputs: dict[str, str] = field(default_factory=dict)
    #: 跑后新增/变化的产物：相对路径 → sha1
    outputs: dict[str, str] = field(default_factory=dict)
    fingerprint: str = ""
    note: str = ""
    #: 产物的 ``[size, mtime_ns]``（跳过判定先用 stat 短路，见 :func:`_intact`）
    stamps: dict[str, list[int]] = field(default_factory=dict)

    #: ``done`` ｜ ``skipped``（指纹未变）｜ ``failed`` ｜ ``waiting_manual`` ｜ ``blocked``
    @property
    def ok(self) -> bool:
        return self.status in ("done", "skipped")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "elapsed_s": round(self.elapsed_s, 3),
            "script": self.script,
            "fingerprint": self.fingerprint,
            "params": self.params,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "outputs_stat": self.stamps,
            "note": self.note,
        }


@dataclass
class JobResult:
    """一次 ``run_job`` 的结果。"""

    job_dir: Path
    status: str
    steps: dict[str, StepRun] = field(default_factory=dict)
    exit_code: int = EXIT_OK
    elapsed_s: float = 0.0
    waiting: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    missing_pending: int = 0

    @property
    def done(self) -> list[str]:
        return [k for k, s in self.steps.items() if s.status == "done"]


# --------------------------------------------------------------------------
# 复用 voc.py 的解析器与命令
# --------------------------------------------------------------------------

_VOC_CLI: Any = None


def _voc_cli():
    """加载 ``voc.py``（同目录的兄弟脚本，无法直接 import，按路径加载）。"""
    global _VOC_CLI
    if _VOC_CLI is None:
        import importlib.util

        path = PLATFORM_ROOT / "voc.py"
        spec = importlib.util.spec_from_file_location("voc_cli_for_pipeline", path)
        if spec is None or spec.loader is None:  # pragma: no cover
            raise PipelineError(f"无法加载 CLI：{path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _VOC_CLI = mod
    return _VOC_CLI


def _sub_parser(parser: argparse.ArgumentParser, cmd: str) -> argparse.ArgumentParser:
    for action in parser._actions:  # noqa: SLF001 - argparse 没有公开的取子解析器 API
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return action.choices[cmd]
    raise PipelineError("CLI 里找不到子命令解析器")


def _defaults_of(parser: argparse.ArgumentParser, cmd: str) -> dict[str, Any]:
    """某子命令的默认参数表（含必填项的 ``None`` 默认）。

    用 `required=True` 的参数走 ``parse_args`` 会被拦住（它会校验必填项），所以这里
    直接读 action 的 ``default`` —— 语义上就是「用户什么都没给时的 Namespace」。
    """
    p = _sub_parser(parser, cmd)
    return {a.dest: a.default for a in p._actions if a.dest != "help"}


def _ns_for(step: str, spec: JobSpec, job: JobPaths,
            parser: argparse.ArgumentParser, *,
            allow_missing: bool = False) -> argparse.Namespace:
    """把 job 布局翻译成该步的 Namespace（路径来自 :data:`ARTIFACTS`）。"""
    def _req(kind: str) -> Path:
        return _require(job, kind, allow_missing=allow_missing)

    defaults = _defaults_of(parser, step)
    ns = argparse.Namespace(**defaults)
    ns.cmd = step
    ns.func = _sub_parser(parser, step).get_default("func")

    # LLM 参数只覆盖该步真的声明了的键
    if step in LLM_STEPS:
        for k, v in (spec.llm or {}).items():
            if k in defaults:
                setattr(ns, k, v)
        ns.mock = bool(spec.mock)

    if step == "clean":
        ns.input = str(_input_path(job, spec.input))
        ns.out_dir = str(job.out)
        ns.category = spec.category or "Product"
        ns.comments = str(_input_path(job, spec.comments)) if spec.comments else ""
        ns.keywords = spec.keywords
        ns.brands = spec.brands
        ns.source_note = spec.source_note
        ns.artifact_names = spec.artifact_names or "auto"
        if spec.max_age_years is not None:
            ns.max_age_years = spec.max_age_years

    elif step == "tags":
        ns.category = spec.category or "Product"
        ns.relevant = str(_req("relevant"))
        ns.out = str(job.artifact("tagset"))
        ns.comments = _comments_for_llm(job, spec)
        ns.frame = spec.frame

    elif step == "annotate":
        ns.tagset = str(_req("tagset"))
        ns.relevant = str(_req("relevant"))
        ns.run_dir = str(job.annot)
        ns.comments = _comments_for_llm(job, spec)
        ns.export = str(job.artifact("annotated"))
        ns.export_comments = (str(job.artifact("annotated_comments"))
                              if ns.comments else "")

    elif step == "aggregate":
        ns.run_dir = str(job.annot)
        ns.relevant = str(_req("relevant"))
        ns.tagset = str(_req("tagset"))
        ns.dimensions = str(job.artifact("dimensions"))
        ns.features = str(job.artifact("features"))
        ns.category = spec.category
        ns.comments_csv = _comments_for_llm(job, spec)

    elif step == "report":
        ns.dimensions = str(_req("dimensions"))
        ns.features_json = str(_req("features"))
        ns.stats = str(_req("stats"))
        ns.relevant = str(_req("relevant"))
        ns.comments = _comments_for_llm(job, spec)
        ns.category = spec.category
        ns.out = str(job.artifact("insight_report"))

    elif step == "dashboard":
        ns.stats = str(_req("stats"))
        ns.tagged = str(_req("tagged"))
        ns.relevant = str(_req("relevant"))
        ns.comments = _comments_for_llm(job, spec)
        ns.dimensions = str(_req("dimensions"))
        ns.features_json = str(_req("features"))
        ns.category = spec.category or "Product"
        ns.out = str(job.artifact("dashboard"))
        if spec.title:
            ns.title = spec.title

    elif step == "validate":
        ns.stats = str(_req("stats"))
        ns.dimensions = str(_req("dimensions"))
        ns.features_json = str(_req("features"))
        ns.tagged = str(_req("tagged"))
        ns.relevant = str(_req("relevant"))
        ns.comments = _comments_for_llm(job, spec)
        ns.report = str(job.artifact("insight_report"))
        ns.dashboard = str(job.artifact("dashboard"))
        ns.out = str(job.artifact("quality_report"))
        ns.frame = spec.frame

    else:  # pragma: no cover - 由 select_steps 保证不会到这儿
        raise PipelineError(f"未知步骤：{step}")

    return ns


def _input_path(job: JobPaths, src: str) -> Path:
    """源数据在 job 内的落地路径（``<job>/input/<原名>``）。"""
    if not src:
        raise PipelineError("缺少输入文件（--input）")
    p = Path(src)
    staged = job.input / p.name
    if staged.exists():
        return staged
    if p.exists():
        return p
    raise PipelineError(f"输入文件不存在：{src}")


#: 次级单元（评论）在 job 内的候选名 —— 收窄后优先，其次全量
_COMMENT_NAMES = ("units_comments_relevant.csv",)


def _comments_for_llm(job: JobPaths, spec: JobSpec) -> str:
    """喂给 LLM / 聚合的次级单元文件。

    优先用**收窄后**的 ``units_comments_relevant.csv``（SKILL.md 明确要求：直接把全量
    评论当单元会把过期帖、D 桶帖的评论算进来）。没有就回落到全量。
    """
    for n in _COMMENT_NAMES:
        p = job.out / n
        if p.exists():
            return str(p)
    if spec.comments:
        p = _input_path(job, spec.comments)
        return str(p) if p.exists() else ""
    return ""


def _require(job: JobPaths, kind: str, *, allow_missing: bool = False) -> Path:
    """取上游产物；缺失时给出可操作的提示（而不是让内核报一堆路径找不到）。

    ``allow_missing`` 供 ``--dry-run`` 用：那时上游还没跑，「内核会按 naming 取哪个名」
    尚不可知，所以返回 ``<kind>`` 占位符，只用于展示。
    """
    p: Path | None
    if kind == "tagset":
        p = job.artifact("tagset")
    elif kind == "dimensions":
        p = job.artifact("dimensions")
    elif kind == "features":
        p = job.artifact("features")
    else:
        p = resolve_artifact_path(job, kind)
    if p is not None and Path(p).exists():
        return Path(p)
    if allow_missing:
        return Path(f"<{kind}>")
    raise PipelineError(
        f"上游产物缺失：{kind}（{p if p is not None else '未找到'}）。\n"
        f"  若用 --only / --from 跳步，请确认上游已跑过；否则去掉这两个参数跑完整管线。"
    )


# --------------------------------------------------------------------------
# 快照与指纹
# --------------------------------------------------------------------------

_SHA_CACHE: dict[tuple[str, int, int], str] = {}


def _sha1_file(p: str | Path) -> str:
    """文件内容 sha1（进程内按 (路径, 大小, mtime) 缓存，一次 run 里反复哈希同一批文件）。"""
    path = Path(p)
    try:
        st = path.stat()
    except OSError:
        return ""
    key = (str(path), st.st_size, st.st_mtime_ns)
    hit = _SHA_CACHE.get(key)
    if hit is not None:
        return hit
    h = hashlib.sha1()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    val = h.hexdigest()
    _SHA_CACHE[key] = val
    return val


def _sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _input_key(job: JobPaths, p: Path) -> str:
    """``inputs`` 的键：job 内 → 相对路径；job 外 → ``<ext>/名字``（跨机器稳定）。"""
    try:
        return p.resolve().relative_to(job.root.resolve()).as_posix()
    except (ValueError, OSError):
        return job.rel_or_placeholder(p)


def _dir_sha(d: Path) -> str:
    """把整个目录压成**一个**哈希（名字 + 大小 + mtime）。

    逐文件算 sha1 在几千个标注中间件时太贵，而这里只需要「同一批文件两次跑出同一个
    值」。名字 + 大小 + mtime 的聚合满足这一点 —— 内容被改必然改 mtime；而且只需
    ``stat``，不读盘。
    """
    acc = hashlib.sha1()
    for child in sorted(d.rglob("*")):
        if not child.is_file() or "__pycache__" in child.parts:
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        name = child.relative_to(d).as_posix()
        acc.update(f"{name}\t{st.st_size}\t{st.st_mtime_ns}\n".encode("utf-8"))
    return acc.hexdigest()


def _collect_inputs(step: str, ns: argparse.Namespace, job: JobPaths) -> dict[str, str]:
    """按 :data:`STEP_INPUT_ATTRS` / :data:`STEP_INPUT_DIRS` 精确收集本步的输入指纹。"""
    out: dict[str, str] = {}
    for attr in STEP_INPUT_ATTRS.get(step, ()):
        v = getattr(ns, attr, "") or ""
        if not v:
            continue
        p = Path(v)
        if p.is_file():
            out[_input_key(job, p)] = _sha1_file(p)
    for attr in STEP_INPUT_DIRS.get(step, ()):
        v = getattr(ns, attr, "") or ""
        if not v:
            continue
        d = Path(v)
        if not d.is_dir():
            continue
        out[_input_key(job, d).rstrip("/") + "/"] = _dir_sha(d)
    return out


def _collect_outputs(step: str, job: JobPaths) -> dict[str, str]:
    """按声明收集本步的**产物**指纹（键为 job 内相对路径；目录条目以 ``/`` 结尾）。

    三类来源见 :data:`STEP_OUTPUT_KINDS` / :data:`STEP_OUTPUT_PATHS` /
    :data:`STEP_OUTPUT_DIRS`。只收**存在**的产物 —— 某步按配置不产出某项（例如
    没有 ``--comments`` 时没有收窄后的评论表）属正常，不该记成"有"。
    """
    out: dict[str, str] = {}
    for kind in STEP_OUTPUT_KINDS.get(step, ()):
        p = resolve_artifact_path(job, kind)
        if p is not None and p.is_file():
            out[job.rel(p)] = _sha1_file(p)
    for key in STEP_OUTPUT_PATHS.get(step, ()):
        p = job.artifact(key)
        if p.is_file():
            out[job.rel(p)] = _sha1_file(p)
    for attr in STEP_OUTPUT_DIRS.get(step, ()):
        d = Path(getattr(job, attr, ""))
        if str(d) and d.is_dir() and any(c.is_file() for c in d.rglob("*")):
            out[job.rel(d).rstrip("/") + "/"] = _dir_sha(d)
    return out


def _snapshot(job: JobPaths) -> dict[str, str]:
    """job 内所有文件的 {相对路径: sha1}（跳过 job.json 自己）。"""
    out: dict[str, str] = {}
    root = job.root
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if "__pycache__" in p.parts or p.name == JOB_FILE:
            continue
        rel = p.relative_to(root).as_posix()
        out[rel] = _sha1_file(p)
    return out


def _script_sha(step: str) -> str:
    """该步所依赖的**代码**内容哈希 —— 改了内核代码，指纹自动失效。

    带后处理的步骤（:data:`POST_PROCESSED_STEPS`）额外把编排器自身的哈希算进来：
    那部分代码改了，产物就会变，指纹必须跟着变。
    """
    script = STEP_SCRIPTS.get(step)
    paths = [(PLATFORM_ROOT / "voc_core" / "kernel" / script) if script
             else (PLATFORM_ROOT / VALIDATE_MODULE)]
    if step in POST_PROCESSED_STEPS:
        paths.append(PLATFORM_ROOT / "voc_core" / "pipeline.py")
    return _sha1_text("".join(_sha1_file(p) for p in paths))


def _normalize_params(params: dict[str, Any], job: JobPaths) -> dict[str, Any]:
    """把参数里的路径折叠成 ``<job>/…`` / ``<platform>/…`` / ``<ext>/…``。

    这样换台机器、换个 checkout 目录，同一份参数的指纹仍然相同。
    """
    out: dict[str, Any] = {}
    for k, v in params.items():
        if isinstance(v, (str, Path)) and str(v):
            s = str(v)
            looks_like_path = ("/" in s or "\\" in s or s.endswith((".csv", ".json", ".yaml", ".html", ".md")))
            out[k] = job.rel_or_placeholder(s) if looks_like_path else s
        else:
            out[k] = v
    return out


def _fingerprint(step: str, params: dict[str, Any], inputs: dict[str, str],
                 job: JobPaths) -> str:
    payload = {
        "step": step,
        "script_sha": _script_sha(step),
        "params": _normalize_params(params, job),
        #: 输入产物**内容**的指纹 —— 上游产物一变，这一步自动失效（不需要显式声明依赖）
        "inputs": dict(sorted(inputs.items())),
    }
    return _sha1_text(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _changed(before: dict[str, str], after: dict[str, str]) -> dict[str, str]:
    """跑后新增或内容变化的文件。"""
    return {k: v for k, v in after.items() if before.get(k) != v}


def _intact(job_dir: Path, outputs: dict[str, str],
            stamps: dict[str, Sequence[int]] | None = None) -> bool:
    """上次记录的输出是否还在、内容是否没被手改。

    ``stamps`` 是上次记下的 ``{相对路径: [size, mtime_ns]}``：stat 一字不差就免读盘。
    几千个标注中间件时，这一步把跳过判定从「读 30MB」降到「几千次 stat」。
    手改文件必改 mtime；mtime 变了但内容没变时退化到 sha1，结论仍然正确。
    """
    stamps = stamps or {}
    for rel, sha in (outputs or {}).items():
        p = job_dir / rel
        if rel.endswith("/"):
            # 目录条目：必须重算聚合哈希。**不能**走下面的 stat 短路 —— 目录自身的
            # size/mtime 不随子文件内容变化（Windows 上只有增删顶层条目才会动），
            # 那样手改一个标注件就会被判成"完好"。
            if not p.is_dir() or _dir_sha(p) != sha:
                return False
            continue
        try:
            st = p.stat()
        except OSError:
            return False
        rec = stamps.get(rel)
        if (rec and len(rec) == 2
                and int(rec[0]) == st.st_size and int(rec[1]) == st.st_mtime_ns):
            continue
        if _sha1_file(p) != sha:
            return False
    return True


def _record_outputs(sr: "StepRun", job: JobPaths, outputs: dict[str, str]) -> None:
    """记录产物清单，并顺手存下每个**文件**产物的 ``[size, mtime_ns]``（供下次 stat 短路）。

    目录条目（``rel`` 以 ``/`` 结尾）不存戳：目录的 stat 对子文件改动不敏感，
    存了只会让 :func:`_intact` 漏判。
    """
    sr.outputs = dict(outputs)
    stamps: dict[str, list[int]] = {}
    for rel in sr.outputs:
        if rel.endswith("/"):
            continue
        try:
            st = (job.root / rel).stat()
        except OSError:
            continue
        stamps[rel] = [st.st_size, st.st_mtime_ns]
    sr.stamps = stamps


# --------------------------------------------------------------------------
# 评论收窄（把 SKILL.md 里的一条人肉步骤变成代码）
# --------------------------------------------------------------------------

def narrow_comments(relevant_csv: str | Path, comments_csv: str | Path,
                    out_csv: str | Path) -> dict[str, Any]:
    """按 relevant 主单元收窄次级单元，返回账目。

    SKILL.md 第 338 行的原话是「**评论单元必须收窄到 relevant 帖**：直接喂全量会把
    过期帖、D 桶帖的评论算进来（本例 6,000 → 应只留 3,758）。做法：先用 relevant 集
    过滤出 ``reddit_comments_relevant.csv``」—— 这条「人肉经验」正式成为编排器的一步。
    """
    from voc_core.kernel_common import canonicalize_rows, guess_role

    rel = Path(relevant_csv)
    com = Path(comments_csv)
    if not rel.exists():
        raise PipelineError(f"评论收窄：找不到主单元文件 {rel}")
    if not com.exists():
        raise PipelineError(f"评论收窄：找不到次级单元文件 {com}")

    with rel.open(encoding="utf-8-sig", newline="") as f:
        rdr = csv.DictReader(f)
        rel_cols = list(rdr.fieldnames or [])
        rel_rows = list(rdr)
    with com.open(encoding="utf-8-sig", newline="") as f:
        rdr = csv.DictReader(f)
        com_cols = list(rdr.fieldnames or [])
        com_rows = list(rdr)

    rel_role = guess_role(rel_cols)
    if rel_role == "auto":
        rel_role = "posts"
    canonical_rel = canonicalize_rows(rel_rows, rel_role)
    canonical_com = canonicalize_rows(com_rows, "comments")

    keep: set[str] = set()
    for row in canonical_rel:
        uid = str(row.get("reddit_id") or row.get("unit_id") or "").strip()
        if uid:
            keep.add(uid)

    kept_rows: list[dict[str, Any]] = []
    for raw, can in zip(com_rows, canonical_com):
        parent = str(can.get("post_reddit_id") or can.get("thread_id")
                     or can.get("parent_unit_id") or "").strip()
        if parent and parent in keep:
            kept_rows.append(raw)

    outp = Path(out_csv)
    outp.parent.mkdir(parents=True, exist_ok=True)
    tmp = outp.with_suffix(outp.suffix + ".part")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=com_cols)
        w.writeheader()
        for r in kept_rows:
            w.writerow({k: r.get(k, "") for k in com_cols})
    os.replace(tmp, outp)

    return {
        "applicable": True,
        "relevant_units": len(rel_rows),
        "units_kept": len(keep),
        "comments_total": len(com_rows),
        "comments_kept": len(kept_rows),
        "dropped": len(com_rows) - len(kept_rows),
        "out": str(outp),
    }


def _brand_terms(job: JobPaths) -> list[str]:
    """从 clean 的 ``stats.json`` 取**实际生效**的品牌表。

    用户没给 ``--brands`` 时内核会自动发现，所以读产物比读参数更接近事实
    （与「框架从产物读」同一个道理）。
    """
    p = resolve_artifact_path(job, "stats")
    if not p:
        return []
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [str(t).strip() for t in (data.get("brand_terms") or []) if str(t).strip()]


def rewrite_mentioned_brands(relevant_csv: str | Path, comments_csv: str | Path | None,
                             brand_terms: Sequence[str], *,
                             out_csv: str | Path | None = None) -> dict[str, Any]:
    """把 ``mentioned_brands`` 列重写成**讨论串口径**（标题 + 该帖评论）。

    SKILL.md 第 310 行的原话：「品牌提及必须在标注阶段用讨论串重写 ``mentioned_brands``
    列：analyzer 只按标题算（品牌 10 帖），把它替换成「标题 + 该帖评论」后升到 95 帖。
    **不改这一列，看板品牌页与报告品牌章都会严重低估**」。

    与评论收窄一样，这原本是一条人肉步骤，编排器把它变成流程的一部分。

    匹配规则（机械、可复现）：不区分大小写、要求词边界；输出用 ``brand_terms`` 里的
    **标准写法**、按 ``brand_terms`` 顺序去重连接（与既有产物同构）。
    """
    rel = Path(relevant_csv)
    if not rel.exists():
        return {"applicable": False, "reason": f"没有 relevant.csv（{rel}）"}
    terms = [str(t).strip() for t in (brand_terms or []) if str(t).strip()]
    if not terms:
        return {"applicable": False, "reason": "品牌表为空（--brands 未给且数据里也发现不了）"}

    with rel.open(encoding="utf-8-sig", newline="") as f:
        rdr = csv.DictReader(f)
        cols = list(rdr.fieldnames or [])
        rows = list(rdr)
    if "mentioned_brands" not in cols:
        return {"applicable": False, "reason": "relevant.csv 里没有 mentioned_brands 列"}

    # 该帖的全部次级单元正文（优先用**收窄后**的评论：过期帖、D 桶帖的评论不该算进来）
    by_post: dict[str, list[str]] = {}
    cp = Path(comments_csv) if comments_csv else None
    if cp is not None and cp.exists():
        with cp.open(encoding="utf-8-sig", newline="") as f:
            for c in csv.DictReader(f):
                pid = str(c.get("post_reddit_id") or c.get("thread_id")
                          or c.get("parent_unit_id") or "").strip()
                if pid:
                    by_post.setdefault(pid, []).append(str(c.get("body") or ""))

    pats = [(b, re.compile(r"(?<![a-z0-9])" + re.escape(b.lower()) + r"(?![a-z0-9])"))
            for b in terms]
    before_nonempty = sum(1 for r in rows if str(r.get("mentioned_brands") or "").strip())
    hit_brands: set[str] = set()
    changed = 0
    for r in rows:
        pid = str(r.get("reddit_id") or r.get("unit_id") or "").strip()
        hay = (str(r.get("title") or "") + " " + " ".join(by_post.get(pid, []))).lower()
        hit = [b for b, p in pats if p.search(hay)]
        val = ",".join(hit)
        if val != str(r.get("mentioned_brands") or ""):
            changed += 1
        r["mentioned_brands"] = val
        hit_brands.update(hit)
    after_nonempty = sum(1 for r in rows if str(r.get("mentioned_brands") or "").strip())

    target = Path(out_csv) if out_csv else rel
    tmp = target.parent / (target.name + ".part")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})
    os.replace(tmp, target)

    return {
        "applicable": True,
        "units": len(rows),
        "before_nonempty": before_nonempty,
        "after_nonempty": after_nonempty,
        "changed_rows": changed,
        "brands_hit": len(hit_brands),
        "comment_posts": len(by_post),
        "out": str(target),
    }


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------

def select_steps(*, only: Sequence[str] = (), start: str = "",
                 end: str = "") -> list[str]:
    """按 ``--only`` / ``--from`` / ``--to`` 算出要跑的步骤（顺序恒定）。"""
    steps = list(STEPS)
    if only:
        wanted = [s.strip() for s in only if s and s.strip()]
        unknown = [s for s in wanted if s not in STEPS]
        if unknown:
            raise PipelineError(
                f"未知步骤：{', '.join(unknown)}（可用：{', '.join(STEPS)}）")
        pick = set(wanted)
        return [s for s in steps if s in pick]
    if start:
        if start not in STEPS:
            raise PipelineError(f"--from 未知步骤：{start}（可用：{', '.join(STEPS)}）")
        steps = steps[steps.index(start):]
    if end:
        if end not in STEPS:
            raise PipelineError(f"--to 未知步骤：{end}（可用：{', '.join(STEPS)}）")
        steps = steps[: steps.index(end) + 1]
    return steps


def _log_header(log: Callable[..., None], job: JobPaths, spec: JobSpec,
                steps: Sequence[str]) -> None:
    log("=" * 72)
    log(f"VOC 管线 · {spec.category or '(未指定品类)'} · {len(steps)} 步")
    log(f"job 目录：{job.root}")
    log("=" * 72)


def run_job(spec: JobSpec, job_dir: str | Path, *,
            only: Sequence[str] = (), start: str = "", end: str = "",
            force: str = "", dry_run: bool = False,
            copy_inputs: bool = True, strict: bool = False,
            log: Callable[..., None] = print) -> JobResult:
    """跑一条管线。

    :param force: ``""`` 不强制 ｜ ``"all"`` 全部重跑 ｜ ``"<step>"`` 从该步起强制重跑
    :param strict: 闸门有 error 级发现时也返回非 0（CI 用）
    :param copy_inputs: 把源数据复制进 ``<job>/input/``（自包含；``False`` 则只引用）
    """
    t0 = time.time()
    job = JobPaths(Path(job_dir))
    steps = select_steps(only=only, start=start, end=end)
    if not steps:
        raise PipelineError("没有要跑的步骤")

    parser = _voc_cli().build_parser()

    # ---- dry-run：只解析每步的参数，不落地、不执行 ----
    if dry_run:
        _log_header(log, job, spec, steps)
        for s in steps:
            ns = _ns_for(s, spec, job, parser, allow_missing=True)
            defaults = _defaults_of(parser, s)
            params = _diff_params(ns, defaults)
            log(f"[dry-run] {s:9s} {STEP_LABELS[s]}")
            for k, v in params.items():
                log(f"           {k} = {v}")
        return JobResult(job_dir=job.root, status="dry_run", exit_code=EXIT_OK)

    job.ensure()

    # ---- 输入落地（job 自包含的关键） ----
    orig_input, orig_comments = spec.input, spec.comments
    spec = _stage_inputs(spec, job, copy_inputs=copy_inputs, log=log)

    state = _load_job_state(job.root)
    if state and state.get("schema_version") != JOB_SCHEMA_VERSION:
        # 记录语义变过（见 JOB_SCHEMA_VERSION 的注释）：旧记录的指纹/产物口径不可信，
        # 直接作废、整条重跑。既有产物不删 —— 重算后会原地覆盖，没算到的也不影响。
        log(f"job.json 版本 {state.get('schema_version')} ≠ {JOB_SCHEMA_VERSION}，"
            f"旧记录作废、整条重跑")
        state = None
    state = state or _new_job_state(job, spec, steps)
    state["spec"] = spec.to_dict()
    #: 源数据的**原始**路径（spec.input 落地后已指向 <job>/input/，这里留档便于回查）
    state["source_files"] = {"input": orig_input, "comments": orig_comments}
    #: Stage 7 的验收条件就落在这个字段上：抄进终端即可重放本次分析
    state["reproduce"] = spec.reproduce_command(job.root.as_posix())
    state["steps_order"] = list(steps)
    state["status"] = "running"
    state["updated_at"] = _now()
    _write_job(job, state)

    _log_header(log, job, spec, steps)
    log(f"复现命令：{spec.reproduce_command(job.rel(job.root))}")
    log("")

    result = JobResult(job_dir=job.root, status="running")
    prev_steps: dict[str, Any] = state.setdefault("steps", {})
    force_from = _force_scope(force)

    blocked = False
    gate_failed = False
    for idx, step in enumerate(steps, 1):
        head = f"[{idx}/{len(steps)}] {step:9s} {STEP_LABELS[step]}"
        if blocked:
            sr = StepRun(key=step, status="blocked",
                         note="上游等待人工回填，本步未执行")
            prev_steps[step] = sr.to_dict()
            result.steps[step] = sr
            log(f"{head}  ⊘ 阻塞（上游 waiting_manual）")
            continue

        ns = _ns_for(step, spec, job, parser)
        defaults = _defaults_of(parser, step)
        params = _diff_params(ns, defaults)
        prev = prev_steps.get(step) or {}
        forced = _is_forced(step, force_from)
        #: 输入指纹**精确声明**（见 STEP_INPUT_ATTRS）：只算本步真正读的文件/目录。
        inputs = _collect_inputs(step, ns, job)
        fp = _fingerprint(step, params, inputs, job)

        if (not forced and prev.get("status") == "done"
                and prev.get("fingerprint") == fp
                #: 没有产物记录就绝不跳过 —— 七个阶段每个都至少产出一个文件，
                #: 空清单只可能来自旧记录或声明写漏。这是 :func:`_collect_outputs`
                #: 之外的**第二道**保险：宁可多跑一次，不可漏判"产物完好"。
                and prev.get("outputs")
                and _intact(job.root, prev.get("outputs") or {},
                            prev.get("outputs_stat") or {})):
            sr = StepRun(key=step, status="skipped", fingerprint=fp,
                         script=STEP_SCRIPTS.get(step) or VALIDATE_MODULE,
                         params=_normalize_params(params, job),
                         inputs=prev.get("inputs") or {},
                         outputs=prev.get("outputs") or {},
                         note="指纹未变且产物完好，跳过")
            prev_steps[step] = sr.to_dict()
            result.steps[step] = sr
            result.skipped.append(step)
            log(f"{head}  = 跳过（指纹未变）")
            continue

        log(f"{head}  $ {_cmdline(step, ns)}")
        st = time.time()
        code = _run_step(step, ns)
        elapsed = time.time() - st

        script = STEP_SCRIPTS.get(step) or VALIDATE_MODULE
        sr = StepRun(key=step, exit_code=code, elapsed_s=elapsed, script=script,
                     fingerprint=fp, params=_normalize_params(params, job),
                     inputs=inputs)
        _record_outputs(sr, job, _collect_outputs(step, job))

        if code == EXIT_PENDING:
            sr.status = "waiting_manual"
            sr.note = "任务已入队等待人工回填（退出码 3，不是失败）；回填后重跑同一命令即继续"
            result.waiting.append(step)
            blocked = True
            log(f"{head}  ⏸ 等待人工回填（退出码 3）")
        elif step == "validate" and code == EXIT_FAIL:
            # 闸门是**出口检查**，不产出新结论：退出码 1 只表示「这批产物有
            # 问题」，质量报告已经写出来了。所以这一步本身算完成 —— 要不要
            # 让整体失败，由 --strict 决定（默认不阻断：有 warn/error 级发现
            # 本该被看见，而不是被当成管线崩溃）。
            sr.status = "done"
            sr.note = "闸门结论：不通过（有 error 级发现，质量报告已写出）"
            gate_failed = True
            log(f"{head}  ✓ 完成（{elapsed:.1f}s，闸门结论：不通过）")
        elif code != 0:
            sr.status = "failed"
            sr.note = f"退出码 {code}"
            result.failed.append(step)
            blocked = True
            log(f"{head}  ✗ 失败（退出码 {code}）")
        else:
            sr.status = "done"
            log(f"{head}  ✓ 完成（{elapsed:.1f}s，{len(sr.outputs)} 个产物）")

        # clean 之后的两条后处理 —— 它们都是 SKILL.md 里的**人肉步骤**，编排器接管：
        #   ① 评论收窄（第 338 行）：把次级单元过滤到 relevant 主单元上
        #   ② 品牌列重写（第 310 行）：mentioned_brands 改成「标题 + 该帖评论」口径
        if sr.ok and step == "clean":
            notes2: list[str] = []
            if spec.comments:
                try:
                    info = narrow_comments(resolve_artifact_path(job, "relevant"),
                                           _input_path(job, spec.comments),
                                           job.artifact("comments_relevant"))
                    notes2.append(f"评论收窄：{info['comments_total']} → {info['comments_kept']}"
                                  f"（丢弃 {info['dropped']}）")
                except (PipelineError, OSError, csv.Error) as exc:
                    notes2.append(f"评论收窄跳过：{exc}")
            try:
                bi = rewrite_mentioned_brands(resolve_artifact_path(job, "relevant"),
                                              _comments_for_llm(job, spec),
                                              _brand_terms(job))
                if bi.get("applicable"):
                    notes2.append(f"品牌重写：{bi['before_nonempty']} → "
                                  f"{bi['after_nonempty']} 帖（覆盖 {bi['brands_hit']} 个品牌）")
                else:
                    notes2.append(f"品牌重写跳过：{bi.get('reason')}")
            except (PipelineError, OSError, csv.Error) as exc:
                notes2.append(f"品牌重写跳过：{exc}")
            if notes2:
                sr.note = "；".join(notes2)
                log(f"            └ {sr.note}")
                # 后处理改写了产物（收窄评论、重写品牌列），产物指纹要重收一遍
                _record_outputs(sr, job, _collect_outputs(step, job))

        prev_steps[step] = sr.to_dict()
        result.steps[step] = sr
        state["steps"] = prev_steps
        state["updated_at"] = _now()
        _write_job(job, state)

    # ---- 收口 ----
    result.elapsed_s = time.time() - t0
    if result.waiting:
        status, code = "waiting_manual", EXIT_PENDING
    elif result.failed:
        status, code = "failed", EXIT_FAIL
    else:
        status, code = "done", EXIT_OK

    if strict and gate_failed:
        code = code or EXIT_FAIL

    state["status"] = status
    state["updated_at"] = _now()
    _write_job(job, state)
    result.status = status
    result.exit_code = code

    _print_summary(result, job, log)
    return result


def _diff_params(ns: argparse.Namespace, defaults: dict[str, Any]) -> dict[str, Any]:
    """只保留「与默认值不同」的参数（写进 job.json 与指纹）。"""
    out: dict[str, Any] = {}
    for k, v in vars(ns).items():
        if k in ("func", "cmd"):
            continue
        if k not in defaults:
            continue
        if v != defaults[k]:
            out[k] = v
    return out


def _cmdline(step: str, ns: argparse.Namespace) -> str:
    script = STEP_SCRIPTS.get(step)
    if script:
        return f"{script} …"
    return f"voc {step} …"


def _run_step(step: str, ns: argparse.Namespace) -> int:
    """执行一步：直接调 ``voc.py`` 的 ``cmd_*``（参数拼装只有那一份实现）。"""
    func = getattr(ns, "func", None)
    if func is None:
        raise PipelineError(f"步骤 {step} 没有对应的命令函数")
    try:
        return int(func(ns) or 0)
    except SystemExit as exc:  # 内核里仍有少量 SystemExit（配置错误）
        code = exc.code
        return int(code) if isinstance(code, int) else (EXIT_FAIL if code else EXIT_OK)


def _stage_inputs(spec: JobSpec, job: JobPaths,
                  *, copy_inputs: bool, log: Callable[..., None]) -> JobSpec:
    """把源数据落到 ``<job>/input/`` 并改写 spec 路径；非 CSV 先归一化成内核能吃的 CSV。

    内核的 clean 步骤只读 CSV（旧 schema 的 ``units_legacy_*.csv``），但 Web 表单允许
    上传 xlsx（第三方导出最常见）。所以这里在输入落地后、clean 之前补一道归一化 ——
    转换产物也落在 ``<job>/input/``（进 job 目录、进指纹体系，换数据自动重算）。
    识别不出源时明确报错（配置错误语义），而不是让 clean 把 xlsx 当 csv 读、炸出
    一句看不懂的 UnicodeDecodeError。
    """
    from dataclasses import replace

    staged = replace(spec)
    for attr in ("input", "comments"):
        src = getattr(spec, attr) or ""
        if not src:
            continue
        sp = Path(src)
        if not sp.exists():
            raise PipelineError(f"{attr} 指向的文件不存在：{src}")
        dst = job.input / sp.name
        if sp.resolve() != dst.resolve():
            if copy_inputs:
                job.input.mkdir(parents=True, exist_ok=True)
                if not dst.exists() or _sha1_file(dst) != _sha1_file(sp):
                    shutil.copy2(sp, dst)
                    log(f"[input] 落地 {sp.name}（{sp.stat().st_size:,} 字节）")
        setattr(staged, attr, str(dst))
    return _convert_staged_inputs(staged, job, log)


def _convert_staged_inputs(spec: JobSpec, job: JobPaths,
                           log: Callable[..., None]) -> JobSpec:
    """对落地后的 xlsx/json/jsonl 主/评论文件做归一化，改写 spec 路径指向转换出的 CSV。"""
    from dataclasses import replace

    out = replace(spec)
    for attr in ("input", "comments"):
        src = getattr(out, attr) or ""
        if not src:
            continue
        sp = Path(src)
        if sp.suffix.lower() == ".csv":
            continue  # 内核能直接吃 CSV，无需转换
        converted = _convert_one(sp, job, spec, log, is_comments=(attr == "comments"))
        if converted is not None:
            setattr(out, attr, str(converted))
    # 自动喂评论：主文件若是「帖+评」混装导出（如 Reddit 批量导出 xlsx），``cmd_convert``
    # 已顺手把评论半拆成 ``units_legacy_comments.csv`` 落在 ``input/``。用户没单独传评论时，
    # 自动并入分析 —— 否则这半数据（往往是总量 98% 的评论，VOC 金矿）会被整体弃用。
    if not out.comments:
        auto_comments = job.input / "units_legacy_comments.csv"
        if auto_comments.exists():
            out = replace(out, comments=str(auto_comments))
            log(f"[input] 自动并入选主文件拆出的评论单元：{auto_comments.name}")
    return out


def _convert_one(src: Path, job: JobPaths, spec: JobSpec, log: Callable[..., None],
                 *, is_comments: bool) -> Path | None:
    """用 ``voc.py convert`` 把一份非 CSV 文件归一化成旧 schema CSV。

    产物直接落在 ``<job>/input/``（不建子目录）：这样后续 clean 的 ``--profile auto``
    能从同目录的 ``ingest_report.json`` 推断出正确的源画像（amazon/wayfair…），
    ``_input_path`` 也能按文件名直接定位。返回改写后的主单元/评论 CSV 路径；
    评论文件若没产生评论单元则返回 ``None``（保留原文件，不强行替换）。
    """
    cli = _voc_cli()
    out_dir = job.input
    out_dir.mkdir(parents=True, exist_ok=True)
    ns = argparse.Namespace(
        files=[str(src)],
        source=spec.source or "auto",
        out_dir=str(out_dir),
        category=spec.category or "",
        default_channel="",
        default_subreddit="",
        default_brand="",
        source_note=spec.source_note or "",
    )
    try:
        cli.cmd_convert(ns)
    except SystemExit as exc:  # cmd_convert 用 SystemExit 报错（识别不出源 / 无有效单元）
        raise PipelineError(
            f"源数据归一化失败（{src.name}）：{exc}\n"
            f"  提示：若表单的「数据源」下拉框为空，请在提交时显式选择 "
            f"amazon / wayfair / reddit。"
        )

    if is_comments:
        target = out_dir / "units_legacy_comments.csv"
        if not target.exists():
            log(f"[input] {src.name} 未产生评论单元（units_legacy_comments.csv 缺失），保留原文件")
            return None
    else:
        target = None
        for name in ("units_legacy_reviews.csv", "units_legacy_posts.csv"):
            p = out_dir / name
            if p.exists():
                target = p
                break
        if target is None:
            raise PipelineError(
                f"源数据归一化后没有可分析的主单元（{src.name}）："
                f"未生成 units_legacy_reviews.csv / units_legacy_posts.csv。"
            )
    log(f"[input] 归一化 {src.name} → {target.name}")
    return target


def _force_scope(force: str) -> str:
    """把 ``--force`` 的取值归一：``""`` / ``"all"`` / ``"<step>"``。"""
    if not force:
        return ""
    f = force.strip()
    if f in ("", "all", "true", "1"):
        return "all"
    if f not in STEPS:
        raise PipelineError(f"--force 未知步骤：{f}（可用：all, {', '.join(STEPS)}）")
    return f


def _is_forced(step: str, scope: str) -> bool:
    if scope == "all":
        return True
    if scope and step in STEPS:
        # 从该步起全部强制（它的下游必然也要重算）
        return STEPS.index(step) >= STEPS.index(scope)
    return False


def _print_summary(result: JobResult, job: JobPaths, log: Callable[..., None]) -> None:
    log("")
    log("-" * 72)
    n_done = len([s for s in result.steps.values() if s.status == "done"])
    n_skip = len(result.skipped)
    log(f"管线{'完成' if result.status == 'done' else result.status}"
        f" —— 执行 {n_done} 步，跳过 {n_skip} 步，用时 {result.elapsed_s:.1f}s")
    if result.waiting:
        log(f"⏸ 等待人工回填：{', '.join(result.waiting)}")
        log("  看队列：python voc.py llm pending ｜ 回填：python voc.py llm fill <task_id>")
        log(f"  回填后重跑：{job.root}")
    if result.failed:
        log(f"✗ 失败步骤：{', '.join(result.failed)}")
    log(f"job 状态：{job.job_file}")
    log("-" * 72)


# --------------------------------------------------------------------------
# job.json 读写
# --------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _new_job_state(job: JobPaths, spec: JobSpec, steps: Sequence[str]) -> dict[str, Any]:
    return {
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": job.root.name,
        "job_dir": job.root.as_posix(),
        "created_at": _now(),
        "updated_at": _now(),
        "status": "pending",
        "spec": spec.to_dict(),
        "reproduce": spec.reproduce_command(job.root.as_posix()),
        "python": sys.version.split()[0],
        "steps_order": list(steps),
        "steps": {},
    }


def load_job(job_dir: str | Path | JobPaths) -> dict[str, Any] | None:
    if isinstance(job_dir, JobPaths):  # 让调用方直接传 job 句柄
        job_dir = job_dir.root
    p = Path(job_dir) / JOB_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


_load_job_state = load_job


#: Windows 上杀软 / 备份服务会短暂持有刚写出的文件；覆盖时 ``os.replace`` 会
#: 报 ``PermissionError [WinError 5]``，几百毫秒后自行消失。原子替换的意义就是
#: 「要么旧的、要么新的」，所以这里退避重试而不是降级成非原子写。
_LOCK_ERRNOS = {13, 11, 32, 33}          # EACCES / EAGAIN / WinError 32 / WinError 33


def atomic_replace(tmp: Path | str, dst: Path | str, *,
                   tries: int = 10, delay: float = 0.25) -> None:
    """``os.replace`` + 瞬时锁退避重试。"""
    last: Exception | None = None
    for attempt in range(max(1, tries)):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError as exc:      # 被占用/只读 —— 最典型的是瞬时锁
            last = exc
            if getattr(exc, "errno", None) not in _LOCK_ERRNOS and attempt > 1:
                raise
            time.sleep(delay * (attempt + 1))
    assert last is not None
    raise last


def _write_job(job: JobPaths, state: dict[str, Any]) -> None:
    """原子写 job.json（先写 .part 再 replace），避免中途读取到半截文件。"""
    job.root.mkdir(parents=True, exist_ok=True)
    tmp = job.job_file.parent / (job.job_file.name + ".part")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    atomic_replace(tmp, job.job_file)


def list_jobs(runs_dir: str | Path) -> list[dict[str, Any]]:
    """列出 ``runs/`` 下的所有 job 摘要（按修改时间倒序）。"""
    d = Path(runs_dir)
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(d.glob(f"*/{JOB_FILE}")):
        st = load_job(p.parent)
        if not st:
            continue
        steps = st.get("steps") or {}
        out.append({
            "job_id": st.get("job_id") or p.parent.name,
            "job_dir": p.parent.as_posix(),
            "status": st.get("status", "?"),
            "category": (st.get("spec") or {}).get("category", ""),
            "frame": (st.get("spec") or {}).get("frame", "") or "consumer",
            "updated_at": st.get("updated_at", ""),
            "done": sum(1 for s in steps.values() if s.get("status") in ("done", "skipped")),
            "total": len(st.get("steps_order") or STEPS),
            "mtime": p.stat().st_mtime,
        })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


# --------------------------------------------------------------------------
# 批量
# --------------------------------------------------------------------------

def run_batch(specs: Sequence[tuple[str, JobSpec]], runs_dir: str | Path, *,
              keep_going: bool = True, **kwargs: Any) -> tuple[list[JobResult], int]:
    """串行跑多个 job（Stage 7 §4「批量模式」）。

    :param specs: ``[(job_id, JobSpec), …]``
    :return: ``(结果列表, 汇总退出码)`` —— 全部成功为 0，有等待为 3，有失败为 1
    """
    results: list[JobResult] = []
    for job_id, spec in specs:
        job_dir = Path(runs_dir) / job_id
        try:
            results.append(run_job(spec, job_dir, **kwargs))
        except PipelineError as exc:
            log = kwargs.get("log", print)
            log(f"[batch] {job_id} 配置错误：{exc}")
            results.append(JobResult(job_dir=job_dir, status="failed",
                                     exit_code=EXIT_CONFIG))
        if results[-1].exit_code != 0 and not keep_going:
            break
    codes = {r.exit_code for r in results}
    if codes & {EXIT_FAIL, EXIT_CONFIG}:
        return results, EXIT_FAIL
    if EXIT_PENDING in codes:
        return results, EXIT_PENDING
    return results, EXIT_OK
