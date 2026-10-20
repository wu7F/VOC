#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web/auth.py · 认证与多租户隔离（**纯标准库**）。

C1 的定位是「内网/单机自用」，所以**没有**认证 —— 谁都能提交任务、读任意报告。
C2 一旦要给别人用，这是第一件必须补上的事。

三个刻意的取舍
==============

**1. 可选启用，且默认不打破单机用法。**

============================ ==========================================
``VOC_WEB_AUTH``             行为
============================ ==========================================
未设 / 空                    有 ``users.json`` 就启用；没有就**关**（C1 行为）
``1`` / ``true`` / ``yes`` /``on``   强制启用
``0`` / ``false`` / ``no`` / ``off`` **强制关闭**（验收测试用，也方便临时排障）
============================ ==========================================

判据是「有没有用户」而不是某个开关 —— 否则「我明明建了用户，怎么还是免登录」这种
事一定会发生，而且**是往不安全的方向错**。反过来，一旦启用但一个用户都没有，
那就是谁都进不去（fail-closed，见 :meth:`Auth.authenticate` 的注释）。

**2. 口令用 PBKDF2-SHA256，会话用 HMAC 签名 —— 都不引第三方。**
``hashlib.pbkdf2_hmac`` 与 ``hmac`` 都是标准库，强度足够；而 flask-login / passlib /
python-jose 这些库带来的供应链面，在一个「内核必须裸解释器可跑」的项目里不值得。

**3. 密钥落盘，重启不失效。**
``VOC_WEB_SECRET`` 优先；否则自动生成 32 字节写到 ``<state_dir>/secret.key``（0600）。
不落盘的话每次重启所有会话失效 —— 在单机部署里这是纯粹的骚扰。

安全边界（**别高估它**）
========================

- cookie **不加密**，只签名。payload 里只有用户名/角色/过期时间，**不放任何秘密**。
- 没有 CSRF token。靠 ``SameSite=Lax`` 挡住跨站表单提交 —— 对本平台（同源前端 +
  JSON API）够用；要开放到公网应先加 CSRF。
- 没有登录限速 / 锁定。公网部署前必须补（否则可被离线爆破 —— 不过 PBKDF2 的
  20 万次迭代让在线爆破也不划算）。
- **HTTPS 是部署方的事**。明文 HTTP 下 cookie 可被中间人窃取，这是设计之外。

CLI::

    python -m web.auth adduser ethan --role admin     # 口令从 stdin 读（不回显）
    python -m web.auth adduser alice                  # 默认 role=user
    python -m web.auth list
    python -m web.auth passwd ethan
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

#: PBKDF2 迭代次数。2026 年的常规下限；20 万次在本机约 60ms —— 登录不觉得慢，
#: 爆破却很贵。
PBKDF2_ROUNDS = 200_000
#: 会话默认有效期（秒）：7 天
SESSION_TTL_S = 7 * 24 * 3600
#: 口令最短长度
MIN_PASSWORD_LEN = 6

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)

COOKIE_NAME = "voc_session"


class AuthError(Exception):
    """配置 / 用法错误（不是「认证失败」—— 那是返回 ``None``）。"""


# ==========================================================================
# 口令
# ==========================================================================


