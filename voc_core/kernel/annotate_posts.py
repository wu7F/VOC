#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据标注（reddit分析 专用）—— 参考 数据标注 skill 的逐条语义标注 + 断点续传。

用整个标签集对每篇 A+B 帖逐条语义标注（reddit_id 替代 comment_id，文本=标题+换行+摘要）。
彻底取代原关键词 link_posts，从根上消除关键词遗漏。

评论单元（v3）：评论不再使用独立的「评论者人群画像」标签集，而是与主标签集**合并**——
用 `--comment-dims` 指定评论标注哪些维度（默认 = 分段轴维度 + 全部画像维度，
consumer 框架下即 `purchase_status` + 两个画像维度）。
帖子单元与评论单元因此共享同一套标签键，聚合阶段可按分段轴归桶、按单元类型堆叠计数。

分析框架（Stage 5）：标注规则 6（分段轴互斥判定）与规则 7（画像桶条件）由
`--frame` 渲染。默认从**标签集的 `analysis_frame` 块**读（产物自描述，框架只在
第一步给一次）；显式给 `--frame` 则覆盖（用于读旧标签集）。

LLM 后端（Stage 4）：可插拔，见 ``voc_core/llm.py``。

* ``--backend auto``（默认）有 openai/local 配置就用它，否则切 ``manual`` 人工兜底队列
* ``--backend manual`` 任务落盘待回填，回填后**重跑即命中**（退出码 3，不是失败）
* ``--backend mock|replay`` 跑通链路 / 离线重放
* ``--mock`` 仍是内核自带的规则标注（``MOCK_PURCHASED_KW`` 等），不走任何后端

提示词在 ``prompts/annotate.*.zh.md``（``--lang`` 可换）。**没有 ``sys.exit`` 了**：
退出码 0=成功 / 2=配置错误 / 3=等待人工回填。

运行：
  python3 annotate_posts.py --tagset tags/reddit_tagset.json \
      --relevant data/reddit_analysis/reddit_posts_relevant.csv \
      --comments data/reddit_analysis/reddit_comments.csv \
      --run-dir annot/runs/<run> --threads 20 [--export annot/reddit_annotations.jsonl]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# 平台侧公共设施：列名归一化 + 源画像 + 分析框架 + LLM 后端容错加载
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
    load_frame_from_product,
    load_profile,
)

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

# 评论单元默认标注的维度：购买状态判定 + 两个人群画像维度。
# 人群画像在 v3 起「帖子单元 + 评论单元合并计数」，因此评论必须与帖子共用同一套标签键，
# 只标注人群相关的维度（否则上万条评论全维度标注的 LLM 开销不可接受）。
#
# Stage 5 起这个清单**由框架决定**（分段轴 + 全部画像维度），见
# :func:`comment_dims_for`；下面的常量只是 consumer 框架下的默认值，兼容外部引用。
DEFAULT_COMMENT_DIMS = "purchase_status,user_personas_purchased,user_personas_non_purchased"


def comment_dims_for(frame: AnalysisFrame, override: str = "") -> list[str]:
    """评论单元要标注的维度。

    默认 = 分段轴维度 + 框架里全部画像维度。换框架就自动跟着换
    （B2B 是 ``decision_role`` + 三个代表角色的画像维度），不必改代码。
    """
    if override:
        return [d.strip() for d in override.split(",") if d.strip()]
    dims: list[str] = [frame.axis.dim] if frame.axis else []
    dims += [d.id for d in frame.active_dims if d.persona_bucket]
    return dims


