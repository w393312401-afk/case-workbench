"""Reviewer avatars, downloaded to this computer and shown on comment cards (2026-11-09).

Platform avatar links are signed and expire within days (old TikTok video comment links already
answer 403), so a link is only useful right after a scrape: the image is downloaded then, into
reviews/avatars/<key>.<ext>. One file per reviewer (platform + handle or name), shared by all their
comments. A link that cannot be fetched is remembered in reviews/avatars/index.json and not tried
again, unless a later export brings a different link for the same reviewer.

Avatars stay on this computer: they are served only through the workbench API, like the comments.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import urllib.error
import urllib.request

MAX_BYTES = 300_000
TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif"}
# Where each platform's export keeps the avatar link (the first one present is used).
FIELDS = {"tiktok_video": ("avatarThumbnail",), "tiktok_shop": ("reviewer_avatar_url",),
          "facebook": ("profilePicture",), "amazon": ("userProfileImage", "profileImage", "userAvatar"), "reddit": ()}


def author_key(platform, handle, name):
    who = (handle or "").strip().lower() or (name or "").strip()
    if not who:
        return None
    return hashlib.sha1(f"{platform}|{who}".encode("utf-8")).hexdigest()[:20]


def avatar_link(platform, item):
    for field in FIELDS.get(platform, ()):
        value = item.get(field)
        if isinstance(value, dict):
            value = value.get("url") or value.get("uri")
        if isinstance(value, str) and value.startswith("https://"):
            return value
    author = item.get("author")
    if platform == "facebook" and isinstance(author, dict):
        picture = author.get("profilePicture")
        url = picture.get("uri") if isinstance(picture, dict) else picture
        if isinstance(url, str) and url.startswith("https://"):
            return url
    return None


class ReviewAvatars:
    def __init__(self, root, *, fetch=None):
        self.folder = Path(root) / "avatars"
        self.index_path = self.folder / "index.json"
        self.fetch = fetch or self._fetch
        self.lock = threading.Lock()
        self.worker = None
        self.pending = False

    def file(self, key):
        """The saved image for a reviewer key, or None."""
        if not key or not all(c in "0123456789abcdef" for c in key):
            return None
        for ext in TYPES.values():
            path = self.folder / f"{key}{ext}"
            if path.is_file():
                return path
        return None

    def keys_with_files(self):
        if not self.folder.is_dir():
            return set()
        return {path.stem for path in self.folder.iterdir() if path.suffix in TYPES.values()}

    def _index(self):
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_index(self, data):
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.index_path)

    @staticmethod
    def _fetch(url):
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*"})
        with urllib.request.urlopen(request, timeout=15) as response:
            kind = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            return kind, response.read(MAX_BYTES + 1)

    def download(self, wanted):
        """wanted: {key: link}. Downloads what is missing; returns how many images were saved."""
        with self.lock:
            index = self._index()
        have, saved = self.keys_with_files(), 0
        for key, link in wanted.items():
            if key in have or index.get(key, {}).get("link") == link:
                continue  # already saved, or this exact link already failed
            entry = {"link": link}
            try:
                kind, body = self.fetch(link)
                if kind not in TYPES:
                    raise ValueError(f"不是图片（{kind or '未知'}）")
                if len(body) > MAX_BYTES or not body:
                    raise ValueError("图片太大或是空的")
                self.folder.mkdir(parents=True, exist_ok=True)
                path = self.folder / f"{key}{TYPES[kind]}"
                tmp = path.with_suffix(".part")
                tmp.write_bytes(body)
                os.replace(tmp, path)
                entry["saved"] = True
                saved += 1
            except (urllib.error.URLError, OSError, ValueError, TimeoutError) as error:
                entry["error"] = getattr(error, "code", None) or type(error).__name__
            index[key] = entry
            with self.lock:
                self._save_index(index)
        return saved

    def wanted_from_sources(self, sources, read_source, parsers):
        """{key: link} for every reviewer in the raw exports that carries an avatar link."""
        wanted = {}
        for platform in parsers:
            folder = Path(sources) / platform
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.json")):
                try:
                    items = json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    continue
                for item in items if isinstance(items, list) else []:
                    if not isinstance(item, dict):
                        continue
                    link = avatar_link(platform, item)
                    record = parsers[platform](item) if link else None
                    key = record and author_key(platform, record.get("author_handle"), record.get("author_name"))
                    if key:
                        wanted[key] = link  # a later file's link replaces an older one
        return wanted

    def refresh(self, sources, read_source, parsers):
        """Download missing avatars in the background; a call while one runs queues one more pass."""
        with self.lock:
            if self.worker and self.worker.is_alive():
                self.pending = True
                return
            self.pending = False

        def run():
            while True:
                try:
                    self.download(self.wanted_from_sources(sources, read_source, parsers))
                except Exception:
                    pass  # avatars are a nicety; never let them break the review page
                with self.lock:
                    if not self.pending:
                        return
                    self.pending = False

        self.worker = threading.Thread(target=run, daemon=True, name="review-avatars")
        self.worker.start()
