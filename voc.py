#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""voc · 通用 VOC 洞察内核的命令行入口。

用法::

    # 1) 看看支持哪些数据源
    python voc.py sources

    # 2) 体检一个文件：识别数据源 + 规模统计（不写任何东西）
    python voc.py inspect "批量导出_13617条.xlsx"

    # 3) 归一化：产出统一契约 + 旧 schema（可直接喂给现有分析内核）
    python voc.py convert "批量导出_13617条.xlsx" --out-dir out/nursery
    python voc.py convert 全部_ASIN_评论.csv --source amazon_reviews --out-dir out/asin
    python voc.py convert wayfair.csv --source wayfair_reviews --out-dir out/wf

``convert`` 的产物::

    out/<job>/
      units.csv           统一契约（19 列，与数据源无关）
      units.jsonl         同上，JSON Lines
      units_legacy_posts.csv     旧 reddit_posts 形态（帖子）
      units_legacy_comments.csv  旧 reddit_comments 形态（评论）
      units_legacy_reviews.csv   旧 电商评论 形态（亚马逊 / Wayfair）
      ingest_report.json  规模统计与识别结果

最后三个文件与现有分析内核的输入 schema **逐字段一致**，因此可以零改动接上
下游的清洗分桶 → 标签设计 → 标注 → 聚合 → 看板/报告 —— 这是渐进迁移的关键。

     # 4) 查看/导出「源画像」——内核里的用户可见文案全由它注入
     python voc.py profile                      # 列出内置画像
     python voc.py profile wayfair              # 看某个画像的完整字段
     python voc.py profile wayfair --out p.json # 导出（可再传给 --profile）

     # 5) 跑管线（全部转调内核；--profile auto 会按 ingest_report.json 推断源）
     #    5a 清洗/分桶/统计 —— 产出 tagged / relevant / stats.json
     python voc.py clean --input out/nursery/units_legacy_reviews.csv \
         --out-dir out/nursery/analysis --category "Nursery Glider" \
         --keywords "nursery,glider,recliner" --brands "wayfair,pottery barn"
     #    5b 看板与洞察报告
     python voc.py dashboard --stats s.json --tagged t.csv --category "..." \
         --dimensions d.json --features-json f.json --out dash.html
     python voc.py report --dimensions d.json --features-json f.json --stats s.json \
         --relevant r.csv --out insight.md

     #    5c 两个会调 LLM 的环节（Stage 4：后端可插拔，见 7）
     #        5c-1 标签设计：从样本里长出标签集 JSON
     python voc.py tags --category "Nursery Glider" --relevant r.csv \
         --out out/analysis/tagset.json
     #        5c-2 逐条标注：按标签集给每篇帖子/评论打标
     python voc.py annotate --tagset out/analysis/tagset.json \
         --relevant r.csv --run-dir out/analysis

     # 6) 质量闸门（Stage 6）：这批产物能不能信
     python voc.py validate --stats s.json --dimensions d.json \
         --dashboard dash.html --out quality_report.json
     python voc.py validate --list-checks        # 列出全部检查项与依据

     # 7) LLM 后端与人工兜底队列（Stage 4）
     python voc.py llm backends        # 有哪几种后端，当前用的是哪种、配置来自哪
     python voc.py llm config          # 当前生效的完整配置（密钥已打码）
     python voc.py llm status          # 人工兜底队列：待回填几条
     python voc.py llm pending         # 列出待回填任务
     python voc.py llm show <task_id>  # 看某条任务的完整提示词
     python voc.py llm fill <task_id> --text-file out.json   # 回填模型输出
     python voc.py llm import results.jsonl                  # 批量回填

     # 8) 提示词模板（Stage 4：已从代码里抽出来，改措辞不用碰 Python）
     python voc.py prompts             # 列出模板与占位符
     python voc.py prompts annotate.user.zh   # 看某个模板全文
     python voc.py prompts --check     # 自检占位符与裸 $

     # 9) 编排（Stage 7）：一条命令跑完 Phase 1→5，可重跑、可批量
     python voc.py run --input data/reddit_posts.csv --comments data/reddit_comments.csv \
         --category "Nursery Glider" --job-dir runs/nursery --mock
     python voc.py run --job-dir runs/nursery          # 重跑：没变的步骤自动跳过
     python voc.py run --job-dir runs/nursery --force tags   # 从某步起强制重算
     python voc.py run --batch jobs.yaml               # 批量（每项一个 job 目录）
     python voc.py runs                                # 列出所有 job 与状态

``clean`` → ``dashboard`` / ``report`` → ``validate`` 就是整条管线；四个子命令都接受
``--profile``（默认 ``auto``），因此同一份内核代码、同一套命令，
Reddit 与电商源走的完全一致，只有文案与来源标注按源画像切换。

**LLM 后端（Stage 4）**：``tags`` 与 ``annotate`` 会调大模型。用哪个后端由
``--backend`` 决定，规则是**永不硬失败**——有 ``--base-url`` + ``--api-key`` 走
``openai``，只有地址走 ``local``（默认 Ollama），都没有就落到 ``manual``：
任务落盘到 ``out/llm_queue/pending/``，命令以**退出码 3**（等待人工回填，不是失败）
返回，回填后重跑同一命令即命中。退出码全项目统一：
``0`` 成功 ｜ ``1`` 一般失败 ｜ ``2`` 输入/配置错误 ｜ ``3`` 等待人工回填（可重跑）。

**产物命名**：``clean`` 的产物文件名也按源走（``--artifact-names``，默认 ``auto``）——
Reddit 保旧名（``reddit_posts_tagged.csv``，真·零回归），其他源用中性名
（``units_tagged.csv``）。起因是产物路径会印进报告的「数据文件路径」附录，
跑 Wayfair 却出现 ``reddit_posts_relevant.csv``，被质量闸门判为错误级失败。

**去域名化说明**：内核（6,900 行）原本把 ``reddit_id`` / ``subreddit`` / 「帖子」
写死在数据读取与文案里。Stage 3 后，这两处边界分别由

* ``voc_core/kernel_common.canonicalize_rows``（列名别名 → 内核规范键，补键不改名）
* ``voc_core/source_profile.SourceProfile``（术语表）

接管。因此同一份内核代码可以跑 Reddit / 亚马逊 / Wayfair，输出各自正确的
术语与来源标注，而 **Reddit 行为逐字不变**（未传 ``--profile`` 时）。

**评测回归（Stage 8）**：改一次口径，5 分钟内知道有没有变差。``eval`` 跑 golden set
（`eval/golden/<案>/`：一份真实输入 + 人工核对过的答案），比对两层判据——

* **锚点**（人写的下界断言）：分段轴计数、排除桶规模、关键结论 top-N、标杆引文可回溯；
* **基线**（冻结的全量指标）：任何一格变了都列出来，变好变坏由人判断。

     python voc.py eval                    # 比对基线，有漂移就非零退出
     python voc.py eval --case reddit-ng-consumer
     python voc.py eval --freeze           # 首次建立基线
     python voc.py eval --update           # 已确认改动，接受新答案
     python voc.py eval --markdown out/verify/stage8/report.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 保证脚本可独立运行（不必先安装）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from voc_core import summarize  # noqa: E402
from voc_core.adapters import available, detect_best, get  # noqa: E402
from voc_core.io_utils import write_json, write_jsonl, write_rows  # noqa: E402
from voc_core.schema import TYPE_COMMENT, TYPE_POST, UNIT_FIELDS  # noqa: E402

# 旧 schema 的列顺序（与现有内核的三个导入脚本一致）
LEGACY_POST_FIELDS = ["reddit_id", "title", "url", "subreddit", "author", "published",
                      "updated", "summary_text", "query", "scope", "sort",
                      "time_filter", "feed_url"]
LEGACY_COMMENT_FIELDS = ["comment_id", "post_reddit_id", "post_subreddit", "post_title",
                         "author", "published", "body", "is_root_comment"]
