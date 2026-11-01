"""Expanded production records show current completion facts."""
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from playwright.sync_api import expect
import test_content_flow_ui as content
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

    def test_publish_view_row_is_one_line_that_opens_the_publish_panel(self):
        job = self.finished_job("尚未关联图文的视频")
        page = self.content_page()
        self.view(page, "publish")
        row = page.locator(f'[data-content-id="video:{job["id"]}"]')
        self.assertLessEqual(row.bounding_box()["height"], 44)
        expect(row.locator(".flow-chain")).to_have_count(0)
        # 待发布 names neither the shared stage nor the shared next step; the title opens the publish panel.
        expect(row.locator(".flow-stage, .flow-next")).to_have_count(0)
        self.assertIn("step=publish", row.locator(".flow-title").get_attribute("href"))
        expect(row.locator(".flow-cal")).to_have_text("未上传日历")
        expect(row.locator(".flow-menu")).to_have_count(1)

if __name__ == "__main__":
    unittest.main()
