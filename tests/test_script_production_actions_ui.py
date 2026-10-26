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

    def test_empty_script_offers_video_and_creates_only_after_confirmation(self):
        page = self.detail_page(width=390)
        self.assertEqual(page.locator("#script-production-toggle").inner_text(), "开始制作")
        self.assertFalse(page.locator("#script-produce").is_visible())
        self.assertFalse(page.locator("#script-case").is_visible())
        page.locator("#script-production-toggle").click()
        expect(page.locator("#script-produce-carousel")).to_have_count(0)
        page.locator("#script-produce").click()
        page.locator(".production-dialog[open]").wait_for()
        self.assertEqual(self.app.production.public()["jobs"], [])
        page.locator('.production-dialog [data-close]').first.click()
        page.locator("#script-produce").click()
        page.locator(".production-dialog[open] [data-submit]").click()
        page.locator("#job-archive").wait_for(state="attached")
        jobs = self.app.production.public()["jobs"]
        self.assertEqual((len(jobs), jobs[0]["script_id"]), (1, self.script["id"]))
        self.assertEqual(self.app.production.public()["carousels"], [])
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))

    def test_existing_carousel_resumes_without_a_new_carousel_entry_or_creation(self):
        from test_production_carousel import CANDIDATE
        from variant_parse import carousel_text, parse_carousels
        source = {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 1,
                  "product": "202H", "case_id": None, "candidate_text": CANDIDATE,
                  "candidate_sha256": "x", "snapshot": {}}
        self.script = self.app.creation.mutate("script", {"request_id": uuid4().hex,
            "revision": self.app.creation.current_revision(), "script": {"product": "202H", "title": "已有图文",
            "text": carousel_text(parse_carousels(CANDIDATE)[0]), "confirmed": True}}, source)["script"]
        carousel = self.app.create_carousel({"request_id": uuid4().hex,
            "revision": self.app.production.current_revision(), "carousel": {"script_id": self.script["id"]}})["carousel"]
        page = self.detail_page()
        expect(page.locator("#script-production-toggle")).to_have_text("继续制作")
        page.locator("#script-production-toggle").click()
        expect(page.locator("#script-produce-carousel")).to_have_count(0)
        page.locator(".script-production-link").click()
        page.locator("#carousel-archive").wait_for(state="attached")
        self.assertEqual(page.evaluate("location.hash"), f'#/202H/production/{carousel["id"]}')
        carousels = self.app.production.public()["carousels"]
        self.assertEqual((len(carousels), carousels[0]["script_id"]), (1, self.script["id"]))
        self.assertEqual(self.app.production.public()["jobs"], [])

    def test_multiple_jobs_only_offer_exact_active_script_links_and_explicit_new_version(self):
        listing = {"jobs": [self.item("PRD-111111111111", status="awaiting_review"),
                            self.item("PRD-222222222222", archived=True),
                            self.item("PRD-333333333333", script_id="SCR-unrelated")],
                   "carousels": [self.item("CAR-111111111111", status="approved"),
                                 self.item("CAR-222222222222", archived=True),
                                 self.item("CAR-333333333333", script_id="SCR-unrelated")]}
        page = self.detail_page(listing, width=390)
        self.assertEqual(page.locator("#script-production-toggle").inner_text(), "继续制作")
        page.locator("#script-production-toggle").click()
        links = page.locator(".script-production-link")
        self.assertEqual(links.count(), 2)
        self.assertEqual(links.nth(0).get_attribute("href"), "#/202H/production/PRD-111111111111")
        self.assertEqual(links.nth(1).get_attribute("href"), "#/202H/production/CAR-111111111111")
        self.assertIn("待人工审查 · 去审查", links.nth(0).inner_text())
        self.assertNotIn("领取", links.nth(0).inner_text())
        self.assertFalse(page.locator("#script-produce").is_visible())
        page.locator(".script-production-new > summary").click()
        self.assertTrue(page.locator("#script-produce").is_visible())
        expect(page.locator("#script-produce-carousel")).to_have_count(0)
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))
        self.assertEqual(self.app.production.public()["jobs"], [])

    def test_failed_or_incomplete_lookup_requires_retry_before_new_creation(self):
        for payload, status in (({"error": "临时读取失败"}, 503), ({"jobs": []}, 200)):
            with self.subTest(status=status):
                page = self.open_page()
                calls = []

                def listing(route):
                    calls.append(True)
                    route.fulfill(status=status if len(calls) == 1 else 200,
                                  content_type="application/json",
                                  body=json.dumps(payload if len(calls) == 1 else {"jobs": [], "carousels": []}))

                page.route("**/api/production?product=*", listing)
                page.evaluate("id => location.hash = '#/202H/scripts/' + id", self.script["id"])
                page.get_by_role("button", name="重新读取制作单", exact=True).wait_for()
                self.assertIn("暂时无法核对已有制作单", page.locator("#script-production-note").inner_text())
                self.assertEqual(page.locator("#script-produce").count(), 0)
                self.assertEqual(page.locator("#script-produce-carousel").count(), 0)
                page.get_by_role("button", name="重新读取制作单", exact=True).click()
                page.get_by_role("button", name="开始制作", exact=True).wait_for()
                self.assertFalse(page.locator("#script-production-note").is_visible())
                self.assertEqual(self.app.production.public()["jobs"], [])

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
