# -*- coding: utf-8 -*-
"""登录会话与权限。

为什么必须有
------------
平台放到公网后，**任何人拿到网址都能删光你的报告**。所以鉴权不是"锦上添花"，
而是暴露到公网的**前置条件**。

设计
----
* **账号即邮箱**，口令哈希校验（PBKDF2，见 `accounts.py`）。
* **不区分管理员与游客** —— 所有已登录账号权限完全相同（新建 / 重跑 / 删除 / 导出）。
  正因为没有"更高级"的角色，账号增删**不开放 HTTP 接口**，只能由本机脚本
  `账号管理.bat` 操作 —— 持有磁盘访问权的人才有资格增删账号。
* 一个账号都没有时：本机 `127.0.0.1` 直接可用，远程一律拒绝写操作。
  保留"自己双击就能用"的体验，同时不把风险带到网上。
* 会话用 **HMAC 签名的 Cookie**，服务端无状态，密钥首次启动随机生成后落盘。

刻意保持简单：没有用户表、没有注册流程、没有数据库依赖 —— 一台机器一个小工具，
不值得引入一整套身份系统。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

COOKIE_NAME = "voc_session"
SESSION_TTL = 7 * 24 * 3600  # 7 天
ROLE_USER = "user"   # 不分角色，统一叫 user


# ---------------------------------------------------------------------------
# 账号存储
# ---------------------------------------------------------------------------

def _account_store():
    from .accounts import AccountStore
    from .paths import data_root
    return AccountStore(data_root() / "accounts.json")


def auth_enabled() -> bool:
    """是否已有可用账号。"""
    try:
        return _account_store().exists()
    except Exception:
        return False


def account_list() -> list[dict]:
    return _account_store().list()


def create_account(email: str, password: str, note: str = "") -> dict:
    return _account_store().create(email, password, note)


def remove_account(email: str) -> bool:
    return _account_store().remove(email)


def set_account_password(email: str, password: str) -> bool:
    return _account_store().set_password(email, password)


def verify_login(email: str, password: str) -> str | None:
    """校验成功返回邮箱，否则 None。"""
    try:
        return _account_store().verify(email, password)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 签名密钥
# ---------------------------------------------------------------------------

_SECRET_CACHE: bytes | None = None


def _secret_path() -> Path:
    from .paths import data_root
    return data_root() / ".session_secret"


def _secret() -> bytes:
    """首次启动随机生成并落盘；之后固定不变（否则重启会让所有人掉线）。"""
    global _SECRET_CACHE
    if _SECRET_CACHE is not None:
        return _SECRET_CACHE
    p = _secret_path()
    try:
        if p.exists():
            data = p.read_bytes().strip()
            if len(data) >= 32:
                _SECRET_CACHE = data
                return _SECRET_CACHE
    except OSError:
        pass
    data = secrets.token_bytes(48)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        try:
            os.chmod(p, 0o600)  # 仅所有者可读
        except OSError:
            pass
    except OSError:
        pass
    _SECRET_CACHE = data
    return data


# ---------------------------------------------------------------------------
# 会话签发 / 校验
# ---------------------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def issue(email: str) -> str:
    """签发一个会话 token：``payload.signature``。"""
    payload = json.dumps({"e": email, "t": int(time.time())}, separators=(",", ":"))
    raw = payload.encode()
    sig = hmac.new(_secret(), raw, hashlib.sha256).digest()[:32]
    return f"{_b64e(raw)}.{_b64e(sig)}"


def read_email(token: str | None) -> str | None:
    """校验 token，返回邮箱；无效或过期返回 None。"""
    if not token or "." not in token:
        return None
    try:
        body, sig = token.rsplit(".", 1)
        raw = _b64d(body)
        expect = hmac.new(_secret(), raw, hashlib.sha256).digest()[:32]
        if not hmac.compare_digest(_b64d(sig), expect):
            return None
        data = json.loads(raw.decode())
    except Exception:
        return None
    if int(time.time()) - int(data.get("t", 0)) > SESSION_TTL:
        return None
    email = data.get("e")
    return email if isinstance(email, str) and email else None


def can_write(email: str | None, *, loopback: bool = False) -> bool:
    """是否有写权限。

    所有账号权限相同，因此"已登录"即"可写"。
    一个账号都没有时：本机放行全部写操作，远程一律拒绝 ——
    这样"还没配账号"不会导致公网裸奔。
    """
    if not auth_enabled():
        return bool(loopback)
    return bool(email)


# ---------------------------------------------------------------------------
# 登录限流（防在线爆破）
# ---------------------------------------------------------------------------

_MAX_ATTEMPTS = 8          # 同一 IP 允许的连续失败次数
_LOCK_SECONDS = 300        # 超限后锁定时长


_attempts: dict[str, list[float]] = {}


def check_rate(client_ip: str) -> tuple[bool, int]:
    """返回 (是否允许尝试, 剩余秒数)。"""
    now = time.time()
    hits = [t for t in _attempts.get(client_ip, []) if now - t < _LOCK_SECONDS]
    _attempts[client_ip] = hits
    if len(hits) >= _MAX_ATTEMPTS:
        return False, int(_LOCK_SECONDS - (now - hits[0]))
    return True, 0


def note_failure(client_ip: str) -> None:
    _attempts.setdefault(client_ip, []).append(time.time())


def clear_failures(client_ip: str) -> None:
    _attempts.pop(client_ip, None)


def remaining_attempts(client_ip: str) -> int:
    """该 IP 距被锁定还差几次失败（已排除过期记录）。"""
    now = time.time()
    hits = [t for t in _attempts.get(client_ip, []) if now - t < _LOCK_SECONDS]
    return max(0, _MAX_ATTEMPTS - len(hits))