def build_annotate_system(frame: AnalysisFrame, *, unit: str, secondary: str,
                          secondary_speaker: str, lang: str = P.DEFAULT_LANG) -> str:
    """渲染 ``annotate.system`` —— 其中规则 6（分段轴）与规则 7（画像桶）跟框架绑定。

    改造前这两条规则是模板里的死字：换成 B2B 框架后模型会继续去找
    ``purchase_status``，而标签集里根本没有这个维度 —— 归桶闸门于是永远判
    「无法判断」，画像两桶全空，**而报错一个都不会有**。
    """
    rules = FP.annotate_rules(frame, unit=unit, secondary=secondary,
                              secondary_speaker=secondary_speaker)
    return P.render("annotate.system", lang,
                    annotate_axis_rule=rules["annotate_axis_rule"],
                    annotate_persona_rule=rules["annotate_persona_rule"])


def load_tagset(path: str):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    ts = data.get("tagset", data)
    # 扁平化：tk -> {dim, def}
    flat = {}
    for dk, dv in ts.get("dimensions", {}).items():
        for tk, td in dv.get("tags", {}).items():
            flat[tk] = {"dim": dk, "def": td}
    return ts, flat


def load_posts(path: str) -> list[dict]:
    rows = canonicalize_rows(
        list(csv.DictReader(Path(path).open(encoding="utf-8"))), "posts")
    out = []
    for i, r in enumerate(rows):
        out.append({
            "reddit_id": r.get("reddit_id"),
            "title": (r.get("title") or "").strip(),
            "summary": (r.get("summary") or r.get("summary_text") or r.get("selftext") or "").strip(),
            "mentioned_brands": (r.get("mentioned_brands") or "").strip(),
            "_seq": i,
        })
    return out


def _tag_defs_block(flat: dict) -> str:
    """把标签集渲染成「维度 → 标签」文本块（单帖与批量两种 prompt 共用）。

    层层套模板而不是拼 f-string，是为了让**所有露给模型的字**都待在
    ``prompts/`` 里：``dim_block`` / ``tag_block`` / ``tag_reason`` 各一层。
    代码只负责按维度分组与拼接，措辞一处也不写死。

    ``tag_block`` 里那行 reason 是**条件行**：模板用 ``${reason_block}`` 顶在
    ``- sentiment:`` 前面，有原因词表时传入「一行文本 + 换行」，没有时传空串。
    """
    by_dim: dict = {}
    for tk, info in flat.items():
        by_dim.setdefault(info["dim"], []).append((tk, info["def"]))

    dims = []
    for dk, items in by_dim.items():
        tags = []
        for tk, td in items:
            reasons = td.get(f"{tk}_reason")
            reason_block = ""
            if reasons:
                reason_block = P.render("annotate.tag_reason", tag_key=tk,
                                        reason_list="、".join(reasons)) + "\n"
            tags.append(P.render(
                "annotate.tag_block",
                tag_key=tk,
                category=td.get("category"),
                thoughts=td.get("thoughts"),
                mentioned=td.get("mentioned"),
                original_text=td.get("original_text"),
                summary_value=td.get(f"{tk}_summary"),
                sentiment=td.get("sentiment"),
                reason_block=reason_block))
        dims.append(P.render("annotate.dim_block", dim=dk, tag_blocks="\n".join(tags)))
    return "\n".join(dims)


def build_user_prompt(post: dict, flat: dict) -> str:
    return P.render("annotate.user",
                    post_title=post["title"],
                    post_summary=post["summary"],
                    dim_blocks=_tag_defs_block(flat))


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


def annotate_one(post, flat, chat_fn, system):
    user = build_user_prompt(post, flat)
    content, usage = chat_fn(system, user)
    try:
        data = extract_json(content)
    except Exception:
        data = {}
    annotations = {}
    for tk, info in flat.items():
        ta = data.get(tk, {}) or {}
        s = ta.get(f"{tk}_summary", ta.get("summary", ""))
        r = ta.get(f"{tk}_reason", ta.get("reason", ""))
        annotations[tk] = {
            "mentioned": str(ta.get("mentioned", "no")).lower(),
            "original_text": ta.get("original_text", ""),
            "summary": s,
            "sentiment": ta.get("sentiment", ""),
            "thoughts": ta.get("thoughts", ""),
            "reason": r,
        }
    return {
        "reddit_id": post.get("reddit_id"),
        "title": post.get("title"),
        "summary": post.get("summary", ""),
        "mentioned_brands": post.get("mentioned_brands", ""),
        "annotations": annotations,
        "usage": usage,
        "annotated_at": datetime.now().isoformat(),
    }


