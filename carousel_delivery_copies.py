"""Remove verified download copies of retired packages, never arbitrary image folders."""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import re
import shutil
import zipfile

from carousel_delivery_retention import DELIVERY, _regular_path, delivery_folder, folder_size
from elevenlabs_client import VoiceError


def _sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cleanup_delivery_copies(root, carousel_id, records, *, download_roots=None, dry_run=False):
    report = {"files": 0, "bytes": 0, "warnings": []}
    locations = download_roots if download_roots is not None else (
        Path.home() / "Downloads", Path.home() / "Downloads" / "Compressed")
    for record in records:
        warnings_before = len(report["warnings"])
        identifier, digest = record.get("id"), record.get("sha256")
        if not DELIVERY.fullmatch(str(identifier)) or not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            continue
        try:
            owned = delivery_folder(root, carousel_id, identifier) / str(record.get("filename", ""))
            _regular_path(owned)
            if owned.resolve().parent != delivery_folder(root, carousel_id, identifier).resolve():
                raise VoiceError(409, "旧成品包副本校验路径无效。")
            matching, folders = [], []
            for location in locations:
                location = Path(location).absolute()
                _regular_path(location)
                if not location.is_dir():
                    continue
                for candidate in location.glob(f"*{identifier}*"):
                    _regular_path(candidate)
                    if candidate.resolve().parent != location.resolve():
                        continue
                    if candidate.is_file() and candidate.suffix.lower() == ".zip" and _sha(candidate) == digest:
                        matching.append(candidate)
                    elif candidate.is_dir():
                        folders.append(candidate)
            basis = owned if owned.is_file() and _sha(owned) == digest else next(iter(matching), None)
            if basis is None:
                continue
            with zipfile.ZipFile(basis) as archive:
                expected = {}
                for member in archive.infolist():
                    if member.is_dir() or PurePosixPath(member.filename).name != member.filename or member.file_size > 30 * 1024 * 1024:
                        raise VoiceError(409, "旧成品包副本内容无法安全核对，未清理下载副本。")
                    expected[member.filename] = hashlib.sha256(archive.read(member)).hexdigest()
            for folder in dict.fromkeys(folders):
                count, size = folder_size(folder)
                entries = list(folder.iterdir())
                if (not expected or {entry.name for entry in entries} != set(expected) or
                        any(not entry.is_file() or _sha(entry) != expected[entry.name] for entry in entries)):
                    report["warnings"].append(f"下载目录 {folder.name} 含其它或已改动文件，已保留。")
                    continue
                if not dry_run:
                    # The absolute child and every descendant were verified above.
                    shutil.rmtree(folder)
                report["files"] += count
                report["bytes"] += size
            if len(report["warnings"]) != warnings_before:
                continue  # Keep a verified ZIP so a later retry can validate an extracted folder again.
            for path in dict.fromkeys(matching):
                _regular_path(path)
                if _sha(path) != digest:
                    continue
                size = path.stat().st_size
                if not dry_run:
                    path.unlink()
                report["files"] += 1
                report["bytes"] += size
        except (VoiceError, OSError, ValueError, zipfile.BadZipFile) as exc:
            report["warnings"].append(str(exc))
    return report
