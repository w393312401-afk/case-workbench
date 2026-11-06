"""照原片做视频 dialog: the original script is the default and optimized scripts are an optional count; mocked API only."""
from __future__ import annotations

import unittest

import test_ai_selection_ui as browser
import test_server as fixture


JOB = {"id": "PRD-0123456789ab", "product": "202H", "title": "1:1 复刻｜t", "status": "draft"}


class CaseReplicaBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.AISelectionBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.AISelectionBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        self.server.daemon_threads = False

    def open_dialog(self, jobs=(), reply=None):
        context = self.browser.new_context(viewport={"width": 1080, "height": 850})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(7000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        self.posts = []
        preview = {"case_id": self.identifier, "title": "t", "product": "202H", "origin": "self_produced", "timed": True,
                   "machine_transcript": False, "duration": 4.0, "shots": 3, "jobs": list(jobs),
                   "lines": [{"spoken": "One.", "start": 0, "end": 1, "zh": "一"}, {"spoken": "Two.", "start": 1, "end": 2, "zh": "二"}]}

        def replica(route):
            if route.request.method == "POST":
                self.posts.append(route.request.post_data_json)
                route.fulfill(json=reply or {"job": JOB, "task": {"task_id": "AIT-0123456789ab"}})
            else:
                route.fulfill(json=preview)
        page.route(f"**/api/cases/{self.identifier}/replica", replica)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/case/{self.identifier}")
        page.wait_for_function("() => !!state.token")
        page.locator("#replica-top").click()
        page.locator("#replica-dialog[open]").wait_for()
        return page

    def test_original_is_default_and_two_optimized_scripts_are_preselected(self):
        page = self.open_dialog()
        dialog = page.locator("#replica-dialog")
        self.assertIn("默认用原片稿", dialog.inner_text())
        self.assertEqual("2", dialog.locator("[data-optimize]").input_value())
        dialog.locator("[data-optimize]").select_option("3")
        dialog.locator('[data-replica="new"]').click()
        page.wait_for_function("() => !document.querySelector('#replica-dialog[open]')")
        self.assertEqual([{"new": True, "optimize": 3}], self.posts)
        self.assertIn(JOB["id"], page.evaluate("location.hash"))

    def test_continuing_an_open_job_can_still_write_optimized_scripts(self):
        page = self.open_dialog(jobs=[JOB], reply={"job": JOB, "reused": True})
        dialog = page.locator("#replica-dialog")
        dialog.locator("[data-optimize]").select_option("0")
        dialog.locator('[data-replica="continue"]').click()
        page.wait_for_function("() => !document.querySelector('#replica-dialog[open]')")
        self.assertEqual([{"new": False, "optimize": 0}], self.posts)


if __name__ == "__main__":
    unittest.main()
