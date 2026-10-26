"""Direction detail first paint, task-loading races and off-screen interactions.

All browser requests use an isolated temporary case library. Delayed responses
are held by Playwright so a real slow task read can be observed without sleeps.
"""
import copy
import unittest
from urllib.parse import parse_qs, urlparse

import test_direction_catalog_ui as catalog_ui
import test_server as fixture


class DirectionProgressiveTests(unittest.TestCase):
    setUpClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.tearDownClass.__func__)
    setUp = catalog_ui.DirectionCatalogBrowserTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    make_playable_video = catalog_ui.DirectionCatalogBrowserTests.make_playable_video
    catalog = catalog_ui.DirectionCatalogBrowserTests.catalog
    create_point = catalog_ui.DirectionCatalogBrowserTests.create_point
    open_page = catalog_ui.DirectionCatalogBrowserTests.open_page

    detail_pattern = "**/api/direction-catalog/202H-S1?**"
    task_pattern = "**/api/ai/tasks?**"

    def core_data(self):
        status, _, data = self.request("GET", "/api/direction-catalog/202H-S1?product=202H&view=page&include_tasks=0")
        self.assertEqual(status, 200, data)
        variants = data["direction"].get("variants") or [data["direction"]]
        prototype = copy.deepcopy(variants[0])
        data["direction"]["variants"] = [
            {**copy.deepcopy(prototype), "baseline": index == 0,
             "concern": f"隔离测试顾虑 {index + 1}", "closing": f"隔离测试收口 {index + 1}",
             "execution_change": f"隔离测试改动 {index + 1}", "progression": "问题-解决"}
            for index in range(9)
        ]
        data["tasks"] = []
        data["tasks_pending"] = True
        return data

    def task_data(self, title="延迟到达的 AI 任务"):
        return {"task_id": "AIT-123456789abc", "case_id": self.identifier,
                "case_title": title, "status": "completed", "updated_at": "2026-10-11T08:00:00Z"}

    def intercept_core(self, page, data):
        calls = []

        def respond(route):
            calls.append(route.request.url)
            route.fulfill(status=200, json=copy.deepcopy(data))

        page.route(self.detail_pattern, respond)
        return calls

    def hold_tasks(self, page):
        held = []

        def hold(route):
            if parse_qs(urlparse(route.request.url).query).get("direction_id"):
                held.append(route)
            else:
                route.continue_()

        page.route(self.task_pattern, hold)
        return held

    def wait_for_task_read(self, page, held):
        # Browser event delivery runs while the status assertion waits.
        page.wait_for_function("()=>document.querySelector('#direction-tasks-panel')?.getAttribute('aria-busy')==='true'")
        deadline = 30
        while not held and deadline:
            page.wait_for_timeout(10)
            deadline -= 1
        self.assertEqual(len(held), 1, "A task read should start after the core detail is visible")

    def open_detail(self, page, route="#/202H/directions/202H-S1"):
        page.evaluate("value=>{location.hash=value}", route)
        page.locator("#direction-definition").wait_for()

    def test_core_and_all_variants_render_before_tasks_and_keep_user_selection(self):
        page = self.open_page()
        data = self.core_data()
        core_calls = self.intercept_core(page, data)
        held = self.hold_tasks(page)
        page.evaluate("""()=>{
            new MutationObserver(()=>{
                if(document.querySelector('#direction-definition')&&!window.__coreFrameRequested){
                    window.__coreFrameRequested=true;
                    requestAnimationFrame(()=>window.__firstCoreFrame=performance.now());
                }
            }).observe(document.querySelector('#content'),{childList:true,subtree:true});
        }""")
        observed = []
        page.on("request", lambda request: observed.append(page.evaluate("""()=>({
            core:!!document.querySelector('#direction-definition'),
            variants:document.querySelectorAll('[data-variant-progression]').length,
            frame:!!window.__firstCoreFrame})"""))
                if parse_qs(urlparse(request.url).query).get("direction_id") == ["202H-S1"] else None)
        self.open_detail(page)
        self.wait_for_task_read(page, held)
        self.assertEqual(page.locator(".dir-variant").count(), 9)
        self.assertEqual(page.locator("#direction-tasks small").inner_text(), "…")
        self.assertIn("正在读取 AI 任务", page.locator("#direction-tasks-panel").inner_text())
        self.assertNotIn("还没有关联", page.locator("#direction-tasks-panel").inner_text())
        self.assertEqual(observed, [{"core": True, "variants": 9, "frame": True}])
        query = parse_qs(urlparse(core_calls[0]).query)
        self.assertEqual(query["view"], ["page"])
        self.assertEqual(query["include_tasks"], ["0"])
        task_query = parse_qs(urlparse(held[0].request.url).query)
        self.assertEqual(task_query, {"product": ["202H"], "direction_id": ["202H-S1"]})
        page.locator("#direction-cases").click()
        page.evaluate("""()=>{
            window.__variantControl=document.querySelector('[data-variant-progression="8"]');
            window.__variantControl.value='结果倒叙';
        }""")
        held[0].fulfill(status=200, json={"tasks": [self.task_data()]})
        page.wait_for_function("()=>document.querySelector('#direction-tasks small')?.textContent==='1'")
        self.assertEqual(page.locator("#direction-cases").get_attribute("aria-selected"), "true")
        self.assertIsNone(page.locator("#direction-tasks-panel").get_attribute("aria-busy"))
        self.assertTrue(page.evaluate("document.querySelector('[data-variant-progression=\"8\"]')===window.__variantControl"))
        self.assertEqual(page.locator('[data-variant-progression="8"]').input_value(), "结果倒叙")
        page.locator("#direction-tasks").click()
        page.get_by_role("link", name="延迟到达的 AI 任务 已完成", exact=False).wait_for()

    def test_all_products_resolves_the_task_product_and_restores_default_task_tab(self):
        page = self.open_page()
        data = self.core_data()
        data["scripts"] = []
        data["videos"] = []
        self.intercept_core(page, data)
        held = self.hold_tasks(page)
        self.open_detail(page, "#/all/directions/202H-S1")
        self.wait_for_task_read(page, held)
        self.assertEqual(parse_qs(urlparse(held[0].request.url).query)["product"], ["202H"])
        self.assertEqual(page.locator("#direction-cases").get_attribute("aria-selected"), "true")
        held[0].fulfill(status=200, json={"tasks": [self.task_data()]})
        page.locator("#direction-tasks-panel .direction-related-item").wait_for()
        self.assertEqual(page.locator("#direction-tasks").get_attribute("aria-selected"), "true")

    def test_task_failure_is_unknown_and_retry_only_reloads_tasks(self):
        page = self.open_page()
        core_calls = self.intercept_core(page, self.core_data())
        held = self.hold_tasks(page)
        self.open_detail(page)
        self.wait_for_task_read(page, held)
        page.locator("#direction-tasks").click()
        page.evaluate("window.__definitionNode=document.querySelector('#direction-definition')")
        held[0].fulfill(status=503, json={"error": "隔离测试：任务读取暂不可用"})
        page.locator("#direction-task-retry").wait_for()
        self.assertEqual(page.locator("#direction-tasks small").inner_text(), "—")
        self.assertIn("任务读取暂不可用", page.locator("#direction-tasks-panel [role=alert]").inner_text())
        self.assertIsNone(page.locator("#direction-tasks-panel").get_attribute("aria-busy"))
        held.clear()
        page.locator("#direction-task-retry").click()
        self.wait_for_task_read(page, held)
        held[0].fulfill(status=200, json={"tasks": []})
        page.locator("#direction-tasks-panel").get_by_text("还没有关联的 AI 协作任务。", exact=True).wait_for()
        self.assertEqual(page.locator("#direction-tasks small").inner_text(), "0")
        self.assertEqual(len(core_calls), 1)
        self.assertTrue(page.evaluate("document.querySelector('#direction-definition')===window.__definitionNode"))

    def test_older_full_response_needs_no_additional_task_read(self):
        page = self.open_page()
        data = self.core_data()
        data.pop("tasks_pending")
        data["tasks"] = [self.task_data("旧服务完整任务")]
        self.intercept_core(page, data)
        held = self.hold_tasks(page)
        self.open_detail(page)
        page.locator("#direction-tasks").click()
        page.get_by_role("link", name="旧服务完整任务 已完成", exact=False).wait_for()
        page.evaluate("()=>new Promise(resolve=>requestAnimationFrame(()=>setTimeout(resolve,0)))")
        self.assertEqual(held, [])
        self.assertEqual(page.locator("#direction-tasks small").inner_text(), "1")
        self.assertIsNone(page.locator("#direction-tasks-panel").get_attribute("aria-busy"))

    def test_leaving_while_core_is_pending_does_not_start_tasks(self):
        page = self.open_page()
        cores = []
        page.route(self.detail_pattern, lambda route: cores.append(route))
        held = self.hold_tasks(page)
        page.evaluate("location.hash='#/202H/directions/202H-S1'")
        page.locator("#content [role=status]").get_by_text("正在读取卖点与方向…", exact=True).wait_for()
        page.wait_for_timeout(30)
        self.assertEqual(len(cores), 1)
        page.evaluate("location.hash='#/202H/settings'")
        page.locator(".settings-page").wait_for()
        cores[0].fulfill(status=200, json=self.core_data())
        page.evaluate("()=>new Promise(resolve=>requestAnimationFrame(()=>setTimeout(resolve,0)))")
        self.assertEqual(held, [])
        self.assertEqual(page.locator("#direction-definition").count(), 0)
        self.assertEqual(page.evaluate("location.hash"), "#/202H/settings")

    def test_leaving_before_scheduled_task_read_cancels_the_read(self):
        page = self.open_page()
        self.intercept_core(page, self.core_data())
        held = self.hold_tasks(page)
        page.evaluate("""()=>{
            const observer=new MutationObserver(()=>{
                if(document.querySelector('#direction-definition')){
                    observer.disconnect();location.hash='#/202H/settings';
                }
            });observer.observe(document.querySelector('#content'),{childList:true,subtree:true});
            location.hash='#/202H/directions/202H-S1';
        }""")
        page.locator(".settings-page").wait_for()
        page.evaluate("()=>new Promise(resolve=>requestAnimationFrame(()=>setTimeout(resolve,0)))")
        self.assertEqual(held, [])
        self.assertEqual(page.locator("#direction-definition").count(), 0)

    def test_late_task_response_does_not_patch_the_next_page(self):
        page = self.open_page()
        self.intercept_core(page, self.core_data())
        held = self.hold_tasks(page)
        self.open_detail(page)
        self.wait_for_task_read(page, held)
        page.evaluate("location.hash='#/202H/settings'")
        page.locator(".settings-page").wait_for()
        # Settings has its own asynchronous backup-status update. Detect a stale
        # direction render without treating that legitimate update as pollution.
        page.evaluate("window.__settingsNode=document.querySelector('.settings-page')")
        held[0].fulfill(status=200, json={"tasks": [self.task_data("过期回应不能出现")]})
        page.evaluate("()=>new Promise(resolve=>requestAnimationFrame(()=>setTimeout(resolve,0)))")
        self.assertTrue(page.evaluate("document.querySelector('.settings-page')===window.__settingsNode"))
        self.assertEqual(page.evaluate("location.hash"), "#/202H/settings")
        self.assertNotIn("过期回应不能出现", page.locator("#content").inner_text())
        self.assertEqual(page.locator("#direction-definition").count(), 0)
        self.assertEqual(page.locator("#direction-tasks").count(), 0)

    def test_ninth_variant_and_offscreen_case_table_work_on_a_narrow_viewport(self):
        page = self.open_page(width=390)
        self.intercept_core(page, self.core_data())
        page.route(self.task_pattern, lambda route: route.fulfill(status=200, json={"tasks": []})
                   if parse_qs(urlparse(route.request.url).query).get("direction_id") else route.continue_())
        writes = []

        def save_variant(route):
            if route.request.method == "POST":
                writes.append(route.request.post_data_json)
                route.fulfill(status=200, json={})
            else:
                route.continue_()

        page.route("**/api/direction-catalog", save_variant)
        self.open_detail(page)
        page.locator('details.dir-variant[data-variant-index="8"] > summary').click()
        last = page.locator('[data-variant-progression="8"]')
        last.scroll_into_view_if_needed()
        self.assertTrue(last.is_visible())
        self.assertIn("隔离测试顾虑 9", page.locator(".dir-variant").last.inner_text())
        last.select_option("结果倒叙")
        page.get_by_text("变体标注已保存", exact=True).wait_for()
        self.assertEqual(writes[0]["direction"]["index"], 8)
        self.assertEqual(writes[0]["direction"]["progression"], "结果倒叙")
        table = page.locator(".dir-primary>.direction-group .direction-observations")
        table.scroll_into_view_if_needed()
        table.focus()
        self.assertTrue(table.locator("tbody tr").first.is_visible())
        table.evaluate("node=>{node.scrollLeft=node.scrollWidth}")
        self.assertGreater(table.evaluate("node=>node.scrollLeft"), 0)
        table.locator("summary").first.click()
        self.assertTrue(table.locator(".direction-record-details").first.get_attribute("open") is not None)
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth>document.documentElement.clientWidth"))


if __name__ == "__main__":
    unittest.main()
