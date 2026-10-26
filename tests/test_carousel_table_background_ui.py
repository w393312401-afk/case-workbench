"""Background carousel-table dialogs; temporary scripts and mocked task endpoints only."""
from __future__ import annotations

import copy
import time
import unittest
from urllib.parse import parse_qs, urlsplit

import test_ai_selection_ui as browser
import test_phase4_writing_ui as writing
import test_server as fixture


TABLE_ID = "TBL-0123456789ab"
OTHER_TABLE_ID = "TBL-abcdef012345"
CAROUSEL_ID = "CAR-0123456789ab"
TABLE_ROOT = "/api/production/carousel-tables"


def table_record(script, *, table_id=TABLE_ID, status="running", title="Hook"):
    item = {
        "slides": [{"number": number, "role": "钩子" if number == 1 else "证明",
                    "title_lines": [f"{title} {number}"], "body": f"Body {number}.",
                    "title_zh": f"标题{number}", "body_zh": f"正文{number}。",
                    "source": "临时实拍", "forbidden": "价格", "base": None}
                   for number in (1, 2, 3)],
        "caption_en": "A fixture carousel. #test", "caption_zh": "临时图文。",
        "problems": [],
    }
    return {"id": table_id, "script_id": script["id"], "product": script["product"],
            "status": status, "progress": "表生成中", "created_at": "2026-10-13T08:00:00Z",
            "error": "", "note": "", "carousel_id": "",
            "carousels": [item] if status == "completed" else []}


class TableEndpoints:
    def __init__(self, page, script):
        self.script = script
        self.tables = {}
        self.calls = []
        self.defer_post = False
        self.defer_details = False
        self.pending_posts = []
        self.pending_details = []
        page.route("**/api/production/carousel-tables**", self.handle_table)
        page.route("**/api/production/carousels**", self.handle_carousel)

    def record(self, request):
        parsed = urlsplit(request.url)
        call = {"method": request.method, "path": parsed.path, "query": parse_qs(parsed.query)}
        if request.method == "POST":
            call["body"] = request.post_data_json
        self.calls.append(call)
        return call

    def handle_table(self, route):
        call = self.record(route.request)
        if call["path"] == TABLE_ROOT:
            if call["method"] == "GET":
                identifier = call["query"].get("script_id", [""])[0]
                route.fulfill(json={"tables": [copy.deepcopy(table) for table in self.tables.values()
                                               if table["script_id"] == identifier]})
            elif call["method"] == "POST":
                if self.defer_post:
                    self.pending_posts.append(route)
                else:
                    self.confirm_post(route)
            else:
                route.fulfill(status=405, json={"error": "Unexpected fixture method"})
        elif call["method"] == "GET" and call["path"].rsplit("/", 1)[-1] in self.tables:
            if self.defer_details:
                self.pending_details.append(route)
            else:
                route.fulfill(json={"table": copy.deepcopy(self.tables[call["path"].rsplit("/", 1)[-1]])})
        else:
            route.fulfill(status=404, json={"error": "Unexpected fixture table request"})

    def confirm_post(self, route):
        self.tables[TABLE_ID] = table_record(self.script)
        route.fulfill(json={"table": copy.deepcopy(self.tables[TABLE_ID])})

    def handle_carousel(self, route):
        call = self.record(route.request)
        if call["method"] == "POST" and call["path"] == "/api/production/carousels":
            table_id = call["body"]["carousel"]["table_id"]
            self.tables[table_id]["carousel_id"] = CAROUSEL_ID
            route.fulfill(json={"carousel": {"id": CAROUSEL_ID, "product": self.script["product"],
                                              "script_id": self.script["id"]}})
        else:
            # Navigation is the subject of these tests; no actual carousel is written.
            route.fulfill(status=404, json={"error": "Fixture carousel destination"})

    def count(self, method, path):
        return sum(call["method"] == method and call["path"] == path for call in self.calls)


class CarouselTableBackgroundBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.AISelectionBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.AISelectionBrowserTests.tearDownClass.__func__)
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    catalog = writing.WritingLifecycleBrowserTests.catalog
    create_script = writing.WritingLifecycleBrowserTests.create_script

    def open_page(self, *, script=None, tables=(), tail=""):
        script = script or self.create_script("Seven plates. Overkill?\nThe clamp holds the angle.")
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.clock.install()
        page.set_default_timeout(7000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        endpoints = TableEndpoints(page, script)
        endpoints.tables = {table["id"]: copy.deepcopy(table) for table in tables}
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/scripts/{script['id']}{tail}")
        page.locator("#script-production-toggle").wait_for()
        page.wait_for_function("() => !!window.CaseProduction && !!window.CaseScripts")
        return page, script, endpoints

    def start_generation(self, page, script):
        page.evaluate("script => { void CaseProduction.fromScriptCarousel(script); }", script)
        dialog = page.locator(".production-dialog[open]")
        dialog.get_by_role("heading", name="用图文技能生成逐张表", exact=True).wait_for()
        dialog.get_by_role("button", name="生成逐张表", exact=True).click()
        return dialog

    def close_dialog(self, page, dialog, method, *, locked=False):
        if method == "escape":
            page.keyboard.press("Escape")
        else:
            selector = ".dialog-heading [data-close]" if method == "cross" else ".dialog-footer [data-close]"
            button = dialog.locator(selector)
            if locked:
                button.evaluate("node => node.click()")
            else:
                button.click()

    def assert_no_cancellation(self, endpoints):
        self.assertFalse(any("cancel" in call["path"] for call in endpoints.calls), endpoints.calls)

    def wait_for_fixture(self, page, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            page.wait_for_timeout(10)
        self.assertTrue(predicate(), "Pending fixture route was not delivered")

    def test_unconfirmed_generation_remains_locked_until_running_task_is_saved(self):
        for method in ("cross", "escape", "footer"):
            with self.subTest(close=method):
                page, script, endpoints = self.open_page()
                endpoints.defer_post = True
                dialog = self.start_generation(page, script)
                page.wait_for_function("() => document.querySelector('.production-dialog [data-submit]').disabled")
                self.assertEqual(len(endpoints.pending_posts), 1)
                self.close_dialog(page, dialog, method, locked=True)
                self.assertTrue(dialog.evaluate("node => node.open"))
                self.assertEqual(endpoints.count("POST", TABLE_ROOT), 1)
                endpoints.confirm_post(endpoints.pending_posts.pop())
                dialog.get_by_role("button", name="关闭窗口", exact=True).wait_for()
                self.close_dialog(page, dialog, method)
                self.assertEqual(page.locator(".production-dialog[open]").count(), 0)
                self.assert_no_cancellation(endpoints)
                page.close()

    def test_saved_running_task_can_close_by_cross_escape_or_footer_without_cancel(self):
        for method in ("cross", "escape", "footer"):
            with self.subTest(close=method):
                page, script, endpoints = self.open_page()
                dialog = self.start_generation(page, script)
                dialog.get_by_role("button", name="关闭窗口", exact=True).wait_for()
                self.assertIn("后台", dialog.inner_text())
                self.assertTrue(dialog.locator('[name="instruction"]').is_disabled())
                self.close_dialog(page, dialog, method)
                self.assertEqual(page.locator(".production-dialog[open]").count(), 0)
                page.evaluate("({script, id}) => { void CaseProduction.resumeCarouselTable(script, id); }",
                              {"script": script, "id": TABLE_ID})
                resumed = page.locator(".production-dialog[open]")
                resumed.get_by_role("button", name="关闭窗口", exact=True).wait_for()
                self.assertEqual(endpoints.count("POST", TABLE_ROOT), 1)
                self.assertEqual(endpoints.count("GET", f"{TABLE_ROOT}/{TABLE_ID}"), 1)
                self.close_dialog(page, resumed, method)
                endpoints.tables[TABLE_ID] = table_record(script, status="completed")
                page.clock.run_for(8000)
                self.assertEqual(page.locator(".production-dialog[open]").count(), 0)
                self.assertEqual(endpoints.count("POST", TABLE_ROOT), 1)
                self.assertEqual(endpoints.count("POST", "/api/production/carousels"), 0)
                self.assert_no_cancellation(endpoints)
                page.close()

    def test_late_completed_read_does_not_replace_a_new_production_dialog(self):
        page, script, endpoints = self.open_page()
        endpoints.defer_details = True
        dialog = self.start_generation(page, script)
        dialog.get_by_role("button", name="关闭窗口", exact=True).wait_for()
        with page.expect_request(f"**{TABLE_ROOT}/{TABLE_ID}"):
            page.clock.run_for(2600)
        self.wait_for_fixture(page, lambda: len(endpoints.pending_details) == 1)
        self.assertEqual(len(endpoints.pending_details), 1)
        self.close_dialog(page, dialog, "cross")
        page.evaluate("script => { void CaseProduction.fromScript(script); }", script)
        new_dialog = page.locator(".production-dialog[open]")
        new_dialog.get_by_role("heading", name="为这条脚本创建制作单", exact=True).wait_for()
        new_dialog.evaluate("node => { node.dataset.fixtureNewDialog = 'yes'; }")
        with page.expect_response(f"**{TABLE_ROOT}/{TABLE_ID}") as completed_read:
            endpoints.pending_details.pop().fulfill(json={"table": table_record(script, status="completed")})
        completed_read.value.body()
        page.clock.run_for(3000)
        self.assertEqual(page.locator('.production-dialog[open][data-fixture-new-dialog="yes"]').count(), 1)
        self.assertEqual(new_dialog.get_by_role("heading").inner_text(), "为这条脚本创建制作单")
        self.assertEqual(endpoints.count("POST", TABLE_ROOT), 1)
        self.assertEqual(endpoints.count("POST", "/api/production/carousels"), 0)
        self.assert_no_cancellation(endpoints)

    def test_completed_result_resumes_same_table_and_linked_carousel_opens_without_regeneration(self):
        script = self.create_script()
        table = table_record(script, status="completed")
        page, script, endpoints = self.open_page(script=script, tables=[table])
        for attempt in range(2):
            page.evaluate("({script, id}) => { void CaseProduction.resumeCarouselTable(script, id); }",
                          {"script": script, "id": TABLE_ID})
            dialog = page.locator(".production-dialog[open]")
            dialog.get_by_role("heading", name="核对图文逐张表", exact=True).wait_for()
            self.assertEqual(dialog.locator("tbody tr").count(), 3)
            self.assertIn("Hook 1", dialog.inner_text())
            self.assertEqual(endpoints.count("GET", f"{TABLE_ROOT}/{TABLE_ID}"), attempt + 1)
            self.assertEqual(endpoints.count("POST", TABLE_ROOT), 0)
            if attempt == 0:
                self.close_dialog(page, dialog, "cross")
        dialog.get_by_role("button", name="创建图文制作单", exact=True).click()
        page.wait_for_url(f"**/#/202H/production/{CAROUSEL_ID}")
        self.assertEqual(endpoints.count("POST", "/api/production/carousels"), 1)
        body = next(call["body"] for call in endpoints.calls
                    if call["method"] == "POST" and call["path"] == "/api/production/carousels")
        self.assertEqual(body["carousel"], {"script_id": script["id"], "table_id": TABLE_ID})
        page.evaluate("({script, id}) => { void CaseProduction.resumeCarouselTable(script, id); }",
                      {"script": script, "id": TABLE_ID})
        page.wait_for_function("() => !document.querySelector('.production-dialog[open]')")
        page.clock.run_for(100)
        self.assertEqual(endpoints.count("GET", f"{TABLE_ROOT}/{TABLE_ID}"), 3)
        self.assertEqual(endpoints.count("POST", TABLE_ROOT), 0)
        self.assertEqual(endpoints.count("POST", "/api/production/carousels"), 1)
        self.assertIn(f"/production/{CAROUSEL_ID}", page.url)
        self.assert_no_cancellation(endpoints)

    def test_script_table_link_recovers_exact_result_and_preserves_other_script_edit(self):
        script = self.create_script()
        tables = [table_record(script, status="completed", title="Chosen"),
                  table_record(script, table_id=OTHER_TABLE_ID, status="completed", title="Other")]
        page, script, endpoints = self.open_page(script=script, tables=tables, tail=f"?table={TABLE_ID}")
        dialog = page.locator(".production-dialog[open]")
        dialog.get_by_role("heading", name="核对图文逐张表", exact=True).wait_for()
        self.assertIn("Chosen 1", dialog.inner_text())
        self.assertNotIn("Other 1", dialog.inner_text())
        self.assertEqual(endpoints.count("GET", f"{TABLE_ROOT}/{OTHER_TABLE_ID}"), 0)
        self.assertEqual(endpoints.count("POST", TABLE_ROOT), 0)
        self.close_dialog(page, dialog, "cross")
        page.locator("#script-more-actions > summary").click()
        page.locator("#script-derive").click()
        editor = page.locator("#script-editor-dialog[open]")
        editor.locator('[name="text"]').fill("Keep this unsaved script draft.")
        reads_before = endpoints.count("GET", f"{TABLE_ROOT}/{TABLE_ID}")
        page.evaluate("({id, scriptId}) => { void CaseScripts.resumeTableResult(id, scriptId); }",
                      {"id": TABLE_ID, "scriptId": script["id"]})
        page.clock.run_for(100)
        self.assertEqual(editor.locator('[name="text"]').input_value(), "Keep this unsaved script draft.")
        self.assertTrue(editor.evaluate("node => node.open"))
        self.assertEqual(page.locator(".production-dialog[open]").count(), 0)
        self.assertEqual(endpoints.count("GET", f"{TABLE_ROOT}/{TABLE_ID}"), reads_before)
        self.assertEqual(endpoints.count("POST", TABLE_ROOT), 0)
        self.assert_no_cancellation(endpoints)

    def test_existing_table_uses_get_resume_and_new_generation_requires_two_explicit_steps(self):
        for status in ("running", "completed"):
            with self.subTest(status=status):
                script = self.create_script()
                table = table_record(script, status=status)
                page, script, endpoints = self.open_page(script=script, tables=[table])
                page.evaluate("script => { void CaseProduction.fromScriptCarousel(script); }", script)
                dialog = page.locator(".production-dialog[open]")
                dialog.get_by_role("heading", name="继续图文逐张表", exact=True).wait_for()
                self.assertEqual(endpoints.count("POST", TABLE_ROOT), 0)
                dialog.locator(f'[data-resume-table="{TABLE_ID}"]').click()
                resumed = page.locator(".production-dialog[open]")
                if status == "running":
                    resumed.get_by_role("button", name="关闭窗口", exact=True).wait_for()
                else:
                    resumed.get_by_role("heading", name="核对图文逐张表", exact=True).wait_for()
                self.assertEqual(endpoints.count("GET", f"{TABLE_ROOT}/{TABLE_ID}"), 1)
                self.assertEqual(endpoints.count("POST", TABLE_ROOT), 0)
                self.close_dialog(page, resumed, "cross")

                page.evaluate("script => { void CaseProduction.fromScriptCarousel(script); }", script)
                dialog = page.locator(".production-dialog[open]")
                dialog.get_by_role("heading", name="继续图文逐张表", exact=True).wait_for()
                dialog.get_by_role("button", name="另生成逐张表", exact=True).click()
                generator = page.locator(".production-dialog[open]")
                generator.get_by_role("heading", name="用图文技能生成逐张表", exact=True).wait_for()
                self.assertEqual(endpoints.count("POST", TABLE_ROOT), 0)
                generator.locator('[name="instruction"]').fill("另外生成三张临时图文。")
                generator.get_by_role("button", name="生成逐张表", exact=True).click()
                generator.get_by_role("button", name="关闭窗口", exact=True).wait_for()
                self.assertTrue(generator.locator('[name="instruction"]').is_disabled())
                self.assertEqual(endpoints.count("POST", TABLE_ROOT), 1)
                submitted = next(call["body"] for call in endpoints.calls
                                 if call["method"] == "POST" and call["path"] == TABLE_ROOT)
                self.assertEqual(submitted["script_id"], script["id"])
                self.assertEqual(submitted["instruction"], "另外生成三张临时图文。")
                self.assert_no_cancellation(endpoints)
                page.close()


if __name__ == "__main__":
    unittest.main()
