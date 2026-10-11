"""Fixed carousel delivery snapshots; all media comes from disposable fake jobs."""
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4
import zipfile

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from PIL import Image
from elevenlabs_client import VoiceError
import test_production_carousel_images as images_fixture
from workbench_carousel_delivery import CarouselDeliveryMixin, carousel_selection_signature


class DeliveryBench(images_fixture.ImageBench, CarouselDeliveryMixin):
    pass


class CarouselDeliveryTests(unittest.TestCase):
    def setUp(self):
        # Compose the old setup/helpers; importing its TestCase as a module avoids
        # rediscovering all image tests in this file.
        self.fixture = images_fixture.CarouselImageTests()
        with patch.object(images_fixture, "ImageBench", DeliveryBench):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bench = self.fixture.bench
        self.identifier = self.fixture.item["id"]

    def ready(self):
        self.fixture.complete_group()
        return self.fixture.approve_all()

    def create(self, body=None):
        return self.bench.create_carousel_delivery(self.identifier, body or self.fixture.body())

    def current(self):
        return self.fixture.current()

    def error(self, status, method, *args, **kwargs):
        with self.assertRaises(VoiceError) as caught:
            method(*args, **kwargs)
        self.assertEqual(caught.exception.status, status)

    def test_package_preserves_original_bytes_order_words_and_checked_manifest(self):
        item = self.ready()
        # Include JPEG and WebP without rewriting the bytes during collection.
        with self.bench.lock:
            catalog = self.bench.production.read()
            for slide, extension in zip(catalog["carousels"][0]["slides"][1:], (".jpg", ".webp")):
                output = slide["outputs"][-1]
                old = self.bench._carousel_dir(self.identifier) / output["file"]
                target = old.with_suffix(extension)
                with Image.open(old) as image:
                    image.save(target)
                output.update(file=f"outputs/{target.name}", sha256=hashlib.sha256(target.read_bytes()).hexdigest())
            self.bench.production.write(catalog)
        # Changing encoded bytes invalidates the old review, even when the picture looks the same.
        self.error(409, self.create)
        for number in (2, 3):
            output_id = self.current()["slides"][number - 1]["outputs"][-1]["id"]
            reviewed = self.fixture.recheck(number, output_id)
            self.assertEqual(reviewed["review"]["file_sha256"], reviewed["sha256"])
            self.fixture.approve(number, output_id)
        result = self.create()
        record = result["delivery"]
        self.assertRegex(record["id"], r"^DLV-[0-9a-f]{12}$")
        self.assertEqual(record["selection_signature"], carousel_selection_signature(self.current()))
        path = self.bench.carousel_delivery_file(self.identifier, record["id"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), record["sha256"])
        self.assertEqual([entry["file"] for entry in record["slides"]], ["01.png", "02.jpg", "03.webp"])
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.namelist()[:3], ["01.png", "02.jpg", "03.webp"])
            for slide, saved in zip(self.current()["slides"], record["slides"]):
                output = next(entry for entry in slide["outputs"] if entry["id"] == saved["output_id"])
                source = self.bench.carousel_output(self.identifier, output["id"], "full")
                self.assertEqual(archive.read(saved["file"]), source.read_bytes())
            self.assertEqual(archive.read("caption_en.txt").decode(), item["caption_en"])
            self.assertEqual(archive.read("caption_zh.txt").decode(), item["caption_zh"])
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["slides"], record["slides"])
            self.assertEqual(manifest["carousel_id"], self.identifier)
            self.assertIn("发布平台", archive.read("领取说明.txt").decode())

    def test_repeat_same_selection_reuses_one_complete_package_and_request_replays(self):
        self.ready()
        body = self.fixture.body()
        first = self.create(body)
        self.assertEqual(self.create(body), first)
        second = self.create()
        self.assertEqual(second["delivery"]["id"], first["delivery"]["id"])
        self.assertEqual(len(self.current()["deliveries"]), 1)
        folders = list((self.bench._carousel_dir(self.identifier) / "deliveries").iterdir())
        self.assertEqual(len(folders), 1)

    def test_redo_makes_new_package_without_mutating_old_and_old_can_be_current_again(self):
        self.ready()
        first = self.create()["delivery"]
        old_path = self.bench.carousel_delivery_file(self.identifier, first["id"])
        old_bytes = old_path.read_bytes()
        self.fixture.generate(slides=[2])
        self.fixture.approve(2, self.current()["slides"][1]["outputs"][-1]["id"])
        second = self.create()["delivery"]
        self.assertNotEqual(second["id"], first["id"])
        live = self.bench.carousel_delivery_live(self.current())
        self.assertEqual([(entry["current"], entry["stale"]) for entry in live["deliveries"]], [(False, True), (True, False)])
        self.assertEqual(old_path.read_bytes(), old_bytes)
        self.fixture.approve(2, first["slides"][1]["output_id"])
        self.assertEqual(self.create()["delivery"]["id"], first["id"])
        self.assertEqual(len(self.current()["deliveries"]), 2)
        self.assertEqual(self.current()["deliveries"][0], first)

    def test_stale_revision_and_invalid_request_leave_no_new_package_files(self):
        self.ready()
        body = self.fixture.body()
        self.fixture.change_notes(1)
        self.error(409, self.create, body)
        self.error(400, self.create, {**self.fixture.body(), "request_id": "short"})
        self.assertFalse((self.bench._carousel_dir(self.identifier) / "deliveries").exists())

    def test_unconfirmed_stale_changed_missing_archived_and_running_groups_are_rejected(self):
        self.error(409, self.create)
        self.fixture.generate()
        self.fixture.approve(1)
        self.error(409, self.create)
        self.fixture.generate(slides=[2, 3])
        self.fixture.approve_all()
        self.bench._image_live = {"IMG-ffffffffffff": {"carousel_id": self.identifier}}
        self.error(409, self.create)
        self.bench._image_live.clear()
        self.fixture.change_notes(1)
        self.error(409, self.create)
        self.fixture.generate(slides=[1])
        self.fixture.approve(1, self.current()["slides"][0]["outputs"][-1]["id"])
        output = self.current()["slides"][0]["outputs"][-1]
        path = self.bench.carousel_output(self.identifier, output["id"], "full")
        path.write_bytes(b"changed media")
        self.error(409, self.create)
        path.unlink()
        self.error(409, self.create)
        self.bench.archive_carousel(self.identifier, self.fixture.body(archived=True))
        self.error(409, self.create)
        self.assertFalse(self.current().get("deliveries"))

    def test_revoked_approval_and_changed_caption_make_fixed_package_stale(self):
        self.ready()
        record = self.create()["delivery"]
        output_id = record["slides"][0]["output_id"]
        self.bench.approve_carousel_output(self.identifier, self.fixture.body(slide=1, output_id=output_id, approved=False))
        self.assertTrue(self.bench.carousel_delivery_live(self.current())["deliveries"][0]["stale"])
        self.fixture.approve(1)
        self.assertTrue(self.bench.carousel_delivery_live(self.current())["deliveries"][0]["current"])
        with self.bench.lock:
            data = self.bench.production.read()
            data["carousels"][0]["caption_en"] += " Added caption."
            self.bench.production.write(data)
        self.assertTrue(self.bench.carousel_delivery_live(self.current())["deliveries"][0]["stale"])
        self.assertTrue(self.bench.carousel_delivery_file(self.identifier, record["id"]).is_file())
        second = self.create()["delivery"]
        self.assertNotEqual(second["id"], record["id"])

    def test_missing_or_changed_package_can_be_rebuilt_without_overwriting_history(self):
        self.ready()
        first = self.create()["delivery"]
        first_path = self.bench.carousel_delivery_file(self.identifier, first["id"])
        first_path.write_bytes(b"corrupt zip")
        self.error(409, self.bench.carousel_delivery_file, self.identifier, first["id"])
        second = self.create()["delivery"]
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(first_path.read_bytes(), b"corrupt zip")
        second_path = self.bench.carousel_delivery_file(self.identifier, second["id"])
        second_path.unlink()
        self.error(404, self.bench.carousel_delivery_file, self.identifier, second["id"])
        third = self.create()["delivery"]
        self.assertEqual(len({first["id"], second["id"], third["id"]}), 3)
        self.assertEqual(len(self.current()["deliveries"]), 3)

    def test_download_belongs_to_carousel_and_rejects_tampered_path(self):
        self.ready()
        record = self.create()["delivery"]
        other = self.fixture.carousel()
        self.error(404, self.bench.carousel_delivery_file, other["id"], record["id"])
        self.error(404, self.bench.carousel_delivery_file, self.identifier, "DLV-ffffffffffff")
        with self.bench.lock:
            data = self.bench.production.read()
            data["carousels"][0]["deliveries"][0]["zip_file"] = "../../catalog.json"
            self.bench.production.write(data)
        self.error(404, self.bench.carousel_delivery_file, self.identifier, record["id"])

    def test_filename_is_safe_for_unicode_and_windows_and_zip_members_ignore_title(self):
        self.ready()
        with self.bench.lock:
            data = self.bench.production.read()
            data["carousels"][0]["title"] = 'CON ../产品:标题*?\n unsafe'
            self.bench.production.write(data)
        record = self.create()["delivery"]
        self.assertNotRegex(record["filename"], r'[<>:"/\\|?*\x00-\x1f]')
        self.assertIn("产品", record["filename"])
        with zipfile.ZipFile(self.bench.carousel_delivery_file(self.identifier, record["id"])) as archive:
            expected = {"caption_en.txt", "caption_zh.txt", "manifest.json", "领取说明.txt"} | {slide["file"] for slide in record["slides"]} | {
                entry["file"] for entry in record.get("attachments", [])}
            self.assertEqual(set(archive.namelist()), expected)
            self.assertFalse(any(PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts or "\\" in name
                                 for name in archive.namelist()))

    def test_package_write_failure_keeps_originals_prior_zip_and_history(self):
        self.ready()
        original = self.create()["delivery"]
        original_path = self.bench.carousel_delivery_file(self.identifier, original["id"])
        original_bytes = original_path.read_bytes()
        self.fixture.generate(slides=[1])
        self.fixture.approve(1, self.current()["slides"][0]["outputs"][-1]["id"])
        before = self.bench.production.path.read_bytes()
        with patch("workbench_carousel_delivery.zipfile.ZipFile", side_effect=OSError("disk full private/path")):
            self.error(500, self.create)
        self.assertEqual(self.bench.production.path.read_bytes(), before)
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertEqual(self.current()["deliveries"], [original])
        for path, data in self.fixture.originals.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertNotEqual(self.create()["delivery"]["id"], original["id"])

    def test_identical_concurrent_requests_replay_one_package(self):
        self.ready()
        body = self.fixture.body()
        barrier = threading.Barrier(3)
        results, errors = [], []
        def create():
            barrier.wait()
            try:
                results.append(self.create(body))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.current()["deliveries"]), 1)
        self.assertEqual(len(list((self.bench._carousel_dir(self.identifier) / "deliveries").iterdir())), 1)

    def test_concurrent_same_selection_creates_one_snapshot(self):
        self.ready()
        bodies = [self.fixture.body(), self.fixture.body()]
        barrier = threading.Barrier(3)
        results, errors = [], []
        def create(body):
            barrier.wait()
            try:
                results.append(self.create(body))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=create, args=(body,)) for body in bodies]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        # Different requests share a revision: the later request must refresh,
        # while an identical request is replayable without creating a second ZIP.
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], VoiceError)
        self.assertEqual(errors[0].status, 409)
        self.assertEqual(len(self.current()["deliveries"]), 1)

    def test_collection_list_includes_approved_or_historical_carousels_and_verified_video_only(self):
        self.assertEqual(self.bench.production_deliveries()["items"], [])
        self.ready()
        row = self.bench.production_deliveries()["items"][0]
        self.assertEqual((row["kind"], row["id"], row["published"]), ("carousel", self.identifier, False))
        self.assertNotIn("delivery_id", row)
        record = self.create()["delivery"]
        self.fixture.change_notes(1)
        with self.bench.lock:
            data = self.bench.production.read()
            data["carousels"][0]["publication"] = {"delivery_id": record["id"], "post_id": "123", "published_on": "2026-10-11"}
            for number, state in enumerate(("awaiting_review", "running", "failed", "awaiting_review"), 1):
                uid = f"video-{number}"
                data["jobs"].append({"id": "PRD-" + str(number) * 12, "product": "202H", "title": f"Video {number}",
                    "direction_id": "202H-S2", "updated_at": "2026-10-11T10:00:00", "archived": number == 4,
                    "selected_take": "TAKE-current", "timelines": [{"unique_id": uid, "name": f"Lab-{number}", "take_id": "TAKE-current"}],
                    "edits": [{"id": "EDT-" + str(number) * 12, "timeline_uid": uid, "timeline_name": f"Lab-{number}", "status": state,
                               "validation": {"status": state, "issues": [], "checked_at": "2026-10-11"}}]})
            data["jobs"].append({"id": "PRD-555555555555", "product": "202H", "title": "Missing timeline", "timelines": [],
                                 "edits": [{"status": "completed", "timeline_uid": "missing", "timeline_name": "Missing"}]})
            self.bench.production.write(data)
        rows = self.bench.production_deliveries("202H")["items"]
        self.assertEqual({(entry["kind"], entry["id"]) for entry in rows}, {("carousel", self.identifier), ("video", "PRD-111111111111")})
        carousel = next(entry for entry in rows if entry["kind"] == "carousel")
        self.assertEqual((carousel["delivery_id"], carousel["published"]), (record["id"], True))
        video = next(entry for entry in rows if entry["kind"] == "video")
        self.assertEqual(video["timeline_name"], "Lab-1")
        self.assertEqual((video["status"], video["timeline_uid"]), ("awaiting_review", "video-1"))
        self.assertNotIn("zip_file", video)
        self.assertEqual(self.bench.production_deliveries("999X")["items"], [])
        self.bench.archive_carousel(self.identifier, self.fixture.body(archived=True))
        self.assertEqual([entry["kind"] for entry in self.bench.production_deliveries()["items"]], ["video"])

    def video_job(self, states=("awaiting_review",), *, selected="TAKE-current", validation=True):
        job = {"id": "PRD-999999999999", "product": "202H", "title": "待审查视频", "selected_take": selected,
               "timelines": [{"unique_id": "video-current", "name": "Lab-current", "take_id": "TAKE-current"}],
               "edits": [{"id": f"EDT-{index:012d}", "status": state, "timeline_uid": "video-current",
                          "timeline_name": "Lab-current", **({"validation": {"status": state, "issues": []}} if validation else {})}
                         for index, state in enumerate(states)]}
        with self.bench.lock:
            data = self.bench.production.read()
            data["jobs"] = [job]
            self.bench.production.write(data)
        return job

    def test_later_failed_or_running_repair_revokes_old_verified_result_for_same_timeline(self):
        for state in ("running", "failed", "needs_repair", "needs_material", "verification_failed", "cancelled", "interrupted"):
            with self.subTest(state=state):
                self.video_job(("awaiting_review", state))
                self.assertEqual(self.bench.production_deliveries()["items"], [])
        self.video_job(("failed", "awaiting_review"))
        self.assertEqual(len(self.bench.production_deliveries()["items"]), 1)

    def test_legacy_completed_and_missing_or_failed_validation_do_not_enter_overview(self):
        for state, validated in (("completed", False), ("awaiting_review", False), ("verification_failed", True), ("unverified", True)):
            with self.subTest(state=state, validated=validated):
                self.video_job((state,), validation=validated)
                self.assertEqual(self.bench.production_deliveries()["items"], [])
        self.video_job()
        with self.bench.lock:
            data = self.bench.production.read()
            data["jobs"][0]["edits"][0]["validation"]["status"] = "needs_repair"
            self.bench.production.write(data)
        self.assertEqual(self.bench.production_deliveries()["items"], [])

    def test_selected_take_must_match_verified_timeline(self):
        self.video_job(selected="TAKE-new")
        self.assertEqual(self.bench.production_deliveries()["items"], [])
        self.video_job(selected="")
        self.assertEqual(self.bench.production_deliveries()["items"], [])
        self.video_job()
        self.assertEqual(len(self.bench.production_deliveries()["items"]), 1)


