"""One-click restart for this workbench, including legacy-instance cleanup."""
from __future__ import annotations

from collections import Counter
import ctypes
from ctypes import wintypes
from contextlib import contextmanager
from datetime import date
from fnmatch import fnmatch
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from app_config import load_config

ROOT = Path(__file__).resolve().parent
ACTIVE_TASKS = {"queued", "running", "stopping"}
# Business records that exist only on this machine; copied on every start.
BACKUP_SOURCES = (("runtime_root", ("directions", "creation", "ai-tasks", "reviews", "revisions", "navigation-assignments.json",
                                    "production", "library", "performance", "evidence", "davinci-ledger",
                                    "task-activity.sqlite", "ai-preferences.json", "ai-kind-preferences.json", "hook-bank", "benchmark", "script-reviews")),
                  ("skill_root", ("cases", "references", "sources", "deleted_case_sources.json")))
# These keep records next to gigabytes of media. Images, video and WAV (rebuilt from the MP3 take) stay with the
# full backup in data_backup.py, as do caches and one-off database copies. MP3 takes are paid output and stay here.
BACKUP_RECORDS_ONLY = frozenset({"production", "library", "performance", "evidence", "hook-bank"})
BACKUP_MEDIA_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".m4v", ".webm", ".wav"})
BACKUP_SKIP_NAMES = ("clip-cache", "thumb-cache", "public-exports", "tiering-*", "*.before-*", "*.lock", "*.tmp")
BACKUP_KEEP = 14
BACKUP_DAYS = 14
# An archive that lost much of what the last complete one held is marked thin and has its own small quota.
BACKUP_THIN_MARK = "-thin"
BACKUP_THIN_KEEP = 5
BACKUP_THIN_RATIO = .5
BACKUP_CORE_RATIO = .1
BACKUP_CORE_FILES = ("runtime/production/catalog.json", "runtime/production/video-publications/catalog.json",
                     "runtime/creation/catalog.json", "runtime/directions/catalog.json",
                     "runtime/performance/performance.sqlite", "runtime/reviews/reviews.sqlite")
# A deliberate large cleanup stops counting as loss after this long, so the comparison cannot stay frozen forever.
BACKUP_BASELINE_DAYS = 30
LOG_LIMIT = 5 * 1024 * 1024
LOG_KEEP = 5


