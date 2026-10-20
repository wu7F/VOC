#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析框架（Stage 5）：让「分析什么」成为配置。

**改造前的问题**：能分析什么，是代码里的两个常量 —— ``design_tags.DIMENSIONS`` 的
8 个键，加上 ``aggregate_annotations`` 里写死的 ``purchase_status`` 归桶闸门。
消费品品类刚好合适，换个行业就不对了：

* B2B / 组织采购：要按**决策角色**分桶（决策者 / 影响者 / 使用者），根本没有「购买状态」；
* 订阅制 App：要按**付费档位**分桶（免费 / 订阅 / 流失）；
* 硬件配件：可能只关心**兼容机型**，连画像分桶都不需要。

**改造后的形状**：``frames/<name>.yaml`` 声明三件事 ——

1. ``dimensions``：启用哪些维度、每个维度的中文名与标签 category；
2. ``segmentation_axis``：**用哪个分类器分桶、桶怎么定义、桶怎么映射到画像维度**；
   闸门逻辑（恰好一个标签命中才生效，命中 0 或 ≥2 判 unknown）原样保留，
   只是**轴本身**从写死的 ``purchase_status`` 变成可配的一项；
3. ``modules``：看板模块开关（``rating_distribution: auto`` 表示按数据可用性自动开关）。

**为什么要跟 ``--profile``（Stage 3 的源画像）分开**：两者正交且都叫 profile 会出事。
``--profile`` 回答「数据从哪来」（Reddit 的「帖子 / 子版块」还是电商的「评论 / 站点」，
决定**用户可见文案与能力开关**）；``--frame`` 回答「分析什么」（维度与分桶轴，决定
**分析结构**）。同一份亚马逊评论，可以按购买状态分析，也可以按决策角色分析 ——
所以是两个参数，不是两个取值。目录也刻意分开：``frames/`` vs ``voc_core/source_profile.py``。

