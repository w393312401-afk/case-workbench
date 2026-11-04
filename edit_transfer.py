"""Move frozen production inputs to another workstation without sharing its filesystem.

Packages contain selected voice audio, script data and the editing skill's text rules, never
Resolve projects, credentials, source footage or code. Returned receipts stay separate from host timelines.
Both computers must run the same application version; the rules travel with each order.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import platform
import re
import shutil
import zipfile
from uuid import uuid4

from direction_store import now
from elevenlabs_client import VoiceError
from production_store import JOB_ID, TAKE_ID

MAX_PACKAGE = 80 * 1024 * 1024
MAX_MANIFEST = 2 * 1024 * 1024
MAX_SKILL = 2 * 1024 * 1024
MAX_SKILL_FILES = 200
SKILL_SUFFIXES = {'.md', '.txt', '.json', '.yaml', '.yml'}
SOURCE_SUFFIXES = {'.py', '.js', '.css', '.html', '.svg', '.ico', '.png', '.woff', '.woff2', '.md'}


def _source_files(root):
    files = [*root.glob('*.py'), *root.glob('*.pyw'), root / 'requirements.txt']
    for directory in ('static', 'scripts', 'sources'):
        files.extend(path for path in (root / directory).rglob('*') if path.is_file()
                     and path.suffix.lower() in SOURCE_SUFFIXES)
    return [path for path in files if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root)
            and '__pycache__' not in path.parts]


def _content_hash(content):
    # Git may check files out with different line endings on each computer.
    return hashlib.sha256(content.replace(b'\r\n', b'\n')).hexdigest()


def source_version():
    """Fingerprint of the application files this computer runs; the same commit gives the same value anywhere."""
    root = Path(__file__).resolve().parent
    rows = sorted((path.relative_to(root).as_posix(), _content_hash(path.read_bytes())) for path in _source_files(root))
    return digest(rows)[:16]


def _skill_files(folder):
    """The editing skill's text rules keyed by package path; scripts and the agents folder never travel."""
    files = {}
    for path in sorted(folder.rglob('*')):
        relative = path.relative_to(folder)
        if (path.is_file() and not path.is_symlink() and relative.parts[0] != 'agents'
                and '__pycache__' not in relative.parts and path.suffix.lower() in SKILL_SUFFIXES):
            files['skill/' + relative.as_posix()] = path.read_bytes()
    if len(files) > MAX_SKILL_FILES or sum(len(content) for content in files.values()) > MAX_SKILL:
        raise VoiceError(413, '剪辑技能过大，无法随剪辑包转移。')
    return files


def skill_version(files):
    return digest(sorted((name, _content_hash(content)) for name, content in files.items()))[:16]


def _skill_entry(name):
    parts = name.split('/')
    return (len(parts) > 1 and parts[0] == 'skill' and not re.search(r'[\\:\x00]', name)
            and all(part not in ('', '.', '..') for part in parts) and Path(name).suffix.lower() in SKILL_SUFFIXES)


def _store_skill(app, identifier, files):
    """Replace this order's rules snapshot; edits already started keep the copy they were launched with."""
    base = app.production.root / 'edit-skill'
    folder, staging = base / identifier, base / f'.{identifier}.new'
    shutil.rmtree(staging, ignore_errors=True)
    for name, content in files.items():
        target = staging / name.removeprefix('skill/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    shutil.rmtree(folder, ignore_errors=True)
    staging.rename(folder)


def client_package():
    """Ship this version's public application files with fresh, relative settings."""
    root = Path(__file__).resolve().parent
    files = _source_files(root)
    if sum(path.stat().st_size for path in files) > MAX_PACKAGE:
        raise VoiceError(413, '程序包超过 80 MB。')
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(root).as_posix())
        archive.writestr('config.json', json.dumps({'skill_root': '.local/case-library',
            'runtime_root': '.local/runtime', 'port': 8767}, indent=2))
        archive.writestr('本机剪辑安装说明.txt',
            '在剪辑电脑解压到独立文件夹，安装 Python 3.12。\n'
            '在该文件夹打开 PowerShell，依次执行：\n'
            'py -3.12 -m venv .venv\n'
            '.\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt\n'
            '.\\.venv\\Scripts\\python.exe server.py\n\n'
            '浏览器打开 http://127.0.0.1:8767/#production/import 并导入剪辑包。\n'
            '保持服务窗口运行。素材先导入这台电脑的达芬奇项目，再在制作设置填写创作者和项目。\n'
            'AI 剪辑还需要本机 AI 客户端登录；程序包不包含账号。\n'
            '达芬奇需要支持脚本连接，并配置 ResolveMCP.exe；其他位置可设置 RESOLVE_MCP。\n'
            '完成后下载本机剪辑结果，回到共享工作台原制作单上传。\n'
            '更新：本机程序要和共享工作台同一版本，版本不一致时剪辑包会被拒绝并提示。\n'
            '已安装 Git 的电脑建议改用 git clone 获取程序，之后用 git pull 更新，更新后重启服务；\n'
            '没有 Git 时重新下载本程序包，解压覆盖程序文件（保留 .local 和 config.json）。\n'
            '剪辑规则随每个剪辑包送达，不需要单独安装剪辑技能。\n'
            '程序包没有共享工作台的业务数据、凭据和本机配置。\n')
    return output.getvalue()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def frozen(job):
    return {key: copy.deepcopy(job.get(key)) for key in ('lines', 'selected_take', 'product', 'replica')}


