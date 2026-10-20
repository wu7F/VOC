#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""C2 · 对象存储抽象（标准库实现，本机核验过 local；s3 只验契约不联网）。

要守的**那一条契约**
====================

    ensure_local(key) -> Path

内核是**本地文件密集**的：七步里每一步都读写 ``<job>/...``，尤其 ``annotate`` 要给
几千条评论逐个写中间文件（实测 162s）。它只认本地路径 —— 可一旦上了对象存储，
``JobSpec.input`` 指过去的对象**内核是打不开的**。

所以 ``ensure_local`` 是整个抽象里唯一不可妥协的东西：**任何后端都必须保证它返回一个
真的在本地磁盘上、内核立即能读的路径**。S3 后端的做法是按需拉回 ``cache_dir``。

其余方法（``put_bytes`` / ``get_bytes`` / ``list`` / ``mirror``）都只是围绕它服务。
本文件的 :class:`TestLocalListView` 与 :class:`TestBackendsShareTheContract`
各自咬住这份契约的一半。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[1]
if str(PLATFORM) not in sys.path:
    sys.path.insert(0, str(PLATFORM))

from web import storage as ST  # noqa: E402


def _mk(tmp: str) -> ST.LocalStorage:
    return ST.LocalStorage(tmp)


# ==========================================================================
# key 消毒：上传文件名是**用户输入**，这是我们第一道防线
# ==========================================================================


class TestSafeKey(unittest.TestCase):
    """``PosixPath`` 对 Windows 风格的分隔符不做「非法」判定 ----- 得我们自己做。

    不消毒的话，一个恶意的文件名可以做到：
    - ``../../voc_core/pipeline.py`` → 覆盖内核源码（下一次请求就执行它）
    - ``abs/path`` → 写到根目录之外

    这些 key 最终会被拼进 ``JobSpec.input`` 再由内核读取，所以必须在**入口**堵住。
    """

    def test_traversal_up_is_rejected(self):
        for bad in ("../../etc/passwd", "..", "../secret", "a/../../b", "x/y/../../../z"):
            with self.assertRaises(ST.StorageError, msg=f"放行了 {bad!r}"):
                ST.safe_key(bad)

    def test_absolute_path_is_rejected(self):
        for bad in ("/etc/passwd", "/abs/path", "//server/share"):
            with self.assertRaises(ST.StorageError, msg=f"放行了 {bad!r}"):
                ST.safe_key(bad)

    def test_windows_separators_are_rejected(self):
        """Windows 上 ``a\\..\\b`` 同样是穿越 —— 不能只挡 ``/``。"""
        for bad in ("a\\..\\b", "C:\\windows\\system32\\x", "..\\..\\x"):
            with self.assertRaises(ST.StorageError, msg=f"放行了 {bad!r}"):
                ST.safe_key(bad)

    def test_empty_and_blank_rejected(self):
        for bad in ("", "   ", "."):
            with self.assertRaises(ST.StorageError, msg=f"放行了 {bad!r}"):
                ST.safe_key(bad)

    def test_redundant_segments_are_normalized_not_rejected(self):
        """``./x``、``a//b`` 无害 —— **规范化**即可，不必当成攻击拒掉。

        但必须规范化到同一个 key：否则 ``./x`` 与 ``x`` 会变成两份互不相干的对象，
        排查时会以为上传丢了。
        """
        self.assertEqual(ST.safe_key("./x"), "x")
        self.assertEqual(ST.safe_key("a//b"), "a/b")
        self.assertEqual(ST.safe_key("a/./b/"), "a/b")
        self.assertEqual(ST.safe_key("  uploads/j1/a.csv  "), "uploads/j1/a.csv")

    def test_windows_reserved_names_rejected(self):
        """保留设备名（CON/NUL/COM1…）在 Windows 上不是普通文件 —— 挡掉它们。"""
        for bad in ("CON", "nul.txt", "a/COM1", "aux", "LPT1.log"):
            with self.assertRaises(ST.StorageError, msg=f"放行了 {bad!r}"):
                ST.safe_key(bad)

    def test_nested_key_is_allowed(self):
        """正常的分层 key 必须放行 —— 别把消毒做成「不许有目录」。"""
        self.assertEqual(ST.safe_key("uploads/2026-10-08/job-1/data.csv"),
                         "uploads/2026-10-08/job-1/data.csv")
        self.assertEqual(ST.safe_key("a.json"), "a.json")

    def test_keys_of_filters_bad_entries(self):
        """批量转换时**跳过**坏项而不是整体失败（一颗老鼠屎坏一锅就不好用了）。"""
        self.assertEqual(ST.keys_of(["ok/a.csv", "../evil", "b/c.json"]),
                         ["ok/a.csv", "b/c.json"])


