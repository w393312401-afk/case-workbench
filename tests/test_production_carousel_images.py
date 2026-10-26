"""Carousel image generation, version history and human review, with no live engines."""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import patch
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from PIL import Image

from codex_bridge import BridgeCancelled, BridgeError
from elevenlabs_client import VoiceError
from production_store import ProductionStore
from test_production_carousel import CANDIDATE
from test_production_carousel_stills import Searcher, StillBench
from variant_parse import carousel_text, parse_carousels
from workbench_carousel_images import MAX_CONCURRENCY, CarouselImagesMixin, normalize_review
from workbench_carousel_delivery import CarouselDeliveryMixin
import test_server as http_fixture
from test_ai_tasks import FakeBridge


class ImageGenerator:
    """Two independent fake calls: write a new image, then read back its visible words."""

    def __init__(self):
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block_stage = None
        self.file_mode = "new"
        self.review_mode = "pass"

    def status(self):
        return {"available": True, "label": "Fake Codex"}

    def run(self, prompt, *, cwd, images, cancel_event, on_progress, on_event=None,
            timeout=None, schema=None, read_only=False):
        folder = Path(cwd)
        snapshot = json.loads((folder / "input.json").read_text(encoding="utf-8"))
        stage = "review" if read_only else "generation"
        self.calls.append({"prompt": prompt, "cwd": folder, "images": images, "read_only": read_only,
                           "schema": schema, "snapshot": snapshot})
        on_progress("已开始模拟" + stage)
        if self.block_stage == stage:
            self.entered.set()
            deadline = time.monotonic() + 5
            while not self.release.wait(.01):
                if cancel_event.is_set():
                    raise BridgeCancelled("cancelled")
                if time.monotonic() > deadline:
                    raise BridgeError("fake job timed out")
        if cancel_event.is_set():
            raise BridgeCancelled("cancelled")
        if not read_only:
            if self.file_mode == "error":
                raise BridgeError("模拟生成失败")
            base = Path(images[0])
            target = folder / "render.png"
            name = target.name
            if self.file_mode == "base":
                name = base.name
            elif self.file_mode == "copy":
                shutil.copyfile(base, target)
            elif self.file_mode == "outside":
                target = folder.parent / "outside.png"
                name = "../outside.png"
            elif self.file_mode == "missing":
                name = "missing.png"
            elif self.file_mode == "corrupt":
                target.write_bytes(b"not an image")
            elif self.file_mode == "truncated":
                Image.new("RGB", (144, 192), "orange").save(target, format="JPEG")
                target.write_bytes(target.read_bytes()[:-10])
            if self.file_mode in {"new", "outside"}:
                size = (144, 192) if snapshot["aspect"] == "3:4" else (108, 192)
                Image.new("RGB", size, (100 + len(self.calls), snapshot["slide"]["number"] * 60, 200)).save(target)
            return json.dumps({"file": name, "summary": "模拟生成完成"})
        if self.review_mode == "error":
            raise BridgeError("模拟核对不可用")
        if self.review_mode == "malformed":
            return json.dumps({"summary": "缺少逐项核对"})
        slide = snapshot["slide"]
        result = {"summary": "四项核对完成", "observed_title_lines": slide["title_lines"],
                  "observed_body": slide["body"], "checks": [
                      {"id": key, "status": "pass", "note": "已比对"}
                      for key in ("subject", "evidence", "text", "layout")], "issues": []}
        if self.review_mode == "wrong_words":
            result["observed_body"] = "Different visible words."
        return json.dumps(result, ensure_ascii=False)


class ImageBench(StillBench, CarouselImagesMixin, CarouselDeliveryMixin):
    def __init__(self, root, generator):
        super().__init__(root, Searcher())
        self.image_generator = generator


class ReviewLimitationTests(unittest.TestCase):
    def review(self, *, evidence="pass", has_reference=True, scope=None, generated=True, malformed=False):
        slide = {"title_lines": ["Read this title"], "body": ""}
        answer = {"summary": "已核对", "observed_title_lines": slide["title_lines"], "observed_body": "",
                  "checks": [{"id": key, "status": evidence if key == "evidence" else "pass", "note": "已查看",
                              "limitation_code": "generated_visual_fidelity"}
                             for key in ("visual", "text", "product", "evidence")], "issues": []}
        return normalize_review("invalid-json" if malformed else json.dumps(answer), slide, (1080, 1440), "3:4",
                                requirements={"mode": "generated_visual" if generated else "layout_only"},
                                scope=scope, has_reference=has_reference)

    def test_only_a_generated_evidence_pass_receives_the_machine_limitation(self):
        review = self.review()
        evidence = next(check for check in review["checks"] if check["id"] == "evidence")
        self.assertEqual(review["status"], "uncertain")
        self.assertEqual(evidence["status"], "uncertain")
        self.assertEqual(evidence["limitation_code"], "generated_visual_fidelity")
        self.assertEqual(review["issues"], [])
        self.assertTrue(all("limitation_code" not in check for check in review["checks"] if check["id"] != "evidence"))

    def test_model_claims_cannot_mark_uncertainty_missing_reference_or_unchecked_evidence_as_a_fixed_limitation(self):
        for options in ({"evidence": "uncertain"}, {"evidence": "fail"}, {"has_reference": False},
                        {"scope": ["visual", "text", "product"]}, {"generated": False}, {"malformed": True}):
            with self.subTest(options=options):
                review = self.review(**options)
                self.assertTrue(all("limitation_code" not in check for check in review["checks"]))


class CarouselImageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="carousel-images-")
        self.addCleanup(temp.cleanup)
        mcp = Path(temp.name) / "ResolveMCP.exe"
        mcp.write_bytes(b"")
        environment = patch.dict(os.environ, {"RESOLVE_MCP": str(mcp)})
        environment.start()
        self.addCleanup(environment.stop)
        self.generator = ImageGenerator()
        self.bench = ImageBench(temp.name, self.generator)
        self.addCleanup(self.stop)
        self.item = self.carousel()
        self.originals = {}
        for number in (1, 2, 3):
            self.pick_new_base(number)
        self.words = [(slide["title_lines"], slide["body"]) for slide in self.current()["slides"]]
        self.script_before = copy.deepcopy(self.bench.creation.public()["scripts"])

    def stop(self):
        self.bench.stop_images()
        self.generator.release.set()
        self.wait()

    def carousel(self):
        found = parse_carousels(CANDIDATE)[0]
        source = {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 1,
                  "product": "202H", "case_id": None, "candidate_text": CANDIDATE,
                  "candidate_sha256": "x", "snapshot": {}}
        script = self.bench.creation.mutate("script", {"request_id": uuid4().hex,
            "revision": self.bench.creation.public()["revision"], "script": {"product": "202H",
            "direction_id": "202H-S2", "title": "送礼 · 图文", "text": carousel_text(found), "confirmed": True}}, source)["script"]
        return self.bench.create_carousel(self.body(carousel={"script_id": script["id"]},
            requirements={"mode": "photo_preserving"}))["carousel"]

    def body(self, **values):
        return {"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"], **values}

    def current(self):
        return self.bench.production.carousel(self.item["id"])

    def pick_new_base(self, number):
        relative = f"202H-S2/原图/{number}-{uuid4().hex[:8]}.png"
        path = self.bench.carousel_image_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (108, 192), (number * 40, 41 + len(self.originals), 42)).save(path)
        self.originals[path] = path.read_bytes()
        candidate = self.bench.import_folder_image(self.item["id"], self.body(slide=number, path=relative))["candidate"]
        self.bench.choose_base(self.item["id"], self.body(slide=number, candidate=candidate["id"]))
        return candidate

    def start(self, **values):
        return self.bench.start_image_generation(self.item["id"], self.body(**values))["generation"]

    def wait(self):
        deadline = time.monotonic() + 6
        while self.bench.image_running() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.bench.image_running(), "fake image worker did not finish")
        return self.current()

    def generate(self, **values):
        self.start(**values)
        return self.wait()

    def complete_group(self, **values):
        """Confirm the first slide, then make the rest so they take its style (optional, never required)."""
        self.generate(slides=[1], **values)
        self.approve(1)
        self.assertTrue(self.bench.carousel_first_preflight(self.item["id"])["ready"])
        return self.generate(slides=[2, 3], **values)

    def recheck(self, number, output_id=None):
        slide = self.current()["slides"][number - 1]
        output = next(entry for entry in slide["outputs"] if entry["id"] == (output_id or slide["output_id"]))
        self.bench.start_carousel_review(self.item["id"], self.body(slide=number, output_id=output["id"]))
        self.wait()
        return next(entry for entry in self.current()["slides"][number - 1]["outputs"] if entry["id"] == output["id"])

    def approve(self, number, output_id=None, **values):
        slide = self.current()["slides"][number - 1]
        return self.bench.approve_carousel_output(self.item["id"], self.body(slide=number,
            output_id=output_id or slide["outputs"][-1]["id"], approved=True, **values))["carousel"]

    def approve_all(self):
        for number in (1, 2, 3):
            self.approve(number)
        return self.current()

    def assert_error(self, status, function, *args, **kwargs):
        with self.assertRaises(VoiceError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.status, status)

    def change_notes(self, number):
        slides = [{key: slide[key] for key in ("number", "role", "source", "forbidden")}
                  for slide in self.current()["slides"]]
        slides[number - 1]["source"] += "；保留原实拍边缘"
        return self.bench.save_carousel_slides(self.item["id"], self.body(slides=slides))["carousel"]

    def test_generate_all_makes_every_slide_at_once_and_waits_for_human_confirmation(self):
        item = self.generate()
        run = item["generations"][-1]
        self.assertEqual((run["status"], run["added"], run["slides"], run["first_gate"]), ("completed", 3, [1, 2, 3], False))
        self.assertEqual([len(slide["outputs"]) for slide in item["slides"]], [1, 1, 1])
        self.assertEqual(len(self.generator.calls), 6)
        for number in (1, 2, 3):
            generation = next(call for call in self.generator.calls if not call["read_only"] and call["snapshot"]["slide"]["number"] == number)
            review = next(call for call in self.generator.calls if call["read_only"] and call["snapshot"]["slide"]["number"] == number)
            self.assertNotEqual(generation["cwd"], review["cwd"])
            # Nothing is confirmed yet, so no slide borrows another slide's style.
            self.assertEqual((len(generation["images"]), len(review["images"])), (1, 2))
            self.assertNotIn("style_reference", generation["snapshot"])
            self.assertEqual(generation["schema"]["required"], ["file", "summary"])
            self.assertIn('<skill name="tiktok-photo-commerce">\n---\nname: tiktok-photo-commerce', generation["prompt"])
            self.assertIn("observed_title_lines", review["schema"]["required"])
        for slide in item["slides"]:
            output = slide["outputs"][0]
            self.assertEqual((slide["output_id"], output["review"]["status"]), (output["id"], "pass"))
            self.assertFalse(output["approved"])
            self.assertEqual(output["review"]["file_sha256"], output["sha256"])
            for size in ("full", "thumb"):
                self.assertTrue(self.bench.carousel_output(item["id"], output["id"], size).is_file())
        self.assertEqual(self.approve_all()["status"], "approved")
        self.assertEqual([(slide["title_lines"], slide["body"]) for slide in self.current()["slides"]], self.words)
        self.assertEqual(self.bench.creation.public()["scripts"], self.script_before)
        for path, original in self.originals.items():
            self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.bench.runner.calls, [])

    def test_redo_preserves_versions_and_other_slides_and_can_select_matching_old_version(self):
        self.complete_group()
        before = self.approve_all()
        item = self.generate(slides=[2])
        self.assertEqual(item["slides"][0], before["slides"][0])
        self.assertEqual(item["slides"][2], before["slides"][2])
        slide = item["slides"][1]
        self.assertEqual(len(slide["outputs"]), 2)
        self.assertEqual(slide["output_id"], before["slides"][1]["output_id"])
        self.assertTrue(slide["outputs"][0]["approved"])
        self.assertFalse(slide["outputs"][-1]["approved"])
        old = before["slides"][1]["output_id"]
        restored = self.approve(2, old)
        self.assertEqual((restored["slides"][1]["output_id"], restored["status"]), (old, "approved"))
        self.assertTrue(self.bench.carousel_output(item["id"], old, "full").is_file())

    def test_failed_redo_keeps_current_confirmed_image(self):
        self.generate(slides=[1])
        before = self.approve(1)["slides"][0]
        self.generator.file_mode = "error"
        item = self.generate(slides=[1])
        self.assertEqual(item["generations"][-1]["status"], "failed")
        phase = item["generations"][-1]["stages"]["1"]["generation"]
        self.assertEqual(phase["status"], "failed")
        self.assertIn("模拟生成失败", phase["note"])
        self.assertEqual(item["slides"][0], before)
        self.generator.file_mode = "new"
        retry = self.generate(slides=[1])
        self.assertEqual((retry["generations"][-1]["status"], len(retry["slides"][0]["outputs"])), ("completed", 2))

    def test_changing_base_and_making_notes_invalidates_only_affected_slide(self):
        self.complete_group()
        before = self.approve_all()
        self.pick_new_base(1)
        item = self.change_notes(2)
        live = self.bench.carousel_images_live(copy.deepcopy(item))
        for number in (1, 2):
            slide = live["slides"][number - 1]
            self.assertTrue(slide["outputs"][0]["stale"])
            self.assertFalse(slide["outputs"][0]["approved"])
            self.assert_error(409, self.approve, number, before["slides"][number - 1]["output_id"])
        self.assertEqual(item["slides"][2], before["slides"][2])
        self.assertEqual([len(slide["outputs"]) for slide in item["slides"]], [1, 1, 1])

    def test_input_change_during_generation_retains_history_without_selecting_late_output(self):
        self.generate(slides=[1])
        old = self.approve(1)["slides"][0]["output_id"]
        self.generator.block_stage = "generation"
        self.start(slides=[1])
        self.assertTrue(self.generator.entered.wait(2))
        self.pick_new_base(1)
        changed = self.change_notes(1)["slides"][0]
        self.generator.release.set()
        item = self.wait()
        slide = item["slides"][0]
        late = slide["outputs"][-1]
        self.assertEqual((slide["base"], slide["source"], slide["output_id"]),
                         (changed["base"], changed["source"], old))
        self.assertEqual((len(slide["outputs"]), late["input_changed"], late["approved"]), (2, True, False))
        self.assert_error(409, self.approve, 1, late["id"], acknowledge=True)

    def test_word_change_during_generation_does_not_bind_result_to_changed_script(self):
        self.generator.block_stage = "generation"
        self.start(slides=[1])
        self.assertTrue(self.generator.entered.wait(2))
        # Model a concurrent adopted-word/version change only in this disposable catalog.
        with self.bench.lock:
            data = self.bench.production.read()
            carousel = data["carousels"][0]
            carousel["script_version"] += 1
            carousel["slides"][0]["title_lines"] = ["Changed adopted title"]
            self.bench.production.write(data)
        self.generator.release.set()
        slide = self.wait()["slides"][0]
        self.assertIsNone(slide["output_id"])
        self.assertEqual(slide["title_lines"], ["Changed adopted title"])
        self.assertTrue(slide["outputs"][0]["input_changed"])
        self.assert_error(409, self.approve, 1, slide["outputs"][0]["id"])

    def test_aspect_switch_invalidates_entire_old_group_without_deleting_versions(self):
        self.complete_group()
        before = self.approve_all()
        item = self.generate(slides=[1], aspect="9:16")
        self.assertEqual(item["image_aspect"], "9:16")
        self.assertEqual([len(slide["outputs"]) for slide in item["slides"]], [2, 1, 1])
        self.assertFalse(any(output["approved"] for slide in item["slides"] for output in slide["outputs"]))
        self.assertEqual(item["slides"][0]["outputs"][-1]["review"]["status"], "pass")
        self.approve(1, item["slides"][0]["outputs"][-1]["id"])
        self.assert_error(409, self.approve, 2, before["slides"][1]["output_id"])
        self.assertNotEqual(self.current()["status"], "approved")

    def test_different_carousels_generate_side_by_side_but_one_carousel_runs_one_job(self):
        self.generator.block_stage = "generation"
        self.start(slides=[1])
        self.assertTrue(self.generator.entered.wait(2))
        self.assert_error(409, self.start, slides=[2])
        other = self.carousel()
        relative = f"202H-S2/原图/other-{uuid4().hex[:8]}.png"
        path = self.bench.carousel_image_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (108, 192), (10, 20, 30)).save(path)
        candidate = self.bench.import_folder_image(other["id"], self.body(slide=1, path=relative))["candidate"]
        self.bench.choose_base(other["id"], self.body(slide=1, candidate=candidate["id"]))
        self.bench.start_image_generation(other["id"], self.body(slides=[1]))
        deadline = time.monotonic() + 3
        while sum(not call["read_only"] for call in self.generator.calls) < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(sum(not call["read_only"] for call in self.generator.calls), 2)
        self.generator.block_stage = None
        self.generator.release.set()
        self.wait()
        for identifier in (self.item["id"], other["id"]):
            self.assertEqual(self.bench.production.carousel(identifier)["generations"][-1]["status"], "completed")

    def test_cancel_is_owned_by_carousel_and_a_cancelled_job_can_be_retried(self):
        self.generator.block_stage = "generation"
        generation = self.start(slides=[1])
        self.assertTrue(self.generator.entered.wait(2))
        self.assert_error(409, self.bench.cancel_image_generation, "CAR-ffffffffffff", generation["id"])
        self.assertTrue(self.bench.image_running())
        self.bench.cancel_image_generation(self.item["id"], generation["id"])
        item = self.wait()
        self.assertEqual((item["generations"][-1]["status"], item["slides"][0]["outputs"]), ("cancelled", []))
        self.generator.block_stage = None
        self.assertEqual(self.generate(slides=[1])["generations"][-1]["status"], "completed")

    def test_stop_during_review_preserves_unconfirmed_image_with_warning(self):
        self.generator.block_stage = "review"
        self.start(slides=[1])
        self.assertTrue(self.generator.entered.wait(2))
        self.bench.stop_images()
        item = self.wait()
        self.assertEqual(item["generations"][-1]["status"], "cancelled")
        output = item["slides"][0]["outputs"][0]
        self.assertEqual((output["approved"], output["review"]["status"]), (False, "review_failed"))
        self.assertTrue(self.approve(1)["slides"][0]["outputs"][0]["approved"])
        self.generator.block_stage = None
        self.recheck(1)
        self.approve(1)

    def test_restart_interrupts_job_and_late_worker_cannot_resurrect_or_select_output(self):
        self.generate(slides=[1])
        before = self.approve(1)["slides"][0]
        self.generator.block_stage = "generation"
        generation = self.start(slides=[1])
        self.assertTrue(self.generator.entered.wait(2))
        restarted = ProductionStore(self.bench.production.root.parent, self.bench.lock)
        restarted.interrupt_running()
        interrupted = restarted.carousel(self.item["id"])["generations"][-1]
        self.assertEqual(interrupted["status"], "interrupted")
        self.generator.release.set()
        item = self.wait()
        self.assertEqual(item["generations"][-1], interrupted)
        self.assertEqual(item["slides"][0], before)
        self.generator.block_stage = None
        retry = self.generate(slides=[1])
        self.assertNotEqual(retry["generations"][-1]["id"], generation["id"])
        self.assertEqual((retry["generations"][-1]["status"], len(retry["slides"][0]["outputs"])), ("completed", 2))

    def test_concurrent_identical_request_replays_one_worker_and_changed_replay_is_rejected(self):
        self.generator.block_stage = "generation"
        body = self.body(slides=[1])
        barrier = threading.Barrier(3)
        results, errors = [], []
        def start():
            try:
                barrier.wait()
                results.append(self.bench.start_image_generation(self.item["id"], body))
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["generation"]["id"], results[1]["generation"]["id"])
        self.assert_error(409, self.bench.start_image_generation, self.item["id"], {**body, "slides": [2]})
        self.assert_error(409, self.bench.start_image_generation, self.item["id"], self.body(slides=[2]))
        self.generator.release.set()
        item = self.wait()
        replay = self.bench.start_image_generation(self.item["id"], body)
        self.assertEqual(replay["generation"]["id"], results[0]["generation"]["id"])
        self.assertEqual((len(item["generations"]), len(self.generator.calls)), (1, 2))

    def test_invalid_or_missing_generated_file_never_replaces_original_or_prior_output(self):
        self.generate(slides=[1])
        before = self.approve(1)["slides"][0]
        for mode in ("outside", "base", "copy", "missing", "corrupt", "truncated"):
            with self.subTest(mode=mode):
                self.generator.file_mode = mode
                item = self.generate(slides=[1])
                self.assertEqual(item["generations"][-1]["status"], "failed")
                phases = item["generations"][-1]["stages"]["1"]
                self.assertEqual(phases["generation"]["status"], "failed")
                self.assertEqual(phases["layout"]["status"], "pending")
                self.assertEqual(phases["review"]["status"], "pending")
                self.assertEqual(item["slides"][0], before)
        for path, original in self.originals.items():
            self.assertEqual(path.read_bytes(), original)

    def test_malformed_generation_manifest_fails_before_completing_generation(self):
        for answer in ("{", "[]", "null", "{}", '{"summary":"未生成图片"}',
                       '{"file":null,"summary":"未生成图片"}', '{"file":[],"summary":"未生成图片"}',
                       '{"file":"","summary":"未生成图片"}', '{"file":"render.png","summary":null}'):
            with self.subTest(answer=answer), patch.object(self.generator, "run", return_value=answer) as generator:
                item = self.generate(slides=[1])
                run = item["generations"][-1]
                self.assertEqual((run["status"], run["added"]), ("failed", 0))
                self.assertEqual(run["stages"]["1"]["generation"]["status"], "failed")
                self.assertEqual(run["stages"]["1"]["review"]["status"], "pending")
                self.assertEqual(item["slides"][0]["outputs"], [])
                self.assertEqual(generator.call_count, 1)

    def test_generation_without_image_preserves_backend_failure_summary(self):
        summary = "当前会话无法调用 imagegen，未生成 render.png；请检查生图工具配置。"
        answer = json.dumps({"file": "render.png", "summary": summary}, ensure_ascii=False)
        with patch.object(self.generator, "run", return_value=answer) as generator:
            item = self.generate(slides=[1])
        run = item["generations"][-1]
        self.assertEqual((run["status"], run["added"]), ("failed", 0))
        phase = run["stages"]["1"]["generation"]
        self.assertEqual(phase["status"], "failed")
        self.assertIn(summary, phase["note"])
        self.assertTrue(any(summary in problem for problem in run["problems"]))
        self.assertEqual(item["slides"][0]["outputs"], [])
        self.assertEqual(generator.call_count, 1)

    def test_generation_completes_only_after_valid_image_is_saved_independent_of_summary(self):
        generate, stage = self.generator.run, self.bench._stage
        summary = "第一次生成失败后已重试，本次图片已保存。"
        completed_outputs = []

        def answer_with_summary(*args, **options):
            answer = generate(*args, **options)
            if not options.get("read_only"):
                manifest = json.loads(answer)
                manifest["summary"] = summary
                return json.dumps(manifest, ensure_ascii=False)
            return answer

        def record_stage(identifier, generation_id, number, phase, status, **values):
            if phase == "generation" and status == "completed":
                output = self.current()["slides"][number - 1]["outputs"][-1]
                self.assertEqual(output["generation_id"], generation_id)
                for size in ("full", "thumb"):
                    with Image.open(self.bench.carousel_output(identifier, output["id"], size)) as image:
                        image.load()
                completed_outputs.append(output["id"])
            return stage(identifier, generation_id, number, phase, status, **values)

        with patch.object(self.generator, "run", side_effect=answer_with_summary), patch.object(self.bench, "_stage", side_effect=record_stage):
            item = self.generate(slides=[1])
        run = item["generations"][-1]
        self.assertEqual((run["status"], run["added"]), ("completed", 1))
        self.assertEqual(run["stages"]["1"]["generation"]["status"], "completed")
        self.assertEqual(completed_outputs, [item["slides"][0]["outputs"][0]["id"]])
        self.assertEqual(item["slides"][0]["outputs"][0]["summary"], summary)

    def test_legacy_review_failure_retains_candidate_and_allows_manual_confirmation(self):
        for mode in ("error", "malformed"):
            with self.subTest(mode=mode):
                self.generator.review_mode = mode
                item = self.generate(slides=[1])
                output = item["slides"][0]["outputs"][-1]
                self.assertEqual((item["generations"][-1]["status"], output["review"]["status"], output["approved"]),
                                 ("completed", "review_failed", False))
                self.assertTrue(self.approve(1, output["id"])["slides"][0]["outputs"][-1]["approved"])
                self.generator.review_mode = "pass"
                before_calls = len(self.generator.calls)
                retry = self.recheck(1, output["id"])
                self.assertEqual(len(self.generator.calls), before_calls + 1)
                self.assertTrue(self.generator.calls[-1]["read_only"])
                self.assertEqual(retry["sha256"], output["sha256"])
                self.assertTrue(self.approve(1, output["id"])["slides"][0]["outputs"][-1]["approved"])

    def test_legacy_review_findings_do_not_block_manual_confirmation(self):
        self.generator.review_mode = "wrong_words"
        item = self.generate(slides=[1])
        review = item["slides"][0]["outputs"][0]["review"]
        self.assertEqual(review["status"], "needs_fix")
        self.assertEqual(next(check for check in review["checks"] if check["id"] == "text")["status"], "fail")
        self.assertTrue(self.approve(1)["slides"][0]["outputs"][0]["approved"])
        self.assertEqual([(slide["title_lines"], slide["body"]) for slide in item["slides"]], self.words)

    def test_changed_base_or_output_bytes_cannot_be_confirmed(self):
        self.complete_group()
        self.approve(1)
        slide = self.current()["slides"][0]
        path = self.bench.carousel_image(self.item["id"], slide["base"]["id"], "full")
        Image.new("RGB", (108, 192), "red").save(path)
        live = self.bench.carousel_images_live(copy.deepcopy(self.current()))
        self.assertEqual((live["slides"][0]["outputs"][0]["stale"], live["slides"][0]["outputs"][0]["approved"]), (True, False))
        self.assert_error(409, self.approve, 1)
        output = self.current()["slides"][1]["outputs"][0]
        path = self.bench.carousel_output(self.item["id"], output["id"], "full")
        Image.new("RGB", (144, 192), "blue").save(path)
        self.assert_error(409, self.approve, 2)

    def test_remaining_slides_have_separate_product_references_and_two_active_workers(self):
        self.generate(slides=[1])
        self.approve(1)
        original_run = self.generator.run
        barrier, guard = threading.Barrier(2), threading.Lock()
        active, peak = 0, 0

        def run(prompt, **options):
            nonlocal active, peak
            generating = not options.get("read_only")
            if generating:
                with guard:
                    active += 1
                    peak = max(peak, active)
                barrier.wait(2)
            try:
                return original_run(prompt, **options)
            finally:
                if generating:
                    with guard:
                        active -= 1

        self.generator.run = run
        item = self.generate(slides=[2, 3])
        self.assertEqual(peak, 2)
        run_record = item["generations"][-1]
        self.assertEqual(run_record["concurrency_limit"], MAX_CONCURRENCY)
        self.assertEqual(run_record["status"], "completed")
        jobs = [call for call in self.generator.calls if not call["read_only"] and call["snapshot"]["slide"]["number"] > 1]
        self.assertEqual(len({job["cwd"] for job in jobs}), 2)
        self.assertNotEqual(jobs[0]["snapshot"]["base_sha256"], jobs[1]["snapshot"]["base_sha256"])
        self.assertEqual(jobs[0]["snapshot"]["style_reference"]["sha256"], jobs[1]["snapshot"]["style_reference"]["sha256"])
        for number in (2, 3):
            phases = run_record["stages"][str(number)]
            self.assertTrue(phases["generation"]["started_at"])
            self.assertTrue(phases["generation"]["finished_at"])
            self.assertEqual(phases["review"]["status"], "completed")
            self.assertEqual(phases["confirmation"]["status"], "pending")
            self.assertEqual(phases["package"]["status"], "pending")

    def test_failed_new_review_keeps_confirmed_old_selection(self):
        self.generate(slides=[1])
        old = self.approve(1)["slides"][0]
        self.generator.review_mode = "error"
        item = self.generate(slides=[1], redo=True)
        slide = item["slides"][0]
        self.assertEqual(slide["output_id"], old["output_id"])
        self.assertTrue(slide["outputs"][0]["approved"])
        candidate = slide["outputs"][-1]
        self.assertEqual(candidate["review"]["status"], "review_failed")
        self.assertTrue(self.approve(1, candidate["id"])["slides"][0]["outputs"][-1]["approved"])
        self.generator.review_mode = "pass"
        retry = self.recheck(1, candidate["id"])
        self.assertEqual(retry["sha256"], candidate["sha256"])
        self.assertEqual(self.current()["slides"][0]["output_id"], candidate["id"])

    def test_partial_legacy_review_does_not_block_manual_confirmation_and_retries_are_bound(self):
        self.generate(slides=[1])
        output = self.current()["slides"][0]["outputs"][0]
        self.bench.start_carousel_review(self.item["id"], self.body(slide=1, output_id=output["id"], scope=["text"]))
        item = self.wait()
        reviewed = item["slides"][0]["outputs"][0]
        self.assertEqual(reviewed["review"]["scope"], ["text"])
        self.assertTrue(self.approve(1)["slides"][0]["outputs"][0]["approved"])
        completed = self.recheck(1)
        self.assertEqual(completed["review"]["file_sha256"], completed["sha256"])
        self.assertEqual(completed["review"]["input_signature"], completed["input_signature"])
        self.approve(1)

    def test_requirements_authorize_hands_and_generated_evidence_is_not_pixel_fidelity(self):
        item = self.generate(slides=[1], requirements={"mode": "generated_visual", "hands_authorized": True,
            "editable_regions": ["hands", "background", "text"], "style": "多毛粗臂，完整画面"}, instruction="保留完整生成图")
        output = item["slides"][0]["outputs"][0]
        generation, review = self.generator.calls[-2:]
        self.assertEqual(generation["snapshot"]["requirements"], review["snapshot"]["requirements"])
        self.assertTrue(generation["snapshot"]["requirements"]["hands_authorized"])
        self.assertIn("本张必须改手：按「", generation["prompt"])
        self.assertIn("获准改变的手", review["prompt"])
        self.assertEqual(output["review"]["status"], "uncertain")
        evidence = next(check for check in output["review"]["checks"] if check["id"] == "evidence")
        self.assertIn("不能证明原实拍逐像素保真", evidence["note"])
        self.assertIsNone(output["model"])
        self.approve(1, acknowledge=True)

    def test_layout_only_does_not_call_generation_and_preserves_uncovered_pixels(self):
        item = self.generate(slides=[1], aspect="9:16", requirements={"mode": "layout_only", "caption_area": "top"})
        self.assertEqual(len(self.generator.calls), 1)
        self.assertTrue(self.generator.calls[0]["read_only"])
        output = item["slides"][0]["outputs"][0]
        self.assertEqual(output["origin"], "local_layout")
        self.assertEqual(item["generations"][-1]["stages"]["1"]["generation"]["status"], "skipped")
        with Image.open(self.bench.carousel_image(item["id"], item["slides"][0]["base"]["id"], "full")) as base:
            with Image.open(self.bench.carousel_output(item["id"], output["id"], "full")) as result:
                self.assertEqual(result.size, base.size)
                self.assertEqual(result.crop((0, 100, 108, 192)).tobytes(), base.crop((0, 100, 108, 192)).tobytes())

    def test_rugged_caption_preset_uses_local_layout_and_same_review_contract(self):
        # A realistic phone frame leaves room for the exact adopted words.
        slide = self.current()["slides"][0]
        base = self.bench.carousel_image(self.item["id"], slide["base"]["id"], "full")
        Image.new("RGB", (720, 960), (42, 63, 77)).save(base)
        item = self.generate(slides=[1], aspect="3:4", requirements={"mode": "layout_only", "caption_area": "top", "caption_style": "rugged"})
        self.assertEqual(len(self.generator.calls), 1)
        self.assertTrue(self.generator.calls[0]["read_only"])
        output = item["slides"][0]["outputs"][0]
        self.assertEqual(output["requirements_snapshot"]["caption_style"], "rugged")
        self.assertEqual(self.generator.calls[0]["snapshot"]["requirements"], output["requirements_snapshot"])
        self.assertEqual(output["layout_receipt"]["caption_style"], "rugged")
        self.assertEqual(output["layout_receipt"]["title_lines"], slide["title_lines"])
        self.assertEqual(output["layout_receipt"]["body"], slide["body"])
        self.assertEqual(output["review"]["status"], "pass")
        with Image.open(base) as source, Image.open(self.bench.carousel_output(item["id"], output["id"], "full")) as rendered:
            self.assertEqual(source.size, rendered.size)
            self.assertEqual(source.crop((0, 288, 720, 960)).tobytes(), rendered.crop((0, 288, 720, 960)).tobytes())

    def test_interrupted_review_retry_uses_same_candidate_and_late_results_stay_history(self):
        self.generate(slides=[1])
        output = self.current()["slides"][0]["outputs"][0]
        self.generator.block_stage = "review"
        self.bench.start_carousel_review(self.item["id"], self.body(slide=1, output_id=output["id"]))
        self.assertTrue(self.generator.entered.wait(2))
        before = self.current()["slides"][0]["outputs"][0]["latest_review_id"]
        self.change_notes(1)
        self.generator.release.set()
        item = self.wait()
        late = item["slides"][0]["outputs"][0]["review_runs"][-1]
        self.assertEqual(late["id"], before)
        self.assertTrue(late["stale"])
        self.assertEqual(len(item["slides"][0]["outputs"]), 1)
        self.assert_error(409, self.approve, 1, acknowledge=True)

    def test_resume_after_review_cancel_does_not_regenerate_saved_candidate(self):
        self.generator.block_stage = "review"
        self.start(slides=[1])
        self.assertTrue(self.generator.entered.wait(2))
        self.bench.stop_images()
        saved = self.wait()["slides"][0]["outputs"][0]
        self.generator.block_stage = None
        item = self.generate(slides=[1])
        slide = item["slides"][0]
        self.assertEqual(len(slide["outputs"]), 1)
        self.assertEqual(slide["outputs"][0]["sha256"], saved["sha256"])
        self.assertEqual(slide["outputs"][0]["review"]["status"], "pass")
        self.assertEqual(sum(not call["read_only"] for call in self.generator.calls), 1)
        self.assertEqual(item["generations"][-1]["stages"]["1"]["generation"]["status"], "skipped")

    def test_restart_during_standalone_review_rejects_late_completion_and_can_recheck(self):
        self.generate(slides=[1])
        output = self.current()["slides"][0]["outputs"][0]
        self.generator.block_stage = "review"
        started = self.bench.start_carousel_review(self.item["id"], self.body(slide=1, output_id=output["id"]))
        self.assertTrue(self.generator.entered.wait(2))
        restarted = ProductionStore(self.bench.production.root.parent, self.bench.lock)
        restarted.interrupt_running()
        self.generator.release.set()
        item = self.wait()
        candidate = item["slides"][0]["outputs"][0]
        self.assertEqual(candidate["latest_review_id"], started["review"]["id"])
        self.assertEqual(candidate["review_runs"][-1]["status"], "interrupted")
        self.assertEqual(candidate["review"]["execution_status"], "interrupted")
        self.assertTrue(self.approve(1)["slides"][0]["outputs"][0]["approved"])
        self.generator.block_stage = None
        retried = self.recheck(1)
        self.assertEqual(retried["sha256"], output["sha256"])
        self.assertEqual(retried["review"]["status"], "pass")

    def test_generated_delivery_contains_only_images_with_original_bytes(self):
        self.complete_group()
        self.approve_all()
        record = self.bench.create_carousel_delivery(self.item["id"], self.body())["delivery"]
        with zipfile.ZipFile(self.bench.carousel_delivery_file(self.item["id"], record["id"])) as archive:
            self.assertEqual(archive.namelist(), ["01.png", "02.png", "03.png"])
            for number in (1, 2, 3):
                output = self.current()["slides"][number - 1]["outputs"][0]
                source = self.bench.carousel_output(self.item["id"], output["id"], "full")
                self.assertEqual(archive.read(f"{number:02d}.png"), source.read_bytes())

    def use_direct_images(self):
        self.generator.direct_image_edit = True
        self.generator.review_available = False
        self.generator.source = "cockpit_image_edit"

    def test_direct_edit_saves_candidate_without_review_or_agent_skill_dependency(self):
        self.use_direct_images()
        self.bench.edit_skill_dir = Path("does-not-exist")
        self.generator.review_mode = "error"
        item = self.generate(slides=[1])
        output = item["slides"][0]["outputs"][0]
        self.assertEqual(len(self.generator.calls), 1)
        self.assertFalse(self.generator.calls[0]["read_only"])
        self.assertNotIn("review", output)
        self.assertNotIn("review_runs", output)
        self.assertEqual(output["origin"], "cockpit_image_edit")
        self.assertTrue(output["direct_image_edit"])
        self.assertFalse(output["approved"])
        self.assertEqual(item["generations"][-1]["stages"]["1"]["review"]["status"], "skipped")
        self.assertNotIn("内置 imagegen", self.generator.calls[0]["prompt"])
        self.assertNotIn("input.json", self.generator.calls[0]["prompt"])
        self.assertFalse((self.bench._carousel_dir(item["id"]) / "reviews").exists())
        self.assertTrue(self.approve(1)["slides"][0]["outputs"][0]["approved"])
        self.assert_error(409, self.recheck, 1)

    def test_direct_edit_can_be_checked_by_an_independent_read_only_reviewer(self):
        self.use_direct_images()
        reviewer = self.bench.image_reviewer = ImageGenerator()
        item = self.generate(slides=[1])
        output = item["slides"][0]["outputs"][0]
        before = self.bench.carousel_output(self.item["id"], output["id"], "full").read_bytes()
        self.assertEqual(len(reviewer.calls), 0, "direct generation must still save without implicit review")
        self.assertTrue(self.bench.image_status()["review_available"])

        checked = self.recheck(1)

        self.assertEqual(len(self.generator.calls), 1)
        self.assertEqual(len(reviewer.calls), 1)
        call = reviewer.calls[0]
        self.assertTrue(call["read_only"])
        self.assertIn("observed_title_lines", call["schema"]["required"])
        self.assertEqual(len(call["images"]), 2)
        self.assertEqual(call["snapshot"]["reference_bindings"][0]["role"], "product_reference")
        self.assertEqual(checked["review"]["status"], "pass")
        self.assertEqual(checked["review"]["file_sha256"], output["sha256"])
        self.assertEqual(checked["review"]["input_signature"], output["input_signature"])
        self.assertFalse(checked["approved"])
        self.assertEqual(self.bench.carousel_output(self.item["id"], output["id"], "full").read_bytes(), before)

    def test_review_availability_is_independent_of_generation_availability(self):
        self.use_direct_images()
        self.generate(slides=[1])
        self.generator.status = lambda: {"available": False, "review_available": False, "label": "生图离线"}
        reviewer = self.bench.image_reviewer = ImageGenerator()
        status = self.bench.image_status()
        self.assertFalse(status["available"])
        self.assertTrue(status["review_available"])
        self.assertEqual(self.recheck(1)["review"]["status"], "pass")
        self.assertEqual(len(reviewer.calls), 1)

        self.generator.status = lambda: {"available": True, "review_available": False, "label": "生图在线"}
        reviewer.status = lambda: {"available": False, "label": "核对离线", "error": "模拟核对未连接"}
        status = self.bench.image_status()
        self.assertTrue(status["available"])
        self.assertFalse(status["review_available"])
        self.assertEqual(status["review_error"], "模拟核对未连接")
        self.assert_error(503, self.recheck, 1)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(len(self.current()["slides"][0]["outputs"]), 1)

    def test_independent_review_does_not_start_a_duplicate_for_the_same_output(self):
        self.use_direct_images()
        self.generate(slides=[1])
        reviewer = self.bench.image_reviewer = ImageGenerator()
        reviewer.block_stage = "review"
        output = self.current()["slides"][0]["outputs"][0]
        body = self.body(slide=1, output_id=output["id"])
        try:
            started = self.bench.start_carousel_review(self.item["id"], body)
            self.assertTrue(reviewer.entered.wait(2))
            replay = self.bench.start_carousel_review(self.item["id"], body)
            self.assertEqual(replay["review"]["id"], started["review"]["id"])
            self.assert_error(409, self.bench.start_carousel_review, self.item["id"],
                              self.body(slide=1, output_id=output["id"]))
            self.assertEqual(len(reviewer.calls), 1)
        finally:
            reviewer.release.set()
            self.wait()

    def test_legacy_review_bridge_remains_available_without_an_independent_reviewer(self):
        self.assertTrue(self.bench.image_status()["review_available"])
        self.use_direct_images()
        self.assertFalse(self.bench.image_status()["review_available"])

    def latest_picks(self):
        return [{"slide": slide["number"], "output_id": slide["outputs"][-1]["id"]} for slide in self.current()["slides"]]

    def test_approve_all_confirms_every_slide_in_one_save(self):
        self.generate()
        before = self.bench.production.public()["revision"]
        result = self.bench.approve_carousel_outputs(self.item["id"], self.body(selections=self.latest_picks()))
        self.assertEqual(result["approved"], [1, 2, 3])
        item = self.current()
        self.assertEqual(item["status"], "approved")
        for slide in item["slides"]:
            output = slide["outputs"][-1]
            self.assertEqual((slide["output_id"], output["approved"], output["approval_actor"]), (output["id"], True, "human"))
        self.assertEqual(sum(entry["event"] == "approval" for entry in item["history"]), 3)
        self.assertNotEqual(self.bench.production.public()["revision"], before)
        self.assert_error(409, self.bench.approve_carousel_outputs, self.item["id"],
                          {"request_id": uuid4().hex, "revision": before, "selections": self.latest_picks()})

    def test_approve_all_confirms_nothing_when_one_image_changed_or_picks_are_invalid(self):
        self.generate()
        picks = self.latest_picks()
        changed = self.bench.carousel_output(self.item["id"], picks[1]["output_id"], "full")
        Image.new("RGB", (144, 192), "blue").save(changed)
        self.assert_error(409, self.bench.approve_carousel_outputs, self.item["id"], self.body(selections=picks))
        self.assertFalse(any(slide["outputs"][-1].get("approved") for slide in self.current()["slides"]))
        self.assert_error(400, self.bench.approve_carousel_outputs, self.item["id"], self.body(selections=[picks[0], picks[0]]))
        self.assert_error(400, self.bench.approve_carousel_outputs, self.item["id"], self.body(selections=[]))
        self.assert_error(404, self.bench.approve_carousel_outputs, self.item["id"],
                          self.body(selections=[{"slide": 1, "output_id": picks[2]["output_id"]}]))

    def test_unconfirmed_group_generates_all_slides_concurrently(self):
        original_run = self.generator.run
        barrier, guard = threading.Barrier(3), threading.Lock()
        active, peak = 0, 0

        def run(prompt, **options):
            nonlocal active, peak
            generating = not options.get("read_only")
            if generating:
                with guard:
                    active += 1
                    peak = max(peak, active)
                barrier.wait(2)
            try:
                return original_run(prompt, **options)
            finally:
                if generating:
                    with guard:
                        active -= 1

        self.generator.run = run
        item = self.generate()
        self.assertEqual(peak, 3)
        self.assertEqual(item["generations"][-1]["status"], "completed")

    def test_direct_edit_uses_a_confirmed_first_slide_as_style_for_later_images(self):
        self.use_direct_images()
        first = self.generate(slides=[1])
        self.assertEqual(first["generations"][-1]["status"], "completed")
        self.assertEqual(len(self.generator.calls), 1)
        self.approve(1)
        self.assertTrue(self.bench.carousel_first_preflight(self.item["id"])["ready"])
        item = self.generate(slides=[2, 3])
        self.assertEqual(len(self.generator.calls), 3)
        for call in self.generator.calls[1:]:
            self.assertEqual(len(call["images"]), 2)
            self.assertEqual(call["snapshot"]["style_reference"]["scope"], "style_only")
            self.assertNotEqual(call["snapshot"]["base_sha256"], first["slides"][0]["outputs"][0]["base_sha256"])
        self.approve_all()
        record = self.bench.create_carousel_delivery(item["id"], self.body())["delivery"]
        with zipfile.ZipFile(self.bench.carousel_delivery_file(item["id"], record["id"])) as archive:
            self.assertEqual(archive.namelist(), ["01.png", "02.png", "03.png"])

    def test_direct_edit_resume_uses_saved_candidate_without_another_call(self):
        self.use_direct_images()
        first = self.generate(slides=[1])
        item = self.generate(slides=[1])
        self.assertEqual(len(self.generator.calls), 1)
        self.assertEqual(len(item["slides"][0]["outputs"]), 1)
        self.assertEqual(item["slides"][0]["output_id"], first["slides"][0]["output_id"])
        self.assertEqual(item["generations"][-1]["stages"]["1"]["generation"]["status"], "skipped")

    def test_direct_edit_native_dimensions_and_encoded_bytes_survive_confirmation(self):
        self.use_direct_images()
        original_run = self.generator.run
        encoded = []
        def native_result(prompt, **options):
            original_run(prompt, **options)
            path = Path(options["cwd"]) / "render.webp"
            Image.new("RGB", (1024, 1536), "orange").save(path, format="WEBP", quality=85)
            encoded.append(path.read_bytes())
            return json.dumps({"file": path.name, "summary": ""})
        self.generator.run = native_result
        item = self.generate(slides=[1])
        output = item["slides"][0]["outputs"][0]
        self.assertEqual((output["width"], output["height"]), (1024, 1536))
        self.assertEqual(self.bench.carousel_output(item["id"], output["id"], "full").read_bytes(), encoded[0])
        self.approve(1)
        self.assertTrue(self.bench.carousel_first_preflight(item["id"])["ready"])

    def test_direct_edit_failure_does_not_retry_or_replace_confirmed_image(self):
        self.use_direct_images()
        self.generate(slides=[1])
        old = self.approve(1)["slides"][0]
        self.generator.file_mode = "error"
        item = self.generate(slides=[1], redo=True)
        self.assertEqual(len(self.generator.calls), 2)
        self.assertEqual(item["generations"][-1]["status"], "failed")
        self.assertEqual(item["slides"][0]["output_id"], old["output_id"])
        self.assertTrue(item["slides"][0]["outputs"][0]["approved"])

    def test_explicit_layout_after_subject_lock_uses_local_exact_copy_despite_direct_channel(self):
        self.use_direct_images()
        self.bench.save_carousel_requirements(self.item["id"], self.body(requirements={
            "workflow": "subject_locked", "hands_authorized": True, "authorized_hand_slides": [1]}))
        slide = self.current()["slides"][0]
        base = self.bench.carousel_image(self.item["id"], slide["base"]["id"], "full")
        Image.new("RGB", (720, 960), (42, 63, 77)).save(base)
        with patch.object(self.bench, "_plan_generation_prompts", side_effect=AssertionError("layout must not plan AI images")):
            item = self.generate(slides=[1], redo=True, requirements={"mode": "layout_only", "workflow": "review_first",
                "caption_area": "top", "caption_style": "rugged", "editable_regions": ["text"], "hands_authorized": False})
        self.assertEqual(len(self.generator.calls), 1)
        self.assertTrue(self.generator.calls[0]["read_only"], "only independent review may use the image channel")
        output = item["slides"][0]["outputs"][0]
        self.assertEqual(output["origin"], "local_layout")
        self.assertNotIn("direct_image_edit", output)
        self.assertNotIn("ai_generated", output)
        self.assertNotIn("subject_preservation", output)
        self.assertEqual(output["requirements_snapshot"]["workflow"], "review_first")
        self.assertFalse(output["requirements_snapshot"]["hands_authorized"])
        self.assertEqual(output["requirements_snapshot"]["editable_regions"], ["text"])
        self.assertEqual(output["layout_receipt"]["title_lines"], slide["title_lines"])
        self.assertEqual(output["layout_receipt"]["body"], slide["body"])
        self.assertEqual([(entry["title_lines"], entry["body"]) for entry in item["slides"]], self.words)
        self.assertFalse((self.bench._carousel_dir(item["id"]) / "subject-masks").exists())
        with Image.open(base) as source, Image.open(self.bench.carousel_output(item["id"], output["id"], "full")) as result:
            self.assertEqual(result.size, source.size)
            self.assertEqual(result.crop((0, 288, 720, 960)).tobytes(), source.crop((0, 288, 720, 960)).tobytes())
        self.assertTrue(self.approve(1)["slides"][0]["outputs"][0]["approved"])

    def test_direct_edit_can_preserve_identical_reference_bytes_from_channel(self):
        self.use_direct_images()
        self.generator.file_mode = "copy"
        item = self.generate(slides=[1])
        output = item["slides"][0]["outputs"][0]
        self.assertEqual(output["sha256"], output["base_sha256"])
        self.assertTrue(self.approve(1)["slides"][0]["outputs"][0]["approved"])

    def test_direct_edit_manual_confirmation_accepts_previous_needs_fix_image(self):
        self.generator.review_mode = "wrong_words"
        item = self.generate(slides=[1])
        output = item["slides"][0]["outputs"][0]
        self.assertEqual(output["review"]["status"], "needs_fix")
        self.use_direct_images()
        self.bench.select_carousel_output(item["id"], self.body(slide=1, output_id=output["id"]))
        self.assertTrue(self.approve(1)["slides"][0]["outputs"][0]["approved"])
        self.assertTrue(self.bench.carousel_first_preflight(item["id"])["ready"])


