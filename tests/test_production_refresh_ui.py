"""Local result updates keep the user's drafts, playback nodes and selections."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from test_production_validation_ui import ProductionValidationBrowserTests
from test_production_carousel_images_ui import CarouselImageBrowserTests
from test_production_queue_ui import ProductionQueueBrowserTests


class VoiceRefreshBrowserTests(ProductionValidationBrowserTests):
    def test_late_delivery_read_cannot_replace_new_approval_state(self):
        job = self.payload['job']
        take = self.take('TAK-first',[{'number':1,'start':0,'end':1.5,'coverage':1}])
        edit = self.edit('completed',validated=False)
        edit.update(completion_policy='skill_delivery',report={'saved':True,'shots':1,'gaps':[]})
        job.update(status='awaiting_review',takes=[take],selected_take=take['id'],edits=[edit],
                   timelines=[{'unique_id':'uid-own','name':'Lab-1','take_id':take['id'],'frames':60,
                               'fps':'30','creator':'Lab','markers':1}])
        self.payload['revision'] = 1
        reads = []
        original_context = self.browser.new_context

        def delivery(route):
            reads.append(route)
            if len(reads)>1:
                row={'kind':'video','id':job['id'],'status':'awaiting_review','edit_id':edit['id'],'approved_at':edit['approval']['at']}
                route.fulfill(status=200,content_type='application/json',body=json.dumps({'items':[row]}))

        def context_factory(**kwargs):
            context=original_context(**kwargs)
            context.route('**/api/production/deliveries?*',delivery)
            return context

        def approve(route):
            edit['approval']={'at':'2026-10-14T10:00:00+08:00','reviewed':route.request.post_data_json['reviewed']}
            job['status']='approved'
            route.fulfill(status=200,content_type='application/json',body=json.dumps({'approved':True,'edit':edit}))

        with patch.object(self.browser,'new_context',side_effect=context_factory):
            page=self.open_page(post=approve)
        for box in page.locator('[data-edit-review] input[name="reviewed"]').all():
            box.check()
        page.locator('[data-approve-edit]').click()
        # The 下一步 strip is hidden in favour of the step bar but still tracks the state.
        page.locator('[data-next-state]').get_by_text('已人工审查',exact=True).wait_for(state='attached')
        self.assertEqual(len(reads),2)
        old={'kind':'video','id':job['id'],'status':'awaiting_review','edit_id':edit['id'],'approved_at':''}
        reads[0].fulfill(status=200,content_type='application/json',body=json.dumps({'items':[old]}))
        page.evaluate('() => new Promise(resolve => setTimeout(resolve,100))')
        self.assertEqual(page.locator('[data-next-state]').inner_text(),'已人工审查')

    def test_voice_submission_shows_elapsed_then_actual_backend_phase(self):
        pending = []
        page = self.open_page(post=lambda route: pending.append(route))
        page.locator('#take-form button[type="submit"]').click()
        runtime = page.locator('#voice-panel .production-runtime')
        runtime.get_by_text('准备配音',exact=True).wait_for()
        self.assertTrue(runtime.locator('[data-run-started]').get_attribute('data-run-started'))
        self.payload['job'].update(voicing=True,voicing_live={
            'started_at':(datetime.now(timezone.utc)-timedelta(seconds=8)).isoformat(),
            'stage':'alignment','progress':'已取得音频，正在对齐意群',
        })
        runtime.get_by_text('对齐意群',exact=True).wait_for(timeout=7000)
        self.assertEqual(len(pending),1)
        pending[0].fulfill(status=503,content_type='application/json',body=json.dumps({'error':'Fixture voice unavailable'}))
        page.locator('#take-form [data-error]').get_by_text('Fixture voice unavailable',exact=False).wait_for()
        self.assertEqual(runtime.count(),0)

    def test_take_selection_preserves_audio_details_and_other_step_drafts(self):
        job = self.payload['job']
        groups = [{'number':1,'start':0,'end':1.5,'coverage':1}]
        job.update(selected_take='TAK-first', takes=[self.take('TAK-first',groups),self.take('TAK-second',groups)],
                   timelines=[{'unique_id':f'uid-{name}','name':f'Lab-{name}','take_id':f'TAK-{name}','frames':60,
                               'fps':'30','creator':'Lab','markers':1} for name in ('first','second')])
        self.payload['revision'] = 1

        def select(route):
            self.assertTrue(route.request.url.endswith('/select'))
            job['selected_take'] = route.request.post_data_json['take_id']
            self.payload['revision'] += 1
            route.fulfill(status=200,content_type='application/json',body='{}')

        page = self.open_page(post=select)
        page.locator('#edit-panel textarea[name="instruction"]').fill('Keep this unfinished edit request.')
        page.evaluate("window.__originalForm=document.querySelector('#edit-panel #edit-form');window.__originalAudio=document.querySelector('[data-take-id=TAK-first] audio')")
        page.locator('[data-step="voice"]').click()
        page.locator('[data-take-id="TAK-first"] details > summary').click()
        page.locator('[data-select-take="TAK-second"]').click()
        page.locator('[data-take-id="TAK-second"] .take-head .script-status').get_by_text('已选用',exact=True).wait_for()
        self.assertTrue(page.evaluate("window.__originalAudio===document.querySelector('[data-take-id=TAK-first] audio')"))
        self.assertTrue(page.locator('[data-take-id="TAK-first"] details').evaluate('node=>node.open'))
        self.assertTrue(page.evaluate("window.__originalForm===document.querySelector('#edit-panel #edit-form')"))
        self.assertEqual(page.locator('#edit-panel textarea[name="instruction"]').input_value(),'Keep this unfinished edit request.')
        self.assertEqual(page.locator('.production-steps [aria-selected="true"]').get_attribute('data-step'),'voice')
        # A second mutation must use the revision returned by the local refresh.
        page.locator('[data-select-take="TAK-first"]').click()
        page.locator('[data-take-id="TAK-first"] .take-head .script-status').wait_for()
        self.assertEqual([call['body']['revision'] for call in self.calls],[1,2])


class CarouselRefreshBrowserTests(CarouselImageBrowserTests):
    def test_unpick_keeps_image_form_and_card_selection(self):
        page = self.open_images()
        page.locator('#carousel-image-form textarea[name="instruction"]').fill('Keep this image draft.')
        page.locator('#carousel-requirements-toggle > summary').click()
        page.locator('[data-redo-pick="2"]').uncheck()
        page.evaluate("window.__imageForm=document.querySelector('#carousel-image-form')")
        page.locator('[data-step="pick"]').click()
        page.locator('[data-unpick="1"]').click()
        page.locator('.production-steps [data-step="pick"] small').get_by_text('2/3 已选',exact=True).wait_for()
        self.assertTrue(page.evaluate("window.__imageForm===document.querySelector('#carousel-image-form')"))
        self.assertEqual(page.locator('#carousel-image-form textarea[name="instruction"]').input_value(),'Keep this image draft.')
        self.assertTrue(page.locator('#carousel-requirements-toggle').evaluate('node=>node.open'))
        self.assertFalse(page.locator('[data-redo-pick="2"]').is_checked())
        self.assertEqual(page.locator('.production-steps [aria-selected="true"]').get_attribute('data-step'),'pick')
        self.assertIn('无底图',page.locator('[data-overview-slide="1"]').get_attribute('aria-label') or '')


class QueueProgressBrowserTests(ProductionQueueBrowserTests):
    def test_running_video_and_carousel_show_concise_live_progress_on_mobile(self):
        job = self.job(1, 'editing')
        job['edits'] = [{'id':'EDT-live', 'status':'running', 'live':{'progress':'正在核对 4 / 12 个片段'}}]
        carousel = self.carousel(1, current=False)
        carousel['generations'] = [{'id':'GEN-live', 'status':'running', 'progress':'已生成 2 / 5 张图片'}]
        page = self.open_queue(width=390)
        page.locator('[data-flow-view="running"]').click()
        self.row(page, job).locator('.flow-progress').get_by_text('正在核对 4 / 12 个片段', exact=True).wait_for()
        self.row(page, carousel).locator('.flow-progress').get_by_text('已生成 2 / 5 张图片', exact=True).wait_for()
        self.assertEqual(page.locator('.flow-row').count(), 2)
        self.assertEqual(page.locator('.production-queue-progress, .production-queue-thumb').count(), 0)
        job['edits'][0]['live']['progress'] = '正在挑选镜头'
        self.row(page, job).locator('.flow-progress').get_by_text('正在挑选镜头', exact=True).wait_for(timeout=10000)
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 391)


for case,base in ((VoiceRefreshBrowserTests,ProductionValidationBrowserTests),
                  (CarouselRefreshBrowserTests,CarouselImageBrowserTests),
                  (QueueProgressBrowserTests,ProductionQueueBrowserTests)):
    for name in dir(base):
        if name.startswith('test_') and name not in case.__dict__:
            setattr(case,name,None)
del ProductionValidationBrowserTests,CarouselImageBrowserTests,ProductionQueueBrowserTests,case,base,name

if __name__ == '__main__':
    unittest.main()
