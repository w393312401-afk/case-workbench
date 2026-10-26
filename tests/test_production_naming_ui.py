"""Video names stay consistent across the content list and production detail."""
from __future__ import annotations

import json
import unittest

from playwright.sync_api import expect

from production_store import public_job
import test_production_queue_ui as queue
import test_production_validation_ui as detail


class ProductionNamingQueueBrowserTests(unittest.TestCase):
    setUpClass = classmethod(queue.ProductionQueueBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(queue.ProductionQueueBrowserTests.tearDownClass.__func__)
    setUp = queue.ProductionQueueBrowserTests.setUp
    stop_server = queue.ProductionQueueBrowserTests.stop_server
    request = queue.ProductionQueueBrowserTests.request
    make_playable_video = queue.ProductionQueueBrowserTests.make_playable_video
    job = queue.ProductionQueueBrowserTests.job
    carousel = queue.ProductionQueueBrowserTests.carousel
    catalog = queue.ProductionQueueBrowserTests.catalog
    open_queue = queue.ProductionQueueBrowserTests.open_queue
    row = queue.ProductionQueueBrowserTests.row

    def named_job(self, number, topic):
        job = self.job(number)
        job.update(title="202H-S2 变体 1", direction_id="202H-S2", selected_take="take-chosen",
                   lines=[{"number": 1, "zh": topic, "spoken": "One complete kit."}],
                   timelines=[{"name": f"Zack261021-202-{number}", "unique_id": f"uid-{number}",
                               "take_id": "take-chosen"}])
        return job

    def test_topics_name_the_content_and_timeline_names_remain_searchable_metadata(self):
        first = self.named_job(1, "一块石头能磨锋利，镜面还需要后面的精修")
        second = self.named_job(2, "不想东拼西凑，就把完整路径装在一个箱子里")
        # A later build for another voiceover must not hide the chosen one's name.
        first["timelines"].append({"name": "Other261021-202-9", "unique_id": "uid-other", "take_id": "take-other"})
        page = self.open_queue(width=390)
        expect(self.row(page, first).locator(".flow-title")).to_have_text(
            "一块石头能磨锋利，镜面还需要后面的精修｜视频方案 1")
        expect(self.row(page, second).locator(".flow-title")).to_have_text(
            "不想东拼西凑，就把完整路径装在一个箱子里｜视频方案 1")
        expect(self.row(page, first).locator(".flow-record-meta")).to_contain_text("Zack261021-202-1")
        expect(self.row(page, second).locator(".flow-record-meta")).to_contain_text("Zack261021-202-2")
        page.locator("#content-flow-query").fill("Zack261021-202-2")
        expect(page.locator(".flow-row")).to_have_count(1)
        expect(self.row(page, second)).to_have_count(1)
        page.reload()
        expect(self.row(page, second).locator(".flow-title")).to_have_text(
            "不想东拼西凑，就把完整路径装在一个箱子里｜视频方案 1")
        page.locator("#content-flow-query").fill("202H-S2 变体 1")
        expect(page.locator(".flow-row")).to_have_count(2)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_unbuilt_video_and_carousel_share_the_topic_and_format_rule(self):
        draft = self.named_job(1, "未建时间线也能看到这条视频的主题")
        draft["timelines"] = []
        carousel = self.carousel(1, current=False)
        carousel["title"] = "202H-S2 图文保留原标题"
        escaped = self.named_job(2, "")
        escaped["title"] = "202H-S2 <img src=x onerror=window.__namingExecuted=1>"
        page = self.open_queue(width=390)
        expect(self.row(page, draft).locator(".flow-title")).to_have_text("未建时间线也能看到这条视频的主题｜视频方案 1")
        expect(self.row(page, carousel).locator(".flow-title")).to_have_text("图文保留原标题｜图文")
        self.assertIn("<img", self.row(page, escaped).locator(".flow-title").inner_text())
        self.assertEqual(page.locator(".flow-record-title img").count(), 0)
        self.assertFalse(page.evaluate("Boolean(window.__namingExecuted)"))
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)


class ProductionNamingDetailBrowserTests(unittest.TestCase):
    setUpClass = classmethod(detail.ProductionValidationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(detail.ProductionValidationBrowserTests.tearDownClass.__func__)
    setUp = detail.ProductionValidationBrowserTests.setUp
    stop_server = detail.ProductionValidationBrowserTests.stop_server
    request = detail.ProductionValidationBrowserTests.request
    make_playable_video = detail.ProductionValidationBrowserTests.make_playable_video
    open_page = detail.ProductionValidationBrowserTests.open_page

    def test_list_uses_topic_and_format_while_detail_keeps_the_timeline_name(self):
        job = self.payload["job"]
        job.update(title="202H-S2 变体 B", direction_id="202H-S2", selected_take="take-chosen",
                   lines=[{"number": 1, "spoken": "One stone is just the beginning.", "zh": "一块石头只是开始"}],
                   timelines=[{"name": "Zack261021-202-4", "unique_id": "uid-own", "take_id": "take-chosen",
                               "creator": "Zack", "markers": 1, "frames": 60, "fps": "29.97", "built_at": "2026-10-21"}])
        self.payload["job"] = public_job(job)
        page = self.open_page(width=390)
        expected = "Zack261021-202-4｜一块石头只是开始"
        expect(page.locator("[data-production-title]")).to_have_text(expected)
        page.route("**/api/content?*", lambda route: route.fulfill(status=200, content_type="application/json",
            body=json.dumps(queue.content_response(self.server.app, [job], [], route.request.url), ensure_ascii=False)))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/content?view=all")
        expect(page.locator(".flow-title")).to_have_text("一块石头只是开始｜视频方案 B")
        expect(page.locator(".flow-record-meta")).to_contain_text("Zack261021-202-4")
        page.locator(".flow-title").click()
        expect(page.locator("[data-production-title]")).to_have_text(expected)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_detail_without_a_timeline_uses_english_when_chinese_is_missing(self):
        job = self.payload["job"]
        job.update(title="202H-S2 变体 2", direction_id="202H-S2",
                   lines=[{"number": 1, "spoken": "Keep the complete sharpening path in one case.", "zh": ""}])
        self.payload["job"] = public_job(job)
        page = self.open_page(width=390)
        expect(page.locator("[data-production-title]")).to_have_text(
            "Keep the complete sharpening path in one case.")
        self.assertNotIn("｜", page.locator("[data-production-title]").inner_text())
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)


if __name__ == "__main__":
    unittest.main()
