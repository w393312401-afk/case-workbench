"""Maintain the same-domain guest's sanitized, read-only snapshot.

Only the private remote-access/guest directory is written. This module imports
the exporter as an independent module; the retired Pages site is never changed
or published. The exporter reads business data using GET requests only.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_config import load_config
BASE = "http://127.0.0.1:8767"
STATES = frozenset({"starting", "waiting_local_service", "pending", "building",
                    "ready", "scan_failed", "build_failed", "source_changed", "stopped"})
# Unlike a serialized-JSON scan, this excludes JavaScript's backslash matching
# expressions while still identifying literal drive paths, UNC hosts and URIs.
SOURCE_PATH = re.compile(r"(?i)(?:(?<![A-Za-z0-9])[a-z]:[\\/]+(?=[A-Za-z0-9_\u0080-\uffff])"
                         r"|file://|\\{2,}[A-Za-z0-9][A-Za-z0-9._-]*[\\/]+(?=[A-Za-z0-9_\u0080-\uffff]))")


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # dataclasses consult sys.modules while the sync module is initialized.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


SYNC = load_module(Path(__file__).with_name("sync_text_site.py"), "_workbench_guest_sync")
MEDIA = load_module(Path(__file__).with_name("guest_media_snapshot.py"), "_workbench_guest_media")
ASSETS = load_module(Path(__file__).with_name("guest_assets.py"), "_workbench_guest_assets")


def validate_base(value):
    url = urlsplit(value)
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username is not None or url.password is not None
            or url.path not in {"", "/"} or url.query or url.fragment):
        raise ValueError("configuration_invalid")
    if not url.port or not 1 <= url.port <= 65535:
        raise ValueError("configuration_invalid")
    return value.rstrip("/")


def linked(path):
    path = Path(path)
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def guest_directory(root=ROOT):
    config = load_config(root)
    runtime = Path(config["runtime_root"])
    if not runtime.is_absolute():
        raise ValueError("configuration_invalid")
    remote = runtime / "remote-access"
    guest = remote / "guest"
    if linked(remote) or linked(guest) or linked(guest / "site"):
        raise ValueError("configuration_invalid")
    guest.mkdir(parents=True, exist_ok=True)
    if guest.resolve().parent != remote.resolve():
        raise ValueError("configuration_invalid")
    return guest


def guest_page(page, asset_versions=None):
    """Keep the full site's source untouched and namespace every guest asset."""
    page = re.sub(r'<script\b(?=[^>]*\bsrc\s*=\s*([\"\'])/demo\.js(?:\?[^\"\']*)?\1)[^>]*>.*?</script\s*>',
                  "", page, flags=re.I | re.S)
    public_script = '<script src="/public-mode.js"></script>'
    if page.count(public_script) != 1 or "</head>" not in page:
        raise ValueError("guest_template_invalid")
    page = page.replace(public_script, '<meta name="workbench-guest" content="true">\n  '
                        '<script src="/guest-mode.js"></script>\n  ' + public_script, 1)
    page = page.replace("</head>", '  <link rel="stylesheet" href="/guest-mode.css">\n</head>', 1)
    return guest_asset_references(page, asset_versions)


def guest_asset_references(page, asset_versions=None):
    """Namespace and version both individual and bundled guest program files."""
    def asset_tag(match):
        tag = match.group(0)
        is_script = bool(re.match(r"<script\b", tag, flags=re.I))
        if not is_script and not re.search(r'\brel\s*=\s*([\"\'])stylesheet\1', tag, flags=re.I):
            return tag
        attribute = "src" if is_script else "href"
        def rewrite(value):
            path = value.group(3)
            if path.startswith("//"):
                return value.group(0)
            target = urlsplit(path)
            relative = target.path.removeprefix("/guest-assets/").lstrip("/")
            digest = (asset_versions or {}).get(relative)
            if digest:
                query = [(key, item) for key, item in parse_qsl(target.query, keep_blank_values=True) if key != "v"]
                query.append(("v", digest))
                path = urlunsplit(("", "", target.path, urlencode(query), target.fragment))
            if not path.startswith("/guest-assets/"):
                path = "/guest-assets" + path
            return value.group(1) + value.group(2) + path + value.group(2)
        return re.sub(r'(\b' + attribute + r'\s*=\s*)([\"\'])(/[^\"\']*)\2', rewrite, tag, flags=re.I)

    return re.sub(r"<(?:script|link)\b[^>]*>", asset_tag, page, flags=re.I)


class SourceChanged(RuntimeError):
    pass


