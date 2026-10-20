#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内核公共设施 —— 列名归一化 + 画像加载 + LLM 连接器容错。

这个模块是**唯一**被内核脚本 import 的平台侧依赖，它解决三件事：

1. **列名归一化**（数据边界去域名化）
   内核到处写 ``r["reddit_id"]`` / ``r["subreddit"]`` / ``r["summary_text"]``。
   与其把 200+ 处引用逐个改名（高风险、零收益），不如**在读取入口补键**：
   任意源的列名（``unit_id`` / ``channel`` / ``text``）会被翻译成内核认识的
   旧列名**同时保留原键**。对 Reddit 输入而言这是恒等变换，行为零变化。

2. **画像加载**（呈现边界去域名化）
   ``--profile`` 参数可以是内置源名或 JSON 路径，内核通过
   ``profile.term("channel_section")`` 取用户可见文案。

3. **LLM 连接器容错加载**
   vendor 后内核不再保证 ``LLM连接器`` skill 一定在位（它可能未安装、或
   部署到没有 WorkBuddy skill 目录的服务器）。因此加载失败时降级为
   ``LocalFallback`` 语义，而不是 ImportError 崩溃。

设计约束：本模块**不 import 内核**，内核单向依赖它，避免循环。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

# 平台根（voc-platform/）—— 让内核脚本能 import voc_core
PLATFORM_ROOT = Path(__file__).resolve().parents[1]
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

from voc_core.source_profile import SourceProfile, get_profile  # noqa: E402

__all__ = [
    "PLATFORM_ROOT",
    "canonicalize_row",
    "canonicalize_rows",
    "guess_role",
    "load_profile",
    "term",
    "row_get",
    "strip_bom_keys",
    "import_llm_connector",
    "add_llm_arguments",
    "configure_llm",
    "llm_backend_note",
    "FILL_RULES",
    "add_frame_argument",
    "load_frame",
    "load_frame_from_product",
]


#: UTF-8 BOM —— Excel / 第三方导出常带；用 ``encoding="utf-8"`` 打开时它会黏在
#: **第一个列名**上（``"\ufeffreddit_id"``），于是 ``row["reddit_id"]`` 永远取不到值。
BOM = "\ufeff"


def strip_bom_keys(row: dict[str, Any]) -> dict[str, Any]:
    """把被 BOM 污染的首列键改回正常键名（**保持原列顺序**）。

    必须在补键**之前**做，否则补键逻辑会在末尾另造一个同名键 —— 结果是
    ``write_csv`` 的 ``list(rows[0].keys())`` 会写出**重复列**
    （``\\ufeffreddit_id,title,…,reddit_id``），列顺序被打乱、下游多出一列。

    读到 ``\\ufeffreddit_id`` 时，键名会改回 ``reddit_id``（不新增、不换位）。
    """
    if not any(isinstance(k, str) and k.startswith(BOM) for k in row):
        return dict(row)
    out: dict[str, Any] = {}
    for k, v in row.items():
        nk = k.lstrip(BOM) if isinstance(k, str) else k
        if nk in out:
            # 万一同时存在 "\ufeffid" 与 "id"：非空的那个胜出
            if not _has(out, nk) and _has({nk: v}, nk):
                out[nk] = v
            continue
        out[nk] = v
    return out


# --------------------------------------------------------------------------
# 1. 列名归一化
# --------------------------------------------------------------------------
#: 内核期望的旧列名 → 新契约/任意源的候选列名（按优先级）。
#:
#: 这些是**全部**被内核引用的域名列名（用 ``grep -o 'r\["[a-z_]*"\]'`` 枚举
#: 得到），补全它们即完成数据边界的去域名化。
FILL_RULES: dict[str, tuple[str, ...]] = {
    # -- 帖子 ------------------------------------------------------------
    "reddit_id": ("unit_id", "post_id", "postid", "id"),
    "summary_text": ("text", "body", "content", "review_text", "selftext"),
    "published": ("created_at", "date", "review_date", "submission_time", "time"),
    # -- 评论 ------------------------------------------------------------
    "comment_id": ("unit_id", "commentid", "id"),
    "post_reddit_id": ("thread_id", "post_id", "postid", "parent_post_id"),
    "body": ("text", "content", "summary_text", "comment_text"),
    "post_title": ("thread_title", "title"),
    # -- 共用 ------------------------------------------------------------
    "subreddit": ("channel", "post_subreddit", "marketplace", "site", "store_name", "country"),
    "post_subreddit": ("subreddit", "channel", "marketplace", "site", "store_name", "country"),
    "url": ("permalink", "link", "review_url"),
    "author": ("reviewer", "user", "username", "nickname"),
    "title": ("review_title", "headline", "subject"),
    # -- 电商评论 --------------------------------------------------------
    "review_id": ("unit_id", "id"),
    "content": ("text", "body", "summary_text", "review_text"),
    "asin": ("product_id", "sku", "part_number", "supplier_part_number"),
    "product": ("product_name", "item_name", "name"),
    "brand": ("brand_name", "vendor"),
    "country": ("channel", "marketplace", "site"),
    "date": ("created_at", "published", "review_date"),
}

