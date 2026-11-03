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


if __name__ == "__main__":
    unittest.main()
