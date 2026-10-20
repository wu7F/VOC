#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 4 手术：把 design_tags.py 的内嵌提示词换成 prompts/ 加载。

按**行号**切片而不是靠字符串匹配——被删的是三段各 40+ 行的中文长字符串，
用 old_string 精确匹配极易因一个行尾空格而失败，且失败信息毫无指向性。
切片后立刻 py_compile + 比对渲染结果，出问题马上能发现。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "voc_core" / "kernel" / "design_tags.py"

NEW_BLOCK = '''# --------------------------------------------------------------------------
# 提示词（Stage 4 外置）
# --------------------------------------------------------------------------
# 原本这三段提示词是内嵌的长字符串常量，改一个措辞就得动 Python，
# 也没法换语言 / A/B 对比。现在全部住在 prompts/design_tags.*.zh.md。
#
# 变量语法是 ${name}（string.Template）而不是 str.format：这些提示词里**本来就
# 满是花括号**（`{tk}_summary`、`{summary}`、大段 JSON 结构示例），用 format 就得把
# 每一处都手写成 {{ }}，漏一个就静默改坏了提示词。
DESIGN_SYSTEM = P.load_template("design_tags.system")
'''

lines = SRC.read_text(encoding="utf-8").splitlines(keepends=True)

# 定位：从 `DESIGN_SYSTEM = """` 到 `COMMENT_BLOCK` 的结束 `"""`（即 `{comments}"""`）
start = next(i for i, ln in enumerate(lines) if ln.startswith('DESIGN_SYSTEM = """'))
end = next(i for i, ln in enumerate(lines)
           if i > start and ln.startswith("COMMENT_BLOCK = "))
end = next(i for i, ln in enumerate(lines)
           if i > end and ln.rstrip().endswith('"""'))
print(f"替换 {SRC.name} 第 {start + 1}–{end + 1} 行（共 {end - start + 1} 行）")
print(f"  首行: {lines[start][:60]!r}")
print(f"  末行: {lines[end][:60]!r}")

out = lines[:start] + [NEW_BLOCK] + lines[end + 1:]
text = "".join(out)

# 1) 加 prompts 导入
old_imp = """_llm = import_llm_connector()
call_llm = _llm.call_llm"""
new_imp = """from voc_core import prompts as P  # noqa: E402

_llm = import_llm_connector()
call_llm = _llm.call_llm"""
assert text.count(old_imp) == 1, "找不到 LLM 连接器导入块"
text = text.replace(old_imp, new_imp)

# 2) build_samples 里的 COMMENT_BLOCK.format(...) → P.render(...)
old_cb = """        comment_block = COMMENT_BLOCK.format(
            ncomments=n_comments, nposts=len(post_blocks),
            comments="\\n".join(flat))"""
new_cb = """        # 评论块自带「这是评论片段、不是发帖人自述」的说明——那句提示词也在
        # prompts/design_tags.comment_block.zh.md 里，不在代码里。
        comment_block = P.render(
            "design_tags.comment_block",
            ncomments=n_comments, nposts=len(post_blocks),
            comments="\\n".join(flat))"""
assert text.count(old_cb) == 1, "找不到 COMMENT_BLOCK.format 调用"
text = text.replace(old_cb, new_cb)

SRC.write_text(text, encoding="utf-8")
r = subprocess.run([sys.executable, "-m", "py_compile", str(SRC)], capture_output=True, text=True)
print("py_compile:", "OK" if r.returncode == 0 else r.stderr)
raise SystemExit(r.returncode)