#: 大小写/分隔符不敏感查找用的键（``Review Title`` → ``review_title``）
def _norm_key(k: str) -> str:
    out = []
    prev_lower = False
    for ch in str(k).lstrip(BOM).strip():  # lstrip(BOM)：容忍 "\ufeffreddit_id"
        if ch.isupper() and prev_lower:
            out.append("_")
        if ch.isalnum():
            out.append(ch.lower())
            prev_lower = ch.islower() or ch.isdigit()
        else:
            out.append("_")
            prev_lower = False
    return "".join(out).strip("_")


#: 各 role 该补哪些键（避免 posts 文件被补上 review_id 造成噪声）
ROLE_KEYS: dict[str, tuple[str, ...]] = {
    "posts": ("reddit_id", "summary_text", "published", "subreddit", "url", "author", "title"),
    # 注：不补 post_subreddit / post_title —— 内核只在 JS 端以 `parent.subreddit`
    # 优先、`c.post_subreddit` 兜底地读过它们，补齐会让**旧产物的输出多出键**
    # 而收益近似为零。而反向（新产物的 post_subreddit → subreddit）是必须的，
    # 已由 FILL_RULES["subreddit"] 的别名列表覆盖。
    "comments": ("comment_id", "post_reddit_id", "body", "subreddit",
                 "published", "author", "url"),
    "reviews": ("review_id", "content", "asin", "product", "brand", "country", "date", "author", "url", "title"),
    "auto": tuple(FILL_RULES),
}


def _has(row: dict[str, Any], key: str) -> bool:
    v = row.get(key)
    return v is not None and str(v).strip() != ""


