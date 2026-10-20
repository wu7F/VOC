#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C2 · 变异验证（证明那些新判据**真的抓得住**，不是摆设）。

规矩沿用 Stage 6/8：**注入一个退化 → 断言恰好那条判据翻红 → 复原 → 再确认通过**。
只显示「全绿」的测试没有信息量 —— 绿的原因可能是判据压根没被执行到。

一次计的六处变异，每处都对着一条 C2 里**最容易被人改坏**的东西：

===== ========================== ====================================================
 #    变异                        期望翻红的判据（没翻红 = 那条判据是死的）
===== ========================== ====================================================
 M1   ``BEGIN IMMEDIATE``      →   跨进程唯一认领
      → 延迟到 UPDATE 前          （会双认领；错误要跑到第三步才看得出来）
 M2   守卫 ``AND state='queued'`` → 同上（同一 job 被两个 worker 同时跑）
      → 无守卫 UPDATE
 M3   按心跳回收的 ``<=``        →   「回收不误伤正在干活的任务」
      → 回到 C1 的「全量判死」
 M4   坏库隔离的重试             →   「坏库不能让服务起不来」
      → 去掉
 M5   签名校验整段跳过        →   「伪造/篡改的会话 cookie 必须失效」
 M6   ``safe_key`` 放行 ``..``   →   「目录穿越必须被拒绝」
===== ========================== ====================================================

两处**被本验证剔除**的变异（它们的教训值钱，记在这里免得再试一遍）
------------------------------------------------------------------

1. **单独拆 ``claim_next`` 的任一道防线**：只去掉 SELECT 的 ``state='queued'`` 过滤，
   或只去掉 UPDATE 的 ``AND state=?`` 守卫，**都没有任何判据翻红** —— 因为它们
   **互为备份**：一道漏了，另一道照样把重复认领挡住。

   这是**好性质**（认领正确性不是单点依赖），但它说明「单处变异 ⇒ 判据翻红」在这段代码上
   不成立，所以 M2 改成**同时拆两道**（那才会真的双认领，也确实被抓住了）。

2. **``hmac.compare_digest`` → ``==``**：同样翻不了红，而且**永远翻不了** ——
   两者只差**比较耗时**，不差比较结果。计时侧信道**不是功能测试能探测的东西**，
   所以 compare_digest 属于「靠 review 守」的那类，不该指望变异验证。
   现在的 M5 是整段跳过签名校验，那才是有功能后果的退化。

用法::

    python tools/_mutate_c2.py            # 全部跑
    python tools/_mutate_c2.py --only 3   # 只跑第 3 条

**跑之前确保工作区干净**（脚本会把改过的文件复原，中断要用 ``--restore`` 兜底）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
PY = sys.executable

#: (编号, 说明, 目标文件, 原文, 变异文, 期望翻红的测试)
MUTATIONS: list[tuple] = [
    (1, "跨进程认领不再一开始就拿写锁：BEGIN IMMEDIATE → BEGIN DEFERRED\n"
        "      （写到一半才发现别人改过，SQLite 会抛 busy / snapshot冲突 —— 要么双认领，要么任务直接报错）",
     "web/store.py",
     '            c.execute("BEGIN IMMEDIATE")',
     '            c.execute("BEGIN DEFERRED")   # MUTATION',
     "tests.test_web_queue.TestCrossProcessClaim.test_two_processes_never_claim_the_same_job"),

    (2, "认领的两道防线**同时**被拆：SELECT 不再按 state 过滤 + UPDATE 不再带守卫\n"
        "      （单独拆任一道都翻不了红 —— 它们互为备份，这是好性质，见文件末尾的说明）",
     "web/store.py",
     '''                row = c.execute(
                    "SELECT job_id FROM jobs WHERE state=? "
                    "ORDER BY priority DESC, seq ASC LIMIT 1",
                    (STATE_QUEUED,)).fetchone()
                if row is None:
                    c.execute("ROLLBACK")
                    return None
                now = _now()
                cur = c.execute(
                    "UPDATE jobs SET state=?, started_at=?, heartbeat_at=?, worker=?,"
                    " attempts=attempts+1 WHERE job_id=? AND state=?",
                    (STATE_RUNNING, now, now, worker_id, row["job_id"], STATE_QUEUED))''',
     '''                row = c.execute(          # MUTATION —— ① 不按 state 过滤
                    "SELECT job_id FROM jobs "
                    "ORDER BY priority DESC, seq ASC LIMIT 1").fetchone()
                if row is None:
                    c.execute("ROLLBACK")
                    return None
                now = _now()
                cur = c.execute(
                    "UPDATE jobs SET state=?, started_at=?, heartbeat_at=?, worker=?,"
                    " attempts=attempts+1 WHERE job_id=?",   # MUTATION —— ② 也不带守卫
                    (STATE_RUNNING, now, now, worker_id, row["job_id"]))''',
     "tests.test_web_queue.TestClaim.test_claimed_item_is_not_claimable_again"),

    (3, "回到 C1 的「running 一律判死」：不再按心跳过滤，正在干活的任务也会被回收",
     "web/store.py",
     '''                "SELECT job_id FROM jobs WHERE state=? AND (heartbeat_at='' OR"
                " heartbeat_at <= ?)", (STATE_RUNNING, cutoff)).fetchall()''',
     '''                "SELECT job_id FROM jobs WHERE state=?", (STATE_RUNNING,)).fetchall()  # MUTATION''',
     "tests.test_web_queue.TestReapSafety.test_live_worker_keeps_its_job"),

    (4, "坏库不再隔离：连不上就直接抛，Web 起不来",
     "web/store.py",
     "                if attempt or not self._quarantine_corrupt():",
     "                if True:   # MUTATION",
     "tests.test_web_queue.TestMigrationAndResilience.test_corrupt_db_is_quarantined_not_silently_deleted"),

    (5, "会话签名**整段不校验**：任意形如 v1.x.y 的串都当成合法会话",
     "web/auth.py",
     """        if not hmac.compare_digest(sig, expect):
            return None""",
     """        if False:   # MUTATION —— 签名校验被跳过
            return None""",
     "tests.test_web_auth.TestSession.test_tampered_signature_rejected"),

    (6, "safe_key 放行 '..'：上传名可以直接指向 root 之外（目录穿越）",
     "web/storage.py",
     '''        if s == "..":
            raise UnsafeKey(f"key 不许含 '..'：{key!r}")''',
     '''        if False:   # MUTATION
            raise UnsafeKey(f"key 不许含 '..'：{key!r}")''',
     "tests.test_web_storage.TestSafeKey.test_traversal_up_is_rejected"),
]