def same_path(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def windows_argv(command):
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel.LocalFree.restype = wintypes.HLOCAL
    count = ctypes.c_int()
    pointer = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not pointer:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [pointer[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(pointer)


def matches_workbench(process, root, config):
    """Match the executed script and configuration, never a substring or all Python."""
    executable = process.get("ExecutablePath") or ""
    if Path(executable).name.lower() not in {"python.exe", "pythonw.exe"}:
        return False
    command = process.get("CommandLine")
    if not command or not process.get("CreatedTicks"):
        return False
    args = windows_argv(command)
    if not args or not same_path(args[0], executable):
        return False
    index = 1
    while index < len(args) and args[index] in {"-u", "-B", "-E", "-I", "-s", "-S", "-O", "-OO"}:
        index += 1
    if index >= len(args) or not Path(args[index]).is_absolute() or not any(
            same_path(args[index], root / name) for name in ("server.py", "service_gateway.py")):
        return False
    options = {"--port": str(config["port"]), "--skill-root": config["skill_root"], "--runtime-root": config["runtime_root"]}
    seen = set()
    index += 1
    while index < len(args):
        key, separator, value = args[index].partition("=")
        if key == "--no-open" and not separator:
            index += 1
            continue
        if key not in options or key in seen:
            return False
        seen.add(key)
        if not separator:
            index += 1
            if index >= len(args):
                return False
            value = args[index]
        if key == "--port":
            if value != options[key]:
                return False
        elif not Path(value).is_absolute() or not same_path(value, options[key]):
            return False
        index += 1
    return True


def matching_processes(root, config):
    # Read-only inventory. Exact arguments are checked in Python.
    command = r"""[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
      Select-Object ProcessId,ExecutablePath,CommandLine,@{Name='CreatedTicks';Expression={[string]$_.CreationDate.ToUniversalTime().ToFileTimeUtc()}}) |
      ConvertTo-Json -Compress"""
    completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                               capture_output=True, text=True, encoding="utf-8-sig", check=True,
                               timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    values = json.loads(completed.stdout or "[]")
    if isinstance(values, dict):
        values = [values]
    return [process for process in values if matches_workbench(process, root, config)]


def windows_kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcess": ((wintypes.DWORD, wintypes.BOOL, wintypes.DWORD), wintypes.HANDLE),
        "CloseHandle": ((wintypes.HANDLE,), wintypes.BOOL),
        "GetProcessTimes": ((wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)), wintypes.BOOL),
        "QueryFullProcessImageNameW": ((wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)), wintypes.BOOL),
        "TerminateProcess": ((wintypes.HANDLE, wintypes.UINT), wintypes.BOOL),
        "WaitForSingleObject": ((wintypes.HANDLE, wintypes.DWORD), wintypes.DWORD),
        "CreateMutexW": ((ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR), wintypes.HANDLE),
        "ReleaseMutex": ((wintypes.HANDLE,), wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


@contextmanager
def launch_lock(root, port):
    kernel = windows_kernel()
    identity = hashlib.sha256(f"{os.path.normcase(str(root))}:{port}".encode()).hexdigest()[:24]
    handle = kernel.CreateMutexW(None, False, "Local\\CaseWorkbenchLauncher-" + identity)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        if result not in (0, 0x80, 0x102):
            raise ctypes.WinError(ctypes.get_last_error())
        acquired = result in (0, 0x80)
        yield acquired  # A second click exits rather than queuing another restart.
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def process_identity(kernel, handle):
    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    name, length = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
    if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(length)):
        raise ctypes.WinError(ctypes.get_last_error())
    return created, name.value


def stop_legacy_process(process):
    """Stop a legacy instance only while holding its verified process handle."""
    kernel = windows_kernel()
    handle = kernel.OpenProcess(0x100000 | 0x1000 | 0x0001, False, process["ProcessId"])
    if not handle:
        if ctypes.get_last_error() == 87:
            return  # Already exited.
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if kernel.WaitForSingleObject(handle, 0) == 0:
            return
        created, executable = process_identity(kernel, handle)
        # CIM timestamps have microsecond precision; FILETIME uses 100ns ticks.
        if created // 10 != int(process["CreatedTicks"]) // 10 or not same_path(executable, process["ExecutablePath"]):
            raise RuntimeError("旧服务进程已发生变化，请重新双击工作台。")
        if not kernel.TerminateProcess(handle, 0):
            if kernel.WaitForSingleObject(handle, 0) != 0:
                raise ctypes.WinError(ctypes.get_last_error())
        if kernel.WaitForSingleObject(handle, 10000) != 0:
            raise RuntimeError("旧服务仍在退出，请稍后再次打开工作台。")
    finally:
        kernel.CloseHandle(handle)


def read_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def read_existing_service(url, phase, timeout):
    """Keep migration's expensive full-library check out of the fast path."""
    try:
        return read_json(url, timeout=timeout)
    except urllib.error.HTTPError:
        raise  # A 404 identifies the legacy protocol, not a failed restart.
    except (TimeoutError, urllib.error.URLError) as error:
        timed_out = isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError)
        reason = "超时" if timed_out else "失败"
        raise RuntimeError(f"{phase}{reason}，尚未关闭旧服务。请稍后重新双击TK工作台。") from error


def wait_for_process_exit(process):
    kernel = windows_kernel()
    handle = kernel.OpenProcess(0x100000 | 0x1000, False, process["ProcessId"])
    if not handle:
        if ctypes.get_last_error() == 87:
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if kernel.WaitForSingleObject(handle, 0) == 0:
            return
        created, executable = process_identity(kernel, handle)
        if created // 10 != int(process["CreatedTicks"]) // 10 or not same_path(executable, process["ExecutablePath"]):
            return  # The old PID was reused; never act on its new owner.
        if kernel.WaitForSingleObject(handle, 10000) != 0:
            raise RuntimeError("服务还在关闭，请稍后再次打开工作台。")
    finally:
        kernel.CloseHandle(handle)


def correct_service(state, config):
    paths = state.get("paths", {})
    return (state.get("app") == "video-case-workbench"
            and bool(paths.get("records")) and bool(paths.get("library"))
            and same_path(paths["records"], Path(config["skill_root"]) / "cases")
            and same_path(paths["library"], Path(config["runtime_root"]) / "library"))


