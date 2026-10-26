"""TK cards use automatic direction links without reopening the direction wizard.

All records live in disposable stores; controlled candidate responses exercise
production state and association failures independently.
"""
from __future__ import annotations

import copy
import unittest
from uuid import uuid4

from playwright.sync_api import expect

import test_direction_catalog_ui as catalog_ui
import test_tk_candidates as candidate_http
from test_tk_candidates import CARD, REFERENCE
from tk_candidates import clean_direction, suggestion


class TKCandidatesBrowserTests(unittest.TestCase):
    setUpClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.tearDownClass.__func__)
    stop_server = catalog_ui.DirectionCatalogBrowserTests.stop_server
    request = catalog_ui.DirectionCatalogBrowserTests.request
    make_playable_video = catalog_ui.DirectionCatalogBrowserTests.make_playable_video
    catalog = catalog_ui.DirectionCatalogBrowserTests.catalog
    create_point = catalog_ui.DirectionCatalogBrowserTests.create_point

    def setUp(self):
        catalog_ui.DirectionCatalogBrowserTests.setUp(self)
        self.candidates = []

    @staticmethod
    def candidate(identifier, status="new", **values):
        direction = clean_direction({**CARD, "id": "202H-S1", "name": identifier,
                                     "kind": "执行变体", "chain": ["S1 定角不用猜", "怕磨坏 → A2 定角", "收口：下面入手"]})
        return {"candidate_id": identifier, "product": "202H", "direction": direction,
                "reference": {"url": "https://www.tiktok.com/@knifeguy/video/" + REFERENCE["id"],
                              "author": "knifeguy", "title": REFERENCE["title"], "views": REFERENCE["views"]},
                "saved_at": "2026-10-21T02:00:00Z", "suggestion": suggestion(direction),
                "status": status, "direction_id": "202H-S1", "job_id": "", "writing_task_id": "", **values}

    def listing(self):
        return {"candidates": copy.deepcopy(self.candidates),
                "counts": {status: sum(item["status"] == status for item in self.candidates)
                           for status in ("new", "adopted", "dismissed", "writing", "produced")}}

    def open_page(self, width=1280):
        context = self.browser.new_context(viewport={"width": width, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        page.route("**/api/tk-candidates?*", lambda route: route.fulfill(json=self.listing()))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/directions")
        page.locator(".tk-candidates").wait_for()
        # Any accidental return to the wizard is an explicit browser failure.
        page.evaluate("() => {CaseDirections.openCreate = () => {throw new Error('candidate reopened direction wizard')}}")
        return page

    def create_job(self):
        app = self.server.app
        script = app.creation.mutate("script", {"request_id": uuid4().hex,
            "revision": app.creation.current_revision(), "script": {"product": "202H",
            "title": "已有候选制作单", "text": "Keep the angle steady.", "translation_zh": "保持角度。",
            "confirmed": True}})["script"]
        return app.create_production_job({"request_id": uuid4().hex,
            "revision": app.production.current_revision(), "job": {"script_id": script["id"]}})["job"]

    def test_associated_cards_keep_production_counts_and_existing_card_fields(self):
        self.candidates = [self.candidate("TKC-new"), self.candidate("TKC-produced", "produced", job_id="PRD-existing"),
                           self.candidate("TKC-writing", "writing", writing_task_id="AIT-existing")]
        page = self.open_page(width=390)
        section = page.locator(".tk-candidates")
        expect(section.locator("h2 small")).to_have_text("TK 看板 · 1 条待处理 · 1 条写稿中")
        expect(section.get_by_text("已归入 202H-S1", exact=True)).to_have_count(2)
        expect(section.locator("[data-tk-produce='TKC-new']")).to_have_text("直接建制作单")
        expect(section.locator("[data-tk-status='dismissed']")).to_have_count(1)
        expect(section.get_by_role("link", name="查看方向", exact=True)).to_have_count(2)
        expect(section.get_by_role("link", name="打开制作单", exact=True)).to_have_count(0)
        expect(section.locator(".tk-cand-footer")).to_contain_text("已处理 1 · 已忽略 0")
        section.get_by_role("button", name="显示已处理", exact=True).click()
        expect(section.get_by_role("link", name="打开制作单", exact=True)).to_have_attribute(
            "href", "#/202H/production/PRD-existing")
        self.assertNotIn("待补方向", section.inner_text())
        self.assertNotIn("补成方向", section.inner_text())
        self.assertNotIn("采纳为方向", section.inner_text())
        expect(section.locator("[data-tk-associate]")).to_have_count(0)
        expect(section.locator("select, input")).to_have_count(0)
        first = section.locator(".tk-cand").first
        expect(first).to_contain_text(CARD["audience"])
        expect(first).to_contain_text("S1 定角不用猜 → 怕磨坏 → A2 定角 → 收口：下面入手")
        first.locator("summary").click()
        expect(first.locator(".tk-cand-script")).to_contain_text(CARD["lines"][0]["en"])
        expect(first.locator(".tk-cand-script")).to_contain_text(CARD["lines"][0]["zh"])
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))
        posts = []
        page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
        first.get_by_role("link", name="查看方向", exact=True).click()
        page.locator("#direction-definition").wait_for()
        self.assertEqual(page.evaluate("location.hash"), "#/202H/directions/202H-S1")
        expect(page.locator("#direction-create-dialog[open]")).to_have_count(0)
        self.assertEqual(posts, [])

    def test_produced_and_legacy_adopted_open_the_existing_order_without_a_write(self):
        job = self.create_job()
        self.candidates = [self.candidate("TKC-produced", "produced", job_id=job["id"]),
                           self.candidate("TKC-legacy", "adopted", job_id=job["id"])]
        page = self.open_page()
        expect(page.locator(".tk-candidates .tk-cand")).to_have_count(0)
        expect(page.locator(".tk-candidates .tk-cand-footer")).to_contain_text("已处理 2 · 已忽略 0")
        posts = []
        page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
        page.get_by_role("button", name="显示已处理", exact=True).click()
        expect(page.locator(".tk-candidates").get_by_role("link", name="打开制作单", exact=True)).to_have_count(2)
        expect(page.locator(".tk-candidates [data-tk-produce]")).to_have_count(0)
        page.locator(".tk-candidates .tk-cand").last.get_by_role("link", name="打开制作单", exact=True).click()
        page.locator("#job-archive").wait_for(state="attached")
        self.assertEqual(page.evaluate("location.hash"), f'#/202H/production/{job["id"]}')
        self.assertEqual(len(self.server.app.production.public()["jobs"]), 1)
        self.assertEqual(posts, [])

    def test_association_retry_uses_saved_candidate_without_a_selection_form(self):
        self.candidates = [self.candidate("TKC-retry", "produced", direction_id="",
                                         direction_error="缺少可用的 S1 主打卖点，无法自动归入方向。")]
        page = self.open_page()
        requests = []

        def associate(route):
            requests.append(route.request.post_data_json)
            self.candidates[0].update(direction_id="202H-S1")
            self.candidates[0].pop("direction_error")
            route.fulfill(json={"candidate": copy.deepcopy(self.candidates[0])})

        page.route("**/api/tk-candidates/associate", associate)
        section = page.locator(".tk-candidates")
        expect(section.get_by_role("status")).to_have_text("缺少可用的 S1 主打卖点，无法自动归入方向。")
        section.get_by_role("button", name="重新关联", exact=True).click()
        expect(section.get_by_text("已归入 202H-S1", exact=True)).to_be_visible()
        expect(section.get_by_role("button", name="重新关联", exact=True)).to_have_count(0)
        expect(section.get_by_role("link", name="查看方向", exact=True)).to_have_attribute("href", "#/202H/directions/202H-S1")
        expect(section.locator("h2 small")).to_have_text("TK 看板 · 1 条待处理")
        expect(page.locator("#direction-create-dialog[open]")).to_have_count(0)
        self.assertEqual(requests, [{"candidate_id": "TKC-retry"}])

    def test_completed_cards_are_collapsed_on_reload_without_changing_production_status(self):
        self.candidates = [self.candidate("TKC-produced", "produced", job_id="PRD-existing"),
                           self.candidate("TKC-new-complete", "new", job_id="PRD-existing"),
                           self.candidate("TKC-legacy", "adopted", job_id="PRD-existing"),
                           self.candidate("TKC-ignored", "dismissed", job_id="PRD-existing")]
        page = self.open_page()
        section = page.locator(".tk-candidates")
        expect(section.locator(".tk-cand")).to_have_count(0)
        expect(section.locator("h2 small")).to_have_text("TK 看板 · 0 条待处理")
        expect(section.locator(".tk-cand-footer")).to_contain_text("已处理 3 · 已忽略 1")
        posts = []
        page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
        section.get_by_role("button", name="显示已处理", exact=True).click()
        expect(section.locator(".tk-cand.done")).to_have_count(4)
        expect(section.get_by_role("link", name="打开制作单", exact=True)).to_have_count(4)
        expect(section.get_by_role("button", name="移回待处理", exact=True)).to_have_count(0)
        expect(section.get_by_role("button", name="取消忽略", exact=True)).to_have_count(1)
        expect(section.locator("[data-tk-produce], [data-tk-status='dismissed']")).to_have_count(0)
        page.reload()
        expect(section.locator(".tk-cand")).to_have_count(0)
        self.assertEqual(posts, [])
        self.assertEqual([item["status"] for item in self.candidates], ["produced", "new", "adopted", "dismissed"])

    def test_only_incomplete_or_unresolved_work_stays_visible_and_retry_settles_the_card(self):
        self.candidates = [self.candidate("TKC-direction-only"),
                           self.candidate("TKC-job-only", "produced", direction_id="", job_id="PRD-existing"),
                           self.candidate("TKC-error", "produced", job_id="PRD-existing", direction_error="关联尚未补齐。"),
                           self.candidate("TKC-writing", "writing", job_id="PRD-old", writing_task_id="AIT-existing")]
        page = self.open_page()
        section = page.locator(".tk-candidates")
        expect(section.locator(".tk-cand")).to_have_count(4)
        expect(section.locator("h2 small")).to_have_text("TK 看板 · 3 条待处理 · 1 条写稿中")
        expect(section.get_by_role("link", name="查看写稿", exact=True)).to_have_count(1)
        def associate(route):
            self.candidates[2].pop("direction_error")
            route.fulfill(json={"candidate": copy.deepcopy(self.candidates[2])})
        page.route("**/api/tk-candidates/associate", associate)
        section.get_by_role("button", name="重新关联", exact=True).click()
        expect(section.locator(".tk-cand")).to_have_count(3)
        expect(section.locator("h2 small")).to_have_text("TK 看板 · 2 条待处理 · 1 条写稿中")
        expect(section.locator(".tk-cand-footer")).to_contain_text("已处理 1 · 已忽略 0")
        section.get_by_role("button", name="显示已处理", exact=True).click()
        expect(section.locator(".tk-cand.done")).to_have_count(1)
        expect(section.locator(".tk-cand.done")).to_contain_text("TKC-error")
        expect(page.locator("#direction-create-dialog[open]")).to_have_count(0)

    def test_retry_keeps_specific_unresolved_error_and_legacy_card_can_produce(self):
        self.candidates = [self.candidate("TKC-error", direction_id="", direction_error="缺少收口，无法自动关联。"),
                           self.candidate("TKC-legacy", "adopted")]
        job = self.create_job()
        page = self.open_page()
        requests = []
        page.route("**/api/tk-candidates/associate", lambda route: route.fulfill(
            json={"candidate": copy.deepcopy(self.candidates[0])}))
        page.get_by_role("button", name="重新关联", exact=True).click()
        expect(page.locator(".tk-candidates [role='status']")).to_have_text("缺少收口，无法自动关联。")
        expect(page.locator(".tk-candidates [data-tk-associate]:not([disabled])")).to_have_count(1)
        page.get_by_role("button", name="显示已处理", exact=True).click()

        def produce(route):
            requests.append(route.request.post_data_json)
            route.fulfill(json={"job": job, "reused": False})

        page.route("**/api/tk-candidates/produce", produce)
        page.locator("[data-tk-produce='TKC-legacy']").click()
        page.locator("#job-archive").wait_for(state="attached")
        self.assertEqual(requests, [{"candidate_id": "TKC-legacy"}])
        self.assertEqual(page.evaluate("location.hash"), f'#/202H/production/{job["id"]}')
        expect(page.locator("#direction-create-dialog[open]")).to_have_count(0)

    def test_new_plugin_save_automatically_builds_and_collapses_a_fresh_record(self):
        for code in ("S8", "A2", "B4"):
            self.create_point(code, code + " 测试卖点")
        silent = {**CARD, "name": "缺英文的草稿", "lines": [{"zh": "只有中文"}], "cta": {"zh": "现在入手"}}
        body = {"product": "202H", "reference": REFERENCE, "directions": [silent, CARD]}
        status, _, saved = candidate_http.CandidateHTTPTests.extension(self, "/api/public-stats/direction-candidates", body)
        self.assertEqual(status, 200, saved)
        pending, done = saved["candidates"]
        self.assertTrue(done["job_id"])
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/directions")
        section = page.locator(".tk-candidates")
        expect(section.locator(".tk-cand")).to_have_count(1)
        expect(section.locator(".tk-cand")).to_contain_text("缺英文的草稿")
        expect(section.get_by_role("status")).to_contain_text("没有英文口播")
        expect(section.locator(".tk-cand-footer")).to_contain_text("已处理 1 · 已忽略 0")
        expect(section.locator("h2 small")).to_have_text("TK 看板 · 1 条待处理")
        section.get_by_role("button", name="显示已处理", exact=True).click()
        handled = section.locator(".tk-cand.done")
        expect(handled).to_have_count(1)
        expect(handled.get_by_role("link", name="打开制作单", exact=True)).to_have_attribute(
            "href", "#/202H/production/" + done["job_id"])
        expect(handled.get_by_role("link", name="查看方向", exact=True)).to_have_attribute(
            "href", "#/202H/directions/202H-S8")
        status, _, replay = candidate_http.CandidateHTTPTests.extension(self, "/api/public-stats/direction-candidates", body)
        self.assertEqual(status, 200, replay)
        self.assertEqual(replay["candidates"][1]["job_id"], done["job_id"])
        self.assertEqual(len(self.server.app.production.records()["jobs"]), 1)
        page.reload()
        expect(section.locator(".tk-cand")).to_have_count(1)
        expect(section.locator(".tk-cand.done")).to_have_count(0)


if __name__ == "__main__":
    unittest.main()
