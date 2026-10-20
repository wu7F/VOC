#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标签设计（reddit分析 专用）—— 参考 标签设计 skill 的 5 字段结构。

把原「LLM 直接归纳标签值 + 关键词补标」升级为「先设计标签集、再逐条标注」：本脚本只产出
**标签集定义**（不含 count/证据），供 annotate_posts.py 标注与 aggregate_annotations.py 聚合消费。

**维度框架由 ``--frame`` 决定（Stage 5）**：维度清单、标签 category、「用哪个分类器
分桶、桶怎么映射到画像维度」全部读 ``frames/<name>.yaml``。不带 ``--frame`` 时加载内置
``consumer``，与平台化之前逐字节等价（8 个维度 + 按购买状态分两桶）。

除「分段轴分类器维度」（``kind: classifier``，由脚本注入固定分类器）外，各维度下的
**标签值**由 LLM 基于品类 + A+B 样本语义归纳生成。其中 ``features``(功能体验) 维度必须
按本次品类重新生成功能点标签。

分段轴（segmentation axis）—— 消费品的轴是购买状态，B2B 可以是决策角色：
  * 人群画像不按「发帖人 / 评论者」拆两套标签，而是**按分段轴**拆维度——消费品是
    `user_personas_purchased` / `user_personas_non_purchased`。两者共用一套标注单元：
    帖子单元 + 评论单元**合并计数**。
  * 轴维度是一个**固定分类器维度**（脚本注入，不交给 LLM 生成）：每个单元先被判成
    恰好一个桶标签，聚合时只把该单元计入对应画像桶，保证两桶严格互斥。
  * **无法判定的单元不进画像桶**，聚合阶段直接排除（数量以一行披露写进报告）。
    原第三个维度 `user_personas_unknown`（购买状态不明者人群画像）已于 2026-10-06 废弃，
    在 consumer 框架里声明为 ``deprecated``，本脚本不再生成该维度。
  * 归纳人群画像时**同时参考对应帖子的评论**（`--comments`，每帖最多 `--comments-per-post`
    条、每条截断 `--comment-chars` 字）。评论在 prompt 中被显式标注为
    「评论片段，非发帖人自述」，避免把他人观点误当发帖人自述；人群标签需同时覆盖
    发帖人与评论者两类发言者。
  * 样本量默认 **100 条**（`--nsample`）。

LLM 后端（Stage 4）：可插拔，见 ``voc_core/llm.py``。

* ``--backend auto``（默认）有 openai/local 配置就用它，否则切 ``manual`` 人工兜底队列
* ``--backend openai|local`` 真调 OpenAI 兼容 HTTP 端点
* ``--backend manual`` 任务落盘待回填，回填后**重跑即命中**（退出码 3，不是失败）
* ``--backend mock|replay`` 跑通链路 / 离线重放
* ``--mock`` 仍是内核自带的「输出内置示例标签集」，不走任何后端

提示词在 ``prompts/design_tags.*.zh.md``（``--lang`` 可换），其中**跟分析框架有关**的
段落（维度清单 / 分析目的 / 分段轴说明 / 画像特殊要求 / JSON 形状）由框架渲染，
见 ``voc_core/frame_prompts.py``。**没有 ``sys.exit`` 了**：
退出码 0=成功 / 2=配置错误 / 3=等待人工回填。

运行：
  python3 design_tags.py --category "Automatic Pet Feeder" \
      --relevant data/reddit_analysis/reddit_posts_relevant.csv \
      --comments data/reddit_analysis/reddit_comments.csv \
      --out tags/reddit_tagset.json [--frame consumer|b2b_role|frames/x.yaml] \
      [--backend openai|manual|mock | --mock]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

# 平台侧公共设施：列名归一化 + 源画像 + LLM 后端容错加载 + 分析框架
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kernel_common import (  # noqa: E402
    add_frame_argument,
    add_llm_arguments,
    add_profile_argument,
    canonicalize_rows,
    configure_llm,
    import_llm_connector,
    llm_backend_note,
    llm_waiting_note,
    load_frame,
    load_profile,
)

from voc_core import classifiers as C  # noqa: E402
from voc_core import frame_prompts as FP  # noqa: E402
from voc_core import prompts as P  # noqa: E402
from voc_core.analysis_frame import AnalysisFrame  # noqa: E402

