"""Direction filter reuse and refresh correctness, using an isolated case library."""
import re
import unittest
from urllib.parse import urlparse
from uuid import uuid4

import test_direction_catalog_ui as catalog_ui
import test_server as fixture


class DirectionInteractionTests(unittest.TestCase):
    setUpClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.tearDownClass.__func__)
    setUp = catalog_ui.DirectionCatalogBrowserTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    make_playable_video = catalog_ui.DirectionCatalogBrowserTests.make_playable_video
    catalog = catalog_ui.DirectionCatalogBrowserTests.catalog
    create_point = catalog_ui.DirectionCatalogBrowserTests.create_point
    open_page = catalog_ui.DirectionCatalogBrowserTests.open_page

    def watch_reads(self, page):
        reads = []
        page.on("request", lambda request: reads.append(request.url)
                if request.method == "GET" and urlparse(request.url).path in
                ("/api/direction-catalog", "/api/directions") else None)
        return reads

    def test_search_and_tier_status_use_the_complete_mounted_data_and_keep_history(self):
        page = self.open_page()
        reads = self.watch_reads(page)
        page.locator('[name="q"]').fill("定角")
        page.wait_for_function("()=>new URLSearchParams(location.hash.split('?')[1]).get('q')==='定角'")
        self.assertEqual(page.locator("#dir-list .dir-row").count(), 1)
        # The table is the other tab: the search carries over and the tiers filter there.
        page.locator('[data-dx-tab="points"]').click()
        page.locator("#dir-points .px-row").wait_for()
        self.assertEqual(page.locator("#dir-points .px-row").count(), 1)
        page.locator('[data-tier-filter="A"]').click()
        page.locator(".direction-empty").wait_for()
        page.locator('[name="q"]').fill("")
        page.wait_for_function("()=>document.querySelectorAll('#dir-points .px-row').length===1&&document.querySelector('#dir-points .dir-code').textContent==='A8'")
        page.locator('[data-tier-filter=""]').click()
        page.wait_for_function("()=>document.querySelector('[data-tier-filter=\\\"\\\"]')?.getAttribute('aria-pressed')==='true'")
        self.assertEqual(page.locator("#dir-points .px-row").count(), 3)
        self.assertEqual(reads, [])
        page.go_back()
        page.wait_for_function("()=>document.querySelectorAll('#dir-points .px-row').length===1")
        self.assertEqual(page.locator('#direction-filters [name="tier"]').input_value(), "A")
        self.assertEqual(reads, [])

    def test_archive_product_and_page_reopen_read_fresh_data(self):
        page = self.open_page()
        reads = self.watch_reads(page)
        # The 案例范围 picker left the page on 2026-11-22; the scope still comes from the address.
        page.evaluate("location.hash='#/all/directions?archive=all'")
        page.locator("#direction-filters").wait_for()
        self.assertEqual(len(reads), 2)
        reads.clear()
        page.evaluate("location.hash='#/all/settings'")
        page.locator(".settings-page").wait_for()
        page.evaluate("location.hash='#/all/directions?archive=all'")
        page.locator("#direction-filters").wait_for()
        self.assertEqual(len(reads), 2)

    def test_force_and_direction_change_read_new_saved_data(self):
        page = self.open_page("#/202H/directions?tab=points")
        reads = self.watch_reads(page)
        self.create_point("B10", "刷新之后的新卖点")
        page.evaluate("async()=>await route(true,true)")
        page.locator(".dir-list").get_by_text("刷新之后的新卖点", exact=True).wait_for(state="attached")
        self.assertEqual(len(reads), 2)
        reads.clear()
        self.create_point("B11", "事件通知之后的新卖点")
        page.evaluate("window.dispatchEvent(new CustomEvent('workbench:directionchange'))")
        page.locator(".dir-list").get_by_text("事件通知之后的新卖点", exact=True).wait_for(state="attached")
        self.assertEqual(len(reads), 2)

    def test_detail_names_the_chain_from_one_response_and_supports_older_servers(self):
        page = self.open_page()
        reads = self.watch_reads(page)
        page.locator('.dir-row[data-direction-id="202H-S1"] a').first.click()
        page.locator("#direction-definition").wait_for()
        page.locator('#direction-definition .dir-ref[title]').first.wait_for()
        self.assertEqual(reads, [])
        page.locator("#navigate-up").click()
        page.locator("#direction-filters").wait_for()
        reads.clear()

        def older_response(route):
            # Ask without the page's "unchanged?" tag, so the answer always carries a body to rewrite.
            headers = {key: value for key, value in route.request.headers.items() if key.lower() != "if-none-match"}
            response = route.fetch(headers={**headers, "Connection": "close"})
            data = response.json()
            data.pop("selling_points", None)
            route.fulfill(status=response.status, json=data)

        page.route("**/api/direction-catalog/202H-S1?**", older_response)
        catalog_ui.show_direction(page)
        page.locator('#direction-definition .dir-ref[title]').first.wait_for()
        # Beside the mounted list the names come from the list itself; only a direction opened on its own asks again.
        self.assertEqual(reads, [])
        # A narrow window opened straight on the direction has no list to take them from and asks once more.
        context = self.browser.new_context(viewport={"width": 390, "height": 900})
        self.addCleanup(context.close)
        narrow = context.new_page()
        narrow.set_default_timeout(10000)
        narrow.route("**/api/direction-catalog/202H-S1?**", older_response)
        narrow.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/directions/202H-S1")
        narrow.locator('#direction-definition .dir-ref[title]').first.wait_for()
        self.assertEqual(narrow.locator("#dir-list").count(), 0)

    def test_choosing_another_direction_reads_only_that_direction_and_is_remembered(self):
        second = self.create_point("S2", "一套磨到镜面")
        status, _, saved = self.request("POST", "/api/direction-catalog", {
            "revision": self.catalog()["revision"], "request_id": uuid4().hex,
            "direction": {"product": "202H", "selling_point_id": second["id"], "concern": "全套会不会多余", "closing": "下方看整套"}})
        self.assertEqual(status, 200, saved)
        page = self.open_page()
        # With no address and nothing remembered, the first direction of the list opens beside it.
        page.locator('#direction-pane:not([aria-busy])[data-direction-id="202H-S1"]').wait_for()
        self.assertEqual(page.locator("#dir-list .dx-item.is-active").get_attribute("data-direction-id"), "202H-S1")
        self.assertEqual(page.evaluate("location.hash"), "#/202H/directions")
        reads = self.watch_reads(page)
        details = []
        page.on("request", lambda request: details.append(urlparse(request.url).path)
                if urlparse(request.url).path.startswith("/api/direction-catalog/") else None)
        page.evaluate("window.__list=document.querySelector('#dir-list')")
        page.locator('#dir-list [data-direction-id="202H-S2"] a').click()
        page.locator('#direction-pane:not([aria-busy])[data-direction-id="202H-S2"]').wait_for()
        self.assertIn("一套磨到镜面", page.locator("#direction-definition h2").inner_text())
        self.assertEqual(page.locator("#dir-list .dx-item.is-active").get_attribute("data-direction-id"), "202H-S2")
        self.assertTrue(page.evaluate("window.__list===document.querySelector('#dir-list')"))
        self.assertEqual((reads, details), ([], ["/api/direction-catalog/202H-S2"]))
        # Back on the bare list address it is still the one open, also after the page is opened afresh.
        page.locator("#navigate-up").click()
        page.wait_for_function("location.hash==='#/202H/directions'")
        self.assertEqual(page.locator("#direction-pane").get_attribute("data-direction-id"), "202H-S2")
        self.assertEqual(details, ["/api/direction-catalog/202H-S2"])
        page.reload()
        page.locator('#direction-pane:not([aria-busy])[data-direction-id="202H-S2"]').wait_for()
        # A narrow window shows the list alone, then the direction alone.
        page.set_viewport_size({"width": 390, "height": 900})
        self.assertTrue(page.locator("#dir-list").is_visible())
        self.assertTrue(page.locator("#direction-pane").is_hidden())
        page.locator('#dir-list [data-direction-id="202H-S1"] a').click()
        page.locator('#direction-pane:not([aria-busy])[data-direction-id="202H-S1"]').wait_for()
        self.assertTrue(page.locator("#dir-list").is_hidden())
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth>document.documentElement.clientWidth"))

    def test_composition_then_enter_preserves_the_final_search_and_caret(self):
        page = self.open_page()
        reads = self.watch_reads(page)
        original_count = page.locator(".dir-row").count()
        page.evaluate("""()=>{
            const input=document.querySelector('[name=q]');input.focus();
            input.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));
            input.value='定角';
            input.dispatchEvent(new InputEvent('input',{bubbles:true,isComposing:true}));
        }""")
        page.wait_for_timeout(200)
        self.assertEqual(page.locator(".dir-row").count(), original_count)
        self.assertNotIn("q=", page.evaluate("location.hash"))
        page.evaluate("""()=>{
            const input=document.querySelector('[name=q]');
            input.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true}));
            input.setSelectionRange(1,1);
            input.dispatchEvent(new KeyboardEvent('keydown',{bubbles:true,key:'Enter'}));
        }""")
        page.wait_for_function("()=>document.querySelectorAll('.dir-row').length===1")
        self.assertEqual(page.locator('[name="q"]').evaluate("input=>[document.activeElement===input,input.selectionStart,input.selectionEnd]"), [True, 1, 1])
        page.wait_for_timeout(200)
        self.assertEqual(reads, [])

    def test_page_response_renders_the_same_detail_as_the_full_response(self):
        page = self.open_page()
        pattern = "**/api/direction-catalog/202H-S1?**"

        def full_response(route):
            response = route.fetch(url=route.request.url.replace("view=page", "view=full"),
                                   headers={**route.request.headers, "Connection": "close"})
            route.fulfill(status=response.status, json=response.json())

        settled = "()=>document.querySelector('.direction-variant-list.has-progress')&&document.querySelector('#direction-tasks-panel')?.getAttribute('aria-busy')!=='true'"
        page.route(pattern, full_response)
        catalog_ui.show_direction(page)
        page.wait_for_function(settled)
        full_html = page.locator("#content").inner_html()
        page.unroute(pattern, full_response)
        page.locator("#navigate-up").click()
        page.locator("#direction-filters").wait_for()
        catalog_ui.show_direction(page)
        page.wait_for_function(settled)
        # Enhanced dropdowns number their generated ids per page render.
        numbered = lambda html: re.sub(r"select-\d+", "select-N", html)
        self.assertEqual(numbered(page.locator("#content").inner_html()), numbered(full_html))

    def test_pending_search_does_not_navigate_after_leaving_or_product_switch(self):
        page = self.open_page()
        reads = self.watch_reads(page)
        page.locator('[name="q"]').fill("定角")
        page.evaluate("location.hash='#/202H/settings'")
        page.locator(".settings-page").wait_for()
        page.wait_for_timeout(200)
        self.assertEqual(page.evaluate("location.hash"), "#/202H/settings")
        self.assertEqual(reads, [])
        page.evaluate("location.hash='#/202H/directions'")
        page.locator("#direction-filters").wait_for()
        page.locator('[name="q"]').fill("定角")
        page.evaluate("location.hash='#/all/directions'")
        page.wait_for_function("()=>!!document.querySelector('#direction-filters')&&!document.querySelector('[name=q]').value")
        page.wait_for_timeout(200)
        self.assertEqual(page.evaluate("location.hash"), "#/all/directions")
        self.assertEqual(page.locator('[name="q"]').input_value(), "")


if __name__ == "__main__":
    unittest.main()