**零回归约定**：不带 ``--frame`` 时加载内置 ``consumer``，其渲染结果与平台化之前
**逐字节相同**。这一点由 ``tests/test_frame.py`` 的文案等价性用例守着。
"""

from __future__ import annotations

import os
import string
from dataclasses import dataclass, field
from pathlib import Path

from voc_core import classifiers as C
from voc_core import miniyaml

__all__ = [
    "FrameError", "DimSpec", "Bucket", "Axis", "AnalysisFrame",
    "FRAME_DIR", "DEFAULT_FRAME", "load_frame", "available_frames", "frame_path",
    "frame_from_product",
]

#: 框架文件目录（与 prompts/ 同级，随平台一起分发）
FRAME_DIR = Path(__file__).resolve().parents[1] / "frames"
#: 默认框架：不带 --frame 时用它，行为等同平台化之前
DEFAULT_FRAME = "consumer"

#: 顶层允许的键。**未知键一律报错** —— 手滑写成 ``dimension:`` 而静默忽略，
#: 症状是「我明明配了但没生效」，比报错难查一个数量级。
_TOP_KEYS = frozenset({"name", "label", "description", "sample_tags_per_dim",
                       "dimensions", "segmentation_axis", "modules", "notes",
                       "category_legend", "prompt"})
_DIM_KEYS = frozenset({"id", "label", "category", "kind", "persona_bucket",
                       "analysis_hint", "experience", "deprecated", "optional",
                       "prompt_line"})
_AXIS_KEYS = frozenset({"dim", "label", "classifier", "distribution_key", "unit_counts_key",
                        "matrix_bucket", "unknown_bucket", "buckets", "texts", "dashboard",
                        "prompt", "prompt_axis_question"})
_BUCKET_KEYS = frozenset({"id", "label_cn", "short_label", "judge_label", "persona_short",
                          "persona_label", "persona_condition", "tags", "excluded", "dashboard"})


class FrameError(ValueError):
    """框架配置有问题。消息里尽量带上「哪个文件 / 哪个键」。"""


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DimSpec:
    """一个维度。

    ``kind``：``generative``（标签值由 LLM 归纳）/ ``classifier``（脚本注入固定分类器）。
    """

    id: str
    label: str
    category: str = ""
    kind: str = "generative"
    persona_bucket: str = ""      # 非空 = 这个维度是某桶的人群画像
    analysis_hint: str = ""       # 注入该维度各标签 thoughts 的【判定前置】前缀
    experience: bool = False      # 参与情感/满意度统计
    deprecated: bool = False      # 旧标签集里可能有，一律忽略、不产出
    optional: bool = False        # 校验时允许缺失
    prompt_line: str = ""         # 该维度在 design_tags 提示词里的说明行（可含 ${dim_id} 等变量）

    def to_dict(self) -> dict:
        d = {"id": self.id, "label": self.label, "category": self.category,
             "kind": self.kind}
        for k in ("persona_bucket", "analysis_hint", "prompt_line"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        for k in ("experience", "deprecated", "optional"):
            if getattr(self, k):
                d[k] = True
        return d


@dataclass(frozen=True)
class Bucket:
    """一个分桶。

    同一个桶在不同句子里有三种叫法，全部**显式配置**而不是代码里裁字符串
    （裁剪脆弱且不可配）：

    * ``label_cn`` —— 完整名，分布图与表格里用（「已购买者」）；
    * ``short_label`` —— 判定句式里用（「已购 / 未购 / 无法判断」）；
    * ``persona_short`` —— 画像小结里用（「购买者 / 未购买者」）；
    * ``persona_label`` —— 该桶对应画像维度的中文名，**加载时自动从维度反查**，
      YAML 里通常不用写（「购买者人群画像」）。
    """

    id: str
    label_cn: str
    short_label: str = ""
    judge_label: str = ""
    persona_short: str = ""
    persona_label: str = ""
    persona_condition: str = ""   # 标注规则里那句话的条件，如「已购买」「尚未购买」
    tags: tuple[str, ...] = ()
    excluded: bool = False        # 不进画像桶、不进分布图（只计数并披露）
    dashboard: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"id": self.id, "label_cn": self.label_cn,
             "short_label": self.short_label or self.label_cn,
             "judge_label": self.judge_label or self.label_cn,
             "tags": list(self.tags)}
        for k in ("persona_short", "persona_label", "persona_condition"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if self.excluded:
            d["excluded"] = True
        if self.dashboard:
            d["dashboard"] = dict(self.dashboard)
        return d


@dataclass
class Axis:
    """分段轴：拿哪个分类器把单元分进哪几个桶，桶又怎么映射到画像维度。"""

    dim: str
    label: str
    classifier: str
    buckets: tuple[Bucket, ...] = ()
    distribution_key: str = ""
    unit_counts_key: str = ""
    matrix_bucket: str = ""
    unknown_bucket: str = ""
    texts: dict = field(default_factory=dict)
    dashboard: dict = field(default_factory=dict)
    prompt: dict = field(default_factory=dict)
    #: 标注规则里那句「先判断发言者___」的填空，如「是否已购买该品类产品」
    prompt_axis_question: str = ""

    # -- 派生 ---------------------------------------------------------------
    def bucket(self, bid: str) -> Bucket | None:
        for b in self.buckets:
            if b.id == bid:
                return b
        return None

    @property
    def active_buckets(self) -> tuple[Bucket, ...]:
        return tuple(b for b in self.buckets if not b.excluded)

    @property
    def excluded_bucket(self) -> Bucket | None:
        return next((b for b in self.buckets if b.excluded), None)

    @property
    def bucket_list(self) -> str:
        """``已购 / 未购 / 无法判断`` —— 全部桶（含被排除的那个）。"""
        return " / ".join(b.short_label or b.label_cn for b in self.buckets)

    @property
    def bucket_full_list(self) -> str:
        return " / ".join(b.label_cn for b in self.buckets)

    @property
    def persona_list(self) -> str:
        return " / ".join(b.persona_short or b.label_cn for b in self.active_buckets)

    @property
    def judge_list(self) -> str:
        """``已购买 / 未购买 / 无法判断`` —— 判定句里的叫法（与短名 ``已购`` 又不同）。"""
        return " / ".join(b.judge_label or b.label_cn for b in self.buckets)

    @property
    def distribution_buckets(self) -> tuple[Bucket, ...]:
        return self.active_buckets

    def to_dict(self) -> dict:
        return {
            "dim": self.dim, "label": self.label, "classifier": self.classifier,
            "distribution_key": self.distribution_key,
            "unit_counts_key": self.unit_counts_key,
            "matrix_bucket": self.matrix_bucket,
            "unknown_bucket": self.unknown_bucket,
            "prompt_axis_question": self.prompt_axis_question,
            "buckets": [b.to_dict() for b in self.buckets],
            "texts": dict(self.texts),
            "dashboard": dict(self.dashboard),
            "prompt": dict(self.prompt),
        }

    @staticmethod
    def from_dict(d: dict | None) -> "Axis | None":
        """从产物里的 ``analysis_frame.axis`` 还原（下游看板 / 报告 / 闸门用）。"""
        if not d:
            return None
        return Axis(
            dim=d.get("dim", ""), label=d.get("label", ""),
            classifier=d.get("classifier", ""),
            buckets=tuple(Bucket(
                id=b.get("id", ""), label_cn=b.get("label_cn", ""),
                short_label=b.get("short_label", ""),
                judge_label=b.get("judge_label", ""),
                persona_short=b.get("persona_short", ""),
                persona_label=b.get("persona_label", ""),
                persona_condition=b.get("persona_condition", ""),
                tags=tuple(b.get("tags") or ()),
                excluded=bool(b.get("excluded")),
                dashboard=dict(b.get("dashboard") or {}),
            ) for b in (d.get("buckets") or [])),
            distribution_key=d.get("distribution_key", ""),
            unit_counts_key=d.get("unit_counts_key", ""),
            matrix_bucket=d.get("matrix_bucket", ""),
            unknown_bucket=d.get("unknown_bucket", ""),
            prompt_axis_question=d.get("prompt_axis_question", ""),
            texts=dict(d.get("texts") or {}),
            dashboard=dict(d.get("dashboard") or {}),
            prompt=dict(d.get("prompt") or {}),
        )

    # -- 文案渲染 -----------------------------------------------------------
    def render(self, template: str, **ctx) -> str:
        """渲染一条文案模板（``${var}`` 语法，与 prompts/ 一致）。

        严格模式：变量缺失直接报错。宁可炸在配置上，也别把
        ``${axis}判为…`` 原样印进交付给客户的报告里。
        """
        try:
            return string.Template(template).substitute(**ctx)
        except KeyError as exc:
            raise FrameError(
                f"框架文案 {template[:30]!r}… 里的变量 {exc.args[0]!r} 没有人提供；"
                f"可用变量：{'、'.join(sorted(ctx))}") from exc

    def _render_all(self, src: dict, base: dict) -> dict:
        """渲染一组文案，允许组内互相引用（最多 3 趟）。

        为什么需要：``matrix_subtitle`` 的尾部要拼 ``matrix_note``，而后者本身
        也是一条要渲染的模板（`` | 仅${matrix_bucket}${unit}入矩阵…``）。
        一趟渲染做不到「先出 A 再拿 A 拼 B」，写死在代码里又会把文案从配置
        搬回 Python。多趟渲染既保住"文案全在 YAML 里"，又让缺失变量照样报错
        ——最后一轮还渲染不出来的模板，用它的原始报错抛出去。

        ``base`` 里已有的键**优先**：调用方显式传进来的值（例如按「有无次级单元」
        选好的那一版 note）不该被 YAML 里的同名模板覆盖。
        """
        ctx = dict(base)
        out: dict = {}
        pending = dict(src)
        for _ in range(3):
            if not pending:
                break
            still: dict = {}
            for k, v in pending.items():
                try:
                    out[k] = self.render(v, **ctx)
                except FrameError:
                    still[k] = v
            for k, v in out.items():
                ctx.setdefault(k, v)
            if len(still) == len(pending) and still:
                self.render(next(iter(still.values())), **ctx)   # 抛真错误
            pending = still
        return out

    def render_text(self, key: str, **ctx) -> str:
        """渲染**单条** ``texts`` 文案（不把整组都算一遍）。

        用途是 ``scope_rule`` / ``excluded_note`` 这类「只有本句要出、句内也不引用
        同组其它文案」的键。**别拿它渲染会互引的句子** —— 例如看板的
        ``matrix_subtitle`` 尾部要拼 ``matrix_note``，那必须整组渲染
        （:meth:`render_dashboard`），单条渲染会因为拿不到同组的值而报缺变量。

        变量找不到就按 :meth:`render` 的严格模式抛错：宁可炸在配置上，也别把
        ``${axis}判为…`` 原样印进交付给客户的报告里。
        """
        tpl = self.texts.get(key, "")
        if not tpl:
            return ""
        base = self._base_ctx(**ctx)
        base.update(ctx)
        return self.render(tpl, **base)

    def render_texts(self, **ctx) -> dict:
        base = self._base_ctx(**ctx)
        base.update(ctx)
        return self._render_all(self.texts, base)

    def render_dashboard(self, **ctx) -> dict:
        """渲染看板文案。

        **刻意不注入 texts 组**：``matrix_note`` 在两组里同名不同值 ——
        报告的矩阵小节说「仅已购**帖**入矩阵」，看板卡片说「仅已购**帖子**入矩阵」
        （主单元名在报告里用简称、看板里用全称）。注入会互相覆盖，
        症状是看板副标题里悄悄少一个字。
        """
        base = self._base_ctx(**ctx)
        base.update(ctx)
        return self._render_all(self.dashboard, base)

    def _base_ctx(self, **ctx) -> dict:
        """渲染文案时**总是**可用的变量。少一个就在这里补，别在调用点拼。

        命名约定：``bucketN`` = 第 N 个**非排除**桶的短名，``bucketN_full`` = 完整名，
        ``personaN`` = 该桶对应画像维度的中文名。``persona_list`` = 全部画像桶简称。
        """
        active = self.active_buckets
        base = {
            "axis": self.label,
            "axis_dim": self.dim,
            "bucket_list": self.bucket_list,
            "bucket_full_list": self.bucket_full_list,
            "judge_list": self.judge_list,
            "persona_list": self.persona_list,
            "excluded": (self.excluded_bucket.short_label if self.excluded_bucket else ""),
            "excluded_full": (self.excluded_bucket.label_cn if self.excluded_bucket else ""),
            # 矩阵相关文案用的是「已购」这种判定短名（不是「购买者」那种画像简称）：
            # 「已购人群画像 × 功能」「仅已购帖入矩阵」「不含未购 / 状态不明人群」
            "matrix_bucket": self._bucket_name(self.matrix_bucket, "short_label"),
            "matrix_bucket_full": self._bucket_name(self.matrix_bucket, "label_cn"),
            "matrix_bucket_persona": self._bucket_name(self.matrix_bucket, "persona_label"),
            # 除进矩阵那个桶之外的非排除桶（「未购」/「影响者 / 使用者」）：
            # 桶数从 2 变 3 时，写死「${bucket1}人群」的句子就不对了
            "other_buckets": " / ".join(
                b.short_label or b.label_cn
                for b in self.active_buckets if b.id != self.matrix_bucket),
        }
        for i, b in enumerate(active):
            base[f"bucket{i}"] = b.short_label or b.label_cn
            base[f"bucket{i}_full"] = b.label_cn
            base[f"persona{i}"] = b.persona_label or (b.persona_short or b.label_cn)
            base[f"persona{i}_short"] = b.persona_short or b.label_cn
        # 看板「分析框架：A → B → …」那行：把配置里的逗号列表渲染成箭头串
        items = str(self.dashboard.get("framework_items") or "")
        base["framework_items"] = " &rarr; ".join(x.strip() for x in items.split(",") if x.strip())
        return base

    def _bucket_name(self, bid: str, attr: str) -> str:
        b = self.bucket(bid)
        return (getattr(b, attr, "") or b.label_cn) if b else ""


@dataclass
class AnalysisFrame:
    """一套分析框架。"""

    name: str
    label: str = ""
    description: str = ""
    dimensions: tuple[DimSpec, ...] = ()
    axis: Axis | None = None
    modules: dict = field(default_factory=dict)
    sample_tags_per_dim: int = 3
    #: 标签 category → 中文组名（提示词里「取值严格为以下 N 类之一：user（人群画像）…」）
    category_legend: dict = field(default_factory=dict)
    #: 提示词片段（purpose_line / axis_note / persona_requirements / json_shape …）
    prompt: dict = field(default_factory=dict)
    source: str = ""

    # -- 提示词渲染 ---------------------------------------------------------
    def render_prompt(self, template: str, **ctx) -> str:
        """渲染一条提示词模板（严格模式，变量缺失即报错）。"""
        try:
            return string.Template(template).substitute(**ctx)
        except KeyError as exc:
            raise FrameError(
                f"框架 {self.name} 的提示词模板 {template[:40]!r}… 里的变量 "
                f"{exc.args[0]!r} 没有人提供；可用变量：{'、'.join(sorted(ctx))}") from exc

    # -- 查询 ---------------------------------------------------------------
    @property
    def active_dims(self) -> tuple[DimSpec, ...]:
        return tuple(d for d in self.dimensions if not d.deprecated)

    def dim(self, did: str) -> DimSpec | None:
        return next((d for d in self.dimensions if d.id == did), None)

    def dim_ids(self, *, include_deprecated: bool = False) -> list[str]:
        """按声明顺序返回维度 id。

        **顺序有意义**：标签集与看板的维度顺序都跟它走。
        """
        src = self.dimensions if include_deprecated else self.active_dims
        return [d.id for d in src]

    def labels(self, *, include_deprecated: bool = False) -> dict[str, str]:
        src = self.dimensions if include_deprecated else self.active_dims
        return {d.id: d.label for d in src}

    def categories(self) -> dict[str, str]:
        return {d.id: d.category for d in self.active_dims}

    def classifier_dims(self) -> tuple[DimSpec, ...]:
        return tuple(d for d in self.active_dims if d.kind == "classifier")

    def generative_dims(self) -> tuple[DimSpec, ...]:
        return tuple(d for d in self.active_dims if d.kind != "classifier")

    def deprecated_dims(self) -> set[str]:
        return {d.id for d in self.dimensions if d.deprecated}

    def experience_dims(self) -> set[str]:
        return {d.id for d in self.active_dims if d.experience}

    def persona_dims(self) -> dict[str, str]:
        """``{画像维度 id: 桶 id}``。**归桶闸门的映射表**。"""
        return {d.id: d.persona_bucket for d in self.active_dims if d.persona_bucket}

    def persona_dim_ids(self) -> list[str]:
        return list(self.persona_dims())

    def axis_of(self) -> Axis | None:
        return self.axis

    def to_metadata(self) -> dict:
        """写进产物（``tagset.analysis_frame`` / ``dimensions.analysis_frame``）。

        **产物自描述**是这里的关键设计：框架信息只需在第一步给一次，
        标注 / 聚合 / 看板 / 报告都从上游产物读，不必各自再挂一个 ``--frame``
        参数并祈求它们保持一致。

        输出刻意与 ``frames/*.yaml`` **同构**（同样用 ``segmentation_axis`` 键名、
        deprecated 维度就标在 dimensions 里）—— 于是 ``frame_from_product()``
        能原样读回来，用户也可以把一份产物里的框架直接存成 yaml 复用。
        """
        meta = {
            "name": self.name, "label": self.label, "description": self.description,
            "sample_tags_per_dim": self.sample_tags_per_dim,
            "category_legend": dict(self.category_legend),
            "dimensions": [d.to_dict() for d in self.dimensions],
            "modules": dict(self.modules),
            "prompt": dict(self.prompt),
        }
        if self.axis is not None:
            meta["segmentation_axis"] = self.axis.to_dict()
        return meta


# --------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------

def available_frames() -> list[str]:
    """内置框架名（frames/ 目录下的 yaml/json，不含下划线开头的草稿）。"""
    if not FRAME_DIR.is_dir():
        return []
    out = []
    for p in sorted(FRAME_DIR.iterdir()):
        if p.suffix.lower() in (".yaml", ".yml", ".json") and not p.name.startswith("_"):
            out.append(p.stem)
    return out


def frame_path(name: str) -> Path | None:
    for suf in (".yaml", ".yml", ".json"):
        p = FRAME_DIR / f"{name}{suf}"
        if p.exists():
            return p
    return None


def _from_dict(d: dict, source: str = "") -> AnalysisFrame:
    if not isinstance(d, dict):
        raise FrameError(f"{source or '框架'} 的顶层必须是「键: 值」映射，实际是 {type(d).__name__}")

    unknown = set(d) - _TOP_KEYS
    if unknown:
        raise FrameError(
            f"{source or '框架'} 里有不认识的顶层键：{'、'.join(sorted(unknown))}；"
            f"可用的是 {'、'.join(sorted(_TOP_KEYS))}")

    name = str(d.get("name") or "").strip()
    if not name:
        raise FrameError(f"{source or '框架'} 缺少必填的 name")

    # -- dimensions --------------------------------------------------------
    raw_dims = d.get("dimensions")
    if not isinstance(raw_dims, list) or not raw_dims:
        raise FrameError(f"框架 {name} 的 dimensions 不能为空（分析什么由它决定）")
    dims: list[DimSpec] = []
    seen: set[str] = set()
    for i, rd in enumerate(raw_dims):
        if not isinstance(rd, dict):
            raise FrameError(f"框架 {name} 的 dimensions[{i}] 必须是映射")
        unk = set(rd) - _DIM_KEYS
        if unk:
            raise FrameError(f"框架 {name} 的 dimensions[{i}] 有不认识的键："
                             f"{'、'.join(sorted(unk))}；可用的是 {'、'.join(sorted(_DIM_KEYS))}")
        did = str(rd.get("id") or "").strip()
        if not did:
            raise FrameError(f"框架 {name} 的 dimensions[{i}] 缺少 id")
        if did in seen:
            raise FrameError(f"框架 {name} 的维度 id 重复：{did}")
        seen.add(did)
        kind = str(rd.get("kind") or "generative").strip()
        if kind not in ("generative", "classifier"):
            raise FrameError(f"框架 {name} 的维度 {did} 的 kind 只能是 generative / classifier，"
                             f"实际是 {kind!r}")
        dims.append(DimSpec(
            id=did,
            label=str(rd.get("label") or did),
            category=str(rd.get("category") or ""),
            kind=kind,
            persona_bucket=str(rd.get("persona_bucket") or ""),
            analysis_hint=str(rd.get("analysis_hint") or ""),
            experience=bool(rd.get("experience")),
            deprecated=bool(rd.get("deprecated")),
            optional=bool(rd.get("optional")),
            prompt_line=str(rd.get("prompt_line") or ""),
        ))

    # -- segmentation_axis -------------------------------------------------
    axis = None
    raw_axis = d.get("segmentation_axis")
    if raw_axis is not None:
        if not isinstance(raw_axis, dict):
            raise FrameError(f"框架 {name} 的 segmentation_axis 必须是映射（不想要分段轴就整段删掉）")
        unk = set(raw_axis) - _AXIS_KEYS
        if unk:
            raise FrameError(f"框架 {name} 的 segmentation_axis 有不认识的键："
                             f"{'、'.join(sorted(unk))}；可用的是 {'、'.join(sorted(_AXIS_KEYS))}")
        axis = _parse_axis(raw_axis, name, dims)

    frame = AnalysisFrame(
        name=name,
        label=str(d.get("label") or name),
        description=str(d.get("description") or "").strip(),
        dimensions=tuple(dims),
        axis=axis,
        modules=dict(d.get("modules") or {}),
        sample_tags_per_dim=int(d.get("sample_tags_per_dim") or 3),
        category_legend={str(k): str(v) for k, v in (d.get("category_legend") or {}).items()},
        prompt={str(k): str(v) for k, v in (d.get("prompt") or {}).items()},
        source=source,
    )
    _validate_cross(frame)
    return frame


def _parse_axis(ra: dict, fname: str, dims: list[DimSpec]) -> Axis:
    adim = str(ra.get("dim") or "").strip()
    if not adim:
        raise FrameError(f"框架 {fname} 的 segmentation_axis 缺少 dim")
    spec = next((x for x in dims if x.id == adim), None)
    if spec is None:
        raise FrameError(f"框架 {fname} 的 segmentation_axis.dim={adim} 不在 dimensions 里")
    if spec.kind != "classifier":
        raise FrameError(
            f"框架 {fname} 的分段轴维度 {adim} 必须声明 kind: classifier —— "
            f"分段轴是脚本注入的固定分类器，不能交给 LLM 自由生成"
            f"（口径漂移会让份额数字悄悄不可比）")

    cname = str(ra.get("classifier") or adim).strip()
    try:
        cdef = C.get_classifier(cname)
    except KeyError as exc:
        raise FrameError(f"框架 {fname}：{exc.args[0]}") from exc

    raw_buckets = ra.get("buckets")
    if not isinstance(raw_buckets, list) or not raw_buckets:
        raise FrameError(f"框架 {fname} 的 segmentation_axis.buckets 不能为空")
    buckets: list[Bucket] = []
    bseen: set[str] = set()
    for i, rb in enumerate(raw_buckets):
        if not isinstance(rb, dict):
            raise FrameError(f"框架 {fname} 的 buckets[{i}] 必须是映射")
        unk = set(rb) - _BUCKET_KEYS
        if unk:
            raise FrameError(f"框架 {fname} 的 buckets[{i}] 有不认识的键："
                             f"{'、'.join(sorted(unk))}；可用的是 {'、'.join(sorted(_BUCKET_KEYS))}")
        bid = str(rb.get("id") or "").strip()
        if not bid:
            raise FrameError(f"框架 {fname} 的 buckets[{i}] 缺少 id")
        if bid in bseen:
            raise FrameError(f"框架 {fname} 的桶 id 重复：{bid}")
        bseen.add(bid)
        tags = rb.get("tags")
        if not isinstance(tags, list) or not tags:
            raise FrameError(f"框架 {fname} 的桶 {bid} 必须用 tags 列出一个或多个标签键")
        for tk in tags:
            if tk not in cdef.tags:
                raise FrameError(
                    f"框架 {fname} 的桶 {bid} 引用了分类器 {cname} 里没有的标签 {tk!r}；"
                    f"该分类器的标签有：{'、'.join(cdef.tags)}")
        buckets.append(Bucket(
            id=bid,
            label_cn=str(rb.get("label_cn") or bid),
            short_label=str(rb.get("short_label") or ""),
            judge_label=str(rb.get("judge_label") or ""),
            persona_short=str(rb.get("persona_short") or ""),
            persona_label=str(rb.get("persona_label") or ""),
            persona_condition=str(rb.get("persona_condition") or ""),
            tags=tuple(str(t) for t in tags),
            excluded=bool(rb.get("excluded")),
            dashboard={str(k): str(v) for k, v in (rb.get("dashboard") or {}).items()},
        ))

    # 画像维度名从 dimensions 反查填进来：YAML 里写两遍（维度一处、桶一处）
    # 迟早会不一致，不如一处声明、另一处派生。
    from dataclasses import replace as _replace
    label_of = {d.persona_bucket: d.label for d in dims
                if d.persona_bucket and not d.deprecated}
    buckets = [_replace(b, persona_label=b.persona_label or label_of.get(b.id, ""))
               for b in buckets]

    excluded = [b.id for b in buckets if b.excluded]
    if len(excluded) > 1:
        raise FrameError(f"框架 {fname} 最多只能有一个 excluded（被排除的）桶，"
                         f"实际有 {len(excluded)} 个：{'、'.join(excluded)}")
    active = [b for b in buckets if not b.excluded]
    if len(active) < 2:
        raise FrameError(f"框架 {fname} 至少要两个非排除桶才有「分桶」的意义")

    unknown = str(ra.get("unknown_bucket") or (excluded[0] if excluded else "")).strip()
    if unknown and unknown not in bseen:
        raise FrameError(f"框架 {fname} 的 unknown_bucket={unknown} 不在 buckets 里")
    matrix = str(ra.get("matrix_bucket") or active[0].id).strip()
    if matrix not in bseen:
        raise FrameError(f"框架 {fname} 的 matrix_bucket={matrix} 不在 buckets 里")
    if matrix in excluded:
        raise FrameError(f"框架 {fname} 的 matrix_bucket 不能指向被排除的桶 {matrix}")

    # 默认键名：distribution_key 决定产物里的分布键，unit_counts_key 决定计数子块键。
    # 显式可配是为了让 B2B 产物里不出现一个叫 purchase_status_distribution 的键。
    dk = str(ra.get("distribution_key") or f"{adim}_distribution").strip()
    uk = str(ra.get("unit_counts_key") or adim).strip()

    texts = ra.get("texts") or {}
    dash = ra.get("dashboard") or {}
    prompt = ra.get("prompt") or {}
    for name_, obj in (("texts", texts), ("dashboard", dash), ("prompt", prompt)):
        if not isinstance(obj, dict):
            raise FrameError(f"框架 {fname} 的 segmentation_axis.{name_} 必须是映射")

    return Axis(
        dim=adim, label=str(ra.get("label") or cdef.dim_label), classifier=cname,
        buckets=tuple(buckets), distribution_key=dk, unit_counts_key=uk,
        matrix_bucket=matrix, unknown_bucket=unknown,
        texts={str(k): str(v) for k, v in texts.items()},
        dashboard={str(k): str(v) for k, v in dash.items()},
        prompt={str(k): str(v) for k, v in prompt.items()},
        prompt_axis_question=str(ra.get("prompt_axis_question") or ""),
    )


def _validate_cross(frame: AnalysisFrame) -> None:
    """跨字段校验：维度与桶必须对得上。"""
    axis = frame.axis
    if axis is None:
        return
    buckets = {b.id for b in axis.buckets}
    persona = frame.persona_dims()
    for did, bid in persona.items():
        if bid not in buckets:
            raise FrameError(
                f"框架 {frame.name}：维度 {did} 的 persona_bucket={bid} 不在分段轴的桶里"
                f"（{'、'.join(sorted(buckets))}）")
        b = axis.bucket(bid)
        if b is not None and b.excluded:
            raise FrameError(
                f"框架 {frame.name}：维度 {did} 指向被排除的桶 {bid} —— "
                f"被排除的桶不产出画像，画像维度不该映射到它")
    mapped = set(persona.values())
    active = {b.id for b in axis.active_buckets}
    if mapped and not mapped >= active:
        raise FrameError(
            f"框架 {frame.name}：这些桶没有对应的画像维度：{'、'.join(sorted(active - mapped))}；"
            f"每个非排除桶都要有一个 persona_bucket 指向它的画像维度")
    for d in frame.active_dims:
        if d.kind == "classifier" and axis.dim != d.id:
            raise FrameError(
                f"框架 {frame.name}：维度 {d.id} 声明为 classifier，但分段轴用的是 {axis.dim}；"
                f"目前一个框架只支持一条分段轴")


def frame_from_product(product: dict | None, *, strict: bool = False) -> AnalysisFrame | None:
    """从产物里的 ``analysis_frame`` 块还原框架。

    ``product`` 可以是整个 ``tagset.json``（框架在 ``tagset.analysis_frame``）
    或整个 ``dimensions.json``（框架在顶层 ``analysis_frame``）。

    找不到时返回 ``None``，调用方据此回落到内置 consumer —— 那是**平台化之前
    的产物**（没有这个块），回落保证它们还能被新代码读。
    """
    if not isinstance(product, dict):
        return None
    meta = product.get("analysis_frame")
    if meta is None:
        ts = product.get("tagset")
        if isinstance(ts, dict):
            meta = ts.get("analysis_frame")
    if not isinstance(meta, dict):
        if strict:
            raise FrameError(
                "产物里没有 analysis_frame 块 —— 它由 design_tags 写入；"
                "老产物请直接复用同一份框架（--frame）或重新生成标签集")
        return None
    return _from_dict(meta, source="<产物里的 analysis_frame>")


def load_frame(spec=None, *, search_env: bool = True) -> AnalysisFrame:
    """加载框架。

    ``spec`` 可以是：内置名（``consumer``）、文件路径（``frames/b2b_role.yaml``）、
    ``dict``、或 ``None``（→ 内置 ``consumer``，即改造前行为）。
    """
    if isinstance(spec, AnalysisFrame):
        return spec
    if isinstance(spec, dict):
        return _from_dict(spec, source="<内存 dict>")

    name = str(spec or "").strip()
    if not name:
        name = DEFAULT_FRAME

    if search_env:
        env = os.getenv("VOC_FRAME", "").strip()
        if env and not str(spec or "").strip():
            name = env

    # 路径形态：带分隔符或以 yaml/json 结尾
    looks_like_path = ("/" in name or "\\" in name
                       or Path(name).suffix.lower() in (".yaml", ".yml", ".json"))
    p = Path(name) if looks_like_path else frame_path(name)
    if p is None or not p.exists():
        avail = available_frames()
        raise FrameError(
            f"找不到分析框架 {name!r}。内置的框架有：{'、'.join(avail) or '（frames/ 目录为空）'}；"
            f"也可以直接给 .yaml / .json 文件路径。目录：{FRAME_DIR}")
    try:
        raw = miniyaml.load(p)
    except miniyaml.MiniYamlError as exc:
        raise FrameError(f"解析框架文件 {p.name} 失败 —— {exc}") from exc
    return _from_dict(raw, source=str(p))
