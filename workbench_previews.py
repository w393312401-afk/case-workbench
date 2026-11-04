"""Private, per-timeline MP4 previews; independent of review and publication records."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
from uuid import UUID, uuid4

from direction_store import now
from elevenlabs_client import VoiceError
from production_operations import durable_json
from production_store import JOB_ID, active_timelines, timeline_running

PREVIEW_ID = re.compile(r'PVW-[0-9a-f]{12}')


def source_key(job, timeline):
    edits = [row for row in job.get('edits', []) if row.get('timeline_uid') == timeline['unique_id']]
    latest = edits[-1] if edits else {}
    value = {'timeline': {key: timeline.get(key) for key in ('unique_id', 'project_id', 'take_id', 'frames', 'fps',
        'width', 'height', 'hook_overlay', 'subtitle_check')}, 'edit': {
        key: latest.get(key) for key in ('id', 'finished_at')}}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def inspect_video(path):
    executable = shutil.which('ffprobe')
    if not executable:
        raise VoiceError(503, '需要安装 FFmpeg，才能核对预览 MP4。')
    try:
        result = subprocess.run([executable, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)],
                                capture_output=True, timeout=30, check=True)
        info = json.loads(result.stdout)
        video = next(row for row in info['streams'] if row.get('codec_type') == 'video')
        audio = next(row for row in info['streams'] if row.get('codec_type') == 'audio')
        duration = float(info['format']['duration'])
        if video.get('codec_name') != 'h264' or audio.get('codec_name') != 'aac' or duration <= 0:
            raise ValueError('unsupported preview')
        return {'duration': duration, 'width': video['width'], 'height': video['height']}
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as error:
        raise VoiceError(409, '导出文件未通过视频和声音读取检查，请重新生成预览。') from error


class PreviewExports:
    def __init__(self, app):
        self.app = app
        self.root = app.production.root / 'previews'
        self.lock = threading.RLock()
        self.running = {}

    def busy(self, job_id=None):
        with self.lock:
            return any(job_id is None or owner == job_id for owner in self.running.values())

    def _folder(self, job_id, identifier):
        if not JOB_ID.fullmatch(str(job_id)) or not PREVIEW_ID.fullmatch(str(identifier)):
            raise VoiceError(404, '预览文件不存在。')
        path = self.root / job_id / identifier
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise VoiceError(409, '预览文件位置无效。')
        return path

    def _records(self, job_id):
        if not JOB_ID.fullmatch(str(job_id)):
            raise VoiceError(404, '制作单不存在。')
        rows = []
        for path in (self.root / job_id).glob('PVW-*/record.json'):
            try:
                if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
                    continue
                row = json.loads(path.read_text(encoding='utf-8'))
                if row['job_id'] == job_id and row['id'] == path.parent.name and PREVIEW_ID.fullmatch(row['id']):
                    rows.append(row)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(rows, key=lambda row: row.get('created_at', ''))

    def _save(self, row, **patch):
        row.update(patch, updated_at=now())
        durable_json(self._folder(row['job_id'], row['id']) / 'record.json', row)

    def _target(self, job_id, uid):
        job = self.app.production.job(job_id)
        timeline = next((row for row in active_timelines(job) if row.get('unique_id') == uid), None)
        if not timeline:
            raise VoiceError(404, '请选择这张制作单的有效时间线。')
        return job, timeline

    def _file(self, row):
        name = row.get('filename', '')
        if not name or '/' in name or '\\' in name or ':' in name or not name.endswith('.mp4'):
            raise VoiceError(409, '预览文件记录无效。')
        path = self._folder(row['job_id'], row['id']) / name
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise VoiceError(409, '预览文件位置已变化。')
        return path

    def _public(self, row):
        if not row:
            return None
        result = {key: row.get(key) for key in ('id', 'request_id', 'status', 'message', 'error', 'percent', 'created_at',
            'finished_at', 'filename', 'size', 'duration', 'width', 'height', 'warning')}
        if row['status'] in {'queued', 'running'}:
            if row['id'] not in self.running:
                self._save(row, status='failed', error='上次预览任务已中断，请确认达芬奇渲染已结束后重新生成。')
                result.update(status=row['status'], error=row['error'])
            else:
                try:
                    progress = json.loads((self._folder(row['job_id'], row['id']) / 'progress.json').read_text())
                    result.update(percent=progress.get('percent', 0), message=progress.get('message', '正在导出…'))
                except (OSError, ValueError):
                    pass
        return result

    def detail(self, job_id, uid):
        with self.app.lock, self.lock:
            job, timeline = self._target(job_id, uid)
            rows = [row for row in self._records(job_id) if row['timeline_uid'] == uid]
            task = rows[-1] if rows else None
            preview = next((row for row in reversed(rows) if row['status'] == 'completed'), None)
            if preview:
                try:
                    available = self._file(preview).is_file() and self._file(preview).stat().st_size == preview['size']
                except (OSError, VoiceError):
                    available = False
                if not available:
                    preview = None
            data = self._public(preview)
            if data:
                data['url'] = f"/api/production/jobs/{job_id}/preview/{preview['id']}/video"
                data['source_current'] = preview['source_key'] == source_key(job, timeline)
            return {'timeline_uid': uid, 'name': timeline['name'], 'task': self._public(task), 'preview': data,
                    'blocked_reason': '制作单已归档，请恢复后再生成。' if job.get('archived') else
                        'AI 正在剪辑，完成后再生成预览。' if timeline_running(job, timeline) else ''}

    def start(self, job_id, body):
        if not isinstance(body, dict) or body.keys() - {'request_id', 'timeline_uid'}:
            raise VoiceError(400, '预览请求无效。')
        try:
            request_id = str(UUID(body['request_id']))
        except (KeyError, ValueError, TypeError, AttributeError) as error:
            raise VoiceError(400, '预览请求编号无效，请刷新后重试。') from error
        uid = body.get('timeline_uid')
        with self.app.lock, self.lock:
            job, timeline = self._target(job_id, uid)
            for row in self._records(job_id):
                if row.get('request_id') == request_id:
                    if row['timeline_uid'] != uid:
                        raise VoiceError(409, '这次请求属于另一条时间线。')
                    return self.detail(job_id, uid)
            blocked = self.detail(job_id, uid)['blocked_reason']
            if blocked:
                raise VoiceError(409, blocked)
            if self.busy() or self.app.resolve.busy():
                raise VoiceError(409, '达芬奇正在执行其他操作，请完成后再生成预览。')
            identifier = 'PVW-' + uuid4().hex[:12]
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', timeline['name'])[:120] or 'video'
            row = {'id': identifier, 'job_id': job_id, 'timeline_uid': uid, 'request_id': request_id,
                   'name': timeline['name'], 'source_key': source_key(job, timeline), 'created_at': now(),
                   'filename': name + '-preview.mp4', 'status': 'queued', 'percent': 0, 'message': '正在准备预览导出。'}
            self._save(row)
            self.running[identifier] = job_id
            threading.Thread(target=self._run, args=(row, dict(timeline)), daemon=True, name='video-preview').start()
            return self.detail(job_id, uid)

    def _run(self, row, timeline):
        try:
            with self.lock:
                self._save(row, status='running', message='达芬奇正在准备导出预览。')
            path = self._file(row)
            payload = {key: timeline[key] for key in ('project', 'project_id', 'unique_id', 'name') if key in timeline}
            payload.update(output=str(path), progress_file=str(path.parent / 'progress.json'))
            result = self.app.resolve.run('render_preview', payload, timeout=35*60)
            if Path(result.get('file', '')).resolve() != path.resolve() or not path.is_file():
                raise VoiceError(409, '没有收到对应时间线的预览文件。')
            info = inspect_video(path)
            expected = result.get('frames', 0) / max(float(result.get('fps') or 0), .001)
            if expected <= 0 or abs(info['duration'] - expected) > .3:
                raise VoiceError(409, '预览时长与所选时间线不一致，请重新生成。')
            with self.lock:
                self._save(row, status='completed', percent=100, message='预览已生成。', finished_at=now(),
                           size=path.stat().st_size, warning='；'.join(result.get('warnings') or []), **info)
        except Exception as error:
            with self.lock:
                self._save(row, status='failed', error=str(error) if isinstance(error, VoiceError) else '预览生成失败，请核对达芬奇后重试。')
        finally:
            with self.lock:
                self.running.pop(row['id'], None)

    def video(self, job_id, identifier):
        with self.app.lock, self.lock:
            self.app.production.job(job_id)
            row = next((row for row in self._records(job_id) if row['id'] == identifier and row['status'] == 'completed'), None)
            if not row:
                raise VoiceError(404, '预览尚未生成或已经移除。')
            path = self._file(row)
            if not path.is_file() or path.stat().st_size != row['size']:
                raise VoiceError(409, '预览文件缺失或已变化，请重新生成。')
            return path
