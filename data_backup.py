"""Portable, verified full-data archives. Does not include application code or credentials."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
from uuid import uuid4
import zipfile


FORMAT = "case-workbench-full-data"
VERSION = 1
SKILL_PATHS = ("cases", "sources", "references/products", "deleted_case_sources.json")
APP_PATHS = ("图文素材",)
RUNTIME_EXCLUDED = frozenset({"auth", "credentials", "thumbnail-cache", "cache", "__pycache__",
    "staging", "split-staging", "text-public-sync", "data-transfer"})
# Public access keeps this machine's tunnel token, login hashes and guest site here. Never exported, never
# replaced by an import; older archives that carried it stay importable and only skip it.
RUNTIME_PRIVATE = frozenset({"remote-access"})
TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".csv", ".tsv", ".yaml", ".yml", ".xml", ".srt", ".vtt", ".html"}
# These formats already compress their payload. Deflating gigabytes of finished
# videos wastes daily-backup time and rarely makes the archive any smaller.
COMPRESSED_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".mpeg", ".mpg",
    ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".avif", ".heic", ".heif", ".ico", ".zip", ".7z", ".rar", ".gz", ".bz2", ".xz", ".zst",
    ".xlsx", ".xlsm", ".docx", ".pptx", ".ods", ".odt", ".odp", ".pdf"})
CREDENTIAL_NAMES = frozenset({"auth.json", "credentials.json", ".credentials.json", ".netrc", "_netrc",
    ".npmrc", ".pypirc", ".git-credentials", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "identity",
    "id_ecdsa_sk", "id_ed25519_sk"})
PRIVATE_KEY_NAMES = frozenset({"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "identity", "id_ecdsa_sk", "id_ed25519_sk"})
CREDENTIAL_DIRECTORIES = frozenset({".ssh", ".aws", ".azure", ".kube", ".gnupg"})
CREDENTIAL_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".pkcs12", ".jks", ".keystore", ".ppk"})
CHUNK = 1024 * 1024
MAX_FILES = 250000
MAX_MANIFEST = 64 * 1024 * 1024
MAX_BYTES = 2 * 1024 ** 4
SQLITE_SNAPSHOT_TIMEOUT = 30
WINDOWS_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


class BackupError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def no_links(path, root):
    """The configured root may already resolve through a platform skill directory link."""
    path, root = Path(path), Path(root)
    if not path.is_relative_to(root):
        raise BackupError("数据文件超出了允许的目录。")
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if linked(current):
            raise BackupError(f"数据目录中存在链接，请先处理该链接：{current.name}")
    return path


def safe_name(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise BackupError("备份包包含不安全的文件路径。")
    parts = PurePosixPath(name).parts
    if (name.startswith("/") or "/".join(parts) != name or
            any(part in {"", ".", ".."} or ":" in part or part.rstrip(" .") != part
                or WINDOWS_RESERVED.match(part) or any(ord(c) < 32 for c in part) for part in parts)):
        raise BackupError("备份包包含不安全的文件路径。")
    return parts


def _quote(value):
    return '"' + value.replace('"', '""') + '"'


def _absolute_local(value):
    return isinstance(value, str) and not value.startswith(("\\\\", "//")) and (Path(value).is_absolute() or PureWindowsPath(value).is_absolute())


def _credential_reference(path):
    """Untrusted business text must not turn local login files into media assets."""
    name = path.name.casefold()
    return (name in CREDENTIAL_NAMES or name == ".env" or name.startswith(".env.") or
        any(name.startswith(key + ".") for key in PRIVATE_KEY_NAMES) or
        path.suffix.casefold() in CREDENTIAL_SUFFIXES or
        any(part.casefold() in CREDENTIAL_DIRECTORIES for part in path.parts))


def _logical_path(path, root, category):
    relative = path.relative_to(root)
    return category if not relative.parts else category + "/" + relative.as_posix()


def _alias_target(value):
    parts = safe_name(value)
    if parts[0] not in {"skill", "runtime", "app"}:
        raise BackupError("备份的路径别名清单无效。")
    return parts


def _json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result
    return json.loads(value, object_pairs_hook=pairs)


def _sqlite(path):
    with path.open("rb") as stream:
        return stream.read(16) == b"SQLite format 3\x00"


def _hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.digest()


def _sqlite_strings(path):
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(" + _quote(table) + ")")]
            # Scan each table once, without materializing blob/index payloads.
            selected = ["CASE WHEN typeof(" + _quote(column) + ") = 'text' THEN " + _quote(column) + " END" for column in columns]
            for row in connection.execute("SELECT " + ", ".join(selected) + " FROM " + _quote(table)):
                yield from (value for value in row if isinstance(value, str))
    finally:
        connection.close()


def _snapshot_sqlite(source, destination):
    deadline = time.monotonic() + SQLITE_SNAPSHOT_TIMEOUT

    def progress(status, remaining, total):
        if time.monotonic() >= deadline:
            raise BackupError(f"数据库快照超时，请结束占用后重试：{source.name}", 503)

    incoming = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=0)
    try:
        outgoing = sqlite3.connect(destination)
        try:
            # connect(timeout=...) does not bound backup's BUSY/LOCKED retries.
            incoming.backup(outgoing, pages=256, progress=progress, sleep=.05)
        finally:
            outgoing.close()
    finally:
        incoming.close()


def _json_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_strings(item)


class BackupManager:
    def __init__(self, skill_root, runtime_root, app_root=None, locks=(), validator=None, extra_sources=()):
        self.skill_root = Path(skill_root).resolve()
        self.runtime_root = Path(runtime_root).resolve()
        self.app_root = Path(app_root).resolve() if app_root else None
        if self.skill_root == self.runtime_root or self.skill_root.is_relative_to(self.runtime_root) or self.runtime_root.is_relative_to(self.skill_root):
            raise BackupError("案例目录和运行资料目录不能互相包含。")
        self.lock = threading.RLock()
        self.locks = list({id(lock): lock for lock in locks}.values())
        self.validator = validator
        candidates = tuple(dict.fromkeys(Path(path).expanduser().resolve() for path in extra_sources))
        self.extra_sources = tuple(path for path in candidates if not any(path != other and path.is_relative_to(other) for other in candidates))
        self.transfer_root = self.runtime_root / "data-transfer"
        self.on_progress = lambda message: None

    @contextmanager
    def business_locks(self):
        with ExitStack() as stack:
            for lock in self.locks:
                stack.enter_context(lock)
            yield

    @contextmanager
    def operation_lock(self, *, hold_business_locks=True):
        """Shared by the local app and its independent daily backup worker."""
        with self.lock, self.business_locks() if hold_business_locks else nullcontext():
            self.runtime_root.mkdir(parents=True, exist_ok=True)
            if self.transfer_root.exists() and linked(self.transfer_root):
                raise BackupError("备份工作目录不能是链接。")
            self.transfer_root.mkdir(exist_ok=True)
            mutex = self.transfer_root / "backup.lock"
            if mutex.exists() and linked(mutex):
                raise BackupError("备份锁文件不能是链接。")
            with mutex.open("a+b") as stream:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0, os.SEEK_END)
                    if stream.tell() == 0:
                        stream.write(b"0")
                        stream.flush()
                    deadline = time.monotonic() + 60
                    while True:
                        try:
                            stream.seek(0)
                            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise BackupError("另一个数据备份或导入操作仍在进行，请稍后重试。", 409)
                            time.sleep(.1)
                    try:
                        yield
                    finally:
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _runtime_entries(self):
        if not self.runtime_root.exists():
            return []
        return sorted(path.name for path in self.runtime_root.iterdir()
            if path.name.casefold() not in RUNTIME_EXCLUDED | RUNTIME_PRIVATE and not path.name.endswith(("-wal", "-shm")))

    def _walk(self, path, root, archive_path, directories):
        safe_name(archive_path)
        no_links(path, root)
        if path.is_dir():
            directories.add(archive_path)
            for child in sorted(path.iterdir()):
                yield from self._walk(child, root, archive_path + "/" + child.name, directories)
        elif path.is_file():
            if not path.name.endswith(("-wal", "-shm")):
                yield archive_path, path, root
        else:
            raise BackupError(f"无法备份这个数据文件：{path.name}")

    def _destination(self, destination):
        destination = Path(destination).expanduser().resolve()
        for relative in SKILL_PATHS:
            source = self.skill_root / relative
            if destination == source or destination.is_relative_to(source):
                raise BackupError("备份位置不能放在要备份的案例或素材目录里面。")
        if destination == self.skill_root or destination == self.runtime_root:
            raise BackupError("请选择独立的备份文件夹。")
        if destination.is_relative_to(self.runtime_root):
            relative = destination.relative_to(self.runtime_root)
            if not relative.parts or relative.parts[0].casefold() not in RUNTIME_EXCLUDED:
                raise BackupError("备份位置不能放在要备份的运行资料目录里面。")
        for source in (*((self.app_root / p for p in APP_PATHS) if self.app_root else ()), *self.extra_sources):
            if destination == source or destination.is_relative_to(source):
                raise BackupError("备份位置不能放在要备份的原始素材目录里面。")
        destination.mkdir(parents=True, exist_ok=True)
        return destination

    def create_archive(self, destination, kind="export", preferences=None):
        with self.operation_lock(hold_business_locks=False):
            return self._create_archive(destination, kind, preferences)

    def _archive_inputs(self):
        """Enumerate a consistent business view before metadata discovery or compression."""
        directories, external, aliases, extra_roots, included_sources, queue = set(), {}, {}, {}, {}, []
        runtime_entries = self._runtime_entries()
        for relative in SKILL_PATHS:
            path = self.skill_root / relative
            if path.exists():
                queue.extend(self._walk(path, self.skill_root, "skill/" + relative, directories))
        for relative in runtime_entries:
            queue.extend(self._walk(self.runtime_root / relative, self.runtime_root, "runtime/" + relative, directories))
        if self.app_root:
            for relative in APP_PATHS:
                path = self.app_root / relative
                if path.exists():
                    queue.extend(self._walk(path, self.app_root, "app/" + relative, directories))
        external_seen = {}
        for source in self.extra_sources:
            if not source.exists():
                continue  # Historical missing sales folders do not invalidate durable imported rows.
            if source != self.runtime_root and self.runtime_root.is_relative_to(source):
                # Walking it as an external folder would pack the excluded login and remote-access files.
                raise BackupError("原始销售资料目录不能包含运行资料目录。")
            included = None
            if source.is_relative_to(self.runtime_root):
                relative = source.relative_to(self.runtime_root)
                if relative.parts and relative.parts[0] in runtime_entries:
                    included = _logical_path(source, self.runtime_root, "runtime")
            elif source.is_relative_to(self.skill_root) and any(source == self.skill_root / p or source.is_relative_to(self.skill_root / p) for p in SKILL_PATHS):
                included = _logical_path(source, self.skill_root, "skill")
            elif self.app_root and any(source == self.app_root / p or source.is_relative_to(self.app_root / p) for p in APP_PATHS):
                included = _logical_path(source, self.app_root, "app")
            if included:
                included_sources[str(source)] = included
                continue
            if source.is_relative_to(self.runtime_root):
                raise BackupError("原始销售资料目录不能设置在登录资料或临时缓存目录里。")
            mapped = "external/" + hashlib.sha256(os.fsencode(str(source))).hexdigest()[:24] + "/" + source.name
            extra_roots[str(source)] = mapped
            for archive_name, original, root in self._walk(source, source.parent, mapped, directories):
                key = str(original)
                external[key] = archive_name
                external_seen[key] = archive_name
                queue.append((archive_name, original, root))
        return directories, external, aliases, extra_roots, included_sources, queue, runtime_entries, external_seen

    def _create_archive(self, destination, kind, preferences=None, *, business_locked=False):
        destination = self._destination(destination)
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        name = "case-workbench-" + re.sub(r"[^a-zA-Z0-9_-]", "", str(kind))[:30] + "-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8] + ".zip"
        final = destination / name
        temporary = destination / ("." + name + ".partial")
        seen, records, warnings, references_seen = set(), [], [], set()
        # Discover and validate path aliases from metadata before streaming large
        # media. Newly discovered external JSON is inserted before pending media.
        metadata_suffixes = {".json", ".sqlite", ".db"}
        with tempfile.TemporaryDirectory(prefix="backup-sqlite-", dir=self.transfer_root) as database_directory:
            try:
                snapshots = {}
                self.on_progress("正在建立业务资料快照")
                with self.business_locks() if not business_locked else nullcontext():
                    # Enumeration and metadata copies must see the same view:
                    # even a GET can migrate catalogs and create history files.
                    directories, external, aliases, extra_roots, included_sources, queue, runtime_entries, external_seen = self._archive_inputs()
                    queue.sort(key=lambda row: row[1].suffix.casefold() not in metadata_suffixes)
                    for number, (_, source, root) in enumerate(queue):
                        no_links(source, root)
                        snapshot = Path(database_directory) / str(number)
                        if _sqlite(source):
                            self.on_progress("正在保存数据库快照：" + source.name)
                            _snapshot_sqlite(source, snapshot)
                            snapshots[source] = (snapshot, True)
                        elif source.suffix.casefold() in TEXT_SUFFIXES:
                            shutil.copyfile(source, snapshot)
                            snapshots[source] = (snapshot, False)
                self.on_progress("正在整理素材引用并打包")
                with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
                    index = 0
                    while index < len(queue):
                        archive_name, source, root = queue[index]
                        index += 1
                        safe_name(archive_name)
                        if archive_name.casefold() in seen:
                            raise BackupError("数据文件存在大小写冲突，无法创建可移植的备份。")
                        seen.add(archive_name.casefold())
                        if len(seen) > MAX_FILES:
                            raise BackupError("备份文件数量超过支持范围。")
                        read_path, is_database = snapshots.get(source, (source, False))
                        if read_path == source:
                            no_links(source, root)
                            before = source.stat()
                            if _sqlite(source):
                                read_path = Path(database_directory) / (str(index) + ".sqlite")
                                _snapshot_sqlite(source, read_path)
                                is_database = True
                        self.on_progress(f"正在打包 {index}/{len(queue)}：{source.name}")
                        digest, count = hashlib.sha256(), 0
                        json_content = bytearray() if source.suffix.casefold() == ".json" else None
                        entry = zipfile.ZipInfo(archive_name, date_time=datetime.now().timetuple()[:6])
                        entry.compress_type = zipfile.ZIP_STORED if source.suffix.casefold() in COMPRESSED_SUFFIXES else zipfile.ZIP_DEFLATED
                        entry._compresslevel = 6
                        with read_path.open("rb") as content, archive.open(entry, "w", force_zip64=True) as output:
                            while chunk := content.read(CHUNK):
                                output.write(chunk)
                                digest.update(chunk)
                                count += len(chunk)
                                if json_content is not None:
                                    if count > MAX_MANIFEST:
                                        raise BackupError(f"JSON 数据文件过大：{source.name}")
                                    json_content.extend(chunk)
                        if read_path == source:
                            no_links(source, root)
                            after = source.stat()
                            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                                raise BackupError(f"备份期间文件发生变化，请重试：{source.name}", 409)
                        records.append({"path": archive_name, "size": count, "sha256": digest.hexdigest()})
                        strings = []
                        if json_content is not None:
                            try:
                                strings = _json_strings(_json(json_content.decode("utf-8-sig")))
                            except (ValueError, UnicodeError) as exc:
                                if kind != "pre-import":
                                    raise BackupError(f"JSON 数据无法读取：{source.name}") from exc
                                # A sound backup can repair a damaged live JSON catalog;
                                # the safety archive must still retain its original bytes.
                                warnings.append("原数据 JSON 无法解析，恢复包已保留原始文件：" + archive_name)
                        elif is_database:
                            strings = _sqlite_strings(read_path)
                        for value in strings:
                            # DB cells may contain serialized JSON with nested asset paths.
                            candidates = [value]
                            if value.startswith(("{", "[")):
                                try:
                                    candidates.extend(_json_strings(json.loads(value)))
                                except ValueError:
                                    pass
                            for candidate in candidates:
                                reference = Path(candidate)
                                if not reference.is_absolute() or candidate.startswith(("\\\\", "//")):
                                    continue
                                if candidate in references_seen:
                                    continue
                                references_seen.add(candidate)
                                if _credential_reference(reference):
                                    continue
                                # Platform skill links resolve to the canonical business root; keep
                                # that alias in the manifest instead of treating it as new media.
                                try:
                                    canonical = reference.resolve()
                                    if _credential_reference(canonical):
                                        continue
                                    exists = canonical.is_file()
                                    directory_exists = canonical.is_dir()
                                except (OSError, ValueError):
                                    # Historic notes and DB cells may contain a path-like
                                    # string that is not a valid local file name.
                                    continue
                                if not exists and not directory_exists:
                                    # Archived notes sometimes join several absolute paths
                                    # with prose punctuation; those are not filesystem aliases.
                                    continue
                                included = None
                                if canonical == self.skill_root or canonical.is_relative_to(self.skill_root) and any(canonical == self.skill_root / p or canonical.is_relative_to(self.skill_root / p) for p in SKILL_PATHS):
                                    included = _logical_path(canonical, self.skill_root, "skill")
                                elif canonical.is_relative_to(self.runtime_root):
                                    included = _logical_path(canonical, self.runtime_root, "runtime")
                                elif self.app_root and (canonical == self.app_root or any(canonical == self.app_root / p or canonical.is_relative_to(self.app_root / p) for p in APP_PATHS)):
                                    included = _logical_path(canonical, self.app_root, "app")
                                if included:
                                    if str(reference) != str(canonical):
                                        _alias_target(included)
                                        aliases[str(reference)] = included
                                    continue
                                if not exists:
                                    continue
                                no_links(canonical, Path(canonical.anchor))
                                key = str(reference)
                                if key in external:
                                    continue
                                canonical_key = str(canonical)
                                if canonical_key in external_seen:
                                    external[key] = external_seen[canonical_key]
                                    continue
                                mapped = "external/" + hashlib.sha256(os.fsencode(canonical_key)).hexdigest()[:24] + "/" + canonical.name
                                external[key] = mapped
                                external_seen[canonical_key] = mapped
                                safe_name(mapped)
                                directories.add(str(PurePosixPath(mapped).parent))
                                pending = (mapped, canonical, Path(canonical.anchor))
                                queue.insert(index, pending) if canonical.suffix.casefold() in metadata_suffixes else queue.append(pending)
                    manifest = {"format": FORMAT, "version": VERSION, "kind": str(kind), "created_at": created,
                        "source_roots": {"skill": str(self.skill_root), "runtime": str(self.runtime_root), "app": str(self.app_root) if self.app_root else None},
                        "skill_paths": list(SKILL_PATHS), "runtime_entries": runtime_entries,
                        "directories": sorted(directories), "external_files": external,
                        "path_aliases": aliases, "extra_source_roots": extra_roots,
                        "included_source_roots": included_sources,
                        "preferences": preferences or {}, "files": records, "bytes": sum(row["size"] for row in records)}
                    if warnings:
                        manifest["warnings"] = warnings
                    archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))
                # Windows _commit/fsync requires a writable file descriptor.
                with temporary.open("rb+") as stream:
                    os.fsync(stream.fileno())
                with zipfile.ZipFile(temporary) as check:
                    self._manifest(check)
                os.replace(temporary, final)
                return self._summary(manifest, final)
            except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
                raise BackupError(f"备份未完成：{exc}", 500) from exc
            finally:
                if temporary.exists():
                    temporary.unlink()

    @staticmethod
    def _summary(manifest, path):
        return {"path": str(path), "name": Path(path).name, "files": len(manifest["files"]),
            "bytes": manifest["bytes"], "created_at": manifest["created_at"], "kind": manifest.get("kind", "export"),
            "external_files": len(manifest.get("external_files", {})), "preferences": manifest.get("preferences", {}),
            "source_roots": manifest["source_roots"], "archive_bytes": Path(path).stat().st_size}

    @staticmethod
    def _manifest(archive):
        entries = archive.infolist()
        if not entries or len(entries) > MAX_FILES + 1:
            raise BackupError("备份包文件数量无效。")
        by_name = {}
        for entry in entries:
            safe_name(entry.filename)
            mode = entry.external_attr >> 16
            if entry.is_dir() or stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                raise BackupError("备份包包含链接或特殊文件。")
            key = entry.filename.casefold()
            if key in by_name:
                raise BackupError("备份包存在重复或大小写冲突的文件。")
            by_name[key] = entry
        info = by_name.get("manifest.json")
        if info is None or info.filename != "manifest.json" or info.file_size > MAX_MANIFEST:
            raise BackupError("这不是工作台的完整数据备份包。")
        try:
            manifest = _json(archive.read(info).decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise BackupError("备份清单无法读取。") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
            raise BackupError("备份格式或版本不受支持。")
        if not isinstance(manifest.get("created_at"), str):
            raise BackupError("备份创建时间无效。")
        try:
            datetime.fromisoformat(manifest["created_at"])
        except ValueError as exc:
            raise BackupError("备份创建时间无效。") from exc
        if manifest.get("skill_paths") != list(SKILL_PATHS) or not isinstance(manifest.get("runtime_entries"), list):
            raise BackupError("备份的数据范围不完整。")
        for entry in manifest["runtime_entries"]:
            parts = safe_name(entry)
            if len(parts) != 1 or entry.casefold() in RUNTIME_EXCLUDED:
                raise BackupError("备份清单包含不允许导入的目录。")
        if len({entry.casefold() for entry in manifest["runtime_entries"]}) != len(manifest["runtime_entries"]):
            raise BackupError("备份目录存在大小写冲突。")
        if (not isinstance(manifest.get("source_roots"), dict) or
                any(not _absolute_local(manifest["source_roots"].get(key)) for key in ("skill", "runtime")) or
                manifest["source_roots"].get("app") is not None and not _absolute_local(manifest["source_roots"].get("app"))):
            raise BackupError("备份来源目录信息无效。")
        if not isinstance(manifest.get("preferences", {}), dict) or not isinstance(manifest.get("files"), list):
            raise BackupError("备份清单结构无效。")
        declared, total = {}, 0
        for row in manifest["files"]:
            if (not isinstance(row, dict) or type(row.get("size")) is not int or row["size"] < 0 or
                    not isinstance(row.get("sha256"), str) or not re.fullmatch("[a-f0-9]{64}", row["sha256"])):
                raise BackupError("备份文件校验信息无效。")
            parts = safe_name(row.get("path"))
            allowed = (parts[0] == "skill" and any(row["path"] == "skill/" + p or row["path"].startswith("skill/" + p + "/") for p in SKILL_PATHS)
                or len(parts) > 1 and parts[0] == "runtime" and parts[1] in manifest["runtime_entries"]
                or len(parts) > 1 and parts[0] == "app" and parts[1] in APP_PATHS
                or len(parts) >= 3 and parts[0] == "external" and bool(re.fullmatch("[a-f0-9]{24}", parts[1])))
            if not allowed or row["path"].casefold() in declared:
                raise BackupError("备份文件超出数据范围或存在冲突。")
            info = by_name.get(row["path"].casefold())
            if info is None or info.filename != row["path"] or info.file_size != row["size"]:
                raise BackupError("备份文件缺失或大小不符。")
            declared[row["path"].casefold()] = row
            total += row["size"]
        if total > MAX_BYTES or type(manifest.get("bytes")) is not int or total != manifest["bytes"]:
            raise BackupError("备份总大小无效。")
        if set(by_name) != {"manifest.json", *declared}:
            raise BackupError("备份包包含清单以外的文件。")
        directories = manifest.get("directories", [])
        if not isinstance(directories, list) or len(directories) > MAX_FILES:
            raise BackupError("备份目录清单无效。")
        all_names = dict(declared)
        for directory in directories:
            parts = safe_name(directory)
            allowed = (parts[0] == "skill" and any(directory == "skill/" + p or directory.startswith("skill/" + p + "/") for p in SKILL_PATHS if p != "deleted_case_sources.json")
                or len(parts) > 1 and parts[0] == "runtime" and parts[1] in manifest["runtime_entries"]
                or len(parts) > 1 and parts[0] == "app" and parts[1] in APP_PATHS
                or len(parts) >= 2 and parts[0] == "external" and bool(re.fullmatch("[a-f0-9]{24}", parts[1])))
            if not allowed or directory.casefold() in all_names:
                raise BackupError("备份目录超出数据范围或与文件冲突。")
            all_names[directory.casefold()] = None
        for name in all_names:
            parts = name.split("/")
            if any("/".join(parts[:i]) in declared for i in range(1, len(parts))):
                raise BackupError("备份包存在文件和目录路径冲突。")
        namespace = {}
        for name in [*(row["path"] for row in manifest["files"]), *directories]:
            parts = name.split("/")
            for length in range(1, len(parts) + 1):
                prefix = "/".join(parts[:length])
                previous = namespace.setdefault(prefix.casefold(), prefix)
                if previous != prefix:
                    raise BackupError("备份包存在目录大小写冲突。")
        external = manifest.get("external_files", {})
        if not isinstance(external, dict) or any(not _absolute_local(key) or value.casefold() not in declared or not value.startswith("external/") for key, value in external.items() if isinstance(value, str)) or any(not isinstance(value, str) for value in external.values()):
            raise BackupError("备份的外部素材清单无效。")
        if set(external.values()) != {row["path"] for row in declared.values() if row["path"].startswith("external/")}:
            raise BackupError("备份的外部素材清单不完整。")
        aliases = manifest.get("path_aliases", {})
        if not isinstance(aliases, dict) or any(not _absolute_local(key) or not isinstance(value, str) for key, value in aliases.items()):
            raise BackupError("备份的路径别名清单无效。")
        for value in aliases.values():
            _alias_target(value)
        extra_roots = manifest.get("extra_source_roots", {})
        if not isinstance(extra_roots, dict) or any(not _absolute_local(key) or not isinstance(value, str) or not value.startswith("external/") for key, value in extra_roots.items()):
            raise BackupError("备份的原始数据目录无效。")
        for value in extra_roots.values():
            safe_name(value)
        included_sources = manifest.get("included_source_roots", {})
        if (not isinstance(included_sources, dict) or any(not _absolute_local(key) or not isinstance(value, str)
                or value not in directories or not value.startswith(("skill/", "runtime/", "app/")) for key, value in included_sources.items())):
            raise BackupError("备份的已迁移原始数据目录无效。")
        return manifest

    @staticmethod
    def _without_private(manifest):
        """What an import restores: an older archive's remote-access entries are verified but never extracted."""
        def private(name):
            parts = PurePosixPath(name).parts
            return len(parts) > 1 and parts[0] == "runtime" and parts[1].casefold() in RUNTIME_PRIVATE
        files = [row for row in manifest["files"] if not private(row["path"])]
        return {**manifest, "files": files, "bytes": sum(row["size"] for row in files),
            "runtime_entries": [entry for entry in manifest["runtime_entries"] if entry.casefold() not in RUNTIME_PRIVATE],
            "directories": [directory for directory in manifest.get("directories", []) if not private(directory)],
            "included_source_roots": {source: target for source, target in manifest.get("included_source_roots", {}).items()
                if not private(target)}}

    def _extract_verified(self, path, stage=None):
        try:
            with zipfile.ZipFile(path) as archive:
                manifest = self._manifest(archive)
                restored = self._without_private(manifest)
                kept = {row["path"] for row in restored["files"]}
                if stage is not None:
                    for directory in restored["directories"]:
                        (stage / directory).mkdir(parents=True, exist_ok=True)
                for row in manifest["files"]:
                    digest, count = hashlib.sha256(), 0
                    destination = stage / row["path"] if stage is not None and row["path"] in kept else None
                    if destination is not None:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                    with ExitStack() as stack:
                        incoming = stack.enter_context(archive.open(row["path"]))
                        outgoing = stack.enter_context(destination.open("xb")) if destination is not None else None
                        while chunk := incoming.read(CHUNK):
                            count += len(chunk)
                            if count > row["size"]:
                                raise BackupError("备份解压大小与清单不符。")
                            digest.update(chunk)
                            if outgoing:
                                outgoing.write(chunk)
                    if count != row["size"] or digest.hexdigest() != row["sha256"]:
                        raise BackupError("备份文件校验失败，原数据没有被修改。")
                return restored
        except (OSError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
            raise BackupError(f"备份包无法读取：{exc}") from exc

    def inspect_archive(self, path):
        path = Path(path).resolve()
        return self._summary(self._extract_verified(path), path)

    @staticmethod
    def _replacements(manifest, skill_root, runtime_root, app_root=None):
        mappings = [(source, str(runtime_root / "restored-assets" / Path(*PurePosixPath(target).parts[1:])))
                    for source, target in manifest.get("external_files", {}).items()]
        mappings.extend([(manifest["source_roots"]["skill"], str(skill_root)), (manifest["source_roots"]["runtime"], str(runtime_root))])
        if app_root and manifest["source_roots"].get("app"):
            mappings.append((manifest["source_roots"]["app"], str(app_root)))
        roots = {"skill": skill_root, "runtime": runtime_root, "app": app_root}
        for source, target in manifest.get("path_aliases", {}).items():
            parts = PurePosixPath(target).parts
            if roots[parts[0]]:
                mappings.append((source, str(roots[parts[0]] / Path(*parts[1:]))))
        mappings.extend((source, str(runtime_root / "restored-assets" / Path(*PurePosixPath(target).parts[1:])))
            for source, target in manifest.get("extra_source_roots", {}).items())
        for source, target in manifest.get("included_source_roots", {}).items():
            parts = PurePosixPath(target).parts
            if roots[parts[0]]:
                mappings.append((source, str(roots[parts[0]] / Path(*parts[1:]))))
        replacements = {}
        for source, target in mappings:
            for before, after in ((source, target), (source.replace("\\", "/"), target.replace("\\", "/")), (source.replace("/", "\\"), target.replace("/", "\\"))):
                if before == after:
                    continue
                replacements[before] = after
                for ascii_only in (True, False):
                    replacements[json.dumps(before, ensure_ascii=ascii_only)[1:-1]] = json.dumps(after, ensure_ascii=ascii_only)[1:-1]
        return sorted(replacements.items(), key=lambda row: len(row[0]), reverse=True)

    @staticmethod
    def _text_mapper(replacements):
        # One pass prevents a destination nested under a former root from being remapped again.
        if not replacements:
            return lambda text: text
        mapping = dict(replacements)
        pattern = re.compile("(?:" + "|".join(re.escape(key) for key in mapping) + r")(?=$|[/\\\s\"'\],)}:;#<>])")
        return lambda text: pattern.sub(lambda match: mapping[match.group(0)], text)

    @staticmethod
    def _remap_text(text, replacements):
        return BackupManager._text_mapper(replacements)(text)

    def _remap(self, stage, manifest):
        replacements = self._replacements(manifest, self.skill_root, self.runtime_root, self.app_root)
        remap = self._text_mapper(replacements)
        publication_documents, publication_revisions = [], {}
        publication_stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        for row in manifest["files"]:
            path = stage / row["path"]
            if path.suffix.casefold() in TEXT_SUFFIXES:
                try:
                    raw = path.read_bytes()
                    text = raw.decode("utf-8")
                except UnicodeError:
                    continue
                remapped = remap(text)
                publication_document = row["path"].startswith("runtime/production/video-publications/") and path.suffix.casefold() == ".json"
                if publication_document:
                    # A portable restore changes local paths inside otherwise frozen
                    # review snapshots. Verify the original before signing that rewrite.
                    from workbench_video_publish import remap_publication_document
                    try:
                        original, mapped = _json(text), _json(remapped)
                        mapped = remap_publication_document(original, mapped, publication_stamp, publication_revisions)
                    except ValueError as exc:
                        raise BackupError("视频发布快照无法通过恢复核对。") from exc
                    if mapped != original:
                        remapped = json.dumps(mapped, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
                if remapped != text:
                    encoded = remapped.encode("utf-8")
                    path.write_bytes(encoded)
                    if path.parent.name == "backups" and re.fullmatch("[a-f0-9]{64}", path.stem):
                        renamed = path.with_name(hashlib.sha256(encoded).hexdigest() + path.suffix)
                        if renamed != path:
                            if renamed.exists():
                                if renamed.read_bytes() != encoded:
                                    raise BackupError("恢复的目录历史备份发生内容冲突。")
                                path.unlink()
                            else:
                                os.replace(path, renamed)
                            path = renamed
                if publication_document:
                    publication_documents.append(path)
            elif _sqlite(path):
                connection = sqlite3.connect(path)
                try:
                    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                    for table in tables:
                        # Use SQLite's scalar function to preserve non-text values and WITHOUT ROWID tables.
                        connection.create_function("restore_path", 1, lambda value: remap(value) if isinstance(value, str) else value)
                        columns = [row[1] for row in connection.execute("PRAGMA table_info(" + _quote(table) + ")")]
                        for column in columns:
                            quoted = _quote(column)
                            connection.execute("UPDATE " + _quote(table) + " SET " + quoted + " = restore_path(" + quoted + ") WHERE typeof(" + quoted + ") = 'text' AND " + quoted + " != restore_path(" + quoted + ")")
                    connection.commit()
                finally:
                    connection.close()
        if publication_documents:
            from workbench_video_publish import remap_publication_revisions
            for path in set(publication_documents):
                before = path.read_bytes()
                value = _json(before.decode("utf-8"))
                previous = json.dumps(value, ensure_ascii=False, sort_keys=True)
                remap_publication_revisions(value, publication_revisions)
                if json.dumps(value, ensure_ascii=False, sort_keys=True) != previous:
                    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
                    path.write_bytes(encoded)
                    if path.parent.name == "backups" and re.fullmatch("[a-f0-9]{64}", path.stem):
                        renamed = path.with_name(hashlib.sha256(encoded).hexdigest() + path.suffix)
                        if renamed != path:
                            os.replace(path, renamed)
        for target in set(manifest.get("external_files", {}).values()):
            relative = PurePosixPath(target)
            destination = stage / "runtime" / "restored-assets" / Path(*relative.parts[1:])
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = stage / target
            if destination.exists():
                # A second export may already include a prior restored-assets file.
                if destination.stat().st_size != source.stat().st_size or _hash_file(destination) != _hash_file(source):
                    raise BackupError("导入素材与已有恢复素材发生冲突。")
                source.unlink()
            else:
                os.replace(source, destination)

    def _validate(self, stage):
        expected = {"directions": (("selling_points", "directions", "video_links"), ("requests",)),
            "creation": (("scripts", "materials", "theme_links", "plans"), ("requests",)),
            "production": (("voices", "readings", "jobs"), ("defaults", "requests"))}
        if (stage / "runtime").exists():
            for incoming in (stage / "runtime").iterdir():
                live = self.runtime_root / incoming.name
                # A short (8.3) alias such as REMOTE~1 would otherwise replace a local-only folder.
                if live.exists() and live.resolve().name.casefold() in RUNTIME_EXCLUDED | RUNTIME_PRIVATE:
                    raise BackupError("备份清单包含不允许导入的目录。")
        for tree in (stage / "skill", stage / "runtime"):
            if not tree.exists():
                tree.mkdir()
            for path in tree.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix.casefold() == ".json":
                    try:
                        value = _json(path.read_text(encoding="utf-8-sig"))
                    except (ValueError, UnicodeError) as exc:
                        raise BackupError(f"导入数据 JSON 无效：{path.name}") from exc
                    relative = path.relative_to(stage / "runtime") if tree.name == "runtime" else None
                    if relative and len(relative.parts) == 2 and relative.parts[1] == "catalog.json" and relative.parts[0] in expected:
                        lists, objects = expected[relative.parts[0]]
                        if (not isinstance(value, dict) or value.get("schema_version") != 1 or
                                any(not isinstance(value.get(key), list) for key in lists) or
                                any(not isinstance(value.get(key), dict) for key in objects)):
                            raise BackupError("导入的业务目录结构无效：" + relative.parts[0])
                elif _sqlite(path):
                    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                    try:
                        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                            raise BackupError("导入数据库完整性检查失败。")
                    finally:
                        connection.close()
                elif path.name in {"reviews.sqlite", "performance.sqlite"}:
                    raise BackupError("导入数据库格式无效。")
        if self.validator:
            self.validator(stage / "skill", stage / "runtime")

    def _apply(self, stage, rollback):
        moves = []
        try:
            runtime_new = [path.name for path in (stage / "runtime").iterdir() if path.name.casefold() not in RUNTIME_PRIVATE]
            selections = [(self.skill_root / relative, stage / "skill" / relative, rollback / "skill" / relative) for relative in SKILL_PATHS]
            if self.app_root:
                selections.extend((self.app_root / relative, stage / "app" / relative, rollback / "app" / relative) for relative in APP_PATHS)
            elif (stage / "app").exists():
                raise BackupError("这个备份包含图文素材，导入时需要配置工作台目录。")
            selections.extend((self.runtime_root / relative, stage / "runtime" / relative, rollback / "runtime" / relative)
                for relative in sorted(set(self._runtime_entries()) | set(runtime_new)))
            for live, incoming, previous in selections:
                root = self.skill_root if live.is_relative_to(self.skill_root) else self.runtime_root if live.is_relative_to(self.runtime_root) else self.app_root
                if live.exists():
                    no_links(live, root)
                    previous.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(live, previous)
                    moves.append(("old", live, previous))
                if incoming.exists():
                    live.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(incoming, live)
                    moves.append(("new", live, incoming))
        except Exception as exc:
            failures = []
            for kind, live, saved in reversed(moves):
                try:
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(live, saved) if kind == "new" else os.replace(saved, live)
                except OSError as rollback_error:
                    failures.append(str(rollback_error))
            if failures:
                raise BackupError("导入失败，自动回滚未完全完成；请保留导入前恢复包，并检查：" + str(rollback), 500) from exc
            if rollback.exists():
                shutil.rmtree(rollback)
            raise BackupError("导入失败，已自动恢复原数据。", 500) from exc

    def restore_archive(self, path, recovery_directory, recovery_preferences=None):
        path = Path(path).resolve()
        with self.operation_lock():
            temporary = Path(tempfile.mkdtemp(prefix="restore-", dir=self.transfer_root))
            completed = False
            recovery = None
            try:
                stage = temporary / "incoming"
                stage.mkdir()
                manifest = self._extract_verified(path, stage)
                self._remap(stage, manifest)
                self._validate(stage)
                recovery = self._create_archive(recovery_directory, "pre-import", recovery_preferences, business_locked=True)
                self._apply(stage, temporary / "rollback")
                completed = True
                return {**self._summary(manifest, path), "recovery_path": recovery["path"],
                    "preferences": manifest.get("preferences", {}), "restored": True,
                    "extra_source_roots": [str(self.runtime_root / "restored-assets" / Path(*PurePosixPath(target).parts[1:]))
                        for target in manifest.get("extra_source_roots", {}).values()] +
                        [str({"skill": self.skill_root, "runtime": self.runtime_root, "app": self.app_root}[PurePosixPath(target).parts[0]] /
                             Path(*PurePosixPath(target).parts[1:])) for target in manifest.get("included_source_roots", {}).values()]}
            except (OSError, sqlite3.Error) as exc:
                raise BackupError(f"导入未完成，原数据保持不变：{exc}", 500) from exc
            finally:
                # Failed rollback retains original directory copies for manual recovery.
                if completed or not (temporary / "rollback").exists():
                    shutil.rmtree(temporary)