_llm = import_llm_connector()
call_llm = _llm.call_llm
is_external_backend = _llm.is_external_backend
LocalFallback = _llm.LocalFallback
ManualPending = _llm.ManualPending
#: 退出码：3 = 等待人工回填（不是失败，重跑即可继续）
EXIT_PENDING = _llm.EXIT_PENDING
EXIT_CONFIG = _llm.EXIT_CONFIG

# --------------------------------------------------------------------------
# 默认框架（consumer）的只读视图
# --------------------------------------------------------------------------
# 平台化之前，``DIMENSIONS`` / ``SAMPLE_TAGS_PER_DIM`` 是写死的常量；现在它们只是
# **内置 consumer 框架的一个视图**（从 frames/consumer.yaml 派生），保留同名导出是为了
# 让测试与外部工具还能问「默认框架长什么样」。真正的逻辑一律走 ``frame`` 参数 ——
# 下面每个函数都接受 ``frame=None``，``None`` 即「用内置 consumer」，也就是改造前行为。
_DEFAULT_FRAME: AnalysisFrame | None = None


def default_frame() -> AnalysisFrame:
    """内置 consumer 框架（带缓存）。``frame=None`` 时的落点。"""
    global _DEFAULT_FRAME
    if _DEFAULT_FRAME is None:
        _DEFAULT_FRAME = load_frame(None)
    return _DEFAULT_FRAME


def _dims_view(frame: AnalysisFrame | None = None) -> dict[str, tuple[str, str]]:
    """``{维度 id: (中文 label, category)}``（按声明顺序）。"""
    f = frame or default_frame()
    return {d.id: (d.label, d.category) for d in f.active_dims}


#: 兼容别名（只描述**默认框架** consumer）。权威值在 ``frame.sample_tags_per_dim``。
SAMPLE_TAGS_PER_DIM = default_frame().sample_tags_per_dim
DEFAULT_NSAMPLE = 100          # 人群画像/整体归纳的样本帖数（原为 40）
DEFAULT_COMMENTS_PER_POST = 10  # 每帖最多参考的评论条数
DEFAULT_COMMENT_CHARS = 500     # 每条评论截断字数
DEFAULT_COMMENT_BLOCK_BUDGET = 120000  # 评论块总字符预算（≈3 万 token），防止 prompt 撑爆上下文

# --------------------------------------------------------------------------
# 提示词（Stage 4 外置 / Stage 5 框架化）
# --------------------------------------------------------------------------
# Stage 4 把三段提示词从内嵌常量搬到 prompts/*.zh.md；Stage 5 又把其中
# **跟分析框架有关**的部分（维度清单 / 分析目的 / 轴说明 / 画像特殊要求 / JSON 形状）
# 抽进 frames/*.yaml —— 否则换个框架（例如 B2B 按决策角色分桶）模型照样输出合法
# JSON，只是每一维都在回答错误的问题，而且**错得很安静**。
#
# 变量语法是 ${name}（string.Template）而不是 str.format：这些提示词里**本来就
# 满是花括号**（`{tk}_summary`、`{summary}`、大段 JSON 结构示例），用 format 就得把
# 每一处都手写成 {{ }}，漏一个就静默改坏了提示词。
def build_design_prompts(frame: AnalysisFrame, *, category: str, nsample: int,
                         samples: str, comment_block: str,
                         unit: str, secondary: str, secondary_speaker: str,
                         lang: str = P.DEFAULT_LANG) -> tuple[str, str]:
    """渲染 design_tags 的 ``(system, user)`` 提示词。

    **这是本脚本唯一产出提示词的地方** —— 测试与验收工具都从这里取，
    避免在别处重写一遍「哪些变量要传」而悄悄与内核分叉。
    """
    parts = FP.prompt_parts(frame, category=category, unit=unit,
                            secondary=secondary, secondary_speaker=secondary_speaker)
    # ``P.render`` 是严格模式：声明了却没传 / 传了没声明都会报错。
    # system 模板的占位符集合由框架渲染结果决定，这里按模板声明逐一对齐。
    system = P.render("design_tags.system", lang,
                      **{k: parts.get(k, "") for k in P.template_vars("design_tags.system", lang)})
    user = P.render("design_tags.user", lang,
                    category=category, nsample=nsample,
                    samples=samples, comment_block=comment_block,
                    purpose_line=parts["purpose_line"],
                    dim_block=parts["dim_block"],
                    axis_note=parts["axis_note"],
                    summary_line=parts["summary_line"])
    return system, user

