"""Frozen meaning groups survive table parsing, creation, edits and readback."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))
from direction_store import fingerprint
from elevenlabs_client import VoiceError
from production_store import ProductionStore, carousel_input_signature
from variant_parse import parse_carousels
from workbench_carousel import CarouselWorkbenchMixin
from workbench_carousel_table import CarouselTableMixin, TABLE_FORMAT
from workbench_video_carousel import PICKS_FORMAT, video_handoff, VideoCarouselMixin


RECORDS = [
    {"number": 1, "spoken": "Pick your angle.", "zh": "选择角度。", "proof": "旋钮与刻度", "tags": "调角", "evidence": "操作"},
    {"number": 2, "spoken": "Check the clamp position.", "zh": "检查装夹位置。", "proof": "装夹条件", "tags": "装夹", "evidence": "实拍"},
    {"number": 3, "spoken": "Choose a finishing stage.", "zh": "选择精修阶段。", "proof": "磨料更换", "tags": "磨料", "evidence": "步骤"},
]


def reply(*, legacy=False):
    slides = []
    for number in (1, 2, 3):
        slide = {"number": number, "title_lines": [f"Title {number}"], "body": "Original words.",
                 "title_zh": "中文标题", "body_zh": "原意。"}
        if not legacy:
            slide.update(source_lines=[number], proof_goal=f"本张任务 {number}",
                         visual_method="动作特写", evidence_requirement="旋钮动作清楚，不能用整机图代替")
        slides.append(slide)
    return "```json\n" + json.dumps({"content_type": "photo_carousel", "slides": slides}, ensure_ascii=False) + "\n```"


class Bench(CarouselWorkbenchMixin, CarouselTableMixin, VideoCarouselMixin):
    def __init__(self, root):
        self.lock = threading.RLock()
        self.production = ProductionStore(root, self.lock)
        self.script = {"id": "SCR-0123456789ab", "version": 2, "family_id": "FAM-test", "product": "202H",
                       "direction_id": "202H-S2", "title": "调角", "text": "Updated source.", "source": {}}
        self.creation = self

    def get_script(self, identifier):
        return copy.deepcopy(self.script)


class LineageDataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="carousel-lineage-")
        self.addCleanup(temporary.cleanup)
        self.bench = Bench(temporary.name)
        self.table_id = "TBL-0123456789ab"

    def body(self, **fields):
        return {"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"], **fields}

    def table(self, output=None, **extra):
        record = {"id": self.table_id, "status": "completed", "script_id": self.bench.script["id"],
                  "script_version": 2, "output": output or reply(), "records": copy.deepcopy(RECORDS),
                  **{key: "" for key in ("product", "direction_id", "title", "variant_label", "note", "instruction",
                                         "model", "effort", "started_at", "finished_at", "error", "progress")}, **extra}
        folder = self.bench._table_dir(self.table_id)
        folder.mkdir(parents=True, exist_ok=True)
        self.bench._write_table(folder, record)
        return record

    def create(self):
        return self.bench.create_carousel(self.body(carousel={"script_id": self.bench.script["id"],
                                                               "table_id": self.table_id}))['carousel']

    def test_parse_create_edit_read_preserves_mapping_and_frozen_source(self):
        parsed = parse_carousels(reply())[0]
        self.assertEqual(parsed["slides"][0]["source_lines"], [1])
        # The table snapshot wins over a stale duplicate and the current script.
        self.table(script_context={"script_id": "wrong", "script_version": 1, "records": []})
        created = self.create()
        frozen = created["script_context"]
        self.assertEqual(frozen["records"], RECORDS)
        self.assertEqual((frozen["script_id"], frozen["script_version"]), (self.bench.script["id"], 2))
        slides = [{"number": slide["number"], "proof_goal": "能看清调角动作" if slide["number"] == 1 else slide["proof_goal"]}
                  for slide in created["slides"]]
        edited = self.bench.save_carousel_slides(created["id"], self.body(slides=slides,
            script_context={"records": [{"number": 99, "spoken": "forged"}]}))["carousel"]
        self.assertEqual(edited["slides"][0]["proof_goal"], "能看清调角动作")
        self.assertEqual(edited["slides"][0]["source_lines"], [1])
        self.assertEqual(edited["slides"][0]["evidence_requirement"], parsed["slides"][0]["evidence_requirement"])
        self.bench.script.update(version=3, text="A later script with a different claim.")
        stored = self.bench.production.carousel(created["id"])
        self.assertEqual(stored["script_context"], frozen)
        self.assertEqual(stored["script_version"], 2)
        self.assertEqual(stored["slides"][0]["visual_method"], "动作特写")

    def test_markdown_columns_survive_when_json_has_no_lineage(self):
        table = ("| 张 | 原稿意群 | 本张任务 | 表达方式 | 画面证据 | 作用 | 叠字 | 正文 | 中文 | 素材来源 | 禁写 |\n"
                 "|---|---|---|---|---|---|---|---|---|---|---|\n"
                 "| 1 | [1,2] | 看见装夹条件 | 双图对照 | 两种装夹实拍 | 承接 | Title 1 | Original words. | 原意 | 素材A | 通用刻度 |\n")
        slide = parse_carousels(table + reply(legacy=True))[0]["slides"][0]
        self.assertEqual(slide["source_lines"], [1, 2])
        self.assertEqual(slide["proof_goal"], "看见装夹条件")
        self.assertEqual(slide["evidence_requirement"], "两种装夹实拍")
        self.assertEqual(slide["role"], "承接")

    def test_public_table_uses_context_only_snapshot_and_explicit_empty_records_win(self):
        context = {"script_id": self.bench.script["id"], "script_version": 2, "records": copy.deepcopy(RECORDS),
                   "lineage_required": True, "source_note": "冻结来源说明"}
        record = self.table(script_context=context)
        del record["records"]
        public = self.bench._public_carousel_table(record, [], include_result=True)
        self.assertEqual(public["carousels"][0]["script_context"], context)
        public["carousels"][0]["script_context"]["records"].clear()
        self.assertEqual(len(record["script_context"]["records"]), 3)
        record["records"] = []
        empty = self.bench._public_carousel_table(record, [], include_result=True)["carousels"][0]["script_context"]
        self.assertEqual(empty["records"], [])
        self.assertTrue(empty["lineage_required"])
        self.assertEqual(empty["source_note"], "冻结来源说明")

    def test_new_script_and_video_tables_mark_lineage_required_and_creation_preserves_it(self):
        skills = self.bench.production.root.parent / "skills"
        photo = skills / "tiktok-photo-commerce"
        photo.mkdir(parents=True)
        (photo / "SKILL.md").write_text("# Test skill", encoding="utf-8")
        self.bench.edit_skill_dir = skills / "editing"
        self.bench.ai = SimpleNamespace(resolve_default_settings=lambda *args, **kwargs: {"model": "codex-default", "effort": "medium"},
                                        _available=lambda: None, _resources=lambda *_: [])
        with patch("workbench_carousel_table.threading.Thread"):
            table = self.bench.start_carousel_table({"request_id": uuid4().hex, "script_id": self.bench.script["id"]})["table"]
        stored = json.loads((self.bench._table_dir(table["id"]) / "state.json").read_text(encoding="utf-8"))
        self.assertTrue(stored["script_context"]["lineage_required"])
        job = {"id": "JOB-test", "lines": copy.deepcopy(RECORDS)}
        with patch("workbench_video_carousel.threading.Thread"):
            self.bench._start_video_carousel(job, self.bench.script, uuid4().hex, "hash", "", 3, "codex-default", "medium",
                                            None, None, "视频", None, photo)
        video = next(record for record in self.bench._read_tables() if record.get("origin") == "video")
        self.assertTrue(video["script_context"]["lineage_required"])
        # Creation uses the frozen snapshot even when the returned slide table
        # omitted all mapping fields; the reviewer can distinguish new work.
        video.update(status="completed", output=reply(legacy=True))
        self.bench._write_table(self.bench._table_dir(video["id"]), video)
        self.table_id = video["id"]
        self.assertTrue(self.create()["script_context"]["lineage_required"])

    def test_new_ai_table_missing_fields_reports_repair_without_breaking_legacy(self):
        record = self.table(reply(legacy=True), script_context={"records": RECORDS})
        result = self.bench._saved_carousel_table_result(record)[0]
        self.assertTrue(any("第 1 张缺少原稿意群、本张任务、表达方式、画面证据" in p for p in result["problems"]))
        del record["script_context"]
        old = self.bench._saved_carousel_table_result(record)[0]
        self.assertFalse(any("缺少原稿意群" in p for p in old["problems"]))

    def test_invalid_reference_cannot_be_silently_dropped_during_parse_and_create(self):
        for bad in ([0], [-1], [True], [1, 1], [2, 1], "1", [99]):
            with self.subTest(bad=bad):
                payload = json.loads(reply().split("\n")[1])
                payload["slides"][0]["source_lines"] = bad
                output = "```json\n" + json.dumps(payload) + "\n```"
                self.table(output)
                with self.assertRaises(VoiceError):
                    self.create()

    def test_edit_rejects_unknown_and_reordered_meaning_groups_without_saving(self):
        self.table()
        created = self.create()
        for refs in ([99], [-2], [3]):
            with self.subTest(refs=refs):
                slides = [{"number": slide["number"], "source_lines": refs if slide["number"] == 1 else slide["source_lines"]}
                          for slide in created["slides"]]
                with self.assertRaises(VoiceError):
                    self.bench.save_carousel_slides(created["id"], self.body(slides=slides))
                self.assertEqual(self.bench.production.carousel(created["id"])["slides"][0]["source_lines"], [1])

    def test_historic_signature_stays_exact_and_new_fields_invalidate_render(self):
        slide = parse_carousels(reply(legacy=True))[0]["slides"][0]
        slide["base"] = None
        item = {"script_id": "SCR-test", "script_version": 2}
        old = fingerprint({"script": ["SCR-test", 2], "aspect": "3:4",
                           "slide": {key: slide.get(key) for key in ("number", "title_lines", "body", "role", "source", "forbidden")},
                           "base_id": None})
        self.assertEqual(carousel_input_signature(item, slide, "3:4"), old)
        slide["source_lines"] = [1]
        mapped = carousel_input_signature(item, slide, "3:4")
        self.assertNotEqual(mapped, old)
        slide["proof_goal"] = "装夹动作"
        self.assertNotEqual(carousel_input_signature(item, slide, "3:4"), mapped)
        item["script_context"] = {"script_id": "SCR-test", "script_version": 2, "records": RECORDS}
        frozen = carousel_input_signature(item, slide, "3:4")
        item["script_context"]["records"] = []
        self.assertNotEqual(carousel_input_signature(item, slide, "3:4"), frozen)

    def test_both_ai_entry_points_require_source_task_evidence_and_conditions(self):
        handoff = self.bench._handoff(self.bench.script, "调角", RECORDS, "", "")
        video = video_handoff({"id": "JOB-test", "lines": RECORDS}, self.bench.script, [], [],
                              duration=3, source_label="成片", slides_wanted=3, instruction="", note="",
                              context_dir=self.bench.production.root)
        for prompt in (handoff + TABLE_FORMAT, video + PICKS_FORMAT):
            for fragment in ("source_lines", "proof_goal", "visual_method", "evidence_requirement", "限定条件", "不强套痛点开头"):
                self.assertIn(fragment, prompt)
        self.assertIn("素材标签", video)
        self.assertIn("步骤", video)

    def legacy_job(self):
        self.table(reply(legacy=True))
        item = self.create()
        data = json.loads(self.bench.production.path.read_text(encoding="utf-8"))
        next(row for row in data["carousels"] if row["id"] == item["id"]).pop("script_context", None)
        self.bench.production.write(data)
        return self.bench.production.carousel(item["id"])

    def test_legacy_mapping_save_recovers_exact_table_source_and_replays(self):
        item = self.legacy_job()
        self.bench.script.update(version=99, text="The current version must not be used.")
        body = self.body(slides=[{"number": s["number"], "source_lines": [s["number"]],
                                "proof_goal": "对应动作", "evidence_requirement": "待补动作特写"} for s in item["slides"]],
                         script_context={"records": [{"number": 1, "spoken": "forged"}]})
        result = self.bench.save_carousel_slides(item["id"], body)
        context = result["carousel"]["script_context"]
        self.assertEqual(context["records"], RECORDS)
        self.assertEqual(context["script_version"], 2)
        self.assertEqual(context["source_table_id"], self.table_id)
        self.assertEqual(len(context["source_sha256"]), 64)
        self.assertTrue(context["lineage_required"])
        self.assertEqual(self.bench.save_carousel_slides(item["id"], body), result)
        self.assertEqual([s["title_lines"] for s in result["carousel"]["slides"]], [s["title_lines"] for s in item["slides"]])

    def test_legacy_save_without_mapping_does_not_change_context_or_signature(self):
        item = self.legacy_job()
        before = carousel_input_signature(item, item["slides"][0], "3:4")
        saved = self.bench.save_carousel_slides(item["id"], self.body(slides=[{"number": s["number"]} for s in item["slides"]]))["carousel"]
        self.assertNotIn("script_context", saved)
        self.assertEqual(before, carousel_input_signature(saved, saved["slides"][0], "3:4"))

    def test_legacy_mapping_refuses_wrong_source_or_absent_original_without_writing(self):
        item = self.legacy_job()
        for changed in ({"script_version": 77}, {"records": []}):
            self.table(reply(legacy=True), **changed)
            before = self.bench.production.current_revision()
            with self.assertRaises(VoiceError):
                self.bench.save_carousel_slides(item["id"], self.body(slides=[{"number": s["number"], "source_lines": [s["number"]]} for s in item["slides"]]))
            self.assertEqual(before, self.bench.production.current_revision())
            self.assertNotIn("script_context", self.bench.production.carousel(item["id"]))


if __name__ == "__main__":
    unittest.main()
