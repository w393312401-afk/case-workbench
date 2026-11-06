"""全部 folds 已完结 orders under one line; a waiting video can be marked 不发了 with a reason, and undone."""
from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect

from content_management import build_content_items
import test_ui_regressions as browser


class ContentFinishedBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    setUp = browser.WorkbenchBrowserTests.setUp
    stop_server = browser.WorkbenchBrowserTests.stop_server
    request = browser.WorkbenchBrowserTests.request
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def test_finished_rows_fold_and_skip_dialog_sends_the_reason(self):
        base_job = {"product": "202H", "direction_id": "202H-S2", "updated_at": "2026-11-01T08:00:00Z",
                    "lines": [{"number": 1, "zh": "开头", "spoken": "Open it."}]}
        waiting = {**base_job, "id": "PRD-wait", "title": "202H-S2 变体 1", "status": "approved"}
        skipped = {**base_job, "id": "PRD-skip", "title": "202H-S2 变体 2", "status": "approved"}
        marks = {"items": {"video:PRD-skip": {"skip_publish": True, "skip_reason": "开头不行", "updated_at": "x"}}}
        rows = {item["id"]: item for item in build_content_items([], [], [waiting, skipped], marks)}
        open_row, done_row = rows["video:PRD-wait"], rows["video:PRD-skip"]
        self.assertEqual(done_row["stage"], "skipped")

        def respond(route):
            query = parse_qs(urlparse(route.request.url).query)
            finished = query.get("status") == ["finished"]
            items = [done_row] if finished else [open_row]
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "items": items, "total": 1, "page": 1, "pages": 1, "page_size": 30,
                "counts": {"attention": 0, "running": 0, "publish": 1, "all": 1},
                "finished_hidden": 0 if finished else 1, "product": "202H", "revision": "finished-fixture",
                "archived_deletable": 0}, ensure_ascii=False))

        sent = []

        def manage(route):
            sent.append(json.loads(route.request.post_data))
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"id": sent[-1]["id"], "management": {}, "revision": "next"}))

        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.route("**/api/content?*", respond)
        page.route("**/api/content/manage", manage)
        base = f"http://127.0.0.1:{self.server.server_port}"
        page.goto(base + "/#/202H/content?view=all")
        fold = page.locator("[data-flow-finished]")
        expect(fold).to_contain_text("已完结 1 张")

        row = page.locator('[data-content-id="video:PRD-wait"]')
        row.locator(".flow-menu > summary").click()
        row.locator("[data-skip]").click()
        dialog = page.locator(".flow-skip-dialog")
        dialog.locator('[name="reason"]').fill("同方向已发过")
        dialog.locator('[type="submit"]').click()
        expect(dialog).to_have_count(0)
        self.assertEqual((sent[0]["id"], sent[0]["management"]),
                         ("video:PRD-wait", {"skip_publish": True, "skip_reason": "同方向已发过"}))

        fold.click()
        skipped_row = page.locator('[data-content-id="video:PRD-skip"]')
        expect(skipped_row.locator(".flow-stage")).to_have_text("不发了")
        self.assertIn("开头不行", skipped_row.locator(".flow-title").get_attribute("title"))
        expect(page.locator("[data-flow-finished]")).to_have_count(0)
        skipped_row.locator(".flow-menu > summary").click()
        skipped_row.locator("[data-unskip]").click()
        for _ in range(50):
            if len(sent) > 1:
                break
            page.wait_for_timeout(100)
        self.assertEqual(sent[-1]["management"], {"skip_publish": False, "skip_reason": ""})


if __name__ == "__main__":
    unittest.main()