def load_relevant(path: Path, nsample: int = DEFAULT_NSAMPLE) -> list[dict]:
    # 用 with 打开：裸 ``path.open()`` 靠引用计数回收，会留下 ResourceWarning
    # （Windows 上文件锁也释放得晚），跑测试时每次都能看到。
    with path.open(encoding="utf-8", newline="") as f:
        rows = canonicalize_rows(list(csv.DictReader(f)), "posts")
    out = []
    for r in rows[:nsample]:
        title = (r.get("title") or "").strip()
        summary = (r.get("summary_text") or r.get("summary") or r.get("selftext") or "").strip()
        out.append({"reddit_id": r.get("reddit_id"), "title": title, "summary": summary[:400]})
    return out


def load_comments(path: Path, post_ids: set[str], per_post: int, chars: int) -> dict[str, list[str]]:
    """读评论并按帖子分组（每帖最多 per_post 条、每条截断 chars 字）。"""
    by_post: dict[str, list[str]] = {}
    if not path or not Path(path).exists():
        return by_post
    with Path(path).open(encoding="utf-8", newline="") as f:
        for r in canonicalize_rows(csv.DictReader(f), "comments"):
            pid = (r.get("post_reddit_id") or "").strip()
            body = (r.get("body") or "").strip()
            if not pid or not body:
                continue
            if post_ids and pid not in post_ids:
                continue
            lst = by_post.setdefault(pid, [])
            if len(lst) < per_post:
                lst.append(body[:chars])
    return by_post


def build_samples(rows: list[dict], nsample: int,
                  comments_by_post: dict[str, list[str]] | None = None,
                  per_post: int = DEFAULT_COMMENTS_PER_POST,
                  chars: int = DEFAULT_COMMENT_CHARS,
                  include_comments: bool = True,
                  block_budget: int = DEFAULT_COMMENT_BLOCK_BUDGET) -> tuple[str, str, dict]:
    """返回 (帖子样本文本, 评论块文本, 统计)。

    评论块显式标注「评论片段，非发帖人自述」。`block_budget` 为评论块的总字符预算——
    100 帖 × 10 条 × 500 字上限可达 50 万字（≈12 万 token），会撑爆上下文；
    超出预算时按顺序截断并回报 dropped 条数，便于判断是否需要调小 --nsample。
    """
    lines = []
    post_blocks: list[tuple[str, list[str]]] = []
    used = 0
    dropped = 0
    for i, r in enumerate(rows[:nsample], 1):
        t = r["title"] or "(无标题)"
        s = r["summary"] or ""
        lines.append(f"[{i}] (id={r['reddit_id']}) {t}\n    {s}")
        if not include_comments or not comments_by_post:
            continue
        cms = comments_by_post.get(r["reddit_id"] or "") or []
        if not cms:
            continue
        header = f"[{i}] (post_id={r['reddit_id']}) 原帖：{t}"
        clines: list[str] = []
        for j, c in enumerate(cms[:per_post], 1):
            line = f"    c{j}. 「评论片段，非发帖人自述」{c}"
            if block_budget and used + len(line) > block_budget:
                dropped += 1
                continue
            used += len(line)
            clines.append(line)
        if clines:
            post_blocks.append((header, clines))

    comment_block = ""
    if post_blocks:
        n_comments = sum(len(cl) for _, cl in post_blocks)
        flat: list[str] = []
        for header, clines in post_blocks:
            flat.append(header)
            flat.extend(clines)
        # 评论块自带「这是评论片段、不是发帖人自述」的说明——那句提示词也在
        # prompts/design_tags.comment_block.zh.md 里，不在代码里。
        comment_block = P.render(
            "design_tags.comment_block",
            ncomments=n_comments, nposts=len(post_blocks),
            comments="\n".join(flat))
    stats = {"comments_included": sum(len(cl) for _, cl in post_blocks),
             "posts_with_comments": len(post_blocks),
             "comments_dropped_by_budget": dropped,
             "block_chars": len(comment_block)}
    return "\n".join(lines), comment_block, stats


def extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def _build_tag(tk, cat, thought, summary_hint, reason=None, label_cn=None, mock_kw=None):
    t = {
        "label_cn": label_cn or tk,
        "category": cat,
        "thoughts": thought,
        "mentioned": "Output 'yes' or 'no'. If 'no', leave all other fields blank.",
        "original_text": "Extract the exact phrase(s) from the post that support this tag.",
        f"{tk}_summary": summary_hint,
        "sentiment": "positive / neutral / negative" if cat in ("post_purchase", "feature") else "n/a",
    }
    if reason is not None:
        t[f"{tk}_reason"] = reason
    if mock_kw is not None:
        # 仅 mock 测试用：英文匹配词（出现在 reddit 原文中），不参与真实标注契约
        t["mock_keywords"] = "||".join(mock_kw)
    return t


#: mock 的通用占位标签（按**维度 id** 命中时优先用它，保证 consumer 的 mock 产物
#: 与平台化之前逐字节一致）。
_GENERIC_BY_DIM = {
    "user_personas_purchased": [("purchased_group_a", "已购人群A"), ("purchased_group_b", "已购人群B")],
    "user_personas_non_purchased": [("nonpurchased_group_a", "未购人群A"), ("nonpurchased_group_b", "未购人群B")],
    "usage_scenarios": [("scene_a", "场景A"), ("scene_b", "场景B")],
    "pre_purchase_concerns": [("pre_a", "购前关注A"), ("pre_b", "购前关注B")],
    "post_purchase_experience": [("post_issue", "负面体验"), ("post_ok", "正面体验")],
    "features": [("feature_a", "功能A"), ("feature_b", "功能B")],
    "unmet_needs": [("need_a", "需求A"), ("need_b", "需求B")],
}
#: 兜底：按 **category** 生成占位标签，键名带维度 id 前缀（否则多个同 category 的
#: 维度会撞出同名标签键，平坦化后互相覆盖）。换框架（B2B / App 订阅）时用它，
#: 免得 mock 产物里整片画像维度是空的、聚合阶段什么都没得算。
_GENERIC_BY_CATEGORY = {
    "user": ["画像人群A", "画像人群B"],
    "scene": ["场景A", "场景B"],
    "pre_purchase": ["购前关注A", "购前关注B"],
    "post_purchase": ["负面体验", "正面体验"],
    "feature": ["功能A", "功能B"],
}