def _cut_settled(job):
    # Only a receipt reports cuts, so only a cut still in progress makes it incomplete.
    if job.get('timeline_building') or any(e.get('status') == 'running' for e in job.get('edits', [])):
        raise VoiceError(409, '这张制作单还在剪辑或生成时间线，请完成后再回传结果。')


def export_package(app, identifier):
    # Running tasks never block this: the package snapshots frozen inputs under the lock, and a
    # later change of script or selected take is caught by the fingerprint when the result returns.
    with app.lock:
        job = app.production_job(identifier)['job']
        take = next((t for t in job.get('takes', []) if t['id'] == job.get('selected_take')), None)
        if not take:
            raise VoiceError(409, '请先生成并选用配音，再转到本机剪辑。')
        if not TAKE_ID.fullmatch(take['id']):
            raise VoiceError(400, '配音编号无效。')
        source = frozen(job)
        origin = {'job_id': identifier, 'fingerprint': digest(source), 'inputs': source}
        # Deliberately omit every existing edit, timeline, publication and machine path.
        keys = ('title', 'product', 'direction_id', 'script_id', 'script_version', 'lines', 'supplement', 'fill_markdown',
                'variant_label', 'replica', 'created_at')
        clean = {key: copy.deepcopy(job[key]) for key in keys if key in job}
        clean.update(id=identifier, takes=[copy.deepcopy(take)], selected_take=take['id'])
        for key in ('mp3', 'wav'):
            clean['takes'][0][key] = f"{identifier}/{take['id']}/voiceover.{key}" if take.get(key) else ''
        skill_dir = getattr(app, 'edit_skill_dir', None)
        skill = _skill_files(Path(skill_dir)) if skill_dir and (Path(skill_dir) / 'SKILL.md').is_file() else {}
        manifest = {'schema': 1, 'kind': 'workbench-local-edit', 'origin': origin, 'job': clean,
                    'source_version': source_version(), 'skill_version': skill_version(skill) if skill else ''}
        references = {}
        if job.get('replica'):
            folder = app.replica_frames(job)
            references = {f"reference/shot-{shot['number']:02d}.jpg": folder / f"shot-{shot['number']:02d}.jpg"
                          for shot in job['replica']['shots']}
        if sum(p.stat().st_size for p in references.values()) > MAX_PACKAGE - MAX_MANIFEST:
            raise VoiceError(413, '原片参考图过大，剪辑包上限为 80 MB。')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
            for kind in ('mp3', 'wav'):
                if not clean['takes'][0].get(kind):
                    continue
                audio = app.production_audio(identifier, take['id'], kind)
                if audio.stat().st_size + output.tell() > MAX_PACKAGE:
                    raise VoiceError(413, '配音包超过 80 MB。')
                archive.write(audio, f'voiceover.{kind}')
            for name, file in references.items():
                archive.write(file, name)
            for name, content in skill.items():
                archive.writestr(name, content)
        if output.tell() > MAX_PACKAGE:
            raise VoiceError(413, '配音包超过 80 MB。')
        return output.getvalue()


