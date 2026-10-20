# -*- coding: utf-8 -*-
"""LLM 客户端（OpenAI 兼容的 /chat/completions）。

配置来源优先级：**环境变量 > 项目根目录 llm_config.json**。

    LLM_CONNECTOR_BASE_URL   接口根地址，如 https://api.deepseek.com/v1
    LLM_CONNECTOR_API_KEY    密钥
    LLM_CONNECTOR_MODEL      模型名，如 deepseek-chat

说明：
- 只实现最小必要能力（JSON 模式、重试、超时），不引入第三方 SDK。
- 错误一律抛 `LLMError`，并把网关返回的原始 message 带出来 —— 这样
  「余额不足」这类问题能直接显示给用户，而不是变成一句笼统的调用失败。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .paths import config_path

# 配置随数据走：源码运行读项目根，打包后读 VOCData/，两种情况都能找到
CONFIG_PATH = config_path()

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}


class LLMError(RuntimeError):
    """调用失败（网络 / 鉴权 / 余额 / 协议错误）。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class LLMNotConfigured(LLMError):
    """未配置 LLM。"""


def _file_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def load_config() -> dict:
    """环境变量优先，其次 llm_config.json。"""
    fc = _file_config()

    def pick(env_name: str, key: str, default=""):
        v = (os.getenv(env_name) or "").strip()
        if v:
            return v
        v = str(fc.get(key, "") or "").strip()
        return v or default

    def pick_int(env_name: str, key: str, default: int) -> int:
        raw = pick(env_name, key, "")
        try:
            return int(raw) if raw else default
        except ValueError:
            return default

    jm = pick("LLM_CONNECTOR_JSON_MODE", "json_mode", "")
    return {
        "base_url": pick("LLM_CONNECTOR_BASE_URL", "base_url").rstrip("/"),
        "api_key": pick("LLM_CONNECTOR_API_KEY", "api_key"),
        "model": pick("LLM_CONNECTOR_MODEL", "model"),
        "timeout": pick_int("LLM_CONNECTOR_TIMEOUT", "timeout", 180),
        "retries": pick_int("LLM_CONNECTOR_RETRIES", "retries", 4),
        "max_tokens": pick_int("LLM_CONNECTOR_MAX_TOKENS", "max_tokens", 4096),
        "json_mode": (jm.lower() not in ("0", "false", "no")) if jm else bool(
            fc.get("json_mode", True)),
    }


def is_configured() -> bool:
    c = load_config()
    return bool(c["base_url"] and c["api_key"] and c["model"])


def endpoint(base_url: str) -> str:
    b = base_url.rstrip("/")
    return b if b.endswith("/chat/completions") else b + "/chat/completions"


def _friendly_error(status: int, raw: str) -> str:
    """把网关错误翻译成人能看懂的原因（保留原始信息）。"""
    try:
        j = json.loads(raw)
        msg = (j.get("error") or {}).get("message") or j.get("message") or raw
    except Exception:
        msg = raw
    msg = str(msg).strip()[:400]
    hint = {
        401: "密钥无效或已过期",
        402: "账户余额不足，请先充值",
        403: "密钥无权访问该模型",
        404: "接口地址或模型名不存在",
        429: "请求过于频繁或额度耗尽",
    }.get(status, "")
    return f"HTTP {status} · {hint}｜{msg}" if hint else f"HTTP {status}｜{msg}"


def chat(system: str, user: str, *, json_mode: bool = True,
         temperature: float = 0.2, max_tokens: int | None = None) -> tuple[str, dict]:
    """调用一次对话补全，返回 (内容, usage)。失败抛 LLMError。"""
    cfg = load_config()
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
        raise LLMNotConfigured("未配置 LLM（缺少 base_url / api_key / model）")

    url = endpoint(cfg["base_url"])
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
    }
    mt = max_tokens or cfg.get("max_tokens")
    if mt:
        payload["max_tokens"] = mt
    use_json = json_mode and cfg.get("json_mode", True)
    if use_json:
        payload["response_format"] = {"type": "json_object"}

    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {cfg['api_key']}"}
    extra = (os.getenv("LLM_CONNECTOR_EXTRA_HEADERS") or "").strip()
    if extra:
        for kv in extra.split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                headers[k.strip()] = v.strip()

    last_err: Exception | None = None
    attempts = max(1, cfg["retries"])
    for i in range(attempts):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
                body = json.loads(r.read().decode("utf-8"))
            try:
                content = body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise LLMError(f"响应结构异常：{json.dumps(body, ensure_ascii=False)[:300]}")
            return content or "", body.get("usage") or {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            # 网关不支持 response_format 时，去掉该字段重试一次
            if e.code == 400 and "response_format" in payload:
                payload.pop("response_format", None)
                last_err = LLMError(_friendly_error(e.code, raw), e.code)
                continue
            err = LLMError(_friendly_error(e.code, raw), e.code)
            if e.code in RETRYABLE_STATUS and i < attempts - 1:
                last_err = err
                time.sleep(min(2 ** i, 12))
                continue
            raise err
        except urllib.error.URLError as e:
            last_err = LLMError(f"网络错误：{getattr(e, 'reason', e)}")
            if i < attempts - 1:
                time.sleep(min(2 ** i, 12))
                continue
            raise last_err
        except TimeoutError:
            last_err = LLMError(f"请求超时（{cfg['timeout']}s）")
            if i < attempts - 1:
                continue
            raise last_err
    raise last_err or LLMError("调用失败")


def preflight() -> dict:
    """连通性与可用性自检。返回 {ok, message, model, latency_ms}。"""
    cfg = load_config()
    if not (cfg["base_url"] and cfg["api_key"] and cfg["model"]):
        return {"ok": False, "configured": False,
                "message": "未配置 LLM（缺少 base_url / api_key / model）",
                "model": "", "base_url": ""}
    t0 = time.time()
    try:
        chat("You are a health check. Reply with JSON only.",
             '返回 {"ok": true}', json_mode=True, max_tokens=16)
        r = {"ok": True, "configured": True,
             "message": "连接正常", "model": cfg["model"],
             "base_url": cfg["base_url"],
             "latency_ms": int((time.time() - t0) * 1000)}
    except LLMError as e:
        r = {"ok": False, "configured": True, "message": str(e),
             "model": cfg["model"], "base_url": cfg["base_url"],
             "latency_ms": int((time.time() - t0) * 1000)}
    _HEALTH["ts"] = time.time()
    _HEALTH["result"] = r
    return r


# 自检结果短期缓存：避免后端不可用时（如余额耗尽）每次分析都白跑一次请求
_HEALTH: dict = {"ts": 0.0, "result": None}


def health(max_age_sec: int = 300, force: bool = False) -> dict:
    """带缓存的自检。默认 5 分钟内复用上次结果。"""
    if (not force and _HEALTH["result"] is not None
            and (time.time() - _HEALTH["ts"]) < max_age_sec):
        return _HEALTH["result"]
    return preflight()


def public_config() -> dict:
    """可安全展示给前端的配置摘要（密钥打码）。"""
    cfg = load_config()
    key = cfg["api_key"]
    masked = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("已设置" if key else "")
    return {
        "configured": is_configured(),
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "api_key_masked": masked,
        "source": "env" if (os.getenv("LLM_CONNECTOR_API_KEY") or "").strip() else "file",
    }


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        r = preflight()
        print(json.dumps(r, ensure_ascii=False, indent=2))
        raise SystemExit(0 if r["ok"] else 1)
    print(json.dumps(public_config(), ensure_ascii=False, indent=2))
