"""Preview and permanently remove one archived local case, with bounded file scope.

The caller must serialize writers (the workbench already holds its lock). No
content backup or recycle bin is created. Only explicitly registered evidence
files are considered; paths embedded in case prose are never deletion targets.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from datetime import datetime, timezone

from case_store import CaseStore, StoreError, case_id, load_json
from render_case_library import render_library


class DeletionError(StoreError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def revision(case):
    return digest(case)


def _absolute(value):
    path = Path(value)
    if not path.is_absolute() or str(value).startswith(("\\\\", "//")):
        raise DeletionError("删除范围必须使用本机绝对路径。")
    return path


def _safe_path(path, root):
    """Check every existing ancestor before resolving or touching a target."""
    path, root = _absolute(path), _absolute(root)
    for cursor in (path, *path.parents):
        if cursor.is_symlink() or getattr(cursor, "is_junction", lambda: False)():
            raise DeletionError(f"不允许通过符号链接或目录联接删除：{cursor}")
        if cursor.exists():
            mode = cursor.lstat()
            if getattr(mode, "st_file_attributes", 0) & 0x400:
                raise DeletionError(f"不允许删除重解析路径：{cursor}")
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise DeletionError(f"删除目标超出允许范围：{path}")
    return path


def _stamp(path, *, content=False):
    info = path.stat()
    result = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
              info.st_ctime_ns, info.st_nlink, stat.S_IFMT(info.st_mode)]
    if content:
        result.append(hashlib.sha256(path.read_bytes()).hexdigest())
    return result


def _identity(path):
    """Lexical/resolved reference identity; never use this as delete authority."""
    return os.path.normcase(str(Path(path).resolve()))


def _references(case):
    references = set()
    for snapshot in [case.get("current"), *(event.get("current") for event in case["events"])]:
        snapshot = snapshot or {}
        references.update(snapshot.get("assets", {}).values())
        source = snapshot.get("source_case")
        if source:
            references.add(source["file_path"])
    # A standalone structured original source can also be a shared local file.
    raw = case.get("original", {}).get("source")
    if isinstance(raw, str) and Path(raw).is_absolute() and not raw.startswith(("\\\\", "//")):
        references.add(raw)
    return references


class DeletionManager:
    def __init__(self, skill_root, runtime_root, source_root=None, *, source_handler=None):
        self.root = _safe_path(_absolute(skill_root), _absolute(skill_root)).resolve()
        self.runtime = _safe_path(_absolute(runtime_root), _absolute(runtime_root)).resolve()
        if self.root.is_relative_to(self.runtime) or self.runtime.is_relative_to(self.root):
            raise DeletionError("案例库与运行资料目录必须彼此独立。")
        self.store = CaseStore(self.root)
        self.evidence = self.runtime / "evidence"
        self.library = self.runtime / "library"
        self.source_root = Path(source_root or self.root.parent / "tiktok-video-script-voiceover").resolve()
        self.source_handler = source_handler
        if source_handler is None and (Path(__file__).parent / "current_source_deletion.py").is_file():
            from current_source_deletion import CurrentSourceDeletion
            self.source_handler = CurrentSourceDeletion()
        elif source_handler is None and (Path(__file__).parent / "legacy_case_deletion.py").is_file():
            from legacy_case_deletion import LegacySourceDeletion
            self.source_handler = LegacySourceDeletion()

    def _markers(self):
        path = _safe_path(self.root / "deleted_case_sources.json", self.root)
        if not path.exists():
            return {"schema_version": 1, "sources": []}, None
        if not path.is_file() or path.stat().st_nlink != 1:
            raise DeletionError("删除登记必须是未共享的普通文件。")
        data = load_json(path)
        if not isinstance(data, dict) or set(data) != {"schema_version", "sources"} or data["schema_version"] != 1 or not isinstance(data["sources"], list):
            raise DeletionError("删除登记格式无效，请先修复。")
        keys = set()
        for entry in data["sources"]:
            if (not isinstance(entry, dict) or set(entry) != {"library_id", "source_root", "case_id", "deleted_at"}
                    or any(not isinstance(value, str) or not value.strip() for value in entry.values())):
                raise DeletionError("删除登记条目格式无效，请先修复。")
            case_id(entry["case_id"])
            if not re.fullmatch(r"VSL-(?:LT|T[0-4])-\d{3}", entry["library_id"]):
                raise DeletionError("删除登记来源编号无效，请先修复。")
            _absolute(entry["source_root"])
            key = (_identity(entry["source_root"]), entry["library_id"])
            if key in keys:
                raise DeletionError("删除登记包含重复来源，请先修复。")
            keys.add(key)
        return data, _stamp(path, content=True)

    def _write_markers(self, source_ids, expected_stamp):
        if not source_ids:
            return
        data, stamp = self._markers()
        if stamp != expected_stamp:
            raise DeletionError("删除登记已发生变化，请重新预览。", 409)
        known = {(_identity(entry["source_root"]), entry["library_id"]) for entry in data["sources"]}
        now = datetime.now(timezone.utc).isoformat()
        for library_id, identifier in source_ids:
            key = (_identity(self.source_root), library_id)
            if key not in known:
                data["sources"].append({"library_id": library_id, "source_root": str(self.source_root),
                                        "case_id": identifier, "deleted_at": now})
                known.add(key)
        destination = _safe_path(self.root / "deleted_case_sources.json", self.root)
        descriptor, temporary = tempfile.mkstemp(prefix=".deleted-sources-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            _safe_path(destination, self.root)
            if self._markers()[1] != expected_stamp:
                raise DeletionError("删除登记已发生变化，请重新预览。", 409)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                Path(temporary).unlink()

    def _tree(self, folder, root, kind):
        """Inventory ordinary files and directories without following links."""
        folder = _safe_path(folder, root)
        if not folder.exists():
            return [], []
        if not folder.is_dir():
            raise DeletionError(f"预期目录被其他文件占用：{folder}")
        files, directories = [], []
        pending = [folder]
        while pending:
            directory = pending.pop()
            _safe_path(directory, root)
            directories.append({"path": str(directory), "stamp": _stamp(directory)})
            for entry in sorted(directory.iterdir()):
                _safe_path(entry, root)
                info = entry.stat()
                if stat.S_ISDIR(info.st_mode):
                    pending.append(entry)
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1:
                        raise DeletionError(f"目录内文件存在硬链接，需先处理链接：{entry}")
                    files.append({"path": str(entry), "kind": kind, "bytes": info.st_size,
                                  "stamp": _stamp(entry, content=True), "root": str(root)})
                else:
                    raise DeletionError(f"目录包含无法安全删除的特殊文件：{entry}")
        return files, directories

    def _source_plan(self, case, cases, include_source):
        if self.source_handler:
            return self.source_handler.preview(self.root, self.source_root, case, cases, include_source)
        source = (case.get("current") or {}).get("source_case")
        if include_source:
            raise DeletionError("当前未配置旧库源文件删除；请先关闭该选项。")
        retained = []
        if source:
            retained.append({"path": source["file_path"], "reason": "旧库源文件保留；可选择同时移除旧库源资料。"})
        return {"files": [], "retained": retained,
                "source": {"available": False, "included": False,
                           "label": source["library_id"] if source else None}, "token_data": None}

    def _plan(self, identifier, include_source):
        case_id(identifier)
        if type(include_source) is not bool:
            raise DeletionError("是否移除旧库源资料必须为布尔值。")
        if not self.store.folder(identifier).exists():
            raise DeletionError("案例不存在，或已被彻底删除。", 404)
        index = self.store.read_index()
        case = self.store.read_case(identifier)
        if not case.get("archived"):
            raise DeletionError("只能彻底删除已归档案例，请先归档。")
        cases = [self.store.read_case(entry["case_id"]) for entry in index["cases"]]
        related = [item["case_id"] for item in cases if identifier in item.get("related_case_ids", [])]
        if related:
            raise DeletionError("其他案例仍关联此编号，请先处理关联关系：" + "、".join(related), 409)
        other_refs = {}
        for item in cases:
            if item["case_id"] == identifier:
                continue
            for raw in _references(item):
                other_refs.setdefault(_identity(raw), []).append(item["case_id"])
        record_files, record_dirs = self._tree(self.store.folder(identifier), self.root / "cases", "record")
        reading_files, reading_dirs = self._tree(self.library / identifier, self.library, "reading")
        files = record_files + reading_files
        for file in files:
            if _identity(file["path"]) in other_refs:
                raise DeletionError("案例目录内文件被其他案例引用，暂不能删除：" + file["path"], 409)
        retained, missing = [], []
        planned = {_identity(file["path"]) for file in files}
        assets = set()
        for snapshot in [case.get("current"), *(event.get("current") for event in case["events"])]:
            assets.update((snapshot or {}).get("assets", {}).values())
        for raw in sorted(assets):
            path = Path(raw)
            identity = _identity(path)
            if identity in planned:
                continue
            if identity in other_refs:
                retained.append({"path": raw, "reason": "其他案例的当前或历史记录仍引用此文件。"})
                continue
            try:
                _safe_path(path, self.evidence)
            except DeletionError:
                retained.append({"path": raw, "reason": "位于受管理资料目录之外，或路径含链接；保留原素材。"})
                continue
            if not path.exists():
                missing.append(raw)
                continue
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                retained.append({"path": raw, "reason": "附件不是普通文件，未纳入删除。"})
            elif info.st_nlink != 1:
                retained.append({"path": raw, "reason": "文件存在硬链接，保留以保护其他副本引用。"})
            else:
                files.append({"path": raw, "kind": "evidence", "bytes": info.st_size,
                              "stamp": _stamp(path), "root": str(self.evidence)})
                planned.add(identity)
        source_plan = self._source_plan(case, cases, include_source)
        marker_data, marker_stamp = self._markers()
        source_ids = sorted({(snapshot["source_case"]["library_id"], identifier)
                             for snapshot in [case.get("current"), *(event.get("current") for event in case["events"])]
                             if snapshot and snapshot.get("source_case")
                             and re.fullmatch(r"VSL-(?:LT|T[0-4])-\d{3}", snapshot["source_case"]["library_id"])})
        updates = [{"path": str(self.root / "cases" / "index.json"), "kind": "case_index", "label": "移除案例索引及检索内容。"},
                   {"path": str(self.library / "INDEX.md"), "kind": "reading_index", "label": "同步使用中案例目录。"},
                   {"path": str(self.library / "ARCHIVE.md"), "kind": "reading_index", "label": "移除归档目录中的案例条目。"}]
        updates.extend(source_plan.get("updates", []))
        if source_ids:
            updates.append({"path": str(self.root / "deleted_case_sources.json"), "kind": "deletion_marker",
                            "label": "记录来源编号，阻止旧案例再次自动导入；不保留正文。"})
        retained.extend(source_plan.get("retained", []))
        source_files = source_plan.get("files", [])
        source_deletions = {_identity(file["path"]) for file in source_files}
        retained = [entry for entry in retained if _identity(entry["path"]) not in source_deletions]
        warnings = list(source_plan.get("warnings", []))
        if missing:
            warnings.append(f"{len(missing)} 个登记附件已不存在。")
        public_files = [{key: file[key] for key in ("path", "kind", "bytes")} for file in files] + source_files
        public = {"case_id": identifier, "title": (case.get("current") or {}).get("title", case["title"]),
                  "revision": revision(case), "files": public_files, "retained": retained,
                  "total_bytes": sum(file["bytes"] for file in public_files),
                  "source": source_plan["source"], "updates": updates, "warnings": warnings}
        public["plan_token"] = digest({"plan": public, "files": files, "directories": record_dirs + reading_dirs,
                                      "missing": missing, "other_refs": other_refs,
                                      "source": source_plan.get("token_data"), "include_source": include_source,
                                      "markers": [marker_data, marker_stamp]})
        return public, {"files": files, "record_dirs": record_dirs, "reading_dirs": reading_dirs,
                        "source": source_plan, "case": case, "source_ids": source_ids, "marker_stamp": marker_stamp}

    def preview(self, case_id, include_source=False):
        return self._plan(case_id, include_source)[0]

    def _unlink(self, file):
        path = _safe_path(Path(file["path"]), Path(file["root"]))
        if _stamp(path, content=file["kind"] in {"record", "reading"}) != file["stamp"]:
            raise DeletionError("删除期间文件已变化，请重新查看删除范围：" + str(path), 409)
        if not path.is_file() or path.stat().st_nlink != 1:
            raise DeletionError("删除期间文件类型或链接数量发生变化：" + str(path), 409)
        path.unlink()

    def _empty_dirs(self, directories, root):
        for entry in sorted(directories, key=lambda item: len(Path(item["path"]).parts), reverse=True):
            path = _safe_path(Path(entry["path"]), root)
            path.rmdir()  # Never recursively remove a newly appeared file.

    def delete(self, case_id, revision, plan_token, confirmation, include_source=False):
        if confirmation != case_id:
            raise DeletionError("请输入完整案例编号确认彻底删除。")
        public, plan = self._plan(case_id, include_source)
        if revision != public["revision"] or plan_token != public["plan_token"]:
            raise DeletionError("案例或删除范围已变化，请重新查看删除清单后确认。", 409)
        records = [file for file in plan["files"] if file["kind"] == "record"]
        count = 0
        # Evidence and generated documents go first. If one is locked, the
        # complete case record is still present and can be retried safely.
        try:
            self._write_markers(plan["source_ids"], plan["marker_stamp"])
            for file in plan["files"]:
                if file["kind"] != "record":
                    self._unlink(file)
                    count += 1
            self._empty_dirs(plan["reading_dirs"], self.library)
            if self.source_handler:
                source_result = self.source_handler.execute(plan["source"])
                count += (source_result or {}).get("deleted_file_count", len(plan["source"].get("files", [])))
        except (OSError, StoreError) as exc:
            status = exc.status if isinstance(exc, DeletionError) else 500
            raise DeletionError(f"尚未删除案例记录；已清理 {count} 个资料文件。请处理问题后重新预览：{exc}", status) from exc
        # Retain record bytes only in memory for rollback of an unlink failure;
        # no content backup, trash directory or retained history is written.
        memory = {file["path"]: Path(file["path"]).read_bytes() for file in records}
        try:
            for file in records:
                self._unlink(file)
                count += 1
            self._empty_dirs(plan["record_dirs"], self.root / "cases")
        except (OSError, StoreError) as exc:
            restored = True
            for raw, content in memory.items():
                try:
                    path = _safe_path(Path(raw), self.root / "cases" / case_id)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if not path.exists():
                        with path.open("xb") as stream:
                            stream.write(content)
                            stream.flush()
                            os.fsync(stream.fileno())
                except (OSError, StoreError):
                    restored = False
            try:
                self.store.rebuild_index()
            except (OSError, StoreError):
                restored = False
            message = "案例记录已恢复，请重新预览后重试。" if restored else "案例记录未能完整恢复，请先检查本地记录并修复目录。"
            raise DeletionError(f"删除未完成，{message} 原因：{exc}", 500) from exc
        finally:
            memory.clear()
        warnings = []
        try:
            self.store.rebuild_index()
        except (OSError, StoreError) as exc:
            warnings.append("原始记录与列出的附件已删除，但案例索引中仍有残留内容，请重新同步完成清理。")
        if not warnings:
            try:
                render_library(self.root, self.library)
            except Exception:
                warnings.append("原始记录与列出的附件已删除，但阅读目录中仍有残留内容，请重新同步完成清理。")
        return {"deleted": True, "case_id": case_id, "deleted_file_count": count,
                "retained": public["retained"], "warning": " ".join(warnings) or None}