def hash_password(password: str, *, rounds: int = PBKDF2_ROUNDS,
                  salt: bytes | None = None) -> str:
    """``pbkdf2_sha256$<轮数>$<salt hex>$<hash hex>``。

    自带算法与轮数，将来换参数时旧口令仍能校验（不用逼所有人改密）。
    """
    if not password:
        raise AuthError("口令不能为空")
    salt = salt if salt is not None else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
    return f"pbkdf2_sha256${int(rounds)}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """常数时间校验。任何格式问题一律返回 ``False``（不抛异常 —— 别给探测者信号）。"""
    try:
        algo, rounds_s, salt_hex, hash_hex = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(rounds_s))
    except (ValueError, TypeError, binascii.Error):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


# ==========================================================================
# 用户表
# ==========================================================================


def users_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / "users.json"


def load_users(state_dir: str | Path) -> dict[str, dict[str, Any]]:
    """``{username: {...}}``。文件不存在 / 损坏都返回空表（别拦着服务启动）。"""
    p = users_path(state_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if isinstance(data, dict):        # 允许写成 {name: {...}}
        data = [dict(v, username=k) for k, v in data.items()]
    if not isinstance(data, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for u in data:
        if isinstance(u, dict) and u.get("username"):
            out[str(u["username"])] = u
    return out


def save_users(state_dir: str | Path, users: dict[str, dict[str, Any]]) -> None:
    p = users_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(list(users.values()), ensure_ascii=False, indent=2)
    #: 先写临时文件再换入 —— 这个文件写坏 = 谁都登不进来。
    #: 本机 ``os.replace`` 会瞬时失败（Stage 9 的踩坑），所以带退避重试。
    tmp = p.with_name(p.name + ".part")
    for attempt in range(10):
        try:
            tmp.write_text(blob, encoding="utf-8")
            os.replace(tmp, p)
            if p.read_text(encoding="utf-8") == blob:
                return
        except OSError:
            pass
        time.sleep(0.05 * (attempt + 1))
    raise AuthError(f"用户表写不进去：{p}")


def add_user(state_dir: str | Path, username: str, password: str, *,
             role: str = ROLE_USER) -> dict[str, Any]:
    """建用户。**已存在则报错** —— 绝不静默改口令。"""
    username = (username or "").strip()
    if not username or any(c in username for c in " \t\n/\\:"):
        raise AuthError(f"用户名不合法：{username!r}")
    if len(password or "") < MIN_PASSWORD_LEN:
        raise AuthError(f"口令至少 {MIN_PASSWORD_LEN} 位")
    if role not in ROLES:
        raise AuthError(f"角色只能是 {ROLES}，收到 {role!r}")
    users = load_users(state_dir)
    #: 复名必须**报错**，不能顺手覆盖。理由：这条路走下去就是「第二次 add-user
    #: 悄无声息地换了别人的口令」，而对方要到下次登录失败才发现 —— 往不安全的方向错。
    #: 改口令请走 :func:`set_password`（那个函数要求用户先存在，语义明确）。
    if username in users:
        raise AuthError(
            f"用户已存在：{username}（改口令请用 passwd；"
            "顺手覆盖会让对方在下次登录时才发现口令被换过）")
    users[username] = {
        "username": username,
        "role": role,
        "password_hash": hash_password(password),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    save_users(state_dir, users)
    return users[username]


def set_password(state_dir: str | Path, username: str, password: str) -> None:
    users = load_users(state_dir)
    if username not in users:
        raise AuthError(f"没有这个用户：{username}")
    if len(password or "") < MIN_PASSWORD_LEN:
        raise AuthError(f"口令至少 {MIN_PASSWORD_LEN} 位")
    users[username]["password_hash"] = hash_password(password)
    save_users(state_dir, users)


# ==========================================================================
# 会话
# ==========================================================================


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def _falsy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("0", "false", "no", "off")


class Auth:
    """口令校验 + 签名会话 cookie + 角色判定。

    :param state_dir: ``users.json`` / ``secret.key`` 所在目录
    :param enabled: 显式覆盖启用状态；``None`` 则按 ``VOC_WEB_AUTH`` 与用户表推断
    :param secret: 显式密钥（默认读环境变量或密钥文件）
    """

    def __init__(self, state_dir: str | Path, *,
                 enabled: bool | None = None,
                 secret: bytes | None = None,
                 ttl_s: int = SESSION_TTL_S,
                 cookie_secure: bool | None = None) -> None:
        self.dir = Path(state_dir)
        self.ttl_s = int(ttl_s)
        self.cookie_secure = (_truthy(os.environ.get("VOC_WEB_COOKIE_SECURE"))
                              if cookie_secure is None else bool(cookie_secure))

        users = load_users(self.dir)
        env = os.environ.get("VOC_WEB_AUTH")
        #: 开关是否由调用方/环境变量**显式**定死。显式定死时，事后加多少用户都不改它。
        self._explicit = enabled is not None or _truthy(env) or _falsy(env)
        if enabled is None:
            if _falsy(env):
                enabled = False
            elif _truthy(env):
                enabled = True
            else:
                #: 判据是「有没有用户」—— 「建了用户却还是免登录」是往不安全方向错
                enabled = bool(users)
        self.enabled = bool(enabled)
        self._users = users
        self._users_stamp = self._users_signature()
        self._secret = secret if secret is not None else self._load_or_make_secret()

    # -- 用户表热更新 ------------------------------------------------------
    def _users_signature(self) -> str:
        """用户表的 ``(修改时间, 大小)`` —— 变了才值得重读。"""
        try:
            st = users_path(self.dir).stat()
        except OSError:
            return ""
        return f"{st.st_mtime_ns}:{st.st_size}"

    def maybe_reload(self) -> None:
        """用户表有变动就重读 —— 服务**不用重启**也认得新建的用户。

        为什么必须有这个方法：``Auth`` 是**懒构造**的（第一次被问到时才建）。典型顺序是
        —— 服务先起来（此时一个用户都没有 ⇒ ``enabled=False``，纯本机形态），管理员随后
        用 ``web.auth adduser`` 加了人。若不再读一次，服务会**一直停在免登录**，
        而那个失效方向正好是最坏的一种：**朝着不安全的一侧错**。

        所以这不单是「方便」：它保证了「在命令行加过人之后，认证必然自动开启」这条安全
        性质。每次判定前先看一眼 stat —— 内网服务每秒请求数在个位数，这点开销可以忽略。
        """
        stamp = self._users_signature()
        if stamp == self._users_stamp:
            return
        self._users_stamp = stamp
        users = load_users(self.dir)
        self._users = users
        #: 自动模式下加了第一批用户 ⇒ 认证就此开启（反过来，删空了**不会**自动关）
        if not self._explicit and users:
            self.enabled = True

    # -- 密钥 --------------------------------------------------------------
    def _load_or_make_secret(self) -> bytes:
        env = os.environ.get("VOC_WEB_SECRET")
        if env:
            return env.encode("utf-8")
        p = self.dir / "secret.key"
        if p.exists():
            try:
                raw = p.read_bytes()
                if len(raw) >= 16:
                    return raw
            except OSError:
                pass
        raw = secrets.token_bytes(32)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_bytes(raw)
            with contextlib.suppress(OSError):   #: Windows 上 chmod 基本是空操作
                os.chmod(p, 0o600)
        except OSError:
            pass
        return raw

    def reload(self) -> None:
        """重新读用户表（CLI 建完用户后，不重启服务也能生效）。"""
        self._users = load_users(self.dir)

    @property
    def users(self) -> dict[str, dict[str, Any]]:
        return dict(self._users)

    # -- 会话令牌 ----------------------------------------------------------
    def issue(self, username: str, *, role: str = "") -> str:
        """签发会话令牌：``v1.<payload>.<hmac>``（payload 是 base64 的 JSON，**不加密**）。"""
        u = self._users.get(username) or {}
        payload = {
            "u": username,
            "r": role or u.get("role") or ROLE_USER,
            "exp": int(time.time()) + self.ttl_s,
        }
        body = _b64e(json.dumps(payload, separators=(",", ":"),
                                ensure_ascii=False).encode("utf-8"))
        sig = _b64e(hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest())
        return f"v1.{body}.{sig}"

    def read(self, token: str | None) -> dict[str, Any] | None:
        """校验签名与有效期。**任何一项不过都返回 ``None``。**"""
        if not token:
            return None
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != "v1":
            return None
        _, body, sig = parts
        expect = _b64e(hmac.new(self._secret, body.encode("ascii"),
                                hashlib.sha256).digest())
        #: compare_digest：别用 ==，那会按字节短路，理论上可被计时侧信道摸索签名
        if not hmac.compare_digest(sig, expect):
            return None
        try:
            payload = json.loads(_b64d(body).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return None
        if not isinstance(payload, dict) or not payload.get("u"):
            return None
        if int(payload.get("exp") or 0) < time.time():
            return None
        return {"username": str(payload["u"]),
                "role": str(payload.get("r") or ROLE_USER),
                "exp": int(payload.get("exp") or 0)}

    # -- 登录 --------------------------------------------------------------
    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        """口令校验。**启用但一个用户都没有时，谁都进不去**（fail-closed）。"""
        if not self.enabled:
            return None
        u = self._users.get((username or "").strip())
        if not u:
            #: 仍然跑一次哈希：别让「用户不存在」比「口令错」快一个数量级
            hash_password(password or "x", salt=b"0" * 16, rounds=1000)
            return None
        if not verify_password(password or "", u.get("password_hash") or ""):
            return None
        return {"username": u["username"], "role": u.get("role") or ROLE_USER}

    def login(self, username: str, password: str) -> str | None:
        user = self.authenticate(username, password)
        return self.issue(user["username"], role=user["role"]) if user else None

    # -- 授权 --------------------------------------------------------------
    def can_see(self, user: dict[str, Any] | None, owner: str) -> bool:
        """能否看某个 job。admin 看全部；user 只看自己的。

        ``owner == ""`` 是「命令行直接跑出来的 job」（没经过 Web 提交）——
        **只有 admin 看得到**：普通用户不该因为一次 CLI 运行就瞥见别人的数据。
        """
        if user is None:
            return False
        if user.get("role") == ROLE_ADMIN:
            return True
        return bool(owner) and owner == user.get("username")


# ==========================================================================
# CLI
# ==========================================================================


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="web.auth", description="VOC 平台用户管理")
    ap.add_argument("--state-dir",
                    default=os.environ.get("VOC_WEB_STATE_DIR") or "web_state")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("adduser", help="新建用户（已存在则报错）")
    p_add.add_argument("username")
    p_add.add_argument("--role", default=ROLE_USER, choices=list(ROLES))
    p_add.add_argument("--password", default="", help="不给就从 stdin 读（推荐）")

    p_pw = sub.add_parser("passwd", help="改口令")
    p_pw.add_argument("username")
    p_pw.add_argument("--password", default="")

    sub.add_parser("list", help="列出用户")

    args = ap.parse_args(argv)
    sd = Path(args.state_dir)

    def _read_pw(confirm: bool) -> str:
        pw = getpass.getpass("口令：")
        if confirm and pw != getpass.getpass("再输一次："):
            raise AuthError("两次输入不一致")
        return pw

    try:
        if args.cmd == "adduser":
            if args.username in load_users(sd):
                print(f"用户已存在：{args.username}（用 passwd 改口令）", file=sys.stderr)
                return 1
            pw = args.password or _read_pw(True)
            u = add_user(sd, args.username, pw, role=args.role)
            print(f"已创建 {u['username']}（role={u['role']}）→ {users_path(sd)}")
            print("提示：此后服务将要求登录（VOC_WEB_AUTH=0 可强制关闭）")
        elif args.cmd == "passwd":
            set_password(sd, args.username, args.password or _read_pw(True))
            print(f"已更新 {args.username} 的口令")
        elif args.cmd == "list":
            users = load_users(sd)
            if not users:
                print(f"（没有用户）{users_path(sd)}")
            for u in sorted(users.values(), key=lambda x: x["username"]):
                print(f"  {u['username']:20s} {u.get('role', '?'):6s} {u.get('created_at', '')}")
    except AuthError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
