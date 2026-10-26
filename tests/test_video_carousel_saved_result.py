"""Video status preserves access to a saved table after its production order is deleted."""
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_production_carousel_table as fixture


class VideoCarouselSavedResultTests(unittest.TestCase):
    setUp = fixture.CarouselTableTests.setUp
    stop_server = fixture.CarouselTableTests.stop_server
    request = fixture.CarouselTableTests.request
    ok = fixture.CarouselTableTests.ok
    script = fixture.CarouselTableTests.script
    start = fixture.CarouselTableTests.start
    wait = fixture.CarouselTableTests.wait
    fixture = fixture.CarouselTableTests.fixture

    def test_deleted_order_keeps_original_table_in_video_status_without_writing(self):
        app = self.server.app
        app.ai.bridge = fixture.FakeBridge(output=fixture.TABLE)
        script = self.script()
        job = app.create_production_job({
            "request_id": uuid4().hex, "revision": app.production.public()["revision"],
            "job": {"script_id": script["id"]}})["job"]
        table = self.wait(self.start(script)["id"])
        carousel = app.create_carousel({
            "request_id": uuid4().hex, "revision": app.production.public()["revision"],
            "carousel": {"script_id": script["id"], "table_id": table["id"]}})["carousel"]
        folder = app._table_dir(table["id"])
        record = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        record.update(origin="video", video_job_id=job["id"], status="needs_attention",
                      carousel_id=carousel["id"])
        app._write_table(folder, record)
        app.production.mutate("carousel_delete", {"request_id": uuid4().hex, "carousel_id": carousel["id"]})
        before = (folder / "state.json").read_bytes(), app.production.path.read_bytes()
        with patch.object(app, "_write_table", side_effect=AssertionError("status rewrote table")), \
                patch.object(app.production, "mutate", side_effect=AssertionError("status recreated order")), \
                patch.object(app.ai, "_available", side_effect=AssertionError("status started AI")):
            status = self.ok("GET", f"/api/production/jobs/{job['id']}/video-carousel")
        self.assertFalse(status["running"])
        run, = status["runs"]
        self.assertEqual(run["id"], table["id"])
        self.assertEqual(run["status"], "needs_attention")
        self.assertEqual(run["carousel_id"], "")
        self.assertTrue(run["carousel_missing"])
        self.assertTrue(run["has_result"])
        self.assertEqual(run["slide_count"], 3)
        self.assertFalse(run["delivery_available"])
        self.assertEqual(((folder / "state.json").read_bytes(), app.production.path.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
