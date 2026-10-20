#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VOC 平台 · 读写工具。

把散落在各导入脚本里、且**各自修过 bug** 的 IO 逻辑集中到一处：

* 编码自适应解码 —— 合并了两个来源的做法。Reddit 侧的第三方导出常是
  **混合编码**（同一文件里 UTF-8 与 GBK 字节并存，典型如弯引号 ’ 为 ``A1 AF``）：
  严格按 UTF-8 解会整体失败，整体按 GB18030 解又会把 UTF-8 的中文解成乱码。
  做法是两种编码都用 ``errors='replace'`` 解一遍，**取 U+FFFD 替换符更少的那种**。
  电商侧（utf-8-sig / gbk / cp1252 / latin-1 依次尝试）在这一策略下同样成立。
* CSV 一律 **utf-8-sig** 读写 —— 曾有脚本用 ``utf-8`` 读带 BOM 的文件，首列名
  变成 ``\\ufeffreddit_id``，后果是品牌证据 id 全丢、矩阵整块为空、报告里所有
  来源链接静默消失。这个坑在这里一次性封死。
* 时间归一化：epoch 秒/毫秒 → ISO 8601 UTC。

这些坑的原始记录见 ``reddit分析/SKILL.md`` 的实战清单，本模块是它们的代码化。
"""

from __future__ import annotations

import csv
import json
import re
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

#: 写 CSV 一律带 BOM，Excel 打开中文不乱码；读时用 utf-8-sig 自动吃掉 BOM
CSV_ENCODING = "utf-8-sig"


# --------------------------------------------------------------------------
# 解码
# --------------------------------------------------------------------------

def read_text_auto(path: Path) -> str:
    """读文本，自动处理 BOM 与混合编码。

    策略（三步，逐级放宽）：
    1. 先按 ``utf-8-sig`` **严格**解码 —— 覆盖绝大多数正常文件，且顺手吃掉 BOM。
    2. 严格失败说明是混合编码或非 UTF-8：把 ``utf-8-sig`` 与 ``gb18030`` 各按
       ``errors='replace'`` 解一遍，**取 U+FFFD 替换符更少的那份**。
       注意这一档必须无条件返回（哪怕某个候选的替换符数为 0）——
       "0 个替换符"正是最理想的结果，早期版本在这里多写了一个 ``> 0`` 判断，
       导致纯 GBK 文件被错误地继续下沉到 cp1252 而整篇乱码。
    3. 兜底 cp1252 / latin-1（两者对任意字节都合法，故**不能**参与第 2 档的
       替换符比较 —— latin-1 永远是 0 个替换符，会把真正的答案挤掉）。

    已知取舍：纯西语（cp1252）文件可能被第 2 档判成 GBK。平台定位是中英双语，
    ASCII 无损、中文正确，故接受此取舍。
    """
    raw = path.read_bytes()
    try:
        return raw.decode(CSV_ENCODING)
    except UnicodeDecodeError:
        pass

    best: tuple[int, str] | None = None
    for enc in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(enc, errors="replace")
        except Exception:  # noqa: BLE001 - 任何解码器异常都跳过
            continue
        bad = text.count("\ufffd")
        if best is None or bad < best[0]:
            best = (bad, text)
    if best is not None:
        return best[1]

    for enc in ("cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------

def read_rows(path: str | Path, limit: int | None = None) -> list[dict[str, Any]]:
    """读 CSV / JSONL / JSON 数组，统一返回 dict 列表。

    JSON 支持常见包装形状：``{"data":[...]}`` / ``{"reviews":[...]}`` /
    ``{"rows":[...]}`` / ``{"results":[...]}`` / ``{"items":[...]}`` /
    ``{"posts":[...]}``。

    ``limit`` 用于**采样**（数据源识别只需看前若干行，没必要读 12 MB 的全量）。
    """
    p = Path(path)
    suf = p.suffix.lower()

    if suf in (".jsonl", ".ndjson"):
        rows: list[dict[str, Any]] = []
        for line in read_text_auto(p).splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
                if limit and len(rows) >= limit:
                    break
        return rows

    if suf == ".json":
        data = json.loads(read_text_auto(p))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "reviews", "rows", "results", "items", "posts", "children"):
                if isinstance(data.get(key), list):
                    return data[key]
        raise SystemExit(f"无法识别的 JSON 结构（顶层既不是数组也没有已知列表键）：{p}")

    text = read_text_auto(p)
    reader = csv.DictReader(text.splitlines(True))
    if limit is None:
        return list(reader)
    out: list[dict[str, Any]] = []
    for row in reader:
        out.append(row)
        if len(out) >= limit:
            break
    return out


def read_xlsx_rows(
    path: str | Path,
    sheet: str | int | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """读 xlsx 为 dict 列表（按首行做表头）。

    用 ``openpyxl`` 的只读流式模式（本机无 pandas 时的标准做法）。
    观察到的第三方导出（``批量导出_<N>条.xlsx``）列形如
    ``author, authorUrl, content, date, likes, ..., type, postId``。
    """
    try:
        from openpyxl import load_workbook  # 延迟导入：非 xlsx 路径不需要该依赖
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "读取 .xlsx 需要 openpyxl。请先安装："
            "pip install openpyxl（项目内已声明为可选依赖）"
        ) from exc

    with warnings.catch_warnings():
        # 第三方导出常缺默认样式表（如 Wayfair 官方 PartReviews 的 xlsx），
        # openpyxl 会发 "Workbook contains no default style" —— 无害，
        # 但会混进 CLI 输出里看着像报错，这里抑制掉。
        warnings.filterwarnings("ignore", message=".*no default style.*")
        wb = load_workbook(filename=str(path), read_only=True, data_only=True)
    ws = wb[sheet] if sheet is not None else wb.active
    if ws is None:
        raise SystemExit(f"xlsx 没有可读工作表：{path}")

    it = ws.iter_rows(values_only=True)
    try:
        header = [str(h).strip() if h is not None else "" for h in next(it)]
    except StopIteration:
        return []
    rows: list[dict[str, Any]] = []
    for values in it:
        if values is None:
            continue
        row = {}
        for i, h in enumerate(header):
            if not h:
                continue
            v = values[i] if i < len(values) else None
            row[h] = "" if v is None else v
        if any(str(v).strip() for v in row.values()):
            rows.append(row)
            if limit and len(rows) >= limit:
                break
    wb.close()
    return rows


# --------------------------------------------------------------------------
# 写
# --------------------------------------------------------------------------

def write_rows(
    path: str | Path,
    rows: Iterable[dict[str, Any]],
    fields: list[str] | None = None,
    *,
    extrasaction: str = "ignore",
) -> Path:
    """写 CSV（utf-8-sig）。``fields`` 为空时取所有行键的并集。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    if fields is None:
        fields = []
        seen: set[str] = set()
        for r in rows:
            for k in r:
                if k not in seen:
                    seen.add(k)
                    fields.append(k)
    with p.open("w", encoding=CSV_ENCODING, newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(fields), extrasaction=extrasaction)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    return p


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> Path:
    """写 JSON Lines（utf-8）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def write_json(path: str | Path, obj: Any) -> Path:
    """写 JSON（utf-8，缩进 2，不转义中文）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# 值归一化
