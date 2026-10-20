#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web/storage.py · 存储抽象（**对象存储可换，内核接口不换**）。

为什么需要一个抽象层
====================

路线图原本写「文件 → 对象存储」。但真去做的时候有一个**绕不过去的约束**：

    内核只能读**本地路径**。

``JobSpec.input`` 是一个 ``Path``，``run_job`` 会把它复制进 ``<job>/input/``；
中间的每一个文件也都写在本地 —— 单次 ``annotate`` 就要给 7,662 条评论逐个写中间文件。
把中间文件搬到对象存储上，延迟与计费都不成立（路线图自己把这条列为「三处硬撞墙」之一）。

所以本模块划的界线是：

============================ ==========================================================
数据                          放哪
============================ ==========================================================
**原始上传**                 走 :class:`Storage` —— 这是最该活过机器故障的东西
**中间文件**                 本地。内核的形态决定的，不迁移
**最终产物**（report/*）      本地；可选 ``VOC_WEB_MIRROR_ARTIFACTS=1`` 镜像一份出去
============================ ==========================================================

于是协议里最关键的一个方法是 :meth:`Storage.ensure_local` ——
「给我一个本地路径」。本地后端直接返回原路径；S3 后端得先下载到缓存目录。
**没有这个方法，抽象层就是假的**（调用方会自己拼 ``root / key``，S3 立刻崩）。

``S3Storage`` 的现实状况（**别读过这一段就以为它是验过的**）
===========================================================

- ``LocalStorage``：本机实测，默认后端，一致性用例常驻守卫。
- ``S3Storage``：**按 boto3 API 写完的，但本机没有 S3 凭据，因此没有跑过真实后端。**
  它和 ``LocalStorage`` 共享同一套契约用例（``tests/test_web_storage.py``），
  设上 ``VOC_TEST_S3_BUCKET`` 即可对真实桶验证。**在验证之前，别在生产上开它。**
  这么写出来，比假装它验过要诚实。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

#: 单个 key 的段：允许中英文/数字/点/下划线/连字符/空格；**不允许** ``..``、``:``、``\``
_SEG_RE = re.compile(r"^[A-Za-z0-9._\u4e00-\u9fff][A-Za-z0-9._\u4e00-\u9fff \-]*$")
#: Windows 保留设备名 —— 拿来当目录名会静默失败或行为诡异
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class StorageError(Exception):
    """存储层错误。"""


class StorageBackendUnavailable(StorageError):
    """后端不可用（缺依赖 / 缺配置 / 缺凭据）。"""


class UnsafeKey(StorageError):
    """key 不合法 —— 例如含 ``..`` 或绝对路径。**这层拦截是为了防目录穿越。**"""


def safe_key(key: str) -> str:
    """校验并规范化 key，返回 ``a/b/c`` 形式的相对路径。

    这是**安全边界**，不是格式化：``LocalStorage`` 会把它拼到 root 后面当真实路径用，
    放过一个 ``../..`` 就等于把整个磁盘交出去。所以这里**拒绝**而不是「修正」——
    静默修正会让调用方的 bug 藏起来。
    """
    k = (key or "").replace("\\", "/").strip()
    if not k:
        raise UnsafeKey("key 不能为空")
    if k.startswith("/") or re.match(r"^[A-Za-z]:", k):
        raise UnsafeKey(f"key 不许是绝对路径：{key!r}")
    segs = [s for s in k.split("/") if s not in ("", ".")]
    if not segs:
        raise UnsafeKey(f"key 里没有有效路径段：{key!r}")
    for s in segs:
        if s == "..":
            raise UnsafeKey(f"key 不许含 '..'：{key!r}")
        if "/" in s or "\\" in s:
            raise UnsafeKey(f"key 段含路径分隔符：{s!r}")
        if not _SEG_RE.match(s):
            raise UnsafeKey(f"key 段含非法字符：{s!r}")
        if s.split(".")[0].lower() in _RESERVED:
            raise UnsafeKey(f"key 段是系统保留名：{s!r}")
    return "/".join(segs)


@runtime_checkable
class Storage(Protocol):
    """存储后端契约。**所有实现都必须通过 ``tests/test_web_storage.py`` 的一致性用例。**"""

    scheme: str

    def put_bytes(self, key: str, data: bytes) -> str:
        """写入字节，返回规范化后的 key。**必须做写后校验**（本机有瞬时文件锁）。"""

    def put_file(self, key: str, src: str | Path) -> str:
        """把本地文件的内容写到 ``key``。"""

    def get_bytes(self, key: str) -> bytes:
        """读回字节。不存在则抛 :class:`StorageError`。"""

    def ensure_local(self, key: str) -> Path:
        """**内核接口**：给一个本地路径（至少在进程退出前有效）。"""

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> bool:
        """删除；返回是否真的删掉了（不存在返回 ``False``，不抛）。"""

    def list(self, prefix: str = "") -> list[str]:
        """列出 ``prefix`` 下的 key（相对 root，正斜杠）。"""

    def mirror(self, src_dir: str | Path, prefix: str, *,
               pattern: str = "**/*", max_files: int = 2000) -> list[str]:
        """把本地目录递归镜像到 ``prefix`` 下，返回写入的 key 列表。"""

    def describe(self) -> dict[str, Any]:
        """给 ``/api/meta`` 用的一句话描述（不许含凭据）。"""


