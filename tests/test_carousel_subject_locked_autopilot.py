"""Default batch orchestration enforces subject receipts before reuse and delivery."""
import copy
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

from codex_bridge import BridgeError
from direction_store import fingerprint, now
from production_store import carousel_input_signature, carousel_requirements, SUBJECT_RENDER_CONTRACT
from workbench_carousel_autopilot import CarouselAutopilotMixin
import test_production_carousel_images as fixture
from workbench_carousel_images import image_hash


class SubjectBatchBench(CarouselAutopilotMixin, fixture.ImageBench):
    """Complete fake image workers synchronously; exercise the real store and approval paths."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.batches = []
        self.bad_pixels = set()
        self.generated_workflow = "subject_locked"
        self.visual_checks = []
        self.visual_failure = False

    def check_subject_locked_final_visual(self, identifier, generation_id, cancel):
        item = self.production.carousel(identifier)
        choices = [self._autopilot_generated_output(item, slide, "subject_locked") for slide in item["slides"]]
        self.visual_checks.append({"generation_id": generation_id,
            "outputs": [output["id"] for output in choices], "approved": [output["approved"] for output in choices]})
        if any(output["approved"] for output in choices):
            raise AssertionError("final visual review must precede every approval")
        if self.visual_failure:
            raise BridgeError("最终画面存在产品副本或贴片边缘，整组未通过视觉核对。")
        return {"status": "passed"}

    def start_image_generation(self, identifier, body):
        self.batches.append(copy.deepcopy(body))
        generation_id = "IMG-" + uuid4().hex[:12]
        record = {"id": generation_id, "status": "running", "aspect": body["aspect"],
                  "slides": body["slides"], "started_at": now(), "added": 0}
        result = self.production.mutate("carousel_generation_start", {**body, "carousel_id": identifier}, record)
        item = self.production.carousel(identifier)
        for slide in item["slides"]:
            if slide["number"] not in body["slides"]:
                continue
            output_id = "OUT-" + uuid4().hex[:12]
            target = self._carousel_dir(identifier) / "outputs" / (output_id + ".png")
            target.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (108, 144), (slide["number"] * 30, 120, 80)).save(target)
            signature = carousel_input_signature(item, slide, body["aspect"])
            base_sha = image_hash(self._base_path(identifier, slide))
            final_sha = image_hash(target)
            output = {"id": output_id, "generation_id": generation_id, "file": f"outputs/{target.name}",
                "thumb": f"outputs/{target.name}", "width": 108, "height": 144, "sha256": final_sha,
                "base_id": slide["base"]["id"], "base_sha256": base_sha,
                "input_signature": signature, "input_changed": False, "aspect": body["aspect"],
                "instruction": "", "created_at": now(), "origin": "fake_subject_edit", "model": None,
                "requirements_snapshot": carousel_requirements(item, slide, body["aspect"]),
                "summary": "模拟图生图", "workflow": self.generated_workflow, "ai_generated": True,
                "review_deferred": True, "review": {"status": "deferred", "completed": False, "execution_status": "not_run"},
                "subject_preservation": {"status": "passed", "method": "masked_source_pixels", "base_sha256": base_sha,
                    "render_contract": SUBJECT_RENDER_CONTRACT,
                    "input_signature": signature, "final_sha256": final_sha, "protected_pixel_count": 100, "mismatch_pixels": 0},
                "test_bad_pixels": slide["number"] in self.bad_pixels}
            self.production.mutate("carousel_output", self._autopilot_body(carousel_id=identifier,
                generation_id=generation_id, slide=slide["number"]), output)
        self.production.mutate("carousel_generation_finish", self._autopilot_body(carousel_id=identifier,
            generation_id=generation_id), {"status": "completed", "finished_at": now()})
        return result


class SubjectLockedAutopilotTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.CarouselImageTests()
        with patch.object(fixture, "ImageBench", SubjectBatchBench):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bench, self.identifier = self.fixture.bench, self.fixture.item["id"]
        self.addCleanup(self.bench.stop_carousel_autopilots)
        # The pixel validator has its own real-image tests. Here a mismatch lets
        # us prove that orchestration cannot select or package a rejected image.
        self.validator = patch("carousel_subject_lock.verify_subject_lock", side_effect=self.verify)
        self.check = self.validator.start()
        self.addCleanup(self.validator.stop)

    @staticmethod
    def verify(root, output):
        if output.get("test_bad_pixels"):
            raise BridgeError("原图主体像素不一致")
        return output["subject_preservation"]

    def run_default(self):
        self.bench.start_carousel_autopilot(self.identifier, self.fixture.body(workflow="subject_locked"))
        deadline = time.monotonic() + 10
        while self.bench.autopilot_running() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.bench.autopilot_running())
        return self.fixture.current()

    def test_default_batches_all_slides_and_only_packages_verified_subjects(self):
        item = self.run_default()
        run = item["autopilot_runs"][-1]
        self.assertEqual(run["workflow"], "subject_locked")
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(len(self.bench.batches), 1)
        self.assertEqual(self.bench.batches[0]["slides"], [1, 2, 3])
        self.assertEqual(self.bench.batches[0]["workflow"], "subject_locked")
        self.assertEqual(item["requirements"]["render_contract"], SUBJECT_RENDER_CONTRACT)
        self.assertEqual(run["completed_slides"], [1, 2, 3])
        self.assertTrue(run["result"]["product_preservation_checked"])
        self.assertFalse(run["result"]["product_consistency_checked"])
        self.assertTrue(run["result"]["final_visual_checked"])
        self.assertTrue(run["delivery_id"])
        self.assertEqual(len(self.bench.visual_checks), 1)
        self.assertEqual(self.bench.visual_checks[0]["approved"], [False, False, False])
        self.assertGreaterEqual(self.check.call_count, 3)
        self.assertIn("主体", run["message"])

    def test_subject_locked_batch_runs_while_editing_owns_resolve(self):
        self.bench._reserve("EDT-parallel")
        self.bench.resolve.lock.acquire()
        try:
            self.bench.runner.calls.clear()
            item = self.run_default()
            run = item["autopilot_runs"][-1]
            self.assertEqual(run["status"], "completed", run)
            self.assertTrue(run["delivery_id"])
            self.assertTrue(self.bench.resolve.busy())
            self.assertFalse(self.bench._edit_cancel["EDT-parallel"].is_set())
            self.assertEqual(self.bench.runner.calls, [])
        finally:
            self.bench.resolve.lock.release()
            self.bench._forget("EDT-parallel")

    def test_pixel_failure_retains_images_but_blocks_selection_and_delivery(self):
        self.bench.bad_pixels = {2}
        item = self.run_default()
        run = item["autopilot_runs"][-1]
        self.assertEqual(run["status"], "failed", run)
        self.assertEqual(run["completed_slides"], [1, 3])
        self.assertEqual(run["result"]["images_generated"], 3)
        self.assertEqual(run["result"]["subject_preserved_count"], 2)
        self.assertFalse(run["result"]["product_preservation_checked"])
        self.assertFalse(item["slides"][1]["outputs"][0]["approved"])
        self.assertFalse(any(output["approved"] for slide in item["slides"] for output in slide["outputs"]))
        self.assertEqual(self.bench.visual_checks, [])
        self.assertEqual(item.get("deliveries", []), [])
        self.assertEqual([len(slide["outputs"]) for slide in item["slides"]], [1, 1, 1])

    def test_whole_redraw_output_cannot_satisfy_subject_locked_run(self):
        self.bench.generated_workflow = "generation_first"
        item = self.run_default()
        run = item["autopilot_runs"][-1]
        self.assertEqual(run["status"], "failed", run)
        self.assertEqual(run["completed_slides"], [])
        self.assertEqual(item.get("deliveries", []), [])
        self.assertFalse(any(output["approved"] for slide in item["slides"] for output in slide["outputs"]))

    def test_resume_verifies_bytes_again_and_reuses_only_protected_images(self):
        first = self.run_default()
        self.assertEqual(first["autopilot_runs"][-1]["status"], "completed", first["autopilot_runs"][-1])
        self.check.reset_mock()
        second = self.run_default()
        self.assertEqual(second["autopilot_runs"][-1]["status"], "completed", second["autopilot_runs"][-1])
        self.assertEqual(len(self.bench.batches), 1)
        self.assertGreaterEqual(self.check.call_count, 3)
        self.assertEqual(len(self.bench.visual_checks), 2)
        self.assertIsNone(self.bench.visual_checks[-1]["generation_id"])
        self.assertEqual([slide["output_id"] for slide in first["slides"]],
                         [slide["output_id"] for slide in second["slides"]])

    def test_previous_approved_subject_images_are_remade_when_receipt_contract_is_missing(self):
        first = self.run_default()
        self.assertEqual(first["autopilot_runs"][-1]["status"], "completed", first["autopilot_runs"][-1])
        previous_ids = [slide["output_id"] for slide in first["slides"]]
        with self.bench.lock, self.bench.production.catalog_transaction():
            data = self.bench.production.read()
            stored = next(item for item in data["carousels"] if item["id"] == self.identifier)
            stored["requirements"].pop("render_contract", None)
            for slide in stored["slides"]:
                for output in slide["outputs"]:
                    output["subject_preservation"].pop("render_contract")
            self.bench.production.write(data)
        second = self.run_default()
        run = second["autopilot_runs"][-1]
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(len(self.bench.batches), 2)
        self.assertEqual(self.bench.batches[-1]["slides"], [1, 2, 3])
        self.assertTrue(all(slide["output_id"] != old for slide, old in zip(second["slides"], previous_ids)))
        self.assertEqual([len(slide["outputs"]) for slide in second["slides"]], [2, 2, 2])
        self.assertTrue(all(row["output_id"] not in previous_ids for row in second["deliveries"][-1]["slides"]))

    def test_final_visual_failure_retains_group_without_approving_or_packaging_any_image(self):
        self.bench.visual_failure = True
        item = self.run_default()
        run = item["autopilot_runs"][-1]
        self.assertEqual(run["status"], "failed", run)
        self.assertEqual(run["result"]["images_generated"], 3)
        self.assertTrue(run["result"]["product_preservation_checked"])
        self.assertFalse(run["result"]["final_visual_checked"])
        self.assertEqual(len(self.bench.visual_checks), 1)
        self.assertEqual(item.get("deliveries", []), [])
        self.assertFalse(run["delivery_id"])
        self.assertEqual([len(slide["outputs"]) for slide in item["slides"]], [1, 1, 1])
        self.assertFalse(any(output["approved"] for slide in item["slides"] for output in slide["outputs"]))

    def test_approved_v2_candidates_are_remade_and_cannot_enter_v3_delivery(self):
        first = self.run_default()
        self.assertEqual(first["autopilot_runs"][-1]["status"], "completed", first["autopilot_runs"][-1])
        previous_ids = [slide["output_id"] for slide in first["slides"]]
        with self.bench.lock, self.bench.production.catalog_transaction():
            data = self.bench.production.read()
            stored = next(item for item in data["carousels"] if item["id"] == self.identifier)
            stored["requirements"]["render_contract"] = "background_plate_v2"
            for slide in stored["slides"]:
                for output in slide["outputs"]:
                    old_requirements = copy.deepcopy(output["requirements_snapshot"])
                    old_requirements["render_contract"] = "background_plate_v2"
                    with patch("production_store.carousel_requirements", return_value=old_requirements):
                        old_signature = carousel_input_signature(stored, slide, output["aspect"])
                    output.update(requirements_snapshot=old_requirements, requirements_signature=fingerprint(old_requirements),
                                  input_signature=old_signature, approved_input_signature=old_signature)
                    output["subject_preservation"].update(render_contract="background_plate_v2", input_signature=old_signature)
            self.bench.production.write(data)
        second = self.run_default()
        run = second["autopilot_runs"][-1]
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(second["requirements"]["render_contract"], "segmented_background_v4")
        self.assertEqual(len(self.bench.batches), 2)
        self.assertEqual(self.bench.batches[-1]["slides"], [1, 2, 3])
        self.assertEqual(len(self.bench.visual_checks), 2)
        self.assertTrue(run["result"]["final_visual_checked"])
        self.assertTrue(all(slide["output_id"] != old for slide, old in zip(second["slides"], previous_ids)))
        self.assertTrue(all(row["output_id"] not in previous_ids for row in second["deliveries"][-1]["slides"]))

    def test_older_canvas_layout_images_are_remade_even_when_still_picked(self):
        first = self.run_default()
        self.assertEqual(first["autopilot_runs"][-1]["status"], "completed", first["autopilot_runs"][-1])
        previous_ids = [slide["output_id"] for slide in first["slides"]]
        with self.bench.lock, self.bench.production.catalog_transaction():
            data = self.bench.production.read()
            stored = next(item for item in data["carousels"] if item["id"] == self.identifier)
            for slide in stored["slides"]:
                for output in slide["outputs"]:
                    output["subject_preservation"]["render_contract"] = "segmented_background_v3"
            self.bench.production.write(data)
        second = self.run_default()
        run = second["autopilot_runs"][-1]
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(self.bench.batches[-1]["slides"], [1, 2, 3], "images made on the old layout are all made again")
        self.assertTrue(all(slide["output_id"] not in previous_ids for slide in second["slides"]))

    def test_the_newest_valid_image_wins_over_an_older_picked_one(self):
        first = self.run_default()
        self.assertEqual(first["autopilot_runs"][-1]["status"], "completed", first["autopilot_runs"][-1])
        picked = first["slides"][1]["output_id"]
        # A redo of slide 2 finishes: the new image exists but nobody has picked it yet.
        self.bench.start_image_generation(self.identifier, self.bench._autopilot_body(
            slides=[2], aspect="3:4", redo=True, workflow="subject_locked", review_deferred=True))
        item = self.fixture.current()
        slide = item["slides"][1]
        self.assertEqual(slide["output_id"], picked)
        newest = slide["outputs"][-1]["id"]
        self.assertNotEqual(newest, picked)
        self.assertEqual(self.bench._autopilot_generated_output(item, slide, "subject_locked")["id"], newest)
        # The group's final visual check therefore looks at the redo, and approval moves the pick onto it.
        second = self.run_default()
        run = second["autopilot_runs"][-1]
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual(self.bench.visual_checks[-1]["outputs"][1], newest)
        self.assertEqual(second["slides"][1]["output_id"], newest)
        self.assertEqual(len(self.bench.batches), 2, "nothing else is generated again")

    def test_failed_visual_recheck_revokes_reused_approvals_without_regenerating(self):
        first = self.run_default()
        self.assertEqual(first["autopilot_runs"][-1]["status"], "completed", first["autopilot_runs"][-1])
        self.bench.visual_failure = True
        second = self.run_default()
        run = second["autopilot_runs"][-1]
        self.assertEqual(run["status"], "failed", run)
        self.assertFalse(run["result"]["final_visual_checked"])
        self.assertEqual(len(self.bench.batches), 1)
        self.assertEqual(len(self.bench.visual_checks), 2)
        self.assertIsNone(self.bench.visual_checks[-1]["generation_id"])
        self.assertEqual([slide["output_id"] for slide in second["slides"]],
                         [slide["output_id"] for slide in first["slides"]])
        self.assertFalse(any(output["approved"] for slide in second["slides"] for output in slide["outputs"]))
        self.assertEqual(len(second["deliveries"]), 1)
        self.assertFalse(run["delivery_id"])


if __name__ == "__main__":
    unittest.main()
