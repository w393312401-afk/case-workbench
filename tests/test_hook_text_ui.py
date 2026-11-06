"""The publish step's hook button: what it says, what it sends and how it shows the result."""
from __future__ import annotations

import json
import unittest

from playwright.sync_api import expect

import test_production_manual_completion_ui as completion
import test_production_timeline_lifecycle_ui as lifecycle
import test_production_validation_ui as fixture


class HookButtonBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ProductionValidationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ProductionValidationBrowserTests.tearDownClass.__func__)
    stop_server = fixture.ProductionValidationBrowserTests.stop_server
    request = fixture.ProductionValidationBrowserTests.request
    make_playable_video = fixture.ProductionValidationBrowserTests.make_playable_video
    take = fixture.ProductionValidationBrowserTests.take
    edit = fixture.ProductionValidationBrowserTests.edit
    timeline = completion.ProductionManualCompletionBrowserTests.timeline
    receipt = completion.ProductionManualCompletionBrowserTests.receipt
    setUp = lifecycle.ProductionTimelineLifecycleBrowserTests.setUp
    open_page = lifecycle.ProductionTimelineLifecycleBrowserTests.open_page

    def mutate(self, route):
        body = route.request.post_data_json
        self.assertTrue(route.request.url.endswith('/hook'))
        self.assertTrue(body['request_id'])
        timeline = next(item for item in self.payload['job']['timelines'] if item['unique_id'] == body['timeline_uid'])
        if self.fail_with:
            route.fulfill(status=409, content_type='application/json', body=json.dumps({'error': self.fail_with}))
            return
        self.sent = body['text']
        timeline['hook_overlay'] = {'status': 'saved', 'message': '屏显已加', 'created': True, 'text': body['text'],
                                    'start': 0, 'end': 90, 'at': '2026-11-01T09:00:00+00:00'}
        self.payload['revision'] += 1
        route.fulfill(status=200, content_type='application/json', body=json.dumps({'timeline': timeline}))

    def tools(self, hook_text, options=None):
        self.fail_with = ''
        self.payload.update(timeline_uid='uid-own', timelines=[{'unique_id': 'uid-own', 'name': 'Lab261011-202-2'}], copy={'screen_en': hook_text, 'screen_options': options or []})
        page = self.open_page()
        page.locator('.production-steps [data-step="publish"]').click()
        return page, page.locator('#video-publication-panel [data-timeline-tools="uid-own"]')

    def test_button_adds_the_hook_and_then_offers_to_update_it(self):
        page, tools = self.tools('Stop guessing / Check the bevel')
        button = tools.locator('[data-add-hook]')
        expect(button).to_have_text('加屏显')
        expect(button).to_have_attribute('title','在开头 3 秒加上：Stop guessing / Check the bevel')
        button.click()
        expect(page.locator('#toast')).to_contain_text('屏显已加')
        expect(tools.locator('[data-hook-state="saved"]')).to_contain_text('Stop guessing / Check the bevel')
        expect(tools.locator('[data-add-hook]')).to_have_text('更新屏显')
        expect(tools.locator('[data-add-hook]')).to_be_enabled()

    def test_order_without_a_hook_can_still_use_own_words(self):
        for value in ('', '无开头屏显：第一句够强'):
            with self.subTest(value=value):
                _, tools = self.tools(value)
                button = tools.locator('[data-add-hook]')
                expect(button).to_be_disabled()
                expect(button).to_have_attribute('title','这张制作单还没有开头屏显，可以选“自己写”。')
                expect(tools.locator('[data-hook-choice]')).to_have_value('custom')
        tools.locator('[data-hook-custom]').fill('My words / Second line')
        expect(button).to_be_enabled()
        expect(button).to_have_attribute('title','在开头 3 秒加上：My words / Second line')
        button.click()
        expect(tools.locator('[data-hook-state="saved"]')).to_be_visible()
        self.assertEqual(self.sent, 'My words / Second line')

    def test_three_options_default_to_the_recommended_one_and_send_the_chosen_one(self):
        page, tools = self.tools('Still not sharp?', options=[
            {'label': '同写', 'en': 'Still not sharp?', 'zh': '还是不快？'},
            {'label': '点破', 'en': 'Still not sharp? / Check your angle', 'zh': '还是不快？/ 看看角度'},
            {'label': '好处', 'en': 'Your hands do the work / The guide holds it', 'zh': '手上的活还是你的'}])
        choice, button = tools.locator('[data-hook-choice]'), tools.locator('[data-add-hook]')
        expect(choice.locator('option')).to_have_count(4)
        expect(choice).to_have_value('0')
        expect(tools.locator('[data-hook-reading]')).to_have_text('还是不快？')
        expect(tools.locator('[data-hook-custom]')).to_be_hidden()
        choice.select_option('1')
        expect(tools.locator('[data-hook-reading]')).to_have_text('还是不快？/ 看看角度')
        expect(button).to_have_attribute('title','在开头 3 秒加上：Still not sharp? / Check your angle')
        button.click()
        expect(page.locator('#toast')).to_contain_text('屏显已加')
        self.assertEqual(self.sent, 'Still not sharp? / Check your angle')
        # After the repaint the list shows what is on the timeline, not the recommended one.
        expect(tools.locator('[data-hook-choice]')).to_have_value('1')
        expect(tools.locator('[data-add-hook]')).to_have_text('更新屏显')

    def test_failure_shows_the_reason_and_lets_you_retry(self):
        page, tools = self.tools('One line')
        self.fail_with = '达芬奇没能插入 Text+，屏显没有加。'
        tools.locator('[data-add-hook]').click()
        expect(tools.locator('[data-timeline-tools-error]')).to_contain_text('没能插入 Text+')
        expect(tools.locator('[data-add-hook]')).to_have_text('加屏显')
        expect(tools.locator('[data-add-hook]')).to_be_enabled()


