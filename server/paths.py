# -*- coding: utf-8 -*-
"""运行期路径解析：让「代码」与「数据」彻底分离。

为什么需要它
------------
VOC 洞察平台最终要打成**单个 exe** 放到别人机器上跑，同时数据要放在稳定目录
（不能放桌面）。这要求：

* **代码位置**随 exe 走（只读）
* **数据位置**独立可配（可迁移、可备份）

规则（优先级从高到低）：

1. 环境变量 ``VOC_DATA_DIR`` —— 显式指定，最可控
2. 打包成 exe 后 → ``<exe 同级>/VOCData``
3. 源码运行 → 项目根目录（保持历史行为不变）

所有产出（``runs/``）、配置（``llm_config.json``）、演示数据（``data/``）都挂在数据根目录下，
于是「拷走 exe + VOCData 目录」就是完整迁移。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """代码根目录：源码运行时是项目根，打包后是 exe 所在目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_root() -> Path:
    """数据根目录：产出 + 配置 + 演示数据都放这里。"""
    env = os.environ.get("VOC_DATA_DIR", "").strip()
    if env:
        p = Path(env).expanduser()
        try:
            return p.resolve()
        except OSError:
            return p
    if is_frozen():
        return (app_root() / "VOCData").resolve()
    return app_root()


def ensure_dirs() -> dict:
    """确保各子目录存在，返回解析后的关键路径。"""
    root = data_root()
    runs = root / "runs"
    demo = root / "data"
    for p in (root, runs, demo):
        p.mkdir(parents=True, exist_ok=True)
    return {"root": root, "runs": runs, "demo": demo}


def config_path() -> Path:
    """llm_config.json：优先数据根目录，其次项目根（兼容旧部署）。"""
    p = data_root() / "llm_config.json"
    if p.exists():
        return p
    legacy = app_root() / "llm_config.json"
    return legacy if legacy.exists() else p


def web_dir() -> Path:
    """前端静态资源目录。

    源码运行时是 ``<项目根>/web``；打包后资源被 PyInstaller 释放到临时目录，
    由 ``_MEIPASS`` 指定。
    """
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base) / "web"
    return app_root() / "web"