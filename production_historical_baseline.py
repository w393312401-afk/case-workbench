"""Recover a genuine pre-resumption snapshot from independent saved artifacts.

This module reads historical task files only. It never reads Resolve or fills
missing historical fields from the current timeline.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

from direction_store import now
from elevenlabs_client import VoiceError
from production_validation import MARKER_FIELDS, snapshot
from workbench_production import meaning_markers

CATALOG_NAME = re.compile(r"[0-9a-f]{64}\.json")
EDIT_ID = re.compile(r"EDT-[0-9a-f]{12}")
MAX_FILE_BYTES = 64 * 1024 * 1024


def _refuse(message):
    raise VoiceError(409, "历史核验依据无法恢复，未补写基线：" + message)


def _time(value, label):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone missing")
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        _refuse(f"{label}缺少可信的时间记录。")


def _bytes(path):
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            _refuse(f"{path.name}超过历史资料容量限制。")
        return path.read_bytes()
    except OSError as exc:
        _refuse(f"缺少或无法读取 {path.name}（{str(exc)[:120]}）。")


def _json(raw, label):
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        _refuse(f"{label}不是完整的 JSON 资料。")
    if not isinstance(value, dict):
        _refuse(f"{label}格式无效。")
    return value


def _source(path, raw, role):
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(), "role": role}


def _path(value):
    return str(value or "").replace("\\", "/").casefold()


def _markers_equal(left, right):
    canonical = lambda rows: sorted([tuple(row.get(key) for key in MARKER_FIELDS) for row in rows], key=repr)
    return canonical(left) == canonical(right)


def _handoff(text, project, timeline):
    project_names = re.findall(r"达芬奇项目[：:]\s*([^（(；;\n]+)", text)
    target = re.findall(r"目标时间线[：:]\s*([^（(\n]+)[（(]UniqueId\s+([^）)\s]+)[）)]\s*[，,]\s*([0-9.]+)\s*fps[^\n]*?([0-9]+)\s*[×x]\s*([0-9]+)", text)
    voice = re.findall(r"从第\s*0\s*帧开始[，,]\s*共\s*([0-9]+)\s*帧", text)
    if len(project_names) != 1 or project_names[0].strip() != project or len(target) != 1 or len(voice) != 1:
        _refuse("原交接文件的项目、时间线或配音区间不完整。")
    name, uid, fps, width, height = target[0]
    if name.strip() != timeline["name"] or uid != timeline["unique_id"] or str(fps) != str(timeline["fps"]):
        _refuse("原交接文件的时间线身份或帧率与独立建线记录矛盾。")
    if int(voice[0]) != timeline["frames"]:
        _refuse("原交接文件的配音帧数与独立建线记录矛盾。")
    ranges = re.findall(r"^\|\s*(\d+)\s*\|[^\n]*?\[\s*(\d+)\s*,\s*(\d+)\s*\)\s*\|", text, re.M)
    if not ranges or len({number for number, _, _ in ranges}) != len(ranges):
        _refuse("原交接文件的意群区间缺失或编号重复。")
    return {"fps": fps, "width": int(width), "height": int(height),
            "ranges": {int(number): (int(start), int(end)) for number, start, end in ranges}}


def _catalogs(root, job, edit, timeline, take, captured):
    """Find matching content-addressed catalogs from before this edit was started."""
    matches = []
    for path in sorted((root / "backups").glob("*.json")):
        if not CATALOG_NAME.fullmatch(path.name):
            continue
        raw = _bytes(path)
        if job["id"].encode() not in raw:
            continue
        value = _json(raw, path.name)
        old_jobs = [item for item in value.get("jobs", []) if isinstance(item, dict) and item.get("id") == job["id"]]
        if len(old_jobs) != 1:
            continue
        old_job = old_jobs[0]
        # Catalogs containing this edit are later evidence, not a source from
        # which to reconstruct its pre-edit inputs.
        if any(row.get("id") == edit["id"] for row in old_job.get("edits", []) if isinstance(row, dict)):
            continue
        old_timelines = [row for row in old_job.get("timelines", []) if row.get("unique_id") == timeline["unique_id"]]
        old_takes = [row for row in old_job.get("takes", []) if row.get("id") == take["id"]]
        if not old_timelines or not old_takes:
            continue
        if hashlib.sha256(raw).hexdigest() != path.stem:
            _refuse(f"独立资料 {path.name} 的内容哈希与文件名不一致。")
        if len(old_timelines) != 1 or len(old_takes) != 1:
            _refuse("独立建线资料里存在重复的时间线或配音编号。")
        old_timeline, old_take = old_timelines[0], old_takes[0]
        for key in ("name", "unique_id", "take_id", "fps", "frames", "project"):
            if old_timeline.get(key) != timeline.get(key):
                _refuse(f"独立建线资料的 {key} 与这条制作单矛盾。")
        for key in ("product", "script_id", "script_version"):
            if old_job.get(key) != job.get(key):
                _refuse("独立稿件版本与当前制作单不是同一版本。")
        for key in ("wav", "audio", "duration", "groups", "tts_lines", "tts_sha256"):
            if old_take.get(key) != take.get(key):
                _refuse(f"独立配音资料的 {key} 与制作单矛盾。")
        old_settings = (value.get("settings") or {}).get("products", {}).get(old_job["product"], {})
        if old_settings.get("project", old_job["product"]) != old_timeline["project"]:
            _refuse("独立项目配置与建线项目矛盾。")
        if _time(old_timeline.get("built_at"), "独立建线资料") > captured or _time(old_take.get("created_at"), "独立配音资料") > captured:
            _refuse("独立建线或配音资料晚于续剪前取证时间。")
        matches.append((old_job, old_take, old_settings, _source(path, raw, "pre_edit_catalog")))
    if not matches:
        _refuse("没有找到内容哈希完整且匹配原制作单的建线前资料备份。")
    return matches


def _native_backup(raw, timeline, voice):
    """Check the native DRT's empty picture/A1 and one original voice clip."""
    try:
        from io import BytesIO
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                _refuse("原生时间线备份里存在重复档案。")
            containers = [name for name in names if re.fullmatch(r"SeqContainer/[^/]+\.xml", name)]
            pools = [name for name in names if name.endswith("MpFolder.xml")]
            if len(containers) != 1 or not pools:
                _refuse("原生时间线备份不包含唯一目标时间线。")
            def xml(name):
                if archive.getinfo(name).file_size > MAX_FILE_BYTES:
                    _refuse("原生时间线备份的 XML 过大。")
                text = archive.read(name).decode("utf-8-sig")
                # Resolve exports C++ class tags such as ListMgt::LmVersionTable,
                # which are not XML namespace names. Normalize tags in memory;
                # audit hashes always refer to the unchanged original bytes.
                text = re.sub(r"(<\/?)([A-Za-z_][\w:.-]*)", lambda match: match[1] + match[2].replace(":", "_"), text)
                return ET.fromstring(text)
            pool_trees = [xml(name) for name in pools]
            labels = [node.text for tree in pool_trees for node in tree.findall(".//Sm2Timeline/Name")]
            if labels != [timeline["name"]]:
                _refuse("原生时间线备份的名称与目标不一致。")
            tree = xml(containers[0])
            videos = tree.findall("./VideoTrackVec/Element/Sm2TiTrack")
            audios = tree.findall("./AudioTrackVec/Element/Sm2TiTrack")
            if len(videos) != 1 or len(audios) != 2:
                _refuse("原生时间线备份的轨道数量与历史快照矛盾。")
            if list(videos[0].find("Items")) or list(audios[0].find("Items")):
                _refuse("原生时间线备份的画面或原声轨并非空轨。")
            entries = audios[1].findall("./Items/Element")
            clips = audios[1].findall("./Items/Element/Sm2TiAudioClip")
            if len(entries) != 1 or len(clips) != 1:
                _refuse("原生时间线备份不是唯一独立配音片段。")
            clip = clips[0]
            media_ref = clip.findtext("MediaRef")
            media = [node for pool_tree in pool_trees for node in pool_tree.iter() if node.get("DbId") == media_ref]
            if (_path(clip.findtext("MediaFilePath")) != _path(voice["media_path"]) or
                    clip.findtext("Name") != voice.get("name") or not media_ref or len(media) != 1 or
                    _path(media[0].findtext("Name")) != _path(voice["media_path"]).rsplit("/", 1)[-1]):
                _refuse("原生时间线备份的配音引用与历史快照矛盾。")
            start = float(clip.findtext("Start", "nan").split("|", 1)[0])
            duration = float(clip.findtext("Duration", "nan").split("|", 1)[0])
            source = float(clip.findtext("MediaStartTime", "nan").split("|", 1)[0])
            if (start, duration, source) != (voice["start"], voice["end"] - voice["start"], voice["source_start"]):
                _refuse("原生时间线备份的配音起点、时长或源点与历史快照矛盾。")
    except VoiceError:
        raise
    except (OSError, ValueError, UnicodeError, zipfile.BadZipFile, ET.ParseError, TypeError, AttributeError) as exc:
        _refuse(f"原生时间线备份无法完整核对（{type(exc).__name__}）。")