# --------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def collapse_ws(v: Any) -> str:
    """折叠空白（含换行、制表符）为单个空格并 strip。"""
    return _WS_RE.sub(" ", str(v or "")).strip()


def epoch_to_iso(val: Any) -> str:
    """epoch 秒/毫秒 → ISO 8601 (UTC, 带 Z)。非数字原样返回。"""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return str(val or "").strip()
    if f > 1e12:  # 毫秒
        f = f / 1000.0
    if f <= 0:
        return ""
    return datetime.fromtimestamp(f, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_bool(val: Any) -> bool | None:
    """宽松布尔：true/yes/1/是/y/vp → True；false/no/0/否/n → False；其余 None。"""
    s = str(val or "").strip().lower()
    if s in ("true", "yes", "1", "是", "y", "vp"):
        return True
    if s in ("false", "no", "0", "否", "n"):
        return False
    return None


def to_int(val: Any) -> int | None:
    """宽松整数：支持 "5.0"、"1,234"、"5 星"；失败返回 None。"""
    s = re.sub(r"[^\d.\-]", "", str(val or ""))
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def strip_prefixes(v: Any, *prefixes: str) -> str:
    """剥离形如 t3_ / t1_ / u/ / r/ 的前缀。"""
    s = str(v or "").strip()
    for p in prefixes:
        if s.lower().startswith(p.lower()):
            return s[len(p):]
    return s


def norm_key(k: Any) -> str:
    """把列名归一化为可比较形式。

    电商导出的列名五花八门：``Review Title`` / ``review-title`` / ``review_title``
    / ``ReviewTitle`` 指的是同一件事。归一化规则：去 BOM、转小写、把空格与
    连字符 / 点号统一成下划线、合并连续下划线。这样 ``pick`` 才能用一张别名表
    覆盖所有写法。
    """
    s = str(k or "").strip().lstrip("\ufeff").lower()
    s = re.sub(r"[\s\-./]+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")


def pick(row: dict[str, Any], aliases: Iterable[str], *, mark_used: set[str] | None = None) -> str:
    """按别名优先级从行里取值（列名归一化后比较，忽略大小写与分隔符差异）。

    ``mark_used`` 传入时记录真正被消费的**原始**列名，供"透传多余字段"时避免重复。
    """
    index: dict[str, Any] = {}
    for k in row:
        nk = norm_key(k)
        if nk and nk not in index:
            index[nk] = k
    for a in aliases:
        key = index.get(norm_key(a))
        if key is None:
            continue
        v = row.get(key)
        if v is not None and str(v).strip() != "":
            if mark_used is not None:
                mark_used.add(str(key))
            return str(v).strip()
    return ""
