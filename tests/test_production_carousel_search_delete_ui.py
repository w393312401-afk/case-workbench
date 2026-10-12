"""Search history on a carousel order: newest run open, older folded, delete and cleanup with a confirmation; slides in a grid."""
from __future__ import annotations

from pathlib import Path
import time
import unittest
from uuid import uuid4

from playwright.sync_api import expect

import test_production_carousel_ui as carousel_fixture


class SearchHistoryBrowserTests(unittest.TestCase):
    setUpClass = classmethod(carousel_fixture.CarouselBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(carousel_fixture.CarouselBrowserTests.tearDownClass.__func__)
    setUp = carousel_fixture.CarouselBrowserTests.setUp
    stop_server = carousel_fixture.CarouselBrowserTests.stop_server
    request = carousel_fixture.CarouselBrowserTests.request
    open_page = carousel_fixture.CarouselBrowserTests.open_page
    make_playable_video = carousel_fixture.CarouselBrowserTests.make_playable_video

    def run_search(self):
        skill = Path(self.app.edit_skill_dir).parent / "tiktok-photo-commerce"
        if not (skill / "SKILL.md").is_file():
            (skill / "references").mkdir(parents=True, exist_ok=True)
            (skill / "SKILL.md").write_text("\n".join(["---", "name: tiktok-photo-commerce", "---", "# 图文证据边界"]), encoding="utf-8")
        self.app.start_still_search(self.carousel["id"], {"request_id": uuid4().hex, "materials": ["蓝点折刀成品素材"], "per_slide": 2})
        deadline = time.monotonic() + 10
        while self.app.edit_running() and time.monotonic() < deadline:
            time.sleep(.05)
        return self.app.production.carousel(self.carousel["id"])["searches"][-1]

    def test_older_runs_fold_and_each_can_be_deleted_after_confirming(self):
        first, second = self.run_search(), self.run_search()
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.locator('[data-step="pick"]').click()
        page.locator(".still-history > summary").click()
        older = page.locator("#search-runs details.edit-older")
        expect(older.locator(":scope > summary")).to_contain_text("较早的 1 次找底图")
        expect(page.locator("#search-runs > .edit-card")).to_have_count(1)
        expect(page.locator(f'#search-runs > .edit-card[data-activity-source="{second["id"]}"]')).to_have_count(1)
        older.locator(":scope > summary").click()
        older.locator("[data-delete-search]").click()
        dialog = page.locator("dialog[open]")
        expect(dialog).to_contain_text("删除这次找画面记录")
        expect(dialog).to_contain_text("不会改动达芬奇")
        dialog.locator("[data-submit]").click()
        page.wait_for_function("() => !document.querySelector('#search-runs details.edit-older')", timeout=15000)
        remaining = [entry["id"] for entry in self.app.production.carousel(self.carousel["id"])["searches"]]
        self.assertEqual(remaining, [second["id"]])
        self.assertNotIn(first["id"], remaining)

    def test_cleanup_keeps_only_the_newest_and_slides_sit_in_a_grid(self):
        self.run_search()
        self.run_search()
        newest = self.run_search()
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.locator('[data-step="pick"]').click()
        grid = page.locator("#pick-panel .pick-slides")
        expect(grid).to_have_count(1)
        columns = grid.evaluate("node => getComputedStyle(node).gridTemplateColumns.split(' ').length")
        self.assertGreaterEqual(columns, 2)
        self.assertEqual(page.locator("#pick-panel .pick-slide").count(), len(self.app.production.carousel(self.carousel["id"])["slides"]))
        page.locator(".still-history > summary").click()
        page.locator("#search-runs details.edit-older > summary").click()
        page.locator("[data-clean-searches]").click()
        dialog = page.locator("dialog[open]")
        expect(dialog).to_contain_text("清理 2 次旧的找画面记录")
        dialog.locator("[data-submit]").click()
        page.wait_for_function("() => !document.querySelector('#search-runs details.edit-older')", timeout=15000)
        self.assertEqual([entry["id"] for entry in self.app.production.carousel(self.carousel["id"])["searches"]], [newest["id"]])

    def test_large_image_opens_in_place_and_leftover_candidates_are_cleared(self):
        search = self.run_search()
        carousel = self.app.production.carousel(self.carousel["id"])
        first = carousel["slides"][0]["candidates"][0]
        self.app.choose_base(self.carousel["id"], {"request_id": uuid4().hex, "revision": self.app.production.public()["revision"],
                                                   "slide": 1, "candidate": first["id"]})
        self.app.import_folder_image(self.carousel["id"], {"request_id": uuid4().hex, "slide": 1, "path": "202H-S2/原图/03.jpg"})
        self.assertEqual(len(self.app.production.carousel(self.carousel["id"])["slides"][0]["candidates"]), 2)
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.locator('[data-step="pick"]').click()
        page.wait_for_function("() => document.querySelectorAll('#pick-panel .pick-slide:first-child .pick-card').length === 1", timeout=15000)
        self.assertEqual(len(self.app.production.carousel(self.carousel["id"])["slides"][0]["candidates"]), 1)
        pages = len(page.context.pages)
        page.locator("#pick-panel .pick-card a").first.click()
        lightbox = page.locator("dialog.image-lightbox img")
        expect(lightbox).to_be_visible()
        self.assertEqual(len(page.context.pages), pages)
        self.assertIn("#production/", page.url.replace("#/202H/", "#"))
        page.keyboard.press("Escape")
        expect(page.locator("dialog.image-lightbox")).to_have_count(0)
        self.assertTrue(search["id"])

    def test_archived_order_can_be_deleted_permanently_with_its_folder(self):
        self.run_search()
        folder = self.app.production.root / "carousels" / self.carousel["id"]
        self.assertTrue(folder.is_dir())
        self.app.archive_carousel(self.carousel["id"], {"request_id": uuid4().hex, "revision": self.app.production.public()["revision"], "archived": True})
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.locator("#carousel-delete").click()
        dialog = page.locator("dialog[open]")
        expect(dialog).to_contain_text("永久删除制作单")
        expect(dialog).to_contain_text("不能撤销")
        dialog.locator("[data-submit]").click()
        self.assertTrue(folder.is_dir())  # the confirmation box is required
        dialog.locator('input[name="sure"]').check()
        dialog.locator("[data-submit]").click()
        page.wait_for_function("() => !location.hash.includes('CAR-')", timeout=15000)
        self.assertFalse(folder.exists())
        self.assertEqual(self.app.production.public()["carousels"], [])

    def test_unarchived_order_can_be_deleted_without_archiving_first(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.locator("#carousel-archive").wait_for()
        self.assertEqual(page.locator("#carousel-delete").count(), 1)


if __name__ == "__main__":
    unittest.main()
