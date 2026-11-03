"""Portable defaults with private, machine-specific overrides."""
from __future__ import annotations

import json
import os
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
