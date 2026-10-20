#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""产物命名：把「文件名」也纳入去域名化的范围。

Stage 3 去掉了列名与文案里的 ``reddit``，但**产物文件名**仍带着 ``reddit_`` 前缀。
当初判定它"属数据契约层、风险大于收益"而推迟 —— 直到 Stage 6 的闸门把它抓了出来：

    附录里的「清洗后相关评论：out/wayfair/analysis/reddit_posts_relevant.csv」

这是**用户真正看得到**的一句，跑 Wayfair 数据却报出 ``reddit_posts``。闸门报的是
``presentation_vocabulary`` 错误级失败，于是这条从"化妆品"变成了必修项。

迁移策略（``auto``）：**Reddit 保旧名、其他源用中性名**。

* Reddit 保旧名 —— 既有脚本、既有归档、既有测试全部零改动，这是**真·零回归**；
* 其他源用中性名 —— 泄漏从源头消失，Wayfair 的报告不再出现 ``reddit_posts``。

两边都可以用 ``--artifact-names legacy|neutral`` 强制覆盖。
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "ARTIFACT_KINDS",
    "NEUTRAL_NAMES",
    "LEGACY_NAMES",
    "artifact_name",
    "artifact_path",
    "resolve_artifact",
    "NAME_MODES",
]

#: ``auto`` 由调用方按源判定；``legacy`` / ``neutral`` 强制。
NAME_MODES = ("auto", "legacy", "neutral")

ARTIFACT_KINDS = (
    "tagged", "relevant", "summary", "stats", "data_report", "dimensions", "features",
)

#: 源中立名（新）
NEUTRAL_NAMES: dict[str, str] = {
    "tagged": "units_tagged.csv",
    "relevant": "units_relevant.csv",
    "summary": "dataset_summary.csv",
    "stats": "dataset_stats.json",
    "data_report": "dataset_data_report.md",
    "dimensions": "dataset_dimensions.json",
    "features": "dataset_features.json",
}

#: 历史名（Reddit 时期定下的，带 reddit_ 前缀）
LEGACY_NAMES: dict[str, str] = {
    "tagged": "reddit_posts_tagged.csv",
    "relevant": "reddit_posts_relevant.csv",
    "summary": "reddit_analysis_summary.csv",
    "stats": "reddit_analysis_stats.json",
    "data_report": "reddit_analysis_data_report.md",
    "dimensions": "reddit_analysis_dimensions.json",
    "features": "reddit_analysis_features.json",
}


def _pick(kind: str, mode: str, is_reddit: bool) -> bool:
    """返回 True 表示用 legacy 名。"""
    if kind not in NEUTRAL_NAMES:
        raise KeyError(f"未知产物类型：{kind}（可用：{', '.join(ARTIFACT_KINDS)}）")
    if mode == "legacy":
        return True
    if mode == "neutral":
        return False
    if mode != "auto":
        raise ValueError(f"未知命名模式：{mode}（可用：{', '.join(NAME_MODES)}）")
    return is_reddit


def artifact_name(kind: str, mode: str = "auto", is_reddit: bool = True) -> str:
    """按模式取产物文件名。"""
    table = LEGACY_NAMES if _pick(kind, mode, is_reddit) else NEUTRAL_NAMES
    return table[kind]


def artifact_path(out_dir: str | Path, kind: str, mode: str = "auto",
                  is_reddit: bool = True) -> Path:
    return Path(out_dir) / artifact_name(kind, mode, is_reddit)


def resolve_artifact(dir_path: str | Path, kind: str) -> Path | None:
    """在目录里找某类产物：先中性名、后历史名。找不到返回 ``None``。"""
    if kind not in NEUTRAL_NAMES:
        raise KeyError(f"未知产物类型：{kind}")
    d = Path(dir_path)
    for n in (NEUTRAL_NAMES[kind], LEGACY_NAMES[kind]):
        p = d / n
        if p.exists():
            return p
    return None
