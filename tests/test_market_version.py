"""「做英国版」 over HTTP, with isolated data and a fake AI bridge."""
import time
import unittest
from uuid import uuid4

import test_server as fixture
from test_ai_tasks import FakeBridge
from test_production import CANDIDATE

BRITISH = CANDIDATE.replace("One kit", "One set")


class MarketVersionHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def ok(self, method, path, body=None):
        status, _, data = self.request(method, path, body)
        self.assertEqual(status, 200, data)
        return data

    def revision(self, path):
        return self.ok("GET", path)["revision"]

    def american_job(self):
        self.server.app.ai.bridge = FakeBridge(output=CANDIDATE)
        task = self.ok("POST", "/api/ai/tasks", {"request_id": uuid4().hex, "product": "202H", "action": "variants",
                                                 "instruction": "写一条", "skill_selection": "none"})["task"]
        task = self.finished(task["task_id"])
        round_id = task["rounds"][0]["round_id"]
        chosen = self.ok("GET", f"/api/production/variants?task_id={task['task_id']}&round_id={round_id}")["variants"][0]
        script = self.ok("POST", "/api/scripts", {"revision": self.revision("/api/creation?product=202H"), "request_id": uuid4().hex,
            "script": {"product": "202H", "title": "原稿", "text": "\n".join(line["spoken"] for line in chosen["lines"]),
                       "translation_zh": "\n".join(line["zh"] for line in chosen["lines"]), "confirmed": True},
            "source": {"task_id": task["task_id"], "round_id": round_id, "task_revision": task["revision"], "variant_index": 0}})["script"]
        return self.ok("POST", "/api/production/jobs", {"revision": self.revision("/api/production"), "request_id": uuid4().hex,
                                                        "job": {"script_id": script["id"], "variant_index": 0}})["job"]

    def finished(self, task_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = self.server.app.ai.get(task_id)
            if not task["busy"]:
                return task
            time.sleep(.02)
        self.fail("写稿没有结束")

    def start(self, job, output):
        from test_ai_skills import install_review_fixture
        install_review_fixture(self.skill)
        self.server.app.ai.bridge = FakeBridge(output=output)
        started = self.ok("POST", f"/api/production/jobs/{job['id']}/uk-version", {"request_id": uuid4().hex})["job"]
        self.finished(started["uk_version"]["task_id"])
        return self.ok("GET", f"/api/production/jobs/{job['id']}")["job"]

    def test_checked_version_becomes_a_new_job_and_the_original_stays(self):
        job = self.american_job()
        before = [line["spoken"] for line in job["lines"]]
        waiting = self.start(job, BRITISH)
        version = waiting["uk_version"]
        self.assertEqual(version["status"], "review")
        self.assertEqual([line["before"] for line in version["review"]["lines"]], before)
        self.assertTrue(any(line["before"] != line["spoken"] for line in version["review"]["lines"]))
        task = self.server.app.ai._read(version["task_id"])
        self.assertEqual(task["snapshot"]["creation_context"]["market"], "uk")
        self.assertIn(f"必须正好 {len(before)} 行", task["rounds"][0]["instruction"])
        self.assertIn("本轮目标市场：英国", self.server.app.ai.bridge.calls[0]["prompt"])

        body = {"request_id": uuid4().hex}
        created = self.ok("POST", f"/api/production/jobs/{job['id']}/uk-version-confirm", body)["job"]
        self.assertEqual(created["market"], "uk")
        self.assertEqual([line["spoken"] for line in created["lines"]], [line["spoken"] for line in version["review"]["lines"]])
        self.assertEqual([line["tags"] for line in created["lines"]], [line["tags"] for line in job["lines"]])
        self.assertEqual(created["derived_from"]["job_id"], job["id"])
        self.assertIn("英国版", created["title"])
        repeat = self.ok("POST", f"/api/production/jobs/{job['id']}/uk-version-confirm", {"request_id": uuid4().hex})["job"]
        self.assertEqual(repeat["id"], created["id"])

        original = self.ok("GET", f"/api/production/jobs/{job['id']}")["job"]
        self.assertEqual([line["spoken"] for line in original["lines"]], before)
        self.assertEqual((original["uk_version"]["status"], original["uk_version"]["job_id"]), ("created", created["id"]))
        self.assertEqual(self.request("POST", f"/api/production/jobs/{job['id']}/uk-version", {"request_id": uuid4().hex})[0], 409)
        self.assertEqual(self.request("POST", f"/api/production/jobs/{created['id']}/uk-version", {"request_id": uuid4().hex})[0], 409)

    def test_a_round_with_a_different_line_count_makes_nothing(self):
        job = self.american_job()
        short = "**口播（配音用）**\n\n```\nJust one line.\n```\n\n**对应中文**\n\n```\n一句。\n```\n"
        waiting = self.start(job, short)
        self.assertEqual(waiting["uk_version"]["status"], "failed")
        self.assertIn(f"正好 {len(job['lines'])} 行", waiting["uk_version"]["error"])
        self.assertEqual(self.request("POST", f"/api/production/jobs/{job['id']}/uk-version-confirm", {"request_id": uuid4().hex})[0], 409)
        self.assertEqual(self.start(job, BRITISH)["uk_version"]["status"], "review")

    def test_chinese_in_the_spoken_lines_is_refused(self):
        job = self.american_job()
        spoken = job["lines"][0]["spoken"]
        self.assertEqual(self.start(job, CANDIDATE.replace(spoken, "这是中文"))["uk_version"]["status"], "failed")

    def test_a_job_written_for_another_market_cannot_be_converted(self):
        job = self.american_job()
        self.start(job, BRITISH)
        created = self.ok("POST", f"/api/production/jobs/{job['id']}/uk-version-confirm", {"request_id": uuid4().hex})["job"]
        status, _, data = self.request("POST", f"/api/production/jobs/{created['id']}/uk-version", {"request_id": uuid4().hex})
        self.assertEqual(status, 409, data)
        self.assertIn("只有美国稿", data["error"])


if __name__ == "__main__":
    unittest.main()