class CarouselDeliveryHTTPTests(unittest.TestCase):
    request = images_fixture.CarouselImageHTTPTests.request
    body = images_fixture.CarouselImageTests.body
    carousel = images_fixture.CarouselImageTests.carousel
    current = images_fixture.CarouselImageTests.current
    pick_new_base = images_fixture.CarouselImageTests.pick_new_base
    wait_worker = images_fixture.CarouselImageHTTPTests.wait_worker
    stop_server = images_fixture.CarouselImageHTTPTests.stop_server

    def setUp(self):
        images_fixture.CarouselImageHTTPTests.setUp(self)
        for number in (2, 3):
            self.pick_new_base(number)
        self.bench.start_image_generation(self.item["id"], self.body(slides=[1]))
        self.wait_worker()
        first = self.current()["slides"][0]
        self.bench.approve_carousel_output(self.item["id"], self.body(slide=1, output_id=first["output_id"], approved=True))
        self.bench.start_image_generation(self.item["id"], self.body(slides=[2, 3]))
        self.wait_worker()
        for slide in self.current()["slides"]:
            self.bench.approve_carousel_output(self.item["id"], self.body(slide=slide["number"], output_id=slide["output_id"], approved=True))
        self.delivery = self.bench.create_carousel_delivery(self.item["id"], self.body())["delivery"]
        self.download = self.prefix + f"/deliveries/{self.delivery['id']}/download"

    def test_actual_download_and_range_return_checked_zip_and_safe_filename(self):
        path = self.bench.carousel_delivery_file(self.item["id"], self.delivery["id"])
        status, headers, payload = self.request("GET", self.download)
        self.assertEqual(status, 200)
        self.assertEqual(payload, path.read_bytes())
        self.assertTrue(headers["Content-Disposition"].startswith("attachment; filename*=UTF-8''"))
        self.assertIn(headers["Content-Type"], {"application/zip", "application/x-zip-compressed"})
        status, headers, payload = self.request("GET", self.download, headers={"Range": "bytes=0-31"})
        self.assertEqual(status, 206)
        self.assertEqual(payload, path.read_bytes()[:32])
        self.assertEqual(headers["Content-Range"], f"bytes 0-31/{path.stat().st_size}")
        self.assertEqual(self.request("GET", self.download, headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        path.write_bytes(b"changed package")
        self.assertEqual(self.request("GET", self.download)[0], 409)
        path.unlink()
        self.assertEqual(self.request("GET", self.download)[0], 404)

    def test_actual_list_and_detail_show_fixed_package_current_without_catalog_writes(self):
        before = self.bench.production.path.read_bytes()
        status, _, result = self.request("GET", "/api/production/deliveries?product=202H")
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["delivery_id"], self.delivery["id"])
        status, _, detail = self.request("GET", self.prefix)
        self.assertEqual(status, 200)
        self.assertTrue(detail["carousel"]["deliveries"][0]["current"])
        self.assertFalse(detail["carousel"]["deliveries"][0]["stale"])
        self.assertEqual(self.bench.production.path.read_bytes(), before)
        foreign = self.download.replace(self.item["id"], "CAR-ffffffffffff")
        self.assertEqual(self.request("GET", foreign)[0], 404)


if __name__ == "__main__":
    unittest.main()
