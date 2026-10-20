# -*- coding: utf-8 -*-
"""数据集与任务状态的持久化（轻量 JSON 存储 + 线程锁）。

所有数据都存放在平台自身的 `runs/` 目录下，不触碰用户其它文件。
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path

_LOCK = threading.RLock()


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.uploads = self.root / "_uploads"
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "_index.json"
        self._data = self._load()

    # ---------------- 基础 ----------------
    def _load(self) -> dict:
        if self.index_path.exists():
            try:
                return json.loads(self.index_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"datasets": []}

    def _save(self) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.index_path)

    # ---------------- 数据集 ----------------
    def new_id(self) -> str:
        return "ds_" + time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]

    def add(self, *, name: str, platform: str, category: str, brands: str,
            comment_limit: int, files: list[str], engine: str = "auto",
            uploads: list[str] | None = None) -> dict:
        with _LOCK:
            did = self.new_id()
            rec = {
                "id": did,
                "name": name,
                "platform": platform,
                "category": category,
                "brands": brands,
                "comment_limit": comment_limit,
                "engine": engine,
                "files": files,
                "uploads": list(uploads or []),
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "queued",
                "progress": 0,
                "message": "排队中…",
                "elapsed": None,
                "engine_used": "",
                "warnings": [],
                "overview": None,
                "error": "",
            }
            self._data["datasets"].insert(0, rec)
            self._save()
            return rec

    def list(self) -> list[dict]:
        """按创建时间**倒序**返回（最新在前）。

        这里显式排序而不是依赖数组插入顺序 —— 索引文件被手工编辑、
        或旧版本追加过记录时，插入顺序都可能与时间顺序不一致。
        """
        with _LOCK:
            recs = list(self._data["datasets"])
        recs.sort(key=lambda d: (d.get("created_at") or "", d.get("id") or ""), reverse=True)
        return recs

    def get(self, did: str) -> dict | None:
        with _LOCK:
            return next((d for d in self._data["datasets"] if d["id"] == did), None)

    def update(self, did: str, **fields) -> dict | None:
        with _LOCK:
            rec = self.get(did)
            if not rec:
                return None
            rec.update(fields)
            self._save()
            return rec

    def remove(self, did: str) -> bool:
        with _LOCK:
            recs = self._data["datasets"]
            rec = next((d for d in recs if d["id"] == did), None)
            if not rec:
                return False
            self._data["datasets"] = [d for d in recs if d["id"] != did]
            self._save()
            # 清理该数据集自身目录（限定在 runs/ 内）
            d = (self.root / did).resolve()
            if d.is_dir() and str(d).startswith(str(self.root.resolve())):
                shutil.rmtree(d, ignore_errors=True)
            # 连同上传的原始数据文件一起清除 —— 否则删了报告却把几十 MB 原件留在磁盘上
            self._drop_uploads(rec, {d["id"] for d in self._data["datasets"]})
            return True

    # ---------------- 上传原件 ----------------
    @staticmethod
    def _upload_key(filename: str) -> str:
        """上传文件在 _uploads 里的后缀键：与 save_upload 的安全化规则保持一致。"""
        safe = "".join(c for c in Path(filename).name if c.isalnum() or c in "._- ()[]")
        return safe or ""

    def _is_upload_path(self, p: Path) -> bool:
        """确认路径确实位于本平台的 _uploads 目录内（防止越权删除）。"""
        try:
            return p.resolve().parent == self.uploads.resolve() and p.is_file()
        except OSError:
            return False

    def _drop_uploads(self, rec: dict, other_ids: set) -> int:
        """删除该记录的上传原件。

        新记录带 `uploads`（保存时的精确绝对路径），直接按路径删，最精确。
        早期记录没有该字段，只能按文件名后缀匹配 —— 此时跳过仍被其它记录引用的键，
        避免误删同名文件的其他记录的上传件。
        """
        n = 0
        up = self.uploads.resolve()
        for raw in rec.get("uploads") or []:
            p = Path(raw)
            try:
                if p.resolve().parent == up and p.is_file():
                    p.unlink()
                    n += 1
            except OSError:
                continue
        if rec.get("uploads"):
            return n

        key = self._upload_key((rec.get("files") or [""])[0]) if rec.get("files") else ""
        if not key:
            return 0
        tail = "_" + key
        try:
            entries = list(self.uploads.iterdir())
        except OSError:
            return 0
        for p in entries:
            if not p.name.endswith(tail) or not self._is_upload_path(p):
                continue
            if self._key_used_by_others(key, other_ids):
                continue
            try:
                p.unlink()
                n += 1
            except OSError:
                continue
        return n

    def _key_used_by_others(self, key: str, other_ids: set) -> bool:
        for rec in self._data["datasets"]:
            if rec["id"] in other_ids:
                continue
            for name in rec.get("files") or []:
                if self._upload_key(name) == key:
                    return True
        return False

    # ---------------- 残留清理 ----------------
    def _referenced_upload_paths(self) -> set:
        """所有记录仍在引用的上传原件绝对路径。"""
        paths = set()
        for rec in self._data["datasets"]:
            for raw in rec.get("uploads") or []:
                try:
                    paths.add(str(Path(raw).resolve()))
                except OSError:
                    continue
        return paths

    def _legacy_keys(self) -> set:
        """早期记录（无 uploads 字段）引用的文件名键。"""
        keys = set()
        for rec in self._data["datasets"]:
            if rec.get("uploads"):
                continue
            for name in rec.get("files") or []:
                k = self._upload_key(name)
                if k:
                    keys.add(k)
        return keys

    @staticmethod
    def _dir_size(p: Path) -> int:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())

    def _orphan_uploads(self) -> tuple:
        """返回 (无主上传文件列表, 总字节)。"""
        referenced = self._referenced_upload_paths()
        keys = self._legacy_keys()
        orphans, total = [], 0
        try:
            entries = list(self.uploads.iterdir())
        except OSError:
            return orphans, total
        for p in entries:
            if not p.is_file():
                continue
            try:
                rp = str(p.resolve())
            except OSError:
                continue
            if rp in referenced:
                continue
            if keys and any(p.name.endswith("_" + k) for k in keys):
                continue
            orphans.append(p)
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return orphans, total

    def disk_report(self) -> dict:
        """磁盘占用与残留统计：孤立产出目录 + 无主上传原件。"""
        with _LOCK:
            ids = {d["id"] for d in self._data["datasets"]}
        orphan_dirs = []
        try:
            entries = list(self.root.iterdir())
        except OSError:
            entries = []
        for p in entries:
            if p.is_dir() and not p.name.startswith("_") and p.name not in ids:
                try:
                    orphan_dirs.append({"id": p.name, "bytes": self._dir_size(p)})
                except OSError:
                    orphan_dirs.append({"id": p.name, "bytes": 0})
        with _LOCK:
            ups, up_bytes = self._orphan_uploads()
        return {
            "records": len(ids),
            "orphan_dirs": orphan_dirs,
            "orphan_dir_bytes": sum(d["bytes"] for d in orphan_dirs),
            "orphan_uploads": len(ups),
            "orphan_upload_bytes": up_bytes,
        }

    def cleanup(self) -> dict:
        """清除孤立产出目录与无主上传原件。

        只删除**不在 `runs/_index.json` 里**的 `ds_*` 目录，以及不再被任何记录引用的
        上传原件；正在排队/运行的任务目录在索引中，因此不会被误删。
        """
        report = self.disk_report()
        removed_dirs: list[str] = []
        removed_uploads = 0
        with _LOCK:
            root = self.root.resolve()
            for it in report["orphan_dirs"]:
                d = (self.root / it["id"]).resolve()
                if d.is_dir() and str(d).startswith(str(root)):
                    shutil.rmtree(d, ignore_errors=True)
                    removed_dirs.append(it["id"])
            ups, _ = self._orphan_uploads()
            for p in ups:
                try:
                    if self._is_upload_path(p):
                        p.unlink()
                        removed_uploads += 1
                except OSError:
                    continue
        return {
            "removed_dirs": removed_dirs,
            "removed_uploads": removed_uploads,
            "freed_bytes": report["orphan_dir_bytes"] + report["orphan_upload_bytes"],
        }

    def remove_many(self, ids: list[str]) -> int:
        """批量删除，返回实际删除条数。"""
        n = 0
        for did in ids or []:
            try:
                if self.remove(did):
                    n += 1
            except Exception:
                continue
        return n

    # ---------------- 路径 ----------------
    def dataset_dir(self, did: str) -> Path:
        """返回数据集目录路径（不创建 —— 由管线在真正写入时才创建，
        避免查询不存在的 id 时留下空目录）。"""
        return self.root / did

    def save_upload(self, filename: str, content: bytes) -> str:
        safe = "".join(c for c in Path(filename).name if c.isalnum() or c in "._- ()[]")
        if not safe:
            safe = "upload.bin"
        stamp = time.strftime("%H%M%S")
        out = self.uploads / f"{stamp}_{uuid.uuid4().hex[:6]}_{safe}"
        out.write_bytes(content)
        return str(out)