def pending_posts(posts, run_dir):
    done = {p.stem for p in run_dir.glob("*.json")}
    return [p for p in posts if str(p["reddit_id"]) not in done]


def load_comment_units(path: str, post_ids: set[str], chars: int = 1500, limit: int = 0) -> list[dict]:
    """把 reddit_comments.csv 转成「评论单元」——每条评论是一个独立标注单元。

    单元结构与帖子同构（reddit_id / title / summary），因此可复用同一套标注与中间文件逻辑；
    额外保留 post_reddit_id / author / published / is_root_comment，供聚合与看板区分单元类型。
    """
    units: list[dict] = []
    p = Path(path)
    if not p.exists():
        return units
    with p.open(encoding="utf-8", newline="") as f:
        for r in canonicalize_rows(csv.DictReader(f), "comments"):
            cid = (r.get("comment_id") or "").strip()
            pid = (r.get("post_reddit_id") or "").strip()
            body = (r.get("body") or "").strip()
            if not cid or not body:
                continue
            if post_ids and pid not in post_ids:
                continue
            author = (r.get("author") or "").strip() or "匿名"
            ptitle = (r.get("post_title") or "").strip()
            units.append({
                "reddit_id": cid,
                "post_reddit_id": pid,
                "title": f"评论者 @{author} 在原帖《{ptitle}》下的发言",
                "summary": body[:chars],
                "mentioned_brands": "",
                "author": author,
                "published": (r.get("published") or "").strip(),
                "is_root_comment": (r.get("is_root_comment") or "").strip(),
            })
            if limit and len(units) >= limit:
                break
    return units


def run_comment_annotation(args, flat_c: dict, chat_fn, post_ids: set[str], system: str):
    """用同一套标签集（维度子集）给评论单元打标（评论独立计数）。

    ``system`` 与帖子标注**完全相同**：规则 6/7 讲的是分段轴与画像桶，
    与「这次只标哪几个维度」无关；变的只有 user 里的标签集定义块。
    """
    units = load_comment_units(args.comments, post_ids, args.comment_chars, args.comment_max)
    run_dir = Path(args.comments_run_dir) if args.comments_run_dir else Path(args.run_dir) / "comments"
    run_dir.mkdir(parents=True, exist_ok=True)
    if not units:
        print("[annotate-comments] 无可用评论单元（检查 --comments 路径或被 --relevant 过滤）。")
        return run_dir
    pend = pending_posts(units, run_dir)
    print(f"[annotate-comments] 标签数={len(flat_c)} 评论单元={len(units)} "
          f"已完成={len(units) - len(pend)} 待标注={len(pend)} 目录={run_dir}")
    if not pend:
        print("[annotate-comments] 全部已完成，无需标注。")
        return run_dir
    counter = {"done": 0, "err": 0}
    lock = threading.Lock()
    abort: dict = {"pending": None}

    def worker(u):
        with lock:
            if abort["pending"] is not None:
                return
        try:
            rec = annotate_one(u, flat_c, chat_fn, system)
            rec["unit_type"] = "comment"
            rec["post_reddit_id"] = u["post_reddit_id"]
            rec["author"] = u["author"]
            rec["published"] = u["published"]
            rec["is_root_comment"] = u["is_root_comment"]
            write_intermediate(rec, run_dir)
            with lock:
                counter["done"] += 1
                if counter["done"] % 500 == 0:
                    print(f"  ... 评论标注进度 {counter['done']}/{len(pend)}")
        except ManualPending as e:
            # 评论是**逐条**一任务，人工兜底时会一次产生上万条待办 ——
            # 立刻刹车，别把队列灌爆。
            with lock:
                abort["pending"] = e
        except Exception as e:
            with lock:
                counter["err"] += 1
                print(f"  ✗ 评论 {u['reddit_id']} 错误: {e}")

    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        futures = [ex.submit(worker, u) for u in pend]
        for _f in as_completed(futures):
            pass
    print(f"[annotate-comments] 本轮完成 {counter['done']}，失败 {counter['err']}。中间文件: {run_dir}")
    if abort["pending"] is not None:
        raise abort["pending"]
    return run_dir


