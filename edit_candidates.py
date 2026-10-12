"""Candidate footage for an AI edit, prepared before the AI starts and remembered across edits.

The workbench lists every material clip with its labels, its source range and the parts other edits already used,
and exports a few frames of each clip into one contact image. What an AI later writes about a clip it looked at is
kept per source range, so the next edit of the same footage reads text instead of looking at the frames again.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile

from direction_store import now
from production_validation import _intervals_uncovered

INDEX_DIR = "davinci-visual-index"
MIN_FREE_FRAMES = 8
LONG_CLIP_FRAMES = 90
GRID_LIMIT = 1560
CAPTION_HEIGHT = 24
NOTE_FIELDS = {"summary": 500, "subject": 120}
CODE = re.compile(r"[A-Za-z]+\d+")
UNSURE = "待核"
# What a clip's label is worth to the edit: only a clip-labeling label bound to this clip stands for having seen it.
LABEL_STATUS = {"verified": "已核标签", "unsure": "标签待核，需看图", "manual": "手写标签，需看图", "none": "无标签，需看图"}


def label_status(clip):
    labels, verified = clip.get("labels") or [], clip.get("verified_labels") or []
    if any(UNSURE in label for label in labels):
        return "unsure"
    return "verified" if verified else "manual" if labels else "none"


def _identity(clip):
    path = str(clip.get("media_path") or "").replace("\\", "/").casefold()
    return path or f"media:{clip.get('media_id')}"


def candidate_key(clip):
    """A source range is the same footage whatever its label, position or material timeline."""
    code = next((found for label in reversed(clip.get("labels") or []) for found in reversed(CODE.findall(label))), "C")
    digest = hashlib.sha1(f"{_identity(clip)}|{clip['source_start']}|{clip['source_end']}".encode("utf-8")).hexdigest()[:8]
    return f"{code}-{digest}"


def sample_frames(start, end):
    """Record frames to look at: start, middle and end, or six evenly spaced frames for clips over three seconds."""
    length = end - start
    if length <= 0:
        return []
    count = 3 if length <= LONG_CLIP_FRAMES else 6
    inset = min(2, (length - 1) // 4)
    first, last = start + inset, end - 1 - inset
    return list(dict.fromkeys(round(first + (last - first) * index / (count - 1)) for index in range(count)))


def source_span(item):
    """Half-open source frames. Resolve reports the last source frame itself, which shows as one frame short at normal speed."""
    left, right = item.get("source_start"), item.get("source_end")
    if left is None or right is None:
        return None
    if right - left + 1 == item.get("end", 0) - item.get("start", 0):
        right += 1
    return left, right


def _bigrams(text):
    text = re.sub(r"\s+", "", str(text or "")).casefold()
    return {text[index:index + 2] for index in range(len(text) - 1)} | set(re.findall(r"[a-z0-9]+", text))


def rank_by_relevance(candidates, wanted_text):
    """Clips whose labels share wording with what the script needs come first; order is otherwise kept."""
    wanted = _bigrams(wanted_text)
    scored = [(-len(_bigrams(CODE.sub("", item["label"])) & wanted), number, item) for number, item in enumerate(candidates)]
    return [item for _, _, item in sorted(scored, key=lambda value: value[:2])]


def build_candidates(listing):
    """Material clips with their free source ranges; overlap uses half-open source frames, as the validation does."""
    used = {}
    for item in listing.get("used") or []:
        span = source_span(item)
        if not span:
            continue
        used.setdefault(_identity(item), []).append((*span, item.get("timeline_name") or ""))
        if item.get("media_id"):
            used.setdefault(f"media:{item['media_id']}", []).append((*span, item.get("timeline_name") or ""))
    found = []
    for material in listing.get("materials") or []:
        for clip in material.get("clips") or []:
            span = source_span(clip)
            if not span or clip["end"] <= clip["start"]:
                continue
            left, right = span
            spans = used.get(_identity(clip), []) + used.get(f"media:{clip.get('media_id')}", [])
            overlapping = [(a, b, name) for a, b, name in spans if max(a, left) < min(b, right)]
            free = [list(span) for span in _intervals_uncovered([(a, b) for a, b, _ in overlapping], left, right)]
            found.append({"key": candidate_key(clip), "label": " / ".join(clip.get("labels") or []) or clip.get("name") or "",
                          "timeline": material["name"], "timeline_uid": material["unique_id"], "item_uid": clip.get("uid"),
                          "record": [clip["start"], clip["end"]], "source": [left, right], "frames": clip["end"] - clip["start"],
                          "free": free, "free_frames": sum(b - a for a, b in free),
                          "used_by": sorted({name for _, _, name in overlapping if name}),
                          "media_id": clip.get("media_id"), "media_path": clip.get("media_path") or "", "has_audio": clip.get("has_audio"),
                          "label_status": label_status(clip)})
    unique = {}
    for candidate in found:
        unique.setdefault(candidate["key"], candidate)
    return list(unique.values())


def compose_grid(files, target, offsets):
    """One contact image of a single clip, frames in time order; each cell stays large enough to judge an action."""
    from PIL import Image, ImageDraw
    images = [Image.open(path).convert("RGB") for path in files]
    try:
        width, height = images[0].size
        columns = min(len(images), 3 if height >= width else 2)
        rows = math.ceil(len(images) / columns)
        cell_width = 432 if height >= width else 760
        cell_height = round(cell_width * height / width)
        scale = min(1.0, GRID_LIMIT / (columns * cell_width), GRID_LIMIT / (rows * (cell_height + CAPTION_HEIGHT)))
        cell_width, cell_height = int(cell_width * scale), int(cell_height * scale)
        sheet = Image.new("RGB", (columns * cell_width, rows * (cell_height + CAPTION_HEIGHT)), "black")
        draw = ImageDraw.Draw(sheet)
        for index, (image, offset) in enumerate(zip(images, offsets)):
            x, y = index % columns * cell_width, index // columns * (cell_height + CAPTION_HEIGHT)
            sheet.paste(image.resize((cell_width, cell_height)), (x, y + CAPTION_HEIGHT))
            draw.text((x + 8, y + 5), f"{index + 1}/{len(images)}  +{offset}f", fill="white")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        sheet.save(temporary, "JPEG", quality=80)
        os.replace(temporary, target)
    finally:
        for image in images:
            image.close()


class VisualIndex:
    """Per-project store of contact images and descriptions, keyed by source range."""

    def __init__(self, root, project):
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(project)).strip(" .") or "project"
        self.folder = Path(root) / name
        self.path, self.frames = self.folder / "index.json", self.folder / "frames"
        self.project = project

    def load(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        entries = value.get("entries") if isinstance(value, dict) else None
        return entries if isinstance(entries, dict) else {}

    def save(self, entries):
        self.folder.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".index-", suffix=".tmp", dir=self.folder)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"schema_version": 1, "project": self.project, "updated_at": now(), "entries": entries},
                          stream, ensure_ascii=False, indent=1)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def grid(self, key):
        path = self.frames / f"{key}.jpg"
        return path if path.is_file() else None


def remember(entries, candidate, **values):
    entry = entries.setdefault(candidate["key"], {})
    entry.update({"media_id": candidate["media_id"], "media_path": candidate["media_path"], "source": candidate["source"],
                  "label": candidate["label"], "timeline": candidate["timeline"], **values})
    return entry


def _describe_free(candidate):
    if candidate["free_frames"] == candidate["frames"] and not candidate["used_by"]:
        return "全段可用"
    spans = " ".join(f"[{a}, {b})" for a, b in candidate["free"])
    return f"可用源帧 {spans}（其余已被 {'、'.join(candidate['used_by']) or '其他成片'} 用过）"


def write_task_files(folder, found, index, entries, listing):
    """candidates.json/.md in the task folder; contact images are linked in, not copied, when the volume allows."""
    target = Path(folder) / "candidates"
    target.mkdir(parents=True, exist_ok=True)
    listed = []
    for candidate in found:
        if candidate["free_frames"] < MIN_FREE_FRAMES:
            continue
        grid, image = index.grid(candidate["key"]), None
        if grid:
            image = target / grid.name
            if not image.exists():
                try:
                    os.link(grid, image)
                except OSError:
                    shutil.copy2(grid, image)
        description = (entries.get(candidate["key"]) or {}).get("description")
        listed.append({**candidate, "grid": f"candidates/{image.name}" if image else None, "description": description})
    data = {"schema_version": 1, "materials": [material["name"] for material in listing.get("materials") or []],
            "missing": listing.get("missing") or [], "used_complete": bool(listing.get("used_complete")), "candidates": listed}
    (Path(folder) / "candidates.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = ["# 候选画面", "",
             "每行一个素材片段：候选编号｜标签｜标签状态｜素材时间线位置（帧，半开）｜时长｜可用范围｜对照图｜画面描述。",
             "「已核标签」是打标签技能看过画面后写的，可直接按标签选材；其余状态的片段，选用前先看对照图或已有描述。",
             "可用范围按实时工程里其他成片时间线算出，源帧坐标；已被用尽的片段没有列出。", ""]
    for name in data["materials"]:
        rows = [item for item in listed if item["timeline"] == name]
        lines += [f"## {name}（{len(rows)} 个可用片段）", ""]
        for item in rows:
            text = item["description"]["summary"] if item["description"] else ("—" if item["label_status"] == "verified" else "未描述，请看对照图")
            if item["description"] and item["description"].get("subject"):
                text += f"；主体：{item['description']['subject']}"
            if item["description"] and item["description"].get("cta") is not None:
                text += "；有号召手势" if item["description"]["cta"] else "；无号召手势"
            lines.append(f"- {item['key']}｜{item['label'] or '无标签'}｜{LABEL_STATUS[item['label_status']]}｜"
                         f"[{item['record'][0]}, {item['record'][1]})｜{item['frames']} 帧｜"
                         f"{_describe_free(item)}｜{item['grid'] or '无对照图'}｜{text}")
        lines.append("")
    (Path(folder) / "candidates.md").write_text("\n".join(lines), encoding="utf-8")
    return {"clips": len(found), "listed": len(listed), "used_up": len(found) - len(listed),
            "described": sum(1 for item in listed if item["description"]),
            "verified": sum(1 for item in listed if item["label_status"] == "verified"),
            "to_look": sum(1 for item in listed if item["label_status"] != "verified" and not item["description"]),
            "grids": sum(1 for item in listed if item["grid"])}


def _note(value):
    if not isinstance(value, dict) or not isinstance(value.get("key"), str):
        return None
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    note = {field: str(value[field]).strip()[:limit] for field, limit in NOTE_FIELDS.items()
            if isinstance(value.get(field), str) and value[field].strip()}
    for field in ("cta", "clear"):
        if type(value.get(field)) is bool:
            note[field] = value[field]
    return note


def merge_notes(folder, index, *, source):
    """Keep the descriptions an AI wrote in visual-notes.json, only for clips this edit listed."""
    path = Path(folder) / "visual-notes.json"
    if not path.is_file():
        return {"saved": 0, "rejected": 0}
    try:
        notes = json.loads(path.read_text(encoding="utf-8-sig"))
        listed = {item["key"]: item for item in json.loads((Path(folder) / "candidates.json").read_text(encoding="utf-8"))["candidates"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {"saved": 0, "rejected": 0, "error": "visual-notes.json 无法读取"}
    if isinstance(notes, dict):
        notes = notes.get("notes", [])
    if not isinstance(notes, list):
        return {"saved": 0, "rejected": 0, "error": "visual-notes.json 格式无效"}
    entries, saved, rejected = index.load(), 0, 0
    for value in notes[:5000]:
        note = _note(value)
        candidate = listed.get(value.get("key")) if note else None
        if not candidate:
            rejected += 1
            continue
        remember(entries, candidate, description=note, described_at=now(), described_by=source)
        saved += 1
    if saved:
        index.save(entries)
    return {"saved": saved, "rejected": rejected}
