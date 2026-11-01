#!/usr/bin/env python3
"""Report code files missing from docs/项目地图.md and map entries whose file is gone."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = Path("docs") / "项目地图.md"
ENTRY = re.compile(r"`([^`\s]+\.(?:py|pyw|js|css))`")
PLACEHOLDER = re.compile(r"xxx")


def code_files(root):
    """Map each mapped name to its file: root code, front-end assets and scripts."""
    files = [*root.glob("*.py"), *root.glob("*.pyw"), *(root / "static").glob("*.js"), *(root / "static").glob("*.css")]
    names = {path.name for path in files}
    names.update(f"scripts/{path.name}" for path in (root / "scripts").glob("*.py"))
    return names


def check(root=ROOT):
    """Return (code files absent from the map, map entries with no file)."""
    entries = {name for name in ENTRY.findall((Path(root) / MAP_PATH).read_text(encoding="utf-8"))
               if not PLACEHOLDER.search(name)}
    names = code_files(Path(root))
    return sorted(names - entries), sorted(entries - names)


def main():
    missing, stale = check()
    for name in missing:
        print(f"地图漏记：{name}")
    for name in stale:
        print(f"文件已不存在：{name}")
    if missing or stale:
        print(f"请更新 {MAP_PATH.as_posix()}")
        return 1
    print("项目地图与代码一致。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
