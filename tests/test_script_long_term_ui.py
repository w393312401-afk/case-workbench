"""Content owns adopted copy and automatically retained revisions; orders keep their exact draft."""
from pathlib import Path
import unittest
from uuid import uuid4

from playwright.sync_api import expect
import test_phase4_writing_ui as writing


class ScriptLongTermBrowserTests(unittest.TestCase):
    setUpClass = classmethod(writing.WritingLifecycleBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(writing.WritingLifecycleBrowserTests.tearDownClass.__func__)
    setUp = writing.WritingLifecycleBrowserTests.setUp
    stop_server = writing.WritingLifecycleBrowserTests.stop_server
    request = writing.WritingLifecycleBrowserTests.request
    catalog = writing.WritingLifecycleBrowserTests.catalog
    create_script = writing.WritingLifecycleBrowserTests.create_script
    open_page = writing.WritingLifecycleBrowserTests.open_page

    def derive(self, parent):
        code, _, result = self.request("POST", "/api/scripts", {"request_id": uuid4().hex,
            "revision": self.catalog()["revision"], "script": {"parent_id": parent["id"],
            "product": "202H", "title": "修改后的正文", "text": "New revised wording.", "confirmed": True}})
        self.assertEqual(code, 200, result)
        return result["script"]

    def test_legacy_list_and_detail_links_lead_to_content_without_a_version_tab(self):
        old = self.create_script("Historical original opening.")
        newer = self.derive(old)
        independent = self.create_script("Independent content with same title.")
        page = self.open_page("#/202H/scripts", width=390)
        expect(page.locator("#content-flow-list .flow-row")).to_have_count(2)
        self.assertEqual(page.evaluate("location.hash"), "#/202H/content")
        expect(page.locator(".scripts-tabs")).to_have_count(0)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/scripts/{old['id']}")
        expect(page.locator("#content-adopted-script")).to_have_attribute("data-adopted-script-id", old["id"])
        self.assertEqual(page.evaluate("location.hash"), f"#/202H/content/{old['id']}")
        history = page.locator("[data-script-versions]")
        history.locator(":scope > summary").click()
        expect(history.locator("[data-script-version]")).to_have_count(2)
        expect(history.locator(f'[data-script-version="{independent["id"]}"]')).to_have_count(0)
        latest = history.locator(f'[data-script-version="{newer["id"]}"]')
        latest.locator(":scope > summary").click()
        expect(latest).to_contain_text(newer["text"])
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))

    def test_edit_in_content_automatically_retains_original_and_production_association(self):
        old = self.create_script("Original adopted words, already in production.")
        app = self.server.app
        job = app.create_production_job({"request_id": uuid4().hex,
            "revision": app.production.public()["revision"], "job": {"script_id": old["id"]}})["job"]
        page = self.open_page(f"#/202H/content/{old['id']}", width=390)
        page.locator("#script-more-actions > summary").click()
        page.locator("#script-derive").click()
        dialog = page.locator("#script-editor-dialog")
        dialog.locator('[name="text"]').fill("Revised adopted words for the next production.")
        dialog.get_by_role("button", name="保存采用稿", exact=True).click()
        expect(page.locator("#content-adopted-script .adopted-copy")).to_contain_text("Revised adopted words")
        newest = self.catalog()["scripts"][-1]
        self.assertEqual((newest["parent_id"], newest["version"]), (old["id"], 2))
        self.assertEqual(app.creation.get_script(old["id"])["text"], old["text"])
        self.assertEqual(app.production.job(job["id"])["script_id"], old["id"])
        history = page.locator("[data-script-versions]")
        history.locator(":scope > summary").click()
        original = history.locator(f'[data-script-version="{old["id"]}"]')
        original.locator(":scope > summary").click()
        expect(original).to_contain_text(old["text"])
        expect(original.locator(f'a[href$="/production/{job["id"]}"]')).to_have_count(1)
        folder = Path(__file__).resolve().parents[1] / ".test-artifacts"
        folder.mkdir(exist_ok=True)
        for scheme in ("light", "dark"):
            page.emulate_media(color_scheme=scheme)
            page.screenshot(path=str(folder / f"adopted-history-{scheme}-mobile.png"), full_page=True)
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))

    def test_archived_draft_stays_readable_in_history_and_restore_keeps_its_text(self):
        old = self.create_script("Unique archived original words.")
        latest = self.derive(old)
        code, _, result = self.request("POST", f'/api/scripts/{old["id"]}/status', {
            "request_id": uuid4().hex, "revision": self.catalog()["revision"], "script_status": {"archived": True}})
        self.assertEqual(code, 200, result)
        page = self.open_page(f"#/202H/content/{latest['id']}")
        history = page.locator("[data-script-versions]")
        history.locator(":scope > summary").click()
        archived = history.locator(f'[data-script-version="{old["id"]}"]')
        archived.locator(":scope > summary").click()
        expect(archived).to_contain_text("已归档")
        expect(archived).to_contain_text(old["text"])
        archived.get_by_role("link", name="查看这版内容").click()
        page.locator("#script-more-actions > summary").click()
        page.locator("#script-archive").click()
        page.locator('#script-editor-dialog [data-submit]').click()
        page.locator("#script-editor-dialog").wait_for(state="detached")
        restored = self.server.app.creation.get_script(old["id"])
        self.assertFalse(restored["archived"])
        self.assertEqual((restored["status"], restored["text"]), ("adopted", old["text"]))


if __name__ == '__main__':
    unittest.main()
