"""Feedback actions remain reviewable and only submit after their final button."""
import unittest

import test_review_ui as review_fixture
import test_plan_variant_binding as plan_fixture
import test_performance as performance
from test_ai_tasks import FakeBridge
import test_feedback_chain as feedback_fixture


class FeedbackChainBrowserTests(unittest.TestCase):
    setUpClass = classmethod(review_fixture.ReviewPageTests.setUpClass.__func__)
    tearDownClass = classmethod(review_fixture.ReviewPageTests.tearDownClass.__func__)
    stop_server = review_fixture.ReviewPageTests.stop_server
    request = review_fixture.ReviewPageTests.request
    open_page = review_fixture.ReviewPageTests.open_page
    direction = plan_fixture.PlanVariantHTTPTests.direction
    ok = feedback_fixture.FeedbackChainHTTPTests.ok
    chain = feedback_fixture.FeedbackChainHTTPTests.chain
    review = feedback_fixture.FeedbackChainHTTPTests.review

    def setUp(self):
        review_fixture.ReviewPageTests.setUp(self)
        self.video_id = performance.LINKED
        self.window = self.server.app.performance.video(self.video_id)["window_end"]
        self.server.app.ai.bridge = FakeBridge(output="已核对资料，建议下一轮先证明夹持。")

    def video_page(self, width=1080, ready="[data-feedback-save]"):
        return self.open_page(width=width,route=f"#/202H/videos?video={self.video_id}",ready=ready)

    def pick_direction(self, page, direction):
        more = page.locator('.feedback-more-actions')
        if not more.evaluate('node => node.open'):
            more.locator('summary').click()
        select = page.locator('[data-feedback-direction]')
        if select.count():
            page.wait_for_function('id => [...document.querySelectorAll("[data-feedback-direction] option")].some(o => o.value === id)',arg=direction["direction_id"])
            select.select_option(direction["direction_id"])

    def test_actions_show_first_with_figures_on_demand_and_save_conclusion_and_plan(self):
        direction = self.direction()
        page = self.video_page(width=390)
        page.locator('.perf-video-board').get_by_text('首两周播放').wait_for()
        page.locator('.feedback-compare').wait_for(state='attached')
        self.assertFalse(page.locator('.feedback-compare').is_visible())
        self.assertTrue(page.locator('.feedback-stage').is_visible())
        self.assertTrue(page.locator('[data-feedback-note]').is_visible())
        page.locator('.feedback-data-details > summary').click()
        self.assertTrue(page.locator('.feedback-compare').is_visible())
        self.assertEqual(page.locator('.feedback-figure-grid').count(), 0)  # Same scope appears once; outer metrics survive reload failures.
        self.assertEqual(self.server.app.review_followups.public()["entries"], [])
        page.locator('[data-feedback-note]').fill('先展示装夹稳定，再验证点击与成交。')
        self.pick_direction(page, direction)
        self.assertEqual(self.server.app.creation.public()["plans"], [])
        page.locator('[data-feedback-decide=execution]').click()
        page.locator('[data-feedback-redo]').wait_for()
        review = self.chain()["review"]
        self.assertEqual((review["note"], review["decision"]), ('先展示装夹稳定，再验证点击与成交。', "execution"))
        plans = self.server.app.creation.public()["plans"]
        self.assertEqual(len(plans),1)
        self.assertEqual((plans[0]["action"], plans[0]["status"]), ("execution", "pending"))
        self.assertEqual(plans[0]["source_review"]["note"],'先展示装夹稳定，再验证点击与成交。')
        self.assertEqual(self.server.app.ai.list_tasks(),[])
        page.locator('[data-feedback-write]').click()
        page.locator('#ai-create-dialog[open]').wait_for()
        self.assertEqual(page.locator('#ai-create-instruction').input_value(),'先展示装夹稳定，再验证点击与成交。')
        self.assertFalse(page.evaluate('document.documentElement.scrollWidth>innerWidth'))

    def test_ai_draft_fills_conclusion_without_promoting_optional_actions(self):
        self.server.app.ai.bridge = FakeBridge(output="对照偏弱。\n结论：开头太慢，装夹证明来得晚。\n建议动作：换执行")
        page = self.video_page()
        page.locator('[data-feedback-draft]').click()
        page.wait_for_function('() => document.querySelector("[data-feedback-note]")?.value === "开头太慢，装夹证明来得晚。"',timeout=20000)
        self.assertTrue(page.locator('[data-feedback-save].primary').is_visible())
        self.assertEqual(page.locator('[data-feedback-decide].primary').count(),0)
        self.assertFalse(page.locator('[data-feedback-decide=execution]').is_visible())
        self.assertEqual(page.locator('[data-feedback-note]').input_value(),'开头太慢，装夹证明来得晚。')
        tasks = self.server.app.ai.list_tasks()
        self.assertEqual(len(tasks),1)
        self.assertEqual(self.server.app.review_followups.public()["entries"], [])

    def test_review_heading_copies_the_video_id_and_the_drawer_has_no_separate_id_fold(self):
        page = self.video_page()
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        source = page.locator('#perf-video-drawer .perf-video-source')
        copy = source.locator('[data-copy-id]')
        copy.wait_for()
        self.assertFalse(page.locator('.feedback-data-details').evaluate('node => node.open'))
        self.assertEqual(copy.get_attribute('data-copy-id'), self.video_id)
        self.assertIn(self.video_id, source.inner_text())
        copy.click()
        page.get_by_text("视频 ID已复制").wait_for()
        self.assertEqual(page.evaluate("navigator.clipboard.readText()"), self.video_id)
        self.assertEqual(page.locator('.perf-video-id').count(), 0)
        # The direct TikTok link sits on the same line as the ID, and no longer among the bottom actions.
        link = source.locator('a[href*="tiktok.com"]')
        self.assertTrue(link.get_attribute('href').endswith(f'/video/{self.video_id}'))
        self.assertTrue(link.get_attribute('href').startswith('https://www.tiktok.com/@'))
        self.assertEqual(link.get_attribute('target'), '_blank')
        source.locator('[data-copy-video-link]').click()
        self.assertEqual(page.evaluate("navigator.clipboard.readText()"), link.get_attribute('href'))
        self.assertEqual(page.locator('#video-feedback-chain .feedback-source').count(), 0)
        self.assertEqual(page.locator('#perf-video-drawer .perf-video-actions a[href*="tiktok.com"]').count(), 0)
        self.assertNotIn("视频标识", page.locator('#perf-video-drawer').inner_text())

    def test_video_link_on_another_page_opens_the_drawer_in_place(self):
        page = self.open_page(route="#/202H/directions", ready="#content")
        before = page.evaluate("location.hash")
        page.evaluate("""id => {
          for (const [index, href] of [`#/202H/videos?video=${id}`, `#videos?video=${id}&feedback_window=2099-01-01`].entries()) {
            const link = document.createElement('a');link.href = href;link.className = 'test-video-link';link.textContent = href;
            link.style.cssText = `position:fixed;top:${4 + index * 32}px;left:4px;z-index:2147483647;background:#fff;color:#000;padding:4px`;
            document.body.append(link);
          }
        }""", self.video_id)
        page.locator('.test-video-link').first.click()
        drawer = page.locator('#perf-video-drawer-floating')
        drawer.locator('#video-feedback-chain').wait_for()
        self.assertTrue(drawer.evaluate('node => node.open'))
        self.assertEqual(page.evaluate("location.hash"), before, "The reader stays on the current module")
        self.assertEqual(page.locator('#perf-board, #perf-results').count(), 0, "The data page was never rendered")
        drawer.locator('.feedback-data-details > summary').click()
        self.assertIn(self.video_id, drawer.inner_text())
        drawer.locator('[data-drawer-close]').click()
        self.assertFalse(drawer.evaluate('node => node.open'))

    def test_drawer_can_be_dragged_wider_and_remembers_the_width(self):
        page = self.video_page()
        drawer = page.locator('#perf-video-drawer')
        handle = drawer.locator(':scope > .drawer-resize')
        handle.wait_for(state='attached')
        start = drawer.evaluate('node => node.getBoundingClientRect().width')
        box = handle.bounding_box()
        page.mouse.move(box['x'] + 4, box['y'] + 200)
        page.mouse.down()
        page.mouse.move(box['x'] - 120, box['y'] + 200, steps=6)
        page.mouse.up()
        widened = drawer.evaluate('node => node.getBoundingClientRect().width')
        self.assertAlmostEqual(widened, start + 120, delta=6)
        stored = page.evaluate("localStorage.getItem('workbench-drawer-width:video')")
        self.assertEqual(int(stored), round(widened))
        page.reload()
        page.locator('[data-feedback-save]').wait_for()
        self.assertAlmostEqual(page.locator('#perf-video-drawer').evaluate('node => node.getBoundingClientRect().width'), widened, delta=2)
        # The keyboard works too, the limits hold, and a double click returns to the default width.
        handle = page.locator('#perf-video-drawer > .drawer-resize')
        handle.focus()
        page.keyboard.press('ArrowLeft')
        self.assertAlmostEqual(page.locator('#perf-video-drawer').evaluate('node => node.getBoundingClientRect().width'), widened + 24, delta=2)
        page.evaluate("localStorage.setItem('workbench-drawer-width:video', '99999')")
        handle.dblclick()
        self.assertAlmostEqual(page.locator('#perf-video-drawer').evaluate('node => node.getBoundingClientRect().width'), 420, delta=2)
        self.assertIsNone(page.evaluate("localStorage.getItem('workbench-drawer-width:video')"))

    def test_removed_actions_and_judgment_controls_are_absent_from_mature_review(self):
        page = self.video_page()
        self.assertEqual(page.locator('[data-feedback-decide]').all_text_contents(), ['加码', '换执行', '停测'])
        self.assertFalse(page.locator('.feedback-more-actions').evaluate('node => node.open'))
        self.assertTrue(page.locator('[data-feedback-save]').is_visible())
        self.assertEqual(page.locator('[data-feedback-outcome], [data-feedback-evidence], [data-feedback-decide=observe], [data-feedback-decide=none]').count(), 0)
        page.locator('.feedback-more-actions > summary').click()
        for text in ('本条表现判断', '判断依据', '本轮暂无发现', '继续观察'):
            self.assertNotIn(text, page.locator('#video-feedback-chain').inner_text())
        self.assertEqual(self.server.app.review_followups.public()['entries'], [])
        self.assertEqual(self.server.app.creation.public()['plans'], [])

    def test_stop_without_direction_preserves_negative_review_and_creates_no_plan(self):
        self.assertFalse(self.chain()["summary"]["direction_id"])
        self.review(legacy=True, decision='stop', outcome='negative', evidence='preliminary')
        page = self.video_page(ready='[data-feedback-redo]')
        self.assertEqual(page.locator('[data-feedback-redo]').inner_text(),'编辑复盘')
        page.locator('[data-feedback-redo]').click()
        page.locator('[data-feedback-note]').fill('虽有成交，但剪辑与文案逻辑混乱、主题飘忽；本条流量期已结束。')
        page.locator('.feedback-more-actions > summary').click()
        self.assertEqual(page.locator('[data-feedback-direction]').input_value(), '')
        requests = []
        page.on('request', lambda request: requests.append(request.url) if request.method == 'POST' else None)
        page.locator('[data-feedback-decide=stop]').click()
        page.locator('[data-feedback-redo]').wait_for()
        saved = self.chain()["review"]
        self.assertEqual((saved["state"], saved["decision"]), ("reviewed", "stop"))
        self.assertEqual((saved["outcome"], saved["evidence"]), ("negative", "preliminary"))
        self.assertEqual(saved["note"], '虽有成交，但剪辑与文案逻辑混乱、主题飘忽；本条流量期已结束。')
        self.assertIn('已结束本条复盘', page.locator('.feedback-stage').inner_text())
        self.assertTrue(page.locator('[data-feedback-experience]').is_visible())
        self.assertEqual(self.server.app.creation.public()["plans"], [])
        self.assertEqual(self.server.app.ai.list_tasks(), [])
        self.assertFalse(any('/api/plans' in url for url in requests))

    def test_stop_with_selected_direction_leaves_direction_and_plans_unchanged(self):
        direction = self.direction()
        before = self.server.app.directions.read()
        page = self.video_page()
        self.pick_direction(page, direction)
        page.locator('[data-feedback-note]').fill('只结束这一条视频，保留方向供其他执行验证。')
        page.locator('[data-feedback-decide=stop]').click()
        page.locator('[data-feedback-redo]').wait_for()
        self.assertEqual(self.chain()["review"]["decision"], "stop")
        self.assertEqual(self.server.app.creation.public()["plans"], [])
        self.assertEqual(self.server.app.directions.read(), before)

    def test_home_list_has_no_retired_shortcuts(self):
        page = self.open_page(route='#/202H/home',ready='[data-home-tab]')
        page.locator('[data-home-tab=due]').click()
        page.locator('.followup-controls > summary').click()
        page.locator('[data-followup-filter]').select_option('pending')
        page.locator('[data-followup-chain]').first.wait_for()
        self.assertEqual(page.locator('[data-followup-none], [data-followup-observe]').count(), 0)
        self.assertEqual(self.server.app.review_followups.public()['entries'], [])
        self.assertEqual(self.server.app.ai.list_tasks(), [])

    def test_unknown_plan_save_retries_same_request_without_duplicate_plan(self):
        direction = self.direction()
        self.review()
        page = self.video_page(ready="[data-feedback-plan]")
        page.locator('[data-feedback-plan]').click()
        form = page.locator('.feedback-form-dialog')
        form.locator('[name=direction_id]').select_option(direction["direction_id"])
        requests = []

        def lost_response(route):
            if route.request.method != 'POST':
                return route.continue_()
            requests.append(route.request.post_data_json)
            response = route.fetch()
            if len(requests) == 1:
                route.fulfill(status=503,json={'error':'保存响应中断'})
            else:
                route.fulfill(response=response)

        page.route('**/api/plans',lost_response)
        form.get_by_role('button',name='保存下一轮计划',exact=True).click()
        form.get_by_text('结果尚未确认',exact=False).wait_for()
        self.assertTrue(form.locator('[name=note]').is_disabled())
        form.get_by_role('button',name='保存下一轮计划',exact=True).click()
        form.wait_for(state='detached')
        self.assertEqual(requests[0],requests[1])
        self.assertEqual(len(self.server.app.creation.public()["plans"]),1)
        self.assertEqual(self.server.app.ai.list_tasks(),[])

    def test_home_followup_opens_same_explicit_chain_without_creating_work(self):
        self.review()
        page = self.open_page(route='#/202H/home',ready='[data-home-tab]')
        page.locator('[data-home-tab=due]').click()
        page.locator('.followup-controls > summary').click()
        page.locator('[data-followup-filter]').select_option('reviewed')
        page.locator('[data-followup-chain]').first.click()
        page.locator('#perf-video-drawer-floating[open] [data-feedback-plan]').wait_for()
        self.assertEqual(self.server.app.creation.public()["plans"],[])
        self.assertEqual(self.server.app.ai.list_tasks(),[])

    def test_uncertain_plan_reopen_displays_frozen_variant_and_note_then_replays_same_request(self):
        direction = self.direction()
        self.review('原始复盘结论：先证明装夹。')
        page = self.video_page(ready="[data-feedback-plan]")
        page.locator('[data-feedback-plan]').click()
        form = page.locator('.feedback-form-dialog')
        form.locator('[name=direction_id]').select_option(direction["direction_id"])
        form.locator('[name=variant]').select_option('0')
        form.locator('[name=note]').fill('本轮保留的执行安排。')
        requests = []

        def lost_response(route):
            if route.request.method != 'POST':
                return route.continue_()
            requests.append(route.request.post_data_json)
            response = route.fetch()
            route.fulfill(status=503,json={'error':'响应中断'}) if len(requests) == 1 else route.fulfill(response=response)

        page.route('**/api/plans',lost_response)
        form.get_by_role('button',name='保存下一轮计划',exact=True).click()
        form.get_by_text('结果尚未确认',exact=False).wait_for()
        form.locator('[data-close]').click()
        form.wait_for(state='detached')
        self.review('后来修正的结论，不属于待核对请求。')
        page.locator('#video-feedback-chain [data-feedback-refresh]').click()
        page.locator('[data-feedback-plan]').wait_for()
        page.locator('[data-feedback-plan]').click()
        form.wait_for()
        self.assertEqual(form.locator('[name=direction_id]').input_value(),direction["direction_id"])
        self.assertEqual(form.locator('[name=variant]').input_value(),'0')
        self.assertTrue(form.locator('[name=variant]').is_disabled())
        self.assertEqual(form.locator('[name=note]').input_value(),'本轮保留的执行安排。')
        self.assertEqual(form.locator('blockquote').inner_text(),'原始复盘结论：先证明装夹。')
        form.get_by_role('button',name='保存下一轮计划',exact=True).click()
        form.wait_for(state='detached')
        self.assertEqual(requests[0],requests[1])
        self.assertEqual(len(self.server.app.creation.public()["plans"]),1)


if __name__ == '__main__':
    unittest.main()
