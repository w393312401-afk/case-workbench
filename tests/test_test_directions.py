"""Direction metadata roundtrips and comparisons use isolated cases and fake AI only."""
from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

import test_server as fixture
import test_ui_regressions as browser
import test_ai_tasks as tasks


DIRECTION = {
    "direction_id": "202H-lock-angle", "main_difference": "对比徒手磨石，把角度锁住",
    "concern": "推磨时会不会晃", "closing": "看连续推磨，再判断是否适合自己",
    "variant_type": "direction",
}


class DirectionHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    detail = fixture.WorkbenchHTTPTests.detail
    body = fixture.WorkbenchHTTPTests.body
    save = fixture.WorkbenchHTTPTests.save
    record_bytes = fixture.WorkbenchHTTPTests.record_bytes
    assert_old_records_unchanged = fixture.WorkbenchHTTPTests.assert_old_records_unchanged

    def test_direction_save_metrics_update_clear_and_history(self):
        before = self.record_bytes()
        body = self.body()
        body["current"]["test_direction"] = copy.deepcopy(DIRECTION)
        body["current"]["production"] = {"script_version": "V2"}
        body["current"]["evidence"]["window"] = "2026-10-01 至 2026-10-07"
        self.assertEqual(self.save(body)[0], 200)
        stored = self.detail()
        self.assertEqual(stored["current"]["test_direction"], DIRECTION)
        state = self.request("GET", "/api/state")[2]
        self.assertEqual(state["cases"][0]["current"]["test_direction"], DIRECTION)
        self.assertIn("202h-lock-angle", state["cases"][0]["search_text"])
        # A new performance snapshot retains the test metadata and takes its own window.
        body = self.body(stored)
        body["current"]["metrics"]["items_sold"] = 18
        body["current"]["evidence"]["window"] = "2026-10-01 至 2026-10-14"
        self.assertEqual(self.save(body)[0], 200)
        summary = self.request("GET", "/api/directions?product=202H")[2]
        self.assertEqual(summary["groups"][0]["case_count"], 1)
        self.assertNotIn("items_sold", summary["groups"][0]["observations"][0]["metrics"])
        self.assertEqual(summary["groups"][0]["observations"][0]["sales_data"]["status"], "unavailable")
        self.assertEqual(summary["groups"][0]["cohorts"][0]["script_version"], "V2")
        self.assertNotIn("items_sold_total", summary["groups"][0])
        body = self.body()
        del body["current"]["test_direction"]
        self.assertEqual(self.save(body)[0], 200)
        cleared = self.detail()
        self.assertNotIn("test_direction", cleared["current"])
        self.assertTrue(any(e.get("current", {}).get("test_direction") == DIRECTION for e in cleared["events"]))
        self.assert_old_records_unchanged(before)

    def test_partial_invalid_direction_never_commits(self):
        before = self.record_bytes()
        body = self.body()
        body["current"]["test_direction"] = {"direction_id": "202H-lock-angle"}
        self.assertEqual(self.save(body)[0], 400)
        self.assertEqual(self.record_bytes(), before)
        self.assertEqual(self.request("GET", "/api/directions?archive=invalid")[0], 400)
        self.assertEqual(self.request("GET", "/api/directions?product=202H&product=202H")[0], 400)