def ensure_no_active_tasks(config):
    # Includes busy legacy instances that are not currently answering the port.
    for path in (Path(config["skill_root"]) / "cases").glob("PVR-*/collaboration/AIT-*/task.json"):
        task = json.loads(path.read_text(encoding="utf-8"))
        if task.get("status") in ACTIVE_TASKS:
            raise RuntimeError("AI 协作正在进行。请等待任务完成或在工作台取消后，再双击重启。")


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=.3):
            return True
    except OSError:
        return False


def wait_for_port_release(port, timeout=10):
    deadline = time.monotonic() + timeout
    while port_open(port):
        if time.monotonic() >= deadline:
            raise RuntimeError("工作台端口仍被占用，尚未启动第二个实例。请稍后重试。")
        time.sleep(.1)


def prune(folder, pattern, keep):
    for old in sorted(folder.glob(pattern))[:-keep]:
        old.unlink()


def _deflate(entry):
    """Compress one file quickly with deflate; zlib releases the GIL, so threads run side by side."""
    path, name = entry
    data = path.read_bytes()
    compressor = zlib.compressobj(zlib.Z_BEST_SPEED, zlib.DEFLATED, -15)
    info = zipfile.ZipInfo.from_file(path, name)
    return info.date_time, info.external_attr, zlib.crc32(data), len(data), compressor.compress(data) + compressor.flush()


