"""Carousel detail keeps coherent business snapshots without locking slow media reads."""
import copy
import json
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_server as fixture
from test_production_carousel import CANDIDATE
from variant_parse import carousel_text, parse_carousels


class CarouselDetailLoadingTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def create_carousel(self):
        app = self.server.app
        source = {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 1,
                  "product": "202H", "case_id": None, "candidate_text": CANDIDATE,
                  "candidate_sha256": "x", "snapshot": {}}
        script = app.creation.mutate("script", {"request_id": uuid4().hex,
            "revision": app.creation.current_revision(), "script": {"product": "202H", "title": "Original title",
            "text": carousel_text(parse_carousels(CANDIDATE)[0]), "confirmed": True}}, source)["script"]
        return app.create_carousel({"request_id": uuid4().hex,
            "revision": app.production.current_revision(), "carousel": {"script_id": script["id"]}})["carousel"]

    def test_slow_detail_media_does_not_block_other_business_gets(self):
        app = self.server.app
        item = self.create_carousel()
        revision = app.production.current_revision()
        for method in ("carousel_images_live", "carousel_delivery_live", "carousel_publication_live"):
            with self.subTest(method=method):
                entered, release = threading.Event(), threading.Event()
                replies, failures, settled = [], [], []
                def slow(snapshot):
                    entered.set()
                    if not release.wait(4):
                        raise AssertionError("media check was not released")
                    return snapshot
                def read():
                    try:
                        replies.append(self.request("GET", f"/api/production/carousels/{item['id']}"))
                    except Exception as error:
                        failures.append(error)
                def settle(*args):
                    settled.append(app.lock._is_owned())
                with patch.object(app, method, side_effect=slow), \
                        patch("workbench_carousel.settle_resolved_activity", side_effect=settle):
                    worker = threading.Thread(target=read)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(2))
                        acquired = app.lock.acquire(timeout=.25)
                        self.assertTrue(acquired, "slow detail enrichment held the global lock")
                        if acquired:
                            app.lock.release()
                        self.assertEqual(self.request("GET", "/api/creation?product=202H")[0], 200)
                    finally:
                        release.set()
                        worker.join(4)
                self.assertEqual(failures, [])
                self.assertEqual(replies[0][0], 200)
                self.assertEqual(replies[0][2]["revision"], revision)
                self.assertEqual(app.production.current_revision(), revision)
                self.assertEqual(settled, [True])

    def test_concurrent_save_keeps_original_business_snapshot_and_cannot_settle_stale_activity(self):
        app = self.server.app
        item = self.create_carousel()
        with app.lock:
            data = app.production.read()
            data["settings"]["products"]["202H"] = {"project": "Before project", "material_timelines": ["Before material"]}
            app.production.write(data)
            app._clip_ai_live = {item["id"]: {"stage": "before", "progress": {"note": "before"}}}
            picks = app._clip_picks_file(item["id"])
            picks.parent.mkdir(parents=True)
            picks.write_text(json.dumps({"picks": {"1": [{}]}, "materials": ["Before material"]}), encoding="utf-8")
            revision = app.production.current_revision()
        entered, release = threading.Event(), threading.Event()
        replies, failures = [], []
        def slow(snapshot):
            entered.set()
            if not release.wait(4):
                raise AssertionError("media check was not released")
            snapshot["status"] = "enriched"
            return snapshot
        def read():
            try:
                replies.append(app.carousel_detail(item["id"]))
            except Exception as error:
                failures.append(error)
        with patch.object(app, "carousel_images_live", side_effect=slow), \
                patch("workbench_carousel.settle_resolved_activity") as settle:
            worker = threading.Thread(target=read)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                with app.lock:
                    data = app.production.read()
                    data["carousels"][0]["title"] = "After title"
                    data["settings"]["products"]["202H"] = {"project": "After project", "material_timelines": ["After material"]}
                    app.production.write(data)
                    scripts = app.creation.read()
                    scripts["scripts"][0]["title"] = "After script title"
                    app.creation.write(scripts)
                    app._clip_ai_live[item["id"]]["stage"] = "after"
                    app._clip_ai_live[item["id"]]["progress"]["note"] = "after"
                    picks.write_text(json.dumps({"picks": {"1": [{}, {}]}, "materials": ["After material"]}), encoding="utf-8")
            finally:
                release.set()
                worker.join(4)
            settle.assert_not_called()
        self.assertEqual(failures, [])
        detail = replies[0]
        self.assertEqual(detail["revision"], revision)
        self.assertNotEqual(app.production.current_revision(), revision)
        self.assertEqual((detail["carousel"]["title"], detail["script"]["title"]), ("Original title", "Original title"))
        self.assertEqual((detail["project"], detail["material_timelines"]), ("Before project", ["Before material"]))
        self.assertEqual(detail["carousel"]["clip_picking_live"], {"stage": "before", "progress": {"note": "before"}})
        self.assertEqual(detail["clip_picks"]["counts"], {"1": 1})
        self.assertEqual(detail["clip_picks"]["materials"], ["Before material"])
        self.assertNotEqual(app.production.carousel(item["id"])["status"], "enriched")

    def test_image_live_progress_is_copied_before_slow_file_verification(self):
        app = self.server.app
        item = self.create_carousel()
        snapshot = copy.deepcopy(item)
        generation_id = "IMG-0123456789ab"
        snapshot["generations"] = [{"id": generation_id, "status": "running"}]
        snapshot["slides"][0]["base"] = {"id": "PIC-0123456789ab"}
        class Handle:
            def is_alive(self):
                return True
        with app.lock:
            app._image_tasks()[generation_id] = {"handle": Handle(), "progress": {"note": "before"}}
        entered, release = threading.Event(), threading.Event()
        failures = []
        def slow(path):
            entered.set()
            if not release.wait(4):
                raise AssertionError("media check was not released")
            return "saved-hash"
        def enrich():
            try:
                app.carousel_images_live(snapshot)
            except Exception as error:
                failures.append(error)
        with patch.object(app, "_base_path", return_value=self.root / "temporary.png"), \
                patch("workbench_carousel_images.image_hash", side_effect=slow):
            worker = threading.Thread(target=enrich)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                acquired = app.lock.acquire(timeout=.25)
                self.assertTrue(acquired, "image validation held the global lock")
                if acquired:
                    try:
                        app._image_live[generation_id]["progress"]["note"] = "after"
                    finally:
                        app.lock.release()
            finally:
                release.set()
                worker.join(4)
        self.assertEqual(failures, [])
        self.assertEqual(snapshot["generations"][0]["live"], {"progress": {"note": "before"}})


if __name__ == "__main__":
    unittest.main()
