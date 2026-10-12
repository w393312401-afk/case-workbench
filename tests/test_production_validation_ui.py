"""Production verification states and recovery controls in an isolated browser."""
from __future__ import annotations

import copy
import json
import unittest

import test_ui_regressions as browser


JOB_ID = "PRD-0123456789ab"


class ProductionValidationBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = browser.WorkbenchBrowserTests.stop_server
    request = browser.WorkbenchBrowserTests.request
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def setUp(self):
        browser.WorkbenchBrowserTests.setUp(self)
        self.payload = {"job": {"id": JOB_ID, "title": "核验状态测试", "product": "202H", "status": "timeline",
            "direction_id": "202H-S1", "updated_at": "2026-10-11", "lines": [{"number": 1, "spoken": "One kit.", "zh": "一套。"}],
            "takes": [], "selected_take": None, "timelines": [], "edits": [], "history": [], "problems": []},
            "voices": [{"voice_id": "VOICE123456789", "name": "Test voice", "model_id": "test-model"}],
            "models": [{"id": "test-model", "name": "Test model", "settings": []}], "default_model": "test-model",
            "defaults": {}, "tts": {"text": "One kit.", "characters": 8}, "creators": ["Lab"], "today": "261011",
            "product_settings": {"project": "202H", "fps": "29.97", "width": 1080, "height": 1920, "code": "202"}}
        self.calls = []

    def edit(self, state, identifier="EDT-0123456789ab", *, validated=True):
        return {"id": identifier, "status": state, "execution_status": "completed", "timeline_uid": "uid-own",
            "timeline_name": "Lab261011-202-1", "model": "claude-default", "mode": "explain",
            "started_at": "2026-10-11T10:00:00", "finished_at": "2026-10-11T10:10:00",
            **({"validation": {"status": state, "checked_at": "2026-10-11T10:10:00", "issues": []}} if validated else {})}

    def take(self, identifier, groups, duration=2):
        return {"id": identifier, "voice_id": "VOICE123456789", "model_id": "test-model", "settings": {},
                "duration": duration, "created_at": "2026-10-11", "groups": groups, "alignment_source": "tts"}

    def open_page(self, width=1080, post=None):
        context = self.browser.new_context(viewport={"width": width, "height": 740})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))

        def job_route(route):
            request = route.request
            if request.method == "GET":
                route.fulfill(status=200, content_type="application/json", body=json.dumps(self.payload, ensure_ascii=False))
                return
            self.calls.append({"url": request.url, "body": request.post_data_json})
            if post:
                post(route)
            else:
                route.fulfill(status=200, content_type="application/json", body="{}")

        page.route(f"**/api/production/jobs/{JOB_ID}**", job_route)
        page.route("**/api/production/resolve", lambda route: route.fulfill(status=200, content_type="application/json",
            body=json.dumps({"ok": True, "project": "202H", "version": "test", "timelines": getattr(self, "timelines", ["素材时间线"])})))
        page.route("**/api/production/edit-clients", getattr(self, "edit_clients_route", lambda route: route.fulfill(status=200,
            content_type="application/json", body=json.dumps({"providers": {"claude": {"available": True},
                "codex": {"available": True}}, "busy": False}))))
        page.route("**/api/ai/options", getattr(self, "ai_options_route", lambda route: route.fulfill(status=200,
            content_type="application/json", body=json.dumps({"default_settings": {"model": "claude-default", "reasoning_effort": None},
            "models": [{"id": "claude-default", "name": "Claude 客户端默认", "available": True,
                        "reasoning_efforts": ["high", "xhigh", "max"]}], "skills": []}))))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#production/{JOB_ID}")
        page.get_by_role("heading", name="核验状态测试", exact=True).wait_for()
        return page

    def test_end_states_show_issues_and_legacy_completion_requires_verification(self):
        states = [("awaiting_review", "待人工审查"), ("needs_material", "待补素材"), ("needs_repair", "待修正"),
                  ("verification_failed", "核验未完成"), ("failed", "失败"), ("cancelled", "已停止"),
                  ("interrupted", "已中断"), ("completed", "旧记录待核验")]
        self.payload["job"]["edits"] = [self.edit(state, f"EDT-{index:012d}", validated=state != "completed")
                                        for index, (state, _) in enumerate(states)]
        self.payload["job"]["edits"][2]["validation"]["issues"] = [{"code": "voice_changed", "message": "配音轨内容和建线时不一致。"}]
        page = self.open_page(width=390)
        page.locator(".edit-older > summary").click()
        for index, (_, label) in enumerate(states):
            card = page.locator(f'[data-edit-id="EDT-{index:012d}"]')
            card.locator(".script-status").get_by_text(label, exact=True).wait_for()
            self.assertTrue(card.get_by_role("button", name="重新核验", exact=True).is_enabled())
        page.locator(".edit-validation-issues").get_by_text("配音轨内容和建线时不一致。", exact=True).wait_for()
        page.get_by_text("字幕、声音和导出由你处理。", exact=False).wait_for()
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_recheck_reads_existing_edit_without_starting_another_edit_or_voice(self):
        self.payload["job"]["edits"] = [self.edit("needs_repair")]

        def finish(route):
            self.assertTrue(route.request.url.endswith("/edits/EDT-0123456789ab/recheck"))
            self.payload["job"]["edits"][0]["status"] = "awaiting_review"
            self.payload["job"]["edits"][0]["validation"]["status"] = "awaiting_review"
            route.fulfill(status=200, content_type="application/json", body="{}")

        page = self.open_page(post=finish)
        page.get_by_role("button", name="重新核验", exact=True).click()
        page.locator("#edit-runs .script-status").get_by_text("待人工审查", exact=True).wait_for()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["body"], {})
        page.get_by_text("读回并保存现有工程", exact=False).wait_for()

    def test_low_coverage_invalid_missing_and_out_of_bounds_times_cannot_be_selected(self):
        good = {"number": 1, "start": 0, "end": 1, "coverage": .6}
        cases = {"low": [{**good, "coverage": .59}], "reverse": [{**good, "start": 1, "end": 0}],
                 "missing": [], "negative": [{**good, "start": -1}], "string": [{**good, "end": "1"}],
                 "outside": [{**good, "end": 3}], "coverage_over": [{**good, "coverage": 1.1}],
                 "bad_number": [{**good, "number": 2}], "good": [good]}
        self.payload["job"]["takes"] = [self.take(identifier, groups) for identifier, groups in cases.items()]
        page = self.open_page()
        for identifier in cases:
            button = page.locator(f'[data-select-take="{identifier}"]')
            self.assertEqual(button.is_enabled(), identifier == "good", identifier)

    def test_pending_voice_result_does_not_promise_that_unknown_generation_was_free(self):
        def fail(route):
            route.fulfill(status=500, content_type="application/json", body=json.dumps({"error": "隔离测试：保存中断"}, ensure_ascii=False))

        page = self.open_page(post=fail)
        page.locator('#take-form button[type="submit"]').click()
        error = page.locator("#take-form [data-error]")
        error.get_by_text("已取得的配音可接续恢复", exact=False).wait_for()
        self.assertIn("先核实 ElevenLabs 记录", error.inner_text())
        self.assertNotIn("不会重复扣额度", error.inner_text())

    def test_recovery_uses_original_request_and_keeps_unknown_voice_message(self):
        original_id = "original-request-12345"
        self.payload["job"]["recovery_operations"] = [
            {"request_id": original_id, "kind": "take", "state": "result_ready", "message": "配音已保存，等待登记。"},
            {"request_id": "unknown-request-12345", "kind": "take", "state": "uncertain", "uncertain": True,
             "can_retry_not_generated": True, "message": "配音结果不确定，请先核对服务商记录。"}]

        def recovered(route):
            self.assertTrue(route.request.url.endswith(f"/jobs/{JOB_ID}/recover"))
            self.payload["job"]["recovery_operations"] = self.payload["job"]["recovery_operations"][1:]
            route.fulfill(status=200, content_type="application/json", body="{}")

        page = self.open_page(width=390, post=recovered)
        self.assertEqual(page.locator('[data-confirm-no-voice="original-request-12345"]').count(), 0)
        page.get_by_role("button", name="核对并恢复", exact=True).wait_for()
        page.get_by_role("button", name="恢复已取得的结果", exact=True).click()
        page.locator(f'[data-recover-operation="{original_id}"]').wait_for(state="detached")
        self.assertEqual(self.calls[0]["body"], {"request_id": original_id, "kind": "take"})
        self.assertEqual(len(self.calls), 1)
        page.get_by_text("配音结果不确定，请先核对服务商记录。", exact=True).wait_for()
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_unknown_voice_retries_only_after_explicit_confirmation_and_keeps_error(self):
        original_id = "unknown-request-12345"
        self.payload["job"]["recovery_operations"] = [{"request_id": original_id, "kind": "take", "state": "uncertain",
            "uncertain": True, "can_retry_not_generated": True, "message": "配音结果不确定，请先核对服务商记录。"}]

        def recover(route):
            if len(self.calls) == 1:
                route.fulfill(status=409, content_type="application/json", body=json.dumps({"error": "已发现可恢复音频，请先恢复已有结果。"}, ensure_ascii=False))
            else:
                self.payload["job"]["recovery_operations"] = []
                route.fulfill(status=200, content_type="application/json", body="{}")

        page = self.open_page(width=390, post=recover)
        checkbox = page.locator(f'[data-confirm-no-voice="{original_id}"]')
        button = page.get_by_role("button", name="确认未生成，重试本次配音", exact=True)
        self.assertFalse(checkbox.is_checked())
        self.assertTrue(button.is_disabled())
        button.evaluate("button => button.click()")
        self.assertEqual(self.calls, [])
        page.get_by_text("重试将重新请求配音并消耗 ElevenLabs 额度。", exact=False).wait_for()
        checkbox.check()
        button.click()
        page.locator('#production-recovery [data-error]').get_by_text("已发现可恢复音频，请先恢复已有结果。", exact=True).wait_for()
        expected = {"request_id": original_id, "kind": "take", "decision": "not_generated", "confirmed": True}
        self.assertEqual(self.calls[0]["body"], expected)
        self.assertTrue(checkbox.is_checked())
        self.assertFalse(checkbox.is_disabled())
        self.assertFalse(button.is_disabled())
        button.click()
        page.locator('#production-recovery').wait_for(state="detached")
        self.assertEqual([call["body"] for call in self.calls], [expected, expected])
        self.assertTrue(all(call["url"].endswith(f"/jobs/{JOB_ID}/recover") for call in self.calls))
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_existing_response_result_or_audio_hides_not_generated_confirmation(self):
        self.payload["job"]["recovery_operations"] = [
            {"request_id": "known-response-12345", "kind": "take", "state": "uncertain", "uncertain": False,
             "has_response": True, "can_retry_not_generated": False, "message": "已保存返回，可恢复。"},
            {"request_id": "known-result-12345", "kind": "take", "state": "uncertain", "uncertain": False,
             "has_result": True, "can_retry_not_generated": False, "message": "已保存结果，可恢复。"},
            {"request_id": "known-audio-12345", "kind": "take", "state": "uncertain", "uncertain": True,
             "has_audio": True, "can_retry_not_generated": False, "message": "已找到音频，请先核对并恢复。"}]
        page = self.open_page()
        self.assertEqual(page.locator('[data-confirm-no-voice]').count(), 0)
        self.assertEqual(page.get_by_role("button", name="恢复已取得的结果", exact=True).count(), 2)
        self.assertEqual(page.get_by_role("button", name="核对并恢复", exact=True).count(), 1)

    def test_queue_does_not_promote_legacy_delivery_rows_without_verified_state(self):
        page = self.open_page()
        page.route("**/api/production/eleven", lambda route: route.fulfill(status=200,
            content_type="application/json", body=json.dumps({"ok": True})))
        base = {"kind": "video", "product": "202H", "title": "测试视频", "direction_id": "202H-S1",
                "timeline_name": "Lab261011-202-1", "updated_at": "2026-10-11"}
        rows = [
            {**base, "id": "PRD-111111111111"},
            {**base, "id": "PRD-222222222222", "status": "completed", "edit_id": "EDT-222222222222"},
            {**base, "id": "PRD-333333333333", "status": "awaiting_review"},
            {**base, "id": "PRD-444444444444", "status": "awaiting_review", "edit_id": "EDT-444444444444"}]
        page.route("**/api/production/deliveries?*", lambda route: route.fulfill(status=200,
            content_type="application/json", body=json.dumps({"items": rows}, ensure_ascii=False)))
        self.reviewable()
        jobs = [{**copy.deepcopy(self.payload["job"]), "id": row["id"], "status": "awaiting_review",
                 "edits": [self.edit("awaiting_review", row.get("edit_id", "EDT-0123456789ab"))]} for row in rows]
        page.route("**/api/production?*", lambda route: route.fulfill(status=200, content_type="application/json",
            body=json.dumps({"jobs": jobs, "carousels": [], "voices": [], "defaults": {}}, ensure_ascii=False)))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#production")
        page.locator('[data-delivery-item="PRD-444444444444"]').wait_for()
        self.assertEqual(page.locator("#production-queue .production-card").count(), 4)
        self.assertEqual(page.locator('[data-delivery-kind="video"]').count(), 1)
        for identifier in ("PRD-111111111111", "PRD-222222222222", "PRD-333333333333"):
            self.assertEqual(page.locator(f'[data-delivery-item="{identifier}"]').count(), 0)
        page.locator('[data-production-id="PRD-444444444444"]').get_by_text("待人工审查", exact=True).wait_for()
        page.locator("#production-status").select_option("awaiting_review")
        self.assertEqual(page.locator("#production-queue .production-card").count(), 1)

    def reviewable(self):
        self.payload["job"].update(selected_take="TAK-0123456789ab", timelines=[{"name": "Lab261011-202-1", "unique_id": "uid-own",
            "take_id": "TAK-0123456789ab", "creator": "Lab", "markers": 1, "frames": 60, "fps": "29.97", "built_at": "2026-10-11"}])
        self.payload["job"]["edits"] = [self.edit("awaiting_review")]
        self.payload["job"]["edits"][0]["validation"]["manual_scope"] = ["画面内容与口播是否匹配", "镜头时长例外与视觉连贯性", "字幕、声音听审和成片导出"]

    def skill_delivery(self):
        self.reviewable()
        self.payload["job"]["status"] = "awaiting_review"
        edit = self.payload["job"]["edits"][0]
        edit.update(status="completed", completion_policy="skill_delivery", validation=None, summary=None,
                    report={"shots": 24, "saved": True, "gaps": []})
        return edit

    def test_skill_delivery_is_reviewable_without_claiming_engineering_verification(self):
        self.skill_delivery()
        page = self.open_page(width=390)
        card = page.locator('[data-edit-id="EDT-0123456789ab"]')
        card.locator(".script-status").get_by_text("待人工审查", exact=True).wait_for()
        card.get_by_text("工程核验未执行，素材去重已归档停用", exact=False).wait_for()
        self.assertNotIn("工程核验通过", card.inner_text())
        self.assertNotIn("画面全部覆盖", card.inner_text())
        self.assertNotIn("undefined", card.inner_text())
        self.assertTrue(card.get_by_role("button", name="确认完成", exact=True).is_enabled())
        self.assertEqual(self.calls, [])
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_skill_delivery_reported_gaps_show_as_waiting_for_material(self):
        edit = self.skill_delivery()
        edit["report"]["gaps"] = [{"start": 30, "end": 60, "need": "锁紧旋钮特写"}]
        page = self.open_page()
        card = page.locator('[data-edit-id="EDT-0123456789ab"]')
        card.locator(".script-status").get_by_text("待补素材", exact=True).wait_for()
        card.get_by_text("帧 [30, 60) · 锁紧旋钮特写", exact=True).wait_for()
        self.assertNotIn("undefined", card.inner_text())
        self.assertEqual(card.get_by_role("button", name="确认完成", exact=True).count(), 0)

    def test_skill_delivery_pending_result_recovers_without_requesting_verification(self):
        edit = self.skill_delivery()
        edit.update(status="needs_repair", result_pending=True, error="剪辑结果待恢复。")

        def recover(route):
            self.assertTrue(route.request.url.endswith("/recheck"))
            self.assertEqual(route.request.post_data_json, {"recover_result": True})
            edit.update(status="completed", result_pending=False, error="")
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"job": self.payload["job"], "edit": edit}))

        page = self.open_page(post=recover)
        card = page.locator('[data-edit-id="EDT-0123456789ab"]')
        self.assertEqual(card.get_by_role("button", name="重新核验", exact=True).count(), 0)
        card.get_by_role("button", name="恢复结果", exact=True).click()
        card.locator(".script-status").get_by_text("待人工审查", exact=True).wait_for()
        card.get_by_text("工程核验未执行", exact=False).wait_for()
        self.assertEqual(len(self.calls), 1)

    def test_skill_delivery_manual_confirmation_has_no_automatic_recheck(self):
        edit = self.skill_delivery()

        def approve(route):
            self.assertTrue(route.request.url.endswith("/approval"))
            edit["approval"] = {"at": "2026-10-12T11:00:00", "note": "人工确认", "basis": "human_review"}
            self.payload["job"]["status"] = "approved"
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"approved": True, "edit": edit}))

        page = self.open_page(post=approve)
        page.get_by_role("button", name="确认完成", exact=True).click()
        dialog = page.locator("dialog.production-dialog")
        dialog.get_by_text("仅登记你的人工审查，不自动读回工程核验或计算素材去重", exact=False).wait_for()
        submit = dialog.get_by_role("button", name="确认人工审查完成", exact=True)
        self.assertTrue(submit.is_disabled())
        for box in dialog.locator('input[name="reviewed"]').all():
            box.check()
        submit.click()
        card = page.locator('[data-edit-id="EDT-0123456789ab"]')
        card.locator(".script-status").get_by_text("已完成", exact=True).wait_for()
        card.get_by_text("工程核验未执行", exact=False).wait_for()
        self.assertEqual(len(self.calls), 1)

    def test_skill_delivery_saved_failure_does_not_offer_manual_completion(self):
        edit = self.skill_delivery()
        edit["report"]["saved"] = False
        page = self.open_page()
        card = page.locator('[data-edit-id="EDT-0123456789ab"]')
        card.locator(".script-status").get_by_text("待修正", exact=True).wait_for()
        self.assertEqual(card.get_by_role("button", name="确认完成", exact=True).count(), 0)
        self.assertNotIn("已按技能完成保存交付", card.inner_text())

    def test_skill_delivery_enters_review_queue_without_validation(self):
        edit = self.skill_delivery()
        page = self.open_page()
        page.route("**/api/production?*", lambda route: route.fulfill(status=200, content_type="application/json",
            body=json.dumps({"jobs": [self.payload["job"]], "carousels": [], "voices": [], "defaults": {}})))
        row = {"kind": "video", "id": JOB_ID, "status": "awaiting_review", "edit_id": edit["id"]}
        page.route("**/api/production/deliveries?*", lambda route: route.fulfill(status=200, content_type="application/json",
            body=json.dumps({"items": [row]})))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#production")
        card = page.locator(f'[data-production-id="{JOB_ID}"]')
        card.get_by_text("待人工审查", exact=True).wait_for()
        card.get_by_text("按技能保存交付 · 工程核验未执行", exact=True).wait_for()
        self.assertNotIn("工程核验通过", card.inner_text())
        page.locator("#production-status").select_option("awaiting_review")
        self.assertEqual(page.locator("#production-queue .production-card").count(), 1)

    def test_completion_needs_every_manual_check_and_then_shows_done(self):
        self.reviewable()

        def approve(route):
            self.payload["job"]["status"] = "approved"
            self.payload["job"]["edits"][0]["approval"] = {"at": "2026-10-11T11:00:00", "note": "已导出"}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"approved": True, "edit": self.payload["job"]["edits"][0]}, ensure_ascii=False))

        page = self.open_page(width=390, post=approve)
        page.get_by_role("button", name="确认完成", exact=True).click()
        dialog = page.locator("dialog.production-dialog")
        submit = dialog.get_by_role("button", name="读回核验并确认完成", exact=True)
        self.assertTrue(submit.is_disabled())
        boxes = dialog.locator('input[name="reviewed"]')
        self.assertEqual(boxes.count(), 3)
        boxes.nth(0).check()
        boxes.nth(1).check()
        self.assertTrue(submit.is_disabled())
        boxes.nth(2).check()
        dialog.locator('textarea[name="note"]').fill("已导出")
        submit.click()
        card = page.locator('[data-edit-id="EDT-0123456789ab"]')
        card.locator(".script-status").get_by_text("已完成", exact=True).wait_for()
        card.get_by_role("button", name="撤销完成", exact=True).wait_for()
        self.assertEqual(card.get_by_role("button", name="确认完成", exact=True).count(), 0)
        body = self.calls[0]["body"]
        self.assertTrue(self.calls[0]["url"].endswith("/edits/EDT-0123456789ab/approval"))
        self.assertEqual((body["approved"], body["note"], len(body["reviewed"])), (True, "已导出", 3))
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_superseded_or_unselected_edits_offer_no_completion(self):
        self.reviewable()
        self.payload["job"]["selected_take"] = "TAK-999999999999"
        page = self.open_page()
        page.locator('[data-edit-id="EDT-0123456789ab"]').wait_for()
        self.assertEqual(page.get_by_role("button", name="确认完成", exact=True).count(), 0)

    def test_old_edit_without_snapshot_offers_historical_restoration(self):
        self.reviewable()
        self.payload["historical_baseline_restore"] = True
        edit = self.payload["job"]["edits"][0]
        edit.update(status="needs_repair")
        edit["validation"].update(status="needs_repair", issues=[{"code": "baseline_missing", "message": "旧记录没有真实剪前快照。"}])

        def restore(route):
            self.assertTrue(route.request.url.endswith("/edits/EDT-0123456789ab/baseline"))
            self.assertEqual(route.request.post_data_json["source"], "historical_resume")
            edit.update(status="awaiting_review", baseline={"tracks": {}}, baseline_origin="historical_resume", baseline_at="2026-10-10T13:49:53+00:00",
                        baseline_evidence={"captured_at": "2026-10-10T13:49:53+00:00", "restored_at": "2026-10-11T05:00:00+00:00",
                                           "scope": "Codex接续剪辑前", "sources": []})
            edit["validation"].update(status="awaiting_review", issues=[])
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"edit": edit}, ensure_ascii=False))

        page = self.open_page(post=restore)
        page.get_by_role("button", name="补齐核验依据", exact=True).click()
        page.locator("dialog.production-dialog").get_by_role("button", name="恢复依据并核验", exact=True).click()
        card = page.locator('[data-edit-id="EDT-0123456789ab"]')
        card.get_by_text("恢复依据不会自动确认完成", exact=False).wait_for()
        self.assertEqual(card.get_by_role("button", name="补齐核验依据", exact=True).count(), 0)
        card.get_by_role("button", name="确认完成", exact=True).wait_for()


if __name__ == "__main__":
    unittest.main()
