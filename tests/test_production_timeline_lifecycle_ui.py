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
        if route.request.url.endswith('/subtitles'):
            self.assertTrue(body['request_id'])
            timeline = next(item for item in self.payload['job']['timelines'] if item['unique_id'] == body['timeline_uid'])
            timeline['subtitle_check'] = {
                'status': 'needs_review', 'message': '字幕已自动改 1 处，1 处待你确认', 'at': '2026-11-01T09:00:00+00:00',
                'changes': [{'start': 253, 'end': 300, 'before': 'On the Sharpeball 202H,', 'after': 'On the SHARPAL 202H,'}],
                'review': [{'start': 310, 'end': 346, 'text': 'use the water', 'wrote': 'water stone', 'expected': 'whetstone'}]}
            self.payload['revision'] += 1
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'timeline': timeline}))
            return
        if route.request.url.endswith('/timeline/delete'):
            timeline = next(item for item in self.payload['job']['timelines'] if item['unique_id'] == body['timeline_uid'])
            if body.get('preview'):
                result = {'timeline_uid': timeline['unique_id'], 'name': timeline['name'], 'bytes': 3145728, 'files': 4, 'edits': 1}
            else:
                self.assertTrue(body['request_id'])
                self.payload['job']['timelines'].remove(timeline)
                self.payload['revision'] += 1
                result = {'warnings': []}
            route.fulfill(status=200, content_type='application/json', body=json.dumps(result))
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

    def test_subtitle_check_runs_from_the_publish_step_and_keeps_later_saves_current(self):
        # The fixture answers the publication request with the same payload, so the panel's fields sit beside the job.
        self.payload.update(timeline_uid='uid-own', timelines=[{'unique_id': 'uid-own', 'name': 'Lab261011-202-2'}])
        page = self.open_page()
        expect(page.locator('#timeline-panel [data-check-subtitles]')).to_have_count(0)
        expect(page.locator('#timeline-panel [data-open-timeline]')).to_have_count(2)
        page.locator('.production-steps [data-step="publish"]').click()
        tools = page.locator('#video-publication-panel [data-timeline-tools="uid-own"]')
        expect(tools).to_contain_text('Lab261011-202-2')
        page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
        tools.locator('[data-copy-id]').click()
        expect(page.locator('#toast')).to_contain_text('时间线名称已复制')
        self.assertEqual(page.evaluate('navigator.clipboard.readText()'), 'Lab261011-202-2')
        tools.locator('[data-open-timeline]').click()
        expect(page.locator('#toast')).to_contain_text('已在达芬奇打开 Lab261011-202-2')
        expect(tools.locator('[data-subtitle-state]')).to_have_count(0)
        tools.locator('[data-check-subtitles]').click()
        expect(page.locator('#toast')).to_contain_text('字幕已自动改 1 处，1 处待你确认')
        expect(tools.locator('[data-subtitle-state="needs_review"]')).to_contain_text('字幕已自动改 1 处，1 处待你确认')
        details = tools.locator('.video-publication-subtitle-details')
        expect(details.locator('summary')).to_have_text('查看 2 处')
        self.assertFalse(details.evaluate('node => node.open'))
        details.locator('summary').click()
        expect(details).to_contain_text('On the SHARPAL 202H,')
        expect(details).to_contain_text('稿子是“whetstone”')
        expect(tools.locator('[data-check-subtitles]')).to_be_enabled()
        expect(tools.locator('[data-check-subtitles]')).to_have_text('核对字幕')
        # The check moved the revision; the timeline step's next save must send the new one.
        page.locator('.production-steps [data-step="timeline"]').click()
        page.locator('[data-timeline-task="uid-old"]').select_option('in_progress')
        expect(page.locator('#toast')).to_contain_text('已更新任务状态。')
        self.assertEqual(self.calls[-1]['body']['revision'], 2)

    def test_delete_asks_first_then_removes_the_card_and_keeps_order_target_and_drafts(self):
        page = self.open_page(width=390)
        form = page.locator('#edit-panel #edit-form')
        page.locator('.production-steps [data-step="edit"]').click()
        form.locator('[name="instruction"]').fill('保留这段尚未提交的剪辑要求')
        form.locator('[name="timeline"]').select_option('uid-own')
        page.evaluate('window.__editForm=document.querySelector("#edit-panel #edit-form")')
        page.locator('.production-steps [data-step="timeline"]').click()
        expect(page.locator('[data-archive-timeline]')).to_have_count(0)
        page.locator('[data-delete-timeline="uid-old"]').click()
        dialog = page.locator('dialog.production-dialog')
        expect(dialog).to_contain_text('Lab261010-202-1')
        expect(dialog).to_contain_text('1 次 AI 剪辑记录')
        expect(dialog).to_contain_text('3 MB')
        expect(dialog).to_contain_text('不会改动达芬奇里的时间线')
        dialog.locator('button[data-close]').last.click()
        expect(page.locator('#timeline-cards > .timeline-card')).to_have_count(2)
        self.assertEqual([call for call in self.calls if not call['body'].get('preview')], [])
        page.locator('[data-delete-timeline="uid-old"]').click()
        page.locator('dialog.production-dialog [data-submit]').click()
        expect(page.locator('#toast')).to_contain_text('时间线及本地文件已删除')
        expect(page.locator('#timeline-cards > .timeline-card')).to_have_count(1)
        expect(page.locator('[data-timeline-count]')).to_have_text('1 条')
        expect(page.locator('.timeline-history')).to_have_count(0)
        expect(form.locator('[name="timeline"]')).to_have_value('uid-own')
        expect(form.locator('[name="timeline"] option')).to_have_count(1)
        expect(form.locator('[name="instruction"]')).to_have_value('保留这段尚未提交的剪辑要求')
        self.assertTrue(page.evaluate('window.__editForm===document.querySelector("#edit-panel #edit-form")'))
        self.assertEqual([call['body']['timeline_uid'] for call in self.calls if not call['body'].get('preview')], ['uid-old'])
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 391)

    def test_legacy_archived_timeline_stays_in_history_with_restore_and_delete(self):
        self.payload['job']['timelines'][0]['archived'] = True
        page = self.open_page(width=390)
        history = page.locator('.timeline-history')
        expect(history.locator('summary')).to_have_text('历史时间线（1 条）')
        self.assertFalse(history.evaluate('node => node.open'))
        history.locator('summary').click()
        expect(history).to_contain_text('已归档')
        expect(history).to_contain_text('归档前：待剪辑')
        expect(history.locator('[data-delete-timeline="uid-old"]')).to_have_text('删除时间线')
        history.locator('[data-open-timeline]').click()
        expect(page.locator('#toast')).to_contain_text('已在达芬奇打开 Lab261010-202-1')
        history.locator('[data-archive-timeline="uid-old"]').click()
        expect(history).to_have_count(0)
        expect(page.locator('#timeline-cards > .timeline-card')).to_have_count(2)
        self.assertEqual([call['body']['archived'] for call in self.calls if '/timeline/archive' in call['url']], [False])
        expect(page.locator('[data-timeline-task="uid-old"]')).to_have_value('pending')
        expect(page.locator('[data-archive-timeline]')).to_have_count(0)

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
        expect(card.locator('[data-delete-timeline]')).to_be_disabled()
        expect(card.locator('[data-timeline-task]')).to_be_disabled()
        expect(card.locator('[data-timeline-ai-state]')).to_have_text('AI 剪辑：运行中')
        edit.update(status='failed', execution_status='failed')
        job['status'] = 'failed'
        self.payload['revision'] += 1
        expect(card.locator('[data-delete-timeline]')).to_be_enabled(timeout=8000)
        expect(card.locator('[data-timeline-ai-state]')).to_have_text('AI 剪辑：失败')

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
