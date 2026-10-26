"""Run one DaVinci Resolve operation in its own process and print one JSON result.

The workbench never loads fusionscript into its server process, so a hung or crashed Resolve call
ends with this process. Requests arrive as UTF-8 JSON on stdin; Chinese names are never source literals.
Only the project the user already has open is used; this worker never switches or creates projects.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile

API_DIRS = (os.environ.get("RESOLVE_SCRIPT_API"), r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting")
LIB_FILES = (os.environ.get("RESOLVE_SCRIPT_LIB"), r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll",
             r"D:\DaVinci Resolve\fusionscript.dll")
MARKER_PREFIX = "workbench-group:"


class WorkerError(Exception):
    pass


def connect():
    api = next((path for path in API_DIRS if path and Path(path, "Modules", "DaVinciResolveScript.py").is_file()), None)
    lib = next((path for path in LIB_FILES if path and Path(path).is_file()), None)
    if not api or not lib:
        raise WorkerError("没有找到达芬奇脚本组件（fusionscript.dll），请确认已安装 DaVinci Resolve Studio。")
    os.environ["RESOLVE_SCRIPT_API"], os.environ["RESOLVE_SCRIPT_LIB"] = api, lib
    sys.path.append(str(Path(api, "Modules")))
    import DaVinciResolveScript
    resolve = DaVinciResolveScript.scriptapp("Resolve")
    if not resolve:
        raise WorkerError("连不上达芬奇：请先打开 DaVinci Resolve，并在偏好设置 → 系统 → 常规里把“外部脚本使用”设为“本地”。")
    return resolve


def current_project(resolve, expected=None):
    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
        raise WorkerError("达芬奇里没有打开的项目。")
    name = project.GetName()
    if expected and name != expected:
        raise WorkerError(f"达芬奇当前打开的是项目「{name}」，不是「{expected}」。请先在达芬奇里打开「{expected}」再建时间线；工作台不会替你切换项目。")
    return project


def timelines(project):
    return [project.GetTimelineByIndex(index) for index in range(1, project.GetTimelineCount() + 1)]


FOLDER_DEPTH = 4
LOCATION = {}  # Where the project handed out by request_project lives; read by results that report it.


def project_candidates(manager, names):
    """Find exact project names (case-insensitive), without opening or closing projects."""
    found = []
    wanted = {name.casefold() for name in names}

    def walk(path):
        for name in manager.GetProjectListInCurrentFolder() or []:
            if name.casefold() in wanted:
                found.append((path, name))
        if len(path) >= FOLDER_DEPTH:
            return
        for child in manager.GetFolderListInCurrentFolder() or []:
            if manager.OpenFolder(child):
                walk(path + [child])
                manager.GotoParentFolder()

    manager.GotoRootFolder()
    walk([])
    return found


def project_folders(manager, name):
    return [path for path, _ in project_candidates(manager, [name])]


def creator_folder(paths, creator, name):
    """The one folder whose path names this creator, or None when no folder does (the caller then keeps the open project)."""
    wanted = str(creator or "").casefold()
    matches = [path for path in paths if wanted and wanted in "/".join(path).casefold()]
    if len(matches) > 1:
        raise WorkerError(f"达芬奇里有多个文件夹名含「{creator}」且都有项目「{name}」：{'、'.join('/'.join(path) for path in matches)}，无法判断用哪个。")
    return matches[0] if matches else None


def project_matches(project, request):
    """Whether the open project is the one the request means: same project id, or it holds the timeline being worked on.

    Same-named projects exist per channel, and their timelines never share ids."""
    wanted = request.get("project_id")
    if wanted:
        return optional(project, "GetUniqueId") == wanted
    uid = request.get("unique_id")
    return bool(uid) and any(item.GetUniqueId() == uid for item in timelines(project))


def request_project(resolve, request):
    """The DaVinci project a request is about.

    A known project/timeline id survives a rename. Otherwise, without a creator or folder this is the open project,
    which must carry the expected name. With one, the project lives in
    that creator's project-manager folder (the folder is saved on the timeline record, or found by the creator name in the
    folder path); if another same-named project is open, it is saved and closed and the creator's is opened."""
    name, folder, creator = request["project"], request.get("folder"), request.get("creator")
    LOCATION.clear()
    manager = resolve.GetProjectManager()
    current = manager.GetCurrentProject()
    identified = bool(request.get("project_id") or request.get("unique_id"))

    def checked(project):
        if identified and not project_matches(project, request):
            raise WorkerError(f"达芬奇项目「{project.GetName()}」不是工作台记录的那一个（可能被重建过），没有继续。")
        return project

    if current and project_matches(current, request):
        LOCATION.update({"folder": folder} if folder is not None else {})
        return current
    if folder is None and not creator:
        return checked(current_project(resolve, name))
    if request.get("current_only"):
        # A record-only read never closes the project the user has open to fetch another one.
        raise WorkerError(f"达芬奇当前打开的不是这次任务的项目「{name}」，没有切换项目。")
    names = [name]
    # The product setting stays shared by creators. A renamed channel project
    # may be called Zack-202H while that setting (and older records) says 202H.
    if creator and not name.casefold().startswith(f"{creator}-".casefold()):
        names.append(f"{creator}-{name}")
    candidates = project_candidates(manager, names)
    if folder is not None:
        path = [part for part in str(folder).split("/") if part]
        candidates = [(parts, actual) for parts, actual in candidates if parts == path]
        if not candidates:
            raise WorkerError(f"达芬奇文件夹「{folder}」里找不到项目「{' / '.join(names)}」，没有切换项目。")
    else:
        path = creator_folder([parts for parts, _ in candidates], creator, name)
        if path is None:
            return checked(current_project(resolve, name))
        candidates = [(parts, actual) for parts, actual in candidates if parts == path]
    if len(candidates) != 1:
        raise WorkerError(f"达芬奇文件夹「{'/'.join(path)}」里有多个匹配项目「{' / '.join(names)}」，无法判断用哪个，没有切换项目。")
    actual_name = candidates[0][1]
    if current:
        if not manager.SaveProject():
            raise WorkerError("达芬奇没能保存当前项目，没有切换项目。")
        if not manager.CloseProject(current):
            raise WorkerError(f"达芬奇没能关闭当前项目「{current.GetName()}」，没有切换到「{'/'.join(path)}」里的「{name}」。")
    manager.GotoRootFolder()
    for part in path:
        if not manager.OpenFolder(part):
            raise WorkerError(f"达芬奇里找不到文件夹「{part}」（路径 {'/'.join(path)}）。")
    project = manager.LoadProject(actual_name)
    if not project:
        raise WorkerError(f"达芬奇文件夹「{'/'.join(path)}」里打不开项目「{name}」；原来的项目已被关闭，请在达芬奇里重新打开需要的项目。")
    checked(project)
    LOCATION.clear()
    LOCATION.update(folder="/".join(path))
    return project