def sanitize_resources(site, exporter):
    """Clean known HTML examples and validate data in its decoded representation."""
    scripts = []
    for item in sorted(site.rglob("*")):
        if not item.is_file():
            continue
        text = item.read_text(encoding="utf-8")
        if item.suffix.lower() == ".json":
            # Escaped backslashes in JSON are not UNC paths. Validate every
            # decoded key/value instead, including escaped credential strings.
            values = exporter.strings(json.loads(text))
            if any(exporter.LOCAL_PATH.search(value) or exporter.SECRET.search(value) for value in values):
                raise ValueError("guest_content_rejected")
            continue
        if item.suffix.lower() in {".html", ".js", ".css"}:
            # Existing local-path examples are HTML placeholder attributes. Do
            # not rewrite JavaScript regular expressions or arbitrary program text.
            def placeholder(match):
                return match.group(1) + exporter.LOCAL_PATH.sub("[本机文件]", match.group(3)) + match.group(2)
            cleaned = re.sub(r'(\bplaceholder\s*=\s*([\"\']))(.*?)\2', placeholder, text, flags=re.I | re.S)
            if cleaned != text:
                item.write_text(cleaned, encoding="utf-8", newline="\n")
            text = cleaned
            if SOURCE_PATH.search(text) or exporter.SECRET.search(text):
                raise ValueError("guest_content_rejected")
        elif exporter.LOCAL_PATH.search(text) or exporter.SECRET.search(text):
            raise ValueError("guest_content_rejected")
        if item.suffix.lower() == ".js":
            scripts.append(item)
    validate_javascript(scripts)


