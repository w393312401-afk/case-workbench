"""剪辑说明的时间轴对照：把交付快照、意群、原片镜头和剪辑说明里的逐镜头对比对齐到成片帧。

只读：数据来自制作单记录、剪辑目录里的 delivered-timeline.json 与 candidates.json、
案例原视频和素材原件；缩略图缓存到运行目录 thumbnail-cache/edits/<剪辑编号>/。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from uuid import uuid4

from case_replica import shot_targets
import edit_rhythm
from elevenlabs_client import VoiceError
from production_store import EDIT_ID, JOB_ID
from production_validation import rate

STATUSES = ("exact", "close", "substitute", "missing")
CLIP_CODE = re.compile(r"\b([A-Z]{1,6}\d{2,4})\b")
FRAME_RANGE = re.compile(r"(\d+)\s*[,，–—\-~～]\s*(\d+)")
NUMBER_RANGE = re.compile(r"(\d+)(?:\s*(?:尾|头)?\s*[–—\-~～]\s*(\d+))?")
JSON_BLOCK = re.compile(r"```json[\s\S]*?```")
BULLET = re.compile(r"^\s*[-*]\s*\*\*意群\s*([^*]+?)\*\*\s*\[(\d+),\s*(\d+)\)\s*[：:，,]?\s*(.*)$", re.M)
EXACT = re.compile(r"^\s*(对上|一致|基本一致|完全一致|相同)")
SUBSTITUTE = re.compile(r"换了动作|换成|换了|没有.{0,6}动作|不是同一动作")
_slots = threading.BoundedSemaphore(2)
_rates: dict[str, float] = {}
_rates_lock = threading.Lock()


def _find(app, job_id, edit_id):
    if not JOB_ID.fullmatch(str(job_id)) or not EDIT_ID.fullmatch(str(edit_id)):
        raise VoiceError(400, "剪辑编号无效。")
    job = app.production.job(job_id)
    edit = next((item for item in job.get("edits", []) if item.get("id") == edit_id), None)
    if not edit:
        raise VoiceError(404, "这次剪辑不存在。")
    return job, edit


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _snapshot(app, edit_id):
    data = _read_json(Path(app.production.root) / "edits" / edit_id / "delivered-timeline.json")
    timeline = data.get("timeline") if isinstance(data, dict) else None
    return timeline if isinstance(timeline, dict) else None


def _picture_items(timeline):
    """Footage on the lowest enabled video track that has any; overlays (stickers, titles) sit above it."""
    tracks = (timeline.get("tracks") or {}).get("video") or []
    start = int(timeline.get("start_frame") or 0)
    for track in sorted(tracks, key=lambda item: item.get("index") or 0):
        if track.get("enabled") is False or "贴纸" in str(track.get("name") or ""):
            continue
        items = [item for item in track.get("items") or [] if item.get("enabled") is not False
                 and isinstance(item.get("start"), int) and isinstance(item.get("end"), int) and item["end"] > item["start"]]
        if items:
            return [{**item, "start": item["start"] - start, "end": item["end"] - start} for item in sorted(items, key=lambda item: item["start"])]
    return []


def _candidates(app, edit_id):
    data = _read_json(Path(app.production.root) / "edits" / edit_id / "candidates.json")
    return [item for item in (data or {}).get("candidates") or [] if isinstance(item, dict)] if isinstance(data, dict) else []


def _clip_code(item, candidates):
    """The material code (MD055) of the candidate this timeline item was cut from."""
    at = item.get("source_start")
    for candidate in candidates:
        source = candidate.get("source") or []
        if (candidate.get("media_path") == item.get("media_path") and len(source) == 2
                and isinstance(at, (int, float)) and source[0] <= at < source[1]):
            found = CLIP_CODE.search(str(candidate.get("key") or "")) or CLIP_CODE.search(str(candidate.get("label") or ""))
            return found.group(1) if found else "", str(candidate.get("label") or "")
    return "", ""


def _original_shots(job, edit):
    """Each original shot's range on this cut, as the handoff gave it to the editor; None outside 1:1 replicas."""
    reference = job.get("replica")
    if not isinstance(reference, dict) or not reference.get("shots") or not reference.get("spans"):
        return None
    timeline = next((item for item in job.get("timelines", []) if item.get("unique_id") == edit.get("timeline_uid")), None)
    take = next((item for item in job.get("takes", []) if item.get("id") == (edit.get("take_id") or (timeline or {}).get("take_id"))), None)
    if not timeline or not take or not take.get("groups") or not timeline.get("frames"):
        return None
    try:
        rows = shot_targets(reference, take["groups"], timeline.get("fps"), timeline.get("lead_frames", 0), timeline["frames"])
    except (ValueError, TypeError, KeyError, ZeroDivisionError):
        return None
    return [{"number": row["number"], "start": row["target_start"], "end": row["target_end"],
             "seconds": [row["start"], row["end"]], "lines": row.get("lines") or []} for row in rows]