def write_zip_parallel(partial, entries):
    """Write the same deflate archive as ZipFile.write, compressing files on several threads.

    Returns False without leaving a result when the archive would need ZIP64 or fails its read-back check;
    the caller then writes it the ordinary way, so a backup is never skipped."""
    limit = 0xFFFFFFFF
    if len(entries) >= 0xFFFF:
        return False
    central, written, offset = [], [], 0
    try:
        with ThreadPoolExecutor(max_workers=min(16, os.cpu_count() or 1)) as pool, partial.open("wb") as stream:
            for (path, name), (stamp, attributes, crc, size, packed) in zip(entries, pool.map(_deflate, entries)):
                encoded = name.encode("utf-8")
                flags = 0 if name.isascii() else 0x800
                date = ((stamp[0] - 1980) << 9) | (stamp[1] << 5) | stamp[2]
                clock = (stamp[3] << 11) | (stamp[4] << 5) | (stamp[5] // 2)
                if size >= limit or len(packed) >= limit or offset >= limit:
                    raise OverflowError("ZIP64")
                fields = (20, flags, 8, clock, date, crc, len(packed), size, len(encoded))
                stream.write(struct.pack("<L5HL2L2H", 0x04034B50, *fields, 0) + encoded + packed)
                central.append(struct.pack("<L6H3L5H2L", 0x02014B50, 20, *fields, 0, 0, 0, 0, attributes, offset) + encoded)
                written.append((name, crc, size))
                offset += 30 + len(encoded) + len(packed)
            if offset >= limit:
                raise OverflowError("ZIP64")
            directory = b"".join(central)
            stream.write(directory + struct.pack("<L4H2LH", 0x06054B50, 0, 0, len(central), len(central), len(directory), offset, 0))
        with zipfile.ZipFile(partial) as archive:
            # The directory must list exactly what was written; the first and last file are read back in full.
            if [(info.filename, info.CRC, info.file_size) for info in archive.infolist()] != written:
                raise ValueError("read-back")
            for _, name in (entries[:1] + entries[-1:]):
                archive.read(name)
    except (OverflowError, ValueError, zipfile.BadZipFile):
        partial.unlink(missing_ok=True)
        return False
    return True


def backup_files(source):
    """Every file of one source; record-only sources leave out media and regenerable copies."""
    if source.is_file():
        return [source]
    if not source.is_dir():
        return []
    records_only = source.name in BACKUP_RECORDS_ONLY
    found = []
    for root, dirs, files in os.walk(source):
        if records_only:
            dirs[:] = [name for name in dirs if not any(fnmatch(name, pattern) for pattern in BACKUP_SKIP_NAMES)]
        root_path = Path(root)
        for name in files:
            if records_only and (Path(name).suffix.casefold() in BACKUP_MEDIA_SUFFIXES
                                 or any(fnmatch(name, pattern) for pattern in BACKUP_SKIP_NAMES)):
                continue
            found.append(root_path / name)
    found.sort()
    return found


def archive_sizes(path):
    """File sizes from the zip directory, cheap however large the archive; None when it cannot be read."""
    try:
        with zipfile.ZipFile(path) as archive:
            return {info.filename: info.file_size for info in archive.infolist()}
    except (OSError, zipfile.BadZipFile):
        return None


def data_loss(sizes, baseline):
    """Name what shrank sharply against the last complete archive; empty when nothing did.

    Sources are compared one by one because a wipe can leave the rest intact, and sizes
    only for core files because large stores legitimately prune their own history."""
    def counts(files):
        return Counter("/".join(name.split("/")[:2]) for name in files if not name.endswith(("-wal", "-shm")))
    found, expected = counts(sizes), counts(baseline)
    for source, count in expected.items():
        if found[source] < count * BACKUP_THIN_RATIO:
            return f"{source} 从 {count} 个文件变成 {found[source]} 个"
    for name in BACKUP_CORE_FILES:
        if name in baseline and sizes.get(name, 0) < baseline[name] * BACKUP_CORE_RATIO:
            return f"{name} 从 {baseline[name]} 字节变成 {sizes.get(name, 0)} 字节"
    return ""


def prune_backups(folder):
    """Mark thin archives, then prune each group on its own so a wipe cannot push complete copies out.

    Keeps the newest BACKUP_KEEP complete archives and, for BACKUP_DAYS days, the newest one of each day, so a burst
    of restarts cannot erase yesterday either. Returns {original path: (marked path, reason)} for archives marked now."""
    complete, thin, marked, baseline, baseline_time = [], [], {}, None, 0
    for path in sorted(folder.glob("business-*.zip")):
        if not path.stem.endswith(BACKUP_THIN_MARK):
            modified, sizes = path.stat().st_mtime, archive_sizes(path)
            expired = modified - baseline_time > BACKUP_BASELINE_DAYS * 86400
            reason = "压缩包无法读取" if sizes is None else "" if baseline is None or expired else data_loss(sizes, baseline)
            if not reason:
                complete.append((modified, path))
                baseline, baseline_time = sizes, modified
                continue
            original, path = path, path.replace(path.with_name(path.stem + BACKUP_THIN_MARK + path.suffix))
            marked[original] = (path, reason)
        thin.append(path)
    for old in thin[:-BACKUP_THIN_KEEP]:
        old.unlink()
    cutoff, days = time.time() - BACKUP_DAYS * 86400, set()
    for index, (modified, path) in enumerate(reversed(complete)):
        day = date.fromtimestamp(modified)
        if index < BACKUP_KEEP or modified >= cutoff and day not in days:
            days.add(day)
            continue
        path.unlink()
    return marked


def backup_business_data(root, config):
    """Zip the local-only business records while no service is writing them."""
    entries = []
    for key, names in BACKUP_SOURCES:
        if not config.get(key):
            continue
        base = Path(config[key])
        for name in names:
            entries += [(path, f"{key.removesuffix('_root')}/{path.relative_to(base).as_posix()}") for path in backup_files(base / name)]
    if not entries:
        return None
    folder = root / "archive" / "data-backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"business-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    partial = target.with_suffix(".zip.partial")
    if not write_zip_parallel(partial, entries):
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive:
            for path, name in entries:
                archive.write(path, name)
    partial.replace(target)
    marked = prune_backups(folder)
    if target in marked:
        target, reason = marked[target]
        log_housekeeping(root, f"本次备份比上一份完整备份少了很多（{reason}），已标记为 {target.name}，不占完整备份名额。请检查运行目录的数据。")
    return target


def rotate_server_log(root, name="server.log"):
    """Also used for gateway.log, which had no limit and reached 42 MB by 2026-11-15."""
    log = root / name
    if not log.is_file() or log.stat().st_size < LOG_LIMIT:
        return None
    folder = root / "archive" / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    stem = Path(name).stem
    target = folder / f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}.log"
    log.replace(target)
    prune(folder, f"{stem}-*.log", LOG_KEEP)
    return target


def log_housekeeping(root, message):
    with (root / "launcher-error.log").open("a", encoding="utf-8") as log:
        log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def prepare_start(root, config):
    """Housekeeping must never block the restart; failures go to the error log."""
    steps = (("备份业务数据", lambda: backup_business_data(root, config)), ("切分 server.log", lambda: rotate_server_log(root)))
    for label, step in steps:
        try:
            step()
        except Exception as error:
            log_housekeeping(root, f"{label}失败（不影响启动）：{error}")