# ==========================================================================
# 本地后端：读写与 ensure_local
# ==========================================================================


class TestLocalBackend(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.s = _mk(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_roundtrip(self):
        self.s.put_bytes("j1/main.csv", b"a,b\n1,2\n")
        self.assertEqual(self.s.get_bytes("j1/main.csv"), b"a,b\n1,2\n")
        self.assertTrue(self.s.exists("j1/main.csv"))

    def test_ensure_local_is_readable_by_the_kernel(self):
        """契约的核心：给回来的路径**必须**能立即被内核读（不能是一句"s3://..."）。"""
        self.s.put_bytes("j1/in.csv", b"payload")
        p = self.s.ensure_local("j1/in.csv")
        self.assertIsInstance(p, Path)
        self.assertTrue(p.exists(), "ensure_local 返回了不存在的路径")
        self.assertEqual(p.read_bytes(), b"payload")

    def test_ensure_local_missing_key_raises(self):
        """不存在就**报错**，不要返回个假路径让内核自己去猜。

        静默返回一个不存在的路径，会让错误从「提交阶段」推迟到「跑到第三步才炸」。
        """
        with self.assertRaises(ST.StorageError):
            self.s.ensure_local("nope/missing.csv")

    def test_read_missing_key_raises(self):
        with self.assertRaises(ST.StorageError):
            self.s.get_bytes("nope.csv")

    def test_put_file_and_delete(self):
        src = self.root / "_src.json"
        src.write_text('{"x":1}', encoding="utf-8")
        self.s.put_file("j2/a.json", src)
        self.assertTrue(self.s.exists("j2/a.json"))
        self.assertTrue(self.s.delete("j2/a.json"))
        self.assertFalse(self.s.exists("j2/a.json"))
        self.assertFalse(self.s.delete("j2/a.json"), "重复删除应返回 False 而不是报错")

    def test_bad_key_is_refused_even_on_write(self):
        """消毒必须落在**每一次**读写上，不能只在 UI 那一层做。"""
        with self.assertRaises(ST.StorageError):
            self.s.put_bytes("../../evil.py", b"rm -rf /")
        with self.assertRaises(ST.StorageError):
            self.s.get_bytes("../../evil.py")

    def test_written_bytes_match_exactly(self):
        """写后回读校验 —— 本机杀软会扣住刚写的文件导致写入静默失败（Stage 9 的坑）。"""
        blob = bytes(range(256)) * 40          # 10240 字节，含各种控制字符
        self.s.put_bytes("j3/blob.bin", blob)
        self.assertEqual(self.s.get_bytes("j3/blob.bin"), blob)
        self.assertEqual(len(self.s.get_bytes("j3/blob.bin")), len(blob))

    def test_describe_has_scheme(self):
        d = self.s.describe()
        self.assertEqual(d["scheme"], "local")
        self.assertIn("root", d)


# ==========================================================================
# 列表与镜像
# ==========================================================================


class TestLocalListView(unittest.TestCase):
    """``list`` / ``mirror`` 用相对 root 的 posix key —— 换后端也不能变口径。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.s = _mk(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_list_is_sorted_and_posix_relative(self):
        self.s.put_bytes("j1/a.csv", b"x")
        self.s.put_bytes("j1/sub/b.csv", b"x")
        self.assertEqual(self.s.list("j1"), ["j1/a.csv", "j1/sub/b.csv"])

    def test_list_missing_prefix_is_empty_not_error(self):
        self.assertEqual(self.s.list("nothing-here"), [])

    def test_mirror_copies_every_file(self):
        src = self.root / "_src"
        (src / "report").mkdir(parents=True)
        (src / "report" / "dashboard.html").write_text("<html>", encoding="utf-8")
        (src / "job.json").write_text("{}", encoding="utf-8")
        keys = self.s.mirror(src, "artifacts/j1")
        self.assertEqual(keys, ["artifacts/j1/job.json",
                                "artifacts/j1/report/dashboard.html"])
        #: 镜像过去的必须能原样读回来
        self.assertEqual(self.s.get_bytes("artifacts/j1/report/dashboard.html"),
                         b"<html>")

    def test_mirror_missing_dir_is_noop(self):
        self.assertEqual(self.s.mirror(self.root / "nope", "p"), [])

    def test_mirror_caps_file_count(self):
        """上限保护：别让一个畸形 job 目录把接口和对象存储账单一起拖死。"""
        src = self.root / "_big"
        src.mkdir()
        for i in range(10):
            (src / f"f{i}.txt").write_text("x", encoding="utf-8")
        self.assertEqual(len(self.s.mirror(src, "big", max_files=3)), 3)


# ==========================================================================
# 后端选择与契约一致性
# ==========================================================================


class TestBackendsShareTheContract(unittest.TestCase):
    """两种后端必须**方法齐全且同名** —— 否则换存储那天才炸。

    只比名字不比行为：行为差异属于各自集成核验（S3 需要真实桶。
    **本机没 boto3 就把话说清楚**，不要静默退回 local —— 静默退回会让
    「以为在上云、其实写本机」这种事发生，而那种错要等换机器那天才暴露。
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._td.cleanup()

    def test_local_satisfies_the_protocol_members(self):
        required = {"put_bytes", "put_file", "get_bytes", "ensure_local",
                    "exists", "delete", "list", "mirror", "describe"}
        missing = required - {m for m in dir(ST.LocalStorage) if not m.startswith("_")}
        self.assertEqual(set(), missing, f"本地后端缺方法：{sorted(missing)}")
        missing = required - {m for m in dir(ST.S3Storage) if not m.startswith("_")}
        self.assertEqual(set(), missing, f"S3 后端缺方法：{sorted(missing)}")

    def test_default_picks_local(self):
        s = ST.storage_from_env(self._td.name, env={})
        self.assertEqual(s.describe()["scheme"], "local")

    def test_explicit_local_and_root_override(self):
        s = ST.storage_from_env("/ignored",
                                env={"VOC_WEB_STORAGE": "local",
                                     "VOC_WEB_STORAGE_ROOT": self._td.name})
        self.assertEqual(s.describe()["root"], self._td.name)

    def test_unknown_scheme_raises_loudly(self):
        with self.assertRaises(ST.StorageBackendUnavailable):
            ST.storage_from_env(self._td.name, env={"VOC_WEB_STORAGE": "gcs"})

    def test_s3_without_boto3_explains_itself(self):
        """缺依赖要说**缺什么**，不要抛个 ImportError 让人猜。"""
        try:
            ST.S3Storage("bucket", cache_dir=self._td.name)
        except ST.StorageBackendUnavailable as exc:
            self.assertIn("boto3", str(exc))
        except ST.StorageError as exc:
            self.assertIn("boto3", str(exc))
        # 真装了 boto3 也不该从这里崩 —— 那时它需要的是合法凭据，同样会明说

    def test_reads_environment_when_env_not_passed(self):
        old = os.environ.get("VOC_WEB_STORAGE")
        os.environ["VOC_WEB_STORAGE"] = "local"
        try:
            self.assertEqual(ST.storage_from_env(self._td.name).describe()["scheme"],
                             "local")
        finally:
            if old is None:
                os.environ.pop("VOC_WEB_STORAGE", None)
            else:
                os.environ["VOC_WEB_STORAGE"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
