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

    @staticmethod
    def reply(route, value, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(value, ensure_ascii=False))

    def picker(self, *, width=1280, height=800, carousel=False, production_status=200):
        page = self.open_page(width=width)
        page.set_viewport_size({"width": width, "height": height})
        path = "carousel-variants" if carousel else "variants"
        items = self.photo_variants if carousel else self.video_variants
        page.route(f"**/api/production/{path}?*", lambda route: self.reply(route, {"carousels" if carousel else "variants": items, "task_revision": 7}))

        def script(route):
            if route.request.method != "POST":
                self.reply(route, {"error": "fixture only accepts an adoption"}, 404)
                return
            body = route.request.post_data_json
            self.posts.append({"kind": "script", "body": body})
            identifier = f"SCR-{len([item for item in self.posts if item['kind'] == 'script']):012x}"
            self.reply(route, {"script": {"id": identifier}})

        def production(route):
            body = route.request.post_data_json
            kind = "carousel" if carousel else "job"
            self.posts.append({"kind": kind, "body": body})
            if production_status != 200:
                self.reply(route, {"error": "fixture temporary creation error"}, production_status)
            else:
                self.reply(route, {kind: {"id": "CAR-0123456789ab" if carousel else "PRD-0123456789ab", "product": "202H"}})

        page.route("**/api/scripts", script)
        page.route("**/api/production/carousels" if carousel else "**/api/production/jobs", production)
        page.route("**/api/production/carousels/*" if carousel else "**/api/production/jobs/*",
                   lambda route: self.reply(route, {"error": "Temporary destination is not persisted"}, 404))
        page.evaluate("async ({task,round,carousel}) => {await window.CaseProduction[carousel ? 'fromCandidateCarousel' : 'fromCandidate'](task,round);}",
                      {"task": TASK, "round": ROUND, "carousel": carousel})
        dialog = page.locator(".variant-picker-dialog[open]")
        dialog.wait_for()
        self.assertEqual(dialog.locator('input[name="variant"]').count(), 10)
        return page, dialog

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

    def test_mobile_long_list_keeps_selected_video_and_carousel_script_visible(self):
        for carousel in (False, True):
            with self.subTest(carousel=carousel):
                page, dialog = self.picker(width=390, height=780, carousel=carousel)
                options, content = dialog.locator(".variant-picker-options .variant-options"), dialog.locator("[data-variant-content]")
                above, below = options.bounding_box(), content.bounding_box()
                self.assertLessEqual(above["y"] + above["height"], below["y"])
                options.evaluate("node => {node.scrollTop = node.scrollHeight;}")
                dialog.locator('input[name="variant"][value="9"]').check()
                self.assert_first_script_visible(page, dialog)
                text = "Carousel 10 slide 01" if carousel else "Preview 10 sentence 01."
                self.assertIn(text, dialog.locator("[data-variant-preview]").inner_text())
                self.screenshot(page, "mobile-carousel-10" if carousel else "mobile-video-10")
                if os.environ.get("WORKBENCH_VARIANT_PICKER_QA"):
                    print(json.dumps({"carousel": carousel, "geometry": self.geometry(dialog)}))
                footer = dialog.locator(".dialog-footer").bounding_box()
                self.assertLessEqual(footer["y"] + footer["height"], 780)
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)

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

    def test_carousel_switch_refreshes_slides_caption_and_adoption_index(self):
        page, dialog = self.picker(carousel=True)
        dialog.locator('[name="title"]').fill("保留图文标题")
        content = dialog.locator("[data-variant-content]")
        content.evaluate("node => {node.scrollTop = node.scrollHeight;}")
        dialog.locator('input[name="variant"][value="9"]').check()
        self.assertEqual(content.evaluate("node => node.scrollTop"), 0)
        preview = dialog.locator("[data-variant-preview]").inner_text()
        self.assertIn("Carousel 10 slide 01", preview)
        self.assertIn("Caption for carousel 10.", preview)
        self.assertNotIn("Caption for carousel 1.", preview)
        self.assertEqual(dialog.locator('[name="title"]').input_value(), "保留图文标题")
        dialog.locator("[data-submit]").click()
        page.wait_for_function("() => !window.CaseProduction.modalOpen()")
        self.assertEqual([item["kind"] for item in self.posts], ["script", "carousel"])
        self.assertEqual(self.posts[0]["body"]["script"]["text"], "Carousel script 10.")
        self.assertEqual(self.posts[0]["body"]["script"]["title"], "保留图文标题")
        self.assertEqual(self.posts[1]["body"]["carousel"]["variant_index"], 9)
        self.assertEqual(self.server.app.production.public()["carousels"], [])


if __name__ == "__main__":
    unittest.main()
