"""Direct human completion of video orders, with isolated browser/API fixtures."""
from __future__ import annotations

import copy
import json
import unittest

from test_production_validation_ui import JOB_ID, ProductionValidationBrowserTests
from test_production_queue_ui import content_response
from test_content_flow_ui import set_content_condition


TAKE_ID = "TAK-0123456789ab"


class ProductionManualCompletionBrowserTests(ProductionValidationBrowserTests):
    def setUp(self):
        super().setUp()
        self.payload["job"].update(
            selected_take=TAKE_ID,
            takes=[self.take(TAKE_ID, [{"number": 1, "start": 0, "end": 1.5, "coverage": 1}])],
            timelines=[self.timeline("uid-own", "Lab261011-202-1")],
        )

    def timeline(self, uid, name, take_id=TAKE_ID):
        return {"unique_id": uid, "name": name, "creator": "Lab", "markers": 1,
                "frames": 60, "fps": "29.97", "take_id": take_id, "built_at": "2026-10-11"}

    def receipt(self, *, uid="uid-own", name="Lab261011-202-1", note=""):
        return {"at": "2026-10-13T10:30:00+08:00", "note": note, "timeline_uid": uid,
                "timeline_name": name, "take_id": TAKE_ID, "basis": "human_review"}

    def completion_route(self, route):
        self.assertTrue(route.request.url.endswith(f"/jobs/{JOB_ID}/completion"))
        body = route.request.post_data_json
        job = self.payload["job"]
        if body["completed"]:
            timeline = next(item for item in job["timelines"] if item["unique_id"] == body["timeline_uid"])
            job["manual_completion"] = self.receipt(
                uid=timeline["unique_id"], name=timeline["name"], note=body.get("note", ""))
            job["status"] = "approved"
        else:
            job.pop("manual_completion", None)
            job["status"] = "timeline"
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"job": job, "completed": body["completed"], "manual_completion": job.get("manual_completion")},
            ensure_ascii=False))

    def open_edit_step(self, **kwargs):
        page = self.open_page(**kwargs)
        page.locator('.production-steps [data-step="edit"]').click()
        page.locator("#edit-panel").wait_for()
        return page

    def assert_completed(self, page):
        page.locator("[data-revoke-job-completion]").wait_for()
        step = page.locator('.production-steps [data-step="edit"]')
        self.assertIn("done", step.get_attribute("class").split())
        step.get_by_text("人工完成", exact=True).wait_for()
        page.locator(".page-heading .script-status").get_by_text("已人工完成", exact=True).wait_for()
        self.assertEqual(page.locator("[data-edit-id]").count(), 0)
        self.assertEqual(page.locator("dialog.production-dialog[open]").count(), 0)

    def test_direct_mark_without_ai_edit_uses_latest_selected_take_timeline_and_survives_reload(self):
        self.payload["job"]["timelines"] += [
            self.timeline("uid-selected-new", "Lab261011-202-2"),
            self.timeline("uid-unselected-newer", "Lab261011-202-3", "TAK-other"),
        ]
        page = self.open_edit_step(width=390, post=self.completion_route)
        button = page.locator("[data-complete-job]")
        self.assertEqual(button.inner_text(), "手动标记成功")
        self.assertTrue(button.is_enabled())
        button.click()
        self.assert_completed(page)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["body"]["completed"], True)
        self.assertEqual(self.calls[0]["body"]["timeline_uid"], "uid-selected-new")
        self.assertEqual(self.payload["job"]["edits"], [])
        page.reload()
        self.assert_completed(page)
        self.assertEqual(len(self.calls), 1)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_optional_note_is_literal_text_and_completion_can_be_revoked(self):
        note = '<img src=x onerror="window.__completionNoteExecuted = true"> 已人工剪好。'
        page = self.open_edit_step(width=390, post=self.completion_route)
        page.locator(".production-completion-details > summary").click()
        page.locator('textarea[name="completion_note"]').fill(note)
        page.locator("[data-complete-job]").click()
        self.assert_completed(page)
        page.locator("#edit-panel").get_by_text(note, exact=True).wait_for()
        self.assertEqual(page.locator("#edit-panel img").count(), 0)
        self.assertFalse(page.evaluate("Boolean(window.__completionNoteExecuted)"))
        self.assertEqual(self.calls[0]["body"]["note"], note)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
        page.locator("[data-revoke-job-completion]").click()
        page.locator("[data-complete-job]").wait_for()
        self.assertTrue(page.locator("[data-complete-job]").is_enabled())
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1]["body"]["completed"], False)
        self.assertEqual(self.calls[1]["body"]["timeline_uid"], "uid-own")
        self.assertEqual(self.calls[1]["body"]["completion_at"], "2026-10-13T10:30:00+08:00")
        self.assertNotIn("manual_completion", self.payload["job"])
        self.assertNotIn("done", page.locator('.production-steps [data-step="edit"]').get_attribute("class").split())
        page.reload()
        page.locator("[data-complete-job]").wait_for()
        self.assertEqual(page.locator("[data-revoke-job-completion]").count(), 0)
        self.assertEqual(self.calls[1]["url"], self.calls[0]["url"])

    def test_no_current_built_timeline_or_running_edit_disables_manual_completion(self):
        original = copy.deepcopy(self.payload["job"])
        blocked = "剪辑记录尚未结束登记，请先结束剪辑或恢复结果，再修改成功标记。"
        cases = {
            "no_timeline": {"timelines": []},
            "unselected_timeline": {"selected_take": "TAK-other"},
            "no_selected_take": {"selected_take": None},
            "missing_timeline_identity": {"timelines": [self.timeline("", "未登记时间线")]},
            "unsaved_timeline": {"timelines": [{**self.timeline("uid-own", "Lab261011-202-1"), "saved": False}]},
            "archived_job": {"archived": True},
            "running_edit": {"edits": [{**self.edit("running"), "execution_status": "running"}]},
            "interrupted_public_worker": {"manual_completion_blocked": blocked,
                "edits": [self.edit("interrupted")]},
            "pending_skill_delivery": {"manual_completion_blocked": blocked,
                "edits": [{**self.edit("needs_repair"), "completion_policy": "skill_delivery",
                           "result_pending": True, "validation": None}]},
            "pending_verification": {"manual_completion_blocked": blocked,
                "edits": [{**self.edit("verification_failed"), "result_pending": True}]},
        }
        for name, changes in cases.items():
            with self.subTest(name=name):
                self.payload["job"] = {**copy.deepcopy(original), **changes}
                page = self.open_edit_step()
                button = page.locator("[data-complete-job]")
                self.assertTrue(button.is_disabled())
                if changes.get("manual_completion_blocked"):
                    page.locator("#job-completion-form").get_by_text(blocked, exact=True).wait_for()
                button.evaluate("node => node.click()")
                self.assertEqual(self.calls, [])

    def test_queue_recognizes_manual_completion_without_an_edit_or_delivery_row(self):
        job = self.payload["job"]
        job.update(status="approved", manual_completion=self.receipt())
        draft = {**copy.deepcopy(job), "id": "PRD-999999999999", "title": "未完成制作单", "status": "timeline"}
        draft.pop("manual_completion")
        page = self.open_page(width=390)
        page.route('**/api/content?*', lambda route: route.fulfill(status=200, content_type='application/json',
            body=json.dumps(content_response(self.server.app, [job, draft], [], route.request.url))))
        page.goto(f'http://127.0.0.1:{self.server.server_port}/#/202H/content?view=all')
        card = page.locator(f'[data-content-id="video:{JOB_ID}"]')
        card.get_by_text('待发布', exact=True).wait_for()
        self.assertEqual(page.locator('.flow-row').count(), 2)
        set_content_condition(page, 'status', ['approved'])
        self.assertEqual(page.locator('.flow-filter-chip').inner_text(), '状态：已完成待发布')
        page.locator('#content-flow-status').wait_for(state='hidden')
        self.assertEqual(page.locator('.flow-row').count(), 1)
        self.assertEqual(card.count(), 1)
        set_content_condition(page, 'status', ['in_progress'])
        page.locator('#content-flow-status').wait_for(state='hidden')
        self.assertEqual(card.count(), 0)
        self.assertEqual(page.locator('[data-content-id="video:PRD-999999999999"]').count(), 1)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_header_matches_the_publish_step_once_the_current_version_is_registered(self):
        self.payload["job"].update(status="approved", manual_completion=self.receipt())
        page = self.open_page()
        header, next_state = page.locator(".page-heading .script-status"), page.locator("[data-next-state]")
        publish_note = page.locator('.production-steps [data-step="publish"] small')
        header.get_by_text("已人工完成", exact=True).wait_for()
        publication = {"id": "VPU-111111111111", "post_id": "7697632860966030623", "published_on": "2026-10-17",
                       "account": "fixture", "script_version": 1, "registered_at": "2026-10-19T05:27:48Z", "source": {},
                       "source_current": True, "calendar_uploaded": False}
        detail = {"revision": "r1", "source_signature": "s1", "can_register": True, "publication": publication, "history": []}
        page.route(f"**/api/production/jobs/{JOB_ID}/publication", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(detail, ensure_ascii=False)))
        page.reload()
        header.get_by_text("已登记发布", exact=True).wait_for()
        self.assertEqual((next_state.inner_text(), publish_note.inner_text()), ("已登记发布", "已登记发布"))
        # A registration of an earlier version leaves the header on the review state, like the step.
        publication["source_current"] = False
        page.reload()
        page.locator('.production-steps [data-step="publish"] small', has_text="登记版本需核对").wait_for()
        self.assertEqual(header.inner_text(), "已人工完成")
        # So is a registration whose bound film is missing: header, step and next action all ask for a check.
        publication.update(source_current=True, file={"name": "final.mp4", "size": 10}, file_available=False)
        page.reload()
        page.locator('[data-next-action]', has_text="核对发布登记与当前版本").wait_for(state="attached")
        self.assertEqual((header.inner_text(), publish_note.inner_text()), ("已人工完成", "登记版本需核对"))


# Use the browser fixture without repeating its existing regression suite.
for _name in [name for name in vars(ProductionValidationBrowserTests) if name.startswith("test_")]:
    setattr(ProductionManualCompletionBrowserTests, _name, None)
del ProductionValidationBrowserTests, _name


if __name__ == "__main__":
    unittest.main()