def write_intermediate(rec, run_dir):
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{rec['reddit_id']}.json"
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")


# mock 专用的分段轴判定词表：真实运行由 LLM 判定，这里只是让 --mock 端到端跑得通、
# 能验证「归桶闸门」是否真的把帖子/评论分到了各画像桶里。
MOCK_PURCHASED_KW = ["i bought", "my feeder", "mine has", "we have had", "i've had", "just got",
                     "switched from", "been using", "i own", "my cats love"]
MOCK_NON_PURCHASED_KW = ["should i buy", "looking for", "any recommendations", "which one",
                         "is it worth", "thinking about", "first time", "vs ", "budget"]


def _mock_axis_choice(active: list[str], excluded: list[str], p_hit: bool, n_hit: bool,
                      text: str) -> str | None:
    """mock 的分桶选择：**恰好一个**桶拿到 yes（其余全 no），与真实闸门同构。

    两桶（consumer / B2B……凡 active 数量 ≤2）时用「已购信号 / 未购信号」判定，
    与平台化之前逐字节一致；桶数 ≥3 时（B2B 决策者 / 影响者 / 使用者）按文本
    散列铺开，保证**每个桶都有样本**，这样换框架的端到端验收里画像卡片不是空的。
    """
    if p_hit and not n_hit:
        if len(active) <= 2:
            return active[0]
        return active[sum(map(ord, text)) % len(active)]
    if n_hit and not p_hit:
        if len(active) <= 2:
            return active[1] if len(active) > 1 else active[0]
        rest = active[1:]
        return rest[sum(map(ord, text)) % len(rest)]
    if not p_hit and not n_hit:
        # 两侧信号都没有 → 判「无法判断」，与真实判定口径一致
        return excluded[0] if excluded else None
    # 两侧信号同时出现 = 自相矛盾 → 一个桶都不给（下游同样会判 unknown）
    return None


