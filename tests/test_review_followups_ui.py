"""Persistent backlog decisions through the real API, in a disposable browser and runtime."""
from datetime import date, timedelta
import json
import unittest

import test_review_ui as review


def open_edit(page):
    details = page.locator('.followup-details').first
    if details.get_attribute('open') is None:
        details.locator('summary').first.click()
    page.locator('[data-followup-edit]').first.click()


def open_filters(page):
    controls = page.locator('.followup-controls')
    controls.wait_for()
    if controls.get_attribute('open') is None:
        controls.locator('summary').first.click()


class FollowupUITests(unittest.TestCase):
    setUpClass = classmethod(review.ReviewPageTests.setUpClass.__func__)
    tearDownClass = classmethod(review.ReviewPageTests.tearDownClass.__func__)
    setUp = review.ReviewPageTests.setUp
    stop_server = review.ReviewPageTests.stop_server
    request = review.ReviewPageTests.request
    open_page = review.ReviewPageTests.open_page

    def test_old_windows_complete_defer_reopen_and_mobile(self):
        page=self.open_page(width=390,route='#/202H/home',ready='[data-home-tab]')
        page.locator('[data-home-tab=due]').click()
        open_filters(page)
        page.locator('[data-followup-filter]').select_option('pending')
        count=page.locator('[data-followup-edit]').count()
        self.assertGreaterEqual(count,4)  # fixture windows ended in September; clock is in October
        open_edit(page)
        dialog=page.locator('.followup-dialog')
        dialog.locator('[name=state]').select_option('reviewed')
        dialog.locator('[name=note]').fill('已复盘：下一轮先展示装夹。')
        dialog.get_by_role('button',name='保存',exact=True).click()
        dialog.wait_for(state='detached')
        page.wait_for_function('(count)=>document.querySelectorAll("[data-followup-edit]").length===count',arg=count-1)
        page.reload()
        page.locator('[data-home-tab=due]').click()
        open_filters(page)
        page.locator('[data-followup-filter]').select_option('reviewed')
        page.locator('.followup-details > summary').first.click()
        page.get_by_text('已复盘：下一轮先展示装夹。',exact=True).wait_for()
        open_edit(page)
        dialog.locator('[name=state]').select_option('deferred')
        dialog.locator('[name=defer_until]').fill((date.today()+timedelta(days=3)).isoformat())
        dialog.get_by_role('button',name='保存',exact=True).click()
        dialog.wait_for(state='detached')
        open_filters(page)
        page.locator('[data-followup-filter]').select_option('deferred')
        open_edit(page)
        dialog.locator('[name=state]').select_option('pending')
        dialog.get_by_role('button',name='保存',exact=True).click()
        dialog.wait_for(state='detached')
        open_filters(page)
        page.locator('[data-followup-filter]').select_option('pending')
        self.assertEqual(page.locator('[data-followup-edit]').count(),count)
        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))

    def test_unknown_save_retries_same_request_and_preserves_one_history_entry(self):
        page=self.open_page(route='#/202H/home',ready='[data-home-tab]')
        page.locator('[data-home-tab=due]').click()
        open_filters(page)
        page.locator('[data-followup-filter]').select_option('pending')
        open_edit(page)
        dialog=page.locator('.followup-dialog')
        dialog.locator('[name=state]').select_option('reviewed')
        dialog.locator('[name=note]').fill('检查重试')
        requests=[]
        def interrupt(route):
            if route.request.method!='POST':
                return route.continue_()
            requests.append(route.request.post_data_json)
            response=route.fetch()
            if len(requests)==1:
                route.fulfill(status=503,content_type='application/json',body=json.dumps({'error':'连接中断'}))
            else:
                route.fulfill(response=response)
        page.route('**/api/review-followups',interrupt)
        dialog.get_by_role('button',name='保存',exact=True).click()
        dialog.get_by_text('保存结果尚未确认',exact=False).wait_for()
        self.assertTrue(dialog.locator('[name=note]').is_disabled())
        dialog.get_by_role('button',name='保存',exact=True).click()
        dialog.wait_for(state='detached')
        self.assertEqual(requests[0],requests[1])
        saved=self.request('GET','/api/review-followups')[2]['entries']
        self.assertEqual(len(saved[0]['history']),1)

    def test_removed_actions_and_judgment_controls_are_absent_from_queue_and_editor(self):
        page=self.open_page(route='#/202H/home',ready='[data-home-tab]')
        page.locator('[data-home-tab=due]').click()
        row=page.locator('.followup-row').first
        row.wait_for()
        row.locator('.followup-details > summary').click()
        self.assertIn('表现：',row.inner_text())
        self.assertIn('证据：',row.inner_text())
        self.assertEqual(page.locator('[data-followup-observe],[data-followup-none]').count(),0)
        self.assertNotIn('继续观察',row.inner_text())
        self.assertNotIn('本轮暂无发现',row.inner_text())
        row.locator('[data-followup-edit]').click()
        dialog=page.locator('.followup-dialog')
        self.assertEqual(dialog.locator('[name=state] option').evaluate_all('nodes=>nodes.map(node=>node.value)'),
                         ['pending','reviewed','deferred'])
        self.assertEqual(dialog.locator('[name=outcome],[name=evidence],[name=next_review_on],[name=observation_reason]').count(),0)
        requests=[]
        page.on('request',lambda request: requests.append(request.post_data_json)
                if request.method=='POST' and '/api/review-followups' in request.url else None)
        dialog.locator('[name=state]').select_option('reviewed')
        dialog.locator('[name=note]').fill('人工复盘已完成，保留需要参考的发现。')
        dialog.get_by_role('button',name='保存',exact=True).click()
        dialog.wait_for(state='detached')
        self.assertEqual(len(requests),1)
        self.assertTrue({'outcome','evidence','next_review_on','observation_reason'}.isdisjoint(requests[0]))
        self.assertNotIn(requests[0]['decision'],['observe','none'])

    def test_legacy_decisions_are_readable_but_cannot_be_resubmitted(self):
        video=next(row for row in self.server.app.review_videos() if row['review_context']['closing_allowed'])
        store=self.server.app.review_followups
        catalog=store.read()
        catalog['entries'].append({'product':video['product'],'video_id':video['video_id'],
            'window_end':video['window_end'],'state':'reviewed','decision':'none',
            'note':'保留旧复盘结论','outcome':'negative','evidence':'preliminary',
            'reference_value':'weak','at':'2026-10-24T12:00:00+08:00','history':[]})
        store.write(catalog)
        page=self.open_page(route='#/202H/home',ready='[data-home-tab]')
        page.locator('[data-home-tab=due]').click()
        open_filters(page)
        page.locator('[data-followup-filter]').select_option('reviewed')
        row=page.locator(f'[data-followup-video="{video['video_id']}"]')
        row.wait_for()
        self.assertIn('弱参考价值',row.inner_text())
        self.assertIn('已结束',row.inner_text())
        self.assertNotIn('本轮暂无发现',row.inner_text())
        row.locator('.followup-details > summary').click()
        self.assertIn('保留旧复盘结论',row.inner_text())
        row.locator('[data-followup-edit]').click()
        dialog=page.locator('.followup-dialog')
        requests=[]
        page.on('request',lambda request: requests.append(request.post_data_json)
                if request.method=='POST' and '/api/review-followups' in request.url else None)
        dialog.get_by_role('button',name='保存',exact=True).click()
        dialog.wait_for(state='detached')
        self.assertEqual(requests[0]['decision'],'')
        entry=next(row for row in store.public()['entries'] if row['video_id']==video['video_id'])
        self.assertEqual(entry['reference_value'],'weak')
        self.assertEqual(entry['outcome'],'negative')
        self.assertEqual(entry['evidence'],'preliminary')


if __name__=='__main__':
    unittest.main()
