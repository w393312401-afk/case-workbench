"""Resolve worker crash recovery against an in-memory project, without the live editor."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_production_resolve as fixture
import resolve_worker as worker


class RecoverableWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="resolve-recover-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.audio = self.root / "voiceover.wav"
        self.audio.write_bytes(b"wav")
        self.request = {**fixture.build_request(self.audio), "operation_key": "a" * 64,
                        "recovery_file": str(self.root / "resolve.json")}
        self.project = fixture.FakeProject()
        self.resolve = fixture.FakeResolve(self.project)

    def assert_one_build(self):
        self.assertEqual(len(self.project.items), 1)
        self.assertEqual(len(self.project.pool.clips), 1)
        target = self.project.items[0]
        self.assertEqual(len(target.GetItemListInTrack("audio", 2)), 1)
        self.assertEqual(len(target.GetMarkers()), len(self.request["markers"]))
        self.assertNotIn("__WB_", target.GetName())

    def test_repeated_identity_returns_original_timeline_without_import_or_append(self):
        first = worker.build(self.resolve, self.request)
        again = worker.build(self.resolve, self.request)
        self.assertEqual(first["unique_id"], again["unique_id"])
        self.assertTrue(again["recovered"])
        self.assert_one_build()

    def test_crash_between_create_and_journal_uid_recovers_unique_temporary_name(self):
        write = worker.durable_json
        def crashed(path, value):
            if value.get("stage") == "created":
                raise OSError("crash after Resolve created the timeline")
            return write(path, value)
        with patch.object(worker, "durable_json", side_effect=crashed):
            with self.assertRaises(OSError):
                worker.build(self.resolve, self.request)
        original = self.project.items[0].uid
        result = worker.build(self.resolve, self.request)
        self.assertEqual(result["unique_id"], original)
        self.assert_one_build()

    def test_crash_after_append_reads_existing_clip_instead_of_appending_again(self):
        append = self.project.pool.AppendToTimeline
        def crashed(infos):
            append(infos)
            raise RuntimeError("lost append response")
        with patch.object(self.project.pool, "AppendToTimeline", side_effect=crashed):
            with self.assertRaises(RuntimeError):
                worker.build(self.resolve, self.request)
        worker.build(self.resolve, self.request)
        self.assert_one_build()

    def test_crash_after_save_recovers_same_uid_after_temporary_name_was_removed(self):
        write = worker.durable_json
        def crashed(path, value):
            if value.get("stage") == "completed":
                raise OSError("lost final result write")
            return write(path, value)
        with patch.object(worker, "durable_json", side_effect=crashed):
            with self.assertRaises(OSError):
                worker.build(self.resolve, self.request)
        original = self.project.items[0].uid
        result = worker.build(self.resolve, self.request)
        self.assertEqual(result["unique_id"], original)
        self.assertTrue(result["saved"])
        self.assert_one_build()

    def test_partial_markers_can_finish_without_rebuilding(self):
        self.project.fail_marker = "2"
        with self.assertRaises(worker.WorkerError):
            worker.build(self.resolve, self.request)
        self.project.items[0].fail_marker = None
        worker.build(self.resolve, self.request)
        self.assert_one_build()

    def test_early_recovery_preserves_user_settings_at_each_partial_stage(self):
        for stage, crash_write in (("creating", "created"), ("created", "placing"),
                                   ("placing", "placed"), ("marking", None)):
            with self.subTest(stage=stage):
                project = fixture.FakeProject()
                resolve = fixture.FakeResolve(project)
                request = {**self.request, "recovery_file": str(self.root / (stage + ".json"))}
                write = worker.durable_json
                def crashed(path, value):
                    if value.get("stage") == crash_write:
                        raise OSError("lost setup journal")
                    return write(path, value)
                if crash_write:
                    with patch.object(worker, "durable_json", side_effect=crashed):
                        with self.assertRaises(OSError):
                            worker.build(resolve, request)
                else:
                    project.fail_marker = "2"
                    with self.assertRaises(worker.WorkerError):
                        worker.build(resolve, request)
                self.assertEqual(json.loads(Path(request["recovery_file"]).read_text())["stage"], stage)
                target = project.items[0]
                target.settings = {"timelineFrameRate": "25", "timelineResolutionWidth": "720",
                                   "timelineResolutionHeight": "1280"}
                target.SetTrackName("audio", 2, "用户配音")
                target.fail_marker = None
                before = copy.deepcopy((target.settings, target.track_names, target.items, target.markers))
                with self.assertRaisesRegex(worker.WorkerError, "未覆盖用户修改"):
                    worker.build(resolve, request)
                self.assertEqual(target.settings, before[0])
                self.assertEqual(target.track_names, before[1])
                self.assertEqual(len(target.GetItemListInTrack("audio", 2)), len(before[2].get(2, [])))
                self.assertEqual(target.markers, before[3])
                self.assertEqual(len(project.items), 1)
                self.assertEqual(len(project.pool.clips), 1)
                self.assertEqual(resolve.saved, 0)

    def test_partial_marker_recovery_preserves_user_track_name_alone(self):
        self.project.fail_marker = "2"
        with self.assertRaises(worker.WorkerError):
            worker.build(self.resolve, self.request)
        target = self.project.items[0]
        target.SetTrackName("audio", 2, "人工声音")
        target.fail_marker = None
        with self.assertRaisesRegex(worker.WorkerError, "未覆盖用户修改"):
            worker.build(self.resolve, self.request)
        self.assertEqual(target.GetTrackName("audio", 2), "人工声音")
        self.assertEqual(len(target.GetMarkers()), 1)

    def test_ambiguous_create_without_original_timeline_does_not_create_another(self):
        with patch.object(self.project.pool, "CreateEmptyTimeline", side_effect=RuntimeError("lost response")):
            with self.assertRaises(RuntimeError):
                worker.build(self.resolve, self.request)
        with self.assertRaisesRegex(worker.WorkerError, "未重复建线"):
            worker.build(self.resolve, self.request)
        self.assertEqual(self.project.items, [])
        self.assertEqual(len(self.project.pool.clips), 1)

    def test_changed_parameters_or_deleted_original_cannot_reuse_identity(self):
        worker.build(self.resolve, self.request)
        with self.assertRaisesRegex(worker.WorkerError, "不同参数"):
            worker.build(self.resolve, {**self.request, "width": 720})
        self.project.items.clear()
        with self.assertRaisesRegex(worker.WorkerError, "未重复建线"):
            worker.build(self.resolve, self.request)
        self.assertEqual(len(self.project.pool.clips), 1)

    def test_save_failure_is_recorded_and_cannot_be_mistaken_for_durable_success(self):
        with patch.object(self.resolve, "SaveProject", return_value=False):
            result = worker.build(self.resolve, self.request)
        self.assertFalse(result["saved"])
        self.assertIn("保存未确认", result["warnings"][0])
        self.assert_one_build()

    def test_unregistered_saved_timeline_preserves_later_user_changes(self):
        write = worker.durable_json
        def crashed(path, value):
            if value.get("stage") == "completed":
                raise OSError("final journal failed")
            return write(path, value)
        with patch.object(worker, "durable_json", side_effect=crashed):
            with self.assertRaises(OSError):
                worker.build(self.resolve, self.request)
        target = self.project.items[0]
        target.settings = {"timelineFrameRate": "25", "timelineResolutionWidth": "720", "timelineResolutionHeight": "1280"}
        target.SetTrackName("audio", 2, "用户配音")
        target.items[2][0].media = fixture.FakeClip(self.root / "user-voice.wav")
        before = copy.deepcopy(target.settings)
        with self.assertRaisesRegex(worker.WorkerError, "未覆盖用户修改"):
            worker.build(self.resolve, self.request)
        self.assertEqual(target.settings, before)
        self.assertEqual(target.GetTrackName("audio", 2), "用户配音")
        self.assertEqual(len(self.project.items), 1)
        self.assertEqual(self.resolve.saved, 1)

    def test_checkpoint_comparison_detects_marker_content_changes(self):
        write = worker.durable_json
        def crashed(path, value):
            if value.get("stage") == "completed":
                raise OSError("final journal failed")
            return write(path, value)
        with patch.object(worker, "durable_json", side_effect=crashed):
            with self.assertRaises(OSError):
                worker.build(self.resolve, self.request)
        target = self.project.items[0]
        first = next(iter(target.markers.values()))
        first["name"] = "用户修改的意群"
        with self.assertRaisesRegex(worker.WorkerError, "未覆盖用户修改"):
            worker.build(self.resolve, self.request)
        self.assertEqual(first["name"], "用户修改的意群")

    def test_unknown_source_in_another_output_is_not_silently_omitted_from_dedup(self):
        built = worker.build(self.resolve, self.request)
        other = fixture.FakeTimeline("Other output")
        other.GetItemListInTrack = lambda kind, index: [fixture.FakeItem(0, 30)] if kind == "video" else []
        self.project.items.append(other)
        result = worker.inspect(self.resolve, {"project": "202H", "unique_id": built["unique_id"]})
        self.assertFalse(result["dedup_complete"])

    def test_inspection_is_read_only_and_returns_actual_identity_source_and_track_fields(self):
        built = worker.build(self.resolve, self.request)
        saved = self.resolve.saved
        result = worker.inspect(self.resolve, {"project": "202H", "unique_id": built["unique_id"]})
        voice = result["tracks"]["audio"][1]
        item = voice["items"][0]
        self.assertEqual((result["fps"], result["width"], result["height"]), ("29.97", "1080", "1920"))
        self.assertEqual((voice["enabled"], voice["locked"], item["source_start"], item["source_end"]), (True, False, 0, 179))
        self.assertTrue(item["media_id"])
        self.assertEqual(item["media_path"], str(self.audio))
        self.assertEqual(self.resolve.saved, saved)
        self.assertNotIn("saved", result)


if __name__ == "__main__":
    unittest.main()
