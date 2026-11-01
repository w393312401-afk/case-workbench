"""Selected meaning-group provenance across script and video APIs, with disposable data."""
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
from production_store import ProductionStore
from elevenlabs_client import ElevenLabsClient, VoiceError


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
        self.server.app.ai.bridge = FakeBridge(output="")

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

    def test_old_scripts_without_a_selected_index_still_match_by_spoken_lines(self):
        script = self.adopt(selected=False)
        self.assertNotIn("variant_index", script["source"])
        self.assertEqual(self.job(script)["lines"][0]["proof"], "PROOF_1")

    def test_changed_lines_do_not_borrow_another_variants_matching_proof(self):
        script = self.adopt(index=0, text="A different line.", output=candidate("A different line."))
        job = self.job(script, variant_index=1)
        self.assertEqual(job["source"]["variant_index"], 0)
        self.assertEqual(job["lines"][0]["spoken"], "A different line.")
        self.assertEqual(job["lines"][0]["proof"], "")
        self.assertIn("所选变体", job["problems"][0])

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

    def test_edited_or_cleared_selected_translation_reaches_video_without_changing_proof(self):
        for translation in ("用户修订中文。", ""):
            with self.subTest(translation=translation):
                script = self.adopt(index=1, chinese=translation)
                job = self.job(script)
                self.assertEqual((job["lines"][0]["zh"], job["lines"][0]["proof"]), (translation, "PROOF_2"))

    def test_legacy_nonempty_translation_is_used_but_old_empty_translation_keeps_candidate_chinese(self):
        for translation, expected in (("历史已采纳译文。", "历史已采纳译文。"), ("", "中文1。")):
            with self.subTest(translation=translation):
                script = self.adopt(selected=False, chinese=translation)
                self.assertEqual(self.job(script)["lines"][0]["zh"], expected)

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

if __name__ == "__main__":
    unittest.main()
