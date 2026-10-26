"""Public-page pushes refresh cached video drawers without discarding review drafts.

All exports, snapshots and HTTP traffic use the disposable review-page fixture.
The browser starts with a fresh context and never uses the user's Chrome profile.
"""
from __future__ import annotations

import re
import unittest
from datetime import datetime, timedelta, timezone

from playwright.sync_api import expect

import test_performance as performance
import test_review_ui as fixture
import test_server as server_fixture


SECOND = "7600000000000000002"


class PublicStatsRefreshBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ReviewPageTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ReviewPageTests.tearDownClass.__func__)
    setUp = fixture.ReviewPageTests.setUp
    stop_server = fixture.ReviewPageTests.stop_server
    request = fixture.ReviewPageTests.request
    open_page = fixture.ReviewPageTests.open_page

    def surface(self, floating=False):
        page = self.open_page(
            route="#/202H/home" if floating else "#/202H/videos?tab=list",
            ready="[data-home-tab]" if floating else ".perf-table",
        )
        # Establish the first state revision before injecting a later push.
        page.wait_for_function("() => !state.polling && Boolean(state.digest)")
        return page

    def drawer(self, page, floating=False):
        selector = "#perf-video-drawer-floating" if floating else "#perf-video-drawer"
        return page.locator(selector + "[open]")

    def open_video(self, page, video_id=performance.LINKED, floating=False):
        if floating:
            page.evaluate("id => { void CasePerformance.openVideoInPlace(id); }", video_id)
        else:
            page.locator(f'[data-video-row="{video_id}"] .perf-title').click()
        return self.drawer(page, floating)

    def fact(self, drawer, label):
        return drawer.locator(".perf-video-board dt").filter(has_text=re.compile("^" + re.escape(label) + "$" )).locator("xpath=following-sibling::dd[1]")

    def table_metric(self, page, label, video_id=performance.LINKED):
        column = page.locator(".perf-video-table thead th").evaluate_all(
            "(nodes, label) => nodes.findIndex(node => node.textContent.replace(/[\\s↑↓↕]/g, '') === label)", label)
        self.assertGreaterEqual(column, 0, "Missing visible metric column: " + label)
        return page.locator(f'[data-video-row="{video_id}"] td').nth(column)

    def push(self, views, video_id=performance.LINKED, **fields):
        result = self.server.app.push_public_stats({"videos": [{"id": video_id, "views": views, **fields}]})
        self.assertEqual(result["saved"], 1, result)

    def assert_video(self, drawer, video_id, views):
        expect(drawer.locator('.perf-video-source [data-copy-label="视频 ID"]')).to_have_attribute("data-copy-id", video_id)
        expect(self.fact(drawer, "累计播放")).to_have_text(f"{views:,.0f}")

    def assert_public_time(self, container, observed):
        stamp = observed.isoformat(timespec="seconds").replace("+00:00", "Z")
        expect(container.locator("[data-public-stats-source] time[datetime]")).to_have_attribute("datetime", stamp)

    def test_opening_cached_video_reads_new_push_on_data_page_and_home(self):
        for floating in (False, True):
            with self.subTest(floating=floating):
                page = self.surface(floating)
                drawer = self.open_video(page, floating=floating)
                previous = self.server.app.performance.video(performance.LINKED)["views"]
                self.assert_video(drawer, performance.LINKED, previous)
                drawer.locator("[data-feedback-note]").wait_for()
                drawer.locator("[data-drawer-close]").click()
                # Both the data table and floating lookup have now cached this video.
                # No page refresh/poll is invoked between this push and reopening it.
                self.push(previous + 12345)
                drawer = self.open_video(page, floating=floating)
                self.assert_video(drawer, performance.LINKED, previous + 12345)
                expect(self.fact(drawer, "首两周播放")).to_have_text("12,000")
                page.close()

    def test_state_refresh_updates_only_figures_and_preserves_unsaved_review(self):
        for floating in (False, True):
            with self.subTest(floating=floating):
                page = self.surface(floating)
                drawer = self.open_video(page, floating=floating)
                note = drawer.locator("[data-feedback-note]")
                note.wait_for()
                # Finish the fresh drawer read before editing, then retain node identity.
                current = self.server.app.performance.video(performance.LINKED)["views"]
                self.assert_video(drawer, performance.LINKED, current)
                draft = "这段复盘还没有保存，继续核对开头和成交原因。"
                note.fill(draft)
                drawer.locator("[data-feedback-reference]").select_option("medium")
                page.evaluate("""() => {
                    window.savedStatsDrawer = document.querySelector('dialog.perf-video-drawer[open]');
                    window.savedStatsNote = savedStatsDrawer.querySelector('[data-feedback-note]');
                    window.statsDrawerCloses = 0;
                    savedStatsDrawer.addEventListener('close', () => statsDrawerCloses++);
                }""")
                before_url = page.url
                self.push(current + 10000)
                page.wait_for_function("() => !state.polling")
                page.evaluate("() => refresh(true)")
                self.assert_video(drawer, performance.LINKED, current + 10000)
                expect(note).to_have_value(draft)
                expect(drawer.locator("[data-feedback-reference]")).to_have_value("medium")
                self.assertTrue(page.evaluate("savedStatsDrawer === document.querySelector('dialog.perf-video-drawer[open]') && savedStatsNote === savedStatsDrawer.querySelector('[data-feedback-note]')"))
                self.assertEqual(page.evaluate("statsDrawerCloses"), 0)
                self.assertEqual(page.url, before_url)
                self.assertEqual(self.server.app.review_followups.public()["entries"], [])
                expect(self.fact(drawer, "首两周播放")).to_have_text("12,000")
                expect(self.fact(drawer, "订单")).to_have_text("4")
                page.close()

    def test_interaction_only_push_refreshes_all_counts_without_mixing_review_comments(self):
        labels = {"views": "累计播放", "likes": "点赞", "comments": "评论", "shares": "转发", "saves": "收藏"}
        base = datetime.now(timezone.utc) - timedelta(minutes=2)
        for index, floating in enumerate((False, True)):
            with self.subTest(floating=floating):
                first = {"views": 60000, "likes": 1250, "comments": 91, "shares": 304, "saves": 580}
                observed = base + timedelta(seconds=index * 60)
                self.push(**first, observedAt=observed.timestamp() * 1000)
                page = self.surface(floating)
                drawer = self.open_video(page, floating=floating)
                note = drawer.locator("[data-feedback-note]")
                note.wait_for()
                for key, label in labels.items():
                    expect(self.fact(drawer, label)).to_have_text(f"{first[key]:,}")
                    if not floating:
                        expect(self.table_metric(page, label)).to_have_text(f"{first[key]:,}")
                self.assert_public_time(drawer, observed)
                # The fixture contains three saved comments. That library count
                # must remain separate from the video's 91 public-page comments.
                expect(drawer.locator('.perf-reviews a[href*="reviews?"]')).to_have_text("评论库已收录 3 条")
                if not floating:
                    expect(page.locator(f'[data-video-row="{performance.LINKED}"] .perf-title')).to_contain_text("评论库已收录 3 条")
                draft = "互动数据刷新时，这份未保存的复盘草稿仍要保留。"
                note.fill(draft)
                drawer.locator("[data-feedback-reference]").select_option("weak")
                page.evaluate("""() => {
                    window.interactionDrawer = document.querySelector('dialog.perf-video-drawer[open]');
                    window.interactionNote = interactionDrawer.querySelector('[data-feedback-note]');
                }""")
                before_url = page.url

                updates = [
                    {"views": 60000, "likes": 1264, "comments": 105, "shares": 325, "saves": 602},
                    {"views": 60000, "likes": 0, "comments": None, "shares": None, "saves": 0},
                ]
                for update_index, counts in enumerate(updates, start=1):
                    observed = base + timedelta(seconds=index * 60 + update_index * 10)
                    self.push(**counts, observedAt=observed.timestamp() * 1000)
                    page.wait_for_function("() => !state.polling")
                    page.evaluate("() => refresh(true)")
                    for key, label in labels.items():
                        expected = "—" if counts[key] is None else f"{counts[key]:,}"
                        expect(self.fact(drawer, label)).to_have_text(expected)
                        if not floating:
                            expect(self.table_metric(page, label)).to_have_text(expected)
                    self.assert_public_time(drawer, observed)
                    expect(note).to_have_value(draft)
                    expect(drawer.locator("[data-feedback-reference]")).to_have_value("weak")
                    expect(drawer.locator('.perf-reviews a[href*="reviews?"]')).to_have_text("评论库已收录 3 条")
                    self.assertTrue(page.evaluate("interactionDrawer === document.querySelector('dialog.perf-video-drawer[open]') && interactionNote === interactionDrawer.querySelector('[data-feedback-note]')"))
                    self.assertEqual(page.url, before_url)
                    self.assertEqual(self.server.app.review_followups.public()["entries"], [])
                    expect(self.fact(drawer, "首两周播放")).to_have_text("12,000")
                    expect(self.fact(drawer, "订单")).to_have_text("4")
                page.close()

    def test_case_without_sales_rows_shows_public_counts_and_refreshes_interactions(self):
        current = server_fixture.new_case_snapshot(self.video)
        video_id = current["metrics"]["video_id"]
        case = self.store.add({
            "title": "仅有公开页指标的案例", "product": "SHARPAL 202H",
            "original": {"text": "This case has public stats only.", "source": "浏览器回归夹具"},
            "feedback": {"kind": "editorial", "text": "验证公开页互动不依赖销售表。"},
            "current": current,
        })
        original = self.store.read_case(case["case_id"])["current"]
        observed = datetime.now(timezone.utc) - timedelta(minutes=1)
        counts = {"views": 45678, "likes": 123, "comments": 45, "shares": 6, "saves": 7}
        self.push(**counts, video_id=video_id, observedAt=observed.timestamp() * 1000)
        self.assertIsNone(self.server.app.performance.video(video_id))
        page = self.open_page(route=f'#case/{case["case_id"]}', ready=".detail-heading")
        page.locator('.detail-tabs [data-tab="data"]').click()
        panel = page.locator("[data-case-public-stats]")
        labels = {"views": "播放量", "likes": "点赞", "comments": "评论", "shares": "转发", "saves": "收藏"}

        def metric(label):
            return panel.locator(".metric-label").filter(has_text=re.compile("^" + re.escape(label) + "$")).locator("..").locator(".metric-value")

        for key, label in labels.items():
            expect(metric(label)).to_have_text(str(counts[key]))
        expect(panel.locator("time[datetime]")).to_have_attribute("datetime", observed.isoformat(timespec="seconds").replace("+00:00", "Z"))
        page.wait_for_function("() => !state.polling && Boolean(state.digest)")
        counts.update(likes=0, comments=46, shares=None, saves=0)
        observed += timedelta(seconds=20)
        self.push(**counts, video_id=video_id, observedAt=observed.timestamp() * 1000)
        page.evaluate("() => refresh(true)")
        for key, label in labels.items():
            expect(metric(label)).to_have_text("—" if counts[key] is None else str(counts[key]))
        expect(panel.locator("time[datetime]")).to_have_attribute("datetime", observed.isoformat(timespec="seconds").replace("+00:00", "Z"))
        self.assertEqual(self.store.read_case(case["case_id"])["current"], original)
        self.assertIsNone(self.server.app.performance.video(video_id))

    def test_late_single_video_read_cannot_replace_newer_drawer(self):
        for floating in (False, True):
            with self.subTest(floating=floating):
                page = self.surface(floating)
                # Prime any list cache so the held read is specifically the fresh detail read.
                drawer = self.open_video(page, floating=floating)
                drawer.locator("[data-feedback-note]").wait_for()
                drawer.locator("[data-drawer-close]").click()
                target = "/api/performance/videos/" + performance.LINKED
                status, _, old_response = self.request("GET", target)
                self.assertEqual(status, 200)
                held = []
                page.route("**" + target, lambda route: held.append(route))
                with page.expect_request("**" + target):
                    self.open_video(page, floating=floating)
                # Use the dialog element if it is already mounted; the read may still be loading.
                if drawer.count():
                    drawer.locator("[data-drawer-close]").click()
                newer = self.open_video(page, SECOND, floating)
                expected = self.server.app.performance.video(SECOND)["views"]
                self.assert_video(newer, SECOND, expected)
                newer.locator("[data-feedback-note]").wait_for()
                newer.locator("[data-feedback-note]").fill("第二条视频的草稿。")
                self.assertEqual(len(held), 1)
                held[0].fulfill(json=old_response)
                # Flush the fulfilled response and its queued rendering continuation.
                page.wait_for_timeout(150)
                self.assert_video(newer, SECOND, expected)
                expect(newer.locator("[data-feedback-note]")).to_have_value("第二条视频的草稿。")
                self.assertEqual(page.locator("dialog.perf-video-drawer[open]").count(), 1)
                page.close()

    def test_inflight_list_reload_cannot_overwrite_a_new_public_stats_revision(self):
        for initial_load in (False, True):
            with self.subTest(initial_load=initial_load):
                # Home has not initialized CasePerformance's list; the data-page
                # variant already has a list when its next load starts.
                page = self.surface(floating=initial_load)
                page.wait_for_load_state("networkidle")
                status, _, stale = self.request("GET", "/api/performance/videos")
                self.assertEqual(status, 200)
                previous = next(v["views"] for v in stale["videos"] if v["video_id"] == performance.LINKED)
                held, requests = [], []

                def delay_first_list(route):
                    requests.append(route.request.url)
                    if len(requests) == 1:
                        held.append(route)
                    else:
                        route.continue_()

                page.route("**/api/performance/videos?*", delay_first_list)
                with page.expect_request("**/api/performance/videos?*"):
                    page.evaluate("""() => {
                        history.replaceState(null, '', '#/202H/videos?tab=list');
                        window.statsReloadDone = false;
                        void CasePerformance.route().finally(() => { window.statsReloadDone = true; });
                    }""")
                # The request event precedes Playwright's route callback.
                for _ in range(100):
                    if held:
                        break
                    page.wait_for_timeout(10)
                self.assertEqual(len(held), 1)
                self.push(previous + 6789)
                page.evaluate("() => CasePerformance.publicStatsChanged()")
                if not initial_load:
                    # The new read finishes before the older load is released.
                    expect(self.table_metric(page, "累计播放")).to_have_text(f"{previous + 6789:,.0f}")
                    self.assertGreaterEqual(len(requests), 2)
                else:
                    # No existing list: the revision still invalidates the held
                    # initial read, even though publicStatsChanged has no rows yet.
                    self.assertEqual(len(requests), 1)
                held[0].fulfill(json=stale)
                page.wait_for_function("() => window.statsReloadDone")
                expect(self.table_metric(page, "累计播放")).to_have_text(f"{previous + 6789:,.0f}")
                self.assertGreaterEqual(len(requests), 2 if initial_load else 3)
                page.close()


if __name__ == "__main__":
    unittest.main()
