"""Video publication uses disposable files/stores, never exports or calls a paid provider."""
import copy
from datetime import date, timedelta
import hashlib
import json
import shutil
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creation_store import CreationStore
from elevenlabs_client import VoiceError
from production_store import ProductionStore, public_job
from workbench_video_publish import VideoPublicationStore, VideoPublishMixin, remap_publication_document, reviewed_source

JOB = "PRD-111111111111"
TAKE = "TAK-111111111111"
SCRIPT = "SCR-111111111111"
POST = "7999999999999999901"
OTHER = "7999999999999999902"
VIDEO = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"test-video-bytes" * 100


class Bench(VideoPublishMixin):
    def __init__(self, root):
        self.lock = threading.RLock()
        self.production = ProductionStore(root, self.lock)
        self.creation = CreationStore(root, self.lock)
        self.state_cache = {}

    def save_script_status(self, identifier, body, *, video_job_id=None):
        body = copy.deepcopy(body)
        body["script_status"]["script_id"] = identifier
        return self.creation.mutate("script_status", body)


class VideoPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bench = Bench(self.root)
        self.video = self.root / "actual.mp4"
        self.video.write_bytes(VIDEO)
        job = {"id": JOB, "product": "051A", "script_id": SCRIPT, "script_version": 1,
               "script_family": SCRIPT, "title": "Test", "direction_id": "A1", "variant_label": "A",
               "lines": [{"number": 1, "spoken": "Try this.", "zh": "试试这个。"}], "text_sha256": "frozen",
               "selected_take": TAKE, "takes": [{"id": TAKE, "model_id": "model", "voice_id": "voice", "text": "Try this."}],
               "timelines": [{"unique_id": "timeline-1", "name": "Reviewed", "take_id": TAKE, "saved": True}],
               "edits": [], "manual_completion": {"at": "2026-10-01", "take_id": TAKE, "timeline_uid": "timeline-1", "basis": "human_review"},
               "archived": False, "history": []}
        data = self.bench.production.empty()
        data["jobs"] = [job]
        self.bench.production.write(data)
        data = self.bench.creation.empty()
        data["scripts"] = [{"id": SCRIPT, "version": 1, "product": "051A", "status": "adopted", "video_id": "",
                            "published_on": "", "case_id": "", "history": []}]
        self.bench.creation.write(data)

    def body(self, **kwargs):
        detail = self.bench.video_publication_detail(JOB)
        return {"revision": detail["revision"], "source_signature": detail["source_signature"],
                "request_id": uuid4().hex, "post_id": POST, "published_on": "2026-10-01", "account": "fixture",
                "file_path": str(self.video), "file_confirmed": True, **kwargs}

    def save(self, body=None):
        return self.bench.save_video_publication(JOB, body or self.body())

    def edit_job(self, callback):
        data = self.bench.production.read()
        callback(data["jobs"][0])
        self.bench.production.write(data)

    def error(self, code, method, *args):
        with self.assertRaises(VoiceError) as caught:
            method(*args)
        self.assertEqual(code, caught.exception.status)

    def test_registration_copies_exact_bytes_and_freezes_script_take_review(self):
        result = self.save()
        record = result["publication"]
        path = self.bench.video_publication_file(JOB, record["id"])
        self.assertNotEqual(path, self.video)
        self.assertEqual(path.read_bytes(), VIDEO)
        self.assertEqual(record["file"]["sha256"], hashlib.sha256(VIDEO).hexdigest())
        self.assertEqual(record["source"]["take"]["id"], TAKE)
        self.assertEqual(record["source"]["manual_completion"]["basis"], "human_review")
        self.assertEqual(record["source"]["script"]["lines"][0]["spoken"], "Try this.")
        self.assertEqual(self.bench.creation.get_script(SCRIPT)["video_id"], POST)
        self.assertTrue(self.bench.video_publication_detail(JOB)["publication"]["source_current"])
        self.video.unlink()
        self.assertEqual(self.bench.video_publication_file(JOB, record["id"]).read_bytes(), VIDEO)

    def test_replay_after_restart_does_not_copy_or_create_duplicate_history(self):
        body = self.body()
        saved = self.save(body)
        self.video.unlink()
        self.bench = Bench(self.root)
        with patch.object(VideoPublicationStore, "capture", side_effect=AssertionError("must replay")):
            self.assertEqual(saved, self.save(body))
        self.assertEqual([], self.bench.video_publication_detail(JOB)["history"])
        self.error(409, self.save, {**body, "account": "different"})

    def test_update_retains_prior_full_snapshot_and_detects_stale_revision(self):
        stale = self.body()
        first = self.save()["publication"]
        self.error(409, self.save, stale)
        self.video.write_bytes(VIDEO + b"new")
        second = self.save(self.body(post_id=OTHER))["publication"]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.bench.video_publication_detail(JOB)["history"], [first])
        self.assertEqual(self.bench.video_publication_file(JOB, first["id"]).read_bytes(), VIDEO)

    def test_unreviewed_current_timeline_never_uses_old_approval(self):
        self.edit_job(lambda job: job["timelines"].append({"unique_id": "timeline-2", "take_id": TAKE, "saved": True}))
        detail = self.bench.video_publication_detail(JOB)
        self.assertFalse(detail["can_register"])
        self.error(409, self.save)

    def test_timeline_management_keeps_existing_publication_current(self):
        record = self.save()["publication"]
        self.edit_job(lambda job: job["timelines"][0].update(archived=False, task_status="completed"))
        detail = self.bench.video_publication_detail(JOB)
        self.assertTrue(detail["publication"]["source_current"])
        self.assertEqual(detail["source_signature"], record["source_signature"])
        self.assertEqual(detail["source"], record["source"])
        self.assertEqual(reviewed_source(public_job(self.bench.production.job(JOB))), record["source"])

    def test_restoring_same_timeline_restores_publication_version_match(self):
        record = self.save()["publication"]
        self.edit_job(lambda job: job["timelines"][0].update(archived=True, task_status="completed"))
        detail = self.bench.video_publication_detail(JOB)
        self.assertFalse(detail["can_register"])
        self.assertFalse(detail["publication"]["source_current"])
        self.assertEqual(detail["publication"]["source"], record["source"])
        self.edit_job(lambda job: job["timelines"][0].update(archived=False))
        detail = self.bench.video_publication_detail(JOB)
        self.assertTrue(detail["publication"]["source_current"])
        self.assertEqual(detail["source_signature"], record["source_signature"])

    def test_timeline_content_change_still_invalidates_publication_version(self):
        self.save()
        self.edit_job(lambda job: job["timelines"][0].update(name="Another reviewed version", archived=False, task_status="completed"))
        self.assertFalse(self.bench.video_publication_detail(JOB)["publication"]["source_current"])

    def test_changed_selected_take_and_revoked_review_keep_historical_publication(self):
        record = self.save()["publication"]
        self.edit_job(lambda job: job.update(selected_take="TAK-222222222222"))
        detail = self.bench.video_publication_detail(JOB)
        self.assertFalse(detail["can_register"])
        self.assertFalse(detail["publication"]["source_current"])
        self.assertEqual(detail["publication"]["source"], record["source"])

    def test_live_edit_or_archived_order_cannot_register(self):
        for change in ({"archived": True}, {"edits": [{"id": "EDT-111111111111", "status": "running"}]}):
            with self.subTest(change=change):
                self.edit_job(lambda job: job.update(archived=False, edits=[]))
                self.edit_job(lambda job: job.update(change))
                self.error(409, self.save)

    def test_skill_delivery_review_records_edit_identity(self):
        self.edit_job(lambda job: (job.pop("manual_completion"), job["edits"].append({
            "id": "EDT-111111111111", "timeline_uid": "timeline-1", "status": "completed", "execution_status": "completed",
            "completion_policy": "skill_delivery", "report": {"saved": True, "gaps": []}, "approval": {"at": "2026-10-01"}})))
        self.assertEqual(self.save()["publication"]["source"]["edit"]["id"], "EDT-111111111111")
        self.edit_job(lambda job: job["edits"][0]["report"].update(gaps=[{"need": "proof"}]))
        self.error(409, self.save)

    def test_reviewed_manual_gap_fill_keeps_original_report_in_publication(self):
        old_gaps = [{"start": 0, "end": 30, "need": "original missing footage"}]
        self.edit_job(lambda job: (job.pop("manual_completion"), job["edits"].append({
            "id": "EDT-111111111111", "timeline_uid": "timeline-1", "status": "awaiting_review",
            "execution_status": "completed", "completion_policy": "skill_delivery",
            "report": {"saved": True, "gaps": old_gaps},
            "validation": {"scope": "current_coverage", "status": "awaiting_review", "saved": True},
            "summary": {"picture_gaps": [], "resolved_gaps": old_gaps}, "approval": {"at": "2026-10-01"}})))
        source = self.save()["publication"]["source"]["edit"]
        self.assertEqual(source["report"]["gaps"], old_gaps)
        self.assertEqual(source["validation"]["scope"], "current_coverage")
        self.edit_job(lambda job: job["edits"][0]["summary"].update(picture_gaps=[{"start": 5, "end": 10}]))
        self.error(409, self.save)

    def test_post_fields_and_explicit_file_confirmation_are_validated(self):
        for value in ({"post_id": "bad"}, {"published_on": "2026-02-30"},
                      {"file_confirmed": False},
                      {"post_url": f"https://www.tiktok.com/@fixture/video/{OTHER}"}, {"post_url": "javascript:alert(1)"}):
            with self.subTest(value=value):
                self.error(400, self.save, self.body(**value))

    def test_video_id_alone_registers_without_account_or_file(self):
        result = self.save(self.body(account="", file_path="", file_confirmed=False))
        record = result["publication"]
        self.assertIsNone(record["file"])
        self.assertEqual(record["post_id"], POST)
        self.assertEqual(self.bench.creation.get_script(SCRIPT)["video_id"], POST)
        detail = self.bench.video_publication_detail(JOB)
        self.assertFalse(detail["publication"]["file_available"])
        self.error(409, self.bench.verify_video_publication, JOB, {"publication_id": record["id"]})
        self.error(404, self.bench.video_publication_file, JOB, record["id"])
        self.bench._video_publications().validate(check_files=True)

    def test_calendar_mark_defaults_to_not_uploaded_and_rejects_non_booleans(self):
        record = self.save(self.body(file_path="", file_confirmed=False))["publication"]
        self.assertIs(record["calendar_uploaded"], False)
        for value in ("1", 1, None):
            with self.subTest(value=value):
                self.error(400, self.save, self.body(post_id=OTHER, file_path="", file_confirmed=False, calendar_uploaded=value))

    def test_calendar_mark_can_be_set_at_registration_and_flipped_in_place(self):
        record = self.save(self.body(file_path="", file_confirmed=False, calendar_uploaded=True))["publication"]
        self.assertIs(record["calendar_uploaded"], True)
        result = self.bench.set_video_calendar_status(JOB, {"publication_id": record["id"], "calendar_uploaded": False})
        self.assertIs(result["calendar_uploaded"], False)
        detail = self.bench.video_publication_detail(JOB)
        self.assertIs(detail["publication"]["calendar_uploaded"], False)
        self.assertEqual(detail["publication"]["id"], record["id"])
        self.assertEqual(detail["history"], [])
        self.assertEqual(detail["revision"], result["revision"])
        self.assertEqual(detail["publication"]["source_signature"], record["source_signature"])
        self.bench._video_publications().validate()

    def test_calendar_mark_refuses_a_stale_registration_and_bad_input(self):
        record = self.save(self.body(file_path="", file_confirmed=False))["publication"]
        self.error(409, self.bench.set_video_calendar_status, JOB, {"publication_id": "VPU-000000000000", "calendar_uploaded": True})
        self.error(400, self.bench.set_video_calendar_status, JOB, {"publication_id": record["id"], "calendar_uploaded": "yes"})
        self.error(400, self.bench.set_video_calendar_status, JOB, {"publication_id": record["id"], "calendar_uploaded": True, "post_id": OTHER})
        self.assertIs(self.bench.video_publication_detail(JOB)["publication"]["calendar_uploaded"], False)

    def test_arbitrary_or_network_paths_and_non_video_contents_are_rejected(self):
        for name in ("relative.mp4", "\\\\server\\share\\video.mp4", "//server/share/video.mp4", str(self.root / "secret.txt")):
            with self.subTest(path=name):
                self.error(400, self.save, self.body(file_path=name))
        self.video.write_bytes(b"this is not a movie file")
        self.error(400, self.save)

    def test_source_changes_during_streaming_copy_prevent_registration(self):
        original = VideoPublicationStore.capture
        def changing(store, *args):
            result = original(store, *args)
            self.edit_job(lambda job: job["manual_completion"].update(at="2026-10-02"))
            return result
        with patch.object(VideoPublicationStore, "capture", changing):
            self.error(409, self.save)
        self.assertEqual(self.bench.creation.get_script(SCRIPT)["status"], "adopted")

    def test_failure_after_script_write_recovers_without_original_file_or_duplicate_event(self):
        body = self.body()
        with patch.object(VideoPublicationStore, "write", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.save(body)
        self.assertEqual(self.bench.creation.get_script(SCRIPT)["video_id"], POST)
        self.video.unlink()
        self.bench = Bench(self.root)
        self.save(body)
        self.assertEqual(len(self.bench.creation.get_script(SCRIPT)["history"]), 1)
        self.assertEqual(self.bench.video_publication_detail(JOB)["publication"]["post_id"], POST)

    def test_recovery_cannot_overwrite_new_script_state(self):
        body = self.body()
        with patch.object(VideoPublicationStore, "write", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.save(body)
        data = self.bench.creation.read()
        data["scripts"][0]["video_id"] = OTHER
        self.bench.creation.write(data)
        self.error(409, self.save, body)
        self.assertEqual(self.bench.creation.get_script(SCRIPT)["video_id"], OTHER)

    def test_hash_check_detects_same_length_tampering_and_prevents_download(self):
        record = self.save()["publication"]
        path = self.bench._video_publications().managed_path(record["file"])
        path.write_bytes(VIDEO[:-1] + b"X")
        result = self.bench.verify_video_publication(JOB, {"publication_id": record["id"]})
        self.assertFalse(result["matches"])
        self.error(409, self.bench.video_publication_file, JOB, record["id"])
        self.error(409, self.bench.verify_video_publication, JOB, {"publication_id": "VPU-old"})

    def test_duplicate_post_cannot_bind_second_job(self):
        self.save()
        data = self.bench.production.read()
        second = copy.deepcopy(data["jobs"][0])
        second["id"] = "PRD-222222222222"
        data["jobs"].append(second)
        self.bench.production.write(data)
        detail = self.bench.video_publication_detail(second["id"])
        self.error(409, self.bench.save_video_publication, second["id"], self.body(revision=detail["revision"], source_signature=detail["source_signature"]))

    def test_one_script_can_be_registered_again_under_another_post_and_counts_registrations(self):
        first = self.save()["publication"]
        data = self.bench.production.read()
        second_job = copy.deepcopy(data["jobs"][0])
        second_job["id"] = "PRD-222222222222"
        data["jobs"].append(second_job)
        self.bench.production.write(data)
        self.assertEqual(self.bench.video_publication_detail(second_job["id"])["script_registrations"], [first["post_id"]])
        detail = self.bench.video_publication_detail(second_job["id"])
        second = self.bench.save_video_publication(second_job["id"], self.body(
            post_id=OTHER, revision=detail["revision"], source_signature=detail["source_signature"]))["publication"]
        self.assertEqual(self.bench.video_publication_detail(JOB)["script_registrations"], [first["post_id"], OTHER])
        self.assertEqual(second["post_id"], OTHER)
        # The script keeps the link to its first post.
        self.assertEqual(self.bench.creation.get_script(SCRIPT)["video_id"], first["post_id"])

    def test_guard_refuses_parallel_registration_and_permanent_deletion(self):
        with self.bench._video_publication_job_guard(JOB):
            self.error(409, self.save)

    def test_permanent_cleanup_removes_history_requests_media_and_backups_but_keeps_original(self):
        self.save()
        self.save(self.body(post_id=OTHER))
        folders, files = self.bench.video_publication_footprint(JOB)
        self.assertTrue(folders and files)
        with self.bench._video_publication_job_guard(JOB):
            self.assertEqual([], self.bench.purge_video_publication(JOB))
        self.assertTrue(self.video.exists())
        self.assertEqual(([], []), self.bench.video_publication_footprint(JOB))
        store = self.bench._video_publications()
        self.assertEqual({}, store.read()["jobs"])
        self.assertEqual({}, store.read()["requests"])
        for backup in (store.root / "backups").glob("*.json"):
            self.assertNotIn(JOB, backup.read_text(encoding="utf-8"))
        with self.bench._video_publication_job_guard(JOB):
            self.assertEqual([], self.bench.purge_video_publication(JOB))

    def test_deleted_jobs_do_not_lock_script_even_before_cleanup(self):
        self.save()
        self.assertEqual(self.bench.video_publication_for_script(SCRIPT)["job_id"], JOB)
        data = self.bench.production.read()
        data["jobs"] = []
        self.bench.production.write(data)
        self.assertIsNone(self.bench.video_publication_for_script(SCRIPT))

    def test_restore_validation_rejects_tampered_source_and_managed_path(self):
        self.save()
        store = self.bench._video_publications()
        original = store.read()
        self.assertEqual(original, store.validate(check_files=True))
        for change in (lambda item: item["source"]["script"].update(title="changed"),
                       lambda item: item["file"].update(managed_path="../outside.mp4")):
            modified = copy.deepcopy(original)
            change(modified["jobs"][JOB]["publication"])
            store.path.write_text(json.dumps(modified), encoding="utf-8")
            self.error(500, store.validate)
        store.path.write_text(json.dumps(original), encoding="utf-8")

    def test_path_migration_does_not_resign_an_already_invalid_original_snapshot(self):
        original = self.save()["publication"]
        original["source"]["take"]["text"] = "tampered before restoration"
        self.error(409, remap_publication_document, original, copy.deepcopy(original), "2026-10-13", {})


class VideoPublicationHTTPTests(unittest.TestCase):
    def setUp(self):
        import test_server as http_fixture
        fixture = VideoPublicationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.video = fixture.video
        runtime = fixture.root / "runtime"
        runtime.mkdir()
        for name in ("creation", "production"):
            shutil.copytree(fixture.root / name, runtime / name)
        self.server = http_fixture.make_test_server(fixture.root / "skill", runtime)
        self.bench = self.server.app
        self.token = self.bench.token
        self.bench.performance.video_brief = lambda identifiers: {}
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), self.thread.join(3)))
        self.request = http_fixture.WorkbenchHTTPTests.request.__get__(self)

    body = VideoPublicationTests.body

    def test_http_registration_download_verify_and_script_guard(self):
        path = f"/api/production/jobs/{JOB}/publication"
        status, _, detail = self.request("GET", path)
        self.assertEqual(status, 200, detail)
        status, _, saved = self.request("POST", path, self.body())
        self.assertEqual(status, 200, saved)
        record = saved["publication"]
        status, _, media = self.request("GET", path + f"/{record['id']}/download")
        self.assertEqual((status, media), (200, VIDEO))
        status, _, result = self.request("POST", path + "/verify", {"publication_id": record["id"]})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["matches"])
        status, _, error = self.request("POST", f"/api/scripts/{SCRIPT}/status", {
            "revision": self.bench.creation.public()["revision"], "request_id": uuid4().hex,
            "script_status": {"video_id": OTHER, "status": "published", "published_on": "2026-10-01"}})
        self.assertEqual(status, 409, error)

    def test_http_delete_cleans_registered_media_and_replay(self):
        status, _, saved = self.request("POST", f"/api/production/jobs/{JOB}/publication", self.body())
        self.assertEqual(status, 200, saved)
        managed = self.bench._video_publications().managed_path(saved["publication"]["file"])
        body = {"request_id": uuid4().hex}
        status, _, result = self.request("POST", f"/api/production/jobs/{JOB}/delete", body)
        self.assertEqual(status, 200, result)
        self.assertFalse(managed.exists())
        self.assertEqual({}, self.bench._video_publications().read()["jobs"])
        status, _, result = self.request("POST", f"/api/production/jobs/{JOB}/delete", body)
        self.assertEqual(status, 200, result)
        self.assertEqual([], result["warnings"])

    def portable_backup(self):
        from data_backup import BackupManager
        target = self.video.parent / "restored"
        skill, runtime = target / "skill", target / "runtime"
        manager = BackupManager(self.bench.store.root, self.bench.runtime)
        archive = Path(manager.create_archive(self.video.parent / "archives")["path"])
        restored = BackupManager(skill, runtime, validator=self.bench.validate_imported_data)
        restored.restore_archive(archive, runtime / "data-transfer" / "recovery")
        return runtime

    def add_source_path(self):
        audio = self.bench.production.root / "audio" / JOB / "reviewed.wav"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"frozen-voice-bytes")
        data = self.bench.production.read()
        data["jobs"][0]["takes"][0]["file_path"] = str(audio)
        self.bench.production.write(data)

    def test_portable_backup_preserves_video_creation_followup_and_resigns_only_mapped_source(self):
        from review_followups import ReviewFollowupStore
        self.add_source_path()
        body = self.body()
        record = self.bench.save_video_publication(JOB, body)["publication"]
        video = {"product": "051A", "video_id": POST, "window_end": "2026-10-08"}
        followups = self.bench.review_followups
        followups.save({"request_id": uuid4().hex, "revision": followups.revision(followups.read()),
                        **video, "state": "reviewed", "note": "retain review conclusion"}, video)
        runtime = self.portable_backup()
        restored = Bench(runtime)
        detail = restored.video_publication_detail(JOB)
        after = detail["publication"]
        self.assertEqual(record["file"]["sha256"], after["file"]["sha256"])
        self.assertEqual(restored.video_publication_file(JOB, after["id"]).read_bytes(), VIDEO)
        self.assertTrue(after["source_current"])
        self.assertNotEqual(record["source_signature"], after["source_signature"])
        self.assertEqual(after["path_migrations"][-1]["previous_source_signature"], record["source_signature"])
        self.assertTrue(Path(after["source"]["take"]["file_path"]).is_relative_to(runtime))
        self.assertEqual(restored.creation.get_script(SCRIPT)["video_id"], POST)
        self.assertEqual(ReviewFollowupStore(runtime).read()["entries"][0]["note"], "retain review conclusion")
        self.assertEqual(restored.save_video_publication(JOB, body)["revision"], detail["revision"])
        VideoPublicationStore(runtime / "production").validate(check_files=True)

    def test_portable_backup_recovers_prepared_update_with_remapped_expected_revision(self):
        self.add_source_path()
        self.bench.save_video_publication(JOB, self.body())
        body = self.body(post_id=OTHER)
        with patch.object(VideoPublicationStore, "write", side_effect=OSError("interrupted after script write")):
            with self.assertRaises(OSError):
                self.bench.save_video_publication(JOB, body)
        runtime = self.portable_backup()
        restored = Bench(runtime)
        saved = restored.save_video_publication(JOB, body)
        self.assertEqual(saved["publication"]["post_id"], OTHER)
        self.assertEqual(len(saved["history"]), 1)
        self.assertTrue(restored.video_publication_detail(JOB)["publication"]["source_current"])
        self.assertEqual(len(restored.creation.get_script(SCRIPT)["history"]), 2)


if __name__ == "__main__":
    unittest.main()
