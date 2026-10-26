"""Bundle already-sanitized guest program assets without changing script scopes.

This only writes the staging directory passed by the guest snapshot builder.
Original files remain available for old pages and debugging.
"""
from __future__ import annotations

from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


TAGS = re.compile(r"<script\b[^>]*>[\s\S]*?</script\s*>|<link\b[^>]*>|<style\b[^>]*>[\s\S]*?</style\s*>", re.I)
ASSET = re.compile(r"^/(?:guest-assets/)?([A-Za-z0-9_-]+\.(js|css))$")
BOOTSTRAP = frozenset({"guest-mode.js", "public-mode.js"})


class _Tag(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tag = None
        self.attributes = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        if self.tag is None:
            self.tag, self.attributes = tag, attrs


def _asset(site, value, suffix):
    if not isinstance(value, str):
        return None
    target = urlsplit(value)
    match = ASSET.fullmatch(target.path)
    if target.scheme or target.netloc or not match or match[2] != suffix:
        return None
    file = site / match[1]
    if not file.is_file() or file.resolve().parent != site.resolve():
        raise ValueError("guest_bundle_asset_missing")
    return file


def _classic_loader(sources):
    # Each original file remains a separate classic script. Concatenating them
    # would hoist later declarations into earlier files and change strictness.
    # Guest pages already permit inline scripts in their fixed CSP. These strings
    # contain sanitized, fixed build inputs, never request or business data.
    data = json.dumps(sources, ensure_ascii=False, separators=(",", ":"))
    return """/* Generated from sanitized guest program sources, in document order. */
(async () => {
  const sources = %s;
  for (const [name, source] of sources) {
    const script = document.createElement('script');
    script.dataset.guestSource = name;
    script.textContent = source + '\\n//# sourceURL=/guest-assets/' + name;
    document.head.appendChild(script);
    script.remove();
    // Match the microtask checkpoint between separately loaded classic files.
    // These resolved waits drain before the queued DOMContentLoaded event.
    await Promise.resolve();
  }
})();
""" % data


def _inline_classic(file):
    # Escaping every less-than sign prevents HTML from interpreting an embedded
    # closing script tag (including mixed case) inside a JavaScript string.
    source = json.dumps(file.read_text(encoding="utf-8"), ensure_ascii=False).replace("<", "\\u003c")
    return f'''<script data-guest-bootstrap="{file.name}">
(() => {{
  const source = {source};
  const script = document.createElement('script');
  script.dataset.guestSource = '{file.name}';
  script.textContent = source + '\\n//# sourceURL=/guest-assets/{file.name}';
  document.head.appendChild(script);
  script.remove();
}})();
</script>'''


def bundle_guest_assets(site, page):
    """Return HTML using a few flat guest assets, before version hashes are added.

    The two known synchronous guards execute inline in their original positions,
    without a blocking download. Other synchronous scripts stay unchanged. Deferred classic
    scripts are grouped only while consecutive in script execution order;
    modules, inline scripts and custom script attributes form group boundaries.
    Styles retain cascade order, including boundaries from inline/media styles.
    The caller hashes the generated final bytes and applies guest namespacing.
    """
    site = Path(site)
    tags = []
    for match in TAGS.finditer(page):
        parsed = _Tag(match[0])
        attrs = dict(parsed.attributes)
        duplicate = len(attrs) != len(parsed.attributes)
        kind, file = None, None
        if not duplicate and parsed.tag == "script" and set(attrs) == {"src"}:
            target = urlsplit(attrs["src"] or "")
            if target.path.rsplit("/", 1)[-1] in BOOTSTRAP:
                file = _asset(site, attrs["src"], "js")
                if file is not None:
                    kind = "bootstrap"
        elif not duplicate and parsed.tag == "script" and set(attrs) == {"src", "defer"}:
            file = _asset(site, attrs["src"], "js")
            if file is not None:
                # Source-sensitive scripts must keep their own real URL. There
                # are none in today's guest build, but fail safely if added.
                source = file.read_text(encoding="utf-8")
                if not re.search(r"\bdocument\s*\.\s*(?:currentScript|write|writeln)\b", source):
                    kind = "js"
        elif (not duplicate and parsed.tag == "link" and set(attrs) == {"rel", "href"}
              and str(attrs["rel"] or "").lower() == "stylesheet"):
            file = _asset(site, attrs["href"], "css")
            if file is not None:
                source = file.read_text(encoding="utf-8")
                # Import and namespace directives are scoped to one stylesheet.
                if not re.search(r"@(?:import|namespace)\b", source, flags=re.I):
                    kind = "css"
        tags.append({"match": match, "tag": parsed.tag, "attrs": attrs, "kind": kind, "file": file})

    replacements = {item["match"].start(): (item["match"].end(), _inline_classic(item["file"]))
                    for item in tags if item["kind"] == "bootstrap"}
    for kind in ("js", "css"):
        runs, run = [], []
        for item in tags:
            if item["kind"] == kind:
                run.append(item)
            elif ((kind == "js" and item["tag"] == "script")
                  or (kind == "css" and (item["tag"] == "style" or
                      item["tag"] == "link" and str(item["attrs"].get("rel") or "").lower() == "stylesheet"))):
                if run:
                    runs.append(run)
                    run = []
        if run:
            runs.append(run)
        for number, group in enumerate(runs):
            if len(group) < 2:
                continue
            name = f"guest-{'program' if kind == 'js' else 'styles'}-{number}.{kind}"
            if kind == "js":
                content = _classic_loader([(item["file"].name, item["file"].read_text(encoding="utf-8")) for item in group])
                replacement = f'<script src="/{name}" defer></script>'
            else:
                chunks = []
                for item in group:
                    source = item["file"].read_text(encoding="utf-8").lstrip("\ufeff")
                    source = re.sub(r'^\s*@charset\s+[\"\'][^\"\']*[\"\']\s*;', "", source, count=1, flags=re.I)
                    chunks.append(f"/* {item['file'].name} */\n{source}\n")
                content = "\n".join(chunks)
                replacement = f'<link rel="stylesheet" href="/{name}">'
            (site / name).write_text(content, encoding="utf-8", newline="\n")
            for index, item in enumerate(group):
                replacements[item["match"].start()] = (item["match"].end(), replacement if index == 0 else "")
    for start, (end, replacement) in sorted(replacements.items(), reverse=True):
        page = page[:start] + replacement + page[end:]
    return page
