"""Read-only engineering checks for editable Resolve timelines.

Passing these checks means the timeline can be handed to the user for review. It
does not approve pictures, subtitles, sound, or a rendered export.
"""
from __future__ import annotations

import copy
import math
import re

from direction_store import now

GAP_NAME = re.compile(r"^\s*缺素材")
MARKER_FIELDS = ("frame", "duration", "custom", "name", "note", "color")
ITEM_FIELDS = ("uid", "media_id", "media_path", "source_start", "source_end", "start", "end", "enabled", "properties")
TRACK_FIELDS = ("index", "name", "locked", "enabled")
# What engineering checks cannot judge; a user confirms each before marking an edit complete.
MANUAL_SCOPE = ("画面内容与口播是否匹配", "镜头时长例外与视觉连贯性", "字幕、声音听审和成片导出")
RATES = {"23.976": 24000 / 1001, "29.97": 30000 / 1001, "59.94": 60000 / 1001}


def rate(value):
    """Use the actual timeline rate, including rates outside the creation menu."""
    text = str(value).strip()
    number = RATES.get(text)
    if number is None:
        number = float(text)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("时间线帧率无法读取。")
    return number


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("时间线区间不是有效帧数。")
    return value


def snapshot(value):
    """Check shape before consuming a worker reply; malformed data never passes."""
    if not isinstance(value, dict) or not isinstance(value.get("tracks"), dict):
        raise ValueError("达芬奇回读缺少轨道资料。")
    _number(value.get("start_frame"))
    _number(value.get("end_frame"))
    rate(value.get("fps"))
    for kind in ("video", "audio"):
        tracks = value["tracks"].get(kind)
        if not isinstance(tracks, list):
            raise ValueError("达芬奇回读的轨道资料格式无效。")
        for track in tracks:
            if not isinstance(track, dict) or not isinstance(track.get("items"), list):
                raise ValueError("达芬奇回读的片段资料格式无效。")
            for item in track["items"]:
                if not isinstance(item, dict) or _number(item.get("end")) <= _number(item.get("start")):
                    raise ValueError("达芬奇回读包含无效片段区间。")
    if not isinstance(value.get("markers"), list) or any(not isinstance(marker, dict) for marker in value["markers"]):
        raise ValueError("达芬奇回读的标记资料格式无效。")
    for marker in value["markers"]:
        _number(marker.get("frame"))
        _number(marker.get("duration"))
    return copy.deepcopy(value)


def voice_track(inspect):
    tracks = inspect["tracks"]["audio"]
    return next((track for track in tracks if track.get("name") == "配音"), None) or next(
        (track for track in tracks if track.get("index") == 2), None)


def _identity(item):
    path = str(item.get("media_path") or "").replace("\\", "/").casefold()
    return ("path", path) if path else (("media", str(item.get("media_id"))) if item.get("media_id") else None)


def _same_source(left, right):
    if left.get("media_id") and right.get("media_id") and left["media_id"] == right["media_id"]:
        return True
    return _identity(left) is not None and _identity(left) == _identity(right)


def _canonical(items, fields):
    return sorted(({key: item.get(key) for key in fields} for item in items), key=lambda item: repr(item))


def shot_signature(ranges):
    """The checked shots in a comparable form; item ids are left out since only the cut itself matters."""
    return sorted([str(_identity(item)), item.get("source_start"), item.get("source_end"), item.get("start"), item.get("end")]
                  for item in ranges if isinstance(item, dict))


def _intervals_uncovered(intervals, start, end):
    uncovered, cursor = [], start
    for left, right in sorted(intervals):
        if right <= cursor or left >= end:
            continue
        if left > cursor:
            uncovered.append((cursor, min(left, end)))
        cursor = max(cursor, min(right, end))
    if cursor < end:
        uncovered.append((cursor, end))
    return uncovered