def canonicalize_row(
    row: dict[str, Any],
    role: str = "auto",
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """为一行补上内核认识的旧列名（同时保留原有键）。

    :param role: ``posts`` / ``comments`` / ``reviews`` / ``auto``
    :param overwrite: 目标键已存在且非空时，是否用**别名**的值覆盖它。
        （默认否 —— 已是旧 schema 的输入必须原样透传，这是 Reddit 无回归的前提）
    :return: 补全后的新 dict（不修改入参）

    补键分两步，顺序不能颠倒：

    0. **去 BOM**：首列键若被 UTF-8 BOM 污染（``"\ufeffreddit_id"``）先改回
       ``reddit_id``，否则第 1 步认不出它，会在末尾另造一个同名列。
    1. **变体写法**：目标键本身在源文件里写作 ``Review ID`` / ``review-title``
       这类形式。只在目标键缺失时生效 —— 它补的是「同一个东西的另一种写法」，
       不该覆盖已有值。
    2. **别名**：来自 ``FILL_RULES``（如 ``unit_id`` → ``reddit_id``）。这是
       「换个源换了个列名」的情形，``overwrite=True`` 时才允许覆盖。
    """
    out = strip_bom_keys(row)
    # 建索引：归一化键 → 真实键名
    lookup: dict[str, str] = {}
    for k in out:
        nk = _norm_key(k)
        if nk and nk not in lookup:
            lookup[nk] = k

    for target in ROLE_KEYS.get(role, ROLE_KEYS["auto"]):
        # 1) 变体写法：目标键缺失时才补
        if not _has(out, target):
            real = lookup.get(target)
            if real is not None and _has(out, real):
                out[target] = out[real]
                continue
        elif not overwrite:
            continue  # 已有值且不允许覆盖 → 原样保留

        # 2) 别名
        for cand in FILL_RULES.get(target, ()):
            real = lookup.get(cand)
            if real is None or real == target or not _has(out, real):
                continue
            out[target] = out[real]
            break
    return out


def canonicalize_rows(
    rows: Iterable[dict[str, Any]],
    role: str = "auto",
    *,
    overwrite: bool = False,
) -> list[dict[str, Any]]:
    """批量补键。"""
    return [canonicalize_row(r, role, overwrite=overwrite) for r in rows]


def guess_role(columns: Sequence[str] | dict[str, Any]) -> str:
    """按列名猜文件角色（posts / comments / reviews）。猜不出返回 ``auto``。"""
    cols = {_norm_key(c) for c in (columns.keys() if isinstance(columns, dict) else columns)}
    if cols & {"rating", "review_id", "asin", "review_text", "supplier_part_number"}:
        # 有星级的多半是电商评论；但要排除「帖子文件恰好带 rating 列」的误判
        if not (cols & {"subreddit", "reddit_id"}):
            return "reviews"
    if cols & {"post_reddit_id", "postid", "parentid", "is_root_comment"}:
        return "comments"
    if cols & {"reddit_id", "subreddit", "summary_text", "voc_score"}:
        return "posts"
    if cols & {"comment_id", "commentid"}:
        return "comments"
    if cols & {"review_id", "rating"}:
        return "reviews"
    return "auto"


def row_get(row: dict[str, Any], canonical: str, default: str = "") -> str:
    """按规范名取值（未来内核改用规范名时的读取入口）。

    支持任意别名，命中顺序同 ``FILL_RULES``。
    """
    if _has(row, canonical):
        return str(row[canonical]).strip()
    lookup = {_norm_key(k): k for k in row}
    for cand in FILL_RULES.get(canonical, ()):
        real = lookup.get(cand)
        if real and _has(row, real):
            return str(row[real]).strip()
    return default


# --------------------------------------------------------------------------
# 2. 画像加载
# --------------------------------------------------------------------------

def load_profile(spec: str | Path | dict | None) -> SourceProfile:
    """``--profile`` 参数 → ``SourceProfile``。

    支持：内置名（reddit/amazon/wayfair/generic）、JSON 路径、dict、空（→Reddit）。
    额外支持简写 —— 直接给源名如 ``amazon`` 时也接受 ``Amazon`` 等大小写变体。
    """
    return get_profile(spec)


def term(profile: SourceProfile | str | None, key: str, default: str = "") -> str:
    """便利函数：``term(profile, "channel_section")``。"""
    if not isinstance(profile, SourceProfile):
        profile = load_profile(profile)
    return profile.term(key, default)


def add_profile_argument(parser) -> None:
    """给 argparse 加统一的 ``--profile`` 参数（内核脚本共用）。"""
    parser.add_argument(
        "--profile",
        default=os.getenv("VOC_SOURCE_PROFILE", ""),
        help="源画像：内置名（reddit/amazon/wayfair/generic）或 JSON 路径；"
             "留空等价 reddit（保持旧行为）",
    )


# --------------------------------------------------------------------------
# 3. LLM 连接器容错加载
# --------------------------------------------------------------------------

class LocalFallback(RuntimeError):
    """无可用的外部 LLM 后端。语义与 ``LLM连接器`` skill 中的同名异常一致。"""


def import_llm_connector():
    """加载 LLM 后端模块。

    **查找顺序（Stage 4 起平台实现优先）**：

    1. 已在本进程 ``sys.modules["llm_connector"]`` 里 → 直接用（幂等）
    2. ``VOC_LLM_CONNECTOR_DIR`` 显式指定 → 加载该目录下的 ``llm_connector.py``
    3. **平台自带实现** ``voc_core._llm_connector_stub``（→ :mod:`voc_core.llm`）

    为什么把平台实现提到 WorkBuddy skill **之前**（这是 Stage 4 的一处真修正）：
    原顺序是「先找 ``~/.workbuddy/skills/LLM连接器/scripts/llm_connector.py``」。
    在本机实测发现本机**装了这个 skill**，于是内核 import 到的是那个旧模块 ——
    **Stage 4 的多后端、人工兜底队列、录制重放全部被静默旁路**，
    而链路看起来照常工作（旧模块也有 ``call_llm``），根本看不出问题。
    那种「装了什么就悄悄换掉实现」的行为正是平台化要消灭的。因此现在：

    * 平台实现是权威默认；
    * 想用外部 skill 就显式设 ``VOC_LLM_CONNECTOR_DIR``（意图明确、可审计）；
    * 两者的环境变量契约一致（``LLM_CONNECTOR_*`` 仍然被读取），换回来无需改配置。

    :return: 模块对象；它有 ``call_llm`` / ``is_external_backend`` / ``LocalFallback``，
        平台实现还额外提供 ``ManualPending`` / ``backend_info`` / ``configure``。
    """
    mod = sys.modules.get("llm_connector")
    if mod is not None:
        return mod

    import importlib

    env_dir = os.getenv("VOC_LLM_CONNECTOR_DIR", "").strip()
    if env_dir:
        d = Path(env_dir)
        if (d / "llm_connector.py").exists():
            if str(d) not in sys.path:
                sys.path.insert(0, str(d))
            return importlib.import_module("llm_connector")
        # 显式指定却找不到 —— 不能静默回落到平台实现，那会让"我明明配了"变成假象
        raise ImportError(
            f"VOC_LLM_CONNECTOR_DIR={env_dir} 下没有 llm_connector.py。"
            "要么修正该路径，要么清掉这个环境变量以使用平台自带实现。")

    return importlib.import_module("voc_core._llm_connector_stub")


# --------------------------------------------------------------------------
# 4. LLM 后端的命令行接线（Stage 4）
# --------------------------------------------------------------------------
#
# 内核脚本（design_tags / annotate_posts）需要**同一组** LLM 参数。放在这里而不是
# 各自复制一份：两份参数表迟早会漂移，而漂移的表现是"某个脚本不认某个开关"，
# 属于最难查的一类问题。
#
# 调用方只需两步：
#     add_llm_arguments(ap, default_temperature=0.3)   # 注册参数
#     cfg = configure_llm(args)                        # 解析并设为默认后端

#: ``--backend`` 的可选值（``auto`` 会按配置解析成其中一个）
LLM_BACKEND_CHOICES = ("auto", "openai", "local", "manual", "mock", "replay")


def add_llm_arguments(ap, *, default_temperature: float = 0.3):
    """给 argparse 注册统一的 LLM 参数组。

    ``--temperature`` 的默认值是 ``None``（**不是**具体数字）：只有知道「用户没给」，
    才能让配置文件里的温度生效。任务的默认温度由 :func:`configure_llm` 的
    ``default_temperature`` 传入 —— 标签设计求多样（0.3），逐条标注求稳定（0）。
    """
    g = ap.add_argument_group("LLM 后端（Stage 4）")
    g.add_argument("--backend", default="", choices=("",) + LLM_BACKEND_CHOICES,
                   help="LLM 后端：auto（默认，有 openai/local 配置就用它，否则切人工兜底）"
                        " / openai / local / manual / mock / replay")
    g.add_argument("--model", default="", help="模型名（覆盖配置文件与环境变量）")
    g.add_argument("--base-url", dest="base_url", default="",
                   help="OpenAI 兼容端点，如 https://api.openai.com/v1")
    g.add_argument("--api-key", dest="api_key", default="", help="API Key（本地端点可填任意非空值）")
    g.add_argument("--temperature", type=float, default=None,
                   help=f"采样温度（不传则用配置；本任务默认 {default_temperature}）")
    g.add_argument("--lang", default="", help="提示词语言（prompts/<任务>.<语言>.md，默认 zh）")
    g.add_argument("--llm-config", dest="llm_config", default="",
                   help="LLM 配置文件 JSON（默认找 ./voc.llm.json 或 $VOC_LLM_CONFIG）")
    g.add_argument("--llm-record", dest="llm_record", default="",
                   help="把所有调用录制到该 JSONL（可复现 / 可离线重放）")
    g.add_argument("--llm-replay", dest="llm_replay", default="",
                   help="从录制文件重放（配 --backend replay）")
    g.add_argument("--llm-queue", dest="llm_queue", default="",
                   help="人工兜底队列目录（默认 out/llm_queue）")
    return g


def configure_llm(args, *, default_temperature: float | None = None):
    """按 CLI / 环境 / 配置文件装配 LLM 配置，并设为本进程默认后端。

    优先级：``命令行 > 环境变量 > 配置文件 > 本任务默认 > 全局默认``。

    温度是唯一需要「本任务默认」的参数：标签设计要 0.3（求多样）、逐条标注要 0
    （求稳定），不能共用一个值。实现上靠 ``cfg.sources`` 判断「环境/配置文件有没有
    给过温度」—— 给过就尊重它，没给才落到本任务的默认值。
    """
    from voc_core import llm

    cfg = llm.LLMConfig.resolve(
        config_file=getattr(args, "llm_config", "") or None,
        backend=getattr(args, "backend", ""),
        model=getattr(args, "model", ""),
        base_url=getattr(args, "base_url", ""),
        api_key=getattr(args, "api_key", ""),
        lang=getattr(args, "lang", ""),
        temperature=getattr(args, "temperature", None),
        threads=getattr(args, "threads", None),
        batch_size=getattr(args, "batch_size", None),
        record=getattr(args, "llm_record", ""),
        replay=getattr(args, "llm_replay", ""),
        queue_dir=getattr(args, "llm_queue", ""),
    )
    if default_temperature is not None and "temperature" not in cfg.sources:
        cfg.temperature = float(default_temperature)
        cfg.sources["temperature"] = "任务默认"
    llm.set_default_config(cfg)
    return cfg


def llm_backend_note(cfg) -> str:
    """一行诊断：当前用哪个后端、关键值从哪来。

    每个产 LLM 结果的脚本都该把它打出来 —— 「这次结果是谁出的、模型是哪个」
    必须能从日志里读出来，否则出了问题只能重跑猜。
    """
    name = cfg.resolved_backend()
    bits = [f"后端={name}"]
    if name in ("openai", "local"):
        bits.append(f"模型={cfg.model or '(默认)'}")
        bits.append(f"端点={cfg.base_url or '(未配置)'}")
    bits.append(f"温度={cfg.temperature}")
    bits.append(f"提示词语言={cfg.lang}")
    src = cfg.sources.get("backend") or cfg.sources.get("_file")
    if src:
        bits.append(f"来源={src}")
    if cfg.record:
        bits.append(f"录制={cfg.record}")
    return "[llm] " + " | ".join(bits)


def llm_waiting_note(exc) -> str:
    """``ManualPending`` 的统一提示文案（退出码 3，不是失败）。"""
    n = getattr(exc, "pending_count", 0)
    return (
        f"\n{'=' * 72}\n"
        f"未配置可自动调用的 LLM 后端，已把 {n} 条任务放进人工兜底队列。\n"
        f"这不是失败 —— 回填后重跑同一条命令即可继续（已完成的不会重复问）。\n\n"
        f"  1) 看看有哪些任务：  python voc.py llm pending\n"
        f"  2) 取某条任务的提示词：python voc.py llm show <task_id>\n"
        f"  3) 把模型输出回填：    python voc.py llm fill <task_id> --text-file out.json\n"
        f"     （或批量：        python voc.py llm import results.json）\n\n"
        f"  想改成自动调用，任一方式即可：\n"
        f"    --backend openai --base-url https://api.openai.com/v1 --api-key sk-...\n"
        f"    或写 voc.llm.json（{PLATFORM_ROOT / 'voc.llm.json'}），"
        f"或设 VOC_LLM_BACKEND / VOC_LLM_BASE_URL / VOC_LLM_API_KEY\n"
        f"  只想跑通链路：--backend mock\n"
        f"{'=' * 72}\n"
    )


# --------------------------------------------------------------------------
# 5. 分析框架（Stage 5）
# --------------------------------------------------------------------------
# **别把 ``--frame`` 跟 ``--profile`` 混为一谈**：
#   * ``--profile``（Stage 3）= **数据源画像**：数据从哪来（Reddit / Amazon / …），
#     决定用户可见文案（「帖子 / 子版块」还是「评论 / 站点」）与能力开关（有没有星级）。
#   * ``--frame``（Stage 5）= **分析框架**：分析哪些维度、按什么轴分桶。
# 两者正交 —— 同一份亚马逊评论，可以按购买状态分析，也可以按决策角色分析。
# 所以是两个参数、两个目录（``voc_core/source_profile.py`` vs ``frames/``），
# 不是一个参数的两种取值。

def add_frame_argument(parser) -> None:
    """给 argparse 加 ``--frame``（分析框架）。"""
    parser.add_argument(
        "--frame",
        default=os.getenv("VOC_FRAME", ""),
        help="分析框架：内置名（consumer / b2b_role）或 .yaml/.json 文件路径；"
             "留空 = consumer（平台化之前的行为）")


def load_frame(spec=None):
    """``--frame`` 参数 → :class:`AnalysisFrame`。

    ``None`` / 空串 → 内置 ``consumer``，其行为与平台化之前一致。
    """
    from voc_core.analysis_frame import load_frame as _load_frame
    return _load_frame(spec)


def load_frame_from_product(product, *, default=None):
    """从上游产物读框架；产物里没有（= 平台化之前的老产物）就用 ``default``。

    这是「框架只需在第一步给一次」的落地点：``tags`` 把框架写进 tagset.json，
    下游 annotate / aggregate / dashboard / report 从产物读，不必各自
    再挂一个 ``--frame`` 再祈求它们保持一致。
    """
    from voc_core.analysis_frame import frame_from_product
    got = frame_from_product(product)
    if got is not None:
        return got
    return load_frame(default) if default is not None else None