def located(project):
    return {"project_id": optional(project, "GetUniqueId") or "", **({"folder": LOCATION["folder"]} if "folder" in LOCATION else {})}


def library(project):
    """The project's material timelines: what a creator's footage is cut from."""
    return [item.GetName() for item in timelines(project) if "素材" in item.GetName() and not SKIPPED_TIMELINES.search(item.GetName())]


def status(resolve, request):
    project = request_project(resolve, request) if request.get("project") else current_project(resolve)
    current = project.GetCurrentTimeline()
    return {"version": resolve.GetVersionString(), "project": project.GetName(), **located(project),
            "page": optional(resolve, "GetCurrentPage") or "",
            "fps": str(project.GetSetting("timelineFrameRate")), "width": str(project.GetSetting("timelineResolutionWidth")),
            "height": str(project.GetSetting("timelineResolutionHeight")),
            "current_timeline": current.GetName() if current else "", "timeline_count": project.GetTimelineCount(),
            "current_timeline_uid": current.GetUniqueId() if current else "", "current_timecode": current.GetCurrentTimecode() if current else "",
            "timelines": [item.GetName() for item in timelines(project)]}


def same_setting(actual, wanted):
    try:
        return abs(float(actual) - float(wanted)) < 1e-6
    except (TypeError, ValueError):
        return str(actual) == str(wanted)


def folder(media_pool, name):
    root = media_pool.GetRootFolder()
    found = next((item for item in root.GetSubFolderList() or [] if item.GetName() == name), None)
    return found or media_pool.AddSubFolder(root, name)


def next_name(project, prefix):
    pattern = re.compile(re.escape(prefix) + r"(\d+)")
    used = [int(found.group(1)) for found in (pattern.match(item.GetName()) for item in timelines(project)) if found]
    return f"{prefix}{max(used, default=0) + 1}"


def new_name(project, request):
    """A chosen name is used as given and must be free; otherwise the prefix gets the next sequence number."""
    name = str(request.get("name") or "").strip()
    if not name:
        return next_name(project, request["prefix"])
    if any(item.GetName() == name for item in timelines(project)):
        raise WorkerError(f"项目里已有名为「{name}」的时间线，请换一个名称，或留空自动编号。")
    return name


