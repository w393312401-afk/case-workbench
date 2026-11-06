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

    def test_same_job_other_timeline_can_submit_and_a_running_target_can_only_be_recut(self):
        self.add_running_edit()
        self.add_other_timeline()
        page = self.open_page()
        form = page.locator("#edit-panel #edit-form")
        target = form.locator('[name="timeline"]')
        submit = form.locator('button[type="submit"]')
        recut = form.locator('[name="recut"]')
        page.locator('.production-focus [data-timeline-focus="uid-own"]').click()
        expect(target).to_have_value("uid-own")
        expect(submit).to_have_text("另建时间线并开始剪辑")
        expect(submit).to_be_enabled()
        expect(recut).to_be_checked()
        expect(recut).to_be_disabled()
        expect(form.locator("[data-edit-provider-status]")).not_to_contain_text("当前目标时间线已有剪辑任务")
        page.locator('.production-focus [data-timeline-focus="uid-other"]').click()
        expect(target).to_have_value("uid-other")
        expect(submit).to_have_text("开始 AI 剪辑")
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
        # With two timelines the target is picked from the tabs, not the select.
        page.locator('.production-focus [data-timeline-focus="uid-own"]').click()
        expect(form.locator('[name="timeline"]')).to_have_value("uid-own")
        submit = form.locator('button[type="submit"]')
        expect(submit).to_have_text("另建时间线并开始剪辑")
        edit.update(status="cancelled", execution_status="cancelled", finished_at="2026-10-13T16:30:00")
        expect(submit).to_have_text("开始 AI 剪辑", timeout=10000)
        expect(submit).to_be_enabled()
        page.locator('.production-focus [data-timeline-focus="uid-other"]').click()
        expect(submit).to_have_text("另建时间线并开始剪辑")
        expect(form.locator('[name="recut"]')).to_be_disabled()
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

    def test_finished_edit_shows_what_it_took_from_the_quota(self):
        edit = fixture.ProductionValidationBrowserTests.edit(self, "awaiting_review")
        part = {"runs": 1, "turns": 40, "input_tokens": 1000, "cache_read_input_tokens": 2500000,
                "cache_creation_input_tokens": 99000, "output_tokens": 30000, "cost_usd": 3.2}
        planning = {**part, "runs": 3, "turns": 30, "cache_read_input_tokens": 600000, "cost_usd": 1}
        edit["ai_usage"] = {"editor": part, "planning": planning, "total": {**part, "cost_usd": 4.2}}
        self.payload["job"]["edits"] = [edit]
        page = self.open_page()
        note = page.locator(f'[data-edit-id="{edit["id"]}"] .edit-usage')
        expect(note).to_contain_text("主剪辑 40 轮，读入 260 万（缓存 250 万），输出 3.0 万")
        expect(note).to_contain_text("选材 3 次 30 轮")
        expect(note).to_contain_text("$4.20")

    def test_busy_resolve_never_offers_another_creators_materials_for_a_project_without_any(self):
        # Built in a project with no material timelines: the product's last list belongs to another creator's project.
        self.payload["job"]["timelines"][0].update(project="Zack-202H", materials=[])
        page = self.open_page()
        page.route("**/api/production/resolve", lambda route: route.fulfill(status=200,
            content_type="application/json", body=json.dumps({"ok": False, "busy": True,
                "error": "达芬奇正在执行另一项工作台操作"})))
        page.reload()
        page.locator('.production-steps [data-step="edit"]').click()
        form = page.locator("#edit-panel #edit-form")
        expect(form.locator('[name="material_text"]')).to_have_value("")
        expect(form.locator("[data-materials]")).to_contain_text("「Zack-202H」")
        form.get_by_role("button", name="开始 AI 剪辑", exact=True).click()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
