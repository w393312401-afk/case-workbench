"""A link to one timeline's 图文 (#production/<id>?step=cut&photo=<uid>) opens that 图文, not just the order."""
import unittest

from test_production_validation_ui import ProductionValidationBrowserTests, JOB_ID


class PhotoLinkBrowserTests(ProductionValidationBrowserTests):
    def test_photo_link_opens_that_timelines_carousel(self):
        self.payload["job"]["timelines"] = [
            {"unique_id": "uid-a", "name": "Lab-A", "creator": "Lab", "markers": 1, "frames": 60, "fps": "29.97", "take_id": "", "built_at": "2026-10-11"},
            {"unique_id": "uid-own", "name": "Lab261011-202-1", "creator": "Lab", "markers": 1, "frames": 60, "fps": "29.97", "take_id": "", "built_at": "2026-10-11"}]
        page = self.open_page()
        page.evaluate(f"() => location.hash = '#production/{JOB_ID}?step=cut&photo=uid-own'")
        dialog = page.locator("dialog.photo-carousel-dialog")
        dialog.wait_for()
        self.assertEqual(dialog.locator("h2").inner_text(), "Lab261011-202-1")
        self.assertEqual(page.locator('.production-steps [aria-selected="true"]').get_attribute("data-stage"), "review")
        # The link's photo= is dropped, so closing or refreshing does not open the window again.
        self.assertNotIn("photo=", page.evaluate("location.hash"))
        dialog.locator("[data-close]").click()
        page.wait_for_timeout(500)
        self.assertEqual(page.locator("dialog.photo-carousel-dialog").count(), 0)

    def test_link_to_a_timeline_no_longer_on_the_order_says_so(self):
        page = self.open_page()
        page.evaluate(f"() => location.hash = '#production/{JOB_ID}?step=cut&photo=gone'")
        page.locator("#production-target-note").get_by_text("已不在当前制作单", exact=False).wait_for()
        self.assertEqual(page.locator("dialog.photo-carousel-dialog").count(), 0)


if __name__ == "__main__":
    unittest.main()
