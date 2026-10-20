#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C2 · 认证与多租户隔离（标准库实现，无第三方依赖）。

为什么不用 NiceGUI / OAuth / JWT 库
===================================

内核刻意保持**零依赖**（``python -S -E voc.py`` 必须能跑）。认证是 Web 层的可选件，
一旦引入第三方 JWT/OAuth 库，``pip install -e ".[web]"`` 就变成装服务而非装粘接剂。
而我们需要的全部能力其实只有三样：口令哈希、签名 cookie、角色判定 ——
``hashlib.pbkdf2_hmac`` / ``hmac.compare_digest`` / ``secrets`` 都是标准库。

安全边界写在这里，是为了让「改坏了会怎样」一眼看得出来：

======================== ===================================================
不用 JWT                 没有第三方依赖；payload 只是 ``{u, r, exp}``，**不加密**，
                         所以**谁都别往 payload 里放敏感东西**
用 ``compare_digest``    不用 ``==``：后者按字节短路，理论上可被计时侧信道摸索签名
用 PBKDF2 + 随机 salt    同一口令两次哈希结果不同 —— 彩虹表与撞库都失效
role + owner 双重隔离     admin 看全部；user 只看自己提交的。**这才叫多租户**
======================== ===================================================

**认证默认关闭**：没建用户就是本机单用户形态，行为与 C1 完全一致。
一旦 ``add-user`` 建过用户，认证自动启用 —— 「建了用户却还免登录」是往不安全方向错。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
if str(PLATFORM) not in sys.path:
    sys.path.insert(0, str(PLATFORM))

from web import auth as A  # noqa: E402


def _mkauth(tmp: str, **kw) -> A.Auth:
    return A.Auth(tmp, secret=b"0123456789abcdef0123456789abcdef", **kw)


# ==========================================================================
# 启用与否
# ==========================================================================


