#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 4 的守门测试：LLM 层可插拔 + 提示词外置。

路线图的验收标准只有一句话：

    同一品类，三种后端产出的 JSON 结构完全一致（内容质量可不同）。

这句话必须被**直接断言**，而不是靠「跑一遍没报错」——因为换后端出问题的典型
症状恰恰是**不报错**：多了一层装饰（录制/重放）之后后端名变成 ``openai+record``，
分支判断悄悄走错；提示词搬了家但少了个变量，模型照常回话、只是答得不对。
这两类都不会抛异常。所以本文件做三件事：

1. **验收**：三种后端（``openai`` 真 socket / ``manual`` 队列回填 / ``mock`` 假响应）
   下，同一份输入产出的标签集**结构指纹完全相同**，且每个后端**收到的提示词逐字节相同**。
   —— 这是「换后端不改产出形状」的可执行证明。
2. **反证**：故意让某一个后端吐出一份维度不同的 JSON，断言上面的结构比对**会翻红**。
   —— 没有反证的验收测试等于没测（与 ``test_validate.py`` 的变异测试同一思路）。
3. **零回归**：提示词从 Python 常量外置到 ``prompts/*.md`` 之后，5 个模板的渲染结果
   与改造前冻结的基线**逐字节一致**。改一个字的后果是模型输出悄悄变了，
   没有任何业务测试会发现 —— 只能靠这份基线。

外加配置优先级、各后端实现细节、CLI 退出码（0/1/2/3）的常规用例。
"""

from __future__ import annotations

import csv
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "voc_core" / "kernel"))

from voc_core import llm  # noqa: E402
from voc_core import prompts as P  # noqa: E402

import design_tags as dt  # noqa: E402

PY = sys.executable
VOC = ROOT / "voc.py"
GOLD = ROOT / "tests" / "fixtures" / "prompts_golden"

CATEGORY = "Nursery Glider Recliner"

#: 提示词与标签集后处理都跟**分析框架**有关（Stage 5）。这里统一用内置
#: consumer 框架 —— 它就是平台化之前那份写死的 8 维度框架的等价声明，
#: 所以「不传 --frame」时的一切断言都该在它下面成立。
FRAME = dt.default_frame()

#: 干净环境：宿主机器上可能装了 ``LLM连接器`` skill 或设了 ``LLM_CONNECTOR_*``，
#: 不隔离的话子进程会读到它们，测试结论就不再只反映平台自己的代码。
_CLEAN_ENV = {k: v for k, v in os.environ.items()
              if not k.startswith("VOC_LLM_") and not k.startswith("LLM_")
              and k != "VOC_LLM_CONFIG"}


# ==========================================================================
# 夹具：一份「模型输出」，三种后端都回这一份
# ==========================================================================
#
# 刻意只给 2 个维度、4 个标签 —— 结构比对要能看清差异，不需要真实体量。
# 其余维度由内核 ``setdefault`` 补成空标签集，所以结构指纹仍覆盖全部 12 维。

CANNED_TAGSET = {
    "tagset": {
        "name": "Nursery Glider Recliner 标签集",
        "category": CATEGORY,
        "dimensions": {
            "user_personas_purchased": {
                "label": "用户画像·已购",
                "tags": {
                    "owner_long_term": {
                        "category": "user",
                        "thoughts": "仅当发言者已购买并长期使用（数月以上）时标 yes。",
                        "mentioned": "Output 'yes' or 'no'. If 'no', leave all other fields blank.",
                        "original_text": "抽取表明已长期持有的原文",
                        "owner_long_term_summary": "已购·长期使用，围绕耐用性",
                        "sentiment": "n/a",
                    },
                },
            },
            "features": {
                "label": "功能体验",
                "tags": {
                    "swivel": {
                        "category": "feature",
                        "thoughts": "判断是否提到旋转 / 摇摆功能体验。",
                        "mentioned": "Output 'yes' or 'no'. If 'no', leave all other fields blank.",
                        "original_text": "抽取提到旋转/摇摆的原文",
                        "swivel_summary": "旋转摇摆",
                        "swivel_reason": ["顺滑", "异响", "卡顿"],
                        "sentiment": "positive / neutral / negative",
                    },
                    "recline": {
                        "category": "feature",
                        "thoughts": "判断是否提到后仰 / 躺平功能体验。",
                        "mentioned": "Output 'yes' or 'no'. If 'no', leave all other fields blank.",
                        "original_text": "抽取提到后仰/躺平的原文",
                        "recline_summary": "后仰躺平",
                        "sentiment": "positive / neutral / negative",
                    },
                },
            },
        },
    }
}
CANNED_TEXT = json.dumps(CANNED_TAGSET, ensure_ascii=False)

_POSTS = [
    {"reddit_id": "p1", "title": "Best glider ever",
     "summary_text": "I bought this glider three years ago and it is still comfortable "
                     "for daily nursing. The swivel is smooth.",
     "url": "https://www.reddit.com/r/baby/p1", "subreddit": "baby",
     "author": "alice", "published": "2026-01-01"},
    {"reddit_id": "p2", "title": "Looking for a recliner",
     "summary_text": "Any recommendations for a nursery chair with good recline? "
                     "Budget around 400 dollars.",
     "url": "https://www.reddit.com/r/baby/p2", "subreddit": "baby",
     "author": "bob", "published": "2026-02-01"},
    {"reddit_id": "p3", "title": "Squeaky swivel after a year",
     "summary_text": "My glider started squeaking when it swivels. Otherwise fine.",
     "url": "https://www.reddit.com/r/baby/p3", "subreddit": "baby",
     "author": "carol", "published": "2026-03-01"},
]


def write_relevant_csv(path: Path) -> Path:
    """写一份最小的 reddit_posts_relevant.csv（列名用内核认的规范名）。"""
    fields = ["reddit_id", "title", "summary_text", "url", "subreddit", "author", "published"]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in _POSTS:
            w.writerow(r)
    return path


# ==========================================================================
# 一个真的 OpenAI 兼容端点（真开 socket、真走 urllib）
# ==========================================================================
#
# 为什么不 mock 掉 ``urllib.request.urlopen``：Stage 4 的约束是「零依赖、裸解释器
# 也能跑真实 HTTP 后端」。把 urlopen 换掉等于把这段代码从测试里删掉 ——
# 拼 URL、发 Authorization 头、解三种方言的响应、4xx 不重试，全都没被验证。

class FakeOpenAI:
    """最小 OpenAI 兼容服务端。记录收到的每个请求体，供断言使用。"""

    def __init__(self, text: str = CANNED_TEXT, *, status: int = 200,
                 dialect: str = "choices"):
        self.text = text
        self.status = status
        self.dialect = dialect
        self.requests: list[dict] = []
        self.auth_headers: list[str] = []
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = 0

    # ---- 生命周期 ----
    def start(self) -> "FakeOpenAI":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):  # 别刷屏
                pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8")
                try:
                    outer.requests.append(json.loads(raw))
                except json.JSONDecodeError:
                    outer.requests.append({"_raw": raw})
                outer.auth_headers.append(self.headers.get("Authorization", ""))

                if outer.status != 200:
                    body = json.dumps({"error": {"message": "boom"}}).encode()
                    self.send_response(outer.status)
                else:
                    body = json.dumps(outer._body()).encode()
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

    def _body(self) -> dict:
        if self.dialect == "choices":
            return {"choices": [{"message": {"role": "assistant", "content": self.text}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 22}}
        if self.dialect == "choices_text":
            return {"choices": [{"text": self.text}]}
        if self.dialect == "flat":
            return {"content": self.text}
        if self.dialect == "legacy":
            return {"response": self.text}
        raise AssertionError(self.dialect)

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    # ---- 断言辅助 ----
    @property
    def last_messages(self) -> dict:
        msgs = self.requests[-1].get("messages") or []
        out = {}
        for m in msgs:
            out[m.get("role")] = m.get("content")
        return out


# ==========================================================================
# 结构指纹
# ==========================================================================

def tagset_skeleton(ts: dict) -> dict:
    """标签集的**骨架**：与内容无关的结构指纹。

    保留：顶层键集合 / 维度键集合 / 每个维度的字段名 / 每个维度内出现过的
    标签字段名与值类型（取并集）/ 该维度有没有标签。
    丢掉：标签键名本身、文案、reason 词表的条数。

    用于「换一版内容不该翻红」的场景（模型换个措辞、改个标签名都不算结构变化）。
    """
    dims = ts.get("dimensions") or {}
    fp: dict = {
        "top_keys": sorted(ts),
        "dimensions": sorted(dims),
        "dim_fields": {dk: sorted((dims.get(dk) or {}).keys()) for dk in sorted(dims)},
        "tag_fields": {},
    }
    for dk in sorted(dims):
        tags = (dims.get(dk) or {}).get("tags") or {}
        names: set[str] = set()
        types: set[str] = set()
        for _tk, td in tags.items():
            if isinstance(td, dict):
                names |= set(td)
                types |= {type(v).__name__ for v in td.values()}
        fp["tag_fields"][dk] = {"names": sorted(names), "types": sorted(types),
                                "has_tags": bool(tags)}
    return fp


def tagset_shape(ts: dict) -> dict:
    """标签集的**严格结构指纹**：骨架 + 逐标签的键名与字段名。

    比骨架更严：能抓到「某个标签少了一个字段」这类只在单个标签内的结构损伤
    （骨架取的是维度内并集，看不见）。代价是对内容敏感 —— 模型给同一维度换了
    一批标签名就会翻红，所以只在**喂进同一份模型输出**时使用（Stage 4 验收正是
    这种情形：三个后端回同一份 JSON，差异只应来自后端而不该来自内容）。
    """
    dims = ts.get("dimensions") or {}
    fp = tagset_skeleton(ts)
    fp["tags"] = {}
    for dk in sorted(dims):
        tags = (dims.get(dk) or {}).get("tags") or {}
        fp["tags"][dk] = {
            str(tk): sorted(td) for tk, td in sorted(tags.items())
            if isinstance(td, dict)
        }
    return fp


def normalize_tagset(data: dict, category: str = CATEGORY) -> dict:
    """复刻内核 ``design_tags.main()`` 里与后端无关的那段后处理。

    这段代码**必须在所有后端之间共享** —— 它一旦分叉，换后端就会改产出形状。
    直接调用内核自己的函数，而不是在测试里重写一遍：测试写的副本会和产品代码
    一起漂移，那时它测的就是自己。
    """
    ts = data.get("tagset", data) if isinstance(data, dict) else {}
    ts.setdefault("name", f"{category} 标签集")
    ts.setdefault("category", category)
    ts.setdefault("dimensions", {})
    dt.prune_unknown_dims(ts, FRAME)
    for d in FRAME.active_dims:
        ts["dimensions"].setdefault(d.id, {"label": d.label, "tags": {}})
    dt.inject_classifier_dims(ts, FRAME)
    # Stage 5：标签集自带分析框架（产物自描述），下游从产物读、不必再传 --frame。
    ts.setdefault("analysis_frame", FRAME.to_metadata())
    return ts


# ==========================================================================
# A. 配置装配
# ==========================================================================

class TestConfigResolution(unittest.TestCase):
    """``auto`` 的解析规则与四级优先级 —— 这是「永不硬失败」的实现基础。"""

    def test_auto_resolves_to_manual_when_nothing_configured(self):
        cfg = llm.LLMConfig.resolve()
        self.assertEqual(cfg.resolved_backend(), "manual")

    def test_auto_resolves_to_openai_with_url_and_key(self):
        cfg = llm.LLMConfig.resolve(base_url="https://api.example.com/v1", api_key="sk-x")
        self.assertEqual(cfg.resolved_backend(), "openai")

    def test_auto_resolves_to_local_with_url_only(self):
        cfg = llm.LLMConfig.resolve(base_url="http://127.0.0.1:11434/v1")
        self.assertEqual(cfg.resolved_backend(), "local")

    def test_local_gets_default_ollama_url(self):
        cfg = llm.LLMConfig.resolve(backend="local")
        self.assertEqual(cfg.base_url, "http://127.0.0.1:11434/v1")
        self.assertEqual(cfg.resolved_backend(), "local")

    def test_base_url_trailing_slash_stripped(self):
        cfg = llm.LLMConfig.resolve(base_url="https://api.example.com/v1/")
        self.assertEqual(cfg.base_url, "https://api.example.com/v1")

    def test_unknown_backend_raises(self):
        with self.assertRaises(llm.LLMUnavailable) as ctx:
            llm.LLMConfig.resolve(backend="gpt5-turbo-max")
        self.assertIn("未知的 LLM 后端", str(ctx.exception))

    def test_empty_and_none_overrides_are_ignored(self):
        """CLI 的默认值（``""`` / ``None``）不能把配置文件里的值冲掉。"""
        cfg = llm.LLMConfig.resolve(model="from-cli", base_url="", api_key=None)
        self.assertEqual(cfg.model, "from-cli")
        self.assertEqual(cfg.base_url, "")

    def test_cli_beats_env_beats_file(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "voc.llm.json"
            f.write_text(json.dumps({"model": "file-model", "temperature": 0.9}),
                         encoding="utf-8")
            old = os.environ.get("VOC_LLM_MODEL")
            os.environ["VOC_LLM_MODEL"] = "env-model"
            try:
                cfg = llm.LLMConfig.resolve(config_file=f)
                self.assertEqual(cfg.model, "env-model")          # 环境 > 文件
                self.assertEqual(cfg.temperature, 0.9)            # 文件生效
                self.assertIn("配置文件", cfg.sources["temperature"])
                cfg2 = llm.LLMConfig.resolve(config_file=f, model="cli-model")
                self.assertEqual(cfg2.model, "cli-model")         # 命令行 > 环境
                self.assertEqual(cfg2.sources["model"], "命令行")
            finally:
                if old is None:
                    os.environ.pop("VOC_LLM_MODEL", None)
                else:
                    os.environ["VOC_LLM_MODEL"] = old

    def test_legacy_connector_env_still_honoured(self):
        """Stage 3 之前的内核只认 ``LLM_CONNECTOR_*``，老部署不该被破坏。"""
        old = {k: os.environ.get(k) for k in
               ("LLM_CONNECTOR_BASE_URL", "LLM_CONNECTOR_API_KEY", "LLM_CONNECTOR_MODEL")}
        os.environ["LLM_CONNECTOR_BASE_URL"] = "https://legacy.example.com/v1"
        os.environ["LLM_CONNECTOR_API_KEY"] = "sk-legacy"
        os.environ["LLM_CONNECTOR_MODEL"] = "legacy-model"
        try:
            cfg = llm.LLMConfig.resolve()
            self.assertEqual(cfg.base_url, "https://legacy.example.com/v1")
            self.assertEqual(cfg.model, "legacy-model")
            self.assertEqual(cfg.resolved_backend(), "openai")
            self.assertIn("历史名", cfg.sources["base_url"])
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_api_key_env_indirection_keeps_secret_out_of_file(self):
        """配置文件里只写环境变量名，密钥本身不进文件。"""
        old = os.environ.get("MY_LLM_KEY")
        os.environ["MY_LLM_KEY"] = "sk-secret-value"
        try:
            with tempfile.TemporaryDirectory() as td:
                f = Path(td) / "voc.llm.json"
                f.write_text(json.dumps({"api_key_env": "MY_LLM_KEY", "backend": "openai"}),
                             encoding="utf-8")
                cfg = llm.LLMConfig.resolve(config_file=f)
                self.assertEqual(cfg.api_key, "sk-secret-value")
                self.assertIn("api_key_env", cfg.sources["api_key"])
        finally:
            if old is None:
                os.environ.pop("MY_LLM_KEY", None)
            else:
                os.environ["MY_LLM_KEY"] = old

    def test_missing_explicit_config_file_raises(self):
        with self.assertRaises(llm.LLMUnavailable):
            llm.LLMConfig.resolve(config_file="/definitely/not/here/voc.llm.json")

    def test_bad_json_config_raises(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "voc.llm.json"
            f.write_text("{not json", encoding="utf-8")
            with self.assertRaises(llm.LLMUnavailable):
                llm.LLMConfig.resolve(config_file=f)

    def test_optional_fields_keep_their_type(self):
        """``max_tokens`` / ``record`` / ``replay`` 的默认值是 None，推不出类型。

        踩过的坑：靠「当前值的类型」猜目标类型，会把它们全部变成字符串，
        于是 ``max_tokens="1024"`` 一路传到 HTTP 请求体里变成类型错误。
        """
        cfg = llm.LLMConfig.resolve(max_tokens="1024", record="rec.jsonl", replay="rep.jsonl")
        self.assertIsInstance(cfg.max_tokens, int)
        self.assertEqual(cfg.max_tokens, 1024)
        self.assertIsInstance(cfg.record, Path)
        self.assertIsInstance(cfg.replay, Path)

    def test_numeric_fields_coerced(self):
        cfg = llm.LLMConfig.resolve(temperature="0.75", threads="4", batch_size="2",
                                    timeout="30", retries="1")
        self.assertEqual(cfg.temperature, 0.75)
        self.assertEqual(cfg.threads, 4)
        self.assertEqual(cfg.batch_size, 2)
        self.assertEqual(cfg.timeout, 30.0)
        self.assertEqual(cfg.retries, 1)

    def test_normalize_clamps_degenerate_values(self):
        cfg = llm.LLMConfig.resolve(threads="0", batch_size="0", retries="-3",
                                    temperature="0")
        self.assertEqual(cfg.threads, 1)
        self.assertEqual(cfg.batch_size, 1)
        self.assertEqual(cfg.retries, 0)
        self.assertEqual(cfg.temperature, 0.0)

    def test_secrets_are_redacted_in_to_dict(self):
        cfg = llm.LLMConfig.resolve(api_key="sk-1234567890abcdef")
        self.assertEqual(cfg.to_dict()["api_key"], "sk-1***")
        self.assertEqual(cfg.to_dict(redact=False)["api_key"], "sk-1234567890abcdef")


# ==========================================================================
# B. 各后端实现
# ==========================================================================

class TestOpenAICompatBackend(unittest.TestCase):
    """真 socket 上验证 OpenAI 兼容后端（``local`` 与它同一段代码）。"""

    def _cfg(self, base_url, **kw):
        kw.setdefault("retries", 1)      # 测试默认不重试，需要重试的用例自己覆盖
        kw.setdefault("timeout", 10)
        return llm.LLMConfig.resolve(backend="openai", base_url=base_url,
                                     api_key="sk-test", **kw)

    def test_roundtrip_and_request_shape(self):
        with FakeOpenAI() as srv:
            be = llm.OpenAICompatBackend(self._cfg(srv.base_url, model="my-model"))
            res = be.complete("SYSTEM-TEXT", "USER-TEXT", json_mode=True)
            self.assertEqual(res.text, CANNED_TEXT)
            self.assertEqual(res.backend, "openai")
            self.assertGreaterEqual(res.elapsed, 0.0)
            # 请求体：模型名、两条消息、json 模式、温度
            req = srv.requests[-1]
            self.assertEqual(req["model"], "my-model")
            self.assertEqual(req["messages"][0], {"role": "system", "content": "SYSTEM-TEXT"})
            self.assertEqual(req["messages"][1], {"role": "user", "content": "USER-TEXT"})
            self.assertEqual(req["response_format"], {"type": "json_object"})
            self.assertEqual(req["temperature"], 0.3)
            self.assertEqual(srv.auth_headers[-1], "Bearer sk-test")

    def test_url_appends_chat_completions(self):
        with FakeOpenAI() as srv:
            be = llm.OpenAICompatBackend(self._cfg(srv.base_url))
            self.assertEqual(be._url(), f"http://127.0.0.1:{srv.port}/v1/chat/completions")

    def test_url_not_doubled_when_already_specific(self):
        be = llm.OpenAICompatBackend(self._cfg("https://x.example.com/v1/chat/completions"))
        self.assertEqual(be._url(), "https://x.example.com/v1/chat/completions")

    def test_usage_is_read_through(self):
        with FakeOpenAI() as srv:
            be = llm.OpenAICompatBackend(self._cfg(srv.base_url))
            res = be.complete("s", "u")
            self.assertEqual(res.usage.get("completion_tokens"), 22)

    def test_explicit_temperature_and_max_tokens_override_config(self):
        with FakeOpenAI() as srv:
            be = llm.OpenAICompatBackend(self._cfg(srv.base_url, max_tokens=999))
            be.complete("s", "u", temperature=0.0, max_tokens=64)
            req = srv.requests[-1]
            self.assertEqual(req["temperature"], 0.0)
            self.assertEqual(req["max_tokens"], 64)

    def test_all_response_dialects_extract_text(self):
        """兼容端点的方言不统一：choices/message、choices/text、content、response。"""
        for dialect in ("choices", "choices_text", "flat", "legacy"):
            with self.subTest(dialect=dialect):
                srv = FakeOpenAI(dialect=dialect).start()
                try:
                    be = llm.OpenAICompatBackend(self._cfg(srv.base_url))
                    self.assertEqual(be.complete("s", "u").text, CANNED_TEXT)
                finally:
                    srv.stop()

    def test_unrecognisable_response_raises(self):
        srv = FakeOpenAI().start()
        try:
            class Handler:  # 空响应体
                pass
            be = llm.OpenAICompatBackend(self._cfg(srv.base_url))
            with self.assertRaises(llm.LLMUnavailable):
                be._extract_text({"weird": True})
        finally:
            srv.stop()

    def test_4xx_fails_fast_without_retry(self):
        """4xx（除 429）是请求本身的问题，重试只会浪费额度 —— 必须立即失败。"""
        with FakeOpenAI(status=401) as srv:
            be = llm.OpenAICompatBackend(self._cfg(srv.base_url, retries=3))
            with self.assertRaises(llm.LLMUnavailable) as ctx:
                be.complete("s", "u")
            self.assertIn("401", str(ctx.exception))
            self.assertEqual(len(srv.requests), 1, "4xx 不该被重试")

    def test_5xx_is_retried(self):
        with FakeOpenAI(status=500) as srv:
            be = llm.OpenAICompatBackend(self._cfg(srv.base_url, retries=2))
            with self.assertRaises(llm.LLMUnavailable):
                be.complete("s", "u")
            self.assertEqual(len(srv.requests), 2, "5xx 应该重试到上限")

    def test_connection_failure_is_always_normalized(self):
        """连不上**必须**归一成 LLMUnavailable。

        Windows 上实测有两条不同的失败路径：连到已关闭的端口可能得到
        ``URLError``（连接被拒），也可能在**读响应阶段**得到裸的
        ``ConnectionResetError``（远程主机关闭连接）。后者不是 URLError，
        只捕 URLError 会让它带着原始栈穿出库层 —— 内核只 except LLMUnavailable，
        于是「配置错误」变成「退出码 1 + 一屏 traceback」。
        这条用例把两种情形都钉住（各跑几次，覆盖 bind-then-close 的时序差异）。
        """
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead_port = s.getsockname()[1]
        be = llm.OpenAICompatBackend(self._cfg(f"http://127.0.0.1:{dead_port}/v1"))
        for i in range(3):
            with self.subTest(attempt=i):
                with self.assertRaises(llm.LLMUnavailable):
                    be.complete("s", "u")

    def test_connection_reset_is_normalized(self):
        """直接构造裸 ConnectionResetError，确认它与 URLError 走同一条路。"""
        be = llm.OpenAICompatBackend(self._cfg("http://127.0.0.1:9/v1"))
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=ConnectionResetError(10054, "reset")):
            with self.assertRaises(llm.LLMUnavailable) as ctx:
                be.complete("s", "u")
            self.assertIn("ConnectionResetError", str(ctx.exception))

    def test_missing_api_key_raises_before_network(self):
        be = llm.OpenAICompatBackend(llm.LLMConfig.resolve(
            backend="openai", base_url="https://api.example.com/v1"))
        with self.assertRaises(llm.LLMUnavailable) as ctx:
            be.complete("s", "u")
        self.assertIn("api_key", str(ctx.exception))

    def test_missing_base_url_raises(self):
        be = llm.OpenAICompatBackend(llm.LLMConfig.resolve(backend="openai", api_key="k"))
        with self.assertRaises(llm.LLMUnavailable):
            be.complete("s", "u")

    def test_local_and_openai_share_implementation(self):
        with FakeOpenAI() as srv:
            cfg = llm.LLMConfig.resolve(backend="local", base_url=srv.base_url,
                                        api_key="ollama", retries=1)
            be = llm.build_backend(cfg)
            self.assertIsInstance(be, llm.OpenAICompatBackend)
            self.assertEqual(be.name, "local")
            self.assertEqual(be.complete("s", "u").text, CANNED_TEXT)


class TestManualBackend(unittest.TestCase):
    """人工兜底：有状态、可重试、断点续传。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.queue = Path(self._td.name) / "queue"

    def tearDown(self):
        self._td.cleanup()

    def _be(self):
        return llm.ManualBackend(llm.LLMConfig.resolve(queue_dir=self.queue))

    def test_first_call_enqueues_and_raises_pending(self):
        be = self._be()
        with self.assertRaises(llm.ManualPending) as ctx:
            be.complete("SYS", "USR", json_mode=True)
        exc = ctx.exception
        self.assertEqual(exc.pending_count, 1)
        self.assertEqual(len(exc.task_ids), 1)
        # 任务文件里存着完整提示词，可以照抄去问模型
        tid = exc.task_ids[0]
        rec = json.loads((self.queue / "pending" / f"{tid}.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["system"], "SYS")
        self.assertEqual(rec["user"], "USR")
        self.assertTrue(rec["json_mode"])
        self.assertIn("created_at", rec)

    def test_rerun_is_idempotent(self):
        """同一提示词重跑不产生重复任务（task_id 内容寻址）。"""
        be = self._be()
        with self.assertRaises(llm.ManualPending) as a:
            be.complete("SYS", "USR")
        with self.assertRaises(llm.ManualPending) as b:
            be.complete("SYS", "USR")
        self.assertEqual(a.exception.task_ids, b.exception.task_ids)
        self.assertEqual(len(be.pending()), 1)

    def test_task_id_is_content_addressed_and_model_agnostic(self):
        """task_id 只由提示词决定 —— 换模型不该让已回填的答案作废。"""
        t1 = llm.task_id_for("SYS", "USR", True)
        self.assertEqual(t1, llm.task_id_for("SYS", "USR", True))
        self.assertNotEqual(t1, llm.task_id_for("SYS", "USR", False))
        self.assertNotEqual(t1, llm.task_id_for("SYS", "USR2", True))
        self.assertNotEqual(t1, llm.task_id_for("SYS2", "USR", True))
        # 换模型名不影响
        cfg_a = llm.LLMConfig.resolve(queue_dir=self.queue, model="m-a")
        cfg_b = llm.LLMConfig.resolve(queue_dir=self.queue, model="m-b")
        ra = llm.ManualBackend(cfg_a)
        rb = llm.ManualBackend(cfg_b)
        with self.assertRaises(llm.ManualPending) as a:
            ra.complete("SYS", "USR")
        with self.assertRaises(llm.ManualPending) as b:
            rb.complete("SYS", "USR")
        self.assertEqual(a.exception.task_ids, b.exception.task_ids)

    def test_fill_then_rerun_returns_cached(self):
        """完整闭环：入队 → 退出码 3 → 回填 → 重跑命中（这正是断点续传）。"""
        be = self._be()
        with self.assertRaises(llm.ManualPending) as ctx:
            be.complete("SYS", "USR")
        tid = ctx.exception.task_ids[0]
        be.fill(tid, CANNED_TEXT, by="tester")
        res = be.complete("SYS", "USR")
        self.assertEqual(res.text, CANNED_TEXT)
        self.assertTrue(res.cached)
        self.assertEqual(res.task_id, tid)
        self.assertEqual(len(be.pending()), 0)
        self.assertEqual(len(be.done()), 1)
        rec = json.loads((self.queue / "done" / f"{tid}.json").read_text(encoding="utf-8"))
        self.assertEqual(rec["filled_by"], "tester")
        self.assertIn("filled_at", rec)

    def test_fill_unknown_task_raises(self):
        with self.assertRaises(llm.LLMError):
            self._be().fill("deadbeefdeadbeef", "x")

    def test_task_lookup_prefers_pending(self):
        be = self._be()
        with self.assertRaises(llm.ManualPending) as ctx:
            be.complete("SYS", "USR")
        tid = ctx.exception.task_ids[0]
        self.assertEqual(be.task(tid)["user"], "USR")
        with self.assertRaises(llm.LLMError):
            be.task("0000000000000000")

    def test_import_results_jsonl(self):
        be = self._be()
        with self.assertRaises(llm.ManualPending) as ctx:
            be.complete("SYS", "USR")
        tid = ctx.exception.task_ids[0]
        f = Path(self._td.name) / "results.jsonl"
        f.write_text(json.dumps({"task_id": tid, "text": CANNED_TEXT}, ensure_ascii=False) + "\n",
                     encoding="utf-8")
        n, unknown = be.import_results(f)
        self.assertEqual((n, unknown), (1, []))
        self.assertEqual(be.complete("SYS", "USR").text, CANNED_TEXT)

    def test_import_results_json_object(self):
        be = self._be()
        with self.assertRaises(llm.ManualPending) as ctx:
            be.complete("SYS", "USR")
        tid = ctx.exception.task_ids[0]
        f = Path(self._td.name) / "results.json"
        f.write_text(json.dumps({tid: CANNED_TEXT}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(be.import_results(f), (1, []))

    def test_import_results_single_line_jsonl_is_not_mistaken_for_a_map(self):
        """一行的 JSONL 与「task_id → 文本」映射都是 ``{...}`` 开头。

        踩过的坑：靠「开头是 ``{`` 且第一段里没有换行」来猜，会把单行 JSONL
        当成映射对象，于是把 ``"task_id"`` / ``"text"`` 这两个**字面键**当成
        任务 id 去找，静默返回「0 条成功 + 2 个未知 id」。
        """
        be = self._be()
        with self.assertRaises(llm.ManualPending) as ctx:
            be.complete("SYS", "USR")
        tid = ctx.exception.task_ids[0]
        f = Path(self._td.name) / "one_line.jsonl"
        f.write_text(json.dumps({"task_id": tid, "text": CANNED_TEXT}, ensure_ascii=False),
                     encoding="utf-8")
        n, unknown = be.import_results(f)
        self.assertEqual((n, unknown), (1, []))

    def test_import_results_multiline_jsonl(self):
        be = self._be()
        with self.assertRaises(llm.ManualPending) as ctx:
            be.complete("SYS", "USR")
        tid = ctx.exception.task_ids[0]
        f = Path(self._td.name) / "multi.jsonl"
        f.write_text("\n".join([
            json.dumps({"task_id": tid, "text": CANNED_TEXT}, ensure_ascii=False),
            json.dumps({"task_id": "ffffffffffffffff", "text": "ghost"}, ensure_ascii=False),
        ]) + "\n", encoding="utf-8")
        n, unknown = be.import_results(f)
        self.assertEqual(n, 1)
        self.assertEqual(unknown, ["ffffffffffffffff"])

    def test_import_results_non_string_text_is_json_encoded(self):
        """映射对象的值可以是 JSON 结构，不是字符串时按 JSON 序列化。"""
        be = self._be()
        with self.assertRaises(llm.ManualPending) as ctx:
            be.complete("SYS", "USR")
        tid = ctx.exception.task_ids[0]
        f = Path(self._td.name) / "obj.json"
        f.write_text(json.dumps({tid: {"tagset": {"dimensions": {}}}}, ensure_ascii=False),
                     encoding="utf-8")
        self.assertEqual(be.import_results(f), (1, []))
        self.assertIn("tagset", be.complete("SYS", "USR").text)

    def test_import_results_bad_line_reports_line_number(self):
        be = self._be()
        with self.assertRaises(llm.ManualPending):
            be.complete("SYS", "USR")
        f = Path(self._td.name) / "broken.jsonl"
        f.write_text('{"task_id": "aaaaaaaaaaaaaaaa", "text": "ok"}\nnot json\n',
                     encoding="utf-8")
        with self.assertRaises(llm.LLMError) as ctx:
            be.import_results(f)
        self.assertIn("第 2 行", str(ctx.exception))

    def test_import_results_line_without_task_id_says_what_is_expected(self):
        be = self._be()
        with self.assertRaises(llm.ManualPending):
            be.complete("SYS", "USR")
        f = Path(self._td.name) / "no_id.jsonl"
        f.write_text('{"text": "ok"}\n', encoding="utf-8")
        with self.assertRaises(llm.LLMError) as ctx:
            be.import_results(f)
        self.assertIn("task_id", str(ctx.exception))
        self.assertIn("JSONL", str(ctx.exception))

    def test_import_returns_unmatched_ids(self):
        """未匹配的 task_id **必须**回传 —— 静默丢弃会让人以为「我明明回填了」。"""
        be = self._be()
        with self.assertRaises(llm.ManualPending) as ctx:
            be.complete("SYS", "USR")
        tid = ctx.exception.task_ids[0]
        f = Path(self._td.name) / "results.json"
        f.write_text(json.dumps({tid: "ok", "ffffffffffffffff": "ghost"},
                                ensure_ascii=False), encoding="utf-8")
        n, unknown = be.import_results(f)
        self.assertEqual(n, 1)
        self.assertEqual(unknown, ["ffffffffffffffff"])

    def test_import_missing_file_raises(self):
        with self.assertRaises(llm.LLMError):
            self._be().import_results(Path(self._td.name) / "nope.jsonl")

    def test_info_reports_queue_sizes(self):
        be = self._be()
        with self.assertRaises(llm.ManualPending):
            be.complete("SYS", "USR")
        info = be.info()
        self.assertEqual(info["backend"], "manual")
        self.assertEqual(info["pending"], 1)
        self.assertEqual(info["done"], 0)


class TestMockReplayRecording(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_mock_default_responder_is_deterministic(self):
        be = llm.MockBackend(llm.LLMConfig.resolve(backend="mock"))
        a = be.complete("SYS", "USR", json_mode=True).text
        b = be.complete("SYS", "USR", json_mode=True).text
        self.assertEqual(a, b)
        self.assertTrue(json.loads(a)["mock"])
        self.assertNotEqual(a, be.complete("SYS", "OTHER", json_mode=True).text)

    def test_mock_uses_custom_responder(self):
        seen = {}

        def responder(system, user, **kw):
            seen.update({"system": system, "user": user, "kw": kw})
            return CANNED_TEXT

        be = llm.MockBackend(llm.LLMConfig.resolve(backend="mock"), responder)
        self.assertEqual(be.complete("SYS", "USR", json_mode=True).text, CANNED_TEXT)
        self.assertEqual(seen["system"], "SYS")
        self.assertEqual(seen["user"], "USR")
        self.assertTrue(seen["kw"]["json_mode"])

    def test_record_then_replay_roundtrip(self):
        rec = self.tmp / "rec.jsonl"
        cfg = llm.LLMConfig.resolve(backend="mock", record=rec)
        be = llm.build_backend(cfg, responder=lambda s, u, **k: CANNED_TEXT)
        self.assertEqual(be.name, "mock+record")
        be.complete("SYS", "USR", json_mode=True)
        self.assertTrue(rec.exists())

        cfg2 = llm.LLMConfig.resolve(backend="replay", replay=rec)
        rb = llm.build_backend(cfg2)
        res = rb.complete("SYS", "USR", json_mode=True)
        self.assertEqual(res.text, CANNED_TEXT)
        self.assertTrue(res.cached)

    def test_replay_miss_raises_instead_of_empty_string(self):
        """没录到就报错。静默返回空串会让下游拿到一份「看起来正常」的废产物。"""
        rec = self.tmp / "rec.jsonl"
        llm.build_backend(llm.LLMConfig.resolve(backend="mock", record=rec),
                          responder=lambda s, u, **k: CANNED_TEXT).complete("SYS", "USR")
        rb = llm.build_backend(llm.LLMConfig.resolve(backend="replay", replay=rec))
        with self.assertRaises(llm.LLMUnavailable) as ctx:
            rb.complete("SYS", "NEVER-RECORDED")
        self.assertIn("没有任务", str(ctx.exception))

    def test_replay_needs_a_file(self):
        with self.assertRaises(llm.LLMUnavailable):
            llm.build_backend(llm.LLMConfig.resolve(backend="replay"))
        with self.assertRaises(llm.LLMUnavailable):
            llm.build_backend(llm.LLMConfig.resolve(backend="replay",
                                                    replay=self.tmp / "nope.jsonl"))

    def test_recorded_record_keeps_effective_temperature(self):
        rec = self.tmp / "rec.jsonl"
        be = llm.build_backend(llm.LLMConfig.resolve(backend="mock", record=rec,
                                                     temperature=0.7),
                               responder=lambda s, u, **k: "x")
        be.complete("SYS", "USR")
        rec1 = json.loads(rec.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec1["temperature"], 0.7)         # 未显式传 → 取内层配置
        be.complete("SYS", "USR2", temperature=0.0)
        rec2 = json.loads(rec.read_text(encoding="utf-8").splitlines()[1])
        self.assertEqual(rec2["temperature"], 0.0)         # 显式传 → 用它


class TestCompatLayer(unittest.TestCase):
    """旧连接器契约（``call_llm`` / ``is_external_backend`` / ``LocalFallback``）。"""

    def tearDown(self):
        llm.set_default_config(None)

    def test_resolved_name_unwraps_recording_layer(self):
        """套了录制层后 name 变 ``mock+record``，直接比对会静默走错分支。"""
        with tempfile.TemporaryDirectory() as td:
            be = llm.build_backend(llm.LLMConfig.resolve(backend="mock", record=Path(td) / "r.jsonl"),
                                   responder=lambda s, u, **k: "x")
            self.assertEqual(be.name, "mock+record")
            self.assertEqual(llm.resolved_name(be), "mock")

    def test_is_external_backend_true_for_non_manual(self):
        for backend in ("mock", "replay"):
            with self.subTest(backend=backend):
                if backend == "replay":
                    continue  # 需要录制文件，单独测
                llm.set_default_config(llm.LLMConfig.resolve(backend=backend))
                self.assertTrue(llm.is_external_backend())

    def test_is_external_backend_false_for_manual(self):
        llm.set_default_config(llm.LLMConfig.resolve(backend="manual"))
        self.assertFalse(llm.is_external_backend())

    def test_call_llm_returns_text_or_tuple(self):
        llm.set_default_config(llm.LLMConfig.resolve(backend="mock"),
                               responder=lambda s, u, **k: CANNED_TEXT)
        self.assertEqual(llm.call_llm("s", "u"), CANNED_TEXT)
        text, usage = llm.call_llm("s", "u", return_usage=True)
        self.assertEqual(text, CANNED_TEXT)
        self.assertIsInstance(usage, dict)

    def test_local_fallback_alias_is_preserved(self):
        """内核写的是 ``except LocalFallback``，改名不能让那些分支失联。"""
        self.assertIs(llm.LocalFallback, llm.LLMUnavailable)
        self.assertTrue(issubclass(llm.ManualPending, llm.LLMError))

    def test_backend_info_survives_a_broken_backend(self):
        """诊断函数自己不该炸：拿不到后端要报「unavailable」，info() 出错要记下来。"""
        with mock.patch.object(llm, "get_backend",
                               side_effect=llm.LLMUnavailable("没有可用后端")):
            info = llm.backend_info()
        self.assertEqual(info["backend"], "unavailable")
        self.assertIn("没有可用后端", info["error"])

        class Broken:
            name = "broken"

            def complete(self, *_a, **_k):  # pragma: no cover
                raise llm.LLMUnavailable("x")

            def info(self):
                raise RuntimeError("info 炸了")

        with mock.patch.object(llm, "get_backend", return_value=Broken()):
            info = llm.backend_info()
        self.assertEqual(info["backend"], "broken")
        self.assertIn("info_error", info)
        self.assertIn("info 炸了", info["info_error"])

    def test_exit_codes_are_pinned(self):
        self.assertEqual(llm.EXIT_PENDING, 3)
        self.assertEqual(llm.EXIT_CONFIG, 2)


# ==========================================================================
# C. ★ Stage 4 验收：同一品类，三种后端产出同一结构
# ==========================================================================

class TestSameCategorySameStructureAcrossBackends(unittest.TestCase):
    """路线图验收标准的直接断言。

    做法：把**同一份提示词**分别送给三个后端，三者都回**同一份**模型输出，
    然后断言

    * 每个后端**收到的提示词逐字节相同**（换后端不改提示词 —— 这是结构的根因），
    * 三个后端产出的标签集**结构指纹完全相同**（维度键 / 字段名 / 字段类型）。

    三者的差异只在「谁来出这份 JSON」：真 HTTP 端点、磁盘待办队列、假响应。
    """

    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._td.name)
        cls.csv = write_relevant_csv(cls.tmp / "relevant.csv")

        rows = dt.load_relevant(cls.csv, dt.DEFAULT_NSAMPLE)
        samples, comment_block, _ = dt.build_samples(rows, dt.DEFAULT_NSAMPLE, {}, 0, 0)
        cls.system, cls.user = dt.build_design_prompts(
            FRAME, category=CATEGORY, nsample=len(rows), samples=samples,
            comment_block=comment_block, unit="帖子", secondary="评论",
            secondary_speaker="评论者", lang="zh")

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def tearDown(self):
        llm.set_default_config(None)

    # ---- 三个后端各自跑一遍，返回 (标签集, 实际收到的 (system, user)) ----

    def _run_openai(self, text=CANNED_TEXT):
        srv = FakeOpenAI(text, dialect="choices").start()
        try:
            llm.set_default_config(llm.LLMConfig.resolve(
                backend="openai", base_url=srv.base_url, api_key="sk-test",
                temperature=0.3, retries=1, timeout=10))
            out = llm.call_llm(self.system, self.user, json_mode=True)
            got = srv.last_messages
            return out, (got.get("system"), got.get("user"))
        finally:
            srv.stop()

    def _run_manual(self, text=CANNED_TEXT):
        queue = self.tmp / "accept_queue"
        cfg = llm.LLMConfig.resolve(backend="manual", queue_dir=queue, temperature=0.3)
        llm.set_default_config(cfg)
        be = llm.ManualBackend(cfg)
        try:
            llm.call_llm(self.system, self.user, json_mode=True)
        except llm.ManualPending as exc:
            tid = exc.task_ids[0]
            rec = be.task(tid)
            be.fill(tid, text, by="acceptance-test")
            got = (rec.get("system"), rec.get("user"))
        else:  # pragma: no cover - 有回填残留才会发生
            raise AssertionError("manual 后端第一次调用不该直接返回")
        llm.set_default_config(cfg)
        out = llm.call_llm(self.system, self.user, json_mode=True)
        # 复位队列，避免影响下一次
        for p in be.done():
            p.unlink()
        return out, got

    def _run_mock(self, text=CANNED_TEXT):
        seen = {}

        def responder(system, user, **kw):
            seen.update({"system": system, "user": user})
            return text

        llm.set_default_config(llm.LLMConfig.resolve(backend="mock", temperature=0.3),
                               responder=responder)
        out = llm.call_llm(self.system, self.user, json_mode=True)
        return out, (seen.get("system"), seen.get("user"))

    # ---- 验收断言 ----

    def test_three_backends_produce_identical_structure(self):
        outs, prompts = {}, {}
        for name, runner in (("openai", self._run_openai),
                             ("manual", self._run_manual),
                             ("mock", self._run_mock)):
            text, recv = runner()
            outs[name] = normalize_tagset(dt.extract_json(text), CATEGORY)
            prompts[name] = recv

        # 前提 1：三个后端收到的是**同一份提示词**（逐字节）
        for name, (sys_text, user_text) in prompts.items():
            with self.subTest(backend=name):
                self.assertEqual(sys_text, self.system,
                                 f"{name} 收到的 system 提示词被改动了")
                self.assertEqual(user_text, self.user,
                                 f"{name} 收到的 user 提示词被改动了")

        # 前提 2：产出结构指纹完全相同 —— 这就是验收标准
        shapes = {name: tagset_shape(ts) for name, ts in outs.items()}
        ref = shapes["openai"]
        for name, shape in shapes.items():
            with self.subTest(backend=name):
                self.assertEqual(shape, ref, f"{name} 的 JSON 结构与 openai 不一致")

        # 顺带把「结构到底长什么样」钉住，防止三个后端一起漂移
        self.assertEqual(ref["top_keys"],
                         ["analysis_frame", "category", "dimensions", "name"])
        self.assertEqual(set(ref["dimensions"]), set(FRAME.dim_ids()))
        self.assertTrue(ref["tag_fields"]["features"]["has_tags"])
        self.assertTrue(ref["tag_fields"]["user_personas_purchased"]["has_tags"])
        # 购买状态分类器在所有后端下都被注入（它是归桶闸门，必须一致）。
        # 注意它落在独立的 purchase_status 维度，不是并进两个画像维度 ——
        # 这样「谁算已购」这套判定标准不会被 LLM 换一版措辞就改掉。
        for name, ts in outs.items():
            with self.subTest(backend=name, check="purchase_status"):
                ps_tags = ts["dimensions"]["purchase_status"]["tags"]
                self.assertEqual({"ps_purchased", "ps_non_purchased", "ps_unknown"},
                                 set(ps_tags), f"{name} 的购买状态分类器缺失或被改写")

    def test_structure_check_is_not_vacuous(self):
        """反证：让 mock 少吐一个维度，上面的结构比对**必须**翻红。

        没有这一条，前一个用例可能只是「永远相等」的空断言。
        """
        broken = json.loads(json.dumps(CANNED_TAGSET))
        del broken["tagset"]["dimensions"]["features"]
        broken_text = json.dumps(broken, ensure_ascii=False)

        good = normalize_tagset(dt.extract_json(self._run_openai()[0]), CATEGORY)
        bad = normalize_tagset(dt.extract_json(self._run_mock(broken_text)[0]), CATEGORY)

        self.assertNotEqual(tagset_shape(bad), tagset_shape(good),
                            "少了一个维度却判定结构相同 → 结构比对失效了")

    def test_structure_check_detects_field_change(self):
        """反证 2：只去掉**一个标签的一个字段**（维度和标签都在）也要能翻红。"""
        broken = json.loads(json.dumps(CANNED_TAGSET))
        del broken["tagset"]["dimensions"]["features"]["tags"]["swivel"]["original_text"]
        broken_text = json.dumps(broken, ensure_ascii=False)

        good = normalize_tagset(dt.extract_json(self._run_openai()[0]), CATEGORY)
        bad = normalize_tagset(dt.extract_json(self._run_mock(broken_text)[0]), CATEGORY)
        self.assertNotEqual(tagset_shape(bad), tagset_shape(good),
                            "单个标签少了一个字段却没被发现")

    def test_structure_check_detects_tag_rename(self):
        """反证 3：标签被改名（结构位相同、键名不同）也要能翻红。"""
        broken = json.loads(json.dumps(CANNED_TAGSET))
        tags = broken["tagset"]["dimensions"]["features"]["tags"]
        tags["swivel_renamed"] = tags.pop("swivel")
        broken_text = json.dumps(broken, ensure_ascii=False)

        good = normalize_tagset(dt.extract_json(self._run_openai()[0]), CATEGORY)
        bad = normalize_tagset(dt.extract_json(self._run_mock(broken_text)[0]), CATEGORY)
        self.assertNotEqual(tagset_shape(bad), tagset_shape(good))

    def test_structure_check_ignores_content(self):
        """反向确认：**只改文案、不改骨架**不该翻红（否则测试会变成噪声源）。"""
        other = json.loads(json.dumps(CANNED_TAGSET))
        other["tagset"]["dimensions"]["features"]["tags"]["swivel"]["swivel_summary"] = \
            "完全不同的措辞"
        other["tagset"]["dimensions"]["features"]["tags"]["swivel"]["swivel_reason"] = ["只留一个"]
        other["tagset"]["name"] = "换个名字"

        a = normalize_tagset(dt.extract_json(self._run_openai()[0]), CATEGORY)
        b = normalize_tagset(dt.extract_json(self._run_mock(json.dumps(other, ensure_ascii=False))[0]),
                             CATEGORY)
        self.assertEqual(tagset_skeleton(a), tagset_skeleton(b))


class TestAcceptanceThroughRealCLI(unittest.TestCase):
    """同一份验收，但走**真子进程 + 真命令行**：证明平台链路而非库函数可用。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.csv = write_relevant_csv(self.tmp / "relevant.csv")

    def tearDown(self):
        self._td.cleanup()

    def _voc(self, *argv, timeout=180):
        return subprocess.run([PY, str(VOC), *argv], cwd=str(ROOT), env=_CLEAN_ENV,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)

    def test_openai_backend_end_to_end(self):
        with FakeOpenAI() as srv:
            out = self.tmp / "tagset.json"
            r = self._voc("tags", "--category", CATEGORY, "--relevant", str(self.csv),
                          "--out", str(out),
                          "--backend", "openai", "--base-url", srv.base_url,
                          "--api-key", "sk-test")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            ts = normalize_tagset(json.loads(out.read_text(encoding="utf-8"))["tagset"], CATEGORY)
            self.assertTrue(ts["dimensions"]["features"]["tags"])
            # 子进程真的把外置提示词发出来了（不是空串、不是代码里的旧副本）
            self.assertIn("Nursery Glider Recliner", srv.last_messages["user"])
            self.assertEqual(srv.requests[-1]["response_format"], {"type": "json_object"})

    def test_manual_backend_exit3_then_fill_then_exit0(self):
        """退出码 3 的语义：**不是失败**，是「等你回填」。"""
        out = self.tmp / "tagset.json"
        queue = self.tmp / "queue"
        base = ["tags", "--category", CATEGORY, "--relevant", str(self.csv),
                "--out", str(out), "--backend", "manual", "--llm-queue", str(queue)]

        r1 = self._voc(*base)
        self.assertEqual(r1.returncode, 3, r1.stdout + r1.stderr)
        self.assertIn("人工兜底队列", r1.stdout)
        self.assertIn("llm fill", r1.stdout)
        self.assertFalse(out.exists(), "还没回填就不该写出产物")

        pend = sorted((queue / "pending").glob("*.json"))
        self.assertEqual(len(pend), 1)
        tid = pend[0].stem

        r2 = self._voc("llm", "fill", tid, "--text-file", str(self._canned_file()),
                       "--llm-queue", str(queue))
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)

        r3 = self._voc(*base)
        self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)
        self.assertTrue(out.exists())
        ts = normalize_tagset(json.loads(out.read_text(encoding="utf-8"))["tagset"], CATEGORY)
        self.assertTrue(ts["dimensions"]["features"]["tags"])

    def _canned_file(self) -> Path:
        p = self.tmp / "canned.json"
        if not p.exists():
            p.write_text(CANNED_TEXT, encoding="utf-8")
        return p

    def test_manual_rerun_does_not_duplicate_tasks(self):
        out = self.tmp / "tagset.json"
        queue = self.tmp / "queue"
        base = ["tags", "--category", CATEGORY, "--relevant", str(self.csv),
                "--out", str(out), "--backend", "manual", "--llm-queue", str(queue)]
        self.assertEqual(self._voc(*base).returncode, 3)
        self.assertEqual(self._voc(*base).returncode, 3)
        self.assertEqual(len(list((queue / "pending").glob("*.json"))), 1)

    def test_bad_backend_is_config_error(self):
        r = self._voc("tags", "--category", CATEGORY, "--relevant", str(self.csv),
                      "--out", str(self.tmp / "x.json"), "--backend", "nope")
        self.assertEqual(r.returncode, 2)  # argparse 的 choices 挡住

    def test_llm_status_and_pending(self):
        out = self.tmp / "tagset.json"
        queue = self.tmp / "queue"
        self._voc("tags", "--category", CATEGORY, "--relevant", str(self.csv),
                  "--out", str(out), "--backend", "manual", "--llm-queue", str(queue))
        r = self._voc("llm", "status", "--llm-queue", str(queue))
        self.assertEqual(r.returncode, 0)
        self.assertIn("待回填 1 条", r.stdout)
        r = self._voc("llm", "pending", "--llm-queue", str(queue))
        self.assertEqual(r.returncode, 0)
        self.assertIn("json_mode=True", r.stdout)
        r = self._voc("llm", "backends")
        self.assertEqual(r.returncode, 0)
        self.assertIn("manual", r.stdout)
        r = self._voc("llm", "config")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["resolved_backend"], "manual")

    def test_llm_show_exposes_full_prompt(self):
        out = self.tmp / "tagset.json"
        queue = self.tmp / "queue"
        self._voc("tags", "--category", CATEGORY, "--relevant", str(self.csv),
                  "--out", str(out), "--backend", "manual", "--llm-queue", str(queue))
        tid = sorted((queue / "pending").glob("*.json"))[0].stem
        r = self._voc("llm", "show", tid, "--part", "all", "--llm-queue", str(queue))
        self.assertEqual(r.returncode, 0)
        self.assertIn("SYSTEM", r.stdout)
        self.assertIn("USER", r.stdout)
        self.assertIn("Nursery Glider Recliner", r.stdout)

    def test_import_reports_unmatched_ids_with_exit1(self):
        out = self.tmp / "tagset.json"
        queue = self.tmp / "queue"
        self._voc("tags", "--category", CATEGORY, "--relevant", str(self.csv),
                  "--out", str(out), "--backend", "manual", "--llm-queue", str(queue))
        tid = sorted((queue / "pending").glob("*.json"))[0].stem
        f = self.tmp / "results.json"
        f.write_text(json.dumps({tid: CANNED_TEXT, "0000000000000000": "ghost"},
                                ensure_ascii=False), encoding="utf-8")
        r = self._voc("llm", "import", "--file", str(f), "--llm-queue", str(queue))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("找不到", r.stdout)

    def test_tags_mock_flag_still_bypasses_backends(self):
        """``--mock``（内核自带示例标签集）与 ``--backend mock`` 是两回事，别混淆。"""
        out = self.tmp / "tagset.json"
        r = self._voc("tags", "--category", CATEGORY, "--relevant", str(self.csv),
                      "--out", str(out), "--mock")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(out.exists())
        self.assertNotIn("[llm]", r.stdout)

    def test_prompts_check_passes(self):
        r = self._voc("prompts", "--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_unavailable_backend_is_config_error_not_crash(self):
        """配了 openai 但地址连不上 → 退出码 2（输入/配置错误），不是崩栈。"""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = s.getsockname()[1]
        r = self._voc("tags", "--category", CATEGORY, "--relevant", str(self.csv),
                      "--out", str(self.tmp / "x.json"),
                      "--backend", "openai", "--base-url", f"http://127.0.0.1:{dead}/v1",
                      "--api-key", "sk-test")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("不可用", r.stdout)
        self.assertNotIn("Traceback", r.stderr)


# ==========================================================================
# D. 提示词外置的零回归（Stage 3 式保证）
# ==========================================================================

class TestPromptZeroRegression(unittest.TestCase):
    """提示词从 Python 常量搬到 ``prompts/*.md``，渲染结果必须**逐字节一致**。

    为什么必须逐字节：提示词改一个字的后果是**模型输出悄悄变了** ——
    没有任何业务断言会失败，看板照样生成，只是数字不对了。
    所以改造前就把渲染成品冻成基线（``fixtures/prompts_golden/rendered``），
    这里逐字节比对。
    """

    CATEGORY = "Nursery Glider Recliner"
    SAMPLES = "[1] 标题: Best glider ever\n正文: I bought this glider and it is comfortable"
    COMMENT_BLOCK_RENDERED = (
        "\n\n## 各帖的评论片段（共 2 条，覆盖 1 个帖子）\n"
        "**注意：以下内容全部是「评论片段，非发帖人自述」**—（fixture 截断）\n\n"
        "- c1: I love the swivel\n- c2: mine squeaks"
    )
    POSTS = [
        {"reddit_id": "p1", "title": "Best glider ever",
         "summary": "I bought this glider and it is comfortable",
         "mentioned_brands": "Babyletto", "_seq": 0},
        {"reddit_id": "p2", "title": "Looking for a recliner",
         "summary": "Any recommendations for a nursery chair?", "mentioned_brands": "",
         "_seq": 1},
    ]
    FLAT = {
        "nursery_glider": {
            "dim": "features",
            "def": {
                "category": "feature",
                "thoughts": "用户提到摇篮椅的摇摆功能",
                "mentioned": "Output 'yes' or 'no'. If 'no', leave all other fields blank.",
                "original_text": "抽取打标对应的原文片段",
                "nursery_glider_summary": "具体功能点",
                "nursery_glider_reason": ["省力", "哄睡快"],
                "sentiment": "positive / neutral / negative",
            },
        },
        "budget_shopper": {
            "dim": "user_personas_non_purchased",
            "def": {
                "category": "user",
                "thoughts": "仅当发言者尚未购买时才标 yes",
                "mentioned": "Output 'yes' or 'no'. If 'no', leave all other fields blank.",
                "original_text": "抽取打标对应的原文片段",
                "budget_shopper_summary": "预算区间与比较维度",
                "sentiment": "n/a",
            },
        },
    }

    def _rendered(self) -> dict:
        import annotate_posts as ap
        system, user = dt.build_design_prompts(
            FRAME, category=self.CATEGORY, nsample=len(self.POSTS),
            samples=self.SAMPLES, comment_block=self.COMMENT_BLOCK_RENDERED,
            unit="帖子", secondary="评论", secondary_speaker="评论者", lang="zh")
        return {
            "design_tags.system.zh.txt": system,
            "design_tags.user.zh.txt": user,
            "annotate.system.zh.txt": ap.build_annotate_system(
                FRAME, unit="帖子", secondary="评论", secondary_speaker="评论者", lang="zh"),
            "annotate.user.zh.txt": ap.build_user_prompt(self.POSTS[0], self.FLAT),
            "annotate.batch.user.zh.txt": ap.build_batch_user_prompt(self.POSTS, self.FLAT),
        }

    def test_rendered_prompts_match_frozen_baseline(self):
        for name, text in self._rendered().items():
            with self.subTest(prompt=name):
                want = (GOLD / "rendered" / name).read_text(encoding="utf-8")
                self.assertEqual(text, want,
                                 f"{name} 与改造前基线不一致 —— 模型输出会悄悄变化")

    def test_templates_match_frozen_baseline(self):
        """模板原文也要比：渲染一致但模板被「整理」过，下一次改动就会踩空。

        基线里的 ``templates/`` 存的是**转换后**的文本（即 ``${var}`` 形态，
        与 ``prompts/`` 里的一致）—— ``tools/_freeze_prompts_baseline.py`` 冻结
        原文时一并做了 ``{var}`` → ``${var}`` 转换，并用
        ``Template(tpl).substitute(args) == 原 .format() 结果`` 自检过，
        所以那边可以直接逐字节比。

        **Stage 5 的分工**：``design_tags.system`` / ``design_tags.user`` /
        ``annotate.system`` 三个模板被**有意改过**（「分析什么」那部分搬进了
        ``frames/*.yaml``，模板里换成 ``${dim_block}`` 这类占位符）。对它们，
        不变的是**渲染结果**（上一条用例守着），模板本身不再与老基线逐字节比，
        改比「占位符集合」——它不是随便变的，变了就说明又有人往模板里塞内容。
        ``design_tags.comment_block`` 没动，继续逐字节比。
        """
        unchanged = {"design_tags.comment_block.zh.txt": ["ncomments", "nposts", "comments"]}
        for fname, names in unchanged.items():
            with self.subTest(template=fname):
                want = (GOLD / "templates" / fname).read_text(encoding="utf-8")
                got = P.load_template(fname[:-len(".zh.txt")])
                self.assertEqual(got.rstrip("\n"), want.rstrip("\n"))
                for n in names:
                    self.assertIn("${" + n + "}", got)

        # 框架化之后，这三个模板的占位符集合就是「框架往提示词里注入什么」的契约：
        #   * system —— 整段由框架渲染（维度说明 / 体验说明 / category 图例 / 画像要求 / JSON 形状）
        #   * user   —— 分析目的 / 维度块 / 轴说明 / 小结行由框架渲染
        #   * annotate.system —— 规则 6（分段轴）与规则 7（画像桶）
        expected = {
            "design_tags.system": {"experience_note", "category_note",
                                   "persona_requirements", "json_shape"},
            "design_tags.user": {"category", "nsample", "samples", "comment_block",
                                 "purpose_line", "dim_block", "axis_note", "summary_line"},
            "annotate.system": {"annotate_axis_rule", "annotate_persona_rule"},
        }
        for stem, names in expected.items():
            with self.subTest(template=stem, check="placeholders"):
                self.assertEqual(P.template_vars(stem), names)

    def test_placeholder_counts_and_declaration_agree(self):
        """模板里的 ``${`` 必须**全部**是合法占位符（没有裸 ``$``、没有 ``{${``）。

        这条是「没有漏改的 ``.format()`` 残留」的可执行代理：若某处仍是
        ``{category}`` 的旧写法，它既不会被 ``setdollar`` 认成占位符，
        渲染时也不会被替换 —— 于是渲染结果与基线不符（上一条用例抓它）。
        """
        for name, lang in P.available():
            with self.subTest(prompt=name):
                text = P.load_template(name, lang)
                found = sorted(set(
                    re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text)))
                self.assertEqual(found, sorted(P.template_vars(name, lang)))
                self.assertNotIn("{${", text)          # 替换写坏
                self.assertEqual(P.check_all(lang), [])

    def test_rendered_outputs_have_no_placeholders_left(self):
        """渲染成品里不该还有 ``${``：有就说明有变量没被代入。"""
        for name, text in self._rendered().items():
            with self.subTest(prompt=name):
                self.assertNotIn("${", text)


class TestPromptsModule(unittest.TestCase):

    def test_all_templates_are_parseable(self):
        self.assertEqual(P.check_all("zh"), [])

    def test_expected_templates_exist(self):
        names = {n for n, _ in P.available()}
        for required in ("design_tags.system", "design_tags.user", "design_tags.comment_block",
                         "annotate.system", "annotate.user", "annotate.dim_block",
                         "annotate.tag_block", "annotate.tag_reason",
                         "annotate.batch.user", "annotate.batch_post_block"):
            self.assertIn(required, names)

    def test_render_reports_both_missing_and_extra_vars(self):
        with self.assertRaises(P.PromptVars) as ctx:
            P.render("annotate.dim_block", dim="x", tag_blocks="y", bogus="z")
        msg = str(ctx.exception)
        self.assertIn("bogus", msg)
        with self.assertRaises(P.PromptVars) as ctx2:
            P.render("annotate.dim_block", dim="x")
        self.assertIn("tag_blocks", str(ctx2.exception))

    def test_missing_template_lists_what_exists(self):
        with self.assertRaises(P.MissingPrompt) as ctx:
            P.load_template("no.such.template")
        self.assertIn("design_tags.system", str(ctx.exception))

    def test_illegal_names_rejected(self):
        for bad in ("../etc/passwd", "a b", "a/b", ""):
            with self.subTest(name=bad):
                with self.assertRaises(P.PromptError):
                    P.prompt_path(bad)
        with self.assertRaises(P.PromptError):
            P.prompt_path("annotate.user", "zh;rm -rf /")

    def test_lang_falls_back_to_default(self):
        """只有 zh 随仓库发布；请求不存在的语言应回落到 zh 而不是报错。"""
        self.assertEqual(P.load_template("annotate.system", "fr"),
                         P.load_template("annotate.system", "zh"))

    def test_trailing_newline_stripped_only(self):
        """只剥结尾换行 —— comment_block 的**开头**两个空行是有意义的。"""
        t = P.load_template("design_tags.comment_block")
        self.assertTrue(t.startswith("\n\n"), "comment_block 开头空行被吃掉了")
        self.assertFalse(t.endswith("\n"))

    def test_comment_block_render_keeps_leading_blank_lines(self):
        got = P.render("design_tags.comment_block", ncomments=0, nposts=0, comments="")
        self.assertTrue(got.startswith("\n\n"))

    def test_template_vars_matches_declaration(self):
        # Stage 5：design_tags.system 的正文由框架渲染，因此有了这 4 个占位符
        # （改造前它们是内嵌在 Python 里的整段文字）。
        self.assertEqual(P.template_vars("design_tags.system"),
                         {"experience_note", "category_note",
                          "persona_requirements", "json_shape"})
        self.assertEqual(P.template_vars("annotate.tag_block"),
                         {"tag_key", "category", "thoughts", "mentioned", "original_text",
                          "reason_block", "summary_value", "sentiment"})

    def test_annotate_system_braces_are_literal(self):
        """``{summary}`` / ``{reason}`` 是**提示词里的字面量**，不是占位符。

        这正是改用 ``${var}`` 的原因：用 ``.format()`` 就得把每一处改成 ``{{ }}``，
        漏一个就静默改坏了提示词。（Stage 5 起规则 6/7 由框架渲染，
        所以渲染时必须给 ``annotate_axis_rule`` / ``annotate_persona_rule``；
        这里塞进同样带花括号的值，验证它们不会被当成占位符。）
        """
        text = P.load_template("annotate.system")
        self.assertIn("{summary}", text)
        rendered = P.render("annotate.system", annotate_axis_rule="{summary}",
                            annotate_persona_rule="{reason}")
        self.assertIn("多个 original_text / {summary} 用 || 分隔", rendered)
        self.assertIn("{reason}", rendered)


# ==========================================================================
# E. kernel_common 的 CLI 装配
# ==========================================================================

class TestKernelCommonLLMPlumbing(unittest.TestCase):

    def tearDown(self):
        llm.set_default_config(None)

    def _args(self, **kw):
        import argparse
        from voc_core.kernel_common import add_llm_arguments
        ap = argparse.ArgumentParser()
        add_llm_arguments(ap, default_temperature=0.3)
        return ap.parse_args([]), ap

    def test_temperature_default_is_none_not_a_number(self):
        """默认必须是 None：否则 argparse 的默认值会遮蔽配置文件里的温度。"""
        args, ap = self._args()
        self.assertIsNone(args.temperature)
        self.assertIn("--temperature", ap.format_help())

    def test_task_default_temperature_applies_when_nothing_else_gave_one(self):
        from voc_core.kernel_common import configure_llm
        args, _ = self._args()
        cfg = configure_llm(args, default_temperature=0.0)     # 标注任务
        self.assertEqual(cfg.temperature, 0.0)
        self.assertEqual(cfg.sources["temperature"], "任务默认")
        cfg2 = configure_llm(args, default_temperature=0.3)    # 标签设计任务
        self.assertEqual(cfg2.temperature, 0.3)

    def test_explicit_cli_temperature_beats_task_default(self):
        from voc_core.kernel_common import configure_llm
        args, _ = self._args()
        args.temperature = 0.9
        args.backend = "mock"
        cfg = configure_llm(args, default_temperature=0.0)
        self.assertEqual(cfg.temperature, 0.9)
        self.assertEqual(cfg.sources["temperature"], "命令行")

    def test_config_file_temperature_survives_task_default(self):
        from voc_core.kernel_common import configure_llm
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "voc.llm.json"
            f.write_text(json.dumps({"temperature": 0.55}), encoding="utf-8")
            args, _ = self._args()
            args.llm_config = str(f)
            cfg = configure_llm(args, default_temperature=0.0)
            self.assertEqual(cfg.temperature, 0.55)
            self.assertIn("配置文件", cfg.sources["temperature"])

    def test_configure_llm_installs_the_backend(self):
        from voc_core.kernel_common import configure_llm
        args, _ = self._args()
        args.backend = "mock"
        cfg = configure_llm(args, default_temperature=0.0)
        self.assertEqual(cfg.resolved_backend(), "mock")
        self.assertEqual(llm.resolved_name(llm.get_backend()), "mock")

    def test_threads_and_batch_size_default_to_none_for_config_to_win(self):
        from voc_core.kernel_common import configure_llm
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--threads", type=int, default=None)
        ap.add_argument("--batch-size", dest="batch_size", type=int, default=None)
        args = ap.parse_args([])
        cfg = configure_llm(args, default_temperature=0.0)
        self.assertEqual(cfg.threads, 20)       # 落到配置默认
        self.assertEqual(cfg.batch_size, 8)

    def test_backend_note_mentions_source(self):
        from voc_core.kernel_common import configure_llm, llm_backend_note
        args, _ = self._args()
        args.backend = "manual"
        cfg = configure_llm(args, default_temperature=0.3)
        note = llm_backend_note(cfg)
        self.assertIn("后端=manual", note)
        self.assertIn("温度=0.3", note)

    def test_waiting_note_teaches_the_three_steps(self):
        from voc_core.kernel_common import llm_waiting_note
        exc = llm.ManualPending("x", pending_count=2, task_ids=["a", "b"])
        note = llm_waiting_note(exc)
        self.assertIn("不是失败", note)
        self.assertIn("llm pending", note)
        self.assertIn("llm show", note)
        self.assertIn("llm fill", note)

    def test_import_llm_connector_prefers_platform_over_installed_skill(self):
        """查找顺序必须是「内存 → VOC_LLM_CONNECTOR_DIR → 平台自带」。

        踩过的坑：本机装了 ``LLM连接器`` skill，旧的查找顺序把它的
        ``scripts`` 目录放在平台实现**之前**，于是内核 import 到旧模块，
        Stage 4 的多后端／人工兜底／录制重放全部被静默旁路 —— 链路照常工作，
        完全看不出问题。
        """
        from voc_core.kernel_common import import_llm_connector
        mod = import_llm_connector()
        for attr in ("call_llm", "ManualPending", "LocalFallback",
                     "EXIT_PENDING", "EXIT_CONFIG", "is_external_backend"):
            self.assertTrue(hasattr(mod, attr), f"连接器缺少 {attr}")
        self.assertEqual(mod.EXIT_PENDING, 3)
        self.assertTrue(mod.ManualPending is llm.ManualPending)

    def test_explicit_connector_dir_is_respected_and_never_silently_ignored(self):
        from voc_core.kernel_common import import_llm_connector
        old = os.environ.get("VOC_LLM_CONNECTOR_DIR")
        try:
            os.environ["VOC_LLM_CONNECTOR_DIR"] = str(ROOT / "no-such-dir")
            with self.assertRaises(ImportError):
                import_llm_connector()
        finally:
            if old is None:
                os.environ.pop("VOC_LLM_CONNECTOR_DIR", None)
            else:
                os.environ["VOC_LLM_CONNECTOR_DIR"] = old


# ==========================================================================
# F. annotate 侧的兜底行为：不能把队列灌爆
# ==========================================================================

class TestAnnotatePendingDoesNotFloodQueue(unittest.TestCase):
    """``ManualPending`` 在批量/并发路径里必须**向上冒泡**，不能逐条重试。

    踩过的坑：批量标注里若把 ``ManualPending`` 当成可重试错误，20 个线程会各自
    重试，把同一批任务重复灌进队列（N 倍膨胀），而且每条都白等退避时间。
    """

    def test_chat_with_retry_does_not_retry_pending(self):
        import annotate_posts as ap

        calls = {"n": 0}

        def chat(_system, _user, **_kw):
            calls["n"] += 1
            raise llm.ManualPending("pending")

        with self.assertRaises(llm.ManualPending):
            ap.chat_with_retry(chat, "s", "u")
        self.assertEqual(calls["n"], 1, "ManualPending 被重试了 —— 会重复灌任务")

    def test_chat_with_retry_does_retry_transient_failure(self):
        import annotate_posts as ap

        calls = {"n": 0}

        def chat(_system, _user, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise llm.LLMUnavailable("transient")
            return CANNED_TEXT, {}

        out = ap.chat_with_retry(chat, "s", "u")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(out[0], CANNED_TEXT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
