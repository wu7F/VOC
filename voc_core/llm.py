#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一 LLM 接口 + 可插拔后端（Stage 4）。

改造前的问题
------------
内核只认一种后端：环境变量 ``LLM_CONNECTOR_BASE_URL`` / ``..._API_KEY``。
没配就 ``sys.exit(2)`` 硬失败，唯一的退路是文档里的一句忠告（「由 WorkBuddy
亲自产 JSON 后回填」）—— 那是一条**没有状态、不能重试、只在人脑子里**的流程。

本模块把「谁来出这份 JSON」变成一个**显式的、可切换的、有状态的后端**：

===========  ==============================================================
backend      说明
===========  ==============================================================
``openai``   OpenAI 兼容 HTTP 接口（含 gpt/DeepSeek/Qwen/豆包等一切兼容端点）
``local``    同上，但默认指向本机端口（Ollama / vLLM / llama.cpp server）
``manual``   **人工兜底**：任务落盘成待办，回填后重跑即命中（内容寻址、可重试）
``mock``     确定性假响应，用于跑通链路与验收（不消耗额度）
``replay``   从录制文件重放，用于离线复现与回归
``auto``     有 openai/local 配置就用它，否则落到 ``manual``（**永不硬失败**）
===========  ==============================================================

两条贯穿全文件的设计原则
------------------------
1. **绝不 ``sys.exit``。** 库层只抛异常（:class:`LLMUnavailable` / :class:`ManualPending`），
   由 CLI 决定退出码。库自己杀掉进程，调用方就没法降级、没法重试、没法写台账。
2. **区分「失败」与「还没好」。** ``manual`` 后端抛的 :class:`ManualPending` 不是错误，
   是「等人工回填」——对应退出码 3，重跑同一命令即可继续，已完成的不会重复问。

退出码约定（CLI 层，全项目统一）
--------------------------------
``0`` 成功 ｜ ``1`` 一般失败 ｜ ``2`` 输入/配置错误 ｜ ``3`` **等待人工回填（可重跑）**

零依赖
------
只用标准库 ``urllib``，因此裸解释器（没装任何三方包）也能跑真实 HTTP 后端。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, ClassVar, Iterable, Protocol

__all__ = [
    "LLMError", "LLMUnavailable", "ManualPending", "LLMResponseInvalid",
    "LocalFallback", "LLMResult", "LLMConfig", "Backend",
    "OpenAICompatBackend", "ManualBackend", "MockBackend", "ReplayBackend",
    "RecordingBackend", "build_backend", "get_backend", "set_default_config",
    "call_llm", "is_external_backend", "backend_info", "task_id_for",
    "BACKENDS", "EXIT_PENDING", "EXIT_CONFIG",
]

PLATFORM_ROOT = Path(__file__).resolve().parents[1]

#: 「等人工回填」的退出码 —— 不是失败，重跑即可继续
EXIT_PENDING = 3
#: 输入/配置错误
EXIT_CONFIG = 2

#: 可用的后端名（``auto`` 会解析成其中一种）
BACKENDS: tuple[str, ...] = ("auto", "openai", "local", "manual", "mock", "replay")

#: ``task_id_for`` 的产物形态（sha1 前 16 位十六进制）。回填文件靠它区分
#: 「task_id → 文本」映射 与 「一行一条记录」的 JSONL。
_TASK_ID_RE = re.compile(r"^[0-9a-fA-F]{8,}$")

#: 缺省人工兜底队列目录（相对项目根）
DEFAULT_QUEUE_DIR = PLATFORM_ROOT / "out" / "llm_queue"


# ==========================================================================
# 异常
# ==========================================================================

class LLMError(RuntimeError):
    """LLM 层错误基类。"""


class LLMUnavailable(LLMError):
    """后端不可用：没配置、连不上、或模型拒绝。

    兼容旧名 :data:`LocalFallback` —— 内核里 ``except LocalFallback`` 的分支
    因此无需改动（同一个类对象）。
    """


