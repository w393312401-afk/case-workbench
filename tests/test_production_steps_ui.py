"""Production orders show one step at a time under a pinned top bar, and only 成品素材 timelines are offered."""
import unittest

import json

from test_production_carousel_delivery_ui import CarouselDeliveryBrowserTests
from test_production_carousel_ui import CarouselBrowserTests
from test_production_validation_ui import ProductionValidationBrowserTests


PINNED = """() => {
  const bar = document.querySelector('.topbar').getBoundingClientRect(), steps = document.querySelector('.production-steps').getBoundingClientRect();
  return {bar: Math.round(bar.top), steps: Math.round(steps.top), height: Math.round(bar.height)};
}"""


class VideoStepsBrowserTests(ProductionValidationBrowserTests):
    def test_steps_switch_sections_and_stay_pinned(self):
        self.payload["job"]["takes"] = [self.take("TAK-0123456789ab", [{"number": 1, "start": 0, "end": 1.5, "coverage": 1}])]
        self.payload["job"]["selected_take"] = "TAK-0123456789ab"
        self.payload["job"]["timelines"] = [{"unique_id": "uid-own", "name": "Lab261011-202-1", "creator": "Lab", "markers": 1,
            "frames": 60, "fps": "29.97", "take_id": "TAK-0123456789ab", "built_at": "2026-10-11"}]
        self.timelines = ["蓝点折刀成品素材", "Lab261011-202-1", "户外猎刀成品素材", "混剪素材"]
        page = self.open_page()
        steps = page.locator(".production-steps")
        self.assertEqual(steps.locator("[data-step] span").all_inner_texts(), ["脚本", "配音", "达芬奇时间线", "AI 剪辑与人工审查", "成片与发布"])
        self.assertEqual(steps.locator('[aria-selected="true"]').get_attribute("data-step"), "edit")
        page.locator("#edit-panel").wait_for()
        for hidden in ("#voice-panel", "#timeline-panel"):
            self.assertFalse(page.locator(hidden).is_visible())
        options = page.locator('#edit-form input[name="material"]')
        options.first.wait_for()
        self.assertEqual([options.nth(i).get_attribute("value") for i in range(options.count())], ["蓝点折刀成品素材", "户外猎刀成品素材"])
        self.assertTrue(all(options.nth(i).is_checked() for i in range(options.count())))

        steps.locator('[data-step="voice"]').click()
        page.locator("#voice-panel").wait_for()
        self.assertFalse(page.locator("#edit-panel").is_visible())
        # Open completed controls so this fixture remains long enough to test sticky scrolling.
        page.locator('[data-production-controls="voice"] > summary').click()
        page.mouse.wheel(0, 3000)
        page.wait_for_function("() => scrollY > 0")
        pinned = page.evaluate(PINNED)
        self.assertEqual(pinned["bar"], 0)
        self.assertEqual(pinned["steps"], pinned["height"])

    def test_without_finished_material_timelines_names_can_be_typed(self):
        self.payload["job"]["timelines"] = [{"unique_id": "uid-own", "name": "Lab261011-202-1", "creator": "Lab", "markers": 1,
            "frames": 60, "fps": "29.97", "take_id": "", "built_at": "2026-10-11"}]
        self.payload["job"]["edits"] = [self.edit("awaiting_review")]
        page = self.open_page()
        page.locator('#edit-form input[name="material_text"]').wait_for()
        self.assertEqual(page.locator('#edit-form input[name="material"]').count(), 0)
        page.locator('#edit-form input[name="material_text"]').fill("混剪素材")
        page.locator("#edit-panel").get_by_role("button", name="开始 AI 剪辑", exact=True).click()
        page.locator("#edit-form [data-error]").get_by_text("素材时间线只能选名称带「成品素材」的时间线。", exact=True).wait_for()
        self.assertEqual(self.calls, [])

    def test_selecting_a_take_builds_its_timeline_without_changing_step(self):
        good = [{"number": 1, "start": 0, "end": 1.5, "coverage": 1}]
        self.payload["job"]["takes"] = [self.take("TAK-new", good), self.take("TAK-built", good)]
        self.payload["job"]["timelines"] = [{"unique_id": "uid-own", "name": "Lab261011-202-1", "creator": "Lab", "markers": 1,
            "frames": 60, "fps": "29.97", "take_id": "TAK-built", "built_at": "2026-10-11"}]

        def answer(route):
            body = {"timeline": {"name": "Lab261011-202-2"}} if route.request.url.endswith("/timeline") else {}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        page = self.open_page(post=answer)
        page.route("**/api/production/resolve", lambda route: route.fulfill(json={"ok": False, "busy": True,
            "error": "达芬奇正在执行另一项工作台操作（可能是 AI 剪辑），请完成后再试。"}))
        self.assertEqual(page.locator('[data-select-take="TAK-built"]').inner_text(), "选用这一版")
        button = page.locator('[data-select-take="TAK-new"]')
        self.assertEqual(button.inner_text(), "选用并建时间线（Lab）")
        button.click()
        page.wait_for_function("() => !document.querySelector('[data-select-take=\"TAK-new\"]').disabled")
        page.locator('.production-steps [data-step="voice"][aria-selected="true"]').wait_for()
        self.assertEqual([call["url"].rsplit("/", 1)[-1] for call in self.calls], ["select", "timeline"])
        self.assertEqual(self.calls[0]["body"]["take_id"], "TAK-new")
        self.assertEqual(self.calls[1]["body"]["creator"], "Lab")

    def test_take_card_picks_the_creator_or_selects_without_building(self):
        good = [{"number": 1, "start": 0, "end": 1.5, "coverage": 1}]
        self.payload["creators"] = ["Lab", "Zack"]
        self.payload["job"]["takes"] = [self.take("TAK-new", good), self.take("TAK-other", good)]
        page = self.open_page()
        card = page.locator('[data-take-id="TAK-new"]')
        card.locator("[data-build-creator]").select_option("Zack")
        self.assertEqual(card.locator("[data-select-take]").get_attribute("data-build"), "Zack")
        card.locator("[data-select-only]").click()
        page.wait_for_function("() => document.querySelector('#toast')?.innerText.includes('已选用这一版配音')")
        self.assertEqual([call["url"].rsplit("/", 1)[-1] for call in self.calls], ["select"])
        self.assertEqual(self.calls[0]["body"]["take_id"], "TAK-new")

    def test_timeline_options_send_only_changes_and_a_queued_build_can_be_withdrawn(self):
        self.payload["creators"] = ["Lab", "Zack"]
        self.payload["job"]["takes"] = [self.take("TAK-0123456789ab", [{"number": 1, "start": 0, "end": 1.5, "coverage": 1}])]
        self.payload["job"].update(selected_take="TAK-0123456789ab", status="voiced", timeline_failure={
            "request_id": "old", "at": "2026-10-11", "error": "达芬奇当前打开的是项目「X」", "cancelled": False})

        def answer(route):
            if route.request.url.endswith("/timeline"):
                self.payload["job"]["timeline_building"] = {"request_id": route.request.post_data_json["request_id"],
                                                            "creator": "Zack", "stage": "waiting", "since": "2026-10-11"}
                body = {"queued": True, "message": "达芬奇正在执行另一项工作台任务（如 AI 剪辑），建线已排队，空闲后自动开始。"}
            else:
                self.payload["job"]["timeline_building"] = None
                self.payload["job"]["timeline_failure"]["cancelled"] = True
                body = {"ok": True}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body, ensure_ascii=False))
        page = self.open_page(post=answer)
        form = page.locator("#timeline-form")
        form.wait_for()
        page.locator("[data-timeline-failure]").get_by_text("项目「X」", exact=False).wait_for()
        self.assertFalse(form.locator('select[name="size"]').is_visible())
        form.locator('select[name="creator"]').select_option("Zack")
        self.assertEqual(form.locator("[data-timeline-name]").inner_text(), "Zack261011-202-序号")
        form.locator("[data-timeline-options] > summary").click()
        form.locator('select[name="size"]').select_option("1080x1440")
        form.locator('input[name="lead"]').fill("0.5")
        form.locator('input[name="markers"]').uncheck()
        form.locator("[data-markers-note]").wait_for()
        self.assertEqual(form.locator("[data-options-summary]").inner_text(), "3:4 · 留白 0.5 秒 · 不写意群标记")
        form.get_by_role("button", name="在达芬奇建时间线", exact=True).click()
        page.locator("[data-timeline-busy]").get_by_text("建线排队中（Zack）", exact=False).wait_for()
        self.assertTrue(page.locator("[data-resolve-status]").is_hidden())
        self.assertEqual({key: value for key, value in self.calls[0]["body"].items() if key != "request_id"},
                         {"creator": "Zack", "options": {"width": 1080, "height": 1440, "lead_seconds": 0.5, "markers": False}})
        self.assertEqual(page.locator("[data-timeline-failure]").count(), 0)
        self.assertTrue(page.locator('#timeline-form button[type="submit"]').is_disabled())
        # The chosen options survive the re-render, so a withdrawn build can be resent as it was.
        self.assertEqual(page.locator("#timeline-form [data-options-summary]").inner_text(), "3:4 · 留白 0.5 秒 · 不写意群标记")
        page.locator("[data-cancel-timeline]").click()
        page.locator("[data-timeline-busy]").wait_for(state="detached")
        self.assertTrue(self.calls[1]["url"].endswith("/timeline/cancel"))
        self.assertEqual(self.calls[1]["body"]["request_id"], self.calls[0]["body"]["request_id"])
        self.assertEqual(page.locator("[data-timeline-failure]").count(), 0)
        self.assertFalse(page.locator('#timeline-form button[type="submit"]').is_disabled())


