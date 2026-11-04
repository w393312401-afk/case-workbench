"""Two isolated workstations exchange inputs and receipts, never timeline ownership."""
import io
import json
import threading
import zipfile

import pytest

from edit_transfer import export_package, import_package, export_receipt, import_receipt
from edit_transfer import find_imported_package
from elevenlabs_client import VoiceError
from production_store import ProductionStore
from workbench_production import ProductionWorkbenchMixin
from creation_store import CreationStore


class Bench(ProductionWorkbenchMixin):
    def __init__(self, root):
        self.lock = threading.RLock()
        self.production = ProductionStore(root, self.lock)
        self.creation = CreationStore(root, self.lock)


@pytest.fixture
def stations(tmp_path):
    host, local = Bench(tmp_path / 'host'), Bench(tmp_path / 'local')
    job = dict(id='PRD-0123456789ab', title='Test', product='202H', direction_id='', script_id='SCR-0123456789ab',
               script_version=1, lines=[dict(number=1, spoken='Test.', zh='', proof='Show', tags='', evidence='')],
               takes=[dict(id='TAK-0123456789ab', voice_id='VOICE123456789', mp3='old', wav='old',
                           groups=[dict(number=1, start=0., end=1., coverage=1.)], duration=1.)],
               selected_take='TAK-0123456789ab', timelines=[], edits=[], archived=False, history=[], problems=[])
    data = host.production.empty();data['jobs'] = [job];host.production.write(data)
    for kind in ('mp3', 'wav'):
        path = host.production.audio / job['id'] / job['selected_take'] / f'voiceover.{kind}'
        path.parent.mkdir(parents=True, exist_ok=True);path.write_bytes(b'audio-' + kind.encode())
    return host, local, job['id']


def test_import_can_read_full_job_and_generate_markers(stations):
    host, local, identifier = stations
    raw = export_package(host, identifier)
    result = import_package(local, raw)
    assert result['job_id'] != identifier
    assert import_package(local, raw) == dict(job_id=result['job_id'], reused=True)
    job = local.production_job(result['job_id'])['job']
    assert not job['timelines'] and not job['edits']
    assert job['script_version'] == 1
    assert local.production_audio(job['id'], job['selected_take'], 'wav').read_bytes() == b'audio-wav'
    from workbench_production import meaning_markers
    markers = meaning_markers(job, job['takes'][0], '30')
    assert job['id'] in markers[0]['custom']


def test_lookup_reuses_checked_inputs_only_when_rules_and_media_are_present(stations, tmp_path):
    host, local, identifier = stations
    host.edit_skill_dir = _skill(tmp_path / 'rules')
    raw = export_package(host, identifier)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
    origin, rules = manifest['origin'], manifest['skill_version']
    args = (origin['job_id'], origin['fingerprint'], rules)
    assert find_imported_package(local, *args)['job_id'] is None
    imported = import_package(local, raw)['job_id']
    assert find_imported_package(local, *args) == {'job_id': imported, 'reused': True}
    assert find_imported_package(local, origin['job_id'], '0'*64, rules)['job_id'] is None
    assert find_imported_package(local, origin['job_id'], origin['fingerprint'], '0'*16)['job_id'] is None
    assert find_imported_package(local, origin['job_id'], origin['fingerprint'])['job_id'] is None
    local.production_audio(imported, manifest['job']['selected_take'], 'wav').unlink()
    assert find_imported_package(local, *args)['job_id'] is None
    assert len(local.production.read()['jobs']) == 1


@pytest.mark.parametrize('identifier,fingerprint,rules', [('bad','0'*64,''), ('PRD-0123456789ab','bad',''),
                                                        ('PRD-0123456789ab','0'*64,'bad')])
def test_lookup_rejects_invalid_identifiers_without_creating_records(stations, identifier, fingerprint, rules):
    _, local, _ = stations
    with pytest.raises(VoiceError):
        find_imported_package(local, identifier, fingerprint, rules)
    assert not local.production.read()['jobs']


def test_result_preserves_host_timeline_ownership_and_is_idempotent(stations):
    host, local, identifier = stations
    imported = import_package(local, export_package(host, identifier))['job_id']
    data = local.production.read()
    data['jobs'][0]['timelines'] = [dict(unique_id='LOCAL-UID', name='Local cut', take_id='TAK-0123456789ab')]
    local.production.write(data)
    receipt = export_receipt(local, imported)
    import_receipt(host, identifier, receipt);import_receipt(host, identifier, receipt)
    job = host.production.job(identifier)
    assert job['timelines'] == [] and job['edits'] == []
    assert len(job['local_edit_results']) == 1
    assert job['local_edit_results'][0]['timelines'][0]['unique_id'] == 'LOCAL-UID'


