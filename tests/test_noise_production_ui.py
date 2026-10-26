"""Information priority and truthful completed states, using isolated HTTP/browser fixtures."""
from __future__ import annotations

import os
from pathlib import Path
import unittest

import test_production_video_review_ui as video
import test_production_carousel_receipts_ui as carousel
import test_video_publication_ui as publication


def evidence(page,name):
    directory = os.environ.get('NOISE_UI_EVIDENCE_DIR')
    if directory:
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        page.evaluate('window.scrollTo(0,0)')
        page.screenshot(path=str(output / name), full_page=True)


class NoiseVideoReviewTests(unittest.TestCase):
    setUpClass = classmethod(video.ProductionVideoReviewBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(video.ProductionVideoReviewBrowserTests.tearDownClass.__func__)
    setUp = video.ProductionVideoReviewBrowserTests.setUp
    stop_server = video.ProductionVideoReviewBrowserTests.stop_server
    request = video.ProductionVideoReviewBrowserTests.request
    make_playable_video = video.ProductionVideoReviewBrowserTests.make_playable_video
    open_page = video.ProductionVideoReviewBrowserTests.open_page
    edit = video.ProductionVideoReviewBrowserTests.edit
    take = video.ProductionVideoReviewBrowserTests.take
    reviewable = video.ProductionVideoReviewBrowserTests.reviewable
    skill_delivery = video.ProductionVideoReviewBrowserTests.skill_delivery

    def test_current_result_and_review_lead_the_step_and_reedit_draft_survives_approval(self):
        edit = self.skill_delivery()

        def approve(route):
            edit['approval'] = {'at': '2026-10-23T12:00:00', 'note': '已逐项核对'}
            self.payload['job']['status'] = 'approved'
            route.fulfill(content_type='application/json', body='{"approved":true}')

        page = self.open_page(width=390, post=approve)
        page.locator('[data-step="edit"]').click()
        self.assertTrue(page.locator('.edit-review-form').is_visible())
        self.assertFalse(page.locator('#edit-panel #edit-form').is_visible())
        evidence(page,'video-review-results-first.png')
        if os.environ.get('NOISE_UI_EVIDENCE_DIR'):
            page.set_viewport_size({'width':1280,'height':720})
            evidence(page,'video-review-results-first-desktop.png')
            page.set_viewport_size({'width':390,'height':740})
        self.assertTrue(page.evaluate("document.querySelector('#edit-runs').compareDocumentPosition(document.querySelector('#edit-form')) & Node.DOCUMENT_POSITION_FOLLOWING"))
        page.locator('[data-production-controls="edit"] > summary').click()
        page.locator('#edit-panel #edit-form [name="instruction"]').fill('下一版保留我的要求')
        for check in page.locator('.edit-review-form input[name="reviewed"]').all():
            check.check()
        page.locator('[data-approve-edit]').click()
        page.locator('[data-revoke-edit]').wait_for()
        self.assertTrue(page.locator('#edit-panel #edit-form').is_visible())
        self.assertEqual(page.locator('#edit-panel #edit-form [name="instruction"]').input_value(), '下一版保留我的要求')
        self.assertEqual(len(self.calls), 1)
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 391)

    def test_approval_makes_old_questions_history_without_claiming_machine_verification(self):
        edit = self.skill_delivery()
        edit['report']['questions'] = ['调角旋钮方向待人工判断']
        edit['approval'] = {'at': '2026-10-23T12:00:00', 'note': '人工已核对'}
        self.payload['job']['status'] = 'approved'
        page = self.open_page()
        history = page.locator('.edit-delivery-questions')
        self.assertFalse(history.evaluate('node => node.open'))
        self.assertIn('工程核验未执行', page.locator('.edit-validation-note').inner_text())
        self.assertEqual(page.get_by_role('heading', name='待你确认', exact=True).count(), 0)
        history.locator('summary').click()
        self.assertIn('调角旋钮方向待人工判断', history.inner_text())
        self.assertNotIn('问题已解决', history.inner_text())
        edit.pop('approval')
        self.payload['job']['status'] = 'awaiting_review'
        page.reload()
        page.get_by_role('heading', name='待你确认', exact=True).wait_for()
        self.assertTrue(page.locator('.edit-review-form').is_visible())

    def test_exact_heading_duplicate_is_removed_but_variant_number_and_timeline_stay(self):
        self.skill_delivery()
        self.payload['job'].update(title='Lab261023-202-1 | 固定角选择 → 换购判断',
                                   variant_label='变体 #3 · 固定角选择 → 换购判断')
        page = self.open_page()
        heading = page.locator('.page-heading')
        self.assertEqual(heading.inner_text().count('固定角选择 → 换购判断'), 1)
        self.assertIn('变体 #3', heading.inner_text())
        self.assertIn('Lab261023-202-1', heading.locator('h1').inner_text())
        self.payload['job']['title'] = 'Lab261023-202-1 | 变体 #30 · 另一项测试'
        page.reload()
        page.locator('[data-production-title]').wait_for()
        self.assertIn('变体 #3', page.locator('.page-heading p').inner_text())

    def test_a_new_target_without_its_own_result_keeps_initial_edit_controls_open(self):
        self.skill_delivery()
        timeline = dict(self.payload['job']['timelines'][0], unique_id='uid-new-target', name='新建待剪时间线')
        self.payload['job']['timelines'].append(timeline)
        page = self.open_page()
        page.locator('[data-step="edit"]').click()
        self.assertEqual(page.locator('#edit-panel #edit-form [name="timeline"]').input_value(), 'uid-new-target')
        self.assertTrue(page.locator('#edit-panel #edit-form').is_visible())


