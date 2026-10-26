"""AI carousel handoff remains visible and follows background work without manual generation."""
import json
from pathlib import Path
import unittest
from uuid import uuid4

from playwright.sync_api import sync_playwright

import test_production_carousel_ui as fixture


class CarouselAutopilotBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.CarouselBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.CarouselBrowserTests.tearDownClass.__func__)
    setUp = fixture.CarouselBrowserTests.setUp
    stop_server = fixture.CarouselBrowserTests.stop_server
    request = fixture.CarouselBrowserTests.request
    open_page = fixture.CarouselBrowserTests.open_page
    make_playable_video = fixture.CarouselBrowserTests.make_playable_video

    def open_autopilot(self, run=None):
        self.path = f"/api/production/carousels/{self.carousel['id']}"
        self.payload = self.request("GET", self.path)[2]
        self.payload['carousel']['autopilot_runs'] = [run] if run else []
        if run and run.get('delivery_id'):
            self.payload['carousel']['deliveries'] = [{'id': run['delivery_id'], 'current': True}]
        self.calls = []
        page = self.open_page('#library')
        page.route('**' + self.path, lambda route: route.fulfill(
            content_type='application/json', body=json.dumps(self.payload)))

        def post(route):
            self.calls.append({'url': route.request.url, 'body': route.request.post_data_json})
            if route.request.url.endswith('/cancel'):
                self.payload['carousel']['autopilot_runs'][-1].update(status='cancelled', phase='', message='已保留完成的图片。')
            else:
                self.payload['carousel']['autopilot_runs'].append(self.autopilot_run())
            route.fulfill(content_type='application/json', body='{}')

        page.route('**' + self.path + '/autopilot**', post)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#production/{self.carousel['id']}")
        page.locator('#carousel-autopilot-panel').wait_for()
        return page

    @staticmethod
    def autopilot_run(**extra):
        return {'id': 'AUTO-111111111111', 'status': 'running', 'phase': 'AI 选图',
                'message': '正在比较候选画面', 'error': '', 'started_at': '2026-10-19T10:00:00+08:00',
                'finished_at': '', 'delivery_id': '', **extra}

    def test_start_follows_every_stage_without_generation_and_preserves_image_draft(self):
        page = self.open_autopilot()
        page.locator('[data-step="images"]').click()
        page.locator('#carousel-image-form textarea[name="instruction"]').fill('Keep this draft')
        page.evaluate("window.keptImageForm = document.querySelector('#carousel-image-form')")
        page.locator('[data-autopilot-start]').click()
        page.locator('[data-autopilot-cancel]').wait_for()
        self.assertEqual(len(self.calls), 1)
        self.assertIn('revision', self.calls[0]['body'])
        self.assertNotIn('workflow', self.calls[0]['body'], 'the button must follow the saved contract instead of forcing background editing')
        self.assertGreaterEqual(len(self.calls[0]['body']['request_id']), 10)
        self.assertTrue(page.evaluate("window.keptImageForm === document.querySelector('#carousel-image-form')"))
        self.assertEqual(page.locator('#carousel-image-form textarea[name="instruction"]').input_value(), 'Keep this draft')
        self.assertEqual(self.payload['carousel']['generations'], [])
        self.payload['carousel']['autopilot_runs'][-1].update(phase='AI 核对', message='正在核对文字和产品')
        page.locator('#carousel-autopilot-panel').get_by_text('正在核对文字和产品', exact=True).first.wait_for()
        self.payload['carousel']['deliveries'] = [{'id': 'DLV-111111111111', 'current': True}]
        self.payload['carousel']['autopilot_runs'][-1].update(status='completed', phase='已完成', message='3 张成品已整理', delivery_id='DLV-111111111111')
        link = page.locator('#carousel-autopilot-panel').get_by_role('link', name='下载成品包')
        link.wait_for()
        self.assertEqual(link.get_attribute('href'), self.path + '/deliveries/DLV-111111111111/download')
        self.assertEqual(page.locator('[data-autopilot-cancel]').count(), 0)
        self.assertNotIn('人工确认', page.locator('#carousel-autopilot-panel').inner_text())

    def test_failed_run_explains_reason_and_can_resume_then_stop(self):
        page = self.open_autopilot(self.autopilot_run(status='failed', phase='AI 核对', error='第 2 张文字未通过核对'))
        page.locator('#carousel-autopilot-panel').get_by_text('第 2 张文字未通过核对', exact=True).wait_for()
        self.assertEqual(page.locator('#carousel-autopilot-panel a[download]').count(), 0)
        page.get_by_role('button', name='继续由 AI 完成', exact=True).click()
        page.locator('[data-autopilot-cancel]').click()
        page.locator('#carousel-autopilot-panel strong').get_by_text('已停止', exact=True).wait_for()
        self.assertTrue(self.calls[-1]['url'].endswith('/autopilot/AUTO-111111111111/cancel'))

    def test_completed_run_does_not_offer_a_cleaned_historical_zip(self):
        page = self.open_autopilot(self.autopilot_run(status='completed', phase='已完成',
            delivery_id='DLV-111111111111', message='原制作记录'))
        self.payload['carousel']['deliveries'] = [{'id': 'DLV-222222222222', 'current': True}]
        page.reload()
        panel = page.locator('#carousel-autopilot-panel')
        panel.get_by_text('AI 制作已完成', exact=True).wait_for()
        self.assertEqual(panel.locator('a[download]').count(), 0)
        self.assertIn('原制作记录', panel.inner_text())

    def test_existing_bases_can_start_autopilot_while_an_edit_owns_resolve(self):
        for slide in self.carousel['slides']:
            candidate = self.app.import_uploaded_image(self.carousel['id'], slide['number'],
                self.source.name, self.source.read_bytes(), uuid4().hex)['candidate']
            self.app.choose_base(self.carousel['id'], {'request_id': uuid4().hex,
                'revision': self.app.production.current_revision(), 'slide': slide['number'], 'candidate': candidate['id']})
        edit_id = 'EDT-111111111111'
        self.app._reserve(edit_id)
        self.addCleanup(self.app._forget, edit_id)
        self.app.resolve.lock.acquire()
        self.addCleanup(self.app.resolve.lock.release)
        page = self.open_autopilot()
        button = page.locator('[data-autopilot-start]')
        self.assertTrue(button.is_enabled())
        button.click()
        page.locator('[data-autopilot-cancel]').wait_for()
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.app.resolve.busy())
        self.assertIsNotNone(self.app.edit_live(edit_id))

    def test_waiting_still_search_keeps_queue_status_and_cancel_available(self):
        page = self.open_autopilot()
        search_id = 'STS-111111111111'
        self.payload['carousel']['searches'] = [{'id': search_id, 'status': 'running',
            'slides': [1], 'per_slide': 1, 'materials': ['蓝点折刀成品素材'],
            'model': 'codex-default', 'provider': 'codex', 'started_at': '2026-10-21T10:00:00+08:00',
            'live': {'progress': '等待达芬奇：其他任务完成后自动找底图。'}}]
        cancelled = []
        page.route('**' + self.path + '/searches/' + search_id + '/cancel',
            lambda route: (cancelled.append(route.request.url), route.fulfill(content_type='application/json', body='{}')))
        page.reload()
        page.locator('[data-step="pick"]').click()
        page.locator('.still-history > summary').click()
        card = page.locator('#search-runs .edit-card.running')
        self.assertEqual(card.locator('.script-status').inner_text(), '等待达芬奇')
        card.get_by_role('button', name='取消排队', exact=True).click()
        page.wait_for_function("() => document.querySelector('[data-cancel-search]')?.disabled")
        self.assertEqual(len(cancelled), 1)
        self.assertTrue(page.locator('[data-autopilot-start]').is_disabled(), 'same-carousel work must stay exclusive')


class VideoCarouselAutopilotBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel='chrome', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='<main id="host"></main>'))
        self.page.goto('http://127.0.0.1:43219/test')
        self.page.add_script_tag(path=str(Path(__file__).resolve().parents[1] / 'static' / 'video-publication.js'))
        self.page.evaluate("""async () => {
          window.calls = [];
          window.videoState = {runs:[],running:false,source:{available:true,kind:'file',label:'测试成片'}};
          window.mountTest = () => VideoPublication.mount(document.querySelector('#host'),{
            job:{id:'PRD-111111111111'}, api:async (url,options) => {
              if (options) {calls.push({url,body:options.body});return {};}
              if (url.endsWith('/video-carousel')) return structuredClone(videoState);
              if (url === '/api/ai/options') return {models:[],default_settings:{}};
              return {history:[],source:{},can_register:false};
            }
          });
          await mountTest();
        }""")
        self.page.locator('.video-carousel-form').wait_for(state='attached')
        self.page.locator('.video-carousel-again > summary').click()

    def test_default_handoff_and_explicit_table_only_choice(self):
        self.assertTrue(self.page.locator('[name="autopilot"]').is_checked())
        self.page.get_by_role('button', name='AI 全程制作图文', exact=True).click()
        self.page.wait_for_function('calls.length === 1')
        self.assertTrue(self.page.evaluate('calls[0].body.autopilot'))
        self.page.locator('[name="autopilot"]').uncheck()
        self.page.get_by_role('button', name='生成逐张表和候选帧', exact=True).click()
        self.page.wait_for_function('calls.length === 2')
        self.assertFalse(self.page.evaluate('calls[1].body.autopilot'))

    def test_running_does_not_claim_done_and_completed_has_download(self):
        self.page.evaluate("""async () => {videoState.running=true;videoState.runs=[{id:'TBL-111111111111',status:'running',autopilot:true,progress:'AI 正在核对图片'}];await mountTest();}""")
        self.page.get_by_text('AI 正在全程制作图文', exact=True).wait_for()
        self.assertEqual(self.page.locator('.video-carousel-run a[download]').count(), 0)
        self.page.evaluate("""async () => {videoState.running=false;videoState.runs[0]={...videoState.runs[0],status:'completed',carousel_id:'CAR-111111111111',delivery_id:'DLV-111111111111',delivery_available:true};await mountTest();}""")
        link = self.page.get_by_role('link', name='下载成品包', exact=True)
        link.wait_for()
        self.assertTrue(link.get_attribute('href').endswith('/deliveries/DLV-111111111111/download'))

    def test_cleaned_video_carousel_package_opens_the_task_without_an_old_download_link(self):
        self.page.evaluate("""async () => {videoState.runs=[{id:'TBL-111111111111',status:'completed',
          carousel_id:'CAR-111111111111',delivery_id:'DLV-111111111111',delivery_available:false}];await mountTest();}""")
        card = self.page.locator('.video-carousel-run.is-done')
        self.assertEqual(card.locator('a[download]').count(), 0)
        link = card.get_by_role('link', name='打开图文制作单', exact=True)
        self.assertTrue(link.get_attribute('href').endswith('CAR-111111111111'))

    def test_resolve_queue_shows_waiting_and_can_be_cancelled(self):
        self.page.evaluate("""async () => {videoState.running=true;videoState.runs=[{
          id:'TBL-111111111111',status:'running',autopilot:true,phase:'等待达芬奇',
          progress:'等待达芬奇：其他任务完成后自动取帧。'}];await mountTest();}""")
        card = self.page.locator('.video-carousel-run.is-running')
        self.assertEqual(card.locator('strong').inner_text(), '等待达芬奇')
        card.get_by_role('button', name='取消排队', exact=True).click()
        self.page.wait_for_function('calls.length === 1')
        self.assertTrue(self.page.evaluate("calls[0].url.endsWith('/carousel-tables/TBL-111111111111/cancel')"))
        self.assertEqual(card.locator('a[download]').count(), 0)

    def test_failed_or_cancelled_video_keeps_a_link_to_continue_the_existing_carousel(self):
        for status in ('failed', 'cancelled'):
            with self.subTest(status=status):
                self.page.evaluate("""async status => {videoState.running=false;videoState.runs=[{id:'TBL-111111111111',status,autopilot:true,carousel_id:'CAR-111111111111',error:'本次核对未完成'}];await mountTest();}""", status)
                card = self.page.locator('.video-carousel-run.is-failed')
                link = card.get_by_role('link', name='继续完成这组图文', exact=True)
                link.wait_for()
                self.assertTrue(link.get_attribute('href').endswith('CAR-111111111111'))
                self.assertIn('逐张表、候选画面和已完成图片已保留', card.inner_text())
                self.assertEqual(card.locator('a[download]').count(), 0)
                self.assertFalse(self.page.locator('.video-carousel-again').evaluate('node => node.open'))


if __name__ == '__main__':
    unittest.main()
