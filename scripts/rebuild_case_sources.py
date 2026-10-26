#!/usr/bin/env python3
"""Index source documents without evaluating their script style or performance."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

from case_store import StoreError, require
from import_legacy_cases import frontmatter

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "sources" / "video-cases"


def scan(root: Path, exclude=()):
    root = root.resolve()
    cases = {}
    for path in sorted(root.rglob("*.md")):
        if path.name in {"INDEX.md", "case_template.md"}:
            continue
        require(not path.is_symlink() and path.resolve().is_relative_to(root), "Unsafe source path")
        text = path.read_text(encoding="utf-8-sig")
        if not text.startswith("---"):
            continue
        metadata, _ = frontmatter(text)
        identifier = metadata.get("library_id")
        if not identifier:
            continue
        require(re.fullmatch(r"VSL-(?:LT|T[0-4])-\d{3}", identifier) is not None, f"Invalid source identity: {path}")
        require(identifier not in cases, f"Duplicate source identity: {identifier}")
        if identifier in exclude:
            continue
        cases[identifier] = {"file": path.relative_to(root).as_posix(),
                             **{key: metadata.get(key) for key in ("title", "short_name", "tier", "category", "duration_sec", "skeleton_id", "transcript_status")},
                             "archived": "_archive" in path.relative_to(root).parts}
    return cases


def rendered(cases):
    lines = ["# 完整视频案例来源", "", "仅用于定位来源。有效稿、生命周期、指标和评价以 PVR 当前记录为准；历史分级不证明市场效果。", "",
             "| 来源编号 | 文档 | 历史归档 |", "|---|---|---|"]
    for identifier, item in sorted(cases.items()):
        label = str(item.get("title") or identifier).replace("|", "\\|")
        lines.append(f"| {identifier} | [{label}]({item['file']}) | {'是' if item['archived'] else '否'} |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        cases = scan(args.root)
        index = args.root / "index.json"
        markdown = args.root / "INDEX.md"
        if args.check:
            require(json.loads(index.read_text(encoding="utf-8"))["cases"] == cases, "Source index is stale")
            require(markdown.read_text(encoding="utf-8") == rendered(cases), "Source reading index is stale")
        else:
            index.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "cases": cases}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            markdown.write_text(rendered(cases), encoding="utf-8")
        print(json.dumps({"ok": True, "source_count": len(cases), "checked": args.check}, ensure_ascii=False))
    except (OSError, ValueError, StoreError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
