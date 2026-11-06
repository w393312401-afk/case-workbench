"""Preview ownership, media validation and worker cleanup use isolated projects/files."""
import copy
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from uuid import uuid4

import pytest

from elevenlabs_client import VoiceError
from test_edit_transfer import stations
from test_server import make_test_server
from workbench_previews import PreviewExports
import resolve_worker as worker


@pytest.fixture
def preview_media(tmp_path):
    executable = shutil.which('ffmpeg')
    if not executable:
        pytest.skip('FFmpeg is required for playable MP4 fixtures')
    path = tmp_path / 'fixture.mp4'
    subprocess.run([executable, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=90x160:r=30:d=1',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=1', '-c:v', 'libx264',
        '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', '-movflags', '+faststart', str(path)], check=True, timeout=20)
    return path


class PreviewResolve:
    def __init__(self, media, release=None):
        self.media, self.release, self.calls = media, release, []
    def busy(self): return False
    def closed(self): return False
    def run(self, command, payload=None, **options):
        if command == 'status': return {'ok': True, 'project': 'Fixture', 'timelines': []}
        assert command == 'render_preview'
        self.calls.append(copy.deepcopy(payload))
        if self.release is not None:
            assert self.release.wait(10)
        target = Path(payload['output']);shutil.copyfile(self.media, target)
        return {'file': str(target), 'frames': 30, 'fps': 30, 'warnings': []}


def install_job(app, host, identifier):
    data = app.production.empty();job = host.production.job(identifier)
    job['timelines'] = [{'unique_id': 'timeline-one', 'name': 'Fixture-preview', 'project': 'Fixture',
        'project_id': 'project-one', 'take_id': job['selected_take'], 'saved': True, 'frames': 30, 'fps': '30',
        'width': 1080, 'height': 1920}]
    data['jobs'] = [job];app.production.write(data)
    return app.production.job(identifier)


def wait_complete(manager, identifier):
    deadline = time.monotonic()+10
    while time.monotonic() < deadline:
        result = manager.detail(identifier, 'timeline-one')
        if not manager.busy(): return result
        time.sleep(.02)
    raise AssertionError('Preview did not finish')


def test_preview_keeps_review_state_replays_request_and_binds_download(stations, preview_media):
    host, app, identifier = stations
    original = install_job(app, host, identifier)
    app.resolve = PreviewResolve(preview_media)
    manager = PreviewExports(app)
    body = {'request_id': uuid4().hex, 'timeline_uid': 'timeline-one'}
    assert manager.detail(identifier, 'timeline-one')['preview'] is None
    manager.start(identifier, body)
    result = wait_complete(manager, identifier)
    assert result['task']['status'] == 'completed', result
    preview = result['preview']
    assert preview['source_current'] and preview['width'] == 90 and preview['height'] == 160
    assert manager.video(identifier, preview['id']).read_bytes() == preview_media.read_bytes()
    manager.start(identifier, body)
    assert len(app.resolve.calls) == 1
    assert app.production.job(identifier) == original
    assert PreviewExports(app).video(identifier, preview['id']).read_bytes() == preview_media.read_bytes()
    with pytest.raises(VoiceError): manager.video('PRD-abcdef012345', preview['id'])
    data = app.production.read();data['jobs'][0]['timelines'][0]['hook_overlay'] = {'text': 'Changed'}
    app.production.write(data)
    assert manager.detail(identifier, 'timeline-one')['preview']['source_current'] is False


def test_unreadable_export_is_not_exposed(stations, tmp_path):
    host, app, identifier = stations;install_job(app, host, identifier)
    broken = tmp_path / 'broken.mp4';broken.write_bytes(b'not a video')
    app.resolve = PreviewResolve(broken);manager = PreviewExports(app)
    manager.start(identifier, {'request_id':uuid4().hex, 'timeline_uid':'timeline-one'})
    result = wait_complete(manager, identifier)
    assert result['task']['status'] == 'failed' and result['preview'] is None
    with pytest.raises(VoiceError):manager.video(identifier, result['task']['id'])


def test_running_preview_blocks_another_export_and_restart(stations, preview_media, tmp_path):
    host, _, identifier = stations
    server = make_test_server(tmp_path / 'skill', tmp_path / 'runtime')
    install_job(server.app, host, identifier)
    release = threading.Event();server.app.resolve = PreviewResolve(preview_media, release)
    manager = server.app.preview_exports
    try:
        manager.start(identifier, {'request_id': uuid4().hex, 'timeline_uid': 'timeline-one'})
        assert manager.busy(identifier)
        with pytest.raises(VoiceError, match='其他操作'):
            manager.start(identifier, {'request_id': uuid4().hex, 'timeline_uid': 'timeline-one'})
        assert '预览' in server._activity_blocker()
        with pytest.raises(Exception, match='预览'): server.app.ensure_data_idle()
        release.set()
        assert wait_complete(manager, identifier)['preview']
    finally:
        release.set();server.server_close()


