#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极简 YAML 子集解析器（零依赖，只用标准库）。

**为什么不直接 ``pip install pyyaml``**：整个平台有一条硬约束 —— 只用标准库，
裸解释器（``python -S -E``）也要能跑。Stage 4 的 LLM 层为此把 HTTP 客户端
写成 ``urllib``；框架配置这一层同理，不能因为「配置文件写着 .yaml」就引入依赖。

**为什么是「子集」而不是「兼容 YAML」**：一个半吊子的 YAML 兼容层比没有更危险 ——
用户写了它不支持的写法，解析器却给出一个**看起来正常**的结果，错误就潜伏到
分析产物里去了。所以这里的原则是：**支持范围小而明确，范围外一律报错带行号**。

支持（配置文件里真正用得到的部分）::

    # 顶层注释
    name: consumer                 # 行内注释（# 前须有空格）
    label: "含: 冒号的值"           # 单/双引号强制按字符串
    enabled: true                  # true/false → bool
    ratio: 0.5                     # 整数 / 浮点
    empty: null                    # null / ~ / 留空 → None
    tags: [a, b, c]                # 行内列表
    meta: {x: 1, y: 2}             # 行内映射（浅层）
    dimensions:
      - id: purchase_status        # 列表内映射（续行与 - 后内容对齐）
        label: 购买状态判定
      - id: features
        label: 功能体验
    note: |                        # 字面块标量（保留换行）
      第一行
      第二行

不支持并**明确报错**：锚点/别名（``&``/``*``）、多文档（``---``）、
``!`` 标签、tab 缩进、重复键、同一层级缩进不齐、``-`` 的层级跳跃。

类型推断刻意保守：**只有 ``true``/``false``**（含首字母大写）算布尔，
``yes``/``no``/``on``/``off`` 一律当字符串 —— 否则 ``country: no`` 会静默
变成布尔 False（这是 YAML 1.1 里最有名的咬人坑，不值得为了「兼容」踩）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

__all__ = ["MiniYamlError", "loads", "load"]


class MiniYamlError(ValueError):
    """解析失败。消息里始终带 ``第 N 行``，方便直接定位。"""

    def __init__(self, message: str, lineno: int = 0, text: str = ""):
        self.lineno = lineno
        loc = f"第 {lineno} 行" if lineno else ""
        hint = f"\n  原文: {text}" if text else ""
        super().__init__(f"{loc}{'：' if loc else ''}{message}{hint}")


#: ``key: value`` —— 键允许中文、下划线、连字符、点
_KEY = re.compile(r"^([^\s:][^:]*?)\s*:(?:\s+(.*))?$")
_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")
_TRUE = {"true", "True", "TRUE"}
_FALSE = {"false", "False", "FALSE"}
_NULL = {"null", "Null", "NULL", "~", ""}
#: 块标量的标记（含 chomping 变体）：``键: |`` / ``键: >`` / ``- |``
_BLOCK_MARKS = frozenset({"|", "|+", "|-", ">", ">+", ">-"})


def is_block_head(content: str) -> bool:
    """这行是不是块标量的头？

    不能用「以 ``|`` 结尾」这种土办法判断 —— ``note: "a: |"`` 里那个竖线在
    引号内，YAML 当它是**普通字符串**，判成块标量会把后面几行的缩进结构全带歪。
    所以走 ``_KEY`` 把键值切开，只看**值本身**是不是块标记。
    """
    m = _KEY.match(content)
    if m is not None:
        return (m.group(2) or "").strip() in _BLOCK_MARKS
    if content.startswith("-"):
        return content[1:].strip() in _BLOCK_MARKS
    return False


