"""Local full-data transfer and persisted daily backup scheduling."""
from __future__ import annotations

from datetime import datetime, timedelta
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time

from data_backup import BackupManager


DEFAULT_SETTINGS = {"enabled": False, "directory": "", "interval_days": 1, "time": "02:00", "keep": 14}
PREFERENCE_KEY = re.compile(r"^(?:case|workbench|native|material)[.-]")


class TransferError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def preferences(value):
    if not isinstance(value, dict) or len(value) > 100:
        raise TransferError("浏览器偏好格式无效。")
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not PREFERENCE_KEY.match(key) or len(key) > 200:
            raise TransferError("浏览器偏好包含未知设置。")
        if not isinstance(item, str) or len(item) > 1024 * 1024:
            raise TransferError("浏览器偏好内容过大或格式无效。")
        result[key] = item
    if len(json.dumps(result)) > 4 * 1024 * 1024:
        raise TransferError("浏览器偏好内容过大。")
    return result


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class WindowsSchedule:
    """Register a task for this user, with an argv action and no credentials."""
    def __init__(self, runtime, app_root):
        digest = hashlib.sha256(str(runtime).casefold().encode()).hexdigest()[:16]
        self.name = "CaseWorkbenchBackup-" + digest
        self.runtime = runtime
        self.app_root = app_root

    def configure(self, settings, first_run, config_path):
        if os.name != "nt":
            return {"installed": False, "mode": "app", "message": "自动备份在工作台运行时执行；关闭后会在下次打开时补备份。"}
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        if pythonw.is_file():
            python = pythonw
        arguments = subprocess.list2cmdline(["-X", "utf8", str(self.app_root / "backup_worker.py"), "--config", str(config_path)])
        payload = {"name": self.name, "enabled": settings["enabled"], "days": settings["interval_days"],
                   "at": first_run, "exe": str(python), "args": arguments, "cwd": str(self.app_root)}
        script = r'''
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$taskData = [Console]::In.ReadToEnd() | ConvertFrom-Json
if (-not $taskData.enabled) {
  $existingTask = Get-ScheduledTask -TaskName $taskData.name -ErrorAction SilentlyContinue
  if ($existingTask) { Unregister-ScheduledTask -TaskName $taskData.name -Confirm:$false }
} else {
  $action = New-ScheduledTaskAction -Execute $taskData.exe -Argument $taskData.args -WorkingDirectory $taskData.cwd
  $trigger = New-ScheduledTaskTrigger -Daily -DaysInterval $taskData.days -At ([datetime]::Parse($taskData.at))
  $principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
  $options = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 12) -RestartInterval (New-TimeSpan -Minutes 5) -RestartCount 72
  Register-ScheduledTask -TaskName $taskData.name -Action $action -Trigger $trigger -Principal $principal -Settings $options -Description '案例工作台总数据自动备份' -Force | Out-Null
}
'''
        try:
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                    input=json.dumps(payload, ensure_ascii=False), capture_output=True,
                                    text=True, encoding="utf-8", timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TransferError("Windows 自动备份任务未能保存，请重试。", 503) from exc
        if result.returncode:
            detail = (result.stderr.strip() or result.stdout.strip())[-1500:]
            raise TransferError("Windows 自动备份任务未能保存：" + detail, 503)
        return {"installed": settings["enabled"], "mode": "windows", "task_name": self.name,
                "message": "关闭工作台后也会备份；电脑需开机并登录当前 Windows 用户，错过时间会在下次可运行时补备份。"}