class CarouselStepsBrowserTests(CarouselBrowserTests):
    def test_carousel_opens_on_next_step_and_offers_finished_material_only(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        steps = page.locator(".production-steps")
        steps.wait_for()
        self.assertEqual(steps.locator('[aria-selected="true"]').get_attribute("data-step"), "pick")
        self.assertEqual(steps.locator("[data-step]").evaluate_all("nodes => nodes.map(node => node.dataset.step)"),
                         ["slides", "pick", "images", "delivery"])
        self.assertTrue(page.locator("#still-panel").is_visible())
        for hidden in ("#carousel-images-panel", "#carousel-delivery-panel"):
            self.assertFalse(page.locator(hidden).is_visible())
        options = page.locator('#still-panel input[name="material"]')
        options.first.wait_for()
        self.assertEqual(options.evaluate_all("nodes => nodes.map(node => [node.value, node.checked])"), [["蓝点折刀成品素材", True]])

        steps.locator('[data-step="images"]').click()
        page.locator("#carousel-images-panel").wait_for()
        self.assertFalse(page.locator("#still-panel").is_visible())
        page.reload()
        page.locator(".production-steps").wait_for()
        self.assertEqual(page.locator('.production-steps [aria-selected="true"]').get_attribute("data-step"), "pick")


class CarouselShortcutBrowserTests(CarouselDeliveryBrowserTests):
    def test_settings_fold_and_confirming_the_group_packages_it(self):
        page = self.open_images()
        toggle = page.locator("#carousel-requirements-toggle")
        self.assertFalse(toggle.evaluate("node => node.open"))
        self.assertFalse(page.locator('[name="image_mode"]').is_visible())
        self.assertIn("实拍保真版", toggle.locator("summary").inner_text())
        toggle.locator("summary").click()
        page.locator('[name="image_mode"]').wait_for()
        self.confirm_group(page)
        self.assertEqual(len(self.detail().get("deliveries", [])), 1)
        page.locator('[data-step="delivery"]').click()
        page.locator("[data-download-delivery]").wait_for()


# Reuse the fixtures without running the inherited tests a second time.
for case, base in ((VideoStepsBrowserTests, ProductionValidationBrowserTests), (CarouselStepsBrowserTests, CarouselBrowserTests),
                   (CarouselShortcutBrowserTests, CarouselDeliveryBrowserTests)):
    for name in [name for name in vars(base) if name.startswith("test_")]:
        setattr(case, name, None)
del ProductionValidationBrowserTests, CarouselBrowserTests, CarouselDeliveryBrowserTests, case, base, name

if __name__ == "__main__":
    unittest.main()
