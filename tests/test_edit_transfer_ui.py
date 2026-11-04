"""Exercise ZIP import and result download in the real page with temporary data."""
from contextlib import ExitStack, contextmanager
import http.client
import io
import json
from pathlib import Path
import queue
import subprocess
import threading
import wave
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright, expect

from test_edit_transfer import stations, package_with_manifest
from edit_transfer import export_package
from remote_accounts import RemoteAccounts
from test_server import make_test_server


@contextmanager
def import_site(context, server, tmp_path, remote):
    if not remote:
        yield f'http://127.0.0.1:{server.server_port}'
        return
    # A remote HTTPS browser origin relays through the real password proxy and
    # Python server. Only transport is replaced, so no certificate is needed.
    origin = 'https://workbench.test'
    account = RemoteAccounts(tmp_path / 'proxy-runtime')
    account.directory.mkdir(parents=True)
    config = dict(hostname='workbench.test', authMode='password',
                  upstream=f'http://127.0.0.1:{server.server_port}')
    account.config.write_text(json.dumps(config), encoding='utf-8')
    account.save(dict(username='fixture-admin', password='temporary-import-pass', confirm_password='temporary-import-pass'))
    module = (Path(__file__).resolve().parents[1] / 'deployment/remote_proxy.mjs').as_uri()
    script = (f'import {{ createRemoteProxy }} from {json.dumps(module)};'
              f'const server = createRemoteProxy({json.dumps(config)}, {{accountFile:{json.dumps(str(account.account))}}});'
              'server.listen(0, "127.0.0.1", () => console.log(server.address().port));')
    proxy = subprocess.Popen(['node', '--input-type=module', '-e', script], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
    ready = queue.Queue()
    threading.Thread(target=lambda: ready.put(proxy.stdout.readline()), daemon=True).start()
    try:
        port = int(ready.get(timeout=10).strip())

        def relay(route):
            request = route.request
            headers = {key: value for key, value in request.all_headers().items()
                       if key not in {'host', 'connection', 'accept-encoding', 'content-length'}}
            headers['Host'] = config['hostname']
            url = urlsplit(request.url)
            body = request.post_data_buffer
            if body is None and request.method == 'POST' and url.path == '/api/production/local-edit/import':
                # Chromium omits file-backed XHR bodies from interception. Relay
                # the exact selected fixture File, without changing the page's upload.
                body = bytes(request.frame.locator('[data-local-package]').evaluate(
                    'async input => Array.from(new Uint8Array(await input.files[0].arrayBuffer()))'))
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
            try:
                connection.request(request.method, url.path + ('?' + url.query if url.query else ''),
                                   body=body, headers=headers)
                response = connection.getresponse()
                body = response.read()
                response_headers = {key: value for key, value in response.getheaders()
                                    if key.lower() not in {'connection', 'transfer-encoding', 'content-length'}}
                route.fulfill(status=response.status, headers=response_headers, body=body)
            finally:
                connection.close()

        context.route(origin + '/**', relay)
        yield origin
    finally:
        context.unroute(origin + '/**')
        proxy.terminate()
        try:
            proxy.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proxy.kill();proxy.communicate(timeout=5)


@pytest.mark.parametrize('remote', [False, True], ids=['local', 'remote-password'])
def test_browser_imports_package_and_downloads_receipt(stations, tmp_path, remote):
    host, _, identifier = stations
    wave_audio = None
    if remote:
        # Real exported orders can contain only WAV. Exercise actual browser
        # decoding through the password proxy, rather than checking file bytes alone.
        data = host.production.read();take = data['jobs'][0]['takes'][0];take['mp3'] = ''
        host.production.write(data)
        output = io.BytesIO()
        with wave.open(output, 'wb') as sound:
            sound.setnchannels(1);sound.setsampwidth(2);sound.setframerate(8000)
            sound.writeframes(b'\x00\x00' * 8000)
        wave_audio = output.getvalue()
        (host.production.audio / identifier / take['id'] / 'voiceover.wav').write_bytes(wave_audio)
    raw = export_package(host, identifier)
    if remote:
        raw = package_with_manifest(raw, source_version='558a60fc8da609ff')
    package = tmp_path / 'local-edit.zip';package.write_bytes(raw)
    server = make_test_server(tmp_path / 'skill', tmp_path / 'runtime')
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=True)
            resources = ExitStack()
            try:
                context = browser.new_context()
                origin = resources.enter_context(import_site(context, server, tmp_path, remote))
                page = context.new_page()
                errors = [];page.on('pageerror', lambda error: errors.append(str(error)))
                uploads = []
                page.on('request', lambda request: uploads.append(request.url)
                        if request.method == 'POST' and request.url.endswith('/local-edit/import') else None)
                # The import page is reachable from the content page's menu, not only by typing its address.
                page.goto(origin + ('/auth/login?next=%2F%23content' if remote else '/#content'))
                if remote:
                    page.locator('#username').fill('fixture-admin')
                    page.locator('#password').fill('temporary-import-pass')
                    page.locator('#login-submit').click()
                    page.wait_for_url(origin + '/#content')
                    # A logged-in browser still needs the current page token.
                    denied = page.evaluate('''async () => (await fetch('/api/production/local-edit/import', {
                        method: 'POST', headers: {'Content-Type': 'application/zip'}, body: 'invalid'
                    })).status''')
                    assert denied == 403
                    assert server.app.production.read()['jobs'] == []
                page.locator('.flow-main-menu > summary').click()
                page.get_by_role('menuitem', name='导入剪辑包', exact=True).click()
                expect(page.get_by_role('heading', name='导入剪辑包', exact=True)).to_be_visible()
                expect(page.locator('[data-import-target]')).to_contain_text(urlsplit(origin).netloc)
                expect(page.get_by_role('link', name='本机剪辑入口')).to_have_count(0)
                page.locator('[data-local-package]').set_input_files(dict(name='invalid.zip', mimeType='application/zip', buffer=b'invalid'))
                page.locator('[data-import-local]').click()
                expect(page.locator('[data-error]')).to_contain_text('剪辑包无效')
                expect(page.locator('[data-import-local]')).to_be_enabled()
                expect(page.locator('[data-local-package]')).to_be_enabled()
                assert server.app.production.read()['jobs'] == []
                # Background refreshes must keep both the file and failure reason.
                page.evaluate('() => window.CaseProduction.route(true)')
                expect(page.locator('[data-error]')).to_contain_text('剪辑包无效')
                assert page.locator('[data-local-package]').evaluate('input => input.files[0].name') == 'invalid.zip'
                page.locator('[data-local-package]').set_input_files(str(package))
                page.evaluate('() => window.CaseProduction.route(true)')
                assert page.locator('[data-local-package]').evaluate('input => input.files[0].name') == 'local-edit.zip'
                page.locator('[data-import-local]').click()
                expect(page.locator('[data-production-title]')).to_have_text('Test')
                expect(page.get_by_role('heading', name='导入的剪辑制作单')).to_be_visible()
                with page.expect_download() as download:
                    page.locator('[data-local-download]').click()
                assert download.value.suggested_filename.endswith('-result.json')
                assert len(server.app.production.read()['jobs']) == 1
                imported = server.app.production.read()['jobs'][0]
                assert server.app.production_audio(imported['id'], imported['selected_take'], 'wav').read_bytes() == (wave_audio or b'audio-wav')
                if remote:
                    assert imported['local_edit_package']['source_version'] == '558a60fc8da609ff'
                    audio = page.locator('[data-take-id] audio')
                    expect(audio).to_have_attribute('src', f"/api/production/audio/{imported['id']}/{imported['selected_take']}/wav")
                    expect(page.locator('.take-links a[href*="/mp3"]')).to_have_count(0)
                    expect(page.locator('.take-links a[href*="/wav"]')).to_have_count(1)
                    audio.evaluate('async audio => {audio.muted = true;await audio.play();}')
                    page.wait_for_function('''() => {
                        const audio = document.querySelector('[data-take-id] audio');
                        return audio && audio.readyState >= 2 && !audio.error && audio.currentTime > 0 && audio.duration >= 1;
                    }''')
                    audio.evaluate('audio => audio.pause()')
                else:
                    expect(page.locator('[data-take-id] audio')).to_have_attribute('src', f"/api/production/audio/{imported['id']}/{imported['selected_take']}/mp3")
                receipt = tmp_path / 'result.json';download.value.save_as(receipt)
                # Choosing the same ZIP again opens the checked order without sending its media.
                upload_count = len(uploads)
                page.goto(origin + '/#production/import')
                page.locator('[data-local-package]').set_input_files(str(package))
                page.locator('[data-import-local]').click()
                expect(page.locator('[data-production-title]')).to_have_text('Test')
                assert len(uploads) == upload_count
                assert len(server.app.production.read()['jobs']) == 1
                # The same server plays the shared side: the original order receives the result.
                data = server.app.production.read();data['jobs'].append(host.production.job(identifier))
                server.app.production.write(data)
                page.goto(origin + f'/#production/{identifier}')
                expect(page.locator('[data-production-title]')).to_have_text('Test')
                # Most orders are cut on this computer: no card, only menu entries.
                expect(page.get_by_role('heading', name='换一台电脑剪辑')).to_have_count(0)
                page.locator('.production-more-actions > summary').click()
                expect(page.locator('[data-local-download]')).to_be_visible()
                page.locator('[data-local-result]').set_input_files(str(receipt))
                expect(page.locator('.local-edit-receipt')).to_contain_text('尚未建立时间线')
                assert server.app.production.job(identifier)['local_edit_results']
                if remote:
                    with page.expect_response(lambda response: '/local-edit/lookup' in response.url):
                        page.goto(origin + '/#production/import')
                    context.clear_cookies()
                    page.locator('[data-local-package]').set_input_files(dict(name='invalid.zip', mimeType='application/zip', buffer=b'invalid'))
                    page.locator('[data-import-local]').click()
                    page.wait_for_url(origin + '/auth/login?next=**')
                    assert len(server.app.production.read()['jobs']) == 2
                assert errors == []
            finally:
                resources.close()
                browser.close()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)


