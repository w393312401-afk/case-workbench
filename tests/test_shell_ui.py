"""Workflow-shell integration against a disposable library and browser profile."""
from __future__ import annotations

import copy
import json
import unittest
from urllib.parse import parse_qs

import test_server as fixture
import test_ui_regressions as browser
from test_review_store import build_sources


class ShellBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def setUp(self):
        browser.WorkbenchBrowserTests.setUp(self)
        build_sources(self.runtime / "reviews" / "sources")
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["test_direction"] = {
            "direction_id": "202H-S1", "main_difference": "S1 定角不用猜", "concern": "装夹怎么做",
            "closing": "查看工具组合", "variant_type": "direction",
        }
        self.store.append(self.identifier, {"kind": "editorial", "text": "Isolated shell fixture", "current": current})
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def open_page(self, route="#library", *, width=1280, navigation=None):
        context = self.browser.new_context(viewport={"width": width, "height": 900})
        self.addCleanup(context.close)
        if navigation is not None:
            context.add_init_script("localStorage.setItem('case-library-navigation-v1', " + json.dumps(navigation) + ");")
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        page.goto(self.base + "/" + route)
        page.locator("#connection-text").get_by_text("案例库同步").wait_for(state="attached")
        return page

    def assert_route(self, page, name, *, product="202H", tail="", **query):
        expected = f"#/{product}/{name}{tail}"
        page.wait_for_function("expected => location.hash.split('?')[0] === expected", arg=expected)
        path, _, parameters = page.evaluate("location.hash").partition("?")
        self.assertEqual(path, expected)
        self.assertEqual(parse_qs(parameters), {key: [value] for key, value in query.items()})
        self.assertEqual(page.locator("#global-product").input_value(), product)

    def assert_no_overflow(self, page):
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))

    def test_legacy_links_canonicalize_and_page_actions_follow_the_page(self):
        page = self.open_page()
        routes = [
            ("#library", "library", "#case-search", "收录案例", "", {}),
            ("#native", "library", "#case-search", "收录案例", "", {"view": "native"}),
            (f"#case/{self.identifier}/history", "case", ".history-item", "用这个案例写稿", f"/{self.identifier}/history", {}),
            ("#files", "settings", ".settings-page", "", "", {}),
            ("#videos", "videos", ".perf-tabs", "", "", {}),
            ("#directions", "directions", "#direction-filters", "新建方向", "", {}),
            ("#reviews", "reviews", "#rev-page", "", "", {}),
            ("#ai", "ai", "#ai-task-list", "开始写稿", "", {}),
        ]
        for old, name, ready, action, tail, query in routes:
            with self.subTest(route=old):
                page.goto(self.base + "/" + old)
                page.locator(ready).first.wait_for()
                self.assert_route(page, name, tail=tail, **query)
                # Pages whose actions only sync or check data keep them in the page header, not the top bar.
                if action:
                    self.assertEqual(page.locator("#add-top").inner_text().strip(), action)
                else:
                    self.assertFalse(page.locator("#add-top").is_visible())
        page.locator("#add-top").click()
        page.locator("#ai-create-dialog[open]").wait_for()
        self.assertIn("空白写稿", page.locator("#ai-create-case").inner_text())
        page.locator("#cancel-ai-create").click()
        page.locator('[data-ai-start="case"]').click()
        page.locator(f'#writing-case-dialog[open] [data-case-id="{self.identifier}"]').click()
        page.locator("#ai-create-dialog[open]").wait_for()
        self.assertEqual(page.locator("#ai-create-case").inner_text(), "完整系统价值")
        self.assertEqual(page.locator("#ai-create-version").inner_text(), "基于当前案例版本")
        self.assert_route(page, "ai")
        self.assertEqual(self.request("GET", "/api/ai/tasks")[2]["tasks"], [])
        page.locator("#cancel-ai-create").click()
        page.goto(f"{self.base}/#case/{self.identifier}")
        page.locator("#case-player").wait_for(state="attached")
        page.locator("#add-top").click()
        page.locator("#ai-create-dialog[open]").wait_for()
        self.assertEqual(page.locator("#ai-create-case").inner_text(), "完整系统价值")
        self.assertEqual(self.request("GET", "/api/ai/tasks")[2]["tasks"], [])
        page.locator("#cancel-ai-create").click()
        page.get_by_role("button", name="编辑概要", exact=True).click()
        page.locator('.case-inline-editor[data-edit-section="overview"]').wait_for()
        self.assertEqual(page.locator('[name="title"]').input_value(), "完整系统价值")
        page.locator("[data-inline-cancel]").first.click()

    def test_empty_global_product_context_keeps_modules_scoped_while_library_is_independent(self):
        page = self.open_page("#/999X/library")
        page.locator("#case-search").wait_for()
        self.assertEqual(page.locator(".case-card").count(), 1)
        self.assertEqual(page.locator("#library-product-filter").input_value(), "")
        for name, ready in [("reviews", "#rev-page"), ("directions", "#direction-filters"),
                            ("videos", ".perf-tabs"), ("content", "#content-flow-list"), ("settings", ".settings-page"),
                            ("library", "#case-search")]:
            page.locator(f'[data-page="{name}"]').click()
            page.locator(ready).first.wait_for()
            self.assert_route(page, name, product="999X")
            if name == "reviews":
                page.locator("#rev-count").get_by_text("0 条", exact=False).wait_for()
                self.assertEqual(page.locator(".rev-card").count(), 0)
            if name == "directions":
                page.get_by_text("当前产品还没有卖点", exact=True).wait_for()
                self.assertNotIn("202H-S1", page.locator("#content").inner_text())
        page.reload()
        page.locator("#case-search").wait_for()
        self.assert_route(page, "library", product="999X")
        self.assertEqual(page.locator(".case-card").count(), 1)
        self.assertEqual(page.locator("#library-product-filter").input_value(), "")
        page.locator("#global-product").select_option("202H")
        page.locator(f'.case-card[href="#case/{self.identifier}"]').wait_for()
        self.assert_route(page, "library")

    def test_global_search_keyboard_opens_case_and_escape_keeps_current_case(self):
        page = self.open_page()
        page.keyboard.press("Control+k")
        page.locator("#workbench-search[open]").wait_for()
        page.locator("#workbench-search-input").fill(self.identifier)
        result = page.locator(f'.global-search-result[href="#/202H/case/{self.identifier}"]')
        result.wait_for()
        self.assertIn("完整系统价值", result.inner_text())
        page.keyboard.press("Enter")
        page.locator("#case-player").wait_for(state="attached")
        self.assert_route(page, "case", tail="/" + self.identifier)
        self.assertFalse(page.locator("#workbench-search").evaluate("element => element.open"))
        page.evaluate("window.shellTestPlayer = document.querySelector('#case-player')")
        page.keyboard.press("Control+k")
        page.locator("#workbench-search[open]").wait_for()
        page.keyboard.press("Escape")
        self.assertFalse(page.locator("#workbench-search").evaluate("element => element.open"))
        self.assert_route(page, "case", tail="/" + self.identifier)
        self.assertTrue(page.evaluate("document.querySelector('#case-player') === window.shellTestPlayer"))

    def test_settings_retains_saved_navigation_and_assignment_on_narrow_screen(self):
        custom_id = "custom-abcdefgh"
        before = self.store.read_case(self.identifier)
        body = {"case_id": self.identifier, "target": "column:" + custom_id,
                "revision": fixture.SERVER.revision(before), "previous": None}
        self.assertEqual(self.request("POST", "/api/navigation/move", body)[0], 200)
        assignment = (self.runtime / "navigation-assignments.json").read_bytes()
        legacy = json.dumps([
            {"id": custom_id, "label": "我的常用案例", "icon": "folder", "visible": True,
             "content": "custom", "description": "以前保存的栏目", "caseIds": [self.identifier]},
            {"id": "all", "label": "全部资料", "icon": "grid", "visible": True},
            {"id": "positive", "label": "暂时隐藏", "visible": False},
        ], ensure_ascii=False)
        page = self.open_page("#files", width=390, navigation=legacy)
        page.locator(".settings-page").wait_for()
        self.assert_route(page, "settings")
        self.assert_no_overflow(page)
        page.get_by_role("button", name="管理保存的视图").click()
        page.locator("#library-view-manager[open]").wait_for()
        custom = page.locator(f'#library-view-manager [data-view-id="{custom_id}"]')
        self.assertEqual(custom.locator("input").input_value(), "我的常用案例")
        self.assert_no_overflow(page)
        page.get_by_role("button", name="完成", exact=True).click()
        self.assertEqual(page.evaluate("localStorage.getItem('case-library-navigation-v1')"), legacy)
        page.locator('[data-page="library"]').click()
        page.locator("#case-search").wait_for()
        page.locator("#library-saved-view").select_option(custom_id)
        page.locator(f'.case-card[href="#case/{self.identifier}"]').wait_for()
        self.assertTrue(page.locator("#library-navigation").is_hidden())
        self.assert_no_overflow(page)
        page.keyboard.press("Control+k")
        page.locator("#workbench-search-input").fill(self.identifier)
        page.locator(".global-search-result").first.wait_for()
        self.assert_no_overflow(page)
        bounds = page.locator("#workbench-search").bounding_box()
        self.assertGreaterEqual(bounds["x"], 0)
        self.assertLessEqual(bounds["x"] + bounds["width"], 390)
        page.keyboard.press("Escape")
        self.assertEqual((self.runtime / "navigation-assignments.json").read_bytes(), assignment)
        self.assertEqual(self.store.read_case(self.identifier), before)


if __name__ == "__main__":
    unittest.main()
