"""Isolated timeline preview rendering, persistence and HTTP playback."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import test_server as fixture
from resolve_bridge import ResolveBridge
from timeline_preview import TimelinePreviews, optimize_playback

JOB = "PRD-0123456789ab"


def wait(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("preview did not finish")


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.job = {"id": JOB, "selected_take": "take", "takes": [{"id": "take"}],
                    "timelines": [{"unique_id": "one", "take_id": "take", "name": "First", "project": "202H", "creator": "Zack",
                    "folder": ["Zack"], "project_id": "zack-project"}, {"unique_id": "two", "name": "Second", "project": "202H"}], "edits": []}
        self.calls, self.gate, self.failed = [], None, False
        self.app = type("App", (), {})()
        self.app.lock = threading.RLock()
        self.app.production = type("Production", (), {})()
        self.app.production.root = Path(self.temp.name) / "production"
        self.app.production.job = lambda identifier: copy.deepcopy(self.job)
        self.app.resolve = ResolveBridge(runner=self.runner)
        self.previews = TimelinePreviews(self.app, optimizer=lambda source, output, progress: source)

    def runner(self, command, **options):
        payload = json.loads(options["input"])
        self.calls.append(payload)
        if payload["command"] == "export_preview":
            Path(payload["progress_path"]).write_text(json.dumps({"message": "正在导出预览…", "percent": 35}), encoding="utf-8")
            if self.gate:
                self.gate.wait(5)
            if self.failed:
                raise subprocess.TimeoutExpired(command, 1320)
            path = Path(payload["output_dir"]) / "preview.mp4"
            path.write_bytes(b"preview-video")
            result = {"ok": True, "unique_id": payload["unique_id"], "restored": True, "video_path": str(path)}
        else:
            result = {"ok": True, "restored": True}
        return subprocess.CompletedProcess(command, 0, json.dumps(result).encode(), b"")

    def start(self, uid="one"):
        reply = self.previews.start(JOB, {"timeline_uid": uid})
        wait(lambda: self.previews.detail(JOB, uid)["status"] != "running")
        return reply, self.previews.detail(JOB, uid)

    def test_partial_timeline_is_rendered_with_its_identity_and_updates_its_own_player_url(self):
        reply, done = self.start()
        self.assertEqual(reply["status"], "running")
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(done["video"]["kind"], "半成品预览")
        self.assertEqual(done["video"]["name"], "First.mp4")
        self.assertEqual(self.calls[0]["project_id"], "zack-project")
        self.assertEqual(self.calls[0]["folder"], ["Zack"])
        self.assertEqual(self.calls[0]["command"], "export_preview")
        self.assertIsNone(self.previews.detail(JOB, "two")["video"])
        _, again = self.start()
        self.assertNotEqual(done["video"]["url"], again["video"]["url"])
        self.assertFalse(self.previews.busy())

    def test_reviewed_timeline_is_labelled_as_a_final_preview(self):
        with patch("timeline_preview.reviewed_source", return_value={}):
            _, done = self.start()
        self.assertEqual(done["video"]["kind"], "成片预览")

    def confirmation(self, stamp="2026-11-05T00:00:00+00:00"):
        receipt = {"timeline_uid": "one", "timeline_name": "First", "take_id": "take", "at": stamp, "basis": "human_review"}
        self.job["manual_completion"] = receipt
        return {"completed": True, "timeline_uid": "one", "manual_completion": receipt}

    def test_human_confirmation_exports_once_and_exposes_the_hd_download(self):
        result = self.confirmation()
        queued = self.previews.after_confirmation(JOB, result)
        self.assertTrue(queued["completed"])
        self.assertEqual(queued["video_preview"]["status"], "running")
        wait(lambda: not self.previews.busy())
        done = self.previews.detail(JOB, "one")
        self.assertTrue(done["automatic"])
        self.assertEqual(done["video"]["kind"], "成片预览")
        self.assertEqual(done["video"]["download_url"], done["video"]["url"])
        self.assertIsNone(self.previews.detail(JOB, "two")["video"])
        self.assertEqual(self.previews.after_confirmation(JOB, result)["video_preview"]["video"], done["video"])
        restored = TimelinePreviews(self.app, optimizer=lambda source, output, progress: source)
        wait(lambda: not restored.busy())
        restored.after_confirmation(JOB, result)
        self.assertEqual([call["command"] for call in self.calls], ["export_preview"])

    def test_confirmation_during_an_older_preview_queues_a_fresh_capture(self):
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)
        self.previews.start(JOB, {"timeline_uid": "one"})
        original = self.previews._read(JOB, "one")["run_id"]
        wait(lambda: bool(self.calls))
        result = self.confirmation()
        self.previews.after_confirmation(JOB, result)
        self.previews.after_confirmation(JOB, result)
        self.gate.set()
        wait(lambda: not self.previews.busy() and len(self.calls) == 2)
        done = self.previews.detail(JOB, "one")
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(done["video"]["kind"], "成片预览")
        self.assertNotEqual(done["video"]["version"], original)
        # The fresh capture replaces the older preview instead of piling up beside it.
        self.assertEqual(len(list(self.previews._folder(JOB, "one").glob("runs/*/preview.mp4"))), 1)

    def test_revoked_confirmation_is_not_exported_after_waiting_for_resolve(self):
        result = self.confirmation()
        with self.app.resolve.access():
            self.previews.after_confirmation(JOB, result)
            self.job.pop("manual_completion")
        wait(lambda: not self.previews.busy())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.previews.detail(JOB, "one")["status"], "failed")
        self.assertIsNone(self.previews.detail(JOB, "one")["video"])

    def test_a_confirmation_queued_behind_an_interrupted_preview_resumes_after_restart(self):
        result = self.confirmation()
        folder = self.previews._folder(JOB, "one")
        folder.mkdir(parents=True)
        (folder / "preview.json").write_text(json.dumps({"job_id": JOB, "timeline_uid": "one", "run_id": "interrupted",
            "status": "running", "video": None, "pending_confirmation": self.previews._confirmation(result)}), encoding="utf-8")
        restored = TimelinePreviews(self.app, optimizer=lambda source, output, progress: source)
        wait(lambda: not restored.busy())
        done = restored.detail(JOB, "one")
        self.assertEqual((done["status"], done["video"]["kind"]), ("done", "成片预览"))
        self.assertEqual(len(self.calls), 1)

    def test_export_failure_keeps_the_review_and_previous_video_without_repeating_the_request(self):
        _, previous = self.start()
        self.failed = True
        result = self.confirmation()
        self.previews.after_confirmation(JOB, result)
        wait(lambda: not self.previews.busy())
        self.assertEqual(self.previews.detail(JOB, "one")["video"], previous["video"])
        self.assertEqual(self.job["manual_completion"], result["manual_completion"])
        calls = len(self.calls)
        self.assertEqual(self.previews.after_confirmation(JOB, result)["video_preview"]["status"], "failed")
        self.assertEqual(len(self.calls), calls)

    def test_new_review_exports_again_and_an_old_replayed_review_cannot_replace_it(self):
        old = self.confirmation()
        self.previews.after_confirmation(JOB, old)
        wait(lambda: not self.previews.busy())
        new = self.confirmation("2026-11-05T00:01:00+00:00")
        self.previews.after_confirmation(JOB, new)
        wait(lambda: not self.previews.busy())
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.previews.after_confirmation(JOB, old)["video_preview"]["status"], "failed")
        self.assertEqual(len(self.calls), 2)

    def test_duplicate_requests_share_the_same_render_and_report_progress(self):
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)
        self.previews.start(JOB, {"timeline_uid": "one"})
        wait(lambda: bool(self.calls))
        self.assertEqual(self.previews.detail(JOB, "one")["percent"], 35)
        self.previews.start(JOB, {"timeline_uid": "one"})
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.previews.busy(JOB))
        self.gate.set()
        wait(lambda: not self.previews.busy())

    def test_a_new_preview_replaces_the_earlier_ones(self):
        _, first = self.start()
        runs = self.previews._folder(JOB, "one") / "runs"
        stuck = runs / "unrestored"
        stuck.mkdir(parents=True)
        (stuck / "recovery.json").write_text(json.dumps({"restored": False}), encoding="utf-8")
        _, second = self.start()
        # Only the newest capture stays, plus a run whose DaVinci state still needs restoring.
        self.assertEqual(sorted(path.name for path in runs.iterdir()), sorted([second["video"]["version"].split("-")[0], "unrestored"]))
        self.assertEqual(self.previews.video_file(JOB, "one", second["video"]["version"]).read_bytes(), b"preview-video")

    def test_failure_recovers_and_keeps_the_previous_video(self):
        _, old = self.start()
        self.failed = True
        _, failed = self.start()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["video"], old["video"])
        self.assertEqual(self.calls[-1]["command"], "restore_textless")
        self.assertEqual(self.previews.video_file(JOB, "one", old["video"]["version"]).read_bytes(), b"preview-video")

    def test_existing_export_is_available_but_photo_video_is_not_used_as_preview(self):
        photo = self.app.production.root / "photo-sets" / "PHS-0123456789ab"
        photo.mkdir(parents=True)
        (photo / "video.mp4").write_bytes(b"textless")
        self.assertIsNone(self.previews.detail(JOB, "one")["video"])
        exports = self.app.production.root / "exports"
        exports.mkdir()
        (exports / "First.mp4").write_bytes(b"existing-video")
        video = self.previews.detail(JOB, "one")["video"]
        self.assertEqual(video["kind"], "已导出视频")
        self.assertEqual(self.previews.video_file(JOB, "one", video["version"]).read_bytes(), b"existing-video")

    def test_unknown_or_archived_binding_and_unexpected_input_are_rejected(self):
        for body in ({"timeline_uid": "missing"}, {"timeline_uid": "one", "textless": True}):
            with self.assertRaises(Exception):
                self.previews.start(JOB, body)
        self.job["timelines"][0]["archived"] = True
        with self.assertRaises(Exception):
            self.previews.start(JOB, {"timeline_uid": "one"})
        self.assertFalse(self.calls)

    def test_startup_upgrades_old_playback_without_rerendering_or_breaking_old_urls(self):
        _, old = self.start()
        def optimize(source, output, progress):
            self.assertFalse(self.app.resolve.busy())
            output.write_bytes(b"small-playback")
            return output
        upgraded = TimelinePreviews(self.app, optimizer=optimize)
        wait(lambda: not upgraded.busy())
        current = upgraded.detail(JOB, "one")
        self.assertNotEqual(current["video"]["url"], old["video"]["url"])
        self.assertEqual(current["video"]["source_version"], old["video"]["version"])
        self.assertEqual(upgraded.video_file(JOB, "one", current["video"]["version"]).read_bytes(), b"small-playback")
        self.assertEqual(upgraded.video_file(JOB, "one", old["video"]["version"]).read_bytes(), b"preview-video")
        self.assertEqual(len(self.calls), 1)

    def test_playback_encoding_releases_resolve_and_failure_keeps_a_playable_capture(self):
        def optimize(source, output, progress):
            self.assertFalse(self.app.resolve.busy())
            raise RuntimeError("encoder unavailable")
        self.previews.optimizer = optimize
        _, done = self.start()
        self.assertEqual(done["status"], "done")
        self.assertIn("高清", done["message"])
        self.assertEqual(self.previews.video_file(JOB, "one", done["video"]["version"]).read_bytes(), b"preview-video")


class PlaybackEncodingTests(unittest.TestCase):
    def test_actual_preview_is_smaller_seekable_and_has_audio_and_front_loaded_metadata(self):
        import struct
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "hd.mp4", root / "playback.mp4"
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=3",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:v", "libx264", "-preset", "ultrafast",
                            "-crf", "17", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)], check=True, capture_output=True, creationflags=flags)
            original = source.read_bytes()
            optimize_playback(source, output, lambda _: None)
            self.assertEqual(source.read_bytes(), original)
            self.assertLess(output.stat().st_size, source.stat().st_size)
            metadata = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
                                      check=True, capture_output=True, creationflags=flags)
            metadata = json.loads(metadata.stdout)
            video = next(s for s in metadata["streams"] if s["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"], video["codec_name"]), (720, 1280, "h264"))
            self.assertTrue(any(s["codec_type"] == "audio" for s in metadata["streams"]))
            self.assertAlmostEqual(float(metadata["format"]["duration"]), 3, delta=.15)
            atoms, offset = [], 0
            with output.open("rb") as stream:
                while offset + 8 <= output.stat().st_size:
                    stream.seek(offset)
                    size, kind = struct.unpack(">I4s", stream.read(8))
                    self.assertGreaterEqual(size, 8)
                    atoms.append(kind)
                    offset += size
            self.assertLess(atoms.index(b"moov"), atoms.index(b"mdat"))


class PreviewRoutesTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def test_saved_ai_delivery_confirmation_exports_its_timeline_without_another_inspect(self):
        from test_production_verification_http import VerificationHTTPTests
        from production_validation import MANUAL_SCOPE
        job, edit = VerificationHTTPTests.seed_job(self)
        app = self.server.app
        data = app.production.read()
        row = data["jobs"][-1]["edits"][-1]
        row.update(completion_policy="skill_delivery", status="completed", report={"saved": True, "gaps": []}, validation=None)
        app.production.write(data)
        path = f"/api/production/jobs/{job}/edits/{edit}/approval"
        body = {"request_id": "confirm-export-isolated", "approved": True, "reviewed": list(MANUAL_SCOPE)}
        status, _, result = self.request("POST", path, body)
        self.assertEqual(status, 200, result)
        self.assertTrue(result["approved"])
        self.assertEqual(result["video_preview"]["status"], "running")
        wait(lambda: not app.video_previews.busy())
        preview = app.video_previews.detail(job, "uid-1")
        self.assertEqual((preview["status"], preview["video"]["kind"]), ("done", "成片预览"))
        self.assertEqual([call["command"] for call in self.runner.calls], ["export_preview"])
        self.assertEqual(self.runner.calls[0]["unique_id"], "uid-1")
        self.request("POST", path, body)
        self.assertEqual(len(self.runner.calls), 1)

    def test_a_queue_failure_does_not_undo_the_saved_human_confirmation(self):
        from test_production_manual_completion import ManualCompletionHTTPTests
        job = ManualCompletionHTTPTests.seed_job(self)
        service = self.server.app.video_previews
        with patch.object(service, "start", side_effect=RuntimeError("export queue unavailable")):
            status, _, result = self.request("POST", f"/api/production/jobs/{job}/completion",
                {"request_id": "confirm-queue-failure", "completed": True, "timeline_uid": "uid-1"})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["completed"])
        self.assertEqual(self.server.app.production.job(job)["status"], "approved")
        self.assertEqual(result["video_preview"]["status"], "failed")
        self.assertIn("人工确认已保存", result["video_preview"]["error"])
        self.assertEqual(self.runner.calls, [])

    def test_preview_video_is_seekable_and_rejects_another_version_or_timeline(self):
        job = {"id": JOB, "timelines": [{"unique_id": "one", "name": "First", "project": "202H"}], "edits": []}
        with patch.object(self.server.app.production, "job", return_value=job):
            root = self.server.app.production.root / "exports"
            root.mkdir()
            (root / "First.mp4").write_bytes(self.video_bytes)
            status, _, detail = self.request("GET", f"/api/production/jobs/{JOB}/preview?timeline=one")
            self.assertEqual(status, 200, detail)
            status, headers, body = self.request("GET", detail["video"]["url"], headers={"Range": "bytes=4-15"})
            self.assertEqual(status, 206)
            self.assertEqual(headers["Content-Type"], "video/mp4")
            self.assertEqual(body, self.video_bytes[4:16])
            self.assertEqual(headers["Cache-Control"], "private, max-age=31536000, immutable")
            tag = headers["ETag"]
            cached, cached_headers, cached_body = self.request("GET", detail["video"]["url"], headers={"If-None-Match":tag})
            self.assertEqual(cached, 304)
            self.assertFalse(cached_body)
            self.assertEqual(cached_headers["ETag"], tag)
            self.assertEqual(self.request("GET", detail["video"]["url"], headers={"Range":"bytes=4-15","If-Range":tag})[0], 206)
            changed, _, full = self.request("GET", detail["video"]["url"], headers={"Range":"bytes=4-15","If-Range":'"old"'})
            self.assertEqual(changed, 200)
            self.assertEqual(full, self.video_bytes)
            self.assertEqual(self.request("GET", f"/api/production/jobs/{JOB}/preview/video?timeline=one&version=other")[0], 404)
            self.assertEqual(self.request("GET", f"/api/production/jobs/{JOB}/preview?timeline=other")[0], 404)

    def test_background_route_requires_page_token_and_blocks_restart_while_rendering(self):
        service = self.server.app.video_previews
        path = f"/api/production/jobs/{JOB}/preview"
        with patch.object(service, "start", return_value={"status": "running"}) as start:
            self.assertEqual(self.request("POST", path, {"timeline_uid": "one"}, authenticated=False)[0], 403)
            start.assert_not_called()
            self.assertEqual(self.request("POST", path, {"timeline_uid": "one"})[0], 202)
            start.assert_called_once_with(JOB, {"timeline_uid": "one"})
        with patch.object(service, "busy", return_value=True):
            self.assertIn("视频预览", self.server._activity_blocker())
            with self.assertRaisesRegex(Exception, "视频预览"):
                self.server.app.ensure_data_idle()


if __name__ == "__main__":
    unittest.main()