def reported_gaps(report, voice_end):
    """Missing-footage gaps as the edit's final json declares them; the timeline itself carries no gap markers."""
    found, gaps = [], report.get("gaps") if isinstance(report, dict) else None
    for gap in gaps if isinstance(gaps, list) else []:
        if not isinstance(gap, dict):
            continue
        left, right = gap.get("start"), gap.get("end")
        if type(left) is not int or type(right) is not int or not 0 <= left < right <= voice_end:
            continue
        found.append({"start": left, "end": right, "need": str(gap.get("need") or "").strip()[:300]})
    return found


def _declared(spans, picture_gaps):
    """Picture gaps a declaration touches. A reported range need not hit exact frames: overlapping one is enough."""
    return [(a, b) for a, b in picture_gaps if any(max(a, left) < min(b, right) for left, right in spans)]


def engineering_summary(inspect, job, record, fps=None, report=None):
    inspect = snapshot(inspect)
    fps = inspect["fps"]
    start, actual_rate = inspect["start_frame"], rate(fps)
    voice = voice_track(inspect)
    voice_items = voice["items"] if voice else []
    voice_end = max((item["end"] for item in voice_items), default=inspect["end_frame"]) - start
    shots = sorted((item["start"] - start, item["end"] - start, item.get("name", ""))
                   for track in inspect["tracks"]["video"] if track.get("enabled") is not False
                   for item in track["items"] if item.get("enabled") is not False)
    limit = math.floor(actual_rate)
    gaps = [{"start": marker["frame"], "end": marker["frame"] + marker["duration"], "name": marker.get("name") or "",
             "note": marker.get("note") or "", "seconds": round(marker["frame"] / actual_rate, 2)}
            for marker in inspect["markers"] if GAP_NAME.match(marker.get("name") or "") and marker.get("color") == "Red"
            and marker["duration"] > 0 and str(marker.get("note") or "").strip()]
    intervals = [(left, right) for left, right, _ in shots]
    raw_uncovered = _intervals_uncovered(intervals, 0, voice_end)
    declared = reported_gaps(report, voice_end)
    for left, right in _declared([(gap["start"], gap["end"]) for gap in declared], raw_uncovered):
        need = "；".join(gap["need"] for gap in declared if max(left, gap["start"]) < min(right, gap["end"]) and gap["need"])
        gaps.append({"start": left, "end": right, "name": need or "缺素材", "note": "", "seconds": round(left / actual_rate, 2),
                     "source": "report"})
    uncovered = _intervals_uncovered(intervals + [(gap["start"], gap["end"]) for gap in gaps], 0, voice_end)
    prefix = f"workbench-group:{job['id']}:"
    groups = [marker for marker in inspect["markers"] if str(marker.get("custom") or "").startswith(prefix)]
    details = lambda spans: [{"start": left, "end": right, "seconds": round(left / actual_rate, 2)} for left, right in spans]
    return {"timeline": inspect.get("name"), "fps": fps, "voice_frames": voice_end, "voice_seconds": round(voice_end / actual_rate, 2),
            "shots": len(shots), "long_shots": sum(right - left > limit for left, right, _ in shots), "frame_limit": limit,
            "max_shot": max((right - left for left, right, _ in shots), default=0), "gaps": gaps, "uncovered": details(uncovered),
            "picture_gaps": details(raw_uncovered), "group_markers": len(groups), "groups_expected": len(job["lines"]),
            "voice_items": len(voice_items), "voice_changed": voice_end != record.get("frames") or len(voice_items) != 1}


def failed_validation(code, message, **details):
    return {"schema_version": 1, "status": "verification_failed", "checked_at": now(),
            "issues": [{"code": code, "message": str(message)[:1000], "severity": "failure", **details}], "checks": {}}


