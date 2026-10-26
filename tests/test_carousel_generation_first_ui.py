"""Subject-locked handoff and historical generation-first results remain honest."""
import unittest

import test_carousel_autopilot_ui as fixture


class CarouselGenerationFirstBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.CarouselAutopilotBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.CarouselAutopilotBrowserTests.tearDownClass.__func__)
    setUp = fixture.CarouselAutopilotBrowserTests.setUp
    stop_server = fixture.CarouselAutopilotBrowserTests.stop_server
    request = fixture.CarouselAutopilotBrowserTests.request
    open_page = fixture.CarouselAutopilotBrowserTests.open_page
    make_playable_video = fixture.CarouselAutopilotBrowserTests.make_playable_video
    open_autopilot = fixture.CarouselAutopilotBrowserTests.open_autopilot
    autopilot_run = staticmethod(fixture.CarouselAutopilotBrowserTests.autopilot_run)

    def test_new_handoff_requests_subject_locked_generation(self):
        page = self.open_autopilot()
        page.locator('[data-autopilot-start]').click()
        page.locator('[data-autopilot-cancel]').wait_for()
        self.assertEqual(self.calls[0]['body']['workflow'], 'subject_locked')

    def test_resumed_handoff_requests_subject_locked_generation(self):
        page = self.open_autopilot(self.autopilot_run(
            workflow='review_first', status='failed', phase='AI 核对',
            error='历史核对未完成'))
        panel = page.locator('#carousel-autopilot-panel')
        self.assertIn('AI 编写逐张提示词', panel.inner_text())
        self.assertIn('参考原图视角生成不含产品的空背景', panel.inner_text())
        self.assertIn('随后在本地恢复原图主体', panel.inner_text())
        self.assertIn('逐张验证主体像素后打包', panel.inner_text())
        self.assertIn('产品主体不重绘', panel.inner_text())
        page.locator('[data-autopilot-start]').click()
        page.locator('[data-autopilot-cancel]').wait_for()
        self.assertEqual(self.calls[0]['body']['workflow'], 'subject_locked')
        self.assertIn('revision', self.calls[0]['body'])
        self.assertGreaterEqual(len(self.calls[0]['body']['request_id']), 10)

    def test_deferred_review_is_neutral_and_images_and_package_are_downloadable(self):
        page = self.open_autopilot(self.autopilot_run(
            workflow='generation_first', status='completed', phase='已出图',
            message='3 张图片已生成并打包', delivery_id='DLV-111111111111',
            review_deferred=True))
        item = self.payload['carousel']
        for slide in item['slides']:
            number = slide['number']
            output = {'id': f'OUT-{number:012x}', 'aspect': '3:4', 'approved': True,
                      'workflow': 'generation_first', 'ai_generated': True,
                      'review_deferred': True,
                      'review': {'status': 'deferred', 'execution_status': 'not_run',
                                 'completed': False, 'checks': []},
                      'stages': {'review': {'status': 'skipped'}}}
            slide.update(outputs=[output], output_id=output['id'])
        page.route('**/outputs/**', lambda route: route.fulfill(
            content_type='image/jpeg', body=self.source.read_bytes()))
        page.reload()
        panel = page.locator('#carousel-autopilot-panel')
        panel.get_by_text('图片已生成', exact=True).wait_for()
        self.assertIn('产品一致性核对暂未执行', panel.inner_text())
        self.assertIn('核对暂缓', panel.inner_text())
        self.assertNotIn('核对全部通过', panel.inner_text())
        package = panel.get_by_role('link', name='下载成品包')
        self.assertEqual(package.get_attribute('href'),
                         self.path + '/deliveries/DLV-111111111111/download')
        page.locator('[data-step="images"]').click()
        self.assertEqual(page.locator('[data-review-tier="unchecked"]').count(), len(item['slides']))
        self.assertEqual(page.locator('.review-needs-fix').count(), 0)
        self.assertEqual(page.locator('[data-review-status="pass"]').count(), 0)
        self.assertEqual(page.locator('[data-image-phase="review"][data-phase-status="skipped"]').count(), len(item['slides']))
        self.assertIn('暂未自动核对', page.locator('#carousel-image-slides').inner_text())
        downloads = page.locator('[data-download-output]')
        self.assertEqual(downloads.count(), len(item['slides']))
        with page.expect_download() as event:
            downloads.first.click()
        self.assertTrue(event.value.url.endswith(downloads.first.get_attribute('href')))
        # A later optional review must replace the deferred display, even while
        # the original workflow flag remains in the saved generation receipt.
        item['slides'][0]['outputs'][0]['review'] = {'status': 'running', 'checks': []}
        item['slides'][1]['outputs'][0]['review'] = {
            'status': 'needs_fix', 'completed': True,
            'checks': [{'id': 'product', 'status': 'fail', 'note': '请核对主体'}]}
        page.reload()
        page.locator('[data-step="images"]').click()
        self.assertEqual(page.locator('[data-image-slide="1"]').get_attribute('data-review-tier'), 'waiting')
        self.assertEqual(page.locator('[data-image-slide="2"]').get_attribute('data-review-tier'), 'attention')
        self.assertEqual(page.locator('[data-image-slide="2"] .review-needs-fix').count(), 1)
        self.assertEqual(page.locator('[data-image-slide="3"]').get_attribute('data-review-tier'), 'unchecked')

    def subject_outputs(self):
        item = self.payload['carousel']
        for slide in item['slides']:
            number = slide['number']
            output = {'id': f'OUT-{number:012x}', 'aspect': '3:4', 'approved': True,
                      'workflow': 'subject_locked', 'ai_generated': True,
                      'sha256': f'final-{number}', 'base_sha256': f'base-{number}',
                      'input_signature': f'input-{number}', 'review_deferred': True,
                      'requirements_snapshot': {'render_contract': 'segmented_background_v4'},
                      'review': {'status': 'deferred', 'execution_status': 'not_run',
                                 'completed': False, 'checks': []},
                      'subject_preservation': {
                          'status': 'passed', 'method': 'masked_source_pixels',
                          'render_contract': 'segmented_background_v4',
                          'final_sha256': f'final-{number}', 'base_sha256': f'base-{number}',
                          'input_signature': f'input-{number}',
                          'protected_pixel_count': 1000, 'mismatch_pixels': 0}}
            slide.update(outputs=[output], output_id=output['id'])
        return item

    def test_bound_subject_receipts_show_pixel_preservation_without_semantic_pass(self):
        page = self.open_autopilot(self.autopilot_run(
            workflow='subject_locked', status='completed', phase='已出图',
            message='3 张图片已生成并打包', delivery_id='DLV-333333333333',
            review_deferred=True, product_preservation_checked=True))
        item = self.subject_outputs()
        page.reload()
        panel = page.locator('#carousel-autopilot-panel')
        panel.get_by_text('原图主体已恢复', exact=True).wait_for()
        self.assertIn('已在 AI 背景上恢复原图主体并通过主体像素验证', panel.inner_text())
        self.assertNotIn('产品一致性核对暂未执行', panel.inner_text())
        self.assertEqual(panel.get_by_role('link', name='下载成品包').count(), 1)
        page.locator('[data-step="images"]').click()
        self.assertEqual(page.locator('[data-subject-preservation="passed"]').count(), len(item['slides']))
        self.assertEqual(page.locator('[data-review-tier="unchecked"]').count(), len(item['slides']))
        self.assertEqual(page.locator('[data-review-status="pass"]').count(), 0)
        self.assertIn('文字、证据与排版暂未自动核对', page.locator('#carousel-image-slides').inner_text())
        self.assertNotIn('核对全部通过', page.locator('#carousel-images-panel').inner_text())

    def test_wrong_workflow_or_unbound_receipts_never_claim_subject_preservation(self):
        page = self.open_autopilot(self.autopilot_run(
            workflow='subject_locked', status='completed', phase='已出图',
            message='3 张图片已生成', review_deferred=True))
        item = self.subject_outputs()
        item['slides'][0]['outputs'][0]['subject_preservation']['final_sha256'] = 'old-final'
        item['slides'][1]['outputs'][0]['workflow'] = 'generation_first'
        item['slides'][2]['outputs'][0].pop('subject_preservation')
        page.reload()
        panel = page.locator('#carousel-autopilot-panel')
        panel.get_by_text('图片待重新制作', exact=True).wait_for()
        self.assertNotIn('原图主体已恢复', panel.inner_text())
        self.assertIn('尚未取得整组当前主体恢复验证回执', panel.inner_text())
        page.locator('[data-step="images"]').click()
        self.assertEqual(page.locator('[data-subject-preservation="passed"]').count(), 0)
        self.assertEqual(page.locator('[data-subject-preservation="unverified"]').count(), 2)
        self.assertIn('核对暂缓', page.locator('[data-image-slide="2"]').inner_text())

    def test_old_render_contract_is_not_current_approved_delivery(self):
        page = self.open_autopilot(self.autopilot_run(
            workflow='subject_locked', status='completed', phase='旧版已出图',
            message='旧版记录', delivery_id='DLV-444444444444', review_deferred=True))
        item = self.subject_outputs()
        for slide in item['slides']:
            output = slide['outputs'][-1]
            output['subject_preservation'].pop('render_contract')
            output['requirements_snapshot'].pop('render_contract')
        page.reload()
        panel = page.locator('#carousel-autopilot-panel')
        panel.get_by_text('图片待重新制作', exact=True).wait_for()
        self.assertIn('此版本不能作为当前确认成品', panel.inner_text())
        self.assertEqual(panel.get_by_role('link', name='下载成品包').count(), 0)
        page.locator('[data-step="images"]').click()
        self.assertEqual(page.locator('[data-subject-preservation="passed"]').count(), 0)
        self.assertEqual(page.locator('[data-revoke-output]').count(), 0)
        self.assertEqual(page.locator('[data-approve-output]:disabled').count(), len(item['slides']))
        self.assertEqual(page.locator('#carousel-approve-all').inner_text(), '有图片需重新制作')
        self.assertTrue(page.locator('#carousel-approve-all').is_disabled())
        self.assertEqual(page.locator('[data-download-output]').count(), len(item['slides']))

    def test_explicit_review_first_history_retains_its_actual_result(self):
        page = self.open_autopilot(self.autopilot_run(
            workflow='review_first', status='completed', phase='已完成',
            message='图片核对与打包已完成', delivery_id='DLV-222222222222'))
        panel = page.locator('#carousel-autopilot-panel')
        panel.get_by_text('AI 制作已完成', exact=True).wait_for()
        self.assertEqual(panel.locator('[data-autopilot-generation-summary]').count(), 0)
        self.assertEqual(panel.get_by_role('link', name='下载成品包').get_attribute('href'),
                         self.path + '/deliveries/DLV-222222222222/download')


if __name__ == '__main__':
    unittest.main()
