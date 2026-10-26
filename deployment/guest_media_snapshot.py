"""Build a path-free media allowlist from verified case/event relationships."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat


EXTENSIONS = {
    "video": frozenset({".mp4", ".webm", ".mov", ".m4v"}),
    "screenshot": frozenset({".png", ".jpg", ".jpeg", ".webp"}),
    "contact_sheet": frozenset({".png", ".jpg", ".jpeg", ".webp"}),
}
CASE_ID = re.compile(r"PVR-[a-f0-9]{12}\Z")
EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def media_path(kind, value):
    if kind not in EXTENSIONS or not isinstance(value, str) or not value or "\0" in value:
        return None
    path = Path(value)
    if not path.is_absolute() or path.suffix.lower() not in EXTENSIONS[kind]:
        return None
    return path


def valid_header(suffix, header):
    if suffix == ".png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return header.startswith(b"\xff\xd8\xff")
    if suffix == ".webp":
        return len(header) >= 16 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    if suffix == ".webm":
        return header.startswith(b"\x1a\x45\xdf\xa3")
    # ISO base media formats and the legacy QuickTime atom layout. The size
    # must describe an atom, rather than arbitrary text renamed as a video.
    atoms = {b"ftyp"} if suffix != ".mov" else {b"ftyp", b"moov", b"mdat", b"wide", b"free"}
    return len(header) >= 12 and header[4:8] in atoms and int.from_bytes(header[:4], "big") >= 8


def inspect_media(kind, raw, cache):
    path = media_path(kind, raw)
    if path is None:
        return None
    key = (kind, str(path))
    if key in cache:
        return cache[key]
    result = None
    try:
        # Registered attachments may be outside the case store. Do not follow
        # links to a different resource when deciding which IDs to publish.
        if any(item.is_symlink() or bool(getattr(item, "is_junction", lambda: False)())
               for item in (path, *path.parents)):
            return None
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if stat.S_ISREG(info.st_mode) and info.st_size > 0:
                header = stream.read(512)
                if valid_header(path.suffix.lower(), header):
                    result = {"name": path.name, "exists": True, "bytes": info.st_size}
    except (OSError, ValueError):
        pass
    cache[key] = result
    return result


def restore_media(raw_bundle, cleaned_bundle, clean):
    """Restore only usable media metadata after the shared text sanitizer ran.

    Nothing from raw asset dictionaries is copied verbatim. The returned index
    records IDs and kinds only; a media request must resolve the same event on
    the local service rather than accepting a file path from a visitor.
    """
    index = {"schema": 1, "cases": {}}
    raw_cases = raw_bundle.get("cases", {})
    clean_cases = cleaned_bundle.get("cases", {})
    if not isinstance(raw_cases, dict) or not isinstance(clean_cases, dict):
        return index
    summaries = {item.get("case_id"): item for item in cleaned_bundle.get("state", {}).get("cases", [])
                 if isinstance(item, dict)}
    cache = {}
    for case_id, raw_case in raw_cases.items():
        if (not isinstance(case_id, str) or not CASE_ID.fullmatch(case_id) or not isinstance(raw_case, dict)
                or raw_case.get("case_id") != case_id or not isinstance(clean_cases.get(case_id), dict)):
            continue
        events, current_id = raw_case.get("events"), raw_case.get("current_event_id")
        if not isinstance(events, list) or not isinstance(current_id, str) or not EVENT_ID.fullmatch(current_id):
            continue
        by_id = {}
        for event in events:
            event_id = event.get("event_id") if isinstance(event, dict) else None
            if not isinstance(event_id, str) or not EVENT_ID.fullmatch(event_id) or event_id in by_id:
                by_id = {}
                break
            by_id[event_id] = event
        if current_id not in by_id:
            continue
        media = {}
        metadata = {}
        for event_id, event in by_id.items():
            current = event.get("current")
            assets = current.get("assets") if isinstance(current, dict) else None
            if not isinstance(assets, dict):
                continue
            usable = {}
            for kind, raw in assets.items():
                info = inspect_media(kind, raw, cache)
                if info is not None:
                    usable[kind] = {**info, "name": clean(info["name"])}
            if usable:
                media[event_id] = sorted(usable)
                metadata[event_id] = usable
        if not media:
            continue
        index["cases"][case_id] = {"current_event_id": current_id, "events": media}
        cleaned_case = clean_cases[case_id]
        current_assets = metadata.get(current_id, {})
        private_metadata = raw_case.get("assets")
        for kind, info in current_assets.items():
            original = private_metadata.get(kind) if isinstance(private_metadata, dict) else None
            if isinstance(original, dict):
                for flag in ("owned", "managed", "temporary"):
                    if isinstance(original.get(flag), bool):
                        info[flag] = original[flag]
        for target in (cleaned_case, summaries.get(case_id)):
            if not isinstance(target, dict):
                continue
            target["assets"] = {kind: dict(info) for kind, info in current_assets.items()}
            if isinstance(target.get("current"), dict):
                target["current"]["assets"] = {kind: True for kind in current_assets}
        for event in cleaned_case.get("events", []):
            if isinstance(event, dict) and isinstance(event.get("current"), dict):
                event["current"]["assets"] = {kind: True for kind in media.get(event.get("event_id"), [])}
    return index
