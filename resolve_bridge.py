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


class ResolveBridge:
    def __init__(self, *, runner=subprocess.run, python=sys.executable, worker=WORKER, probe=None):
        self.runner, self.python, self.worker = runner, python, worker
        # A substituted runner (tests, previews) has no real DaVinci process to look for.
        self.probe = probe or (resolve_process_running if runner is subprocess.run else (lambda: None))
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

    def run(self, command, payload=None, timeout=180, held=False):
        """`held` means the caller already holds the lock, as a running editing task does for its whole run."""
        if not held and not self.lock.acquire(blocking=False):
            raise VoiceError(409, "达芬奇正在执行另一项工作台操作（可能是 AI 剪辑），请完成后再试。")
        try:
            completed = self.runner([self.python, "-X", "utf8", "-I", str(self.worker)],
                                    input=json.dumps({**(payload or {}), "command": command}, ensure_ascii=False).encode("utf-8"),
                                    capture_output=True, timeout=timeout,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired as exc:
            raise VoiceError(504, "达芬奇长时间没有响应，本次操作已停止。请到达芬奇里核对时间线是否已经建好，再决定是否重试。") from exc
        except OSError as exc:
            raise VoiceError(503, "无法启动达芬奇操作进程。") from exc
        finally:
            if not held:
                self.lock.release()
        try:
            result = json.loads(completed.stdout.decode("utf-8"))
        except (ValueError, UnicodeError, AttributeError) as exc:
            raise VoiceError(502, "达芬奇操作没有返回可读结果，请到达芬奇里核对。") from exc
        if not result.get("ok"):
            raise VoiceError(409, result.get("error") or "达芬奇操作没有完成。")
        if command == "open" and self.runner is subprocess.run:
            focus_resolve_window()
        return result