class NoiseCarouselTests(unittest.TestCase):
    setUpClass = classmethod(carousel.CarouselReceiptsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(carousel.CarouselReceiptsBrowserTests.tearDownClass.__func__)
    setUp = carousel.CarouselReceiptsBrowserTests.setUp
    stop_server = carousel.CarouselReceiptsBrowserTests.stop_server
    request = carousel.CarouselReceiptsBrowserTests.request
    open_page = carousel.CarouselReceiptsBrowserTests.open_page
    make_playable_video = carousel.CarouselReceiptsBrowserTests.make_playable_video
    add_base = carousel.CarouselReceiptsBrowserTests.add_base
    stop_images = carousel.CarouselReceiptsBrowserTests.stop_images
    open_images = carousel.CarouselReceiptsBrowserTests.open_images
    generate = carousel.CarouselReceiptsBrowserTests.generate
    detail = carousel.CarouselReceiptsBrowserTests.detail
    body = carousel.CarouselReceiptsBrowserTests.body
    record_review = carousel.CarouselReceiptsBrowserTests.record_review

    def finish_group(self):
        page = self.open_images(width=390)
        self.generate(page)
        for number in (1, 2, 3):
            page.locator(f'[data-image-slide="{number}"] [data-approve-output]').click()
            page.locator(f'[data-image-slide="{number}"] [data-revoke-output]').wait_for()
        return page

    def test_completed_group_prioritizes_delivery_and_keeps_redo_import_and_audit_available(self):
        page = self.finish_group()
        page.reload()
        page.locator('[data-step="delivery"][aria-selected="true"]').wait_for()
        self.assertFalse(page.locator('[data-autopilot-start]').is_visible())
        evidence(page,'carousel-completed-delivery.png')
        self.assertEqual(page.locator('#carousel-overview').evaluate('node => getComputedStyle(node).position'), 'static')
        page.locator('[data-step="images"]').click()
        self.assertFalse(page.locator('#carousel-image-form').is_visible())
        self.assertFalse(page.locator('#carousel-bulk-actions').is_visible())
        self.assertTrue(page.locator('#carousel-import-finished').is_visible())
        self.assertFalse(page.locator('[data-redo-slide="1"]').is_visible())
        page.locator('[data-image-slide="1"] .image-redo-controls > summary').click()
        self.assertTrue(page.locator('[data-redo-slide="1"]').is_visible())
        for empty in page.locator('.image-review-empty').all():
            self.assertIn('已确认选用', empty.inner_text())
            self.assertNotIn('请人工核对', empty.inner_text())
        self.assertFalse(page.locator('#carousel-images-service').is_visible())
        page.locator('[data-production-controls="images"] > summary').click()
        self.assertTrue(page.locator('#carousel-image-form').is_visible())
        self.assertTrue(page.locator('#carousel-bulk-actions').is_visible())
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 391)
        page.locator('.image-redo-controls,[data-production-controls="images"]').evaluate_all('nodes => nodes.forEach(node => {node.open=false;})')
        page.set_viewport_size({'width':1280,'height':720})
        self.assertLessEqual(page.locator('.image-stack-cover').first.evaluate('node => node.getBoundingClientRect().width'), 180)
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 1281)
        evidence(page,'carousel-completed-images-desktop.png')
        page.emulate_media(color_scheme='dark')
        evidence(page,'carousel-completed-images-dark.png')

    def test_review_failure_stays_visible_after_manual_confirmation(self):
        page = self.finish_group()
        output = self.detail()['slides'][0]['outputs'][0]
        self.record_review(output, [{'id': key, 'status': 'fail' if key == 'text' else 'pass',
                                    'note': '原核对问题仍保留'}
                                   for key in ('product', 'evidence', 'text', 'visual')], 'needs_fix')
        page.reload()
        page.locator('[data-step="images"]').click()
        card = page.locator(f'[data-output-id="{output["id"]}"]')
        self.assertTrue(card.locator('[data-review-check="text"]').is_visible())
        self.assertEqual(card.locator('[data-review-check="text"]').get_attribute('data-review-status'), 'fail')
        self.assertTrue(page.locator('#carousel-image-form').is_visible())
        self.assertTrue(page.locator('[data-autopilot-start]').is_visible())
        self.assertTrue(page.locator('[data-redo-slide="1"]').is_visible())