def mock_tagset(category: str, frame: AnalysisFrame | None = None) -> dict:
    f = frame or default_frame()
    key = category.strip().lower()
    has_consumer_dims = "user_personas_purchased" in f.dim_ids()
    if key in ("automatic pet feeder", "pet feeder", "自动宠物喂食器") and has_consumer_dims:
        dims = {
            "user_personas_purchased": ("购买者人群画像", {
                "owner_multi_cat": _build_tag("owner_multi_cat", "user", "仅当发言者已购买喂食器且为多猫/多宠家庭时标 yes，围绕分食防抢食。", "已购·多猫家庭，围绕分食防抢食", mock_kw=["cats", "cat"]),
                "owner_long_term": _build_tag("owner_long_term", "user", "仅当发言者已购买并长期使用（数月以上）时标 yes。", "已购·长期使用用户", mock_kw=["year", "years", "month"]),
                "owner_weight_mgmt": _build_tag("owner_weight_mgmt", "user", "仅当发言者已购买且用于宠物体重/热量管理时标 yes。", "已购·体重管理用途", mock_kw=["diet", "food"]),
            }),
            "user_personas_non_purchased": ("未购买者人群画像", {
                "shopper_first_time": _build_tag("shopper_first_time", "user", "仅当发言者尚未购买、首次选购并求推荐时标 yes。", "未购·首次选购求推荐", mock_kw=["recommend", "best"]),
                "shopper_comparing": _build_tag("shopper_comparing", "user", "仅当发言者尚未购买、正在多款/多品牌间比较时标 yes。", "未购·多品牌比较中", mock_kw=["vs", "compare"]),
                "shopper_budget": _build_tag("shopper_budget", "user", "仅当发言者尚未购买且明确受预算约束时标 yes。", "未购·预算敏感", mock_kw=["budget", "cheap"]),
            }),
            "usage_scenarios": ("使用场景", {
                "away_travel": _build_tag("away_travel", "scene", "判断是否外出/旅行/短期离家场景。", "外出/旅行/短期离家", mock_kw=["away", "trip"]),
                "daytime_work": _build_tag("daytime_work", "scene", "判断是否白天上班/工作时段的定时投喂。", "白天上班/工作时段定时投喂", mock_kw=["work", "schedule"]),
                "multi_pet_zone": _build_tag("multi_pet_zone", "scene", "判断是否多宠分区/分碗场景。", "多宠分区/分碗", mock_kw=["cats", "cat"]),
            }),
            "pre_purchase_concerns": ("购买前关注点", {
                "portion_accuracy": _build_tag("portion_accuracy", "pre_purchase", "判断购前是否关注分量控制精度。", "分量控制精度", mock_kw=["portion", "food"]),
                "brand_reliability": _build_tag("brand_reliability", "pre_purchase", "判断购前是否关注品牌可靠性/口碑。", "品牌可靠性/口碑", mock_kw=["reliable", "automatic"]),
                "wifi_app_control": _build_tag("wifi_app_control", "pre_purchase", "判断购前是否关注 WiFi/App 控制能力。", "WiFi/App 控制能力", mock_kw=["app", "wifi"]),
            }),
            "post_purchase_experience": ("购买后体验", {
                "jam_issue": _build_tag("jam_issue", "post_purchase", "判断是否反馈卡粮/不出粮问题。", "卡粮/不出粮问题", ["卡粮堵塞", "出粮口设计缺陷", "粮型不匹配"], mock_kw=["jam", "stuck"]),
                "easy_clean": _build_tag("easy_clean", "post_purchase", "判断是否满意清洁便利性。", "清洁便利性", ["可拆卸清洗", "无死角", "配件易购"], mock_kw=["clean", "wash"]),
                "app_reliability": _build_tag("app_reliability", "post_purchase", "判断是否反馈 App/连接稳定性问题。", "App/连接稳定性", ["断连", "通知延迟", "配网失败"], mock_kw=["app", "connect"]),
            }),
            "features": ("功能体验", {
                "portion_control": _build_tag("portion_control", "feature", "判断分量控制功能体验。", "分量控制", ["精度不足", "档位太少", "忽多忽少"], mock_kw=["portion", "food"]),
                "scheduling": _build_tag("scheduling", "feature", "判断定时投喂功能体验。", "定时投喂", ["定时不准", "时区错", "漏投"], mock_kw=["schedule", "timer"]),
                "build_quality": _build_tag("build_quality", "feature", "判断做工/材质体验。", "做工/材质", ["做工扎实", "塑料感", "掉漆"], mock_kw=["plastic", "sturdy"]),
                "battery_backup": _build_tag("battery_backup", "feature", "判断断电续航能力。", "断电续航", ["断电停摆", "续航短", "无备份"], mock_kw=["battery", "power"]),
            }),
            "unmet_needs": ("未满足需求", {
                "microchip_id": _build_tag("microchip_id", "feature", "判断是否期望芯片识别分食。", "芯片识别分食", ["无法分食", "抢食", "需要识别"], mock_kw=["rfid", "chip"]),
                "wet_food_support": _build_tag("wet_food_support", "feature", "判断是否期望湿粮/冷藏支持。", "湿粮/冷藏支持", ["只支持干粮", "需冷藏", "湿粮堵塞"], mock_kw=["wet", "water"]),
            }),
        }
    else:
        # 通用 fallback：按**框架的维度清单**生成占位标签（每维度 2 个）。
        dims = {}
        for d in f.active_dims:
            picks = _GENERIC_BY_DIM.get(d.id)
            if picks:
                tags = {tk: _build_tag(tk, d.category, f"判断是否与「{hint}」相关。", hint)
                        for tk, hint in picks}
            else:
                tags = {f"{d.id}_{i}": _build_tag(f"{d.id}_{i}", d.category,
                                                 f"判断是否与「{hint}」相关。", hint)
                        for i, hint in enumerate(
                            _GENERIC_BY_CATEGORY.get(d.category, [f"{d.label}A", f"{d.label}B"]), 1)}
            dims[d.id] = (d.label, tags)
    CN = {
        "owner_multi_cat": "已购·多猫家庭主人", "owner_long_term": "已购·长期使用用户",
        "owner_weight_mgmt": "已购·体重管理用途",
        "shopper_first_time": "未购·首次选购者", "shopper_comparing": "未购·多品牌比较者",
        "shopper_budget": "未购·预算敏感者",
        "unknown_short_reply": "状态不明·纯附和短评", "unknown_lurker": "状态不明·泛泛围观",
        "unknown_third_party": "状态不明·第三方视角",
        "multi_cat_owner": "多猫家庭主人", "office_frequent_out": "上班族与经常外出者",
        "weight_mgmt_user": "体重管理需求用户", "away_travel": "外出与旅行离家",
        "daytime_work": "白天上班时段", "multi_pet_zone": "多宠分区",
        "portion_accuracy": "分量控制精度", "brand_reliability": "品牌可靠性",
        "wifi_app_control": "WiFi与App控制", "jam_issue": "卡粮问题",
        "easy_clean": "易于清洁", "app_reliability": "App连接稳定性",
        "portion_control": "分量控制", "scheduling": "定时投喂",
        "build_quality": "做工与材质", "battery_backup": "断电续航",
        "microchip_id": "芯片识别分食", "wet_food_support": "湿粮与冷藏支持",
    }
    for dk, (lab, tags) in dims.items():
        for tk, td in tags.items():
            if tk in CN:
                td["label_cn"] = CN[tk]
    out = {"tagset": {"name": f"{category} 标签集", "category": category,
                      # mock 固定件：这条文案是 consumer 框架的说明，只用于测试可读性，
                      # 不参与提示词渲染，因此刻意保持与平台化之前逐字节一致。
                      "analysis_purpose": "Reddit 用户原声多维分析（人群按购买状态分边）",
                      "description": "mock 标签集（测试用）",
                      "dimensions": {dk: {"label": v[0], "tags": v[1]} for dk, v in dims.items()}}}
    return out