class Feed:
    """Only the projection call settle_resolved_activity makes; keeps what it resolved."""
    def __init__(self, rows):
        self.rows, self.resolved = rows, None
    def existing(self):
        return self.rows
    def archive_owner(self, owner, archived):
        pass
    def project_attention(self, owner, resolved):
        self.resolved = resolved


def test_returned_remote_cut_stops_edit_nags_on_the_original_order(stations):
    from task_activity import settle_resolved_activity
    host, local, identifier = stations
    imported = import_package(local, export_package(host, identifier))['job_id']
    rows = [dict(run_id='RUN-edit', owner_id=identifier, kind='edit', source_id='EDT-1', status='failed', owner_archived=0),
            dict(run_id='RUN-voice', owner_id=identifier, kind='voice', source_id='TAK-other', status='failed', owner_archived=0)]
    host.activity = Feed(rows)
    # A receipt with no timeline yet does not answer the edit request.
    import_receipt(host, identifier, export_receipt(local, imported))
    settle_resolved_activity(host, 'job', host.production_job(identifier)['job'])
    assert 'RUN-edit' not in host.activity.resolved
    data = local.production.read()
    data['jobs'][0]['timelines'] = [dict(unique_id='LOCAL-UID', name='Local cut', take_id='TAK-0123456789ab')]
    local.production.write(data)
    import_receipt(host, identifier, export_receipt(local, imported))
    # The detail read and the catalog read must agree, or the nag would flap.
    for job in (host.production_job(identifier)['job'], host.production.public()['jobs'][0]):
        settle_resolved_activity(host, 'job', job)
        assert '另一台电脑' in host.activity.resolved['RUN-edit']
        assert 'RUN-voice' not in host.activity.resolved
    # Changing the spoken lines makes the remote cut stale; the nag comes back.
    data = host.production.read();data['jobs'][0]['lines'][0]['spoken'] = 'Changed.';host.production.write(data)
    settle_resolved_activity(host, 'job', host.production_job(identifier)['job'])
    assert 'RUN-edit' not in host.activity.resolved


def test_conflicting_script_rejected(stations):
    host, local, identifier = stations
    imported = import_package(local, export_package(host, identifier))['job_id']
    receipt = export_receipt(local, imported)
    data = host.production.read();data['jobs'][0]['lines'][0]['spoken'] = 'Changed.';host.production.write(data)
    with pytest.raises(VoiceError, match='已变化'):
        import_receipt(host, identifier, receipt)
    assert not host.production.job(identifier).get('local_edit_results')


@pytest.mark.parametrize('filename', ['../escape', 'account.json', 'voiceover.mp3'])
def test_rejects_path_traversal_unknown_and_duplicate_entries(stations, filename):
    host, local, identifier = stations
    raw = io.BytesIO(export_package(host, identifier))
    with zipfile.ZipFile(raw, 'a') as archive:
        archive.writestr(filename, 'bad')
    with pytest.raises(VoiceError):
        import_package(local, raw.getvalue())
    assert not local.production.read()['jobs']


def test_missing_audio_and_bad_manifest_rejected(stations):
    _, local, _ = stations
    for value in ({}, {'schema': 1, 'kind': 'workbench-local-edit', 'job': {}}):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            archive.writestr('manifest.json', json.dumps(value))
        with pytest.raises(VoiceError):
            import_package(local, raw.getvalue())


def test_imported_job_builds_timeline_using_local_audio(stations):
    from uuid import uuid4
    from resolve_bridge import ResolveBridge
    from test_production_resolve import FakeRunner
    host, local, identifier = stations
    imported = import_package(local, export_package(host, identifier))['job_id']
    runner = FakeRunner();local.resolve = ResolveBridge(runner=runner)
    local.production.mutate('settings', {'request_id': uuid4().hex,
        'revision': local.production.public()['revision'], 'settings': {'creators': ['Lab']}})
    result = local.build_timeline(imported, {'request_id': uuid4().hex, 'creator': 'Lab'})
    assert result['timeline']['take_id'] == 'TAK-0123456789ab'
    assert str(local.production.audio) in runner.calls[-1]['audio']
    assert imported in runner.calls[-1]['markers'][0]['custom']


