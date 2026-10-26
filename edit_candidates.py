"""Candidate footage for an AI edit, prepared before the AI starts and remembered across edits.

The workbench lists every material clip with its labels and its full source range,
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


def build_candidates(listing, *, deduplicate_source_ranges=False, count_usage=False):
    """List complete material clips; archived source-range deduplication requires explicit opt-in.

    count_usage keeps every clip whole but records which finished timelines already used it (use_count), so the edit
    can prefer the least-used footage."""
    used = {}
    for item in (listing.get("used") or []) if deduplicate_source_ranges or count_usage else []:
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
            free = ([list(span) for span in _intervals_uncovered([(a, b) for a, b, _ in overlapping], left, right)]
                    if deduplicate_source_ranges else [[left, right]])
            found.append({"key": candidate_key(clip), "label": " / ".join(clip.get("labels") or []) or clip.get("name") or "",
                          "timeline": material["name"], "timeline_uid": material["unique_id"], "item_uid": clip.get("uid"),
                          "record": [clip["start"], clip["end"]], "source": [left, right], "frames": clip["end"] - clip["start"],
                          "free": free, "free_frames": sum(b - a for a, b in free),
                          "used_by": sorted({name for _, _, name in overlapping if name}),
                          "use_count": len({name for _, _, name in overlapping if name}),
                          "media_id": clip.get("media_id"), "media_path": clip.get("media_path") or "", "has_audio": clip.get("has_audio"),
                          "label_status": label_status(clip)})
    if not deduplicate_source_ranges:
        return found
    unique = {}
    for candidate in found:
        unique.setdefault(candidate["key"], candidate)
    return list(unique.values())


IN_PROGRESS = "（剪辑中）"


def label_code(key):
    """The label code a candidate key starts with (DG043 in DG043-3d173093); empty for clips whose labels carry none."""
    code = str(key or "").rsplit("-", 1)[0]
    return code if CODE.fullmatch(code) else ""


def pick_keys(picks, candidates, foreign=()):
    """The candidate keys a planner's first-choice list names, and how many names in it matched none of `candidates`.

    An entry is a candidate key, or an object holding one under "key" (or several under "key"/"keys" for a 意群 that
    needs several shots). A key that is not listed still names its clip by its label code (planners write DG043, or
    mistype the digest) when every candidate carrying that code comes from one media file: one clip, perhaps split into
    adjacent pieces or listed on two material timelines. A code found on several media files needs the exact key.
    `foreign` keys belong to clips the planner may see but does not own; they name nothing, not even by their code."""
    keys = list(dict.fromkeys(item["key"] for item in candidates if item.get("key")))
    media = {}
    for item in candidates:
        media.setdefault(label_code(item.get("key")), set()).add(_identity(item))
    names = []
    for entry in picks:
        values = [entry.get(field) for field in ("key", "keys")] if isinstance(entry, dict) else [entry]
        found = [value for item in values for value in (item if isinstance(item, list) else [item]) if value is not None]
        names.extend(found or [None])
    chosen, unknown = [], 0
    for name in names:
        name = name.strip() if isinstance(name, str) else ""
        code = label_code(name)
        found = [name] if name in keys else [] if name in foreign else (
            [key for key in keys if label_code(key) == code] if code and len(media.get(code, ())) == 1 else [])
        unknown += not found
        chosen.extend(key for key in found if key not in chosen)
    return chosen, unknown


def planned_spans(keys, candidates):
    """Source ranges of the candidates a running edit's planners chose first; the edit holds them until it ends."""
    wanted = set(keys)
    return [{"media_id": item.get("media_id"), "media_path": item.get("media_path") or "", "source": list(item["source"])}
            for item in candidates if item.get("key") in wanted]


def refreshed_usage(rows, fresh):
    """The snapshot's candidates with use counts from a fresh listing, and how many of them changed.

    Counts belong to the source range (the key), whatever material timeline lists it. A clip the fresh listing no
    longer has, because it was trimmed or removed since, keeps the count the snapshot gave it."""
    counts = {item["key"]: (item.get("use_count", 0), item.get("used_by", [])) for item in fresh}
    updated, changed = [], 0
    for row in rows:
        before = (row.get("use_count", 0), row.get("used_by", []))
        count, used_by = counts.get(row.get("key"), before)
        changed += (count, used_by) != before
        updated.append({**row, "use_count": count, "used_by": used_by})
    return updated, changed