LEGACY_REVIEW_FIELDS = ["review_id", "title", "content", "rating", "asin", "product",
                        "brand", "author", "date", "url", "verified", "helpful_votes",
                        "country", "variant", "source"]


def _use_utf8_stdout() -> None:
    """Windows 控制台默认 GBK，中文日志会炸 —— 统一切到 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - 环境不支持就算了
                pass


def _resolve(source: str, paths: list[Path]) -> tuple[object, dict]:
    """确定用哪个适配器：显式指定，或按第一份文件自动识别。"""
    if source and source != "auto":
        return get(source), {}

    adapter, scores = detect_best(paths[0])
    if adapter is None:
        raise SystemExit(
            "无法自动识别数据源。请用 --source 显式指定。\n"
            f"各适配器置信度：{json.dumps(scores, ensure_ascii=False)}\n"
            "可用：" + ", ".join(a.adapter_id for a in available())
        )
    return adapter, scores


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

def _stats_note(st: dict | None) -> str:
    """把适配器的导入台账渲染成一行摘要（键序固定，便于横向对比）。"""
    if not st:
        return ""
    order = ("rows_total", "rows", "kept", "skipped_no_text", "skipped_empty",
             "skipped_duplicate", "merged_duplicates")
    parts = [f"{k}={st[k]}" for k in order if k in st and not isinstance(st[k], dict)]
    parts += [f"{k}={v}" for k, v in st.items()
              if k not in order and not isinstance(v, dict)]
    return ", ".join(parts)


def cmd_sources(_args) -> int:
    print("已注册的数据源适配器：\n")
    for a in available():
        print(f"  {a.adapter_id:18s} [{a.source:8s}] {a.label}")
        print(f"  {'':18s}  产出：{a.produces}")
    print("\n提示：--source auto 会按文件内容自动识别；列名独特性不足时会明确报错而不是瞎猜。")
    return 0


def cmd_inspect(args) -> int:
    for raw in args.files:
        p = Path(raw)
        if not p.exists():
            print(f"[跳过] 不存在：{p}")
            continue
        print(f"\n{'=' * 68}\n文件：{p.name}  ({p.stat().st_size / 1024:.0f} KB)")
        adapter, scores = _resolve(args.source, [p])
        print(f"识别：{adapter.adapter_id}  ({adapter.label})")
        order = sorted(scores.items(), key=lambda kv: -kv[1])
        print("置信度：" + ", ".join(f"{k}={v}" for k, v in order))
        units = adapter.normalize(p)
        s = summarize(units)
        print(f"单元：{s['total_units']}  讨论串：{s['threads']}")
        print(f"来源：{s['by_source']}   形态：{s['by_source_type']}")
        if s["rating_distribution"]:
            print(f"星级：{s['rating_distribution']}")
        if s["lang_distribution"]:
            print(f"语言：{s['lang_distribution']}")
        print(f"有日期：{s['dated_units']}  无日期占比：{s['undated_ratio']:.1%}")
        errs = [e for u in units for e in u.validate()]
        print(f"校验：{'全部通过' if not errs else f'{len(errs)} 条问题，例：{errs[:3]}'}")
        st = getattr(adapter, "last_stats", None)
        note = _stats_note(st)
        if note:
            print(f"台账：{note}")
        if st and st.get("skipped_no_text"):
            print(f"      ⚠ {st['skipped_no_text']} 条「只打星、未写文字」已排除在内容分析外，"
                  f"星级分布：{st.get('rating_only_distribution')}")
        if units:
            u = units[0]
            print(f"样例：{u.unit_id} | {u.source_type} | {u.channel or '(无渠道)'} | {u.author or '(匿名)'}")
            print(f"      {(u.title or u.text)[:80]}")
    return 0


def cmd_convert(args) -> int:
    paths = [Path(f) for f in args.files]
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise SystemExit("以下文件不存在：" + ", ".join(str(p) for p in missing))

    adapter, scores = _resolve(args.source, paths)
    print(f"[convert] 适配器：{adapter.adapter_id}（{len(paths)} 个文件）")

    units = []
    ledger: list[dict] = []
    rating_only: dict[str, int] = {}
    for p in paths:
        got = adapter.normalize(
            p,
            category=args.category,
            default_channel=args.default_channel,
            default_subreddit=args.default_channel,
            default_brand=args.default_brand,
            source_note=args.source_note,
            query=args.category,
        )
        st = getattr(adapter, "last_stats", None) or {}
        note = _stats_note(st)
        print(f"  {p.name}: {len(got)} 单元" + (f"  [{note}]" if note else ""))
        if st:
            ledger.append({"file": str(p), **st})
            for k, v in (st.get("rating_only_distribution") or {}).items():
                rating_only[k] = rating_only.get(k, 0) + v
        units.extend(got)

    if not units:
        raise SystemExit("归一化后没有任何有效单元，请检查文件内容与 --source 是否匹配。")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 统一契约
    write_rows(out_dir / "units.csv", [u.to_row() for u in units], UNIT_FIELDS)
    write_jsonl(out_dir / "units.jsonl", [u.to_dict() for u in units])

    # 旧 schema（按形态分组）—— 可直接喂给现有内核
    posts = [u.legacy_row() for u in units if u.source_type == TYPE_POST]
    comments = [u.legacy_row() for u in units if u.source_type == TYPE_COMMENT]
    reviews = [u.legacy_row() for u in units
               if u.source_type not in (TYPE_POST, TYPE_COMMENT)]

    written = []
    if posts:
        written.append(write_rows(out_dir / "units_legacy_posts.csv", posts, LEGACY_POST_FIELDS))
    if comments:
        written.append(write_rows(out_dir / "units_legacy_comments.csv", comments, LEGACY_COMMENT_FIELDS))
    if reviews:
        written.append(write_rows(out_dir / "units_legacy_reviews.csv", reviews, LEGACY_REVIEW_FIELDS))

    stats = summarize(units)
    errs = [e for u in units for e in u.validate()]
    report = {
        "adapter": adapter.adapter_id,
        "source": adapter.source,
        "files": [str(p) for p in paths],
        "detect_scores": scores,
        "source_note": args.source_note,
        "category": args.category,
        "stats": stats,
        # 导入台账：逐文件的 原始行数 / 保留 / 各类跳过数。
        # 有它才能回答"平台上显示 97 条，为什么这里只有 95 条"。
        "ingest_ledger": ledger,
        "rating_only_distribution": dict(
            sorted(rating_only.items(), key=lambda kv: -int(kv[0]))
        ),
        "validation_errors": errs[:50],
        "validation_error_count": len(errs),
    }
    write_json(out_dir / "ingest_report.json", report)

    print(f"\n[convert] 输出目录：{out_dir}")
    print(f"  units.csv / units.jsonl          {len(units)} 单元（统一契约）")
    for w in written:
        print(f"  {w.name:32s} （旧 schema，可直接接现有内核）")
    print(f"  ingest_report.json               规模统计、导入台账与识别结果")
    if errs:
        print(f"\n[convert] ⚠ 校验问题 {len(errs)} 条（详见 ingest_report.json）")
    if rating_only:
        n = sum(rating_only.values())
        print(f"[convert] ⚠ {n} 条「只打星、未写文字」未进内容分析；"
              f"若要对齐平台星级分布需把它们计入：{rating_only}")
    print(f"[convert] 规模：{stats['total_units']} 单元 / {stats['threads']} 讨论串 "
          f"/ 形态 {stats['by_source_type']}")
    return 0


def cmd_render(args) -> int:
    """把 units.csv 渲染成可读清单，供人工核对或喂给 LLM 归纳标签。"""
    from voc_core.schema import Unit

    units = [Unit.from_row(r) for r in _read_units(Path(args.units))]
    limit = args.limit or len(units)
    lines = []
    for i, u in enumerate(units[:limit], 1):
        head = u.title or (u.text[:60] if u.text else "(无标题)")
        lines.append(f"[{i}] {u.unit_id} | {u.source}/{u.source_type} | "
                     f"{u.channel or '-'} | {u.created_at[:10] or '-'}")
        lines.append(f"    标题：{head}")
        if u.text and u.title:
            lines.append(f"    正文：{u.text[:200]}")
        if u.rating:
            lines.append(f"    星级：{u.rating}")
        lines.append("")
    out = Path(args.out) if args.out else None
    text = "\n".join(lines)
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"[render] 已写 {out}（{min(limit, len(units))} / {len(units)} 单元）")
    else:
        print(text)
    return 0


def _read_units(path: Path):
    from voc_core.io_utils import read_rows
    return read_rows(path)


# --------------------------------------------------------------------------
# 内核转调（Stage 3：内核去域名化后，平台可直接驱动它跑任意源）
# --------------------------------------------------------------------------

KERNEL_DIR = Path(__file__).resolve().parent / "voc_core" / "kernel"


def _run_kernel(script: str, argv: list[str]) -> int:
    """在子进程里跑内核脚本，保持内核可独立运行（也便于容器化）。"""
    import subprocess

    path = KERNEL_DIR / script
    if not path.exists():
        raise SystemExit(f"内核脚本缺失：{path}")
    return subprocess.call([sys.executable, str(path), *argv])


def _profile_search_dirs(args) -> list[Path]:
    """``ingest_report.json`` / ``source_profile.json`` 可能落在哪几个目录（按优先级）。"""
    dirs: list[Path] = []
    for attr in ("stats", "input", "relevant"):
        v = getattr(args, attr, "") or ""
        if v:
            dirs.append(Path(v).parent)
    out_dir = getattr(args, "out_dir", "") or ""
    if out_dir:
        dirs.append(Path(out_dir))
    seen: set[Path] = set()
    uniq: list[Path] = []
    for d in dirs:
        try:
            rp = d.resolve()
        except OSError:
            continue
        if rp not in seen:
            seen.add(rp)
            uniq.append(d)
    return uniq


#: clean 会把解析出的源名落在这里，好让后面的 dashboard / report 也能 --profile auto
_PROFILE_FILE = "source_profile.json"


def _read_source_from(dir_path: Path) -> str:
    """从一个目录里推断源名：``ingest_report.json``（convert 写的）优先，
    其次 ``source_profile.json``（clean 写的）。"""
    rep = dir_path / "ingest_report.json"
    if rep.exists():
        try:
            src = (json.loads(rep.read_text(encoding="utf-8")) or {}).get("source", "")
        except Exception:  # noqa: BLE001 - 推断失败就退回默认，不阻断分析
            src = ""
        if src:
            return src, f"{rep.name}"
    own = dir_path / _PROFILE_FILE
    if own.exists():
        try:
            src = (json.loads(own.read_text(encoding="utf-8")) or {}).get("source", "")
        except Exception:  # noqa: BLE001
            src = ""
        if src:
            return src, f"{own.name}"
    return "", ""


def _resolve_profile(args) -> str:
    """决定这次跑哪个源画像。

    优先级：显式 ``--profile`` → 从数据所在目录的 ``ingest_report.json`` /
    ``source_profile.json`` 自动推断 → 空（内核按 Reddit 处理）。

    ``auto`` 是默认值，意味着**只要数据是 convert / clean 出来的，就不必手填画像**。
    """
    prof = (getattr(args, "profile", "") or "").strip()
    if prof and prof.lower() != "auto":
        return prof
    for d in _profile_search_dirs(args):
        src, where = _read_source_from(d)
        if src:
            print(f"[{getattr(args, 'cmd', '')}] 画像自动推断：{src}（来自 {d / where}）")
            return src
    return ""


def _persist_profile(out_dir: str, source: str) -> None:
    """把源名落到 ``<out-dir>/source_profile.json``，供后续步骤 --profile auto 使用。"""
    if not out_dir:
        return
    d = Path(out_dir)
    if (d / "ingest_report.json").exists():
        return  # convert 的台账已经带 source，不必另立一份
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / _PROFILE_FILE).write_text(
            json.dumps({"source": source or "reddit"}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError:
        pass  # 落盘失败不影响分析本身


def cmd_profile(args) -> int:
    from voc_core.source_profile import BUILTIN_PROFILES, get_profile

    if not args.name:
        print("内置源画像（内核的用户可见文案全部由它注入）：\n")
        print(f"  {'源':10s} {'名称':10s} {'计数单元':8s} {'渠道名':6s} {'次级单元':8s} {'星级'}")
        for k, p in sorted(BUILTIN_PROFILES.items()):
            sec = p.opt("secondary_noun") or "（无）"
            print(f"  {k:10s} {p.label:10s} {p.term('unit_noun'):8s} "
                  f"{p.term('channel_noun'):6s} {sec:8s} {'有' if p.has_rating else '无'}")
        print("\n查看某个画像的完整字段：python voc.py profile wayfair")
        print("导出为 JSON（给 --profile 用）：python voc.py profile wayfair --out profile.json")
        return 0

    p = get_profile(args.name)
    if args.out:
        p.to_json(args.out)
        print(f"[profile] 已导出 {args.out}（{p.source} / {p.label}）")
    else:
        print(json.dumps(p.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_clean(args) -> int:
    """清洗 + 相关性分桶 + 数据量统计（转调内核 clean_and_bucket.py）。

    这是管线的第一步：它的产物（tagged / relevant / stats.json）正是
    ``dashboard`` 与 ``report`` 的输入。
    """
    argv = ["--input", args.input, "--out-dir", args.out_dir,
            "--category", args.category]
    for flag, val in (("--report", args.report),
                      ("--keywords", args.keywords),
                      ("--brands", args.brands),
                      ("--comments", args.comments),
                      ("--source-note", args.source_note)):
        if val:
            argv += [flag, val]
    if args.max_age_years is not None:
        argv += ["--max-age-years", str(args.max_age_years)]
    if args.artifact_names and args.artifact_names != "auto":
        argv += ["--artifact-names", args.artifact_names]
    prof = _resolve_profile(args)
    if prof:
        argv += ["--profile", prof]
    _persist_profile(args.out_dir, prof)
    return _run_kernel("clean_and_bucket.py", argv)


def cmd_dashboard(args) -> int:
    argv = ["--stats", args.stats, "--tagged", args.tagged,
            "--category", args.category, "--out", args.out]
    if args.relevant:
        argv += ["--relevant", args.relevant]
    if args.comments:
        argv += ["--comments", args.comments]
    if args.dimensions:
        argv += ["--dimensions", args.dimensions]
    if args.features_json:
        argv += ["--features-json", args.features_json]
    if args.title:
        argv += ["--title", args.title]
    if args.no_logo:
        argv += ["--no-logo"]
    if args.frame:
        argv += ["--frame", args.frame]
    prof = _resolve_profile(args)
    if prof:
        argv += ["--profile", prof]
    return _run_kernel("generate_dashboard.py", argv)


def cmd_report(args) -> int:
    argv = ["--dimensions", args.dimensions, "--features-json", args.features_json,
            "--stats", args.stats, "--relevant", args.relevant,
            "--category", args.category, "--out", args.out]
    if args.comments:
        argv += ["--comments", args.comments]
    if args.frame:
        argv += ["--frame", args.frame]
    prof = _resolve_profile(args)
    if prof:
        argv += ["--profile", prof]
    return _run_kernel("generate_report.py", argv)


def cmd_validate(args) -> int:
    """跑质量闸门（Stage 6）。

    这是整条管线的**出口检查**：不产出新结论，只回答「这批产物能不能信」。
    默认从 ``--stats`` 所在目录自动补齐兄弟产物（tagged / relevant / comments /
    dimensions / features），因此常规用法只需给一个目录。
    """
    from voc_core.validate import audit, build_context, render_console, SEV_ORDER

    if getattr(args, "list_checks", False):
        from voc_core.validate import main as validate_main
        return validate_main(["--list-checks"])

    _autofill_validate_paths(args)
    # 内核在无画像时就是按 Reddit 处理（get_profile(None) → REDDIT），
    # 这里保持一致，否则呈现层检查会因为"源未知"而整块跳过。
    args.profile = _resolve_profile(args)
    if not args.profile:
        print("[validate] 未能推断源画像，按内核默认的 reddit 处理（可用 --profile 显式指定）")
        args.profile = "reddit"
    ctx = build_context(args)
    report = audit(ctx,
                   only=[s for s in (args.only or "").split(",") if s.strip()],
                   skip=[s for s in (args.skip or "").split(",") if s.strip()])

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_console(report))
        if args.out:
            print(f"\n质量报告：{args.out}")

    worst = min((SEV_ORDER.get(f["severity"], 3)
                 for f in report["findings"] if f["status"] == "fail"), default=99)
    return 1 if worst <= SEV_ORDER[args.fail_on] else 0


#: ``--stats`` 给定后，兄弟产物按这些名字自动补齐（用 naming 统一解析：中性名优先、回落旧名）
_VALIDATE_SIBLINGS: tuple[tuple[str, str], ...] = (
    ("tagged", "tagged"),
    ("relevant", "relevant"),
    ("features_json", "features"),
    ("dimensions", "dimensions"),
)

#: 次级单元（评论）的候选名，**超集优先** —— 引文回溯的语料越大越不容易误判，
#: 所以先找全量导出，再找按 relevant 收窄过的派生文件。
_COMMENT_CANDIDATES = ("units_comments.csv", "reddit_comments.csv",
                       "reddit_comments_relevant.csv")


def _autofill_validate_paths(args) -> None:
    """把 ``--stats`` 所在目录里的兄弟产物补进未显式给出的参数。"""
    if not args.stats:
        return
    from voc_core.naming import resolve_artifact

    d = Path(args.stats).parent
    filled: list[str] = []
    for attr, kind in _VALIDATE_SIBLINGS:
        if getattr(args, attr, ""):
            continue
        p = resolve_artifact(d, kind)
        if p:
            setattr(args, attr, str(p))
            filled.append(f"{attr}={p.name}")
    if not getattr(args, "comments", ""):
        for n in _COMMENT_CANDIDATES:
            if (d / n).exists():
                args.comments = str(d / n)
                filled.append(f"comments={n}")
                break
    if filled:
        print(f"[validate] 自动补齐：{', '.join(filled)}")


def cmd_aggregate(args) -> int:
    """聚合逐条标注 → 维度 / 功能 JSON（转调内核 aggregate_annotations.py）。

    管线第三步：把 ``annotate`` 的逐条结果收敛成看板契约的两个 JSON。
    框架默认从 ``--tagset`` 自带的分析框架读（产物自描述）；``--frame`` 只在
    读**旧标签集**（平台化之前产物、里面没有 ``analysis_frame`` 块）时才需要显式给。
    """
    argv = ["--run-dir", args.run_dir, "--relevant", args.relevant,
            "--tagset", args.tagset, "--dimensions", args.dimensions,
            "--features", args.features]
    for flag, val in (("--category", args.category),
                      ("--comments-run-dir", args.comments_run_dir),
                      ("--comments-csv", args.comments_csv)):
        if val:
            argv += [flag, val]
    if args.no_status_gating:
        argv += ["--no-status-gating"]
    if args.frame:
        argv += ["--frame", args.frame]
    prof = _resolve_profile(args)
    if prof:
        argv += ["--profile", prof]
    return _run_kernel("aggregate_annotations.py", argv)


def cmd_tags(args) -> int:
    """标签设计（Phase 4）—— 转调内核 design_tags.py。

    这是第一个**会调 LLM** 的环节，也是 Stage 4 后端的第一个使用者：
    ``--backend`` 决定谁来出标签集 JSON。
    ``--frame`` 在这里给一次就够 —— 框架会被写进标签集（产物自描述），
    后面的 annotate / aggregate / report / dashboard 都从产物读。
    """
    argv = ["--category", args.category, "--relevant", args.relevant, "--out", args.out]
    for flag, val in (("--comments", args.comments),
                      ("--nsample", args.nsample), ("--comments-per-post", args.comments_per_post)):
        if val:
            argv += [flag, str(val)]
    if args.mock:
        argv += ["--mock"]
    if args.frame:
        argv += ["--frame", args.frame]
    prof = _resolve_profile(args)
    if prof:
        argv += ["--profile", prof]
    argv += _llm_argv(args)
    return _run_kernel("design_tags.py", argv)


def cmd_annotate(args) -> int:
    """逐条标注（Phase 4.5）—— 转调内核 annotate_posts.py。

    框架默认从 ``--tagset`` 里自带的分析框架读；``--frame`` 只在读**旧标签集**
    （平台化之前产物、里面没有那个块）时才需要显式给。
    """
    argv = ["--tagset", args.tagset, "--relevant", args.relevant,
            "--run-dir", args.run_dir]
    for flag, val in (("--comments", args.comments), ("--export", args.export),
                      ("--export-comments", args.export_comments),
                      ("--comment-dims", args.comment_dims)):
        if val:
            argv += [flag, val]
    if args.comment_max:
        argv += ["--comment-max", str(args.comment_max)]
    if args.threads:
        argv += ["--threads", str(args.threads)]
    if args.batch_size:
        argv += ["--batch-size", str(args.batch_size)]
    if args.mock:
        argv += ["--mock"]
    if args.frame:
        argv += ["--frame", args.frame]
    prof = _resolve_profile(args)
    if prof:
        argv += ["--profile", prof]
    argv += _llm_argv(args)
    return _run_kernel("annotate_posts.py", argv)


def _llm_argv(args) -> list[str]:
    """把 ``--backend / --model / ...`` 透传给内核（只传用户真正给过的）。"""
    argv: list[str] = []
    for attr, flag in (("backend", "--backend"), ("model", "--model"),
                       ("base_url", "--base-url"), ("api_key", "--api-key"),
                       ("lang", "--lang"), ("llm_config", "--llm-config"),
                       ("llm_record", "--llm-record"), ("llm_replay", "--llm-replay"),
                       ("llm_queue", "--llm-queue")):
        v = getattr(args, attr, "")
        if v:
            argv += [flag, str(v)]
    if getattr(args, "temperature", None) is not None:
        argv += ["--temperature", str(args.temperature)]
    return argv


def add_llm_cli_arguments(p) -> None:
    """给 voc.py 的子命令加 LLM 参数（与内核 ``--help`` 保持一致）。"""
    from voc_core.kernel_common import LLM_BACKEND_CHOICES

    g = p.add_argument_group("LLM 后端（Stage 4）")
    g.add_argument("--backend", default="", choices=("",) + LLM_BACKEND_CHOICES,
                   help="auto/openai/local/manual/mock/replay（默认 auto）")
    g.add_argument("--model", default="")
    g.add_argument("--base-url", dest="base_url", default="")
    g.add_argument("--api-key", dest="api_key", default="")
    g.add_argument("--temperature", type=float, default=None)
    g.add_argument("--lang", default="", help="提示词语言（默认 zh）")
    g.add_argument("--llm-config", dest="llm_config", default="")
    g.add_argument("--llm-record", dest="llm_record", default="")
    g.add_argument("--llm-replay", dest="llm_replay", default="")
    g.add_argument("--llm-queue", dest="llm_queue", default="")


def cmd_llm(args) -> int:
    """LLM 后端与人工兜底队列的管理入口（Stage 4）。

    与管线命令分开，因为它是**运行环境的运维面**：看当前用哪个后端、
    有哪些任务等人回填、回填进去。放在管线里会让人以为每次都要手工配。
    """
    from voc_core import llm

    action = args.action

    # ---- backends / config：纯诊断，不需要队列 ----
    if action in ("backends", "config"):
        cfg = llm.LLMConfig.resolve(config_file=args.llm_config or None,
                                     queue_dir=args.llm_queue or "")
        if action == "backends":
            print("可用后端：\n")
            for name, desc in (
                ("openai", "OpenAI 兼容 HTTP 端点（也适用于 DeepSeek/Qwen/豆包等）"),
                ("local", "本机 OpenAI 兼容端点（Ollama / vLLM / llama.cpp），默认 127.0.0.1:11434"),
                ("manual", "人工兜底：任务落盘 → 回填 → 重跑即命中（有状态、可重试）"),
                ("mock", "确定性假响应，跑通链路 / 验收用，不消耗额度"),
                ("replay", "从 --llm-record 的录制文件重放，离线复现"),
                ("auto", "有 openai/local 配置就用它，否则落到 manual（永不硬失败）"),
            ):
                marker = " ← 当前" if cfg.resolved_backend() == name else ""
                print(f"  {name:8s} {desc}{marker}")
            print(f"\n当前解析结果：{cfg.resolved_backend()}（配置里写的是 {cfg.backend}）")
            print("关键值的来源：")
            for k, v in sorted(cfg.sources.items()):
                print(f"  {k:12s} {v}")
            if not cfg.sources:
                print("  （没有任何配置来源 —— 已落到人工兜底）")
        else:
            print(json.dumps(cfg.to_dict(redact=True), ensure_ascii=False, indent=2))
        return 0

    # ---- 队列操作 ----
    be = llm.ManualBackend(llm.LLMConfig.resolve(config_file=args.llm_config or None,
                                                queue_dir=args.llm_queue or ""))

    if action == "status":
        pend, done = be.pending(), be.done()
        print(f"人工兜底队列：{be.root}")
        print(f"  待回填 {len(pend)} 条 ｜ 已完成 {len(done)} 条")
        if pend:
            print("\n  前 5 条待回填：")
            for p in pend[:5]:
                rec = json.loads(p.read_text(encoding="utf-8"))
                print(f"    {rec['task_id']}  {rec.get('created_at', '')}  "
                      f"{_preview_text(rec.get('user', ''), 60)}")
        return 0

    if action == "pending":
        pend = be.pending()
        if not pend:
            print(f"没有待回填任务（{be.root}）")
            return 0
        print(f"待回填 {len(pend)} 条（{be.root}）：\n")
        for p in pend[: max(1, args.limit)]:
            rec = json.loads(p.read_text(encoding="utf-8"))
            print(f"  {rec['task_id']}  {rec.get('created_at', '')}  "
                  f"json_mode={rec.get('json_mode')}  模型={rec.get('model') or '(未指定)'}")
            print(f"      {_preview_text(rec.get('user', ''), 100)}")
        if len(pend) > args.limit:
            print(f"\n  … 其余 {len(pend) - args.limit} 条省略（--limit 调整）")
        print(f"\n看某条的完整提示词：voc.py llm show <task_id>")
        return 0

    if action == "show":
        rec = be.task(args.task_id)
        part = args.part
        if part in ("system", "all"):
            print("=" * 72)
            print("SYSTEM")
            print("=" * 72)
            print(rec.get("system", ""))
        if part in ("user", "all"):
            print("=" * 72)
            print("USER")
            print("=" * 72)
            print(rec.get("user", ""))
        print("=" * 72)
        print(f"task_id={rec['task_id']}  json_mode={rec.get('json_mode')}  "
              f"模型={rec.get('model') or '(未指定)'}")
        print("把模型输出的**原始文本**（JSON）回填：")
        print(f"  voc.py llm fill {rec['task_id']} --text-file out.json")
        return 0

    if action == "fill":
        if args.text_file:
            text = Path(args.text_file).read_text(encoding="utf-8")
        elif args.text:
            text = args.text
        else:
            print("需要 --text-file <文件> 或 --text '<模型输出>'", file=sys.stderr)
            return 2
        out = be.fill(args.task_id, text)
        left = len(be.pending())
        print(f"已回填 {args.task_id} → {out}")
        print(f"剩余待回填 {left} 条。" + ("回填完就重跑原命令即可。" if left else "全部完成，重跑原命令即可。"))
        return 0

    if action == "import":
        n, unknown = be.import_results(args.file)
        print(f"批量回填成功 {n} 条。")
        if unknown:
            print(f"⚠ 有 {len(unknown)} 个 task_id 在队列里找不到（未被写入）：")
            for t in unknown[:10]:
                print(f"    {t}")
            print("  这通常说明提示词变了 → task_id 跟着变。请用 llm pending 取最新的 id。")
            return 1
        print(f"剩余待回填 {len(be.pending())} 条。")
        return 0

    print(f"未知动作 {action}", file=sys.stderr)
    return 2


def _preview_text(text: str, n: int) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[:n] + "…"


# --------------------------------------------------------------------------
# Stage 7 · 编排
# --------------------------------------------------------------------------

#: 默认的 job 根目录（相对当前工作目录）
DEFAULT_RUNS_DIR = "runs"


def _job_id_for(category: str) -> str:
    """把品类名折成一个稳定的 job id：``2026-10-08-nursery-glider``。"""
    import re
    from datetime import date

    slug = re.sub(r"[^\w\-]+", "-", (category or "job").strip(), flags=re.UNICODE)
    slug = re.sub(r"-{2,}", "-", slug).strip("-").lower()
    return f"{date.today().isoformat()}-{slug or 'job'}"


def _spec_from_args(args):
    """把 CLI 参数装进 :class:`JobSpec`（LLM 参数按 dest 名平铺）。"""
    from voc_core.pipeline import LLM_KEYS, JobSpec

    llm: dict = {}
    for k in LLM_KEYS:
        v = getattr(args, k, None)
        if k == "temperature":
            if v is not None:
                llm[k] = v
        elif v not in (None, ""):
            llm[k] = v
    return JobSpec(
        category=args.category or "",
        input=args.input or "",
        comments=args.comments or "",
        source=args.source or "auto",
        keywords=args.keywords or "",
        brands=args.brands or "",
        source_note=args.source_note or "",
        frame=args.frame or "",
        profile=args.profile or "auto",
        artifact_names=args.artifact_names or "auto",
        max_age_years=args.max_age_years,
        mock=bool(args.mock),
        title=args.title or "",
        llm=llm,
    )


def _load_batch_config(path: str) -> dict:
    """批量配置：JSON 或 YAML 子集，形如 ``jobs: [{id, category, input, …}, …]``。"""
    from voc_core.miniyaml import load as yaml_load

    p = Path(path)
    if not p.exists():
        raise SystemExit(f"批量配置文件不存在：{p}")
    try:
        data = yaml_load(p)
    except Exception as exc:  # noqa: BLE001 - 解析错误一律当配置错误报出去
        raise SystemExit(f"批量配置解析失败：{exc}")
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise SystemExit("批量配置需要一个 `jobs:` 列表（每项含 id / category / input …）")
    return data


def _job_json(result) -> dict:
    return {
        "job_dir": result.job_dir.as_posix(),
        "status": result.status,
        "exit_code": result.exit_code,
        "elapsed_s": round(result.elapsed_s, 2),
        "steps": {k: {"status": v.status, "exit_code": v.exit_code,
                      "elapsed_s": round(v.elapsed_s, 3), "note": v.note}
                  for k, v in result.steps.items()},
        "waiting": result.waiting,
        "failed": result.failed,
        "skipped": result.skipped,
    }


def cmd_run(args) -> int:
    """Stage 7 · 编排：一条命令跑完 Phase 1→5，可重跑、可批量。

    这里**不重写**每一步的参数拼装 —— 直接转交给 :mod:`voc_core.pipeline`，它复用
    本文件 ``build_parser()`` 里各子命令的默认 Namespace，因此「手工单步跑」与
    「编排器跑」的参数永远同构。
    """
    from voc_core import pipeline as P

    kwargs = dict(
        only=[s for s in (args.only or "").split(",") if s.strip()],
        start=args.start or "",
        end=args.end or "",
        force=args.force or "",
        dry_run=bool(args.dry_run),
        copy_inputs=not bool(args.no_copy),
        strict=bool(args.strict),
    )

    # ---- 批量模式 ----
    if args.batch:
        data = _load_batch_config(args.batch)
        specs = []
        for item in data["jobs"]:
            if not isinstance(item, dict):
                raise SystemExit("jobs 列表的每一项都应是映射（id / category / input …）")
            item = dict(item)
            jid = str(item.pop("id", "") or _job_id_for(str(item.get("category", ""))))
            specs.append((jid, P.JobSpec.from_dict(item)))
        results, code = P.run_batch(specs, args.runs_dir or DEFAULT_RUNS_DIR, **kwargs)
        if args.json:
            print(json.dumps([_job_json(r) for r in results], ensure_ascii=False, indent=2))
        return code

    # ---- 单次 ----
    if not args.input:
        print("需要 --input <源数据>（或用 --batch <配置> 跑批量）", file=sys.stderr)
        return 2
    spec = _spec_from_args(args)
    job_dir = Path(args.job_dir) if args.job_dir else (
        Path(args.runs_dir or DEFAULT_RUNS_DIR)
        / (args.job_id or _job_id_for(spec.category)))
    result = P.run_job(spec, job_dir, **kwargs)
    if args.json:
        print(json.dumps(_job_json(result), ensure_ascii=False, indent=2))
    return result.exit_code


def cmd_runs(args) -> int:
    """列出 ``runs/`` 下的 job 与状态（Stage 7）。"""
    from voc_core import pipeline as P

    runs_dir = args.runs_dir or DEFAULT_RUNS_DIR
    rows = P.list_jobs(runs_dir)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(f"没有 job（{runs_dir}）—— 用 `voc.py run --input …` 建一个")
        return 0
    print(f"{'job_id':34s} {'状态':14s} {'步':6s} {'框架':10s} {'品类':22s} 更新时间")
    print("-" * 104)
    for r in rows:
        print(f"{r['job_id'][:34]:34s} {r['status']:14s} "
              f"{r['done']}/{r['total']:<4d} {r['frame'][:10]:10s} "
              f"{r['category'][:22]:22s} {r['updated_at']}")
    print(f"\n共 {len(rows)} 个 job（{runs_dir}）")
    return 0


def cmd_eval(args) -> int:
    """跑 Stage 8 评测回归：golden set + 基线比对。

    两层判据（见 ``voc_core/eval`` 模块文档）：**锚点**是人写的下界断言，
    违反即失败；**基线**是冻结的全量指标，任何一格变了都列出来供人复核。
    """
    from voc_core import eval as EV

    cases = EV.discover_cases(
        args.golden_dir,
        only=[s for s in (args.case or "").split(",") if s.strip()],
        tier=EV.resolve_tier(args.all, args.tier))
    if not cases:
        print("没有可跑的评测案（看看 eval/golden/ 下有没有 case.json）。")
        return 2

    report = EV.evaluate(cases, baselines_dir=args.baselines_dir,
                         work_root=args.work_root,
                         freeze=args.freeze, update=args.update)

    if args.markdown:
        p = Path(args.markdown)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(EV.render_markdown(report), encoding="utf-8")
        print(f"Markdown 报告：{p}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(EV.render_console(report))
    return int(report["exit_code"])


def cmd_frames(args) -> int:
    """分析框架：列出 / 查看（Stage 5）。

    ``--frame`` 决定「分析什么」（维度清单 + 分桶轴），与 ``--profile``
    （数据从哪来）正交。这里只做只读展示，改框架直接编辑 ``frames/*.yaml``。
    """
    from voc_core.analysis_frame import (FRAME_DIR, available_frames, frame_path,
                                         load_frame)

    if args.name:
        try:
            f = load_frame(args.name)
        except Exception as exc:  # noqa: BLE001 - 配置错误原样报给用户
            print(f"加载框架 {args.name!r} 失败：{exc}")
            return 2
        print(f"框架：{f.name}（{f.label}）  源文件：{frame_path(f.name) or f.source}")
        if f.description:
            print(f"说明：{f.description}\n")
        print(f"维度（{len(f.active_dims)} 个启用"
              + (f"，{len(f.deprecated_dims())} 个废弃" if f.deprecated_dims() else "")
              + "）：")
        for d in f.dimensions:
            bits = [f"{d.label}", f"category={d.category or '—'}"]
            if d.kind == "classifier":
                bits.append("分类器（脚本注入）")
            if d.persona_bucket:
                bits.append(f"画像桶={d.persona_bucket}")
            if d.experience:
                bits.append("体验类")
            if d.deprecated:
                bits.append("已废弃")
            print(f"  - {d.id:32s} {' | '.join(bits)}")
        ax = f.axis
        if ax is None:
            print("\n分段轴：无（不做人群分桶）")
        else:
            print(f"\n分段轴：{ax.label}（维度 {ax.dim}，分类器 {ax.classifier}）")
            print(f"  产物键名：{ax.distribution_key} / unit_counts.{ax.unit_counts_key}")
            print(f"  进矩阵的桶：{ax.matrix_bucket}"
                  + (f"；被排除的桶：{ax.unknown_bucket}" if ax.unknown_bucket else ""))
            for b in ax.buckets:
                mark = "（排除）" if b.excluded else ""
                print(f"  - {b.id:20s} {b.label_cn}{mark}"
                      f"  短名={b.short_label or '—'}  判定名={b.judge_label or '—'}"
                      f"  画像维度={b.persona_label or '—'}  标签={', '.join(b.tags)}")
        print("\n改「分析什么」直接编辑 frames/*.yaml；换框架用 --frame（只在 tags 那步给一次即可）。")
        return 0

    names = available_frames()
    print(f"框架目录：{FRAME_DIR}\n")
    if not names:
        print("（目录为空）")
        return 1
    for n in names:
        try:
            f = load_frame(n)
            note = f"{len(f.active_dims)} 维度" + (
                f"，轴={f.axis.label}" if f.axis else "，无分段轴")
        except Exception as exc:  # noqa: BLE001 - 坏框架也要列出来，标注清楚
            note = f"⚠ 加载失败：{exc}"
        print(f"  {n:20s} {note}")
    print("\n查看某个框架：python voc.py frames consumer"
          "\n换框架跑分析：python voc.py tags ... --frame b2b_role")
    return 0


def cmd_prompts(args) -> int:
    """提示词模板的查看与自检（Stage 4）。"""
    from voc_core import prompts as P

    if args.check:
        issues = P.check_all(args.lang)
        if issues:
            print(f"提示词自检发现 {len(issues)} 个问题：")
            for i in issues:
                print(f"  ✗ {i}")
            return 1
        print(f"提示词自检通过（{len(P.available())} 个文件，语言 {args.lang}）。")
        return 0

    if args.name:
        text = P.load_template(args.name, args.lang)
        print(text)
        print("-" * 72)
        print(f"占位符：{', '.join(sorted(P.template_vars(args.name, args.lang))) or '（无）'}")
        return 0

    print(f"提示词目录：{P.PROMPT_DIR}\n")
    rows = P.available()
    if not rows:
        print("（目录为空）")
        return 1
    width = max(len(n) for n, _ in rows)
    for name, lang in rows:
        vs = ", ".join(sorted(P.template_vars(name, lang))) or "—"
        print(f"  {name: <{width}}  [{lang}]  {len(P.load_template(name, lang)):>5} 字符  占位符: {vs}")
    print(f"\n共 {len(rows)} 个模板。改措辞直接编辑这些 .md 文件即可，不用碰 Python。")
    return 0


# --------------------------------------------------------------------------
# 命令行解析
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """构造完整命令行解析器。

    抽成独立函数（Stage 7）：``voc_core.pipeline`` 需要按**每个阶段**拿到该子命令
    的默认 :class:`argparse.Namespace`，才能复用同一套 ``cmd_*`` 拼装规则 —— 于是
    「手工单步跑」与「编排器跑」的参数**永远同构**，不存在两套实现漂移的余地。
    """
    ap = argparse.ArgumentParser(
        prog="voc",
        description="通用 VOC 洞察内核 · 数据导入与归一化",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sources", help="列出已注册的数据源适配器")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("inspect", help="体检文件：识别数据源 + 规模统计（只读）")
    p.add_argument("files", nargs="+")
    p.add_argument("--source", default="auto", help="适配器 id 或源名（默认 auto）")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("convert", help="归一化：产出统一契约 + 旧 schema")
    p.add_argument("files", nargs="+")
    p.add_argument("--source", default="auto", help="适配器 id 或源名（默认 auto）")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--category", default="", help="品类名（写入报告与统计）")
    p.add_argument("--default-channel", default="", help="缺渠道时的默认值")
    p.add_argument("--default-brand", default="", help="缺品牌时的默认值")
    p.add_argument("--source-note", default="", help="数据来源说明（写入报告）")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("render", help="把 units.csv 渲染成可读清单")
    p.add_argument("--units", required=True)
    p.add_argument("--out", default="")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("profile", help="查看/导出源画像（决定内核的用户可见文案）")
    p.add_argument("name", nargs="?", default="", help="内置画像名：reddit/amazon/wayfair/generic")
    p.add_argument("--out", default="", help="导出为 JSON（可再传给 --profile）")
    p.set_defaults(func=cmd_profile)

    p = sub.add_parser("clean", help="清洗+分桶+统计（转调内核，管线的第一步）")
    p.add_argument("--input", required=True, help="原始 CSV（任意源列名均可）")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--category", default="Product")
    p.add_argument("--report", default="", help="数据概览 Markdown 的路径")
    p.add_argument("--keywords", default="", help="逗号分隔的品类关键词（相关性打分用）")
    p.add_argument("--brands", default="", help="逗号分隔的品牌/竞品名（品牌识别用）")
    p.add_argument("--comments", default="", help="次级单元 CSV（可选，Reddit 的评论）")
    p.add_argument("--source-note", default="", help="数据来源说明（覆盖报告里的「数据来源」行）")
    p.add_argument("--max-age-years", dest="max_age_years", type=float, default=None,
                   help="超过 N 年的记录被清洗（默认 2.0）")
    p.add_argument("--artifact-names", dest="artifact_names",
                   choices=("auto", "legacy", "neutral"), default="auto",
                   help=("产物命名：auto=Reddit 用旧名、其他源用中性名（默认）；"
                         "legacy/neutral 强制"))
    p.add_argument("--profile", default="auto",
                   help="源画像：内置名 / JSON 路径 / auto（默认按同目录 ingest_report.json 推断）")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("dashboard", help="生成交互式看板（转调内核，按源画像切换文案）")
    p.add_argument("--stats", required=True)
    p.add_argument("--tagged", required=True)
    p.add_argument("--relevant", default="")
    p.add_argument("--comments", default="")
    p.add_argument("--dimensions", default="")
    p.add_argument("--features-json", dest="features_json", default="")
    p.add_argument("--category", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--out", required=True)
    p.add_argument("--no-logo", action="store_true")
    p.add_argument("--profile", default="auto",
                   help="源画像：内置名 / JSON 路径 / auto（默认按 ingest_report.json 推断）")
    p.add_argument("--frame", default="",
                   help="分析框架：内置名 / YAML 路径（留空 = 从产物读，再回落内置 consumer）")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("report", help="生成 Markdown 洞察报告（转调内核）")
    p.add_argument("--dimensions", required=True)
    p.add_argument("--features-json", dest="features_json", required=True)
    p.add_argument("--stats", required=True)
    p.add_argument("--relevant", required=True)
    p.add_argument("--comments", default="")
    p.add_argument("--category", default="")
    p.add_argument("--out", required=True)
    p.add_argument("--profile", default="auto",
                   help="源画像：内置名 / JSON 路径 / auto（默认按 ingest_report.json 推断）")
    p.add_argument("--frame", default="",
                   help="分析框架：内置名 / YAML 路径（留空 = 从产物读，再回落内置 consumer）")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("validate", help="跑质量闸门（Stage 6）：这批产物能不能信")
    p.add_argument("--stats", default="", help="数据集统计 JSON（给了就能自动补齐兄弟产物）")
    p.add_argument("--dimensions", default="", help="维度 JSON")
    p.add_argument("--features-json", dest="features_json", default="", help="功能体验 JSON")
    p.add_argument("--tagged", default="", help="清洗后单元 CSV")
    p.add_argument("--relevant", default="", help="A/B 单元 CSV")
    p.add_argument("--comments", default="", help="次级单元（评论）CSV")
    p.add_argument("--report", default="", help="Markdown 报告（做呈现层检查）")
    p.add_argument("--dashboard", default="", help="看板 HTML（做呈现层检查）")
    p.add_argument("--profile", default="auto",
                   help="源画像：内置名 / JSON 路径 / auto")
    p.add_argument("--only", default="", help="只跑这些检查项（逗号分隔 id）")
    p.add_argument("--skip", default="", help="跳过这些检查项（逗号分隔 id）")
    p.add_argument("--max-quote-miss-rate", dest="max_quote_miss_rate", type=float,
                   default=0.0, help="引文回溯允许的未命中率（默认 0：必须逐字）")
    p.add_argument("--fail-on", choices=("error", "warn", "info"), default="error",
                   help="到这个级别就返回非零退出码（默认 error）")
    p.add_argument("--out", default="", help="quality_report.json 写入路径")
    p.add_argument("--json", action="store_true", help="stdout 直接打印 JSON")
    p.add_argument("--list-checks", action="store_true", help="列出全部检查项")
    p.add_argument("--frame", default="",
                   help="分析框架：内置名 / YAML 路径（留空 = 从产物读，再回落内置 consumer）")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("aggregate",
                       help="聚合标注 → 维度/功能 JSON（转调内核，管线第三步）")
    p.add_argument("--run-dir", dest="run_dir", required=True,
                   help="标注中间文件目录（annotate 的 --run-dir）")
    p.add_argument("--relevant", required=True)
    p.add_argument("--tagset", required=True, help="标签集 JSON（分析框架从这里读）")
    p.add_argument("--dimensions", required=True, help="输出 dimensions.json")
    p.add_argument("--features", required=True, help="输出 features.json")
    p.add_argument("--category", default="")
    p.add_argument("--comments-run-dir", dest="comments_run_dir", default="")
    p.add_argument("--comments-csv", dest="comments_csv", default="")
    p.add_argument("--no-status-gating", dest="no_status_gating", action="store_true",
                   help="关闭分段轴归桶（人群画像标签命中即计数；仅作兜底）")
    p.add_argument("--frame", default="",
                   help="分析框架：内置名 / YAML 路径（留空 = 从 tagset 读）")
    p.add_argument("--profile", default="auto",
                   help="源画像：内置名 / JSON 路径 / auto")
    p.set_defaults(func=cmd_aggregate)

    p = sub.add_parser("tags", help="标签设计（转调内核，会调 LLM）")
    p.add_argument("--category", required=True)
    p.add_argument("--relevant", required=True, help="A/B 单元 CSV（清洗后的相关单元）")
    p.add_argument("--out", required=True, help="标签集 JSON 写入路径")
    p.add_argument("--comments", default="", help="次级单元 CSV（可选，采样时带上评论）")
    p.add_argument("--nsample", type=int, default=0,
                   help="采样条数（0 = 用内核默认 100）")
    p.add_argument("--comments-per-post", dest="comments_per_post", type=int, default=0,
                   help="每篇帖子附带几条评论（0 = 用内核默认 10）")
    p.add_argument("--mock", action="store_true", help="确定性假响应，跑通链路不消耗额度")
    p.add_argument("--frame", default="",
                   help="分析框架：内置名（consumer / b2b_role）或 .yaml 路径；"
                        "留空 = consumer（按购买状态分桶）")
    p.add_argument("--profile", default="auto",
                   help="源画像（决定「帖子/评论」这类称呼）：内置名或 JSON 路径；"
                        "auto = 从数据目录推断（默认）")
    add_llm_cli_arguments(p)
    p.set_defaults(func=cmd_tags)

    p = sub.add_parser("annotate", help="逐条标注（转调内核，会调 LLM）")
    p.add_argument("--tagset", required=True, help="标签集 JSON（tags 的产物）")
    p.add_argument("--relevant", required=True, help="A/B 单元 CSV")
    p.add_argument("--run-dir", dest="run_dir", required=True, help="运行目录（产物落这里）")
    p.add_argument("--comments", default="", help="次级单元 CSV（可选，一并标注）")
    p.add_argument("--export", default="", help="逐帖标注结果 CSV 路径")
    p.add_argument("--export-comments", dest="export_comments", default="",
                   help="逐评论标注结果 CSV 路径")
    p.add_argument("--comment-dims", dest="comment_dims", default="",
                   help="评论只标注这些维度（逗号分隔）")
    p.add_argument("--comment-max", dest="comment_max", type=int, default=0,
                   help="评论最多标注条数（默认 0=不限）")
    p.add_argument("--threads", type=int, default=None, help="并发线程数（默认取配置）")
    p.add_argument("--batch-size", dest="batch_size", type=int, default=None,
                   help="每批帖子数（默认取配置）")
    p.add_argument("--mock", action="store_true", help="确定性假响应，跑通链路不消耗额度")
    p.add_argument("--frame", default="",
                   help="覆盖标签集里的分析框架（一般不用给：默认从 --tagset 读）")
    p.add_argument("--profile", default="auto",
                   help="源画像（决定「帖子/评论」这类称呼）：内置名或 JSON 路径；"
                        "auto = 从数据目录推断（默认）")
    add_llm_cli_arguments(p)
    p.set_defaults(func=cmd_annotate)

    p = sub.add_parser("llm", help="LLM 后端与人工兜底队列（Stage 4）")
    p.add_argument("action", choices=("backends", "config", "status", "pending",
                                     "show", "fill", "import"),
                   help="backends/config/status/pending/show/fill/import")
    p.add_argument("task_id", nargs="?", default="", help="show / fill 的任务 id")
    p.add_argument("--text-file", dest="text_file", default="",
                   help="fill：模型输出所在的文件（推荐，避免转义）")
    p.add_argument("--text", default="", help="fill：模型输出文本（内联）")
    p.add_argument("--file", default="", help="import：结果文件（JSON 或 JSONL）")
    p.add_argument("--limit", type=int, default=20, help="pending：最多列几条（默认 20）")
    p.add_argument("--part", choices=("system", "user", "all"), default="user",
                   help="show：看哪部分提示词（默认 user）")
    p.add_argument("--llm-config", dest="llm_config", default="", help="配置文件路径")
    p.add_argument("--llm-queue", dest="llm_queue", default="", help="人工兜底队列目录")
    p.set_defaults(func=cmd_llm)

    p = sub.add_parser("prompts", help="提示词模板：查看 / 自检（Stage 4）")
    p.add_argument("name", nargs="?", default="", help="模板名，如 annotate.user.zh")
    p.add_argument("--lang", default="zh", help="语言（默认 zh）")
    p.add_argument("--check", action="store_true", help="自检占位符与裸 $")
    p.set_defaults(func=cmd_prompts)

    p = sub.add_parser("frames", help="分析框架：列出 / 查看（Stage 5）")
    p.add_argument("name", nargs="?", default="", help="框架名，如 consumer / b2b_role")
    p.set_defaults(func=cmd_frames)

    p = sub.add_parser("run", help="编排：一条命令跑完 Phase 1→5（Stage 7，可重跑）")
    p.add_argument("--input", default="", help="主单元源数据（用 --batch 时可省）")
    p.add_argument("--comments", default="", help="次级单元（评论）源数据，可选")
    p.add_argument("--category", default="", help="品类名")
    p.add_argument("--source", default="auto", help="适配器 id 或源名（默认 auto）")
    p.add_argument("--keywords", default="", help="逗号分隔的品类关键词")
    p.add_argument("--brands", default="", help="逗号分隔的品牌/竞品名")
    p.add_argument("--source-note", dest="source_note", default="",
                   help="数据来源说明（覆盖报告里的「数据来源」行）")
    p.add_argument("--frame", default="",
                   help="分析框架（Stage 5）：consumer / b2b_role / …yaml")
    p.add_argument("--profile", default="auto",
                   help="源画像（Stage 3）：内置名 / JSON 路径 / auto")
    p.add_argument("--artifact-names", dest="artifact_names",
                   choices=("auto", "legacy", "neutral"), default="auto")
    p.add_argument("--max-age-years", dest="max_age_years", type=float, default=None)
    p.add_argument("--title", default="", help="看板标题覆盖")
    p.add_argument("--mock", action="store_true",
                   help="tags / annotate 用确定性假响应（跑通链路、不消耗额度）")
    p.add_argument("--job-dir", dest="job_dir", default="",
                   help="job 目录（默认 runs/<job-id>）")
    p.add_argument("--job-id", dest="job_id", default="",
                   help="job 名（默认「日期-品类」）")
    p.add_argument("--runs-dir", dest="runs_dir", default=DEFAULT_RUNS_DIR,
                   help=f"job 根目录（默认 {DEFAULT_RUNS_DIR}）")
    p.add_argument("--only", default="", help="只跑这几步（逗号分隔）")
    p.add_argument("--from", dest="start", default="", help="从这步开始跑")
    p.add_argument("--to", dest="end", default="", help="跑到这步为止")
    p.add_argument("--force", nargs="?", const="all", default="",
                   help="强制重跑：--force 全部；--force <step> 从该步起")
    p.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="只打印每步解析出的参数，不执行也不写盘")
    p.add_argument("--strict", action="store_true",
                   help="质量闸门有 error 级发现时也返回非 0（CI 用）")
    p.add_argument("--no-copy", dest="no_copy", action="store_true",
                   help="不把源数据复制进 <job>/input/（只引用外部路径）")
    p.add_argument("--batch", default="",
                   help="批量配置（JSON / YAML，含 `jobs:` 列表）")
    p.add_argument("--json", action="store_true", help="结果以 JSON 输出")
    add_llm_cli_arguments(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("runs", help="列出 job 与状态（Stage 7）")
    p.add_argument("--runs-dir", dest="runs_dir", default=DEFAULT_RUNS_DIR)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("eval", help="评测回归（Stage 8）：改口径后有没有变差")
    p.add_argument("--case", default="", help="只跑这些案（逗号分隔 id）")
    p.add_argument("--tier", default="",
                   help="只跑该 tier（core / extended）；默认 core") 
    p.add_argument("--all", action="store_true",
                   help="跑全部 tier（含 ~3 分钟/案的 mock 端到端案）")
    p.add_argument("--golden-dir", dest="golden_dir", default="eval/golden",
                   help="golden set 根目录（相对 voc-platform/）")
    p.add_argument("--baselines-dir", dest="baselines_dir", default="eval/baselines",
                   help="基线目录（相对 voc-platform/）")
    p.add_argument("--work-root", dest="work_root", default="out/verify/stage8",
                   help="评测产物的落盘根目录")
    p.add_argument("--freeze", action="store_true", help="写基线（首次建立用）")
    p.add_argument("--update", action="store_true",
                   help="覆盖基线（已确认改动、要接受新答案时用）")
    p.add_argument("--markdown", default="", help="把 Markdown 对比表写到该路径")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_eval)

    return ap


def main(argv: list[str] | None = None) -> int:
    """命令行入口：解析参数并分派到对应子命令。"""
    _use_utf8_stdout()
    ap = build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
