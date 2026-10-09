"""Inline editing and compact creation against isolated temporary case records."""
from __future__ import annotations

import copy
import unittest
from uuid import uuid4

import test_server as fixture
import test_ui_regressions as browser


class InlineEditorBrowserTests(unittest.TestCase):
    setUp = browser.WorkbenchBrowserTests.setUp
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    open_page = browser.WorkbenchBrowserTests.open_page
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def inline(self, page, section):
        page.evaluate("([id, section]) => { if (state.detail?.case_id !== id) throw new Error('Wrong case'); CaseInlineEditor.open(state.detail, section); }", [self.identifier, section])
        form = page.locator(f'.case-inline-editor[data-edit-section="{section}"]')
        form.wait_for()
        self.assertFalse(page.locator("#edit-dialog").evaluate("element => element.open"))
        return form

    def test_compact_creation_needs_only_title_source_and_conclusion(self):
        page = self.open_page("#/202H/library")
        page.locator("#add-top").click()
        form = page.locator("#edit-form")
        self.assertEqual(form.locator("[required]").evaluate_all("nodes => nodes.map(node => node.name)"), ["title", "url", "conclusion"])
        self.assertEqual(form.locator('.editor-grid').count(), 0)
        form.locator('[name="title"]').fill("三项收录")
        form.locator('[name="url"]').fill("https://www.tiktok.com/@test/video/7999999999999999999")
        form.locator('[name="conclusion"]').fill("先保存明确的购买理由，后续补齐文案。")
        form.locator("#save-button").click()
        page.locator("#edit-dialog").wait_for(state="hidden")
        page.locator('.detail-heading h1').get_by_text("三项收录", exact=True).wait_for()
        identifier = page.evaluate("state.detail.case_id")
        saved = self.store.read_case(identifier)
        self.assertEqual(saved["product"], "202H")
        self.assertEqual(saved["current"]["metrics"]["video_id"], "7999999999999999999")
        self.assertNotIn("transcript", saved["current"])
        self.assertTrue(page.url.endswith(f"#/202H/case/{identifier}"))

    def test_compact_creation_allows_local_video_instead_of_url(self):
        page = self.open_page("#/all/library")
        page.locator("#add-top").click()
        page.locator('[data-upload="video"]').set_input_files(self.video)
        page.wait_for_function("() => state.editor?.videoUpload?.state === 'complete'")
        self.assertFalse(page.locator('[name="url"]').evaluate("element => element.required"))
        page.locator('[name="title"]').fill("本地视频收录")
        page.locator('[name="conclusion"]').fill("本地画面可以作为后续文案的参考。")
        page.locator("#save-button").click()
        page.locator("#edit-dialog").wait_for(state="hidden")
        page.locator('.detail-heading h1').get_by_text("本地视频收录", exact=True).wait_for()
        saved = self.store.read_case(page.evaluate("state.detail.case_id"))
        self.assertTrue(saved["current"]["assets"]["video"].startswith(str(self.runtime)))

    def test_inline_insights_save_appends_revision_and_preserves_other_sections(self):
        before = self.store.read_case(self.identifier)
        page = self.open_page(f"#case/{self.identifier}")
        page.locator('.detail-heading').wait_for()
        page.locator('[data-tab="script"]').click()
        page.locator('[data-inline-edit="insights"]').click()
        form = page.locator('.case-inline-editor[data-edit-section="insights"]')
        form.wait_for()
        self.assertFalse(page.locator("#edit-dialog").evaluate("element => element.open"))
        form.locator('[name="highlights"]').fill("第一条新判断\n第二条新判断")
        form.locator('[data-inline-save]').click()
        form.wait_for(state="detached")
        after = self.store.read_case(self.identifier)
        self.assertEqual(len(after["events"]), len(before["events"]) + 1)
        self.assertEqual(after["current"]["highlights"], ["第一条新判断", "第二条新判断"])
        unchanged = copy.deepcopy(after["current"])
        unchanged["highlights"] = before["current"]["highlights"]
        # A saved video is moved into managed storage by the existing backend.
        unchanged["assets"] = before["current"]["assets"]
        unchanged.pop("source_event_ids", None)
        expected = copy.deepcopy(before["current"])
        expected.pop("source_event_ids", None)
        self.assertEqual(unchanged, expected)

    def test_inline_conflict_preserves_the_draft_and_external_revision(self):
        page = self.open_page(f"#case/{self.identifier}")
        page.locator('.detail-heading').wait_for()
        form = self.inline(page, "overview")
        form.locator('[name="conclusion"]').fill("仍未保存的现场判断")
        external = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        external["conclusion"] = "外部最新判断"
        self.store.append(self.identifier, {"kind": "editorial", "text": "外部修改", "current": external})
        form.locator('[data-inline-save]').click()
        form.locator('[data-inline-status]').get_by_text("不会覆盖其他位置的修改", exact=False).wait_for()
        self.assertEqual(form.locator('[name="conclusion"]').input_value(), "仍未保存的现场判断")
        self.assertEqual(self.store.read_case(self.identifier)["current"]["conclusion"], "外部最新判断")

    def test_inline_navigation_keeps_draft_until_user_discards(self):
        page = self.open_page(f"#case/{self.identifier}")
        page.locator('.detail-heading').wait_for()
        form = self.inline(page, "overview")
        form.locator('[name="conclusion"]').fill("不能在切页时丢失")
        page.locator('#videos-nav').click()
        page.locator('#discard-dialog').wait_for()
        self.assertIn(f"/case/{self.identifier}", page.url)
        page.locator('#keep-editing').click()
        self.assertEqual(form.locator('[name="conclusion"]').input_value(), "不能在切页时丢失")
        page.locator('#videos-nav').click()
        page.locator('#discard-dialog').wait_for()
        page.locator('#discard-edits').click()
        page.wait_for_url('**/videos')
        self.assertEqual(page.locator('.case-inline-editor').count(), 0)

    def test_transcript_input_clears_stale_translation_immediately(self):
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["translation_zh"] = {"channel": "transcript", "source_text": current["transcript"]["text"], "text": "旧的中文对照"}
        self.store.append(self.identifier, {"kind": "editorial", "text": "加上对照", "current": current})
        page = self.open_page(f"#case/{self.identifier}")
        page.locator('.detail-heading').wait_for()
        form = self.inline(page, "transcript")
        form.locator('[name="transcript_text"]').fill("The revised original wording.")
        self.assertEqual(form.locator('[name="translation_text"]').input_value(), "")
        self.assertEqual(form.locator('[name="translation_source_text"]').input_value(), "The revised original wording.")

    def test_inline_video_upload_uses_existing_import_pipeline(self):
        before = self.store.read_case(self.identifier)["current"]
        page = self.open_page(f"#case/{self.identifier}")
        page.locator('.detail-heading').wait_for()
        form = self.inline(page, "assets")
        form.locator('[data-upload="video"]').set_input_files(self.video)
        page.wait_for_function("() => state.editor?.videoUpload?.state === 'complete'")
        self.assertEqual(page.evaluate("state.editor.mode"), "inline")
        form.locator('[data-inline-save]').click()
        form.wait_for(state="detached")
        saved = self.store.read_case(self.identifier)["current"]
        self.assertTrue(saved["assets"]["video"].startswith(str(self.runtime)))
        self.assertEqual(saved["transcript"], before["transcript"])
        self.assertEqual(saved["metrics"], before["metrics"])

    def test_tab_changes_keep_form_and_forced_refresh_asks_before_replacing_it(self):
        page = self.open_page(f"#case/{self.identifier}")
        page.locator('.detail-heading').wait_for()
        form = self.inline(page, "overview")
        form.locator('[name="conclusion"]').fill("切换页签后仍保留")
        page.locator('[data-tab="script"]').click()
        self.assertEqual(form.count(), 1)
        page.locator('[data-tab="current"]').click()
        self.assertEqual(form.locator('[name="conclusion"]').input_value(), "切换页签后仍保留")
        page.evaluate("() => route(true, true)")
        page.locator('#discard-dialog').wait_for()
        page.locator('#keep-editing').click()
        self.assertEqual(form.locator('[name="conclusion"]').input_value(), "切换页签后仍保留")

    def test_external_delete_preserves_copyable_inline_draft(self):
        _, _, detail = self.request("GET", f"/api/cases/{self.identifier}")
        status, _, _ = self.request("POST", f"/api/cases/{self.identifier}/archive", {"request_id": uuid4().hex, "revision": detail["revision"], "archived": True, "note": "Isolated deletion fixture"})
        self.assertEqual(status, 200)
        page = self.open_page(f"#case/{self.identifier}")
        page.locator('.detail-heading').wait_for()
        form = self.inline(page, "overview")
        form.locator('[name="conclusion"]').fill("删除后也能复制的未保存内容")
        _, _, plan = self.request("GET", f"/api/cases/{self.identifier}/delete-preview")
        status, _, _ = self.request("POST", f"/api/cases/{self.identifier}/delete", {"request_id": uuid4().hex, "revision": plan["revision"], "plan_token": plan["plan_token"], "confirmation": self.identifier, "include_source": False})
        self.assertEqual(status, 200)
        page.evaluate("() => refresh(true)")
        form.locator('[data-inline-status]').get_by_text("案例已在其他位置删除", exact=False).wait_for()
        self.assertTrue(form.locator('[data-inline-save]').is_disabled())
        self.assertTrue(form.locator('[data-inline-copy]').is_enabled())
        self.assertEqual(form.locator('[name="conclusion"]').input_value(), "删除后也能复制的未保存内容")


if __name__ == "__main__":
    unittest.main()
