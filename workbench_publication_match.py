"""Register a posted video by itself: match TikTok posts to the timelines still waiting for their registration.

A post is known from the TK 看板 extension (pushed when one of our accounts' pages is open) or from an imported Shop
export. It belongs to a waiting timeline when its caption headline is one of the order's 🔴 title lines, it went up
no earlier than the timeline was built, and its account is the one the timeline's creator has posted from before.
Only a pair that is unique both ways is registered by itself; every other match waits in the publication panel for
one click. An automatic registration can be undone, and an undone or refused post is never offered for that
timeline again.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
import time
import unicodedata

from creation_store import CreationError
from direction_store import fingerprint, now
from elevenlabs_client import VoiceError
from production_operations import durable_json, file_lock
from production_store import active_timelines, text
from workbench_video_publish import record_timeline, reviewed_source, timeline_records

HEADLINE_MIN = 8
POOL_DAYS = 45
SOURCES = {"tk_lens": "TK 看板", "shop_export": "Shop 导出"}


def headline_key(title):
    """The caption's headline (before the first hashtag), letters and digits only, for comparing exactly."""
    head = str(title or "").split("#", 1)[0]
    key = "".join(char for char in unicodedata.normalize("NFKC", head).casefold() if char.isalnum())
    return key if len(key) >= HEADLINE_MIN else ""


def posted_on(post_id, fallback=""):
    """The day a post went up, read from its ID at the Shop exports' fixed UTC−8, as the registration form does."""
    try:
        seconds = int(post_id) >> 32
    except (TypeError, ValueError):
        seconds = 0
    if 1420070400 <= seconds <= time.time() + 86400:
        return (datetime.fromtimestamp(seconds, timezone.utc) - timedelta(hours=8)).date().isoformat()
    day = str(fallback or "")[:10]
    try:
        return date.fromisoformat(day).isoformat()
    except ValueError:
        return ""


def match_posts(posts, slots, accounts):
    """Every (waiting timeline, post) pair that fits; strong when the account is the creator's known one."""
    pairs = []
    for slot in slots:
        known = accounts.get(str(slot.get("creator") or "").casefold(), set())
        for post in posts.values():
            if (post["key"] not in slot["keys"] or post["post_id"] in slot["refused"]
                    or not post["published_on"] or post["published_on"] < slot["earliest"]):
                continue
            if known and post["account"].casefold() not in known:
                continue
            pairs.append({"slot": slot, "post": post, "strong": bool(known)})
    return pairs


def unique_pairs(pairs):
    """The strong pairs whose timeline has no other post and whose post fits no other timeline."""
    slots, posts = {}, {}
    for pair in pairs:
        slots[pair["slot"]["key"]] = slots.get(pair["slot"]["key"], 0) + 1
        posts[pair["post"]["post_id"]] = posts.get(pair["post"]["post_id"], 0) + 1
    return [pair for pair in pairs if pair["strong"] and slots[pair["slot"]["key"]] == 1 and posts[pair["post"]["post_id"]] == 1]