class CarouselImageHTTPTests(unittest.TestCase):
    """Use the actual request handler and shutdown gate on a temporary loopback port."""

    request = http_fixture.WorkbenchHTTPTests.request
    body = CarouselImageTests.body
    carousel = CarouselImageTests.carousel
    current = CarouselImageTests.current
    pick_new_base = CarouselImageTests.pick_new_base

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="carousel-images-http-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.generator = ImageGenerator()
        silence = patch.object(http_fixture.SERVER.Handler, "log_message", lambda *args: None)
        silence.start()
        self.addCleanup(silence.stop)
        with patch.object(http_fixture.SERVER, "CockpitImageBridge", return_value=self.generator):
            self.server = http_fixture.SERVER.make_server(root / "skill", root / "runtime", port=0,
                allow_open=False, ai_bridge=FakeBridge())
        self.bench = self.server.app
        self.bench.image_reviewer = self.generator
        self.bench.carousel_image_root = root / "existing-images"
        photo = Path(self.bench.edit_skill_dir).parent / "tiktok-photo-commerce"
        photo.mkdir(parents=True, exist_ok=True)
        (photo / "SKILL.md").write_text("---\nname: tiktok-photo-commerce\n---\n# 图文", encoding="utf-8")
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.closed = False
        self.addCleanup(self.stop_server)
        self.token = self.request("GET", "/api/service/status")[2]["token"]
        self.item = self.carousel()
        self.originals = {}
        self.pick_new_base(1)
        self.prefix = f"/api/production/carousels/{self.item['id']}"

    def stop_server(self):
        if not self.closed:
            self.server.shutdown()
            self.server.server_close()
            self.closed = True
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.generator.release.set()
        self.wait_worker()

    def wait_worker(self):
        deadline = time.monotonic() + 30  # a polling limit; large catalogs make full runs take several seconds
        while self.bench.image_running() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.bench.image_running())
        return self.current()

    def test_running_images_reject_service_shutdown_and_reopen_actual_write_admission(self):
        with patch.object(self.bench, "image_running", return_value=True), \
             patch.object(self.bench, "edit_running", return_value=False), \
             patch.object(self.bench.ai, "list_tasks", return_value=[]):
            status, _, result = self.request("POST", "/api/service/shutdown", {})
            self.assertEqual(status, 409, result)
            self.assertIn("出图", result["error"])
            self.assertFalse(self.server.shutting_down)
            candidate = self.current()["slides"][0]["base"]["id"]
            status, _, result = self.request("POST", self.prefix + "/base", self.body(slide=1, candidate=candidate))
            self.assertEqual(status, 200, result)
            with self.server._write_gate:
                self.assertTrue(self.server._write_gate.wait_for(lambda: self.server._active_writes == 0, timeout=2))
        self.assertEqual(self.request("GET", "/api/service/status")[0], 200)
        self.assertEqual(self.generator.calls, [])

    def test_server_close_notifies_active_image_worker_to_stop(self):
        self.generator.block_stage = "generation"
        status, _, result = self.request("POST", self.prefix + "/generate", self.body(slides=[1]))
        self.assertEqual(status, 200, result)
        self.assertTrue(self.generator.entered.wait(2))
        self.assertTrue(self.bench.image_running())
        with patch.object(self.bench, "stop_images", wraps=self.bench.stop_images) as stop:
            self.stop_server()
        stop.assert_called_once_with()
        self.assertEqual(self.current()["generations"][-1]["status"], "cancelled")
        self.assertEqual(self.current()["slides"][0]["outputs"], [])

    def test_image_mutations_require_page_token_and_reject_external_requests(self):
        before = self.bench.production.path.read_bytes()
        routes = (self.prefix + "/generate", self.prefix + "/approval",
                  self.prefix + "/generations/IMG-000000000000/cancel")
        for path in routes:
            with self.subTest(path=path, guard="token"):
                self.assertEqual(self.request("POST", path, self.body(slides=[1]), authenticated=False)[0], 403)
            with self.subTest(path=path, guard="origin"):
                self.assertEqual(self.request("POST", path, {}, headers={"Origin": "https://external.invalid"})[0], 403)
        candidate = self.current()["slides"][0]["base"]["id"]
        self.assertEqual(self.request("GET", self.prefix + f"/images/{candidate}",
            headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.bench.production.path.read_bytes(), before)
        self.assertEqual(self.generator.calls, [])

    def test_generate_approval_cancel_routes_report_invalid_inputs_without_starting_a_worker(self):
        for values in ({"slides": []}, {"slides": [True]}, {"slides": [1, 1]}, {"slides": [99]}, {"aspect": "1:1"}):
            with self.subTest(values=values):
                status, _, result = self.request("POST", self.prefix + "/generate", self.body(**values))
                self.assertEqual(status, 400, result)
        self.assertEqual(self.request("POST", self.prefix + "/generate", [1])[0], 400)
        self.assertEqual(self.request("POST", "/api/production/carousels/CAR-ffffffffffff/generate", self.body(slides=[1]))[0], 404)
        status, _, result = self.request("POST", self.prefix + "/approval",
            self.body(slide=1, output_id="OUT-000000000000", approved=True))
        self.assertEqual(status, 404, result)
        self.assertEqual(self.request("POST", self.prefix + "/generations/IMG-000000000000/cancel", {})[0], 409)
        self.assertFalse(self.bench.image_running())
        self.assertEqual(self.generator.calls, [])

    def test_generated_files_are_served_for_the_owner_and_missing_or_escaped_paths_are_rejected(self):
        status, _, result = self.request("POST", self.prefix + "/generate", self.body(slides=[1]))
        self.assertEqual(status, 200, result)
        output = self.wait_worker()["slides"][0]["outputs"][0]
        path = self.prefix + "/outputs/" + output["id"]
        for query, mime in (("?size=full", "image/png"), ("?size=thumb", "image/jpeg")):
            with self.subTest(size=query):
                status, headers, content = self.request("GET", path + query)
                self.assertEqual((status, headers["Content-Type"]), (200, mime))
                self.assertEqual(content, self.bench.carousel_output(self.item["id"], output["id"], "full" if "full" in query else "thumb").read_bytes())
        self.assertEqual(self.request("GET", path, headers={"Origin": "https://external.invalid"})[0], 403)
        self.assertEqual(self.request("GET", path.replace(self.item["id"], "CAR-ffffffffffff"))[0], 404)
        self.assertEqual(self.request("GET", self.prefix + "/outputs/OUT-000000000000")[0], 404)
        status, _, result = self.request("POST", self.prefix + "/approval",
            self.body(slide=1, output_id=output["id"], approved=True))
        self.assertEqual(status, 200, result)
        self.assertEqual(result["carousel"]["slides"][0]["status"], "approved")
        self.bench.carousel_output(self.item["id"], output["id"], "full").unlink()
        self.assertEqual(self.request("GET", path + "?size=full")[0], 404)
        outside = self.bench.production.root / "outside.png"
        Image.new("RGB", (144, 192), "red").save(outside)
        with self.bench.lock:
            data = self.bench.production.read()
            data["carousels"][0]["slides"][0]["outputs"][0]["file"] = "../../outside.png"
            self.bench.production.write(data)
        self.assertEqual(self.request("GET", path + "?size=full")[0], 404)

    def confirmed_detail(self):
        status, _, result = self.request("POST", self.prefix + "/generate", self.body(slides=[1]))
        self.assertEqual(status, 200, result)
        output = self.wait_worker()["slides"][0]["outputs"][-1]
        status, _, result = self.request("POST", self.prefix + "/approval",
            self.body(slide=1, output_id=output["id"], approved=True))
        self.assertEqual(status, 200, result)
        detail = self.request("GET", self.prefix)[2]["carousel"]
        self.assertEqual(detail["slides"][0]["status"], "approved")
        return output

    def test_detail_invalidates_confirmation_when_current_output_file_disappears(self):
        output = self.confirmed_detail()
        self.bench.carousel_output(self.item["id"], output["id"], "full").unlink()
        status, _, result = self.request("GET", self.prefix)
        self.assertEqual(status, 200, result)
        item = result["carousel"]
        slide = item["slides"][0]
        current = slide["outputs"][0]
        self.assertEqual((current["stale"], current["approved"], slide["status"]), (True, False, "awaiting_review"))
        self.assertIn("丢失", current["stale_reason"])
        self.assertEqual((slide["output_id"], len(slide["outputs"])), (output["id"], 1))
        self.assertNotEqual(item["status"], "approved")
        self.assertEqual(self.request("POST", self.prefix + "/approval",
            self.body(slide=1, output_id=output["id"], approved=True))[0], 404)

    def test_detail_invalidates_confirmation_when_current_output_bytes_change(self):
        output = self.confirmed_detail()
        Image.new("RGB", (144, 192), "red").save(
            self.bench.carousel_output(self.item["id"], output["id"], "full"))
        status, _, result = self.request("GET", self.prefix)
        self.assertEqual(status, 200, result)
        item = result["carousel"]
        slide = item["slides"][0]
        current = slide["outputs"][0]
        self.assertEqual((current["stale"], current["approved"], slide["status"]), (True, False, "awaiting_review"))
        self.assertIn("变化", current["stale_reason"])
        self.assertEqual((slide["output_id"], len(slide["outputs"])), (output["id"], 1))
        self.assertNotEqual(item["status"], "approved")
        self.assertEqual(self.request("POST", self.prefix + "/approval",
            self.body(slide=1, output_id=output["id"], approved=True))[0], 409)


if __name__ == "__main__":
    unittest.main()
