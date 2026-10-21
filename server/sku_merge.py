# -*- coding: utf-8 -*-
"""SKU 归族：把高度相似的 SKU 写法合并成「产品族」。

电商台账里同一个产品常有几十种写法（颜色/材质/规格/批次后缀各不相同），
例如：
    MR221118BR-WKS / MR221118-BR-MJT-BJ-1 / MR221118-BR-wks-x / 04-MR221118BR-ZJ
它们都是同一款产品的不同 SKU 变体，单独统计毫无意义，必须合并。

归族规则：
  1. 剥掉品牌前缀（YJMR / YJSR / MR / SR …，可配置）
  2. 取「型号段」= 字母(0-3) + 数字(2-6)
  3. 产品族键 = 品牌 + 型号

实测（亚马逊售后台账 519 个 SKU）：→ 135 个产品族，
73 个合并组覆盖 457 个 SKU / 94% 的记录。

还支持一份**手工覆盖表**（sku_merge.json）：
    {"MR221118BR-WKS": "MR-221118"}     把某个 SKU 归到指定族
    {"MR-XXX": null}                      把某个 SKU 从任何族里拆出来
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# 品牌前缀（长→短匹配）。可被 sku_merge.json 里的 "_brands" 覆盖。
_DEFAULT_BRANDS = [
    "YJPWSR", "YJSRCR", "YJSRHLR", "YJSRNPG", "SRYJ", "NPSR",
    "YJMR", "YJSR", "MR", "SR", "YJ",
]

# 批次/序号前缀（纯数字且 <=2 位，如 04- 、2- ）
_BATCH_SEG = re.compile(r"^\d{1,2}$")
_SPLIT = re.compile(r"[-_/\s.,]+")
# 型号：字母(0-3) + 数字(2-6)
_MODEL = re.compile(r"^([A-Z]{0,3})(\d{2,6})")


def load_overrides(data_dir: Path | str | None) -> dict:
    """读取手工覆盖表：{SKU: 族名 | null}。"""
    if data_dir is None:
        return {}
    p = Path(data_dir) / "sku_merge.json"
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _brands(data_dir: Path | str | None) -> list[str]:
    if data_dir is not None:
        ov = load_overrides(data_dir)
        brands = ov.get("_brands")
        if isinstance(brands, list) and brands:
            return [str(b).upper() for b in brands]
    return list(_DEFAULT_BRANDS)


def sku_family(sku: str, brands: list[str] | None = None) -> str:
    """返回 SKU 所属的产品族标识，如 "MR-221118"。"""
    s = (sku or "").strip().upper()
    if not s:
        return ""
    segs = [x for x in _SPLIT.split(s) if x]
    if not segs:
        return ""
    brands = brands or _DEFAULT_BRANDS

    # 1) 剥品牌前缀（可能跨段，如 MR-221118 或 YJMR8018）
    brand = ""
    rest_start = 0
    for b in brands:
        for i, sg in enumerate(segs):
            if sg.startswith(b):
                brand = b
                rest_start = i
                break
        if brand:
            break
    if not brand:
        # 没有已知品牌 → 取首段开头的连续字母作为品牌
        m = re.match(r"^([A-Z]+)", segs[0])
        brand = m.group(1) if m else "SKU"
        rest_start = 0

    # 2) 找型号段
    model = ""
    for j in range(rest_start, len(segs)):
        sg = segs[j]
        # 剥掉品牌后剩下的部分
        if j == rest_start and sg.startswith(brand):
            body = sg[len(brand):]
        elif j == rest_start and brand == "SKU":
            body = sg
        else:
            body = sg
        # 跳过纯数字的批次/变体段（<=2 位）
        if _BATCH_SEG.match(sg):
            continue
        m = _MODEL.match(body)
        if m:
            if not m.group(1) and len(m.group(2)) < 3:
                continue      # 纯数字不足 3 位 → 多半是变体号，继续找
            model = m.group(1) + m.group(2)
            break
    if not model:
        # 退化：直接用剥品牌后的首段
        seg = segs[rest_start]
        body = seg[len(brand):] if seg.startswith(brand) else seg
        model = body[:8] or seg[:8]

    return "%s-%s" % (brand, model)


class SkuGrouper:
    """把原始 SKU 映射到产品族，并记录每个族的成员明细。"""

    def __init__(self, data_dir: Path | str | None = None):
        self.brands = _brands(data_dir)
        self.overrides = {k: v for k, v in load_overrides(data_dir).items()
                         if not k.startswith("_")}

    def family(self, sku: str) -> str:
        s = (sku or "").strip()
        if not s:
            return ""
        # 非真实 SKU（缺产品列时的占位）原样保留，不要加前缀
        if s in ("未标注产品", "未标注", "-"):
            return s
        if s in self.overrides:
            ov = self.overrides[s]
            if ov:
                return str(ov)
            # 显式拆出 → 单独成族
            return s
        return sku_family(s, self.brands)
