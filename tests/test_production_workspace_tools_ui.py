"""Production settings and permanent deletion are tools of the unified content workspace."""
from __future__ import annotations

import json
import unittest

from playwright.sync_api import expect
import test_ui_regressions as browser


class ProductionWorkspaceToolsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = browser.WorkbenchBrowserTests.stop_server
    request = browser.WorkbenchBrowserTests.request
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def setUp(self):
        browser.WorkbenchBrowserTests.setUp(self)
        self.data = {"revision": 1, "jobs": [], "carousels": [], "voices": [{"voice_id": "fixtureVoice1234567890", "name": "Fixture", "note": "旧备注"}],
                     "defaults": {}, "readings": [], "settings": {"creators": ["Lab"]},
                     "product_settings": {"project": "202H", "code": "202", "fps": "30", "width": 1080, "height": 1920,
                                          "audio_bin": "配音", "timeline_bin": "制作"}}
        self.posts, self.reads, self.eleven_calls = [], [], []

    def open_content(self, hold_settings=None):
        context = self.browser.new_context(viewport={"width": 1080, "height": 850})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))

        def settings(route):
            self.reads.append(route.request.url)
            if hold_settings is not None:
                hold_settings.append(route)
            else:
                route.fulfill(status=200, content_type="application/json", body=json.dumps(self.data))

        def eleven(route):
            self.eleven_calls.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "tier": "fixture"}))

        def save(route):
            body = route.request.post_data_json
            self.posts.append({"url": route.request.url, "body": body})
            if route.request.url.endswith("/voices"):
                voice = body["voice"]
                found = next((item for item in self.data["voices"] if item["voice_id"] == voice["voice_id"]), None)
                operation = body["operation"]
                if operation == "default":
                    self.data["defaults"][body["product"]] = voice["voice_id"]
                elif operation in {"enable", "disable"}:
                    found["disabled"] = operation == "disable"
                elif found:
                    found.update(voice)
                else:
                    self.data["voices"].append({"name": "Added", **voice})
            elif route.request.url.endswith("/readings"):
                if body.get("operation") == "remove":
                    self.data["readings"] = [item for item in self.data["readings"] if item["id"] != body["reading"]["id"]]
                else:
                    self.data["readings"].append({"id": "RDG-fixture", **body["reading"]})
            elif route.request.url.endswith("/settings"):
                self.data["settings"]["creators"] = body["settings"]["creators"]
                self.data["product_settings"].update(body["settings"])
            self.data["revision"] += 1
            route.fulfill(status=200, content_type="application/json", body="{}")

        page.route("**/api/production?*", settings)
        page.route("**/api/production/eleven", eleven)
        for endpoint in ("voices", "readings", "settings"):
            page.route("**/api/production/" + endpoint, save)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/content")
        page.locator("#content-flow-query").wait_for()
        page.evaluate("window.__workspaceHeader=document.querySelector('#content .page-heading')")
        return page

    def open_settings(self, page):
        page.evaluate("void CaseProduction.openSettings()")
        page.locator("#production-settings #voice-add").wait_for()
        return page.locator("#production-settings")

    def assert_workspace_retained(self, page):
        self.assertEqual(page.evaluate("location.hash"), "#/202H/content")
        self.assertTrue(page.evaluate("window.__workspaceHeader===document.querySelector('#content .page-heading')"))
        self.assertEqual(page.locator("#production-queue, .production-main").count(), 0)

    def test_settings_save_all_sections_without_replacing_content_or_other_drafts(self):
        page = self.open_content()
        self.assertEqual(self.eleven_calls, [])
        drawer = self.open_settings(page)
        drawer.get_by_text("ElevenLabs 已连接", exact=False).wait_for()
        drawer.locator('#resolve-settings [name="creators"]').fill("Lab, Zack")
        drawer.locator('#voice-add [name="voice_id"]').fill("anotherVoice1234567890")
        drawer.locator('#voice-add [name="note"]').fill("新音色")
        drawer.locator('#voice-add button[type="submit"]').click()
        drawer.locator('.voice-row').filter(has_text="新音色").wait_for()
        self.assertEqual(drawer.locator('#resolve-settings [name="creators"]').input_value(), "Lab, Zack")
        drawer.locator('#reading-add [name="written"]').fill("202H")
        drawer.locator('#reading-add [name="spoken"]').fill("two oh two H")
        drawer.locator('#reading-add button[type="submit"]').click()
        drawer.locator('.reading-list').get_by_text("two oh two H", exact=True).wait_for()
        drawer.locator('#resolve-settings [name="project"]').fill("202H Workspace")
        drawer.locator('#resolve-settings [name="code"]').fill("202NEW")
        drawer.locator('#resolve-settings [name="fps"]').select_option("29.97")
        drawer.locator('#resolve-settings [name="width"]').fill("1440")
        drawer.locator('#resolve-settings [name="height"]').fill("2560")
        drawer.locator('#resolve-settings button[type="submit"]').click()
        expect(drawer.locator('#resolve-settings [name="project"]')).to_have_value("202H Workspace")
        page.wait_for_function("() => !CaseProduction.hasUnsavedInput()")
        self.assertEqual([call["body"]["revision"] for call in self.posts], [1, 2, 3])
        saved = self.posts[-1]["body"]["settings"]
        self.assertEqual((saved["creators"], saved["product"], saved["fps"], saved["width"], saved["height"]),
                         (["Lab", "Zack"], "202H", "29.97", 1440, 2560))
        self.assert_workspace_retained(page)
        self.assertTrue(all("product=202H" in path for path in self.reads))
        drawer.locator('[data-drawer-close]').click()
        expect(drawer).to_have_count(0)

    def test_voice_defaults_toggle_notes_and_reading_remove_keep_workspace(self):
        page = self.open_content()
        drawer = self.open_settings(page)
        drawer.locator('[data-voice-default]').click()
        drawer.get_by_text("默认", exact=True).wait_for()
        drawer.locator('[data-voice-toggle]').click()
        drawer.get_by_text("已停用", exact=True).wait_for()
        drawer.locator('[data-voice-toggle]').click()
        expect(drawer.get_by_text("已停用", exact=True)).to_have_count(0)
        drawer.locator('[data-voice-note]').click()
        page.locator('.production-dialog [name="note"]').fill("新的声音备注")
        page.locator('.production-dialog [data-submit]').click()
        drawer.get_by_text("新的声音备注", exact=False).wait_for()
        drawer.locator('#reading-add [name="written"]').fill("202H")
        drawer.locator('#reading-add [name="spoken"]').fill("two oh two H")
        drawer.locator('#reading-add button[type="submit"]').click()
        drawer.locator('[data-reading-remove]').click()
        expect(drawer.locator('[data-reading-remove]')).to_have_count(0)
        self.assert_workspace_retained(page)
        self.assertEqual(len(self.posts), 6)

    def test_unsaved_settings_are_kept_when_closing_or_navigating(self):
        page = self.open_content()
        drawer = self.open_settings(page)
        drawer.locator('#resolve-settings [name="creators"]').fill("Unfinished")
        drawer.locator('[data-drawer-close]').click()
        page.locator('#production-settings-discard [data-keep]').click()
        expect(drawer.locator('#resolve-settings [name="creators"]')).to_have_value("Unfinished")
        page.evaluate("location.hash='#/202H/ai'")
        page.locator('#production-settings-discard').wait_for()
        self.assertEqual(page.evaluate("location.hash"), "#/202H/content")
        page.locator('#production-settings-discard [data-keep]').click()
        expect(drawer.locator('#resolve-settings [name="creators"]')).to_have_value("Unfinished")
        drawer.locator('[data-drawer-close]').click()
        page.locator('#production-settings-discard [data-discard]').click()
        expect(drawer).to_have_count(0)
        self.assertEqual(self.posts, [])
        self.assert_workspace_retained(page)

    def test_closed_settings_ignore_late_response(self):
        held = []
        page = self.open_content(hold_settings=held)
        page.evaluate("void CaseProduction.openSettings()")
        page.locator('#production-settings [data-drawer-close]').click()
        page.wait_for_function("() => !CaseProduction.modalOpen()")
        self.assertEqual(len(held), 1)
        held.pop().fulfill(status=200, content_type='application/json', body=json.dumps(self.data))
        expect(page.locator('#production-settings')).to_have_count(0)
        self.assert_workspace_retained(page)

    def test_batch_delete_preserves_per_order_failures_and_refreshes_content_callback(self):
        page = self.open_content()
        sent = []
        def remove(route):
            body = route.request.post_data_json
            sent.append(body)
            route.fulfill(status=200, content_type='application/json', body=json.dumps({
                "changed": 1, "failed": 1, "skipped": 0, "results": [
                    {"id": "PRD-000000000001", "kind": "job", "ok": True, "warnings": []},
                    {"id": "CAR-000000000002", "kind": "carousel", "ok": False, "error": "仍有任务在运行", "warnings": []}]}))
        page.route('**/api/production/orders/batch', remove)
        page.evaluate("""() => {window.__deletedResult=null;void CaseProduction.deleteContentOrders([
            {id:'PRD-000000000001',kind:'video'},{id:'CAR-000000000002',kind:'carousel'},
            {id:'PRD-000000000001',kind:'video'}],{onComplete:result => {window.__deletedResult=result;}});} """)
        dialog = page.locator('.production-dialog')
        expect(dialog).to_contain_text('所选 2 张制作单')
        expect(dialog).to_contain_text('仍有任务在运行的制作单会被跳过并说明原因')
        dialog.locator('[name="sure"]').check()
        dialog.locator('[data-submit]').click()
        page.wait_for_function('window.__deletedResult!==null')
        expect(dialog).to_have_count(0)
        self.assertEqual(sent[0]['items'], [{'id': 'PRD-000000000001', 'kind': 'job'}, {'id': 'CAR-000000000002', 'kind': 'carousel'}])
        self.assertEqual(page.evaluate('window.__deletedResult.failed'), 1)
        expect(page.locator('#toast')).to_contain_text('仍有任务在运行')
        self.assert_workspace_retained(page)


if __name__ == '__main__':
    unittest.main()
