"""Show a requested window on the workbench's Windows desktop."""
import ctypes
from ctypes import wintypes
import os
import subprocess
import time


def foreground_window(hwnd):
    """Restore a minimized window and check that it actually became active."""
    if not hwnd or os.name != "nt":
        return False
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.IsIconic.argtypes = user.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user.ShowWindowAsync.argtypes = (wintypes.HWND, ctypes.c_int)
    user.keybd_event.argtypes = (wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t)

    def active():
        return user.GetForegroundWindow() == hwnd and not user.IsIconic(hwnd)

    if active():
        return True
    if user.IsIconic(hwnd):
        user.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
    user.SetForegroundWindow(hwnd)
    for attempt in range(20):
        if active():
            return True
        if attempt == 3:
            # A background HTTP handler did not receive the browser's click.
            if not user.GetAsyncKeyState(0x12) & 0x8000:
                user.keybd_event(0x12, 0, 0, 0)
                user.keybd_event(0x12, 0, 2, 0)
            user.SetForegroundWindow(hwnd)
        time.sleep(.05)
    return active()


_SELECT_FILE = r"""
$ErrorActionPreference = 'Stop'
$file = Get-Item -LiteralPath $env:WORKBENCH_REVEAL_FILE
$shell = New-Object -ComObject Shell.Application
$shell.Explore($file.DirectoryName)
$deadline = [DateTime]::UtcNow.AddSeconds(6)
do {
    foreach ($window in $shell.Windows()) {
        try {
            $view = $window.Document
            if ($view.Folder.Self.Path -ne $file.DirectoryName) { continue }
            $item = $view.Folder.ParseName($file.Name)
            if ($null -eq $item) { continue }
            # Select, deselect other items, scroll into view and focus the file.
            $view.SelectItem($item, 29)
            if ($view.SelectedItems().Item(0).Path -eq $file.FullName) {
                [Console]::WriteLine($window.HWND)
                exit 0
            }
        } catch {
            # Explorer may still be navigating or the window may have closed.
        }
    }
    Start-Sleep -Milliseconds 100
} while ([DateTime]::UtcNow -lt $deadline)
exit 1
"""


def reveal_file(path):
    """Select the exact file and activate the Explorer window that reported it."""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command", _SELECT_FILE],
            env={**os.environ, "WORKBENCH_REVEAL_FILE": str(path.absolute())},
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode or not result.stdout.strip().isdigit():
            return False
        return foreground_window(int(result.stdout.strip()))
    except (OSError, subprocess.TimeoutExpired):
        return False