def _cells(line):
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def split_table(text):
    """The first markdown table with three or more columns: (header, rows, text without it)."""
    lines = text.splitlines()
    for index in range(len(lines) - 1):
        if (lines[index].lstrip().startswith("|") and re.fullmatch(r"\s*\|?[\s:|-]*-{3,}[\s:|-]*\|?\s*", lines[index + 1])
                and len(_cells(lines[index])) >= 3):
            end = index + 2
            while end < len(lines) and lines[end].lstrip().startswith("|"):
                end += 1
            header = _cells(lines[index])
            rows = [cells + [""] * (len(header) - len(cells)) for cells in map(_cells, lines[index + 2:end])]
            # The bold heading right above the table ("**逐镜头对比**") goes with it.
            above = index - 1
            while above >= 0 and not lines[above].strip():
                above -= 1
            heading = above if above >= 0 and re.fullmatch(r"\s*\*\*[^*]+\*\*[^|]*", lines[above]) else index
            rest = "\n".join(lines[:heading] + lines[end:])
            return header, rows, re.sub(r"\n{3,}", "\n\n", rest).strip()
    return None, [], text.strip()


def _column(header, *words, exclude=()):
    for position, name in enumerate(header):
        if position and any(word in name for word in words) and not any(word in name for word in exclude):
            return position
    return None


def _status(text):
    text = str(text or "").strip()
    if not text:
        return None
    if EXACT.match(text):
        return "exact"
    return "substitute" if SUBSTITUTE.search(text) else "close"


def _numbers(label):
    numbers = []
    for low, high in NUMBER_RANGE.findall(str(label)):
        low = int(low)
        high = int(high) if high else low
        if 0 < low <= high <= low + 50:
            numbers.extend(range(low, high + 1))
    return sorted(set(numbers))


def _rows_from_segments(segments, total):
    rows = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        start, end = segment.get("start"), segment.get("end")
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end):
            continue
        status = segment.get("status") if segment.get("status") in STATUSES else None
        shots = [value for value in segment.get("shots") or [] if isinstance(value, int)]
        rows.append({"label": str(segment.get("label") or ("–".join(map(str, [shots[0], shots[-1]])) if len(shots) > 1 else shots[0] if shots else "")),
                     "start": start, "end": min(end, total), "status": status, "shots": shots,
                     "original": str(segment.get("original") or ""), "used": str(segment.get("used") or ""),
                     "note": str(segment.get("note") or segment.get("diff") or "")})
    return rows


def _rows_from_table(header, cells_list, *, total, originals, items, lines, replica):
    """Place each row of the editor's table on the cut: frame column, original shots, material codes, then 意群."""
    frames_at = _column(header, "帧", "区间")
    original_at = _column(header, "原片", exclude=("差", "秒"))
    used_at = _column(header, "这次", "用的", "用了", "素材", "片段")
    picture_at = _column(header, "画面")
    note_at = _column(header, "差")
    by_shot = {row["number"]: row for row in originals or []}
    pointer, rows = 0, []
    for cells in cells_list:
        label = cells[0]
        start = end = None
        found = FRAME_RANGE.search(cells[frames_at]) if frames_at is not None else None
        if found:
            start, end = int(found.group(1)), int(found.group(2))
        shots = _numbers(label) if replica and "镜头" in header[0] else []
        if start is None and shots and all(number in by_shot for number in shots):
            start, end = by_shot[shots[0]]["start"], by_shot[shots[-1]]["end"]
        if start is None:
            codes = set(CLIP_CODE.findall(" ".join(cells[1:])))
            taken = []
            while pointer < len(items) and items[pointer]["code"] in codes:
                taken.append(items[pointer])
                pointer += 1
            if taken:
                start, end = taken[0]["start"], taken[-1]["end"]
        if start is None and "意群" in header[0]:
            groups = [line for line in lines if line["number"] in _numbers(label)]
            if groups:
                start, end = groups[0]["start"], groups[-1]["end"]
        if start is None or not 0 <= start < end:
            continue
        if not shots and originals:
            shots = [row["number"] for row in originals if row["start"] < end and start < row["end"]]
        if items:
            pointer = max(pointer, next((index for index, item in enumerate(items) if item["start"] >= end), len(items)))
        used = cells[used_at] if used_at is not None else ""
        note = cells[note_at] if note_at is not None else ""
        rows.append({"label": label, "start": start, "end": min(end, total), "shots": shots,
                     "status": _status(note) if replica and note_at is not None else None,
                     "original": cells[original_at] if original_at is not None else "",
                     "used": used or (cells[picture_at] if picture_at is not None else ""),
                     "picture": cells[picture_at] if picture_at is not None and used_at is not None else "",
                     "note": note})
    return rows


