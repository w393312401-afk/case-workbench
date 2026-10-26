"""Script-to-slide context stays visible across the table, picking and editing screens."""
import unittest

import test_production_carousel_ui as carousel_fixture
import test_carousel_table_background_ui as table_fixture


def add_context(item):
    item['script_context'] = {
        'script_id': item.get('script_id', 'SCP-0123456789ab'), 'script_version': 2,
        'records': [
            {'number': 1, 'spoken': 'Choose the angle for your knife.', 'zh': '按刀具选择角度。', 'proof': '', 'tags': [], 'evidence': ''},
            {'number': 2, 'spoken': 'The clamp position matters.', 'zh': '装夹位置也影响参照。', 'proof': '', 'tags': [], 'evidence': ''},
            {'number': 3, 'spoken': 'Tighten the knob.', 'zh': '拧紧旋钮。', 'proof': '', 'tags': [], 'evidence': ''},
            {'number': 4, 'spoken': 'Choose your finishing stage.', 'zh': '选择精修阶段。', 'proof': '', 'tags': [], 'evidence': ''},
        ],
    }
    for slide, numbers in zip(item['slides'], ([1, 2], [3], [4])):
        slide.update(source_lines=numbers, proof_goal=f"第 {slide['number']} 张承接原稿的选择理由",
                     evidence_requirement='手、旋钮和刻度须清楚；缺少锁紧动作特写', visual_method='动作特写与参照对照')
    return item