# ==========================================================================
# 可靠的写入：本机 os.replace 会被杀软瞬时扣住，删了重建才稳（Stage 9 的血泪）
# ==========================================================================


def write_hard(path: Path, text: str) -> None:
    expect = None
    for attempt in range(12):
        tmp = path.with_name(path.name + f".mut{attempt}")
        try:
            tmp.write_text(text, encoding="utf-8")
            if tmp.read_text(encoding="utf-8") != text:
                raise OSError("回读不一致")
            if path.exists():
                os.remove(path)
            shutil.copyfile(tmp, path)
            got = path.read_text(encoding="utf-8")
            if got == text:
                tmp.unlink(missing_ok=True)
                return
            expect = got[:80]
        except OSError as exc:
            expect = str(exc)
        tmp.unlink(missing_ok=True)
        time.sleep(0.2 * (attempt + 1))
    raise SystemExit(f"写不进去 {path}（最后一次：{expect}）")


def run_test(target: str) -> tuple[bool, str]:
    """跑一条用例。返回 (通过?, 输出尾部)。"""
    p = subprocess.run([PY, "-m", "unittest", target],
                       cwd=str(PLATFORM), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    out = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
    ok = p.returncode == 0 and "OK" in out[-1]
    return ok, "\n".join(out[-6:])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C2 变异验证")
    ap.add_argument("--only", type=int, default=0, help="只跑第 N 条变异")
    ap.add_argument("--restore", action="store_true", help="只复原所有目标文件")
    args = ap.parse_args(argv)

    targets = sorted({m[2] for m in MUTATIONS})
    back_targets = {t: (PLATFORM / t).read_text(encoding="utf-8") for t in targets}

    #: **开工前的基线检查。** 这一条是被真事教出来的：某次跑到一半被打断，
    #: 变异留在了文件里；下一次运行时它的「备份」本身就是脏的，于是 --restore
    #: 也救不回来，而 M3 会因为锚点对不上被静默跳过 —— 看着像「判据漏网」，其实是脏基线。
    dirty = [t for t in targets if "# MUTATION" in back_targets[t]]
    if dirty:
        print("✗ 这些文件里还留着上一次的变异 —— 先手工把它们改回来再跑：")
        for t in dirty:
            for n, line in enumerate(back_targets[t].splitlines(), 1):
                if "# MUTATION" in line:
                    print(f"    {t}:{n}: {line.strip()}")
        return 2

    backups = back_targets

    if args.restore:
        for t, txt in backups.items():
            write_hard(PLATFORM / t, txt)
        print("已复原：", ", ".join(targets))
        return 0

    print(f"解释器：{PY}")
    print(f"工作区：{PLATFORM}\n")

    caught = 0
    total = 0
    try:
        for num, desc, fname, old, new, target in MUTATIONS:
            if args.only and num != args.only:
                continue
            total += 1
            path = PLATFORM / fname
            print(f"── 变异 {num} ── {desc}")

            original = path.read_text(encoding="utf-8")
            if old not in original:
                print("   ⚠ 锚点没匹配上 —— 判据对不上代码了，必须先修正本脚本\n")
                continue
            write_hard(path, original.replace(old, new, 1))

            ok_after_mut, tail = run_test(target)
            if not ok_after_mut:
                caught += 1
                print("   ✅ 被抓住（判据翻红，符合预期）")
            else:
                print("   ❌ 没抓住 —— 这条判据是死的，下面是变异后的输出：")
                for line in tail.splitlines():
                    print("      " + line)

            write_hard(path, original)
            ok_restored, tail2 = run_test(target)
            if ok_restored:
                print("   ↩ 复原后重新通过\n")
            else:
                print("   ⚠ 复原后竟然还没通过（复原可能失败）：")
                for line in tail2.splitlines():
                    print("      " + line)
                print()
    finally:
        for t, txt in backups.items():
            write_hard(PLATFORM / t, txt)

    print("=" * 66)
    print(f"结论：{caught}/{total} 个变异被 C2 的判据抓住"
          f"（{'全部有效' if caught == total else '有漏网'}）")
    return 0 if caught == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
