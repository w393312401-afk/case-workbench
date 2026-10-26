"""Local carousel publication records and two-store recovery, with isolated data."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from creation_store import CreationError
from elevenlabs_client import VoiceError
from sales_data import SalesDataResolver
from test_ai_tasks import FakeBridge
import test_production_carousel_images as images_fixture
import test_server as http_fixture


POST = "7999999999999999901"
OTHER_POST = "7999999999999999902"
DAY = "2026-10-11"


class Performance:
    """The real sales resolver reads only the exact IDs this source contains."""

    def __init__(self, rows=None):
        self.rows = rows or {}
        self.calls = []

    def video_brief(self, identifiers):
        wanted = set(identifiers)
        self.calls.append(wanted)
        return {identifier: copy.deepcopy(row) for identifier, row in self.rows.items() if identifier in wanted}


class CarouselPublishTests(unittest.TestCase):
    request = http_fixture.WorkbenchHTTPTests.request
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
        self.performance = Performance()
        self.bench.performance = self.performance
        self.bench.sales = SalesDataResolver(self.performance)
        self.delivery = self.complete_delivery()

    def complete_delivery(self):
        first_run = self.bench.start_image_generation(self.item["id"], self.body(slides=[1]))["generation"]["id"]
        first_item = self.wait_worker()
        first = first_item["slides"][0]
        first_output = next(output for output in reversed(first["outputs"]) if output.get("generation_id") == first_run)
        self.assertEqual(first_output["review"]["status"], "pass")
        self.bench.approve_carousel_output(self.item["id"], self.body(slide=1, output_id=first_output["id"], approved=True))
        self.assertTrue(self.bench.carousel_first_preflight(self.item["id"])["ready"])
        rest_run = self.bench.start_image_generation(self.item["id"], self.body(slides=[2, 3]))["generation"]["id"]
        completed = self.wait_worker()
        self.assertEqual(next(run for run in completed["generations"] if run["id"] == rest_run)["status"], "completed")
        for slide in completed["slides"][1:]:
            output = next(output for output in reversed(slide["outputs"]) if output.get("generation_id") == rest_run)
            self.assertEqual(output["review"]["status"], "pass")
            self.bench.approve_carousel_output(self.item["id"], self.body(slide=slide["number"],
                output_id=output["id"], approved=True))
        return self.bench.create_carousel_delivery(self.item["id"], self.body())["delivery"]

    def publication_body(self, **values):
        value = {"delivery_id": self.delivery["id"], "post_id": POST, "published_on": DAY,
                 "account": "fixture", "note": "本地登记", **values}
        value.setdefault("post_url", f"https://www.tiktok.com/@fixture/photo/{value['post_id']}")
        return self.body(**value)

    def publish(self, body=None, identifier=None):
        return self.bench.save_carousel_publication(identifier or self.item["id"], body or self.publication_body())

    def script(self):
        return self.bench.creation.get_script(self.item["script_id"])

    def stored_bytes(self):
        return self.bench.production.path.read_bytes(), self.bench.creation.path.read_bytes()

    def assert_error(self, status, function, *args, **kwargs):
        with self.assertRaises((CreationError, VoiceError)) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.status, status)

    def another_carousel(self, *, same_script=False):
        saved_item = self.item
        if same_script:
            # This fixture exercises generated-image publication; new real tasks default to local lettering.
            other = self.bench.create_carousel(self.body(carousel={"script_id": saved_item["script_id"]},
                requirements={"mode": "photo_preserving"}))["carousel"]
        else:
            other = self.carousel()
        self.item = other
        try:
            for number in (1, 2, 3):
                self.pick_new_base(number)
            delivery = self.complete_delivery()
        finally:
            self.item = saved_item
        return other, delivery

    def reload_application(self):
        previous = self.bench
        previous.ai.shutdown()
        with patch.object(http_fixture.SERVER, "CockpitImageBridge", return_value=self.generator):
            self.bench = http_fixture.SERVER.Workbench(previous.store.root, previous.runtime,
                allow_open=False, ai_bridge=FakeBridge(), performance_sources=[])
        self.bench.image_reviewer = self.generator
        self.bench.carousel_image_root = previous.carousel_image_root
        self.bench.performance = self.performance
        self.bench.sales = SalesDataResolver(self.performance)
        self.server.app = self.bench
        self.token = self.bench.token

    def test_published_source_stays_frozen_after_new_package_replaces_old_zip_and_registration_is_edited(self):
        old_delivery = copy.deepcopy(self.delivery)
        old_path = self.bench.carousel_delivery_file(self.item["id"], old_delivery["id"])
        script_before = self.script()
        initial = self.publish()["publication"]
        frozen = copy.deepcopy(initial["delivery_snapshot"])
        self.bench.start_image_generation(self.item["id"], self.body(slides=[1]))
        self.wait_worker()
        slide = self.current()["slides"][0]
        self.bench.approve_carousel_output(self.item["id"], self.body(slide=1, output_id=slide["outputs"][-1]["id"], approved=True))
        new_delivery = self.bench.create_carousel_delivery(self.item["id"], self.body())["delivery"]
        self.assertNotEqual(new_delivery["slides"][0]["output_id"], old_delivery["slides"][0]["output_id"])
        self.pick_new_base(1)
        result = self.publish(self.publication_body(note="纠正原发布备注，保持实际发布来源"))
        publication = result["publication"]
        self.assertEqual((publication["delivery_id"], publication["post_id"], publication["published_on"]),
                         (old_delivery["id"], POST, DAY))
        self.assertEqual(publication["delivery_sha256"], old_delivery["sha256"])
        self.assertEqual(publication["delivery_snapshot"], frozen)
        self.assertEqual(publication["delivery_snapshot"]["slides"][0]["output_id"], old_delivery["slides"][0]["output_id"])
        self.assertNotEqual(publication["delivery_id"], new_delivery["id"])
        self.assertEqual(publication["note"], "纠正原发布备注，保持实际发布来源")
        current = self.current()
        self.assertEqual(current["publication"], publication)
        self.assertEqual([entry["id"] for entry in current["deliveries"]], [new_delivery["id"]])
        self.assertIn(initial, current["publication_history"])
        self.assertFalse(old_path.exists())
        self.assert_error(404, self.bench.carousel_delivery_file, self.item["id"], old_delivery["id"])
        script = self.script()
        self.assertEqual((script["status"], script["video_id"], script["published_on"]), ("published", POST, DAY))
        self.assertEqual((script["text"], script["translation_zh"], script["version"]),
                         (script_before["text"], script_before["translation_zh"], script_before["version"]))

    def test_retired_unpublished_package_cannot_be_registered_or_rebound_to_the_newest_one(self):
        old_delivery = copy.deepcopy(self.delivery)
        self.bench.start_image_generation(self.item["id"], self.body(slides=[1]))
        self.wait_worker()
        output = self.current()["slides"][0]["outputs"][-1]
        self.bench.approve_carousel_output(self.item["id"], self.body(slide=1, output_id=output["id"], approved=True))
        new_delivery = self.bench.create_carousel_delivery(self.item["id"], self.body())["delivery"]
        before = self.stored_bytes()
        self.assert_error(404, self.publish, self.publication_body(delivery_id=old_delivery["id"]))
        self.assertEqual(self.stored_bytes(), before)
        self.assertFalse(self.current().get("publication"))
        self.assertEqual(self.script()["status"], "adopted")
        self.assertEqual([entry["id"] for entry in self.current()["deliveries"]], [new_delivery["id"]])

    def test_calendar_mark_defaults_to_not_uploaded_and_must_be_a_boolean(self):
        before = self.stored_bytes()
        for value in ("1", 1, None):
            with self.subTest(value=value):
                self.assert_error(400, self.publish, self.publication_body(calendar_uploaded=value))
        self.assertEqual(self.stored_bytes(), before)
        self.assertIs(self.publish()["publication"]["calendar_uploaded"], False)
        second = self.publish(self.publication_body(post_id=OTHER_POST, calendar_uploaded=True))["publication"]
        self.assertIs(second["calendar_uploaded"], True)

    def test_calendar_mark_flips_in_place_without_a_new_registration(self):
        publication = self.publish()["publication"]
        history = copy.deepcopy(self.current()["publication_history"])
        result = self.bench.set_carousel_calendar_status(self.item["id"], {
            "request_id": "calendar-" + uuid4().hex, "publication_id": publication["id"], "calendar_uploaded": True})
        self.assertIs(result["calendar_uploaded"], True)
        item = self.current()
        self.assertIs(item["publication"]["calendar_uploaded"], True)
        self.assertEqual(item["publication"]["id"], publication["id"])
        self.assertEqual(len(item["publication_history"]), len(history))
        self.assertIs(item["publication_history"][-1]["calendar_uploaded"], True)
        self.assertEqual(item["publication"]["delivery_id"], publication["delivery_id"])
        self.assertEqual((self.script()["video_id"], self.script()["status"]), (POST, "published"))
        self.assert_error(409, self.bench.set_carousel_calendar_status, self.item["id"], {
            "request_id": "calendar-" + uuid4().hex, "publication_id": "PUB-000000000000", "calendar_uploaded": False})
        self.assert_error(400, self.bench.set_carousel_calendar_status, self.item["id"], {
            "request_id": "calendar-" + uuid4().hex, "publication_id": publication["id"], "calendar_uploaded": "no"})
        self.assert_error(400, self.bench.set_carousel_calendar_status, self.item["id"], {
            "request_id": "calendar-" + uuid4().hex, "publication_id": publication["id"], "calendar_uploaded": True, "post_id": OTHER_POST})

    def test_correcting_same_carousel_keeps_prior_publication_and_delivery_history(self):
        first = self.publish()["publication"]
        before_deliveries = copy.deepcopy(self.current()["deliveries"])
        second_body = self.publication_body(post_id=OTHER_POST, published_on="2026-10-12", note="纠正帖子 ID")
        second = self.publish(second_body)["publication"]
        self.assertEqual(second["post_id"], OTHER_POST)
        item = self.current()
        self.assertIn(first, item["publication_history"])
        self.assertEqual(item["publication_history"], [first, second])
        self.assertEqual(item["deliveries"], before_deliveries)
        self.assertEqual((self.script()["video_id"], self.script()["published_on"]), (OTHER_POST, "2026-10-12"))

    def test_completed_request_replays_durably_without_duplicate_history(self):
        body = self.publication_body()
        first = self.publish(body)
        before = self.stored_bytes()
        script_history = copy.deepcopy(self.script()["history"])
        publication_history = copy.deepcopy(self.current()["publication_history"])
        self.reload_application()
        replay = self.publish(body)
        self.assertEqual(replay["publication"], first["publication"])
        self.assertEqual(self.stored_bytes(), before)
        self.assertEqual(self.script()["history"], script_history)
        self.assertEqual(self.current()["publication_history"], publication_history)
        self.assert_error(409, self.publish, {**body, "post_id": OTHER_POST})

    def test_invalid_identity_date_and_required_delivery_do_not_change_either_catalog(self):
        before = self.stored_bytes()
        invalid = ({"post_id": "12345"}, {"post_id": "1" * 26}, {"post_id": "not-a-post"},
                   {"post_id": 7999999999999999901}, {"published_on": ""}, {"published_on": "2026-02-30"},
                   {"published_on": "20261011"}, {"post_url": f"https://www.tiktok.com/@fixture/photo/{OTHER_POST}"})
        for values in invalid:
            with self.subTest(values=values):
                self.assert_error(400, self.publish, {**self.publication_body(), **values})
                self.assertEqual(self.stored_bytes(), before)
        self.assert_error(404, self.publish, self.publication_body(delivery_id=""))
        self.assertEqual(self.stored_bytes(), before)

    def test_production_revision_conflict_is_rejected_before_script_status_changes(self):
        body = self.publication_body()
        self.bench.choose_base(self.item["id"], self.body(slide=1, candidate=self.current()["slides"][0]["base"]["id"]))
        before = self.stored_bytes()
        self.assert_error(409, self.publish, body)
        self.assertEqual(self.stored_bytes(), before)
        self.assertEqual(self.script()["status"], "adopted")

    def test_foreign_delivery_id_cannot_bind_another_carousel_package(self):
        other, other_delivery = self.another_carousel()
        before = self.stored_bytes()
        self.assert_error(404, self.publish, {**self.publication_body(), "delivery_id": other_delivery["id"]})
        self.assertEqual(self.stored_bytes(), before)
        self.assertFalse(self.bench.production.carousel(other["id"]).get("publication"))

    def test_missing_or_changed_delivery_package_cannot_be_registered_as_actual_publication(self):
        package = self.bench.carousel_delivery_file(self.item["id"], self.delivery["id"])
        original = package.read_bytes()
        for mode, status in (("missing", 404), ("changed", 409)):
            with self.subTest(mode=mode):
                if mode == "missing":
                    package.unlink()
                else:
                    package.write_bytes(original + b"changed")
                before = self.stored_bytes()
                try:
                    self.assert_error(status, self.publish)
                    self.assertEqual(self.stored_bytes(), before)
                    self.assertEqual(self.script()["status"], "adopted")
                    self.assertFalse(self.current().get("publication"))
                finally:
                    package.write_bytes(original)

    def test_cross_product_post_is_rejected_by_existing_script_guard(self):
        self.performance.rows[POST] = {"product": "103N", "title": "Another product"}
        before = self.stored_bytes()
        self.assert_error(409, self.publish)
        self.assertEqual(self.stored_bytes(), before)

    def test_post_already_registered_to_another_script_is_rejected(self):
        other, delivery = self.another_carousel()
        self.publish()
        before = self.stored_bytes()
        self.assert_error(409, self.publish, {**self.publication_body(), "delivery_id": delivery["id"]}, other["id"])
        self.assertEqual(self.stored_bytes(), before)
        self.assertFalse(self.bench.production.carousel(other["id"]).get("publication"))

    def test_same_script_in_two_carousels_can_register_two_posts_and_keeps_the_first_link(self):
        other, delivery = self.another_carousel(same_script=True)
        self.publish()
        saved = self.publish(self.publication_body(delivery_id=delivery["id"], post_id=OTHER_POST), other["id"])
        self.assertEqual(saved["publication"]["post_id"], OTHER_POST)
        self.assertEqual(self.script()["video_id"], POST)
        self.assertEqual(self.bench.script_registrations(self.item["script_id"]), [POST, OTHER_POST])

    def test_existing_case_product_and_direction_guards_are_preserved(self):
        video = self.bench.runtime / "case-fixture.mp4"
        video.write_bytes(b"temporary case video")
        current = http_fixture.snapshot(video)
        current["metrics"]["video_id"] = POST
        case = self.bench.store.add({"title": "其他产品案例", "product": "103N", "original": {"text": "Original."},
            "feedback": {"kind": "editorial", "text": "隔离测试"}, "current": current})
        product_before = self.bench.store.read_case(case["case_id"])["product"]
        before = self.stored_bytes()
        self.assert_error(409, self.publish)
        self.assertEqual(self.stored_bytes(), before)
        self.assertEqual(self.bench.store.read_case(case["case_id"])["product"], product_before)
        # A separate post already assigned to another direction must also keep its guard.
        with patch.object(self.bench.directions, "find_link", return_value={"direction_id": "202H-S1"}):
            self.assert_error(409, self.publish, self.publication_body(post_id=OTHER_POST))

    def test_crash_after_script_commit_recovers_publication_without_duplicate_script_event(self):
        body = self.publication_body()
        original = self.bench.production.mutate
        def fail_publication(kind, *args, **kwargs):
            if kind == "carousel_publication":
                raise OSError("simulated production commit interruption")
            return original(kind, *args, **kwargs)
        with patch.object(self.bench.production, "mutate", side_effect=fail_publication):
            with self.assertRaises(OSError):
                self.publish(body)
        self.assertEqual((self.script()["status"], self.script()["video_id"]), ("published", POST))
        self.assertFalse(self.current().get("publication"))
        script_history = copy.deepcopy(self.script()["history"])
        self.reload_application()
        result = self.publish(body)
        self.assertEqual(result["publication"]["post_id"], POST)
        self.assertEqual(self.script()["history"], script_history)
        self.assertEqual(self.current()["publication_history"], [result["publication"]])
        self.assertTrue(json.loads((self.bench.creation.root / "operations" / (body["request_id"] + ".json")).read_text(encoding="utf-8"))["complete"])

    def test_crash_after_both_commits_recovers_only_missing_receipt(self):
        body = self.publication_body()
        original = self.bench.creation._replace
        def fail_receipt(path, data):
            if path.parent.name == "operations" and data.get("complete"):
                raise OSError("simulated receipt interruption")
            return original(path, data)
        with patch.object(self.bench.creation, "_replace", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.publish(body)
        before = self.stored_bytes()
        self.reload_application()
        replay = self.publish(body)
        self.assertEqual(replay["publication"]["post_id"], POST)
        self.assertEqual(self.stored_bytes(), before)
        self.assertEqual(self.current()["publication_history"], [replay["publication"]])

    def test_unfinished_retry_never_rolls_back_a_later_script_publication(self):
        body = self.publication_body()
        original = self.bench.production.mutate
        def fail_publication(kind, *args, **kwargs):
            if kind == "carousel_publication":
                raise OSError("simulated production commit interruption")
            return original(kind, *args, **kwargs)
        with patch.object(self.bench.production, "mutate", side_effect=fail_publication):
            with self.assertRaises(OSError):
                self.publish(body)
        self.bench.save_script_status(self.item["script_id"], {"request_id": uuid4().hex,
            "revision": self.bench.creation.public()["revision"], "script_status": {
            "status": "published", "video_id": OTHER_POST, "published_on": "2026-10-12"}})
        before = self.stored_bytes()
        self.reload_application()
        self.assert_error(409, self.publish, body)
        self.assertEqual(self.stored_bytes(), before)
        self.assertEqual(self.script()["video_id"], OTHER_POST)

    def test_receipt_retry_after_later_correction_does_not_overwrite_current_registration(self):
        body = self.publication_body()
        original = self.bench.creation._replace
        def fail_receipt(path, data):
            if path.parent.name == "operations" and data.get("complete"):
                raise OSError("simulated receipt interruption")
            return original(path, data)
        with patch.object(self.bench.creation, "_replace", side_effect=fail_receipt):
            with self.assertRaises(OSError):
                self.publish(body)
        corrected = self.publish(self.publication_body(post_id=OTHER_POST))["publication"]
        before = self.stored_bytes()
        self.reload_application()
        self.publish(body)
        self.assertEqual(self.current()["publication"], corrected)
        self.assertEqual(self.script()["video_id"], OTHER_POST)
        self.assertEqual(self.stored_bytes(), before)

    def test_http_publication_and_detail_resolve_exact_post_sales_without_zero_fill(self):
        initial = self.request("GET", self.prefix)[2]["sales_data"]
        self.assertEqual((initial["status"], initial["metrics"]), ("missing_video_id", {}))
        self.performance.rows[POST] = {"product": "202H", "title": "Same title", "account": "fixture", "views": 1234,
            "impressions": 100, "clicks": 10, "orders": 2, "items": 2, "first_data": DAY, "last_data": DAY}
        status, _, result = self.request("POST", self.prefix + "/publication", self.publication_body())
        self.assertEqual(status, 200, result)
        self.assertEqual(result["publication"]["post_id"], POST)
        self.performance.calls.clear()
        status, _, detail = self.request("GET", self.prefix)
        self.assertEqual(status, 200, detail)
        data = detail["sales_data"]
        self.assertEqual((data["status"], data["metrics"]["video_id"], data["metrics"]["views_display"]), ("matched", POST, "1234"))
        self.assertEqual(self.performance.calls, [{POST}])
        status, _, result = self.request("POST", self.prefix + "/publication", self.publication_body(post_id=OTHER_POST))
        self.assertEqual(status, 200, result)
        detail = self.request("GET", self.prefix)[2]
        self.assertEqual(detail["sales_data"]["status"], "not_found")
        self.assertEqual(detail["sales_data"]["metrics"], {"video_id": OTHER_POST})
        self.assertIsNone(detail["sales_data"]["video"])

    def test_http_publication_requires_page_authentication_and_returns_validation_errors(self):
        before = self.stored_bytes()
        path = self.prefix + "/publication"
        self.assertEqual(self.request("POST", path, self.publication_body(), authenticated=False)[0], 403)
        self.assertEqual(self.request("POST", path, self.publication_body(), headers={"Origin": "https://external.invalid"})[0], 403)
        self.assertEqual(self.request("POST", path, [1])[0], 400)
        self.assertEqual(self.request("POST", path, {**self.publication_body(), "post_id": "invalid"})[0], 400)
        self.assertEqual(self.request("POST", path.replace(self.item["id"], "CAR-ffffffffffff"), self.publication_body())[0], 404)
        self.assertEqual(self.stored_bytes(), before)

    def test_script_status_route_cannot_split_publication_links_but_carousel_correction_can(self):
        first = self.publish()["publication"]
        before = self.stored_bytes()
        path = f"/api/scripts/{self.item['script_id']}/status"
        for values in ({"video_id": OTHER_POST}, {"published_on": "2026-10-12"}, {"status": "filmed"}):
            with self.subTest(values=values):
                status, _, result = self.request("POST", path, {"request_id": uuid4().hex,
                    "revision": self.bench.creation.public()["revision"], "script_status": values})
                self.assertEqual(status, 409, result)
                self.assertEqual(self.stored_bytes(), before)
        status, _, result = self.request("POST", path, {"request_id": uuid4().hex,
            "revision": self.bench.creation.public()["revision"], "script_status": {"status": "reviewed"}})
        self.assertEqual(status, 200, result)
        self.assertEqual((self.script()["status"], self.script()["video_id"], self.script()["published_on"]),
                         ("reviewed", POST, DAY))
        self.assertEqual(self.current()["publication"], first)
        status, _, result = self.request("POST", self.prefix + "/publication",
            self.publication_body(post_id=OTHER_POST, published_on="2026-10-12"))
        self.assertEqual(status, 200, result)
        self.assertEqual((self.script()["status"], self.script()["video_id"], self.script()["published_on"]),
                         ("published", OTHER_POST, "2026-10-12"))
        self.assertEqual(self.current()["publication"]["post_id"], OTHER_POST)
        self.assertEqual(self.current()["publication_history"][0], first)


if __name__ == "__main__":
    unittest.main()
