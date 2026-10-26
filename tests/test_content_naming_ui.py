"""One content identity and its task start time remain readable on both entry pages."""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from playwright.sync_api import expect

from content_management import build_content_items
import test_ui_regressions as browser


class ContentNamingBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    setUp = browser.WorkbenchBrowserTests.setUp
    stop_server = browser.WorkbenchBrowserTests.stop_server
    request = browser.WorkbenchBrowserTests.request
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def test_shared_titles_and_distinct_task_start_times_survive_mobile_and_batch_layout(self):
        common = {"product": "202H", "status": "completed", "management_state": "pending_selection",
                  "context": {"title": "按需求配齐磨具", "direction_id": "202H-S1"},
                  "updated_at": "2026-10-22T08:00:00Z"}
        tasks = [{**common, "task_id": "AIT-first", "created_at": "2026-10-21T06:30:00Z"},
                 {**common, "task_id": "AIT-second", "created_at": "2026-10-22T02:15:00Z"}]
        job = {"id": "PRD-one", "product": "202H", "direction_id": "202H-S1",
               "title": "202H-S1 变体 2", "status": "draft", "updated_at": common["updated_at"],
               "lines": [{"number": 1, "zh": "完整磨刀路线", "spoken": "A complete sharpening path."}],
               "timelines": [{"name": "Zack261022-202-2"}]}
        by_id = {item["id"]: item for item in build_content_items(tasks, [], [job], [])}
        first, second, video = (by_id[key] for key in ("task:AIT-first", "task:AIT-second", "video:PRD-one"))
        self.assertEqual((first["task_started_label"], second["task_started_label"]),
                         ("2026/10/21 14:30 发起", "2026/10/22 10:15 发起"))
        items = [first, second, video]
        response = {"items": items, "total": 3, "page": 1, "pages": 1, "page_size": 30,
                    "counts": {"attention": 3, "running": 0, "publish": 0, "all": 3},
                    "product": "202H", "revision": "naming-fixture", "archived_deletable": 0}
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.route("**/api/content?*", lambda route: route.fulfill(status=200,
            content_type="application/json", body=json.dumps(response, ensure_ascii=False)))
        base = f"http://127.0.0.1:{self.server.server_port}"
        page.goto(base + "/#/202H/home")
        page.locator("[data-home-tab=tasks]").click()
        home_rows = page.locator("#home-tasks-title-panel .home-flow-row")
        expect(home_rows).to_have_count(3)
        self.assertEqual(home_rows.locator("strong").all_text_contents(), [item["title"] for item in items])
        for index, task in enumerate((first, second)):
            expect(home_rows.nth(index).locator("small")).to_contain_text(task["task_started_label"])
            self.assertIn(task["next_hash"][1:], home_rows.nth(index).get_attribute("href"))
        expect(home_rows.nth(2).locator("small")).to_contain_text(video["timeline_name"])
        artifacts = Path(__file__).resolve().parents[1] / ".test-artifacts"
        artifacts.mkdir(exist_ok=True)
        page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
        page.screenshot(path=str(artifacts / "content-naming-home-mobile.png"), full_page=True)

        page.goto(base + "/#/202H/content")
        expect(page.locator(".flow-row")).to_have_count(3)
        self.assertEqual(page.locator(".flow-title").all_text_contents(), [item["title"] for item in items])
        for task in (first, second):
            row = page.locator(f'[data-content-id="{task["id"]}"]')
            expect(row.locator(".flow-task-started")).to_have_text(task["task_started_label"])
            expect(row.locator("time")).to_have_attribute("datetime", task["updated_at"])
            self.assertGreaterEqual(row.locator(".flow-task-started").bounding_box()["y"],
                                    row.locator(".flow-title").bounding_box()["y"] + 10)
        expect(page.locator(".flow-record-meta")).to_contain_text(video["timeline_name"])
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
        page.screenshot(path=str(artifacts / "content-naming-flow-mobile.png"), full_page=True)
        page.locator(".flow-main-menu > summary").click()
        page.locator("#content-flow-batch-open").click()
        expect(page.locator(".flow-select")).to_have_count(3)
        for task in (first, second):
            row = page.locator(f'[data-content-id="{task["id"]}"]')
            self.assertGreater(row.locator(".flow-candidate-main").bounding_box()["x"],
                               row.locator(".flow-select").bounding_box()["x"] + 16)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)


if __name__ == "__main__":
    unittest.main()
