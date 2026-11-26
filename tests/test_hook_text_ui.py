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
        page.locator('.production-steps [data-step~="cut"]').click()
        return page, page.locator('#video-publication-panel [data-timeline-tools="uid-own"]')

    def test_button_adds_the_hook_and_then_offers_to_update_it(self):
        page, tools = self.tools('Stop guessing / Check the bevel')
        button = tools.locator('[data-add-hook]')
        expect(button).to_have_text('加上')
        expect(button).to_have_attribute('title','在开头 3 秒加上：Stop guessing / Check the bevel')
        button.click()
        expect(page.locator('#toast')).to_contain_text('屏显已加')
        expect(tools.locator('[data-hook-state="saved"]')).to_contain_text('开头：Stop guessing / Check the bevel')
        expect(tools.locator('[data-add-hook]')).to_have_text('更新')
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
        expect(tools.locator('[data-hook-option]')).to_have_count(3)
        expect(tools.locator('[data-hook-option="0"]')).to_have_attribute('aria-checked', 'true')
        expect(tools.locator('[data-hook-option="1"]')).to_contain_text('还是不快？/ 看看角度')
        expect(choice).to_have_value('0')
        expect(tools.locator('[data-hook-reading]')).to_have_text('还是不快？')
        expect(tools.locator('[data-hook-custom]')).to_be_hidden()
        tools.locator('[data-hook-option="1"]').click()
        expect(tools.locator('[data-hook-option="1"]')).to_have_attribute('aria-checked', 'true')
        expect(tools.locator('[data-hook-reading]')).to_have_text('还是不快？/ 看看角度')
        expect(button).to_have_attribute('title','在开头 3 秒加上：Still not sharp? / Check your angle')
        button.click()
        expect(page.locator('#toast')).to_contain_text('屏显已加')
        self.assertEqual(self.sent, 'Still not sharp? / Check your angle')
        # After the repaint the list shows what is on the timeline, not the recommended one.
        expect(tools.locator('[data-hook-choice]')).to_have_value('1')
        expect(tools.locator('[data-hook-option="1"]')).to_have_attribute('aria-checked', 'true')
        expect(tools.locator('[data-add-hook]')).to_have_text('更新')
        # Own words are one tab away, and the script's options one tab back.
        tools.locator('[data-hook-source="custom"]').click()
        expect(tools.locator('[data-hook-custom]')).to_be_visible()
        expect(tools.locator('[data-hook-list]')).to_be_hidden()
        tools.locator('[data-hook-source="script"]').click()
        expect(tools.locator('[data-hook-choice]')).to_have_value('1')

    def test_failure_shows_the_reason_and_lets_you_retry(self):
        page, tools = self.tools('One line')
        self.fail_with = '达芬奇没能插入 Text+，屏显没有加。'
        tools.locator('[data-add-hook]').click()
        expect(tools.locator('[data-timeline-tools-error]')).to_contain_text('没能插入 Text+')
        expect(tools.locator('[data-add-hook]')).to_have_text('加上')
        expect(tools.locator('[data-add-hook]')).to_be_enabled()