class CampaignCardBrowserTests(HookButtonBrowserTests):
    """The end card and the pinned comment only show while a trusted campaign is on or about to start."""

    RUNNING = {'name': 'Black Friday / Cyber Monday（黑五网一）', 'start': '2026-11-11', 'end': '2026-11-30', 'status': 'running'}

    def campaign_post(self, route):
        body = route.request.post_data_json
        if not route.request.url.endswith('/campaign'):  # e.g. the titles' Chinese reading
            route.fulfill(status=200, content_type='application/json', body='{}')
            return
        self.sent = body
        timeline = next(item for item in self.payload['job']['timelines'] if item['unique_id'] == body['timeline_uid'])
        timeline['campaign_overlay'] = ({'status': 'saved', 'message': '活动屏显已加', 'text': body['text'], 'at': '2026-11-12T09:00:00+00:00'}
                                        if body['action'] == 'add' else {'status': 'removed', 'message': '活动屏显已去掉', 'text': '', 'at': '2026-12-01T09:00:00+00:00'})
        self.payload['revision'] += 1
        route.fulfill(status=200, content_type='application/json', body=json.dumps({'timeline': timeline}))

    def panel(self, campaign):
        self.payload.update(timeline_uid='uid-own', timelines=[{'unique_id': 'uid-own', 'name': 'Lab261011-202-2'}],
                            copy={'screen_en': 'Hook', 'titles': ['🔴Title#tag'], 'en': 'Words.', 'campaign': campaign,
                                  'card_text': 'Black Friday & Cyber Monday sale / Ends Nov 30',
                                  'pin': {'en': 'Black Friday sale ends Nov 30. Tap the cart.', 'zh': ''}})
        page = lifecycle.ProductionTimelineLifecycleBrowserTests.open_page(self, post=self.campaign_post)
        page.locator('.production-steps [data-step="publish"]').click()
        return page, page.locator('#video-publication-panel')

    def test_running_campaign_adds_the_end_card_and_offers_the_pinned_comment(self):
        page, panel = self.panel(self.RUNNING)
        expect(panel.locator('[data-copy-pin]')).to_contain_text('Black Friday sale ends Nov 30')
        expect(panel.locator('[data-copy-pin]')).to_contain_text('先确认本品已报名')
        button = panel.locator('[data-campaign-card="add"]')
        expect(button).to_be_enabled()
        button.click()
        expect(page.locator('#toast')).to_contain_text('活动屏显已加')
        self.assertEqual((self.sent['action'], self.sent['text']), ('add', 'Black Friday & Cyber Monday sale / Ends Nov 30'))
        panel.locator('[data-campaign-card="remove"]').click()
        expect(panel.locator('[data-campaign-card="add"]')).to_be_visible()

    def test_upcoming_campaign_waits_and_no_campaign_shows_nothing(self):
        _, panel = self.panel({**self.RUNNING, 'status': 'upcoming'})
        expect(panel.locator('[data-campaign-card="add"]')).to_be_disabled()
        expect(panel.locator('[data-copy-pin]')).to_contain_text('活动期内发布再置顶')
        _, panel = self.panel(None)
        expect(panel.locator('.video-publication-campaign-card')).to_have_count(0)
        expect(panel.locator('[data-copy-pin]')).to_have_count(0)


if __name__ == "__main__":
    unittest.main()
