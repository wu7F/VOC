#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 4 验收证据：同一品类，三种后端产出的 JSON 结构完全一致。

对应路线图的验收标准（原文）：

    同一品类，三种后端产出的 JSON 结构完全一致（内容质量可不同）。

为什么不能只看「跑通没报错」
----------------------------
换后端出问题的典型症状是**不报错**：套了录制层之后后端名变成 ``openai+record``，
分支判断悄悄走错；提示词搬了家但少了个变量，模型照常回话、只是答得不对。
两类都不抛异常，所以只能靠**结构指纹比对**来证。

本脚本跑的是真链路，不是单元测试里的桩：

* ``openai`` —— 真起一个本地 OpenAI 兼容端点（真 socket、真 urllib、真 Authorization 头）
* ``manual`` —— 真把任务写进 ``pending/``、真读 ``done/``（走「入队 → 回填 → 命中」）
* ``mock``   —— 真走 ``MockBackend``（自定义 responder，不消耗额度、不依赖网络）

三个后端收到的提示词来自**同一份真实 Reddit 数据**（``reddit_posts_relevant.csv``），
回的模型输出是**同一份结构完整的标签集**（内核自带示例标签集，8 维度 / 17 标签），
然后比对：

1. 提示词指纹（``sha1(system)`` / ``sha1(user)``）三者相同 —— 换后端不改提示词
2. 标签集结构指纹三者相同 —— 换后端不改产出形状
3. 每个后端产出的维度键、购买状态分类器、标签字段名逐一列出（人可核对，不只看哈希）

用法::

    python tools/stage4_acceptance.py [--relevant <csv>] [--category "..."]
                                      [--out-dir out/verify/stage4]

退出码：0 通过 ｜ 1 结构不一致（验收失败）｜ 2 输入缺失。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "voc_core" / "kernel"))

from voc_core import llm  # noqa: E402
from voc_core import prompts as P  # noqa: E402

import design_tags as dt  # noqa: E402

DEFAULT_RELEVANT = (ROOT.parent / "data" / "reddit_analysis"
                    / "reddit_posts_relevant.csv")
DEFAULT_CATEGORY = "Nursery Glider Recliner"
DEFAULT_OUT = ROOT / "out" / "verify" / "stage4"


# --------------------------------------------------------------------------
# 本地 OpenAI 兼容端点（真 socket）
# --------------------------------------------------------------------------

