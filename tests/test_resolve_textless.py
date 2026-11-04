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
        return self.enabled[kind, index]

    def SetTrackEnable(self, kind, index, value):
        if (kind, index, value) == self.fail_switch:
            return False
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
        return {"H.264": "H264"}

    def GetCurrentRenderMode(self):
        return self.mode

    def SetCurrentRenderMode(self, value):
        self.mode = value
        return True

    def GetCurrentRenderFormatAndCodec(self):
        return self.format.copy()

    def SetCurrentRenderFormatAndCodec(self, extension, codec):
        self.format = {"format": extension, "codec": codec}
        return True

    def SaveAsNewRenderPreset(self, name):
        self.presets[name] = copy.deepcopy(self.settings)
        return True

    def GetRenderPresetList(self):
        return ["H.264 Master", *self.presets]

    def LoadRenderPreset(self, name):
        self.settings = copy.deepcopy(self.presets.get(name, {"ExportSubtitle": True}))
        return True

    def DeleteRenderPreset(self, name):
        del self.presets[name]
        return True

    def SetRenderSettings(self, settings):
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
            (Path(self.settings["TargetDir"]) / "textless.mp4").write_bytes(b"fixture-video")
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
        self.resolve.GetCurrentPage.return_value = "edit"
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

    def test_failed_or_cancelled_render_restores_without_deleting_existing_queue(self):
        for outcome in ("Failed", "Cancelled"):
            with self.subTest(outcome=outcome):
                self.project.outcome = outcome
                with self.assertRaises(worker.WorkerError):
                    worker.export_textless(self.resolve, self.request)
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
