"""Finished imports exercise bytes, provenance and isolated version transitions."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
CONFIG = json.loads((APP / "config.json").read_text(encoding="utf-8"))
sys.path.insert(0, str(Path(CONFIG["skill_root"]) / "scripts"))
from PIL import Image
from elevenlabs_client import VoiceError
from production_store import ProductionStore, carousel_input_signature
from workbench_carousel_import import CarouselFinishedImportMixin
from workbench_carousel_images import CarouselImagesMixin, generation_prompt


class Bench(CarouselFinishedImportMixin):
    def __init__(self, folder):
        self.production = ProductionStore(folder)
        self.lock = self.production.lock
        self.carousel_finished_root = Path(folder) / "finished"
        self.carousel_finished_root.mkdir()


class FinishedImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="finished-carousel-")
        self.addCleanup(self.temp.cleanup)
        self.bench = Bench(self.temp.name)
        self.store = self.bench.production
        self.item = self.create()
        self.identifier = self.item["id"]
        self.folder = self.bench.carousel_finished_root / "set"
        self.folder.mkdir()
        self.make_files(self.folder)

    def create(self):
        return self.store.mutate("carousel", self.body(), {"script_id": "SCR-0123456789ab", "script_version": 1,
            "product": "202H", "direction_id": "202H-S2", "title": "送礼", "image_aspect": "3:4", "caption_en": "Gift caption.", "caption_zh": "送礼",
            "slides": [{"number": number, "title_lines": [f"Title {number}"], "body": f"Body {number}", "title_zh": f"标题{number}",
                        "body_zh": f"正文{number}", "role": "购买理由", "source": "实拍参考", "forbidden": "不夸大"} for number in (1, 2)]})["carousel"]

    def body(self, **values):
        return {"request_id": uuid4().hex, "revision": self.store.public()["revision"], **values}

    def current(self):
        return self.store.carousel(self.identifier)

    def make_files(self, folder):
        for name in ("PNG", "JPG", "sources", "native", "qa", "references"):
            (folder / name).mkdir()
        for number in (1, 2):
            # Nonuniform images make an accidental wrong-number alias detectable.
            image = Image.new("RGB", (144, 192))
            image.putdata([((x * 3 + number * 10) % 256, (y + number * 40) % 256, number * 50) for y in range(192) for x in range(144)])
            image.save(folder / "PNG" / f"{number:02d}.png")
            image.save(folder / "JPG" / f"{number:02d}.jpg", quality=98, subsampling=0)
            image.save(folder / "sources" / f"{number:02d}.png")
            image.save(folder / "native" / f"{number:02d}.png")
            (folder / "references" / f"prompt-{number}.txt").write_text("实际提示词", encoding="utf-8")
        source_copy = {"direction_id": "202H-S2", "slides": [{key: row[key] for key in ("number", "title_lines", "body", "title_zh", "body_zh")}
                        for row in self.item["slides"]], "caption_en": self.item["caption_en"], "caption_zh": self.item["caption_zh"]}
        (folder / "copy.json").write_text(json.dumps(source_copy, ensure_ascii=False), encoding="utf-8")
        (folder / "qa" / "record.json").write_text('{"source":"external"}', encoding="utf-8")

    def import_images(self, **values):
        return self.bench.import_finished_images(self.identifier, self.body(directory="set", requirements={"mode": "generated_visual",
            "editable_regions": [], "hands_authorized": True, "source": "external_generated_layout"}, **values))

    def error(self, status, function, *args, **kwargs):
        with self.assertRaises(VoiceError) as failure:
            function(*args, **kwargs)
        self.assertEqual(failure.exception.status, status)

    def review(self, output, status="pass", run_id=None):
        run_id = run_id or "REV-" + uuid4().hex[:12]
        body = {"carousel_id": self.identifier, "slide": output["slide"], "output_id": output["id"]}
        slide = next(row for row in self.current()["slides"] if row["number"] == output["slide"])
        signature = carousel_input_signature(self.current(), slide, output["aspect"])
        self.store.mutate("carousel_review_start", self.body(**body), {"id": run_id, "status": "running", "started_at": "2026-10-11T09:00:00Z",
            "finished_at": "", "file_sha256": output["sha256"], "input_signature": signature, "requirements_signature": output.get("requirements_signature"),
            "scope": ["visual", "text", "product", "evidence"]})
        return self.store.mutate("carousel_review_finish", self.body(**body, review_id=run_id), {"status": "completed", "finished_at": "2026-10-11T09:01:00Z",
            "file_sha256": output["sha256"], "input_signature": signature, "review": {"status": status, "summary": "实际核对", "checks": [], "issues": []}})

    def approve(self, output, **values):
        return self.store.mutate("carousel_approval", self.body(carousel_id=self.identifier, slide=output["slide"], output_id=output["id"], approved=True,
            actor="assistant_on_user_authorization", **values))

    def test_import_copies_two_logical_slides_and_preserves_real_provenance(self):
        result = self.import_images()
        self.assertEqual(result["import"]["logical_count"], 2)
        self.assertFalse(result["duplicate"])
        self.assertFalse(self.current()["generations"])
        for row in self.current()["slides"]:
            self.assertIsNone(row["output_id"])
            output = row["outputs"][0]
            self.assertFalse(output["approved"])
            self.assertEqual(output["review"]["status"], "pending")
            self.assertIsNone(output["generation_id"])
            self.assertIsNone(output["provenance"]["model"])
            self.assertFalse(output["provenance"]["source_product_pixel_preservation_claimed"])
            self.assertEqual(len(output["copies"]), 2)
            root = self.store.root / "carousels" / self.identifier
            source = self.folder / "PNG" / f"{row['number']:02d}.png"
            self.assertEqual(source.read_bytes(), (root / output["file"]).read_bytes())
            self.assertEqual(output["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertTrue(all(entry["kind"] == "product_reference" for entry in output["product_reference_files"]))
            self.assertTrue(all(Path(entry["path"]).is_file() for entry in output["archive_refs"]))
        self.assertEqual(len(self.current()["finished_imports"]), 1)
        repeated = self.import_images()
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(result["import"]["id"], repeated["import"]["id"])
        self.assertEqual(len(self.current()["slides"][0]["outputs"]), 1)

    def test_import_does_not_create_generation_requirements(self):
        result = self.import_images()
        self.assertNotIn("requirements", self.current())
        for output in result["outputs"]:
            slide = next(row for row in self.current()["slides"] if row["number"] == output["slide"])
            self.assertEqual(output["input_signature"], carousel_input_signature(self.current(), slide, output["aspect"]))
            self.assertEqual(output["requirements_snapshot"]["mode"], "generated_visual")
            self.review(output)
            self.approve(output)

    def test_import_contract_stays_with_output_and_does_not_pollute_future_generation(self):
        saved = self.bench.save_carousel_requirements(self.identifier, self.body(requirements={
            "mode": "generated_visual", "editable_regions": ["background", "text"],
            "style": "粗犷工坊花字", "source": "当前逐张底图", "aspect": "3:4"}))["carousel"]
        requirements = copy.deepcopy(saved["requirements"])
        defaults = copy.deepcopy(self.store.read()["settings"]["products"])
        import_note = "保留现有完整生成画面；本次只登记与核对，不改图。"
        imported = {**requirements, "editable_regions": [], "source": "外部完成的生成／排版结果", "style": import_note}
        output = self.bench.import_finished_images(self.identifier, self.body(directory="set", requirements=imported))["outputs"][0]
        current = self.current()
        self.assertEqual(current["requirements"], requirements)
        self.assertEqual(self.store.read()["settings"]["products"], defaults)
        self.assertEqual(output["requirements_snapshot"]["style"], import_note)
        self.assertEqual(output["requirements_snapshot"]["editable_regions"], [])
        snapshot = CarouselImagesMixin._snapshot(current, current["slides"][0], "3:4")
        self.assertEqual(snapshot["requirements"]["editable_regions"], ["background", "text"])
        self.assertNotIn(import_note, generation_prompt(snapshot))
        self.review(output)
        approved = self.approve(output)["carousel"]["slides"][0]["outputs"][0]
        self.assertTrue(approved["approved"])
        self.assertEqual(approved["requirements_snapshot"], output["requirements_snapshot"])
        self.assertEqual(approved["review"]["requirements_signature"], output["requirements_signature"])

    def test_import_aspect_does_not_replace_production_aspect(self):
        saved = self.bench.save_carousel_requirements(self.identifier, self.body(requirements={"aspect": "9:16"}))["carousel"]
        result = self.bench.import_finished_images(self.identifier, self.body(directory="set", requirements={"aspect": "3:4"}))
        self.assertEqual(self.current()["image_aspect"], "9:16")
        self.assertEqual(self.current()["requirements"], saved["requirements"])
        self.assertTrue(all(output["aspect"] == "3:4" for output in result["outputs"]))

    def test_path_escape_damaged_image_and_wrong_copy_are_rejected(self):
        self.error(400, self.bench.import_finished_images, self.identifier, self.body(directory="../"))
        source_copy = self.folder / "copy.json"
        original = source_copy.read_bytes()
        changed = json.loads(original)
        changed["slides"][0]["body"] = "Wrong words"
        source_copy.write_text(json.dumps(changed), encoding="utf-8")
        self.error(409, self.import_images)
        source_copy.write_bytes(original)
        (self.folder / "PNG" / "01.png").write_bytes(b"not an image")
        self.error(400, self.import_images)
        self.assertFalse(self.current().get("finished_imports"))

    def test_import_allows_explicit_manual_confirmation_without_ai_review(self):
        output = self.import_images()["outputs"][0]
        approved = self.approve(output)["carousel"]["slides"][0]
        self.assertEqual(approved["output_id"], output["id"])
        self.assertEqual(approved["outputs"][0]["approval_actor"], "assistant_on_user_authorization")

    def test_manual_selection_can_replace_old_confirmed_image_despite_legacy_review_findings(self):
        output = self.import_images()["outputs"][0]
        self.review(output)
        self.approve(output)
        other = self.bench.carousel_finished_root / "set-new"
        shutil_source = self.folder
        import shutil
        shutil.copytree(shutil_source, other)
        image = Image.new("RGB", (144, 192), "orange")
        image.save(other / "PNG" / "01.png")
        image.save(other / "JPG" / "01.jpg", quality=98)
        result = self.bench.import_finished_images(self.identifier, self.body(directory="set-new", requirements=output["requirements_snapshot"]))
        candidate = result["outputs"][0]
        self.review(candidate, "needs_fix")
        self.bench.select_carousel_output(self.identifier, self.body(slide=1, output_id=candidate["id"]))
        self.approve(candidate)
        current = self.current()["slides"][0]
        self.assertEqual(current["output_id"], candidate["id"])
        self.assertTrue(current["outputs"][0]["approved"])

    def test_review_failure_retries_only_review_and_late_run_cannot_replace_new(self):
        output = self.import_images()["outputs"][0]
        body = {"carousel_id": self.identifier, "slide": 1, "output_id": output["id"]}
        signature = output["input_signature"]
        ids = ["REV-" + uuid4().hex[:12] for _ in range(2)]
        for identifier in ids:
            self.store.mutate("carousel_review_start", self.body(**body), {"id": identifier, "status": "running", "started_at": "start",
                "file_sha256": output["sha256"], "input_signature": signature, "requirements_signature": output.get("requirements_signature"), "scope": ["visual", "text"]})
        finish = {"status": "completed", "finished_at": "finish", "file_sha256": output["sha256"], "input_signature": signature,
                  "review": {"status": "pass", "summary": "新核对"}}
        self.store.mutate("carousel_review_finish", self.body(**body, review_id=ids[1]), finish)
        late = self.store.mutate("carousel_review_finish", self.body(**body, review_id=ids[0]), {**finish, "review": {"status": "needs_fix"}})
        self.assertTrue(late["review"]["stale"])
        self.assertEqual(late["output"]["review"]["review_id"], ids[1])
        self.assertEqual(late["output"]["review"]["status"], "pass")
        self.approve(output)  # Manual confirmation does not require an AI review scope.
        self.assertFalse(self.current()["generations"])
        self.assertEqual(len(self.current()["slides"][0]["outputs"]), 1)

    def test_changed_manifest_hash_and_mismatched_jpg_are_rejected(self):
        source_copy = self.folder / "copy.json"
        (self.folder / "manifest.json").write_text(json.dumps({"copy_sha256": "wrong"}), encoding="utf-8")
        self.error(409, self.import_images)
        (self.folder / "manifest.json").unlink()
        Image.new("RGB", (144, 192), "black").save(self.folder / "JPG" / "01.jpg")
        self.error(400, self.import_images)

    def test_source_hand_authorization_is_per_slide_and_no_hand_rule_wins(self):
        (self.folder / "manifest.json").write_text(json.dumps({"hand_policy": {"authorized_change_slides": [1], "no_hand_slide": 2}}), encoding="utf-8")
        result = self.import_images()
        self.assertTrue(result["outputs"][0]["requirements_snapshot"]["hands_authorized"])
        self.assertFalse(result["outputs"][1]["requirements_snapshot"]["hands_authorized"])

    def test_interrupted_review_keeps_image_and_retry_recertifies_same_version(self):
        output = self.import_images()["outputs"][0]
        root = self.store.root / "carousels" / self.identifier
        original = (root / output["file"]).read_bytes()
        self.store.mutate("carousel_review_start", self.body(carousel_id=self.identifier, slide=1, output_id=output["id"]),
            {"id": "REV-" + uuid4().hex[:12], "status": "running", "started_at": "start", "file_sha256": output["sha256"],
             "input_signature": output["input_signature"], "requirements_signature": output.get("requirements_signature"), "scope": ["visual", "text", "product", "evidence"]})
        self.store.interrupt_running()
        self.assertEqual(self.current()["slides"][0]["outputs"][0]["review"]["status"], "review_failed")
        self.approve(output)
        self.review(output)
        self.approve(output)
        self.assertEqual((root / output["file"]).read_bytes(), original)
        self.assertEqual(len(self.current()["slides"][0]["outputs"]), 1)
        self.assertFalse(self.current()["generations"])

    def test_late_review_after_input_change_preserves_original_binding(self):
        output = self.import_images()["outputs"][0]
        review_id = "REV-" + uuid4().hex[:12]
        body = {"carousel_id": self.identifier, "slide": 1, "output_id": output["id"]}
        self.store.mutate("carousel_review_start", self.body(**body), {"id": review_id, "status": "running", "started_at": "start",
            "file_sha256": output["sha256"], "input_signature": output["input_signature"], "requirements_signature": output.get("requirements_signature"), "scope": ["visual", "text", "product", "evidence"]})
        requirements = copy.deepcopy(self.current().get("requirements", {}))
        requirements["style"] = "Different current style"
        self.bench.save_carousel_requirements(self.identifier, self.body(requirements=requirements))
        result = self.store.mutate("carousel_review_finish", self.body(**body, review_id=review_id), {"status": "completed", "finished_at": "finish",
            "file_sha256": "different bytes", "input_signature": "different inputs", "review": {"status": "pass"}})
        self.assertTrue(result["review"]["stale"])
        self.assertEqual(result["review"]["file_sha256"], output["sha256"])
        self.assertEqual(result["review"]["input_signature"], output["input_signature"])
        self.assertEqual(result["output"]["review"]["status"], "pending")
        self.error(409, self.approve, output)

    def test_concurrent_imports_are_isolated_by_carousel(self):
        other = self.create()
        results, failures = [], []
        def run(identifier):
            try:
                results.append(self.bench.import_finished_images(identifier, self.body(directory="set", requirements={"mode": "generated_visual"})))
            except Exception as failure:
                failures.append(failure)
        threads = [threading.Thread(target=run, args=(identifier,)) for identifier in (self.identifier, other["id"])]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(failures)
        self.assertEqual(len(results), 2)
        ids = [set(result["import"]["output_ids"]) for result in results]
        self.assertFalse(ids[0] & ids[1])
        self.assertEqual(len(self.store.carousel(other["id"])["finished_imports"]), 1)

    def test_requirement_changed_during_copy_is_not_overwritten(self):
        mutate = self.store.mutate
        def concurrent_change(kind, body, extra=None):
            if kind == "carousel_import_finished":
                mutate("carousel_requirements", self.body(carousel_id=self.identifier, requirements={"mode": "photo_preserving", "style": "New user request"}))
            return mutate(kind, body, extra)
        with patch.object(self.store, "mutate", side_effect=concurrent_change):
            self.error(409, self.import_images)
        self.assertEqual(self.current()["requirements"]["style"], "New user request")
        self.assertFalse(self.current().get("finished_imports"))


if __name__ == "__main__":
    unittest.main()