def durable_json(path, value):
    """Keep recovery identity before touching Resolve, independently of the main catalog."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".resolve-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def optional(obj, method, *args):
    function = getattr(obj, method, None)
    try:
        return function(*args) if callable(function) else None
    except Exception:
        return None


def pool_clips(media_pool):
    pending = [media_pool.GetRootFolder()]
    while pending:
        current = pending.pop()
        yield from (optional(current, "GetClipList") or [])
        pending.extend(current.GetSubFolderList() or [])


def build_checkpoint(timeline, track):
    audio = [inspect_item(item) for item in timeline.GetItemListInTrack("audio", track) or []]
    for item in audio:
        item.pop("name", None)  # A clip label is not its source identity; our final naming changes it too.
    return {"settings": {key: optional(timeline, "GetSetting", key) for key in
            ("timelineFrameRate", "timelineResolutionWidth", "timelineResolutionHeight")},
            "start": timeline.GetStartFrame(), "track_name": optional(timeline, "GetTrackName", "audio", track),
            "audio": audio,
            "markers": {str(frame): info for frame, info in (timeline.GetMarkers() or {}).items()
                        if str(info.get("customData", "")).startswith(MARKER_PREFIX)}}


def guard_build_setup(target, journal, request):
    """Check early recovery before restoring any settings or track names."""
    wanted = {"timelineFrameRate": request["fps"], "timelineResolutionWidth": str(request["width"]),
              "timelineResolutionHeight": str(request["height"])}
    guard = journal.get("setup_guard")
    stage = journal.get("stage")
    if guard:
        # A crash during setup can leave either the recorded initial value or
        # our intended value. Later stages must retain the configured value.
        changed = target.GetStartFrame() != guard["start"]
        for setting, value in wanted.items():
            actual = optional(target, "GetSetting", setting)
            allowed = [value] + ([guard["settings"].get(setting)] if stage == "created" else [])
            changed |= not any(same_setting(actual, candidate) for candidate in allowed if candidate is not None)
        track_count = target.GetTrackCount("audio")
        changed |= track_count < guard["audio_tracks"] or track_count > max(guard["audio_tracks"], request["track_index"])
        if track_count >= request["track_index"]:
            allowed_names = {request["track_name"]}
            if stage == "created":
                allowed_names.add(guard.get("track_name"))
            changed |= optional(target, "GetTrackName", "audio", request["track_index"]) not in allowed_names
    elif stage == "creating" and journal.get("create_defaults"):
        # Creation succeeded but its response journal failed: only the untouched
        # inherited settings and an absent destination track are recoverable.
        changed = any(not same_setting(optional(target, "GetSetting", setting), value)
                      for setting, value in journal["create_defaults"].items())
        changed |= target.GetTrackCount("audio") >= request["track_index"]
    else:
        changed = True  # Old partial journals lack evidence for safe setup.
    if changed or target.GetName() != journal["temporary_name"]:
        raise WorkerError("原请求时间线的设置或配音轨已被修改或无法核实，请先核实；未覆盖用户修改或重复建线。")


def build_recoverable(resolve, request):
    """A request has a durable, unique timeline identity, including across worker crashes."""
    key, recovery_file = request.get("operation_key"), request.get("recovery_file")
    if not re.fullmatch(r"[0-9a-f]{64}", str(key or "")) or not recovery_file:
        raise WorkerError("建线恢复标识无效，未操作达芬奇。")
    project = request_project(resolve, request)
    media_pool = project.GetMediaPool()
    path = Path(recovery_file)
    digest = hashlib.sha256(json.dumps({k: v for k, v in request.items() if k not in
        {"command", "recovery_file", "operation_path"}}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    existing = path.exists()
    if existing:
        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WorkerError("建线恢复记录无法读取，请核实原时间线，未重复建线。") from exc
        if journal.get("hash") != digest:
            raise WorkerError("建线恢复标识已用于不同参数，未操作达芬奇。")
    else:
        name = new_name(project, request)
        journal = {"hash": digest, "operation_key": key, "stage": "prepared", "name": name,
                   "temporary_name": name + "__WB_" + key[:16]}
        durable_json(path, journal)

    target = next((t for t in timelines(project) if
                   t.GetUniqueId() == journal.get("timeline_uid") or t.GetName() == journal["temporary_name"]), None)
    if journal.get("stage") == "completed":
        if not target:
            raise WorkerError("原请求建好的时间线已不在当前工程中，请核实，未重复建线。")
        # Return the recorded outcome; never treat later manual edits as a different build result.
        return {**journal["result"], **located(project), "project": project.GetName(), "materials": library(project), "recovered": True}
    if journal.get("stage") in {"creating", "created", "placing", "placed", "marking", "renaming", "saving"} and not target:
        raise WorkerError("上次建线结果尚未确认，找不到原请求的时间线；请核实，未重复建线。")
    if target and journal.get("checkpoint"):
        # A result which only lacks its final local journal must never restore
        # old settings over changes the user has since made to that timeline.
        if (build_checkpoint(target, request["track_index"]) != journal["checkpoint"] or
                target.GetName() not in {journal["name"], journal["temporary_name"]}):
            raise WorkerError("原请求时间线的配音、标记或设置已被修改，请先核实；未覆盖用户修改或重复建线。")
        if target.GetName() == journal["temporary_name"] and not target.SetName(journal["name"]):
            raise WorkerError("原时间线未恢复正式名称，可用原请求恢复。")
        result = {**journal["built_result"], "name": target.GetName()}
        result["saved"] = bool(resolve.GetProjectManager().SaveProject()) if request.get("save", True) else False
        if not result["saved"]:
            result["warnings"] = ["时间线已建好，但工程保存未确认，请手动保存后重新核验。"]
        journal.update(stage="completed", result=result)
        durable_json(path, journal)
        return {**result, **located(project), "project": project.GetName(), "materials": library(project), "recovered": True}

    if target and existing:
        guard_build_setup(target, journal, request)

    audio = Path(request["audio"])
    if not audio.is_file():
        raise WorkerError("配音文件不存在，未重复生成或建线。")
    imported = next((clip for clip in pool_clips(media_pool) if
                     optional(clip, "GetUniqueId") == journal.get("audio_uid") and journal.get("audio_uid")), None)
    if not imported and journal.get("stage") == "importing":
        raise WorkerError("上次导入配音结果尚未确认，请核实媒体池，未重复导入或建线。")
    if not imported and journal.get("stage") not in {"prepared", "importing"}:
        # Fakes and old APIs may not expose pool UIDs; a placed item provides the exact imported clip.
        candidates = target.GetItemListInTrack("audio", request["track_index"]) if target else []
        imported = optional(candidates[0], "GetMediaPoolItem") if candidates else None
        if not imported:
            raise WorkerError("原请求的配音媒体无法核实，未重复导入或建线。")
    if not imported:
        journal["stage"] = "importing"
        durable_json(path, journal)
        media_pool.SetCurrentFolder(folder(media_pool, request["audio_bin"]))
        clips = media_pool.ImportMedia([str(audio)]) or []
        if len(clips) != 1:
            raise WorkerError("配音导入结果未确认，未继续建线。")
        imported = clips[0]
        journal.update(stage="imported", audio_uid=optional(imported, "GetUniqueId"))
        # The API guarantees pool unique IDs; without one an interrupted import cannot be resumed safely.
        durable_json(path, journal)
    if not target:
        journal.update(stage="creating", create_defaults={setting: project.GetSetting(setting) for setting in
            ("timelineFrameRate", "timelineResolutionWidth", "timelineResolutionHeight")})
        durable_json(path, journal)
        media_pool.SetCurrentFolder(folder(media_pool, request["timeline_bin"]))
        target = media_pool.CreateEmptyTimeline(journal["temporary_name"])
        if not target:
            raise WorkerError("建线结果尚未确认，请核实原请求，未重复建线。")
        journal.update(stage="created", timeline_uid=target.GetUniqueId(), setup_guard={
            "settings": {setting: optional(target, "GetSetting", setting) for setting in journal["create_defaults"]},
            "start": target.GetStartFrame(), "audio_tracks": target.GetTrackCount("audio"),
            "track_name": optional(target, "GetTrackName", "audio", request["track_index"])})
        durable_json(path, journal)
    elif not journal.get("setup_guard"):
        # The unique temporary timeline recovered from an interrupted create.
        journal.update(stage="created", timeline_uid=target.GetUniqueId(), setup_guard={
            "settings": {setting: optional(target, "GetSetting", setting) for setting in journal["create_defaults"]},
            "start": target.GetStartFrame(), "audio_tracks": target.GetTrackCount("audio"),
            "track_name": optional(target, "GetTrackName", "audio", request["track_index"])})
        durable_json(path, journal)
    if not project.SetCurrentTimeline(target):
        raise WorkerError("没能打开原请求的时间线，未重复建线。")
    wanted = {"timelineFrameRate": request["fps"], "timelineResolutionWidth": str(request["width"]),
              "timelineResolutionHeight": str(request["height"])}
    if any(not same_setting(optional(target, "GetSetting", setting) or project.GetSetting(setting), value)
           for setting, value in wanted.items()):
        if not target.SetSettings({"useCustomSettings": "1", **wanted}):
            raise WorkerError("原请求时间线的帧率或分辨率没有设置成功。")
    track = request["track_index"]
    while target.GetTrackCount("audio") < track:
        if not target.AddTrack("audio", "mono"):
            raise WorkerError("没能为原请求添加配音轨。")
    if optional(target, "GetTrackName", "audio", track) != request["track_name"]:
        if not target.SetTrackName("audio", track, request["track_name"]):
            raise WorkerError("原请求的配音轨名称未设置成功。")
    lead = int(request.get("lead_frames") or 0)  # Silence before the voiceover; markers still start at frame 0.
    placed = target.GetItemListInTrack("audio", track) or []
    if not placed:
        if journal["stage"] == "placing":
            raise WorkerError("上次放入配音的结果未确认，请核实原时间线，未重复放入。")
        journal["stage"] = "placing"
        durable_json(path, journal)
        placed = media_pool.AppendToTimeline([{"mediaPoolItem": imported, "mediaType": 2, "trackIndex": track,
                                              "recordFrame": target.GetStartFrame() + lead}]) or []
    if len(placed) != 1:
        raise WorkerError("原请求配音轨结构异常，请核实，未重复建线。")
    current_media = optional(placed[0], "GetMediaPoolItem")
    current_props = optional(current_media, "GetClipProperty") or {}
    if (optional(current_media, "GetUniqueId") != optional(imported, "GetUniqueId") or
            os.path.normcase(str(current_props.get("File Path") or "")) != os.path.normcase(str(audio)) or
            placed[0].GetStart() != target.GetStartFrame() + lead or optional(placed[0], "GetSourceStartFrame") != 0):
        raise WorkerError("原请求的配音媒体或位置已变化，请核实；未改写配音或重复建线。")
    journal["stage"] = "placed"
    durable_json(path, journal)
    audio_end = placed[0].GetEnd() - target.GetStartFrame()
    journal["stage"] = "marking"
    durable_json(path, journal)
    for marker in request["markers"]:
        duration = min(marker["duration"], audio_end - marker["frame"])
        actual = (target.GetMarkers() or {}).get(marker["frame"])
        if actual:
            if (actual.get("customData") != marker["custom"] or actual.get("duration") != duration or
                    actual.get("name") != marker["name"] or actual.get("note") != marker["note"] or
                    actual.get("color") != request["marker_color"]):
                raise WorkerError("原请求的意群标记有冲突，请核实，未改写现有标记。")
        elif duration <= 0 or not target.AddMarker(marker["frame"], request["marker_color"], marker["name"],
                                                  marker["note"], duration, marker["custom"]):
            raise WorkerError("原请求的意群标记未写完整，可用同一请求恢复。")
    journal.update(stage="renaming", checkpoint=build_checkpoint(target, track), built_result=readback(target, track))
    durable_json(path, journal)
    if target.GetName() != journal["name"]:
        if not target.SetName(journal["name"]):
            raise WorkerError("原时间线已建好但没能恢复正式名称，可用同一请求恢复。")
    imported.SetClipProperty("Clip Name", journal["name"] + request["clip_suffix"])
    journal["checkpoint"] = build_checkpoint(target, track)
    journal["stage"] = "saving"
    durable_json(path, journal)
    result = readback(target, track)
    result["saved"] = bool(resolve.GetProjectManager().SaveProject()) if request.get("save", True) else False
    if not result["saved"]:
        result["warnings"] = ["时间线已建好，但工程保存未确认，请手动保存后重新核验。"]
    journal.update(stage="completed", result=result)
    durable_json(path, journal)
    return {**result, **located(project), "project": project.GetName(), "materials": library(project), "recovered": existing}


def build(resolve, request):
    """Create a timeline with the voiceover on its own track and one range marker per meaning group."""
    if request.get("operation_key"):
        return build_recoverable(resolve, request)
    project = request_project(resolve, request)
    media_pool = project.GetMediaPool()
    audio = Path(request["audio"])
    if not audio.is_file():
        raise WorkerError("配音文件不存在，请重新生成配音。")
    name = new_name(project, request)
    created_timeline = imported = None
    try:
        media_pool.SetCurrentFolder(folder(media_pool, request["audio_bin"]))
        items = media_pool.ImportMedia([str(audio)]) or []
        if len(items) != 1:
            raise WorkerError("达芬奇没能导入配音文件。")
        imported = items[0]
        imported.SetClipProperty("Clip Name", f"{name}{request['clip_suffix']}")
        media_pool.SetCurrentFolder(folder(media_pool, request["timeline_bin"]))
        created_timeline = media_pool.CreateEmptyTimeline(name)
        if not created_timeline:
            raise WorkerError("达芬奇没能新建时间线。")
        project.SetCurrentTimeline(created_timeline)
        wanted = {"timelineFrameRate": request["fps"], "timelineResolutionWidth": str(request["width"]),
                  "timelineResolutionHeight": str(request["height"])}
        if any(not same_setting(project.GetSetting(key), value) for key, value in wanted.items()):
            if not created_timeline.SetSettings({"useCustomSettings": "1", **wanted}):
                raise WorkerError("新时间线的帧率或分辨率没有设置成功。")
        track = request["track_index"]
        while created_timeline.GetTrackCount("audio") < track:
            if not created_timeline.AddTrack("audio", "mono"):
                raise WorkerError("没能为配音添加音频轨。")
        created_timeline.SetTrackName("audio", track, request["track_name"])
        start = created_timeline.GetStartFrame()
        lead = int(request.get("lead_frames") or 0)
        placed = media_pool.AppendToTimeline([{"mediaPoolItem": imported, "mediaType": 2, "trackIndex": track,
                                              "recordFrame": start + lead}]) or []
        if len(placed) != 1:
            raise WorkerError("配音没有放上时间线。")
        # The last marker ends where the voiceover clip actually ends on this timeline.
        audio_end = placed[0].GetEnd() - start
        failed = [marker["number"] for marker in request["markers"] if marker["frame"] >= audio_end or not created_timeline.AddMarker(
            marker["frame"], request["marker_color"], marker["name"], marker["note"],
            min(marker["duration"], audio_end - marker["frame"]), marker["custom"])]
        if failed:
            raise WorkerError(f"意群 {', '.join(map(str, failed))} 的标记没有写入。")
        result = readback(created_timeline, track)
        result["saved"] = bool(resolve.GetProjectManager().SaveProject()) if request.get("save", True) else False
        if not result["saved"]:
            result["warnings"] = ["时间线已建好，但达芬奇没有确认保存工程，请在达芬奇里手动保存一次。"]
        return {**result, **located(project), "project": project.GetName(), "materials": library(project)}
    except Exception:
        if created_timeline:
            media_pool.DeleteTimelines([created_timeline])
        if imported:
            media_pool.DeleteClips([imported])
        raise


def readback(timeline, track):
    placed = timeline.GetItemListInTrack("audio", track) or []
    markers = {frame: info for frame, info in (timeline.GetMarkers() or {}).items()
               if str(info.get("customData", "")).startswith(MARKER_PREFIX)}
    return {"name": timeline.GetName(), "unique_id": timeline.GetUniqueId(), "start_frame": timeline.GetStartFrame(),
            "end_frame": timeline.GetEndFrame(), "audio_items": [{"start": item.GetStart(), "end": item.GetEnd()} for item in placed],
            "markers": sorted([{"frame": int(frame), "duration": info.get("duration"), "custom": info.get("customData")}
                               for frame, info in markers.items()], key=lambda item: item["frame"])}


def find_timeline(project, request):
    for item in timelines(project):
        if item.GetUniqueId() == request.get("unique_id"):
            return item
    raise WorkerError(f"项目里找不到时间线「{request.get('name', '')}」，可能已被删除或改到别的项目。")


def open_timeline(resolve, request):
    """Open an order's timeline, optionally seeking to a zero-based frame offset within it."""
    frame = request.get("frame")
    if "frame" in request and (type(frame) is not int or frame < 0):
        raise WorkerError("帧位置必须是从第 0 帧算起的非负整数。")
    project = request_project(resolve, request)
    timeline = find_timeline(project, request)
    wanted_timecode = None
    if "frame" in request:
        start, end = timeline.GetStartFrame(), timeline.GetEndFrame()
        if not 0 <= frame < end - start:
            raise WorkerError("帧位置超出了这条时间线，请刷新制作单后再试。")
        try:
            fps = timeline.GetSetting("timelineFrameRate")
            if float(fps) <= 0:
                raise ValueError("invalid frame rate")
            wanted_timecode = timecode(start + frame, fps, drop_frame(timeline))
        except (ValueError, TypeError, OverflowError, ZeroDivisionError) as exc:
            raise WorkerError("时间线帧率无法读取，没能定位播放头。") from exc
    if not project.SetCurrentTimeline(timeline):
        raise WorkerError("达芬奇没能切换到这条时间线。")
    resolve.OpenPage("edit")
    if wanted_timecode is not None:
        if not timeline.SetCurrentTimecode(wanted_timecode):
            raise WorkerError("达芬奇已切到这条时间线，但没能把播放头放到这个位置。")
        actual_timecode = timeline.GetCurrentTimecode()
        # Resolve may render the same frame with ':' instead of ';' for its final separator.
        if not actual_timecode or actual_timecode.replace(";", ":") != wanted_timecode.replace(";", ":"):
            raise WorkerError("达芬奇已切到这条时间线，但播放头没有到达指定位置。")
        return {"name": timeline.GetName(), "frame": frame, "timecode": actual_timecode}
    return {"name": timeline.GetName()}