def test_preview_http_requires_page_token_and_supports_range(stations, preview_media, tmp_path):
    host, _, identifier = stations
    server = make_test_server(tmp_path / 'skill', tmp_path / 'runtime')
    install_job(server.app, host, identifier);server.app.resolve = PreviewResolve(preview_media)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True);thread.start()
    def request(method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse();return response.status, dict(response.getheaders()), response.read()
        finally:connection.close()
    base = f'/api/production/jobs/{identifier}/preview'
    body = json.dumps({'request_id': uuid4().hex, 'timeline_uid': 'timeline-one'})
    try:
        assert request('POST', base, body, {'Content-Type':'application/json'})[0] == 403
        assert not server.app.preview_exports._records(identifier)
        assert request('POST', base, body, {'Content-Type':'application/json', 'X-Workbench-Token':server.app.token})[0] == 202
        preview = wait_complete(server.app.preview_exports, identifier)['preview']
        # Both versions of the workbench used the same status URL, with different response fields.
        status, _, shared = request('GET', base + '?timeline=timeline-one')
        assert status == 200
        shared = json.loads(shared)
        assert shared['preview']['id'] == preview['id'] and shared['task']['status'] == 'completed'
        assert shared['video'] is None and shared['status'] == 'idle'
        status, _, portable = request('GET', base + '/portable?timeline=timeline-one')
        assert status == 200 and json.loads(portable)['preview'] == shared['preview']
        status, headers, content = request('GET', preview['url'], headers={'Range':'bytes=0-31'})
        assert status == 206 and len(content) == 32
        assert headers['Content-Type'].startswith('video/mp4')
        status, headers, content = request('GET', preview['url']+'?download=1')
        assert status == 200 and 'attachment' in headers['Content-Disposition']
        assert content == preview_media.read_bytes()
    finally:server.shutdown();server.server_close();thread.join(timeout=5)


class RenderTimeline:
    def __init__(self, uid): self.uid, self.timecode = uid, '01:00:00:03'
    def GetUniqueId(self): return self.uid
    def GetName(self): return self.uid
    def GetCurrentTimecode(self): return self.timecode
    def SetCurrentTimecode(self, value): self.timecode=value;return True
    def GetSetting(self, key): return {'timelineResolutionWidth':'1080', 'timelineResolutionHeight':'1920', 'timelineFrameRate':'30'}[key]
    def GetStartFrame(self): return 100
    def GetEndFrame(self): return 130
    def GetTrackCount(self, kind): return 1


class RenderProject:
    def __init__(self, fail=False):
        self.before, self.target = RenderTimeline('before'), RenderTimeline('target')
        self.current=self.before;self.format={'format':'mov','codec':'ProRes'};self.mode=0
        self.settings={'TargetDir':'user-folder', 'CustomName':'user-name'}
        self.presets={};self.jobs={'user-job':{}};self.started=[];self.fail=fail
    def GetUniqueId(self): return 'project-one'
    def GetCurrentTimeline(self): return self.current
    def GetTimelineCount(self): return 2
    def GetTimelineByIndex(self,index): return [self.before,self.target][index-1]
    def SetCurrentTimeline(self,value): self.current=value;return True
    def IsRenderingInProgress(self): return False
    def GetCurrentRenderMode(self): return self.mode
    def SetCurrentRenderMode(self,value): self.mode=value;return True
    def GetCurrentRenderFormatAndCodec(self): return dict(self.format)
    def SetCurrentRenderFormatAndCodec(self,format,codec): self.format={'format':format,'codec':codec};return True
    def SaveAsNewRenderPreset(self,name): self.presets[name]=copy.deepcopy(self.settings);return True
    def LoadRenderPreset(self,name):
        target=self.settings.get('TargetDir')
        self.settings=copy.deepcopy(self.presets.get(name,{}))
        self.settings['TargetDir']=target
        return True
    def ExportRenderPreset(self,name,directory):
        root=ET.Element('SyRecordInfo');ET.SubElement(root,'RecordTargetDir').text=self.presets[name]['TargetDir']
        ET.ElementTree(root).write(Path(directory)/(name+'.xml'))
        return True
    def DeleteRenderPreset(self,name): del self.presets[name];return True
    def SetRenderSettings(self,settings):
        if 'ReplaceExistingFilesInPlace' in settings:return False
        self.settings.update(settings);return True
    def AddRenderJob(self): self.jobs['owned']=dict(self.settings);return 'owned'
    def StartRendering(self,ids,interactive):
        self.started=ids
        Path(self.settings['TargetDir'],self.settings['CustomName']+'.mp4').write_bytes(b'isolated-render')
        return True
    def GetRenderJobStatus(self,identifier): return {'JobStatus':'Failed' if self.fail else 'Complete', 'Error':'fixture failure'}
    def DeleteRenderJob(self,identifier): del self.jobs[identifier];return True


@pytest.mark.parametrize('fail', [False, True])
def test_worker_restores_settings_timeline_page_and_other_jobs(tmp_path, fail):
    project = RenderProject(fail);before = copy.deepcopy(project.settings)
    resolve = SimpleNamespace(GetProjectManager=lambda:SimpleNamespace(GetCurrentProject=lambda:project), page='edit')
    resolve.GetCurrentPage=lambda:resolve.page
    resolve.OpenPage=lambda value:setattr(resolve,'page',value) or True
    resolve.ExportRenderPreset=project.ExportRenderPreset
    payload={'project':'Fixture','project_id':'project-one','unique_id':'target',
             'output':str(tmp_path/'preview.mp4'),'progress_file':str(tmp_path/'progress.json')}
    if fail:
        with pytest.raises(worker.WorkerError,match='fixture failure'):worker.render_preview(resolve,payload)
    else:
        result=worker.render_preview(resolve,payload)
        assert (result['width'],result['height'],result['frames']) == (720,1280,30)
    assert project.settings == before and project.mode == 0
    assert project.format == {'format':'mov','codec':'ProRes'}
    assert project.current is project.before and resolve.page == 'edit'
    assert project.jobs == {'user-job':{}} and project.started == ['owned'] and project.presets == {}
