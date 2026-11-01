"""The review drawer's entrance: it opens at once, reads one video rather than the whole list, and starts early on a hover."""
from __future__ import annotations

import unittest

from playwright.sync_api import expect

import test_performance as performance
import test_public_stats_refresh_ui as base


class ReviewDrawerEntranceBrowserTests(base.PublicStatsRefreshBrowserTests):
    def requests_to(self, page, fragment):
        seen = []
        page.on("request", lambda request: seen.append(request.url) if fragment in request.url else None)
        return seen

    def test_in_place_drawer_opens_before_the_video_is_read_and_asks_only_for_that_video(self):
        page = self.surface(floating=True)
        page.wait_for_load_state("networkidle")
        target = "/api/performance/videos/" + performance.LINKED
        lists, details, held = [], [], []
        page.on("request", lambda request: lists.append(request.url) if "/api/performance/videos?" in request.url else None)
        page.on("request", lambda request: details.append(request.url) if request.url.endswith(target) else None)
        page.route("**" + target, lambda route: held.append(route))
        page.evaluate("id => { void CasePerformance.openVideoInPlace(id); }", performance.LINKED)
        # The drawer is up with a placeholder while the video is still being read.
        drawer = page.locator("#perf-video-drawer-floating[open]")
        drawer.wait_for()
        expect(drawer).to_contain_text("正在读取")
        for _ in range(50):
            if held:
                break
            page.wait_for_timeout(100)
        self.assertEqual(len(held), 1)
        held[0].continue_()
        drawer.locator("[data-feedback-note]").wait_for()
        self.assertEqual(lists, [], "the whole sales list must not be fetched for one video")
        # The figures just read are not read a second time for the same opening.
        page.wait_for_timeout(300)
        self.assertEqual(len(details), 1, details)
        self.assert_video(drawer, performance.LINKED, self.server.app.performance.video(performance.LINKED)["views"])
        page.close()

    def test_a_post_with_no_figures_only_says_so_and_the_reader_stays_put(self):
        page = self.surface(floating=True)
        page.wait_for_load_state("networkidle")
        before = page.evaluate("location.hash")
        page.evaluate("() => { void CasePerformance.openVideoInPlace('7999999999999999999'); }")
        page.locator(".toast-item").get_by_text("视频表和图文表里都查不到", exact=False).wait_for()
        self.assertEqual(page.locator("#perf-video-drawer-floating[open]").count(), 0)
        self.assertEqual(page.evaluate("location.hash"), before)
        # A photo link whose post no 图文 export has is answered the same way.
        page.evaluate("""() => { const a = document.createElement('a'); a.href = '#videos?photo=7999999999999999998';
          document.body.append(a); a.click(); a.remove(); }""")
        page.locator(".toast-item").get_by_text("图文数据里还没有这条帖子", exact=False).wait_for()
        self.assertEqual(page.evaluate("location.hash"), before)
        page.close()

    def test_a_registered_photo_post_opens_in_the_photo_tab(self):
        page = self.surface(floating=True)
        page.wait_for_load_state("networkidle")
        post = "7999999999999999997"
        page.route(f"**/api/performance/photo-items/{post}*", lambda route: route.fulfill(json={"item_id": post, "shop": "local"}))
        page.evaluate("id => { void CasePerformance.openVideoInPlace(id); }", post)
        page.wait_for_function("() => location.hash.includes('tab=photos')")
        self.assertEqual(page.locator("#perf-video-drawer-floating[open]").count(), 0)
        page.locator(".toast-item").get_by_text("这是一条图文帖子", exact=False).wait_for()
        page.close()

    def test_resting_on_a_row_asks_for_its_review_and_the_click_reuses_the_answer(self):
        page = self.surface(floating=False)
        page.wait_for_load_state("networkidle")
        chains = self.requests_to(page, "/api/feedback-chain")
        row = page.locator(f'[data-video-row="{performance.LINKED}"] .perf-title')
        row.hover()
        for _ in range(50):
            if chains:
                break
            page.wait_for_timeout(100)
        self.assertEqual(len(chains), 1, "hovering should ask for the review ahead of the click")
        row.click()
        drawer = self.drawer(page)
        drawer.locator("[data-feedback-note]").wait_for()
        page.wait_for_timeout(300)
        self.assertEqual(len(chains), 1, "the opened drawer must use the answer already read")
        page.close()

    def test_passing_over_rows_without_resting_asks_for_nothing(self):
        page = self.surface(floating=False)
        page.wait_for_load_state("networkidle")
        chains = self.requests_to(page, "/api/feedback-chain")
        for row in page.locator("[data-video-row] .perf-title").all()[:3]:
            row.hover()
            page.mouse.move(0, 0)
        page.wait_for_timeout(500)
        self.assertEqual(chains, [])
        page.close()


# Reuse the fixture without repeating the suite it comes from.
for _name in [name for name in vars(base.PublicStatsRefreshBrowserTests) if name.startswith("test_")]:
    if _name not in vars(ReviewDrawerEntranceBrowserTests):
        setattr(ReviewDrawerEntranceBrowserTests, _name, None)
del _name


if __name__ == "__main__":
    unittest.main()
