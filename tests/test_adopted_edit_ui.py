"""采用稿详情页原地逐句改：双击句子进入、改中文标待重译、存为新版。只用临时运行目录和假的 AI。"""
from __future__ import annotations

import os
import unittest
from uuid import uuid4

import test_phase4_writing_ui as writing_fixture
import test_server as fixture
import test_completed_task_import as imported
from test_ai_tasks import FakeBridge
from workbench_completed_import import import_completed_task


class AdoptedEditBrowserTests(unittest.TestCase):
    setUpClass = classmethod(writing_fixture.WritingLifecycleBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(writing_fixture.WritingLifecycleBrowserTests.tearDownClass.__func__)
    setUp = writing_fixture.WritingLifecycleBrowserTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    open_page = writing_fixture.WritingLifecycleBrowserTests.open_page
    body = imported.CompletedTaskImportTests.body

    def adopted(self):
        return import_completed_task(self.server.app, self.body())["scripts"][0]

    def test_double_click_edits_in_place_and_saves_the_next_version(self):
        script = self.adopted()
        self.server.app.ai.bridge = FakeBridge(output='{"lines":[{"number":2,"spoken":"Let the guide lead every pass."}]}')
        page = self.open_page(f"#/202H/content/{script['id']}")
        cells = page.locator("#content-adopted-script .adopted-copy .ai-variant-pairs > p")
        cells.nth(3).wait_for()
        self.assertEqual(cells.nth(3).get_attribute("title"), "双击改这一句")
        cells.nth(3).dblclick()
        editor = page.locator("#content-adopted-script .adopted-line-edit")
        editor.wait_for()
        self.assertTrue(page.locator("#content-adopted-script .adopted-copy").is_hidden())
        self.assertEqual(page.evaluate("document.activeElement.getAttribute('aria-label')"), "第 2 句中文")
        save = editor.locator("[data-edit-save]")
        self.assertTrue(save.is_disabled())
        page.keyboard.press("Control+a")
        page.keyboard.type("让导杆带着每一下都走同一条路。")
        row = editor.locator("[data-edit-row]").nth(1)
        self.assertEqual(row.locator(".ai-edit-state").inner_text(), "待重译")
        self.assertEqual(save.inner_text(), "重译 1 句并存为新版")
        self.assertIn("保存后是第 2 版", editor.locator(".ai-edit-foot p").inner_text())
        if os.environ.get("ADOPTED_EDIT_SHOT"):
            page.set_viewport_size({"width": 1400, "height": 900})
            page.screenshot(path=os.environ["ADOPTED_EDIT_SHOT"], full_page=True)
        save.click()
        page.wait_for_function("() => !document.querySelector('.adopted-line-edit') && document.querySelector('#content-adopted-script h2 small')?.textContent.includes('第 2 版')")
        text = page.locator("#content-adopted-script .adopted-copy").inner_text()
        self.assertIn("Let the guide lead every pass.", text)
        self.assertIn("让导杆带着每一下都走同一条路。", text)
        self.assertIn("2. 【重写】中文：让导杆带着每一下都走同一条路。", self.server.app.ai.bridge.calls[0]["prompt"])

    def test_edit_button_opens_rows_and_cancel_leaves_the_copy(self):
        script = self.adopted()
        self.server.app.ai.bridge = FakeBridge()
        page = self.open_page(f"#/202H/content/{script['id']}")
        page.locator("#content-adopted-script [data-adopted-edit]").click()
        editor = page.locator("#content-adopted-script .adopted-line-edit")
        self.assertEqual(editor.locator("[data-edit-row]").count(), 2)
        editor.locator("[data-edit-act=add]").first.click()
        self.assertEqual(editor.locator("[data-edit-row]").count(), 3)
        self.assertEqual(editor.locator("[data-edit-row]").nth(1).locator(".ai-edit-state").inner_text(), "新增")
        page.once("dialog", lambda dialog: dialog.accept())
        editor.locator("[data-edit-cancel]").click()
        self.assertEqual(page.locator("#content-adopted-script .adopted-line-edit").count(), 0)
        self.assertTrue(page.locator("#content-adopted-script .adopted-copy").is_visible())
        # The header menu's entry opens the same rows; a typed spoken line saves without the AI.
        page.locator("#script-more-actions summary").click()
        page.locator("#script-derive").click()
        editor.locator("[data-edit-field=spoken]").first.fill("Hold it tight 1.")
        self.assertEqual(editor.locator("[data-edit-save]").inner_text(), "存为新版")
        editor.locator("[data-edit-save]").click()
        page.wait_for_function("() => document.querySelector('#content-adopted-script h2 small')?.textContent.includes('第 2 版')")
        self.assertEqual(self.server.app.ai.bridge.calls, [])

    def test_a_filled_order_shows_its_table_on_the_adopted_copy(self):
        app = self.server.app
        status, _, data = self.request("POST", "/api/scripts", {"revision": app.creation.public()["revision"], "request_id": uuid4().hex,
            "script": {"product": "202H", "title": "手动", "text": "One kit.\nGrab it below.", "translation_zh": "一套。\n下面拿。", "confirmed": True}})
        script = data["script"]
        job = app.create_production_job({"request_id": uuid4().hex, "revision": app.production.public()["revision"], "job": {"script_id": script["id"]}})["job"]
        app.save_job_lines(job["id"], {"request_id": uuid4().hex, "revision": app.production.public()["revision"],
            "lines": [{"number": 1, "proof": "整套打开", "tags": "开箱", "evidence": "展示"}, {"number": 2, "proof": "结尾 CTA", "tags": "", "evidence": ""}],
            "supplement": {"hook_visual": "手打开工具箱"}})
        page = self.open_page(f"#/202H/content/{script['id']}")
        tabs = page.locator("#content-adopted-script .ai-variant-tabs")
        tabs.wait_for()
        if os.environ.get("ADOPTED_FILL_SHOT"):
            page.screenshot(path=os.environ["ADOPTED_FILL_SHOT"], full_page=True)
        names = tabs.locator("[data-variant-tab]").all_inner_texts()
        self.assertTrue(len(names) >= 2, names)
        for name in names[1:]:
            tabs.locator("[data-variant-tab]", has_text=name).click()
        self.assertIn("整套打开", page.locator("#content-adopted-script").inner_text())


if __name__ == "__main__":
    unittest.main()
