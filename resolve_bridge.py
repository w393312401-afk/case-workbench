"""Run resolve_worker.py one operation at a time; DaVinci Resolve is a single shared application."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from codex_bridge import BridgeCancelled
from elevenlabs_client import VoiceError

WORKER = Path(__file__).resolve().parent / "resolve_worker.py"
MCP_FILES = (r"D:\DaVinci Resolve\ResolveMCP.exe", r"C:\Program Files\Blackmagic Design\DaVinci Resolve\ResolveMCP.exe")


def render_recovery_message(export_error, recovery_error):
    """A fresh recovery attempt can report the same failure the export worker already returned."""
    initial, retry = str(export_error), str(recovery_error)
    if initial.endswith(retry):
        return initial
    return f"{initial.rstrip('。')}。再次恢复时，{retry}"


def locate_mcp():
    """DaVinci Resolve's own MCP server, used by the editing task."""
    found = next((path for path in (os.environ.get("RESOLVE_MCP"), *MCP_FILES) if path and Path(path).is_file()), None)
    if not found:
        raise VoiceError(503, "没有找到达芬奇自带的 MCP 服务（ResolveMCP.exe），无法发起 AI 剪辑。")
    return found


def resolve_process_running(image="resolve.exe"):
    """Whether Resolve.exe (or another image name) exists, from the Windows process snapshot; None when that cannot be read.

    Asking the scripting API while DaVinci is closed waits about four seconds before giving up.
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel32.Process32FirstW.argtypes = kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == wintypes.HANDLE(-1).value:
            return None
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while found:
                if entry.szExeFile.lower() == image:
                    return True
                found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            return False
        finally:
            kernel32.CloseHandle(snapshot)
    except (OSError, AttributeError):
        return None


def resolve_window_ready(image="resolve.exe"):
    """Whether Resolve's main window is up: its title then reads "DaVinci Resolve ... - <project>"."""
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes

    try:
        user32, kernel32 = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        user32.IsWindowVisible.argtypes = user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _):
            if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd):
                title = ctypes.create_unicode_buffer(300)
                user32.GetWindowTextW(hwnd, title, 300)
                if title.value.startswith("DaVinci Resolve") and " - " in title.value:
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
                    if handle:
                        try:
                            path, size = ctypes.create_unicode_buffer(520), wintypes.DWORD(520)
                            if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)) and path.value.lower().endswith("\\" + image):
                                found.append(hwnd)
                        finally:
                            kernel32.CloseHandle(handle)
            return True

        user32.EnumWindows(visit, 0)
        return bool(found)
    except (OSError, AttributeError, ctypes.ArgumentError):
        return True