class TestEnabled(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def test_off_when_no_users(self):
        """没用户 = 本机单用户形态，行为必须与 C1 一致（不能突然开始问口令）。"""
        self.assertFalse(_mkauth(self.dir).enabled)

    def test_on_when_users_exist(self):
        A.add_user(self.dir, "ethan", "secret123", role="admin")
        self.assertTrue(_mkauth(self.dir).enabled,
                        "建了用户就必须启用 —— 免登录是往不安全的方向错")

    def test_env_force_off(self):
        A.add_user(self.dir, "ethan", "secret123")
        os.environ["VOC_WEB_AUTH"] = "0"
        try:
            self.assertFalse(_mkauth(self.dir).enabled)
        finally:
            del os.environ["VOC_WEB_AUTH"]

    def test_explicit_override_wins(self):
        os.environ["VOC_WEB_AUTH"] = "0"
        try:
            self.assertTrue(_mkauth(self.dir, enabled=True).enabled)
        finally:
            del os.environ["VOC_WEB_AUTH"]

    def test_authenticate_fails_when_disabled(self):
        """没启用时谁来都认证不过 —— 别让代码走到「绕过检查」的另一条支路上去。"""
        A.add_user(self.dir, "ethan", "secret123")
        a = _mkauth(self.dir, enabled=False)
        self.assertIsNone(a.authenticate("ethan", "secret123"))


# ==========================================================================
# 口令哈希
# ==========================================================================


class TestPassword(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def test_wrong_password_does_not_verify(self):
        h = A.hash_password("secret123")
        self.assertTrue(A.verify_password("secret123", h))
        self.assertFalse(A.verify_password("Secret123", h))
        self.assertFalse(A.verify_password("", h))

    def test_salt_makes_hashes_differ(self):
        """同一口令两次哈希**必须**不一样 —— 否则彩虹表直接可用。"""
        self.assertNotEqual(A.hash_password("secret123"), A.hash_password("secret123"))

    def test_rounds_are_set_in_hash(self):
        h = A.hash_password("secret123")
        self.assertIn("pbkdf2_sha256$", h, "哈希串要自带算法标识，将来换算法才好区分")

    def test_tampered_hash_is_rejected_not_crashed(self):
        self.assertFalse(A.verify_password("x", "garbage"))
        self.assertFalse(A.verify_password("x", ""))
        self.assertFalse(A.verify_password("x", "pbkdf2_sha256$999$zz$zz"))

    def test_short_password_rejected_at_the_boundary(self):
        """长度策略由 ``add_user`` 把关（``hash_password`` 本身不设限 —— 它是原语）。

        放在边界而不是哈希函数里，是因为将来若要以编程方式校验旧哈希，
        不该被「口令太短」这条**业务**策略挡住。
        """
        with self.assertRaises(A.AuthError):
            A.add_user(self.dir, "ethan", "abc")
        with self.assertRaises(A.AuthError):
            A.set_password(self.dir, "ethan", "abc")

    def test_invalid_username_rejected(self):
        for bad in ("", " ", "a b", "a/b", "a\\b", "a:b"):
            with self.assertRaises(A.AuthError):
                A.add_user(self.dir, bad, "secret123")

    def test_unknown_role_rejected(self):
        with self.assertRaises(A.AuthError):
            A.add_user(self.dir, "ethan", "secret123", role="root")


# ==========================================================================
# 用户表落盘
# ==========================================================================


class TestUserStore(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def test_plaintext_password_never_hits_disk(self):
        A.add_user(self.dir, "ethan", "super-secret-pw", role="admin")
        raw = (Path(self.dir) / "users.json").read_text(encoding="utf-8")
        self.assertNotIn("super-secret-pw", raw)
        #: 落盘是一组用户对象（``save_users`` 写 list），不是 {name: {...}}
        data = json.loads(raw)
        row = next(u for u in data if u["username"] == "ethan")
        self.assertNotIn("password", row)
        self.assertIn("password_hash", row)

    def test_duplicate_user_rejected(self):
        """复名必须报错，不许静默覆盖 —— 否则第二次就悄悄换了别人的口令。"""
        A.add_user(self.dir, "ethan", "secret123")
        with self.assertRaises(A.AuthError):
            A.add_user(self.dir, "ethan", "different-pw")
        #: 而且原口令必须还管用（没被顺手改掉）
        a = A.Auth(self.dir)
        self.assertIsNotNone(a.login("ethan", "secret123"))
        self.assertIsNone(a.login("ethan", "different-pw"))

    def test_set_password_changes_only_the_password(self):
        A.add_user(self.dir, "ethan", "secret123", role="admin")
        A.set_password(self.dir, "ethan", "brand-new-pw")
        a = A.Auth(self.dir)
        self.assertIsNone(a.login("ethan", "secret123"))
        self.assertIsNotNone(a.login("ethan", "brand-new-pw"))
        self.assertEqual(a.read(a.login("ethan", "brand-new-pw"))["role"], "admin",
                         "改口令不该顺带把角色重置掉")

    def test_set_password_rejects_unknown_user(self):
        with self.assertRaises(A.AuthError):
            A.set_password(self.dir, "nobody", "secret123")

    def test_reload_picks_up_later_added_users(self):
        """用 CLI 建完用户，服务**不重启**也要认得 —— 不该为了加人停一次服务。"""
        a = _mkauth(self.dir, enabled=False)
        self.assertEqual(a.users, {})
        A.add_user(self.dir, "late", "secret123")
        a.reload()
        self.assertIn("late", a.users)

    def test_corrupt_users_file_does_not_crash(self):
        """用户表坏了要能用同样的方式往下走，而不是把登录页拖崩。"""
        (Path(self.dir) / "users.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(A.load_users(self.dir), {})


# ==========================================================================
# 会话令牌
# ==========================================================================


class TestSession(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        A.add_user(self._td.name, "ethan", "secret123", role="admin")
        A.add_user(self._td.name, "guest", "guest12345", role="user")
        self.a = _mkauth(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _tok(self, user="ethan", pw="secret123"):
        return self.a.login(user, pw)

    def test_token_roundtrip(self):
        t = self._tok()
        u = self.a.read(t)
        self.assertEqual(u["username"], "ethan")
        self.assertEqual(u["role"], "admin")

    def test_tampered_signature_rejected(self):
        """签名尾巴改一个字符就得失效 —— 会话不可伪造，这是全部的意义所在。"""
        t = self._tok()
        self.assertIsNone(self.a.read(t[:-2] + "xy"))

    def test_tampered_payload_rejected(self):
        """把 payload 换成自己的同一个字：普通用户不能靠改写 payload 提到 admin。"""
        import base64
        t = self._tok()
        head, body, sig = t.split(".")
        #: 原样重签不行（没有密钥）；直接换 payload 更不行（签名对不上）
        fake = base64.urlsafe_b64encode(
            json.dumps({"u": "guest", "r": "admin",
                        "exp": int(time.time()) + 9999}).encode()).decode().rstrip("=")
        fake += "=" * (-len(fake) % 4)
        self.assertIsNone(self.a.read(f"{head}.{fake}.{sig}"))

    def test_secret_difference_invalidates(self):
        """换一台机器（换密钥）时，旧 cookie 必须失效。"""
        other = A.Auth(self._td.name, secret=b"f" * 32, enabled=True)
        other.reload()
        self.assertIsNone(other.read(self._tok()))

    def test_expired_token_rejected(self):
        old = _mkauth(self._td.name, ttl_s=-10)
        t = old.login("ethan", "secret123")
        self.assertIsNone(old.read(t), "过期令牌必须失效")

    def test_malformed_tokens_return_none(self):
        for bad in ("", "x", "v1.abc", "v2.a.b", None, "v1..", "v1.a.b.c"):
            self.assertIsNone(self.a.read(bad))

    def test_bad_credentials_return_none(self):
        self.assertIsNone(self.a.login("ethan", "wrong-pw"))
        self.assertIsNone(self.a.login("nobody", "secret123"))


# ==========================================================================
# 授权隔离（多租户的实质）
# ==========================================================================


class TestAuthorization(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        A.add_user(self._td.name, "ethan", "secret123", role="admin")
        A.add_user(self._td.name, "guest", "guest12345", role="user")
        self.a = _mkauth(self._td.name)
        self.admin = self.a.read(self.a.login("ethan", "secret123"))
        self.user = self.a.read(self.a.login("guest", "guest12345"))

    def tearDown(self):
        self._td.cleanup()

    def test_user_sees_only_own_jobs(self):
        self.assertTrue(self.a.can_see(self.user, "guest"))
        self.assertFalse(self.a.can_see(self.user, "ethan"),
                         "普通用户看到别人的任务 —— 多租户从这里开始漏")

    def test_admin_sees_everything(self):
        for owner in ("guest", "ethan", "somebody-else"):
            self.assertTrue(self.a.can_see(self.admin, owner))

    def test_ownless_jobs_are_visible_only_to_admin(self):
        """命令行直接跑出来的 job（没走 Web 提交，owner 为空）—— **只有 admin 看得到**。

        否则一次 ``voc.py run`` 就会把数据裸露给所有登录用户。
        """
        self.assertFalse(self.a.can_see(self.user, ""))
        self.assertTrue(self.a.can_see(self.admin, ""))

    def test_anonymous_sees_nothing(self):
        self.assertFalse(self.a.can_see(None, "guest"))
        self.assertFalse(self.a.can_see(None, ""))

    def test_role_defaults_to_user(self):
        """没显式给 role 的用户按普通用户算 —— 权限宁小不大。"""
        A.add_user(self._td.name, "newbie", "secret123")
        self.a.reload()
        self.assertEqual(self.a.users["newbie"].get("role"), A.ROLE_USER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
