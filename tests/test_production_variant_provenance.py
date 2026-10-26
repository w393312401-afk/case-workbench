"""Selected meaning-group provenance across script, video and carousel APIs, with disposable data."""
import copy
from pathlib import Path
import time
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_server as fixture
from test_ai_tasks import FakeBridge
from test_production import FakeEleven
from test_production_carousel_table import TABLE
from production_store import ProductionStore
from elevenlabs_client import ElevenLabsClient, VoiceError
from workbench_carousel_table import CarouselTableMixin


def candidate(second_spoken="Same words."):
    blocks = []
    for number, spoken in ((1, "Same words."), (2, second_spoken)):
        blocks.append("\n".join([
            f"**变体 #{number} · 画面{number}**", "**口播（配音用）**", "```", spoken, "```",
            "**对应中文**", "```", f"中文{number}。", "```", "**意群表**",
            "| # | 口播 | 画面要证明什么 | 素材标签 | 证据 |", "|---|---|---|---|---|",
            f"| 1 | {spoken} | PROOF_{number} | TAG_{number} | EVIDENCE_{number} |",
        ]))
    return "\n\n".join(blocks)


class VariantProvenanceTests(unittest.TestCase):
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        photo = Path(self.server.app.edit_skill_dir).parent / "tiktok-photo-commerce"
        (photo / "references").mkdir(parents=True, exist_ok=True)
        (photo / "SKILL.md").write_text("---\nname: tiktok-photo-commerce\n---\n# 图文", encoding="utf-8")
        (photo / "references" / "deliverables.md").write_text("# copy.json", encoding="utf-8")
        self.bridge = FakeBridge(output=TABLE)
        self.server.app.ai.bridge = self.bridge

    def ok(self, method, path, body=None):
        status, _, data = self.request(method, path, body)
        self.assertEqual(status, 200, data)
        return data

    def adoption(self, *, text="Same words.", chinese="对应中文。", index=1, selected=True, output=None):
        source = {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 1,
                  "product": "202H", "candidate_text": output or candidate(), "candidate_sha256": "frozen", "snapshot": {}}
        body = {"revision": self.ok("GET", "/api/creation?product=202H")["revision"], "request_id": uuid4().hex,
                "script": {"product": "202H", "title": "选中意群", "text": text, "translation_zh": chinese, "confirmed": True},
                "source": {key: source[key] for key in ("task_id", "round_id", "task_revision")}}
        if selected:
            body["source"]["variant_index"] = index
        return body, source

    def adopt(self, **options):
        body, source = self.adoption(**options)
        with patch.object(self.server.app.ai, "candidate", return_value=copy.deepcopy(source)):
            script = self.ok("POST", "/api/scripts", body)["script"]
        return script

    def job(self, script, **extra):
        return self.ok("POST", "/api/production/jobs", {
            "revision": self.ok("GET", "/api/production")["revision"], "request_id": uuid4().hex,
            "job": {"script_id": script["id"], **extra}})["job"]

    def table(self, script):
        started = self.ok("POST", "/api/production/carousel-tables", {
            "request_id": uuid4().hex, "script_id": script["id"]})["table"]
        return self.wait_table(started)

    def wait_table(self, started):
        deadline = time.monotonic() + 5
        table = started
        while table["status"] == "running" and time.monotonic() < deadline:
            time.sleep(.02)
            table = self.ok("GET", f"/api/production/carousel-tables/{started['id']}")["table"]
        self.assertEqual(table["status"], "completed", table)
        return table

    def test_same_spoken_words_keep_the_selected_meaning_groups_in_both_paths(self):
        script = self.adopt(index=1)
        self.assertEqual(script["source"]["variant_index"], 1)
        self.assertEqual(self.ok("GET", "/api/creation?product=202H")["scripts"][0]["source"]["variant_index"], 1)
        job = self.job(script, variant_index=0)  # a new request cannot override frozen provenance
        self.assertEqual((job["source"]["variant_index"], job["lines"][0]["proof"]), (1, "PROOF_2"))
        table = self.table(script)
        self.assertEqual(table["variant_index"], 1)
        self.assertEqual(table["variant_label"], "变体 #2 · 画面2")
        self.assertIn("PROOF_2", self.bridge.calls[-1]["prompt"])
        self.assertNotIn("PROOF_1", self.bridge.calls[-1]["prompt"])
        carousel = self.ok("POST", "/api/production/carousels", {
            "revision": self.ok("GET", "/api/production")["revision"], "request_id": uuid4().hex,
            "carousel": {"script_id": script["id"], "table_id": table["id"]}})["carousel"]
        self.assertEqual(carousel["source"]["variant_index"], 1)

    def test_old_scripts_without_a_selected_index_still_match_by_spoken_lines(self):
        script = self.adopt(selected=False)
        self.assertNotIn("variant_index", script["source"])
        self.assertEqual(self.job(script)["lines"][0]["proof"], "PROOF_1")
        table = self.table(script)
        self.assertEqual(table["variant_label"], "变体 #1 · 画面1")
        self.assertIn("PROOF_1", self.bridge.calls[-1]["prompt"])

    def test_changed_lines_do_not_borrow_another_variants_matching_proof(self):
        script = self.adopt(index=0, text="A different line.", output=candidate("A different line."))
        job = self.job(script, variant_index=1)
        self.assertEqual(job["source"]["variant_index"], 0)
        self.assertEqual(job["lines"][0]["spoken"], "A different line.")
        self.assertEqual(job["lines"][0]["proof"], "")
        self.assertIn("所选变体", job["problems"][0])
        table = self.table(script)
        self.assertIn("所选变体", table["note"])
        self.assertNotIn("PROOF_1", self.bridge.calls[-1]["prompt"])
        self.assertNotIn("PROOF_2", self.bridge.calls[-1]["prompt"])

    def test_derived_script_production_uses_its_saved_words_and_retains_the_old_order(self):
        original = self.adopt(index=1)
        old_job = self.job(original)
        old_detail = self.ok("GET", f"/api/production/jobs/{old_job['id']}")
        self.assertIn("Same words.", old_detail["variant"]["markdown"])
        updated_text = "Choose the updated guide.\nKeep every pass steady."
        updated = self.ok("POST", "/api/scripts", {
            "revision": self.ok("GET", "/api/creation?product=202H")["revision"], "request_id": uuid4().hex,
            "script": {"product": "202H", "title": "采用稿改稿", "text": updated_text,
                       "translation_zh": "选用更新后的导杆。\n每一遍都保持稳定。", "parent_id": original["id"], "confirmed": True},
        })["script"]
        self.assertEqual(updated["source"], original["source"])
        new_job = self.job(updated)
        detail = self.ok("GET", f"/api/production/jobs/{new_job['id']}")
        self.assertEqual(detail["tts"]["text"], updated_text)
        self.assertEqual([line["spoken"] for line in detail["job"]["lines"]], updated_text.splitlines())
        self.assertTrue(detail["variant"]["manual"])
        self.assertIn(updated_text, detail["variant"]["markdown"])
        self.assertIn("选用更新后的导杆。", detail["variant"]["markdown"])
        self.assertNotIn("Same words.", detail["variant"]["markdown"])
        self.assertEqual([line["proof"] for line in detail["job"]["lines"]], ["", ""])

        self.server.app.voices = ElevenLabsClient(key_provider=lambda: "sk_test", transport=FakeEleven())
        take = self.ok("POST", f"/api/production/jobs/{new_job['id']}/takes", {
            "request_id": uuid4().hex, "voice_id": "VOICE123456789", "model_id": "eleven_v4", "settings": {"stability": 0.5},
        })["take"]
        self.assertEqual(take["tts_lines"], updated_text.splitlines())

        # Current visual requirements stay attached when the source candidate no longer describes this wording.
        self.ok("POST", f"/api/production/jobs/{new_job['id']}/lines", {
            "request_id": uuid4().hex, "revision": self.ok("GET", "/api/production")["revision"],
            "lines": [{"number": 1, "proof": "NEW_PROOF_1", "tags": "NEW_TAG", "evidence": "新拍"},
                      {"number": 2, "proof": "NEW_PROOF_2", "tags": "NEW_TAG", "evidence": "新拍"}],
            "supplement": {"hook_visual": "展示新版导杆", "hook_text": "UPDATED GUIDE", "pickups": "补拍新版安装"},
        })
        detail = self.ok("GET", f"/api/production/jobs/{new_job['id']}")
        self.assertEqual([line["proof"] for line in detail["job"]["lines"]], ["NEW_PROOF_1", "NEW_PROOF_2"])
        for value in (updated_text, "展示新版导杆", "UPDATED GUIDE", "补拍新版安装"):
            self.assertIn(value, detail["variant"]["markdown"])
        self.assertNotIn("Same words.", detail["variant"]["markdown"])

        # An explicitly completed fill remains the preferred full script, including its additional notes.
        filled_markdown = "**口播（配音用）**\n```\n" + updated_text + "\n```\n\n**读法：** 按新版读法。"
        store = self.server.app.production
        store.mutate("job_fill_start", {"request_id": uuid4().hex, "job_id": new_job["id"], "task_id": "AIT-0123456789ab"})
        store.mutate("job_fill_finish", {"request_id": uuid4().hex, "job_id": new_job["id"], "task_id": "AIT-0123456789ab",
                                        "lines": [{}, {}], "markdown": filled_markdown})
        filled = self.ok("GET", f"/api/production/jobs/{new_job['id']}")
        self.assertEqual(filled["variant"]["markdown"], filled_markdown)
        self.assertFalse(filled["variant"]["manual"])
        self.assertEqual([line["proof"] for line in filled["job"]["lines"]], ["NEW_PROOF_1", "NEW_PROOF_2"])
        unchanged = self.ok("GET", f"/api/production/jobs/{old_job['id']}")
        self.assertEqual(unchanged["job"], old_detail["job"])
        self.assertEqual(unchanged["variant"], old_detail["variant"])
        self.assertEqual(self.ok("GET", f"/api/scripts/{original['id']}")["script"]["text"], original["text"])

    def test_manual_split_keeps_new_lines_and_warns_without_old_visual_requirements(self):
        script = self.adopt(index=1, text="Same\nwords.")
        job = self.job(script)
        self.assertEqual([line["spoken"] for line in job["lines"]], ["Same", "words."])
        self.assertEqual([line["proof"] for line in job["lines"]], ["", ""])
        table = self.table(script)
        self.assertIn("未沿用原画面要求", table["note"])

    def test_edited_or_cleared_selected_translation_reaches_video_and_photo_without_changing_proof(self):
        for translation in ("用户修订中文。", ""):
            with self.subTest(translation=translation):
                script = self.adopt(index=1, chinese=translation)
                job = self.job(script)
                self.assertEqual((job["lines"][0]["zh"], job["lines"][0]["proof"]), (translation, "PROOF_2"))
                table = self.table(script)
                prompt = self.bridge.calls[-1]["prompt"]
                self.assertIn(f"| 1 | Same words. | {translation} | PROOF_2 | TAG_2 | EVIDENCE_2 |", prompt)
                self.assertNotIn("中文2。", prompt)
                self.assertEqual(table["variant_index"], 1)

    def test_legacy_nonempty_translation_is_used_but_old_empty_translation_keeps_candidate_chinese(self):
        for translation, expected in (("历史已采纳译文。", "历史已采纳译文。"), ("", "中文1。")):
            with self.subTest(translation=translation):
                script = self.adopt(selected=False, chinese=translation)
                self.assertEqual(self.job(script)["lines"][0]["zh"], expected)
                self.table(script)
                self.assertIn(f"| 1 | Same words. | {expected} | PROOF_1 |", self.bridge.calls[-1]["prompt"])

    def test_selected_index_is_validated_against_the_server_candidate(self):
        for index in (-1, 2, True, "1", None):
            with self.subTest(index=index):
                body, source = self.adoption(index=index)
                with patch.object(self.server.app.ai, "candidate", return_value=source):
                    status, _, response = self.request("POST", "/api/scripts", body)
                self.assertEqual(status, 400, response)
                self.assertIn("变体编号", response["error"])
        self.assertEqual(self.ok("GET", "/api/creation?product=202H")["scripts"], [])

    def test_adoption_receipt_survives_source_loss_and_rejects_another_selected_index(self):
        body, source = self.adoption(index=1)
        with patch.object(self.server.app.ai, "candidate", return_value=source):
            first = self.ok("POST", "/api/scripts", body)["script"]
        with patch.object(self.server.app.ai, "candidate", side_effect=AssertionError("replay must not reread source")):
            replay = self.ok("POST", "/api/scripts", body)["script"]
            changed = copy.deepcopy(body)
            changed["source"]["variant_index"] = 0
            status, _, result = self.request("POST", "/api/scripts", changed)
        self.assertEqual((first["id"], first["source"]["variant_index"]), (replay["id"], replay["source"]["variant_index"]))
        self.assertEqual(status, 409, result)
        self.assertEqual(len(self.ok("GET", "/api/creation?product=202H")["scripts"]), 1)

    def test_completed_table_replays_without_skill_client_or_process_memory(self):
        script = self.adopt(index=1)
        body = {"request_id": uuid4().hex, "script_id": script["id"], "instruction": "只做3张"}
        started = self.ok("POST", "/api/production/carousel-tables", body)["table"]
        finished = self.wait_table(started)
        with patch.object(self.server.app.ai, "_available", side_effect=AssertionError("replay must not check client")), \
                patch.object(self.server.app.ai, "resolve_default_settings", side_effect=AssertionError("replay must not resolve defaults")):
            replay = self.ok("POST", "/api/production/carousel-tables", body)["table"]
        self.assertEqual((replay["id"], replay["status"]), (finished["id"], "completed"))
        restarted = CarouselTableMixin()
        restarted.lock = threading.RLock()
        restarted.production = ProductionStore(self.runtime)
        # A fresh handler has no live state, creation manager, skill directory or AI client.
        restored = restarted.start_carousel_table(body)["table"]
        self.assertEqual((restored["id"], restored["status"]), (finished["id"], "completed"))
        self.assertEqual(restored["variant_index"], 1)
        self.assertEqual(len(self.bridge.calls), 1)
        with self.assertRaises(VoiceError) as caught:
            restarted.start_carousel_table({**body, "instruction": "另做4张"})
        self.assertEqual(caught.exception.status, 409)

    def test_running_table_alias_receipts_survive_completion_and_changed_input_is_refused(self):
        self.bridge = FakeBridge(output=TABLE, blocked=True)
        self.server.app.ai.bridge = self.bridge
        self.addCleanup(self.bridge.gate.set)
        script = self.adopt(index=1)
        body = {"request_id": uuid4().hex, "script_id": script["id"], "instruction": "3张"}
        started = self.ok("POST", "/api/production/carousel-tables", body)["table"]
        self.assertEqual(started["status"], "running")
        alias = {**body, "request_id": uuid4().hex}
        self.assertEqual(self.ok("POST", "/api/production/carousel-tables", alias)["table"]["id"], started["id"])
        conflict = {**body, "request_id": uuid4().hex, "instruction": "4张"}
        status, _, response = self.request("POST", "/api/production/carousel-tables", conflict)
        self.assertEqual(status, 409, response)
        with patch.object(self.server.app.ai, "resolve_default_settings", return_value={"model": "changed-model", "effort": None}):
            status, _, response = self.request("POST", "/api/production/carousel-tables", {**body, "request_id": uuid4().hex})
        self.assertEqual(status, 409, response)
        with patch.object(self.server.app.ai, "resolve_default_settings", return_value={"model": started["model"], "effort": "changed-effort"}):
            status, _, response = self.request("POST", "/api/production/carousel-tables", {**body, "request_id": uuid4().hex})
        self.assertEqual(status, 409, response)
        self.bridge.gate.set()
        self.wait_table(started)
        replay = self.ok("POST", "/api/production/carousel-tables", alias)["table"]
        self.assertEqual((replay["id"], replay["status"]), (started["id"], "completed"))
        self.assertEqual(len(self.bridge.calls), 1)
        regenerated = self.ok("POST", "/api/production/carousel-tables", {**body, "request_id": uuid4().hex})["table"]
        self.wait_table(regenerated)
        self.assertNotEqual(regenerated["id"], started["id"])
        self.assertEqual(len(self.bridge.calls), 2)


if __name__ == "__main__":
    unittest.main()
