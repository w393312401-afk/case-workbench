"""Retire package data inside verified content-addressed production backups."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from carousel_delivery_retention import _regular_path
from elevenlabs_client import VoiceError
from production_store import CAROUSEL_ID, DELIVERY_ID, scrub_retired_delivery_data

BACKUP_NAME = re.compile(r"([0-9a-f]{64})\.json")


def _backup_directory(production_root):
    root = Path(production_root).absolute()
    for ancestor in (*reversed(root.parents), root):
        _regular_path(ancestor)
    if root.exists() and not root.is_dir():
        raise VoiceError(409, "制作资料根目录格式无效，未清理备份。")
    folder = root / "backups"
    _regular_path(folder)
    if folder.resolve().parent != root.resolve() or folder.exists() and not folder.is_dir():
        raise VoiceError(409, "备份目录不属于当前制作资料目录，未清理。")
    return root, folder


def _checked_backup(path, folder):
    _regular_path(folder)
    _regular_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.resolve().parent != folder.resolve():
        raise VoiceError(409, "备份不是所属目录中的普通文件，未清理。")
    return info


def cleanup_delivery_backups(production_root, carousel_id, old_ids, *, dry_run=False):
    """Rewrite affected hash-named backups before removing their old snapshots.

    ``files`` counts retired JSON files removed (or planned in a dry run).
    ``bytes`` is removed bytes minus newly created backup bytes, so it reports
    net released storage. Receipt files are never deleted here: returned refs
    must be checked against the current catalog and every remaining backup.
    Call while holding the production catalog transaction lock.
    """
    if (not isinstance(carousel_id, str) or not CAROUSEL_ID.fullmatch(carousel_id) or
            not isinstance(old_ids, (list, tuple)) or any(not isinstance(value, str) or not DELIVERY_ID.fullmatch(value)
                                                       for value in old_ids) or type(dry_run) is not bool):
        raise VoiceError(400, "历史成品包备份清理参数无效。")
    root, folder = _backup_directory(production_root)
    result = {"files": 0, "bytes": 0, "warnings": [], "receipt_refs": []}
    if not old_ids or not folder.exists():
        return result
    refs, planned_targets = set(), set()
    for path in sorted(folder.iterdir()):
        match = BACKUP_NAME.fullmatch(path.name)
        if not match:
            continue
        try:
            _backup_directory(root)
            original_info = _checked_backup(path, folder)
            original = path.read_bytes()
            if hashlib.sha256(original).hexdigest() != match[1]:
                raise VoiceError(409, "备份内容与文件名校验值不一致，未改写。")
            data = json.loads(original)
            if not isinstance(data, dict):
                raise VoiceError(409, "备份资料格式无效，未改写。")
            cleanup = scrub_retired_delivery_data(data, carousel_id, old_ids)
            if not cleanup["changed"]:
                continue
            encoded = (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                  allow_nan=False) + "\n").encode("utf-8")
            target = folder / (hashlib.sha256(encoded).hexdigest() + ".json")
            _regular_path(target)
            already_exists = target.exists()
            target_info = None
            if already_exists:
                target_info = _checked_backup(target, folder)
                if target.read_bytes() != encoded:
                    raise VoiceError(409, "清理后的备份文件名已被不同内容占用，保留原备份。")
            add_bytes = 0 if already_exists or target in planned_targets else len(encoded)
            if dry_run:
                planned_targets.add(target)
                result["files"] += 1
                result["bytes"] += len(original) - add_bytes
                refs.update(cleanup["receipt_refs"])
                continue
            _backup_directory(root)
            if not already_exists:
                try:
                    with target.open("xb") as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                except FileExistsError:
                    target_info = _checked_backup(target, folder)
                    if target.read_bytes() != encoded:
                        raise VoiceError(409, "清理后的备份文件已变化，保留原备份。")
                    add_bytes = 0
                else:
                    result["bytes"] -= len(encoded)
            # Preserve the original backup date. Deduplicated snapshots retain
            # their newest source date instead of becoming a fresh backup.
            if target_info is None or original_info.st_mtime_ns > target_info.st_mtime_ns:
                atime = target_info.st_atime_ns if target_info is not None else original_info.st_atime_ns
                os.utime(target, ns=(atime, original_info.st_mtime_ns))
            _backup_directory(root)
            _checked_backup(target, folder)
            if target.read_bytes() != encoded:
                raise VoiceError(409, "清理后的备份校验失败，原备份已保留。")
            current_info = _checked_backup(path, folder)
            if current_info.st_size != len(original) or path.read_bytes() != original:
                raise VoiceError(409, "原备份在清理期间发生变化，未删除。")
            path.unlink()
            result["files"] += 1
            result["bytes"] += len(original)
            refs.update(cleanup["receipt_refs"])
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError, VoiceError) as exc:
            message = str(exc) if isinstance(exc, VoiceError) else "备份读取或清理未完成，原文件已保留。"
            result["warnings"].append(path.name + "：" + message)
    result["receipt_refs"] = sorted(refs)
    return result