def focus_resolve_window(image="resolve.exe"):
    """Bring the main Resolve window to the foreground; best effort, returns whether a window was raised."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    try:
        user32, kernel32 = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        user32.GetWindowTextLengthW.argtypes = user32.IsWindowVisible.argtypes = user32.IsIconic.argtypes = (wintypes.HWND,)
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.SetForegroundWindow.argtypes = user32.BringWindowToTop.argtypes = (wintypes.HWND,)
        user32.keybd_event.argtypes = (wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t)
        found = []

        def is_resolve(pid):
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                buffer, size = ctypes.create_unicode_buffer(520), wintypes.DWORD(520)
                return bool(kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))) and buffer.value.lower().endswith("\\" + image)
            finally:
                kernel32.CloseHandle(handle)

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit(hwnd, _):
            if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4) and user32.GetWindowTextLengthW(hwnd):  # GW_OWNER
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if is_resolve(pid.value):
                    found.append(hwnd)
            return True

        user32.EnumWindows(visit, 0)
        if not found:
            return False
        hwnd = found[0]
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # A tap on Alt lets a background process take the foreground.
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.BringWindowToTop(hwnd)
        return bool(user32.SetForegroundWindow(hwnd))
    except (OSError, AttributeError, ctypes.ArgumentError):
        return False


def open_resolve():
    """Start DaVinci Resolve detached from the workbench; False when its executable cannot be found or started."""
    folders = [Path(path).parent for path in (os.environ.get("RESOLVE_MCP"), *MCP_FILES) if path]
    exe = next((folder / "Resolve.exe" for folder in folders if (folder / "Resolve.exe").is_file()), None)
    if exe is None or os.name != "nt":
        return False
    try:
        subprocess.Popen([str(exe)], cwd=str(exe.parent), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    except OSError:
        return False
    return True


LAUNCH_WAIT = 180  # seconds Resolve gets to start and accept scripting calls
LAUNCH_POLL = 3
LAUNCH_QUIET = 60  # seconds without scripting calls while waiting for the main window, in case its title never shows


class ResolveBridge:
    def __init__(self, *, runner=subprocess.run, python=sys.executable, worker=WORKER, probe=None, launch=None, launch_wait=LAUNCH_WAIT, window_ready=None):
        self.runner, self.python, self.worker = runner, python, worker
        # A substituted runner (tests, previews) has no real DaVinci process to look for or to start.
        self.probe = probe or (resolve_process_running if runner is subprocess.run else (lambda: None))
        self.window_ready = window_ready or (resolve_window_ready if runner is subprocess.run else (lambda: True))
        self.launch = launch if launch is not None else (open_resolve if runner is subprocess.run else None)
        self.launch_wait = launch_wait
        self.lock = threading.Lock()
        self._access_lock = threading.Lock()
        self._access_waiters = []

    def busy(self):
        return self.lock.locked()

    def closed(self):
        """True only when DaVinci is known not to be running."""
        return self.probe() is False

    @contextmanager
    def access(self, *, cancel_event=None, timeout=150 * 60, on_wait=None, ticket=None, ahead_of=()):
        """Queue background tasks for one cancellable, exclusive Resolve operation.

        Take the timeline/playhead snapshot only after admission, and restore it
        before leaving this context. Non-Resolve work belongs outside the lease.
        `ticket` names this request so a later one can go ahead of it; such a
        request passes those tickets as `ahead_of` and is queued just before the
        first of them still waiting, or last when none is.
        """
        cancel = cancel_event if cancel_event is not None else threading.Event()
        ticket, acquired, notified = ticket if ticket is not None else object(), False, False
        deadline = time.monotonic() + timeout
        with self._access_lock:
            ahead = [index for index, waiting in enumerate(self._access_waiters) if any(waiting is item for item in ahead_of)]
            self._access_waiters.insert(ahead[0] if ahead else len(self._access_waiters), ticket)
        try:
            while not acquired:
                if cancel.is_set():
                    raise BridgeCancelled("已按你的要求停止等待达芬奇。")
                if time.monotonic() >= deadline:
                    raise VoiceError(504, "等待达芬奇超时，本次任务已结束；尚未开始操作达芬奇。")
                with self._access_lock:
                    if self._access_waiters[0] is ticket:
                        acquired = self.lock.acquire(blocking=False)
                        if acquired:
                            self._access_waiters.pop(0)
                if not acquired:
                    if not notified and on_wait is not None:
                        on_wait()
                        notified = True
                    cancel.wait(.1)
            if cancel.is_set():
                raise BridgeCancelled("已按你的要求停止等待达芬奇。")
            yield
        finally:
            if acquired:
                self.lock.release()
            with self._access_lock:
                if ticket in self._access_waiters:
                    self._access_waiters.remove(ticket)

    def _call(self, command, payload, timeout):
        try:
            completed = self.runner([self.python, "-X", "utf8", "-I", str(self.worker)],
                                    input=json.dumps({**(payload or {}), "command": command}, ensure_ascii=False).encode("utf-8"),
                                    capture_output=True, timeout=timeout,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired as exc:
            raise VoiceError(504, "达芬奇长时间没有响应，本次操作已停止。请到达芬奇里核对时间线是否已经建好，再决定是否重试。") from exc
        except OSError as exc:
            raise VoiceError(503, "无法启动达芬奇操作进程。") from exc
        try:
            return json.loads(completed.stdout.decode("utf-8"))
        except (ValueError, UnicodeError, AttributeError) as exc:
            raise VoiceError(502, "达芬奇操作没有返回可读结果，请到达芬奇里核对。") from exc

    def _open_if_closed(self):
        """Start DaVinci when it is known not to be running, then wait until it accepts scripting calls."""
        if self.launch is None or self.probe() is not False:
            return
        try:  # The process list can miss a Resolve that is running; one answered call proves it is.
            if self._call("ping", None, 30).get("ok"):
                return
        except VoiceError:
            pass
        if not self.launch():
            raise VoiceError(503, "达芬奇没有打开，也没有找到 Resolve.exe，请手动打开 DaVinci Resolve 后重试。")
        started = time.monotonic()
        deadline = started + self.launch_wait
        while time.monotonic() < deadline:
            time.sleep(LAUNCH_POLL)
            if self.probe() is False:
                continue
            # A scripting call while Resolve starts up can break its script server until the next restart.
            if time.monotonic() < started + LAUNCH_QUIET and not self.window_ready():
                continue
            try:
                if self._call("ping", None, 30).get("ok"):
                    return
            except VoiceError:
                continue
        raise VoiceError(504, "已替你打开达芬奇，但等了 3 分钟还没连上。请确认它已启动，并在偏好设置 → 系统 → 常规里把“外部脚本使用”设为“本地”。")

    def run(self, command, payload=None, timeout=180, held=False):
        """`held` means the caller already holds the lock, as a running editing task does for its whole run."""
        if not held and not self.lock.acquire(blocking=False):
            raise VoiceError(409, "达芬奇正在执行另一项工作台操作（可能是 AI 剪辑），请完成后再试。")
        try:
            self._open_if_closed()
            result = self._call(command, payload, timeout)
        finally:
            if not held:
                self.lock.release()
        if not result.get("ok"):
            raise VoiceError(409, result.get("error") or "达芬奇操作没有完成。")
        if command == "open" and self.runner is subprocess.run:
            focus_resolve_window()
        return result
