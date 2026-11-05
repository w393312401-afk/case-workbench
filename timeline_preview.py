"""Per-timeline work-in-progress/final video previews, rendered with the timeline's current text and sound."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
from uuid import uuid4

from elevenlabs_client import VoiceError
from production_operations import durable_json
from workbench_video_publish import reviewed_source

PLAYBACK_PROFILE = "web1"


def optimize_playback(source, output, progress):
    """Keep the HD capture; publish an H.264/AAC, fast-start preview bounded to 720p and 1.8 Mb/s."""
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise VoiceError(409, "未找到 FFmpeg，暂时使用高清预览。")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.stem + "." + uuid4().hex[:8] + ".partial.mp4")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        probe = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=width,height,codec_name,avg_frame_rate", "-of", "json", str(source)],
                               capture_output=True, check=True, timeout=30, creationflags=flags)
        stream = json.loads(probe.stdout)["streams"][0]
        numerator, _, denominator = str(stream.get("avg_frame_rate") or "30/1").partition("/")
        try:
            rate = float(numerator) / float(denominator or 1)
        except (ValueError, ZeroDivisionError):
            rate = 30
        fps = min(30.0, max(1.0, rate or 30))
        progress("正在准备流畅播放的视频…")
        command = [ffmpeg, "-v", "error", "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1"]
        if source.stat().st_size <= 4 * 1024 * 1024 and stream["codec_name"] == "h264" and stream["width"] <= 720 and stream["height"] <= 1280:
            command += ["-c", "copy"]
        else:
            command += ["-vf", "scale=w='min(720,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                        "-r", str(fps), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-maxrate", "1800k",
                        "-bufsize", "3600k", "-g", str(round(fps * 2)), "-keyint_min", str(round(fps)), "-sc_threshold", "0",
                        "-pix_fmt", "yuv420p", "-threads", "2", "-c:a", "aac", "-b:a", "128k"]
        subprocess.run([*command, "-movflags", "+faststart", str(temporary)], capture_output=True, check=True, timeout=600, creationflags=flags)
        if not temporary.is_file() or not temporary.stat().st_size:
            raise ValueError("empty playback video")
        temporary.replace(output)
        return output
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        temporary.unlink(missing_ok=True)
        raise VoiceError(502, "在线播放版尚未生成，高清原件已保留。") from None


def now():
    return datetime.now(timezone.utc).isoformat()


class TimelinePreviews:
    def __init__(self, app, *, optimizer=optimize_playback):
        self.app, self.lock = app, app.lock
        self.optimizer = optimizer
        self.root = app.production.root / "video-previews"
        self.root.mkdir(parents=True, exist_ok=True)
        for path in self.root.glob("PRD-*/*/preview.json"):
            row = json.loads(path.read_text(encoding="utf-8"))
            if row.get("status") == "running":
                row.update(status="failed", error="工作台重启打断了预览更新，请重试。", finished_at=now())
                durable_json(path, row)
            elif row.get("status") == "optimizing":
                row.update(status="done", message="", error="")
                durable_json(path, row)
            if row.get("status") == "done" and row.get("video") and row["video"].get("profile") != PLAYBACK_PROFILE:
                row.update(status="optimizing", message="正在准备流畅播放的视频…")
                durable_json(path, row)
                threading.Thread(target=self._upgrade_playback, args=(row,), daemon=True, name="preview-playback-upgrade").start()
            elif row.get("pending_confirmation"):
                threading.Thread(target=self._start_pending, args=(row["job_id"], row["timeline_uid"]), daemon=True,
                                 name="confirmed-export-resume").start()

    def _folder(self, job_id, uid):
        return self.root / job_id / hashlib.sha256(uid.encode("utf-8")).hexdigest()[:24]

    def _read(self, job_id, uid):
        path = self._folder(job_id, uid) / "preview.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def _timeline(self, job_id, uid):
        job = self.app.production.job(job_id)
        timeline = next((t for t in job.get("timelines") or [] if t.get("unique_id") == uid and not t.get("archived")), None)
        if not timeline:
            raise VoiceError(404, "请选择这张制作单的有效时间线。")
        return job, timeline

    @staticmethod
    def _label(job, uid):
        try:
            reviewed_source(job, uid)
            return "成片预览"
        except (VoiceError, KeyError, TypeError):
            return "半成品预览"

    @staticmethod
    def _blocked(job, timeline):
        if job.get("archived"):
            return "制作单已归档，恢复后可更新预览。"
        if not timeline.get("project"):
            return "这条时间线没有达芬奇工程关联。"
        if any(e.get("timeline_uid") == timeline["unique_id"] and e.get("status") == "running" for e in job.get("edits") or []):
            return "AI 正在剪辑这条时间线，结束或停止后可更新预览。"
        return ""

    def _video(self, job, timeline, row):
        folder = self._folder(job["id"], timeline["unique_id"]).resolve()
        video = row.get("video")
        if video:
            path = (folder / video["file"]).resolve()
            if path.is_relative_to(folder) and path.is_file() and path.stat().st_size:
                return video, path
        # Existing local exports are also viewable before a new preview is requested.
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", timeline.get("name", ""))
        path = self.app.production.root / "exports" / (name + ".mp4")
        if path.is_file() and path.stat().st_size:
            stamp = path.stat()
            version = "existing-" + hashlib.sha256(f"{stamp.st_mtime_ns}:{stamp.st_size}".encode()).hexdigest()[:16]
            return {"version": version, "name": path.name, "kind": "已导出视频", "generated_at":
                    datetime.fromtimestamp(stamp.st_mtime, timezone.utc).isoformat()}, path
        return None, None

    def _public(self, job, timeline, row):
        video, _ = self._video(job, timeline, row)
        result = {key: row.get(key) for key in ("status", "message", "error", "started_at", "finished_at")}
        result.update(status=row.get("status", "idle"), timeline_uid=timeline["unique_id"], name=timeline.get("name", ""),
                      blocked_reason=self._blocked(job, timeline), automatic=bool(row.get("confirmation") or row.get("pending_confirmation")), video=None)
        if row.get("pending_confirmation") and row.get("status") not in {"running", "optimizing"}:
            result.update(status="running", message="正在接续导出确认后的成片…")
        if video:
            result["video"] = {key: video[key] for key in ("version", "name", "kind", "generated_at")}
            result["video"]["source_version"] = video.get("source_version", video["version"])
            from urllib.parse import urlencode
            result["video"]["url"] = f"/api/production/jobs/{job['id']}/preview/video?" + urlencode(
                {"timeline": timeline["unique_id"], "version": video["version"]})
            result["video"]["download_url"] = f"/api/production/jobs/{job['id']}/preview/video?" + urlencode(
                {"timeline": timeline["unique_id"], "version": video.get("source_version", video["version"])})
        if row.get("status") == "running":
            path = self._folder(job["id"], timeline["unique_id"]) / "runs" / row["run_id"] / "progress.json"
            try:
                progress = json.loads(path.read_text(encoding="utf-8"))
                result.update(message=progress["message"], percent=progress.get("percent"))
            except (OSError, ValueError, KeyError):
                pass
        return result

    def detail(self, job_id, uid):
        with self.lock:
            job, timeline = self._timeline(job_id, uid)
            return self._public(job, timeline, self._read(job_id, uid))

    def export_file(self, job_id, uid):
        """The latest exported video of this timeline at full quality (the HD capture, not the playback copy)."""
        with self.lock:
            job, timeline = self._timeline(job_id, uid)
            row = self._read(job_id, uid)
            video, path = self._video(job, timeline, row)
            if video and video.get("source_file"):
                folder = self._folder(job_id, uid).resolve()
                source = (folder / video["source_file"]).resolve()
                if source.is_relative_to(folder) and source.is_file() and source.stat().st_size:
                    return source
            return path

    def video_file(self, job_id, uid, version):
        with self.lock:
            job, timeline = self._timeline(job_id, uid)
            row = self._read(job_id, uid)
            video, path = self._video(job, timeline, row)
            # A player opened before the one-time upgrade may still request ranges of the HD capture.
            original = row.get("video") or {}
            if original.get("source_version") == version and original.get("source_file"):
                folder = self._folder(job_id, uid).resolve()
                source = (folder / original["source_file"]).resolve()
                if source.is_relative_to(folder) and source.is_file():
                    return source
            if not video or video["version"] != version:
                raise VoiceError(404, "这份预览暂不可用，请读取最新预览或重新导入。")
            return path

    def busy(self, job_id=None):
        with self.lock:
            paths = (self.root / job_id).glob("*/preview.json") if job_id else self.root.glob("PRD-*/*/preview.json")
            for path in paths:
                row = json.loads(path.read_text(encoding="utf-8"))
                if row.get("status") in {"running", "optimizing"} or row.get("pending_confirmation"):
                    return True
            return False

    def _playback_video(self, folder, video, source, progress):
        playback = self.optimizer(source, source.with_name(f"playback-{PLAYBACK_PROFILE}.mp4"), progress).resolve()
        if playback != source.resolve():
            return {**video, "file": str(playback.relative_to(folder.resolve())), "version": video["version"] + "-" + PLAYBACK_PROFILE,
                    "source_file": video["file"], "source_version": video["version"], "profile": PLAYBACK_PROFILE}
        return video

    def _upgrade_playback(self, row):
        folder = self._folder(row["job_id"], row["timeline_uid"])
        try:
            source = (folder / row["video"]["file"]).resolve()
            if not source.is_relative_to(folder.resolve()):
                raise ValueError("invalid source")
            video = self._playback_video(folder, row["video"], source, lambda _: None)
            patch = {"status": "done", "message": "预览播放已优化。", "video": video}
        except Exception:
            patch = {"status": "done", "message": "暂用高清预览，稍后更新预览可重试播放优化。"}
        with self.lock:
            current = self._read(row["job_id"], row["timeline_uid"])
            if current.get("run_id") == row.get("run_id") and current.get("status") == "optimizing":
                current.update(patch)
                durable_json(folder / "preview.json", current)
        self._start_pending(row["job_id"], row["timeline_uid"])

    @staticmethod
    def _confirmation(source):
        receipt = source.get("manual_completion")
        edit = source.get("edit") or {}
        identity = ["manual", receipt] if receipt else ["edit", edit.get("id"), edit.get("approval")]
        return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def after_confirmation(self, job_id, result):
        """Queue one capture per saved human review; exporting cannot undo that review."""
        if result.get("approved") is True:
            uid = result["edit"]["timeline_uid"]
        elif result.get("completed") is True:
            uid = result["timeline_uid"]
        else:
            return result
        try:
            preview = self.start(job_id, {"timeline_uid": uid}, confirmation=self._confirmation(result))
        except Exception as exc:
            preview = {"status": "failed", "error": f"人工确认已保存，但自动导出未启动：{exc}；可在成片与发布中重试。"}
        return {**result, "video_preview": preview}

    def _start_pending(self, job_id, uid):
        with self.lock:
            row = self._read(job_id, uid)
            confirmation = row.get("pending_confirmation")
            if not confirmation or row.get("status") in {"running", "optimizing"}:
                return
            try:
                self.start(job_id, {"timeline_uid": uid}, confirmation=confirmation)
            except Exception as exc:
                row.pop("pending_confirmation", None)
                row.update(status="failed", message="", error=str(exc), finished_at=now())
                durable_json(self._folder(job_id, uid) / "preview.json", row)

    def start(self, job_id, body, *, confirmation=None):
        if not isinstance(body, dict) or set(body) != {"timeline_uid"}:
            raise VoiceError(400, "请选择要更新预览的时间线。")
        uid = body["timeline_uid"]
        with self.lock:
            job, timeline = self._timeline(job_id, uid)
            old = self._read(job_id, uid)
            if confirmation is not None:
                if self._confirmation(reviewed_source(job, uid)) != confirmation:
                    raise VoiceError(409, "人工确认已变化，本次自动导出已取消。")
                if old.get("confirmation") == confirmation:
                    return self._public(job, timeline, old)
            if old.get("status") in {"running", "optimizing"}:
                if confirmation is not None:
                    old["pending_confirmation"] = confirmation
                    durable_json(self._folder(job_id, uid) / "preview.json", old)
                return self._public(job, timeline, old)
            if reason := self._blocked(job, timeline):
                raise VoiceError(409, reason)
            row = {"job_id": job_id, "timeline_uid": uid, "run_id": uuid4().hex[:12], "status": "running",
                   "message": "正在连接达芬奇…", "error": "", "started_at": now(), "video": old.get("video"),
                   "pending_recovery": old.get("pending_recovery") or old.get("run_id")}
            if confirmation is not None:
                row["confirmation"] = confirmation
                row["message"] = "人工确认已保存，正在排队导出成片…"
            durable_json(self._folder(job_id, uid) / "preview.json", row)
            reply = self._public(job, timeline, row)
        threading.Thread(target=self._run, args=(job_id, uid, row), daemon=True, name="timeline-preview").start()
        return reply

    def _run(self, job_id, uid, row):
        folder = self._folder(job_id, uid)
        scratch = folder / "runs" / row["run_id"]
        def update(**values):
            with self.lock:
                # A confirmation may have queued the next capture while this one was rendering.
                current = self._read(job_id, uid)
                if current.get("run_id") != row["run_id"]:
                    return
                row.update(current)
                row.update(values)
                durable_json(folder / "preview.json", row)
        try:
            scratch.mkdir(parents=True, exist_ok=True)
            with self.app.resolve.access(on_wait=lambda: update(message="正在等待达芬奇空闲…")):
                if pending := row.get("pending_recovery"):
                    recovery = folder / "runs" / pending / "recovery.json"
                    if recovery.is_file():
                        self.app.resolve.run("restore_textless", {"recovery_path": str(recovery), "stop_render": False}, timeout=60, held=True)
                job, timeline = self._timeline(job_id, uid)
                if row.get("confirmation") and self._confirmation(reviewed_source(job, uid)) != row["confirmation"]:
                    raise VoiceError(409, "人工确认已变化，本次自动导出已取消。")
                if reason := self._blocked(job, timeline):
                    raise VoiceError(409, reason)
                payload = {key: timeline[key] for key in ("project", "project_id", "creator", "folder") if timeline.get(key)}
                payload.update(unique_id=uid, name=timeline["name"], output_dir=str(scratch), render_timeout=1200,
                               progress_path=str(scratch / "progress.json"), recovery_path=str(scratch / "recovery.json"))
                update(pending_recovery=row["run_id"])
                try:
                    result = self.app.resolve.run("export_preview", payload, timeout=1320, held=True)
                    if result.get("unique_id") != uid or result.get("restored") is not True:
                        raise VoiceError(502, "预览的时间线或导出设置恢复结果未确认。")
                except Exception as exc:
                    try:
                        self.app.resolve.run("restore_textless", payload, timeout=60, held=True)
                    except Exception as recovery_error:
                        raise VoiceError(409, f"{exc}；导出设置恢复未确认：{recovery_error}") from exc
                    raise
                source = Path(result["video_path"]).resolve()
                if source != (scratch / "preview.mp4").resolve() or not source.is_file() or not source.stat().st_size:
                    raise VoiceError(502, "达芬奇没有交回可播放的预览视频。")
            # Encoding belongs outside the Resolve lease, so it cannot hold up another edit.
            video = {"file": str(source.relative_to(folder.resolve())), "version": row["run_id"],
                     "name": timeline["name"] + ".mp4", "kind": self._label(job, uid), "generated_at": now()}
            update(pending_recovery=None)
            message = "预览已更新。"
            try:
                video = self._playback_video(folder, video, source, lambda text: update(message=text))
            except Exception:
                message = "预览已更新，暂用高清版；稍后更新预览可重试播放优化。"
            with self.lock:
                current_job, _ = self._timeline(job_id, uid)
                if row.get("confirmation") and self._confirmation(reviewed_source(current_job, uid)) != row["confirmation"]:
                    raise VoiceError(409, "人工确认已变化，本次自动导出已取消。")
                update(status="done", message=message, error="", video=video, pending_recovery=None, finished_at=now())
        except Exception as exc:
            update(status="failed", message="", error=str(exc), finished_at=now())
        finally:
            self._start_pending(job_id, uid)
