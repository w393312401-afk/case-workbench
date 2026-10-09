"""Stream local uploads and keep an independent video copy for each case."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from uuid import uuid4

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v"}
MAX_VIDEO_BYTES = 2 * 1024**3
BLOCK_BYTES = 1024 * 1024


class VideoFileError(ValueError):
    pass


def no_links(path):
    """Reject junctions and all reparse points before accessing local files."""
    path = Path(path)
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")):
        raise VideoFileError("视频必须使用本机文件的完整路径。")
    for cursor in (path, *path.parents):
        if cursor.is_symlink() or getattr(cursor, "is_junction", lambda: False)():
            raise VideoFileError("视频路径含文件链接或目录联接，请选择实际文件。")
        if cursor.exists() and getattr(cursor.lstat(), "st_file_attributes", 0) & 0x400:
            raise VideoFileError("视频路径含重解析链接，请选择实际文件。")
    return path


def safe_name(raw):
    # Do not accept browser filenames as directory paths, drive names, or ADS.
    name = re.sub(r'[^\w.() -]', "_", str(raw))[-100:].strip(" .")
    if not name or name in {".", ".."}:
        raise VideoFileError("文件名无效，请重新选择文件。")
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name, re.I):
        name = "video_" + name
    return name


def _stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


class ManagedVideoFiles:
    def __init__(self, runtime):
        self.runtime = no_links(Path(runtime)).resolve()
        self.imports = self.runtime / "evidence" / "workbench-imports"
        self.cases = self.runtime / "evidence" / "cases"

    def safe(self, path):
        path = no_links(Path(path))
        if not path.is_relative_to(self.runtime) or not path.resolve().is_relative_to(self.runtime):
            raise VideoFileError("视频存放位置超出案例资料目录。")
        return path

    def _new_folder(self, root):
        self.safe(root).mkdir(parents=True, exist_ok=True)
        self.safe(root)
        while True:
            folder = self.safe(root / uuid4().hex)
            try:
                folder.mkdir()
                return folder
            except FileExistsError:
                continue

    def _write(self, stream, length, folder, name):
        """Publish a complete file only; never hold the video in memory."""
        target = self.safe(folder / name)
        pending = self.safe(folder / ".uploading")
        try:
            remaining = length
            with pending.open("xb") as output:
                while remaining:
                    chunk = stream.read(min(BLOCK_BYTES, remaining))
                    if not chunk:
                        raise VideoFileError("视频传输或复制中断，请重新选择文件。")
                    output.write(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            self.safe(target)
            if target.exists():
                raise VideoFileError("视频保存位置已被占用，请重试。")
            pending.rename(target)
            return target
        except Exception:
            # This directory was created by this call and has not been published.
            self.safe(pending).unlink(missing_ok=True)
            self._empty_folder(folder)
            raise

    def _empty_folder(self, folder):
        try:
            self.safe(folder).rmdir()
        except OSError:
            pass

    def stage(self, stream, length, raw_name):
        if length <= 0:
            raise VideoFileError("不能上传空文件。")
        name = safe_name(raw_name)
        folder = self._new_folder(self.imports)
        return self._write(stream, length, folder, name)

    def _shape(self, path, root, parts):
        try:
            path = self.safe(path)
            relative = path.relative_to(root)
            return relative.parts if len(relative.parts) == parts else None
        except (ValueError, OSError):
            return None

    def temporary(self, path):
        parts = self._shape(path, self.imports, 2)
        return bool(parts and re.fullmatch(r"[0-9a-f]{32}", parts[0]) and parts[1] != ".uploading")

    def owner(self, path):
        parts = self._shape(path, self.cases, 4)
        if (parts and re.fullmatch(r"PVR-[0-9a-f]{12}", parts[0]) and parts[1] == "videos"
                and re.fullmatch(r"[0-9a-f]{32}", parts[2]) and parts[3] != ".uploading"):
            return parts[0]
        return None

    def info(self, path, identifier=None):
        owner = self.owner(path)
        temporary = self.temporary(path)
        return {"managed": bool(owner or temporary), "owned": bool(owner and owner == identifier),
                "temporary": temporary}

    def copy_for_case(self, source, identifier):
        if not re.fullmatch(r"PVR-[0-9a-f]{12}", identifier):
            raise VideoFileError("案例编号无效。")
        source = no_links(Path(source))
        if source.suffix.lower() not in VIDEO_EXTENSIONS or not source.is_file():
            raise VideoFileError("视频不存在或格式不支持，请重新选择。")
        before = source.stat()
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_VIDEO_BYTES:
            raise VideoFileError("请选择非空的普通视频文件，最大支持 2GB。")
        if self.owner(source) == identifier and before.st_nlink == 1:
            return source, False
        folder = self._new_folder(self.cases / identifier / "videos")
        created = None
        try:
            with source.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if _stamp(opened) != _stamp(before):
                    raise VideoFileError("原视频正在变化，请稍后重新保存。")
                created = self._write(stream, before.st_size, folder, safe_name(source.name))
                if _stamp(os.fstat(stream.fileno())) != _stamp(before):
                    raise VideoFileError("复制期间原视频发生变化，请重新保存。")
            no_links(source)
            if _stamp(source.stat()) != _stamp(before):
                raise VideoFileError("复制期间原视频发生变化，请重新保存。")
            return created, True
        except Exception:
            if created:
                self.remove_created(created, identifier)
            self._empty_folder(folder)
            raise

    def remove_created(self, path, identifier):
        """Rollback only the exact unpublished file returned by copy_for_case."""
        path = self.safe(path)
        if self.owner(path) != identifier:
            raise VideoFileError("不能清理其他案例或外部视频。")
        if path.exists() and path.stat().st_nlink != 1:
            return False
        path.unlink(missing_ok=True)
        self._empty_folder(path.parent)
        return True

    def cleanup_stage(self, path, store):
        """Only remove the selected upload, after every saved reference is checked."""
        path = Path(path)
        if not self.temporary(path) or not path.is_file() or path.stat().st_nlink != 1:
            return False
        before = path.stat()
        identity = os.path.normcase(str(path.resolve()))
        for entry in store.read_index()["cases"]:
            case = store.read_case(entry["case_id"])
            references = set()
            for snapshot in [case.get("current"), *(event.get("current") for event in case["events"])]:
                snapshot = snapshot or {}
                references.update(snapshot.get("assets", {}).values())
                if snapshot.get("source_case"):
                    references.add(snapshot["source_case"]["file_path"])
            original_source = case.get("original", {}).get("source")
            if isinstance(original_source, str) and Path(original_source).is_absolute():
                references.add(original_source)
            if any(os.path.normcase(str(Path(raw).resolve())) == identity for raw in references):
                return False
        path = self.safe(path)
        after = path.stat()
        if _stamp(after) != _stamp(before) or after.st_nlink != 1 or not stat.S_ISREG(after.st_mode):
            return False
        path.unlink()
        self._empty_folder(path.parent)
        return True