def make_mock_chat_fn(flat: dict, frame: AnalysisFrame | None = None):
    """mock 模式的 chat_fn 工厂：按传入的标签子集命中关键词（帖子/评论各用各的子集）。

    分段轴的标签**不靠关键词**，而是按框架的桶定义挑一个桶给 yes —— 这样换框架
    以后 mock 依然能验证闸门（哪个桶被喂进去、聚合阶段有没有真的按桶分开）。
    """
    import re as _re
    f = frame or load_frame(None)
    axis = f.axis
    active = [b.id for b in axis.active_buckets] if axis else []
    excluded = ([axis.excluded_bucket.id]
                if (axis and axis.excluded_bucket) else [])
    bucket_of_tag = {tk: b.id for b in (axis.buckets if axis else ()) for tk in b.tags}
    kw = {}
    for tk in flat:
        d = flat[tk]["def"]
        raw = d.get("mock_keywords") or d.get(f"{tk}_summary", "")
        kw[tk] = [w.strip() for w in raw.split("||") if w.strip()]
    NEG = {"jam_issue", "app_reliability", "battery_backup", "build_quality"}
    POS = {"easy_clean", "portion_control", "scheduling", "microchip_id", "wet_food_support"}
    # 分段轴标签：走桶判定，而不是通用关键词命中
    status_keys = [tk for tk in flat if tk in bucket_of_tag]

    def chat_fn(system, user, max_tokens=None):
        m = _re.search(r"正文: (.*?)\n\n## 标签集", user, _re.DOTALL)
        text = (m.group(1) if m else "")
        text_l = text.lower()
        p_hit = any(w in text_l for w in MOCK_PURCHASED_KW)
        n_hit = any(w in text_l for w in MOCK_NON_PURCHASED_KW)
        chosen = _mock_axis_choice(active, excluded, p_hit, n_hit, text)
        obj = {}
        for tk in flat:
            if tk in status_keys:
                # 互斥：恰好一个桶标签为 yes，其余为 no
                yes = bucket_of_tag[tk] == chosen
                obj[tk] = ({"thoughts": "mock-rule", "mentioned": "yes",
                            "original_text": "mock-status", "summary": "mock-status",
                            "sentiment": "", "reason": ""} if yes else
                           {"thoughts": "mock-rule", "mentioned": "no",
                            "original_text": "", "summary": "", "sentiment": "", "reason": ""})
                continue
            hit = [w for w in kw[tk] if w.lower() in text_l]
            if hit:
                joined = " || ".join(hit)
                sent = "negative" if tk in NEG else ("positive" if tk in POS else "neutral")
                obj[tk] = {"thoughts": "mock-rule", "mentioned": "yes",
                           "original_text": joined, "summary": joined,
                           "sentiment": sent, "reason": ""}
            else:
                obj[tk] = {"thoughts": "mock-rule", "mentioned": "no",
                           "original_text": "", "summary": "", "sentiment": "", "reason": ""}
        return json.dumps(obj, ensure_ascii=False), {"mock": True}
    return chat_fn


def run_annotation(args, flat, chat_fn, system, frame, mock_factory=None):
    posts = load_posts(args.relevant)
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    pend = pending_posts(posts, run_dir)
    total = len(posts)
    already = total - len(pend)
    use_batch = (not args.mock) and (args.batch_size or 0) > 1
    print(f"[annotate] 标签数={len(flat)} 总帖={total} 已完成={already} 待标注={len(pend)} "
          f"线程={args.threads} 批大小={args.batch_size if use_batch else 1}")
    if not pend:
        print("[annotate] 全部已完成，无需标注。")
    elif use_batch:
        # 批量标注：每批 N 帖合并为一次 LLM 调用，大幅减少往返次数与重复标签集传输。
        # 单帖中间文件、导出格式与逐条模式完全一致（逻辑等价，仅调用方式更快）。
        batch_annotate(pend, flat, chat_fn, args, run_dir, system)
    else:
        counter = {"done": 0, "err": 0}
        lock = threading.Lock()
        abort: dict = {"pending": None}

        def worker(p):
            with lock:
                if abort["pending"] is not None:
                    return
            try:
                rec = annotate_one(p, flat, chat_fn, system)
                write_intermediate(rec, run_dir)
                with lock:
                    counter["done"] += 1
                    print(f"  ✓ {rec['reddit_id']} ({counter['done']}/{len(pend)})")
            except ManualPending as e:
                # 逐帖模式下人工兜底会一条一任务 —— 立刻刹车，别灌爆队列。
                with lock:
                    abort["pending"] = e
            except Exception as e:
                with lock:
                    counter["err"] += 1
                    print(f"  ✗ {p['reddit_id']} 错误: {e}")

        with ThreadPoolExecutor(max_workers=args.threads) as ex:
            futures = [ex.submit(worker, p) for p in pend]
            for f in as_completed(futures):
                pass
        print(f"[annotate] 本轮完成 {counter['done']}，失败 {counter['err']}。中间文件: {run_dir}")
        if abort["pending"] is not None:
            raise abort["pending"]
    if args.export:
        export_merged(run_dir, Path(args.export))
        print(f"[annotate] 已导出: {args.export}")

    # ---- 评论单元标注（v3）：评论与主标签集合并，只标注 --comment-dims 指定的维度 ----
    if getattr(args, "comments", ""):
        if getattr(args, "comments_tagset", ""):
            print("[annotate] --comments-tagset 已废弃：v3 起评论单元与帖子共用同一套标签集，"
                  "请用 --comment-dims 指定评论需要标注的维度。")
        # 默认维度由框架决定（分段轴 + 全部画像维度），不再写死 purchase_status 那三件套。
        dims = comment_dims_for(frame, getattr(args, "comment_dims", "") or "")
        flat_c = {tk: info for tk, info in flat.items() if info["dim"] in dims}
        if not flat_c:
            print(f"[annotate] --comment-dims 未匹配到任何维度（{dims}），跳过评论单元标注。")
            return
        c_chat = mock_factory(flat_c, frame) if mock_factory else chat_fn
        print(f"[annotate] 评论单元标注维度：{', '.join(dims)}（标签 {len(flat_c)} 个）")
        c_run_dir = run_comment_annotation(args, flat_c, c_chat,
                                           {str(p["reddit_id"]) for p in posts}, system)
        if args.export_comments:
            export_merged(c_run_dir, Path(args.export_comments))
            print(f"[annotate] 已导出评论标注: {args.export_comments}")