def task_host_manifest(root, config):
    """A local control file is never a source of arbitrary hosts, paths or commands."""
    runtime = Path(config["runtime_root"]).resolve()
    path = runtime / "service" / "task-host.json"
    if (path.is_symlink() or path.parent.is_symlink() or
            (path.parent.exists() and bool(getattr(path.parent.lstat(), "st_file_attributes", 0) & 0x400)) or
            not path.resolve().is_relative_to(runtime)):
        raise RuntimeError("后台任务服务登记位置无效，未操作任何进程。")
    if not path.exists():
        return None
    if path.stat().st_nlink > 1:
        raise RuntimeError("后台任务服务登记不能使用文件链接。")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        paths = value["paths"]
        if (value.get("app") != "video-case-workbench" or value.get("role") != "task-host" or
                value.get("protocol_version") != 1 or type(value.get("pid")) is not int or value["pid"] <= 0 or
                type(value.get("port")) is not int or not 1 <= value["port"] <= 65535 or
                value["port"] == config["port"] or
                value.get("public_port") != config["port"] or
                not isinstance(value.get("launch_id"), str) or len(value["launch_id"]) < 16 or
                not isinstance(value.get("proxy_secret"), str) or len(value["proxy_secret"]) < 32 or
                not same_path(paths["records"], Path(config["skill_root"]) / "cases") or
                not same_path(paths["library"], runtime / "library") or
                not same_path(paths["runtime"], runtime) or not same_path(paths["code_root"], root)):
            raise ValueError()
        return value
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError("后台任务服务登记无法核对，未操作任何进程。") from error


def task_host_request(host, path, *, token=None, body=None, timeout=5):
    headers = {"X-Workbench-Gateway": host["proxy_secret"]}
    if token:
        headers["X-Workbench-Token"] = token
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"http://127.0.0.1:{host['port']}" + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None, headers=headers)
    return read_json(request, timeout=timeout)


def task_host_status(root, config, host):
    status = task_host_request(host, "/api/service/status")
    if (not correct_service(status, config) or status.get("role") != "task-host" or
            status.get("protocol_version") != 1 or status.get("pid") != host["pid"] or
            status.get("launch_id") != host["launch_id"]):
        raise RuntimeError("后台任务服务身份已变化，未操作任何进程。")
    return status


def prepare_stopped_engine(root, config):
    """Only a stopped data owner can be copied into a coherent startup backup."""
    from data_backup import BackupManager
    from data_lease import ServerLease
    lease = ServerLease(config["runtime_root"])
    if not lease.acquire():
        raise RuntimeError("后台任务服务仍占用数据，尚未启动第二个实例；请稍后再试。")
    try:
        BackupManager(config["skill_root"], config["runtime_root"], root).recover_interrupted_restores()
        prepare_start(root, config)
    finally:
        lease.close()