def _strip_comment(raw: str) -> str:
    """去行内注释。只在 ``#`` 前是空白（或行首）时才当注释，
    且引号内的 ``#`` 不算 —— ``title: "a # b"`` 必须保留。"""
    out = []
    quote = ""
    for i, ch in enumerate(raw):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (i == 0 or raw[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _split_inline(s: str) -> list[str]:
    """按顶层逗号切分（引号内与括号内不切）。"""
    parts, buf, quote, depth = [], [], "", 0
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            depth += 1
            buf.append(ch)
        elif ch in "]}":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _scalar(s: str, lineno: int):
    """标量 → Python 值。保守的类型推断（见模块文档）。"""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_scalar(p, lineno) for p in _split_inline(inner)] if inner else []
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        out: dict = {}
        for part in (_split_inline(inner) if inner else []):
            if ":" not in part:
                raise MiniYamlError(f"行内映射项缺冒号：{part!r}", lineno, s)
            k, _, v = part.partition(":")
            out[_scalar(k, lineno)] = _scalar(v, lineno)
        return out
    if s in _NULL:
        return None
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    if _INT.match(s):
        return int(s)
    if _FLOAT.match(s):
        return float(s)
    return s


class _Parser:
    def __init__(self, text: str):
        self.raw_lines = text.splitlines()
        self.lines: list[tuple[int, str, int]] = []   # (indent, content, lineno)
        self._prepare()

    def _prepare(self) -> None:
        block_depth: int | None = None    # 当前块标量的基准缩进（None = 不在块里）
        for i, raw in enumerate(self.raw_lines, start=1):
            lead = raw[: len(raw) - len(raw.lstrip())]
            if "\t" in lead:
                raise MiniYamlError("缩进里出现 tab，YAML 只允许空格", i, raw)
            indent = len(lead)

            if block_depth is not None:
                # 块标量内部：**原样保留**（里面的 ``#`` 是正文，不是注释）。
                # **块内**空行也要保留 —— 多段文本（提示词片段）靠空行分段，
                # 丢了空行会让整段粘成一坨，而且**不会有任何报错**。
                # （结尾的空行由 ``_block_scalar`` 丢掉，那是排版分隔，不是内容。）
                if raw.strip() and indent <= block_depth:
                    block_depth = None        # 块结束，本行按普通行继续处理
                elif raw.strip():
                    self.lines.append((indent, raw.strip(), i))
                    continue
                else:
                    # 用一个比基准深的假缩进标记"这是块内空行"，内容为空；
                    # _block_scalar 计算公共缩进时会跳过空行，不影响其他行。
                    self.lines.append((block_depth + 1, "", i))
                    continue

            body = _strip_comment(raw)
            if not body.strip():
                continue
            content = body.strip()
            self._reject_unsupported(content, i, raw)
            self.lines.append((indent, content, i))
            if is_block_head(content):
                block_depth = indent

    @staticmethod
    def _reject_unsupported(content: str, lineno: int, raw: str) -> None:
        """不支持的 YAML 特性：一律报错，不「尽力而为」。

        静默地降级解析（例如把 ``&anchor`` 当普通字符串）比直接报错危险得多 ——
        用户会以为配置生效了。
        """
        if content.startswith("---"):
            raise MiniYamlError("不支持多文档分隔符（---），一个文件只能有一个文档", lineno, raw)
        if content.startswith("..."):
            raise MiniYamlError("不支持文档结束符（...）", lineno, raw)
        for token, what in (("&", "锚点（&anchor）"), ("*", "别名（*alias）"),
                            ("!", "显式标签（!tag）")):
            if content.startswith(token):
                raise MiniYamlError(f"不支持 {what}", lineno, raw)
        # 值以 & / * 开头同样是锚点 / 别名：``key: &x 1`` / ``key: *ref``
        m = _KEY.match(content)
        if m and (m.group(2) or "").strip().startswith(("&", "*")):
            raise MiniYamlError(
                f"不支持锚点/别名（值以 {(m.group(2) or '').strip()[0]!r} 开头）；"
                "若确实需要这个字符，请用引号包起来", lineno, raw)
        if content.startswith("<<") or ": <<" in content:
            raise MiniYamlError("不支持合并键（<<）", lineno, raw)

    # -- 入口 -------------------------------------------------------------
    def parse(self):
        if not self.lines:
            return {}
        first_indent = self.lines[0][0]
        if first_indent != 0:
            raise MiniYamlError(
                f"顶层内容不能有缩进（当前 {first_indent} 个空格）",
                self.lines[0][2], self.lines[0][1])
        val, i = self._block(0, 0)
        if i != len(self.lines):
            indent, content, lineno = self.lines[i]
            raise MiniYamlError(f"多余的内容（无法归入任何层级）：{content!r}", lineno, content)
        return val

    # -- 递归下降 ----------------------------------------------------------
    def _block(self, i: int, indent: int):
        """解析缩进为 ``indent`` 的一个块（映射或列表），返回 (值, 下一个索引)。"""
        if i >= len(self.lines):
            return None, i
        cur_indent, content, _ = self.lines[i]
        if cur_indent != indent:
            raise MiniYamlError(
                f"缩进不齐：期望 {indent} 个空格，实际 {cur_indent} 个",
                self.lines[i][2], content)
        if content == "-" or content.startswith("- "):
            return self._list(i, indent)
        return self._map(i, indent)

    def _map(self, i: int, indent: int) -> tuple[dict, int]:
        out: dict = {}
        while i < len(self.lines):
            cur_indent, content, lineno = self.lines[i]
            if cur_indent < indent:
                break
            if cur_indent > indent:
                raise MiniYamlError(
                    f"缩进跳跃：上一键在 {indent} 个空格处，本行缩进 {cur_indent}",
                    lineno, content)
            if content.startswith("- "):
                break     # 交给上层（列表项与映射不能混在同一层）
            m = _KEY.match(content)
            if not m:
                raise MiniYamlError(f"不是合法的 `键: 值` 形式：{content!r}", lineno, content)
            key = _scalar(m.group(1).strip(), lineno)
            if not isinstance(key, str):
                key = m.group(1).strip()
            if key in out:
                raise MiniYamlError(f"重复的键 {key!r}", lineno, content)
            rest = (m.group(2) or "").strip()
            if rest in ("|", "|-", ">", ">-"):
                val, i = self._block_scalar(i + 1, indent, fold=rest.startswith(">"))
                out[key] = val
                continue
            if rest:
                out[key] = _scalar(rest, lineno)
                i += 1
                continue
            # 值为空 → 看下一行：更深缩进 = 子块；否则是 None
            if i + 1 < len(self.lines) and self.lines[i + 1][0] > indent:
                val, i = self._block(i + 1, self.lines[i + 1][0])
                out[key] = val
            else:
                out[key] = None
                i += 1
        return out, i

    def _list(self, i: int, indent: int) -> tuple[list, int]:
        out: list = []
        while i < len(self.lines):
            cur_indent, content, lineno = self.lines[i]
            if cur_indent < indent:
                break
            if cur_indent > indent:
                raise MiniYamlError(
                    f"缩进跳跃：列表项在 {indent} 个空格处，本行缩进 {cur_indent}",
                    lineno, content)
            if not (content == "-" or content.startswith("- ")):
                break
            rest = content[2:].strip() if content.startswith("- ") else ""
            if not rest:
                if i + 1 < len(self.lines) and self.lines[i + 1][0] > indent:
                    val, i = self._block(i + 1, self.lines[i + 1][0])
                else:
                    out.append(None)
                    i += 1
                continue
            m = _KEY.match(rest)
            if m and not rest.startswith(("[", "{", "\"", "'")):
                # 列表项是个映射：``- id: x`` + 后续与「- 」之后内容列对齐的键
                # ``-`` 在列 indent，所以内容的列号是 indent + 2
                item_indent = indent + 2
                key = m.group(1).strip()
                first_val = (m.group(2) or "").strip()
                item: dict = {}
                if first_val:
                    item[key] = _scalar(first_val, lineno)
                    i += 1
                else:
                    if i + 1 < len(self.lines) and self.lines[i + 1][0] > item_indent:
                        val, i = self._block(i + 1, self.lines[i + 1][0])
                    else:
                        item[key] = None
                        i += 1
                # 收集与 item_indent 对齐的续行键
                if i < len(self.lines) and self.lines[i][0] == item_indent:
                    more, i = self._map(i, item_indent)
                    dup = set(more) & set(item)
                    if dup:
                        raise MiniYamlError(
                            f"列表项里重复的键：{'、'.join(sorted(dup))}",
                            self.lines[i - 1][2] if i else lineno, rest)
                    item.update(more)
                out.append(item)
                continue
            if rest in ("|", "|-", ">", ">-"):
                val, i = self._block_scalar(i + 1, indent, fold=rest.startswith(">"))
                out.append(val)
                continue
            out.append(_scalar(rest, lineno))
            i += 1
        return out, i

    def _block_scalar(self, i: int, indent: int, fold: bool) -> tuple[str, int]:
        """``|`` 字面块 / ``>`` 折行块。收集比 ``indent`` 更深的所有行。

        公共缩进按**非空行**算（空行不参与，否则一个空行就会把整段的
        相对缩进算少，段落里本来有缩进的续行会顶格）。

        **结尾的空行一律丢掉**（``|`` 与 ``|-`` 在这里同义）。原因：块标量后面
        通常紧跟另一个键，中间隔一个空行只是排版习惯 —— 若把它算进内容，
        ``json_shape`` 这类「整块插进提示词末尾」的值就会凭空多一个换行，
        而提示词多一个换行**没有任何测试会发现**。
        """
        body: list[tuple[int, str, int]] = []
        while i < len(self.lines) and self.lines[i][0] > indent:
            body.append(self.lines[i])
            i += 1
        while body and not body[-1][1]:
            body.pop()
        if not body:
            return "", i
        base = min((b[0] for b in body if b[1]), default=indent + 1)
        raws = [(" " * (ind - base) + content) if content else "" for ind, content, _ln in body]
        if fold:
            return " ".join(t.strip() for t in raws if t.strip()), i
        return "\n".join(raws), i


def loads(text: str):
    """解析一段 YAML 子集。先试 JSON（JSON 是 YAML 的子集，且 .json 文件常见）。"""
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass   # 落到下面的 YAML 路径，报错会更具体
    return _Parser(text).parse()


def load(path: str | Path):
    """从文件加载。``.json`` 走 JSON，其余走 YAML 子集（两者都先试 JSON）。"""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        return json.loads(text)
    return loads(text)