class NoisePublicationTests(unittest.TestCase):
    setUpClass = classmethod(publication.VideoPublicationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(publication.VideoPublicationBrowserTests.tearDownClass.__func__)
    setUp = publication.VideoPublicationBrowserTests.setUp
    mount = publication.VideoPublicationBrowserTests.mount

    def test_published_receipt_leads_folded_copy_and_uses_frozen_take_label(self):
        # The workbench always loads the source component the receipt folds its trace facts into.
        self.page.add_script_tag(path=str(Path(__file__).resolve().parents[1] / "static" / "delivery-provenance.js"))
        self.mount("""window.publicationDetail.copy={version:1,titles:['Title #fixture'],zh:'中文发布稿',en:'English copy'};
          window.publicationDetail.publication={id:'VPU-111111111111',post_id:'7999999999999999901',
            published_on:'2026-10-23',source_current:true,calendar_uploaded:false,script_version:1,
            source:{take:{id:'TAK-frozen'},timeline:{name:'Frozen timeline'}},registered_at:'2026-10-23'};
          window.copyText=(text)=>{window.copiedText=text;};""")
        page = self.page
        receipt = page.locator('.video-publication-receipt')
        self.assertIn('当时选用配音', receipt.inner_text())
        self.assertNotIn('TAK-frozen', receipt.inner_text())
        self.assertEqual(receipt.locator('[title="TAK-frozen"]').count(), 1)
        self.assertTrue(page.evaluate("document.querySelector('.video-publication-receipt').compareDocumentPosition(document.querySelector('.video-publish-copy-compact')) & Node.DOCUMENT_POSITION_FOLLOWING"))
        self.assertFalse(page.locator('.video-publish-copy-fold').evaluate('node => node.open'))
        page.locator('.video-publish-copy-compact > [data-copy-publish]').click()
        self.assertEqual(page.evaluate('window.copiedText'), '中文发布稿\n\nEnglish copy')
        self.assertTrue(receipt.locator('[data-calendar-toggle]').is_visible())
        # One source fold carries the registration facts and the take's copy button; no second trace fold.
        self.assertEqual(receipt.get_by_text('查看成片追溯信息', exact=True).count(), 0)
        receipt.locator('.delivery-provenance > summary').click()
        self.assertEqual(receipt.locator('.delivery-provenance dt', has_text='登记时间').count(), 1)
        receipt.locator('[data-copy-take]').click()
        self.assertEqual(page.evaluate('window.copiedText'), 'TAK-frozen')
        page.locator('.video-publication-editor > summary').click()
        self.assertIn('绑定另一个视频', page.locator('.video-publication-editor > summary').inner_text())

    def test_conversion_starts_folded_but_failed_attempt_keeps_its_recovery_form_open(self):
        page = self.page
        page.evaluate("""async () => {
          window.conversion={runs:[],running:false,source:{available:true,kind:'file',label:'隔离成片'}};
          window.mountConversion=()=>VideoPublication.mount(document.querySelector('#publication'),{
            job:{id:'PRD-111111111111'},api:async(url)=>url.endsWith('/video-carousel') ? structuredClone(conversion) :
              url==='/api/ai/options' ? {models:[],default_settings:{}} : {history:[],source:{},can_register:false}
          });
          await mountConversion();
        }""")
        page.locator('.video-carousel-again').wait_for()
        self.assertFalse(page.locator('.video-carousel-form').is_visible())
        page.locator('.video-carousel-again > summary').click()
        self.assertTrue(page.locator('.video-carousel-form').is_visible())
        page.evaluate("""async()=>{conversion.runs=[{id:'TBL-111111111111',status:'failed',error:'隔离失败：已保存部分结果'}];await mountConversion();}""")
        page.get_by_text('隔离失败：已保存部分结果', exact=False).wait_for()
        self.assertTrue(page.locator('.video-carousel-form').is_visible())


if __name__ == '__main__':
    unittest.main()