def _rhythm(app, job, edit, items, lines, fps):
    """The cut's rhythm report with its one-line headline; None when it cannot be worked out. Never fails the write-up."""
    try:
        timeline = next((item for item in job.get("timelines", []) if item.get("unique_id") == edit.get("timeline_uid")), None) or {}
        take = next((item for item in job.get("takes", []) if item.get("id") == (edit.get("take_id") or timeline.get("take_id"))), None)
        read = getattr(app, "take_triples", None)
        triples = read(job["id"], take) if read and take else []
        spans = edit_rhythm.word_spans([line["spoken"] for line in lines], triples) if triples else None
        value = edit_rhythm.report(items, lines, spans, fps, int(timeline.get("lead_frames") or 0))
    except (TypeError, ValueError, KeyError, AttributeError, ZeroDivisionError):
        return None
    return {**value, "headline": edit_rhythm.headline(value)} if value else None


def visual(app, job_id, edit_id):
    """Everything the 时间轴对照 needs, in cut frames from 0."""
    job, edit = _find(app, job_id, edit_id)
    output = str(edit.get("output") or "")
    readable = JSON_BLOCK.sub("", output).strip()
    report = edit.get("report") if isinstance(edit.get("report"), dict) else {}
    timeline = _snapshot(app, edit_id)
    if not timeline:
        return {"available": False, "text": readable, "reason": "这次剪辑没有交付快照，只能显示文字说明。"}
    try:
        fps = rate(timeline.get("fps") or 0)
    except ValueError:
        return {"available": False, "text": readable, "reason": "交付快照读不到帧率。"}
    candidates = _candidates(app, edit_id)
    items = []
    for index, item in enumerate(_picture_items(timeline)):
        code, label = _clip_code(item, candidates)
        items.append({"index": index, "start": item["start"], "end": item["end"], "code": code, "label": label,
                      "name": str(item.get("name") or ""), "source": [item.get("source_start"), item.get("source_end")],
                      "source_start": item.get("source_start"), "source_end": item.get("source_end"),
                      "media_path": item.get("media_path"), "speed": item.get("speed")})
    total = max([item["end"] for item in items] + [int(timeline.get("end_frame") or 0) - int(timeline.get("start_frame") or 0), 1])
    texts = {line.get("number"): line for line in job.get("lines", [])}
    lines = []
    for marker in timeline.get("markers") or []:
        custom = str(marker.get("custom") or "")
        if not custom.startswith(f"workbench-group:{job['id']}:"):
            continue
        try:
            number = int(custom.rsplit(":", 1)[1])
        except ValueError:
            continue
        start = int(marker.get("frame") or 0)
        line = texts.get(number) or {}
        lines.append({"number": number, "start": start, "end": start + max(1, int(marker.get("duration") or 1)),
                      "zh": str(line.get("zh") or ""), "spoken": str(line.get("spoken") or "")})
    lines.sort(key=lambda line: line["start"])
    originals = _original_shots(job, edit)
    replica = edit.get("mode") == "replica" and bool(originals)
    header, cells, rest = split_table(readable)
    segments = report.get("segments") if isinstance(report.get("segments"), list) else None
    rows = _rows_from_segments(segments, total) if segments else []
    from_table = bool(rows)  # The strip now carries the per-shot table, so the text drops it.
    if not rows and header:
        rows = _rows_from_table(header, cells, total=total, originals=originals, items=items, lines=lines, replica=replica)
        from_table = bool(rows)
    if not rows:
        # Some write-ups list 「- **意群 N** [a, b)：画面」 instead of a table; the text stays as written.
        rows = [{"label": f"意群 {label.strip()}", "start": int(start), "end": min(int(end), total), "shots": [], "status": None,
                 "original": "", "used": picture.strip(), "note": ""}
                for label, start, end, picture in BULLET.findall(readable) if int(start) < int(end)]
    if not from_table:
        rest = readable  # Only a table the strip now shows leaves the text.
    gaps = [{"start": gap["start"], "end": gap["end"], "status": gap.get("status") or "missing", "need": str(gap.get("need") or "")}
            for gap in report.get("gaps") or [] if isinstance(gap, dict) and isinstance(gap.get("start"), int)
            and isinstance(gap.get("end"), int) and gap["end"] > gap["start"]]
    # Colours read from the 差在哪 wording are a guess; the editor's own counts stay the headline then.
    inferred = not segments and any(row["status"] for row in rows)
    return {"available": True, "fps": fps, "inferred": inferred, "total": total, "timeline_uid": edit.get("timeline_uid") or "",
            "rhythm": _rhythm(app, job, edit, items, lines, fps),
            "replica": replica, "counts": report.get("replica") if isinstance(report.get("replica"), dict) else None,
            "lines": lines, "cut": [{key: item[key] for key in ("index", "start", "end", "code", "label")} for item in items],
            "original": originals if replica else None, "rows": rows, "gaps": gaps,
            "text": rest}


