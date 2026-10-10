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