def chat_with_retry(chat_fn, system, user, max_tokens=None, retries=3):
    """对瞬时错误（网络抖动 / 限流）做有限次退避重试，最后再抛出。

    ``ManualPending`` **不重试**：它不是瞬时故障，而是「任务已入队、等人工回填」。
    退避重试三次只会白等 12 秒，然后把同一个信号抛出去。
    """
    last = None
    for attempt in range(retries):
        try:
            if max_tokens is not None:
                return chat_fn(system, user, max_tokens=max_tokens)
            return chat_fn(system, user)
        except ManualPending:
            raise
        except Exception as e:  # noqa: BLE001 - 标注阶段任何异常都尝试重试
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def build_batch_user_prompt(posts: list[dict], flat: dict) -> str:
    """与单帖 prompt 同构，但只发送一次标签集，后附多篇待标注帖子。"""
    blocks = [
        P.render("annotate.batch_post_block", idx=idx, post_id=p["reddit_id"],
                 post_title=p["title"], post_summary=p["summary"])
        for idx, p in enumerate(posts, 1)
    ]
    return P.render("annotate.batch.user",
                    dim_blocks=_tag_defs_block(flat),
                    n_posts=len(posts),
                    post_blocks="\n\n".join(blocks))


def _build_rec_from_obj(p, obj, flat, usage):
    annotations = {}
    for tk, info in flat.items():
        ta = obj.get(tk, {}) or {}
        s = ta.get(f"{tk}_summary", ta.get("summary", ""))
        r = ta.get(f"{tk}_reason", ta.get("reason", ""))
        annotations[tk] = {
            "mentioned": str(ta.get("mentioned", "no")).lower(),
            "original_text": ta.get("original_text", ""),
            "summary": s,
            "sentiment": ta.get("sentiment", ""),
            "thoughts": ta.get("thoughts", ""),
            "reason": r,
        }
    return {
        "reddit_id": p.get("reddit_id"),
        "title": p.get("title"),
        "summary": p.get("summary", ""),
        "mentioned_brands": p.get("mentioned_brands", ""),
        "annotations": annotations,
        "usage": usage,
        "annotated_at": datetime.now().isoformat(),
    }


