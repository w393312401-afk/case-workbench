"""Local result updates keep the user's drafts, playback nodes and selections."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

from test_production_validation_ui import ProductionValidationBrowserTests


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

    def test_voice_form_starts_on_the_scripts_market_and_sends_the_choice(self):
        self.payload['markets'] = [{'id':'us','label':'美国 · 英语','voice_hint':''},{'id':'uk','label':'英国 · 英语','voice_hint':'请选英式音色。'},
                                   {'id':'de','label':'德国 · 德语','voice_hint':'请选德语音色。'}]
        self.payload['job']['market'] = 'de'
        pending = []
        page = self.open_page(post=lambda route: pending.append(route))
        select, hint = page.locator('#take-form [name="market"]'), page.locator('#take-form [data-market-hint]')
        self.assertEqual(select.input_value(),'de')
        self.assertEqual(hint.inner_text(),'请选德语音色。')
        select.select_option('us')
        self.assertFalse(hint.is_visible())
        select.select_option('uk')
        self.assertEqual(hint.inner_text(),'请选英式音色。')
        page.locator('#take-form button[type="submit"]').click()
        page.locator('#voice-panel .production-runtime').wait_for()
        self.assertEqual(pending[0].request.post_data_json['market'],'uk')
        pending[0].fulfill(status=503,content_type='application/json',body=json.dumps({'error':'Fixture voice unavailable'}))

    def test_uk_version_is_started_from_the_menu_and_checked_before_a_job_exists(self):
        posts = []
        def post(route):
            posts.append(route.request.url)
            route.fulfill(status=200,content_type='application/json',body=json.dumps({'job':{'id':'PRD-0123456789ab'}}))
        page = self.open_page(post=post)
        page.locator('.production-more-actions summary').click()
        page.locator('#job-uk-start-menu').click()
        page.wait_for_timeout(200)
        self.assertTrue(posts[0].endswith('/uk-version'))
        self.payload['job']['uk_version'] = {'task_id':'AIT-0123456789ab','status':'review','error':'','review':{'round_id':'r','variant_index':0,'problems':[],'titles':['🔴One Set#SHARPAL'],
            'lines':[{'number':1,'before':'One kit.','spoken':'One set.','zh':'一套。'}]}}
        page = self.open_page(post=post)
        self.assertEqual(page.locator('#job-uk-start-menu').count(),0)
        page.locator('#job-uk-review').click()
        dialog = page.locator('dialog.production-dialog')
        self.assertEqual(dialog.locator('tr.is-changed').count(),1)
        self.assertIn('One set.',dialog.locator('.uk-review').inner_text())
        self.assertIn('🔴One Set',dialog.inner_text())
        dialog.locator('[data-submit]').click()
        dialog.wait_for(state='detached')
        self.assertTrue(posts[-1].endswith('/uk-version-confirm'))

    def test_linked_jobs_show_the_way_to_each_other(self):
        self.payload['job']['derived_from'] = {'job_id':'PRD-0123456789ab','market':'us'}
        page = self.open_page()
        self.assertIn('由美国版制作单改来',page.locator('.production-market-version').inner_text())
        self.assertEqual(page.locator('#job-uk-start-menu').count(),0)

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
        page.locator('[data-take-id="TAK-second"] .take-head .script-status').get_by_text('当前使用',exact=True).wait_for()
        self.assertTrue(page.evaluate("window.__originalAudio===document.querySelector('[data-take-id=TAK-first] audio')"))
        self.assertTrue(page.locator('[data-take-id="TAK-first"] details').evaluate('node=>node.open'))
        self.assertTrue(page.evaluate("window.__originalForm===document.querySelector('#edit-panel #edit-form')"))
        self.assertEqual(page.locator('#edit-panel textarea[name="instruction"]').input_value(),'Keep this unfinished edit request.')
        self.assertEqual(page.locator('.production-steps [aria-selected="true"]').get_attribute('data-step'),'voice')
        # A second mutation must use the revision returned by the local refresh.
        page.locator('[data-select-take="TAK-first"]').click()
        page.locator('[data-take-id="TAK-first"] .take-head .script-status').wait_for()
        self.assertEqual([call['body']['revision'] for call in self.calls],[1,2])


for case,base in ((VoiceRefreshBrowserTests,ProductionValidationBrowserTests),):
    for name in dir(base):
        if name.startswith('test_') and name not in case.__dict__:
            setattr(case,name,None)
del ProductionValidationBrowserTests,case,base,name

if __name__ == '__main__':
    unittest.main()