class PublicationMatchMixin:
    def _taken_posts(self, data):
        """Posts already registered to any video order, and the script each video ID is bound to."""
        taken = {record["post_id"] for entry in data["jobs"].values()
                 for record in (entry.get("publication"), *entry.get("history", [])) if record}
        bound = {str(script.get("video_id")): script["id"] for script in self.creation.public()["scripts"] if script.get("video_id")}
        return taken, bound

    @staticmethod
    def _creator_accounts(data):
        """Which accounts each creator has posted from, learned from the registrations saved so far."""
        accounts = {}
        for entry in data["jobs"].values():
            for record in (entry.get("publication"), *entry.get("history", [])):
                creator = str((((record or {}).get("source") or {}).get("timeline") or {}).get("creator") or "")
                if creator and record.get("account"):
                    accounts.setdefault(creator.casefold(), set()).add(str(record["account"]).lstrip("@").casefold())
        return accounts

    def publication_watch_accounts(self):
        """Our own posting accounts, for the extension to push their fresh posts by itself."""
        names = {str(record.get("account") or "").lstrip("@") for entry in self._video_publications().read()["jobs"].values()
                 for record in (entry.get("publication"), *entry.get("history", [])) if record}
        return {"accounts": sorted(name for name in names if re.fullmatch(r"[\w.\-]{1,100}", name))}

    def _publication_slots(self, data):
        """Timelines that are ready to register and have no registration yet."""
        taken, bound = self._taken_posts(data)
        slots = []
        for job in self.production.public()["jobs"]:
            if job.get("archived"):
                continue
            entry = data["jobs"].get(job["id"], {"publication": None, "history": []})
            keys = None
            for row in active_timelines(job):
                uid = row["unique_id"]
                if timeline_records(entry, uid):
                    continue
                try:
                    reviewed_source(job, uid)
                except VoiceError:
                    continue
                if keys is None:
                    keys = {headline_key(line) for line in ((self._video_publish_copy(job) or {}).get("titles") or [])} - {""}
                if not keys:
                    break
                built = str(row.get("built_at") or "")[:10]
                try:
                    earliest = (date.fromisoformat(built) - timedelta(days=1)).isoformat()
                except ValueError:
                    earliest = ""
                refused = {item.get("post_id") for item in entry.get("dismissed", []) if item.get("timeline_uid") == uid}
                # A post bound to another script cannot be registered here; its own script's binding is fine.
                refused |= taken | {post for post, script in bound.items() if script != job["script_id"]}
                slots.append({"key": job["id"] + "|" + uid, "job_id": job["id"], "timeline_uid": uid, "name": row.get("name", ""),
                              "creator": row.get("creator", ""), "earliest": earliest, "keys": keys, "refused": refused})
        return slots

    def _seen_posts_path(self):
        return self._video_publications().root / "seen-posts.json"

    def _seen_posts(self):
        try:
            value = json.loads(self._seen_posts_path().read_text(encoding="utf-8"))
            return value["posts"] if isinstance(value.get("posts"), dict) else {}
        except (OSError, ValueError, AttributeError, KeyError):
            return {}

    def _remember_posts(self, incoming, keys):
        """Keep pushed posts whose headline is one a waiting order uses; the rest of what was browsed is not kept."""
        posts, changed = self._seen_posts(), False
        cutoff = (datetime.now(timezone.utc) - timedelta(days=POOL_DAYS)).isoformat()
        for post_id in [post_id for post_id, post in posts.items() if str(post.get("seen_at") or "") < cutoff]:
            posts.pop(post_id)
            changed = True
        for raw in incoming:
            key = headline_key(raw.get("title"))
            if not key or key not in keys:
                continue
            post = {"account": raw.get("author", ""), "title": raw.get("title", ""), "published_on": posted_on(raw["video_id"], raw.get("published_at")),
                    "source": "tk_lens", "seen_at": now()}
            if {name: value for name, value in (posts.get(raw["video_id"]) or {}).items() if name != "seen_at"} != {
                    name: value for name, value in post.items() if name != "seen_at"}:
                posts[raw["video_id"]] = post
                changed = True
        if changed:
            durable_json(self._seen_posts_path(), {"posts": posts})
        return posts

    def _match_pool(self, slots, pushed):
        """Posts to match: what the extension pushed, then our own videos in the Shop exports since the earliest build."""
        earliest = min((slot["earliest"] for slot in slots if slot["earliest"]), default="")
        posts = {}
        for video in self.performance.videos_readonly():
            day = str(video.get("posted_on") or "")
            if earliest and day and day < earliest:
                continue
            key = headline_key(video.get("title"))
            if key:
                posts[video["video_id"]] = {"post_id": video["video_id"], "account": str(video.get("account") or "").lstrip("@"),
                                            "title": video.get("title") or "", "key": key,
                                            "published_on": posted_on(video["video_id"], day), "source": "shop_export"}
        for post_id, post in pushed.items():
            key = headline_key(post.get("title"))
            if key:
                posts[post_id] = {"post_id": post_id, "account": str(post.get("account") or "").lstrip("@"), "title": post.get("title") or "",
                                  "key": key, "published_on": post.get("published_on") or "", "source": post.get("source") or "tk_lens"}
        return posts

    def auto_register_publications(self, incoming=()):
        """Register every unique match by itself; returns the registrations made. Never raises for a single match."""
        store = self._video_publications()
        try:
            with file_lock(store.root / "match.lock", "发布匹配正在进行。"):
                data = store.read()
                slots = self._publication_slots(data)
                pushed = self._remember_posts(incoming, {key for slot in slots for key in slot["keys"]})
                if not slots:
                    return []
                pairs = match_posts(self._match_pool(slots, pushed), slots, self._creator_accounts(data))
                made = []
                for pair in unique_pairs(pairs):
                    try:
                        made.append(self._auto_register(pair))
                    except (VoiceError, CreationError):
                        continue
                return made
        except VoiceError:
            return []

    def _auto_register(self, pair):
        slot, post = pair["slot"], pair["post"]
        store = self._video_publications()
        job = self.production.job(slot["job_id"])
        source = reviewed_source(job, slot["timeline_uid"])
        revision = store.revision(store.entry(store.read(), slot["job_id"]))
        account = post["account"]
        body = {"request_id": "auto-" + hashlib.sha256(f"{slot['key']}|{post['post_id']}|{revision}".encode()).hexdigest()[:40],
                "revision": revision, "source_signature": fingerprint(source), "timeline_uid": slot["timeline_uid"],
                "post_id": post["post_id"], "published_on": post["published_on"], "account": account,
                "post_url": f"https://www.tiktok.com/@{account}/video/{post['post_id']}" if account else "",
                "note": "", "calendar_uploaded": True}
        self.save_video_publication(slot["job_id"], body, auto={"source": post["source"], "title": post["title"][:300], "matched_at": now()})
        return {"job_id": slot["job_id"], "timeline_uid": slot["timeline_uid"], "post_id": post["post_id"]}

    def publication_candidates(self, job_id, timeline_uid):
        """Posts that fit this waiting timeline, for one-click registration; empty when it is registered or not ready."""
        store = self._video_publications()
        data = store.read()
        slots = self._publication_slots(data)
        mine = [slot for slot in slots if slot["job_id"] == job_id and slot["timeline_uid"] == timeline_uid]
        if not mine:
            return []
        pairs = match_posts(self._match_pool(slots, self._seen_posts()), slots, self._creator_accounts(data))
        elsewhere = {}
        for pair in pairs:
            elsewhere.setdefault(pair["post"]["post_id"], []).append(pair["slot"])
        return [{**{name: pair["post"][name] for name in ("post_id", "account", "title", "published_on", "source")},
                 "source_label": SOURCES.get(pair["post"]["source"], ""), "account_known": pair["strong"],
                 "also": [f"{slot['job_id']} · {slot['name']}" for slot in elsewhere[pair["post"]["post_id"]] if slot is not mine[0]]}
                for pair in pairs if pair["slot"] is mine[0]]

    def _refuse_post(self, data, identifier, post_id, timeline_uid):
        entry = copy.deepcopy(data["jobs"].get(identifier, {"publication": None, "history": []}))
        refused = [item for item in entry.get("dismissed", []) if (item.get("post_id"), item.get("timeline_uid")) != (post_id, timeline_uid)]
        entry["dismissed"] = [*refused, {"post_id": post_id, "timeline_uid": timeline_uid, "at": now()}]
        return entry

    def dismiss_publication_candidate(self, identifier, body):
        """「不是这条」: this post is never offered for this timeline again."""
        if not isinstance(body, dict) or set(body) - {"post_id", "timeline_uid"}:
            raise VoiceError(400, "候选参数无效。")
        post = text(body.get("post_id"), "帖子 ID", limit=25)
        uid = text(body.get("timeline_uid"), "时间线", limit=500)
        if not re.fullmatch(r"\d{6,25}", post):
            raise VoiceError(400, "帖子 ID 须为 6–25 位数字。")
        with self._video_publication_job_guard(identifier):
            store = self._video_publications()
            with self.lock, file_lock(store.root / "catalog.lock", "发布资料正在保存，请稍后重试。"):
                self.production.job(identifier)
                data = store.read()
                data["jobs"][identifier] = self._refuse_post(data, identifier, post, uid)
                store.write(data)
                self.state_cache = None
                return {"revision": store.revision(data["jobs"][identifier])}

    def undo_auto_video_publication(self, identifier, body):
        """Take back an automatic registration: the post leaves the order, the script returns to its earlier state."""
        if not isinstance(body, dict) or set(body) - {"publication_id"}:
            raise VoiceError(400, "撤销参数无效。")
        with self._video_publication_job_guard(identifier):
            store = self._video_publications()
            with self.lock, self.production.catalog_transaction(), file_lock(store.root / "catalog.lock", "发布资料正在保存，请稍后重试。"):
                self.production.job(identifier)
                data = store.read()
                entry = store.entry(data, identifier)
                record = self._standing_publication(entry, body.get("publication_id"))
                if not record or not record.get("auto"):
                    raise VoiceError(409, "这条登记不是自动登记，或已经变化，请刷新后核对。")
                updated = self._refuse_post(data, identifier, record["post_id"], record_timeline(record))
                history = [item for item in updated["history"] if item["id"] != record["id"]]
                publication = updated["publication"]
                if publication and publication["id"] == record["id"]:
                    publication = history.pop() if history else None
                updated.update(publication=publication, history=history)
                data["jobs"][identifier] = updated
                store.write(data)
                self.state_cache = None
                revision = store.revision(updated)
            warning = self._restore_script_after_undo(identifier, record)
        return {"revision": revision, "warning": warning}

    def _restore_script_after_undo(self, identifier, record):
        auto = record["auto"]
        if not auto.get("script_updated"):
            return ""
        before = auto.get("script_before") or {}
        try:
            script = self.creation.get_script(record["script_id"])
            if (script.get("video_id"), script.get("published_on")) != (record["post_id"], record["published_on"]):
                return ""
            self.save_script_status(record["script_id"], {
                "request_id": "videounauto-" + hashlib.sha256(record["id"].encode()).hexdigest()[:32],
                "revision": self.creation.public()["revision"],
                "script_status": {"status": before.get("status") or "draft", "video_id": before.get("video_id") or "",
                                  "published_on": before.get("published_on") or "",
                                  "note": f"撤销自动登记；制作单 {identifier}，帖子 {record['post_id']}"}}, video_job_id=identifier)
            return ""
        except (CreationError, VoiceError) as exc:
            return f"登记已撤销，但脚本状态没有改回：{exc}"
