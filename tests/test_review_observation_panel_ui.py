"""Automatic observation stays passive; retired review operations are absent."""
from datetime import datetime, timezone
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


class ObservationPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def panel(self, *, mature=False, review=None, context=True, width=1000, scheme="light"):
        browser_context = self.browser.new_context(viewport={"width": width, "height": 950},
                                                   color_scheme=scheme, timezone_id="Asia/Shanghai")
        self.addCleanup(browser_context.close)
        page = browser_context.new_page()
        page.set_default_timeout(5000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.clock.install(time=datetime(2026, 10, 25, 4, tzinfo=timezone.utc))
        page.route("**/*", lambda route: route.fulfill(content_type="text/html", body='<main id="panel"></main>'))
        page.goto("http://127.0.0.1:43220/observation-panel-test")
        for name in ("style.css", "home.css", "feedback-chain.css"):
            page.add_style_tag(path=str(ROOT / "static" / name))
        page.evaluate("""({mature,review,hasContext}) => {
          window.esc = value => String(value ?? '').replace(/[&<>"']/g,
            c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
          window.toast = () => {};
          window.requests = [];
          window.chain = {product:'202H',video_id:'7691330683482017055',window_end:'2026-11-01',
            current_window:true,review_revision:'r1',review,source_review:review ? {note:review.note} : null,
            summary:{direction_id:'202H-S2',posted_on:'2026-10-22',metrics:{views:800,window_views:800,orders:0}},
            draft:null,retrospectives:[],next_tasks:[],plans:[],source_scripts:[],next_scripts:[],
            source_outputs:[],next_outputs:[],variants:[],cases:[]};
          if (hasContext) chain.review_context = {
            observation:{ends_on:mature?'2026-10-15':'2026-11-05',age_days:mature?24:3,
              observing:!mature,data_through:'2026-10-24',coverage_complete:mature},
            performance:{traffic:'播放较少',sales:'覆盖期内未出单'},
            evidence:{key:mature?'preliminary':'observing',label:mature?'可做初步判断':'观察尚未满14天',
              reason:mature?'数据已覆盖首14天，结论仍需复测。':'发布未满14天，继续收集表现。'},
            closing_allowed:mature};
          window.api = async (path, options = {}) => {
            if (options.method === 'POST') {
              requests.push({path,body:options.body});
              if (path === '/api/review-followups') {
                chain.review = {...options.body,at:'2026-10-25T12:00:00'};
                chain.source_review = chain.review.state === 'reviewed' ? {note:chain.review.note} : null;
                return {entry:chain.review};
              }
              if (path === '/api/plans') return {plan:{id:'PLAN-1'}};
              throw new Error('Unexpected POST '+path);
            }
            if (path.startsWith('/api/feedback-chain')) return structuredClone(chain);
            if (path.startsWith('/api/creation')) return {revision:'creation-r1'};
            throw new Error('Unexpected GET '+path);
          };
        }""", {"mature": mature, "review": review, "hasContext": context})
        page.add_script_tag(path=str(ROOT / "static" / "feedback-chain.js"))
        page.evaluate("FeedbackChain.mount(document.querySelector('#panel'), {video:{product:'202H',video_id:'7691330683482017055',window_end:'2026-11-01'}})")
        page.locator('.feedback-evidence').wait_for(state='attached')
        return page

    def assert_removed(self, page):
        self.assertEqual(page.locator('[data-feedback-decide]').all_text_contents(), ['加码', '换执行', '停测'])
        self.assertEqual(page.locator('[data-feedback-outcome], [data-feedback-evidence], [data-feedback-decide=observe], [data-feedback-decide=none], [name=next_review_on], [name=observation_reason]').count(), 0)
        for text in ('本条表现判断', '判断依据', '继续观察', '本轮暂无发现'):
            self.assertNotIn(text, page.locator('#panel').inner_text())

    def test_young_post_keeps_automatic_observation_without_manual_actions(self):
        page = self.panel()
        self.assertIn('发布第 4 天', page.locator('.feedback-stage').inner_text())
        self.assertIn('仍在观察期', page.locator('.feedback-stage').inner_text())
        self.assertIn('可以先下阶段结论', page.locator('.feedback-stage').inner_text())
        self.assertFalse(page.locator('.feedback-evidence').is_visible())
        self.assert_removed(page)
        # Concluding early is allowed, with a reminder of what the conclusion cannot rest on yet.
        self.assertTrue(page.locator('.feedback-early-warning').is_visible())
        self.assertTrue(page.locator('[data-feedback-save]').is_enabled())
        for action in ('scale', 'execution', 'stop'):
            self.assertTrue(page.locator(f'[data-feedback-decide={action}]').is_enabled())
        page.locator('.feedback-data-details > summary').click()
        self.assertIn('2026-11-05 满14天', page.locator('.feedback-observation-age').inner_text())
        self.assertIn('数据截至 2026-10-24', page.locator('.feedback-observation-age').inner_text())
        self.assertEqual(page.evaluate('requests'), [])

    def test_mature_review_primary_save_and_optional_actions_have_no_judgment_inputs(self):
        page = self.panel(mature=True)
        self.assert_removed(page)
        self.assertTrue(page.locator('[data-feedback-save]').is_visible())
        self.assertFalse(page.locator('[data-feedback-decide=stop]').is_visible())
        page.locator('.feedback-more-actions > summary').click()
        self.assert_removed(page)
        for action in ('scale', 'execution', 'stop'):
            self.assertTrue(page.locator(f'[data-feedback-decide={action}]').is_enabled())
        self.assertEqual(page.evaluate('requests'), [])

    def test_registered_post_without_sales_can_be_concluded_but_has_no_ai_action(self):
        page = self.panel()
        page.evaluate("""() => {chain.video = {registration_only:true};FeedbackChain.mount(document.querySelector('#panel'), {video:{product:'202H',video_id:chain.video_id,window_end:chain.window_end}});}""")
        page.get_by_text('补齐销售数据后可让 AI 起草复盘。', exact=False).wait_for()
        self.assertEqual(page.locator('[data-feedback-draft]').count(), 0)
        self.assert_removed(page)
        self.assertTrue(page.locator('.feedback-early-warning').is_visible())
        self.assertTrue(page.locator('[data-feedback-save]').is_enabled())

    def test_execution_submits_only_current_fields_then_creates_one_plan(self):
        page = self.panel(mature=True)
        page.locator('[data-feedback-note]').fill('本条的证明镜头不足，下一轮先证明夹持。')
        page.locator('.feedback-more-actions > summary').click()
        page.locator('[data-feedback-decide=execution]').click()
        page.locator('[data-feedback-redo]').wait_for()
        requests = page.evaluate('requests')
        self.assertEqual([r['path'] for r in requests], ['/api/review-followups', '/api/plans'])
        for key in ('outcome', 'evidence', 'next_review_on', 'observation_reason'):
            self.assertNotIn(key, requests[0]['body'])
        self.assertEqual(requests[0]['body']['decision'], 'execution')

    def test_legacy_closed_review_remains_readable_without_retired_controls(self):
        page = self.panel(mature=True, review={'state':'reviewed','decision':'none','note':'旧复盘结论。',
                                               'outcome':'negative','evidence':'preliminary'})
        self.assertIn('旧复盘结论', page.locator('.feedback-conclusion').inner_text())
        self.assertEqual(page.locator('.feedback-saved-judgment, [data-feedback-outcome], [data-feedback-evidence]').count(), 0)
        self.assertNotIn('本轮暂无发现', page.locator('#panel').inner_text())
        self.assertNotIn('有失败信号', page.locator('#panel').inner_text())
        self.assertEqual(page.evaluate('requests'), [])

    def test_legacy_stage_review_keeps_automatic_observation_warning_visible(self):
        page = self.panel(review={'state':'reviewed','decision':'none','note':'早期阶段记录。'})
        self.assertIn('仍在观察期', page.locator('.feedback-stage').inner_text())
        self.assertIn('已保存阶段结论', page.locator('.feedback-stage').inner_text())
        self.assertTrue(page.locator('.feedback-conclusion').is_visible())
        self.assertFalse(page.locator('.feedback-evidence').is_visible())
        self.assertEqual(page.evaluate('requests'), [])

    def test_missing_context_warns_and_narrow_dark_layout_fits(self):
        page = self.panel(context=False, width=390, scheme='dark')
        self.assertIn('请刷新工作台', page.locator('.feedback-early-warning').inner_text())
        self.assertTrue(page.locator('[data-feedback-save]').is_enabled())
        self.assertIn('证据待核对', page.locator('.feedback-stage').inner_text())
        self.assert_removed(page)
        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))

    def test_missing_direction_opens_optional_fields_without_writing(self):
        page = self.panel(mature=True)
        page.evaluate("chain.summary.direction_id='';FeedbackChain.mount(document.querySelector('#panel'), {video:{product:'202H',video_id:chain.video_id,window_end:chain.window_end}})")
        page.locator('[data-feedback-direction]').wait_for(state='attached')
        self.assertFalse(page.locator('[data-feedback-direction]').is_visible())
        for action in ('scale', 'execution'):
            page.locator('.feedback-more-actions > summary').click()
            page.locator(f'[data-feedback-decide={action}]').click()
            self.assertTrue(page.locator('[data-feedback-direction]').is_visible())
            self.assertTrue(page.locator('[data-feedback-direction]').evaluate('node => node === document.activeElement'))
            self.assertNotIn('停测', page.locator('[data-feedback-error]').inner_text())
            page.locator('.feedback-more-actions > summary').click()
        self.assertEqual(page.evaluate('requests'), [])

    def test_stop_on_bound_direction_finishes_only_the_video(self):
        page = self.panel(mature=True)
        page.locator('[data-feedback-note]').fill('本条结束，后续只保留这次经验。')
        page.locator('.feedback-more-actions > summary').click()
        page.locator('[data-feedback-decide=stop]').click()
        page.locator('[data-feedback-redo]').wait_for()
        requests = page.evaluate('requests')
        self.assertEqual([row['path'] for row in requests], ['/api/review-followups'])
        self.assertEqual(requests[0]['body']['state'], 'reviewed')
        self.assertEqual(requests[0]['body']['decision'], 'stop')
        self.assertEqual(requests[0]['body']['note'], '本条结束，后续只保留这次经验。')
        for key in ('outcome', 'evidence', 'next_review_on', 'observation_reason'):
            self.assertNotIn(key, requests[0]['body'])
        self.assertIn('已结束本条复盘', page.locator('.feedback-stage').inner_text())

    def test_ai_core_conclusion_is_visible_and_detailed_reasoning_is_folded(self):
        page = self.panel(mature=True)
        page.evaluate("""() => {
          chain.draft={status:'completed',task_id:'AIT-test',draft:{action:'execution',conclusion:'先补证明镜头。',
            card:{verdict:'开头有信号，证明镜头不足。',reasons:[{at:'3秒',point:'关键证据没有展开',fix:'补对照镜头'}],
              keep:['保留开头'],next:{change:'补证明镜头',hold:'保持同一开头'}}}};
          FeedbackChain.mount(document.querySelector('#panel'), {video:{product:'202H',video_id:chain.video_id,window_end:chain.window_end}});
        }""")
        page.locator('.feedback-draft-details').wait_for()
        # The one-line conclusion lives in the field; the card no longer repeats it as a verdict.
        self.assertEqual(page.locator('[data-feedback-note]').input_value(), '先补证明镜头。')
        self.assertEqual(page.locator('.retro-verdict').count(), 0)
        self.assertFalse(page.locator('.retro-reasons').is_visible())
        self.assertEqual(page.locator('.feedback-draft-details > summary').inner_text(), '分析与建议')
        page.locator('.feedback-draft-details > summary').click()
        self.assertTrue(page.locator('.retro-reasons').is_visible())
        self.assertEqual(page.evaluate('requests'), [])

    def test_ai_recommended_reference_value_preselects_without_saving(self):
        page = self.panel(mature=True)
        page.evaluate("""() => {
          chain.draft={status:'completed',task_id:'AIT-test',draft:{action:'',conclusion:'开头有效但没成交，保留开头、补证明。',
            reference_value:'medium',reference_reason:'播放高于同类，只出 2 单'}};
          FeedbackChain.mount(document.querySelector('#panel'), {video:{product:'202H',video_id:chain.video_id,window_end:chain.window_end}});
        }""")
        page.locator('.feedback-reference-ai').wait_for()
        self.assertEqual(page.locator('[data-feedback-reference]').input_value(), 'medium')
        self.assertIn('中等参考价值（播放高于同类，只出 2 单）', page.locator('.feedback-reference-ai').inner_text())
        self.assertEqual(page.evaluate('requests'), [])

    def test_unsaved_conclusion_survives_remount_and_clears_after_save(self):
        page = self.panel(mature=True)
        page.evaluate("localStorage.clear()")
        page.locator('[data-feedback-note]').fill('还没保存的判断')
        page.evaluate("FeedbackChain.mount(document.querySelector('#panel'), {video:{product:'202H',video_id:chain.video_id,window_end:chain.window_end}})")
        page.locator('[data-feedback-note]').wait_for()
        self.assertEqual(page.locator('[data-feedback-note]').input_value(), '还没保存的判断')
        page.locator('[data-feedback-reference]').select_option('weak')
        page.locator('[data-feedback-save]').click()
        page.locator('[data-feedback-redo]').wait_for()
        self.assertEqual(page.evaluate("Object.keys(localStorage).filter(key => key.startsWith('workbench.feedback-note:'))"), [])

    def test_retired_ai_suggestion_cannot_recreate_a_removed_action(self):
        page = self.panel(mature=True)
        page.evaluate("""() => {
          chain.draft={status:'completed',task_id:'AIT-test',draft:{action:'observe',conclusion:'等待数据。'}};
          FeedbackChain.mount(document.querySelector('#panel'), {video:{product:'202H',video_id:chain.video_id,window_end:chain.window_end}});
        }""")
        page.locator('[data-feedback-note]').wait_for()
        self.assert_removed(page)
        self.assertEqual(page.locator('[data-feedback-decide].primary').count(), 0)


if __name__ == '__main__':
    unittest.main()
