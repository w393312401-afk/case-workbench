"""Maintenance UI checks use a disposable workbench and mocked transfer status."""
from __future__ import annotations

from copy import deepcopy
import unittest
from urllib.parse import urlsplit

from direction_store import DirectionStore
import test_backup_ui as backup
import test_direction_picker_ui as directions
import test_performance as performance
import test_phase4_writing_ui as writing


class DataMaintenanceBrowserTests(unittest.TestCase):
    setUpClass = classmethod(writing.WritingLifecycleBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(writing.WritingLifecycleBrowserTests.tearDownClass.__func__)
    setUp = writing.WritingLifecycleBrowserTests.setUp
    stop_server = writing.WritingLifecycleBrowserTests.stop_server
    request = writing.WritingLifecycleBrowserTests.request
    add_direction = directions.DirectionPickerBrowserTests.add_direction

    def open_page(self, route="#/202H/home", *, busy=False, operation=None):
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(12000)
        page.clock.install()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        self.transfer_status = {**deepcopy(backup.STATUS), "busy": busy, "operation": operation,
                                "progress": "正在恢复数据库" if busy else ""}
        self.calls = []
        self.stable_state = None

        def dispatch(request_route):
            request = request_route.request
            path = urlsplit(request.url).path
            self.calls.append((path, request.method, urlsplit(request.url).query))
            if path == "/api/data/status":
                request_route.fulfill(json=deepcopy(self.transfer_status))
            elif path == "/api/data/preferences" or path == "/api/fixture-write":
                request_route.fulfill(json={"ok": True})
            elif (self.transfer_status["busy"] and self.transfer_status["operation"] == "restore"
                  and path != "/api/service/status"):
                request_route.fulfill(status=409, json={"error": "总数据正在恢复，请稍后读取。",
                                                       "maintenance": True, "operation": "restore"})
            elif path == "/api/state" and self.stable_state:
                result = (self.stable_state if not urlsplit(request.url).query else
                          {"unchanged": True, "token": self.stable_state["token"]})
                request_route.fulfill(json=deepcopy(result))
            else:
                request_route.continue_()

        page.route("**/api/**", dispatch)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/{route}")
        return page

    def wait_connected(self, page):
        page.locator("#connection-text").get_by_text("案例库同步").wait_for(state="attached")
        page.wait_for_function("()=>!state.polling")

    def finish_transfer(self):
        self.transfer_status.update(busy=False, operation=None, progress="")

    def test_restore_on_initial_load_opens_settings_and_recovers_without_error_storm(self):
        page = self.open_page(busy=True, operation="restore")
        page.locator("#data-maintenance-waiting").wait_for()
        page.wait_for_function("()=>!state.polling")
        business_before = [call for call in self.calls if call[0] not in ("/api/data/status", "/api/service/status")]
        page.clock.run_for(15000)
        page.wait_for_function("()=>!state.polling")
        business_after = [call for call in self.calls if call[0] not in ("/api/data/status", "/api/service/status")]
        self.assertEqual(business_after, business_before)
        self.assertFalse(page.locator("#error-banner").is_visible())
        self.assertNotIn("无法连接", page.locator("#connection-text").inner_text())
        page.locator("#data-maintenance-banner a").click()
        page.locator("#settings-data-card").wait_for()
        page.wait_for_function("()=>!document.querySelector('#settings-data-refresh').disabled")
        self.assertFalse(any(call[0] == "/api/data/preferences" for call in self.calls))
        self.assertTrue(page.locator("#settings-data-export").is_disabled())
        self.finish_transfer()
        page.locator("#settings-data-refresh").click()
        page.locator("#data-maintenance-banner").wait_for(state="detached")
        page.wait_for_function("()=>!!state.token && !state.polling")
        self.assertFalse(page.locator("#settings-data-export").is_disabled())
        page.evaluate("location.hash='#/202H/home'")
        page.locator("#home-page").wait_for()
        self.assertEqual(page.locator("#data-maintenance-waiting").count(), 0)

    def test_restore_of_identical_data_forces_route_after_maintenance_ends(self):
        page = self.open_page()
        self.wait_connected(page)
        page.locator("#home-page").wait_for()
        original = page.evaluate("({digest:state.digest,signature:state.signature})")
        self.stable_state = page.evaluate("({navigation:state.navigation,cases:state.cases,paths:state.paths,token:state.token,digest:state.digest,signature:state.signature})")
        self.transfer_status.update(busy=True, operation="restore", progress="正在恢复数据库")
        page.evaluate("refresh()")
        page.locator("#data-maintenance-waiting").wait_for()
        self.assertEqual(page.evaluate("state.digest"), original["digest"])
        resumed_at = len(self.calls)
        self.finish_transfer()
        page.clock.run_for(5000)
        page.locator("#data-maintenance-waiting").wait_for(state="detached")
        page.locator("#home-page").wait_for()
        self.assertEqual(page.evaluate("({digest:state.digest,signature:state.signature})"), original)
        resumed_state_reads = [call for call in self.calls[resumed_at:] if call[0] == "/api/state"]
        self.assertTrue(resumed_state_reads)
        self.assertEqual(resumed_state_reads[0][2], "")

    def test_restore_keeps_unsaved_video_direction_choice_during_manual_and_background_refresh(self):
        self.catalog_store = DirectionStore(self.runtime, self.skill)
        for direction in (directions.FIRST, directions.SECOND):
            self.add_direction(direction)
        folder = self.root / "exports"
        performance.build_exports(folder)
        self.server.app.performance.sources = [folder]
        self.assertEqual(self.request("POST", "/api/performance/sync", {})[0], 200)
        page = self.open_page(f"#/202H/videos?video={directions.VIDEO}")
        self.wait_connected(page)
        drawer = page.locator("#perf-video-drawer")
        drawer.wait_for(state="visible")
        drawer.locator("[data-drawer-close]").click()
        cell = page.locator(f'[data-direction-video="{directions.VIDEO}"]')
        cell.locator("[data-video-direction]").click()
        form = cell.locator("form")
        form.wait_for()
        form.locator("select").select_option("202H-S2")
        self.transfer_status.update(busy=True, operation="restore", progress="正在恢复数据库")
        form.get_by_role("button", name="保存", exact=True).click()
        page.locator("#data-maintenance-banner").wait_for()
        page.evaluate("refresh()")
        self.assertEqual(page.locator("#data-maintenance-waiting").count(), 0)
        self.assertEqual(form.locator("select").input_value(), "202H-S2")
        page.clock.run_for(5000)
        self.assertTrue(form.is_visible())
        self.finish_transfer()
        page.clock.run_for(5000)
        page.locator("#data-maintenance-banner").wait_for(state="detached")
        self.assertTrue(form.is_visible())
        self.assertEqual(form.locator("select").input_value(), "202H-S2")
        self.assertIsNone(self.catalog_store.find_link(self.catalog_store.read(), "202H", directions.VIDEO))
        self.assertEqual(sum(call[:2] == ("/api/video-directions", "POST") for call in self.calls), 1)

    def test_export_allows_business_reads_and_inspection_allows_business_writes(self):
        page = self.open_page()
        self.wait_connected(page)
        self.transfer_status.update(busy=True, operation="export", progress="正在打包文件 25/100")
        page.evaluate("result=>WorkbenchMaintenance.update(result)", self.transfer_status)
        self.assertTrue(page.evaluate("async()=>!!(await api('/api/creation?product=202H')).revision"))
        error = page.evaluate("async()=>{try{await api('/api/fixture-write',{method:'POST',body:{}});return null;}catch(error){return {maintenance:error.maintenance,operation:error.operation};}}")
        self.assertEqual(error, {"maintenance": True, "operation": "export"})
        self.assertFalse(any(call[0] == "/api/fixture-write" for call in self.calls))
        self.transfer_status.update(operation="inspect", progress="正在检查备份文件")
        page.evaluate("result=>WorkbenchMaintenance.update(result)", self.transfer_status)
        self.assertTrue(page.evaluate("async()=>!!(await api('/api/fixture-write',{method:'POST',body:{}})).ok"))
        self.assertEqual(sum(call[0] == "/api/fixture-write" for call in self.calls), 1)
        self.assertEqual(page.locator("#data-maintenance-waiting").count(), 0)


if __name__ == "__main__":
    unittest.main()