class CommentTranslationsBrowserTests(unittest.TestCase):
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

    def test_script_comments_are_listed_with_their_chinese_and_send_the_original_id(self):
        self.payload.update(timeline_uid='uid-own', timelines=[{'unique_id': 'uid-own', 'name': 'Lab261011-202-2'}],
                            copy={'comment_stickers': [{'id': 'r1', 'username': '@one', 'text': 'First comment', 'zh': '已有中文'},
                                                       {'id': 'r2', 'username': '@two', 'text': 'Second comment', 'zh': ''}]})
        def post(route):
            body = route.request.post_data_json
            if route.request.url.endswith('/comment-zh'):
                self.assertEqual(body, {'options': True})
                route.fulfill(status=200, content_type='application/json', body=json.dumps({'comments_zh': {'r2': '第二条翻译'}}, ensure_ascii=False))
            elif route.request.url.endswith('/comment-sticker'):
                self.sent = body
                line = {**self.payload['job']['timelines'][1], 'comment_sticker': {'status': 'saved', 'message': '评论贴纸已加', 'review_id': 'r2', 'username': '@two', 'text': 'Second comment'}}
                route.fulfill(status=200, content_type='application/json', body=json.dumps({'timeline': line}, ensure_ascii=False))
            else:
                route.fulfill(status=200, content_type='application/json', body='{}')
        page = self.open_page(post=post)
        page.locator('.production-steps [data-step~="cut"]').click()
        tools = page.locator('#video-publication-panel [data-timeline-tools="uid-own"]')
        expect(tools.locator('[data-comment-reading]')).to_have_text('已有中文')
        expect(tools.locator('[data-comment-option="0"]')).to_have_attribute('aria-checked', 'true')
        expect(tools.locator('[data-comment-option="0"]')).to_contain_text('已有中文')
        # The list is on show, so the missing Chinese is asked for at once.
        expect(tools.locator('[data-comment-option="1"]')).to_contain_text('第二条翻译')
        tools.locator('[data-comment-option="1"]').click()
        expect(tools.locator('[data-comment-reading]')).to_have_text('第二条翻译')
        tools.locator('[data-add-comment-sticker]').click()
        expect(tools.locator('[data-comment-sticker-state="saved"]')).to_be_visible()
        self.assertEqual(self.sent['review_id'], 'r2')
        self.assertNotIn('text', self.sent)
        tools = page.locator('#video-publication-panel [data-timeline-tools="uid-own"]')
        expect(tools.locator('[data-comment-option="1"]')).to_have_attribute('aria-checked', 'true')

    def test_failed_translation_keeps_choice_usable_and_can_be_retried(self):
        self.payload.update(timeline_uid='uid-own', timelines=[{'unique_id': 'uid-own', 'name': 'Lab261011-202-2'}],
                            copy={'comment_stickers': [{'id': 'r1', 'username': '@one', 'text': 'One', 'zh': '已有中文'},
                                                       {'id': 'r2', 'username': '@two', 'text': 'Two', 'zh': ''}]})
        self.attempts = 0
        def post(route):
            if route.request.url.endswith('/comment-zh') and route.request.post_data_json.get('options'):
                self.attempts += 1
                if self.attempts == 1:
                    route.fulfill(status=500, content_type='application/json', body='{"error":"失败"}')
                else:
                    route.fulfill(status=200, content_type='application/json', body=json.dumps({'comments_zh': {'r2': '重试成功'}}, ensure_ascii=False))
            else:
                route.fulfill(status=200, content_type='application/json', body='{}')
        page = self.open_page(post=post)
        page.locator('.production-steps [data-step~="cut"]').click()
        tools = page.locator('#video-publication-panel [data-timeline-tools="uid-own"]')
        option = tools.locator('[data-comment-option="1"]')
        expect(option).to_contain_text('中文暂不可用')
        option.click()
        expect(tools.locator('[data-add-comment-sticker]')).to_be_enabled()
        tools.locator('[data-comment-zh-retry]').click()
        expect(option).to_contain_text('重试成功')
        expect(option).to_have_attribute('aria-checked', 'true')
        self.assertEqual(self.attempts, 2)

    def test_search_the_library_or_let_the_ai_write_one(self):
        self.payload.update(timeline_uid='uid-own', timelines=[{'unique_id': 'uid-own', 'name': 'Lab261011-202-2'}], copy={'comment_stickers': []})
        self.sent = []
        def post(route):
            body = route.request.post_data_json
            url = route.request.url
            if url.endswith('/comment-search'):
                self.assertEqual(body, {'q': '磨不快'})
                route.fulfill(status=200, content_type='application/json', body=json.dumps({'total': 1, 'comments': [
                    {'id': 'abcdef0123456789', 'username': '@knife.guy', 'text': 'Mine never gets sharp', 'zh': '我的怎么都磨不快', 'avatar': '', 'product': '202H', 'likes': 12}]}, ensure_ascii=False))
            elif url.endswith('/comment-compose'):
                self.assertEqual(body, {'hint': '半信半疑'})
                route.fulfill(status=200, content_type='application/json', body=json.dumps({'username': '@sam_k', 'text': 'does this actually work?', 'zh': '这真的有用吗？'}, ensure_ascii=False))
            elif url.endswith('/comment-sticker'):
                self.sent.append(body)
                line = {**self.payload['job']['timelines'][1], 'comment_sticker': {'status': 'saved', 'message': '评论贴纸已加', 'review_id': body.get('review_id'), 'username': '@knife.guy', 'text': 'Mine never gets sharp'}}
                route.fulfill(status=200, content_type='application/json', body=json.dumps({'timeline': line}, ensure_ascii=False))
            else:
                route.fulfill(status=200, content_type='application/json', body='{}')
        page = self.open_page(post=post)
        page.locator('.production-steps [data-step~="cut"]').click()
        tools = page.locator('#video-publication-panel [data-timeline-tools="uid-own"]')
        # No comment came with the script: the library search is open by default and nothing is picked yet.
        expect(tools.locator('[data-comment-query]')).to_be_visible()
        expect(tools.locator('[data-comment-name]')).to_be_hidden()
        expect(tools.locator('[data-add-comment-sticker]')).to_be_disabled()
        tools.locator('[data-comment-query]').fill('磨不快')
        tools.locator('[data-comment-query]').press('Enter')
        result = tools.locator('[data-comment-result="0"]')
        expect(result).to_contain_text('@knife.guy')
        expect(result).to_contain_text('我的怎么都磨不快')
        result.click()
        expect(tools.locator('[data-comment-reading]')).to_have_text('我的怎么都磨不快')
        tools.locator('[data-add-comment-sticker]').click()
        expect(tools.locator('[data-comment-sticker-state="saved"]')).to_be_visible()
        self.assertEqual(self.sent[0]['review_id'], 'abcdef0123456789')
        self.assertNotIn('text', self.sent[0])
        # After the repaint the library pick stays shown; the AI fills the fields for a made-up one.
        tools = page.locator('#video-publication-panel [data-timeline-tools="uid-own"]')
        expect(tools.locator('[data-comment-picked]')).to_contain_text('@knife.guy')
        tools.locator('[data-comment-source="ai"]').click()
        expect(tools.locator('[data-comment-hint]')).to_be_visible()
        tools.locator('[data-comment-hint]').fill('半信半疑')
        tools.locator('[data-comment-compose]').click()
        expect(tools.locator('[data-comment-name]')).to_have_value('@sam_k')
        expect(tools.locator('[data-comment-text]')).to_have_value('does this actually work?')
        expect(tools.locator('[data-comment-reading]')).to_have_text('这真的有用吗？')
        tools.locator('[data-add-comment-sticker]').click()
        expect(tools.locator('[data-comment-sticker-state="saved"]')).to_be_visible()
        self.assertEqual({key: self.sent[1].get(key) for key in ('username', 'text', 'review_id')}, {'username': '@sam_k', 'text': 'does this actually work?', 'review_id': None})

