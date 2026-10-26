"""Portable defaults with private, machine-specific overrides."""
from __future__ import annotations

import json
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent


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

    for name in ("skill_root", "runtime_root"):
        config[name] = absolute(config[name])
    config["performance_sources"] = [absolute(path) for path in config["performance_sources"]]
    if config.get("image_generation"):
        config["image_generation"] = dict(config["image_generation"])
        for name in ("parameters_file", "auth_config"):
            if config["image_generation"].get(name):
                config["image_generation"][name] = absolute(config["image_generation"][name])
    return config
