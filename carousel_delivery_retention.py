"""Delete only package-owned files below a verified production directory."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import stat

from elevenlabs_client import VoiceError

CAROUSEL = re.compile(r"CAR-[0-9a-f]{12}")
DELIVERY = re.compile(r"DLV-[0-9a-f]{12}")
RECEIPT = re.compile(r"request-results/[0-9a-f]{64}\.json")


def _regular_path(path):
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise VoiceError(409, "成品包清理遇到目录链接，未删除链接及其目标。")


def delivery_folder(root, carousel_id, delivery_id):
    if not CAROUSEL.fullmatch(str(carousel_id)) or not DELIVERY.fullmatch(str(delivery_id)):
        raise VoiceError(400, "成品包清理编号无效。")
    root = Path(root).absolute()
    _regular_path(root)
    expected = root.resolve()
    current = root
    for part in ("carousels", carousel_id, "deliveries", delivery_id):
        current = current / part
        _regular_path(current)
        if current.resolve().parent != expected:
            raise VoiceError(409, "成品包清理路径超出所属目录，未删除。")
        expected = current.resolve()
    return current


def folder_size(folder):
    count = size = 0
    if not folder.exists():
        return count, size
    if not folder.is_dir():
        raise VoiceError(409, "成品包目录格式无效，未删除。")
    for directory, dirs, files in os.walk(folder, followlinks=False):
        for name in (*dirs, *files):
            path = Path(directory) / name
            _regular_path(path)
            if path.is_file():
                count += 1
                size += path.stat().st_size
    return count, size


def remove_delivery_folder(root, carousel_id, delivery_id):
    folder = delivery_folder(root, carousel_id, delivery_id)
    count, size = folder_size(folder)
    if folder.exists():
        # Every absolute target and descendant was checked before recursive removal.
        def writable(function, path, _):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            function(path)
        shutil.rmtree(folder, onerror=writable)
    return count, size


def receipt_file(root, relative):
    if not isinstance(relative, str) or not RECEIPT.fullmatch(relative):
        raise VoiceError(409, "成品包回执清理路径无效，未删除。")
    root = Path(root).absolute()
    _regular_path(root)
    parent = root / "request-results"
    _regular_path(parent)
    target = root / relative
    _regular_path(target)
    if parent.resolve().parent != root.resolve() or target.resolve().parent != parent.resolve():
        raise VoiceError(409, "成品包回执超出所属目录，未删除。")
    return target
