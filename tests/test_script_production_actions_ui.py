"""Script-to-production choices use read-only checks and explicit creation in a disposable library."""
import json
import unittest
from types import SimpleNamespace
from uuid import uuid4

from elevenlabs_client import ElevenLabsClient
from test_production import FakeEleven
import test_phase4_writing_ui as writing
from playwright.sync_api import expect


class ScriptProductionActionsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(writing.WritingLifecycleBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(writing.WritingLifecycleBrowserTests.tearDownClass.__func__)
    stop_server = writing.WritingLifecycleBrowserTests.stop_server
    request = writing.WritingLifecycleBrowserTests.request
    catalog = writing.WritingLifecycleBrowserTests.catalog
    create_script = writing.WritingLifecycleBrowserTests.create_script
    open_page = writing.WritingLifecycleBrowserTests.open_page

    def setUp(self):
        writing.WritingLifecycleBrowserTests.setUp(self)
        self.app = self.server.app
        self.app.voices = ElevenLabsClient(key_provider=lambda: "sk_test", transport=FakeEleven())
        self.app.resolve = SimpleNamespace(status=lambda: {"ok": False, "error": "隔离测试不连接达芬奇"})
        self.script = self.create_script()

    def detail_page(self, listing=None, width=1280):
        page = self.open_page(width=width)
        if listing is not None:
            page.route("**/api/production?product=*", lambda route: route.fulfill(
                status=200, content_type="application/json", body=json.dumps(listing)))
        page.evaluate("id => location.hash = '#/202H/scripts/' + id", self.script["id"])
        page.locator("#script-production-toggle:not([disabled])").wait_for()
        return page

    def item(self, identifier, **overrides):
        return {"id": identifier, "script_id": self.script["id"], "product": "202H",
                "title": "同一条脚本", "status": "draft", "archived": False, **overrides}

    def test_existing_job_resumes_without_creation(self):
        job = self.app.create_production_job({"request_id": uuid4().hex,
            "revision": self.app.production.public()["revision"],
            "job": {"script_id": self.script["id"]}})["job"]
        page = self.detail_page()
        # With an order, the video ID is registered in its publish step; the script page points there.
        release = page.locator("#script-release")
        expect(release).to_have_text("去制作单登记发布")
        page.locator("#script-release-manual").wait_for()
        page.locator("#script-production-toggle").click()
        page.locator(".script-production-link").click()
        page.locator("#job-archive").wait_for(state="attached")
        self.assertEqual(page.evaluate("location.hash"), f'#/202H/production/{job["id"]}')
        self.assertEqual(len(self.app.production.public()["jobs"]), 1)

    def test_more_actions_preserve_derivation_and_case_workflows(self):
        page = self.detail_page()
        self.assertFalse(page.locator("#script-derive").is_visible())
        self.assertTrue(page.locator("#script-release").is_visible())
        page.locator("#script-more-actions > summary").click()
        page.locator("#script-derive").click()
        page.locator("#script-editor-dialog[open]").wait_for()
        self.assertEqual(page.locator('#script-editor-dialog [name="text"]').input_value(), self.script["text"])
        page.locator('#script-editor-dialog [data-close]').first.click()
        page.locator("#script-more-actions > summary").click()
        page.locator("#script-case").click()
        page.locator('#script-editor-dialog [name="conclusion"]').wait_for()
        self.assertEqual(self.app.production.public()["jobs"], [])


if __name__ == "__main__":
    unittest.main()