def _media_rate(path):
    with _rates_lock:
        if path in _rates:
            return _rates[path]
    value = 0.0
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            done = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                                   "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True, timeout=20,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            top, _, bottom = done.stdout.strip().partition("/")
            value = float(top) / float(bottom or 1)
        except (OSError, ValueError, ZeroDivisionError, subprocess.TimeoutExpired):
            value = 0.0
    with _rates_lock:
        _rates[path] = value
    return value


def _grab(video, seconds, target):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise VoiceError(404, "本机没有 ffmpeg，无法生成缩略图。")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.stem}-{uuid4().hex}.jpg")
    try:
        with _slots:
            done = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{max(seconds, 0):.3f}",
                                   "-i", str(video), "-frames:v", "1", "-an", "-sn", "-dn", "-vf", "scale=-2:320", "-q:v", "5", str(temporary)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if done.returncode == 0 and temporary.is_file() and temporary.stat().st_size > 4:
            temporary.replace(target)
            return target
    except (OSError, subprocess.TimeoutExpired):
        pass
    finally:
        temporary.unlink(missing_ok=True)
    raise VoiceError(404, "这一帧暂时取不到。")


def frame(app, job_id, edit_id, kind, number):
    """A cached JPEG: kind "cut" is the middle of timeline item `number`, "orig" the middle of original shot `number`."""
    job, edit = _find(app, job_id, edit_id)
    if kind not in {"cut", "orig"} or not isinstance(number, int) or number < 0:
        raise VoiceError(400, "缩略图参数无效。")
    cache = Path(app.runtime) / "thumbnail-cache" / "edits" / edit_id
    target = cache / f"{kind}-{number}.jpg"
    if target.is_file():
        return target
    if kind == "cut":
        timeline = _snapshot(app, edit_id)
        items = _picture_items(timeline) if timeline else []
        if number >= len(items):
            raise VoiceError(404, "没有这个片段。")
        item = items[number]
        video = Path(str(item.get("media_path") or ""))
        media_rate = _media_rate(str(video)) if video.is_file() else 0
        if not media_rate or not isinstance(item.get("source_start"), (int, float)):
            raise VoiceError(404, "素材原件不在，取不到这一帧。")
        middle = (item["source_start"] + (item.get("source_end") or item["source_start"])) / 2
        return _grab(video, middle / media_rate, target)
    shots = (job.get("replica") or {}).get("shots") or []
    shot = next((item for item in shots if item.get("number") == number), None)
    if not shot:
        raise VoiceError(404, "没有这个原片镜头。")
    try:
        case = app.store.read_case(job["replica"]["case_id"])
        video = Path(str(((case.get("current") or {}).get("assets") or {}).get("video") or ""))
    except Exception as exc:
        raise VoiceError(404, "原案例已不在。") from exc
    if not video.is_file():
        raise VoiceError(404, "原片视频已不在。")
    return _grab(video, (shot["start"] + shot["end"]) / 2, target)
