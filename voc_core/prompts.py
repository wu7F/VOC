#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词外置：从 ``prompts/<任务>.<语言>.md`` 加载模板并做变量插值。

为什么外置（Stage 4 的第二个目标）
---------------------------------
改造前提示词是内核里的三段 f-string 常量（`design_tags.py` 的 `DESIGN_SYSTEM` /
`DESIGN_USER` / `COMMENT_BLOCK`、`annotate_posts.py` 的 `ANNOTATE_SYSTEM`）。
后果：改一个措辞要动 Python；换语言要复制一整段代码；没法 A/B 对比；非工程同学改不了。

为什么用 ``${var}`` 而不是 ``str.format``
----------------------------------------
这些提示词里**本来就全是花括号**——``{tk}_summary``、``{summary}``、``{reason}``，
以及大段 JSON 结构示例。若用 ``.format()``，每一处都得手写成 ``{{`` / ``}}``，
漏一个就是 ``KeyError`` 或静默吃掉花括号。更糟的是「该转义的花括号」与
「真占位符」在文本里长得一模一样，靠肉眼区分必然出错。

改用 ``string.Template`` 的 ``${var}``：**花括号全部恢复成普通字符**，
只有 ``${`` 开头的才是占位符。已核对：现有 10 个模板里的 ``$`` 出现次数
与占位符个数完全一致（见 ``prompts/`` 各文件），没有歧义。

行尾约定
--------
模板文件读入后 **只剥掉结尾的换行**（``rstrip("\\n")``），其余原样保留。
原因：``design_tags.comment_block`` 是**插进别的提示词中间**的片段，
它的**开头**两个空行是有意义的；而结尾若是空的会多出空行。
这条约定必须两侧一致——``tools/_freeze_prompts_baseline.py`` 的基线也是这么比的。
"""

from __future__ import annotations

import re
from pathlib import Path
from string import Template

__all__ = [
    "PROMPT_DIR", "DEFAULT_LANG", "PromptError", "MissingPrompt", "PromptVars",
    "prompt_path", "load_template", "render", "template_vars", "available", "check_all",
]

#: 提示词目录（``voc-platform/prompts/``）
PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"

#: 缺省语言。只有 zh 随仓库发布；其余语言按 ``<任务>.<语言>.md`` 放进来即生效。
DEFAULT_LANG = "zh"

#: 模板里出现的占位符（用于自检与文档）
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class PromptError(RuntimeError):
    """提示词相关的错误基类。"""


class MissingPrompt(PromptError):
    """找不到模板文件。"""


class PromptVars(PromptError):
    """模板占位符与实际传入的变量不匹配。"""


def prompt_path(name: str, lang: str = DEFAULT_LANG) -> Path:
    """``name`` + ``lang`` → 模板文件路径。

    找不到 ``<name>.<lang>.md`` 时回落到 :data:`DEFAULT_LANG`；仍找不到则报错并
    列出目录下现有的模板（而不是让调用方对着 ``FileNotFoundError`` 猜）。
    """
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name):
        raise PromptError(f"非法的模板名 {name!r}（只允许字母/数字/下划线/点）")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", lang or ""):
        raise PromptError(f"非法的语言标识 {lang!r}")

    candidates = [PROMPT_DIR / f"{name}.{lang}.md"]
    if lang != DEFAULT_LANG:
        candidates.append(PROMPT_DIR / f"{name}.{DEFAULT_LANG}.md")
    for p in candidates:
        if p.exists():
            return p

    have = "\n".join(f"    - {p.name}" for p in sorted(PROMPT_DIR.glob("*.md"))) or "    （目录为空）"
    raise MissingPrompt(
        f"找不到提示词 {name}.{lang}.md（回落 {DEFAULT_LANG} 也没有）。\n"
        f"  {PROMPT_DIR} 现有模板：\n{have}")


def load_template(name: str, lang: str = DEFAULT_LANG) -> str:
    """读模板原文，只剥结尾换行。"""
    text = prompt_path(name, lang).read_text(encoding="utf-8")
    return text.rstrip("\n")


def template_vars(name: str, lang: str = DEFAULT_LANG) -> set[str]:
    """模板声明了哪些占位符（按出现顺序去重）。"""
    return {m.group(1) for m in _PLACEHOLDER.finditer(load_template(name, lang))}


def render(name: str, lang: str = DEFAULT_LANG, **variables) -> str:
    """渲染模板。

    占位符不匹配时抛 :class:`PromptVars`，并**同时列出多传的与少传的**——
    这类错误几乎总是「改了模板忘了改调用方」，只报 ``KeyError: 'foo'`` 会让人
    去翻半天代码。
    """
    text = load_template(name, lang)
    declared = {m.group(1) for m in _PLACEHOLDER.finditer(text)}
    given = set(variables)
    missing = declared - given
    extra = given - declared
    if missing or extra:
        bits = []
        if missing:
            bits.append("模板需要但未传入：" + ", ".join(sorted(missing)))
        if extra:
            bits.append("传入了模板没有的变量：" + ", ".join(sorted(extra)))
        raise PromptVars(f"提示词 {name}.{lang} 变量不匹配。 " + "；".join(bits))

    try:
        return Template(text).substitute(variables)
    except (KeyError, ValueError) as exc:  # Template 对 $ 语法错误的兜底
        raise PromptVars(f"提示词 {name}.{lang} 渲染失败：{type(exc).__name__}: {exc}") from exc


def available() -> list[tuple[str, str]]:
    """列出目录下所有模板（按名/语言排序）。"""
    out: list[tuple[str, str]] = []
    for p in sorted(PROMPT_DIR.glob("*.md")):
        stem = p.stem
        if "." not in stem:
            continue
        name, lang = stem.rsplit(".", 1)
        out.append((name, lang))
    return out


def check_all(lang: str = DEFAULT_LANG) -> list[str]:
    """自检全部模板，返回问题列表（空列表 = 全部正常）。

    ``voc.py prompts --check`` 与测试都走这里。检查项：
    模板存在、能解析、占位符命名合法、``$`` 全部落在 ``${}`` 里（没有裸 ``$``）。
    """
    issues: list[str] = []
    for name, lg in available():
        if lg != lang:
            continue
        try:
            text = load_template(name, lg)
        except PromptError as exc:
            issues.append(f"{name}.{lg}: {exc}")
            continue
        consumed = [m.span() for m in _PLACEHOLDER.finditer(text)]
        for i, ch in enumerate(text):
            if ch != "$":
                continue
            if not any(a <= i < b for a, b in consumed):
                line = text[:i].count("\n") + 1
                issues.append(
                    f"{name}.{lg}: 第 {line} 行有裸 '$'（不构成 ${{var}}）——"
                    "Template 会把它当占位符起始，请改用 $$ 或删掉")
    return issues