def process_batch(batch, flat, chat_fn, run_dir, counter, lock, system):
    user = build_batch_user_prompt(batch, flat)
    # 批量输出更长，显式给定 max_tokens 防止被默认上限截断导致 JSON 不完整。
    max_tokens = min(16000, 1500 * len(batch) + 800)
    try:
        content, usage = chat_with_retry(chat_fn, system, user, max_tokens=max_tokens)
    except ManualPending:
        # 人工兜底：连「逐帖回退」都不该做 —— 那会把同一批又拆成 N 条任务塞进队列，
        # 让人去做重复劳动。直接把信号抛给上层统一处理。
        raise
    except Exception:
        # 整批失败 → 逐帖回退（与单帖模式等价），确保不丢帖。
        for p in batch:
            try:
                rec = annotate_one(p, flat, chat_fn, system)
                write_intermediate(rec, run_dir)
                with lock:
                    counter["done"] += 1
            except Exception as e2:
                with lock:
                    counter["err"] += 1
                    print(f"  ✗ {p['reddit_id']} 错误: {e2}")
        return

    try:
        data = extract_json(content)
    except Exception:
        data = {}
    if isinstance(data, list):
        # 个别模型返回数组：按位置映射到帖子 id。
        data = {batch[i]["reddit_id"]: data[i] for i in range(min(len(data), len(batch)))}
    if not isinstance(data, dict):
        data = {}

    missing = []
    for p in batch:
        pid = p["reddit_id"]
        obj = data.get(pid)
        if not isinstance(obj, dict):
            missing.append(p)
            continue
        rec = _build_rec_from_obj(p, obj, flat, usage)
        write_intermediate(rec, run_dir)
        with lock:
            counter["done"] += 1

    # 批内个别缺失 / 解析失败的帖，逐帖补标。
    for p in missing:
        try:
            rec = annotate_one(p, flat, chat_fn, system)
            write_intermediate(rec, run_dir)
            with lock:
                counter["done"] += 1
        except Exception as e2:
            with lock:
                counter["err"] += 1
                print(f"  ✗ {p['reddit_id']} 错误: {e2}")


def batch_annotate(posts, flat, chat_fn, args, run_dir, system):
    batches = [posts[i:i + args.batch_size] for i in range(0, len(posts), args.batch_size)]
    counter = {"done": 0, "err": 0}
    lock = threading.Lock()
    bidx = {"i": 0}
    #: 人工兜底信号。一旦出现就**停止派发剩余批次**，最后统一抛给上层 ——
    #: 否则一个 300 帖的任务会在队列里留下 300 条待办，而用户只看到「失败 300」。
    abort: dict = {"pending": None}

    def worker():
        while True:
            with lock:
                if abort["pending"] is not None:
                    return
                bi = bidx["i"]
                if bi >= len(batches):
                    break
                bidx["i"] += 1
            try:
                process_batch(batches[bi], flat, chat_fn, run_dir, counter, lock, system)
            except ManualPending as e:
                with lock:
                    abort["pending"] = e
                return
            except Exception as e:
                with lock:
                    counter["err"] += 1
                    print(f"  ✗ batch {bi} 错误: {e}")

    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        for _ in range(args.threads):
            ex.submit(worker)
    print(f"[annotate] 批次完成 成功={counter['done']} 失败={counter['err']} 中间文件: {run_dir}")
    if abort["pending"] is not None:
        raise abort["pending"]


def status(args):
    run_dir = Path(args.run_dir)
    files = list(run_dir.glob("*.json"))
    print(json.dumps({"run_dir": str(run_dir), "annotated": len(files)}, ensure_ascii=False, indent=2))


