"""Concurrent edit submission stays available while individual timelines remain protected."""
from __future__ import annotations

import json
import unittest

from playwright.sync_api import expect

import test_production_edit_defaults_ui as defaults
import test_production_validation_ui as fixture


class ProductionEditConcurrencyBrowserTests(unittest.TestCase):
    setUpClass = classmethod(defaults.ProductionEditDefaultsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(defaults.ProductionEditDefaultsBrowserTests.tearDownClass.__func__)
    stop_server = defaults.ProductionEditDefaultsBrowserTests.stop_server
    request = defaults.ProductionEditDefaultsBrowserTests.request
    make_playable_video = defaults.ProductionEditDefaultsBrowserTests.make_playable_video
    open_page = defaults.ProductionEditDefaultsBrowserTests.open_page
    ai_options_route = defaults.ProductionEditDefaultsBrowserTests.ai_options_route
    edit_clients_route = defaults.ProductionEditDefaultsBrowserTests.edit_clients_route

    def setUp(self):
        defaults.ProductionEditDefaultsBrowserTests.setUp(self)
        self.clients["busy"] = True
        self.payload["product_settings"]["material_timelines"] = ["蓝点折刀成品素材"]

    def add_running_edit(self, timeline="uid-own", identifier="EDT-0123456789ab"):
        edit = fixture.ProductionValidationBrowserTests.edit(self, "running", identifier, validated=False)
        edit.update(timeline_uid=timeline, execution_status="running", finished_at="",
                    live={"phase": "waiting_resolve", "progress": "等待操作达芬奇", "latest": ""})
        self.payload["job"]["edits"].append(edit)
        return edit

    def add_other_timeline(self):
        self.payload["job"]["timelines"].append({**self.payload["job"]["timelines"][0],
            "unique_id": "uid-other", "name": "Lab261011-202-2"})
        self.timelines.append("Lab261011-202-2")

    def test_busy_client_can_submit_and_explains_waiting_and_manual_review(self):
        page = self.open_page()
        form = page.locator("#edit-panel #edit-form")
        expect(form.locator("[data-edit-provider-status]")).to_contain_text("可同时准备多条剪辑")
        expect(form.locator("[data-edit-provider-status]")).to_contain_text("需要操作达芬奇时会自动等待")
        expect(form.locator("[data-edit-usage]")).to_contain_text("默认不做剪后工程核验")
        expect(form.locator("[data-edit-usage]")).not_to_contain_text("工作台会自己读回时间线")
        form.get_by_role("button", name="开始 AI 剪辑", exact=True).click()
        expect(page.locator("#toast")).to_contain_text("已加入剪辑任务")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["body"]["timeline"], "uid-own")

    def test_same_job_other_timeline_can_submit_but_running_target_cannot(self):
        self.add_running_edit()
        self.add_other_timeline()
        page = self.open_page()
        form = page.locator("#edit-panel #edit-form")
        target = form.locator('[name="timeline"]')
        submit = form.get_by_role("button", name="开始 AI 剪辑", exact=True)
        target.select_option("uid-own")
        expect(submit).to_be_disabled()
        expect(form.locator("[data-edit-provider-status]")).to_contain_text("当前目标时间线已有剪辑任务")
        form.evaluate("form => form.dispatchEvent(new Event('submit', {cancelable:true}))")
        expect(form.locator("[data-error]")).to_contain_text("当前目标时间线已有剪辑任务")
        self.assertEqual(self.calls, [])
        target.select_option("uid-other")
        expect(submit).to_be_enabled()
        submit.click()
        expect(page.locator("#toast")).to_contain_text("已加入剪辑任务")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["body"]["timeline"], "uid-other")

    def test_poll_releases_finished_target_while_other_timeline_keeps_running(self):
        edit = self.add_running_edit()
        self.add_other_timeline()
        self.add_running_edit("uid-other", "EDT-111111111111")
        page = self.open_page()
        form = page.locator("#edit-panel #edit-form")
        form.locator('[name="timeline"]').select_option("uid-own")
        submit = form.get_by_role("button", name="开始 AI 剪辑", exact=True)
        expect(submit).to_be_disabled()
        edit.update(status="cancelled", execution_status="cancelled", finished_at="2026-10-13T16:30:00")
        expect(submit).to_be_enabled(timeout=10000)
        form.locator('[name="timeline"]').select_option("uid-other")
        expect(submit).to_be_disabled()
        self.assertEqual(self.calls, [])

    def test_busy_resolve_uses_saved_material_names_and_allows_submission(self):
        page = self.open_page()
        page.route("**/api/production/resolve", lambda route: route.fulfill(status=200,
            content_type="application/json", body=json.dumps({"ok": False, "busy": True,
                "error": "达芬奇正在执行另一项工作台操作"})))
        page.reload()
        page.locator('.production-steps [data-step="edit"]').click()
        form = page.locator("#edit-panel #edit-form")
        expect(form.locator('[name="material_text"]')).to_have_value("蓝点折刀成品素材")
        form.get_by_role("button", name="开始 AI 剪辑", exact=True).click()
        expect(page.locator("#toast")).to_contain_text("已加入剪辑任务")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["body"]["materials"], ["蓝点折刀成品素材"])


if __name__ == "__main__":
    unittest.main()
