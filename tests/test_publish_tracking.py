"""Publish date, production links and data cutoffs live in the append-only case snapshot."""
from __future__ import annotations

import unittest

import test_server as fixture
import test_ui_regressions as browser


PRODUCTION = {
    "script_version": "V2｜够用与进阶都备好", "davinci_project": "202H",
    "davinci_timeline": "lab-260929-202-2", "voiceover": "vo-2026-09-29.mp3",
}


class PublishTrackingAPITests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    detail = fixture.WorkbenchHTTPTests.detail
    body = fixture.WorkbenchHTTPTests.body
    save = fixture.WorkbenchHTTPTests.save
    record_bytes = fixture.WorkbenchHTTPTests.record_bytes
    assert_old_records_unchanged = fixture.WorkbenchHTTPTests.assert_old_records_unchanged

    def test_checkpoints_append_snapshots_and_update_reading_and_search(self):
        before = self.record_bytes()
        for as_of, views, days in (("2026-10-01", "12.3K", 1), ("2026-10-07", "48.5K", 7)):
            with self.subTest(as_of=as_of):
                previous = self.detail()
                body = self.body(previous)
                body["current"]["publish"] = {"published_on": "2026-09-30"}
                body["current"]["production"] = PRODUCTION
                body["current"].setdefault("evidence", {"status": "user_reported"})["as_of"] = as_of
                body["current"].setdefault("metrics", {})["views_display"] = views
                status, _, result = self.save(body)
                self.assertEqual(status, 200, result)
                stored = self.store.read_case(self.identifier)
                self.assertEqual(stored["current"]["evidence"]["as_of"], as_of)
                self.assertEqual(len(stored["events"]), len(previous["events"]) + 1)
                reading = (self.runtime / "library" / self.identifier / "case.md").read_text(encoding="utf-8")
                self.assertIn(f"**数据截至**：{as_of}（发布后第 {days} 天）", reading)
                self.assertIn("- 达芬奇时间线：lab-260929-202-2", reading)
                self.assert_old_records_unchanged(before)
        history = [event.get("current", {}).get("evidence", {}).get("as_of") for event in self.store.read_case(self.identifier)["events"]]
        self.assertEqual([value for value in history if value], ["2026-10-01", "2026-10-07"])
        state = self.request("GET", "/api/state")[2]
        row = next(row for row in state["cases"] if row["case_id"] == self.identifier)
        self.assertIn("lab-260929-202-2", row["search_text"])
        self.assertNotIn("vo-2026-09-29", row["search_text"])

    def test_invalid_release_values_are_rejected_without_writing(self):
        before = self.record_bytes()
        index_before = (self.skill / "cases" / "index.json").read_bytes()
        cases = [
            ("publish", {"published_on": "2026-02-30"}, None),
            ("publish", {"published_on": "2026-09-30"}, "2026-09-29"),
            ("production", {}, None),
            ("production", {"timeline": "lab"}, None),
        ]
        for key, value, as_of in cases:
            with self.subTest(key=key, value=value, as_of=as_of):
                body = self.body()
                body["current"][key] = value
                if as_of:
                    body["current"].setdefault("evidence", {"status": "user_reported"})["as_of"] = as_of
                status, _, result = self.save(body)
                self.assertEqual(status, 400, result)
                self.assertEqual(self.record_bytes(), before)
                self.assertEqual((self.skill / "cases" / "index.json").read_bytes(), index_before)


class PublishTrackingBrowserTests(unittest.TestCase):
    """Borrow the isolated browser fixture without re-running its regressions."""
    setUp = browser.WorkbenchBrowserTests.setUp
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    open_page = browser.WorkbenchBrowserTests.open_page
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def test_editor_saves_release_links_without_authoring_sales_cutoffs(self):
        previous_evidence = self.store.read_case(self.identifier)["current"]["evidence"].copy()
        page = self.open_page(f"#case/{self.identifier}")
        page.locator('.detail-tabs [data-tab="data"]').click()
        # Unrecorded cases still show the card, with a shortcut into the editor.
        empty = page.locator("#detail-section-release")
        empty.get_by_text("未记录发布日期与制作关联").wait_for()
        empty.get_by_role("button", name="补发布信息").click()
        page.wait_for_function("() => document.activeElement?.name === 'publish_published_on'")
        page.locator('[name="publish_published_on"]').fill("2026-09-30")
        page.locator("summary", has_text="制作关联").click()
        page.locator('[name="production_davinci_timeline"]').fill("lab-260929-202-2")
        self.assertEqual(page.locator('[name="checkpoint_node"],[name^="evidence_"],#fill-cumulative-window').count(), 0)
        page.locator(".inline-revision-note summary").click()
        page.locator('[name="note"]').fill("记录发布日期与制作关联")
        page.locator("[data-inline-save]").click()
        page.locator(".case-inline-editor").wait_for(state="hidden")
        stored = self.store.read_case(self.identifier)["current"]
        self.assertEqual(stored["publish"], {"published_on": "2026-09-30"})
        self.assertEqual(stored["production"], {"davinci_timeline": "lab-260929-202-2"})
        self.assertEqual(stored["evidence"], previous_evidence)
        release = page.locator("#detail-section-release")
        release.get_by_text("lab-260929-202-2").wait_for()
        self.assertEqual(release.get_by_text("数据截至 2026-10-07 · 发布后第 7 天").count(), 0)
        # Reopening reflects release metadata, while sales remains read-only.
        page.locator(".detail-heading").wait_for()
        page.evaluate("openEditor(state.detail, {advanced: true})")
        self.assertEqual(page.locator('[name="publish_published_on"]').input_value(), "2026-09-30")
        self.assertEqual(page.locator('[name="production_davinci_timeline"]').input_value(), "lab-260929-202-2")


if __name__ == "__main__":
    unittest.main()
