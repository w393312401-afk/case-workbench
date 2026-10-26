"""整组方案与整组验收 through the real UI: edit the plan, see stage/shot columns, run the group review."""
from pathlib import Path
import unittest
from uuid import uuid4

from PIL import Image

import test_production_carousel_ui as carousel_fixture
from test_carousel_group_plan import GroupReviewer


class CarouselGroupPlanBrowserTests(unittest.TestCase):
    setUpClass = classmethod(carousel_fixture.CarouselBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(carousel_fixture.CarouselBrowserTests.tearDownClass.__func__)
    stop_server = carousel_fixture.CarouselBrowserTests.stop_server
    request = carousel_fixture.CarouselBrowserTests.request
    open_page = carousel_fixture.CarouselBrowserTests.open_page
    make_playable_video = carousel_fixture.CarouselBrowserTests.make_playable_video

    def setUp(self):
        carousel_fixture.CarouselBrowserTests.setUp(self)
        self.reviewer = GroupReviewer()
        self.app.image_reviewer = self.reviewer
        self.addCleanup(self.app.stop_images)
        self.addCleanup(self.reviewer.release.set)
        for slide in self.app.production.carousel(self.carousel["id"])["slides"]:
            relative = f"202H-S2/原图/group-{slide['number']}.png"
            path = self.app.carousel_image_root / relative
            Image.new("RGB", (120, 160), (slide["number"] * 50, 90, 120)).save(path)
            body = lambda **values: {"request_id": uuid4().hex, "revision": self.app.production.public()["revision"], **values}
            candidate = self.app.import_folder_image(self.carousel["id"], body(slide=slide["number"], path=relative))["candidate"]
            self.app.choose_base(self.carousel["id"], body(slide=slide["number"], candidate=candidate["id"]))

    def test_plan_is_edited_shown_in_the_table_and_the_group_review_reports_back(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.locator('[data-step="slides"]').click()
        headers = page.locator('.carousel-table thead th').all_inner_texts()
        self.assertEqual(headers[:4], ["张", "环节", "景别", "作用"])
        self.assertIn("未选", page.locator("#carousel-plan").inner_text())
        self.assertIn("展示路线", page.locator("#carousel-plan-block").inner_text())
        page.locator("#carousel-plan-edit").click()
        modal = page.locator(".production-dialog[open]")
        modal.locator('[name="route"]').select_option("product")
        modal.locator('[name="music"]').fill("节奏利落的器乐")
        modal.locator('[name="tag_en_0"]').fill("7 grit plates")
        modal.locator('[name="tag_zh_0"]').fill("7 块磨板")
        count = len(self.app.production.carousel(self.carousel["id"])["slides"])
        stages = ["hook"] + ["show"] * (count - 2) + ["convert"]
        shots = ["close", "detail", "in_use"] + ["wide"] * (count - 3)
        for number in range(1, count + 1):
            modal.locator(f'[name="stage_{number}"]').select_option(stages[number - 1])
            modal.locator(f'[name="shot_{number}"]').select_option(shots[number - 1])
        modal.get_by_role("button", name="保存", exact=True).click()
        modal.wait_for(state="detached")
        page.locator('[data-step="slides"]').click()
        plan = page.locator("#carousel-plan")
        plan.get_by_text("商品展示").wait_for()
        self.assertIn("7 grit plates", plan.inner_text())
        self.assertEqual(page.locator("#carousel-plan-block .carousel-structure").count(), 0)
        self.assertEqual(page.locator(".carousel-table tbody tr").first.locator("td").nth(1).inner_text(), "首图钩子")
        saved = self.app.production.carousel(self.carousel["id"])
        self.assertEqual(saved["plan"]["tags"], [{"en": "7 grit plates", "zh": "7 块磨板"}])

        self.reviewer.mode = "fail"
        page.locator('[data-step="images"]').click()
        panel = page.locator("#carousel-group-panel")
        self.assertTrue(panel.is_visible())
        self.assertIn("整组结构检查通过", panel.inner_text())
        self.assertIn("会先按底图看", panel.inner_text())
        page.locator("#carousel-group-review").click()
        result = page.locator("#carousel-group-result")
        result.get_by_text("有未通过项").wait_for(timeout=15000)
        self.assertIn("删图检查", result.inner_text())
        self.assertIn("第 2 张换成使用场景", result.inner_text())
        self.assertEqual(page.locator("#carousel-group-review").inner_text(), "重新整组验收")
        self.assertTrue(self.reviewer.calls[-1]["read_only"])
        overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        self.assertLessEqual(overflow, 0)


if __name__ == "__main__":
    unittest.main()