class _FakeEndpoint:
    def __init__(self, text: str):
        self.text = text
        self.requests: list[dict] = []
        self._httpd: ThreadingHTTPServer | None = None
        self.port = 0
        self._thread: threading.Thread | None = None

    def start(self) -> "_FakeEndpoint":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append(json.loads(self.rfile.read(n).decode("utf-8")))
                body = json.dumps({
                    "choices": [{"message": {"role": "assistant", "content": outer.text}}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def messages(self) -> dict:
        msgs = self.requests[-1].get("messages") or []
        return {m.get("role"): m.get("content") for m in msgs}


# --------------------------------------------------------------------------
# 结构指纹
# --------------------------------------------------------------------------

def sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def structure_of(ts: dict) -> dict:
    """标签集的**严格结构指纹**：顶层键 / 维度键 / 逐标签字段名（不含具体值）。"""
    dims = ts.get("dimensions") or {}
    return {
        "top_keys": sorted(ts),
        "dimension_keys": sorted(dims),
        "dimension_fields": {dk: sorted((dims.get(dk) or {}).keys()) for dk in sorted(dims)},
        "tag_fields": {
            dk: {str(tk): sorted(td) for tk, td in sorted(((dims.get(dk) or {}).get("tags") or {}).items())
                 if isinstance(td, dict)}
            for dk in sorted(dims)
        },
    }


def normalize(data: dict, category: str) -> dict:
    """复刻内核 ``design_tags.main()`` 里与后端无关的后处理（直接调内核函数）。"""
    ts = data.get("tagset", data) if isinstance(data, dict) else {}
    ts.setdefault("name", f"{category} 标签集")
    ts.setdefault("category", category)
    ts.setdefault("dimensions", {})
    dt.prune_unknown_dims(ts)
    for dk, (lab, _cat) in dt.DIMENSIONS.items():
        ts["dimensions"].setdefault(dk, {"label": lab, "tags": {}})
    dt.inject_purchase_status(ts)
    return ts


# --------------------------------------------------------------------------
# 三种后端各跑一遍
# --------------------------------------------------------------------------

def run_openai(system: str, user: str, canned: str, log: list[str]):
    ep = _FakeEndpoint(canned).start()
    try:
        cfg = llm.LLMConfig.resolve(backend="openai", base_url=ep.base_url,
                                    api_key="sk-stage4-acceptance", retries=1, timeout=15)
        llm.set_default_config(cfg)
        out = llm.call_llm(system, user, json_mode=True)
        got = ep.messages
        log.append(f"  openai  真 HTTP POST {ep.base_url}/chat/completions"
                   f"  → 200  content-type=application/json")
        return out, (got.get("system"), got.get("user"))
    finally:
        ep.stop()


def run_manual(system: str, user: str, canned: str, queue: Path, log: list[str]):
    cfg = llm.LLMConfig.resolve(backend="manual", queue_dir=queue)
    be = llm.ManualBackend(cfg)
    llm.set_default_config(cfg)
    try:
        llm.call_llm(system, user, json_mode=True)
        raise AssertionError("manual 后端第一次调用不该直接返回")
    except llm.ManualPending as exc:
        tid = exc.task_ids[0]
        rec = be.task(tid)
        pend_before = len(be.pending())
        be.fill(tid, canned, by="stage4-acceptance")
        log.append(f"  manual  入队 1 条（task_id={tid}，队列 size={pend_before}）"
                   f" → 回填 done/ → 重跑命中 cached")
    llm.set_default_config(cfg)
    res = llm.call_llm(system, user, json_mode=True)
    got = (rec.get("system"), rec.get("user"))
    for p in be.done():
        p.unlink()
    assert res is not None
    return res, got


def run_mock(system: str, user: str, canned: str, log: list[str]):
    seen = {}

    def responder(s: str, u: str, **_kw):
        seen.update({"system": s, "user": u})
        return canned

    llm.set_default_config(llm.LLMConfig.resolve(backend="mock"), responder=responder)
    out = llm.call_llm(system, user, json_mode=True)
    log.append("  mock    自定义 responder（确定性，不消耗额度、不联网）")
    return out, (seen.get("system"), seen.get("user"))


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 4 验收：三种后端产出同一 JSON 结构")
    ap.add_argument("--relevant", default=str(DEFAULT_RELEVANT))
    ap.add_argument("--category", default=DEFAULT_CATEGORY)
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--nsample", type=int, default=dt.DEFAULT_NSAMPLE)
    args = ap.parse_args()

    relevant = Path(args.relevant)
    if not relevant.exists():
        print(f"[stage4] 输入不存在：{relevant}", file=sys.stderr)
        print("    真实数据在**项目根**的 data/reddit_analysis/ 下，"
              "不在 voc-platform/ 里。用 --relevant 指定路径。", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def emit(s: str = "") -> None:
        lines.append(s)
        print(s)

    emit("Stage 4 验收：同一品类，三种后端产出的 JSON 结构完全一致")
    emit("=" * 78)
    emit(f"品类      ：{args.category}")
    emit(f"输入      ：{relevant}")
    emit(f"提示词    ：{P.PROMPT_DIR}（design_tags.system / design_tags.user）")
    emit(f"模型输出  ：内核自带示例标签集（结构完整：8 维度 / 17 标签）")
    emit()

    # 与内核 main() 完全一致地构造提示词
    rows = dt.load_relevant(relevant, args.nsample)
    samples, comment_block, cstats = dt.build_samples(rows, args.nsample, {}, 0, 0)
    system = dt.DESIGN_SYSTEM
    user = P.render("design_tags.user", category=args.category, nsample=len(rows),
                    n=dt.SAMPLE_TAGS_PER_DIM, samples=samples, comment_block=comment_block)
    emit(f"样本单元  ：{len(rows)} 篇（上限 {args.nsample}）｜评论块 0 条")
    emit(f"system 提示词 {len(system)} 字符  sha1={sha(system)[:16]}")
    emit(f"user   提示词 {len(user)} 字符  sha1={sha(user)[:16]}")
    emit()

    canned = json.dumps(dt.mock_tagset(args.category), ensure_ascii=False)
    emit(f"回给三者的同一份模型输出：{len(canned)} 字符  sha1={sha(canned)[:16]}")
    emit()
    emit("三个后端：")
    log: list[str] = []

    with tempfile.TemporaryDirectory() as td:
        queue = Path(td) / "queue"
        results = {
            "openai": run_openai(system, user, canned, log),
            "manual": run_manual(system, user, canned, queue, log),
            "mock": run_mock(system, user, canned, log),
        }
        for line in log:
            emit(line)

    emit()
    emit("1) 提示词指纹（换后端不该改提示词）")
    emit("-" * 78)
    prompt_ok = True
    ref_p = None
    for name, (_text, (sys_text, user_text)) in results.items():
        row = (sha(sys_text or ""), sha(user_text or ""))
        if ref_p is None:
            ref_p = row
        same = "一致" if row == ref_p else "★不一致"
        prompt_ok &= (row == ref_p) and (sys_text == system) and (user_text == user)
        emit(f"  {name:8s} sha1(system)={row[0][:16]}  sha1(user)={row[1][:16]}  {same}")
    emit(f"  且两者都等于直接渲染出的提示词：{'是' if prompt_ok else '否'}")

    shapes = {name: structure_of(normalize(dt.extract_json(text), args.category))
              for name, (text, _p) in results.items()}

    emit()
    emit("2) 结构指纹（换后端不该改产出形状）")
    emit("-" * 78)
    ref = shapes["openai"]
    ref_json = json.dumps(ref, sort_keys=True, ensure_ascii=False)
    struct_ok = True
    for name, shape in shapes.items():
        same = json.dumps(shape, sort_keys=True, ensure_ascii=False) == ref_json
        struct_ok &= same
        n_tags = sum(len(v) for v in shape["tag_fields"].values())
        emit(f"  {name:8s} sha1(结构)={sha(ref_json)[:16] if same else sha(json.dumps(shape, sort_keys=True))[:16]}"
             f"  维度={len(shape['dimension_keys'])} 有标签的维度={sum(1 for v in shape['tag_fields'].values() if v)}"
             f" 标签={n_tags}  {'一致' if same else '★不一致'}")

    emit()
    emit("3) 结构明细（人可核对，不只看哈希）")
    emit("-" * 78)
    emit(f"顶层键        ：{ref['top_keys']}")
    emit(f"维度键（{len(ref['dimension_keys'])} 个）：")
    for dk in ref["dimension_keys"]:
        tags = ref["tag_fields"].get(dk) or {}
        emit(f"    {dk:28s} {len(tags)} 个标签  字段={','.join(sorted(set().union(*tags.values()))) if tags else '（无）'}")
    emit(f"购买状态分类器：{'、'.join(sorted(ref['tag_fields'].get('purchase_status', {})))}")

    emit()
    emit("=" * 78)
    ok = bool(prompt_ok and struct_ok)
    emit(f"结论：{'通过 ✓ 同一品类下三种后端的 JSON 结构完全相同' if ok else '未通过 ✗ 存在结构差异'}")
    emit("      内容质量可以不同（那是模型的能力），结构必须相同（那是平台的责任）。")

    (out_dir / "acceptance.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "acceptance.json").write_text(json.dumps({
        "category": args.category,
        "relevant": str(relevant),
        "nsample": len(rows),
        "prompt_sha1": {"system": sha(system), "user": sha(user)},
        "canned_sha1": sha(canned),
        "backends": {
            name: {
                "prompt_sha1": {"system": sha(recv[0] or ""), "user": sha(recv[1] or "")},
                "structure_sha1": sha(json.dumps(shapes[name], sort_keys=True, ensure_ascii=False)),
                "dimensions": len(shapes[name]["dimension_keys"]),
                "tags": sum(len(v) for v in shapes[name]["tag_fields"].values()),
            }
            for name, (_t, recv) in results.items()
        },
        "structure_detail": ref,
        "prompt_identical": prompt_ok,
        "structure_identical": struct_ok,
        "verdict": "pass" if ok else "fail",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    llm.set_default_config(None)
    print(f"\n证据已写入：{out_dir / 'acceptance.txt'}")
    print(f"            {out_dir / 'acceptance.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