class CampaignCardBrowserTests(HookButtonBrowserTests):
    """The end card and the pinned comment only show while a trusted campaign is on or about to start."""

    RUNNING = {'name': 'Black Friday / Cyber Monday（黑五网一）', 'start': '2026-11-11', 'end': '2026-11-30', 'status': 'running', 'card_from': '2026-11-04', 'card_open': True}

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
        page.locator('.production-steps [data-step~="cut"]').click()
        return page, page.locator('#video-publication-panel')

    def test_running_campaign_adds_the_end_card_and_offers_the_pinned_comment(self):
        page, panel = self.panel(self.RUNNING)
        expect(panel.locator('[data-copy-pin]')).to_contain_text('Black Friday sale ends Nov 30')
        expect(panel.locator('[data-copy-pin]')).to_contain_text('先确认本品已报名')
        expect(panel.locator('[data-campaign-text]')).to_be_hidden()
        panel.locator('[data-hook-source="campaign"]').click()
        expect(panel.locator('[data-campaign-text]')).to_be_visible()
        button = panel.locator('[data-add-hook]')
        expect(button).to_have_attribute('title','在结尾 3 秒加上：Black Friday & Cyber Monday sale / Ends Nov 30。口播不动，11/30 活动结束后记得去掉。')
        button.click()
        expect(page.locator('#toast')).to_contain_text('活动屏显已加')
        self.assertEqual((self.sent['action'], self.sent['text']), ('add', 'Black Friday & Cyber Monday sale / Ends Nov 30'))
        expect(panel.locator('[data-campaign-state="saved"]')).to_contain_text('结尾：Black Friday')
        panel.locator('[data-campaign-remove]').click()
        expect(panel.locator('[data-campaign-state]')).to_have_count(0)

    def test_campaign_opens_a_week_early_and_no_campaign_shows_nothing(self):
        _, panel = self.panel({**self.RUNNING, 'status': 'upcoming'})
        expect(panel.locator('[data-hook-source="campaign"]')).to_be_enabled()
        expect(panel.locator('[data-copy-pin]')).to_contain_text('活动期内发布再置顶')
        _, panel = self.panel({**self.RUNNING, 'status': 'upcoming', 'card_open': False})
        option = panel.locator('[data-hook-source="campaign"]')
        expect(option).to_be_disabled()
        expect(option).to_contain_text('起可加')
        _, panel = self.panel(None)
        expect(panel.locator('[data-hook-source="campaign"]')).to_have_count(0)
        expect(panel.locator('[data-copy-pin]')).to_have_count(0)


if __name__ == "__main__":
    unittest.main()
