# -*- coding: utf-8 -*-
"""账号体系：邮箱 + 密码，不区分角色。

设计要点
--------
* **账号即邮箱**，作为唯一身份标识；登录后界面右上角显示当前账号。
* **不区分管理员与游客** —— 所有已登录账号权限完全相同（可新建 / 重跑 / 删除 / 导出）。
  因为没有"更高级"的角色，账号管理**不开放 HTTP 接口**，只能通过本脚本在机器上操作
  —— 持有磁盘访问权的人才有资格增删账号。
* 密码**永不存明文**：`PBKDF2-HMAC-SHA256` + 每账号独立随机盐 + 20 万次迭代。
  校验时用常量时间比较，避免时序侧信道。
* 存储在数据目录下的 `accounts.json`，可随时备份/迁移（与 runs/ 一起走）。

首次启动若还没有账号文件，会尝试从环境变量 `VOC_PASSWORD` / `VOC_GUEST_PASSWORD`
引导创建两个账号（保证旧部署平滑过渡），之后一律以账号文件为准。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

PBKDF2_ROUNDS = 200_000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


class AccountError(ValueError):
    """账号相关错误（格式非法 / 已存在 / 不存在）。"""


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(normalize_email(email)))


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """返回 (hex_hash, hex_salt)。"""
    if not password or len(password) < 6:
        raise AccountError("口令至少 6 位")
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return dk.hex(), salt.hex()


def verify_password(password: str, hex_hash: str, hex_salt: str) -> bool:
    try:
        salt = bytes.fromhex(hex_salt)
    except ValueError:
        return False
    calc, _ = hash_password(password or "", salt)
    return hmac.compare_digest(calc, hex_hash or "")


class AccountStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict = {"accounts": []}

    # ---------------- 读写 ----------------

    def _load(self) -> None:
        if not self.path.exists():
            self._data = {"accounts": []}
            self._bootstrap_from_env()
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 文件损坏时不要静默丢账号：改名备份后从空开始，让调用方能立刻发现
            try:
                self.path.rename(self.path.with_suffix(".corrupt.json"))
            except OSError:
                pass
            self._data = {"accounts": []}
            return
        raw.setdefault("accounts", [])
        self._data = raw

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)   # 文件里是口令哈希，仍按敏感文件对待
        except OSError:
            pass

    def _bootstrap_from_env(self) -> None:
        """首次启动：从旧的环境变量口令引导出账号，避免升级后直接登不进去。"""
        made = False
        for env_name in ("VOC_PASSWORD", "VOC_GUEST_PASSWORD"):
            pw = os.environ.get(env_name, "").strip()
            if not pw:
                continue
            email = normalize_email(os.environ.get(f"{env_name}_EMAIL", "")) or f"{env_name.lower()}@local"
            try:
                self._data["accounts"].append(self._make_account(email, pw, note="由环境变量引导创建"))
                made = True
            except AccountError:
                continue
        if made:
            self._save()

    def _save_loaded(self) -> None:
        # 兼容旧调用：先确保内存与磁盘同步
        if not self.path.exists():
            self._save()

    # ---------------- 增删查改 ----------------

    @staticmethod
    def _make_account(email: str, password: str, note: str = "") -> dict:
        email = normalize_email(email)
        if not valid_email(email):
            raise AccountError(f"邮箱格式不正确：{email}")
        h, s = hash_password(password)
        return {
            "email": email,
            "password_hash": h,
            "salt": s,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_login": "",
            "note": note,
        }

    def list(self) -> list[dict]:
        self._load()
        return [
            {"email": a["email"], "created": a.get("created", ""),
             "last_login": a.get("last_login", ""), "note": a.get("note", "")}
            for a in self._data["accounts"]
        ]

    def create(self, email: str, password: str, note: str = "") -> dict:
        self._load()
        email = normalize_email(email)
        if any(a["email"] == email for a in self._data["accounts"]):
            raise AccountError(f"账号已存在：{email}")
        acc = self._make_account(email, password, note)
        self._data["accounts"].append(acc)
        self._save()
        return {"email": acc["email"], "created": acc["created"]}

    def remove(self, email: str) -> bool:
        self._load()
        email = normalize_email(email)
        before = len(self._data["accounts"])
        self._data["accounts"] = [a for a in self._data["accounts"] if a["email"] != email]
        if len(self._data["accounts"]) == before:
            return False
        if not self._data["accounts"]:
            raise AccountError("这是最后一个账号，删掉之后任何人都无法登录")
        self._save()
        return True

    def set_password(self, email: str, password: str) -> bool:
        self._load()
        email = normalize_email(email)
        for a in self._data["accounts"]:
            if a["email"] == email:
                h, s = hash_password(password)
                a["password_hash"], a["salt"] = h, s
                self._save()
                return True
        return False

    def verify(self, email: str, password: str) -> str | None:
        """校验成功返回邮箱，否则返回 None。"""
        self._load()
        email = normalize_email(email)
        for a in self._data["accounts"]:
            if a["email"] == email:
                if verify_password(password, a.get("password_hash", ""), a.get("salt", "")):
                    a["last_login"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    try:
                        self._save()
                    except OSError:
                        pass
                    return a["email"]
                return None
        # 邮箱不存在时也走一遍哈希运算，避免"邮箱是否存在"成为可探测的侧信道
        hash_password(password or "x", bytes.fromhex("00" * 16))
        return None

    def exists(self) -> bool:
        self._load()
        return bool(self._data["accounts"])

    def ensure(self) -> None:
        self._load()
        self._save_loaded()