def inject_classifier_dims(ts: dict, frame: AnalysisFrame | None = None) -> None:
    """把框架里的**分段轴分类器维度**注入标签集（覆盖 LLM 可能自行输出的同名维度）。

    为什么要固定：这是「归桶闸门」——每个单元先被判成恰好命中一个桶标签，
    聚合时只把该单元计入对应画像桶。交给 LLM 自由生成会导致跨品类键名漂移、
    判定标准不一致，闸门就失效了。因此标签键与判定口径在 ``classifiers.py`` 里
    硬编码（**不跟着框架文案一起改**：口径漂移会让份额数字悄悄不可比），
    只让 LLM 生成画像标签值。
    """
    f = frame or default_frame()
    ts.setdefault("dimensions", {})
    for d in f.classifier_dims():
        cname = f.axis.classifier if (f.axis and f.axis.dim == d.id) else d.id
        cdef = C.get_classifier(cname)
        ts["dimensions"][d.id] = {"label": d.label,
                                  "tags": {k: dict(v) for k, v in cdef.tags.items()}}


#: 兼容别名：平台化之前只有「购买状态」一个分类器。
inject_purchase_status = inject_classifier_dims


def prune_unknown_dims(ts: dict, frame: AnalysisFrame | None = None) -> list[str]:
    """剪掉模型自行输出、但已不在框架内的旧维度键（如 user_personas / user_personas_comments）。

    不剪会有两个后果：标注阶段白白多标几十个标签；聚合阶段会把这些维度写进
    dimensions.json，看板出现无法识别的空图表。

    注意用的是 ``frame.dim_ids()``（**只含启用中的维度**）：``deprecated`` 是给
    「读旧标签集」用的安全垫，不该让模型新写的废弃维度留下来。
    """
    f = frame or default_frame()
    allowed = set(f.dim_ids())
    dims = ts.get("dimensions", {})
    extra = [dk for dk in dims if dk not in allowed]
    for dk in extra:
        dims.pop(dk, None)
    return extra


def apply_persona_hints(ts: dict, frame: AnalysisFrame | None = None) -> None:
    """给画像维度的标签补「判定前置」前缀。

    画像标签必须自带「只在该桶条件下才可能标 yes」的口径，否则同一批人会同时落进
    两个桶（下游聚合只按分段轴归桶，标签定义必须自洽）。前缀文案住在
    ``frames/*.yaml`` 的维度 ``analysis_hint`` 里 —— 换框架就自动换口径。
    """
    f = frame or default_frame()
    dims = ts.get("dimensions", {})
    for d in f.active_dims:
        if not d.analysis_hint:
            continue
        for _tk, td in ((dims.get(d.id) or {}).get("tags") or {}).items():
            th = td.get("thoughts") or ""
            if th and not th.startswith("【判定前置】"):
                td["thoughts"] = d.analysis_hint + th


