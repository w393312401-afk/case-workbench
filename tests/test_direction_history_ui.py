"""Historical variant records stay inspectable without adding first-screen noise.

The HTTP server, case library, and browser are disposable fixtures. Only the
progress response is mocked; no live case or production service is contacted.
"""
import unittest

import test_direction_workflow_ui as workflow_ui


class DirectionHistoryBrowserTests(unittest.TestCase):
    setUpClass = classmethod(workflow_ui.DirectionWorkflowBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(workflow_ui.DirectionWorkflowBrowserTests.tearDownClass.__func__)
    setUp = workflow_ui.DirectionWorkflowBrowserTests.setUp
    stop_server = workflow_ui.DirectionWorkflowBrowserTests.stop_server
    request = workflow_ui.DirectionWorkflowBrowserTests.request
    make_playable_video = workflow_ui.DirectionWorkflowBrowserTests.make_playable_video
    catalog = workflow_ui.DirectionWorkflowBrowserTests.catalog
    create_point = workflow_ui.DirectionWorkflowBrowserTests.create_point
    open_page = workflow_ui.DirectionWorkflowBrowserTests.open_page
    detail_data = workflow_ui.DirectionWorkflowBrowserTests.detail_data
    progress_data = workflow_ui.DirectionWorkflowBrowserTests.progress_data
    show_detail = workflow_ui.DirectionWorkflowBrowserTests.show_detail
    wait_for_progress = workflow_ui.DirectionWorkflowBrowserTests.wait_for_progress
    detail_pattern = workflow_ui.DirectionWorkflowBrowserTests.detail_pattern
    progress_pattern = workflow_ui.DirectionWorkflowBrowserTests.progress_pattern

    def history_page(self, width=1280):
        page = self.open_page(width=width)
        data = self.detail_data()
        progress = self.progress_data(data, {"kind": "test", "label": "建立测试卡", "video_id": "7664986913497009439"})
        row = progress["variants"][0]
        notes = {
            "scripts": "核对采用正文、冻结方案与链条一致；保留 <旧稿> & 原始来源。",
            "productions": "制作单明确引用同一文案，按 script_id 继承已核对归属。",
            "videos": "案例当前修订和完整方向链一致，准确视频 ID 已核对；发布日期未知。",
        }
        row["records"] = {
            "tasks": [], "publications": [], "plans": [],
            "scripts": [{"id": "SCR-history-ui", "title": "历史已采用文案", "status": "adopted",
                         "association": {"id": "VL-script", "evidence": {"note": notes["scripts"], "refs": []}}}],
            "productions": [{"id": "PRD-history-ui", "kind": "video", "title": "历史制作单", "status": "draft",
                             "script_id": "SCR-history-ui", "association": {"id": "VL-script",
                                 "inherited_from": "SCR-history-ui", "evidence": {"note": notes["productions"], "refs": []}}}],
            "videos": [{"id": "7664986913497009439", "video_id": "7664986913497009439",
                        "association": {"id": "VL-video", "evidence": {"note": notes["videos"], "refs": []}}}],
        }
        row["stages"]["publishing"] = {"status": "attention", "label": "已关联 1 条视频", "count": 1}
        row["stages"]["production"] = {"status": "working", "label": "制作中", "count": 1}
        row["counts"].update(scripts=1, productions=1, videos=1, publications=0)
        progress["summary"].update(variants_with_scripts=1, variants_in_production=1, variants_with_videos=1,
                                   variants_published=0, unassigned_count=0)
        progress["unassigned"] = {name: [] for name in row["records"]}
        page.route(self.progress_pattern, lambda route: route.fulfill(status=200, json=progress))
        self.show_detail(page, data)
        self.wait_for_progress(page)
        return page, notes

    def expand_history(self, page):
        details = page.locator('details.dir-variant[data-variant-index="0"]')
        self.assertIsNone(details.get_attribute("open"))
        details.locator(":scope > summary").click()
        records = details.locator('[data-variant-records="0"]')
        records.wait_for()
        return records

    def test_linked_historical_video_opens_exact_video_performance_route(self):
        page, _ = self.history_page(width=390)
        flow = page.locator('[data-variant-progress="0"]')
        self.assertIn("已关联 1 条视频", flow.inner_text())
        self.assertNotIn("已登记 1 条发布", flow.inner_text())
        records = self.expand_history(page)
        video = records.get_by_role("link", name="7664986913497009439", exact=True)
        self.assertEqual(video.get_attribute("href"), "#/202H/videos?video=7664986913497009439")
        self.assertEqual(records.get_by_text("发布登记 · 1", exact=True).count(), 0)
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth>document.documentElement.clientWidth"))
        video.click()
        page.locator("#perf-q").wait_for()
        self.assertEqual(page.evaluate("location.hash"), "#/202H/videos?video=7664986913497009439")
        self.assertEqual(page.locator("#perf-q").input_value(), "7664986913497009439")

    def test_each_record_has_its_own_collapsed_evidence_and_preserves_plain_text(self):
        page, notes = self.history_page()
        records = self.expand_history(page)
        disclosure = records.locator("details.dir-record-association")
        self.assertEqual(disclosure.count(), 3)
        for index, kind in enumerate(("scripts", "productions", "videos")):
            with self.subTest(kind=kind):
                item = disclosure.nth(index)
                self.assertIsNone(item.get_attribute("open"))
                self.assertFalse(item.locator("p").is_visible())
                summary = item.locator(":scope > summary")
                summary.focus()
                summary.press("Enter")
                self.assertIsNotNone(item.get_attribute("open"))
                self.assertEqual(item.locator("p").inner_text(), notes[kind])
                self.assertEqual(item.locator("p > *").count(), 0, "Evidence notes must be rendered as text")
                summary.press("Enter")
                self.assertIsNone(item.get_attribute("open"))

    def test_inherited_production_explains_ownership_and_links_to_source_script(self):
        page, _ = self.history_page(width=390)
        records = self.expand_history(page)
        production = records.locator("li").filter(has=page.get_by_role("link", name="历史制作单", exact=True))
        self.assertEqual(production.count(), 1)
        self.assertEqual(production.get_by_role("link", name="历史制作单", exact=True).get_attribute("href"),
                         "#/202H/production/PRD-history-ui")
        evidence = production.locator("details.dir-record-association")
        self.assertIn("沿用文案归属", evidence.locator(":scope > summary").inner_text())
        self.assertFalse(evidence.get_by_role("link", name="查看来源文案", exact=True).is_visible())
        evidence.locator(":scope > summary").click()
        source = evidence.get_by_role("link", name="查看来源文案", exact=True)
        self.assertTrue(source.is_visible())
        self.assertEqual(source.get_attribute("href"), "#/202H/scripts/SCR-history-ui")
        self.assertEqual(records.get_by_role("link", name="历史已采用文案", exact=True).get_attribute("href"),
                         source.get_attribute("href"))
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth>document.documentElement.clientWidth"))


if __name__ == "__main__":
    unittest.main()
