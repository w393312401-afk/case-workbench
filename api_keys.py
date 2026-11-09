"""Third-party API keys the workbench uses, managed from Settings (2026-11-09).

Every key lives in one place: a Windows user environment variable (HKCU\\Environment). The
features read it from there on every call (read()), so a key saved on the Settings page works at
once without a restart, and a key set by hand the old way keeps working.

The page never gets a key back: status() reports only whether it is set and its last four
characters. Saving and checking are allowed only from this computer (see server.py).
"""
from __future__ import annotations

import os
import re

KEYS = {
    "APIFY_TOKEN": {"label": "Apify", "used_for": "用户声音 → 从 Apify 抓评论",
                    "where": "console.apify.com → Settings → API & Integrations", "url": "https://console.apify.com/settings/integrations",
                    "prefix": "apify_api_"},
    "ELEVENLABS_API_KEY": {"label": "ElevenLabs", "used_for": "制作视频的配音与字幕对齐",
                           "where": "elevenlabs.io → Developers → API Keys", "url": "https://elevenlabs.io/app/settings/api-keys",
                           "prefix": "sk_"},
}


class KeyError_(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _registry(name):
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            return winreg.QueryValueEx(handle, name)[0]
    except OSError:
        return None


def read(name):
    """The current value: the user environment variable first (changes made since start), then the process environment."""
    return str(_registry(name) or os.environ.get(name) or "").strip()


def _broadcast():
    """Tell Windows the environment changed, so newly opened programs see the key too. Best effort."""
    try:
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 2000, None)
    except Exception:
        pass


def _write(name, value):
    """Set (value) or delete (None) the user environment variable."""
    if os.name != "nt":
        raise KeyError_(501, "只有 Windows 上能在页面保存密钥，请手动设置环境变量。")
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as handle:
            if value is None:
                try:
                    winreg.DeleteValue(handle, name)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, value)
    except OSError as error:
        raise KeyError_(500, f"改不了用户环境变量（{type(error).__name__}）。") from None


def _spec(name):
    if name not in KEYS:
        raise KeyError_(400, "不认识的密钥。")
    return KEYS[name]


def status():
    rows = []
    for name, spec in KEYS.items():
        value = read(name)
        rows.append({"name": name, **{key: spec[key] for key in ("label", "used_for", "where", "url", "prefix")},
                     "set": bool(value), "tail": value[-4:] if len(value) >= 12 else ("" if not value else "…"),
                     "looks_right": bool(value) and value.startswith(spec["prefix"])})
    return {"keys": rows, "storage": "Windows 用户环境变量"}


def save(body):
    name = str((body or {}).get("name") or "")
    spec = _spec(name)
    value = str((body or {}).get("value") or "").strip()
    if not value:
        raise KeyError_(400, "先粘贴密钥。")
    if len(value) > 512 or re.search(r"\s", value):
        raise KeyError_(400, "密钥里不能有空格或换行，请只复制密钥本身。")
    if not value.startswith(spec["prefix"]):
        raise KeyError_(400, f"{spec['label']} 的密钥应以 {spec['prefix']} 开头，可能复制成了密钥 ID 或别的内容。")
    _write(name, value)
    os.environ[name] = value
    _broadcast()
    return status()


def remove(body):
    name = str((body or {}).get("name") or "")
    _spec(name)
    _write(name, None)
    os.environ.pop(name, None)
    _broadcast()
    return status()
