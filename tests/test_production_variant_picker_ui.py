"""Variant picker visibility and adoption, using only disposable browser fixtures."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest

import test_phase4_writing_ui as writing


TASK = {"task_id": "AIT-0123456789ab", "product": "202H", "revision": 7,
        "context": {"direction_id": "202H-S3"}}
ROUND = {"round_id": "AIR-0123456789ab", "output": "Temporary variant picker fixture"}


def variants():
    return [{"index": index, "label": f"变体 {index + 1} · 可区分的临时脚本", "problems": [],
             "lines": [{"number": number, "spoken": f"Preview {index + 1} sentence {number:02d}.",
                        "zh": f"变体 {index + 1} 的第 {number} 句。", "proof": "展示真实动作与结果。",
                        "tags": "动作特写", "evidence": "临时素材说明"}
                       for number in range(1, 21)]} for index in range(10)]


def carousels():
    return [{"index": index, "label": f"图文变体 {index + 1} · 可区分的临时脚本", "problems": [],
             "caption_en": f"Caption for carousel {index + 1}.", "text": f"Carousel script {index + 1}.",
             "translation_zh": f"图文变体 {index + 1}。",
             "slides": [{"number": number, "role": "证明动作", "title_lines": [f"Carousel {index + 1} slide {number:02d}"],
                         "body": f"Body {index + 1}-{number}.", "title_zh": "叠字中文", "body_zh": "正文中文。",
                         "source": "临时素材特写", "forbidden": "不冒充结果"} for number in range(1, 21)]}
            for index in range(10)]


class VariantPickerBrowserTests(unittest.TestCase):
    setUpClass = classmethod(writing.WritingLifecycleBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(writing.WritingLifecycleBrowserTests.tearDownClass.__func__)
    stop_server = writing.WritingLifecycleBrowserTests.stop_server
    request = writing.WritingLifecycleBrowserTests.request
    open_page = writing.WritingLifecycleBrowserTests.open_page

    def setUp(self):
        writing.WritingLifecycleBrowserTests.setUp(self)
        self.server.app.resolve = SimpleNamespace(status=lambda: {"ok": False, "error": "隔离测试不连接达芬奇"})
        self.video_variants, self.photo_variants = variants(), carousels()
        self.posts = []
        self.variant_reads = []
        self.legacy_reads = []

    @staticmethod
    def reply(route, value, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(value, ensure_ascii=False))

    def picker(self, *, width=1280, height=800, carousel=False, production_status=200, table_status=200, script_status=200, variant_error=None, table_results=None, task=TASK, directions=None):
        page = self.open_page(width=width)
        if directions is not None:
            page.route("**/api/direction-catalog?*", lambda route: self.reply(route, {"directions": directions}))
        page.set_viewport_size({"width": width, "height": height})
        def candidate_variants(route):
            self.variant_reads.append(route.request.url)
            self.reply(route, {"error": variant_error} if variant_error else
                       {"variants": self.video_variants, "task_revision": 7}, 409 if variant_error else 200)

        def legacy_variants(route):
            self.legacy_reads.append(route.request.url)
            self.reply(route, {"error": "New candidate entry must use spoken variants"}, 422)

        page.route("**/api/production/variants?*", candidate_variants)
        page.route("**/api/production/carousel-variants?*", legacy_variants)

        def script(route):
            if route.request.method != "POST":
                self.reply(route, {"error": "fixture only accepts an adoption"}, 404)
                return
            body = route.request.post_data_json
            self.posts.append({"kind": "script", "body": body})
            if script_status != 200:
                self.reply(route, {"error": "fixture temporary adoption error"}, script_status)
                return
            identifier = f"SCR-{len([item for item in self.posts if item['kind'] == 'script']):012x}"
            self.reply(route, {"script": {**body["script"], "id": identifier, "source": body["source"]}})

        def table(route):
            body = route.request.post_data_json
            self.posts.append({"kind": "table", "body": body})
            if table_status != 200:
                self.reply(route, {"error": "fixture temporary table error"}, table_status)
                return
            selected = next(item["body"]["source"].get("variant_index", 0)
                            for item in reversed(self.posts) if item["kind"] == "script")
            result = {"id": "TBL-0123456789ab", "script_id": body["script_id"], "status": "completed", "carousels": [self.photo_variants[selected]]}
            if table_results:
                number = sum(item["kind"] == "table" for item in self.posts) - 1
                result.update(table_results[min(number, len(table_results) - 1)])
            self.reply(route, {"table": result})

        def production(route):
            body = route.request.post_data_json
            kind = "carousel" if carousel else "job"
            self.posts.append({"kind": kind, "body": body})
            if production_status != 200:
                self.reply(route, {"error": "fixture temporary creation error"}, production_status)
            else:
                self.reply(route, {kind: {"id": "CAR-0123456789ab" if carousel else "PRD-0123456789ab", "product": "202H"}})

        page.route("**/api/scripts", script)
        page.route("**/api/production/carousel-tables", table)
        page.route("**/api/production/carousel-tables?script_id=*", lambda route: self.reply(route, {"tables": []}))
        page.route("**/api/production/carousels" if carousel else "**/api/production/jobs", production)
        page.route("**/api/production/carousels/*" if carousel else "**/api/production/jobs/*",
                   lambda route: self.reply(route, {"error": "Temporary destination is not persisted"}, 404))
        page.evaluate("async ({task,round,carousel}) => {await window.CaseProduction[carousel ? 'fromCandidateCarousel' : 'fromCandidate'](task,round);}",
                      {"task": task, "round": ROUND, "carousel": carousel})
        dialog = page.locator(".production-dialog[open]" if variant_error else ".variant-picker-dialog[open]")
        dialog.wait_for()
        if not variant_error:
            self.assertEqual(dialog.locator('input[name="variant"]').count(), 10)
        return page, dialog

    def test_no_direction_round_prefills_each_variants_pick_for_the_person_to_confirm(self):
        for index, variant in enumerate(self.video_variants):
            variant["suggested_direction"] = "202H-S1" if index == 0 else "202H-S2" if index == 1 else "202H-S99"
        catalog = [{"direction_id": "202H-S1", "main_difference": "S1 定角", "status": "testing"},
                   {"direction_id": "202H-S2", "main_difference": "S2 镜面", "status": "testing"},
                   {"direction_id": "202H-S3", "main_difference": "S3 已归档", "status": "archived"}]
        page, dialog = self.picker(task={**TASK, "context": {}}, directions=catalog)
        field = dialog.locator('select[name="direction_id"]')
        self.assertEqual(field.input_value(), "202H-S1")
        self.assertEqual(field.locator("option").count(), 3, "An archived direction is not offered")
        radios = dialog.locator('input[name="variant"]')
        radios.nth(1).check()
        self.assertEqual(field.input_value(), "202H-S2", "The field follows the chosen variant")
        radios.nth(2).check()
        self.assertEqual(field.input_value(), "", "A pick that is not a real direction is not offered")
        radios.nth(1).check()
        field.select_option("202H-S1")
        radios.nth(0).check()
        radios.nth(1).check()
        self.assertEqual(field.input_value(), "202H-S1", "The person's own choice stays")
        dialog.locator('button[type="submit"]').click()
        page.wait_for_function("() => !document.querySelector('.variant-picker-dialog[open]')")
        adopted = next(item["body"] for item in self.posts if item["kind"] == "script")
        self.assertEqual(adopted["script"]["direction_id"], "202H-S1")
        page.close()

    def test_pasting_a_variant_into_save_adoption_follows_its_pick(self):
        page = self.open_page(width=1280)
        catalog = [{"direction_id": "202H-S1", "main_difference": "S1 定角", "status": "testing"},
                   {"direction_id": "202H-S2", "main_difference": "S2 镜面", "status": "testing"}]
        rows = [{**variant, "suggested_direction": "202H-S1" if variant["index"] == 0 else "202H-S2"} for variant in self.video_variants[:2]]
        posted = []
        def script(route):
            posted.append(route.request.post_data_json)
            self.reply(route, {"script": {**route.request.post_data_json["script"], "id": "SCR-0123456789ab", "source": {}}})
        page.route("**/api/direction-catalog?*", lambda route: self.reply(route, {"directions": catalog}))
        page.route("**/api/production/variants?*", lambda route: self.reply(route, {"variants": rows, "task_revision": 7}))
        page.route("**/api/scripts", script)
        page.evaluate("async ({task,round}) => {await window.CaseScripts.adoptCandidate(task,round,false);}",
                      {"task": {**TASK, "context": {}}, "round": {**ROUND, "status": "completed", "rounds": []}})
        dialog = page.locator("#script-editor-dialog")
        field = dialog.locator('select[name="direction_id"]')
        self.assertEqual(field.input_value(), "", "Two different picks: nothing is assumed before a variant is chosen")
        dialog.locator('[name="text"]').fill("Preview 2 sentence 01.\nPreview 2 sentence 02.")
        self.assertEqual(field.input_value(), "202H-S2")
        dialog.get_by_role("button", name="保存采用稿", exact=True).click()
        page.wait_for_function("() => !document.querySelector('#script-editor-dialog[open]')")
        self.assertEqual(posted[0]["script"]["direction_id"], "202H-S2")
        page.close()

    def test_a_round_with_its_own_direction_shows_no_direction_field(self):
        self.video_variants[0]["suggested_direction"] = "202H-S1"
        page, dialog = self.picker(directions=[{"direction_id": "202H-S1", "main_difference": "S1", "status": "testing"}])
        self.assertEqual(dialog.locator('select[name="direction_id"]').count(), 0)
        page.close()

    def assert_first_script_visible(self, page, dialog):
        first = dialog.locator("[data-variant-preview] tbody .spoken").first
        box = first.bounding_box()
        content = dialog.locator("[data-variant-content]").bounding_box()
        viewport = page.viewport_size
        self.assertIsNotNone(box)
        self.assertIsNotNone(content)
        self.assertGreaterEqual(box["y"], content["y"])
        self.assertLess(box["y"], min(content["y"] + content["height"], viewport["height"]))
        self.assertGreaterEqual(box["x"], 0)
        self.assertLess(box["x"], viewport["width"])
        self.assertEqual(dialog.evaluate("node => node.scrollTop"), 0)
        self.assertEqual(dialog.locator(".script-editor-body").evaluate("node => node.scrollTop"), 0)

    def screenshot(self, page, name):
        folder = os.environ.get("WORKBENCH_VARIANT_PICKER_QA")
        if folder:
            path = Path(folder)
            path.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path / f"{name}.png"), full_page=True)

    @staticmethod
    def geometry(dialog):
        return dialog.evaluate("""node => Object.fromEntries(Object.entries({dialog:node,form:node.querySelector('form'),
            body:node.querySelector('.script-editor-body'),layout:node.querySelector('.variant-picker-layout'),
            content:node.querySelector('[data-variant-content]'),footer:node.querySelector('.dialog-footer')}).map(([name,element]) => {
            const rect = element.getBoundingClientRect(), style = getComputedStyle(element);
            return [name,{top:rect.top,bottom:rect.bottom,height:rect.height,width:rect.width,cssHeight:style.height,
                minHeight:style.minHeight,maxHeight:style.maxHeight,boxSizing:style.boxSizing,padding:style.padding,
                border:style.borderWidth,display:style.display,overflow:style.overflow,flexShrink:style.flexShrink,
                scrollTop:element.scrollTop,clientHeight:element.clientHeight,scrollHeight:element.scrollHeight}];
        }))""")

    def test_desktop_options_and_script_scroll_independently_and_keyboard_refreshes_script(self):
        page, dialog = self.picker()
        options, content = dialog.locator(".variant-picker-options .variant-options"), dialog.locator("[data-variant-content]")
        left, right = options.bounding_box(), content.bounding_box()
        self.assertLessEqual(left["x"] + left["width"], right["x"])
        self.assertGreater(options.evaluate("node => node.scrollHeight"), options.evaluate("node => node.clientHeight"))
        self.assertGreater(content.evaluate("node => node.scrollHeight"), content.evaluate("node => node.clientHeight"))
        options.evaluate("node => {node.scrollTop = node.scrollHeight;}")
        before = options.evaluate("node => node.scrollTop")
        radio = dialog.locator('input[name="variant"][value="9"]')
        radio.check()
        self.assertEqual(options.evaluate("node => node.scrollTop"), before)
        self.assertIn("Preview 10 sentence 01.", dialog.locator("[data-variant-preview]").inner_text())
        self.assertEqual(dialog.locator("[data-variant-label]").inner_text(), "变体 10 · 可区分的临时脚本 · 脚本内容")
        self.assertEqual(dialog.locator('[name="title"]').input_value(), "202H-S3 变体 10 · 可区分的临时脚本")
        self.assert_first_script_visible(page, dialog)
        self.screenshot(page, "desktop-variant-10")
        content.evaluate("node => {node.scrollTop = node.scrollHeight;}")
        self.assertGreater(content.evaluate("node => node.scrollTop"), 0)
        radio.press("ArrowUp")
        self.assertTrue(dialog.locator('input[name="variant"][value="8"]').is_checked())
        self.assertIn("Preview 9 sentence 01.", dialog.locator("[data-variant-preview]").inner_text())
        self.assertEqual(content.evaluate("node => node.scrollTop"), 0)
        self.assertEqual(options.evaluate("node => node.scrollTop"), before)
        self.assert_first_script_visible(page, dialog)
        self.assertEqual(self.posts, [])

    def test_selection_preserves_custom_title_and_adopts_current_edited_lines(self):
        page, dialog = self.picker()
        dialog.locator('[name="title"]').fill("我的自定义标题")
        dialog.locator("[data-line-editor] > summary").click()
        dialog.locator('textarea[name="spoken"]').fill("Old variant edit.")
        dialog.locator('textarea[name="chinese"]').fill("旧版本编辑。")
        dialog.locator('input[name="variant"][value="9"]').check()
        self.assertEqual(dialog.locator('[name="title"]').input_value(), "我的自定义标题")
        self.assertEqual(dialog.locator('textarea[name="spoken"]').input_value(), "\n".join(line["spoken"] for line in self.video_variants[9]["lines"]))
        self.assertNotIn("Old variant edit.", dialog.locator('textarea[name="spoken"]').input_value())
        self.assertTrue(dialog.locator("[data-line-editor]").evaluate("node => node.open"))
        dialog.locator('textarea[name="spoken"]').fill("Edited selected variant.\nOnly current lines.")
        dialog.locator('textarea[name="chinese"]').fill("已编辑的选中版本。\n仅当前意群。")
        dialog.locator("[data-submit]").click()
        page.wait_for_function("() => !window.CaseProduction.modalOpen()")
        self.assertEqual([item["kind"] for item in self.posts], ["script", "job"])
        script, job = self.posts
        self.assertEqual(script["body"]["script"]["title"], "我的自定义标题")
        self.assertEqual(script["body"]["script"]["text"], "Edited selected variant.\nOnly current lines.")
        self.assertEqual(script["body"]["script"]["translation_zh"], "已编辑的选中版本。\n仅当前意群。")
        self.assertEqual(script["body"]["source"]["variant_index"], 9)
        self.assertEqual(job["body"]["job"]["variant_index"], 9)
        self.assertEqual(job["body"]["job"]["script_id"], "SCR-000000000001")
        self.assertEqual(self.server.app.production.public()["jobs"], [])

    def test_retry_retains_adoption_but_a_different_selected_variant_is_adopted_again(self):
        _, dialog = self.picker(production_status=503)
        submit = dialog.locator("[data-submit]")
        dialog.locator('input[name="variant"][value="9"]').check()
        submit.click()
        dialog.locator("[data-error]").get_by_text("fixture temporary creation error", exact=False).wait_for()
        submit.click()
        dialog.locator("[data-error]").get_by_text("fixture temporary creation error", exact=False).wait_for()
        self.assertEqual([item["kind"] for item in self.posts], ["script", "job", "job"])
        self.assertEqual(self.posts[1]["body"]["request_id"], self.posts[2]["body"]["request_id"])
        dialog.locator('input[name="variant"][value="8"]').check()
        submit.click()
        dialog.locator("[data-error]").get_by_text("fixture temporary creation error", exact=False).wait_for()
        self.assertEqual([item["kind"] for item in self.posts], ["script", "job", "job", "script", "job"])
        self.assertTrue(self.posts[3]["body"]["script"]["text"].startswith("Preview 9 sentence 01."))
        self.assertEqual(self.posts[4]["body"]["job"]["variant_index"], 8)
        self.assertEqual(self.posts[4]["body"]["job"]["script_id"], "SCR-000000000002")
        self.assertNotEqual(self.posts[1]["body"]["request_id"], self.posts[4]["body"]["request_id"])

if __name__ == "__main__":
    unittest.main()