def inspect_item(item, *, minimal=False):
    media = optional(item, "GetMediaPoolItem")
    clip = optional(media, "GetClipProperty") or {}
    channels = clip.get("Audio Ch")
    try:
        has_audio = int(channels) > 0 if channels not in (None, "") else (True if clip.get("Audio Codec") else None)
    except (ValueError, TypeError):
        has_audio = None
    result = {"uid": optional(item, "GetUniqueId"), "start": item.GetStart(), "end": item.GetEnd(),
              "name": optional(item, "GetName") or "", "media_id": optional(media, "GetUniqueId"),
              "media_path": clip.get("File Path") or "", "source_start": optional(item, "GetSourceStartFrame"),
              "source_end": optional(item, "GetSourceEndFrame"), "has_audio": has_audio,
              "enabled": optional(item, "GetClipEnabled"), "online": clip.get("Online Status")}
    if not minimal:
        links = optional(item, "GetLinkedItems")
        result.update(linked_ids=[optional(link, "GetUniqueId") for link in links] if links is not None else None,
                      properties=optional(item, "GetProperty"),
                      source_start_time=optional(item, "GetSourceStartTime"),
                      source_end_time=optional(item, "GetSourceEndTime"))
    return result


def inspect(resolve, request):
    """Read the target timeline; archived cross-edit source checks require explicit opt-in."""
    project = request_project(resolve, request)
    timeline = find_timeline(project, request)
    tracks = {}
    for kind in ("video", "audio"):
        tracks[kind] = []
        for index in range(1, timeline.GetTrackCount(kind) + 1):
            items = timeline.GetItemListInTrack(kind, index) or []
            # Speed is read here only: build checkpoints compare inspect_item() replies across workbench versions.
            tracks[kind].append({"index": index, "name": timeline.GetTrackName(kind, index),
                                 "enabled": optional(timeline, "GetIsTrackEnabled", kind, index),
                                 "locked": optional(timeline, "GetIsTrackLocked", kind, index),
                                 "items": [{**inspect_item(item), "speed": optional(item, "GetSpeed")} for item in items]})
    markers = [{"frame": frame, "color": info.get("color"), "name": info.get("name"), "note": info.get("note"),
                "duration": info.get("duration"), "custom": info.get("customData")} for frame, info in (timeline.GetMarkers() or {}).items()]
    dedup_enabled = request.get("deduplicate_source_ranges") is True
    related, complete = [], dedup_enabled
    source_ids = {item["media_id"] for track in tracks["video"] for item in track["items"] if item["media_id"]} if dedup_enabled else set()
    source_paths = {os.path.normcase(item["media_path"]) for track in tracks["video"] for item in track["items"] if item["media_path"]} if dedup_enabled else set()
    exclude_names = set(request.get("materials") or [])
    for other in timelines(project) if dedup_enabled else []:
        if other.GetUniqueId() == timeline.GetUniqueId() or other.GetName() in exclude_names or re.search(
                r"素材|备份|草稿|backup|draft|__WB_", other.GetName(), re.I):
            continue
        try:
            for index in range(1, other.GetTrackCount("video") + 1):
                for item in other.GetItemListInTrack("video", index) or []:
                    value = inspect_item(item, minimal=True)
                    if not value["media_id"] and not value["media_path"]:
                        complete = False
                        continue
                    if value["media_id"] in source_ids or (value["media_path"] and os.path.normcase(value["media_path"]) in source_paths):
                        related.append({"timeline_uid": other.GetUniqueId(), "timeline_name": other.GetName(), **value})
        except Exception:
            complete = False
    result = {"name": timeline.GetName(), "unique_id": timeline.GetUniqueId(), "project": project.GetName(),
              "start_frame": timeline.GetStartFrame(), "end_frame": timeline.GetEndFrame(),
              "fps": str(timeline.GetSetting("timelineFrameRate")),
              "width": timeline.GetSetting("timelineResolutionWidth"), "height": timeline.GetSetting("timelineResolutionHeight"),
              "tracks": tracks, "markers": sorted(markers, key=lambda item: item["frame"]),
              "other_shots": related, "dedup_complete": complete, "dedup_enabled": dedup_enabled}
    if request.get("save"):
        result["saved"] = bool(resolve.GetProjectManager().SaveProject())
    return result


