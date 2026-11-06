"""Textless delivery restores track switches and delivery settings on success, failure and recovery."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import resolve_worker as worker


class Timeline:
    def __init__(self, uid):
        self.uid = uid
        self.enabled = {("video", 1): True, ("video", 2): True, ("video", 3): False,
                        ("subtitle", 1): True, ("subtitle", 2): False, ("audio", 1): True, ("audio", 2): False}
        self.names = {("video", 1): "画面", ("video", 2): "屏显", ("video", 3): "字幕",
                      ("subtitle", 1): "英文", ("subtitle", 2): "中文"}
        self.timecode = "01:00:03:00"
        self.offline = False
        self.fail_switch = None
        self.project = None

    def GetUniqueId(self):
        return self.uid

    def GetName(self):
        return self.uid

    def GetStartFrame(self):
        return 90000

    def GetEndFrame(self):
        return 91800

    def GetCurrentTimecode(self):
        return self.timecode

    def SetCurrentTimecode(self, value):
        self.timecode = value
        return True

    def GetTrackCount(self, kind):
        return {"video": 3, "subtitle": 2, "audio": 2}[kind]

    def GetTrackName(self, kind, index):
        return self.names.get((kind, index), kind + str(index))

    def GetIsTrackEnabled(self, kind, index):
        # Native Resolve 21.1.2 reports all tracks disabled when this timeline is not active.
        if self.project and self.project.current is not self:
            return False
        return self.enabled[kind, index]

    def SetTrackEnable(self, kind, index, value):
        if (kind, index, value) == self.fail_switch:
            return False
        # Native 21.1.2 returns True but changes nothing while the Deliver page is open.
        if self.project and self.project.page == "deliver":
            return True
        self.enabled[kind, index] = value
        return True

    def GetItemListInTrack(self, kind, index):
        media = Mock()
        media.GetClipProperty.return_value = {"Online Status": "Offline" if self.offline else "Online"}
        item = Mock()
        item.GetMediaPoolItem.return_value = media
        return [item]

    def GetSetting(self, key):
        return {"timelineResolutionWidth": "1080", "timelineResolutionHeight": "1920", "timelineFrameRate": "29.97"}[key]


class Project:
    def __init__(self):
        self.target, self.previous = Timeline("target"), Timeline("previous")
        self.current = self.previous
        self.target.project = self.previous.project = self
        self.mode = 0
        self.format = {"format": "mov", "codec": "DNxHR"}
        self.settings = {"ExportSubtitle": True, "DataBurnIn": "existing", "MarkIn": 90020, "MarkOut": 90030}
        self.presets = {}
        self.jobs = {"existing": {"JobId": "existing"}}
        self.started = []
        self.in_progress = False
        self.outcome = "Complete"
        self.stops = 0
        self.render_snapshot = None
        self.page = "edit"

    def GetName(self):
        return "Project"

    def GetUniqueId(self):
        return "project-uid"

    def GetTimelineCount(self):
        return 2

    def GetTimelineByIndex(self, index):
        return [self.target, self.previous][index - 1]

    def GetCurrentTimeline(self):
        return self.current

    def SetCurrentTimeline(self, timeline):
        self.current = timeline
        return True

    def GetRenderCodecs(self, extension):
        return {"H.264 NVIDIA": "H264_NVIDIA"}

    def GetCurrentRenderMode(self):
        return self.mode

    def SetCurrentRenderMode(self, value):
        self.mode = value
        return True

    def GetCurrentRenderFormatAndCodec(self):
        return self.format.copy()

    def SetCurrentRenderFormatAndCodec(self, extension, codec):
        if not extension or extension == "unknown" or not codec:
            return False
        self.format = {"format": extension, "codec": codec}
        return True

    def SaveAsNewRenderPreset(self, name):
        self.page = "deliver"  # native Resolve jumps to Deliver here
        self.presets[name] = copy.deepcopy((self.settings, self.format, self.mode))
        return True

    def GetRenderPresetList(self):
        return ["H.264 Master", *self.presets]

    def LoadRenderPreset(self, name):
        if name in self.presets:
            self.settings, self.format, self.mode = copy.deepcopy(self.presets[name])
        else:
            self.settings = {"ExportSubtitle": True}
        return True

    def DeleteRenderPreset(self, name):
        self.page = "deliver"
        del self.presets[name]
        return True

    def SetRenderSettings(self, settings):
        # Native 21.1.2 returns False for the whole dict when NVIDIA receives VideoQuality.
        if "VideoQuality" in settings and self.format["codec"] == "H264_NVIDIA":
            return False
        self.settings.update(settings)
        return True

    def AddRenderJob(self):
        self.jobs["own"] = {"JobId": "own"}
        return "own"

    def StartRendering(self, ids):
        self.started.append(ids)
        self.in_progress = True
        self.render_snapshot = {"tracks": self.target.enabled.copy(), "settings": self.settings.copy(),
                                "mode": self.mode, "uid": self.current.GetUniqueId()}
        if self.outcome == "Complete":
            (Path(self.settings["TargetDir"]) / (self.settings["CustomName"] + ".mp4")).write_bytes(b"fixture-video")
        return True

    def GetRenderJobStatus(self, job_id):
        if self.outcome != "Rendering":
            self.in_progress = False
        return {"JobStatus": self.outcome, "CompletionPercentage": 100 if self.outcome == "Complete" else 25}

    def IsRenderingInProgress(self):
        return self.in_progress

    def StopRendering(self):
        self.stops += 1
        self.in_progress = False
        self.outcome = "Cancelled"

    def GetRenderJobList(self):
        return list(self.jobs.values())

    def DeleteRenderJob(self, job_id):
        del self.jobs[job_id]
        return True


class TextlessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name)
        self.project = Project()
        self.resolve = Mock()
        self.resolve.GetProjectManager.return_value.GetCurrentProject.return_value = self.project
        self.resolve.GetCurrentPage.side_effect = lambda: self.project.page
        self.resolve.OpenPage.side_effect = lambda page: setattr(self.project, "page", page) or True
        self.resolve.GetProjectManager.return_value.SaveProject.return_value = True
        self.request = {"project": "Project", "unique_id": "target", "name": "target", "output_dir": str(folder),
                        "recovery_path": str(folder / "recovery.json"), "progress_path": str(folder / "progress.json")}
        self.before_tracks = self.project.target.enabled.copy()
        self.before_settings = self.project.settings.copy()
        self.addCleanup(patch.stopall)
        patch.object(worker, "request_project", return_value=self.project).start()

    def assert_restored(self):
        self.assertEqual(self.project.target.enabled, self.before_tracks)
        self.assertIs(self.project.current, self.project.previous)
        self.assertEqual(self.project.page, "edit")
        self.assertEqual(self.project.settings, self.before_settings)
        self.assertEqual(self.project.mode, 0)
        self.assertEqual(self.project.format, {"format": "mov", "codec": "DNxHR"})
        self.assertEqual(list(self.project.jobs), ["existing"])
        self.assertFalse(self.project.presets)

    def test_success_renders_the_bound_full_timeline_and_restores_original_state(self):
        result = worker.export_textless(self.resolve, self.request)
        self.assertTrue(result["restored"])
        self.assertEqual(result["unique_id"], "target")
        snapshot = self.project.render_snapshot
        self.assertEqual(snapshot["uid"], "target")
        self.assertEqual(snapshot["mode"], 1)
        self.assertEqual(snapshot["tracks"], {**self.before_tracks, ("video", 2): False, ("subtitle", 1): False})
        settings = snapshot["settings"]
        self.assertTrue(settings["SelectAllFrames"])
        self.assertFalse(settings["ExportSubtitle"])
        self.assertEqual(settings["DataBurnIn"], "None")
        self.assertEqual((settings["FormatWidth"], settings["FormatHeight"], settings["FrameRate"]), (1080, 1920, 29.97))
        self.assertEqual(self.project.started, [["own"]])
        self.assert_restored()
        worker.restore_textless(self.resolve, self.request)  # repeated recovery is harmless
        self.assert_restored()

    def test_tracks_are_switched_on_edit_page_after_preset_save_jumps_to_deliver(self):
        # Seen live: SaveAsNewRenderPreset opened Deliver and the subtitle switch was ignored there.
        result = worker.export_textless(self.resolve, self.request)
        self.assertTrue(result["restored"])
        self.assertFalse(self.project.render_snapshot["tracks"][("subtitle", 1)])
        self.assertFalse(self.project.render_snapshot["tracks"][("video", 2)])
        self.assert_restored()

    def test_export_captures_format_once_deliver_is_initialized(self):
        read = self.project.GetCurrentRenderFormatAndCodec
        def read_format():
            return {"format": "unknown", "codec": ""} if self.project.page == "edit" else read()
        with patch.object(self.project, "GetCurrentRenderFormatAndCodec", side_effect=read_format):
            worker.export_textless(self.resolve, self.request)
        journal = json.loads(Path(self.request["recovery_path"]).read_text(encoding="utf-8"))
        self.assertEqual(journal["format"], {"format": "mov", "codec": "DNxHR"})
        self.assert_restored()

    def test_old_journal_with_unreadable_format_recovers_from_its_own_preset(self):
        for original in ({"format": "unknown", "codec": ""}, {"format": "mov", "codec": ""}, None):
            with self.subTest(original=original):
                self.project.SaveAsNewRenderPreset("backup")
                self.project.format = {"format": "mp4", "codec": "H264_NVIDIA"}
                self.project.mode = 1
                self.project.jobs["own"] = {"JobId": "own"}
                self.project.target.enabled["subtitle", 1] = False
                path = Path(self.request["recovery_path"])
                worker.durable_json(path, {"project": "Project", "project_id": "project-uid",
                    "timeline_uid": "target", "timeline_name": "target", "previous_uid": "previous", "page": "edit",
                    "tracks": [{"kind": "subtitle", "index": 1, "name": "英文", "enabled": True}],
                    "preset": "backup", "preset_saved": True, "format": original, "render_mode": 0,
                    "render_job_id": "own", "restored": False})
                self.project.current = self.project.target
                with patch.object(self.project, "SetCurrentRenderFormatAndCodec") as setter:
                    self.assertTrue(worker.restore_textless(self.resolve, self.request)["restored"])
                    setter.assert_not_called()
                self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["restored"])
                self.assert_restored()

    def test_known_format_mismatch_keeps_backup_until_readback_matches(self):
        original_load = self.project.LoadRenderPreset
        def load_without_format(name):
            current = self.project.format.copy()
            ok = original_load(name)
            self.project.format = current
            return ok
        with patch.object(self.project, "LoadRenderPreset", side_effect=load_without_format):
            setter = self.project.SetCurrentRenderFormatAndCodec
            def ignore_mov(extension, codec):
                return True if extension == "mov" else setter(extension, codec)
            with patch.object(self.project, "SetCurrentRenderFormatAndCodec", side_effect=ignore_mov):
                with self.assertRaisesRegex(worker.WorkerError, "导出格式未恢复"):
                    worker.export_textless(self.resolve, self.request)
            path = Path(self.request["recovery_path"])
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(snapshot["restored"])
            self.assertIn(snapshot["preset"], self.project.presets)
            self.assertIn("own", self.project.jobs)
            self.assertEqual(self.project.target.enabled, self.before_tracks)
            worker.restore_textless(self.resolve, self.request)
        self.assert_restored()

    def test_unreadable_format_still_requires_a_loadable_backup(self):
        load = self.project.LoadRenderPreset
        with patch.object(self.project, "GetCurrentRenderFormatAndCodec", return_value={"format": "unknown", "codec": ""}):
            with patch.object(self.project, "LoadRenderPreset", side_effect=lambda name: name == "H.264 Master" and load(name)):
                with self.assertRaisesRegex(worker.WorkerError, "未能加载原导出设置备份"):
                    worker.export_textless(self.resolve, self.request)
            path = Path(self.request["recovery_path"])
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(snapshot["restored"])
            self.assertEqual(self.project.target.enabled, self.before_tracks)
            backup = self.project.presets.pop(snapshot["preset"])
            with self.assertRaisesRegex(worker.WorkerError, "备份已丢失"):
                worker.restore_textless(self.resolve, self.request)
            self.assertFalse(json.loads(path.read_text(encoding="utf-8"))["restored"])
            self.project.presets[snapshot["preset"]] = backup
            worker.restore_textless(self.resolve, self.request)
        self.assert_restored()

    def test_failed_or_cancelled_render_restores_without_deleting_existing_queue(self):
        for outcome in ("Failed", "Cancelled"):
            with self.subTest(outcome=outcome):
                self.project.outcome = outcome
                with self.assertRaises(worker.WorkerError):
                    worker.export_textless(self.resolve, self.request)
                self.assert_restored()

    def test_normal_preview_preserves_subtitles_screen_text_and_audio(self):
        with patch.object(self.project.target, "SetTrackEnable", wraps=self.project.target.SetTrackEnable) as switch:
            result = worker.export_preview(self.resolve, self.request)
            # Only switched-off tracks are touched, and only to switch them on.
            self.assertTrue(switch.call_args_list)
            self.assertTrue(all(call.args[2] is True for call in switch.call_args_list))
        self.assertEqual(Path(result["video_path"]).name, "preview.mp4")
        self.assertFalse(result["subtitles_off"])
        snapshot = self.project.render_snapshot
        # A preview shows every track, and they stay on afterwards.
        self.before_tracks = {key: True for key in self.before_tracks}
        self.assertEqual(snapshot["tracks"], self.before_tracks)
        self.assertTrue(result["switched_on"])
        self.assertTrue(snapshot["settings"]["ExportSubtitle"])
        self.assertEqual(snapshot["settings"]["SubtitleFormat"], "BurnIn")
        self.assertTrue(snapshot["settings"]["ExportAudio"])
        self.assert_restored()

    def test_inactive_timeline_is_opened_before_preflight_and_original_playhead_is_restored(self):
        self.project.previous.timecode = "01:00:12:08"
        self.assertFalse(self.project.target.GetIsTrackEnabled("video", 1))
        result = worker.export_preview(self.resolve, self.request)
        self.assertTrue(result["restored"])
        self.assertEqual(self.project.render_snapshot["uid"], "target")
        self.assertTrue(self.project.render_snapshot["tracks"]["video", 1])
        self.assertEqual(self.project.previous.timecode, "01:00:12:08")
        self.before_tracks = {key: True for key in self.before_tracks}
        self.assert_restored()

    def test_preview_switches_on_every_disabled_track_for_the_export_and_puts_them_back(self):
        self.project.target.enabled["subtitle", 1] = False
        self.project.target.enabled["video", 2] = False
        self.before_tracks = self.project.target.enabled.copy()
        result = worker.export_preview(self.resolve, self.request)
        snapshot = self.project.render_snapshot
        self.assertTrue(snapshot["settings"]["ExportSubtitle"])
        self.assertTrue(snapshot["tracks"]["subtitle", 1])
        self.assertTrue(snapshot["tracks"]["video", 2])
        self.assertFalse(result["subtitles_off"])
        # Switched on for good: nothing turns them off again.
        self.before_tracks = {key: True for key in self.before_tracks}
        self.assert_restored()

    def test_timeout_stops_only_its_render_then_restores(self):
        self.project.outcome = "Rendering"
        with self.assertRaisesRegex(worker.WorkerError, "超时"):
            worker.export_textless(self.resolve, {**self.request, "render_timeout": -1})
        self.assertEqual(self.project.stops, 1)
        self.assert_restored()

    def test_track_disable_and_settings_failure_restore_partially_changed_state(self):
        self.project.target.fail_switch = ("video", 2, False)
        with self.assertRaisesRegex(worker.WorkerError, "关闭"):
            worker.export_textless(self.resolve, self.request)
        self.assert_restored()
        self.project.target.fail_switch = None
        with patch.object(self.project, "SetRenderSettings", return_value=False):
            with self.assertRaisesRegex(worker.WorkerError, "导出设置"):
                worker.export_textless(self.resolve, self.request)
        self.assert_restored()

    def test_busy_missing_timeline_and_offline_media_fail_before_changes(self):
        self.project.in_progress = True
        with self.assertRaisesRegex(worker.WorkerError, "正在渲染"):
            worker.export_textless(self.resolve, self.request)
        self.project.in_progress = False
        with self.assertRaisesRegex(worker.WorkerError, "找不到时间线"):
            worker.export_textless(self.resolve, {**self.request, "unique_id": "missing"})
        self.project.target.offline = True
        with self.assertRaisesRegex(worker.WorkerError, "离线"):
            worker.export_textless(self.resolve, self.request)
        self.assertFalse(Path(self.request["recovery_path"]).exists())
        self.assert_restored()

    def test_process_recovery_journal_survives_a_failed_restore_and_can_be_retried(self):
        self.project.target.fail_switch = ("subtitle", 1, True)
        with self.assertRaisesRegex(worker.WorkerError, "恢复未完成"):
            worker.export_textless(self.resolve, self.request)
        journal = json.loads(Path(self.request["recovery_path"]).read_text(encoding="utf-8"))
        self.assertFalse(journal["restored"])
        self.project.target.fail_switch = None
        worker.restore_textless(self.resolve, self.request)
        self.assert_restored()

    def test_progress_write_failure_cannot_skip_track_restoration(self):
        original = worker.durable_json
        def write(path, data):
            if str(path) == self.request["progress_path"]:
                raise OSError("disk full")
            original(path, data)
        with patch.object(worker, "durable_json", side_effect=write):
            with self.assertRaises(OSError):
                worker.export_textless(self.resolve, self.request)
        self.assert_restored()


if __name__ == "__main__":
    unittest.main()