def add_in_progress_usage(found, occupied):
    """Count footage that edits still running have planned to use, as if their timelines were already finished.

    occupied maps a running edit's target timeline name to its planned spans. A timeline the scan already counted
    for a clip is not counted twice."""
    for name, spans in occupied.items():
        for candidate in found:
            if name in candidate["used_by"] or name + IN_PROGRESS in candidate["used_by"]:
                continue
            left, right = candidate["source"]
            same = [span for span in spans if (span["media_id"] and span["media_id"] == candidate.get("media_id")) or
                    (span["media_path"] and _identity(span) == _identity(candidate))]
            if any(max(span["source"][0], left) < min(span["source"][1], right) for span in same):
                candidate["used_by"] = sorted([*candidate["used_by"], name + IN_PROGRESS])
                candidate["use_count"] = len(candidate["used_by"])
    return found

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


def _describe_free(candidate, count_usage=False):
    if count_usage:
        used = candidate.get("used_by") or []
        return f"已用 {len(used)} 次（{'、'.join(used)}）" if used else "未用过"
    if candidate["free"] == [candidate["source"]] and not candidate["used_by"]:
        return "全段可用"
    spans = " ".join(f"[{a}, {b})" for a, b in candidate["free"])
    return f"可用源帧 {spans}（其余已被 {'、'.join(candidate['used_by']) or '其他成片'} 用过）"


def write_task_files(folder, found, index, entries, listing, *, deduplicate_source_ranges=False, count_usage=False):
    """candidates.json/.md in the task folder; contact images are linked in, not copied, when the volume allows."""
    target = Path(folder) / "candidates"
    target.mkdir(parents=True, exist_ok=True)
    listed = []
    for candidate in found:
        if deduplicate_source_ranges and candidate["free_frames"] < MIN_FREE_FRAMES:
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
            "missing": listing.get("missing") or [], "dedup_enabled": deduplicate_source_ranges,
            "used_complete": bool(listing.get("used_complete")) if deduplicate_source_ranges or count_usage else False,
            "usage_counted": count_usage, "candidates": listed}
    (Path(folder) / "candidates.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = ["# 候选画面", "",
             f"每行一个素材片段：候选编号｜标签｜标签状态｜素材时间线位置（帧，半开）｜时长｜{'使用次数' if count_usage else '可用范围'}｜对照图｜画面描述。",
             "「已核标签」是打标签技能看过画面后写的，可直接按标签选材；其余状态的片段，选用前先看对照图或已有描述。",
             ("可用范围按实时工程里其他成片时间线算出，源帧坐标；已被用尽的片段没有列出。"
              if deduplicate_source_ranges else
              "按完整素材源区间提供候选；每行注明这个片段已被几条成片用过（同一成片里用几次都算 1 次），同一素材时间线内按用过次数从少到多排列。"
              if count_usage else "按完整素材源区间提供候选，不因其他成片使用过而排除。"), ""]
    for name in data["materials"]:
        rows = [item for item in listed if item["timeline"] == name]
        if count_usage:
            rows.sort(key=lambda item: item.get("use_count", 0))
        lines += [f"## {name}（{len(rows)} 个可用片段）", ""]
        for item in rows:
            text = item["description"]["summary"] if item["description"] else ("—" if item["label_status"] == "verified" else "未描述，请看对照图")
            if item["description"] and item["description"].get("subject"):
                text += f"；主体：{item['description']['subject']}"
            if item["description"] and item["description"].get("cta") is not None:
                text += "；有号召手势" if item["description"]["cta"] else "；无号召手势"
            lines.append(f"- {item['key']}｜{item['label'] or '无标签'}｜{LABEL_STATUS[item['label_status']]}｜"
                         f"[{item['record'][0]}, {item['record'][1]})｜{item['frames']} 帧｜"
                         f"{_describe_free(item, count_usage)}｜{item['grid'] or '无对照图'}｜{text}")
        lines.append("")
    (Path(folder) / "candidates.md").write_text("\n".join(lines), encoding="utf-8")
    return {"clips": len(found), "listed": len(listed), "used_up": len(found) - len(listed),
            "described": sum(1 for item in listed if item["description"]),
            "verified": sum(1 for item in listed if item["label_status"] == "verified"),
            "to_look": sum(1 for item in listed if item["label_status"] != "verified" and not item["description"]),
            "grids": sum(1 for item in listed if item["grid"]),
            "unused": sum(1 for item in listed if not item.get("use_count")) if count_usage else None}


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