class CarouselLineageBrowserTests(unittest.TestCase):
    setUpClass = classmethod(carousel_fixture.CarouselBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(carousel_fixture.CarouselBrowserTests.tearDownClass.__func__)
    setUp = carousel_fixture.CarouselBrowserTests.setUp
    stop_server = carousel_fixture.CarouselBrowserTests.stop_server
    request = carousel_fixture.CarouselBrowserTests.request
    open_page = carousel_fixture.CarouselBrowserTests.open_page
    make_playable_video = carousel_fixture.CarouselBrowserTests.make_playable_video

    def seed_context(self):
        with self.app.production.lock:
            data = self.app.production.read()
            item = next(item for item in data['carousels'] if item['id'] == self.carousel['id'])
            add_context(item)
            self.app.production.write(data)

    def test_current_mapping_shows_source_and_evidence_and_survives_edit(self):
        self.seed_context()
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.locator('[data-step="slides"]').click()
        table = page.locator('.carousel-table')
        for label in ('原稿意群', '本张任务', '画面证据', '表达方式'):
            self.assertIn(label, table.locator('thead').inner_text())
        source = table.locator('tbody tr').first.locator('details')
        source.locator('summary').click()
        self.assertIn('Choose the angle for your knife.', source.inner_text())
        self.assertIn('装夹位置也影响参照。', source.inner_text())
        page.locator('[data-step="pick"]').click()
        pick = page.locator('[data-pick-slide="1"] [data-slide-lineage]')
        self.assertIn('缺少锁紧动作特写', pick.inner_text())
        self.assertIn('动作特写与参照对照', pick.inner_text())
        page.locator('[data-import-slide="1"]').click()
        imported = page.locator('.production-dialog[open]')
        self.assertIn('缺少锁紧动作特写', imported.locator('[data-slide-lineage]').inner_text())
        imported.get_by_role('button', name='取消', exact=True).click()
        page.locator('[data-step="images"]').click()
        image_context = page.locator('[data-image-slide="1"] > details')
        image_context.locator('summary').first.click()
        self.assertIn('缺少锁紧动作特写', image_context.inner_text())

        page.locator('[data-step="slides"]').click()
        page.locator('#carousel-slides-edit').click()
        modal = page.locator('.production-dialog[open]')
        modal.locator('[name="source-lines-1"]').fill('1，2')
        modal.locator('[name="proof-goal-1"]').fill('保留选角条件，先看刀具和装夹位置')
        modal.locator('[name="source-1"]').fill('旋钮近景，待补锁紧动作')
        modal.get_by_role('button', name='保存', exact=True).click()
        modal.wait_for(state='detached')
        saved = self.app.production.carousel(self.carousel['id'])
        slide = saved['slides'][0]
        self.assertEqual(slide['source_lines'], [1, 2])
        self.assertEqual(slide['proof_goal'], '保留选角条件，先看刀具和装夹位置')
        self.assertEqual(slide['evidence_requirement'], '手、旋钮和刻度须清楚；缺少锁紧动作特写')
        self.assertEqual(slide['visual_method'], '动作特写与参照对照')
        self.assertEqual(saved['script_context']['script_version'], 2)

        # The same new context remains readable in both system themes.
        for scheme in ('light', 'dark'):
            page.emulate_media(color_scheme=scheme)
            page.locator('[data-step="pick"]').click()
            self.assertTrue(page.locator('[data-pick-slide="1"] [data-slide-lineage]').is_visible())
            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth - document.documentElement.clientWidth'), 0)

    def test_legacy_mapping_is_not_guessed_and_invalid_reference_stays_in_editor(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.locator('[data-step="slides"]').click()
        self.assertIn('未建立对应', page.locator('.carousel-table tbody tr').first.inner_text())
        page.locator('#carousel-slides-edit').click()
        modal = page.locator('.production-dialog[open]')
        self.assertEqual(modal.locator('[name="source-lines-1"]').input_value(), '')
        self.assertEqual(modal.locator('[name="proof-goal-1"]').input_value(), '')
        modal.locator('[name="source-lines-1"]').fill('1,0')
        modal.get_by_role('button', name='保存', exact=True).click()
        modal.get_by_text('第 1 张的原稿意群须为不重复的正整数编号，用逗号分隔。', exact=True).wait_for()
        self.assertTrue(modal.is_visible())
        modal.locator('[name="source-lines-1"]').fill('')
        modal.locator('[name="role-1"]').fill('保留旧单作用')
        modal.get_by_role('button', name='保存', exact=True).click()
        modal.wait_for(state='detached')
        slide = self.app.production.carousel(self.carousel['id'])['slides'][0]
        self.assertEqual(slide.get('source_lines', []), [])
        self.assertEqual(slide.get('proof_goal', ''), '')
        self.assertEqual(slide['role'], '保留旧单作用')
        for key in ('source_lines', 'proof_goal', 'evidence_requirement', 'visual_method'):
            self.assertNotIn(key, slide, 'Blank legacy mapping must stay absent to preserve its image contract')


class CarouselLineageTablePreviewTests(unittest.TestCase):
    setUpClass = classmethod(table_fixture.CarouselTableBackgroundBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(table_fixture.CarouselTableBackgroundBrowserTests.tearDownClass.__func__)
    stop_server = table_fixture.CarouselTableBackgroundBrowserTests.stop_server
    request = table_fixture.CarouselTableBackgroundBrowserTests.request
    catalog = table_fixture.CarouselTableBackgroundBrowserTests.catalog
    create_script = table_fixture.CarouselTableBackgroundBrowserTests.create_script
    open_page = table_fixture.CarouselTableBackgroundBrowserTests.open_page

    def setUp(self):
        table_fixture.CarouselTableBackgroundBrowserTests.setUp(self)
        self.server.daemon_threads = False

    def test_table_preview_uses_saved_context_without_rewriting_copy(self):
        script = self.create_script()
        table = table_fixture.table_record(script, status='completed')
        add_context(table['carousels'][0])
        # The production endpoint embeds the frozen context inside the parsed carousel.
        # A conflicting historical wrapper must not replace this authoritative snapshot.
        table['script_context'] = {'records': [{'number': 1, 'spoken': 'Wrong wrapper source.'}]}
        page, _, endpoints = self.open_page(script=script, tables=[table])
        page.evaluate('({script,id}) => { void CaseProduction.resumeCarouselTable(script,id); }',
                      {'script': script, 'id': table['id']})
        modal = page.locator('.production-dialog[open]')
        modal.get_by_role('heading', name='核对图文逐张表', exact=True).wait_for()
        source = modal.locator('.carousel-table tbody tr').first.locator('details')
        source.locator('summary').click()
        self.assertIn('装夹位置也影响参照。', source.inner_text())
        self.assertIn('缺少锁紧动作特写', modal.inner_text())
        self.assertIn('Hook 1', modal.inner_text())
        self.assertEqual(endpoints.count('POST', '/api/production/carousels'), 0)


if __name__ == '__main__':
    unittest.main()
