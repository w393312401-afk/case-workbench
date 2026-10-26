"""Queued timeline builds accept an edit request without losing its exact target or drafts."""
from __future__ import annotations

import copy
import json
import unittest

from playwright.sync_api import expect

import test_production_edit_defaults_ui as defaults
import test_production_validation_ui as fixture


BUILD_REQUEST = "queued-build-request-0123456789ab"
TAKE_ID = "TAK-0123456789ab"


class ProductionPendingEditBrowserTests(unittest.TestCase):
    setUpClass = classmethod(defaults.ProductionEditDefaultsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(defaults.ProductionEditDefaultsBrowserTests.tearDownClass.__func__)
    stop_server = defaults.ProductionEditDefaultsBrowserTests.stop_server
    request = defaults.ProductionEditDefaultsBrowserTests.request
    make_playable_video = defaults.ProductionEditDefaultsBrowserTests.make_playable_video
    ai_options_route = defaults.ProductionEditDefaultsBrowserTests.ai_options_route
    edit_clients_route = defaults.ProductionEditDefaultsBrowserTests.edit_clients_route

    def setUp(self):
        defaults.ProductionEditDefaultsBrowserTests.setUp(self)
        take = fixture.ProductionValidationBrowserTests.take(
            self, TAKE_ID, [{"number": 1, "start": 0, "end": 1.5, "coverage": 1}])
        self.payload["job"].update(
            status="voiced", takes=[take], selected_take=TAKE_ID, timelines=[],
            timeline_building={"request_id": BUILD_REQUEST, "creator": "Lab", "stage": "waiting",
                "since": "2026-10-11T10:00:00", "take_id": TAKE_ID, "name": "Lab261011-202-序号",
                "group_markers": True, "materials": ["Lab成品素材"]})
        self.payload["product_settings"]["material_timelines"] = ["Lab成品素材"]
        self.timelines = ["Lab成品素材"]

    def open_page(self, *, post=None, busy=False, width=1080):
        page = defaults.ProductionEditDefaultsBrowserTests.open_page(self, post=post, width=width)
        if busy:
            page.route("**/api/production/resolve", lambda route: route.fulfill(status=200,
                content_type="application/json", body=json.dumps({"ok": False, "busy": True,
                    "error": "达芬奇正在执行另一项工作台操作"}, ensure_ascii=False)))
            page.reload()
            page.locator('.production-steps [data-step="edit"]').click()
        return page

    def form(self, page):
        form = page.locator("#edit-panel #edit-form")
        expect(form).to_be_visible()
        return form

    def test_first_queued_timeline_accepts_exact_build_edit_request_without_handoff(self):
        page = fixture.ProductionValidationBrowserTests.open_page(self, width=390)
        page.locator('.production-steps [data-step="timeline"]').click()
        prepare = page.locator("[data-prepare-pending-edit]")
        expect(prepare).to_be_visible()
        prepare.click()
        expect(page.locator('.production-steps [aria-selected="true"]')).to_have_attribute("data-step", "edit")
        form = self.form(page)
        expect(form.locator('[name="timeline"]')).to_have_value("pending:" + BUILD_REQUEST)
        expect(form.locator('[name="timeline"] option:checked')).to_contain_text("Lab261011-202-序号")
        expect(form.locator('[data-copy-handoff]')).to_be_disabled()
        form.locator('[name="mode"]').select_option("sync")
        form.locator('[name="instruction"]').fill("开头保留切纸结果，随后跟随这版配音。")
        submit = form.locator('button[type="submit"]')
        expect(submit).to_be_enabled()
        submit.click()
        expect(page.locator("#toast")).to_contain_text("已提交剪辑")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0]["url"].endswith("/edit"))
        body = self.calls[0]["body"]
        self.assertTrue(body["request_id"])
        self.assertEqual({key: value for key, value in body.items() if key != "request_id"}, {
            "timeline_request_id": BUILD_REQUEST, "mode": "sync",
            "instruction": "开头保留切纸结果，随后跟随这版配音。", "materials": ["Lab成品素材"]})
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_busy_resolve_uses_pending_creators_saved_materials(self):
        # Another creator's product defaults must not replace the pending target's saved footage list.
        self.payload["product_settings"]["material_timelines"] = ["其他达人成品素材"]
        page = self.open_page(busy=True)
        form = self.form(page)
        expect(form.locator('[name="material"][value="Lab成品素材"]')).to_be_checked()
        self.assertEqual(form.locator('[name="material"]:checked').evaluate_all(
            "items => items.map(item => item.value)"), ["Lab成品素材"])
        form.locator('button[type="submit"]').click()
        expect(page.locator("#toast")).to_contain_text("已提交剪辑")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["body"]["materials"], ["Lab成品素材"])
        self.assertEqual(self.calls[0]["body"]["timeline_request_id"], BUILD_REQUEST)

    def test_pending_target_does_not_silently_use_an_older_timeline(self):
        self.payload["job"]["timelines"] = [{"unique_id": "uid-older", "name": "Lab261010-202-1",
            "creator": "Lab", "markers": 1, "frames": 60, "fps": "29.97",
            "take_id": "TAK-older", "built_at": "2026-10-10", "materials": ["Lab成品素材"]}]
        page = self.open_page()
        form = self.form(page)
        target = form.locator('[name="timeline"]')
        expect(target).to_have_value("pending:" + BUILD_REQUEST)
        expect(form.locator('[data-copy-handoff]')).to_be_disabled()
        target.select_option("uid-older")
        expect(form.locator('[data-copy-handoff]')).to_be_enabled()
        target.select_option("pending:" + BUILD_REQUEST)
        expect(form.locator('[data-copy-handoff]')).to_be_disabled()
        form.locator('button[type="submit"]').click()
        expect(page.locator("#toast")).to_contain_text("已提交剪辑")
        self.assertEqual(self.calls[0]["body"]["timeline_request_id"], BUILD_REQUEST)
        self.assertNotIn("timeline", self.calls[0]["body"])

    def test_cancelled_or_failed_build_keeps_pending_target_until_manual_reselection(self):
        initial = copy.deepcopy(self.payload)
        for cancelled in (True, False):
            for older in (True, False):
                with self.subTest(cancelled=cancelled, older_timeline=older):
                    self.payload = copy.deepcopy(initial)
                    self.calls.clear()
                    if older:
                        self.payload["job"]["timelines"] = [{"unique_id": "uid-older",
                            "name": "Lab261010-202-1", "creator": "Lab", "markers": 1,
                            "frames": 60, "fps": "29.97", "take_id": "TAK-older",
                            "built_at": "2026-10-10", "materials": ["Lab成品素材"]}]
                    page = self.open_page()
                    try:
                        form = self.form(page)
                        target = form.locator('[name="timeline"]')
                        expect(target).to_have_value("pending:" + BUILD_REQUEST)
                        form.locator('[name="instruction"]').fill("这段要求只给原本排队的新时间线")
                        form.locator('[name="mode"]').select_option("sync")
                        self.payload["job"].update(timeline_building=None, timeline_failure={
                            "request_id": BUILD_REQUEST, "cancelled": cancelled,
                            "error": "已取消建线" if cancelled else "原建线失败，未生成时间线",
                            "at": "2026-10-11T10:01:00"})
                        expect(page.locator("[data-timeline-busy]")).to_have_count(0, timeout=7000)
                        expect(page.locator('.production-steps [aria-selected="true"]')).to_have_attribute(
                            "data-step", "edit")
                        expect(target).to_have_value("pending:" + BUILD_REQUEST)
                        expect(form.locator('[name="instruction"]')).to_have_value("这段要求只给原本排队的新时间线")
                        expect(form.locator('[name="mode"]')).to_have_value("sync")
                        expect(form.locator('[data-pending-edit-note]')).to_be_visible()
                        expect(form.locator('[data-pending-edit-note]')).to_contain_text("重新建线")
                        expect(form.locator('button[type="submit"]')).to_be_disabled()
                        expect(form.locator('[data-copy-handoff]')).to_be_disabled()
                        form.evaluate("form => form.dispatchEvent(new Event('submit', {cancelable:true}))")
                        self.assertEqual(self.calls, [])
                        if older:
                            target.select_option("uid-older")
                            expect(form.locator('button[type="submit"]')).to_be_enabled()
                            expect(form.locator('[data-copy-handoff]')).to_be_enabled()
                            expect(form.locator('[data-pending-edit-note]')).to_be_hidden()
                    finally:
                        page.close()

    def test_failed_pending_submission_keeps_edit_choices_and_explains_the_dependency(self):
        def cancelled(route):
            route.fulfill(status=409, content_type="application/json", body=json.dumps(
                {"error": "这次建线已取消，未启动剪辑。请重新建立时间线后再提交。"}, ensure_ascii=False))

        page = self.open_page(post=cancelled)
        form = self.form(page)
        form.locator('[name="instruction"]').fill("保留我尚未提交成功的要求")
        form.locator('[name="mode"]').select_option("montage")
        form.locator('button[type="submit"]').click()
        expect(form.locator("[data-error]")).to_contain_text("这次建线已取消，未启动剪辑")
        expect(form.locator('[name="instruction"]')).to_have_value("保留我尚未提交成功的要求")
        expect(form.locator('[name="mode"]')).to_have_value("montage")
        expect(form.locator('[name="timeline"]')).to_have_value("pending:" + BUILD_REQUEST)
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        self.assertEqual(len(self.calls), 1)

    def test_build_poll_preserves_edit_step_drafts_and_retargets_the_exact_finished_build(self):
        page = self.open_page()
        form = self.form(page)
        form.locator('[name="instruction"]').fill("轮到我时仍要保留这段要求")
        form.locator('[name="mode"]').select_option("sync")
        self.payload["job"]["timeline_building"]["stage"] = "building"
        expect(page.locator("[data-timeline-busy]")).to_contain_text("正在达芬奇里建时间线", timeout=7000)
        expect(page.locator('.production-steps [aria-selected="true"]')).to_have_attribute("data-step", "edit")
        expect(form.locator('[name="instruction"]')).to_have_value("轮到我时仍要保留这段要求")
        expect(form.locator('[name="mode"]')).to_have_value("sync")
        self.payload["job"]["timeline_building"] = None
        self.payload["job"]["status"] = "timeline"
        self.payload["job"]["timelines"] = [{"unique_id": "uid-new", "name": "Lab261011-202-2",
            "creator": "Lab", "markers": 1, "frames": 60, "fps": "29.97", "take_id": TAKE_ID,
            "built_at": "2026-10-11", "build_request_id": BUILD_REQUEST, "materials": ["Lab成品素材"]},
            {"unique_id": "uid-unrelated", "name": "Lab261011-202-3", "creator": "Lab", "markers": 1,
             "frames": 60, "fps": "29.97", "take_id": TAKE_ID, "built_at": "2026-10-11",
             "build_request_id": "unrelated-build-request", "materials": ["Lab成品素材"]}]
        expect(form.locator('[name="timeline"]')).to_have_value("uid-new", timeout=7000)
        expect(page.locator('.production-steps [aria-selected="true"]')).to_have_attribute("data-step", "edit")
        expect(form.locator('[name="instruction"]')).to_have_value("轮到我时仍要保留这段要求")
        expect(form.locator('[name="mode"]')).to_have_value("sync")
        expect(form.locator('[data-copy-handoff]')).to_be_enabled()
        self.assertEqual(self.calls, [])

    def test_pending_build_without_group_markers_does_not_offer_automatic_editing(self):
        self.payload["job"]["timeline_building"]["group_markers"] = False
        page = self.open_page()
        expect(page.locator("#edit-panel #edit-form")).to_have_count(0)
        expect(page.locator("[data-prepare-pending-edit]")).to_have_count(0)
        self.assertEqual(self.calls, [])

    def test_legacy_build_metadata_does_not_offer_an_unsupported_pending_edit(self):
        self.payload["job"]["timeline_building"] = {
            "request_id": BUILD_REQUEST, "creator": "Lab", "stage": "waiting"}
        self.payload["job"]["timelines"] = [{"unique_id": "uid-older", "name": "Lab261010-202-1",
            "creator": "Lab", "markers": 1, "frames": 60, "fps": "29.97",
            "take_id": "TAK-older", "built_at": "2026-10-10", "materials": ["Lab成品素材"]}]
        page = self.open_page()
        form = self.form(page)
        expect(form.locator('[name="timeline"]')).to_have_value("uid-older")
        expect(form.locator('option[value^="pending:"]')).to_have_count(0)
        expect(page.locator("[data-prepare-pending-edit]")).to_have_count(0)
        self.assertEqual(self.calls, [])

    def test_an_edit_already_waiting_for_this_build_blocks_duplicate_submission(self):
        edit = fixture.ProductionValidationBrowserTests.edit(self, "running", validated=False)
        edit.update(execution_status="running", timeline_uid="", timeline_name="Lab261011-202-序号",
            timeline_request_id=BUILD_REQUEST, take_id=TAKE_ID, waiting_timeline=True, finished_at="",
            live={"phase": "waiting_timeline", "progress": "等待指定时间线建立后自动开始剪辑", "latest": ""})
        self.payload["job"].update(status="editing", edits=[edit])
        page = self.open_page()
        form = self.form(page)
        expect(page.locator('[data-need-key="timeline"]')).to_have_count(0)
        expect(page.locator('[data-need-key="pending-edit"]')).to_have_count(0)
        expect(form.locator('button[type="submit"]')).to_be_disabled()
        form.evaluate("form => form.dispatchEvent(new Event('submit', {cancelable:true}))")
        expect(form.locator("[data-error]")).to_contain_text("已有剪辑任务")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