def ensure_task_host(root, config, *, locked=False):
    """Keep the live task owner; only a missing/dead host starts another lease holder."""
    host = task_host_manifest(root, config)
    if host:
        try:
            task_host_status(root, config, host)
            return host
        except urllib.error.HTTPError:
            raise  # A responding but unauthenticated service is not a dead host.
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
    if not locked:
        with launch_lock(root, f"task-host:{config['port']}") as acquired:
            if acquired:
                return ensure_task_host(root, config, locked=True)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                current = task_host_manifest(root, config)
                if current:
                    try:
                        task_host_status(root, config, current)
                        return current
                    except (urllib.error.URLError, TimeoutError, ConnectionError):
                        pass
                time.sleep(.2)
            raise RuntimeError("后台任务服务仍在更新，请稍后再打开网页。")
    # Never back up a live writer just because its HTTP port is unresponsive.
    prepare_stopped_engine(root, config)
    with (root / "server.log").open("ab") as log:
        child = subprocess.Popen([sys.executable, "-u", str(root / "server.py"), "--task-host", "--port", "0",
            "--public-port", str(config["port"]), "--skill-root", config["skill_root"],
            "--runtime-root", config["runtime_root"]], cwd=str(root), stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("后台任务服务启动失败，请查看 server.log。")
        current = task_host_manifest(root, config)
        if current and current["pid"] == child.pid:
            try:
                task_host_status(root, config, current)
                return current
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                pass
        time.sleep(.1)
    raise RuntimeError("后台任务服务尚未就绪；未启动第二个实例，请查看 server.log。")


def refresh_task_host_on_idle(root, config):
    """A gateway restart never touches tasks. Update their code only after atomic idle admission."""
    with launch_lock(root, f"task-host:{config['port']}") as acquired:
        if not acquired:
            return
        host = task_host_manifest(root, config)
        if not host:
            return
        while True:
            current = task_host_manifest(root, config)
            if not current or current["launch_id"] != host["launch_id"]:
                return  # Another host already took over; never stop its new work.
            status = task_host_status(root, config, host)
            if not status.get("code_stale"):
                return
            ready = task_host_request(host, "/api/service/ready")
            if ready.get("launch_id") != host["launch_id"] or ready.get("role") != "task-host":
                raise RuntimeError("后台任务服务身份已变化，已停止更新。")
            if ready.get("ready"):
                try:
                    result = task_host_request(host, "/api/service/shutdown", token=status.get("token"), body={})
                    if not result.get("ok") or result.get("pid") != host["pid"]:
                        raise RuntimeError("后台任务服务未确认关闭，未启动新实例。")
                    break
                except urllib.error.HTTPError as error:
                    if error.code != 409:
                        raise
                    # A new job arrived after the read-only probe. The host keeps it and we wait.
            time.sleep(.5)
        wait_for_port_release(host["port"], timeout=30)
        wait_for_task_host_release(config)
        ensure_task_host(root, config, locked=True)


def wait_for_task_host_release(config, timeout=30):
    """Closing the listening socket can precede cleanup and release of the business-data lease."""
    from data_lease import ServerLease
    deadline = time.monotonic() + timeout
    while True:
        lease = ServerLease(config["runtime_root"])
        if lease.acquire():
            lease.close()
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("原任务引擎仍在结束保存，尚未启动新实例；请稍后重试。")
        time.sleep(.1)


class _NoControlRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def local_control_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoControlRedirect())


def request_service_shutdown(url, token, expected_pid=None):
    request = urllib.request.Request(url + "/api/service/shutdown", data=b"{}",
                                     headers={"Content-Type": "application/json", "X-Workbench-Token": token})
    try:
        with local_control_opener().open(request, timeout=10) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            message = json.load(error).get("error")
        except (ValueError, OSError):
            message = None
        raise RuntimeError(message or "服务暂时无法重启，请等待当前操作完成后重试。") from error
    except (TimeoutError, urllib.error.URLError) as error:
        timed_out = isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError)
        reason = "超时" if timed_out else "失败"
        raise RuntimeError(f"等待旧服务关闭{reason}，尚未启动新实例。请稍后重新双击TK工作台。") from error
    if not result.get("ok") or (expected_pid is not None and result.get("pid") != expected_pid):
        raise RuntimeError("服务未确认关闭，尚未启动第二个实例。")