def test_upload_progress_survives_refresh_and_renews_an_expired_token(stations, tmp_path, monkeypatch):
    import edit_transfer
    host, _, identifier = stations
    package = tmp_path / 'local-edit.zip';package.write_bytes(export_package(host, identifier))
    server = make_test_server(tmp_path / 'skill', tmp_path / 'runtime')
    release = threading.Event()
    original = edit_transfer.import_package

    def delayed(app, raw):
        assert release.wait(timeout=10), 'UI never released the isolated import'
        return original(app, raw)

    monkeypatch.setattr(edit_transfer, 'import_package', delayed)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True);thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=True)
            try:
                page = browser.new_page()
                uploads = []
                page.on('request', lambda request: uploads.append(request.url)
                        if request.method == 'POST' and request.url.endswith('/local-edit/import') else None)
                with page.expect_response(lambda response: '/local-edit/lookup' in response.url):
                    page.goto(f'http://127.0.0.1:{server.server_port}/#production/import')
                page.wait_for_function('Boolean(state.token)')
                server.app.token = 'rotated-isolated-page-token'
                page.locator('[data-local-package]').set_input_files(str(package))
                page.locator('[data-import-local]').click()
                expect(page.locator('[data-import-progress]')).to_contain_text('正在校验并保存')
                expect(page.locator('[data-import-local]')).to_be_disabled()
                expect(page.locator('[data-local-package]')).to_be_disabled()
                page.evaluate('() => window.CaseProduction.route(true)')
                expect(page.locator('[data-import-progress]')).to_contain_text('正在校验并保存')
                page.screenshot(path=str(tmp_path / 'upload-progress.png'), full_page=True)
                release.set()
                expect(page.locator('[data-production-title]')).to_have_text('Test')
                assert len(uploads) == 2
                assert len(server.app.production.read()['jobs']) == 1
            finally:
                release.set();browser.close()
    finally:
        release.set();server.shutdown();server.server_close();thread.join(timeout=5)