# ==========================================================================
# 本地文件系统后端（默认，本机实测）
# ==========================================================================


class LocalStorage:
    """把 ``root`` 当对象存储用。**默认后端，验收跑的就是它。**

    写入刻意**不用** ``os.replace``：上传的 key 每次都唯一
    （``<job_id>/<tag>__<文件名>``），没有「覆盖别人正在读的文件」的问题，
    也就没必要付 ``os.replace`` 在本机最慢 11.68s（Stage 9 实测）的代价。
    取而代之的是**写后回读校验 + 退避重试** —— 本机的瞬时文件锁会吞掉写入，
    盲写是危险的（这条踩过）。
    """

    scheme = "local"

    def __init__(self, root: str | Path, *, name: str = "local", attempts: int = 6) -> None:
        self.root = Path(root)
        self.name = name
        self.attempts = int(attempts)

    # -- 内部 --------------------------------------------------------------
    def _path(self, key: str) -> Path:
        return self.root / safe_key(key)

    def _write_verified(self, path: Path, data: bytes) -> None:
        """写 → 回读比对 → 不符就重试。

        本机杀软/备份服务会扣住刚写出的文件若干秒，``write_bytes`` 会**静默失败**
        （不抛异常，但内容为空或截断）。所以这里不接受「没抛异常就当成功」。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        last: Exception | None = None
        for i in range(self.attempts):
            try:
                with path.open("wb") as fh:
                    fh.write(data)
                back = path.read_bytes()
                if back == data:
                    return
                last = StorageError(
                    f"写后回读不一致：{path.name} 期望 {len(data)} 字节，"
                    f"实得 {len(back)} 字节")
            except OSError as exc:
                last = exc
            time.sleep(0.05 * (i + 1))
        raise StorageError(f"写入失败（重试 {self.attempts} 次）：{path} —— {last}")

    # -- 契约 --------------------------------------------------------------
    def put_bytes(self, key: str, data: bytes) -> str:
        k = safe_key(key)
        self._write_verified(self._path(k), data)
        return k

    def put_file(self, key: str, src: str | Path) -> str:
        return self.put_bytes(key, Path(src).read_bytes())

    def get_bytes(self, key: str) -> bytes:
        p = self._path(key)
        try:
            return p.read_bytes()
        except OSError as exc:
            raise StorageError(f"读不到 {key}：{exc}") from exc

    def ensure_local(self, key: str) -> Path:
        p = self._path(key)
        if not p.exists():
            raise StorageError(f"不存在：{key}")
        return p

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> bool:
        p = self._path(key)
        try:
            p.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StorageError(f"删不掉 {key}：{exc}") from exc

    def list(self, prefix: str = "") -> list[str]:
        base = self.root
        if prefix:
            base = self._path(prefix)
        if not base.exists():
            return []
        return sorted(
            p.relative_to(self.root).as_posix()
            for p in base.rglob("*") if p.is_file())

    def mirror(self, src_dir: str | Path, prefix: str, *,
               pattern: str = "**/*", max_files: int = 2000) -> list[str]:
        src = Path(src_dir)
        if not src.exists():
            return []
        written: list[str] = []
        for p in sorted(src.glob(pattern)):
            if not p.is_file():
                continue
            if len(written) >= max_files:
                break
            rel = p.relative_to(src).as_posix()
            written.append(self.put_file(f"{prefix}/{rel}", p))
        return written

    def describe(self) -> dict[str, Any]:
        return {"scheme": self.scheme, "name": self.name, "root": str(self.root)}


# ==========================================================================
# S3 / 兼容对象存储后端（**实现已写，本机未验证** —— 见模块头）
# ==========================================================================


class S3Storage:
    """S3（或 MinIO / 阿里云 OSS 等 S3 兼容服务）后端。

    ``ensure_local`` 是这里唯一有设计含量的方法：对象存储没有本地路径，
    所以先把对象下载到 ``cache_dir``（按 key 分层）。契约只承诺
    「进程退出前有效」，缓存因此**不做淘汰** —— 一次 job 的输入只有几个文件，
    做完就随 job 目录一起被清理。
    """

    scheme = "s3"

    def __init__(self, bucket: str, *, prefix: str = "",
                 region: str | None = None, endpoint_url: str | None = None,
                 cache_dir: str | Path | None = None,
                 access_key: str | None = None, secret_key: str | None = None) -> None:
        if not bucket:
            raise StorageBackendUnavailable("没配 bucket（VOC_WEB_S3_BUCKET）")
        try:
            import boto3  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover —— 本机没装
            raise StorageBackendUnavailable(
                "S3 后端需要 boto3：pip install boto3（或 pip install -e \".[s3]\"）") from exc
        self.bucket = bucket
        self.prefix = (prefix or "").strip("/")
        self.cache_dir = Path(cache_dir or Path(os.environ.get("TEMP") or "/tmp")
                              / "voc-s3-cache")
        self._s3 = boto3.client(
            "s3", region_name=region, endpoint_url=endpoint_url,
            aws_access_key_id=access_key, aws_secret_access_key=secret_key)

    # -- 内部 --------------------------------------------------------------
    def _full(self, key: str) -> str:
        k = safe_key(key)
        return f"{self.prefix}/{k}" if self.prefix else k

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / safe_key(key)

    # -- 契约 --------------------------------------------------------------
    def put_bytes(self, key: str, data: bytes) -> str:
        k = safe_key(key)
        self._s3.put_object(Bucket=self.bucket, Key=self._full(k), Body=data)
        return k

    def put_file(self, key: str, src: str | Path) -> str:
        k = safe_key(key)
        self._s3.upload_file(str(src), self.bucket, self._full(k))
        return k

    def get_bytes(self, key: str) -> bytes:
        from botocore.exceptions import ClientError  # noqa: PLC0415
        try:
            return self._s3.get_object(Bucket=self.bucket,
                                       Key=self._full(key))["Body"].read()
        except ClientError as exc:
            raise StorageError(f"读不到 {key}：{exc}") from exc

    def ensure_local(self, key: str) -> Path:
        """下载到本地缓存并返回路径。**已存在就不重复下载**（断点续跑会反复要它）。"""
        k = safe_key(key)
        dst = self._cache_path(k)
        if dst.exists() and dst.stat().st_size > 0:
            return dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._s3.download_file(self.bucket, self._full(k), str(dst))
        except Exception as exc:  # noqa: BLE001 —— botocore 异常类型很杂
            raise StorageError(f"下载失败 {key}：{exc}") from exc
        return dst

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError  # noqa: PLC0415
        try:
            self._s3.head_object(Bucket=self.bucket, Key=self._full(key))
            return True
        except ClientError:
            return False

    def delete(self, key: str) -> bool:
        if not self.exists(key):
            return False
        self._s3.delete_object(Bucket=self.bucket, Key=self._full(key))
        return True

    def list(self, prefix: str = "") -> list[str]:
        base = safe_key(prefix) if prefix else ""
        full = f"{self.prefix}/{base}".strip("/")
        keys: list[str] = []
        token = None
        while True:
            kw: dict[str, Any] = {"Bucket": self.bucket}
            if full:
                kw["Prefix"] = full + "/" if not full.endswith("/") else full
            if token:
                kw["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kw)
            for obj in resp.get("Contents") or []:
                k = obj["Key"]
                if self.prefix and k.startswith(self.prefix + "/"):
                    k = k[len(self.prefix) + 1:]
                keys.append(k)
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return sorted(keys)

    def mirror(self, src_dir: str | Path, prefix: str, *,
               pattern: str = "**/*", max_files: int = 2000) -> list[str]:
        src = Path(src_dir)
        if not src.exists():
            return []
        written: list[str] = []
        for p in sorted(src.glob(pattern)):
            if not p.is_file():
                continue
            if len(written) >= max_files:
                break
            written.append(self.put_file(f"{prefix}/{p.relative_to(src).as_posix()}", p))
        return written

    def describe(self) -> dict[str, Any]:
        #: **不许把 access key / secret 写进来** —— 这个 dict 会进 /api/meta
        return {"scheme": self.scheme, "bucket": self.bucket, "prefix": self.prefix}


# ==========================================================================
# 工厂
# ==========================================================================


def time_sleep(seconds: float) -> None:
    """薄封装，便于测试时替换掉（不让用例真的睡）。"""
    import time
    time.sleep(seconds)


def storage_from_env(default_root: str | Path, *,
                     env: dict[str, str] | None = None) -> Storage:
    """按环境变量挑后端。失败时**抛异常**而不是静默退回本地 ——

    静默退回会让「以为在上对象存储、其实写在本机」这种事发生，而那种错要等到
    换机器那天才暴露。
    """
    e = os.environ if env is None else env
    kind = (e.get("VOC_WEB_STORAGE") or "local").strip().lower()
    if kind in ("", "local", "file", "fs"):
        return LocalStorage(e.get("VOC_WEB_STORAGE_ROOT") or default_root)
    if kind in ("s3", "minio", "oss", "cos"):
        return S3Storage(
            e.get("VOC_WEB_S3_BUCKET", ""),
            prefix=e.get("VOC_WEB_S3_PREFIX", ""),
            region=e.get("VOC_WEB_S3_REGION") or None,
            endpoint_url=e.get("VOC_WEB_S3_ENDPOINT") or None,
            cache_dir=e.get("VOC_WEB_S3_CACHE") or None,
            access_key=e.get("VOC_WEB_S3_ACCESS_KEY") or None,
            secret_key=e.get("VOC_WEB_S3_SECRET_KEY") or None)
    raise StorageBackendUnavailable(
        f"不认识的 VOC_WEB_STORAGE={kind!r}（可选：local / s3）")


def keys_of(paths: Iterable[str]) -> list[str]:
    """小工具：把一组相对路径规范化成 key（顺手过滤掉不合法的）。"""
    out = []
    for p in paths:
        try:
            out.append(safe_key(p))
        except UnsafeKey:
            continue
    return out


__all__ = [
    "LocalStorage", "S3Storage", "Storage", "StorageBackendUnavailable",
    "StorageError", "UnsafeKey", "keys_of", "safe_key", "storage_from_env",
    "shutil",
]