def _restore_historical_baseline(production_root, job, edit, timeline, take):
    """Return historical baseline/evidence extra fields, without current-state inputs."""
    if not EDIT_ID.fullmatch(str(edit.get("id"))) or edit.get("timeline_uid") != timeline.get("unique_id") or edit.get("take_id") != take.get("id"):
        _refuse("剪辑、时间线和配音身份不一致。")
    root = Path(production_root).resolve()
    folder = root / "edits" / edit["id"]
    captured_text = (edit.get("resumption") or {}).get("started_at")
    captured = _time(captured_text, "Codex 接续记录")
    finished = _time(edit.get("finished_at"), "原剪辑结束记录")
    if (edit.get("resumption") or {}).get("by") != "Codex" or not _time(edit.get("started_at"), "原剪辑开始记录") <= captured < finished:
        _refuse("历史快照不是这次 Codex 接续剪辑之前的资料。")
    paths = {"resume_snapshot": folder / "resume_snapshot.json", "handoff": folder / "handoff.md",
             "native_timeline_backup": folder / "resume_before.drt"}
    raw = {role: _bytes(path) for role, path in paths.items()}
    if abs(paths["resume_snapshot"].stat().st_mtime - captured.timestamp()) > 1:
        _refuse("续剪前快照文件时间与原接续记录矛盾。")
    for role in ("handoff", "native_timeline_backup"):
        if paths[role].stat().st_mtime >= finished.timestamp():
            _refuse("交接文件或原生备份晚于原剪辑结束，不是剪前证据。")
    archive = _json(raw["resume_snapshot"], "resume_snapshot.json")
    if archive.get("project") != timeline.get("project") or archive.get("active") != timeline["unique_id"]:
        _refuse("历史快照没有在目标项目、目标活动时间线上取证。")
    candidates = [row for row in archive.get("timelines", []) if isinstance(row, dict) and row.get("uid") == timeline["unique_id"]]
    if len(candidates) != 1:
        _refuse("历史快照的目标时间线缺失或重复。")
    old = candidates[0]
    if old.get("name") != timeline["name"] or str(old.get("fps")) != str(timeline["fps"]) or old.get("end") - old.get("start") != timeline["frames"]:
        _refuse("历史快照的时间线身份、帧率或总区间与建线记录矛盾。")
    handoff = _handoff(raw["handoff"].decode("utf-8-sig"), timeline["project"], timeline)
    catalogs = _catalogs(root, job, edit, timeline, take, captured)
    markers = [{"frame": float(frame) if "." in frame else int(frame), "duration": info.get("duration"), "custom": info.get("customData"),
                "name": info.get("name"), "note": info.get("note"), "color": info.get("color")}
               for frame, info in old.get("markers", {}).items()]
    custom_ids = [marker.get("custom") for marker in markers]
    if len(custom_ids) != len(set(custom_ids)):
        _refuse("历史快照的意群编号重复。")
    for old_job, old_take, settings, _ in catalogs:
        if (settings.get("width", 1080), settings.get("height", 1920), str(settings.get("fps", "29.97"))) != (
                handoff["width"], handoff["height"], handoff["fps"]):
            _refuse("原交接文件与独立建线前配置矛盾。")
        try:
            candidates = [meaning_markers(old_job, old_take, timeline["fps"], legacy=legacy, lead_frames=timeline.get("lead_frames", 0))
                          for legacy in (False, True)]
        except Exception:
            _refuse("独立配音意群资料无法重建完整标记。")
        candidates = [[{**marker, "color": "Blue", "duration": min(marker["duration"], timeline["frames"] - marker["frame"])}
                       for marker in expected] for expected in candidates]
        expected = next((rows for rows in candidates if _markers_equal(markers, rows)), None)
        if expected is None or len(handoff["ranges"]) != len(expected):
            _refuse("历史意群标记的区间、文字、备注或颜色与独立原稿资料矛盾。")
        if any(handoff["ranges"].get(marker["number"]) != (marker["frame"], marker["frame"] + marker["duration"]) for marker in expected):
            _refuse("原交接文件的意群区间与历史快照矛盾。")
    tracks = {"video": [], "audio": []}
    for track in old.get("tracks", []):
        kind = track.get("type")
        if kind not in tracks:
            _refuse("历史快照含未识别轨道，不能猜补保护范围。")
        items = [{"uid": item.get("uid"), "name": item.get("name"), "media_id": item.get("media_id"), "media_path": item.get("path"),
                  "source_start": item.get("src_in"), "source_end": item.get("src_end"), "start": item.get("start"), "end": item.get("end"),
                  "properties": copy.deepcopy(item.get("properties")), "enabled": item.get("enabled")} for item in track.get("items", [])]
        tracks[kind].append({key: copy.deepcopy(track.get(key)) for key in ("index", "name", "locked", "enabled")} | {"items": items})
    if ([track["index"] for track in tracks["video"]] != [1] or [track["index"] for track in tracks["audio"]] != [1, 2] or
            tracks["video"][0]["items"] or tracks["audio"][0]["items"] or len(tracks["audio"][1]["items"]) != 1):
        _refuse("历史目标时间线不是空画面、空原声及唯一配音的剪前结构。")
    voice = tracks["audio"][1]["items"][0]
    wav = catalogs[0][1].get("wav")
    expected_voice = (root / "audio" / str(wav or "")).resolve()
    if not wav or not expected_voice.is_relative_to(root / "audio") or _path(voice["media_path"]) != _path(expected_voice):
        _refuse("历史配音媒体不是独立配音版本保存的 WAV 引用。")
    if (not isinstance(voice.get("properties"), dict) or not voice.get("uid") or not voice.get("media_id") or
            any(voice.get(key) is None for key in ("source_start", "source_end", "start", "end"))):
        _refuse("历史配音缺少原媒体身份、源点、位置或属性。")
    if (voice["source_start"], voice["source_end"], voice["start"], voice["end"]) != (0, timeline["frames"], old["start"], old["end"]):
        _refuse("历史配音源点或时间线位置与建线区间矛盾。")
    if any(not isinstance(track.get(key), bool) for kind in tracks.values() for track in kind for key in ("locked", "enabled")):
        _refuse("历史目标轨道缺少实际锁定或启用状态。")
    _native_backup(raw["native_timeline_backup"], timeline, voice)
    baseline = snapshot({"project": archive["project"], "name": old["name"], "unique_id": old["uid"], "start_frame": old["start"],
                         "end_frame": old["end"], "fps": str(old["fps"]), "width": handoff["width"], "height": handoff["height"],
                         "tracks": tracks, "markers": sorted(markers, key=lambda marker: marker["frame"])})
    return {"baseline": baseline, "baseline_at": captured_text, "baseline_origin": "historical_resume",
            "baseline_evidence": {"schema_version": 1, "kind": "historical_resume", "captured_at": captured_text, "restored_at": now(),
                "sources": [_source(paths[role], raw[role], role) for role in paths] + [row[3] for row in catalogs],
                "scope": "Codex接续剪辑前", "unrecorded_fields": ["voice.clip_enabled"] if voice["enabled"] is None else [],
                "configured_fields": ["width", "height"]}}


def restore_historical_baseline(production_root, job, edit, timeline, take):
    """Return recoverable history fields or refuse, without any current Resolve inputs."""
    try:
        return _restore_historical_baseline(production_root, job, edit, timeline, take)
    except VoiceError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        _refuse(f"历史资料字段缺失或格式矛盾（{type(exc).__name__}）。")
