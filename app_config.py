"""Portable defaults with private, machine-specific overrides."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.request
import webbrowser
from http.client import HTTPException
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent
_LOCAL_CONFIG_LOCK = threading.RLock()


def keep_full_speed():
    """Ask Windows not to slow this process down as background work.

    A windowless process counts as background: on a CPU with performance and efficiency cores its threads can be
    moved to the efficiency cores at a reduced clock, and every request then costs several times the CPU time.
    The service answers a person waiting on a page, so it opts out of that throttling. Harmless where unsupported.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class State(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetProcessInformation.restype = wintypes.BOOL
        # ProcessPowerThrottling: take control of EXECUTION_SPEED and leave it off, i.e. never throttled.
        state = State(1, 1, 0)
        return bool(kernel.SetProcessInformation(kernel.GetCurrentProcess(), 4, ctypes.byref(state), ctypes.sizeof(state)))
    except (OSError, AttributeError):
        return False


def load_config(root=APP_ROOT):
    """Resolve paths relative to the application, independent of the caller's cwd."""
    root = Path(root).resolve()
    config = {}
    for name in ("config.json", "config.local.json"):
        path = root / name
        if not path.is_file():
            continue
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError(f"{name} must contain a JSON object")
        config.update(value)
    config.setdefault("skill_root", ".local/case-library")
    config.setdefault("runtime_root", ".local/runtime")
    config.setdefault("port", 8767)
    config.setdefault("performance_sources", [])

    def absolute(value):
        path = Path(value).expanduser()
        return str((path if path.is_absolute() else root / path).resolve())

    for name in ("skill_root", "runtime_root", "skill_code_root"):
        if config.get(name):
            config[name] = absolute(config[name])
    config["performance_sources"] = [absolute(path) for path in config["performance_sources"]]
    # Keep the old spelling: resolving a junction here would erase the alias
    # that stored historical asset paths need in order to be recognized.
    config["legacy_runtime_roots"] = [os.path.abspath(str(Path(path).expanduser() if Path(path).expanduser().is_absolute()
                                          else root / Path(path).expanduser()))
                                      for path in config.get("legacy_runtime_roots", [])]
    return config


def save_local_config(updates, root=APP_ROOT):
    """Atomically update machine settings while preserving unrelated local options."""
    root = Path(root).resolve()
    path = root / "config.local.json"
    with _LOCAL_CONFIG_LOCK:
        current = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
        if not isinstance(current, dict):
            raise ValueError("config.local.json must contain a JSON object")
        current.update(updates)
        descriptor, temporary = tempfile.mkstemp(prefix=".config-local-", suffix=".tmp", dir=root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(current, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def open_edit_import():
    """Both editing launchers open the configured port when the workbench is ready."""
    url = f"http://127.0.0.1:{load_config()['port']}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with opener.open(url + "/api/service/status", timeout=2) as response:
                status = json.load(response)
            if isinstance(status, dict) and status.get("app") == "video-case-workbench":
                webbrowser.open(url + "/#production/import")
                return
        except (OSError, HTTPException, ValueError):
            pass
        time.sleep(1)