def import_package(app, raw):
    if not raw or len(raw) > MAX_PACKAGE:
        raise VoiceError(413, '剪辑包为空或超过 80 MB。')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)) or any(name not in {'manifest.json', 'voiceover.mp3', 'voiceover.wav'}
                    and not re.fullmatch(r'reference/shot-[0-9]{2,3}\.jpg', name) and not _skill_entry(name) for name in names):
                raise ValueError('剪辑包包含未知或重复文件')
            if sum(e.file_size for e in entries) > MAX_PACKAGE or archive.getinfo('manifest.json').file_size > MAX_MANIFEST:
                raise ValueError('剪辑包展开后过大')
            manifest = json.loads(archive.read('manifest.json'))
            if manifest.get('schema') != 1 or manifest.get('kind') != 'workbench-local-edit':
                raise ValueError('不是工作台剪辑包')
            hosted, here = manifest.get('source_version'), source_version()
            if hosted != here:
                raise VoiceError(409, ('这个剪辑包没有程序版本信息，请更新共享工作台后重新下载剪辑包。' if not hosted else
                    f'本机程序版本（{here}）和共享工作台（{hosted}）不一致，剪辑规则可能对不上。'
                    '请先在共享工作台的电脑提交并推送最新代码，再在本机执行 git pull、重启工作台，然后重新导入。'))
            job, origin = manifest['job'], manifest['origin']
            if not JOB_ID.fullmatch(origin['job_id']) or digest(origin['inputs']) != origin['fingerprint']:
                raise ValueError('来源信息无效')
            if frozen(job) != origin['inputs'] or not isinstance(job['lines'], list) or not job['lines']:
                raise ValueError('口播或配音信息不一致')
            if len(job['takes']) != 1 or not TAKE_ID.fullmatch(job['takes'][0]['id']) or job['selected_take'] != job['takes'][0]['id']:
                raise ValueError('配音信息无效')
            audio = {kind: archive.read(f'voiceover.{kind}') for kind in ('mp3', 'wav') if f'voiceover.{kind}' in names}
            if not audio or any(bool(job['takes'][0].get(kind)) != (kind in audio) for kind in ('mp3', 'wav')):
                raise ValueError('配音文件缺失')
            skill = {name: archive.read(name) for name in names if name.startswith('skill/')}
            if bool(skill) != bool(manifest.get('skill_version')) or (skill and (
                    'skill/SKILL.md' not in skill or len(skill) > MAX_SKILL_FILES or skill_version(skill) != manifest['skill_version'])):
                raise ValueError('剪辑规则缺失或与版本不符')
            references = {name: archive.read(name) for name in names if name.startswith('reference/')}
            if job.get('replica'):
                shots = job['replica']['shots']
                if not isinstance(shots, list) or not 0 < len(shots) <= 500 or any(
                        not isinstance(s, dict) or type(s.get('number')) is not int or not 1 <= s['number'] <= 500 for s in shots):
                    raise ValueError('原片镜头信息无效')
                if set(references) != {f"reference/shot-{s['number']:02d}.jpg" for s in shots}:
                    raise ValueError('原片参考图缺失')
            elif references:
                raise ValueError('未知参考图')
    except VoiceError:
        raise
    except (ValueError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError) as exc:
        raise VoiceError(400, '剪辑包无效，请重新从共享工作台下载。') from exc
    with app.lock, app.production.catalog_transaction():
        data = app.production.read()
        prior = next((row for row in data['jobs'] if row.get('local_edit_origin') == origin), None)
        if prior:
            # Downloading the package again is how an order picks up newer editing rules.
            if skill and prior.get('edit_skill_version') != manifest['skill_version']:
                _store_skill(app, prior['id'], skill)
                prior['edit_skill_version'] = manifest['skill_version']
                app.production.write(data)
            return {'job_id': prior['id'], 'reused': True}
        # A new local order cannot accidentally reuse an origin machine's Resolve UID.
        identifier = 'PRD-' + uuid4().hex[:12]
        allowed = ('title', 'product', 'direction_id', 'script_id', 'script_version', 'lines', 'supplement', 'fill_markdown',
                   'variant_label', 'replica', 'created_at', 'takes', 'selected_take')
        local = {key: copy.deepcopy(job[key]) for key in allowed if key in job}
        local.setdefault('script_version', 1)
        local.setdefault('problems', [])
        local.setdefault('source', {})
        local.update(id=identifier, status='voiced', archived=False, timelines=[], edits=[], history=[],
                     updated_at=now(), local_edit_origin=origin)
        take = local['takes'][0]
        folder = app.production.audio / identifier / take['id']
        folder.mkdir(parents=True, exist_ok=False)
        reference_folder = app.production.root / 'replica' / identifier
        try:
            if skill:
                _store_skill(app, identifier, skill)
                local['edit_skill_version'] = manifest['skill_version']
            for kind, content in audio.items():
                (folder / f'voiceover.{kind}').write_bytes(content)
            for kind in ('mp3', 'wav'):
                take[kind] = f"{identifier}/{take['id']}/voiceover.{kind}" if kind in audio else ''
            if references:
                reference_folder.mkdir(parents=True, exist_ok=False)
                for name, content in references.items():
                    (reference_folder / name.split('/')[1]).write_bytes(content)
            data['jobs'].append(local)
            app.production.write(data)
        except Exception:
            shutil.rmtree(app.production.root / 'edit-skill' / identifier, ignore_errors=True)
            for path in folder.iterdir():
                path.unlink()
            folder.rmdir()
            if reference_folder.exists():
                for path in reference_folder.iterdir():
                    path.unlink()
                reference_folder.rmdir()
            raise
        return {'job_id': identifier, 'reused': False}


