"""Reuse only bound AI plates/plans while the local subject renderer is upgraded."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))

from production_store import SUBJECT_RENDER_CONTRACT
from codex_bridge import BridgeError
from workbench_carousel_images import CarouselImagesMixin, image_hash


class CacheBench(CarouselImagesMixin):
    def __init__(self, root):
        self.root = root
        self.stages = []
        self.image_generator = SimpleNamespace(source="cockpit_image_edit", run=Mock(
            side_effect=AssertionError("A valid cached plate must not call the image model again")))
        self.planner = SimpleNamespace(run=Mock(return_value=json.dumps({
            "summary": "Warm empty kitchen background", "style": "Warm natural light",
            "slides": [{"number": 1, "concept": "Keep the countertop perspective",
                        "prompt": "Create a continuous empty countertop with soft daylight.",
                        "protected_regions": [[[300, 400], [700, 400], [700, 800], [300, 800]]]}]})))
        self._check_subject_coverage = Mock(side_effect=self.coverage)

    def _carousel_dir(self, identifier):
        return self.root

    def _image_reviewer(self):
        return self.planner

    def _direct_image_edit(self):
        return True

    def _image_progress(self, *args):
        pass

    def _stage(self, identifier, generation_id, number, phase, status, **kwargs):
        self.stages.append({"phase": phase, "status": status, **kwargs})

    @staticmethod
    def coverage(identifier, generation_id, snapshots, folder, cancel):
        for snapshot, _, _ in snapshots:
            snapshot["subject_lock"]["coverage"] = {"status": "passed"}


class SubjectBackgroundCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.bench = CacheBench(self.root)
        self.cancel = threading.Event()

    def snapshot(self, generation_id, contract=SUBJECT_RENDER_CONTRACT):
        work = self.root / "generations" / generation_id / "01"
        work.mkdir(parents=True)
        base, source = work / "base.png", work / "subject-source.png"
        Image.new("RGB", (24, 32), "orange").save(base)
        Image.new("RGB", (24, 32), "gray").save(source)
        snapshot = {"workflow": "subject_locked", "carousel": {"id": "CAR-cache", "product": "sharpener"},
            "aspect": "3:4", "instruction": "", "input_signature": "input-" + contract,
            "requirements_signature": "requirements-" + contract, "base_sha256": image_hash(base),
            "requirements": {"workflow": "subject_locked", "mode": "photo_preserving",
                             "render_contract": contract, "style": "Warm natural light", "instruction": ""},
            "slide": {"number": 1, "title_lines": ["A cleaner edge"], "body": "", "role": "opening",
                      "source": "Original photo", "forbidden": "", "base": {"id": "BASE-one"}},
            "subject_lock": {"file": source.name, "sha256": image_hash(source),
                             "base_sha256": image_hash(base), "coverage": {"status": "passed"}},
            "prompt_plan": {"prompt": "Make an empty countertop", "style": "Warm natural light"},
            "generation_prompt": "Create a BACKGROUND PLATE without products."}
        self.save(work, snapshot)
        return snapshot, work, base

    @staticmethod
    def save(work, snapshot):
        (work / "input.json").write_text(json.dumps(snapshot), encoding="utf-8")

    def old_plate(self, contract=SUBJECT_RENDER_CONTRACT, generation_id="IMG-old"):
        snapshot, work, base = self.snapshot(generation_id, contract)
        raw, locked = work / "render.png", work / "render-locked.png"
        Image.new("RGB", (24, 32), "blue").save(raw)
        Image.new("RGB", (24, 32), "yellow").save(locked)
        receipt = {"status": "passed", "method": "masked_source_pixels", "render_contract": contract,
            "aspect": snapshot["aspect"], "input_signature": snapshot["input_signature"],
            "base_file": base.relative_to(self.root).as_posix(), "base_sha256": image_hash(base),
            "source_file": (work / "subject-source.png").relative_to(self.root).as_posix(),
            "source_sha256": snapshot["subject_lock"]["sha256"],
            "generated_file": raw.relative_to(self.root).as_posix(), "generated_sha256": image_hash(raw)}
        (work / "subject-lock-receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        return snapshot, work, receipt

    def test_raw_plate_recomposes_without_new_ai_call_and_rejects_unbound_candidates(self):
        prior, old_work, receipt = self.old_plate()
        snapshot, work, base = self.snapshot("IMG-new")
        item = {"slides": [{"number": 1, "outputs": []}]}
        saved = []

        def mutate(action, body, output):
            self.assertEqual(action, "carousel_output")
            saved.append(output)
            return {"output": output}

        def compose(actual_base, actual_work, raw, actual_snapshot):
            self.assertEqual(raw, work / "render.png")
            self.assertEqual(image_hash(raw), receipt["generated_sha256"])
            target = work / "render-locked.png"
            Image.new("RGB", (24, 32), "purple").save(target)
            return target, {"status": "passed", "render_contract": SUBJECT_RENDER_CONTRACT}

        self.bench.production = SimpleNamespace(carousel=lambda identifier: item, mutate=mutate)
        self.bench._base_path = lambda identifier, slide: base
        with patch("carousel_subject_lock.compose_subject_locked", side_effect=compose) as compositor, \
                patch("carousel_subject_lock.verify_subject_lock", return_value={"status": "passed"}) as verifier:
            self.assertEqual(self.bench._produce_slide("CAR-cache", "IMG-new", snapshot, work, base, self.cancel), [])
        self.bench.image_generator.run.assert_not_called()
        compositor.assert_called_once()
        verifier.assert_called_once()
        self.assertEqual(saved[0]["origin"], "cached_ai_background")
        self.assertEqual(saved[0]["background_reused_from"], {"generation_id": "IMG-old",
            "file": receipt["generated_file"], "sha256": receipt["generated_sha256"]})
        persisted = json.loads((work / "input.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted["background_reused_from"], saved[0]["background_reused_from"])
        self.assertTrue(any(stage["phase"] == "generation" and stage["status"] == "skipped"
                            for stage in self.bench.stages))
        self.assertIn("未再次调用生图", saved[0]["summary"])

        originals = {name: (old_work / name).read_bytes() for name in
                     ("base.png", "subject-source.png", "render.png", "input.json", "subject-lock-receipt.json")}
        for case in ("base", "source", "raw", "title", "requirements", "whole_redraw", "locked_output"):
            with self.subTest(case=case):
                for name, content in originals.items():
                    (old_work / name).write_bytes(content)
                if case in {"base", "source", "raw"}:
                    target = {"base": "base.png", "source": "subject-source.png", "raw": "render.png"}[case]
                    Image.new("RGB", (24, 32), "red").save(old_work / target)
                elif case == "locked_output":
                    bad = {**receipt, "generated_file": (old_work / "render-locked.png").relative_to(self.root).as_posix()}
                    (old_work / "subject-lock-receipt.json").write_text(json.dumps(bad), encoding="utf-8")
                else:
                    bad = copy.deepcopy(prior)
                    if case == "title":
                        bad["slide"]["title_lines"] = ["Different title"]
                    elif case == "requirements":
                        bad["requirements"]["style"] = "A different environment"
                    else:
                        bad["workflow"] = bad["requirements"]["workflow"] = "generation_first"
                    self.save(old_work, bad)
                fresh, fresh_work, _ = self.snapshot("IMG-reject-" + case)
                self.assertIsNone(self.bench._reuse_subject_background("CAR-cache", "IMG-reject-" + case, fresh, fresh_work))
                self.assertFalse((fresh_work / "render.png").exists())

    def test_a_plate_is_reused_only_for_the_same_canvas_window(self):
        prior, old_work, _ = self.old_plate()
        prior["subject_lock"]["transform"] = {"crop_top": 0, "crop_height": 1824}
        self.save(old_work, prior)
        moved, moved_work, _ = self.snapshot("IMG-moved")
        moved["subject_lock"]["transform"] = {"crop_top": 192, "crop_height": 1824}   # the window slid after a new fine mask
        self.save(moved_work, moved)
        self.assertIsNone(self.bench._reuse_subject_background("CAR-cache", "IMG-moved", moved, moved_work))
        same, same_work, _ = self.snapshot("IMG-same")
        same["subject_lock"]["transform"] = {"crop_top": 0, "crop_height": 1824}
        self.save(same_work, same)
        self.assertEqual(self.bench._reuse_subject_background("CAR-cache", "IMG-same", same, same_work), same_work / "render.png")

    def test_plates_and_plans_from_an_older_canvas_layout_are_never_reused(self):
        # v3 and older put the photo inside side margins below a dark title slab: their plates and protected outlines
        # belong to that canvas and do not line up with the full-width layout.
        for contract in ("segmented_background_v3", "background_plate_v2"):
            with self.subTest(contract=contract):
                self.old_plate(contract, "IMG-stale-" + contract)
                fresh, fresh_work, _ = self.snapshot("IMG-fresh-" + contract)
                self.assertIsNone(self.bench._reuse_subject_background("CAR-cache", "IMG-fresh-" + contract, fresh, fresh_work))
                self.assertFalse((fresh_work / "render.png").exists())
                calls = self.bench.planner.run.call_count
                stale = self.snapshot("IMG-stale-plan-" + contract, contract)
                current = self.snapshot("IMG-current-plan-" + contract)
                with patch("carousel_subject_lock.build_subject_mask", side_effect=lambda work, regions: {
                        "file": shutil.copyfile(work / "subject-source.png", work / "subject-mask.png") and "subject-mask.png",
                        "preview_file": shutil.copyfile(work / "subject-source.png", work / "mask-preview.png") and "mask-preview.png"}):
                    self.bench._plan_generation_prompts("CAR-cache", "IMG-stale-plan-" + contract, [stale], self.cancel)
                    self.bench._plan_generation_prompts("CAR-cache", "IMG-current-plan-" + contract, [current], self.cancel)
                self.assertGreaterEqual(self.bench.planner.run.call_count - calls, 1, "an older layout's plan is written again")
                for generation_id in ("IMG-stale-plan-" + contract, "IMG-current-plan-" + contract):
                    plan = json.loads((self.root / "generations" / generation_id / "prompt-plan" / "plan.json").read_text(encoding="utf-8"))
                    self.assertNotIn("IMG-stale", plan.get("reused_from", {}).get("generation_id", ""),
                                     "a plan made on an older layout is not taken over, and does not take one over")

    def test_verified_plan_reuses_only_unchanged_inputs_and_repeats_mask_checks(self):
        old = self.snapshot("IMG-plan-old")
        current = self.snapshot("IMG-plan-new")

        def mask(work, regions):
            self.assertTrue(regions)
            shutil.copyfile(work / "subject-source.png", work / "subject-mask.png")
            shutil.copyfile(work / "subject-source.png", work / "mask-preview.png")
            return {"file": "subject-mask.png", "preview_file": "mask-preview.png"}

        with patch("carousel_subject_lock.build_subject_mask", side_effect=mask) as masks:
            self.bench._plan_generation_prompts("CAR-cache", "IMG-plan-old", [old], self.cancel)
            self.bench._plan_generation_prompts("CAR-cache", "IMG-plan-new", [current], self.cancel)
            self.assertEqual(self.bench.planner.run.call_count, 1)
            self.assertEqual(masks.call_count, 2)
            self.assertEqual(self.bench._check_subject_coverage.call_count, 2)
            plan = json.loads((current[1].parent / "prompt-plan" / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["reused_from"]["generation_id"], "IMG-plan-old")
            self.assertEqual(current[0]["subject_lock"]["coverage"]["status"], "passed")
            changed = self.snapshot("IMG-plan-changed")
            changed[0]["slide"]["title_lines"] = ["New locked words"]
            self.bench._plan_generation_prompts("CAR-cache", "IMG-plan-changed", [changed], self.cancel)
            self.assertEqual(self.bench.planner.run.call_count, 2)
            self.assertEqual(masks.call_count, 3)
            self.assertEqual(self.bench._check_subject_coverage.call_count, 3)


    def mask_copy(self, work, regions):
        shutil.copyfile(work / "subject-source.png", work / "subject-mask.png")
        shutil.copyfile(work / "subject-source.png", work / "mask-preview.png")
        return {"file": "subject-mask.png", "preview_file": "mask-preview.png"}

    def box_rounds(self, verdicts):
        """Run the planning step with a fine mask whose coverage check answers `verdicts` in order."""
        snapshot, work, _ = self.snapshot("IMG-box-" + str(len(verdicts)) + str(id(verdicts)))
        answers, calls = list(verdicts), []

        def coverage(identifier, generation_id, snapshots, folder, cancel, *, raise_failures=True):
            failed = answers.pop(0)
            calls.append(raise_failures)
            (folder / "subject-coverage.json").write_text(json.dumps({"slides": failed}), encoding="utf-8")
            if failed and raise_failures:
                raise BridgeError("主体保护区尚未覆盖完整")
            if not failed:
                for entry, _, _ in snapshots:
                    entry["subject_lock"]["coverage"] = {"status": "passed"}
            return failed

        self.bench._check_subject_coverage = Mock(side_effect=coverage)
        self.bench._refine_subject_masks = Mock(return_value={1: {"status": "cached"}})
        self.bench._lock_missed_boxes = Mock(return_value=[1])
        miss = [{"number": 1, "covered": False, "issues": ["a small part"], "missing_boxes": [{"x0": 1, "y0": 1, "x1": 2, "y1": 2}]}]
        verdicts[:] = [miss if v else [] for v in verdicts]
        answers[:] = list(verdicts)
        with patch("carousel_subject_lock.build_subject_mask", side_effect=self.mask_copy):
            outcome = None
            try:
                self.bench._plan_generation_prompts("CAR-cache", work.parent.name, [(snapshot, work, work / "base.png")], self.cancel)
            except BridgeError as error:
                outcome = error
        return outcome, calls, work

    def test_a_second_box_round_may_name_a_part_the_first_round_did_not(self):
        outcome, calls, work = self.box_rounds([True, True, False])
        self.assertIsNone(outcome)
        self.assertEqual(calls, [False, False, True], "the last allowed check is the one that must pass")
        self.assertEqual(self.bench._lock_missed_boxes.call_count, 2)
        plan = work.parent / "prompt-plan"
        self.assertTrue((plan / "subject-coverage-before-boxes.json").exists())
        self.assertTrue((plan / "subject-coverage-before-boxes-2.json").exists(), "each round's verdict is kept")

    def test_box_rounds_are_capped_and_the_standard_is_not_relaxed(self):
        outcome, calls, _ = self.box_rounds([True, True, True])
        self.assertIsInstance(outcome, BridgeError)
        self.assertEqual(self.bench._lock_missed_boxes.call_count, 2, "no third round of locking")
        self.assertEqual(len(calls), 3)

    def test_a_clean_first_check_needs_no_box_round(self):
        outcome, calls, _ = self.box_rounds([False])
        self.assertIsNone(outcome)
        self.assertEqual(calls, [False])
        self.bench._lock_missed_boxes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
