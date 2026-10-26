"""Production routes over HTTP with isolated data, a fake AI bridge and a fake ElevenLabs transport."""
import time
import unittest
from uuid import uuid4

import test_server as fixture
from test_ai_tasks import FakeBridge
from test_production import CANDIDATE, FakeEleven
from elevenlabs_client import ElevenLabsClient
from resolve_bridge import ResolveBridge
from test_production_resolve import FakeRunner


class ProductionHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def ok(self, method, path, body=None):
        status, _, data = self.request(method, path, body)
        self.assertEqual(status, 200, data)
        return data

    def revision(self, path):
        return self.ok("GET", path)["revision"]

    def completed_task(self):
        self.server.app.ai.bridge = FakeBridge(output=CANDIDATE)
        task = self.ok("POST", "/api/ai/tasks", {"request_id": uuid4().hex, "product": "202H", "action": "review",
                                                 "instruction": "核对资料", "skill_selection": "none"})["task"]
        deadline = time.monotonic() + 4
        while task["busy"] and time.monotonic() < deadline:
            time.sleep(.02)
            task = self.server.app.ai.get(task["task_id"])
        self.assertEqual(task["status"], "completed")
        return task

    def test_script_without_a_table_is_filled_in_by_hand(self):
        script = self.ok("POST", "/api/scripts", {"revision": self.revision("/api/creation?product=202H"), "request_id": uuid4().hex,
            "script": {"product": "202H", "title": "手动", "text": "One kit.\nGrab it below.", "translation_zh": "一套。\n下面拿。",
                       "confirmed": True}})["script"]
        job = self.ok("POST", "/api/production/jobs", {"revision": self.revision("/api/production"), "request_id": uuid4().hex,
                                                       "job": {"script_id": script["id"]}})["job"]
        self.assertTrue(any("在制作单里补填" in item for item in job["problems"]))
        detail = self.ok("GET", f"/api/production/jobs/{job['id']}")
        self.assertIsNone(detail["variant"])
        lines = [{"number": 1, "proof": "整套打开", "tags": "开箱", "evidence": "展示"},
                 {"number": 2, "proof": "结尾 CTA，全片唯一一次号召手势", "tags": "闭合工具箱", "evidence": "展示"}]
        supplement = {"hook_visual": "手打开工具箱", "hook_text": "ONE KIT / ALL STAGES", "hook_text_zh": "一套 / 全阶段",
                      "pickups": "补拍合上箱子", "captions": ["🔴One Kit#SHARPAL202H #knifesharpening", "", "🔴Two#SHARPAL202H"]}
        status, _, refused = self.request("POST", f"/api/production/jobs/{job['id']}/lines", {"revision": detail["revision"],
            "request_id": uuid4().hex, "lines": lines, "supplement": {**supplement, "spoken": "x"}})
        self.assertEqual(status, 400, refused)
        saved = self.ok("POST", f"/api/production/jobs/{job['id']}/lines", {"revision": self.revision("/api/production"),
            "request_id": uuid4().hex, "lines": lines, "supplement": supplement})["job"]
        self.assertEqual([line["spoken"] for line in saved["lines"]], ["One kit.", "Grab it below."])
        self.assertEqual(saved["supplement"]["captions"], ["🔴One Kit#SHARPAL202H #knifesharpening", "🔴Two#SHARPAL202H"])
        self.assertFalse(any("在制作单里补填" in item for item in saved["problems"]))
        markdown = self.ok("GET", f"/api/production/jobs/{job['id']}")["variant"]["markdown"]
        for text in ("**开头画面：** 手打开工具箱", "**开头屏显：** `ONE KIT / ALL STAGES`（一套 / 全阶段）", "One kit.\nGrab it below.",
                     "**补拍清单：** 补拍合上箱子", "```text\n🔴Two#SHARPAL202H\n```"):
            self.assertIn(text, markdown)
        self.assertLess(markdown.index("开头画面"), markdown.index("口播（配音用）"))
        titles = self.ok("GET", f"/api/production/jobs/{job['id']}/publication")["copy"]["titles"]
        self.assertEqual(titles, ["🔴One Kit#SHARPAL202H #knifesharpening", "🔴Two#SHARPAL202H"])

    def test_ai_fills_in_a_job_without_a_table_using_the_writing_skill(self):
        from test_ai_skills import install_review_fixture
        install_review_fixture(self.skill)
        self.server.app.ai.bridge = FakeBridge(output=CANDIDATE)

        def bare_job(text):
            script = self.ok("POST", "/api/scripts", {"revision": self.revision("/api/creation?product=202H"), "request_id": uuid4().hex,
                "script": {"product": "202H", "title": "无表", "text": text, "confirmed": True}})["script"]
            return self.ok("POST", "/api/production/jobs", {"revision": self.revision("/api/production"), "request_id": uuid4().hex,
                                                           "job": {"script_id": script["id"]}})["job"]

        def settled(job_id):
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                detail = self.ok("GET", f"/api/production/jobs/{job_id}")
                if detail["job"]["ai_fill"]["status"] != "running":
                    return detail
                time.sleep(.05)
            self.fail("补全没有结束")

        job = bare_job("One kit, replacing this jig.\nOut goes the jig.\nFull 202H kit's below.")
        started = self.ok("POST", f"/api/production/jobs/{job['id']}/fill", {"request_id": uuid4().hex})
        task = self.server.app.ai._read(started["fill"]["task_id"])
        instruction = task["rounds"][0]["instruction"]
        for text in ("写稿技能的口播稿交付格式", "逐字照抄下面 3 句", "2. Out goes the jig."):
            self.assertIn(text, instruction)
        detail = settled(job["id"])
        filled = detail["job"]
        self.assertEqual(filled["ai_fill"]["status"], "completed", filled["ai_fill"])
        self.assertEqual([line["spoken"] for line in filled["lines"]], ["One kit, replacing this jig.", "Out goes the jig.", "Full 202H kit's below."])
        self.assertEqual([line["proof"] for line in filled["lines"]][:2], ["旧夹具被 202H 箱替换", "旧夹具移走"])
        self.assertEqual(filled["lines"][1]["zh"], "夹具拿走。")
        self.assertFalse(any("在制作单里补填" in item for item in filled["problems"]))
        self.assertFalse(detail["variant"]["manual"])
        self.assertIn("读法", detail["variant"]["markdown"])
        self.assertIn("🔴One Kit#SHARPAL202H", detail["variant"]["markdown"])
        copy = self.ok("GET", f"/api/production/jobs/{job['id']}/publication")["copy"]
        self.assertEqual(copy["titles"], ["🔴One Kit#SHARPAL202H #knifesharpening"])
        self.assertEqual(copy["zh"].splitlines()[1], "夹具拿走。")

        # A round whose script has a different number of lines changes nothing and says why.
        long = bare_job("One.\nTwo.\nThree.\nFour.")
        self.ok("POST", f"/api/production/jobs/{long['id']}/fill", {"request_id": uuid4().hex})
        kept = settled(long["id"])["job"]
        self.assertEqual(kept["ai_fill"]["status"], "failed")
        self.assertIn("正好 4 句", kept["ai_fill"]["error"])
        self.assertEqual([line["proof"] for line in kept["lines"]], ["", "", "", ""])

        # Reworded lines are applied by row, the spoken lines stay, and the job says which rows to check.
        reworded = bare_job("One kit.\nGrab it below.")
        self.ok("POST", f"/api/production/jobs/{reworded['id']}/fill", {"request_id": uuid4().hex})
        kept = settled(reworded["id"])["job"]
        self.assertEqual([line["spoken"] for line in kept["lines"]], ["One kit.", "Grab it below."])
        self.assertTrue(any("AI 补全时改写了第" in item for item in kept["problems"]), kept["problems"])

    def test_variant_to_job_to_timed_take_and_audio(self):
        self.server.app.voices = ElevenLabsClient(key_provider=lambda: "sk_test", transport=FakeEleven())
        task = self.completed_task()
        round_id = task["rounds"][0]["round_id"]
        variants = self.ok("GET", f"/api/production/variants?task_id={task['task_id']}&round_id={round_id}")["variants"]
        self.assertEqual([len(item["lines"]) for item in variants], [3, 2])
        chosen = variants[0]
        script = self.ok("POST", "/api/scripts", {"revision": self.revision("/api/creation?product=202H"), "request_id": uuid4().hex,
            "script": {"product": "202H", "title": chosen["label"], "text": "\n".join(line["spoken"] for line in chosen["lines"]),
                       "translation_zh": "\n".join(line["zh"] for line in chosen["lines"]), "confirmed": True},
            "source": {"task_id": task["task_id"], "round_id": round_id, "task_revision": task["revision"]}})["script"]
        job = self.ok("POST", "/api/production/jobs", {"revision": self.revision("/api/production"), "request_id": uuid4().hex,
                                                       "job": {"script_id": script["id"], "variant_index": 0}})["job"]
        self.assertEqual(job["lines"][2]["tags"], "闭合工具箱")
        saved = self.ok("POST", "/api/production/voices", {"revision": self.revision("/api/production"), "request_id": uuid4().hex,
                                                           "operation": "save", "voice": {"voice_id": "VOICE123456789", "note": "Zack 男声"}})
        self.assertEqual(saved["voice"]["name"], "Zack")
        take = self.ok("POST", f"/api/production/jobs/{job['id']}/takes",
                       {"request_id": uuid4().hex, "voice_id": "VOICE123456789", "model_id": "eleven_v4", "settings": {"stability": 0.5}})["take"]
        status, headers, audio = self.request("GET", f"/api/production/audio/{job['id']}/{take['id']}/mp3")
        self.assertEqual((status, audio), (200, b"ID3fake-audio"))
        self.assertEqual(self.request("GET", f"/api/production/audio/{job['id']}/TAK-000000000000/mp3")[0], 404)
        detail = self.ok("GET", f"/api/production/jobs/{job['id']}")
        selected = self.ok("POST", f"/api/production/jobs/{job['id']}/select",
                           {"revision": detail["revision"], "request_id": uuid4().hex, "take_id": take["id"]})["job"]
        self.assertEqual(selected["status"], "voiced")
        self.assertTrue(self.ok("GET", "/api/production/eleven")["ok"])
        listing = self.ok("GET", "/api/production?product=202H")
        self.assertEqual((len(listing["jobs"]), listing["voices"][0]["use_count"]), (1, 1))
        self.assertEqual(listing["product_settings"]["project"], "202H")
        runner = FakeRunner()
        self.server.app.resolve = ResolveBridge(runner=runner)
        self.ok("POST", "/api/production/settings", {"revision": listing["revision"], "request_id": uuid4().hex,
                                                     "settings": {"creators": ["Zack", "Lab"]}})
        built = self.ok("POST", f"/api/production/jobs/{job['id']}/timeline", {"request_id": uuid4().hex, "creator": "Zack"})
        self.assertRegex(built["timeline"]["name"], r"^Zack\d{6}-202-1$")
        detail = self.ok("GET", f"/api/production/jobs/{job['id']}")
        self.assertEqual((detail["job"]["status"], detail["creators"]), ("timeline", ["Zack", "Lab"]))
        self.assertTrue(self.ok("GET", "/api/production/resolve")["ok"])
        self.assertEqual(self.ok("POST", f"/api/production/jobs/{job['id']}/open", {"unique_id": "uid-1"})["name"], "Lab261010-202-1")

    def test_writes_need_the_page_token_and_old_candidates_explain_why(self):
        self.assertEqual(self.request("POST", "/api/production/readings", {"request_id": uuid4().hex}, authenticated=False)[0], 403)
        self.server.app.ai.bridge = FakeBridge(output="**英文口播稿**\n> Old format line.")
        task = self.ok("POST", "/api/ai/tasks", {"request_id": uuid4().hex, "product": "202H", "action": "review",
                                                 "instruction": "核对资料", "skill_selection": "none"})["task"]
        deadline = time.monotonic() + 4
        while task["busy"] and time.monotonic() < deadline:
            time.sleep(.02)
            task = self.server.app.ai.get(task["task_id"])
        status, _, data = self.request("GET", f"/api/production/variants?task_id={task['task_id']}&round_id={task['rounds'][0]['round_id']}")
        self.assertEqual(status, 409, data)
        self.assertIn("口播（配音用）", data["error"])
        self.assertEqual(self.request("GET", "/api/production/variants?task_id=bad&round_id=bad")[0], 400)


if __name__ == "__main__":
    unittest.main()
