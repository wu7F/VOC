# -*- coding: utf-8 -*-
"""VOC洞察平台 · 账号管理（命令行）

    python manage_accounts.py list
    python manage_accounts.py add <邮箱> [密码]
    python manage_accounts.py passwd <邮箱> <新密码>
    python manage_accounts.py remove <邮箱>

密码不写在命令行里时，会交互式询问（输入不回显）。
口令以 PBKDF2-HMAC-SHA256 + 随机盐存储，**文件里没有明文**。

账号不区分角色：所有账号权限相同。增删账号只能在本机操作，
不开放 HTTP 接口 —— 这是"不分管理员"前提下唯一可控的边界。
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.accounts import AccountError, AccountStore  # noqa: E402
from server.paths import data_root  # noqa: E402


def store() -> AccountStore:
    return AccountStore(data_root() / "accounts.json")


def ask(prompt: str) -> str:
    return getpass.getpass(prompt).strip()


def cmd_list() -> int:
    s = store()
    accs = s.list()
    if not accs:
        print("还没有任何账号。")
        print(f"  添加一个： python {Path(__file__).name} add you@example.com")
        print(f"  账号文件位置： {s.path}")
        return 0
    print(f"账号文件：{s.path}")
    print(f"共 {len(accs)} 个账号（所有账号权限相同，不区分管理员/游客）：\n")
    print(f"  {'邮箱':<34} {'创建时间':<20} 最近登录")
    print("  " + "-" * 76)
    for a in accs:
        print(f"  {a['email']:<34} {a['created']:<20} {a['last_login'] or '从未'}")
    return 0


def cmd_add(email: str, password: str | None) -> int:
    if not email:
        print("用法： python manage_accounts.py add <邮箱> [密码]")
        return 1
    if not password:
        while True:
            p1 = ask("设置密码（至少 6 位，输入不回显）: ")
            if len(p1) < 6:
                print("  密码太短")
                continue
            if p1 == ask("再输一次: "):
                password = p1
                break
            print("  两次不一致，重来")
    try:
        r = store().create(email, password)
    except AccountError as e:
        print(f"✗ {e}")
        return 1
    print(f"✓ 已创建账号 {r['email']}（{r['created']}）")
    print("  重启平台后生效：双击 start.bat")
    return 0


def cmd_passwd(email: str, password: str | None) -> int:
    if not email:
        print("用法： python manage_accounts.py passwd <邮箱> [新密码]")
        return 1
    if not password:
        while True:
            p1 = ask("新密码（至少 6 位）: ")
            if len(p1) < 6:
                print("  密码太短")
                continue
            if p1 == ask("再输一次: "):
                password = p1
                break
            print("  两次不一致，重来")
    if store().set_password(email, password):
        print(f"✓ 已更新 {email} 的密码")
        return 0
    print(f"✗ 账号不存在：{email}")
    return 1


def cmd_remove(email: str) -> int:
    if not email:
        print("用法： python manage_accounts.py remove <邮箱>")
        return 1
    ans = ask(f"确认删除账号 {email}？(y/N) ")
    if ans.lower() != "y":
        print("已取消")
        return 1
    try:
        if store().remove(email):
            print(f"✓ 已删除 {email}")
            return 0
        print(f"✗ 账号不存在：{email}")
        return 1
    except AccountError as e:
        print(f"✗ {e}")
        return 1


def main() -> int:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if len(sys.argv) < 2:
        print(__doc__)
        print("可用命令： list / add / passwd / remove")
        return 1
    cmd = sys.argv[1].lower()
    args = sys.argv[2:]
    if cmd == "list":
        return cmd_list()
    if cmd == "add":
        return cmd_add(args[0] if args else "", args[1] if len(args) > 1 else None)
    if cmd in ("passwd", "password"):
        return cmd_passwd(args[0] if args else "", args[1] if len(args) > 1 else None)
    if cmd in ("remove", "rm", "delete"):
        return cmd_remove(args[0] if args else "")
    print(__doc__)
    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        raise SystemExit(130)