def export_merged(run_dir, out):
    files = sorted(run_dir.glob("*.json"), key=lambda p: p.stem)
    recs = [json.loads(p.read_text(encoding="utf-8")) for p in files]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="数据标注：逐条语义标注 reddit 帖子")
    ap.add_argument("--tagset", required=True, help="标签集 JSON 路径")
    ap.add_argument("--relevant", required=True, help="reddit_posts_relevant.csv")
    ap.add_argument("--run-dir", required=True, help="中间文件目录（续传依据）")
    ap.add_argument("--threads", type=int, default=None,
                    help="并发线程数（批量模式下为并发批次数）；默认取 LLM 配置的 threads（20）")
    ap.add_argument("--batch-size", type=int, default=None,
                    help="每批合并标注的帖子数（>1 时批量调用 LLM）；默认取 LLM 配置的 batch_size（8）")
    ap.add_argument("--export", default=None, help="标注完成导出合并文件（jsonl）路径")
    ap.add_argument("--comments", default="", help="reddit_comments.csv 路径（开启评论单元标注）")
    ap.add_argument("--comments-tagset", default="", help="【已废弃·忽略】v3 起评论与主标签集合并，不再使用独立评论标签集")
    ap.add_argument("--comment-dims", default="",
                    help="评论单元标注哪些维度（逗号分隔；留空 = 分段轴维度 + 全部画像维度，"
                         f"consumer 框架下即 {DEFAULT_COMMENT_DIMS}）")
    ap.add_argument("--comments-run-dir", default="", help="评论标注中间文件目录（默认 <run-dir>/comments）")
    ap.add_argument("--comment-chars", type=int, default=1500, help="单条评论标注时截断字数（默认 1500）")
    ap.add_argument("--comment-max", type=int, default=0, help="最多标注的评论单元数（0=全部，测试用）")
    ap.add_argument("--export-comments", default=None, help="评论标注导出 jsonl 路径（可选）")
    ap.add_argument("--mock", action="store_true",
                    help="测试模式：用规则判定代替 LLM（不走任何后端）")
    add_profile_argument(ap)
    add_frame_argument(ap)
    # 温度默认 0：逐条标注求「稳定」，同一条帖子反复标注结论必须一致，否则聚合数字会漂。
    add_llm_arguments(ap, default_temperature=0.0)
    args = ap.parse_args()

    cfg = configure_llm(args, default_temperature=0.0)
    # 并发与批大小的优先级：CLI > LLM 配置（voc.llm.json / 环境变量）> 内置默认。
    # ``--threads`` 的 argparse 默认是 None，正是为了区分「用户没给」和「用户给了 20」。
    args.threads = int(args.threads) if args.threads else cfg.threads
    args.batch_size = int(args.batch_size) if args.batch_size else cfg.batch_size

    ts, flat = load_tagset(args.tagset)
    # 分析框架：优先从**标签集自带的 analysis_frame** 读（框架只在 tags 那步给一次）；
    # 显式给 --frame 则覆盖，用于读平台化之前的老标签集（它们没有这个块）。
    frame = load_frame_from_product(ts, default=args.frame or None) or load_frame(None)
    profile = load_profile(getattr(args, "profile", "") or None)
    unit = profile.term("unit_noun", "帖子")
    secondary = profile.term("secondary_noun", "评论")
    secondary_speaker = profile.term("secondary_speaker", "评论者")
    system = build_annotate_system(frame, unit=unit, secondary=secondary,
                                   secondary_speaker=secondary_speaker, lang=cfg.lang)
    print(f"[annotate] 分析框架={frame.name}（{frame.label}）"
          f" 轴={frame.axis.label if frame.axis else '（无分段轴）'}")

    mock_factory = None
    if args.mock:
        mock_factory = make_mock_chat_fn
        chat_fn = mock_factory(flat, frame)
    else:
        print(llm_backend_note(cfg))
        print(f"[annotate] 并发={cfg.threads} 批大小={args.batch_size} "
              f"（线程/批大小可用 --threads / --batch-size 覆盖）")

        def chat_fn(system, user, max_tokens=None):
            return call_llm(system, user, json_mode=True, return_usage=True,
                            max_tokens=max_tokens)

    try:
        run_annotation(args, flat, chat_fn, system, frame, mock_factory)
    except ManualPending as exc:
        # 人工兜底：任务已入队。**不是失败**，退出码 3，回填后重跑即可续。
        print(llm_waiting_note(exc))
        return EXIT_PENDING
    except LocalFallback as exc:
        print(f"[annotate] LLM 后端不可用：{exc}")
        print("      换后端：--backend mock 跑通链路 / --backend manual 落盘待回填；"
              "或配置 --base-url + --api-key，或写 voc.llm.json。")
        return EXIT_CONFIG
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
