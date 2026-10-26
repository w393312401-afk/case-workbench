"""Bounded file-integrity receipts, with fresh readable handles on every lookup.

Only hashes and small successful-check keys are retained, never media bytes.
Windows ChangeTime detects in-place edits even when LastWriteTime is restored.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat
import threading


class FileSizeError(OSError):
    pass


class DigestMismatchError(ValueError):
    pass


if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in
                    ("CreationTime", "LastAccessTime", "LastWriteTime", "ChangeTime")] + [("FileAttributes", wintypes.DWORD)]

    _get_file_info = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    _get_file_info.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _get_file_info.restype = wintypes.BOOL

    def _change_time(stream):
        value = _FileBasicInfo()
        handle = msvcrt.get_osfhandle(stream.fileno())
        if not _get_file_info(handle, 0, ctypes.byref(value), ctypes.sizeof(value)):
            raise ctypes.WinError(ctypes.get_last_error())
        return value.ChangeTime
else:
    def _change_time(stream):
        return None


def _metadata_key(metadata):
    # CPython 3.12 Windows stat() reports creation as ctime, while fstat()
    # can report ChangeTime there. BirthTime is the consistent identity field;
    # the native ChangeTime is added separately to every handle signature.
    created = getattr(metadata, "st_birthtime_ns", metadata.st_ctime_ns) if os.name == "nt" else metadata.st_ctime_ns
    return (metadata.st_dev, metadata.st_ino, created, metadata.st_mtime_ns,
            metadata.st_size, metadata.st_mode, metadata.st_nlink)


class FileIntegrityCache:
    def __init__(self, max_entries=1024):
        self.max_entries = max_entries
        self._hashes = OrderedDict()
        self._checks = OrderedDict()
        self._lock = threading.RLock()

    def clear(self):
        with self._lock:
            self._hashes.clear()
            self._checks.clear()

    def _lookup(self, cache, key, signature):
        with self._lock:
            cached = cache.get(key)
            if cached is None or cached[0] != signature:
                return None
            cache.move_to_end(key)
            return cached[1]

    def _remember(self, cache, key, signature, value):
        with self._lock:
            cache[key] = (signature, value)
            cache.move_to_end(key)
            while len(cache) > self.max_entries:
                cache.popitem(last=False)

    @contextmanager
    def _open(self, path, max_bytes=None):
        path = Path(os.path.abspath(path))
        for cursor in (path, *path.parents):
            metadata = cursor.lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise OSError("media path contains a reparse link")
            if cursor == path:
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise OSError("media must be a non-shared regular file")
                initial = _metadata_key(metadata)
        # Opening even a cached file checks current access permissions and the
        # actual object behind the path, rather than trusting a past successful read.
        with path.open("rb") as stream:
            metadata = os.fstat(stream.fileno())
            identity = _metadata_key(metadata)
            if identity != initial:
                raise OSError("media changed while opening")
            if max_bytes is not None and metadata.st_size > max_bytes:
                raise FileSizeError("media exceeds its size limit")
            signature = (*identity, _change_time(stream))
            yield str(path), stream, signature
            # Check both the opened object and the pathname; a replaced file
            # must not inherit a receipt generated from the old open handle.
            final = (*_metadata_key(os.fstat(stream.fileno())), _change_time(stream))
            if final != signature or _metadata_key(path.lstat()) != identity:
                raise OSError("media changed during integrity verification")

    @staticmethod
    def _digest(stream):
        stream.seek(0)
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()

    def sha256(self, path, *, max_bytes=None):
        with self._open(path, max_bytes) as (key, stream, signature):
            digest = self._lookup(self._hashes, key, signature)
            if digest is None:
                digest = self._digest(stream)
        self._remember(self._hashes, key, signature, digest)
        return digest

    def check(self, path, declaration, expected_sha256, validator=None):
        """Reuse only a successful validation of this exact declaration and file."""
        with self._open(path) as (key, stream, signature):
            check_key = (key, declaration, expected_sha256)
            complete = self._lookup(self._checks, check_key, signature)
            digest = self._lookup(self._hashes, key, signature)
            if complete is None:
                if digest is None:
                    digest = self._digest(stream)
                if digest != expected_sha256:
                    raise DigestMismatchError("media hash differs from its saved declaration")
                if validator is not None:
                    stream.seek(0)
                    validator(stream)
        # Exceptions, corruption and concurrent changes never publish success.
        if digest is not None:
            self._remember(self._hashes, key, signature, digest)
        self._remember(self._checks, check_key, signature, True)


INTEGRITY_CACHE = FileIntegrityCache()
