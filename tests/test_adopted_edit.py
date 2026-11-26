"""采用稿逐句改：新版本、意群表跟着句子走、改中文的句子按上下文重译。只用临时运行目录和假的 AI。"""
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_server as fixture
from test_ai_tasks import FakeBridge
import test_completed_task_import as imported
from test_completed_task_import import CANDIDATE
import adopted_edit
from creation_store import CreationError
from variant_parse import parse_variants
from workbench_completed_import import import_completed_task


class AdoptedEditTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    body = imported.CompletedTaskImportTests.body

    def adopted(self):
        app = self.server.app
        app.ai.bridge = FakeBridge()
        result = import_completed_task(app, self.body())
        return app, result["scripts"][0]

    def test_a_typed_change_saves_the_next_version_and_keeps_the_table(self):
        app, script = self.adopted()
        lines = [{"source": 2, "spoken": "Then follow the guide 1.", "zh": "再沿着导杆 1。", "translate": False},
                 {"source": 1, "spoken": "Hold the knife 1.", "zh": "夹好刀具 1。", "translate": False},
                 {"source": None, "spoken": "Done in a minute.", "zh": "一分钟就好。", "translate": False}]
        result = app.edit_script_lines(script["id"], {"request_id": uuid4().hex, "lines": lines})
        saved = result["script"]
        self.assertEqual(app.ai.bridge.calls, [])
        self.assertEqual((saved["version"], saved["parent_id"], saved["family_id"]), (2, script["id"], script["family_id"]))
        self.assertEqual(saved["text"], "Then follow the guide 1.\nHold the knife 1.\nDone in a minute.")
        self.assertEqual(saved["translation_zh"], "再沿着导杆 1。\n夹好刀具 1。\n一分钟就好。")
        self.assertTrue(result["edit"]["table_kept"])
        stored = app.creation.get_script(saved["id"])
        variants = parse_variants(stored["source"]["candidate_text"], loose=False)
        edited = variants[stored["source"]["variant_index"]]
        self.assertEqual([line["spoken"] for line in edited["lines"]], saved["text"].splitlines())
        self.assertEqual([line["proof"] for line in edited["lines"]], ["待更新：导杆引导 1", "夹牢刀具 1", "待补"])
        # The other draft of the round and the version it came from are untouched.
        self.assertEqual(variants[1]["lines"], parse_variants(CANDIDATE, loose=False)[1]["lines"])
        self.assertEqual(app.creation.get_script(script["id"])["text"], script["text"])
        self.assertEqual(app.creation.get_script(script["id"])["source"]["candidate_text"], CANDIDATE)

    def test_a_rewritten_chinese_line_is_spoken_again_by_the_writer(self):
        app, script = self.adopted()
        app.ai.bridge = FakeBridge(output='{"lines":[{"number":2,"spoken":"Let the guide rod lead every pass."}]}')
        lines = [{"source": 1, "spoken": "Hold the knife 1.", "zh": "夹好刀具 1。", "translate": False},
                 {"source": 2, "spoken": "Follow the guide 1.", "zh": "让导杆带着每一下都走同一条路。", "translate": True}]
        result = app.edit_script_lines(script["id"], {"request_id": uuid4().hex, "lines": lines})
        prompt = app.ai.bridge.calls[0]["prompt"]
        self.assertIn("2. 【重写】中文：让导杆带着每一下都走同一条路。", prompt)
        self.assertIn("1. Hold the knife 1.", prompt)
        self.assertEqual(result["script"]["text"], "Hold the knife 1.\nLet the guide rod lead every pass.")
        self.assertEqual(result["edit"]["translated"], [2])

    def test_a_failed_retranslation_saves_nothing(self):
        app, script = self.adopted()
        app.ai.bridge = FakeBridge(output="不是 JSON")
        before = len(app.creation.public()["scripts"])
        with self.assertRaises(CreationError) as raised:
            app.edit_script_lines(script["id"], {"request_id": uuid4().hex, "lines": [
                {"source": 1, "spoken": "Hold the knife 1.", "zh": "先把刀夹稳。", "translate": True},
                {"source": 2, "spoken": "Follow the guide 1.", "zh": "沿着导杆 1。", "translate": False}]})
        self.assertEqual(raised.exception.status, 422)
        self.assertEqual(len(app.creation.public()["scripts"]), before)

    def test_retry_returns_the_saved_version_and_no_change_is_refused(self):
        app, script = self.adopted()
        body = {"request_id": uuid4().hex, "lines": [{"source": 1, "spoken": "Hold it 1.", "zh": "夹好刀具 1。", "translate": False},
                                                     {"source": 2, "spoken": "Follow the guide 1.", "zh": "沿着导杆 1。", "translate": False}]}
        first = app.edit_script_lines(script["id"], body)["script"]
        again = app.edit_script_lines(script["id"], body)["script"]
        self.assertEqual(first["id"], again["id"])
        with self.assertRaises(CreationError):
            app.edit_script_lines(script["id"], {"request_id": uuid4().hex, "lines": [
                {"source": 1, "spoken": "Hold the knife 1.", "zh": "夹好刀具 1。", "translate": False},
                {"source": 2, "spoken": "Follow the guide 1.", "zh": "沿着导杆 1。", "translate": False}]})
        with self.assertRaises(CreationError):
            app.edit_script_lines(script["id"], {"request_id": uuid4().hex, "lines": [{"source": 1, "spoken": "x", "zh": "", "translate": False}]})

    def test_a_copy_that_no_longer_matches_its_draft_keeps_the_old_source(self):
        original = [{"spoken": "A.", "zh": "甲。"}]
        record = adopted_edit.prepare(original, [{"source": 1, "spoken": "B.", "zh": "乙。", "translate": False}])
        source = {"candidate_text": CANDIDATE, "variant_index": 0}
        self.assertIsNone(adopted_edit.rebuild_candidate(source, original, record))
        self.assertIsNone(adopted_edit.rebuild_candidate({}, original, record))

    def test_http_route(self):
        app, script = self.adopted()
        status, _, result = self.request("POST", f"/api/scripts/{script['id']}/lines", {"request_id": uuid4().hex, "lines": [
            {"source": 1, "spoken": "Hold it tight 1.", "zh": "夹紧刀具 1。", "translate": False},
            {"source": 2, "spoken": "Follow the guide 1.", "zh": "沿着导杆 1。", "translate": False}]})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["script"]["version"], 2)


if __name__ == "__main__":
    unittest.main()