def validate_tagset(ts: dict, frame: AnalysisFrame | None = None) -> list[str]:
    f = frame or default_frame()
    errors = []
    dims = ts.get("dimensions", {})
    for d in f.active_dims:
        dk = d.id
        if dk not in dims:
            if not d.optional:
                errors.append(f"缺失维度 {dk}")
            continue
        tags = dims[dk].get("tags", {})
        if not tags:
            errors.append(f"维度 {dk} 无标签")
        for tk, td in tags.items():
            for field in ("thoughts", "mentioned", "original_text", "sentiment"):
                if field not in td:
                    errors.append(f"{dk}.{tk} 缺字段 {field}")
            if f"{tk}_summary" not in td:
                errors.append(f"{dk}.{tk} 缺 {tk}_summary")
            if "category" not in td:
                errors.append(f"{dk}.{tk} 缺 category")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(
        description="标签设计：按分析框架（--frame）生成多维标签集；默认框架把人群画像按购买状态拆桶")
    ap.add_argument("--category", required=True, help="品类名")
    ap.add_argument("--relevant", required=True, help="A/B 相关单元 CSV 路径（列名由 --profile 归一化）")
    ap.add_argument("--out", required=True, help="输出标签集 JSON 路径")
    ap.add_argument("--comments", default="", help="次级单元 CSV 路径（把评论片段一并喂给模型归纳人群画像标签）")
    ap.add_argument("--comments-out", default="", help="【已废弃·忽略】v3 起不再单独产出评论者人群画像标签集，评论与帖子共用同一套标签")
    ap.add_argument("--comments-per-post", type=int, default=DEFAULT_COMMENTS_PER_POST, help="每帖最多参考的评论条数（默认 10）")
    ap.add_argument("--comment-chars", type=int, default=DEFAULT_COMMENT_CHARS, help="每条评论截断字数（默认 500）")
    ap.add_argument("--comment-block-budget", type=int, default=DEFAULT_COMMENT_BLOCK_BUDGET,
                    help=f"评论块总字符预算，超出按顺序截断（默认 {DEFAULT_COMMENT_BLOCK_BUDGET}；设 0 表示不限）")
    ap.add_argument("--mock", action="store_true",
                    help="测试模式：输出内置示例标签集（不走 LLM，也不走 mock 后端）")
    ap.add_argument("--nsample", type=int, default=DEFAULT_NSAMPLE, help=f"喂给 LLM 的样本帖数（默认 {DEFAULT_NSAMPLE}）")
    add_profile_argument(ap)
    add_frame_argument(ap)
    add_llm_arguments(ap, default_temperature=0.3)
    args = ap.parse_args()

    # 标签设计求「多样」：同一品类希望每次归纳出略有差异的标签值集合。
    cfg = configure_llm(args, default_temperature=0.3)
    # 分析框架：决定「分析什么」——维度清单、分桶轴、桶到画像维度的映射。
    frame = load_frame(args.frame)
    # 源画像：决定「怎么称呼数据」——帖子/评论 还是 评论/用户（与框架正交）。
    profile = load_profile(getattr(args, "profile", "") or None)
    unit = profile.term("unit_noun", "帖子")
    secondary = profile.term("secondary_noun", "评论")
    secondary_speaker = profile.term("secondary_speaker", "评论者")
    print(f"[design_tags] 分析框架={frame.name}（{frame.label}）"
          f" 维度={len(frame.active_dims)} 轴={frame.axis.label if frame.axis else '（无分段轴）'}")

    rows = load_relevant(Path(args.relevant), args.nsample)
    post_ids = {r["reddit_id"] for r in rows if r.get("reddit_id")}
    comments_by_post: dict[str, list[str]] = {}
    if args.comments:
        comments_by_post = load_comments(Path(args.comments), post_ids,
                                         args.comments_per_post, args.comment_chars)
    n_cmt_posts = len(comments_by_post)
    n_cmts = sum(len(v) for v in comments_by_post.values())
    print(f"[design_tags] 品类={args.category} 样本帖={len(rows)} "
          f"（上限 {args.nsample}）| 参考评论：{n_cmts} 条 / 覆盖 {n_cmt_posts} 帖"
          f"（每帖 ≤{args.comments_per_post} 条 × {args.comment_chars} 字）")

    samples, comment_block, cstats = build_samples(rows, args.nsample, comments_by_post,
                                                   args.comments_per_post, args.comment_chars,
                                                   include_comments=bool(comments_by_post),
                                                   block_budget=args.comment_block_budget)
    print(f"[design_tags] 评论块：纳入 {cstats['comments_included']} 条 / {cstats['posts_with_comments']} 帖，"
          f"字符数={cstats['block_chars']}"
          + (f"，因预算截断 {cstats['comments_dropped_by_budget']} 条" if cstats["comments_dropped_by_budget"] else "")
          + "（已标注「评论片段，非发帖人自述」）")

    if args.mock:
        data = mock_tagset(args.category, frame)
    else:
        print(llm_backend_note(cfg))
        system, user = build_design_prompts(
            frame, category=args.category, nsample=len(rows), samples=samples,
            comment_block=comment_block, unit=unit, secondary=secondary,
            secondary_speaker=secondary_speaker, lang=cfg.lang)
        try:
            content, _ = call_llm(system, user, json_mode=True, return_usage=True)
        except ManualPending as exc:
            # 人工兜底：任务已入队。**不是失败**，退出码 3，回填后重跑即可。
            print(llm_waiting_note(exc))
            return EXIT_PENDING
        except LocalFallback as exc:
            print(f"[design_tags] LLM 后端不可用：{exc}")
            print("      换后端：--backend mock 跑通链路 / --backend manual 落盘待回填；"
                  "或配置 --base-url + --api-key，或写 voc.llm.json。")
            return EXIT_CONFIG
        data = extract_json(content)

    ts = data.get("tagset", data) if isinstance(data, dict) else {}
    ts.setdefault("name", f"{args.category} 标签集")
    ts.setdefault("category", args.category)
    ts.setdefault("dimensions", {})
    # 剪掉模型自行输出的框架外维度键（user_personas / user_personas_comments 等）
    extra = prune_unknown_dims(ts, frame)
    if extra:
        print(f"[design_tags] 已剪掉框架外维度：{', '.join(extra)}")
    for d in frame.active_dims:
        ts["dimensions"].setdefault(d.id, {"label": d.label, "tags": {}})
    # 画像标签必须明确本桶的判定口径（前缀文案来自框架的 analysis_hint）。
    # 注：状态不明（无法判断）的单元不进画像桶，被排除的桶在框架里标 excluded。
    apply_persona_hints(ts, frame)
    # 注入固定的分段轴分类器（覆盖 LLM 可能自行输出的版本）
    inject_classifier_dims(ts, frame)
    # 产物自描述：把框架写进标签集，下游 annotate/aggregate/report/dashboard 从
    # 产物读框架，不必各自再挂一个 --frame 再祈求它们保持一致。
    # mock 后端也写：整条链路（annotate → aggregate → 看板）都靠它。
    ts["analysis_frame"] = frame.to_metadata()

    errors = validate_tagset(ts, frame)
    if errors:
        print("[design_tags] 校验警告:")
        for e in errors:
            print("  -", e)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"tagset": ts}, ensure_ascii=False, indent=2), encoding="utf-8")
    ntags = sum(len(v.get("tags", {})) for v in ts["dimensions"].values())

    print(f"[design_tags] 已写标签集: {out}  维度={len(ts['dimensions'])} 标签总数={ntags}"
          f"  框架={frame.name}")
    if frame.axis is not None:
        # 每个桶的标签数按「映射到该桶的画像维度」统计
        per = {d.persona_bucket: d.id for d in frame.active_dims if d.persona_bucket}
        shown = [f"{b.persona_short or b.label_cn} "
                 f"{len(ts['dimensions'].get(per.get(b.id, ''), {}).get('tags', {}))}"
                 for b in frame.axis.active_buckets]
        excl = frame.axis.excluded_bucket
        print(f"[design_tags] 画像分桶（{frame.axis.label}）：{' / '.join(shown)} 个标签 "
              f"（共用单元合并计数，按 {frame.axis.dim} 归桶；"
              f"「{excl.short_label if excl else '无法判断'}」单元不进画像）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
