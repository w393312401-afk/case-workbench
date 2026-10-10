"""Run one DaVinci Resolve operation in its own process and print one JSON result.

The workbench never loads fusionscript into its server process, so a hung or crashed Resolve call
ends with this process. Requests arrive as UTF-8 JSON on stdin; Chinese names are never source literals.
Only the project the user already has open is used; this worker never switches or creates projects.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys

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


def status(resolve, request):
    project = current_project(resolve)
    current = project.GetCurrentTimeline()
    return {"version": resolve.GetVersionString(), "project": project.GetName(),
            "fps": str(project.GetSetting("timelineFrameRate")), "width": str(project.GetSetting("timelineResolutionWidth")),
            "height": str(project.GetSetting("timelineResolutionHeight")),
            "current_timeline": current.GetName() if current else "", "timeline_count": project.GetTimelineCount(),
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


def build(resolve, request):
    """Create a timeline with the voiceover on its own track and one range marker per meaning group."""
    project = current_project(resolve, request["project"])
    media_pool = project.GetMediaPool()
    audio = Path(request["audio"])
    if not audio.is_file():
        raise WorkerError("配音文件不存在，请重新生成配音。")
    name = next_name(project, request["prefix"])
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
        placed = media_pool.AppendToTimeline([{"mediaPoolItem": imported, "mediaType": 2, "trackIndex": track, "recordFrame": start}]) or []
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
        if request.get("save", True) and not resolve.GetProjectManager().SaveProject():
            result["warnings"] = ["时间线已建好，但达芬奇没有确认保存工程，请在达芬奇里手动保存一次。"]
        return result
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
    project = current_project(resolve, request["project"])
    timeline = find_timeline(project, request)
    if not project.SetCurrentTimeline(timeline):
        raise WorkerError("达芬奇没能切换到这条时间线。")
    resolve.OpenPage("edit")
    return {"name": timeline.GetName()}


def inspect(resolve, request):
    """Everything the workbench needs to judge an edit: clips per track, markers and the timeline extent."""
    project = current_project(resolve, request["project"])
    timeline = find_timeline(project, request)
    tracks = {}
    for kind in ("video", "audio"):
        tracks[kind] = []
        for index in range(1, timeline.GetTrackCount(kind) + 1):
            items = timeline.GetItemListInTrack(kind, index) or []
            tracks[kind].append({"index": index, "name": timeline.GetTrackName(kind, index),
                                 "items": [{"start": item.GetStart(), "end": item.GetEnd(), "name": item.GetName()} for item in items]})
    markers = [{"frame": int(frame), "color": info.get("color"), "name": info.get("name"), "note": info.get("note"),
                "duration": info.get("duration"), "custom": info.get("customData")} for frame, info in (timeline.GetMarkers() or {}).items()]
    return {"name": timeline.GetName(), "start_frame": timeline.GetStartFrame(), "end_frame": timeline.GetEndFrame(),
            "fps": str(timeline.GetSetting("timelineFrameRate")), "tracks": tracks, "markers": sorted(markers, key=lambda item: item["frame"])}


COMMANDS = {"status": status, "build": build, "open": open_timeline, "inspect": inspect}


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
