"""Production phase two: DaVinci timeline build with a fake Resolve object model and a fake worker process."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))

import resolve_worker as worker
from elevenlabs_client import VoiceError
from resolve_bridge import ResolveBridge
from task_activity import ActivityStore
from test_production import Bench, FakeEleven
from workbench_editing import EditingWorkbenchMixin
from workbench_production import meaning_markers


class FakeItem:
    def __init__(self, start, end, media=None):
        self.start, self.end, self.media = start, end, media
        self.uid = "ITEM-" + uuid4().hex[:8]

    def GetUniqueId(self):
        return self.uid

    def GetMediaPoolItem(self):
        return self.media

    def GetName(self):
        return self.media.props.get("Clip Name", "audio") if self.media else "clip"

    def GetSourceStartFrame(self):
        return 0

    def GetSourceEndFrame(self):
        return self.end - self.start

    def GetClipEnabled(self):
        return True

    def GetProperty(self):
        return {"AudioVolume": 0.0}

    def GetLinkedItems(self):
        return []

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end


class FakeClip:
    def __init__(self, path):
        self.path, self.props = path, {}
        self.uid = "MEDIA-" + uuid4().hex[:8]

    def GetUniqueId(self):
        return self.uid

    def GetClipProperty(self):
        return {"File Path": str(self.path), "Audio Ch": "1", "Online Status": "Online", **self.props}

    def SetClipProperty(self, key, value):
        self.props[key] = value
        return True


class FakeTimeline:
    def __init__(self, name, fail_marker=None):
        self.name, self.uid = name, "TL-" + uuid4().hex[:8]
        self.tracks, self.track_names, self.items, self.markers = ["stereo"], {}, {}, {}
        self.settings, self.fail_marker = None, fail_marker
        self.timecode = "01:00:00:00"

    def GetName(self):
        return self.name

    def SetName(self, name):
        self.name = name
        return True

    def GetSetting(self, key):
        values = {"timelineFrameRate": "29.97", "timelineResolutionWidth": "1080", "timelineResolutionHeight": "1920"}
        return (self.settings or {}).get(key, values[key])

    def GetTrackName(self, kind, index):
        return self.track_names.get(index, kind + str(index))

    def GetIsTrackEnabled(self, kind, index):
        return True

    def GetIsTrackLocked(self, kind, index):
        return False

    def GetUniqueId(self):
        return self.uid

    def GetStartFrame(self):
        return 0

    def GetEndFrame(self):
        return max((item.end for items in self.items.values() for item in items), default=0)

    def GetTrackCount(self, kind):
        return len(self.tracks) if kind == "audio" else 1

    def AddTrack(self, kind, sub):
        self.tracks.append(sub)
        return True

    def SetTrackName(self, kind, index, name):
        self.track_names[index] = name
        return True

    def SetSettings(self, values):
        self.settings = values
        return True

    def AddMarker(self, frame, color, name, note, duration, custom):
        if self.fail_marker == custom.rsplit(":", 1)[-1] or frame in self.markers:
            return False
        self.markers[frame] = {"color": color, "name": name, "note": note, "duration": duration, "customData": custom}
        return True

    def GetMarkers(self):
        return dict(self.markers)

    def GetItemListInTrack(self, kind, index):
        return self.items.get(index, []) if kind == "audio" else []

    def SetCurrentTimecode(self, timecode):
        self.timecode = timecode
        return True

    def GetCurrentTimecode(self):
        return self.timecode


class FakeFolder:
    def __init__(self, name):
        self.name, self.children = name, []
        self.clips = []

    def GetClipList(self):
        return self.clips

    def GetName(self):
        return self.name

    def GetSubFolderList(self):
        return self.children


class FakeMediaPool:
    def __init__(self, project, audio_frames=179):
        self.project, self.root, self.audio_frames, self.current = project, FakeFolder("Master"), audio_frames, None
        self.root.children = [FakeFolder("时间线"), FakeFolder("音频字幕")]
        self.clips, self.deleted = [], []

    def GetRootFolder(self):
        return self.root

    def AddSubFolder(self, parent, name):
        folder = FakeFolder(name)
        parent.children.append(folder)
        return folder

    def SetCurrentFolder(self, folder):
        self.current = folder
        return True

    def ImportMedia(self, paths):
        clip = FakeClip(paths[0])
        self.clips.append(clip)
        self.current.clips.append(clip)
        return [clip]

    def CreateEmptyTimeline(self, name):
        timeline = FakeTimeline(name, self.project.fail_marker)
        self.project.items.append(timeline)
        self.project.current = timeline
        return timeline

    def AppendToTimeline(self, infos):
        info = infos[0]
        timeline = self.project.current
        item = FakeItem(info["recordFrame"], info["recordFrame"] + self.audio_frames, info["mediaPoolItem"])
        timeline.items.setdefault(info["trackIndex"], []).append(item)
        return [item]

    def DeleteTimelines(self, items):
        self.deleted.extend(items)
        self.project.items = [item for item in self.project.items if item not in items]
        return True

    def DeleteClips(self, clips):
        self.deleted.extend(clips)
        for folder in [self.root, *self.root.children]:
            folder.clips = [clip for clip in folder.clips if clip not in clips]
        return True


class FakeProject:
    def __init__(self, name="202H", names=(), fps=29.97, fail_marker=None):
        self.name, self.items = name, [FakeTimeline(item) for item in names]
        self.current, self.fail_marker, self.fps = None, fail_marker, fps
        self.pool = FakeMediaPool(self)

    def GetName(self):
        return self.name

    def GetMediaPool(self):
        return self.pool

    def GetTimelineCount(self):
        return len(self.items)

    def GetTimelineByIndex(self, index):
        return self.items[index - 1]

    def SetCurrentTimeline(self, timeline):
        self.current = timeline
        return True

    def GetCurrentTimeline(self):
        return self.current

    def GetSetting(self, key):
        return {"timelineFrameRate": self.fps, "timelineResolutionWidth": "1080", "timelineResolutionHeight": "1920"}[key]


class FakeResolve:
    def __init__(self, project):
        self.project, self.saved, self.page = project, 0, None

    def GetProjectManager(self):
        return self

    def GetCurrentProject(self):
        return self.project

    def SaveProject(self):
        self.saved += 1
        return True

    def OpenPage(self, page):
        self.page = page
        return True

    def GetVersionString(self):
        return "21.1.0.14"


def build_request(audio, **values):
    markers = [{"number": n, "frame": frame, "duration": 60, "name": f"#{n}", "note": "备注", "custom": f"workbench-group:PRD-x:{n}"}
               for n, frame in ((1, 0), (2, 60), (3, 120))]
    return {"project": "202H", "audio": str(audio), "prefix": "Lab261010-202-", "clip_suffix": "_配音", "audio_bin": "音频字幕",
            "timeline_bin": "时间线", "fps": "29.97", "width": 1080, "height": 1920, "track_index": 2, "track_name": "配音",
            "marker_color": "Blue", "markers": markers, **values}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="resolve-worker-")
        self.addCleanup(temp.cleanup)
        self.audio = Path(temp.name) / "voiceover.wav"
        self.audio.write_bytes(b"RIFF")

    def test_build_follows_existing_layout_names_sequence_and_clamps_last_marker(self):
        project = FakeProject(names=["Lab261010-202-1", "Lab261010-202-2切纸钩子", "Lab261009-202-7", "Zack261010-202-4"])
        resolve = FakeResolve(project)
        result = worker.build(resolve, build_request(self.audio))
        timeline = project.items[-1]
        self.assertEqual(result["name"], "Lab261010-202-3")
        self.assertEqual((timeline.tracks, timeline.track_names), (["stereo", "mono"], {2: "配音"}))
        self.assertEqual(project.pool.clips[0].props["Clip Name"], "Lab261010-202-3_配音")
        self.assertEqual(project.pool.current.GetName(), "时间线")
        self.assertIsNone(timeline.settings)
        self.assertEqual([(m["frame"], m["duration"]) for m in result["markers"]], [(0, 60), (60, 60), (120, 59)])
        self.assertEqual(timeline.markers[0]["color"], "Blue")
        self.assertEqual(resolve.saved, 1)

    def test_other_project_is_refused_and_custom_settings_only_when_needed(self):
        with self.assertRaises(worker.WorkerError) as caught:
            worker.build(FakeResolve(FakeProject(name="其他项目")), build_request(self.audio))
        self.assertIn("不会替你切换项目", str(caught.exception))
        project = FakeProject(fps=25)
        worker.build(FakeResolve(project), build_request(self.audio, save=False))
        self.assertEqual(project.items[-1].settings["useCustomSettings"], "1")

    def test_failed_marker_rolls_back_timeline_and_clip(self):
        project = FakeProject(fail_marker="2")
        with self.assertRaises(worker.WorkerError):
            worker.build(FakeResolve(project), build_request(self.audio))
        self.assertEqual(project.items, [])
        self.assertEqual(len(project.pool.deleted), 2)

    def test_worker_process_reports_errors_as_json(self):
        completed = subprocess.run([sys.executable, "-X", "utf8", "-I", str(APP / "resolve_worker.py")],
                                   input=json.dumps({"command": "nope"}).encode(), capture_output=True, timeout=60)
        self.assertEqual(json.loads(completed.stdout.decode("utf-8")), {"ok": False, "error": "未知的达芬奇操作。"})

    def test_restore_recovers_timeline_and_playhead(self):
        project = FakeProject(names=["自己的时间线", "素材时间线"])
        own = project.items[0]
        project.current = project.items[1]
        result = worker.restore(FakeResolve(project), {"project": "202H", "unique_id": own.uid,
                                                       "name": own.name, "timecode": "01:00:12:03"})
        self.assertIs(project.current, own)
        self.assertEqual(result, {"name": own.name, "timecode": "01:00:12:03"})

    def test_restore_reports_playhead_failure_or_unchanged_readback(self):
        project = FakeProject(names=["自己的时间线"])
        own = project.items[0]
        request = {"project": "202H", "unique_id": own.uid, "name": own.name, "timecode": "01:00:12:03"}
        for accepted in (False, True):
            with self.subTest(accepted=accepted), patch.object(own, "SetCurrentTimecode", return_value=accepted):
                with self.assertRaises(worker.WorkerError) as caught:
                    worker.restore(FakeResolve(project), request)
                self.assertIn("播放头", str(caught.exception))

    def test_inspect_reads_only_target_without_archived_dedup_scan(self):
        project = FakeProject(names=["Lab261010-202-1", "Lab261009-202-1"])
        target, other = project.items
        media = FakeClip("C:/media/a.mp4")
        voice = FakeItem(0, 60, media)
        target.items[1] = [voice]
        resolve = FakeResolve(project)
        request = {"project": "202H", "unique_id": target.uid}
        with patch.object(other, "GetTrackCount", side_effect=AssertionError("other edits must not be scanned")):
            result = worker.inspect(resolve, request)
        self.assertEqual(result["tracks"]["audio"][0]["items"][0]["uid"], voice.uid)
        self.assertEqual(result["other_shots"], [])
        self.assertFalse(result["dedup_enabled"])
        self.assertFalse(result["dedup_complete"])
        self.assertEqual(resolve.saved, 0)
        self.assertIsNone(result["tracks"]["audio"][0]["items"][0]["speed"])

    def test_inspect_records_clip_speed_but_build_checkpoints_do_not(self):
        project = FakeProject(names=["Lab261010-202-1"])
        target = project.items[0]
        voice = FakeItem(0, 60, FakeClip("C:/media/a.mp4"))
        voice.GetSpeed = lambda: {"Percentage": 150.0, "PitchCorrection": True}
        target.items[1] = [voice]
        result = worker.inspect(FakeResolve(project), {"project": "202H", "unique_id": target.uid})
        self.assertEqual(result["tracks"]["audio"][0]["items"][0]["speed"], {"Percentage": 150.0, "PitchCorrection": True})
        # Build journals compare these item records across workbench versions; a new field would break recovery.
        self.assertNotIn("speed", worker.build_checkpoint(target, 1)["audio"][0])

    def test_inspect_archived_dedup_scan_requires_explicit_opt_in(self):
        project = FakeProject(names=["Lab261010-202-1", "Lab261009-202-1"])
        target, other = project.items
        media = FakeClip("C:/media/a.mp4")
        target_shot, other_shot = FakeItem(0, 60, media), FakeItem(0, 30, media)
        target_read, other_read = target.GetItemListInTrack, other.GetItemListInTrack
        with patch.object(target, "GetItemListInTrack", side_effect=lambda kind, index: [target_shot] if kind == "video" else target_read(kind, index)), \
             patch.object(other, "GetItemListInTrack", side_effect=lambda kind, index: [other_shot] if kind == "video" else other_read(kind, index)):
            result = worker.inspect(FakeResolve(project), {"project": "202H", "unique_id": target.uid, "deduplicate_source_ranges": True})
        self.assertTrue(result["dedup_enabled"])
        self.assertTrue(result["dedup_complete"])
        self.assertEqual([item["uid"] for item in result["other_shots"]], [other_shot.uid])


class FakeRunner:
    def __init__(self, reply=None):
        self.calls, self.reply = [], reply

    def __call__(self, args, input, **options):
        request = json.loads(input.decode("utf-8"))
        self.calls.append(request)
        if self.reply:
            body = self.reply
        elif request["command"] == "build":
            markers = [{"frame": item["frame"], "duration": item["duration"], "custom": item["custom"]} for item in request["markers"]]
            builds = sum(1 for call in self.calls if call["command"] == "build")
            body = {"ok": True, "saved": True, "name": request.get("name") or request["prefix"] + "1", "unique_id": f"uid-{builds}",
                    "start_frame": 0, "end_frame": 400,
                    "audio_items": [{"start": 0, "end": 400}], "markers": markers}
        elif request["command"] == "status":
            body = {"ok": True, "version": "21.1.0.14", "project": "202H", "fps": "29.97", "width": "1080", "height": "1920",
                    "current_timeline": "", "timeline_count": 0}
        else:
            body = {"ok": True, "name": "Lab261010-202-1"}
        return subprocess.CompletedProcess(args, 0, json.dumps(body, ensure_ascii=False).encode("utf-8"), b"")


class BuildTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="production-resolve-")
        self.addCleanup(temp.cleanup)
        self.bench = Bench(temp.name, FakeEleven())
        self.runner = FakeRunner()
        self.bench.resolve = ResolveBridge(runner=self.runner)

    def mutate(self, kind, **values):
        return self.bench.production.mutate(kind, {"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"], **values})

    def voiced_job(self):
        source = {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 1, "product": "202H",
                  "case_id": None, "candidate_text": "Candidate.", "candidate_sha256": "x", "snapshot": {}}
        script = self.bench.creation.mutate("script", {"request_id": uuid4().hex, "revision": self.bench.creation.public()["revision"],
            "script": {"product": "202H", "title": "稿", "text": "One kit, replacing this jig.\nOut goes the jig.\nFull 202H kit's below.",
                       "confirmed": True}}, source)["script"]
        job = self.bench.create_production_job(
            {"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"], "job": {"script_id": script["id"]}})["job"]
        take = self.bench.generate_take(job["id"], {"request_id": uuid4().hex, "voice_id": "VOICE123456789"})["take"]
        self.mutate("select_take", job_id=job["id"], take_id=take["id"])
        return job, take

    def test_markers_tile_the_voiceover_from_frame_zero(self):
        job = {"id": "PRD-0123456789ab", "lines": [{"number": n, "spoken": f"Line {n}", "zh": "", "proof": "", "tags": "", "evidence": ""} for n in (1, 2, 3)]}
        take = {"duration": 6.0, "groups": [{"number": n, "start": a, "end": b, "coverage": 1.0}
                                             for n, a, b in ((1, 0.4, 1.9), (2, 2.0, 3.9), (3, 4.0, 5.8))]}
        markers = meaning_markers(job, take, "29.97")
        self.assertEqual([(m["frame"], m["duration"]) for m in markers], [(0, 60), (60, 60), (120, 59)])
        self.assertIn("画面表达：待补", markers[0]["note"])
        self.assertNotIn("口播：", markers[0]["note"])
        self.assertNotIn("配音：", markers[0]["note"])
        zh_job = {**job, "lines": [{**line, "zh": "试一次。", "proof": "刃口变亮"} for line in job["lines"]]}
        self.assertEqual(meaning_markers(zh_job, take, "29.97")[0]["note"], "试一次。 画面表达：刃口变亮")
        self.assertIn("口播：", meaning_markers(job, take, "29.97", legacy=True)[0]["note"])
        self.assertEqual(markers[2]["custom"], "workbench-group:PRD-0123456789ab:3")
        # A lead-in shifts every later group; the first marker still opens at frame 0 and covers the silence.
        led = meaning_markers(job, take, "29.97", lead_frames=15)
        self.assertEqual([(m["frame"], m["duration"]) for m in led], [(0, 75), (75, 60), (135, 59)])

    def test_settings_validate_creators_and_product(self):
        with self.assertRaises(VoiceError):
            self.mutate("settings", settings={"creators": ["Zack", "zack"]})
        with self.assertRaises(VoiceError):
            self.mutate("settings", settings={"creators": ["张三"]})
        saved = self.mutate("settings", settings={"creators": ["Zack", "Lab"], "product": "SHARPAL 202H", "project": "202H",
                                                  "code": "202", "fps": "29.97", "width": 1080, "height": 1920})["settings"]
        self.assertEqual((saved["creators"], saved["products"]["202H"]["audio_bin"]), (["Zack", "Lab"], "音频字幕"))
        self.assertEqual(self.bench.production.public("202H")["product_settings"]["timeline_bin"], "时间线")

    def test_build_needs_selected_take_and_known_creator_then_records_timeline(self):
        self.mutate("settings", settings={"creators": ["Zack", "Lab"]})
        job, take = self.voiced_job()
        with self.assertRaises(VoiceError):
            self.bench.build_timeline(job["id"], {"request_id": uuid4().hex, "creator": "Nobody"})
        body = {"request_id": uuid4().hex, "creator": "Lab"}
        result = self.bench.build_timeline(job["id"], body)
        request = self.runner.calls[-1]
        self.assertRegex(request["prefix"], r"^Lab\d{6}-202-$")
        self.assertEqual((request["project"], request["track_index"], request["marker_color"]), ("202H", 2, "Blue"))
        self.assertEqual(len(request["markers"]), 3)
        self.assertTrue(Path(request["audio"]).is_file())
        self.assertEqual((result["job"]["status"], result["timeline"]["markers"], result["timeline"]["take_id"]), ("timeline", 3, take["id"]))
        self.assertEqual(self.bench.build_timeline(job["id"], body), result)
        self.assertEqual(sum(1 for call in self.runner.calls if call["command"] == "build"), 1)
        opened = self.bench.open_timeline(job["id"], {"unique_id": "uid-1"})
        self.assertEqual((opened["ok"], self.runner.calls[-1]["command"]), (True, "open"))

    def test_timeline_remembers_its_creators_project_and_later_calls_use_it(self):
        class Located(FakeRunner):
            def __call__(self, args, input, **options):
                done = super().__call__(args, input, **options)
                request = self.calls[-1]
                if request["command"] != "build":
                    return done
                body = json.loads(done.stdout.decode("utf-8"))
                body.update(folder="B跨境店渠道号-zack", project_id="pid-zack", materials=["蓝点折刀成品素材", "大马士革成品素材"])
                return subprocess.CompletedProcess(args, 0, json.dumps(body, ensure_ascii=False).encode("utf-8"), b"")

        self.runner = Located()
        self.bench.resolve = ResolveBridge(runner=self.runner)
        self.mutate("settings", settings={"creators": ["Zack", "Lab"]})
        job, _ = self.voiced_job()
        first = self.bench.build_timeline(job["id"], {"request_id": uuid4().hex, "creator": "Zack"})
        request = self.runner.calls[-1]
        self.assertEqual(request["creator"], "Zack")
        self.assertNotIn("project_id", request)  # Nothing is known before the first build.
        record = first["timeline"]
        self.assertEqual((record["folder"], record["project_id"], record["materials"]),
                         ("B跨境店渠道号-zack", "pid-zack", ["蓝点折刀成品素材", "大马士革成品素材"]))
        self.bench.build_timeline(job["id"], {"request_id": uuid4().hex, "creator": "Zack"})
        again = self.runner.calls[-1]
        self.assertEqual((again["folder"], again["project_id"]), ("B跨境店渠道号-zack", "pid-zack"))
        self.bench.build_timeline(job["id"], {"request_id": uuid4().hex, "creator": "Lab"})
        self.assertNotIn("project_id", self.runner.calls[-1])  # Another creator's project is a different one.
        self.bench.open_timeline(job["id"], {"unique_id": record["unique_id"]})
        opened = self.runner.calls[-1]
        self.assertEqual((opened["creator"], opened["folder"], opened["project_id"]), ("Zack", "B跨境店渠道号-zack", "pid-zack"))

    def wait_for(self, check, seconds=5):
        deadline = time.monotonic() + seconds
        while not check():
            if time.monotonic() > deadline:
                self.fail("background build did not finish")
            time.sleep(.02)

    def operation(self, job, body):
        return self.bench._production_operations().read("timeline", {**body, "job_id": job["id"]})

    def test_worker_refusal_ends_cleanly_and_does_not_block_the_next_build(self):
        self.mutate("settings", settings={"creators": ["Lab"]})
        job, _ = self.voiced_job()
        self.bench.resolve = ResolveBridge(runner=FakeRunner({"ok": False, "error": "达芬奇当前打开的是项目「X」"}))
        body = {"request_id": uuid4().hex, "creator": "Lab"}
        with self.assertRaises(VoiceError) as caught:
            self.bench.build_timeline(job["id"], body)
        self.assertIn("项目「X」", str(caught.exception))
        self.assertEqual(self.bench.production.job(job["id"])["timelines"], [])
        # The worker journaled nothing, so DaVinci was never touched: no uncertain record, no recovery step.
        self.assertEqual(self.operation(job, body)["state"], "safe_failed")
        detail = self.bench.production_job(job["id"])
        self.assertEqual(detail["recovery_operations"], [])
        self.assertIn("项目「X」", detail["job"]["timeline_failure"]["error"])
        self.bench.resolve = ResolveBridge(runner=self.runner)
        built = self.bench.build_timeline(job["id"], {"request_id": uuid4().hex, "creator": "Lab"})
        self.assertEqual(built["timeline"]["unique_id"], "uid-1")
        self.assertIsNone(self.bench.production_job(job["id"])["job"]["timeline_failure"])

    def test_closed_davinci_answers_status_without_starting_the_worker(self):
        self.bench.resolve = ResolveBridge(runner=self.runner, probe=lambda: False)
        status = self.bench.resolve_status()
        self.assertEqual((status["ok"], status["busy"]), (False, False))
        self.assertIn("没有打开", status["error"])
        self.assertEqual(self.runner.calls, [])
        # An unreadable process list still asks DaVinci itself.
        self.bench.resolve = ResolveBridge(runner=self.runner, probe=lambda: None)
        self.bench.resolve_status()
        self.assertEqual([call["command"] for call in self.runner.calls], ["status"])

    def test_process_probe_finds_a_running_image(self):
        from resolve_bridge import resolve_process_running
        if sys.platform != "win32":
            self.skipTest("Windows process snapshot")
        self.assertTrue(resolve_process_running(Path(sys.executable).name.lower()))
        self.assertFalse(resolve_process_running("no-such-workbench-image.exe"))

    def test_busy_resolve_queues_the_build_until_davinci_is_free(self):
        self.bench.activity = ActivityStore(self.bench.production.root / "activity-test")
        self.mutate("settings", settings={"creators": ["Lab"]})
        job, _ = self.voiced_job()
        body = {"request_id": uuid4().hex, "creator": "Lab"}
        self.bench.resolve.lock.acquire()
        try:
            self.assertFalse(self.bench.resolve_status()["ok"])
            with patch("workbench_production.QUEUE_GRACE", 0):
                queued = self.bench.build_timeline(job["id"], body)
            self.assertEqual((queued["queued"], queued["request_id"]), (True, body["request_id"]))
            self.assertTrue(queued["can_submit_edit"])
            detail = self.bench.production_job(job["id"])
            self.assertEqual(detail["job"]["timeline_building"]["stage"], "waiting")
            self.assertEqual(detail["job"]["timeline_building"]["take_id"], self.operation(job, body)["snapshot"]["take_id"])
            self.assertTrue(detail["job"]["timeline_building"]["group_markers"])
            self.assertEqual(detail["recovery_operations"], [])  # Waiting its turn is not something to recover.
            self.assertEqual(self.runner.calls, [])
            run = self.bench.activity.feed(kind="timeline")["items"][0]
            self.assertEqual((run["status"], run["phase"]), ("queued", "等待达芬奇"))
            self.assertEqual(self.bench.activity.feed()["counts"]["queued"], 1)
        finally:
            self.bench.resolve.lock.release()
        self.wait_for(lambda: self.bench.production.job(job["id"])["timelines"])
        self.wait_for(lambda: self.bench.production_job(job["id"])["job"]["timeline_building"] is None)
        self.assertEqual(self.operation(job, body)["state"], "committed")
        self.assertEqual(self.operation(job, body)["result"]["build_request_id"], body["request_id"])
        self.assertEqual(sum(1 for call in self.runner.calls if call["command"] == "build"), 1)
        run = self.bench.activity.feed(kind="timeline")["items"][0]
        self.assertEqual(run["status"], "completed")
        logs = self.bench.activity.logs(run["run_id"])["logs"]
        self.assertIn(("queued", "等待达芬奇"), [(log["status"], log["phase"]) for log in logs])
        self.assertIn(("running", "正在建立时间线"), [(log["status"], log["phase"]) for log in logs])
        self.assertEqual(self.bench.activity.feed()["counts"]["queued"], 0)

    def test_queued_build_can_be_withdrawn_before_davinci_is_touched(self):
        self.bench.activity = ActivityStore(self.bench.production.root / "activity-test")
        self.mutate("settings", settings={"creators": ["Lab"]})
        job, _ = self.voiced_job()
        with self.assertRaises(VoiceError) as caught:
            self.bench.cancel_timeline(job["id"], {})
        self.assertEqual(caught.exception.status, 404)
        body = {"request_id": uuid4().hex, "creator": "Lab"}
        self.bench.resolve.lock.acquire()
        try:
            with patch("workbench_production.QUEUE_GRACE", 0):
                self.assertTrue(self.bench.build_timeline(job["id"], body)["queued"])
            self.assertEqual(self.bench.cancel_timeline(job["id"], {"request_id": body["request_id"]}), {"ok": True})
            self.wait_for(lambda: self.operation(job, body)["state"] == "safe_failed")
            self.wait_for(lambda: self.bench.production_job(job["id"])["job"]["timeline_building"] is None)
        finally:
            self.bench.resolve.lock.release()
        self.assertTrue(self.operation(job, body)["cancelled"])
        self.assertTrue(self.bench.production_job(job["id"])["job"]["timeline_failure"]["cancelled"])
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.bench.production.job(job["id"])["timelines"], [])
        run = self.bench.activity.feed(kind="timeline")["items"][0]
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(self.bench.activity.feed()["counts"]["queued"], 0)
        self.assertNotIn("running", [log["status"] for log in self.bench.activity.logs(run["run_id"])["logs"]])

    def test_unjournaled_uncertain_build_is_superseded_by_a_new_request(self):
        """Records left uncertain by the old busy-lock path must not block the job forever."""
        self.mutate("settings", settings={"creators": ["Lab"]})
        job, _ = self.voiced_job()
        stale = {"request_id": uuid4().hex, "creator": "Lab", "job_id": job["id"]}
        operations = self.bench._production_operations()
        operations.update(operations.prepare("timeline", stale, {"payload": {}, "config": {}, "creator": "Lab",
                                                                   "take_id": "", "markers": []}), "uncertain", error="busy")
        # Nothing to recover in DaVinci: no recovery card, only a note of why the last build did not happen.
        detail = self.bench.production_job(job["id"])
        self.assertEqual((detail["recovery_operations"], detail["job"]["timeline_failure"]["error"]), ([], "busy"))
        self.assertEqual(self.bench.production_catalog()["jobs"][0]["recovery_operations"], [])
        self.bench.build_timeline(job["id"], {"request_id": uuid4().hex, "creator": "Lab"})
        self.assertEqual(operations.read("timeline", stale)["state"], "safe_failed")
        detail = self.bench.production_job(job["id"])
        self.assertEqual((detail["recovery_operations"], detail["job"]["timeline_failure"]), ([], None))

    def test_build_options_override_format_name_lead_and_markers_for_one_build(self):
        self.mutate("settings", settings={"creators": ["Lab"]})
        job, take = self.voiced_job()
        for options in ({"fps": "31"}, {"width": 10}, {"height": 1920.5}, {"lead_seconds": 11}, {"lead_seconds": True},
                        {"code": "2-0"}, {"markers": "no"}, {"unknown": 1}, {"name": "a\nb"}, [1]):
            with self.assertRaises(VoiceError) as caught:
                self.bench.build_timeline(job["id"], {"request_id": uuid4().hex, "creator": "Lab", "options": options})
            self.assertEqual(caught.exception.status, 400, options)
        self.assertEqual(self.runner.calls, [])
        result = self.bench.build_timeline(job["id"], {"request_id": uuid4().hex, "creator": "Lab", "options": {
            "code": "202B", "fps": "25", "width": 1080, "height": 1440, "lead_seconds": 0.4}})
        request = self.runner.calls[-1]
        self.assertRegex(request["prefix"], r"^Lab\d{6}-202B-$")
        self.assertEqual((request["fps"], request["width"], request["height"], request["lead_frames"], request["name"]),
                         ("25", 1080, 1440, 10, ""))
        self.assertEqual([marker["frame"] for marker in request["markers"]][:2],
                         [0, round(take["groups"][1]["start"] * 25) + 10])
        timeline = result["timeline"]
        self.assertEqual((timeline["fps"], timeline["width"], timeline["height"], timeline["lead_frames"], timeline["group_markers"]),
                         ("25", 1080, 1440, 10, True))
        bare = self.bench.build_timeline(job["id"], {"request_id": uuid4().hex, "creator": "Lab",
                                                     "options": {"markers": False, "name": " Lab-送礼版 "}})["timeline"]
        request = self.runner.calls[-1]
        self.assertEqual((request["markers"], request["name"], request["fps"], request["lead_frames"]), ([], "Lab-送礼版", "29.97", 0))
        self.assertEqual((bare["name"], bare["group_markers"], bare["warnings"]), ("Lab-送礼版", False, []))
        with self.assertRaises(VoiceError) as caught:
            EditingWorkbenchMixin._edit_context(self.bench, self.bench.production.job(job["id"]), bare["unique_id"])
        self.assertIn("没有写意群标记", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
