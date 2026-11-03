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
import shutil
import sys
import time
import tempfile
from uuid import uuid4

API_DIRS = (os.environ.get("RESOLVE_SCRIPT_API"), r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting")
LIB_FILES = (os.environ.get("RESOLVE_SCRIPT_LIB"), r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll",
             r"D:\DaVinci Resolve\fusionscript.dll")
MARKER_PREFIX = "workbench-group:"
CONNECT_TRIES, CONNECT_WAIT = 3, 2


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
    # A running Resolve can refuse one connection for a moment, so a single miss is not yet an answer.
    for attempt in range(CONNECT_TRIES):
        resolve = DaVinciResolveScript.scriptapp("Resolve")
        if resolve:
            return resolve
        if attempt + 1 < CONNECT_TRIES:
            time.sleep(CONNECT_WAIT)
    raise WorkerError("连不上达芬奇：请先打开 DaVinci Resolve，并在偏好设置 → 系统 → 常规里把“外部脚本使用”设为“本地”。已经设好的话，稍等几秒再试；还连不上就关掉达芬奇重开，等主窗口出来后再试。")


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


BLANK_PROJECT = re.compile(r"(?:Untitled Project|未命名项目)(?: \d+)?")


def blank_project(project):
    """The empty, never-saved project Resolve opens with. Saving it asks the user for a name in a dialog that blocks
    every script call, and there is nothing in it to keep, so a switch leaves it alone."""
    try:
        if not BLANK_PROJECT.fullmatch(project.GetName() or "") or project.GetTimelineCount():
            return False
        root = project.GetMediaPool().GetRootFolder()
        return not (root.GetClipList() or root.GetSubFolderList())
    except Exception:
        return False


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
    if current and not blank_project(current):
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


def build_audio(source, key, create=False):
    """This build's own name for the take's audio. Importing a name starting with voiceover hung Resolve for good
    in a project holding such clips, wherever the file was; other names imported at once (2026-10-31, Lab-202H)."""
    target = source.with_name(f"build-{key[:16]}{source.suffix}")
    if create and not (target.is_file() and target.stat().st_size == source.stat().st_size):
        target.unlink(missing_ok=True)
        try:
            os.link(source, target)
        except OSError:
            partial = target.with_name(target.name + ".tmp")
            shutil.copyfile(source, partial)
            os.replace(partial, target)
    return target


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

    source = Path(request["audio"])
    if not source.is_file():
        raise WorkerError("配音文件不存在，未重复生成或建线。")
    audio = Path(journal.get("import_path") or source)  # Journals from before import_path used the take's own file.
    imported = next((clip for clip in pool_clips(media_pool) if
                     optional(clip, "GetUniqueId") == journal.get("audio_uid") and journal.get("audio_uid")), None)
    if not imported and journal.get("stage") == "importing":
        # Resolve hung or closed during the import: the file is in the audio bin or it is not. No timeline
        # exists yet, so taking that clip or importing again cannot build twice.
        wanted_paths = {os.path.normcase(str(item)) for item in (source, build_audio(source, key))}
        imported = next((clip for clip in optional(folder(media_pool, request["audio_bin"]), "GetClipList") or [] if
                         os.path.normcase(str((optional(clip, "GetClipProperty") or {}).get("File Path") or "")) in wanted_paths), None)
        if imported:
            audio = Path(optional(imported, "GetClipProperty")["File Path"])
            journal.update(stage="imported", audio_uid=optional(imported, "GetUniqueId"), import_path=str(audio))
            durable_json(path, journal)
    if not imported and journal.get("stage") not in {"prepared", "importing"}:
        # Fakes and old APIs may not expose pool UIDs; a placed item provides the exact imported clip.
        candidates = target.GetItemListInTrack("audio", request["track_index"]) if target else []
        imported = optional(candidates[0], "GetMediaPoolItem") if candidates else None
        if not imported:
            raise WorkerError("原请求的配音媒体无法核实，未重复导入或建线。")
    if not imported:
        audio = build_audio(source, key, create=True)
        journal.update(stage="importing", import_path=str(audio))
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
    # Reported so an older record that lacks them can be pinned to this project from now on.
    result = {"name": timeline.GetName(), **{key: value for key, value in located(project).items() if value}}
    if wanted_timecode is not None:
        if not timeline.SetCurrentTimecode(wanted_timecode):
            raise WorkerError("达芬奇已切到这条时间线，但没能把播放头放到这个位置。")
        actual_timecode = timeline.GetCurrentTimecode()
        # Resolve may render the same frame with ':' instead of ';' for its final separator.
        if not actual_timecode or actual_timecode.replace(";", ":") != wanted_timecode.replace(";", ":"):
            raise WorkerError("达芬奇已切到这条时间线，但播放头没有到达指定位置。")
        result.update(frame=frame, timecode=actual_timecode)
    return result


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
            # The hook is lettering on top of the cut: it covers no picture and no checks should count it as a shot.
            if kind == "video" and timeline.GetTrackName(kind, index) == HOOK_TRACK and all(is_title(item) for item in items):
                continue
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
                        complete = complete and is_title(item)
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


def subtitle_track(timeline):
    """The one subtitle track to check: the enabled one, else the only one holding subtitles, as finished timelines often switch theirs off.

    With several to choose from the worker cannot tell which is delivered, so it does not guess."""
    filled = [index for index in range(1, timeline.GetTrackCount("subtitle") + 1) if timeline.GetItemListInTrack("subtitle", index)]
    used = [index for index in filled if optional(timeline, "GetIsTrackEnabled", "subtitle", index) is not False] or filled
    if len(used) > 1:
        raise WorkerError("这条时间线有多条字幕轨，请只留一条后再核对。")
    return used[0] if used else None


def subtitle_rows(timeline, track):
    return [{"start": item.GetStart(), "end": item.GetEnd(), "text": item.GetName()} for item in timeline.GetItemListInTrack("subtitle", track) or []] if track else []


def can_move(timeline):
    return callable(getattr(timeline, "MoveItems", None))


def read_subtitles(resolve, request):
    project = request_project(resolve, request)
    timeline = find_timeline(project, request)
    return {"name": timeline.GetName(), "version": optional(resolve, "GetVersionString") or "", "can_replace": can_move(timeline),
            "subtitles": subtitle_rows(timeline, subtitle_track(timeline))}


def srt_time(frames, fps):
    """A timeline frame as SRT time, using the exact rate for the NTSC frame rates."""
    numerator, denominator = {"23.976": (24000, 1001), "29.97": (30000, 1001), "59.94": (60000, 1001)}.get(str(fps), (float(fps), 1))
    total = round(frames * 1000 * denominator / numerator)
    return f"{total // 3600000:02d}:{total // 60000 % 60:02d}:{total // 1000 % 60:02d},{total % 1000:03d}"


def same_text(first, second):
    return " ".join(str(first).split()) == " ".join(str(second).split())


def replace_subtitle(timeline, media_pool, track, change, folder_path, fps):
    """Swap one subtitle. Scripts cannot edit its text, so a one-cue SRT comes in past the timeline end and moves into place.

    The original goes only after the new one has proved correct and movable, so an early failure leaves it untouched."""
    old = next((item for item in timeline.GetItemListInTrack("subtitle", track) or [] if item.GetStart() == change["start"] and
                item.GetEnd() == change["end"] and same_text(item.GetName(), change["before"])), None)
    if not old:
        raise WorkerError(f"第 {change['start']} 帧的字幕已经变了，这条没有改。")
    start = timeline.GetStartFrame()
    path = Path(folder_path) / f"subtitle-{change['start']}-{uuid4().hex[:8]}.srt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"1\n{srt_time(change['start'] - start, fps)} --> {srt_time(change['end'] - start, fps)}\n{change['after']}\n\n", encoding="utf-8")
    imported = media_pool.ImportMedia([str(path)]) or []
    if len(imported) != 1:
        raise WorkerError("达芬奇没有导入字幕文件，这条没有改。")
    added = media_pool.AppendToTimeline([{"mediaPoolItem": imported[0], "recordFrame": 0, "trackIndex": track}]) or []
    def discard(message):
        if added:
            timeline.DeleteClips(added, False)
        media_pool.DeleteClips(imported)
        raise WorkerError(message)
    if len(added) != 1 or not same_text(added[0].GetName(), change["after"]):
        discard("新字幕的文字读回不一致，这条没有改。")
    new = added[0]
    if new.GetEnd() - new.GetStart() != change["end"] - change["start"]:
        discard("新字幕的时长和原字幕不一致，这条没有改。")
    probe = new.GetStart() + 1
    if not timeline.MoveItems([new], {"startFrame": probe}, False) or new.GetStart() != probe:
        discard("达芬奇没能移动新字幕，这条没有改。")
    if not timeline.DeleteClips([old], False):
        discard("达芬奇没能删除原字幕，这条没有改。")
    if not timeline.MoveItems([new], {"startFrame": change["start"]}, False) or (new.GetStart(), new.GetEnd()) != (change["start"], change["end"]):
        raise WorkerError(f"原字幕已删除，但新字幕没能回到第 {change['start']} 帧，目前在第 {new.GetStart()} 帧，请到达芬奇手动移回。")


def replace_subtitles(resolve, request):
    """Make the requested wording fixes on the timeline, then put the user's own timeline and playhead back."""
    project = request_project(resolve, request)
    timeline = find_timeline(project, request)
    if not can_move(timeline):
        raise WorkerError(f"达芬奇 {optional(resolve, 'GetVersionString') or ''} 不能用脚本把字幕放回原位，需要 21.1.2。")
    track = subtitle_track(timeline)
    if not track:
        raise WorkerError("这条时间线没有字幕。")
    if optional(timeline, "GetIsTrackLocked", "subtitle", track):
        raise WorkerError("字幕轨被锁定了，请解锁后再核对。")
    fps, media_pool = timeline.GetSetting("timelineFrameRate"), project.GetMediaPool()
    previous, shelf = project.GetCurrentTimeline(), media_pool.GetCurrentFolder()
    timecode_before = previous.GetCurrentTimecode() if previous else None
    if not project.SetCurrentTimeline(timeline):
        raise WorkerError("达芬奇没能切换到这条时间线。")
    done, failure = [], ""
    try:
        media_pool.SetCurrentFolder(folder(media_pool, request["audio_bin"]))
        for change in request["changes"]:
            try:
                replace_subtitle(timeline, media_pool, track, change, request["folder"], fps)
            except WorkerError as exc:
                failure = str(exc)
                break
            done.append(change)
    finally:
        media_pool.SetCurrentFolder(shelf)
        if previous:
            project.SetCurrentTimeline(previous)
            if timecode_before:
                previous.SetCurrentTimecode(timecode_before)
    saved = bool(done) and bool(resolve.GetProjectManager().SaveProject())
    return {"replaced": done, "error": failure, "saved": saved, "subtitles": subtitle_rows(timeline, track)}


HOOK_TRACK = "屏显"
# Text+ settings by style; a new look is one more entry here. full_line is how many characters fit a 1080x1920 frame
# at this size (21 took 50% of the width, measured 2026-11-01), and longer lines shrink to fit.
HOOK_STYLES = {"white_outline": {"label": "白字黑边", "seconds": 3, "full_line": 38,
                                 "inputs": {"Font": "CapCut Sans Text", "Style": "Medium", "Size": 0.0673, "Center": {1: 0.5, 2: 0.809},
                                            "Red1": 1.0, "Green1": 1.0, "Blue1": 1.0, "Enabled2": 1, "Thickness2": 0.1, "Red2": 0.0, "Green2": 0.0, "Blue2": 0.0}}}
DEFAULT_HOOK_STYLE = "white_outline"


def is_title(item):
    """A title has no source footage, so it says nothing about which footage a timeline used."""
    return not optional(item, "GetMediaPoolItem") and bool(optional(item, "GetFusionCompCount"))


def hook_track(timeline):
    return next((index for index in range(1, timeline.GetTrackCount("video") + 1) if timeline.GetTrackName("video", index) == HOOK_TRACK), None)


def cut_layout(timeline, skip=None):
    """Every clip on the timeline apart from the hook's own track."""
    return {kind: {index: [(item.GetName(), item.GetStart(), item.GetEnd()) for item in timeline.GetItemListInTrack(kind, index) or []]
                   for index in range(1, timeline.GetTrackCount(kind) + 1) if (kind, index) != ("video", skip)}
            for kind in ("video", "audio", "subtitle")}


def write_hook(item, text):
    comp = optional(item, "GetFusionCompByIndex", 1)
    tool = comp.FindTool("Template") if comp else None
    if not tool:
        return None
    tool.SetInput("StyledText", text)
    return tool if tool.GetInput("StyledText") == text else None


def style_hook(tool, style, text):
    longest = max(len(line) for line in text.split("\n"))
    for name, value in style["inputs"].items():
        tool.SetInput(name, value * min(1, style["full_line"] / longest) if name == "Size" else value)


def insert_hook(timeline, track, text, style, start, end):
    """Text+ at the start of the hook track, with everything else locked. Returns the title, or raises with it taken back out."""
    fps, before = timeline.GetSetting("timelineFrameRate"), cut_layout(timeline, track)
    item = timeline.InsertFusionTitleIntoTimeline("Text+")
    def discard(message):
        if item:
            timeline.DeleteClips([item], False)
        raise WorkerError(message if cut_layout(timeline, track) == before else f"{message}时间线可能已经变动，请到达芬奇核对。")
    if not item:
        discard("达芬奇没能插入 Text+，屏显没有加。")
    if item.GetTrackTypeAndIndex() != ["video", track]:
        discard("Text+ 没有落在屏显轨上，屏显没有加。")
    if not timeline.MoveItems([item], {"trackIndex": track, "startFrame": start}, False) or item.GetStart() != start:
        discard("达芬奇没能把 Text+ 移到开头，屏显没有加。")
    frames = min(round(float(fps) * style["seconds"]), end - start)
    if not item.SetRightOffset(item.GetRightOffset() + item.GetDuration() - frames, False, False) or item.GetDuration() != frames:
        discard("达芬奇没能裁短 Text+，屏显没有加。")
    tool = write_hook(item, text)
    if not tool:
        discard("Text+ 的文字读回不一致，屏显没有加。")
    style_hook(tool, style, text)
    if cut_layout(timeline, track) != before or timeline.GetEndFrame() != end:
        discard("加屏显时其他片段动了，屏显没有加。")
    return item


def place_hook(timeline, track, text, style):
    """Add the hook as Text+. An insert cuts and pushes every unlocked track at the playhead, and the playhead reads back
    correct even when the insert then uses an older one, so all other tracks are locked first and the playhead does not matter.

    Locks go back as they were, and on failure a track made for the hook goes too."""
    start, end = timeline.GetStartFrame(), timeline.GetEndFrame()
    made_track = not track
    if made_track:
        if not timeline.AddTrack("video"):
            raise WorkerError("达芬奇没能新建屏显轨，屏显没有加。")
        track = timeline.GetTrackCount("video")
        timeline.SetTrackName("video", track, HOOK_TRACK)
    locks = {(kind, index): timeline.GetIsTrackLocked(kind, index) for kind in ("video", "audio", "subtitle")
             for index in range(1, timeline.GetTrackCount(kind) + 1)}
    try:
        for (kind, index) in locks:
            timeline.SetTrackLock(kind, index, (kind, index) != ("video", track))
        if not all(timeline.GetIsTrackLocked(kind, index) == ((kind, index) != ("video", track)) for kind, index in locks):
            raise WorkerError("达芬奇没能锁住其他轨道，屏显没有加。")
        return insert_hook(timeline, track, text, style, start, end)
    except WorkerError:
        if made_track:
            timeline.DeleteTrack("video", track)
            locks.pop(("video", track))
        raise
    finally:
        for (kind, index), locked in locks.items():
            timeline.SetTrackLock(kind, index, locked)


def add_hook(resolve, request):
    """Put the opening hook on its own track, or rewrite the one already there and keep how the user has adjusted it unless a style is named."""
    style = HOOK_STYLES.get(request.get("style") or DEFAULT_HOOK_STYLE)
    if not style:
        raise WorkerError(f"没有名为「{request['style']}」的屏显样式。")
    project = request_project(resolve, request)
    timeline = find_timeline(project, request)
    if not can_move(timeline):
        raise WorkerError(f"达芬奇 {optional(resolve, 'GetVersionString') or ''} 不能用脚本把屏显放到开头，需要 21.1.2。")
    track = hook_track(timeline)
    items = (timeline.GetItemListInTrack("video", track) or []) if track else []
    if not all(is_title(item) for item in items) or len(items) > 1:
        raise WorkerError("屏显轨上有别的片段，请清理后再加。")
    previous, page = project.GetCurrentTimeline(), optional(resolve, "GetCurrentPage")
    timecode_before = previous.GetCurrentTimecode() if previous else None
    if not project.SetCurrentTimeline(timeline):
        raise WorkerError("达芬奇没能切换到这条时间线。")
    try:
        if page != "edit":
            resolve.OpenPage("edit")
        tool = write_hook(items[0], request["text"]) if items else None
        if items and not tool:
            raise WorkerError("Text+ 的文字读回不一致，屏显没有改。")
        if items and request.get("style"):
            style_hook(tool, style, request["text"])
        item = items[0] if items else place_hook(timeline, track, request["text"], style)
    finally:
        if previous:
            project.SetCurrentTimeline(previous)
            if timecode_before:
                previous.SetCurrentTimecode(timecode_before)
        if page and page != "edit":
            resolve.OpenPage(page)
    return {"created": not items, "start": item.GetStart(), "end": item.GetEnd(), "saved": bool(resolve.GetProjectManager().SaveProject())}


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


def clip_markers(item):
    """Named markers describing the visible part of a clip, usage notes aside.

    Items cut from one long media file can all report the markers of that file's other clips, far outside their own
    range; those describe other footage. A clip with no readable offset keeps all of its markers."""
    left = optional(item, "GetLeftOffset")
    length = item.GetEnd() - item.GetStart()
    return [(frame, info) for frame, info in sorted((optional(item, "GetMarkers") or {}).items())
            if info.get("name") and info.get("customData") != USAGE_MARKER and not is_action_marker(info)
            and (left is None or left <= frame < left + length)]


def is_action_marker(info):
    return str(info.get("customData") or "").startswith(ACTION_PREFIX)


def clip_actions(item):
    """Action ranges from this clip's own range markers, in source frames. The marker is the record, so a range the
    user dragged, renamed or deleted in DaVinci is read back as it now stands."""
    left, source = optional(item, "GetLeftOffset"), optional(item, "GetSourceStartFrame")
    if left is None or source is None:
        return []
    length = item.GetEnd() - item.GetStart()
    found = []
    for frame, info in sorted((optional(item, "GetMarkers") or {}).items()):
        if is_action_marker(info) and info.get("name") and left <= frame < left + length:
            end = min(frame + max(int(info.get("duration") or 1), 1), left + length)
            found.append({"name": str(info["name"]).replace(ACTION_PARTIAL, "").strip(), "start": source + frame - left,
                          "end": source + end - left, "complete": ACTION_PARTIAL not in str(info["name"])})
    return found


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
                markers = clip_markers(item)
                value.update(track=index, actions=clip_actions(item), labels=[info["name"] for _, info in markers],
                             verified_labels=[info["name"] for frame, info in markers if clip_label(item, value["uid"], frame, info)])
                clips.append(value)
                media_ids.add(value["media_id"])
                media_paths.add(os.path.normcase(value["media_path"]))
        materials.append({"name": timeline.GetName(), "unique_id": timeline.GetUniqueId(), "start_frame": timeline.GetStartFrame(),
                          "fps": str(timeline.GetSetting("timelineFrameRate")), "drop": drop_frame(timeline), "clips": clips})
    excluded = {item.GetUniqueId() for item in chosen} | {request.get("target_uid")} | set(request.get("ignore_usage") or [])
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
                        complete = complete and is_title(item)
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
ACTION_PREFIX = "material_action_v1"  # davinci-clip-labeling's range markers: one action each, name is the action.
ACTION_PARTIAL = "（不完整）"
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
    labels = [(frame, info) for frame, info in markers.items()
              if info.get("name") and info.get("customData") != USAGE_MARKER and not is_action_marker(info)]
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


COMMANDS = {"ping": lambda resolve, request: {}, "status": status, "build": build, "open": open_timeline, "inspect": inspect, "restore": restore,
            "candidates": candidates, "frames": frames, "reveal": reveal, "mark_usage": mark_usage,
            "read_subtitles": read_subtitles, "replace_subtitles": replace_subtitles, "add_hook": add_hook}


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
