"""Fresh file identity for parsed-record caches, including Windows ChangeTime."""
from __future__ import annotations

import os
from pathlib import Path


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


def file_identity(path):
    """Return device, inode, size, write time, creation/change time and native change time.

    Windows stat ctime is creation time and cannot detect an in-place write
    whose LastWriteTime is restored. Read native ChangeTime from a fresh
    handle; no file contents are read and no time-based expiry is needed.
    """
    path = Path(path)
    if os.name == "nt":
        with path.open("rb") as stream:
            metadata = os.fstat(stream.fileno())
            basic = _FileBasicInfo()
            handle = msvcrt.get_osfhandle(stream.fileno())
            if not _get_file_info(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
                raise ctypes.WinError(ctypes.get_last_error())
            created = getattr(metadata, "st_birthtime_ns", metadata.st_ctime_ns)
            return (metadata.st_dev, metadata.st_ino, metadata.st_size,
                    metadata.st_mtime_ns, created, basic.ChangeTime)
    metadata = path.stat()
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns, None)
