"""Expanded production records show current completion facts for both delivery formats."""
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from playwright.sync_api import expect
import test_content_flow_ui as content
from test_production_carousel import CANDIDATE
from variant_parse import carousel_text, parse_carousels
from direction_store import fingerprint
from workbench_video_publish import reviewed_source


class ContentWorkflowBrowserTests(unittest.TestCase):
    setUpClass = classmethod(content.ContentFlowBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(content.ContentFlowBrowserTests.tearDownClass.__func__)
    setUp = content.ContentFlowBrowserTests.setUp
    stop_server = content.ContentFlowBrowserTests.stop_server
    request = content.ContentFlowBrowserTests.request
    open_page = content.ContentFlowBrowserTests.open_page
    content_page = content.ContentFlowBrowserTests.content_page
    script = content.ContentFlowBrowserTests.script
    finished_job = content.ContentFlowBrowserTests.finished_job
    view = content.ContentFlowBrowserTests.view
    screenshot = content.ContentFlowBrowserTests.screenshot

    def carousel(self, job=None):
        app = self.server.app
        source = {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 1,
                  "product": "202H", "case_id": None, "candidate_text": CANDIDATE,
                  "candidate_sha256": "fixture", "snapshot": {}}
        script = app.creation.mutate("script", {"request_id": uuid4().hex,
            "revision": app.creation.public()["revision"], "script": {"product": "202H", "title": "关联图文待制作",
            "text": carousel_text(parse_carousels(CANDIDATE)[0]), "confirmed": True}}, source)["script"]
        item = app.create_carousel({"request_id": uuid4().hex, "revision": app.production.public()["revision"],
            "carousel": {"script_id": script["id"]}})["carousel"]
        if job:
            catalog = app.production.read()
            row = next(row for row in catalog["carousels"] if row["id"] == item["id"])
            row["source"]["video_job_id"] = job["id"]
            app.production.write(catalog)
        return item

    def test_completed_edit_keeps_publish_empty_and_shows_linked_carousel_chain(self):
        job = self.finished_job("整条视频链路的真实进度")
        carousel = self.carousel(job)
        independent = self.carousel()
        page = self.content_page()
        self.view(page, "all")
        row = page.locator(f'[data-content-id="video:{job["id"]}"]')
        expect(row.locator(".flow-record-meta")).to_contain_text("个意群")
        self.assertGreater(row.bounding_box()["height"], 100)
        video = row.locator('[data-workflow-kind="video"]')
        expect(video.locator('.flow-chain-step[data-done="true"]')).to_have_count(4)
        expect(video.locator('[data-step-key="publish"]')).to_have_attribute("data-done", "false")
        expect(video.locator(".flow-chain-result")).to_have_text("视频链未完结")
        linked = row.locator(f'[data-workflow-id="{carousel["id"]}"]')
        expect(linked).to_have_attribute("data-linked", "true")
        expect(linked.locator(".flow-chain-step")).to_have_count(7)
        expect(row.locator(f'[data-workflow-id="{independent["id"]}"]')).to_have_count(0)
        expect(page.locator(f'[data-content-id="carousel:{independent["id"]}"] .flow-chain-step')).to_have_count(7)
        expect(page.locator(".scripts-tabs")).to_have_count(0)
        self.screenshot(page, "content-workflows-light-desktop.png")
        page.emulate_media(color_scheme="dark")
        self.screenshot(page, "content-workflows-dark-desktop.png")
        page.set_viewport_size({"width": 390, "height": 900})
        page.locator(".flow-main-menu > summary").click()
        page.locator("#content-flow-batch-open").click()
        page.locator(f'[data-flow-select="video:{job["id"]}"]').check()
        expect(page.locator("#content-flow-batch")).to_contain_text("已选 1 项")
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))
        self.screenshot(page, "content-workflows-dark-mobile.png")
        page.emulate_media(color_scheme="light")
        self.screenshot(page, "content-workflows-light-mobile.png")

    def test_unlinked_video_shows_that_no_carousel_was_created(self):
        job = self.finished_job("尚未关联图文的视频")
        page = self.content_page()
        self.view(page, "publish")
        row = page.locator(f'[data-content-id="video:{job["id"]}"]')
        expect(row.locator('.flow-workflow[data-workflow-kind="carousel"]')).to_contain_text("尚未关联图文")
        expect(row.locator('.flow-workflow[data-workflow-kind="video"] .flow-chain-step')).to_have_count(6)
        expect(row.locator(".flow-record-actions .flow-stage")).to_have_text("待发布")
        expect(row.locator(".flow-next")).to_be_visible()

    def test_current_video_publication_completes_video_without_completing_its_carousel(self):
        app = self.server.app
        job = self.finished_job("已发布视频与未发布图文独立显示")
        carousel = self.carousel(job)
        publication = {"post_id": "7999999999999999917", "published_on": "2026-10-20",
                       "source_signature": fingerprint(reviewed_source(app.production.job(job["id"])))}
        publications = app._video_publications()
        with patch.object(app, "_video_publications", return_value=publications), patch.object(publications, "read", return_value={"jobs": {job["id"]: {"publication": publication}}}):
            page = self.content_page()
            self.view(page, "all")
            row = page.locator(f'[data-content-id="video:{job["id"]}"]')
            video = row.locator('[data-workflow-kind="video"]')
            expect(video).to_have_attribute("data-complete", "true")
            expect(video.locator('.flow-chain-step[data-done="true"]')).to_have_count(5)
            expect(video.locator(".flow-chain-result")).to_have_text("视频链已完结")
            linked = row.locator(f'[data-workflow-id="{carousel["id"]}"]')
            expect(linked).to_have_attribute("data-complete", "false")
            expect(linked.locator(".flow-chain-result")).to_have_text("图文链未完结")

    def test_incomplete_filter_preserves_explicit_formats_and_survives_reload(self):
        app = self.server.app
        waiting = self.finished_job("未发布的视频仍未完结")
        published = self.finished_job("视频已发布但关联图文未完结")
        carousel = self.carousel(published)
        self.script("尚未进入制作的脚本")
        publication = {"post_id": "7999999999999999918", "published_on": "2026-10-20",
                       "source_signature": fingerprint(reviewed_source(app.production.job(published["id"])))}
        publications = app._video_publications()
        with patch.object(app, "_video_publications", return_value=publications), patch.object(publications, "read", return_value={"jobs": {published["id"]: {"publication": publication}}}):
            page = self.content_page()
            self.view(page, "all")
            content.set_content_condition(page, "kind", ["video"])
            # Adding a status retains the explicit video condition; no hidden format reset.
            content.set_content_condition(page, "status", ["workflow_incomplete"])
            expect(page.locator("#content-flow-list")).to_have_attribute("data-view", "all")
            expect(page.locator(".flow-row")).to_have_count(1)
            expect(page.locator(f'[data-content-id="video:{waiting["id"]}"]')).to_be_visible()
            expect(page.locator(f'[data-content-id="carousel:{carousel["id"]}"]')).to_have_count(0)
            self.assertEqual(content.content_filter(page), {"match": "all", "conditions": [
                {"field": "kind", "values": ["video"]},
                {"field": "status", "values": ["workflow_incomplete"]}]})
            # Selecting two forms includes both without discarding the status condition.
            content.set_content_condition(page, "kind", ["video", "carousel"])
            expect(page.locator(".flow-row")).to_have_count(2)
            expect(page.locator(f'[data-content-id="video:{waiting["id"]}"]')).to_be_visible()
            expect(page.locator(f'[data-content-id="carousel:{carousel["id"]}"]')).to_be_visible()
            expect(page.locator(f'[data-content-id="video:{published["id"]}"]')).to_have_count(0)
            expected_filter = {"match": "all", "conditions": [
                {"field": "kind", "values": ["video", "carousel"]},
                {"field": "status", "values": ["workflow_incomplete"]}]}
            self.assertEqual(content.content_filter(page), expected_filter)
            before = page.evaluate("location.hash")
            page.locator(f'[data-content-id="video:{waiting["id"]}"] .flow-title').click()
            expect(page.locator("#navigate-up")).to_have_attribute("href", before)
            page.locator("#navigate-up").click()
            expect(page.locator(".flow-row")).to_have_count(2)
            self.assertEqual(content.content_filter(page), expected_filter)
            page.reload()
            expect(page.locator(".flow-row")).to_have_count(2)
            self.assertEqual(content.content_filter(page), expected_filter)
            content.set_content_condition(page, "kind", ["carousel"])
            expect(page.locator(".flow-row")).to_have_count(1)
            expect(page.locator(f'[data-content-id="carousel:{carousel["id"]}"]')).to_be_visible()
            page.reload()
            expect(page.locator(".flow-row")).to_have_count(1)
            self.assertEqual(content.content_filter(page), {"match": "all", "conditions": [
                {"field": "kind", "values": ["carousel"]},
                {"field": "status", "values": ["workflow_incomplete"]}]})


if __name__ == "__main__":
    unittest.main()
