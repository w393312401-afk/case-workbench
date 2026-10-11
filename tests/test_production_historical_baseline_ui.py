"""Historical evidence display and requests, using only isolated browser fixtures."""
from __future__ import annotations

import json
import unittest

import test_production_validation_ui as fixture


class HistoricalBaselineBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ProductionValidationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ProductionValidationBrowserTests.tearDownClass.__func__)
    setUp = fixture.ProductionValidationBrowserTests.setUp
    stop_server = fixture.ProductionValidationBrowserTests.stop_server
    request = fixture.ProductionValidationBrowserTests.request
    make_playable_video = fixture.ProductionValidationBrowserTests.make_playable_video
    open_page = fixture.ProductionValidationBrowserTests.open_page
    edit = fixture.ProductionValidationBrowserTests.edit

    def evidence(self):
        return {"schema_version": 1, "kind": "historical_resume", "captured_at": "2026-10-10T13:49:53+00:00",
                "restored_at": "2026-10-11T05:00:00+00:00", "scope": "Codex接续剪辑前",
                "unrecorded_fields": ["voice.clip_enabled"], "configured_fields": ["width", "height"],
                "sources": [{"path": f"C:/isolated/{role}.json", "sha256": "a" * 64, "role": role}
                            for role in ("resume_snapshot", "native_timeline_backup", "handoff", "pre_edit_catalog")]}

    def missing(self):
        self.payload["historical_baseline_restore"] = True
        edit = self.edit("needs_repair")
        edit["validation"]["issues"] = [{"code": "baseline_missing", "message": "缺少历史核验依据。"}]
        self.payload["job"]["edits"] = [edit]
        return edit

    def test_historical_source_is_visible_and_restoration_does_not_approve(self):
        edit = self.edit("awaiting_review")
        edit.update(baseline={"tracks": {}}, baseline_origin="historical_resume", baseline_at=self.evidence()["captured_at"],
                    baseline_evidence=self.evidence())
        self.payload["job"]["edits"] = [edit]
        page = self.open_page(width=390)
        page.locator('#edit-runs .script-status').get_by_text("待人工审查", exact=True).wait_for()
        source = page.locator('.historical-baseline')
        source.get_by_text("恢复依据不会自动确认完成", exact=False).wait_for()
        source.get_by_text("历史快照未记录配音片段启用状态", exact=False).wait_for()
        source.locator('summary').click()
        source.get_by_text("resume_snapshot：C:/isolated/resume_snapshot.json", exact=False).wait_for()
        self.assertIn("a" * 64, source.inner_text())
        self.assertEqual(self.calls, [])
        self.assertNotIn("补拍", source.inner_text())
        self.assertEqual(page.locator('.production-dialog[open]').count(), 0)
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 391)

    def test_restore_request_selects_real_history_and_missing_evidence_remains_an_error(self):
        self.missing()

        def refuse(route):
            route.fulfill(status=409, content_type="application/json", body=json.dumps(
                {"error": "没有找到完整历史依据，不能以当前工程替代。"}, ensure_ascii=False))

        page = self.open_page(post=refuse)
        page.get_by_role('button', name="补齐核验依据", exact=True).click()
        modal = page.locator('.production-dialog[open]')
        modal.get_by_text("不把当前工程当作过去的快照", exact=False).wait_for()
        modal.get_by_role('button', name="恢复依据并核验", exact=True).click()
        modal.locator('[data-error]').get_by_text("没有找到完整历史依据，不能以当前工程替代。", exact=True).wait_for()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["body"]["source"], "historical_resume")
        self.assertTrue(self.calls[0]["url"].endswith('/edits/EDT-0123456789ab/baseline'))
        self.assertNotIn("baseline", self.payload["job"]["edits"][0])
        self.assertNotIn("approval", self.payload["job"]["edits"][0])

    def test_old_server_cannot_silently_adopt_current_state_as_history(self):
        self.missing()
        self.payload.pop("historical_baseline_restore")
        page = self.open_page()
        button = page.get_by_role('button', name="补齐核验依据", exact=True)
        self.assertTrue(button.is_disabled())
        button.evaluate('button => button.click()')
        page.get_by_text("需要更新工作台后恢复历史核验依据。", exact=True).wait_for()
        self.assertEqual(self.calls, [])
        self.assertEqual(page.locator('.production-dialog[open]').count(), 0)

    def test_adopted_current_baseline_is_not_described_as_an_earlier_snapshot(self):
        edit = self.edit("awaiting_review")
        edit.update(baseline={"tracks": {}}, baseline_origin="adopted", baseline_at="2026-10-11T05:00:00+00:00")
        self.payload["job"]["edits"] = [edit]
        page = self.open_page()
        note = page.locator('#edit-runs .context-note')
        note.get_by_text("当前保护基准采纳于", exact=False).wait_for()
        self.assertIn("不能证明更早的剪辑过程没有改动", note.inner_text())
        self.assertNotIn("补拍", note.inner_text())


if __name__ == "__main__":
    unittest.main()
