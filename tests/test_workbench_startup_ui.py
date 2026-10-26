"""Startup regressions use a disposable library and held case-library reads."""
from __future__ import annotations

import unittest

import test_module_loading_ui as loading


class WorkbenchStartupTests(unittest.TestCase):
    setUpClass = classmethod(loading.ModuleLoadingTests.setUpClass.__func__)
    tearDownClass = classmethod(loading.ModuleLoadingTests.tearDownClass.__func__)
    setUp = loading.ModuleLoadingTests.setUp
    stop_server = loading.ModuleLoadingTests.stop_server
    request = loading.ModuleLoadingTests.request
    page = loading.ModuleLoadingTests.page
    hold_read = loading.ModuleLoadingTests.hold_read

    def test_independent_pages_open_before_the_library_and_keep_the_same_dom(self):
        modules = (
            ("content", "#content-flow-list .empty"),
            ("scripts", "#content-flow-list .empty"),
            ("production", "#content-flow-list .empty"),
            ("videos?tab=list", ".perf-table"),
            ("reviews", ".rev-card"),
            ("settings", ".settings-page"),
        )
        for module, ready in modules:
            with self.subTest(module=module):
                page, held = self.page(), []
                self.hold_read(page, "**/api/state", held, "state")
                page.goto(f"{self.base}/#/202H/{module}")
                page.locator(ready).first.wait_for()
                expected_module = "content" if module in {"scripts", "production"} else module.split("?")[0]
                self.assertEqual(page.evaluate("location.hash.split('?')[0]"), f"#/202H/{expected_module}")
                if expected_module == "content":
                    self.assertEqual(page.locator("#production-queue, #production-settings-open, .scripts-tabs").count(), 0)
                page.wait_for_function("() => window.heldReads?.state === 1")
                self.assertEqual(len(held), 1)
                self.assertEqual(page.evaluate("state.token"), "")
                page.evaluate("window.firstPageNode = document.querySelector('#content').firstElementChild")
                query = page.locator("#content input[type=search]:visible").first
                if query.count():
                    query.fill("draft while the library is loading")
                    query.focus()
                    page.evaluate("window.firstQueryNode = document.activeElement")
                held.pop().continue_()
                page.wait_for_function("() => !!state.token && !state.polling")
                self.assertTrue(page.evaluate("firstPageNode === document.querySelector('#content').firstElementChild"))
                if query.count():
                    self.assertEqual(query.input_value(), "draft while the library is loading")
                    self.assertTrue(page.evaluate("document.activeElement === firstQueryNode"))
                page.close()

    def test_a_write_from_the_early_page_waits_for_the_session_token(self):
        for module, ready in (("settings", ".settings-page"), ("home", ".home-tabs")):
            with self.subTest(module=module):
                page, held, writes = self.page(), [], []
                self.hold_read(page, "**/api/state", held, "state")

                def capture_write(route):
                    writes.append(route.request.headers.get("x-workbench-token"))
                    route.fulfill(json={"ok": True})

                page.route("**/api/fixture-startup-write", capture_write)
                page.goto(f"{self.base}/#/202H/{module}")
                page.locator(ready).wait_for()
                page.wait_for_function("() => window.heldReads?.state === 1")
                page.evaluate("""() => {
                    window.earlyWriteDone = false;
                    void api('/api/fixture-startup-write', {method: 'POST', body: {}})
                        .then(() => { window.earlyWriteDone = true; });
                }""")
                page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                self.assertEqual(writes, [])
                held.pop().continue_()
                page.wait_for_function("() => earlyWriteDone")
                self.assertEqual(writes, [self.server.app.token])
                page.close()

    def test_repeated_shell_updates_keep_the_top_action_node(self):
        page = self.page()
        page.goto(f"{self.base}/#/202H/content")
        page.locator("#content-flow-list .empty").wait_for()
        page.wait_for_function("() => !!state.token && !state.polling")
        self.assertTrue(page.evaluate("""() => {
            const button = document.querySelector('#add-top');
            const original = button.firstElementChild;
            button.focus();
            WorkbenchShell.update();
            WorkbenchShell.update();
            WorkbenchShell.update();
            return original === button.firstElementChild && document.activeElement === button;
        }"""))

    def test_home_reads_work_in_parallel_with_the_library_without_duplicate_reads(self):
        page, held, work_reads = self.page(), [], []
        work_paths = ("/api/performance/videos", "/api/ai/tasks", "/api/creation?",
                      "/api/review-followups?", "/api/content?")
        page.on("request", lambda request: work_reads.append(request.url)
                if any(request.url.startswith(self.base + path) for path in work_paths) else None)
        self.hold_read(page, "**/api/state", held, "state")
        page.goto(f"{self.base}/#/202H/home")
        page.wait_for_function("""() => ['tasks', 'due', 'plans'].every(key => {
          const node = document.querySelector(`[data-home-tab=${key}] .home-tab-count`);
          return node && node.textContent !== '—';
        })""")
        self.assertEqual(len(held), 1)
        self.assertEqual(len(work_reads), 5)
        self.assertEqual(page.evaluate("state.token"), "")
        self.assertEqual(page.locator('[data-home-tab=cases] .home-tab-count').inner_text(), "—")
        page.locator('[data-home-tab=cases]').click()
        self.assertIn("正在读取", page.locator('#home-cases-title-panel').inner_text())
        page.evaluate("window.homeNode = document.querySelector('#home-page')")
        held.pop().continue_()
        page.wait_for_function("() => !!state.token && !state.polling")
        self.assertNotEqual(page.locator('[data-home-tab=cases] .home-tab-count').inner_text(), "—")
        self.assertEqual(page.locator('[data-home-tab=cases]').get_attribute('aria-selected'), 'true')
        self.assertTrue(page.evaluate("homeNode === document.querySelector('#home-page')"))
        self.assertEqual(len(work_reads), 5, "The library result reuses the five already loaded work lists")

    def test_home_keeps_work_visible_when_the_library_fails_and_can_retry(self):
        page, held = self.page(), []
        self.hold_read(page, "**/api/state", held, "state")
        page.goto(f"{self.base}/#/202H/home")
        page.wait_for_function("() => document.querySelector('[data-home-tab=plans] .home-tab-count')?.textContent === '0'")
        held.pop().fulfill(status=503, json={"error": "temporary library failure"})
        page.locator('[data-home-tab=cases]').click()
        page.get_by_text("案例资料暂时无法读取。", exact=True).wait_for()
        self.assertEqual(page.locator('[data-home-tab=cases] .home-tab-count').inner_text(), "—")
        self.assertEqual(page.locator('[data-home-tab=plans] .home-tab-count').inner_text(), "0")
        page.unroute("**/api/state")
        page.locator('#home-cases-title-panel [data-home-retry]').click()
        page.wait_for_function("() => !!state.token && !state.polling")
        page.wait_for_function("() => document.querySelector('[data-home-tab=cases] .home-tab-count').textContent !== '—'")
        self.assertEqual(page.locator('[data-home-tab=cases]').get_attribute('aria-selected'), 'true')
        self.assertEqual(page.get_by_text("案例资料暂时无法读取。", exact=True).count(), 0)

    def test_late_initial_library_does_not_replace_the_new_home_product_or_page(self):
        for destination, ready in (("#/129N/home", "#home-page"), ("#/202H/settings", ".settings-page")):
            with self.subTest(destination=destination):
                page, held = self.page(), []
                self.hold_read(page, "**/api/state", held, "state")
                page.goto(f"{self.base}/#/202H/home")
                page.locator('.home-tabs').wait_for()
                page.evaluate("hash => { location.hash = hash; }", destination)
                if destination.endswith('/home'):
                    page.locator('.home-product').get_by_text('129N', exact=True).wait_for()
                else:
                    page.locator(ready).wait_for()
                page.evaluate("window.destinationNode = document.querySelector('#content').firstElementChild")
                held.pop().continue_()
                page.wait_for_function("() => !!state.token && !state.polling")
                self.assertTrue(page.evaluate("destinationNode === document.querySelector('#content').firstElementChild"))
                self.assertEqual(page.evaluate("location.hash"), destination)
                if destination.endswith('/home'):
                    self.assertEqual(page.locator('.home-product').inner_text(), '129N')
                    self.assertNotIn('完整系统价值', page.locator('#home-page').inner_text())
                page.close()


if __name__ == "__main__":
    unittest.main()
