"""Review queue date and campaign filters through the real controls, without touching saved data."""
from datetime import datetime, timezone
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
ARTIFACTS = ROOT / ".test-artifacts" / "review-followup-filters"


def video(identifier, posted, due, title=None, status="follow"):
    return {"video_id": identifier, "product": "202H", "posted_on": posted,
            "window_end": due, "headline": title or identifier, "account": "test-account",
            "status": status, "status_label": "跟进", "window_views": 1000, "orders": 2, "last_data": "2026-10-23"}


CAMPAIGNS = [
    {"id": "TKC-manual-september", "name": "人工登记九月活动", "start": "2026-09-17",
     "end": "2026-09-27", "confidence": "manual", "verified": True},
    {"id": "TKC-custom-future", "name": "另一个未来活动", "start": "2026-11-11",
     "end": "2026-11-30", "confidence": "planning", "verified": False},
]


class ReviewFollowupFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def open_queue(self, *, width=1100, scheme="light", failures=0, backlog=False, expand=True):
        context = self.browser.new_context(viewport={"width": width, "height": 850},
                                           color_scheme=scheme, timezone_id="Asia/Shanghai")
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(5000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        page.clock.install(time=datetime(2026, 10, 23, 4, tzinfo=timezone.utc))
        page.route("**/*", lambda route: route.fulfill(content_type="text/html; charset=utf-8", body="""
            <main style="margin:0;min-height:100vh;padding:16px">
              <section class="section-card home-section home-work" style="margin-top:0">
                <div class="home-panel"><h1>待复盘</h1><div id="queue"></div></div>
              </section>
            </main>"""))
        page.goto("http://127.0.0.1:43220/review-followup-filters")
        for name in ("style.css", "home.css"):
            page.add_style_tag(path=str(STATIC / name))
        page.evaluate("""({campaigns, failures}) => {
          window.esc = value => String(value ?? '').replace(/[&<>"']/g,
            char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
          window.campaignRequests = 0;
          window.api = async path => {
            if (path !== '/api/campaigns') throw new Error('Unexpected fixture API: ' + path);
            if (++window.campaignRequests <= failures) throw new Error('活动读取暂时失败');
            return {campaigns, revision:'campaign-r1'};
          };
        }""", {"campaigns": CAMPAIGNS, "failures": failures})
        for name in ("perf-dashboard.js", "perf-date-picker.js", "campaigns.js", "review-followups.js"):
            page.add_script_tag(path=str(STATIC / name))
        videos = [
            video("before", "2026-09-01", "2026-09-13"),
            video("start-edge", "2026-09-10", "2026-09-17", "活动起日边界"),
            video("inside", "2026-09-15", "2026-09-20", "Beta 活动前发布"),
            video("end-edge", "2026-09-27", "2026-10-11", "活动末日边界"),
            video("after", "2026-09-28", "2026-10-11"),
            video("just-before", "2026-09-10", "2026-09-16"),
            video("missing", "2026-09-16", "2026-09-27", "Beta 缺销量", "incomplete"),
            video("reviewed", "2026-09-17", "2026-09-27", "已处理活动视频"),
            video("unknown-post", None, "2026-09-27", "发布日期未记录"),
        ]
        if backlog:
            videos += [video(f"older-{index}", "2026-08-01", "2026-08-16") for index in range(25)]
        page.evaluate("""videos => {
          window.queueFixture = {videos, entries:[{video_id:'reviewed',product:'202H',
            window_end:'2026-09-27',state:'reviewed',decision:'none',note:'已处理'}],
            revision:'r1',week:{today:'2026-10-23',start:'2026-10-19',end:'2026-10-25'},
            scripts:[],tasks:[],refresh:async () => {},url:hash => hash};
          window.mountQueue = () => ReviewFollowups.mount(document.querySelector('#queue'), queueFixture);
          mountQueue();
        }""", videos)
        page.locator("#followup-date-trigger").wait_for(state='attached')
        if expand:
            self.open_filters(page)
        return page

    def open_filters(self, page):
        controls = page.locator('.followup-controls')
        if controls.get_attribute('open') is None:
            controls.locator('summary').first.click()

    def ids(self, page):
        return page.locator("[data-followup-chain]").evaluate_all(
            "nodes => nodes.map(node => node.dataset.followupChain.split(':')[1])")

    def choose_campaign(self, page):
        page.wait_for_function("document.querySelector('[data-followup-campaign] option[value=\"TKC-manual-september\"]')")
        page.locator("[data-followup-campaign]").select_option("TKC-manual-september")

    def show_day(self, page, day):
        panel = page.locator("#followup-date .perf-date-panel")
        for _ in range(24):
            cell = panel.locator(f'[data-date="{day}"]')
            if cell.count():
                return cell
            first = panel.locator("[data-date]").first.get_attribute("data-date")
            panel.locator(f'[data-nav="{-1 if day < first else 1}"]').click()
        self.fail(f"Could not navigate the date picker to {day}")

    def dates(self, page, start, end=None, mode="custom"):
        page.locator("#followup-date-trigger").click()
        page.locator(f'#followup-date [data-mode="{mode}"]').click()
        self.show_day(page, start).click()
        if mode == "custom":
            self.show_day(page, end or start).click()

    def test_due_dates_are_inclusive_and_custom_range_can_be_chosen_backwards(self):
        page = self.open_queue()
        self.assertEqual(page.locator(".followup-tools > span").count(), 0)
        self.dates(page, "2026-09-27", "2026-09-17")
        self.assertEqual(set(self.ids(page)), {"start-edge", "inside"})
        self.assertIn("2026-09-17", page.locator("#followup-date-trigger").inner_text())
        self.assertIn("2026-09-27", page.locator("#followup-date-trigger").inner_text())
        self.assertEqual(page.locator(".followup-tools > span").inner_text(), "2 条")
        self.assertIn("2 条待补数据", page.locator(".followup-waiting").inner_text())
        # This is the window's due date, not its earlier publication date.
        self.dates(page, "2026-09-17", mode="day")
        self.assertEqual(self.ids(page), ["start-edge"])
        self.assertEqual(page.locator(".followup-waiting").count(), 0)

    def test_campaign_overlap_combines_with_search_status_and_date(self):
        page = self.open_queue()
        self.choose_campaign(page)
        self.assertEqual(set(self.ids(page)), {"start-edge", "inside", "end-edge"})
        hint = page.locator(".followup-filter-hint").inner_text()
        self.assertIn("2026-09-17", hint)
        self.assertIn("2026-09-27", hint)
        self.assertIn("观察窗", hint)
        self.assertIn("1 条待补数据", page.locator(".followup-waiting").inner_text())
        page.locator("[data-followup-search]").fill("Beta")
        self.assertEqual(self.ids(page), ["inside"])
        self.assertIn("1 条待补数据", page.locator(".followup-waiting").inner_text())
        page.locator("[data-followup-show-waiting]").click()
        self.assertEqual(set(self.ids(page)), {"inside", "missing"})
        self.dates(page, "2026-09-20", mode="day")
        self.assertEqual(self.ids(page), ["inside"])
        page.locator("[data-followup-filter]").select_option("reviewed")
        self.assertEqual(self.ids(page), [])
        self.assertEqual(page.locator(".followup-tools > span").inner_text(), "0 条")
        page.locator("[data-followup-reset]").click()
        self.assertEqual(page.locator("[data-followup-filter]").input_value(), "ready")
        self.assertEqual(page.locator("[data-followup-search]").input_value(), "")
        self.assertEqual(page.locator("[data-followup-campaign]").input_value(), "")
        self.assertEqual(page.locator(".followup-tools > span").count(), 0)
        self.assertEqual(len(self.ids(page)), 5)
        self.assertEqual(page.locator('.followup-pager > span').inner_text(), '1–5 / 共 6 条')

    def test_filter_changes_reset_pagination_and_remount_preserves_choices(self):
        page = self.open_queue(backlog=True)
        page.locator("[data-followup-next]").click()
        self.assertEqual(page.locator(".followup-pager > span").inner_text(), "6–10 / 共 31 条")
        self.choose_campaign(page)
        self.assertFalse(page.locator('.followup-pager').is_visible())
        self.dates(page, "2026-09-17", "2026-09-27")
        page.locator("[data-followup-search]").fill("Beta")
        page.locator("[data-followup-filter]").select_option("pending")
        page.evaluate("mountQueue()")
        self.assertEqual(page.locator("[data-followup-campaign]").input_value(), "TKC-manual-september")
        self.assertEqual(page.locator("[data-followup-search]").input_value(), "Beta")
        self.assertEqual(page.locator("[data-followup-filter]").input_value(), "pending")
        self.assertIn("2026-09-17", page.locator("#followup-date-trigger").inner_text())
        self.assertEqual(set(self.ids(page)), {"inside", "missing"})

    def test_campaign_failure_has_retry_and_uses_the_registered_calendar(self):
        page = self.open_queue(failures=1)
        page.locator("[data-followup-campaign-retry]").wait_for()
        self.assertEqual(len(self.ids(page)), 5, "Calendar failure must not hide the review queue")
        self.assertEqual(page.locator('.followup-pager > span').inner_text(), '1–5 / 共 6 条')
        page.locator("[data-followup-campaign-retry]").click()
        self.choose_campaign(page)
        self.assertEqual(page.evaluate("campaignRequests"), 2)
        self.assertEqual(set(self.ids(page)), {"start-edge", "inside", "end-edge"})
        page.locator("[data-followup-campaign]").select_option("TKC-custom-future")
        self.assertEqual(self.ids(page), [])
        self.assertEqual(page.locator(".followup-tools > span").inner_text(), "0 条")

    def test_week_month_presets_max_date_and_separate_picker_instance(self):
        page = self.open_queue()
        # A hidden video page must not steal the home picker's events or calendar.
        page.evaluate("""() => {
          const hidden = document.createElement('div'); hidden.hidden = true;
          hidden.innerHTML = PerfDatePicker.trigger('all', {}); document.body.append(hidden);
          PerfDatePicker.bind({period:()=>'all',summary:()=>({}),choose:()=>{throw Error('wrong date picker')}});
        }""")
        self.dates(page, "2026-09-17", mode="week")
        self.assertEqual(set(self.ids(page)), {"start-edge", "inside", "just-before"})
        self.dates(page, "2026-09-17", mode="month")
        self.assertEqual(set(self.ids(page)), {"before", "start-edge", "inside", "just-before"})
        page.locator("#followup-date-trigger").click()
        self.assertTrue(self.show_day(page, "2026-10-25").is_enabled())
        self.assertTrue(self.show_day(page, "2026-10-26").is_disabled())
        page.locator("#followup-date .perf-date-presets").get_by_role("button", name="近 7 天", exact=True).click()
        self.assertIn("近 7 天", page.locator("#followup-date-trigger").inner_text())
        page.locator("#followup-date-trigger").click()
        page.locator("#followup-date .perf-date-presets").get_by_role("button", name="全部周期", exact=True).click()
        self.assertEqual(len(self.ids(page)), 5)
        self.assertEqual(page.locator('.followup-pager > span').inner_text(), '1–5 / 共 6 条')

    def test_light_dark_and_narrow_layout_screenshots(self):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        for scheme, width in (("light", 1100), ("dark", 1100), ("light", 390), ("dark", 390)):
            with self.subTest(scheme=scheme, width=width):
                page = self.open_queue(width=width, scheme=scheme)
                self.choose_campaign(page)
                self.dates(page, "2026-09-17", "2026-09-27")
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                page.screenshot(path=str(ARTIFACTS / f"queue-{scheme}-{width}.png"), full_page=True)
                page.locator("#followup-date-trigger").click()
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                panel = page.locator("#followup-date .perf-date-panel")
                bounds = panel.bounding_box()
                self.assertGreaterEqual(bounds["x"], 0)
                self.assertLessEqual(bounds["x"] + bounds["width"], width)
                page.screenshot(path=str(ARTIFACTS / f"calendar-{scheme}-{width}.png"), full_page=True)

    def test_new_posts_boundary_missing_dates_and_early_reviews_have_distinct_queues(self):
        page=self.open_queue()
        page.evaluate("""() => {
          const base={product:'202H',window_end:'2026-10-18',last_data:'2026-10-23',status:'focus',window_views:12000,orders:3};
          queueFixture.videos=[
            {...base,video_id:'age13',headline:'发布13天阶段结论',posted_on:'2026-10-10'},
            {...base,video_id:'age14',headline:'发布14天可复盘',posted_on:'2026-10-09'},
            {...base,video_id:'new',headline:'新登记暂无销量',posted_on:'2026-10-22',window_end:'2026-11-01',registration_only:true,last_data:null,views:null,orders:null},
            {...base,video_id:'unknown',headline:'缺发布日期',posted_on:null},
            {...base,video_id:'future',headline:'未来日期待核对',posted_on:'2026-10-24'}
          ];
          queueFixture.entries=[{product:'202H',video_id:'age13',window_end:'2026-10-18',state:'reviewed',decision:'none',at:'2026-10-20T12:00:00+08:00',note:'已有阶段结论'}];
          mountQueue();
        }""")
        self.open_filters(page)
        self.assertEqual(self.ids(page),['age14'])
        self.assertEqual(page.locator('[data-followup-queue=ready] span').inner_text(),'1')
        self.assertEqual(page.locator('[data-followup-queue=observing] span').inner_text(),'2')
        self.assertEqual(page.locator('[data-followup-queue=missing] span').inner_text(),'2')
        page.locator('[data-followup-queue=observing]').click()
        self.assertEqual(set(self.ids(page)),{'age13','new'})
        self.assertEqual(page.locator('[data-followup-none]').count(),0)
        self.assertIn('已保留阶段复盘',page.locator('[data-followup-video=age13]').inner_text())
        page.locator('[data-followup-video=new] .followup-details > summary').click()
        self.assertIn('尚无销售数据',page.locator('[data-followup-video=new]').inner_text())
        self.assertEqual(page.locator('[data-followup-observe]').count(),0)
        page.locator('[data-followup-video=new] [data-followup-edit]').click()
        dialog=page.locator('.followup-dialog')
        self.assertEqual(dialog.locator('[name=state]').input_value(),'deferred')
        # An unfinished observation can still be marked reviewed; the dialog only warns once 已复盘 is picked.
        self.assertIsNone(dialog.locator('[name=state] option[value=reviewed]').get_attribute('disabled'))
        self.assertFalse(dialog.locator('[data-early-warning]').is_visible())
        dialog.locator('[name=state]').select_option('reviewed')
        self.assertIn('可再改', dialog.locator('[data-early-warning]').inner_text())
        self.assertEqual(dialog.locator('[name=next_review_on],[name=observation_reason],[name=outcome],[name=evidence]').count(),0)
        self.assertEqual(dialog.locator('[name=state] option[value=observing]').count(),0)
        dialog.locator('[data-close]').click()
        page.locator('[data-followup-queue=missing]').click()
        self.assertEqual(set(self.ids(page)),{'unknown','future'})
        self.assertIn('发布时间在未来',page.locator('[data-followup-video=future]').inner_text())

    def test_registration_only_title_opens_feedback_without_navigating_to_missing_sales_details(self):
        page=self.open_queue()
        page.evaluate("""() => {
          const base={product:'202H',posted_on:'2026-10-01',window_end:'2026-10-11',last_data:'2026-10-23',status:'follow',window_views:4000,orders:2};
          queueFixture.videos=[{...base,video_id:'normal',headline:'有销量数据的视频'},
            {...base,video_id:'registered',headline:'新登记暂无销量',posted_on:'2026-10-22',registration_only:true,last_data:null,orders:null}];
          queueFixture.entries=[];
          window.openedFeedback=[];
          window.CasePerformance={openVideoInPlace:(id,options)=>openedFeedback.push({id,window:options.feedbackWindow,refresh:typeof options.onClose,changed:typeof options.onChanged})};
          mountQueue();
        }""")
        self.open_filters(page)
        page.locator('[data-followup-filter]').select_option('all')
        normal=page.locator('[data-followup-video=normal] .home-row-main > a')
        self.assertEqual(normal.get_attribute('href'),'#videos?tab=list&video=normal')
        self.assertIsNone(normal.get_attribute('data-followup-registration'))
        title=page.locator('[data-followup-video=registered] .home-row-main > a')
        self.assertEqual(title.get_attribute('href'),'#home')
        before=page.url
        title.click()
        self.assertEqual(page.url,before)
        self.assertEqual(page.evaluate('openedFeedback'),[{'id':'registered','window':'2026-10-11','refresh':'function','changed':'function'}])
        page.locator('[data-followup-video=registered] [data-followup-chain]').click()
        self.assertEqual(page.evaluate('openedFeedback.length'),2)

    def test_legacy_manual_schedule_does_not_hide_mature_videos_or_replace_automatic_dates(self):
        page = self.open_queue(width=390, scheme='dark')
        page.evaluate("""() => {
          const base={product:'202H',window_end:'2026-10-18',last_data:'2026-10-23',status:'follow',window_views:4000,orders:2};
          queueFixture.videos=[{...base,video_id:'mature',headline:'已到复盘日',posted_on:'2026-10-01'},
            {...base,video_id:'young',headline:'自动观察中的视频',posted_on:'2026-10-22'}];
          queueFixture.entries=queueFixture.videos.map(video=>({...video,state:'observing',decision:'observe',
            next_review_on:'2027-12-31',observation_reason:'旧的等待条件',note:'原备注保留'}));
          mountQueue();
        }""")
        self.assertEqual(self.ids(page), ['mature'])
        self.open_filters(page)
        page.locator('[data-followup-queue=observing]').click()
        self.assertEqual(self.ids(page), ['young'])
        self.assertIn('2026-11-05 首轮复盘', page.locator('.followup-reason').inner_text())
        self.assertNotIn('2027-12-31', page.locator('.followup-reason').inner_text())
        self.assertNotIn('旧的等待条件', page.locator('.followup-reason').inner_text())
        page.locator('.followup-details > summary').click()
        self.assertIn('原备注保留', page.locator('.followup-row').inner_text())
        self.assertEqual(page.locator('[data-followup-observe],[data-followup-none]').count(), 0)
        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(ARTIFACTS / 'removed-actions-dark-390.png'), full_page=True)

    def test_default_view_focuses_five_reasons_and_actions_with_all_other_controls_collapsed(self):
        page = self.open_queue(expand=False)
        rows = page.locator('.followup-row')
        self.assertEqual(rows.count(), 5)
        self.assertFalse(page.locator('[data-followup-filter]').is_visible())
        self.assertFalse(page.locator('[data-followup-campaign]').is_visible())
        self.assertEqual(page.locator('.followup-controls').get_attribute('open'), None)
        self.assertEqual(rows.first.locator('.followup-first').inner_text(), '先看这条')
        self.assertTrue(rows.first.locator('.followup-reason').is_visible())
        self.assertEqual(rows.first.locator('button:visible').count(), 1)
        self.assertTrue(rows.first.locator('[data-followup-chain]').is_visible())
        self.assertFalse(rows.first.locator('.followup-assessment').is_visible())
        self.assertEqual(rows.first.locator('[data-followup-observe],[data-followup-none]').count(),0)
        first = self.ids(page)
        page.locator('[data-followup-next]').click()
        last = self.ids(page)
        self.assertEqual(len(last), 1)
        self.assertEqual(len(set(first + last)), 6)
        self.assertEqual(page.locator('.followup-pager > span').inner_text(), '6–6 / 共 6 条')


if __name__ == "__main__":
    unittest.main()