def validate_edit(inspect, job, timeline, baseline=None, *, execution_status="completed", require_saved=True, recheck=False, report=None):
    """Compare immutable baseline details and check edited source structure.

    Missing footage is declared in the edit's final json (`report["gaps"]`); red gap markers from older edits still count."""
    try:
        inspect = snapshot(inspect)
        summary = engineering_summary(inspect, job, timeline, report=report)
    except Exception as exc:
        return failed_validation("readback_invalid", str(exc)), None
    issues, checks = [], {}

    def issue(code, message, severity="repair", **details):
        issues.append({"code": code, "message": message, "severity": severity, **details})

    if execution_status != "completed":
        issue("execution_incomplete", "剪辑执行没有正常结束，不能作为通过核验的交付。", "failure", execution_status=execution_status)
    if require_saved and inspect.get("saved") is not True:
        issue("project_not_saved", "达芬奇没有确认工程保存，须先在达芬奇中核对并保存。", "failure")
    checks["project_saved"] = inspect.get("saved") is True

    # Unknown worker fields are explicit failures, rather than comparisons of two nulls.
    missing = []
    for key in ("unique_id", "width", "height"):
        if not inspect.get(key):
            missing.append(key)
    for kind, tracks in inspect["tracks"].items():
        for track in tracks:
            for key in TRACK_FIELDS:
                if track.get(key) is None:
                    missing.append(f"{kind}.{track.get('index')}.{key}")
    voice = voice_track(inspect)
    if not voice or not voice["items"]:
        issue("voice_missing", "配音轨或配音片段已经缺失。")
    for item in (voice or {}).get("items", []):
        for key in ITEM_FIELDS:
            if key in {"media_id", "media_path"} or (recheck and key in {"properties", "enabled"}):
                continue
            if item.get(key) is None:
                missing.append(f"voice.{key}")
        if not _identity(item):
            missing.append("voice.media_identity")
    if missing:
        issue("readback_incomplete", "当前回读缺少核验所需的工程属性，无法确认是否保留。", "failure", fields=sorted(set(missing)))

    if not baseline:
        issue("baseline_missing", "旧记录没有真实剪前快照，配音、意群标记和用户属性是否保留尚未核验。", uncertain=True)
        checks["baseline_comparison"] = False
    else:
        try:
            baseline = snapshot(baseline)
        except Exception:
            baseline = None
            issue("baseline_invalid", "剪前快照无法读取，不能确认受保护内容是否保留。", uncertain=True)
        if baseline:
            checks["baseline_comparison"] = True
            protected = ("unique_id", "start_frame", "fps", "width", "height")
            changed = [key for key in protected if str(inspect.get(key)) != str(baseline.get(key))]
            if changed:
                issue("timeline_settings_changed", "时间线身份、起点、帧率或分辨率与剪前不同。", fields=changed)
            base_voice = voice_track(baseline)
            base_voice_items = (base_voice or {}).get("items", [])
            voice_fields = tuple(key for key in ITEM_FIELDS if not recheck or key not in {"properties", "enabled"})
            incomplete_base = [key for item in base_voice_items for key in voice_fields
                               if key not in {"media_id", "media_path"} and item.get(key) is None]
            if not base_voice_items or incomplete_base:
                issue("baseline_voice_incomplete", "剪前配音快照缺少媒体、源点或属性，无法完整比较。", uncertain=True,
                      fields=sorted(set(incomplete_base)))
            changed_voice = (_canonical(base_voice_items, voice_fields) != _canonical((voice or {}).get("items", []), voice_fields))
            summary["voice_changed"] = changed_voice
            checks["voice_preserved"] = not changed_voice and not incomplete_base and bool(base_voice_items)
            if changed_voice:
                issue("voice_changed", "配音媒体、源区间、位置、启用状态或属性与剪前不同。")
            prefix = f"workbench-group:{job['id']}:"
            base_groups = [marker for marker in baseline["markers"] if str(marker.get("custom") or "").startswith(prefix)]
            groups = [marker for marker in inspect["markers"] if str(marker.get("custom") or "").startswith(prefix)]
            expected_ids = {prefix + str(line.get("number", index)) for index, line in enumerate(job["lines"], 1)}
            baseline_groups_valid = (len(base_groups) == len(expected_ids) and {marker.get("custom") for marker in base_groups} == expected_ids)
            if not baseline_groups_valid:
                issue("baseline_groups_incomplete", "剪前意群标记数量或编号不完整，不能宣称已保留全部意群。", uncertain=True)
            groups_same = _canonical(base_groups, MARKER_FIELDS) == _canonical(groups, MARKER_FIELDS)
            checks["groups_preserved"] = groups_same and baseline_groups_valid
            if not groups_same:
                issue("group_markers_changed", "意群标记的帧位置、范围、编号、文字、备注或颜色与剪前不同。")
            tracks_preserved = True
            for kind, tracks in baseline["tracks"].items():
                current = {track["index"]: track for track in inspect["tracks"].get(kind, [])}
                for track in tracks:
                    other = current.get(track.get("index"))
                    track_fields = tuple(key for key in TRACK_FIELDS if not recheck or kind != "audio" or key not in {"locked", "enabled"})
                    if not other or any(track.get(key) != other.get(key) for key in track_fields):
                        tracks_preserved = False
                        issue("track_properties_changed", "已有轨道的名称、锁定或启用状态与剪前不同。", track_type=kind, track=track.get("index"))
                    elif not recheck and (kind == "subtitle" or (kind == "audio" and track.get("index", 0) > 2)):
                        if _canonical(track["items"], ITEM_FIELDS + ("name",)) != _canonical(other["items"], ITEM_FIELDS + ("name",)):
                            tracks_preserved = False
                            issue("protected_items_changed", "已有字幕或额外音频轨的片段与剪前不同。", track_type=kind, track=track.get("index"))
            checks["track_properties_preserved"] = tracks_preserved

    if summary["uncovered"]:
        issue("unmarked_picture_gap", "配音范围内仍有画面空段，剪辑回复里没有报为缺素材。", ranges=summary["uncovered"])
    valid_gaps = []
    for marker in inspect["markers"]:
        if not GAP_NAME.match(marker.get("name") or ""):
            continue
        left, right = marker["frame"], marker["frame"] + marker["duration"]
        if (marker.get("color") != "Red" or marker["duration"] <= 0 or not str(marker.get("note") or "").strip()
                or left < 0 or right > summary["voice_frames"]):
            issue("invalid_missing_marker", "缺素材标记须为配音范围内的红色范围标记，并写明所需画面。", frame=left)
            continue
        # Stale markers cannot label already-covered footage as missing material.
        uncovered = [(gap["start"], gap["end"]) for gap in summary["picture_gaps"]]
        if not any(a <= left and right <= b for a, b in uncovered):
            issue("missing_marker_not_gap", "缺素材标记没有准确对应当前画面空段，请核对或移除旧标记。", frame=left)
            continue
        valid_gaps.append({"start": left, "end": right, "name": marker.get("name"), "note": marker.get("note")})
    picture_gaps = [(gap["start"], gap["end"]) for gap in summary["picture_gaps"]]
    for gap in reported_gaps(report, summary["voice_frames"]):
        if not _declared([(gap["start"], gap["end"])], picture_gaps):
            issue("reported_gap_not_gap", "剪辑回复报的缺素材区间在时间线上已有画面，请核对。", ranges=[gap])
    valid_gaps += [gap for gap in summary["gaps"] if gap.get("source") == "report"]
    checks["picture_coverage"] = not summary["uncovered"]
    if valid_gaps:
        issue("missing_material", "已保留并报明缺素材空段，须补齐素材后重新核验。", "material", ranges=valid_gaps)

    videos = [item for track in inspect["tracks"]["video"] for item in track["items"]]
    audio = [item for track in inspect["tracks"]["audio"] if track is not voice for item in track["items"]]
    ranges, structural_unknown, pairing_ok, source_unique = [], [], True, True
    for item in videos:
        identity = _identity(item)
        if (not identity or item.get("source_start") is None or item.get("source_end") is None
                or not isinstance(item.get("enabled"), bool)):
            structural_unknown.append(item.get("uid") or item.get("name") or "未识别镜头")
            continue
        try:
            left, right = _number(item["source_start"]), _number(item["source_end"])
            if right <= left:
                raise ValueError("empty source range")
        except ValueError:
            issue("source_range_invalid", "镜头的真实源区间无效。", item=item.get("uid"))
            continue
        ranges.append({"media_id": item.get("media_id"), "media_path": item.get("media_path"), "source_start": left,
                       "source_end": right, "start": item["start"], "end": item["end"], "uid": item.get("uid")})
        for previous in ranges[:-1]:
            if _same_source(item, previous) and max(left, previous["source_start"]) < min(right, previous["source_end"]):
                source_unique = False
                issue("source_range_reused", "同一源媒体的区间在这条时间线中重复使用。", items=[previous.get("uid"), item.get("uid")])
        if item.get("has_audio") is False:
            continue
        if item.get("has_audio") is not True or not isinstance(item.get("linked_ids"), list) or not item.get("uid"):
            structural_unknown.append(item.get("uid") or item.get("name") or "未识别镜头")
            continue
        matches = [sound for sound in audio if _same_source(item, sound) and all(item.get(key) == sound.get(key)
                   for key in ("source_start", "source_end", "start", "end"))]
        paired = any(sound.get("uid") in item["linked_ids"] and item["uid"] in (sound.get("linked_ids") or []) for sound in matches)
        if not paired:
            pairing_ok = False
            issue("original_audio_unpaired", "视频切片缺少媒体、源区间和位置一致的逐片原声链接。", item=item.get("uid"))
    if structural_unknown:
        issue("source_structure_unverified", "镜头源身份、源区间、音源或链接资料不完整，无法核验原声和去重。", "failure",
              items=sorted(set(structural_unknown)))
    checks["source_ranges_unique"] = source_unique and not structural_unknown
    checks["original_audio_paired"] = pairing_ok and not structural_unknown
    if inspect.get("dedup_complete") is not True or not isinstance(inspect.get("other_shots"), list):
        issue("external_sources_unverified", "相关成片的源区间回读不完整，尚不能确认新增镜头没有跨片重复。", "failure")
        checks["external_source_ranges_unique"] = False
    else:
        checks["external_source_ranges_unique"] = True
        previous_videos = [item for track in (baseline or {}).get("tracks", {}).get("video", []) for item in track["items"]]
        for item in videos:
            # Existing shots can remain in a revision. Only newly selected
            # source ranges need to avoid all relevant finished timelines.
            if any(_same_source(item, previous) and item.get("source_start") == previous.get("source_start")
                   and item.get("source_end") == previous.get("source_end") for previous in previous_videos):
                continue
            if not _identity(item) or item.get("source_start") is None or item.get("source_end") is None:
                continue
            for previous in inspect["other_shots"]:
                if not isinstance(previous, dict):
                    checks["external_source_ranges_unique"] = False
                    issue("external_sources_invalid", "相关成片源区间资料格式无效，无法核验跨片重复。", "failure")
                    continue
                if not _same_source(item, previous):
                    continue
                try:
                    left, right = _number(previous.get("source_start")), _number(previous.get("source_end"))
                except ValueError:
                    checks["external_source_ranges_unique"] = False
                    issue("external_sources_invalid", "相关成片缺少真实源入出点，无法核验跨片重复。", "failure",
                          timeline=previous.get("timeline_name"))
                    continue
                if max(item["source_start"], left) < min(item["source_end"], right):
                    checks["external_source_ranges_unique"] = False
                    issue("source_range_used_elsewhere", "新增镜头的源区间与另一条成片重叠。", item=item.get("uid"),
                          timeline=previous.get("timeline_name"), timeline_uid=previous.get("timeline_uid"))
    severity = {entry["severity"] for entry in issues}
    status = "verification_failed" if "failure" in severity else "needs_repair" if "repair" in severity else "needs_material" if valid_gaps else "awaiting_review"
    return {"schema_version": 1, "status": status, "issues": issues, "checked_at": now(), "checks": checks,
            "source_ranges": ranges, "saved": inspect.get("saved"), "baseline_available": bool(baseline),
            "manual_scope": list(MANUAL_SCOPE),
            "source_scope": "本条时间线内及相关成片的源区间；既有镜头可在本版修订中保留"}, summary