def test_replica_reference_frames_travel_with_the_job(stations, tmp_path):
    host, local, identifier = stations
    data = host.production.read()
    data['jobs'][0]['replica'] = {'case_id': 'PVR-0123456789ab', 'shots': [{'number': 1}]}
    host.production.write(data)
    frames = tmp_path / 'frames';frames.mkdir();(frames / 'shot-01.jpg').write_bytes(b'reference-frame')
    host.replica_frames = lambda job: frames
    imported = import_package(local, export_package(host, identifier))['job_id']
    assert (local.production.root / 'replica' / imported / 'shot-01.jpg').read_bytes() == b'reference-frame'


def test_client_contains_current_source_and_fresh_settings_only(tmp_path):
    from edit_transfer import client_package
    with zipfile.ZipFile(io.BytesIO(client_package())) as archive:
        names = set(archive.namelist())
        assert {'server.py', 'edit_transfer.py', 'static/production.js', 'requirements.txt'} <= names
        assert not any(name.startswith(('工作台数据/', 'archive/', '.local/', 'deployment/')) for name in names)
        assert 'config.local.json' not in names
        assert json.loads(archive.read('config.json')) == {
            'skill_root': '.local/case-library', 'runtime_root': '.local/runtime', 'port': 8767}
        archive.extractall(tmp_path / 'client')
    import os
    import subprocess
    import sys
    env = dict(os.environ);env.pop('PYTHONPATH', None)
    result = subprocess.run([sys.executable, '-c',
        "import server; s=server.make_server(server.DEFAULT_CONFIG['skill_root'], server.DEFAULT_CONFIG['runtime_root'],port=0,allow_open=False); s.server_close()"],
        cwd=tmp_path / 'client', env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def _skill(folder, rules='规则一'):
    (folder / 'references').mkdir(parents=True, exist_ok=True);(folder / 'agents').mkdir(exist_ok=True)
    (folder / 'SKILL.md').write_text(f'---\nname: davinci-product-video-editing\n---\n{rules}', encoding='utf-8')
    (folder / 'references' / 'rules.md').write_text('素材识别', encoding='utf-8')
    (folder / 'agents' / 'openai.yaml').write_text('x: 1', encoding='utf-8')
    (folder / 'helper.py').write_text('print(1)', encoding='utf-8')
    return folder


def test_editing_rules_travel_with_the_package_and_refresh_on_redownload(stations, tmp_path):
    from edit_transfer import skill_version
    from workbench_editing import EditingWorkbenchMixin
    host, local, identifier = stations
    host.edit_skill_dir = _skill(tmp_path / 'host-skill')
    local.edit_skill_dir = tmp_path / 'missing-local-skill'
    imported = import_package(local, export_package(host, identifier))['job_id']
    job = local.production_job(imported)['job']
    folder = EditingWorkbenchMixin._edit_skill_dir(local, job)
    assert folder == local.production.root / 'edit-skill' / imported
    assert (folder / 'references' / 'rules.md').read_text(encoding='utf-8') == '素材识别'
    assert not (folder / 'agents').exists() and not (folder / 'helper.py').exists()
    # The host's other orders and un-imported orders keep using the host's own skill.
    assert EditingWorkbenchMixin._edit_skill_dir(host, host.production_job(identifier)['job']) == host.edit_skill_dir
    # Changed rules: downloading the same order again swaps the snapshot, not the order.
    _skill(tmp_path / 'host-skill', rules='规则二')
    again = import_package(local, export_package(host, identifier))
    assert again == dict(job_id=imported, reused=True)
    assert '规则二' in (folder / 'SKILL.md').read_text(encoding='utf-8')
    assert local.production_job(imported)['job']['edit_skill_version'] == skill_version(
        {'skill/SKILL.md': (folder / 'SKILL.md').read_bytes(), 'skill/references/rules.md': b'\xe7\xb4\xa0\xe6\x9d\x90\xe8\xaf\x86\xe5\x88\xab'})


def test_order_without_packaged_rules_uses_the_local_skill(stations, tmp_path):
    from workbench_editing import EditingWorkbenchMixin
    host, local, identifier = stations
    local.edit_skill_dir = tmp_path / 'local-skill'
    imported = import_package(local, export_package(host, identifier))['job_id']
    assert EditingWorkbenchMixin._edit_skill_dir(local, local.production_job(imported)['job']) == local.edit_skill_dir


def package_with_manifest(raw, **changes):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(output, 'w') as target:
        for entry in source.infolist():
            content = source.read(entry.filename)
            if entry.filename == 'manifest.json':
                manifest = json.loads(content);manifest.update(changes)
                content = json.dumps(manifest).encode()
            target.writestr(entry.filename, content)
    return output.getvalue()


def test_supported_package_imports_across_program_updates_and_records_versions(stations, tmp_path, monkeypatch):
    import edit_transfer
    host, local, identifier = stations
    host.edit_skill_dir = _skill(tmp_path / 'rules')
    hosted = edit_transfer.source_version()
    raw = export_package(host, identifier)
    monkeypatch.setattr(edit_transfer, 'source_version', lambda: 'f00dbabe12345678')
    result = import_package(local, raw)
    job = local.production_job(result['job_id'])['job']
    assert job['local_edit_package'] == {'schema': 1, 'source_version': hosted, 'imported_with_version': 'f00dbabe12345678'}
    assert not job['timelines'] and not job['edits']
    assert local.production_audio(job['id'], job['selected_take'], 'wav').read_bytes() == b'audio-wav'
    assert '规则一' in (local.production.root / 'edit-skill' / job['id'] / 'SKILL.md').read_text(encoding='utf-8')
    monkeypatch.setattr(edit_transfer, 'source_version', lambda: 'f00dbabe87654321')
    assert import_package(local, raw) == {'job_id': job['id'], 'reused': True}
    assert len(local.production.read()['jobs']) == 1
    assert local.production_job(job['id'])['job']['local_edit_package']['imported_with_version'] == 'f00dbabe87654321'


@pytest.mark.parametrize('schema', [2, True, '1', None])
def test_unsupported_package_schema_is_rejected_without_creating_an_order(stations, schema):
    host, local, identifier = stations
    raw = package_with_manifest(export_package(host, identifier), schema=schema)
    with pytest.raises(VoiceError, match='格式版本') as caught:
        import_package(local, raw)
    assert caught.value.status == 409
    assert not local.production.read()['jobs']


@pytest.mark.parametrize('version', [None, '', 'invalid', ['0123456789abcdef']])
def test_missing_or_invalid_program_provenance_is_rejected(stations, version):
    host, local, identifier = stations
    raw = package_with_manifest(export_package(host, identifier), source_version=version)
    with pytest.raises(VoiceError):
        import_package(local, raw)
    assert not local.production.read()['jobs']


def test_version_ignores_line_endings_but_not_content(tmp_path, monkeypatch):
    import edit_transfer
    monkeypatch.setattr(edit_transfer, '__file__', str(tmp_path / 'edit_transfer.py'))
    (tmp_path / 'requirements.txt').write_text('pillow', encoding='utf-8')
    path = tmp_path / 'a.py'
    path.write_bytes(b'x = 1\nprint(x)\n');unix = edit_transfer.source_version()
    path.write_bytes(b'x = 1\r\nprint(x)\r\n');assert edit_transfer.source_version() == unix
    path.write_bytes(b'x = 2\nprint(x)\n');assert edit_transfer.source_version() != unix


def test_tampered_or_unsafe_rules_are_rejected(stations, tmp_path, monkeypatch):
    import edit_transfer
    host, local, identifier = stations
    host.edit_skill_dir = _skill(tmp_path / 'host-skill')
    raw = export_package(host, identifier)
    monkeypatch.setattr(edit_transfer, 'source_version', lambda: 'f00dbabe12345678')
    for name, content in (('skill/SKILL.md', 'changed'), ('skill/../escape.md', 'x'), ('skill/run.py', 'x'), ('skill/C:evil.md', 'x')):
        tampered = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(tampered, 'w') as target:
            for entry in source.infolist():
                if entry.filename != name:
                    target.writestr(entry.filename, source.read(entry.filename))
            target.writestr(name, content)
        with pytest.raises(VoiceError):
            import_package(local, tampered.getvalue())
    assert not local.production.read()['jobs'] and not (local.production.root / 'edit-skill').exists()


def test_receipt_records_the_version_that_made_it(stations):
    from edit_transfer import source_version
    host, local, identifier = stations
    imported = import_package(local, export_package(host, identifier))['job_id']
    import_receipt(host, identifier, export_receipt(local, imported))
    assert host.production.job(identifier)['local_edit_results'][0]['source_version'] == source_version()
