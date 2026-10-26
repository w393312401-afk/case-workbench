"""Quick content filters stay responsive and preserve the shared-view workflow."""
from __future__ import annotations

import json
import unittest

from playwright.sync_api import expect
import test_content_filters_ui as filter_fixture
import test_content_saved_views_ui as saved_fixture


class ContentQuickFiltersBrowserTests(unittest.TestCase):
    # Share disposable stores and a real server without inheriting unrelated tests.
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
    records = saved_fixture.ContentSavedViewsBrowserTests.records
    assert_rows = saved_fixture.ContentSavedViewsBrowserTests.assert_rows
    dialog = saved_fixture.ContentSavedViewsBrowserTests.dialog
    params = saved_fixture.ContentSavedViewsBrowserTests.params
    save = saved_fixture.ContentSavedViewsBrowserTests.save
    stored_views = saved_fixture.ContentSavedViewsBrowserTests.stored_views
    screenshot = filter_fixture.ContentFiltersBrowserTests.screenshot

    def add_condition(self, page, field, values, search=""):
        page.locator(".flow-filter-add-quick").click()
        popup = page.locator(".flow-quick-popover")
        expect(popup).to_be_visible()
        expect(popup.locator(".flow-quick-search")).to_be_focused()
        if search:
            popup.locator(".flow-quick-search").fill(search)
            expect(popup.locator("[data-quick-field]:visible")).to_have_count(1)
        popup.locator(f'[data-quick-field="{field}"]').click()
        for value in values:
            popup.locator(f'input[data-quick-value="{value}"]').check()
        popup.locator("[data-quick-done]").click()
        expect(popup).to_have_count(0)

    def test_quick_search_multiselect_and_calendar_view_are_shared_between_browsers(self):
        rows = self.records()
        page = self.content_page()
        self.view(page, "all")
        self.add_condition(page, "kind", ["video", "script"])
        self.assert_rows(page, rows.values())
        expect(page.locator(".flow-filter-chip[data-quick-condition]")).to_have_count(1)
        self.add_condition(page, "review_calendar", ["not_added"], search="日历")
        self.assert_rows(page, [rows["approved"]])
        expect(page.locator("#content-flow-filter-summary")).to_contain_text("未添加到日历")
        self.save(page, "快捷筛选保存的待入日历内容")
        stored = self.stored_views()["views"][0]
        self.assertEqual(stored["filter"]["match"], "all")
        conditions = {row["field"]: set(row["values"]) for row in stored["filter"]["conditions"]}
        self.assertEqual(conditions, {"kind": {"video", "script"}, "review_calendar": {"not_added"}})

        other = self.content_page()
        self.assertIsNot(page.context, other.context)
        other.locator(f'[data-flow-saved-view="{stored["id"]}"]').click()
        self.assert_rows(other, [rows["approved"]])
        expect(other.locator(".flow-filter-chip[data-quick-condition]")).to_have_count(2)

    def test_rapid_checkbox_changes_coalesce_requests_and_settle_on_the_last_selection(self):
        rows = self.records()
        page = self.content_page()
        self.view(page, "all")
        self.assert_rows(page, rows.values())
        requests = []
        page.on("request", lambda request: requests.append(request.url)
                if "/api/content?" in request.url else None)
        page.locator(".flow-filter-add-quick").click()
        popup = page.locator(".flow-quick-popover")
        popup.locator('[data-quick-field="kind"]').click()
        # A burst arrives before the next paint, exercising debounce and replaced controls.
        values = ["video", "script", "video", "script"] * 6 + ["video", "script", "video"]
        page.evaluate("""values => values.forEach(value => {
            const input = document.querySelector(`.flow-quick-popover input[data-quick-value="${value}"]`);
            if (!input) throw new Error('Quick picker disappeared while changing values');
            input.click();
        })""", values)
        self.assert_rows(page, [rows["script"], rows["archived"]])
        expect(popup).to_be_visible()
        expect(popup.locator('[data-quick-value="script"]')).to_be_checked()
        expect(popup.locator('[data-quick-value="video"]')).not_to_be_checked()
        spec = json.loads(self.params(page)["filter"][0])
        self.assertEqual(spec["conditions"], [{"field": "kind", "values": ["script"]}])
        # One settled request is sufficient; allow a second for an already queued refresh.
        self.assertLessEqual(len(requests), 2, requests)
        popup.locator("[data-quick-done]").click()
        self.assert_rows(page, [rows["script"], rows["archived"]])

    def test_content_renders_while_views_are_held_and_recent_metadata_is_reused_until_refresh(self):
        rows = self.records()
        page = self.open_page("#/202H/library")
        # Keep freshness deterministic without imposing a response-time threshold.
        page.evaluate("() => { const now = Date.now(); Date.now = () => now; }")
        metadata_requests, held = [], []

        def hold_first_metadata(route):
            metadata_requests.append(route.request.url)
            if len(metadata_requests) == 1:
                held.append(route)
                return
            route.continue_()

        page.route("**/api/content/views?*", hold_first_metadata)
        page.evaluate("location.hash = '#/202H/content?view=all'")
        # This assertion completes while the saved-view response is still withheld.
        self.assert_rows(page, rows.values())
        self.assertEqual(len(held), 1)
        self.assertEqual(len(metadata_requests), 1)
        expect(page.locator("#content-flow-save-view")).to_be_disabled()
        with page.expect_response(lambda response: "/api/content/views?" in response.url):
            held.pop().continue_()
        expect(page.locator("#content-flow-save-view")).to_be_enabled()

        self.add_condition(page, "kind", ["video"])
        self.assert_rows(page, [rows["approved"], rows["draft"]])
        self.add_condition(page, "status", ["approved"])
        self.assert_rows(page, [rows["approved"]])
        self.assertEqual(len(metadata_requests), 1, "Quick filtering should reuse recent saved-view metadata")
        with page.expect_response(lambda response: "/api/content/views?" in response.url):
            page.locator("#content-flow-refresh").click()
        self.assert_rows(page, [rows["approved"]])
        self.assertEqual(len(metadata_requests), 2, "Explicit refresh must retrieve current shared views")

    def test_match_toggle_chip_removal_and_deselecting_last_value_apply_without_submit(self):
        rows = self.records()
        page = self.content_page()
        self.view(page, "all")
        self.add_condition(page, "kind", ["script"])
        self.assert_rows(page, [rows["script"], rows["archived"]])
        self.add_condition(page, "status", ["approved"])
        self.assert_rows(page, [])
        page.locator(".flow-filter-match").click()
        self.assert_rows(page, [rows["script"], rows["archived"], rows["approved"]])
        self.assertEqual(json.loads(self.params(page)["filter"][0])["match"], "any")
        page.locator('.flow-filter-chip-remove[data-quick-remove="0"]').click()
        self.assert_rows(page, [rows["approved"]])
        expect(page.locator(".flow-filter-chip[data-quick-condition]")).to_have_count(1)
        page.locator('.flow-filter-chip[data-quick-condition="0"]').click()
        popup = page.locator(".flow-quick-popover")
        popup.locator('[data-quick-value="approved"]').uncheck()
        self.assert_rows(page, rows.values())
        expect(page.locator(".flow-filter-chip[data-quick-condition]")).to_have_count(0)
        encoded = self.params(page).get("filter", ["{}"])[0]
        spec = json.loads(encoded)
        self.assertEqual(spec.get("conditions", []), [])
        popup.locator("[data-quick-done]").click()
        # The existing complete editor remains reachable from the rules button.
        page.locator("#content-flow-filter-toggle").click()
        expect(page.locator("#content-flow-filter-editor")).to_be_visible()

    def test_popover_focus_dismissal_and_small_screen_geometry_in_both_themes(self):
        self.records()
        page = self.content_page()
        self.view(page, "all")
        add = page.locator(".flow-filter-add-quick")
        popup = page.locator(".flow-quick-popover")
        add.click()
        expect(popup).to_have_attribute("role", "dialog")
        expect(popup.locator(".flow-quick-search")).to_be_focused()
        page.keyboard.press("Escape")
        expect(popup).to_have_count(0)
        expect(add).to_be_focused()
        add.click()
        page.locator("#content-flow-result-count").click()
        expect(popup).to_have_count(0)

        for scheme, width in (("light", 1280), ("dark", 1280), ("light", 390), ("dark", 390)):
            page.set_viewport_size({"width": width, "height": 900})
            page.emulate_media(color_scheme=scheme)
            add.click()
            popup.locator('[data-quick-field="status"]').click()
            expect(popup.locator(".flow-quick-search")).to_be_focused()
            box = popup.bounding_box()
            self.assertIsNotNone(box)
            self.assertGreaterEqual(box["x"], 0)
            self.assertGreaterEqual(box["y"], 0)
            self.assertLessEqual(box["x"] + box["width"], width)
            self.assertLessEqual(box["y"] + box["height"], 900)
            self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))
            self.screenshot(page, f"content-quick-filter-{scheme}-{width}.png")
            page.keyboard.press("Escape")
            expect(popup).to_have_count(0)


if __name__ == "__main__":
    unittest.main()
