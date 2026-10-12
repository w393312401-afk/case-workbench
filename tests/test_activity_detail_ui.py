"""Deep links select the requested saved round or production run in isolated pages."""
import unittest
from uuid import uuid4

import test_ai_selection_ui as ai_fixture
import test_production_validation_ui as production_fixture


class AIActivityDetailBrowserTests(unittest.TestCase):
    setUpClass = classmethod(ai_fixture.AISelectionBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(ai_fixture.AISelectionBrowserTests.tearDownClass.__func__)
    setUp = ai_fixture.AISelectionBrowserTests.setUp
    stop_server = ai_fixture.AISelectionBrowserTests.stop_server
    request = ai_fixture.AISelectionBrowserTests.request
    detail = ai_fixture.AISelectionBrowserTests.detail
    create_task = ai_fixture.AISelectionBrowserTests.create_task
    open_ai_list = ai_fixture.AISelectionBrowserTests.open_ai_list

    def test_round_query_preserved_across_product_alignment_and_polling(self):
        task = self.create_task("第一轮测试")
        first = task["rounds"][0]["round_id"]
        self.server.app.ai.continue_task(task["task_id"], {"request_id": uuid4().hex,
            "task_revision": task["revision"], "instruction": "第二轮测试", "skill_selection": "none"})
        self.server.app.ai._queue.join()
        task = self.server.app.ai.get(task["task_id"])
        second = task["rounds"][1]["round_id"]
        page = self.open_ai_list()
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/129N/ai/{task['task_id']}?round={first}")
        chosen = page.locator(f'[data-round-id="{first}"]')
        chosen.wait_for()
        page.wait_for_function("() => location.hash.includes('/202H/ai/')")
        self.assertTrue(page.url.endswith("?round=" + first))
        self.assertNotIn("is-collapsed", chosen.get_attribute("class"))
        self.assertEqual(chosen.locator('.task-activity-logs').count(), 1)
        chosen.locator('.activity-inline-logs > summary').click()
        chosen.get_by_text("本轮结果已保存，可以查看。", exact=True).wait_for()
        page.wait_for_timeout(2300)
        self.assertFalse(any("%3Fround" in request or "%3fround" in request for request in requests))
        page.evaluate("hash => location.hash = hash", f"#/202H/ai/{task['task_id']}?round={second}")
        page.wait_for_function("round => document.activeElement?.dataset.roundId === round", arg=second)
        self.assertNotIn("is-collapsed", page.locator(f'[data-round-id="{second}"]').get_attribute("class"))


class ProductionActivityDetailBrowserTests(unittest.TestCase):
    setUpClass = classmethod(production_fixture.ProductionValidationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(production_fixture.ProductionValidationBrowserTests.tearDownClass.__func__)
    setUp = production_fixture.ProductionValidationBrowserTests.setUp
    stop_server = production_fixture.ProductionValidationBrowserTests.stop_server
    request = production_fixture.ProductionValidationBrowserTests.request
    make_playable_video = production_fixture.ProductionValidationBrowserTests.make_playable_video
    take = production_fixture.ProductionValidationBrowserTests.take
    edit = production_fixture.ProductionValidationBrowserTests.edit
    open_page = production_fixture.ProductionValidationBrowserTests.open_page

    def test_voice_and_validation_links_select_the_saved_run(self):
        take_id = "TAK-0123456789ab"
        self.payload["job"]["takes"] = [self.take(take_id, [{"number": 1, "start": 0, "end": 1.5, "coverage": 1}])]
        self.payload["job"]["edits"] = [self.edit("awaiting_review")]
        page = self.open_page()
        page.evaluate("hash => location.hash = hash", f"#/202H/production/{production_fixture.JOB_ID}?step=voice&run={take_id}")
        page.wait_for_function("() => document.querySelector('.production-steps [aria-selected=true]')?.dataset.step === 'voice'")
        page.wait_for_function("id => document.activeElement?.dataset.activitySource === id", arg=take_id)
        take = page.locator(f'[data-activity-source="{take_id}"]')
        take.locator('.activity-inline-logs > summary').click()
        take.get_by_text("此前未记录运行日志", exact=True).wait_for()
        page.evaluate("hash => location.hash = hash", f"#/202H/production/{production_fixture.JOB_ID}?step=validation&run=EDT-0123456789ab")
        page.wait_for_function("() => document.querySelector('.production-steps [aria-selected=true]')?.dataset.step === 'edit'")
        page.wait_for_function("() => document.activeElement?.dataset.activitySource === 'EDT-0123456789ab'")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
