"""Third-party API keys the workbench uses, managed from Settings (2026-11-09).

Existing service keys use Windows user environment variables (HKCU\\Environment).
Antigravity uses the machine's ignored config.local.json, including on hosts that
cannot write the registry. Features read current values on every call.

The page never gets a key back: status() reports only whether it is set and its last four
characters. Saving and checking are allowed only from this computer (see server.py).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from app_config import save_local_config

KEY_CONFIG_ROOT = Path(__file__).resolve().parent

KEYS = {
    "APIFY_TOKEN": {"label": "Apify", "used_for": "用户声音 → 从 Apify 抓评论",
                    "where": "console.apify.com → Settings → API & Integrations", "url": "https://console.apify.com/settings/integrations",
                    "prefix": "apify_api_"},
    "ELEVENLABS_API_KEY": {"label": "ElevenLabs", "used_for": "制作视频的配音与字幕对齐",
                           "where": "elevenlabs.io → Developers → API Keys", "url": "https://elevenlabs.io/app/settings/api-keys",
                           "prefix": "sk_"},
    "ANTIGRAVITY_API_KEY": {"label": "Antigravity", "used_for": "本地 API 写稿、审稿与文字分析",
                            "where": "Antigravity Tools → API 服务", "url": "http://127.0.0.1:8046",
                            "prefix": "sk-"},
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


def _local_key():
    """An explicit empty local value also suppresses stale environment credentials."""
    path = KEY_CONFIG_ROOT / "config.local.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
        if not isinstance(data, dict):
            raise ValueError()
        value = data.get("antigravity_api_key")
        if value is not None and not isinstance(value, str):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise KeyError_(500, "Antigravity 本机密钥配置无法读取，请重新保存。") from None


def read(name, *, config=None):
    """Read saved changes immediately; preserve manually configured environment keys."""
    if name == "ANTIGRAVITY_API_KEY":
        value = _local_key() if config is None else config.get("antigravity_api_key")
        if value is not None and not isinstance(value, str):
            raise KeyError_(500, "Antigravity 本机密钥配置无法读取，请重新保存。")
        if value is not None:
            return value.strip()
    return str(_registry(name) or os.environ.get(name) or "").strip()


def _broadcast():
    """Tell Windows the environment changed, so newly opened programs see the key too. Best effort."""
    try:
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 2000, None)
    except Exception:
        pass


def _write(name, value):
    """Persist a local service credential or a native user environment variable."""
    if name == "ANTIGRAVITY_API_KEY":
        try:
            save_local_config({"antigravity_api_key": value or ""}, KEY_CONFIG_ROOT)
            return
        except (OSError, ValueError):
            raise KeyError_(500, "Antigravity 本机密钥配置无法保存。") from None
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
                     "storage": "本机私有配置" if name == "ANTIGRAVITY_API_KEY" else "Windows 用户环境变量",
                     "set": bool(value), "tail": value[-4:] if len(value) >= 12 else ("" if not value else "…"),
                     "looks_right": bool(value) and value.startswith(spec["prefix"])})
    return {"keys": rows, "storage": "本机私有配置与 Windows 用户环境变量"}


def validate(name, value):
    """Validate a credential without persisting partial service settings."""
    spec = _spec(name)
    value = str(value or "").strip()
    if not value:
        raise KeyError_(400, "先粘贴密钥。")
    if len(value) > 512 or re.search(r"\s", value):
        raise KeyError_(400, "密钥里不能有空格或换行，请只复制密钥本身。")
    if not value.startswith(spec["prefix"]):
        raise KeyError_(400, f"{spec['label']} 的密钥应以 {spec['prefix']} 开头，可能复制成了密钥 ID 或别的内容。")
    return value


def save(body):
    name = str((body or {}).get("name") or "")
    value = validate(name, (body or {}).get("value"))
    _write(name, value)
    os.environ[name] = value
    if name != "ANTIGRAVITY_API_KEY":
        _broadcast()
    return status()


def remove(body):
    name = str((body or {}).get("name") or "")
    _spec(name)
    _write(name, None)
    os.environ.pop(name, None)
    if name != "ANTIGRAVITY_API_KEY":
        _broadcast()
    return status()
