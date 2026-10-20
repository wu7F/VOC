#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``llm_connector`` 的平台实现（Stage 4 起为真实实现，不再是占位）。

内核脚本从 ``~/.workbuddy/skills/LLM连接器/scripts/llm_connector.py`` 导入
``call_llm`` / ``is_external_backend`` / ``LocalFallback``。平台化部署时那个
路径不存在，``kernel_common.import_llm_connector()`` 会回落到本模块。

Stage 4 之前这里是个「调用即抛错」的占位。现在它只是
:mod:`voc_core.llm` 的**薄适配层** —— 语义不变（内核的
``except LocalFallback`` 分支一行都不用改），但底层换成了多后端实现：

* ``openai`` / ``local`` 真调 HTTP（OpenAI 兼容协议）
* ``manual`` 落盘待办，回填后重跑即命中（有状态、可重试）
* ``mock`` / ``replay`` 用于跑通链路、离线复现

这样做而不是直接改内核调用点，是为了让「换后端」这件事**不碰内核**：
内核只认这四个名字，加后端 = 在 :mod:`voc_core.llm` 里加一个类。
"""

from __future__ import annotations

from typing import Any

from voc_core import llm as _llm

__all__ = ["LocalFallback", "ManualPending", "is_external_backend", "call_llm",
           "backend_info", "configure", "EXIT_PENDING", "EXIT_CONFIG"]

#: 与官方实现同名同语义（就是同一个类对象，因此 ``except LocalFallback`` 有效）
LocalFallback = _llm.LocalFallback

#: 人工兜底：任务已入队、结果未回填。**不是失败**，是「重跑就能继续」的等待态。
ManualPending = _llm.ManualPending

#: 退出码约定（CLI 层统一）：2=输入/配置错误，3=等待人工回填（可重跑）
EXIT_CONFIG = _llm.EXIT_CONFIG
EXIT_PENDING = _llm.EXIT_PENDING


def call_llm(
    system: str,
    user: str,
    json_mode: bool = False,
    temperature: float | None = None,
    return_usage: bool = False,
    max_tokens: int | None = None,
    **kwargs: Any,
):
    """同 :func:`voc_core.llm.call_llm`（``return_usage=True`` → ``(text, usage)``）。

    ``temperature=None`` = 用配置里的温度（默认 0.3，与官方连接器一致）。
    """
    return _llm.call_llm(system, user, json_mode=json_mode, temperature=temperature,
                         return_usage=return_usage, max_tokens=max_tokens, **kwargs)


def is_external_backend() -> bool:
    """当前后端能否自动产出结果（``False`` 仅当是人工兜底队列）。"""
    return _llm.is_external_backend()


def backend_info() -> dict[str, Any]:
    return _llm.backend_info()


def configure(**kw: Any):
    """设置本进程的 LLM 配置（内核脚本解析完 CLI 参数后调用）。

    用法::

        configure(backend="openai", model="gpt-4o-mini",
                  base_url="https://api.openai.com/v1", api_key="...")
    """
    return _llm.set_default_config(_llm.LLMConfig.resolve(**kw))