class ManualPending(LLMError):
    """人工兜底：任务已入队，结果尚未回填。**不是失败，是可重试的等待态。**

    携带 :attr:`pending_count` 与 :attr:`task_ids`，让 CLI 能直接告诉用户
    「还有 N 条待回填，其中前 3 条是 …」。
    """

    def __init__(self, message: str, *, pending_count: int = 0,
                 task_ids: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.pending_count = pending_count
        self.task_ids = list(task_ids)


class LLMResponseInvalid(LLMError):
    """后端返回了内容，但不是合法 JSON（仅在 ``expect_json=True`` 时抛）。"""


#: 旧名别名（内核的 ``except LocalFallback`` 依赖它）
LocalFallback = LLMUnavailable


# ==========================================================================
# 结果与配置
# ==========================================================================

@dataclass
class LLMResult:
    """一次调用的结果。``usage`` 缺字段是常态，不要假定键存在。"""

    text: str
    usage: dict[str, Any] = field(default_factory=dict)
    backend: str = ""
    task_id: str = ""
    #: 命中缓存（``manual`` 已回填 / ``replay`` 命中录制）时为 True
    cached: bool = False
    elapsed: float = 0.0

    def as_tuple(self) -> tuple[str, dict[str, Any]]:
        """兼容旧连接器契约：``(text, usage)``。"""
        return self.text, self.usage


def _as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


#: 默认值为 ``None`` 的 Path 字段（不能靠 current value 推类型）
_PATH_FIELDS: frozenset[str] = frozenset({"queue_dir", "record", "replay"})
#: 默认值为 ``None`` 的 int 字段
_OPTIONAL_INT_FIELDS: frozenset[str] = frozenset({"max_tokens"})


@dataclass
class LLMConfig:
    """一次运行的全部 LLM 参数。

    优先级（高 → 低）::

        显式参数（CLI） > 环境变量 > 配置文件 > 本类的默认值

    环境变量用 ``VOC_LLM_*``；同时兼容历史的 ``LLM_CONNECTOR_*``
    （Stage 3 之前内核只认那三个），因此老部署不用改环境。
    """

    backend: str = "auto"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.3
    max_tokens: int | None = None
    timeout: float = 180.0
    retries: int = 3
    #: 每批合并标注的单元数（annotate 用）
    batch_size: int = 8
    #: 并发线程数（annotate 用）
    threads: int = 20
    #: 提示词语言（``prompts/<任务>.<语言>.md``）
    lang: str = "zh"
    #: 人工兜底队列目录
    queue_dir: Path = DEFAULT_QUEUE_DIR
    #: 调用录制文件（JSONL）；一旦设置，任何后端都会被录下来
    record: Path | None = None
    #: 重放来源（``backend=replay`` 时必填）
    replay: Path | None = None
    #: 其它透传给后端的参数（如 ``top_p``）
    extra: dict[str, Any] = field(default_factory=dict)
    #: 配置来源标注（便于 ``voc llm backends`` 说清"这个值是哪儿来的"）
    sources: dict[str, str] = field(default_factory=dict)

    # ---- 解析 ----------------------------------------------------------
    #
    # 注意 _ENV_MAP / _LEGACY_ENV 必须标 ClassVar：不标就会被 dataclass 当成
    # **字段**，而 dict 是不可哈希的可变默认值 → ``ValueError: mutable default``
    # 直接在 import 期炸掉。（Stage 3 的 OPTIONAL_TERMS 踩过同一个坑。）

    _ENV_MAP: ClassVar[tuple[tuple[str, str], ...]] = (
        ("backend", "BACKEND"), ("model", "MODEL"), ("base_url", "BASE_URL"),
        ("api_key", "API_KEY"), ("temperature", "TEMPERATURE"),
        ("max_tokens", "MAX_TOKENS"), ("timeout", "TIMEOUT"), ("retries", "RETRIES"),
        ("batch_size", "BATCH_SIZE"), ("threads", "THREADS"), ("lang", "LANG"),
        ("queue_dir", "QUEUE_DIR"), ("record", "RECORD"), ("replay", "REPLAY"),
    )
    #: 历史变量名（Stage 3 之前的内核只认这三个）
    _LEGACY_ENV: ClassVar[dict[str, str]] = {
        "base_url": "LLM_CONNECTOR_BASE_URL",
        "api_key": "LLM_CONNECTOR_API_KEY",
        "model": "LLM_CONNECTOR_MODEL",
    }

    @classmethod
    def resolve(cls, *, config_file: str | Path | None = None,
                **overrides: Any) -> "LLMConfig":
        """按优先级装配配置。

        ``overrides`` 里值为 ``None`` 或 ``""`` 的项**视为未提供**（这样 CLI
        可以直接把 ``--model ""`` 的默认值丢进来而不会覆盖掉配置文件）。
        """
        cfg = cls()
        cfg.sources = {}

        # 1) 配置文件
        path = cls._find_config_file(config_file)
        if path is not None:
            cfg._apply_file(path)
            cfg.sources["_file"] = str(path)

        # 2) 环境变量
        cfg._apply_env()

        # 3) 显式覆盖
        for k, v in overrides.items():
            if v is None or v == "":
                continue
            if not hasattr(cfg, k):
                cfg.extra[k] = v
                continue
            cfg._set(k, v)
            cfg.sources[k] = "命令行"

        cfg._normalize()
        return cfg

    @staticmethod
    def _find_config_file(explicit: str | Path | None) -> Path | None:
        if explicit:
            p = Path(explicit)
            if not p.exists():
                raise LLMUnavailable(f"指定的 LLM 配置文件不存在：{p}")
            return p
        env = os.getenv("VOC_LLM_CONFIG", "").strip()
        if env:
            p = Path(env)
            if p.exists():
                return p
        for cand in (Path.cwd() / "voc.llm.json", PLATFORM_ROOT / "voc.llm.json"):
            if cand.exists():
                return cand
        return None

    def _apply_file(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise LLMUnavailable(f"LLM 配置文件不是合法 JSON：{path} — {exc}") from exc
        if not isinstance(data, dict):
            raise LLMUnavailable(f"LLM 配置文件的顶层必须是对象：{path}")
        for k, v in data.items():
            if k == "api_key_env":
                # 支持「文件里只写环境变量名，密钥本身不进文件」
                name = str(v).strip()
                val = os.getenv(name, "").strip()
                if val:
                    self.api_key = val
                    self.sources["api_key"] = f"环境变量 {name}（配置文件的 api_key_env）"
                continue
            if hasattr(self, k):
                self._set(k, v)
                self.sources[k] = f"配置文件 {path.name}"
            else:
                self.extra[k] = v

    def _apply_env(self) -> None:
        for field_name, suffix in self._ENV_MAP:
            for prefix in ("VOC_LLM_", "LLM_"):
                key = prefix + suffix
                val = os.getenv(key, "").strip()
                if val:
                    self._set(field_name, val)
                    self.sources[field_name] = f"环境变量 {key}"
                    break
        for field_name, key in self._LEGACY_ENV.items():
            if field_name in self.sources:
                continue
            val = os.getenv(key, "").strip()
            if val:
                self._set(field_name, val)
                self.sources[field_name] = f"环境变量 {key}（历史名）"

    def _set(self, name: str, value: Any) -> None:
        """按字段的**声明类型**做转换。

        不能只看当前值的类型：``max_tokens`` / ``record`` / ``replay`` 的默认值是
        ``None``，从 ``None`` 推不出目标类型，直接走 str 会把它们变成字符串 ——
        于是 ``cfg.record`` 变成 str，后面 ``Path(cfg.record)`` 侥幸能用，
        ``cfg.max_tokens`` 变成 ``"1024"`` 传给 HTTP 就是类型错误。
        """
        if name in _PATH_FIELDS:
            s = str(value).strip()
            setattr(self, name, Path(s) if s else None)
        elif name in _OPTIONAL_INT_FIELDS:
            s = str(value).strip()
            setattr(self, name, int(s) if s else None)
        else:
            cur = getattr(self, name)
            if isinstance(cur, bool):
                setattr(self, name, _as_bool(value, cur))
            elif isinstance(cur, int):
                setattr(self, name, int(value))
            elif isinstance(cur, float):
                setattr(self, name, float(value))
            elif isinstance(cur, Path):
                setattr(self, name, Path(str(value)))
            else:
                setattr(self, name, str(value))

    def _normalize(self) -> None:
        self.backend = (self.backend or "auto").strip().lower()
        if self.backend not in BACKENDS:
            raise LLMUnavailable(
                f"未知的 LLM 后端 {self.backend!r}。可用：{', '.join(BACKENDS)}")
        self.lang = (self.lang or "zh").strip().lower()
        if self.temperature is None:
            self.temperature = 0.3
        if self.retries < 0:
            self.retries = 0
        if self.threads < 1:
            self.threads = 1
        if self.batch_size < 1:
            self.batch_size = 1
        # base_url 去掉结尾斜杠，拼 /chat/completions 时不会出现 //
        self.base_url = (self.base_url or "").rstrip("/")
        # local 后端没给地址时指向最常见的本机 Ollama 端点
        if self.backend == "local" and not self.base_url:
            self.base_url = "http://127.0.0.1:11434/v1"

    # ---- 视图 ----------------------------------------------------------

    def resolved_backend(self) -> str:
        """把 ``auto`` 解析成具体后端名（**不改自身**，纯函数式查询）。"""
        if self.backend != "auto":
            return self.backend
        if self.base_url and self.api_key:
            return "openai"
        if self.base_url:
            return "local"
        return "manual"

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        d = asdict(self)
        d["queue_dir"] = str(self.queue_dir)
        d["record"] = str(self.record) if self.record else None
        d["replay"] = str(self.replay) if self.replay else None
        d["resolved_backend"] = self.resolved_backend()
        if redact and self.api_key:
            d["api_key"] = "***" if len(self.api_key) <= 8 else self.api_key[:4] + "***"
        return d


# ==========================================================================
# 后端协议
# ==========================================================================

class Backend(Protocol):
    """后端协议。实现 ``name`` / ``complete`` / ``info`` 三件即可插拔。"""

    name: str

    def complete(self, system: str, user: str, *, json_mode: bool = False,
                 temperature: float | None = None, max_tokens: int | None = None,
                 **kwargs: Any) -> LLMResult: ...

    def info(self) -> dict[str, Any]: ...


def task_id_for(system: str, user: str, json_mode: bool = False) -> str:
    """任务的内容寻址 id。

    用 ``sha1(system + user + json_mode)`` 而不是随机 uuid，是为了让
    **同一份提示词在任何时候重跑都命中同一个任务** —— 这正是人工兜底能
    「断点续传」的基础，也让 :class:`RecordingBackend` 的重放天然对齐。

    刻意**不含模型名**：换模型不该让已回填的答案作废（模型名记在任务文件里，
    人能看到差异）。若确实要按模型分队列，把模型名并进 ``system`` 即可。
    """
    h = hashlib.sha1()
    h.update(system.encode("utf-8"))
    h.update(b"\x00")
    h.update(user.encode("utf-8"))
    h.update(b"\x01" if json_mode else b"\x00")
    return h.hexdigest()[:16]


def _preview(text: str, n: int = 120) -> str:
    """单行摘要：换行折成 ⏎，便于在终端里一行展示。"""
    s = re.sub(r"\s+", " ", text).strip()
    return s if len(s) <= n else s[:n] + "…"


# --------------------------------------------------------------------------
# 1) OpenAI 兼容 HTTP 后端（也是 local 的实现）
# --------------------------------------------------------------------------

class OpenAICompatBackend:
    """走 ``POST {base_url}/chat/completions``。

    ``local`` 与 ``openai`` 是**同一段代码**、同一个协议，只是默认地址不同 ——
    不假装它们是两种东西。Ollama / vLLM / llama.cpp / LM Studio 都提供
    OpenAI 兼容端点，所以「本地模型」不需要另一套实现。
    """

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self.name = cfg.resolved_backend()

    def _url(self) -> str:
        base = self.cfg.base_url
        if not base:
            raise LLMUnavailable(
                f"后端 {self.name} 缺少 base_url（OpenAI 兼容端点地址）。")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"

    def _payload(self, system: str, user: str, json_mode: bool,
                 temperature: float | None, max_tokens: int | None) -> dict[str, Any]:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": user})
        payload: dict[str, Any] = {
            "model": self.cfg.model or "gpt-4o-mini",
            "messages": msgs,
            "temperature": self.cfg.temperature if temperature is None else temperature,
        }
        mt = self.cfg.max_tokens if max_tokens is None else max_tokens
        if mt:
            payload["max_tokens"] = int(mt)
        if json_mode:
            # 兼容端点的方言不完全一致：两个键都发，服务端不认识就忽略。
            payload["response_format"] = {"type": "json_object"}
        payload.update(self.cfg.extra.get("payload", {}) or {})
        return payload

    def complete(self, system: str, user: str, *, json_mode: bool = False,
                 temperature: float | None = None, max_tokens: int | None = None,
                 **kwargs: Any) -> LLMResult:
        if not self.cfg.api_key:
            raise LLMUnavailable(
                f"后端 {self.name} 缺少 api_key（本地端点可填任意非空字符串）。")
        payload = self._payload(system, user, json_mode, temperature, max_tokens)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url(), data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.cfg.api_key}"})

        last: Exception | None = None
        attempts = max(1, self.cfg.retries)
        for attempt in range(attempts):
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                return LLMResult(
                    text=self._extract_text(body),
                    usage=body.get("usage") or {},
                    backend=self.name,
                    elapsed=time.time() - t0)
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:400]
                except Exception:  # noqa: BLE001
                    pass
                last = LLMUnavailable(f"HTTP {exc.code} {exc.reason}：{detail}")
                # 4xx（除 429）是请求本身的问题，重试无意义，立即失败
                if 400 <= exc.code < 500 and exc.code != 429:
                    raise last from exc
            except (urllib.error.URLError, OSError) as exc:
                # 必须收到裸 OSError：URLError 收不全传输层故障。实测（Windows）
                # 连到「已被关闭的端口」时，故障发生在 getresponse() 读响应阶段，
                # 抛的是 ConnectionResetError —— 它是 OSError 但不是 URLError。
                # 漏掉它，异常就会以原始栈穿出库层（内核只 except LLMUnavailable），
                # 表现为「退出码 1 + 一屏 traceback」，而正确语义是退出码 2（配置错误）。
                # Stage 4 的契约：**库层只抛 LLMError 家族**。
                last = LLMUnavailable(
                    f"连不上 {self._url()}：{type(exc).__name__}: {exc}")
            except json.JSONDecodeError as exc:
                last = LLMUnavailable(f"调用 {self.name} 失败：{type(exc).__name__}: {exc}")
            if attempt < attempts - 1:
                time.sleep(min(2 ** attempt, 8))
        raise last or LLMUnavailable(f"调用 {self.name} 失败（未知原因）")

    @staticmethod
    def _extract_text(body: dict[str, Any]) -> str:
        """从响应里取文本。兼容端点偶尔直接回 ``{"content": ...}``。"""
        if isinstance(body.get("choices"), list) and body["choices"]:
            ch = body["choices"][0]
            msg = ch.get("message") or {}
            if isinstance(msg.get("content"), str):
                return msg["content"]
            if isinstance(ch.get("text"), str):
                return ch["text"]
        if isinstance(body.get("content"), str):
            return body["content"]
        if isinstance(body.get("response"), str):
            return body["response"]
        raise LLMUnavailable(
            f"无法从响应中取出文本，响应顶层键：{sorted(body)[:10]}")

    def info(self) -> dict[str, Any]:
        return {"backend": self.name, "base_url": self.cfg.base_url,
                "model": self.cfg.model, "api_key_set": bool(self.cfg.api_key)}


