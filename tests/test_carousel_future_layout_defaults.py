"""New-order layout contracts over disposable stores and fake image workers."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import numpy as np
from PIL import Image

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import production_store as production
from direction_store import now
from elevenlabs_client import VoiceError
from production_store import ProductionStore, carousel_input_signature, carousel_requirements
from task_activity import ActivityStore, reconcile_activity
import test_production_carousel_images as image_fixture
import test_production_carousel_import as import_fixture
from workbench_carousel_autopilot import CarouselAutopilotMixin


SAFE_FIELDS = {
    "mode": "layout_only", "workflow": "review_first", "editable_regions": ["text"],
    "hands_authorized": False, "caption_style": "yellow_impact", "overlay_only": True,
    "caption_layer": "behind_subject", "dense_layout_policy": "edge_whitespace",
}


class FutureLayoutStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="future-carousel-defaults-")
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name)
        self.store = ProductionStore(self.runtime)

    def body(self, **fields):
        return {"request_id": uuid4().hex, "revision": self.store.current_revision(), **fields}

    @staticmethod
    def resolved():
        return {"script_id": "SCR-0123456789ab", "script_version": 1, "product": "202H",
                "title": "中文标题验证", "direction_id": "202H-S2", "source": {},
                "caption_en": "The complete set. #SHARPAL202H", "caption_zh": "完整一套。",
                "slides": [{"number": n, "title_lines": [f"Exact English {n}.", "Keep this line!"],
                            "body": f"Original body {n}; punctuation stays.", "title_zh": f"中文图片标题{n}",
                            "body_zh": f"中文正文{n}。", "role": "购买理由", "source": "原实拍", "forbidden": "夸大"}
                           for n in (1, 2, 3)]}

    def create(self, **fields):
        return self.store.mutate("carousel", self.body(**fields), self.resolved())["carousel"]

    def assert_safe(self, requirements):
        for key, value in SAFE_FIELDS.items():
            self.assertEqual(requirements.get(key), value, key)
        self.assertTrue(requirements["protected_product"])
        self.assertTrue(requirements["protected_evidence"])

    def test_new_orders_use_safe_defaults_without_rewriting_any_copy(self):
        # Earlier product settings cannot silently re-authorize background or hand edits.
        catalog = self.store.empty()
        catalog["settings"]["products"]["202H"] = {"carousel_requirements": {
            "mode": "generated_visual", "workflow": "subject_locked", "hands_authorized": True,
            "editable_regions": ["background", "text", "hands"], "caption_style": "plain", "caption_area": "top",
            "instruction": "旧换景要求", "hand_look": "旧手部要求", "authorized_hand_slides": [1]}}
        self.store.write(catalog)
        original = self.resolved()
        item = self.create()
        self.assert_safe(item["requirements"])
        self.assert_safe(carousel_requirements(item, item["slides"][0]))
        self.assertTrue(production.carousel_requires_layered_layout(item["requirements"]))
        for actual, expected in zip(item["slides"], original["slides"]):
            for key in ("title_lines", "body", "title_zh", "body_zh"):
                self.assertEqual(actual[key], expected[key])
        self.assertEqual(item["caption_en"], original["caption_en"])
        self.assertNotIn("caption_area", item["requirements"])
        self.assertNotIn("hand_look", item["requirements"])
        self.assertNotIn("authorized_hand_slides", item["requirements"])
        self.assertEqual(item["requirements"]["instruction"], "")
        self.assertEqual(production.NEW_CAROUSEL_REQUIREMENTS["caption_layer"], "behind_subject")

    def test_explicit_creation_requirements_preserve_generation_choices(self):
        choices = [
            ({"mode": "generated_visual", "workflow": "generation_first"}, "generated_visual", "generation_first"),
            ({"workflow": "subject_locked"}, "photo_preserving", "subject_locked"),
        ]
        for requested, expected_mode, expected_workflow in choices:
            with self.subTest(workflow=expected_workflow):
                item = self.create(requirements=requested)
                self.assertEqual(item["requirements"]["mode"], expected_mode)
                self.assertEqual(item["requirements"]["workflow"], expected_workflow)
                self.assertEqual(item["requirements"]["caption_style"], "yellow_impact")
                self.assertFalse(production.carousel_requires_layered_layout(item["requirements"]))

    def test_partial_creation_requirement_keeps_safe_defaults(self):
        item = self.create(requirements={"aspect": "9:16"})
        self.assert_safe(item["requirements"])
        self.assertEqual(item["requirements"]["aspect"], "9:16")
        self.assertEqual(item["image_aspect"], "9:16")
        self.assertTrue(production.carousel_requires_layered_layout(item["requirements"]))

    def test_each_new_caption_style_survives_save_reload_and_signature_binding(self):
        signatures = set()
        original = self.resolved()
        for style in ("yellow_impact", "orange_depth", "red_comic", "lime_pop"):
            with self.subTest(style=style):
                item = self.create(requirements={"caption_style": style})
                self.assertEqual(item["requirements"]["caption_style"], style)
                self.store.mutate("carousel_requirements", self.body(carousel_id=item["id"],
                    requirements={**item["requirements"], "style": "本次花字选择"}))
                reloaded = ProductionStore(self.runtime).carousel(item["id"])
                self.assertEqual(reloaded["requirements"]["caption_style"], style)
                saved = self.store.records()["settings"]["products"]["202H"]["carousel_requirements"]
                self.assertEqual(saved["caption_style"], style)
                for actual, expected in zip(reloaded["slides"], original["slides"]):
                    self.assertEqual(actual["title_lines"], expected["title_lines"])
                    self.assertEqual(actual["body"], expected["body"])
                signatures.add(carousel_input_signature(reloaded, reloaded["slides"][0], "3:4"))
        self.assertEqual(len(signatures), 4, "switching presets must invalidate the previous render")
        following = self.create()
        self.assertEqual(following["requirements"]["caption_style"], "yellow_impact")
        self.assertEqual(following["requirements"]["style"], "本次花字选择")

    def test_existing_caption_styles_remain_explicit_and_invalid_presets_are_rejected(self):
        for style in ("plain", "rugged"):
            with self.subTest(style=style):
                item = self.create(requirements={"caption_style": style})
                self.assertEqual(carousel_requirements(item)["caption_style"], style)
                reloaded = ProductionStore(self.runtime).carousel(item["id"])
                self.assertEqual(reloaded["requirements"]["caption_style"], style)
        for style in ("yellow", "", None, [], {}, True):
            with self.subTest(style=style), self.assertRaises(VoiceError) as raised:
                self.create(requirements={"caption_style": style})
            self.assertEqual(raised.exception.status, 400)

    def test_legacy_canonical_contract_and_signature_stay_exact(self):
        item = {**self.resolved(), "requirements": {"mode": "photo_preserving"}}
        slide = {**item["slides"][0], "base": {"id": "CND-0123456789ab"}}
        legacy = {"mode": "photo_preserving", "editable_regions": ["background", "text"],
                  "protected_product": True, "protected_evidence": True, "hands_authorized": False,
                  "aspect": "3:4", "style": "", "source": "codex_builtin", "instruction": "",
                  "postprocess": "layout_only"}
        self.assertEqual(carousel_requirements(item), legacy)
        legacy_slide = {**legacy, "locked_copy": {"number": 1, "title_lines": slide["title_lines"], "body": slide["body"]},
                        "product_reference": {"base_id": slide["base"]["id"], "source": slide["source"]}}
        signed = {"script": [item["script_id"], 1], "aspect": "3:4",
                  "slide": {key: slide.get(key) for key in ("number", "title_lines", "body", "role", "source", "forbidden")},
                  "base_id": slide["base"]["id"], "requirements": legacy_slide}
        expected = hashlib.sha256(json.dumps(signed, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
        self.assertEqual(carousel_input_signature(item, slide, "3:4"), expected)
        self.assertNotIn("caption_layer", carousel_requirements(item, slide))
        self.assertNotIn("dense_layout_policy", carousel_requirements(item, slide))

    def test_new_layout_fields_are_optional_validated_and_signature_bound(self):
        old = {**self.resolved(), "requirements": {"mode": "layout_only", "editable_regions": ["text"]}}
        layered = {**old, "requirements": {**old["requirements"], "caption_layer": "behind_subject",
                                           "dense_layout_policy": "edge_whitespace"}}
        canonical = carousel_requirements(layered)
        self.assertEqual(canonical["caption_layer"], "behind_subject")
        self.assertEqual(canonical["dense_layout_policy"], "edge_whitespace")
        self.assertNotEqual(carousel_input_signature(old, old["slides"][0], "3:4"),
                            carousel_input_signature(layered, layered["slides"][0], "3:4"))
        for key in ("caption_layer", "dense_layout_policy"):
            for value in ([], {}, None, True, "silently-redraw"):
                with self.subTest(key=key, value=value):
                    with self.assertRaises(VoiceError) as raised:
                        carousel_requirements({**old, "requirements": {**old["requirements"], key: value}})
                    self.assertEqual(raised.exception.status, 400)

    def test_layout_only_ignores_previous_preservation_fallback(self):
        item = self.create()
        previous = copy.deepcopy(item)
        item["requirements"]["preserve_slides"] = [1, 2]
        self.assertNotIn("preserve_slides", carousel_requirements(item))
        for slide in item["slides"]:
            self.assert_safe(carousel_requirements(item, slide))
            self.assertEqual(carousel_input_signature(item, slide, "3:4"),
                             carousel_input_signature(previous, slide, "3:4"))

    def test_saving_product_requirements_keeps_layer_contract_for_next_order(self):
        item = self.create()
        self.store.mutate("carousel_requirements", self.body(carousel_id=item["id"],
            requirements={**item["requirements"], "style": "保留实拍花字"}))
        product = self.store.records()["settings"]["products"]["202H"]["carousel_requirements"]
        self.assertEqual(product["caption_layer"], "behind_subject")
        self.assertEqual(product["dense_layout_policy"], "edge_whitespace")
        following = self.create()
        self.assert_safe(following["requirements"])
        self.assertEqual(following["requirements"]["style"], "保留实拍花字")

    def test_needs_attention_is_durable_terminal_state_with_no_late_reopen(self):
        item = self.create()
        identifier = "AUT-" + uuid4().hex[:12]
        self.store.mutate("carousel_autopilot_start", self.body(carousel_id=item["id"]), {
            "id": identifier, "status": "running", "started_at": now(), "workflow": "review_first",
            "actor": "assistant_on_user_authorization", "authorization": "完成图文制作"})
        update_body = {"request_id": uuid4().hex, "carousel_id": item["id"], "run_id": identifier}
        final = self.store.mutate("carousel_autopilot_update", update_body, {
            "status": "needs_attention", "phase": "待分层协作", "message": production.LAYOUT_COLLABORATION_MESSAGE,
            "completed_slides": [], "result": {"delivery_ready": False}})["run"]
        self.assertTrue(final["finished_at"])
        restarted = ProductionStore(self.runtime)
        restarted.interrupt_running()
        current = restarted.carousel(item["id"])
        self.assertEqual(current["autopilot_runs"][-1], final)
        late = restarted.mutate("carousel_autopilot_update", {**update_body, "request_id": uuid4().hex},
                                 {"status": "completed", "delivery_id": "DLV-0123456789ab"})["run"]
        self.assertEqual(late, final)
        self.assertEqual(current["generations"], [])


class FutureLayoutAutopilotBench(CarouselAutopilotMixin, image_fixture.ImageBench):
    pass


class CutoutImages(image_fixture.ImageGenerator):
    """A channel that could cut out, so the tests can prove captioning never asks it to."""
    supports_cutout = True

    def __init__(self):
        super().__init__()
        self.cutouts = []
        self.regions = []
        self.evidence_fails = set()

    def run(self, prompt, *, cutout=None, **options):
        if cutout is None and "box" in ((options.get("schema") or {}).get("properties") or {}):
            # The empty-space look: the plain table below the product.
            self.regions.append(options["images"][0])
            return json.dumps({"box": [40, 600, 960, 960], "align": "left", "reason": "商品下方是空桌面"}, ensure_ascii=False)
        if cutout is None:
            answer = super().run(prompt, **options)
            number = json.loads((Path(options["cwd"]) / "input.json").read_text(encoding="utf-8"))["slide"]["number"]
            if options.get("read_only") and number in self.evidence_fails:
                parsed = json.loads(answer)
                for check in parsed["checks"]:
                    if check["id"] == "evidence":
                        check.update(status="fail", note="隔离测试：文案声称的效果在画面里没有对应证据。")
                return json.dumps(parsed, ensure_ascii=False)
            return answer
        source = np.array(Image.open(options["images"][0]).convert("RGB"))
        self.cutouts.append(options["images"][0])
        alpha = np.where(source.max(axis=2) < 55, 255, 0).astype("uint8")
        Image.fromarray(np.dstack([source, alpha]), "RGBA").save(Path(options["cwd"]) / "render.png")
        return json.dumps({"file": "render.png", "summary": ""})


def photo_with_product(seed):
    """A 3:4 real-looking frame: a plain table with a detailed product across its upper half."""
    rng = np.random.default_rng(seed)
    array = (150 + rng.integers(0, 6, (1024, 768, 3))).astype("uint8")
    array[60:560, 120:650] = rng.integers(0, 200, (500, 530, 3)).astype("uint8")
    return array


class FutureLayoutAutopilotTests(unittest.TestCase):
    def setUp(self):
        self.fixture = image_fixture.CarouselImageTests()
        with patch.object(image_fixture, "ImageBench", FutureLayoutAutopilotBench), \
                patch.object(image_fixture, "ImageGenerator", CutoutImages):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bench, self.generator = self.fixture.bench, self.fixture.generator
        self.identifier = self.fixture.item["id"]
        # The shared image fixture deliberately covers the historic photo mode;
        # this suite explicitly opts its disposable order into the new contract.
        self.bench.save_carousel_requirements(self.identifier, self.fixture.body(
            requirements=copy.deepcopy(production.NEW_CAROUSEL_REQUIREMENTS)))
        self.bases = {}
        for number in (1, 2, 3):
            relative = f"202H-S2/原图/layered-{number}-{uuid4().hex[:8]}.png"
            path = self.bench.carousel_image_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(photo_with_product(number)).save(path)
            self.bases[number] = np.array(Image.open(path).convert("RGB"))
            candidate = self.bench.import_folder_image(self.identifier, self.fixture.body(slide=number, path=relative))["candidate"]
            self.bench.choose_base(self.identifier, self.fixture.body(slide=number, candidate=candidate["id"]))
        self.bench.runtime = self.bench.production.root.parent
        self.bench.activity = ActivityStore(self.bench.runtime)
        self.addCleanup(self.bench.stop_carousel_autopilots)

    def wait(self):
        deadline = time.monotonic() + 30
        while (self.bench.autopilot_running() or self.bench.image_running()) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.bench.autopilot_running(), "autopilot worker did not finish")
        return self.fixture.current()

    def test_layered_autopilot_captions_the_chosen_bases_reviews_and_packages(self):
        before = self.fixture.current()
        with patch.object(self.bench, "image_status", return_value={"available": False, "review_available": True}):
            run = self.bench.start_carousel_autopilot(self.identifier, self.fixture.body())["run"]
        self.assertEqual(run["status"], "running")
        item = self.wait()
        run = item["autopilot_runs"][-1]
        self.assertEqual(run["status"], "completed", run)
        self.assertTrue(run["delivery_id"])
        # Captions only: no cutout, no image generation, and every slide independently reviewed.
        self.assertEqual(self.generator.cutouts, [])
        self.assertEqual([call for call in self.generator.calls if not call["read_only"]], [])
        self.assertEqual(len([call for call in self.generator.calls if call["read_only"]]), 3)
        self.assertEqual(len(self.generator.regions), 3)
        self.assertIn("选定底图上加字", self.generator.calls[0]["prompt"])
        for slide in item["slides"]:
            output = next(entry for entry in slide["outputs"] if entry["id"] == slide["output_id"])
            receipt = output["layout_receipt"]
            self.assertTrue(output["approved"])
            self.assertEqual(output["origin"], "local_layout")
            self.assertEqual(receipt["method"], "caption_on_base")
            self.assertEqual(receipt["text"], slide["title_lines"])
            made = np.array(Image.open(self.bench.carousel_output(self.identifier, output["id"], "full")).convert("RGB"))
            original = self.bases[slide["number"]]
            self.assertEqual(made.shape, original.shape)
            top, bottom = receipt["caption_bounds"][1], receipt["caption_bounds"][3]
            # The product fills the upper half: the caption goes into the empty table the look picked.
            self.assertEqual(receipt["caption_placement"], "region")
            self.assertGreaterEqual(top, 600 * 1024 // 1000)
            self.assertFalse(np.array_equal(made[top:bottom], original[top:bottom]))
            outside = np.ones(made.shape[:2], dtype=bool)
            outside[top:bottom] = False
            self.assertTrue(np.array_equal(made[outside], original[outside]))
        after = self.fixture.current()
        self.assertEqual(after["requirements"], before["requirements"])
        for actual, expected in zip(after["slides"], before["slides"]):
            self.assertEqual((actual["title_lines"], actual["body"]), (expected["title_lines"], expected["body"]))

    def test_evidence_only_failure_holds_that_slide_finishes_the_rest_and_packages_nothing(self):
        self.generator.evidence_fails = {2}
        self.bench.start_carousel_autopilot(self.identifier, self.fixture.body())
        item = self.wait()
        run = item["autopilot_runs"][-1]
        self.assertEqual(run["status"], "needs_attention", run)
        self.assertEqual(run["phase"], "部分张待人看")
        self.assertEqual(run["completed_slides"], [1, 3])
        self.assertEqual([entry["slide"] for entry in run["result"]["held_slides"]], [2])
        self.assertIn("没有对应证据", run["result"]["held_slides"][0]["reason"])
        self.assertTrue(run["finished_at"])
        self.assertFalse(run["delivery_id"])
        self.assertFalse(item.get("deliveries"))
        approved = {slide["number"]: any(output.get("approved") for output in slide["outputs"]) for slide in item["slides"]}
        self.assertEqual(approved, {1: True, 2: False, 3: True})
        self.assertEqual(len(item["slides"][1]["outputs"]), 1)

        # Laying the same copy out again cannot answer the evidence question: a rerun neither redraws nor re-checks it.
        calls = len(self.generator.calls)
        self.bench.start_carousel_autopilot(self.identifier, self.fixture.body())
        item = self.wait()
        self.assertEqual(item["autopilot_runs"][-1]["status"], "needs_attention")
        self.assertEqual(len(self.generator.calls), calls)
        self.assertEqual(len(item["slides"][1]["outputs"]), 1)

    def test_other_review_failures_still_stop_the_run(self):
        self.generator.review_mode = "wrong_words"
        self.bench.start_carousel_autopilot(self.identifier, self.fixture.body())
        run = self.wait()["autopilot_runs"][-1]
        self.assertEqual(run["status"], "failed", run)
        self.assertEqual(run["completed_slides"], [])

    def test_redo_lays_the_same_caption_out_again_without_a_cutout(self):
        self.generator.supports_cutout = False
        self.fixture.generate(slides=[1])
        self.fixture.generate(slides=[1], redo=True)
        self.assertEqual(self.generator.cutouts, [])
        outputs = self.fixture.current()["slides"][0]["outputs"]
        self.assertEqual(len(outputs), 2)
        self.assertEqual(outputs[0]["sha256"], outputs[1]["sha256"])

    def test_restart_keeps_an_older_wait_run_instead_of_marking_it_interrupted(self):
        # Orders started before layered layout was automatic kept a durable "waiting" run.
        run_id = "AUT-" + uuid4().hex[:12]
        self.bench.production.mutate("carousel_autopilot_start", self.fixture.body(carousel_id=self.identifier), {
            "id": run_id, "status": "running", "started_at": now(), "workflow": "review_first",
            "actor": "assistant_on_user_authorization", "authorization": "完成图文制作"})
        run = self.bench.production.mutate("carousel_autopilot_update", {"request_id": uuid4().hex,
            "carousel_id": self.identifier, "run_id": run_id}, {"status": "needs_attention", "phase": "待分层排版协作",
            "message": production.LAYOUT_COLLABORATION_MESSAGE, "finished_at": now()})["run"]
        self.bench.activity.record("autopilot:" + run_id, kind="images", owner_id=self.identifier, source_id=run_id,
                                   status="running", phase="接手制作", message="处理中")
        restarted = ProductionStore(self.bench.runtime)
        restarted.interrupt_running()
        app = SimpleNamespace(activity=self.bench.activity, runtime=self.bench.runtime,
                              production=restarted, lock=restarted.lock)
        reconcile_activity(app)
        activity = self.bench.activity.feed()["items"][0]
        self.assertEqual(activity["status"], "needs_attention")
        self.assertEqual(restarted.carousel(self.identifier)["autopilot_runs"][-1], run)


class FutureLayoutFinishedImportTests(unittest.TestCase):
    def test_external_finished_images_remain_importable_under_layered_contract(self):
        fixture = import_fixture.FinishedImportTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        before = fixture.current()
        self.assertTrue(production.carousel_requires_layered_layout(before["requirements"]))
        imported = fixture.import_images()
        after = fixture.current()
        self.assertEqual(after["requirements"], before["requirements"])
        self.assertEqual(after["generations"], [])
        self.assertEqual(len(imported["outputs"]), len(before["slides"]))
        for output in imported["outputs"]:
            self.assertIsNone(output["generation_id"])
            self.assertEqual(output["origin"], "external_generated_layout")
            self.assertEqual(output["review"]["status"], "pending")
            self.assertFalse(output["approved"])
        for actual, expected in zip(after["slides"], before["slides"]):
            for key in ("title_lines", "body", "title_zh", "body_zh"):
                self.assertEqual(actual[key], expected[key])


if __name__ == "__main__":
    unittest.main()
