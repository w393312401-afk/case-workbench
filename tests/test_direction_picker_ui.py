"""Direction selection and video binding use a temporary catalog and case library."""
from __future__ import annotations

import copy
import json
import unittest
from uuid import uuid4

from direction_store import DirectionStore
import test_server as fixture
import test_ui_regressions as browser
import test_performance as performance


FIRST = {"product": "202H", "direction_id": "202H-S1", "main_difference": "S1 定角不用猜", "concern": "担心角度不稳", "closing": "锁定角度后连续推磨", "status": "testing"}
SECOND = {"product": "202H", "direction_id": "202H-S2", "main_difference": "S2 全套收纳", "concern": "配件会不会丢", "closing": "用完装回盒子", "status": "active"}
VIDEO = "7600000000000000002"


class DirectionPickerBrowserTests(unittest.TestCase):
    setUp_ = browser.WorkbenchBrowserTests.setUp
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    open_page = browser.WorkbenchBrowserTests.open_page
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def setUp(self):
        self.setUp_()
        self.catalog_store = DirectionStore(self.runtime, self.skill)
        for direction in (FIRST, SECOND, {**FIRST, "direction_id": "202H-S9", "main_difference": "S9 已归档", "status": "archived"}, {**FIRST, "product": "999H", "direction_id": "999H-S1"}):
            self.add_direction(direction)

    def add_direction(self, direction):
        return self.catalog_store.mutate("direction", {"request_id": uuid4().hex, "revision": self.catalog_store.revision(self.catalog_store.read()), "direction": direction})

    def case_direction(self, direction=FIRST):
        case = self.store.read_case(self.identifier)
        current = copy.deepcopy(case["current"])
        current["test_direction"] = {key: direction[key] for key in ("direction_id", "main_difference", "concern", "closing")}
        current["test_direction"].update(variant_type="execution", concern="这条案例自己的顾虑", closing="这条案例自己的收口", execution_change="只换开头镜头", script_version="V3")
        self.store.append(self.identifier, {"kind": "editorial", "text": "隔离方向快照", "current": current})
        return current["test_direction"]

    def inline(self, page):
        page.locator(".detail-heading").wait_for()
        page.locator('[data-inline-edit="direction"]').click()
        form = page.locator('.case-inline-editor[data-edit-section="direction"]')
        form.locator('[data-direction-status]').get_by_text("可选", exact=False).wait_for()
        return form

    def video_list(self):
        folder = self.root / "exports"
        performance.build_exports(folder)
        self.server.app.performance.sources = [folder]
        self.assertEqual(self.request("POST", "/api/performance/sync", {})[0], 200)
        page = self.open_page(f"#/202H/videos?video={VIDEO}")
        drawer = page.locator("#perf-video-drawer")
        drawer.wait_for(state="visible")
        drawer.locator("[data-drawer-close]").click()
        return page

    def video_editor(self, page):
        cell = page.locator(f'[data-direction-video="{VIDEO}"]')
        cell.locator('[data-video-direction]').click()
        form = cell.locator("form")
        form.wait_for()
        return form

    def test_case_selection_keeps_execution_fields_and_clear_keeps_history(self):
        original = self.case_direction()
        page = self.open_page(f"#/202H/case/{self.identifier}")
        form = self.inline(page)
        choices = form.locator('[name="direction_direction_id"] option').evaluate_all("nodes => nodes.map(node => node.value)")
        self.assertEqual(choices, ["", "202H-S1", "202H-S2"])
        self.assertTrue(form.locator('[name="direction_main_difference"]').evaluate("node => node.readOnly"))
        form.locator('[name="direction_direction_id"]').select_option("202H-S2")
        self.assertEqual(form.locator('[name="direction_main_difference"]').input_value(), SECOND["main_difference"])
        for key in ("concern", "closing", "execution_change", "script_version", "variant_type"):
            self.assertEqual(form.locator(f'[name="direction_{key}"]').input_value(), original[key])
        form.locator('[data-inline-save]').click()
        form.wait_for(state="detached")
        saved = self.store.read_case(self.identifier)
        self.assertEqual(saved["current"]["test_direction"], {**original, "direction_id": "202H-S2", "main_difference": SECOND["main_difference"]})
        self.assertTrue(any(event.get("current", {}).get("test_direction") == original for event in saved["events"]))
        form = self.inline(page)
        form.locator('[name="direction_direction_id"]').select_option("")
        form.locator('[data-inline-save]').click()
        form.wait_for(state="detached")
        cleared = self.store.read_case(self.identifier)
        self.assertNotIn("test_direction", cleared["current"])
        self.assertTrue(any(event.get("current", {}).get("test_direction", {}).get("direction_id") == "202H-S2" for event in cleared["events"]))

    def test_empty_case_uses_catalog_defaults_and_rejects_incomplete_chain(self):
        page = self.open_page(f"#/202H/case/{self.identifier}")
        form = self.inline(page)
        form.locator('[name="direction_direction_id"]').select_option("202H-S1")
        self.assertEqual(form.locator('[name="direction_concern"]').input_value(), FIRST["concern"])
        self.assertEqual(form.locator('[name="direction_closing"]').input_value(), FIRST["closing"])
        form.locator('[name="direction_concern"]').fill("")
        form.locator('[data-inline-save]').click()
        form.locator('[data-inline-status]').get_by_text("请补齐", exact=False).wait_for()
        self.assertNotIn("test_direction", self.store.read_case(self.identifier)["current"])
        form.locator('[name="direction_concern"]').fill("核对后的案例顾虑")
        form.locator('[data-inline-save]').click()
        form.wait_for(state="detached")
        self.assertEqual(self.store.read_case(self.identifier)["current"]["test_direction"]["concern"], "核对后的案例顾虑")

    def test_catalog_failure_preserves_original_and_retry_can_load(self):
        original = self.case_direction()
        page = self.open_page(f"#/202H/case/{self.identifier}")
        page.route("**/api/direction-catalog?*", lambda route: route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "临时不可用"})))
        page.locator('[data-inline-edit="direction"]').click()
        form = page.locator('.case-inline-editor[data-edit-section="direction"]')
        form.locator('[data-direction-retry]').wait_for()
        self.assertEqual(form.locator('[name="direction_direction_id"]').input_value(), original["direction_id"])
        form.locator('[name="direction_execution_change"]').fill("失败时仍可保留旧方向并修改执行记录")
        form.locator('[data-inline-save]').click()
        form.wait_for(state="detached")
        self.assertEqual(self.store.read_case(self.identifier)["current"]["test_direction"]["direction_id"], original["direction_id"])
        page.locator('[data-inline-edit="direction"]').click()
        form.locator('[data-direction-retry]').wait_for()
        page.unroute("**/api/direction-catalog?*")
        form.locator('[data-direction-retry]').click()
        form.locator('[data-direction-status]').get_by_text("可选", exact=False).wait_for()
        self.assertEqual(form.locator('[name="direction_direction_id"] option[value="202H-S2"]').count(), 1)

    def test_archived_current_direction_remains_available_without_changing_snapshot(self):
        original = self.case_direction({**FIRST, "direction_id": "202H-S9", "main_difference": "S9 已归档"})
        page = self.open_page(f"#/202H/case/{self.identifier}")
        form = self.inline(page)
        self.assertEqual(form.locator('[name="direction_direction_id"]').input_value(), "202H-S9")
        self.assertIn("已归档", form.locator('[name="direction_direction_id"] option:checked').inner_text())
        form.locator('[data-inline-save]').click()
        form.wait_for(state="detached")
        self.assertEqual(self.store.read_case(self.identifier)["current"]["test_direction"], original)

    def test_video_assignment_unlink_and_collect_inherits_direction(self):
        page = self.video_list()
        page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
        form = self.video_editor(page)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
        self.assertEqual(form.locator('select option[value="999H-S1"]').count(), 0)
        form.locator("select").select_option("202H-S1")
        form.get_by_role("button", name="保存", exact=True).click()
        page.locator(f'[data-direction-video="{VIDEO}"] [data-video-direction]').get_by_text("202H-S1", exact=True).wait_for()
        form = self.video_editor(page)
        form.locator('[data-video-unlink]').click()
        form.get_by_role("button", name="保存", exact=True).click()
        page.locator(f'[data-direction-video="{VIDEO}"] [data-video-direction]').get_by_text("未归类", exact=True).wait_for()
        link = self.catalog_store.find_link(self.catalog_store.read(), "202H", VIDEO)
        self.assertEqual(link["direction_id"], "")
        form = self.video_editor(page)
        form.locator("select").select_option("202H-S2")
        form.get_by_role("button", name="保存", exact=True).click()
        page.locator(f'[data-direction-video="{VIDEO}"] [data-video-direction]').get_by_text("202H-S2", exact=True).wait_for()
        page.locator(f'[data-video-row="{VIDEO}"] td.perf-title').click()  # collect lives in the details drawer
        page.locator(f'[data-collect="{VIDEO}"]').click()
        page.locator('.direction-inherit-note').get_by_text("202H-S2", exact=False).wait_for()
        page.locator('[name="conclusion"]').fill("测试收录会继承视频已经确定的方向")
        page.locator('#save-button').click()
        page.locator('#edit-dialog').wait_for(state="hidden")
        page.locator('.detail-heading').wait_for()
        current = self.store.read_case(page.evaluate("state.detail.case_id"))["current"]
        self.assertEqual(current["test_direction"]["direction_id"], "202H-S2")
        self.assertEqual(current["test_direction"]["concern"], SECOND["concern"])
        self.assertEqual(current["metrics"]["video_id"], VIDEO)

    def test_video_conflict_keeps_choice_and_needs_explicit_retry(self):
        page = self.video_list()
        form = self.video_editor(page)
        form.locator("select").select_option("202H-S2")
        self.catalog_store.mutate("direction", {"request_id": uuid4().hex, "revision": self.catalog_store.revision(self.catalog_store.read()),
                                                "operation": "update", "direction": {**FIRST, "notes": "来自另一页面的修改"}})
        form.get_by_role("button", name="保存", exact=True).click()
        form.locator('[role="status"]').get_by_text("保留你的选择", exact=False).wait_for()
        self.assertEqual(form.locator("select").input_value(), "202H-S2")
        self.assertIsNone(self.catalog_store.find_link(self.catalog_store.read(), "202H", VIDEO))
        # A background update must not replace the active row and its draft.
        page.evaluate("refresh(true)")
        self.assertTrue(form.is_visible())
        self.assertEqual(form.locator("select").input_value(), "202H-S2")
        form.get_by_role("button", name="保存", exact=True).click()
        page.locator(f'[data-direction-video="{VIDEO}"] [data-video-direction]').get_by_text("202H-S2", exact=True).wait_for()

    def test_late_catalog_result_cannot_replace_changed_product_options(self):
        page = self.open_page("#/202H/library")
        page.evaluate("""() => {
          const realApi = window.api; window.directionPending = [];
          window.api = (path, options) => path.startsWith('/api/direction-catalog') ? new Promise(resolve => directionPending.push({path, resolve})) : realApi(path, options);
          const form = document.createElement('form'); form.id = 'picker-test';
          form.innerHTML = '<input name="product" value="202H">' + CaseDirectionPicker.markup({});
          document.body.append(form); CaseDirectionPicker.bind(form, {product:'202H', current:{}});
          form.elements.product.value = '999H'; form.elements.product.dispatchEvent(new Event('change'));
          directionPending[1].resolve({directions:[{product:'999H', direction_id:'999H-S1', main_difference:'999H 独立方向'}]});
        }""")
        picker = page.locator('#picker-test [name="direction_direction_id"]')
        page.wait_for_function("document.querySelector('#picker-test select').options.length === 2")
        page.evaluate("directionPending[0].resolve({directions:[{product:'202H', direction_id:'202H-S1', main_difference:'过期方向'}]})")
        self.assertEqual(picker.locator('option').evaluate_all("nodes => nodes.map(node => node.value)"), ["", "999H-S1"])


if __name__ == "__main__":
    unittest.main()
