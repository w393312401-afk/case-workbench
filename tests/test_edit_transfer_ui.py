"""Exercise ZIP import and result download in the real page with temporary data."""
import threading

from playwright.sync_api import sync_playwright, expect

from test_edit_transfer import stations
from edit_transfer import export_package
from test_server import make_test_server


def test_browser_imports_package_and_downloads_receipt(stations, tmp_path):
    host, _, identifier = stations
    package = tmp_path / 'local-edit.zip';package.write_bytes(export_package(host, identifier))
    server = make_test_server(tmp_path / 'skill', tmp_path / 'runtime')
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=True)
            try:
                page = browser.new_page()
                errors = [];page.on('pageerror', lambda error: errors.append(str(error)))
                # The import page is reachable from the content page's menu, not only by typing its address.
                page.goto(f'http://127.0.0.1:{server.server_port}/#content')
                page.locator('.flow-main-menu > summary').click()
                page.get_by_role('menuitem', name='导入剪辑包（在这台电脑剪辑）').click()
                expect(page.get_by_role('heading', name='在这台电脑剪辑')).to_be_visible()
                page.locator('[data-local-package]').set_input_files(str(package))
                page.locator('[data-import-local]').click()
                expect(page.locator('[data-production-title]')).to_have_text('Test')
                expect(page.get_by_role('heading', name='本机剪辑制作单')).to_be_visible()
                with page.expect_download() as download:
                    page.locator('[data-local-download]').click()
                assert download.value.suggested_filename.endswith('-result.json')
                assert len(server.app.production.read()['jobs']) == 1
                receipt = tmp_path / 'result.json';download.value.save_as(receipt)
                # The same server plays the shared side: the original order receives the result.
                data = server.app.production.read();data['jobs'].append(host.production.job(identifier))
                server.app.production.write(data)
                page.goto(f'http://127.0.0.1:{server.server_port}/#production/{identifier}')
                expect(page.locator('[data-production-title]')).to_have_text('Test')
                # Most orders are cut on this computer: no card, only menu entries.
                expect(page.get_by_role('heading', name='换一台电脑剪辑')).to_have_count(0)
                page.locator('.production-more-actions > summary').click()
                expect(page.locator('[data-local-download]')).to_be_visible()
                page.locator('[data-local-result]').set_input_files(str(receipt))
                expect(page.locator('.local-edit-receipt')).to_contain_text('尚未建立时间线')
                assert server.app.production.job(identifier)['local_edit_results']
                assert errors == []
            finally:
                browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
