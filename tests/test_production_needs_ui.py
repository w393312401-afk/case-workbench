"""Specific work links locate current inputs without re-running jobs or seeking Resolve."""
import copy
import unittest

from playwright.sync_api import expect
import test_production_video_visuals_ui as video_fixture
import test_production_carousel_ui as carousel_fixture


class VideoNeedsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(video_fixture.ProductionVideoVisualsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(video_fixture.ProductionVideoVisualsBrowserTests.tearDownClass.__func__)
    setUp = video_fixture.ProductionVideoVisualsBrowserTests.setUp
    stop_server = video_fixture.ProductionVideoVisualsBrowserTests.stop_server
    request = video_fixture.ProductionVideoVisualsBrowserTests.request
    make_playable_video = video_fixture.ProductionVideoVisualsBrowserTests.make_playable_video
    edit = video_fixture.ProductionVideoVisualsBrowserTests.edit
    take = video_fixture.ProductionVideoVisualsBrowserTests.take
    voices = video_fixture.ProductionVideoVisualsBrowserTests.voices
    coverage = video_fixture.ProductionVideoVisualsBrowserTests.coverage
    open_page = video_fixture.ProductionVideoVisualsBrowserTests.open_page

    def test_gap_link_focuses_exact_gap_without_a_seek_or_generation(self):
        edit = self.coverage()
        page = self.open_page(width=390)
        link = page.locator(f'[data-need-key="gap-{edit["id"]}-0"]')
        expect(link).to_contain_text("切纸结果特写")
        link.evaluate("node=>node.click()")
        target = page.locator(f'[data-edit-id="{edit["id"]}"] [data-edit-gap="0"]')
        expect(target).to_be_visible()
        expect(target).to_have_class("production-target")
        self.assertTrue(target.evaluate("node=>node===document.activeElement"))
        self.assertEqual(self.calls, [])
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth>document.documentElement.clientWidth"))

    def test_changed_gap_range_does_not_focus_a_different_gap_at_same_index(self):
        edit = self.coverage()
        page = self.open_page()
        href = page.locator(f'[data-need-key="gap-{edit["id"]}-0"]').get_attribute("href")
        edit["summary"]["gaps"][0].update(start=120,end=150,seconds=4)
        page.evaluate("href=>location.hash=href",href)
        expect(page.locator("#production-target-note")).to_contain_text("已变化")
        self.assertEqual(page.locator(".production-target[data-edit-gap]").count(),0)
        self.assertEqual(self.calls, [])

    def test_superseded_gap_is_not_in_current_needs_and_selected_voice_target_is_exact(self):
        edit = self.coverage()
        old = copy.deepcopy(edit)
        old.update(id="EDT-old",report={"gaps":[{"need":"已被替代的缺口","start":0,"end":30}]},summary=None)
        self.payload["job"]["edits"].insert(0,old)
        page = self.open_page()
        self.assertNotIn("已被替代的缺口",page.locator("#production-needs").inner_text())
        page.locator('[data-need-key="voice-alignment"]').evaluate("node=>node.click()")
        target=page.locator('[data-take-id="TAK-first"]')
        expect(target).to_be_visible()
        self.assertTrue(target.evaluate("node=>node===document.activeElement"))
        self.assertEqual(self.calls, [])

    def test_frozen_historical_edit_link_opens_its_closed_history(self):
        edit=self.coverage()
        old=copy.deepcopy(edit)
        old["id"]="EDT-old"
        self.payload["job"]["edits"].insert(0,old)
        page=self.open_page()
        page.evaluate("href=>location.hash=href",f'#/202H/production/{video_fixture.JOB_ID}?step=edit&run=EDT-old')
        target=page.locator('[data-edit-id="EDT-old"]')
        expect(target).to_be_visible()
        self.assertTrue(target.evaluate("node=>node===document.activeElement"))
        self.assertEqual(self.calls,[])


class CarouselNeedsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(carousel_fixture.CarouselBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(carousel_fixture.CarouselBrowserTests.tearDownClass.__func__)
    setUp = carousel_fixture.CarouselBrowserTests.setUp
    stop_server = carousel_fixture.CarouselBrowserTests.stop_server
    request = carousel_fixture.CarouselBrowserTests.request
    open_page = carousel_fixture.CarouselBrowserTests.open_page
    make_playable_video = carousel_fixture.CarouselBrowserTests.make_playable_video

    def test_missing_base_links_directly_to_its_slide_without_importing(self):
        page=self.open_page(f'#production/{self.carousel["id"]}')
        posts=[]
        page.on("request",lambda request:posts.append(request.url) if request.method=="POST" else None)
        link=page.locator('[data-need-key="slide-base-2"]')
        expect(link).to_be_attached()
        link.evaluate("node=>node.click()")
        target=page.locator('[data-pick-slide="2"]')
        expect(target).to_be_visible()
        expect(target).to_have_class("pick-slide production-target")
        self.assertTrue(target.evaluate("node=>node===document.activeElement"))
        self.assertEqual(posts,[])
        self.assertIsNone(self.app.production.carousel(self.carousel["id"])["slides"][1].get("base"))


if __name__ == '__main__':
    unittest.main()
