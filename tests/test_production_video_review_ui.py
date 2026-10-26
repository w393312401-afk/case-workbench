"""Inline review, truthful workflow stages and timeline associations in isolated browser fixtures."""
from __future__ import annotations

import copy
import json
import unittest

import test_production_video_visuals_ui as visuals
import test_production_validation_ui as fixture


class ProductionVideoReviewBrowserTests(unittest.TestCase):
    setUpClass = classmethod(visuals.ProductionVideoVisualsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(visuals.ProductionVideoVisualsBrowserTests.tearDownClass.__func__)
    setUp = visuals.ProductionVideoVisualsBrowserTests.setUp
    stop_server = visuals.ProductionVideoVisualsBrowserTests.stop_server
    request = visuals.ProductionVideoVisualsBrowserTests.request
    make_playable_video = visuals.ProductionVideoVisualsBrowserTests.make_playable_video
    open_page = visuals.ProductionVideoVisualsBrowserTests.open_page
    edit = visuals.ProductionVideoVisualsBrowserTests.edit
    take = visuals.ProductionVideoVisualsBrowserTests.take
    audio = visuals.ProductionVideoVisualsBrowserTests.audio
    reviewable = fixture.ProductionValidationBrowserTests.reviewable
    skill_delivery = fixture.ProductionValidationBrowserTests.skill_delivery

    def test_inline_review_requires_every_check_and_preserves_audio_and_forms_after_approval(self):
        edit = self.skill_delivery()
        self.payload["job"]["takes"] = [self.take("TAK-0123456789ab", [{"number": 1, "start": 0, "end": 1, "coverage": 1}])]
        self.payload["job"]["status"] = "awaiting_review"

        def approve(route):
            self.assertTrue(route.request.url.endswith("/approval"))
            edit["approval"] = {"at": "2026-10-14T10:00:00", "note": route.request.post_data_json["note"]}
            self.payload["job"]["status"] = "approved"
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"approved": True, "edit": edit}))

        page = self.open_page(width=390, post=approve)
        form = page.locator(".edit-review-form")
        submit = form.get_by_role("button", name="确认完成", exact=True)
        self.assertTrue(submit.is_disabled())
        submit.evaluate("button => button.click()")
        self.assertEqual(self.calls, [])
        boxes = form.locator('input[name="reviewed"]')
        self.assertEqual(boxes.count(), 3)
        boxes.nth(0).check();boxes.nth(1).check()
        self.assertTrue(submit.is_disabled())
        boxes.nth(2).check()
        self.assertTrue(submit.is_enabled())
        note = '<img src=x onerror="window.__badReview=true"> 人工已核对'
        form.locator("textarea").fill(note)
        page.evaluate("window.__voiceNode=document.querySelector('#take-panel audio');window.__takeForm=document.querySelector('#take-form');window.__editForm=document.querySelector('#edit-form')")
        submit.click()
        page.locator('[data-revoke-edit="EDT-0123456789ab"]').wait_for()
        self.assertEqual(page.locator("[data-complete-job],.edit-review-form,dialog.production-dialog[open]").count(), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.calls[0]["body"]["reviewed"]), 3)
        self.assertEqual(self.calls[0]["body"]["note"], note)
        self.assertTrue(page.evaluate("window.__voiceNode===document.querySelector('#take-panel audio')&&window.__takeForm===document.querySelector('#take-form')&&window.__editForm===document.querySelector('#edit-form')"))
        self.assertFalse(page.evaluate("Boolean(window.__badReview)"))
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_review_failure_restores_all_checks_and_input_for_retry(self):
        self.skill_delivery()
        page = self.open_page(post=lambda route: route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "隔离测试：稍后重试"}, ensure_ascii=False)))
        form = page.locator(".edit-review-form")
        for box in form.locator('input[name="reviewed"]').all():
            box.check()
        form.locator("textarea").fill("保留我的备注")
        form.get_by_role("button", name="确认完成", exact=True).click()
        form.locator("[data-error]").get_by_text("隔离测试：稍后重试", exact=True).wait_for()
        self.assertTrue(form.get_by_role("button", name="确认完成", exact=True).is_enabled())
        self.assertTrue(all(box.is_checked() and box.is_enabled() for box in form.locator('input[name="reviewed"]').all()))
        self.assertEqual(form.locator("textarea").input_value(), "保留我的备注")

    def test_approved_ai_completion_revokes_without_showing_a_second_completion_path(self):
        edit = self.skill_delivery()
        edit["approval"] = {"at": "2026-10-14T10:00:00", "note": "已核对"}
        self.payload["job"]["status"] = "approved"

        def revoke(route):
            self.assertEqual(route.request.post_data_json["approved"], False)
            edit.pop("approval")
            self.payload["job"]["status"] = "awaiting_review"
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"approved": False, "edit": edit}))

        page = self.open_page(post=revoke)
        self.assertEqual(page.locator("[data-complete-job],[data-revoke-job-completion]").count(), 0)
        page.locator('[data-revoke-edit="EDT-0123456789ab"]').click()
        page.locator(".edit-review-form").wait_for()
        self.assertTrue(page.locator("[data-approve-edit]").is_disabled())
        self.assertEqual(page.locator("[data-complete-job],[data-revoke-edit]").count(), 0)

    def test_archived_approval_checklist_and_completion_are_read_only(self):
        edit = self.skill_delivery()
        self.payload["job"]["archived"] = True
        page = self.open_page()
        self.assertTrue(page.locator("[data-approve-edit]").is_disabled())
        self.assertTrue(all(box.is_disabled() for box in page.locator('.edit-review-form input[type="checkbox"]').all()))
        self.assertEqual(self.calls, [])

    def test_manual_completion_keeps_old_ai_runs_in_folded_history(self):
        self.reviewable()
        self.payload["job"].update(status="approved", manual_completion={"at": "2026-10-14T10:00:00", "note": "人工完成",
            "take_id": "TAK-0123456789ab", "timeline_uid": "uid-own", "timeline_name": "Lab261011-202-1"})
        page = self.open_page()
        self.assertTrue(page.locator("[data-revoke-job-completion]").is_visible())
        self.assertFalse(page.locator(".edit-completed-history").evaluate("node => node.open"))
        self.assertFalse(page.locator("[data-edit-id]").is_visible())
        self.assertEqual(page.locator("[data-complete-job],[data-approve-edit]").count(), 0)

    def test_timeline_cards_show_voice_version_latest_edit_and_current_timeline(self):
        self.reviewable()
        self.payload["job"]["takes"] = [self.take("TAK-old", [], 4), self.take("TAK-0123456789ab", [], 2)]
        old = copy.deepcopy(self.payload["job"]["timelines"][0]);old.update(unique_id="uid-old", name="旧时间线", take_id="TAK-old")
        self.payload["job"]["timelines"].insert(0, old)
        self.payload["job"]["edits"].insert(0, {**self.edit("approved", "EDT-old"), "timeline_uid": "uid-old", "approval": {"at": "2026-10-13"}})
        page = self.open_page()
        page.locator('.production-steps [data-step="timeline"]').click()
        active = page.locator('[data-timeline-id="uid-own"]')
        self.assertIn("current", active.get_attribute("class"))
        active.get_by_text("当前生效", exact=True).wait_for()
        active.get_by_text("配音第 2 版（当前选用）", exact=True).wait_for()
        active.get_by_text("AI 剪辑：待人工审查", exact=True).wait_for()
        old_card = page.locator('[data-timeline-id="uid-old"]')
        self.assertEqual(old_card.locator('[data-timeline-task]').input_value(), 'completed')
        old_card.locator('[data-timeline-ai-state]').get_by_text("AI 剪辑：已完成", exact=True).wait_for()
        active.get_by_role("button", name="在达芬奇打开", exact=True).click()
        self.assertEqual(self.calls[0]["body"], {"unique_id": "uid-own"})

    def test_running_edit_stages_use_reported_counts_without_inventing_completed_steps(self):
        self.reviewable()
        edit = self.payload["job"]["edits"][0]
        edit.update(status="running", execution_status="running", live={"stage": "grid", "progress": "导出网格",
            "stages": {"materials": {"status": "completed"}, "grid": {"status": "running"}},
            "grid": {"attempted": 4, "total": 10, "exported": 3, "deferred": 0}})
        page = self.open_page(width=390)
        strip = page.locator(".edit-stage-strip")
        self.assertEqual(strip.locator("li").count(), 5)
        self.assertEqual(strip.locator(".completed").count(), 1)
        self.assertEqual(strip.locator(".running").count(), 1)
        self.assertEqual(strip.locator(".pending").count(), 3)
        self.assertIn("3 / 10", page.locator(".edit-grid-count").inner_text())
        self.assertIn("已处理 4", page.locator(".edit-grid-count").inner_text())
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)


if __name__ == "__main__":
    unittest.main()