class DirectionBrowserTests(unittest.TestCase):
    setUp = browser.WorkbenchBrowserTests.setUp
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    open_page = browser.WorkbenchBrowserTests.open_page
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def add_direction(self):
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["test_direction"] = copy.deepcopy(DIRECTION)
        current["production"] = {"script_version": "V2"}
        current["evidence"]["window"] = "发布以来累计（2026-10-01 至 2026-10-07）"
        self.store.append(self.identifier, {"kind": "editorial", "text": "隔离测试方向", "current": current})

    def test_editor_save_preserve_and_clear_direction(self):
        from direction_store import DirectionStore
        catalog = DirectionStore(self.runtime, self.skill)
        catalog.mutate("direction", {"request_id": uuid4().hex, "revision": catalog.revision(catalog.read()),
                                      "direction": {"product": "202H", **DIRECTION}})
        page = self.open_page(f"#case/{self.identifier}")
        page.locator(".detail-heading").wait_for()
        page.evaluate("openEditor(state.detail, {advanced: true})")
        page.locator('[data-direction-status]').get_by_text("可选", exact=False).wait_for()
        page.locator('[name="direction_direction_id"]').select_option(DIRECTION["direction_id"])
        self.assertTrue(page.locator('[name="direction_main_difference"]').evaluate("node => node.readOnly"))
        page.locator('[name="direction_concern"]').fill("")
        page.locator('[name="note"]').fill("补充方向")
        page.locator("#save-button").click()
        page.get_by_text("测试方向请补齐“顾虑链”", exact=False).wait_for()
        self.assertNotIn("test_direction", self.store.read_case(self.identifier)["current"])
        page.locator('[name="direction_concern"]').fill(DIRECTION["concern"])
        page.locator("#save-button").click()
        page.locator("#edit-dialog").wait_for(state="hidden")
        page.locator("#detail-section-test-direction").get_by_text(DIRECTION["direction_id"], exact=True).first.wait_for()
        page.locator(".detail-heading").wait_for()
        page.evaluate("openEditor(state.detail, {advanced: true})")
        page.locator('[name="conclusion"]').fill("普通编辑仍保留已记录的测试方向")
        page.locator('[name="note"]').fill("补当前判断")
        page.locator("#save-button").click()
        page.locator("#edit-dialog").wait_for(state="hidden")
        self.assertEqual(self.store.read_case(self.identifier)["current"]["test_direction"], DIRECTION)
        page.locator(".detail-heading").wait_for()
        page.evaluate("openEditor(state.detail, {advanced: true})")
        page.locator('[name="direction_direction_id"]').select_option("")
        page.locator('[name="note"]').fill("移除当前方向标记")
        page.locator("#save-button").click()
        page.locator("#edit-dialog").wait_for(state="hidden")
        self.assertNotIn("test_direction", self.store.read_case(self.identifier)["current"])
        self.assertTrue(any(event.get("current", {}).get("test_direction") == DIRECTION for event in self.store.read_case(self.identifier)["events"]))

    def test_filters_list_compare_and_summary_preserve_metrics_sources(self):
        self.add_direction()
        current = fixture.new_case_snapshot(self.video)
        current["test_direction"] = {**DIRECTION, "variant_type": "execution", "execution_change": "只换钩子"}
        current["production"] = {"script_version": "V3"}
        current["evidence"]["window"] = "发布以来累计（2026-10-01 至 2026-10-14）"
        self.store.add({"title": "执行改动测试", "product": "SHARPAL 202H", "original": {"text": "Test source"},
                        "feedback": {"kind": "editorial", "text": "隔离验证"}, "current": current})
        page = self.open_page()
        page.locator("#direction-filter").select_option(DIRECTION["direction_id"])
        page.locator("#variant-filter").select_option("execution")
        self.assertEqual(page.locator(".case-card").count(), 1)
        self.assertIn("variant=execution", page.url)
        page.locator("#variant-filter").select_option("all")
        page.get_by_role("button", name="列表", exact=True).click()
        self.assertEqual(page.locator(".table-direction").count(), 2)
        page.locator("#select-visible").check()
        page.locator("#compare-selected").click()
        page.locator("#case-comparison").get_by_text("只换钩子", exact=True).wait_for()
        page.locator("#close-comparison").click()
        page.locator("#directions-nav").click()
        page.locator(".direction-card-link").first.click()
        page.locator(".direction-group").first.wait_for()
        self.assertEqual(page.locator(".direction-group").count(), 1)
        self.assertEqual(page.locator(".direction-observations tbody tr").count(), 2)
        self.assertEqual(page.locator(".direction-observations").get_by_text("0.94%", exact=True).count(), 0)
        sales_cells = page.locator(".direction-sales-status").all_inner_texts()
        self.assertEqual(len(sales_cells), 2)
        self.assertTrue(all(text in ("未匹配到视频", "暂不可用") for text in sales_cells))
        page.locator(".direction-cohorts summary").click()
        page.locator(".direction-cohorts").get_by_text("V2", exact=False).wait_for()
        page.locator(".direction-cohorts").get_by_text("V3", exact=False).wait_for()

    def test_ordinary_edit_keeps_direction_version_and_historical_metrics(self):
        self.add_direction()
        previous = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        page = self.open_page(f"#case/{self.identifier}")
        self.assertEqual(page.locator("#case-backend-fill").count(), 0)
        page.locator(".detail-heading").wait_for()
        page.evaluate("openEditor(state.detail, {advanced: true})")
        self.assertEqual(page.locator('[name="direction_direction_id"]').input_value(), DIRECTION["direction_id"])
        self.assertEqual(page.locator('[name="production_script_version"]').input_value(), "V2")
        self.assertEqual(page.locator('[name="metric_items_sold"]').count(), 0)
        page.locator('[name="note"]').fill("保留方向、版本和历史快照")
        page.locator("#save-button").click()
        page.locator("#edit-dialog").wait_for(state="hidden")
        stored = self.store.read_case(self.identifier)["current"]
        self.assertEqual(stored["test_direction"], DIRECTION)
        self.assertEqual(stored["production"]["script_version"], "V2")
        self.assertEqual(stored["metrics"], previous["metrics"])
        self.assertEqual(stored["evidence"], previous["evidence"])

    def test_alias_dropdown_groups_known_model_and_shows_cross_direction_assignments(self):
        self.add_direction()
        for label, direction_id in (("202H", DIRECTION["direction_id"]),
                                    ("SHARPAL 202H 手动定角磨刀系统", "202H-care"),
                                    ("SHARPAL 202H Precision Knife Sharpening System", DIRECTION["direction_id"])):
            current = fixture.snapshot(self.video)
            current["test_direction"] = {**DIRECTION, "direction_id": direction_id}
            self.store.add({"title": "别名对照", "product": label, "original": {"text": "Fixture source"},
                            "feedback": {"kind": "editorial", "text": "隔离别名验证"}, "current": current})
        page = self.open_page("#directions?product=202H")
        page.locator(".dir-row.has-direction").first.wait_for()
        self.assertEqual(page.locator('#direction-filters [name="product"]').count(), 0)
        self.assertEqual(page.locator('#global-product option').all_text_contents(),
                         ["全部产品", "SHARPAL 202H"])
        self.assertEqual(page.locator('#global-product').input_value(), "202H")
        self.assertTrue(page.url.endswith("#/202H/directions"), page.url)
        self.assertEqual(page.locator(".dir-row.has-direction").count(), 2)
        page.locator(f'[data-direction-id="{DIRECTION["direction_id"]}"] .direction-card-link').click()
        page.locator(".direction-duplicate").first.get_by_text("同一视频跨方向登记", exact=False).wait_for()
        self.assertEqual(page.locator(".direction-duplicate").first.locator("li").count(), 4)
        for detail in page.locator(".direction-record-details").all():
            detail.locator("summary").click()
        page.locator(".direction-observations").get_by_text("原产品名 202H", exact=True).wait_for()

    def test_direction_table_fits_desktop_and_long_text_does_not_overflow_page(self):
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["title"] = "锁定角度后持续推磨，检查是否晃动与刀刃接触，" * 5
        current["test_direction"] = {**DIRECTION, "direction_id": "202H-" + "angle" * 22,
                                     "main_difference": "与徒手磨石比较，锁住角度并观察连续推磨时的稳定性。" * 5}
        self.store.append(self.identifier, {"kind": "editorial", "text": "隔离长文本布局", "current": current})
        database = self.server.app.performance.database
        database.parent.mkdir(parents=True, exist_ok=True)
        database.touch()
        fake = patch.object(self.server.app.performance, "video_brief", return_value={current["metrics"]["video_id"]: {
            "views": 662246, "clicks": 21295, "items": 22, "orders": 21,
            "first_data": "2026-10-01", "last_data": "2026-10-07"}})
        fake.start()
        self.addCleanup(fake.stop)
        page = self.open_page("#directions?product=202H")
        page.set_viewport_size({"width": 1265, "height": 900})
        page.locator(".direction-card-link").first.click()
        page.locator(".direction-table tbody tr").wait_for()
        self.assertEqual(page.locator(".direction-observations .case-table").count(), 0)
        self.assertEqual(page.locator(".direction-table thead th").all_text_contents(),
                         ["当前案例", "播放量", "商品点击", "售出件数", "CTOR", "统计窗口", "销售数据表"])
        geometry = page.locator(".direction-observations").evaluate("""element => {
          const table = element.querySelector('table'), cells = [...table.querySelectorAll('thead th')];
          return {available:element.clientWidth, table:table.getBoundingClientRect().width,
                  widths:cells.map(cell => cell.getBoundingClientRect().width),
                  rowHeight:table.querySelector('tbody tr').getBoundingClientRect().height};
        }""")
        self.assertLessEqual(geometry["table"], geometry["available"] + 1)
        self.assertGreaterEqual(geometry["widths"][0], 200)
        self.assertTrue(all(60 <= width <= 100 for width in geometry["widths"][1:5]))
        self.assertLess(geometry["rowHeight"], 140)
        self.assertFalse(page.locator(".direction-summary-notes details").evaluate("el => el.open"))
        self.assertFalse(page.locator(".direction-definition-details").evaluate("el => el.open"))
        d = current["test_direction"]
        self.assertEqual(page.locator(".direction-preview").text_content().strip(),
                         "方向链 " + " → ".join([d["main_difference"], d["concern"], d["closing"]]))
        page.locator(".direction-definition-details summary").click()
        page.locator(".direction-definition-details .direction-definition").get_by_text(DIRECTION["concern"], exact=False).wait_for()
        page.locator(".direction-definition-details .direction-definition").get_by_text(DIRECTION["closing"], exact=False).wait_for()
        page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391, page.evaluate("""() => [...document.querySelectorAll('#content *')].filter(el => el.getBoundingClientRect().right > innerWidth + 1 && !el.closest('.direction-observations') && el.getBoundingClientRect().width).map(el => ({tag:el.tagName, cls:el.className, right:el.getBoundingClientRect().right, text:el.textContent.slice(0,80)}))"""))
        container = page.locator(".direction-observations")
        self.assertEqual(container.get_attribute("tabindex"), "0")
        self.assertGreater(container.evaluate("el => el.scrollWidth"), container.evaluate("el => el.clientWidth"))
        container.focus()
        page.keyboard.press("ArrowRight")
        page.wait_for_function("() => document.querySelector('.direction-observations').scrollLeft > 0")

    def test_exact_direction_filter_has_candidates_and_clear_action(self):
        self.add_direction()
        page = self.open_page("#directions?product=202H&direction=missing-direction")
        page.get_by_role("heading", name="当前筛选没有匹配的卖点", exact=True).wait_for()
        self.assertEqual(page.locator(".dir-row.has-direction").count(), 0)
        self.assertEqual(page.locator('#direction-filters [name="direction"]').input_value(), "missing-direction")
        page.locator(".dir-scope").get_by_role("link", name="看全部", exact=True).click()
        page.locator(".dir-row.has-direction").first.wait_for()
        self.assertEqual(page.locator('#direction-filters [name="direction"]').count(), 0)