# --------------------------------------------------------------------------
# 2) 人工兜底后端（有状态、可重试）
# --------------------------------------------------------------------------

class ManualBackend:
    """把「谁来出这份 JSON」变成磁盘上的待办队列。

    目录结构::

        <queue_dir>/
          pending/<task_id>.json   待回填（含 system / user，可直接照抄去问模型）
          done/<task_id>.json      已回填（含 text，以及是谁、什么时候填的）

    ``complete()`` 的行为：

    * 命中 ``done/`` → 直接返回（``cached=True``）—— 这就是**断点续传**；
    * 未命中 → 幂等写入 ``pending/``，然后抛 :class:`ManualPending`。

    因为 ``task_id`` 是内容寻址，**重跑同一命令不会产生重复任务**，
    已回填的也不会被再问一遍。回填方式见 :func:`ManualBackend.fill` 与
    :func:`ManualBackend.import_results`。
    """

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self.name = "manual"

    # ---- 路径 ----
    @property
    def root(self) -> Path:
        return Path(self.cfg.queue_dir)

    @property
    def pending_dir(self) -> Path:
        return self.root / "pending"

    @property
    def done_dir(self) -> Path:
        return self.root / "done"

    # ---- 协议 ----
    def complete(self, system: str, user: str, *, json_mode: bool = False,
                 temperature: float | None = None, max_tokens: int | None = None,
                 **kwargs: Any) -> LLMResult:
        tid = task_id_for(system, user, json_mode)
        done = self.done_dir / f"{tid}.json"
        if done.exists():
            try:
                rec = json.loads(done.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                rec = {}
            return LLMResult(text=str(rec.get("text", "")), usage=rec.get("usage") or {},
                             backend=self.name, task_id=tid, cached=True)

        pend = self.pending_dir / f"{tid}.json"
        if not pend.exists():
            self.pending_dir.mkdir(parents=True, exist_ok=True)
            pend.write_text(json.dumps({
                "task_id": tid,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "json_mode": bool(json_mode),
                "model": self.cfg.model,
                "system": system,
                "user": user,
            }, ensure_ascii=False, indent=2), encoding="utf-8")

        ids = sorted(p.stem for p in self.pending_dir.glob("*.json"))
        raise ManualPending(
            f"人工兜底：任务 {tid} 已入队（队列共 {len(ids)} 条待回填）。\n"
            f"  查看：voc.py llm show {tid}\n"
            f"  回填：voc.py llm fill {tid} --text-file <模型输出.json>\n"
            f"  回填后重跑同一条命令即可继续（已完成的不会重复问）。",
            pending_count=len(ids), task_ids=ids)

    def info(self) -> dict[str, Any]:
        return {"backend": self.name, "queue_dir": str(self.root),
                "pending": len(self.pending()), "done": len(self.done())}

    # ---- 队列操作（供 CLI 与测试使用）----
    def pending(self) -> list[Path]:
        if not self.pending_dir.is_dir():
            return []
        return sorted(self.pending_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)

    def done(self) -> list[Path]:
        if not self.done_dir.is_dir():
            return []
        return sorted(self.done_dir.glob("*.json"))

    def task(self, tid: str) -> dict[str, Any]:
        """取任务全文（优先 pending，其次 done）。"""
        for d in (self.pending_dir, self.done_dir):
            p = d / f"{tid}.json"
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        raise LLMError(f"队列里没有任务 {tid}（既不在 pending 也不在 done）")

    def fill(self, tid: str, text: str, *, by: str = "human",
             usage: dict[str, Any] | None = None) -> Path:
        """把一条任务标为完成。"""
        pend = self.pending_dir / f"{tid}.json"
        if not pend.exists() and not (self.done_dir / f"{tid}.json").exists():
            raise LLMError(f"没有待回填的任务 {tid}")
        self.done_dir.mkdir(parents=True, exist_ok=True)
        out = self.done_dir / f"{tid}.json"
        out.write_text(json.dumps({
            "task_id": tid, "text": text, "usage": usage or {},
            "filled_at": datetime.now().isoformat(timespec="seconds"),
            "filled_by": by,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        pend.unlink(missing_ok=True)
        return out

    def import_results(self, path: str | Path) -> tuple[int, list[str]]:
        """批量回填。

        接受两种格式（含「一行 JSONL」这种边界情形）：

        * JSON 对象：``{"<task_id>": "<模型输出的文本>", ...}``
        * JSONL：每行 ``{"task_id": "...", "text": "..."}``

        返回 ``(成功条数, 未匹配上的 task_id 列表)`` —— 第二条**必须**回传：
        静默丢弃未知 id 会让人以为「我明明回填了」。
        """
        p = Path(path)
        if not p.exists():
            raise LLMError(f"回填文件不存在：{p}")
        raw = p.read_text(encoding="utf-8").strip()
        items = self._parse_fill_items(raw, p)

        n, unknown = 0, []
        for tid, text in items:
            if (self.pending_dir / f"{tid}.json").exists() or \
               (self.done_dir / f"{tid}.json").exists():
                self.fill(tid, text, by=f"import:{p.name}")
                n += 1
            else:
                unknown.append(tid)
        return n, unknown

    @staticmethod
    def _parse_fill_items(raw: str, src: Path) -> list[tuple[str, str]]:
        """把回填文件解析成 ``[(task_id, text), ...]``。

        判定**先解析再看内容**，不靠「开头是不是 ``{``、有没有换行」这类猜测 ——
        上一版就是这么猜的，结果是：一行的 JSONL（``{"task_id": ..., "text": ...}``）
        被当成映射对象，于是把 ``"task_id"`` / ``"text"`` 这两个**字面键**当成了
        任务 id 去队列里找，一条都匹配不上（静默返回「0 条成功、2 个未知 id」）。
        正确做法：

        * 能解析成对象且带 ``task_id`` → 视作**单条记录**（JSONL 只有一行的情形）
        * 能解析成对象且键全像 task_id → 视作 **task_id → text 映射**
        * 其余 → 按行解析 JSONL（每行一条记录）
        """
        def to_text(v: Any) -> str:
            return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = None

        if isinstance(obj, dict):
            if "task_id" in obj:
                return [(str(obj["task_id"]), to_text(obj.get("text", "")))]
            if all(_TASK_ID_RE.match(str(k)) for k in obj) and obj:
                return [(str(k), to_text(v)) for k, v in obj.items()]

        items: list[tuple[str, str]] = []
        for lineno, line in enumerate(raw.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LLMError(f"{src} 第 {lineno} 行不是合法 JSON：{exc}") from exc
            if not isinstance(rec, dict) or "task_id" not in rec:
                raise LLMError(
                    f"{src} 第 {lineno} 行缺少 task_id。期望每行 "
                    '{"task_id": "...", "text": "..."}（JSONL），'
                    '或整个文件是 {"<task_id>": "<模型输出>"}（JSON 对象）。')
            items.append((str(rec["task_id"]), to_text(rec.get("text", ""))))
        return items


# --------------------------------------------------------------------------
# 3) mock / replay / 录制
# --------------------------------------------------------------------------

def _default_responder(system: str, user: str, **kw: Any) -> str:
    """mock 的缺省响应：**确定性**地把请求回显成一个 JSON。

    刻意不做「假装是模型」的事：它只保证链路能跑通、产物结构完整。
    要更真实的假数据，传自己的 ``responder``。
    """
    return json.dumps({
        "mock": True, "json_mode": bool(kw.get("json_mode")),
        "system_chars": len(system), "user_chars": len(user),
        "echo": _preview(user, 80),
    }, ensure_ascii=False)


class MockBackend:
    """确定性假后端。用于跑通链路、验收、CI（不消耗额度、不依赖网络）。"""

    def __init__(self, cfg: LLMConfig,
                 responder: Callable[..., str] | None = None) -> None:
        self.cfg = cfg
        self.name = "mock"
        self.responder = responder or self.cfg.extra.get("responder") or _default_responder

    def complete(self, system: str, user: str, *, json_mode: bool = False,
                 temperature: float | None = None, max_tokens: int | None = None,
                 **kwargs: Any) -> LLMResult:
        text = self.responder(system, user, json_mode=json_mode, **kwargs)
        return LLMResult(text=text, usage={"mock": True}, backend=self.name,
                         task_id=task_id_for(system, user, json_mode))

    def info(self) -> dict[str, Any]:
        return {"backend": self.name, "responder": getattr(self.responder, "__name__", "custom")}


class ReplayBackend:
    """从录制文件重放。

    录制文件就是 :class:`RecordingBackend` 写出的 JSONL，按 ``task_id`` 索引。
    命中不了就抛 :class:`LLMUnavailable` 并说明是「没录到」——
    不静默返回空串（那会让下游拿到一份"看起来正常"的废产物）。
    """

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self.name = "replay"
        if not cfg.replay:
            raise LLMUnavailable("replay 后端需要指定录制文件（--llm-replay 或配置 replay）")
        self.path = Path(cfg.replay)
        if not self.path.exists():
            raise LLMUnavailable(f"录制文件不存在：{self.path}")
        self._index: dict[str, str] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("task_id"):
                self._index[rec["task_id"]] = str(rec.get("response", ""))

    def complete(self, system: str, user: str, *, json_mode: bool = False,
                 temperature: float | None = None, max_tokens: int | None = None,
                 **kwargs: Any) -> LLMResult:
        tid = task_id_for(system, user, json_mode)
        if tid not in self._index:
            raise LLMUnavailable(
                f"录制文件里没有任务 {tid}（{self.path}，共 {len(self._index)} 条）。"
                "说明这份提示词当时没走过——请用真实后端跑一次并打开 --llm-record。")
        return LLMResult(text=self._index[tid], usage={"replay": True},
                         backend=self.name, task_id=tid, cached=True)

    def info(self) -> dict[str, Any]:
        return {"backend": self.name, "path": str(self.path), "tasks": len(self._index)}


class RecordingBackend:
    """给任意后端套一层录制。**可复现的关键**：录了就能离线重放、能回归比对。"""

    def __init__(self, inner: Backend, path: str | Path) -> None:
        self.inner = inner
        self.name = f"{inner.name}+record"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def complete(self, system: str, user: str, *, json_mode: bool = False,
                 temperature: float | None = None, max_tokens: int | None = None,
                 **kwargs: Any) -> LLMResult:
        res = self.inner.complete(system, user, json_mode=json_mode,
                                  temperature=temperature, max_tokens=max_tokens, **kwargs)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "task_id": res.task_id or task_id_for(system, user, json_mode),
                "at": datetime.now().isoformat(timespec="seconds"),
                "backend": self.inner.name,
                "json_mode": bool(json_mode),
                "temperature": self._effective_temperature(temperature),
                "system": system,
                "user": user,
                "response": res.text,
                "usage": res.usage,
            }, ensure_ascii=False) + "\n")
        return res

    def _effective_temperature(self, t: float | None) -> float | None:
        """录制里记**实际生效**的温度：显式传了就用它，否则取内层配置。"""
        if t is not None:
            return t
        cfg = getattr(self.inner, "cfg", None)
        return getattr(cfg, "temperature", None)

    def info(self) -> dict[str, Any]:
        d = {"backend": self.name, "record": str(self.path)}
        d.update({f"inner.{k}": v for k, v in self.inner.info().items()})
        return d


# ==========================================================================
# 工厂
# ==========================================================================

def build_backend(cfg: LLMConfig, *,
                  responder: Callable[..., str] | None = None) -> Backend:
    """按配置造后端。``auto`` 在此解析成具体后端，并套上录制层（若配了 ``record``）。"""
    name = cfg.resolved_backend()
    if name in ("openai", "local"):
        be: Backend = OpenAICompatBackend(cfg)
    elif name == "manual":
        be = ManualBackend(cfg)
    elif name == "mock":
        be = MockBackend(cfg, responder)
    elif name == "replay":
        be = ReplayBackend(cfg)
    else:  # pragma: no cover - _normalize 已挡住
        raise LLMUnavailable(f"未知后端 {name!r}")
    if cfg.record:
        be = RecordingBackend(be, cfg.record)
    return be


_default_cfg: LLMConfig | None = None
_default_backend: Backend | None = None


def set_default_config(cfg: LLMConfig | None, *,
                       responder: Callable[..., str] | None = None) -> Backend:
    """设置模块级默认配置与后端（内核脚本启动时调用一次）。"""
    global _default_cfg, _default_backend
    _default_cfg = cfg
    _default_backend = build_backend(cfg, responder=responder) if cfg else None
    return _default_backend  # type: ignore[return-value]


def get_backend() -> Backend:
    """取当前后端；没设置过就按环境／配置文件懒加载（``auto``）。"""
    global _default_cfg, _default_backend
    if _default_backend is None:
        _default_cfg = LLMConfig.resolve()
        _default_backend = build_backend(_default_cfg)
    return _default_backend


def current_config() -> LLMConfig:
    if _default_cfg is None:
        get_backend()
    assert _default_cfg is not None
    return _default_cfg


# ==========================================================================
# 旧连接器契约的兼容层
# ==========================================================================

def resolved_name(be: Backend) -> str:
    """后端名，去掉装饰层后缀（``openai+record`` → ``openai``）。

    分支判断必须基于**被装饰的内层**：套了录制层之后 ``be.name`` 是
    ``"openai+record"``，直接比对 ``== "manual"`` 会静默走错分支。
    """
    return getattr(be, "name", "").split("+", 1)[0]


def is_external_backend() -> bool:
    """是否**不是**人工兜底。

    保留这个旧名字是因为内核用它做分支判断（``elif is_external_backend():``）。
    语义已从「配了 base_url 和 api_key」放宽为「选中的后端能自动产出结果」——
    于是 ``mock`` / ``replay`` 也走同一条路径，只有 ``manual`` 会走队列分支。
    """
    return resolved_name(get_backend()) != "manual"


def backend_info() -> dict[str, Any]:
    """诊断信息：`voc.py llm backends` 与错误提示都用它。"""
    try:
        be = get_backend()
    except LLMError as exc:
        return {"backend": "unavailable", "error": str(exc)}
    d = {"backend": be.name}
    try:
        d.update(be.info())
    except Exception as exc:  # noqa: BLE001 - 诊断信息不该自己炸
        d["info_error"] = f"{type(exc).__name__}: {exc}"
    return d


def call_llm(system: str, user: str, json_mode: bool = False,
             temperature: float | None = None, return_usage: bool = False,
             max_tokens: int | None = None, **kwargs: Any):
    """兼容旧连接器签名的调用入口。

    ``return_usage=True`` 时返回 ``(text, usage)``，否则只返回 ``text`` ——
    与 ``LLM连接器`` skill 的官方实现一致，因此内核调用点无需改动。

    ``temperature=None`` 表示「用配置里的温度」。配置默认 0.3，与旧连接器的
    默认值一致，所以省略该参数的既有调用行为不变；而各任务想差异化
    （标注要 0、标签设计要 0.3）只需要各自在 ``--temperature`` 上给默认值。
    """
    res = get_backend().complete(system, user, json_mode=json_mode,
                                 temperature=temperature, max_tokens=max_tokens, **kwargs)
    return res.as_tuple() if return_usage else res.text
