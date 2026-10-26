"""Retire package replay data in completed carousel publication journals only."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from carousel_delivery_retention import _regular_path
from elevenlabs_client import VoiceError
from production_store import CAROUSEL_ID, DELIVERY_ID, scrub_retired_delivery_data

OPERATION_NAME = re.compile(r"[\w-]{10,100}\.json")


def _operations_folder(production_root):
    root = Path(production_root).absolute()
    for ancestor in (*reversed(root.parents), root):
        _regular_path(ancestor)
    if root.exists() and not root.is_dir():
        raise VoiceError(409, "制作资料根目录格式无效，未清理发布恢复资料。")
    creation = root.parent / "creation"
    folder = creation / "operations"
    for path in (creation, folder):
        _regular_path(path)
        if path.exists() and not path.is_dir():
            raise VoiceError(409, "发布恢复资料目录格式无效，未清理。")
    if creation.resolve().parent != root.parent.resolve() or folder.resolve().parent != creation.resolve():
        raise VoiceError(409, "发布恢复资料目录超出当前运行目录，未清理。")
    return root, folder


def _checked_operation(path, folder):
    _regular_path(folder)
    _regular_path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.resolve().parent != folder.resolve():
        raise VoiceError(409, "发布恢复记录不是所属目录中的普通文件，未清理。")
    return info


def _completed_operations(production_root, carousel_id, warnings):
    root, folder = _operations_folder(production_root)
    if not folder.exists():
        return
    for path in sorted(folder.iterdir()):
        if not OPERATION_NAME.fullmatch(path.name):
            continue
        try:
            _operations_folder(root)
            info = _checked_operation(path, folder)
            original = path.read_bytes()
            operation = json.loads(original)
            if not isinstance(operation, dict) or operation.get("kind") != "carousel_publication" or operation.get("complete") is not True:
                continue
            if operation.get("request_id") != path.stem:
                raise VoiceError(409, "发布恢复记录的保存标识与文件名不一致，未清理。")
            result = operation.get("result")
            carousel = result.get("carousel") if isinstance(result, dict) else None
            if not isinstance(carousel, dict) or carousel.get("id") != carousel_id:
                continue
            yield root, folder, path, info, original, operation
        except (OSError, ValueError, TypeError, AttributeError, VoiceError):
            warnings.append(path.name + "：发布恢复记录读取或安全核验未通过，原文件已保留。")


def operation_delivery_history(production_root, carousel_id, keep_id):
    """Read package identities still embedded in completed publication replays."""
    if not isinstance(carousel_id, str) or not CAROUSEL_ID.fullmatch(carousel_id) or not isinstance(keep_id, str) or not DELIVERY_ID.fullmatch(keep_id):
        raise VoiceError(400, "历史成品包查询编号无效。")
    records = {}
    for _, _, _, _, _, operation in _completed_operations(production_root, carousel_id, []):
        deliveries = operation["result"]["carousel"].get("deliveries", [])
        if not isinstance(deliveries, list):
            continue
        for record in deliveries:
            if isinstance(record, dict) and isinstance(record.get("id"), str) and DELIVERY_ID.fullmatch(record["id"]) and record["id"] != keep_id:
                records.setdefault(record["id"], copy.deepcopy(record))
    return list(records.values())


def cleanup_delivery_operations(production_root, carousel_id, old_ids, *, dry_run=False):
    """Atomically rewrite replay snapshots, preserving the actual publication facts.

    ``files`` counts rewritten journals; ``bytes`` is net released storage.
    Unfinished operations and every other journal kind remain untouched.
    """
    if (not isinstance(carousel_id, str) or not CAROUSEL_ID.fullmatch(carousel_id) or
            not isinstance(old_ids, (list, tuple)) or any(not isinstance(value, str) or not DELIVERY_ID.fullmatch(value)
                                                       for value in old_ids) or type(dry_run) is not bool):
        raise VoiceError(400, "发布恢复资料清理参数无效。")
    answer = {"files": 0, "bytes": 0, "warnings": []}
    retired = set(old_ids)
    if not retired:
        _operations_folder(production_root)
        return answer
    for root, folder, path, info, original, operation in _completed_operations(production_root, carousel_id, answer["warnings"]):
        temporary = None
        try:
            result = operation["result"]
            carousel = result["carousel"]
            cleanup = scrub_retired_delivery_data({"carousels": [carousel], "requests": {}}, carousel_id, old_ids)
            changed = cleanup["changed"]
            publication = result.get("publication")
            if isinstance(publication, dict) and publication.get("delivery_id") in retired and not publication.get("delivery_snapshot"):
                candidates = [carousel.get("publication"), *carousel.get("publication_history", [])]
                matching = next((entry for entry in candidates if isinstance(entry, dict) and
                    all(entry.get(key) == publication.get(key) for key in ("id", "delivery_id", "delivery_sha256")) and
                    isinstance(entry.get("delivery_snapshot"), dict) and
                    entry["delivery_snapshot"].get("id") == publication.get("delivery_id") and
                    entry["delivery_snapshot"].get("sha256") == publication.get("delivery_sha256")), None)
                if matching:
                    publication["delivery_snapshot"] = copy.deepcopy(matching["delivery_snapshot"])
                    changed = True
            if not changed:
                continue
            encoded = (json.dumps(operation, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
            if not dry_run:
                _operations_folder(root)
                descriptor, temporary = tempfile.mkstemp(prefix=".delivery-journal-", suffix=".tmp", dir=folder)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.utime(temporary, ns=(info.st_atime_ns, info.st_mtime_ns))
                _operations_folder(root)
                _checked_operation(path, folder)
                if path.read_bytes() != original:
                    raise VoiceError(409, "发布恢复记录在清理期间已变化，未覆盖。")
                os.replace(temporary, path)
                temporary = None
            answer["files"] += 1
            answer["bytes"] += len(original) - len(encoded)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, VoiceError):
            answer["warnings"].append(path.name + "：发布恢复记录清理未完成，原文件已保留。")
        finally:
            if temporary is not None:
                temp_path = Path(temporary)
                _regular_path(temp_path)
                if temp_path.exists():
                    temp_path.unlink()
    return answer
