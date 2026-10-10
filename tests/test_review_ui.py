"""Browser checks for the user-review page, using an isolated temporary library and made-up reviews.

Run with: python -m unittest discover -s tests -p test_review_ui.py -v
"""
from __future__ import annotations

import unittest
import time
from datetime import date
from urllib.parse import parse_qs, urlencode

from playwright.sync_api import sync_playwright

import test_performance as performance
import test_server as fixture
from test_review_store import build_sources, write


class ReviewPageTests(unittest.TestCase):
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        sources = self.runtime / "reviews" / "sources"
        build_sources(sources)
        video = f"https://www.tiktok.com/@bushcraftedge/video/{performance.LINKED}"

        def comment(cid, text):
            return {"cid": cid, "videoWebUrl": video, "text": text, "diggCount": 5, "createTimeISO": "2026-06-04T10:00:00.000Z", "uniqueId": "someone"}
        # two comments under a video the video data knows (one is a question nobody answered), and one that names competitors
        write(sources, "tiktok_video", "asked.json", [comment("Q1", "Will this do swords?"), comment("L2", "That clamp looks flimsy"),
                                                       comment("V1", "I have a Lansky and a Work Sharp, this one is easier.")])
        performance.build_exports(self.root / "exports")
        self.server.app.performance.sources = [self.root / "exports"]
        self.assertEqual(self.request("POST", "/api/performance/sync", {})[0], 200)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def open_page(self, width=1080, route="#reviews", ready=".rev-card"):
        context = self.browser.new_context(viewport={"width": width, "height": 800})
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=self.base)
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        page.goto(f"{self.base}/{route}")
        page.locator(ready).first.wait_for()
        return page

    def overflows(self, page):
        return page.evaluate("() => document.documentElement.scrollWidth > document.documentElement.clientWidth")

    def count(self, page, text):
        page.locator(f"#rev-count:has-text('{text}')").wait_for()

    def card(self, page, text):
        return page.locator(".rev-card", has_text=text).first

    # The less common filters live in a drawer, and the less common card actions in a "···" menu.
    def open_drawer(self, page):
        page.locator("#rev-filter-open").click()
        page.locator("#rev-drawer[open]").wait_for()

    def close_drawer(self, page):
        page.locator("#rev-drawer-done").click()
        page.locator("#rev-drawer[open]").wait_for(state="detached")

    def card_action(self, card, name):
        card.locator(".rev-menu summary").click()
        card.get_by_role("menuitem", name=name).click()

    def assert_review_route(self, page, **filters):
        path, _, query = page.evaluate("location.hash").partition("?")
        self.assertEqual(path, "#/202H/reviews")
        self.assertEqual(parse_qs(query), {key: [value] for key, value in filters.items()})

    def test_chinese_search_says_what_it_searched_for(self):
        page = self.open_page()
        page.fill("#rev-q", "晃动")
        page.locator("#rev-query-note").get_by_text("「晃动」也按").wait_for()
        self.assertIn("wobbl", page.locator("#rev-query-note").inner_text())
        self.count(page, "1 条")
        self.assertEqual(page.locator(".rev-text mark").first.inner_text().lower(), "wobbl")  # the English word is marked in the review
        page.fill("#rev-q", "重要")
        page.locator("#rev-query-note").get_by_text("没有对应的英文词").wait_for()
        self.count(page, "0 条")

    def test_unanswered_questions_and_conversations(self):
        page = self.open_page()
        page.get_by_role("button", name="没人回复的提问").click()
        self.count(page, "1 条")
        self.assertEqual(page.locator(".rev-badge-open").inner_text(), "没人回复")
        self.assertEqual(page.locator("[data-thread]").count(), 0)  # nothing to expand under a question nobody answered
        self.assertEqual(page.locator("#rev-replied").input_value(), "no")

        page.locator("[data-reset]").click()
        self.open_drawer(page)
        page.select_option("#rev-replied", "yes")
        self.close_drawer(page)
        self.count(page, "1 条")
        button = page.locator("[data-thread]")
        self.assertEqual(button.inner_text().strip(), "展开对话（1）")
        button.click()
        reply = page.locator(".rev-thread:not([hidden]) .rev-reply.is-brand")
        reply.wait_for()
        self.assertIn("Thanks for watching!", reply.inner_text())
        self.assertEqual(button.get_attribute("aria-expanded"), "true")
        button.click()  # a second click closes it again
        page.locator(".rev-thread:not([hidden])").wait_for(state="detached")
        self.assertEqual(button.get_attribute("aria-expanded"), "false")

    def test_a_reply_can_show_the_comment_it_answers(self):
        page = self.open_page()
        self.open_drawer(page)
        page.get_by_label("品牌或达人账号的回复").check()
        self.close_drawer(page)
        page.fill("#rev-q", "Thanks for watching")
        page.locator("[data-thread]:has-text('看上下文')").click()
        parent = page.locator(".rev-thread:not([hidden]) .rev-reply.is-parent")
        parent.wait_for()
        self.assertIn("That’s a lot of wobble", parent.inner_text())

    def test_comments_show_their_video_and_can_be_filtered_by_it(self):
        page = self.open_page()
        chip = page.locator(f".rev-video-chip[data-video='{performance.LINKED}']").first
        self.assertIn("Is A Mirror Edge Overrated", chip.inner_text())
        line = page.locator(".rev-context", has=chip).first.inner_text()
        self.assertIn("播放 12K", line)
        self.assertIn("订单 4", line)
        self.assertIn("CTOR 1.33%", line)
        chip.click()
        banner = page.locator(".rev-video-bar")
        banner.wait_for()
        text = banner.inner_text()
        for expected in ("@bushcraftedge", "发布 2026-08-31", "GMV $600", "库里有 3 条评论", "其中 1 个提问没人回复"):
            self.assertIn(expected, text)
        self.assertEqual(banner.get_by_role("link", name="查看案例").get_attribute("href"), f"#case/{self.identifier}")
        self.count(page, "3 条")
        self.assert_review_route(page, video=performance.LINKED)
        self.assertEqual(page.locator("#rev-video").input_value(), performance.LINKED)
        self.assertEqual(page.locator(".rev-video-chip").count(), 0)  # inside one video the line would only repeat itself
        page.get_by_role("button", name="看全部视频").click()
        banner.wait_for(state="detached")
        self.assert_review_route(page)

    def test_a_link_with_filters_opens_that_view_and_bad_values_are_ignored(self):
        page = self.open_page(route=f"#reviews?video={performance.LINKED}&nature=question&q=swords")
        self.count(page, "1 条")
        self.assertEqual(page.locator("#rev-video").input_value(), performance.LINKED)
        self.assertEqual(page.locator("#rev-q").input_value(), "swords")
        self.assertEqual(page.locator("[data-chip='nature'][data-code='question']").get_attribute("aria-pressed"), "true")
        self.assert_review_route(page, video=performance.LINKED, nature="question", q="swords")
        # Reload the canonical product route: the video and text query must survive.
        page.reload()
        self.count(page, "1 条")
        self.assertEqual(page.locator("#rev-video").input_value(), performance.LINKED)
        self.assertEqual(page.locator("#rev-q").input_value(), "swords")
        self.assert_review_route(page, video=performance.LINKED, nature="question", q="swords")
        # the page is already showing: a link followed from here applies too
        page.evaluate("() => { location.hash = '#reviews?topic=stability'; }")
        self.count(page, "1 条")
        self.assertEqual(page.locator("[data-chip='topic'][data-code='stability']").get_attribute("aria-pressed"), "true")
        self.assertEqual(page.locator("#rev-video").input_value(), "")
        self.assertEqual(page.locator("#rev-q").input_value(), "")
        self.assert_review_route(page, topic="stability")
        # values the library does not have are dropped instead of hiding everything
        page.goto(f"{self.base}/#reviews?video=abc&sort=nonsense&topic=zzz&rating=9")
        page.reload()
        page.locator(".rev-card").first.wait_for()
        self.count(page, "10 条")
        self.assert_review_route(page)

    def test_initial_route_waits_for_a_delayed_review_module(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)

        def delayed_module(route):
            # Reproduce app.js executing before the later deferred page module.
            time.sleep(0.4)
            route.continue_()

        context.route("**/reviews.js", delayed_module)
        page = context.new_page()
        page.add_init_script("""(() => {
          const original = window.fetch;
          window.reviewModuleReadyAtBootstrap = [];
          window.fetch = function(input, options) {
            if (String(input).startsWith('/api/state'))
              window.reviewModuleReadyAtBootstrap.push(Boolean(window.CaseReviews));
            return original.call(this, input, options);
          };
        })();""")
        page.goto(f"{self.base}/#reviews?topic=stability")
        self.count(page, "1 条")
        self.assertIn("wobble", page.locator(".rev-card").inner_text())
        self.assert_review_route(page, topic="stability")
        self.assertIs(page.evaluate("reviewModuleReadyAtBootstrap[0]"), True)

    def test_product_switch_clears_previous_video_and_sku_filters(self):
        for key, value, expected in (("video", performance.LINKED, "3 条"),
                                     ("sku", "Leather Strop Refill", "1 条")):
            with self.subTest(filter=key):
                page = self.open_page(route="#/all/reviews?" + urlencode({key: value}))
                self.count(page, expected)
                self.assertEqual(page.locator(f"#rev-{key}").input_value(), value)
                page.locator("#global-product").select_option("202H")
                self.count(page, "10 条")
                self.assertEqual(page.locator(f"#rev-{key}").input_value(), "")
                self.assertEqual(page.locator("#global-product").input_value(), "202H")
                self.assert_review_route(page)

    def test_untagged_reviews_and_competitor_tags_can_be_listed(self):
        page = self.open_page()
        self.open_drawer(page)
        competitor = page.locator("[data-chip='competitor'][data-code='lansky']")
        self.assertEqual(competitor.inner_text().split()[-1], "1")
        competitor.click()
        self.count(page, "1 条")
        self.assertIn("Lansky", self.card(page, "I have a Lansky").inner_text())
        page.locator("#rev-reset").click()
        self.count(page, "10 条")  # the list is back to everything: the chips below are counted on that
        untagged = page.locator("[data-chip='topic'][data-code='none']")
        shown = int(untagged.inner_text().split()[-1])
        untagged.click()
        self.count(page, f"{shown} 条")
        self.assertTrue(all("price" not in t for t in page.locator(".rev-card .rev-tag-topic").all_inner_texts()))  # none of them has a topic tag

    def test_tag_changes_by_hand_are_kept_and_marked(self):
        page = self.open_page()
        wobble = self.card(page, "That’s a lot of wobble")
        self.assertIn("晃动、不稳、底座", wobble.locator(".rev-tags").inner_text())
        self.card_action(wobble, "改标签")
        editor = wobble.locator(".rev-tag-editor")
        editor.wait_for()
        editor.locator("[data-kind='topic'][data-code='stability']").click()
        page.locator("#rev-records:has-text('手动改过 1 处标签')").wait_for()
        wobble = self.card(page, "That’s a lot of wobble")
        self.assertNotIn("晃动、不稳、底座", wobble.locator(".rev-tags").inner_text())
        self.assertEqual(wobble.locator(".rev-tag-editor [data-kind='topic'][data-code='stability']").get_attribute("class").count("is-manual"), 1)
        page.reload()  # kept on the server, not just in the page
        page.locator("#rev-records:has-text('手动改过 1 处标签')").wait_for()
        self.assertNotIn("晃动、不稳、底座", self.card(page, "That’s a lot of wobble").locator(".rev-tags").inner_text())
        self.card_action(self.card(page, "That’s a lot of wobble"), "改标签")
        self.card(page, "That’s a lot of wobble").locator("[data-kind='topic'][data-code='stability']").click()  # back to what the rules said
        page.locator("#rev-records:empty").wait_for(state="attached")  # an empty note is hidden, so only its presence can be awaited
        self.assertIn("晃动、不稳、底座", self.card(page, "That’s a lot of wobble").locator(".rev-tags").inner_text())

    def test_picked_quotes_are_noted_and_copied_with_their_source(self):
        page = self.open_page()
        self.card(page, "That’s a lot of wobble").get_by_role("button", name="摘录").click()
        picked = self.card(page, "That’s a lot of wobble").get_by_role("button", name="已摘录")
        picked.wait_for()
        self.assertEqual(picked.get_attribute("aria-pressed"), "true")
        identifier = self.card(page, "That’s a lot of wobble").get_attribute("data-id")
        page.locator("#rev-basket-toggle").click()
        basket = page.locator("#material-basket-dialog")
        field = basket.locator("[data-material-note]")
        field.wait_for()
        field.fill("夹具晃的画面别用")
        basket.locator("[data-material-save]").click()
        page.locator("#toast:has-text('备注已保存')").wait_for()
        basket.locator("[data-material-copy]").click()
        page.wait_for_function("() => navigator.clipboard.readText().then((t) => t.includes('备注'))")
        copied = page.evaluate("() => navigator.clipboard.readText()")
        for value in ("That’s a lot of wobble", identifier, "夹具晃的画面别用", "评论里的说法均未核实"):
            self.assertIn(value, copied)
        basket.locator("[data-material-close]").click()
        page.reload()
        page.locator("#rev-basket-toggle").click()
        basket.locator("[data-material-note]").wait_for()
        self.assertEqual(basket.locator("[data-material-note]").input_value(), "夹具晃的画面别用")
        basket.locator("[data-material-clear]").click()
        self.assertEqual(basket.locator("[data-material-clear]").inner_text(), "再点一次，清空当前产品")
        basket.locator("[data-material-clear]").click()
        basket.get_by_role("heading", name="还没有素材").wait_for()
        basket.locator("[data-material-close]").click()
        unpicked = self.card(page, "That’s a lot of wobble").get_by_role("button", name="摘录").first
        self.assertEqual(unpicked.get_attribute("aria-pressed"), "false")

    def test_the_video_list_links_to_the_comments_under_each_video(self):
        page = self.open_page(route="#videos", ready=".perf-tabs")
        page.locator(".perf-tabs [data-perf-tab=list]").click()
        page.locator(".perf-table").wait_for()
        page.select_option("#perf-range", "all")
        link = page.locator("td.perf-reviews", has=page.locator("a")).first
        self.assertEqual(link.locator("a").inner_text(), "3 条")
        self.assertIn("1 个没人回复", link.inner_text())
        link.locator("a").click()
        page.locator(".rev-video-bar").wait_for()
        self.count(page, "3 条")
        self.assertIn("Is A Mirror Edge Overrated", page.locator(".rev-video-bar").inner_text())

    def test_the_case_page_points_at_the_comments_under_its_own_video(self):
        page = self.open_page(route=f"#case/{self.identifier}/data", ready="#case-review-box")
        box = page.locator("#case-review-box")
        box.wait_for()
        self.assertIn("这条案例的视频下有 3 条评论，其中 1 个提问没人回复", box.inner_text())
        box.get_by_role("link", name="只看这条视频的评论").click()
        page.locator(".rev-video-bar").wait_for()
        self.count(page, "3 条")

    def test_review_cards_fit_a_phone_screen(self):
        page = self.open_page(width=390)
        self.assertFalse(self.overflows(page))
        self.open_drawer(page)
        page.select_option("#rev-replied", "yes")
        self.close_drawer(page)
        page.locator("[data-thread]").first.wait_for()
        self.assertFalse(self.overflows(page))  # the extra conversation button must wrap, not widen the page
        page.locator("[data-reset]").click()
        self.card_action(self.card(page, "That’s a lot of wobble"), "改标签")
        self.card(page, "That’s a lot of wobble").locator(".rev-tag-editor").wait_for()
        page.locator(f".rev-video-chip[data-video='{performance.LINKED}']").first.click()
        page.locator(".rev-video-bar").wait_for()
        page.locator("#rev-basket-toggle").click()
        self.assertFalse(self.overflows(page))  # nor the tag editor, the video bar or the basket


if __name__ == "__main__":
    unittest.main()
