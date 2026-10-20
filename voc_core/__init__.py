#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""voc_core · 通用 VOC 洞察内核。

分层：

* ``schema``     —— 统一数据契约 ``Unit``（与数据源无关）
* ``io_utils``   —— 编码自适应读写、值归一化（把历史踩过的坑封成代码）
* ``adapters``   —— 各数据源适配器（reddit_posts / reddit_comments /
                    amazon_reviews / wayfair_reviews）

上游：任意来源的导出文件。
下游：清洗分桶 → 标签设计 → 逐条标注 → 聚合 → 看板与报告。

用法::

    from voc_core import Unit, summarize
    from voc_core.adapters import detect_best, get

    adapter, scores = detect_best("batch_export.xlsx")
    units = adapter.normalize("batch_export.xlsx")
"""

from __future__ import annotations

from . import adapters, io_utils, schema
from .schema import (
    KNOWN_SOURCES,
    KNOWN_TYPES,
    RATED_SOURCES,
    TYPE_COMMENT,
    TYPE_POST,
    TYPE_REVIEW,
    Unit,
    legacy_fields,
    normalize_all,
    summarize,
)

__version__ = "0.1.0"

__all__ = [
    "adapters",
    "io_utils",
    "schema",
    "Unit",
    "KNOWN_SOURCES",
    "KNOWN_TYPES",
    "RATED_SOURCES",
    "TYPE_POST",
    "TYPE_COMMENT",
    "TYPE_REVIEW",
    "legacy_fields",
    "normalize_all",
    "summarize",
    "__version__",
    #: Stage 9：编排层（惰性导出，见 ``__getattr__`` —— 不用就不加载）
    "pipeline",
    "run_job",
    "list_jobs",
    "load_job",
    "JobSpec",
]


#: 惰性 re-export（PEP 562）。``voc_core`` 的导入保持轻量：只有真正用到编排器时
#: 才加载 ``pipeline``。写成 ``from . import pipeline`` 也行，但那样任何一次
#: ``import voc_core``（比如只想要 ``Unit``）都会连带拉起 argparse 一整套。
#: 值 = 要从 ``pipeline`` 取的属性名；``None`` 表示导出模块本身。
_LAZY_ATTRS: dict[str, str | None] = {
    "pipeline": None,
    "run_job": "run_job",
    "list_jobs": "list_jobs",
    "load_job": "load_job",
    "JobSpec": "JobSpec",
}


def __getattr__(name: str):
    """让 ``from voc_core import run_job`` 也能用（Python 会回落到这里）。

    **注意**：这里必须用 ``importlib.import_module(".pipeline", __name__)``，
    不能写成 ``from . import pipeline``。后者会走 ``_handle_fromlist``，而它第一步就是
    ``hasattr(voc_core, "pipeline")`` —— 又回落进本函数，**无限递归**（实测栈溢出）。
    """
    if name not in _LAZY_ATTRS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    _pipeline = importlib.import_module(".pipeline", __name__)
    attr = _LAZY_ATTRS[name]
    return _pipeline if attr is None else getattr(_pipeline, attr)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_ATTRS))
