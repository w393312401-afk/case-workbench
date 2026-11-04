"""Generate, play and download through the real page and authenticated proxy."""
from contextlib import ExitStack
import threading

import pytest
from playwright.sync_api import sync_playwright, expect

from test_edit_transfer import stations
from test_edit_transfer_ui import import_site
from test_server import make_test_server
from test_video_previews import preview_media, PreviewResolve, install_job


@pytest.mark.parametrize('remote', [False, True], ids=['local', 'remote-password'])
def test_unpublished_timeline_can_be_previewed_played_and_downloaded(stations, preview_media, tmp_path, remote):
    host, _, identifier = stations
    server = make_test_server(tmp_path / 'skill', tmp_path / 'runtime')
    original = install_job(server.app, host, identifier)
    server.app.resolve = PreviewResolve(preview_media)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True);thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=True)
            resources = ExitStack()
            try:
                context = browser.new_context(viewport={'width':390,'height':844})
                origin = resources.enter_context(import_site(context, server, tmp_path, remote))
                page = context.new_page();errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                if remote:
                    page.goto(origin+'/auth/login?next=%2F')
                    page.locator('#username').fill('fixture-admin');page.locator('#password').fill('temporary-import-pass')
                    page.locator('#login-submit').click();page.wait_for_url(origin+'/')
                page.goto(origin+f'/#production/{identifier}')
                page.locator('.production-steps [data-step="publish"]').click()
                button = page.locator('[data-preview-generate]')
                expect(button).to_be_enabled()
                button.click()
                expect(page.locator('[data-preview-player]')).to_be_visible(timeout=10000)
                player = page.locator('[data-preview-player]')
                player.evaluate('async video => {video.muted=true;await video.play();}')
                page.wait_for_function('''() => {
                    const video = document.querySelector('[data-preview-player]');
                    return video && !video.error && video.readyState >= 2 && video.currentTime > 0;
                }''')
                player.evaluate('video => video.pause()')
                expect(button).to_have_text('重新生成预览')
                with page.expect_download() as received:
                    page.locator('[data-preview-download]').click()
                destination = tmp_path / 'download.mp4';received.value.save_as(destination)
                assert destination.read_bytes() == preview_media.read_bytes()
                assert server.app.production.job(identifier) == original
                assert len(server.app.resolve.calls) == 1
                old_url = player.get_attribute('src')
                server.app.resolve.busy = lambda:True
                button.click()
                expect(page.locator('[data-preview-status]')).to_contain_text('其他操作')
                expect(button).to_be_enabled()
                assert player.get_attribute('src') == old_url and len(server.app.resolve.calls) == 1
                server.app.resolve.busy = lambda:False
                assert page.evaluate('document.documentElement.scrollWidth') <= 391
                page.screenshot(path=str(tmp_path/'preview-player.png'),full_page=True)
                if remote:
                    url = player.get_attribute('src')
                    context.clear_cookies()
                    status = page.evaluate('async url => (await fetch(url)).status', url)
                    assert status == 401
                    page.wait_for_url(origin+'/auth/login?next=**')
                assert not errors,errors
            finally:
                resources.close();browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
