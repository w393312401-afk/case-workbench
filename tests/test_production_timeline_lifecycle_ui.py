"""Timeline lifecycle stays independent of AI results and survives local refreshes."""
from __future__ import annotations

import json
import unittest

from playwright.sync_api import expect

import test_production_manual_completion_ui as completion
import test_production_validation_ui as fixture


class ProductionTimelineLifecycleBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ProductionValidationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ProductionValidationBrowserTests.tearDownClass.__func__)
    stop_server = fixture.ProductionValidationBrowserTests.stop_server
    request = fixture.ProductionValidationBrowserTests.request
    make_playable_video = fixture.ProductionValidationBrowserTests.make_playable_video
    take = fixture.ProductionValidationBrowserTests.take
    edit = fixture.ProductionValidationBrowserTests.edit
    timeline = completion.ProductionManualCompletionBrowserTests.timeline
    receipt = completion.ProductionManualCompletionBrowserTests.receipt

    def setUp(self):
        fixture.ProductionValidationBrowserTests.setUp(self)
        self.payload['revision'] = 1
        self.payload['job'].update(
            selected_take=completion.TAKE_ID,
            takes=[self.take(completion.TAKE_ID, [{'number': 1, 'start': 0, 'end': 1.5, 'coverage': 1}])],
            timelines=[{**self.timeline('uid-old', 'Lab261010-202-1'), 'task_status': 'pending', 'archived': False},
                       {**self.timeline('uid-own', 'Lab261011-202-2'), 'task_status': 'completed', 'archived': False}])

    def mutate(self, route):
        body = route.request.post_data_json
        if route.request.url.endswith('/open'):
            timeline = next(item for item in self.payload['job']['timelines'] if item['unique_id'] == body['unique_id'])
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'name': timeline['name']}))
            return
        self.assertEqual(body['revision'], self.payload['revision'])
        self.assertTrue(body['request_id'])
        timeline = next(item for item in self.payload['job']['timelines'] if item['unique_id'] == body['timeline_uid'])
        if route.request.url.endswith('/timeline/archive'):
            timeline['archived'] = body['archived']
        else:
            self.assertTrue(route.request.url.endswith('/timeline/task-status'))
            timeline['task_status'] = body['task_status']
        self.payload['revision'] += 1
        route.fulfill(status=200, content_type='application/json', body='{}')

    def open_page(self, *, post=None, width=1080):
        page = fixture.ProductionValidationBrowserTests.open_page(self, post=post or self.mutate, width=width)
        page.locator('.production-steps [data-step="timeline"]').click()
        return page

    def test_archive_history_is_collapsed_and_restore_keeps_order_target_and_drafts(self):
        page = self.open_page(width=390)
        form = page.locator('#edit-panel #edit-form')
        page.locator('.production-steps [data-step="edit"]').click()
        form.locator('[name="instruction"]').fill('保留这段尚未提交的剪辑要求')
        form.locator('[name="timeline"]').select_option('uid-own')
        page.evaluate('window.__editForm=document.querySelector("#edit-panel #edit-form")')
        page.locator('.production-steps [data-step="timeline"]').click()
        page.locator('[data-archive-timeline="uid-old"]').click()
        history = page.locator('.timeline-history')
        expect(history.locator('summary')).to_have_text('历史时间线（1 条）')
        self.assertFalse(history.evaluate('node => node.open'))
        expect(page.locator('#timeline-cards > .timeline-card')).to_have_count(1)
        expect(page.locator('[data-timeline-count]')).to_have_text('1 条')
        expect(form.locator('[name="timeline"]')).to_have_value('uid-own')
        expect(form.locator('[name="timeline"] option')).to_have_count(1)
        expect(form.locator('[name="instruction"]')).to_have_value('保留这段尚未提交的剪辑要求')
        self.assertTrue(page.evaluate('window.__editForm===document.querySelector("#edit-panel #edit-form")'))
        history.locator('summary').click()
        expect(history).to_contain_text('已归档')
        expect(history).to_contain_text('归档前：待剪辑')
        history.locator('[data-open-timeline]').click()
        expect(page.locator('#toast')).to_contain_text('已在达芬奇打开 Lab261010-202-1')
        page.reload()
        page.locator('.production-steps [data-step="timeline"]').click()
        self.assertFalse(history.evaluate('node => node.open'))
        history.locator('summary').click()
        history.locator('[data-archive-timeline="uid-old"]').click()
        expect(history).to_have_count(0)
        expect(page.locator('#timeline-cards > .timeline-card')).to_have_count(2)
        self.assertEqual(page.locator('#timeline-cards > .timeline-card').evaluate_all(
            'nodes => nodes.map(node => node.dataset.timelineId)'), ['uid-own', 'uid-old'])
        expect(page.locator('.timeline-card.current')).to_have_attribute('data-timeline-id', 'uid-own')
        expect(form.locator('[name="timeline"]')).to_have_value('uid-own')
        expect(page.locator('[data-timeline-task="uid-old"]')).to_have_value('pending')
        self.assertEqual([call['body']['archived'] for call in self.calls if '/timeline/archive' in call['url']], [True, False])
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 391)

    def test_task_completion_and_ai_state_remain_independent_after_save_and_reload(self):
        job = self.payload['job']
        job.update(status='approved', manual_completion=self.receipt(name='Lab261011-202-2'))
        page = self.open_page()
        card = page.locator('[data-timeline-id="uid-own"]')
        expect(card.locator('[data-timeline-task]')).to_have_value('completed')
        # The manual completion of this timeline is its result; the task status stays a separate choice.
        expect(card.locator('[data-timeline-ai-state]')).to_have_text('人工完成')
        card.locator('[data-timeline-task]').select_option('in_progress')
        expect(page.locator('#toast')).to_contain_text('已更新任务状态。')
        expect(card.locator('[data-timeline-task]')).to_have_value('in_progress')
        expect(card.locator('[data-timeline-ai-state]')).to_have_text('人工完成')
        self.assertEqual(job['edits'], [])
        self.assertEqual(job['manual_completion'], self.receipt(name='Lab261011-202-2'))
        page.reload()
        page.locator('.production-steps [data-step="timeline"]').click()
        expect(card.locator('[data-timeline-task]')).to_have_value('in_progress')
        edit = self.edit('failed')
        job['edits'] = [edit]
        page.reload()
        page.locator('.production-steps [data-step="timeline"]').click()
        card.locator('[data-timeline-task]').select_option('completed')
        expect(page.locator('#toast')).to_contain_text('已更新任务状态。')
        # A later failed AI run does not reopen a timeline the user completed by hand; the run itself is kept.
        expect(card.locator('[data-timeline-ai-state]')).to_have_text('人工完成')
        self.assertEqual(edit['status'], 'failed')

    def test_timeline_without_an_edit_shows_no_ai_badge(self):
        page = self.open_page()
        expect(page.locator('#timeline-cards > .timeline-card')).to_have_count(2)
        expect(page.locator('#timeline-cards [data-timeline-ai-state]')).to_have_count(0)

    def test_approved_order_folds_unused_timelines_without_an_ai_badge(self):
        self.payload['job'].update(status='approved', manual_completion=self.receipt(name='Lab261011-202-2'))
        page = self.open_page()
        expect(page.locator('#timeline-cards > .timeline-card')).to_have_count(1)
        expect(page.locator('#timeline-cards > .timeline-card.current')).to_have_attribute('data-timeline-id', 'uid-own')
        others = page.locator('#timeline-cards .timeline-others')
        expect(others.locator('summary')).to_have_text('其他时间线（1 条）')
        self.assertFalse(others.evaluate('node => node.open'))
        old = others.locator('[data-timeline-id="uid-old"]')
        expect(old.locator('[data-timeline-ai-state]')).to_have_count(0)
        expect(old.locator('[data-timeline-task]')).to_have_value('pending')
        others.locator('summary').click()
        old.locator('[data-open-timeline]').click()
        expect(page.locator('#toast')).to_contain_text('已在达芬奇打开 Lab261010-202-1')
        expect(page.locator('.timeline-history')).to_have_count(0)

    def test_timeline_built_after_the_approval_stays_open_beside_its_basis(self):
        job = self.payload['job']
        job.update(status='approved', manual_completion=self.receipt(name='Lab261011-202-2'))
        job['timelines'].append({**self.timeline('uid-new', 'Lab261012-202-3'), 'task_status': 'pending', 'archived': False})
        page = self.open_page()
        expect(page.locator('#timeline-cards > .timeline-card')).to_have_count(2)
        expect(page.locator('#timeline-cards > [data-timeline-id="uid-own"] [data-timeline-ai-state]')).to_have_text('人工完成')
        expect(page.locator('#timeline-cards > [data-timeline-id="uid-new"]')).to_have_count(1)
        expect(page.locator('#timeline-cards .timeline-others [data-timeline-id="uid-old"]')).to_have_count(1)

    def test_conflict_refreshes_revision_and_restores_failed_status_control(self):
        def conflict_once(route):
            if len(self.calls) == 1:
                self.payload['revision'] += 1
                route.fulfill(status=409, content_type='application/json', body=json.dumps({'error': '资料已更新，请核对后重试。'}))
            else:
                self.mutate(route)

        page = self.open_page(post=conflict_once)
        card = page.locator('[data-timeline-id="uid-old"]')
        select = card.locator('[data-timeline-task]')
        select.select_option('in_progress')
        expect(card.locator('[data-error]')).to_contain_text('资料已更新')
        expect(select).to_have_value('pending')
        expect(select).to_be_enabled()
        select.select_option('in_progress')
        expect(page.locator('#toast')).to_contain_text('已更新任务状态。')
        expect(select).to_have_value('in_progress')
        expect(card.locator('[data-error]')).to_be_hidden()
        self.assertEqual([call['body']['revision'] for call in self.calls], [1, 2])

    def test_running_timeline_is_locked_and_poll_rebinds_finished_controls(self):
        job = self.payload['job']
        edit = self.edit('running')
        edit['execution_status'] = 'running'
        job.update(status='editing', edits=[edit])
        job['timelines'][1]['task_status'] = 'in_progress'
        page = self.open_page()
        card = page.locator('[data-timeline-id="uid-own"]')
        expect(card.locator('[data-archive-timeline]')).to_be_disabled()
        expect(card.locator('[data-timeline-task]')).to_be_disabled()
        expect(card.locator('[data-timeline-ai-state]')).to_have_text('AI 剪辑：运行中')
        edit.update(status='failed', execution_status='failed')
        job['status'] = 'failed'
        self.payload['revision'] += 1
        expect(card.locator('[data-archive-timeline]')).to_be_enabled(timeout=8000)
        expect(card.locator('[data-timeline-ai-state]')).to_have_text('AI 剪辑：失败')
        card.locator('[data-archive-timeline]').click()
        expect(page.locator('.timeline-history [data-timeline-id="uid-own"]')).to_have_count(1)
        self.assertEqual(self.calls[0]['body']['revision'], 2)

    def test_all_archived_has_no_active_completion_target_and_restore_reenables_build_step(self):
        job = self.payload['job']
        for timeline in job['timelines']:
            timeline['archived'] = True
        page = self.open_page()
        expect(page.locator('#timeline-cards > .timeline-card')).to_have_count(0)
        expect(page.locator('.timeline-card.current')).to_have_count(0)
        expect(page.locator('[data-timeline-count]')).to_have_text('0 条')
        expect(page.locator('.production-steps [data-step="timeline"]')).not_to_have_class('done')
        expect(page.locator('#timeline-form')).to_be_visible()
        expect(page.locator('[data-complete-job]')).to_be_disabled()
        expect(page.locator('#edit-panel #edit-form')).to_have_count(0)
        page.locator('.timeline-history > summary').click()
        page.locator('[data-archive-timeline="uid-own"]').click()
        expect(page.locator('.timeline-card.current')).to_have_attribute('data-timeline-id', 'uid-own')
        expect(page.locator('#edit-panel #edit-form [name="timeline"]')).to_have_value('uid-own')
        expect(page.locator('[data-complete-job]')).to_be_enabled()


if __name__ == '__main__':
    unittest.main()
