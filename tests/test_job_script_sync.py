"""制作单补全后写回采用稿：同一版补上中文和补全稿，口播不变也不另存新版。只用临时目录和假的 AI。"""
import time
import unittest
from uuid import uuid4

import test_production_http as production
import test_server as fixture
from test_ai_tasks import FakeBridge
from test_production import CANDIDATE
from job_script_sync import job_markdown


class JobScriptSyncTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    ok = production.ProductionHTTPTests.ok
    revision = production.ProductionHTTPTests.revision

    def job_for(self, text, chinese=""):
        script = self.ok("POST", "/api/scripts", {"revision": self.revision("/api/creation?product=202H"), "request_id": uuid4().hex,
            "script": {"product": "202H", "title": "手动", "text": text, "translation_zh": chinese, "confirmed": True}})["script"]
        job = self.ok("POST", "/api/production/jobs", {"revision": self.revision("/api/production"), "request_id": uuid4().hex,
                                                       "job": {"script_id": script["id"]}})["job"]
        return script, job

    def test_filling_by_hand_writes_the_table_back_onto_the_same_version(self):
        script, job = self.job_for("One kit.\nGrab it below.", "一套。\n下面拿。")
        before = len(self.server.app.creation.public()["scripts"])
        lines = [{"number": 1, "proof": "整套打开", "tags": "开箱", "evidence": "展示"},
                 {"number": 2, "proof": "结尾 CTA", "tags": "闭合工具箱", "evidence": "展示"}]
        self.ok("POST", f"/api/production/jobs/{job['id']}/lines", {"revision": self.revision("/api/production"), "request_id": uuid4().hex,
            "lines": lines, "supplement": {"hook_visual": "手打开工具箱", "hook_text": "ONE KIT", "pickups": "补拍合上箱子"}})
        draft = self.ok("GET", f"/api/scripts/{script['id']}/draft")
        self.assertTrue(draft["filled"])
        for text in ("**开头画面：** 手打开工具箱", "**意群表**", "| 1 | One kit. | 整套打开 | 开箱 | 展示 |", "**补拍清单：** 补拍合上箱子"):
            self.assertIn(text, draft["markdown"])
        self.assertLess(draft["markdown"].index("对应中文"), draft["markdown"].index("意群表"))
        detail = self.ok("GET", f"/api/scripts/{script['id']}")["script"]
        self.assertEqual((detail["version"], detail["text"], detail["translation_zh"]), (1, script["text"], script["translation_zh"]))
        self.assertEqual(detail["history"][-1]["event"], "job_fill")
        self.assertEqual(len(self.server.app.creation.public()["scripts"]), before)
        # The content list carries no copy of the long markdown.
        listed = next(row for row in self.ok("GET", "/api/creation?product=202H")["scripts"] if row["id"] == script["id"])
        self.assertNotIn("markdown", listed.get("filled") or {})
        # A later change to the table by hand follows.
        lines[1]["proof"] = "合上箱子，号召手势"
        self.ok("POST", f"/api/production/jobs/{job['id']}/lines", {"revision": self.revision("/api/production"), "request_id": uuid4().hex, "lines": lines})
        self.assertIn("合上箱子，号召手势", self.ok("GET", f"/api/scripts/{script['id']}/draft")["markdown"])

    def test_an_ai_fill_gives_a_copy_without_chinese_its_chinese(self):
        from test_ai_skills import install_review_fixture
        install_review_fixture(self.skill)
        self.server.app.ai.bridge = FakeBridge(output=CANDIDATE)
        script, job = self.job_for("One kit, replacing this jig.\nOut goes the jig.\nFull 202H kit's below.")
        self.ok("POST", f"/api/production/jobs/{job['id']}/fill", {"request_id": uuid4().hex})
        deadline = time.monotonic() + 6
        while self.ok("GET", f"/api/production/jobs/{job['id']}")["job"]["ai_fill"]["status"] == "running":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.05)
        detail = self.ok("GET", f"/api/scripts/{script['id']}")["script"]
        self.assertEqual(detail["translation_zh"].splitlines()[1], "夹具拿走。")
        self.assertEqual(detail["version"], 1)
        draft = self.ok("GET", f"/api/scripts/{script['id']}/draft")["markdown"]
        self.assertIn("读法", draft)
        self.assertIn("旧夹具被 202H 箱替换", draft)

    def test_markdown_puts_the_table_after_the_chinese(self):
        job = {"fill_markdown": "**口播（配音用）**\n\n```\nA.\n```\n\n**对应中文**\n\n```\n甲。\n```\n\n**补拍清单：** 无",
               "lines": [{"number": 1, "spoken": "A.", "proof": "画面 | 一", "tags": "", "evidence": ""}]}
        markdown = job_markdown(job)
        self.assertLess(markdown.index("意群表"), markdown.index("补拍清单"))
        self.assertIn("| 1 | A. | 画面 \\| 一 |  |  |", markdown)
        self.assertEqual(job_markdown({"lines": [{"number": 1, "spoken": "A."}]}), "")


if __name__ == "__main__":
    unittest.main()