def export_receipt(app, identifier):
    with app.lock:
        job = app.production_job(identifier)['job']
        _cut_settled(job)
        origin = job.get('local_edit_origin')
        if not origin:
            raise VoiceError(409, '只有从共享工作台领取的制作单才能回传结果。')
        # A receipt is a human-readable report, never instructions to open host files.
        timelines = [{key: row.get(key) for key in ('name', 'project', 'creator', 'unique_id', 'built_at')}
                     for row in job.get('timelines', [])]
        edits = [{key: row.get(key) for key in ('id', 'status', 'finished_at', 'timeline_uid')}
                 for row in job.get('edits', [])]
        return {'schema': 1, 'kind': 'workbench-local-edit-result', 'origin': origin,
                'local_job_id': identifier, 'computer': platform.node(), 'exported_at': now(), 'source_version': source_version(),
                'status': job.get('status'), 'timelines': timelines, 'edits': edits}


def import_receipt(app, identifier, receipt):
    if not isinstance(receipt, dict) or receipt.get('schema') != 1 or receipt.get('kind') != 'workbench-local-edit-result':
        raise VoiceError(400, '请选择本机剪辑结果文件。')
    if len(json.dumps(receipt)) > MAX_MANIFEST:
        raise VoiceError(413, '剪辑结果文件过大。')
    origin = receipt.get('origin') or {}
    if origin.get('job_id') != identifier:
        raise VoiceError(409, '结果属于另一张制作单。')
    # Only bounded report fields are retained; the receiver never consumes arbitrary paths.
    clean = {key: copy.deepcopy(receipt.get(key)) for key in
             ('schema', 'kind', 'local_job_id', 'computer', 'exported_at', 'status', 'timelines', 'edits')}
    clean['source_version'] = str(receipt.get('source_version') or '')[:64]
    if any(not isinstance(clean.get(key), str) or len(clean[key]) > 200 for key in
           ('local_job_id', 'computer', 'exported_at', 'status')) or not JOB_ID.fullmatch(clean['local_job_id']):
        raise VoiceError(400, '剪辑结果字段无效。')
    for key, fields in (('timelines', ('name', 'project', 'creator', 'unique_id', 'built_at')),
                        ('edits', ('id', 'status', 'finished_at', 'timeline_uid'))):
        rows = clean[key]
        if not isinstance(rows, list) or len(rows) > 500 or any(not isinstance(row, dict) for row in rows):
            raise VoiceError(400, '剪辑结果列表无效。')
        clean[key] = [{field: str(row.get(field) or '')[:300] for field in fields} for row in rows]
    with app.lock, app.production.catalog_transaction():
        data = app.production.read()
        job = app.production._find_job(data, identifier)
        if digest(frozen(job)) != origin.get('fingerprint'):
            raise VoiceError(409, '共享制作单的口播或选用配音已变化，请重新领取，当前结果未覆盖。')
        receipts = job.setdefault('local_edit_results', [])
        key = digest(clean)
        if not any(row.get('receipt_id') == key for row in receipts):
            receipts.append({**clean, 'receipt_id': key, 'received_at': now(), 'fingerprint': origin['fingerprint']})
            job['updated_at'] = now()
            app.production.write(data)
        return {'ok': True, 'receipt_id': key}


def current_remote_cut(job):
    """The newest receipt for the job's current inputs that reports a timeline, if any."""
    fingerprint = digest(frozen(job))
    rows = [row for row in job.get('local_edit_results') or []
            if row.get('fingerprint') == fingerprint and row.get('timelines')]
    return max(rows, key=lambda row: str(row.get('received_at') or ''), default=None)
