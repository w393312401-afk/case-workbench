"""Script-to-production choices use read-only checks and explicit creation in a disposable library."""
import json
import unittest
from types import SimpleNamespace
from uuid import uuid4

from elevenlabs_client import ElevenLabsClient
from test_production import FakeEleven
import test_phase4_writing_ui as writing


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

    def test_empty_script_offers_both_formats_without_creating_until_confirmation(self):
        page = self.detail_page(width=390)
        self.assertEqual(page.locator("#script-production-toggle").inner_text(), "开始制作")
        self.assertFalse(page.locator("#script-produce").is_visible())
        self.assertFalse(page.locator("#script-case").is_visible())
        page.locator("#script-production-toggle").click()
        page.locator("#script-produce").click()
        page.locator(".production-dialog[open]").wait_for()
        self.assertEqual(self.app.production.public()["jobs"], [])
        page.locator('.production-dialog [data-close]').first.click()
        page.locator("#script-produce-carousel").click()
        page.locator(".production-dialog[open]").wait_for()
        self.assertIn("用图文技能生成逐张表", page.locator(".production-dialog").inner_text())
        self.assertEqual(self.app.production.public()["carousels"], [])
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))

    def test_carousel_goes_through_the_photo_skill_table_before_a_job_is_created(self):
        from pathlib import Path
        from test_ai_tasks import FakeBridge
        from test_production_carousel_table import TABLE
        photo = Path(self.app.edit_skill_dir).parent / "tiktok-photo-commerce"
        (photo / "references").mkdir(parents=True, exist_ok=True)
        (photo / "SKILL.md").write_text("---\nname: tiktok-photo-commerce\n---\n# 图文", encoding="utf-8")
        bridge = FakeBridge(output=TABLE)
        self.app.ai.bridge = bridge
        page = self.detail_page()
        page.locator("#script-production-toggle").click()
        page.locator("#script-produce-carousel").click()
        page.locator(".production-dialog[open] [data-submit]").click()
        page.get_by_text("核对图文逐张表").wait_for(timeout=15000)
        self.assertEqual(self.app.production.public()["carousels"], [])
        self.assertEqual([item["name"] for item in bridge.calls[-1]["skills"]], ["tiktok-photo-commerce"])
        self.assertIn("Hook 1", page.locator(".production-dialog").inner_text())
        page.locator(".production-dialog[open] [data-submit]").click()
        for _ in range(150):
            if self.app.production.public()["carousels"]:
                break
            page.wait_for_timeout(100)
        carousels = self.app.production.public()["carousels"]
        self.assertEqual((len(carousels), carousels[0]["script_id"]), (1, self.script["id"]))

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
        self.assertTrue(page.locator("#script-produce-carousel").is_visible())
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
        page.locator("#script-production-toggle").click()
        page.locator(".script-production-link").click()
        page.locator("#job-archive").wait_for()
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
