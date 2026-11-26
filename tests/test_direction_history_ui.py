"""Historical variant records no longer list under each angle on the direction page.

Since 2026-11-22 an angle shows its structure, change and orders only; scripts,
production orders and videos are read on their own pages (the related-content
fold still lists the direction's scripts and videos). The HTTP server, case
library and browser are disposable fixtures; only the progress response is mocked.
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

    def test_angle_rows_keep_orders_and_leave_record_lists_and_evidence_to_their_pages(self):
        for width in (1280, 390):
            with self.subTest(width=width):
                page, notes = self.history_page(width=width)
                details = page.locator('details.dir-variant[data-variant-index="0"]')
                details.locator(":scope > summary").click()
                self.assertEqual(page.locator("[data-variant-records], details.dir-record-association, [data-variant-state]").count(), 0)
                for note in notes.values():
                    self.assertNotIn(note, page.locator("#direction-variants").text_content())
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth>document.documentElement.clientWidth"))
                page.context.close()


if __name__ == "__main__":
    unittest.main()
