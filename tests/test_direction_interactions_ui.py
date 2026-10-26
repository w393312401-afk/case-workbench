"""Direction filter reuse and refresh correctness, using an isolated case library."""
import unittest
from urllib.parse import urlparse

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
        self.assertEqual(page.locator(".dir-row").count(), 1)
        page.locator('[data-tier-filter="A"]').click()
        page.locator(".direction-empty").wait_for()
        page.locator('[name="q"]').fill("")
        page.wait_for_function("()=>document.querySelectorAll('.dir-row').length===1&&document.querySelector('.dir-code').textContent==='A8'")
        page.locator('[data-tier-filter=""]').click()
        page.wait_for_function("()=>document.querySelector('[data-tier-filter=\\\"\\\"]')?.getAttribute('aria-pressed')==='true'")
        page.evaluate("()=>{const form=document.querySelector('#direction-filters');form.elements.status.value='active';form.requestSubmit();}")
        page.locator(".direction-empty").wait_for()
        self.assertEqual(reads, [])
        page.go_back()
        page.locator('.dir-row[data-point-id]').first.wait_for()
        self.assertEqual(page.locator('#direction-filters [name="status"]').input_value(), "")
        self.assertEqual(reads, [])

    def test_archive_product_and_page_reopen_read_fresh_data(self):
        page = self.open_page()
        reads = self.watch_reads(page)
        page.locator('[name="archive"]').select_option("all")
        page.wait_for_function("()=>document.querySelector('#direction-filters')?.elements.archive.value==='all'")
        self.assertEqual(len(reads), 2)
        reads.clear()
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
        page = self.open_page()
        reads = self.watch_reads(page)
        self.create_point("B10", "刷新之后的新卖点")
        page.evaluate("async()=>await route(true,true)")
        page.locator(".dir-list").get_by_text("刷新之后的新卖点", exact=True).wait_for()
        self.assertEqual(len(reads), 2)
        reads.clear()
        self.create_point("B11", "事件通知之后的新卖点")
        page.evaluate("window.dispatchEvent(new CustomEvent('workbench:directionchange'))")
        page.locator(".dir-list").get_by_text("事件通知之后的新卖点", exact=True).wait_for()
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
            response = route.fetch(headers={**route.request.headers, "Connection": "close"})
            data = response.json()
            data.pop("selling_points", None)
            route.fulfill(status=response.status, json=data)

        page.route("**/api/direction-catalog/202H-S1?**", older_response)
        page.locator('.dir-row[data-direction-id="202H-S1"] a').first.click()
        page.locator("#direction-definition").wait_for()
        page.locator('#direction-definition .dir-ref[title]').first.wait_for()
        self.assertEqual(len(reads), 1)

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

        page.route(pattern, full_response)
        page.locator('.dir-row[data-direction-id="202H-S1"] a').first.click()
        page.locator("#direction-definition").wait_for()
        page.wait_for_function("()=>!document.querySelector('#direction-progress-summary')?.textContent.startsWith('正在核对')")
        full_html = page.locator("#content").inner_html()
        page.unroute(pattern, full_response)
        page.locator("#navigate-up").click()
        page.locator("#direction-filters").wait_for()
        page.locator('.dir-row[data-direction-id="202H-S1"] a').first.click()
        page.locator("#direction-definition").wait_for()
        page.wait_for_function("()=>document.querySelector('#direction-tasks-panel')?.getAttribute('aria-busy')!=='true'")
        page.wait_for_function("()=>!document.querySelector('#direction-progress-summary')?.textContent.startsWith('正在核对')")
        self.assertEqual(page.locator("#content").inner_html(), full_html)

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
