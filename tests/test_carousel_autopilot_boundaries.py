"""Autopilot regressions at real CLI snapshot and HTTP ownership boundaries."""
import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

from direction_store import now
import test_carousel_autopilot_http as http_fixture


class CarouselAutopilotBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = http_fixture.CarouselAutopilotHTTPTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bench = self.fixture.bench
        self.identifier = self.fixture.item["id"]

    def test_ai_candidate_selection_supplies_only_job_owned_snapshots(self):
        original = self.fixture.current()["slides"][0]["base"]
        original_path = self.bench.carousel_image(self.identifier, original["id"], "full")
        before = original_path.read_bytes()
        self.bench.production.mutate("carousel_base", self.fixture.body(carousel_id=self.identifier, slide=1, candidate=None))
        calls = []

        def choose(prompt, *, cwd, images, read_only, **options):
            directory = Path(cwd).resolve()
            # CodexImageBridge.run enforces this before starting the CLI. The
            # fixture checks the same boundary without ever invoking the client.
            self.assertTrue(all(Path(path).resolve().is_relative_to(directory) for path in images), images)
            self.assertTrue(read_only)
            self.assertEqual(len(images), 1)
            self.assertEqual(Path(images[0]).read_bytes(), before)
            snapshot = json.loads((directory / "input.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["candidates"][0]["id"], original["id"])
            calls.append(images)
            return json.dumps({"candidate_id": original["id"], "reason": "清楚的产品实拍"})

        with patch.object(self.bench.image_reviewer, "run", side_effect=choose):
            self.bench._autopilot_choose(self.identifier, "AUT-000000000001", 1, threading.Event())
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.fixture.current()["slides"][0]["base"]["id"], original["id"])
        self.assertEqual(original_path.read_bytes(), before)

    def test_restart_is_refused_while_autopilot_has_no_image_child(self):
        entered, release = threading.Event(), threading.Event()
        original = self.bench._autopilot_choose

        def pause(identifier, run_id, number, cancel):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(identifier, run_id, number, cancel)

        with patch.object(self.bench, "_autopilot_choose", side_effect=pause):
            _, started = self.fixture.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.bench.autopilot_running())
                self.assertFalse(self.bench.image_running())
                with self.assertRaises(http_fixture.fixture.http_fixture.SERVER.ApiError) as raised:
                    self.fixture.server.check_restart_ready()
                self.assertEqual(raised.exception.status, 409)
            finally:
                self.bench.cancel_carousel_autopilot(self.identifier, started["run"]["id"])
                release.set()
                self.fixture.wait_worker()

    def test_upload_and_search_deletion_cannot_bypass_an_active_autopilot(self):
        search_id = "STS-000000000001"
        self.bench.production.mutate("carousel_search_start", self.fixture.body(carousel_id=self.identifier), {
            "id": search_id, "status": "completed", "started_at": now(), "finished_at": now(), "slides": [2]})
        self.fixture.generator.block_stage = "generation"
        _, started = self.fixture.start()
        self.assertTrue(self.fixture.generator.entered.wait(2))
        try:
            payload = io.BytesIO()
            Image.new("RGB", (48, 64), "orange").save(payload, "PNG")
            actions = [
                (self.fixture.prefix + "/upload?slide=2&name=extra.png&request_id=" + uuid4().hex,
                 {"raw": payload.getvalue(), "headers": {"Content-Type": "image/png"}}),
                (self.fixture.prefix + f"/searches/{search_id}/delete", {"body": self.fixture.body()})]
            for route, options in actions:
                with self.subTest(route=route):
                    status, _, result = self.fixture.request("POST", route, **options)
                    self.assertEqual(status, 409, result)
        finally:
            self.bench.cancel_carousel_autopilot(self.identifier, started["run"]["id"])
            self.fixture.generator.release.set()
            self.fixture.wait_worker()


if __name__ == "__main__":
    unittest.main()