def validate_javascript(scripts):
    """Check script syntax without treating encoded source strings as file paths."""
    node = shutil.which("node")
    if not node:
        raise ValueError("guest_javascript_validator_unavailable")
    for item in scripts:
        result = subprocess.run([node, "--check", str(item)], capture_output=True, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        if result.returncode:
            raise ValueError("guest_javascript_invalid")


class GuestScanner(SYNC.FingerprintScanner):
    def __init__(self, root):
        super().__init__(root)
        self.media_references = {}

    def take(self):
        found = super().take()
        config = load_config(self.root)
        # Follow-up decisions were added after the original Pages scanner.
        for path in (Path(config["runtime_root"]) / "creation" / "followups" / "catalog.json",
                     self.root / "deployment" / "guest_snapshot.py",
                     self.root / "deployment" / "guest_assets.py",
                     self.root / "deployment" / "guest_media_snapshot.py"):
            try:
                stat = path.stat()
                if path.is_file():
                    found[str(path.absolute())] = SYNC.FileMark(stat.st_size, stat.st_mtime_ns)
            except FileNotFoundError:
                pass
        # Reuse event-file stat changes; media are statted only, never copied or
        # hashed. This also refreshes availability after an attachment moves.
        owners = {path for path in found if Path(path).parent.name == "events"}
        self.media_references = {path: value for path, value in self.media_references.items() if path in owners}
        for owner in owners:
            mark = found[owner]
            if self.media_references.get(owner, (None,))[0] != mark:
                references = set()
                try:
                    if mark.size <= 8 * 1024 * 1024:
                        event = json.loads(Path(owner).read_text(encoding="utf-8-sig"))
                        current = event.get("current") if isinstance(event, dict) else None
                        assets = current.get("assets") if isinstance(current, dict) else None
                        if isinstance(assets, dict):
                            for kind, raw in assets.items():
                                path = MEDIA.media_path(kind, raw)
                                if path is not None:
                                    references.add(str(path))
                except (ValueError, OSError):
                    pass
                self.media_references[owner] = (mark, references)
            for reference in self.media_references[owner][1]:
                path = Path(reference)
                try:
                    stat = path.stat()
                    if path.is_file():
                        found[str(path.absolute())] = SYNC.FileMark(stat.st_size, stat.st_mtime_ns)
                except FileNotFoundError:
                    pass
        return found


def build_snapshot(root, base, *, scanner=None, expected_marks=None, exporter=None):
    root = Path(root)
    base = validate_base(base)
    guest = guest_directory(root)
    own_module = exporter is None
    module_name = "_workbench_guest_export_" + uuid.uuid4().hex
    exporter = exporter or load_module(root / "deployment" / "export_text_site.py", module_name)
    exporter.SITE = guest / "site"
    original_builder = exporter._build_site
    original_clean = exporter.clean
    media_index = {"schema": 1, "cases": {}}
    completed_marks = expected_marks

    def clean_guest(value, key=""):
        nonlocal media_index
        cleaned = original_clean(value, key)
        if isinstance(value, dict) and isinstance(value.get("state"), dict) and isinstance(value.get("cases"), dict):
            media_index = MEDIA.restore_media(value, cleaned, original_clean)
        return cleaned

    def build_guest(bundle, site, output):
        nonlocal completed_marks
        bundle["state"]["guest_mode"] = True
        # Included in the data revision, so a guest-template change refreshes readers.
        bundle["state"]["guest_template_version"] = 4
        bundle["state"]["guest_media"] = True
        original_builder(bundle, site, output)
        (site / "media-index.json").write_text(json.dumps(media_index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        if not all((site / name).is_file() for name in ("guest-mode.js", "guest-mode.css")):
            raise ValueError("guest_assets_unavailable")
        page = site / "index.html"
        sanitize_resources(site, exporter)
        original_files = set(site.iterdir())
        # Remove private demos and install the early read-only guards before
        # selecting any bundle inputs.
        bundled_page = ASSETS.bundle_guest_assets(site, guest_page(page.read_text(encoding="utf-8")))
        # Privacy was checked on every decoded source before packing. Encoded
        # regex backslashes can resemble UNC paths, so only syntax-check the
        # fixed wrappers here rather than rescanning their JSON string encoding.
        validate_javascript([file for file in set(site.iterdir()) - original_files if file.suffix == ".js"])
        # Hash the sanitized bytes that the guest server actually serves. A
        # data-only snapshot update can then reuse unchanged browser assets.
        versions = {item.name: hashlib.sha256(item.read_bytes()).hexdigest()[:12]
                    for item in site.iterdir() if item.is_file() and item.suffix in {".js", ".css"}}
        page.write_text(guest_asset_references(bundled_page, versions), encoding="utf-8", newline="\n")
        # This archive stays private, but keep it consistent with the staged site.
        with zipfile.ZipFile(output / "case-workbench-text.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for item in sorted(site.rglob("*")):
                if item.is_file():
                    archive.write(item, item.relative_to(site).as_posix())
        if scanner is not None and expected_marks is not None:
            completed_marks = scanner.take()
            if SYNC.changed_files(expected_marks, completed_marks, business_only=True):
                raise SourceChanged("source_changed")

    exporter._build_site = build_guest
    exporter.clean = clean_guest
    try:
        # The shared exporter normally prints counts. Never emit its output here.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            exporter.export(base)
    finally:
        exporter._build_site = original_builder
        exporter.clean = original_clean
        if own_module:
            sys.modules.pop(module_name, None)
    return completed_marks


class SnapshotService:
    def __init__(self, root=ROOT, base=BASE, *, interval=30, scanner=None,
                 builder=build_snapshot, ready=SYNC.local_api_ready, clock=time.monotonic,
                 logger=None):
        self.root = Path(root)
        self.base = validate_base(base)
        self.guest = guest_directory(self.root)
        self.interval = max(30, float(interval))
        self.scanner = scanner or GuestScanner(self.root)
        self.builder, self.ready, self.clock = builder, ready, clock
        self.logger = logger or logging.getLogger("workbench.guest_snapshot")
        self.marks = None
        self.last_attempt = float("-inf")
        self.state = None
        self.published_at = None
        try:
            manifest = json.loads((self.guest / "site" / "public-data.json").read_text(encoding="utf-8"))
            value = manifest.get("meta", {}).get("exported_at")
            if isinstance(value, str) and len(value) < 80:
                datetime.fromisoformat(value)
                self.published_at = value
        except (OSError, ValueError, TypeError):
            pass

    def status(self, state):
        if state not in STATES:
            raise ValueError("unknown_state")
        if self.state != state:
            self.logger.info(state)
        self.state = state
        value = {"state": state, "updated_at": timestamp(), "published_at": self.published_at}
        try:
            SYNC.atomic_json(self.guest / "status.json", value)
        except OSError:
            self.logger.warning("status_write_failed")
        return value

    def tick(self, *, force=False):
        now = self.clock()
        if not force and now - self.last_attempt < self.interval:
            return False
        self.last_attempt = now
        try:
            current = self.scanner.take()
        except (OSError, ValueError, KeyError, TypeError):
            self.status("scan_failed")
            return False
        if self.marks is not None and not SYNC.changed_files(self.marks, current, business_only=True):
            self.marks = current
            self.status("ready")
            return False
        if not self.ready(self.base):
            self.status("waiting_local_service")
            return False
        self.status("building")
        try:
            completed = self.builder(self.root, self.base, scanner=self.scanner, expected_marks=current)
        except SourceChanged:
            self.status("source_changed")
            return False
        except Exception:
            self.status("build_failed")
            return False
        self.marks = current if completed is None else completed
        self.published_at = timestamp()
        self.status("ready")
        return True


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--interval", type=float, default=30)
    args = parser.parse_args(argv)
    try:
        service = SnapshotService(base=args.base, interval=args.interval)
        logger = logging.getLogger("workbench.guest_snapshot")
        logger.setLevel(logging.INFO)
        handler = RotatingFileHandler(service.guest / "snapshot.log", maxBytes=256 * 1024,
                                      backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
        stop = threading.Event()
        for name in ("SIGINT", "SIGTERM"):
            signal.signal(getattr(signal, name), lambda *_: stop.set())
        with SYNC.ProcessLock(service.guest / "snapshot.lock"):
            service.status("starting")
            if args.once:
                success = service.tick(force=True)
                print(json.dumps({"state": service.state, "updated_at": timestamp(),
                                  "published_at": service.published_at}))
                return 0 if success else 1
            while not stop.is_set():
                service.tick()
                stop.wait(service.interval)
            service.status("stopped")
        return 0
    except SYNC.AlreadyRunning:
        print(json.dumps({"state": "already_running", "updated_at": timestamp()}))
        return 0
    except Exception:
        print(json.dumps({"state": "configuration_invalid", "updated_at": timestamp()}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
