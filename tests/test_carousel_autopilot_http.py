"""Actual carousel API orchestration over isolated generation and vision workers."""
import io
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
import zipfile

from production_store import ProductionStore
from carousel_plan import GROUP_REVIEW_SCHEMA
import test_production_carousel_images as fixture


class CarouselAutopilotHTTPTests(unittest.TestCase):
    request = fixture.CarouselImageHTTPTests.request
    body = fixture.CarouselImageHTTPTests.body
    carousel = fixture.CarouselImageHTTPTests.carousel
    current = fixture.CarouselImageHTTPTests.current
    pick_new_base = fixture.CarouselImageHTTPTests.pick_new_base

    def setUp(self):
        fixture.CarouselImageHTTPTests.setUp(self)
        # Exercise the production split: a direct edit provider cannot review,
        # and the independent reviewer must only receive read-only calls.
        self.generator.direct_image_edit = True
        self.generator.review_available = False
        self.generator.source = "cockpit_image_edit"
        self.reviewer = fixture.ImageGenerator()
        self.bench.image_reviewer = self.reviewer
        for number in (2, 3):
            self.pick_new_base(number)

    def stop_server(self):
        self.generator.release.set()
        if hasattr(self, "reviewer"):
            self.reviewer.release.set()
        fixture.CarouselImageHTTPTests.stop_server(self)

    def wait_worker(self):
        deadline = time.monotonic() + 15
        while (self.bench.autopilot_running() or self.bench.image_running()) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.bench.autopilot_running(), "autopilot fixture did not finish")
        self.assertFalse(self.bench.image_running(), "image fixture did not finish")
        return self.current()

    def start(self, body=None):
        submitted = {"workflow": "review_first", **(body or self.body())}
        status, _, result = self.request("POST", self.prefix + "/autopilot", submitted)
        self.assertEqual(status, 200, result)
        return submitted, result

    def test_full_api_run_persists_ai_confirmation_and_downloadable_package(self):
        body, started = self.start()
        self.wait_worker()
        status, _, response = self.request("GET", self.prefix)
        self.assertEqual(status, 200, response)
        item = response["carousel"]
        run = item["autopilot_runs"][-1]
        self.assertEqual(run["id"], started["run"]["id"])
        self.assertEqual(run["status"], "completed", run)
        self.assertTrue(run["delivery_id"])
        self.assertEqual(len(self.generator.calls), 3)
        self.assertTrue(all(not call["read_only"] for call in self.generator.calls))
        # The finished run is also read once as a whole group; that call is the group review, not a slide check.
        self.assertEqual(len([call for call in self.reviewer.calls if call["schema"] is not GROUP_REVIEW_SCHEMA]), 3)
        self.assertTrue(all(call["read_only"] for call in self.reviewer.calls))
        for slide in item["slides"]:
            output = next(entry for entry in slide["outputs"] if entry["id"] == slide["output_id"])
            self.assertTrue(output["approved"])
            self.assertEqual(output["approval_actor"], "assistant_on_user_authorization")
            self.assertTrue(output["review"]["completed"])
        self.assertEqual(ProductionStore(self.bench.runtime).carousel(item["id"])["autopilot_runs"][-1], run)
        self.assertFalse(item.get("publication"))
        status, _, payload = self.request("GET", self.prefix + f"/deliveries/{run['delivery_id']}/download")
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(archive.namelist(), ["01.png", "02.png", "03.png"])
            for slide in item["slides"]:
                self.assertEqual(archive.read(f"{slide['number']:02d}.png"),
                    self.bench.carousel_output(item["id"], slide["output_id"], "full").read_bytes())
        _, replay = self.start(body)
        self.assertEqual(replay["run"]["id"], run["id"])
        self.assertEqual(len(self.current()["autopilot_runs"]), 1)
        self.assertEqual(len(self.current()["deliveries"]), 1)
        self.assertEqual(len(self.generator.calls), 3)

    def test_running_api_blocks_manual_mutation_and_data_transfer_then_cancels(self):
        self.generator.block_stage = "generation"
        body, started = self.start()
        self.assertTrue(self.generator.entered.wait(2))
        run_id = started["run"]["id"]
        try:
            _, replay = self.start(body)
            self.assertEqual(replay["run"]["id"], run_id)
            self.assertEqual(self.request("POST", self.prefix + "/autopilot", self.body())[0], 409)
            for action in ("base", "requirements", "generate", "approval", "delivery"):
                with self.subTest(action=action):
                    status, _, result = self.request("POST", self.prefix + "/" + action, self.body())
                    self.assertEqual(status, 409, result)
            status, _, detail = self.request("GET", self.prefix)
            self.assertEqual(status, 200, detail)
            self.assertEqual(detail["carousel"]["autopilot_runs"][-1]["status"], "running")
            with patch.object(self.bench, "image_running", return_value=False):
                with self.assertRaises(fixture.http_fixture.SERVER.TransferError) as caught:
                    self.bench.ensure_data_idle()
                self.assertEqual(caught.exception.status, 409)
            status, _, cancelled = self.request("POST", self.prefix + f"/autopilot/{run_id}/cancel", {})
            self.assertEqual(status, 200, cancelled)
            item = self.wait_worker()
            self.assertEqual(item["autopilot_runs"][-1]["status"], "cancelled")
            self.assertFalse(item.get("deliveries"))
            self.assertEqual(self.reviewer.calls, [])
            self.assertEqual(self.request("POST", self.prefix + f"/autopilot/{run_id}/cancel", {})[0], 200)
            self.bench.ensure_data_idle()
        finally:
            self.generator.release.set()

    def test_autopilot_routes_require_the_local_page_token_and_valid_owner(self):
        before = self.bench.production.path.read_bytes()
        for route in (self.prefix + "/autopilot", self.prefix + "/autopilot/AUT-000000000000/cancel"):
            with self.subTest(route=route):
                self.assertEqual(self.request("POST", route, self.body(), authenticated=False)[0], 403)
                self.assertEqual(self.request("POST", route, self.body(), headers={"Origin": "https://external.invalid"})[0], 403)
        self.assertEqual(self.request("POST", "/api/production/carousels/CAR-ffffffffffff/autopilot", self.body())[0], 404)
        self.assertEqual(self.bench.production.path.read_bytes(), before)
        self.assertEqual(self.generator.calls, [])
        self.assertEqual(self.reviewer.calls, [])


if __name__ == "__main__":
    unittest.main()
