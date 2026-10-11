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
        self.assertEqual(steps.locator("[data-step] span").all_inner_texts(), ["意群表", "配音", "达芬奇时间线", "AI 剪辑与审查"])
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

    def test_selecting_a_take_also_builds_its_timeline(self):
        good = [{"number": 1, "start": 0, "end": 1.5, "coverage": 1}]
        self.payload["job"]["takes"] = [self.take("TAK-new", good), self.take("TAK-built", good)]
        self.payload["job"]["timelines"] = [{"unique_id": "uid-own", "name": "Lab261011-202-1", "creator": "Lab", "markers": 1,
            "frames": 60, "fps": "29.97", "take_id": "TAK-built", "built_at": "2026-10-11"}]

        def answer(route):
            body = {"timeline": {"name": "Lab261011-202-2"}} if route.request.url.endswith("/timeline") else {}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        page = self.open_page(post=answer)
        self.assertEqual(page.locator('[data-select-take="TAK-built"]').inner_text(), "选用这一版")
        button = page.locator('[data-select-take="TAK-new"]')
        self.assertEqual(button.inner_text(), "选用并建时间线（Lab）")
        button.click()
        page.locator('.production-steps [data-step="edit"][aria-selected="true"]').wait_for()
        self.assertEqual([call["url"].rsplit("/", 1)[-1] for call in self.calls], ["select", "timeline"])
        self.assertEqual(self.calls[0]["body"]["take_id"], "TAK-new")
        self.assertEqual(self.calls[1]["body"]["creator"], "Lab")


class CarouselStepsBrowserTests(CarouselBrowserTests):
    def test_carousel_opens_on_next_step_and_offers_finished_material_only(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        steps = page.locator(".production-steps")
        steps.wait_for()
        self.assertEqual(steps.locator('[aria-selected="true"]').get_attribute("data-step"), "pick")
        self.assertEqual(steps.locator("[data-step]").evaluate_all("nodes => nodes.map(node => node.dataset.step)"),
                         ["slides", "pick", "images", "delivery"])
        self.assertTrue(page.locator("#still-form").is_visible())
        for hidden in ("#carousel-images-panel", "#carousel-delivery-panel"):
            self.assertFalse(page.locator(hidden).is_visible())
        options = page.locator('#still-form input[name="material"]')
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
