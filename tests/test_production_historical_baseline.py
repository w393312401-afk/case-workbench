"""Historical recovery uses independent saved files, never the current snapshot."""
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET
import zipfile

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
CONFIG = json.loads((APP / "config.json").read_text(encoding="utf-8"))
sys.path.insert(0, str(Path(CONFIG["skill_root"]) / "scripts"))
from elevenlabs_client import VoiceError
from production_historical_baseline import restore_historical_baseline
from production_validation import validate_edit
from workbench_editing import EditingWorkbenchMixin, handoff_markdown
from workbench_production import meaning_markers


class HistoricalBaselineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="historical-baseline-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.captured = "2026-10-10T13:49:53.002493+00:00"
        self.captured_stamp = datetime.fromisoformat(self.captured).timestamp()
        self.job = {"id": "PRD-0123456789ab", "product": "202H", "title": "测试稿", "script_id": "SCR-0123456789ab",
                    "script_version": 1, "lines": [{"number": n, "spoken": f"Line {n}.", "zh": f"第 {n} 句", "proof": "实拍",
                                                  "tags": "展示", "evidence": "待核对"} for n in (1, 2, 3)]}
        self.take = {"id": "TAK-0123456789ab", "model_id": "eleven_v4", "created_at": "2026-10-10T13:09:00+00:00",
                     "voice_id": "VOICE123456789",
                     "duration": 3.2, "groups": [{"number": n, "start": start, "end": end, "coverage": 1.0}
                                                  for n, start, end in ((1, 0, .9), (2, 1.0, 1.9), (3, 2.0, 3.2))],
                     "wav": "PRD-0123456789ab/TAK-0123456789ab/voiceover.wav", "audio": "voiceover.mp3",
                     "tts_lines": ["Line 1.", "Line 2.", "Line 3."], "tts_sha256": "frozen-text"}
        self.timeline = {"unique_id": "historical-timeline", "name": "Lab261010-202-1", "project": "202H", "fps": "29.97",
                         "frames": 95, "take_id": self.take["id"], "markers": 3, "built_at": "2026-10-10T13:10:00+00:00"}
        self.edit = {"id": "EDT-0123456789ab", "timeline_uid": self.timeline["unique_id"], "timeline_name": self.timeline["name"], "take_id": self.take["id"],
                     "started_at": "2026-10-10T13:10:24+00:00", "finished_at": "2026-10-10T14:12:24+00:00",
                     "resumption": {"by": "Codex", "started_at": self.captured}}
        self.folder = self.root / "edits" / self.edit["id"]
        self.folder.mkdir(parents=True)
        config = {"project": "202H", "fps": "29.97", "width": 1080, "height": 1920}
        self.config = config
        handoff = handoff_markdown(self.job, self.take, self.timeline, config, mode="explain", instruction="", materials=[],
                                   ledger="ledger", notes={}, voice_label="测试音色")
        self.write_file("handoff.md", handoff.encode("utf-8"), self.captured_stamp - 600)
        self.voice_path = str(self.root / "audio" / self.take["wav"])
        self.voice = {"uid": "original-voice-item", "name": "Lab261010-202-1_配音", "media_id": "original-voice-media",
                      "path": self.voice_path, "src_in": 0, "src_end": 95, "start": 0, "end": 95,
                      "properties": {"AudioVolume": 0.0, "AudioPan": 0.0}}
        track = lambda kind, index, name, items: {"type": kind, "index": index, "name": name,
                                                 "locked": False, "enabled": True, "items": items}
        self.old_timeline = {"uid": self.timeline["unique_id"], "name": self.timeline["name"], "start": 0, "end": 95, "fps": 29.97,
                             "markers": {str(m["frame"]): {"duration": m["duration"], "customData": m["custom"], "name": m["name"],
                                                          "note": m["note"], "color": "Blue"} for m in meaning_markers(self.job, self.take, "29.97")},
                             "tracks": [track("video", 1, "视频 1", []), track("audio", 1, "音频 1", []), track("audio", 2, "配音", [self.voice])]}
        self.archive = {"project": "202H", "active": self.timeline["unique_id"], "version": "21.1.0.14", "timelines": [self.old_timeline]}
        self.write_snapshot()
        self.backup = {"schema_version": 1, "jobs": [{**self.job, "takes": [self.take], "timelines": [self.timeline], "edits": []}],
                       "settings": {"products": {"202H": config}}}
        self.backup_path = self.write_catalog(self.backup)
        self.write_drt()

    def write_file(self, name, raw, stamp):
        path = self.folder / name
        path.write_bytes(raw)
        os.utime(path, (stamp, stamp))
        return path

    def write_snapshot(self):
        self.write_file("resume_snapshot.json", json.dumps(self.archive, ensure_ascii=False).encode(), self.captured_stamp)

    def write_catalog(self, value):
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
        directory = self.root / "backups"
        directory.mkdir(exist_ok=True)
        path = directory / (hashlib.sha256(raw).hexdigest() + ".json")
        path.write_bytes(raw)
        return path

    def write_drt(self, *, videos=0, first_audio=0, voices=1, voice_path=None, duration=95, start=0):
        pool = ET.Element("Sm2MpFolder")
        timeline = ET.SubElement(pool, "Sm2Timeline")
        ET.SubElement(timeline, "Name").text = self.timeline["name"]
        media = ET.SubElement(pool, "Sm2MpMediaClip", {"DbId": "media-ref"})
        ET.SubElement(media, "Name").text = "voiceover.wav"
        sequence = ET.Element("Sm2SequenceContainer")
        def track(kind, count):
            vector = sequence.find(kind + "TrackVec")
            if vector is None:
                vector = ET.SubElement(sequence, kind + "TrackVec")
            element = ET.SubElement(vector, "Element")
            row = ET.SubElement(element, "Sm2TiTrack")
            items = ET.SubElement(row, "Items")
            for _ in range(count):
                ET.SubElement(items, "Element")
            return items
        track("Video", videos)
        track("Audio", first_audio)
        items = track("Audio", 0)
        for _ in range(voices):
            element = ET.SubElement(items, "Element")
            clip = ET.SubElement(element, "Sm2TiAudioClip")
            for key, value in {"Name": self.voice["name"], "MediaFilePath": voice_path or self.voice_path, "MediaRef": "media-ref",
                               "Start": str(start), "Duration": f"{duration}|fraction", "MediaStartTime": "0"}.items():
                ET.SubElement(clip, key).text = value
        path = self.folder / "resume_before.drt"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("MediaPool/Master/MpFolder.xml", ET.tostring(pool, encoding="utf-8"))
            archive.writestr("SeqContainer/sequence.xml", ET.tostring(sequence, encoding="utf-8"))
        os.utime(path, (self.captured_stamp + 100, self.captured_stamp + 100))

    def restore(self):
        return restore_historical_baseline(self.root, self.job, self.edit, self.timeline, self.take)

    def current(self, baseline):
        post = copy.deepcopy(baseline)
        post.update(saved=True, dedup_complete=True, other_shots=[])
        voice = post["tracks"]["audio"][1]["items"][0]
        voice["enabled"] = True
        voice["properties"] = {"AudioVolume": -12, "AudioPan": .5}
        videos, audios = [], []
        for n, start, end in ((1, 0, 29), (2, 29, 60), (3, 60, 95)):
            video = {"uid": f"v{n}", "name": "原片", "media_id": "source", "media_path": "D:/footage.mov", "source_start": start,
                     "source_end": end, "start": start, "end": end, "enabled": True, "has_audio": True, "linked_ids": [f"a{n}"], "properties": {}}
            videos.append(video)
            audios.append({**video, "uid": f"a{n}", "linked_ids": [f"v{n}"]})
        post["tracks"]["video"][0]["items"] = videos
        post["tracks"]["audio"][0]["items"] = audios
        return post

    def test_real_history_fields_are_restored_without_current_state_filling(self):
        before = {path: path.read_bytes() for path in self.folder.iterdir()}
        result = self.restore()
        baseline = result["baseline"]
        self.assertEqual(result["baseline_origin"], "historical_resume")
        self.assertEqual(result["baseline_at"], self.captured)
        self.assertEqual(result["baseline_evidence"]["captured_at"], self.captured)
        self.assertEqual({row["role"] for row in result["baseline_evidence"]["sources"]},
                         {"resume_snapshot", "native_timeline_backup", "handoff", "pre_edit_catalog"})
        self.assertEqual(baseline["tracks"]["video"][0]["items"], [])
        self.assertEqual(baseline["tracks"]["audio"][0]["items"], [])
        self.assertIsNone(baseline["tracks"]["audio"][1]["items"][0]["enabled"])
        self.assertEqual(baseline["tracks"]["audio"][1]["items"][0]["properties"], {"AudioVolume": 0.0, "AudioPan": 0.0})
        self.assertEqual(result["baseline_evidence"]["unrecorded_fields"], ["voice.clip_enabled"])
        self.assertEqual(result["baseline_evidence"]["configured_fields"], ["width", "height"])
        post = self.current(baseline)
        validation, _ = validate_edit(post, self.job, self.timeline, baseline, recheck=True)
        self.assertEqual(validation["status"], "awaiting_review")
        self.assertTrue(validation["checks"]["voice_preserved"])
        self.assertTrue(validation["checks"]["groups_preserved"])
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_same_length_current_voice_replacement_does_not_pass(self):
        baseline = self.restore()["baseline"]
        post = self.current(baseline)
        post["tracks"]["audio"][1]["items"][0]["media_id"] = "replacement"
        post["tracks"]["audio"][1]["items"][0]["media_path"] = "D:/wrong.wav"
        validation, _ = validate_edit(post, self.job, self.timeline, baseline, recheck=True)
        self.assertEqual(validation["status"], "needs_repair")
        self.assertIn("voice_changed", [issue["code"] for issue in validation["issues"]])

    def test_current_marker_text_note_or_color_change_does_not_pass(self):
        baseline = self.restore()["baseline"]
        for key in ("name", "note", "color"):
            with self.subTest(key=key):
                post = self.current(baseline)
                post["markers"][0][key] = "changed"
                validation, _ = validate_edit(post, self.job, self.timeline, baseline, recheck=True)
                self.assertIn("group_markers_changed", [issue["code"] for issue in validation["issues"]])

    def test_restored_empty_video_track_does_not_exempt_cross_timeline_dedup(self):
        baseline = self.restore()["baseline"]
        post = self.current(baseline)
        post["other_shots"] = [{"media_id": "source", "media_path": "D:/footage.mov", "source_start": 10, "source_end": 20,
                                "timeline_name": "另一条已剪视频", "timeline_uid": "other"}]
        validation, _ = validate_edit(post, self.job, self.timeline, baseline, recheck=True)
        self.assertIn("source_range_used_elsewhere", [issue["code"] for issue in validation["issues"]])
        self.assertEqual(validation["status"], "needs_repair")
        post["dedup_complete"] = False
        self.assertEqual(validate_edit(post, self.job, self.timeline, baseline, recheck=True)[0]["status"], "verification_failed")

    def test_historical_picture_or_original_sound_not_empty_is_refused(self):
        for index in (0, 1):
            with self.subTest(track=index):
                self.old_timeline["tracks"][index]["items"] = [copy.deepcopy(self.voice)]
                self.write_snapshot()
                with self.assertRaises(VoiceError):
                    self.restore()
                self.old_timeline["tracks"][index]["items"] = []

    def test_missing_artifact_or_changed_catalog_hash_is_refused(self):
        path = self.folder / "resume_before.drt"
        raw = path.read_bytes()
        path.unlink()
        with self.assertRaises(VoiceError):
            self.restore()
        self.write_file("resume_before.drt", raw, self.captured_stamp + 100)
        self.backup_path.write_bytes(self.backup_path.read_bytes() + b"\n")
        with self.assertRaises(VoiceError) as caught:
            self.restore()
        self.assertIn("哈希", str(caught.exception))

    def test_independent_catalogs_and_handoff_must_agree(self):
        conflicting = copy.deepcopy(self.backup)
        conflicting["settings"]["products"]["202H"]["width"] = 720
        self.write_catalog(conflicting)
        with self.assertRaises(VoiceError) as caught:
            self.restore()
        self.assertIn("配置矛盾", str(caught.exception))

    def test_historical_duplicate_or_changed_marker_content_is_refused(self):
        first, second = list(self.old_timeline["markers"].values())[:2]
        second["customData"] = first["customData"]
        self.write_snapshot()
        with self.assertRaises(VoiceError):
            self.restore()
        second["customData"] = f"workbench-group:{self.job['id']}:2"
        first["note"] = "a different note"
        self.write_snapshot()
        with self.assertRaises(VoiceError):
            self.restore()

    def test_native_backup_voice_reference_and_track_structure_are_independent(self):
        for options in ({"voices": 2}, {"first_audio": 1}, {"videos": 1}, {"voice_path": "D:/wrong.wav"}, {"duration": 94}):
            with self.subTest(options=options):
                self.write_drt(**options)
                with self.assertRaises(VoiceError):
                    self.restore()

    def test_capture_after_edit_finished_or_snapshot_file_rewritten_is_refused(self):
        self.edit["finished_at"] = "2026-10-10T13:40:00+00:00"
        with self.assertRaises(VoiceError):
            self.restore()
        self.edit["finished_at"] = "2026-10-10T14:12:24+00:00"
        path = self.folder / "resume_snapshot.json"
        os.utime(path, (self.captured_stamp + 60, self.captured_stamp + 60))
        with self.assertRaises(VoiceError):
            self.restore()

    def test_historical_source_dispatches_real_helper_before_comparing_current_state(self):
        result = self.restore()
        current = self.current(result["baseline"])
        job = {**self.job, "takes": [self.take], "timelines": [self.timeline], "edits": [{**self.edit, "status": "needs_repair"}]}
        bench = EditingWorkbenchMixin()
        bench.lock = threading.RLock()
        production = Mock()
        production.root = self.root
        production.replay.return_value = None
        production.job.return_value = job
        production.read.return_value = {}
        production.voice.return_value = {}
        production.product_settings.return_value = self.config
        production.mutate.side_effect = lambda kind, body, extra: {"edit": extra}
        bench.production = production
        bench.creation = Mock()
        bench.creation.get_script.return_value = {"source": {}}
        lock = threading.Lock()
        def inspect(command, payload, **options):
            self.assertEqual(command, "inspect")
            self.assertTrue(payload["save"])
            self.assertTrue(options["held"])
            self.assertTrue(lock.locked())
            return current
        bench.resolve = SimpleNamespace(lock=lock, run=Mock(side_effect=inspect))
        outcome = bench.adopt_edit_baseline(self.job["id"], self.edit["id"], {"request_id": "history-test-request", "source": "historical_resume"})["edit"]
        self.assertEqual(outcome["baseline_origin"], "historical_resume")
        self.assertEqual(outcome["baseline_at"], self.captured)
        self.assertIsNone(outcome["baseline"]["tracks"]["audio"][1]["items"][0]["enabled"])
        self.assertEqual(outcome["status"], "awaiting_review")
        self.assertEqual(production.mutate.call_count, 1)
        self.assertFalse(lock.locked())

    def test_historical_source_missing_evidence_is_refused_without_current_fallback(self):
        baseline = self.restore()["baseline"]
        (self.folder / "resume_snapshot.json").unlink()
        job = {**self.job, "takes": [self.take], "timelines": [self.timeline], "edits": [{**self.edit, "status": "needs_repair"}]}
        bench = EditingWorkbenchMixin()
        bench.lock = threading.RLock()
        bench.production = Mock()
        bench.production.root = self.root
        bench.production.replay.return_value = None
        bench.production.job.return_value = job
        bench.production.read.return_value = {}
        bench.production.voice.return_value = {}
        bench.production.product_settings.return_value = self.config
        bench.creation = Mock()
        bench.creation.get_script.return_value = {"source": {}}
        lock = threading.Lock()
        bench.resolve = SimpleNamespace(lock=lock, run=Mock(return_value=self.current(baseline)))
        with self.assertRaises(VoiceError) as caught:
            bench.adopt_edit_baseline(self.job["id"], self.edit["id"], {"request_id": "missing-history-request", "source": "historical_resume"})
        self.assertEqual(caught.exception.status, 409)
        bench.production.mutate.assert_not_called()
        self.assertFalse(lock.locked())

    def test_unrecognized_source_is_refused_before_reading_resolve_or_production(self):
        bench = EditingWorkbenchMixin()
        bench.production, bench.resolve = Mock(), Mock()
        for source in ("current", "unknown", None):
            with self.subTest(source=source), self.assertRaises(VoiceError) as caught:
                bench.adopt_edit_baseline(self.job["id"], self.edit["id"], {"source": source})
            self.assertEqual(caught.exception.status, 400)
        bench.production.job.assert_not_called()
        bench.resolve.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
