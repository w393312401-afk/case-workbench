"""Serve versioned program bundles while preserving separate classic script scopes."""
from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import dataclass
import gzip
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re


TAGS = re.compile(r"<script\b[^>]*>[\s\S]*?</script\s*>|<link\b[^>]*>|<style\b[^>]*>[\s\S]*?</style\s*>", re.I)
ASSET = re.compile(r"^/([A-Za-z0-9_-]+\.(js|css))$")
REFERENCES = re.compile(r'((?:src|href)="/)([A-Za-z0-9_.-]+\.(?:js|css|svg))(")')
BOOTSTRAP = """(() => {
  const state = fetch('/api/state');
  state.catch(() => {});
  window.WorkbenchBootstrap = {state};
})();"""


def script_hash(source):
    return "'sha256-" + base64.b64encode(hashlib.sha256(source.encode("utf-8")).digest()).decode("ascii") + "'"


@dataclass(frozen=True)
class Resource:
    name: str
    raw: bytes
    packed: bytes | None
    version: str

    @classmethod
    def build(cls, name, source):
        raw = source.encode("utf-8") if isinstance(source, str) else source
        packed = gzip.compress(raw, compresslevel=5, mtime=0) if len(raw) >= 1024 else None
        if packed is not None and len(packed) >= len(raw):
            packed = None
        return cls(name, raw, packed, hashlib.sha256(raw).hexdigest()[:12])


@dataclass(frozen=True)
class Snapshot:
    index: Resource
    resources: dict[str, Resource]
    script_hashes: tuple[str, ...]


class Tag(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tag, self.attributes = None, []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        if self.tag is None:
            self.tag, self.attributes = tag, attrs


def build_snapshot(folder):
    folder = Path(folder)
    page = (folder / "index.html").read_text(encoding="utf-8")
    tags, resources, hashes = [], {}, []
    for match in TAGS.finditer(page):
        tag = Tag(match[0])
        attrs = dict(tag.attributes)
        kind, source, name = None, None, None
        if len(attrs) == len(tag.attributes):
            candidate = ASSET.fullmatch(attrs.get("src") or attrs.get("href") or "")
            if candidate:
                name, suffix = candidate.groups()
                source = (folder / name).read_text(encoding="utf-8")
                if (tag.tag == "script" and set(attrs) == {"src", "defer"} and suffix == "js"
                        and not re.search(r"\bdocument\s*\.\s*(?:currentScript|write|writeln)\b", source)):
                    kind = "js"
                elif (tag.tag == "link" and set(attrs) == {"rel", "href"}
                      and attrs["rel"].lower() == "stylesheet" and suffix == "css"
                      and not re.search(r"@(?:import|namespace)\b", source, re.I)):
                    kind = "css"
        tags.append((match, tag.tag, attrs, kind, name, source))

    replacements = {}
    for kind in ("js", "css"):
        runs, run = [], []
        for item in tags:
            if item[3] == kind:
                run.append(item)
            elif (kind == "js" and item[1] == "script" or kind == "css" and
                  (item[1] == "style" or item[1] == "link" and str(item[2].get("rel", "")).lower() == "stylesheet")):
                if run:
                    runs.append(run)
                    run = []
        if run:
            runs.append(run)
        for number, group in enumerate(runs):
            if len(group) < 2:
                continue
            name = f"workbench-{'program' if kind == 'js' else 'styles'}-{number}.{kind}"
            if kind == "js":
                units = [(item[4], item[5] + "\n//# sourceURL=/" + item[4]) for item in group]
                hashes.extend(script_hash(source) for _, source in units)
                # Concatenating files changes function hoisting and strictness. Each
                # source stays a separate classic script, authorized by its exact CSP hash.
                data = json.dumps(units, ensure_ascii=False, separators=(",", ":"))
                source = """(async () => {
  const sources = %s;
  for (const [name, source] of sources) {
    const script = document.createElement('script');
    script.dataset.workbenchSource = name;
    script.textContent = source;
    document.head.appendChild(script);
    script.remove();
    await Promise.resolve();
  }
})();
""" % data
            else:
                chunks = []
                for item in group:
                    text = re.sub(r'^\s*@charset\s+[\"\'][^\"\']*[\"\']\s*;', "", item[5].lstrip("\ufeff"), count=1, flags=re.I)
                    chunks.append(f"/* {item[4]} */\n{text}\n")
                source = "\n".join(chunks)
            resource = Resource.build(name, source)
            resources[name] = resource
            url = f"/{name}?v={resource.version}"
            replacement = f'<script src="{url}" defer></script>' if kind == "js" else f'<link rel="stylesheet" href="{url}">'
            for index, item in enumerate(group):
                replacements[item[0].start()] = (item[0].end(), replacement if index == 0 else "")
    for start, (end, replacement) in sorted(replacements.items(), reverse=True):
        page = page[:start] + replacement + page[end:]
    if any(item[4] == "app.js" for item in tags):
        hashes.append(script_hash(BOOTSTRAP))
        page = page.replace("<head>", f"<head>\n<script>{BOOTSTRAP}</script>", 1)

    def stamp(match):
        digest = hashlib.sha256((folder / match[2]).read_bytes()).hexdigest()[:12]
        return f"{match[1]}{match[2]}?v={digest}{match[3]}"

    page = REFERENCES.sub(stamp, page)
    return Snapshot(Resource.build("index.html", page), resources, tuple(dict.fromkeys(hashes)))


class ProgramAssets:
    """Called under the server's static lock; retain in-flight pages across edits."""
    def __init__(self, keep=4):
        self.keep = keep
        self.snapshots = OrderedDict()

    def load(self, folder, signature):
        key = (str(folder), signature)
        if key not in self.snapshots:
            self.snapshots[key] = build_snapshot(folder)
            while len(self.snapshots) > self.keep:
                self.snapshots.popitem(last=False)
        return self.snapshots[key]

    def find(self, name, version):
        for snapshot in reversed(self.snapshots.values()):
            resource = snapshot.resources.get(name)
            if resource and (version is None or resource.version == version):
                return resource
        return None