class DirectionFrozenTaskTests(unittest.TestCase):
    setUp = tasks.TaskTests.setUp
    tearDown = tasks.TaskTests.tearDown
    new_manager = tasks.TaskTests.new_manager
    body = tasks.TaskTests.body
    wait = tasks.TaskTests.wait
    path = tasks.TaskTests.path

    def test_retrospective_freezes_peer_direction_summary_across_continuation(self):
        current = copy.deepcopy(self.current)
        current["test_direction"] = copy.deepcopy(DIRECTION)
        current["metrics"] = {"items_sold": 4, "ctor_display": "0.4%"}
        current["production"] = {"script_version": "V1"}
        self.store.append(self.identifier, {"kind": "editorial", "text": "准备方向", "current": current})
        task = self.wait(self.manager.create(self.identifier, self.body("retrospective", "按方向复盘"))["task_id"])
        raw = json.loads(self.path(task["task_id"]).read_text(encoding="utf-8"))
        frozen = copy.deepcopy(raw["snapshot"]["direction_summary"])
        self.assertNotIn("items_sold", frozen["data"]["groups"][0]["observations"][0]["metrics"])
        current["metrics"]["items_sold"] = 19
        self.store.append(self.identifier, {"kind": "performance", "text": "后续数据", "current": current})
        continued = self.manager.continue_task(task["task_id"], {
            "request_id": uuid4().hex, "task_revision": task["revision"], "instruction": "继续按原窗口讨论",
        })
        self.wait(continued["task_id"])
        latest = json.loads(self.path(task["task_id"]).read_text(encoding="utf-8"))
        self.assertEqual(latest["snapshot"]["direction_summary"], frozen)
        prompt = self.bridges[-1].calls[-1]["prompt"]
        self.assertNotIn('"items_sold": 4', prompt)
        self.assertNotIn('"items_sold": 19', prompt)


if __name__ == "__main__":
    unittest.main()