class DataTransfer:
    def __init__(self, skill_root, runtime, app_root, *, locks=(), validator=None, ensure_idle=None, on_restore=None, extra_sources=None):
        self.skill_root = Path(skill_root).resolve()
        self.runtime = Path(runtime).resolve()
        self.app_root = Path(app_root).resolve()
        self.root = self.runtime / "data-transfer"
        self.config_path = self.root / "settings.json"
        self.manager = BackupManager(self.skill_root, self.runtime, self.app_root, locks=locks, validator=validator)
        self.ensure_idle = ensure_idle or (lambda: None)
        self.on_restore = on_restore or (lambda: None)
        self.maintenance = nullcontext
        self._gate = None
        self.schedule = WindowsSchedule(self.runtime, self.app_root)
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.busy = False
        self._imports = {}
        self._downloads = {}
        self._stop = threading.Event()
        self._thread = None
        self.config = self._load()
        sources = self.config.get("extra_sources", []) if extra_sources is None or self.config.get("sources_restored") else extra_sources
        self.manager.extra_sources = tuple(Path(path).expanduser().resolve() for path in sources)

    def _load(self):
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("settings"), dict):
                raise ValueError("invalid settings")
            return data
        except FileNotFoundError:
            return {"settings": dict(DEFAULT_SETTINGS), "preferences": {}, "history": [], "last_error": "",
                    "scheduler": {"installed": False, "mode": "windows" if os.name == "nt" else "app", "message": "尚未启用自动备份。"}}
        except (ValueError, OSError) as exc:
            raise TransferError("备份设置无法读取，请保留设置文件并检查。", 500) from exc

    def _save(self):
        self.config.update(skill_root=str(self.skill_root), runtime_root=str(self.runtime), app_root=str(self.app_root))
        self.config["extra_sources"] = [str(path) for path in self.manager.extra_sources]
        atomic_json(self.config_path, self.config)

    def start(self, port):
        self.port = port
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="workbench-daily-backup", daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        # Do not release the server lease while a snapshot still touches its files.
        with self.operation:
            pass
        if self._thread:
            self._thread.join(timeout=2)

    def _slot(self, now):
        if not self.config["settings"]["enabled"] or not self.config.get("first_run"):
            return None
        first = datetime.fromisoformat(self.config["first_run"])
        if now < first:
            return None
        period = timedelta(days=self.config["settings"]["interval_days"])
        return first + ((now - first) // period) * period

    def _next(self, now):
        if not self.config["settings"]["enabled"] or not self.config.get("first_run"):
            return None
        first = datetime.fromisoformat(self.config["first_run"])
        slot = self._slot(now)
        done = self.config.get("last_auto_slot")
        if slot and (not done or datetime.fromisoformat(done) < slot):
            return slot.isoformat()
        return (first if slot is None else slot + timedelta(days=self.config["settings"]["interval_days"])).isoformat()

    def status(self):
        with self.lock:
            history = [item for item in self.config.get("history", []) if Path(item["path"]).is_file()]
            error = self.config.get("last_error", "")
            worker_error = self.root / "worker-error.log"
            if worker_error.is_file():
                error = worker_error.read_text(encoding="utf-8")[:2000].strip() or error
            return {"settings": dict(self.config["settings"]), "last_backup": history[-1] if history else None,
                    "last_error": error, "next_backup": self._next(datetime.now()),
                    "scheduler": self.config.get("scheduler", {}), "backups": list(reversed(history[-50:])), "busy": self.busy}

    def _directory(self, value):
        if not isinstance(value, str) or not value.strip() or len(value) > 2048:
            raise TransferError("请填写或选择本地备份文件夹。")
        target = Path(value.strip()).expanduser()
        if not target.is_absolute() or (os.name == "nt" and str(target).startswith(("\\\\", "//"))):
            raise TransferError("请使用本机文件夹的完整路径，例如 D:\\工作台备份。")
        target = target.resolve()
        for root in (self.skill_root, self.runtime, self.app_root):
            if target.is_relative_to(root) or root.is_relative_to(target):
                raise TransferError("备份文件夹必须放在工作台、案例库和运行资料目录之外，避免重复打包。")
        try:
            target.mkdir(parents=True, exist_ok=True)
            descriptor, probe = tempfile.mkstemp(prefix=".backup-write-check-", dir=target)
            os.close(descriptor)
            Path(probe).unlink()
        except OSError as exc:
            raise TransferError("备份文件夹不可写，请检查磁盘或目录权限。") from exc
        return target

    def save_settings(self, body):
        if set(body) != set(DEFAULT_SETTINGS) or not isinstance(body["enabled"], bool):
            raise TransferError("备份设置格式无效。")
        for name, lower, upper in (("interval_days", 1, 365), ("keep", 0, 3650)):
            if isinstance(body[name], bool) or not isinstance(body[name], int) or not lower <= body[name] <= upper:
                raise TransferError("备份间隔需为 1–365 天，保留份数需为 0–3650。")
        if not isinstance(body["time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", body["time"]):
            raise TransferError("请选择有效的备份时间。")
        if not isinstance(body["directory"], str):
            raise TransferError("备份文件夹格式无效。")
        settings = dict(body)
        if settings["directory"].strip() or settings["enabled"]:
            settings["directory"] = str(self._directory(settings["directory"]))
        now = datetime.now()
        hour, minute = map(int, settings["time"].split(":"))
        first = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if first <= now:
            first += timedelta(days=1)
        with self.lock:
            if self.busy:
                raise TransferError("数据操作正在进行，请完成后再保存设置。", 409)
            old = self.config.copy()
            scheduler = self.schedule.configure(settings, first.isoformat(), self.config_path)
            self.config.update(settings=settings, first_run=first.isoformat(), scheduler=scheduler,
                               port=getattr(self, "port", self.config.get("port", 8767)), last_auto_slot=None, last_error="")
            try:
                self._save()
            except Exception:
                self.config = old
                self.schedule.configure(old["settings"], old.get("first_run", first.isoformat()), self.config_path)
                raise
        return self.status()

    def save_preferences(self, body):
        if set(body) != {"preferences"}:
            raise TransferError("浏览器偏好格式无效。")
        value = preferences(body["preferences"])
        with self.lock:
            self.config["preferences"] = value
            self._save()
        return {"ok": True}

    def _begin(self):
        if self._stop.is_set():
            raise TransferError("工作台正在关闭，请重新打开后再操作总数据。", 409)
        if not self.operation.acquire(blocking=False):
            raise TransferError("已有数据导入、导出或备份正在进行，请稍后重试。", 409)
        if self._stop.is_set():
            self.operation.release()
            raise TransferError("工作台正在关闭，请重新打开后再操作总数据。", 409)
        with self.lock:
            self.busy = True
        try:
            self._gate = self.maintenance()
            self._gate.__enter__()
        except Exception:
            self._gate = None
            self._end()
            raise

    def _end(self):
        if self._gate is not None:
            self._gate.__exit__(None, None, None)
            self._gate = None
        with self.lock:
            self.busy = False
        self.operation.release()

    def backup(self, *, automatic=False):
        self._begin()
        try:
            with self.lock:
                settings = dict(self.config["settings"])
                saved_preferences = dict(self.config.get("preferences", {}))
                automatic_slot = self._slot(datetime.now()) if automatic else None
            destination = self._directory(settings["directory"])
            self.ensure_idle()
            archive = self.manager.create_archive(destination, kind="auto" if automatic else "backup", preferences=saved_preferences)
            with self.lock:
                self.config.setdefault("history", []).append(archive)
                self.config["last_error"] = ""
                (self.root / "worker-error.log").unlink(missing_ok=True)
                if automatic_slot:
                    self.config["last_auto_slot"] = automatic_slot.isoformat()
                self._save()
                self._prune(destination, settings["keep"])
            return {"backup": archive, "status": self.status()}
        except Exception as exc:
            with self.lock:
                self.config["last_error"] = str(exc)
                self._save()
            raise
        finally:
            self._end()

    def _prune(self, directory, keep):
        if not keep:
            return
        history = self.config.get("history", [])
        current = [item for item in history if Path(item["path"]).resolve().parent == directory and Path(item["path"]).is_file()]
        for item in current[:-keep]:
            path = Path(item["path"])
            # Delete only successfully registered archives from this manager's destination.
            if path.resolve().parent != directory or path.name != item.get("name") or path.suffix != ".zip" or path.is_symlink():
                continue
            try:
                self.manager.inspect_archive(path)
                path.unlink()
                history.remove(item)
            except (OSError, ValueError):
                continue
        self._save()

    def export(self, body):
        if set(body) != {"preferences"}:
            raise TransferError("导出设置格式无效。")
        value = preferences(body["preferences"])
        self._begin()
        try:
            self.ensure_idle()
            self._cleanup_temporary()
            archive = self.manager.create_archive(self.root / "exports", kind="export", preferences=value)
            key = secrets.token_urlsafe(24)
            self._downloads[key] = (Path(archive["path"]), time.monotonic())
            return {"archive": archive, "download_url": "/api/data/download/" + key}
        finally:
            self._end()

    def download(self, key):
        item = self._downloads.get(key)
        if not item or time.monotonic() - item[1] > 24 * 3600 or not item[0].is_file():
            raise TransferError("导出文件已过期，请重新导出。", 404)
        return item[0]

    def inspect_upload(self, stream, length):
        self._begin()
        path = None
        try:
            self._cleanup_temporary()
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / "imports" / (secrets.token_hex(24) + ".zip")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as target:
                remaining = length
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise TransferError("备份包上传中断，请重新选择文件。")
                    target.write(chunk)
                    remaining -= len(chunk)
            summary = self.manager.inspect_archive(path)
            saved_preferences = preferences(summary.get("preferences", {}))
            key = secrets.token_urlsafe(24)
            self._imports[key] = (path, time.monotonic(), saved_preferences)
            return {"import_id": key, "summary": summary}
        except Exception:
            if path:
                path.unlink(missing_ok=True)
            raise
        finally:
            self._end()

    def restore(self, body):
        if set(body) != {"import_id", "confirm"} or body["confirm"] is not True or not isinstance(body["import_id"], str):
            raise TransferError("请先核对备份包并确认替换当前全部数据。")
        item = self._imports.get(body["import_id"])
        if not item or time.monotonic() - item[1] > 24 * 3600:
            raise TransferError("导入预览已过期，请重新选择备份包。")
        self._begin()
        try:
            self.ensure_idle()
            result = self.manager.restore_archive(item[0], self.root / "recovery", recovery_preferences=self.config.get("preferences", {}))
            with self.lock:
                self.config["preferences"] = item[2]
                if "extra_source_roots" in result:
                    self.manager.extra_sources = tuple(Path(path) for path in result["extra_source_roots"])
                    self.config["sources_restored"] = True
                try:
                    self._save()
                except OSError:
                    result["warning"] = "数据已恢复，但本机备份偏好未能保存，请检查磁盘空间后重新保存备份设置。"
            try:
                self.on_restore()
            except Exception as exc:
                result["warning"] = "数据已恢复，部分页面缓存刷新未完成，请重新启动工作台：" + str(exc)
            self._imports.pop(body["import_id"], None)
            item[0].unlink(missing_ok=True)
            return {**result, "message": "总数据已导入；导入前的数据已保存为恢复备份。" + (result.get("warning", ""))}
        finally:
            self._end()

    def due_backup(self):
        with self.lock:
            slot = self._slot(datetime.now())
            done = self.config.get("last_auto_slot")
            if not slot or (done and datetime.fromisoformat(done) >= slot):
                return None
        return self.backup(automatic=True)

    def _loop(self):
        while not self._stop.wait(30):
            try:
                self.due_backup()
            except Exception:
                # Persisted last_error is shown in settings; retry missed runs after five minutes.
                self._stop.wait(300)

    def _cleanup_temporary(self):
        for folder in ("exports", "imports"):
            for path in (self.root / folder).glob("*.zip"):
                if not path.is_symlink() and time.time() - path.stat().st_mtime > 24 * 3600:
                    path.unlink(missing_ok=True)


def pick_directory():
    script = "import tkinter as tk; from tkinter import filedialog; import json; r=tk.Tk(); r.withdraw(); r.attributes('-topmost',True); p=filedialog.askdirectory(parent=r,title='选择总数据备份文件夹',mustexist=False); r.destroy(); print(json.dumps({'path':p,'cancelled':not bool(p)},ensure_ascii=False))"
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", script], capture_output=True,
                            text=True, encoding="utf-8", timeout=300,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise TransferError("本地目录选择窗口无法打开，请直接填写文件夹完整路径。", 503)
    return json.loads(result.stdout)
