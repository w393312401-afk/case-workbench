"""Task-result and legacy creation edges, with temporary scripts and mocked writes."""
from __future__ import annotations

import copy
import unittest
from urllib.parse import urlsplit

import test_carousel_table_background_ui as background


class CarouselTableResumeEdgesBrowserTests(unittest.TestCase):
    setUpClass = classmethod(background.CarouselTableBackgroundBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(background.CarouselTableBackgroundBrowserTests.tearDownClass.__func__)
    setUp = background.CarouselTableBackgroundBrowserTests.setUp
    stop_server = background.CarouselTableBackgroundBrowserTests.stop_server
    request = background.CarouselTableBackgroundBrowserTests.request
    catalog = background.CarouselTableBackgroundBrowserTests.catalog
    create_script = background.CarouselTableBackgroundBrowserTests.create_script
    open_page = background.CarouselTableBackgroundBrowserTests.open_page

    def test_task_result_adds_legacy_table_link_and_reopens_the_same_url_without_regeneration(self):
        script = self.create_script()
        table = background.table_record(script, status="completed")
        page, _, endpoints = self.open_page(script=script, tables=[table])
        run_id = "table:" + background.TABLE_ID
        run = {"run_id": run_id, "kind": "table", "owner_id": script["id"],
               "source_id": background.TABLE_ID, "product": "202H", "title": "逐张表 · 临时脚本",
               "status": "completed", "phase": "逐张表已结束", "message": "待核对",
               "started_at": "2026-10-13T08:00:00Z", "updated_at": "2026-10-13T08:01:00Z",
               "finished_at": "2026-10-13T08:01:00Z", "read": True,
               "url": f"#/202H/scripts/{script['id']}"}

        def activity(route):
            path = urlsplit(route.request.url).path
            if path.endswith("/logs"):
                route.fulfill(json={"run": copy.deepcopy(run), "logs": [], "next_seq": 1,
                                    "has_more": False, "has_earlier": False})
            elif route.request.method == "POST":
                route.fulfill(json={"ok": True, "claimed": []})
            else:
                route.fulfill(json={"cursor": 1, "epoch": "isolated-table-result", "items": [copy.deepcopy(run)],
                                    "events": [], "counts": {"active": 0, "unread": 0}, "has_more": False})

        page.route("**/api/activity**", activity)
        page.locator("#task-activity-open").click()
        # Completed table runs are folded out of the default current-task view.
        page.locator('[data-activity-filter="all"]').click()
        result = page.locator(f'[data-activity-result="{run_id}"]')
        result.wait_for()
        for attempt in (1, 2):
            if attempt > 1:
                page.locator("#task-activity-open").click()
            result.click()
            dialog = page.locator(".production-dialog[open]")
            dialog.get_by_role("heading", name="核对图文逐张表", exact=True).wait_for()
            self.assertTrue(page.url.endswith("?table=" + background.TABLE_ID))
            self.assertEqual(endpoints.count("GET", f"{background.TABLE_ROOT}/{background.TABLE_ID}"), attempt)
            self.assertEqual(endpoints.count("POST", background.TABLE_ROOT), 0)
            dialog.locator(".dialog-heading [data-close]").click()
        self.assertEqual(endpoints.count("POST", "/api/production/carousels"), 0)

    def legacy_dialog(self, page, script):
        page.evaluate("script => {void CaseProduction.fromScriptCarousel(script);}", script)
        dialog = page.locator(".production-dialog[open]")
        dialog.locator("[data-early]").wait_for()
        return dialog

    def test_saved_original_opens_after_later_stages_stop_without_creating_a_new_order(self):
        script = self.create_script()
        page, _, endpoints = self.open_page(script=script)
        for status in ("needs_attention", "failed", "cancelled", "interrupted", "completed"):
            with self.subTest(status=status):
                table = background.table_record(script, status="completed", title="Saved original")
                table.update(status=status, has_result=True, slide_count=3, carousel_missing=True)
                endpoints.tables[background.TABLE_ID] = table
                page.evaluate("async ({script,id}) => {await CaseProduction.resumeCarouselTable(script,id);}",
                              {"script": script, "id": background.TABLE_ID})
                dialog = page.locator(".production-dialog[open]")
                dialog.get_by_role("heading", name="查看原逐张表", exact=True).wait_for()
                self.assertIn("关联的图文制作单已不存在", dialog.inner_text())
                self.assertEqual(dialog.locator("tbody tr").count(), 3)
                self.assertIn("Saved original 1", dialog.inner_text())
                self.assertFalse(dialog.locator("[data-submit]").is_visible())
                dialog.locator(".dialog-footer [data-close]").click()
        self.assertEqual(endpoints.count("POST", background.TABLE_ROOT), 0)
        self.assertEqual(endpoints.count("POST", "/api/production/carousels"), 0)

    def test_existing_table_picker_includes_saved_original_and_excludes_empty_failures(self):
        script = self.create_script()
        saved = background.table_record(script, status="completed")
        saved.update(status="needs_attention", has_result=True, slide_count=3, carousel_missing=True)
        failed = background.table_record(script, table_id=background.OTHER_TABLE_ID, status="failed")
        failed.update(has_result=False, slide_count=0)
        page, _, endpoints = self.open_page(script=script, tables=[saved, failed])
        page.evaluate("script => {void CaseProduction.fromScriptCarousel(script);}", script)
        dialog = page.locator(".production-dialog[open]")
        dialog.get_by_role("heading", name="继续图文逐张表", exact=True).wait_for()
        self.assertEqual(dialog.locator("[data-resume-table]").count(), 1)
        self.assertIn("3 张", dialog.inner_text())
        dialog.get_by_role("button", name="查看原逐张表", exact=True).click()
        page.get_by_role("heading", name="查看原逐张表", exact=True).wait_for()
        self.assertEqual(endpoints.count("POST", background.TABLE_ROOT), 0)
        self.assertEqual(endpoints.count("POST", "/api/production/carousels"), 0)

    def test_resume_refuses_a_different_table_or_script_without_creating_anything(self):
        script = self.create_script()
        page, _, endpoints = self.open_page(script=script)
        for changed, message in (({"id": background.OTHER_TABLE_ID}, "任务标识不一致"),
                                 ({"script_id": "SCR-000000000099"}, "属于另一份脚本")):
            with self.subTest(changed=changed):
                endpoints.tables[background.TABLE_ID] = {**background.table_record(script, status="completed"), **changed}
                page.evaluate("async ({script,id}) => {await CaseProduction.resumeCarouselTable(script,id);}",
                              {"script": script, "id": background.TABLE_ID})
                self.assertEqual(page.locator(".production-dialog[open]").count(), 0)
                self.assertIn(message, page.locator("#toast").inner_text())
        self.assertEqual(endpoints.count("POST", background.TABLE_ROOT), 0)
        self.assertEqual(endpoints.count("POST", "/api/production/carousels"), 0)

    def test_legacy_create_ignores_duplicate_clicks_and_a_late_reply_cannot_replace_a_new_dialog(self):
        script = self.create_script("01 Legacy overlay\nLegacy body.")
        page, _, endpoints = self.open_page(script=script)
        pending, posts = [], []

        def create(route):
            posts.append(route.request.post_data_json)
            pending.append(route)

        page.route("**/api/production/carousels", create)
        dialog = self.legacy_dialog(page, script)
        button = dialog.locator("[data-early]")
        with page.expect_request(lambda request: request.method == "POST" and request.url.endswith("/api/production/carousels")):
            button.click()
        button.evaluate("node => {node.click();node.dispatchEvent(new MouseEvent('click',{bubbles:true}));}")
        self.assertEqual(len(posts), 1)
        self.assertTrue(button.is_disabled())
        page.evaluate("script => {void CaseProduction.fromScript(script);}", script)
        replacement = page.locator(".production-dialog[open]")
        replacement.get_by_role("heading", name="为这条脚本创建制作单", exact=True).wait_for()
        with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/production/carousels")):
            pending.pop().fulfill(json={"carousel": {"id": background.CAROUSEL_ID, "product": "202H"}})
        page.clock.run_for(100)
        self.assertEqual(replacement.get_by_role("heading").inner_text(), "为这条脚本创建制作单")
        self.assertNotIn("/production/", page.url)
        self.assertEqual(len(posts), 1)
        self.assertEqual(endpoints.count("POST", background.TABLE_ROOT), 0)

    def test_legacy_create_unknown_response_retries_the_same_request(self):
        script = self.create_script("01 Legacy overlay\nLegacy body.")
        page, _, _ = self.open_page(script=script)
        posts = []

        def create(route):
            posts.append(route.request.post_data_json)
            route.fulfill(status=503, json={"error": "fixture unknown legacy creation result"})

        page.route("**/api/production/carousels", create)
        dialog = self.legacy_dialog(page, script)
        for _ in range(2):
            dialog.locator("[data-early]").click()
            dialog.locator("[data-error]").get_by_text("fixture unknown legacy creation result", exact=True).wait_for()
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0], posts[1])
        self.assertEqual(posts[0]["carousel"], {"script_id": script["id"]})


if __name__ == "__main__":
    unittest.main()