def loopback_listener_pid(port):
    """Use the Windows listener table when a gateway's launch arguments are unavailable."""
    result = subprocess.run(["netstat.exe", "-ano", "-p", "tcp"], capture_output=True,
                            timeout=5, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    address = f"127.0.0.1:{port}".encode("ascii")
    for row in result.stdout.splitlines():
        fields = row.split()
        if len(fields) == 5 and fields[:3] == [b"TCP", address, b"0.0.0.0:0"] and fields[4].isdigit():
            return int(fields[4])
    return None


def shutdown_registered_gateway(root, config, url):
    """Only the actual listener paired with this installation's live task host may be adopted."""
    host = task_host_manifest(root, config)
    if not host:
        return False
    try:
        with local_control_opener().open(url + "/api/service/status", timeout=5) as response:
            body = response.read(65537)
        if len(body) > 65536:
            return False
        status = json.loads(body)
    except (OSError, ValueError):
        return False
    if (not isinstance(status, dict) or status.get("role") != "gateway"
            or type(status.get("pid")) is not int or status["pid"] <= 0 or status["pid"] == host["pid"]
            or status.get("port") != config["port"]
            or not correct_service(status, config) or not status.get("shutdown_supported")
            or status.get("task_host_pid") != host["pid"] or status.get("task_host_launch_id") != host["launch_id"]):
        return False
    if loopback_listener_pid(config["port"]) != status["pid"]:
        return False
    engine = task_host_status(root, config, host)
    token = engine.get("token")
    if not isinstance(token, str) or not token or status.get("token") != token:
        return False
    request_service_shutdown(url, token, expected_pid=status["pid"])
    return True


def restart(root, config, no_browser=False):
    url = f"http://127.0.0.1:{config['port']}"
    processes = matching_processes(root, config)
    if processes:
        # Only the public gateway may restart while its independent task owner is busy.
        gateway = all(any(same_path(argument, root / "service_gateway.py")
                          for argument in windows_argv(process["CommandLine"])[1:]) for process in processes)
        if gateway and not config.get("task_host_enabled"):
            raise RuntimeError("当前工作台使用独立任务引擎，请保持 task_host_enabled 开启后重启。")
        if not gateway:
            ensure_no_active_tasks(config)
        elif config.get("task_host_enabled"):
            # A dead host makes the gateway status return 503. Repair it under the data lease first;
            # the gateway then supplies its own verified PID and token for the normal shutdown handshake.
            ensure_task_host(root, config)
        try:
            status = read_existing_service(url + "/api/service/status", "检查旧服务", timeout=5)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            # Legacy state reads every case and its history. A real library is
            # much slower than the lightweight endpoint used by new versions.
            state = read_existing_service(url + "/api/state", "读取旧版工作台状态", timeout=30)
            if not correct_service(state, config):
                raise RuntimeError("当前端口连接到其他工作台，未结束任何进程。")
            # Compatibility cleanup only for versions without graceful shutdown.
            for process in processes:
                stop_legacy_process(process)
        else:
            if (not correct_service(status, config)
                    or status.get("pid") not in {process["ProcessId"] for process in processes}
                    or not status.get("shutdown_supported")):
                raise RuntimeError("服务身份核对失败，未结束任何进程。")
            if len(processes) != 1:
                raise RuntimeError("检测到新旧服务混合运行，请先关闭旧服务后再打开。")
            token = status.get("token")
            if not token:
                # Compatibility with the first graceful-shutdown release.
                state = read_existing_service(url + "/api/state", "读取旧版工作台状态", timeout=30)
                if not correct_service(state, config):
                    raise RuntimeError("当前端口连接到其他工作台，未结束任何进程。")
                token = state.get("token")
            if not isinstance(token, str) or not token:
                raise RuntimeError("无法核对工作台连接凭据，尚未关闭旧服务。")
            request_service_shutdown(url, token)
            wait_for_process_exit(processes[0])
    elif port_open(config["port"]):
        if not (config.get("task_host_enabled") and shutdown_registered_gateway(root, config, url)):
            raise RuntimeError("工作台端口被其他程序占用，未结束该程序。")
    wait_for_port_release(config["port"])
    if config.get("task_host_enabled"):
        ensure_task_host(root, config)
        command = [sys.executable, "-u", str(root / "service_gateway.py"), "--port", str(config["port"]),
                   "--skill-root", config["skill_root"], "--runtime-root", config["runtime_root"]]
        log_name = "gateway.log"
        try:
            rotate_server_log(root, log_name)  # the old gateway has exited, so nothing holds the file
        except Exception as error:
            log_housekeeping(root, f"切分 gateway.log 失败（不影响启动）：{error}")
    else:
        prepare_stopped_engine(root, config)
        command, log_name = [sys.executable, "-u", str(root / "server.py")], "server.log"
    with (root / log_name).open("ab") as log:
        child = subprocess.Popen(command, cwd=str(root), stdout=log, stderr=log,
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("工作台启动失败，请查看工作台目录中的 server.log。")
        try:
            status = read_json(url + "/api/service/status", timeout=2)
            if correct_service(status, config) and status.get("pid") == child.pid:
                if not no_browser:
                    webbrowser.open(url)
                return child.pid
        except (OSError, ValueError):
            pass
        time.sleep(.05)
    raise RuntimeError("工作台尚未就绪，请查看 server.log 后重试。")


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    config = load_config(ROOT)
    if "--refresh-task-host-on-idle" in args:
        refresh_task_host_on_idle(ROOT, config)
        return 0
    if "--inspect" in args:
        print(json.dumps(matching_processes(ROOT, config), ensure_ascii=True))
        return 0
    with launch_lock(ROOT, config["port"]) as acquired:
        if acquired:
            restart(ROOT, config, no_browser="--no-browser" in args)
    return 0


def desktop_main():
    try:
        return main()
    except Exception as error:
        with (ROOT / "launcher-error.log").open("a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {error}\n{traceback.format_exc()}\n")
        if "--no-browser" not in sys.argv[1:]:
            ctypes.windll.user32.MessageBoxW(0, str(error), "TK工作台", 0x10)
        return 1
