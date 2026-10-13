"""Return navigation preserves real writing-module controls in disposable browser profiles."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from uuid import uuid4

import test_ai_selection_ui as ai_fixture
import test_phase4_writing_ui as writing_fixture
import test_review_ui as review_fixture
import test_server as fixture
import test_performance as performance
from elevenlabs_client import ElevenLabsClient
from test_production import FakeEleven
from test_review_store import write


class ModuleBackNavigationBrowserTests(unittest.TestCase):
    setUpClass = classmethod(writing_fixture.WritingLifecycleBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(writing_fixture.WritingLifecycleBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    detail = fixture.WorkbenchHTTPTests.detail
    create_task = ai_fixture.AISelectionBrowserTests.create_task
    open_page = writing_fixture.WritingLifecycleBrowserTests.open_page

    def setUp(self):
        writing_fixture.WritingLifecycleBrowserTests.setUp(self)
        self.app = self.server.app
        self.app.voices = ElevenLabsClient(key_provider=lambda: "sk_test", transport=FakeEleven())
        self.app.resolve = SimpleNamespace(status=lambda: {"ok": False, "error": "隔离测试不连接达芬奇"})
        self.script = self.create_script("导航目标脚本")
        self.other_script = self.create_script("排除记录")
        self.job = self.app.create_production_job({
            "request_id": uuid4().hex, "revision": self.app.production.public()["revision"],
            "job": {"script_id": self.script["id"]},
        })["job"]
        self.other_job = self.app.create_production_job({
            "request_id": uuid4().hex, "revision": self.app.production.public()["revision"],
            "job": {"script_id": self.other_script["id"]},
        })["job"]
        self.app.archive_job(self.job["id"], {
            "request_id": uuid4().hex, "revision": self.app.production.public()["revision"], "archived": True,
        })

    def create_script(self, title):
        return self.app.creation.mutate("script", {
            "request_id": uuid4().hex, "revision": self.app.creation.public()["revision"],
            "script": {"product": "202H", "title": title, "text": "A temporary navigation fixture.", "confirmed": True},
        })["script"]

    def assert_return(self, page, route, ready, scroll):
        page.locator(ready).wait_for()
        page.wait_for_function("expected => location.hash === expected", arg=route)
        page.wait_for_function("expected => Math.abs(window.scrollY - expected) <= 2", arg=scroll)

    def create_scrolling_list(self, page, selector):
        # Real cards and filters remain intact; spacing permits a visible scroll
        # regression with a small temporary library and the production CSP.
        page.evaluate("""selector => {
            const sheet = document.styleSheets[0];
            sheet.insertRule(selector + ' { padding-top: 850px !important; padding-bottom: 1500px !important; }', sheet.cssRules.length);
        }""", selector)

    def enter_detail(self, page, link, ready):
        link.scroll_into_view_if_needed()
        page.wait_for_function("() => window.scrollY > 300")
        scroll = page.evaluate("window.scrollY")
        link.click()
        page.locator(ready).wait_for()
        return scroll

    def leave_detail(self, page, action):
        if action == "top":
            page.locator("#navigate-up").click()
        elif action == "breadcrumb":
            page.locator("#breadcrumb li:not([aria-current]) a").last.click()
        else:
            page.locator("#content h1").click()
            page.keyboard.press("Escape")

    def test_script_search_status_and_scroll_survive_all_return_actions(self):
        page = self.open_page("#/202H/scripts")
        page.locator("#script-query").fill("导航目标")
        page.locator("#script-status-filter").select_option("adopted")
        self.create_scrolling_list(page, "#script-list")
        for action in ("top", "breadcrumb", "escape"):
            with self.subTest(action=action):
                self.assertEqual(page.locator("#script-list .script-card").count(), 1)
                link = page.locator("#script-list .script-card")
                scroll = self.enter_detail(page, link, "#script-production-toggle")
                self.assertEqual(page.locator("#navigate-up").get_attribute("href"), "#/202H/scripts")
                self.assertEqual(page.locator("#content .back-link").inner_text().strip(), "返回脚本版本")
                self.assertFalse(page.locator("#content .back-link").is_visible())
                self.leave_detail(page, action)
                self.assert_return(page, "#/202H/scripts", "#script-query", scroll)
                self.assertEqual(page.locator("#script-query").input_value(), "导航目标")
                self.assertEqual(page.locator("#script-status-filter").input_value(), "adopted")

    def test_standard_production_archive_filter_and_scroll_survive_all_return_actions(self):
        page = self.open_page("#/202H/production")
        page.locator("#production-archived").check()
        self.create_scrolling_list(page, ".production-main")
        for action in ("top", "breadcrumb", "escape"):
            with self.subTest(action=action):
                link = page.locator(f'.production-card[href$="/{self.job["id"]}"]')
                scroll = self.enter_detail(page, link, "#job-archive")
                self.assertEqual(page.locator("#content .back-link").inner_text().strip(), "返回制作单")
                self.assertEqual(page.locator("#content .back-link").get_attribute("href"), "#/202H/production")
                self.leave_detail(page, action)
                self.assert_return(page, "#/202H/production", "#production-archived", scroll)
                self.assertTrue(page.locator("#production-archived").is_checked())
                self.assertEqual(page.locator(".production-card").count(), 2)

    def test_all_product_production_return_keeps_archive_scope_scroll_and_empty_picker(self):
        page = self.open_page("#/all/production")
        page.locator("#production-archived").check()
        self.create_scrolling_list(page, ".production-main")
        for action in ("top", "breadcrumb"):
            with self.subTest(action=action):
                link = page.locator(f'.production-card[href$="/{self.job["id"]}"]')
                scroll = self.enter_detail(page, link, "#job-archive")
                self.assertEqual(page.evaluate("location.hash"), f'#/202H/production/{self.job["id"]}')
                for selector in ("#navigate-up", "#content .back-link"):
                    self.assertEqual(page.locator(selector).get_attribute("href"), "#/all/production")
                if action == "breadcrumb":
                    page.locator("#breadcrumb").get_by_role("link", name="制作单", exact=True).click()
                else:
                    page.locator("#navigate-up").click()
                self.assert_return(page, "#/all/production", "#production-archived", scroll)
                self.assertTrue(page.locator("#production-archived").is_checked())
                self.assertEqual(page.locator("#global-product").input_value(), "")

    def test_ai_status_grouping_and_scroll_survive_all_return_actions(self):
        task = self.create_task("临时导航任务")
        page = self.open_page("#/202H/ai")
        page.locator('[data-ai-filter="completed"]').click()
        page.locator("#ai-group-by").select_option("case")
        self.create_scrolling_list(page, "#ai-task-list")
        for action in ("breadcrumb", "top", "escape"):
            with self.subTest(action=action):
                link = page.locator(f'[data-ai-task-id="{task["task_id"]}"] .ai-task-card')
                scroll = self.enter_detail(page, link, "#ai-rounds")
                self.assertEqual(page.locator("#content .back-link").inner_text().strip(), "返回内容创作")
                self.leave_detail(page, action)
                self.assert_return(page, "#/202H/ai", "#ai-task-list", scroll)
                self.assertEqual(page.locator('[data-ai-filter="completed"]').get_attribute("aria-pressed"), "true")
                self.assertEqual(page.locator("#ai-group-by").input_value(), "case")

    def test_all_product_script_detail_reload_keeps_the_parent_scope_and_filters(self):
        page = self.open_page("#/all/scripts")
        page.locator("#script-query").fill("导航目标")
        page.locator("#script-status-filter").select_option("adopted")
        page.locator("#script-list .script-card").click()
        page.locator("#script-production-toggle").wait_for()
        page.reload()
        page.locator("#script-production-toggle").wait_for()
        self.assertEqual(page.locator("#content .back-link").get_attribute("href"), "#/all/scripts")
        page.locator("#navigate-up").click()
        page.locator("#script-query").wait_for()
        self.assertEqual(page.evaluate("location.hash"), "#/all/scripts")
        self.assertEqual(page.locator("#script-query").input_value(), "导航目标")
        self.assertEqual(page.locator("#script-status-filter").input_value(), "adopted")
        self.assertEqual(page.locator("#global-product").input_value(), "")
        self.assertEqual(page.locator("#script-list .script-card").count(), 1)

    def test_viewing_previous_script_version_keeps_the_original_all_product_parent(self):
        child = self.app.creation.mutate("script", {
            "request_id": uuid4().hex, "revision": self.app.creation.public()["revision"],
            "script": {"product": "202H", "title": "导航子版本", "text": "A revised temporary version.",
                       "confirmed": True, "parent_id": self.script["id"]},
        })["script"]
        page = self.open_page("#/all/scripts")
        page.locator("#script-query").fill("导航子版本")
        page.locator("#script-list .script-card").click()
        page.locator("#script-production-toggle").wait_for()
        page.get_by_role("link", name="查看上一版本", exact=True).click()
        page.locator("#content h1").get_by_text("导航目标脚本", exact=True).wait_for()
        self.assertEqual(page.evaluate("location.hash"), f'#/202H/scripts/{self.script["id"]}')
        self.assertEqual(page.locator("#content .back-link").get_attribute("href"), "#/all/scripts")
        page.locator("#navigate-up").click()
        page.locator("#script-query").wait_for()
        self.assertEqual(page.locator("#script-query").input_value(), "导航子版本")
        self.assertEqual(page.locator("#global-product").input_value(), "")
        self.assertEqual(page.locator("#script-list .script-card").count(), 1)

    def test_direct_all_product_production_deep_link_has_consistent_parent(self):
        page = self.open_page(f'#/all/production/{self.job["id"]}')
        page.locator("#job-archive").wait_for()
        breadcrumb = page.locator("#breadcrumb").get_by_role("link", name="制作单", exact=True)
        for link in (breadcrumb, page.locator("#navigate-up"), page.locator("#content .back-link")):
            self.assertEqual(link.get_attribute("href"), "#/all/production")
        page.locator("#navigate-up").click()
        page.locator("#production-settings-open").wait_for()
        self.assertEqual(page.evaluate("location.hash"), "#/all/production")
        self.assertEqual(page.locator("#global-product").input_value(), "")
        self.assertFalse(page.locator("#production-archived").is_checked())

    def test_weekly_selected_tab_and_scroll_survive_switching_modules(self):
        page = self.open_page("#/202H/home")
        page.locator('[data-home-tab="cases"]').click()
        self.create_scrolling_list(page, "#home-page")
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        page.evaluate("window.scrollTo(0, 640)")
        page.wait_for_function("() => window.scrollY === 640")
        page.locator('[data-page="settings"]').click()
        page.locator(".settings-page").wait_for()
        page.locator('[data-page="home"]').click()
        self.assert_return(page, "#/202H/home", '[data-home-tab="cases"]', 640)
        self.assertEqual(page.locator('[data-home-tab="cases"]').get_attribute("aria-selected"), "true")
        self.assertTrue(page.locator("#home-cases-title-panel").is_visible())


class ModuleSwitchingBrowserTests(unittest.TestCase):
    setUpClass = classmethod(review_fixture.ReviewPageTests.setUpClass.__func__)
    tearDownClass = classmethod(review_fixture.ReviewPageTests.tearDownClass.__func__)
    setUp = review_fixture.ReviewPageTests.setUp
    stop_server = review_fixture.ReviewPageTests.stop_server
    request = review_fixture.ReviewPageTests.request
    open_page = review_fixture.ReviewPageTests.open_page
    create_scrolling_list = ModuleBackNavigationBrowserTests.create_scrolling_list

    def switch_away_and_return(self, page, module, ready, scroll):
        page.locator('[data-page="settings"]').click()
        page.locator(".settings-page").wait_for()
        page.locator(f'[data-page="{module}"]').click()
        page.locator(ready).first.wait_for()
        page.wait_for_function("expected => Math.abs(window.scrollY - expected) <= 2", arg=scroll)

    def test_video_legacy_entry_returns_to_current_board_filters_without_reopening_drawer(self):
        page = self.open_page(route=f"#/202H/videos?tab=list&video={performance.LINKED}", ready="#perf-video-drawer[open]")
        page.locator('#perf-video-drawer [data-drawer-close]').click()
        page.locator("#perf-video-drawer").wait_for(state="hidden")
        page.locator("#perf-q").fill("")
        page.locator("#perf-range").select_option("all")
        page.locator("#perf-account").select_option("bushcraftedge")
        page.locator('.perf-table [data-sort="gmv"]').click()
        page.locator('.perf-table [data-sort="gmv"]').click()
        page.locator("#perf-tab-board").click()
        page.locator('[data-perf-metric="views"]').click()
        self.create_scrolling_list(page, "#perf-board")
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        page.evaluate("window.scrollTo(0, 640)")
        page.wait_for_function("() => window.scrollY === 640")
        self.switch_away_and_return(page, "videos", "#perf-board", 640)
        self.assertEqual(page.locator("#perf-tab-board").get_attribute("aria-selected"), "true")
        self.assertEqual(page.locator("#perf-account").input_value(), "bushcraftedge")
        self.assertEqual(page.locator('[data-perf-metric="views"]').get_attribute("aria-pressed"), "true")
        self.assertFalse(page.locator("#perf-video-drawer").is_visible())
        parameters = page.evaluate("Object.fromEntries(new URLSearchParams(location.hash.split('?')[1]))")
        self.assertNotIn("video", parameters)
        self.assertNotEqual(parameters.get("tab"), "list")
        page.locator("#perf-tab-list").click()
        self.assertEqual(page.locator("#perf-q").input_value(), "")
        self.assertEqual(page.locator("#perf-range").input_value(), "all")
        self.assertEqual(page.locator('.perf-table th[aria-sort="ascending"] [data-sort="gmv"]').count(), 1)

    def test_reviews_search_source_sort_and_expanded_count_survive_sidebar_return(self):
        video = f"https://www.tiktok.com/@bushcraftedge/video/{performance.LINKED}"
        write(self.runtime / "reviews" / "sources", "tiktok_video", "navigation.json", [
            {"cid": f"NAV-{index}", "videoWebUrl": video, "text": f"Clamp temporary navigation record {index}.",
             "diggCount": 1, "createTimeISO": "2026-06-04T10:00:00.000Z", "uniqueId": "fixture-reader"}
            for index in range(60)
        ])
        page = self.open_page(route="#/202H/reviews")
        page.locator('#rev-sources [data-code="tiktok_video"]').click()
        page.locator("#rev-q").fill("clamp")
        page.locator("#rev-q").press("Enter")
        page.locator("#rev-active").get_by_text("“clamp”", exact=False).wait_for()
        page.locator("#rev-sort").select_option("oldest")
        page.locator("#rev-more").click()
        page.wait_for_function("() => document.querySelectorAll('.rev-card').length > 50")
        count = page.locator(".rev-card").count()
        page.evaluate("window.scrollTo(0, 640)")
        page.wait_for_function("() => window.scrollY === 640")
        self.switch_away_and_return(page, "reviews", ".rev-card", 640)
        page.wait_for_function("expected => document.querySelectorAll('.rev-card').length === expected", arg=count)
        self.assertEqual(page.locator("#rev-q").input_value(), "clamp")
        self.assertEqual(page.locator("#rev-sort").input_value(), "oldest")
        self.assertEqual(page.locator('#rev-sources [data-code="tiktok_video"]').get_attribute("aria-pressed"), "true")

    def test_reopened_review_conversation_finishes_loading_before_restoring_bottom_scroll(self):
        video = f"https://www.tiktok.com/@bushcraftedge/video/{performance.LINKED}"
        write(self.runtime / "reviews" / "sources", "tiktok_video", "navigation.json", [
            {"cid": f"NAV-{index}", "videoWebUrl": video, "text": f"Temporary navigation record {index}.",
             "diggCount": 1, "createTimeISO": "2026-06-04T10:00:00.000Z", "uniqueId": "fixture-reader"}
            for index in range(65)
        ])
        page = self.open_page(route="#/202H/reviews")
        page.locator("#rev-more").click()
        page.wait_for_function("() => document.querySelectorAll('.rev-card').length > 50")
        thread = page.locator(".rev-card").filter(has_text="That’s a lot of wobble").locator("[data-thread]")
        thread_id = thread.get_attribute("data-thread")
        thread.click()
        page.locator(".rev-thread:not([hidden]) .rev-reply").wait_for()
        count = page.locator(".rev-card").count()
        page.evaluate("""() => {
            document.documentElement.style.overflowAnchor = 'none';
            document.body.style.overflowAnchor = 'none';
            window.scrollTo(0, document.documentElement.scrollHeight);
        }""")
        saved_scroll = page.evaluate("window.scrollY")
        page.locator('[data-page="settings"]').click()
        page.locator(".settings-page").wait_for()
        page.reload()
        page.locator(".settings-page").wait_for()
        page.evaluate("""() => {
            document.documentElement.style.overflowAnchor = 'none';
            document.body.style.overflowAnchor = 'none';
        }""")
        held = []
        page.route(f"**/api/reviews/{thread_id}", lambda request: held.append(request))
        page.locator('[data-page="reviews"]').click()
        page.wait_for_function("expected => document.querySelectorAll('.rev-card').length === expected", arg=count)
        page.wait_for_function("() => !!document.querySelector('.rev-thread:not([hidden])')")
        self.assertEqual(len(held), 1)
        held[0].continue_()
        page.locator(".rev-thread:not([hidden]) .rev-reply").wait_for()
        page.wait_for_function("expected => Math.abs(window.scrollY - expected) <= 2", arg=saved_scroll)
        self.assertEqual(page.locator(".rev-card").filter(has_text="That’s a lot of wobble").locator("[data-thread]").get_attribute("aria-expanded"), "true")


if __name__ == "__main__":
    unittest.main()
