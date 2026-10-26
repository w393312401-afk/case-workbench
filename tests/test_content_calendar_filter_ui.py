"""Calendar-after-review filtering and shared views through the real workbench API."""
from __future__ import annotations

import json
import unittest
from uuid import uuid4

from playwright.sync_api import expect
import test_content_filters_ui as filter_fixture
import test_content_saved_views_ui as saved_fixture


class ContentCalendarFilterBrowserTests(unittest.TestCase):
    # Reuse helpers without rediscovering the source classes' test cases.
    setUpClass = classmethod(saved_fixture.ContentSavedViewsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(saved_fixture.ContentSavedViewsBrowserTests.tearDownClass.__func__)
    setUp = saved_fixture.ContentSavedViewsBrowserTests.setUp
    stop_server = saved_fixture.ContentSavedViewsBrowserTests.stop_server
    request = saved_fixture.ContentSavedViewsBrowserTests.request
    open_page = saved_fixture.ContentSavedViewsBrowserTests.open_page
    script = saved_fixture.ContentSavedViewsBrowserTests.script
    finished_job = saved_fixture.ContentSavedViewsBrowserTests.finished_job
    flow = saved_fixture.ContentSavedViewsBrowserTests.flow
    manage = saved_fixture.ContentSavedViewsBrowserTests.manage
    content_page = saved_fixture.ContentSavedViewsBrowserTests.content_page
    view = saved_fixture.ContentSavedViewsBrowserTests.view
    assert_rows = saved_fixture.ContentSavedViewsBrowserTests.assert_rows
    editor = saved_fixture.ContentSavedViewsBrowserTests.editor
    open_values = saved_fixture.ContentSavedViewsBrowserTests.open_values
    set_filters = saved_fixture.ContentSavedViewsBrowserTests.set_filters
    dialog = saved_fixture.ContentSavedViewsBrowserTests.dialog
    params = saved_fixture.ContentSavedViewsBrowserTests.params
    save = saved_fixture.ContentSavedViewsBrowserTests.save
    stored_views = saved_fixture.ContentSavedViewsBrowserTests.stored_views
    screenshot = filter_fixture.ContentFiltersBrowserTests.screenshot

    def records(self):
        added = self.finished_job("审查完成且已添加日历")
        pending = self.finished_job("审查完成待添加日历")
        draft_script = self.script("未审查但已标记日历", text="An unreviewed video.")
        draft = self.server.app.create_production_job({
            "request_id": uuid4().hex, "revision": self.server.app.production.public()["revision"],
            "job": {"script_id": draft_script["id"]}})["job"]
        standalone = self.script("尚未制作的采用稿", text="An adopted script.")
        self.manage("video:" + added["id"], {"calendar_uploaded": True})
        self.manage("video:" + draft["id"], {"calendar_uploaded": True})
        return {"added": "video:" + added["id"], "pending": "video:" + pending["id"],
                "draft": "video:" + draft["id"], "script": "script:" + standalone["family_id"]}

    def test_calendar_choices_only_include_reviewed_videos_and_support_multiselect_and_any(self):
        rows = self.records()
        page = self.content_page()
        self.view(page, "all")
        self.assert_rows(page, rows.values())
        self.set_filters(page, [("review_calendar", ["added"])])
        self.assert_rows(page, [rows["added"]])
        expect(page.locator("#content-flow-filter-summary")).to_contain_text("审查后日历")
        expect(page.locator("#content-flow-filter-summary")).to_contain_text("已添加到日历")
        editor = self.editor(page)
        self.open_values(editor.locator("[data-filter-row]"))
        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 900})
            self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))
            self.screenshot(page, f"content-calendar-filter-{width}.png")
        page.set_viewport_size({"width": 1280, "height": 900})
        editor.locator("[data-filter-cancel]").click()
        self.set_filters(page, [("review_calendar", ["not_added"])])
        self.assert_rows(page, [rows["pending"]])
        expect(page.locator("#content-flow-filter-summary")).to_contain_text("未添加到日历")
        self.set_filters(page, [("review_calendar", ["added", "not_added"])])
        self.assert_rows(page, [rows["added"], rows["pending"]])
        self.set_filters(page, [("review_calendar", ["not_added"]), ("kind", ["script"])], match="any")
        self.assert_rows(page, [rows["pending"], rows["script"]])

    def test_saved_calendar_view_survives_reload_and_opens_in_a_separate_browser(self):
        rows = self.records()
        first = self.content_page()
        self.view(first, "all")
        self.set_filters(first, [("review_calendar", ["not_added"])])
        saved = self.save(first, "审查后待添加日历")
        self.assert_rows(first, [rows["pending"]])
        stored = self.stored_views()["views"][0]
        expected_filter = {"match": "all", "conditions": [
            {"field": "review_calendar", "values": ["not_added"]}]}
        self.assertEqual(stored["filter"], expected_filter)
        first.reload()
        expect(saved).to_have_attribute("aria-pressed", "true")
        self.assert_rows(first, [rows["pending"]])

        second = self.content_page()
        self.assertIsNot(first.context, second.context)
        second.locator(f'[data-flow-saved-view="{stored["id"]}"]').click()
        self.assert_rows(second, [rows["pending"]])
        self.assertEqual(json.loads(self.params(second)["filter"][0]), expected_filter)
        editor = self.editor(second)
        expect(editor.locator('[data-filter-field]')).to_have_value("review_calendar")
        expect(editor.locator('[data-filter-value="not_added"]')).to_be_checked()
        expect(editor.locator('[data-filter-value="added"]')).not_to_be_checked()


if __name__ == "__main__":
    unittest.main()
