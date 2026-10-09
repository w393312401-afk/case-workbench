"""Cross-page direction integration using disposable records and browser profiles."""
import copy
import unittest
from uuid import uuid4

import test_server as fixture
import test_ui_regressions as browser


class PhaseThreeIntegrationTests(unittest.TestCase):
    setUp = browser.WorkbenchBrowserTests.setUp
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    open_page = browser.WorkbenchBrowserTests.open_page
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def mutate(self, path, **values):
        catalog = self.request('GET', '/api/direction-catalog')[2]
        status, _, result = self.request('POST', path, {
            'revision': catalog['revision'], 'request_id': str(uuid4()), **values})
        self.assertEqual(status, 200, result)
        return result

    def test_unattached_direction_search_opens_detail_and_product_switch_leaves_it(self):
        point = self.mutate('/api/selling-points', point={
            'product': '202H', 'code': 'S3', 'title': '按用途选择角度'})['point']
        self.mutate('/api/selling-points', point={
            'product': '129N', 'code': 'S1', 'title': '便携维护'})
        self.mutate('/api/direction-catalog', direction={
            'product': '202H', 'selling_point_id': point['id'],
            'concern': '不同刀具如何选择', 'closing': '按需要选择'})
        page = self.open_page('#/202H/library')
        page.keyboard.press('Control+k')
        page.locator('#workbench-search-input').fill('202H-S3')
        result = page.locator('.global-search-result[href="#/202H/directions/202H-S3?archive=all"]')
        result.wait_for()
        result.click()
        page.get_by_role('heading', name='202H-S3', exact=True).wait_for()
        page.locator('#global-product').select_option('129N')
        page.locator('#direction-filters').wait_for()
        self.assertTrue(page.url.endswith('#/129N/directions?archive=all'), page.url)
        self.assertEqual(page.locator('.direction-overview-card').count(), 0)
        page.locator('[data-page="settings"]').click()
        page.locator('[data-settings-copy="directions"]').wait_for()
        self.assertIn(str(self.runtime / 'directions'), page.locator('.settings-locations').inner_text())

    def test_external_direction_edit_prompts_refresh_without_replacing_playing_video(self):
        current = copy.deepcopy(self.store.read_case(self.identifier)['current'])
        current['test_direction'] = {
            'direction_id': '202H-S1', 'main_difference': 'S1 守住角度',
            'concern': '原案例顾虑', 'closing': '原案例收口', 'variant_type': 'execution'}
        self.store.append(self.identifier, {'kind': 'editorial', 'text': '隔离方向夹具', 'current': current})
        page = self.open_page(f'#case/{self.identifier}')
        page.locator('#case-player').wait_for(state='attached')
        page.evaluate("window.phase3Player = document.querySelector('#case-player')")
        catalog = self.request('GET', '/api/direction-catalog')[2]
        direction = {**catalog['directions'][0], 'concern': '更新后的方向默认顾虑'}
        self.mutate('/api/direction-catalog', operation='update', direction=direction)
        page.evaluate('refresh(true)')
        page.locator('#case-update-banner').wait_for()
        self.assertTrue(page.evaluate("document.querySelector('#case-player') === window.phase3Player"))
        self.assertEqual(self.store.read_case(self.identifier)['current']['test_direction'], current['test_direction'])
        page.locator('#case-update-banner button').click()
        page.locator('#detail-section-test-direction').get_by_text('更新后的方向默认顾虑', exact=False).wait_for()
        self.assertIn('原案例顾虑', page.locator('#detail-section-test-direction').inner_text())


if __name__ == '__main__':
    unittest.main()