def restore(resolve, request):
    """Put the user's own timeline and playhead back after a task looked through other timelines."""
    project = request_project(resolve, request)
    timeline = find_timeline(project, request)
    if not project.SetCurrentTimeline(timeline):
        raise WorkerError("达芬奇没能切回原来的时间线。")
    wanted_timecode = request.get("timecode")
    if wanted_timecode and not timeline.SetCurrentTimecode(wanted_timecode):
        raise WorkerError("达芬奇已切回原来的时间线，但没能恢复播放头位置。")
    actual_timecode = timeline.GetCurrentTimecode()
    if wanted_timecode and actual_timecode != wanted_timecode:
        raise WorkerError("达芬奇已切回原来的时间线，但播放头没有回到原来的位置。")
    if request.get("page") and optional(resolve, "GetCurrentPage") != request["page"]:
        resolve.OpenPage(request["page"])
    return {"name": timeline.GetName(), "timecode": actual_timecode}


SKIPPED_TIMELINES = re.compile(r"备份|草稿|backup|draft|__WB_", re.I)


def drop_frame(timeline):
    return str(optional(timeline, "GetSetting", "timelineDropFrameTimecode")) in {"1", "True", "true"}


def timecode(frame, fps, drop):
    """Absolute timeline frame to a timecode string; drop-frame counting only applies to 29.97 and 59.94."""
    nominal = round(float(fps))
    if drop and nominal in (30, 60):
        dropped = nominal // 15
        per_ten, per_minute = nominal * 600 - dropped * 9, nominal * 60 - dropped
        tens, rest = divmod(frame, per_ten)
        frame += dropped * 9 * tens + (dropped * ((rest - dropped) // per_minute) if rest > dropped else 0)
    separator = ";" if drop else ":"
    return (f"{frame // (nominal * 3600) % 24:02d}:{frame // (nominal * 60) % 60:02d}:"
            f"{frame // nominal % 60:02d}{separator}{frame % nominal:02d}")


def clip_label(item, uid, frame, info):
    """A clip-labeling marker still describing what this clip shows.

    Its customData names the clip it was written for; copying a timeline gives clips new IDs while the markers
    travel with them, so an older ID is accepted while the marker stays inside the visible part of the clip.
    A marker outside that part means the clip was trimmed after labeling, whatever its ID."""
    custom = str(info.get("customData") or "")
    if not custom.startswith("material_index_v1:"):
        return False
    left = optional(item, "GetLeftOffset")
    if left is None:
        return custom == f"material_index_v1:{uid}"
    return left <= frame < left + item.GetEnd() - item.GetStart()


def candidates(resolve, request):
    """List material labels and full source ranges; archived usage scanning requires explicit opt-in."""
    project = request_project(resolve, request)
    every = timelines(project)
    wanted = request.get("materials") or []
    if wanted:
        chosen = [item for item in every if item.GetName() in wanted]
    else:
        chosen = [item for item in every if "素材" in item.GetName() and not SKIPPED_TIMELINES.search(item.GetName())]
    materials, media_ids, media_paths = [], set(), set()
    for timeline in chosen:
        clips = []
        for index in range(1, timeline.GetTrackCount("video") + 1):
            for item in timeline.GetItemListInTrack("video", index) or []:
                value = inspect_item(item, minimal=True)
                if not value["media_id"] and not value["media_path"]:
                    continue
                markers = [(frame, info) for frame, info in sorted((optional(item, "GetMarkers") or {}).items())
                           if info.get("name") and info.get("customData") != USAGE_MARKER]
                value.update(track=index, labels=[info["name"] for _, info in markers],
                             verified_labels=[info["name"] for frame, info in markers if clip_label(item, value["uid"], frame, info)])
                clips.append(value)
                media_ids.add(value["media_id"])
                media_paths.add(os.path.normcase(value["media_path"]))
        materials.append({"name": timeline.GetName(), "unique_id": timeline.GetUniqueId(), "start_frame": timeline.GetStartFrame(),
                          "fps": str(timeline.GetSetting("timelineFrameRate")), "drop": drop_frame(timeline), "clips": clips})
    excluded = {item.GetUniqueId() for item in chosen} | {request.get("target_uid")}
    dedup_enabled = request.get("deduplicate_source_ranges") is True
    # Use counting reads the same finished timelines as the archived deduplication, but never removes a candidate.
    scan = dedup_enabled or request.get("count_usage") is True
    used, complete = [], scan
    for other in every if scan else []:
        name = other.GetName()
        if other.GetUniqueId() in excluded or "素材" in name or SKIPPED_TIMELINES.search(name):
            continue
        try:
            for index in range(1, other.GetTrackCount("video") + 1):
                for item in other.GetItemListInTrack("video", index) or []:
                    value = inspect_item(item, minimal=True)
                    if not value["media_id"] and not value["media_path"]:
                        complete = False
                    elif value["media_id"] in media_ids or (value["media_path"] and os.path.normcase(value["media_path"]) in media_paths):
                        used.append({"timeline_name": name, **value})
        except Exception:
            complete = False
    return {**located(project), "project": project.GetName(), "materials": materials, "missing": sorted(set(wanted) - {item.GetName() for item in chosen}),
            "used": used, "used_complete": complete, "dedup_enabled": dedup_enabled, "usage_counted": scan}


# DaVinci's default clip colors, top to bottom in its menu: use count 1 is Orange, 16 and more stay Chocolate.
USAGE_COLORS = ("Orange", "Apricot", "Yellow", "Lime", "Olive", "Green", "Teal", "Navy",
                "Blue", "Purple", "Violet", "Pink", "Tan", "Beige", "Brown", "Chocolate")
USAGE_MARKER = "material_usage_v1"
USAGE_LINE = re.compile("^已使用 \\d+ 次$")  # 已使用 N 次


def usage_color(count):
    return USAGE_COLORS[min(count, len(USAGE_COLORS)) - 1] if count > 0 else ""


def usage_note(note, count):
    """The marker note with its first line saying the use count; the user's own lines are kept."""
    lines = [line for line in str(note or "").splitlines() if not USAGE_LINE.match(line.strip())]
    return "\n".join(([f"已使用 {count} 次"] if count > 0 else []) + lines)


def half_open(value):
    """Source frames as [start, end); Resolve reports the last source frame itself at normal speed."""
    left, right = value.get("source_start"), value.get("source_end")
    if left is None or right is None:
        return None
    if right - left + 1 == value["end"] - value["start"]:
        right += 1
    return left, right


def usage_counts(clips, uses):
    """For each material clip, the finished timelines whose shots overlap its source range."""
    by_source = {}
    for use in uses:
        span = half_open(use)
        if not span:
            continue
        for key in (use["media_id"] and f"id:{use['media_id']}", use["media_path"] and f"path:{os.path.normcase(use['media_path'])}"):
            if key:
                by_source.setdefault(key, []).append((*span, use["timeline_uid"]))
    result = {}
    for clip in clips:
        span = half_open(clip)
        names = set()
        if span:
            for key in (clip["media_id"] and f"id:{clip['media_id']}", clip["media_path"] and f"path:{os.path.normcase(clip['media_path'])}"):
                for left, right, uid in by_source.get(key, []) if key else []:
                    if max(left, span[0]) < min(right, span[1]):
                        names.add(uid)
        result[clip["uid"]] = names
    return result


def mark_usage(resolve, request):
    """Color every material clip by how many finished timelines used it, and write the count into its marker notes.

    Finished timelines are all timelines except material, backup and draft ones. A clip that is no longer used loses
    only the color and note line this command wrote; clips never marked are left alone."""
    project = request_project(resolve, request)
    every = timelines(project)
    materials = [item for item in every if "素材" in item.GetName() and not SKIPPED_TIMELINES.search(item.GetName())]
    uses, unreadable = [], []
    for other in every:
        name = other.GetName()
        if "素材" in name or SKIPPED_TIMELINES.search(name):
            continue
        try:
            for index in range(1, other.GetTrackCount("video") + 1):
                for item in other.GetItemListInTrack("video", index) or []:
                    value = inspect_item(item, minimal=True)
                    if value["media_id"] or value["media_path"]:
                        uses.append({**value, "timeline_uid": other.GetUniqueId(), "timeline_name": name})
        except Exception:
            unreadable.append(name)
    names = {use["timeline_uid"]: use["timeline_name"] for use in uses}
    summary = {"materials": [item.GetName() for item in materials], "finished": len(names), "unreadable": unreadable,
               "clips": 0, "used": 0, "colored": 0, "notes": 0, "cleared": 0, "failed": 0, "counts": {}}
    for timeline in materials:
        items = [item for index in range(1, timeline.GetTrackCount("video") + 1) for item in timeline.GetItemListInTrack("video", index) or []]
        values = [(item, inspect_item(item, minimal=True)) for item in items]
        values = [(item, value) for item, value in values if value["media_id"] or value["media_path"]]
        counts = usage_counts([value for _, value in values], uses)
        for item, value in values:
            count = len(counts[value["uid"]])
            summary["clips"] += 1
            summary["used"] += count > 0
            summary["counts"][str(count)] = summary["counts"].get(str(count), 0) + 1
            try:
                changed = _mark_item(item, count, summary)
            except Exception:
                summary["failed"] += 1
                continue
            summary["notes"] += changed
    if summary["colored"] or summary["notes"] or summary["cleared"]:
        optional(resolve.GetProjectManager(), "SaveProject")
    summary["timelines"] = sorted(names.values())
    return summary


def _mark_item(item, count, summary):
    markers = optional(item, "GetMarkers") or {}
    ours = [frame for frame, info in markers.items() if info.get("customData") == USAGE_MARKER]
    marked = bool(ours) or any(USAGE_LINE.match(line.strip()) for info in markers.values() for line in str(info.get("note") or "").splitlines())
    color = usage_color(count)
    if color:
        if optional(item, "GetClipColor") != color:
            if not item.SetClipColor(color):
                raise WorkerError("clip color")
            summary["colored"] += 1
    elif marked and optional(item, "GetClipColor") in USAGE_COLORS:
        item.ClearClipColor()
        summary["cleared"] += 1
    changed = 0
    labels = [(frame, info) for frame, info in markers.items() if info.get("name") and info.get("customData") != USAGE_MARKER]
    for frame, info in labels:
        note = usage_note(info.get("note"), count)
        if note == str(info.get("note") or ""):
            continue
        # The API cannot edit a note in place: put the same marker back with the new note.
        if item.DeleteMarkerAtFrame(frame):
            if not item.AddMarker(frame, info.get("color") or "Blue", info["name"], note, info.get("duration") or 1, info.get("customData") or ""):
                item.AddMarker(frame, info.get("color") or "Blue", info["name"], str(info.get("note") or ""),
                               info.get("duration") or 1, info.get("customData") or "")
                raise WorkerError("marker")
            changed = 1
    if not labels:
        # An unlabeled clip gets a marker of its own for the count; its customData keeps it out of the clip's labels.
        note = usage_note("", count)
        current = markers.get(ours[0]) if ours else None
        if current and (str(current.get("note") or "") != note or current.get("name") != note):
            item.DeleteMarkerAtFrame(ours[0])
            current = None
            changed = 1
        if note and not current:
            left = optional(item, "GetLeftOffset") or 0
            if not item.AddMarker(left, "Cream", note, note, 1, USAGE_MARKER):
                raise WorkerError("marker")
            changed = 1
    return changed


def frames(resolve, request):
    """Export the requested frames of material clips as stills; the playhead must land on the expected clip for each one."""
    project = request_project(resolve, request)
    timeline = find_timeline(project, request)
    current = project.GetCurrentTimeline()
    if (not current or current.GetUniqueId() != timeline.GetUniqueId()) and not project.SetCurrentTimeline(timeline):
        raise WorkerError("达芬奇没能切换到素材时间线。")
    fps, drop = timeline.GetSetting("timelineFrameRate"), drop_frame(timeline)
    folder = Path(request["folder"])
    folder.mkdir(parents=True, exist_ok=True)
    opened, results = False, []
    for shot in request["shots"]:
        files, problem = [], ""
        for number, frame in enumerate(shot["frames"]):
            landed = False
            for text in dict.fromkeys((timecode(frame, fps, drop), timecode(frame, fps, False))):
                item = timeline.GetCurrentVideoItem() if timeline.SetCurrentTimecode(text) else None
                if item and optional(item, "GetUniqueId") == shot["item_uid"]:
                    landed = True
                    break
            if not landed:
                problem = "播放头没有落在这个片段上"
                break
            path = folder / f"{shot['key']}_{number}.png"
            exported = project.ExportCurrentFrameAsStill(str(path))
            if not exported and not opened:
                opened = True
                resolve.OpenPage("edit")
                exported = project.ExportCurrentFrameAsStill(str(path))
            if not exported or not path.is_file():
                problem = "静帧没有导出成功"
                break
            files.append(str(path))
        results.append({"key": shot["key"], "files": files, "problem": problem})
    return {"shots": results}


def timeline_stills(resolve, request):
    """Export evenly spaced stills of a finished timeline exactly as it plays (grade, reframing and titles included).

    request: step_seconds, max_stills, folder. Frames whose playhead did not land are skipped and counted."""
    project = request_project(resolve, request)
    timeline = find_timeline(project, request)
    current = project.GetCurrentTimeline()
    if (not current or current.GetUniqueId() != timeline.GetUniqueId()) and not project.SetCurrentTimeline(timeline):
        raise WorkerError("达芬奇没能切换到这条时间线。")
    fps, drop = timeline.GetSetting("timelineFrameRate"), drop_frame(timeline)
    start, end = timeline.GetStartFrame(), timeline.GetEndFrame()
    if end <= start:
        raise WorkerError("这条时间线是空的。")
    step_seconds, limit = float(request.get("step_seconds") or 0.5), int(request.get("max_stills") or 120)
    step = max(1, round(float(fps) * step_seconds))
    step = max(step, -(-(end - start) // max(1, limit)))
    folder = Path(request["folder"])
    folder.mkdir(parents=True, exist_ok=True)
    opened, files, skipped = False, [], 0
    for frame in range(start, end, step):
        landed = False
        for text in dict.fromkeys((timecode(frame, fps, drop), timecode(frame, fps, False))):
            if timeline.SetCurrentTimecode(text):
                landed = True
                break
        path = folder / f"{frame - start:07d}.png"
        exported = landed and project.ExportCurrentFrameAsStill(str(path))
        if landed and not exported and not opened:
            opened = True
            resolve.OpenPage("edit")
            exported = project.ExportCurrentFrameAsStill(str(path))
        if exported and path.is_file():
            files.append({"offset": frame - start, "file": str(path)})
        else:
            skipped += 1
    if not files:
        raise WorkerError("静帧没有导出成功，请确认达芬奇在剪辑页并且时间线不是空的。")
    return {"name": timeline.GetName(), "fps": float(fps), "start_frame": start, "end_frame": end, "files": files, "skipped": skipped}


def reveal(resolve, request):
    """Show one clip: open its timeline on the edit page and put the playhead on the given absolute timeline frame."""
    project = request_project(resolve, request)
    timeline = find_timeline(project, request)
    frame = request.get("frame")
    if type(frame) is not int or frame < 0:
        raise WorkerError("片段位置无效。")
    if not project.SetCurrentTimeline(timeline):
        raise WorkerError("达芬奇没能切换到这条时间线。")
    fps, drop = timeline.GetSetting("timelineFrameRate"), drop_frame(timeline)
    reached = ""
    for text in dict.fromkeys((timecode(frame, fps, drop), timecode(frame, fps, False))):
        if timeline.SetCurrentTimecode(text):
            reached = text
            break
    if not reached:
        raise WorkerError("达芬奇已切到素材时间线，但没能把播放头放到这个片段上。")
    if optional(resolve, "GetCurrentPage") != "edit":
        resolve.OpenPage("edit")
    return {"name": timeline.GetName(), "timecode": reached}


COMMANDS = {"status": status, "build": build, "open": open_timeline, "inspect": inspect, "restore": restore,
            "candidates": candidates, "frames": frames, "timeline_stills": timeline_stills, "reveal": reveal, "mark_usage": mark_usage}


def main():
    try:
        request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        command = COMMANDS.get(request.get("command"))
        if not command:
            raise WorkerError("未知的达芬奇操作。")
        result = {"ok": True, **command(connect(), request)}
    except WorkerError as exc:
        result = {"ok": False, "error": str(exc)}
    except Exception as exc:  # Report, never hang the caller; details stay local.
        result = {"ok": False, "error": f"达芬奇操作失败：{type(exc).__name__}: {exc}"[:500]}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
