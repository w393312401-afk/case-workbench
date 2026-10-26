"""Real-user review library for the workbench.

A separate SQLite file under the runtime folder (`reviews/reviews.sqlite`); the
CaseStore stays the only record of cases. Raw scraper exports live next to it in
`reviews/sources/<platform>/*.json` and the database is derived from them: a sync
rebuilds everything in one transaction, so it can be repeated at any time.

Platforms (the sub-folder name decides the parser):
  tiktok_video  Apify "TikTok Comments Scraper" dataset
  amazon        Apify "Amazon reviews" dataset
  tiktok_shop   Apify "TikTok Shop product reviews" dataset
  reddit        Apify "Reddit scraper" comments
  facebook      Apify "Facebook comments scraper" dataset

Public reviewer names and handles are retained when the source supplies them.
Private user IDs and avatars are not part of the normalized library.

What people do to the library is not derived from the sources, so it lives in its
own file (`reviews/state.json`, see ReviewState) and is applied again on every
rebuild: the day each review first appeared, tag changes made by hand, and the
quotes picked for a script. Deleting the database loses nothing but the cache.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import review_taxonomy
import plugin_comments
from review_taxonomy import (BRAND_ACCOUNTS, BRAND_PROFILE, COMPETITOR, DEFECT, NATURE, OBJECTION, PLATFORM_LABELS, RESULT, ROLE_LABELS, TOPIC, TOPIC_RULES,
                             classify, competitors, excerpt, zh_groups)

PRODUCT = "202H"
# Bump when the tables change shape. Known versions migrate in place; other
# versions rebuild the derived database from its sources on the next sync.
DB_VERSION = 3
# A review counts as new in the page for this many days after the sync that first saw it.
NEW_DAYS = 14
# How many quotes the basket for a script can hold.
MAX_PICKS = 500
# The longest note a picked quote can carry; the material basket uses the same limit for every note.
NOTE_LIMIT = 2000
PLATFORMS = tuple(PLATFORM_LABELS)
SORTS = {
    "likes": "r.likes IS NULL, r.likes DESC, r.posted_on DESC",
    "newest": "r.posted_on IS NULL, r.posted_on DESC",
    "oldest": "r.posted_on IS NULL, r.posted_on ASC",
    "rating_low": "r.rating IS NULL, r.rating ASC, r.posted_on DESC",
    "rating_high": "r.rating IS NULL, r.rating DESC, r.posted_on DESC",
    "longest": "LENGTH(r.text) DESC",
}

# The reading of the library handed to writing and review tasks (see ReviewStore._digest).
DIGEST_CHARS = 9000
# Topics where a buyer's complaint is about the hardware or what it can sharpen: the claims a script is most likely to overstate.
DIGEST_HARDWARE = ("clamp", "pivot", "stability", "edge_reach", "small_blade", "blade_fit", "angle", "stones", "build", "accessories")
# What viewers ask about when they want information rather than tease: these questions come first.
DIGEST_PUSHBACK = ("mirror", "strop", "handsharp")
DIGEST_ASKED_ABOUT = ("blade_fit", "small_blade", "accessories", "stones", "angle", "clamp", "price", "shipping", "support", "instructions", "learning", "case", "edge_reach")
DIGEST_NOTES = {
    "说明": "评论者说的话均未核实，评论者不一定是买家（who 写明身份，试用买家有利益关系）；quote 是按规则截出的相关句子，不是全文；id 是评论编号，引用时写编号；"
            "标签由规则自动打，可能有误判，各栏只是候选，不是结论。",
    "buyer_limits": "买家写到的缺陷和限制：稿里的功能、适配、稳定性承诺先对照这里。",
    "viewer_doubts": "观众对演示可信度、稳定性、AI 声音的较真。",
    "viewer_pushback": "观众觉得某个卖点用不上、没必要（如日常用刀不需要镜面、学会磨刀石就行）。可以先认同这类说法再写创作者自己的理由，不要写成反驳或辩论。",
    "unanswered_questions": "TikTok 观众的提问，库里没有任何回复（含修辞性质疑）；能用真实画面回答的可作选题。",
    "buyer_results": "买家怎么描述结果和使用场景，只作语言参照。",
    "learning_curve": "买家写到的上手节奏和学习曲线。",
    "user_picks": "用户亲手从评论库里摘出来的评论；note 是用户自己写的备注，不是评论内容。比自动挑出来的更值得看。",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY, platform TEXT NOT NULL, product TEXT NOT NULL, role TEXT NOT NULL,
    posted_on TEXT, rating INTEGER, title TEXT, text TEXT NOT NULL DEFAULT '', language TEXT,
    likes INTEGER, is_reply INTEGER NOT NULL DEFAULT 0, parent_id TEXT, sku TEXT,
    verified INTEGER, incentivized INTEGER, media_count INTEGER NOT NULL DEFAULT 0,
    relevant INTEGER NOT NULL DEFAULT 1, source_ref TEXT, context TEXT, url TEXT, source_file TEXT,
    first_seen TEXT, author_name TEXT NOT NULL DEFAULT '', author_handle TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS review_tags (
    review_id TEXT NOT NULL, kind TEXT NOT NULL, tag TEXT NOT NULL, PRIMARY KEY (review_id, kind, tag)
);
CREATE TABLE IF NOT EXISTS imports (
    source_file TEXT PRIMARY KEY, platform TEXT, rows INTEGER, skipped INTEGER, modified TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS reviews_platform ON reviews(platform, role);
CREATE INDEX IF NOT EXISTS reviews_posted ON reviews(posted_on);
CREATE INDEX IF NOT EXISTS reviews_parent ON reviews(parent_id);
CREATE INDEX IF NOT EXISTS reviews_video ON reviews(source_ref);
CREATE INDEX IF NOT EXISTS tags_lookup ON review_tags(kind, tag, review_id);
"""
FTS = "CREATE VIRTUAL TABLE IF NOT EXISTS review_fts USING fts5(title, text, tokenize='trigram')"


class ReviewError(ValueError):
    """A source file that cannot be read, or an invalid query."""


def _tagged(kind, tags):
    """SQL: the review carries one of these tags. Tags are code constants, checked here so a typo cannot silently match nothing."""
    valid = NATURE if kind == "nature" else TOPIC
    unknown = [t for t in tags if t not in valid]
    if unknown:
        raise KeyError(unknown[0])
    names = ",".join("'" + t + "'" for t in tags)
    return f"EXISTS (SELECT 1 FROM review_tags t WHERE t.review_id = r.id AND t.kind = '{kind}' AND t.tag IN ({names}))"


def _code_signature():
    """Hash of the code that decides what the database holds: this file (the parsers) and the tag rules. Changing either
    rebuilds the database on the next sync, so an edited rule never leaves stale tags behind."""
    digest = hashlib.sha1(str(DB_VERSION).encode())
    for module in (__file__, review_taxonomy.__file__):
        digest.update(Path(module).read_bytes())
    return digest.hexdigest()


CODE_SIGNATURE = _code_signature()
TAG_KINDS = {"nature": NATURE, "topic": TOPIC, "competitor": COMPETITOR}


class ReviewState:
    """What people have done to the library, kept in its own file because the database is rebuilt from the sources and
    may be deleted at any time:
      seen   review id -> the day a sync first saw it ("" for reviews that were already there when tracking began)
      tags   review id -> kind -> tag -> True (added by hand) / False (removed by hand)
      picks  review id -> {"note": ..., "added": ...}: quotes picked for a script
    """

    def __init__(self, path):
        self.path = Path(path)
        # Counts saves in this process, so readers can cache without trusting file times alone.
        self.saves = 0

    @staticmethod
    def empty():
        return {"version": 1, "seen": {}, "tags": {}, "picks": {}}

    def exists(self):
        return self.path.is_file()

    def load(self):
        if not self.path.is_file():
            return self.empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ReviewError(f"手动记录文件读不了（{type(error).__name__}）：{self.path}") from None
        if not isinstance(data, dict):
            raise ReviewError(f"手动记录文件格式不对：{self.path}")
        state = self.empty()
        for key in ("seen", "tags", "picks"):
            if isinstance(data.get(key), dict):
                state[key] = data[key]
        return state

    def save(self, state):
        """Write the whole record through a temporary file, so a crash never leaves half of it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.saves += 1
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)


def _clean(text):
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(text or "")))
    return re.sub(r"[ \t\r\f\v]+", " ", text).replace(" ", " ").strip()


def _date(value):
    text = str(value or "").strip()
    if re.fullmatch(r"\d{12,13}", text):
        return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    if re.fullmatch(r"\d{9,11}", text):
        return datetime.fromtimestamp(int(text), tz=timezone.utc).strftime("%Y-%m-%d")
    match = re.match(r"(\d{4})[-/](\d{2})[-/](\d{2})", text)
    return "-".join(match.groups()) if match else None


def _int(value):
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def review_id(platform, native):
    return hashlib.sha1(f"{platform}|{native}".encode()).hexdigest()[:16]


# ---- search box -> SQL -----------------------------------------------------------------------
_CJK_RANGE = "\u3400-\u9fff\uf900-\ufaff"  # CJK ideographs and compatibility ideographs, written as escapes
_TERM_RUNS = re.compile("[" + _CJK_RANGE + "]+|[^\\s" + _CJK_RANGE + "]+")
_HAS_CJK = re.compile("[" + _CJK_RANGE + "]")


def parse_query(text):
    """The search box as up to 8 terms. A Chinese term also stands for the English words it is usually written as,
    because the reviews are English."""
    terms = []
    for run in _TERM_RUNS.findall(text or ""):
        chinese = bool(_HAS_CJK.search(run))
        terms.append({"term": run, "chinese": chinese, "groups": zh_groups(run) if chinese else []})
    return terms[:8]


def describe_query(text):
    """What the search did with Chinese words: the English terms it also looked for, and words it had none for."""
    terms = parse_query(text)
    expanded, seen = [], set()
    for term in terms:
        for word, english in term["groups"]:
            if word not in seen:
                seen.add(word)
                expanded.append({"term": word, "english": list(english)})
    return {"expanded": expanded, "unmapped": [t["term"] for t in terms if t["chinese"] and not t["groups"]]}


def _like(value):
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _contains(value):
    """SQL for 'the review mentions this text'. Three or more characters use the full-text index, shorter ones LIKE."""
    if len(value) >= 3:
        return "r.rowid IN (SELECT rowid FROM review_fts WHERE review_fts MATCH ?)", ['"' + value.replace('"', '""') + '"']
    like = _like(value)
    return "(r.text LIKE ? ESCAPE '\\' OR COALESCE(r.title,'') LIKE ? ESCAPE '\\')", [like, like]


def _term_sql(term):
    """WHERE fragment and parameters for one search term."""
    literal = _contains(term["term"])
    if not term["groups"]:
        return literal
    sql, params = [], []
    for word, english in term["groups"]:
        options = [_contains(word)] + [_contains(e) for e in english]
        sql.append("(" + " OR ".join(o[0] for o in options) + ")")
        for _, values in options:
            params += values
    joined = " AND ".join(sql)
    if len(term["groups"]) == 1 and term["groups"][0][0] == term["term"]:
        return "(" + joined + ")", params
    # real Chinese text that spells the term out, or every word the term contains found in English
    return f"({literal[0]} OR ({joined}))", literal[1] + params


# ---- per-platform parsers: raw item -> normalized record (or None to skip) ------------
def _tiktok_video(item):
    if "error" in item or not item.get("cid"):
        return None
    handle = str(item.get("uniqueId") or "").lower()
    url = item.get("videoWebUrl") or item.get("submittedVideoUrl") or ""
    video = re.search(r"/video/(\d+)", url)
    parent = item.get("repliesToId")
    video_author = _clean(item.get("videoAuthor")).lstrip("@")
    return {"native": item["cid"], "role": "brand_account" if handle in BRAND_ACCOUNTS or (handle and handle == video_author.lower()) else "viewer",
            "author_name": _clean(item.get("nickname") or item.get("authorName")), "author_handle": _clean(item.get("uniqueId")).lstrip("@"),
            "created_at": str(item.get("createTimeISO") or ""), "video_author": video_author,
            "product": _clean(item["product"]) if "product" in item else PRODUCT,
            "posted_on": _date(item.get("createTimeISO")), "rating": None, "title": None, "text": _clean(item.get("text")),
            "likes": _int(item.get("diggCount")), "is_reply": 1 if parent else 0, "parent_native": parent or None,
            "sku": None, "verified": None, "incentivized": None, "media_count": 0, "relevant": 1,
            "source_ref": video.group(1) if video else None, "context": None, "url": url or None}


def _amazon(item):
    if not item.get("reviewId"):
        return None
    vine = bool(item.get("isAmazonVine"))
    verified = bool(item.get("isVerified"))
    variant = re.sub(r"^Set name:\s*", "", _clean(item.get("variant"))) or None
    return {"native": item["reviewId"], "role": "incentivized_buyer" if vine else ("verified_buyer" if verified else "unverified_buyer"),
            "author_name": _clean(item.get("userName")), "author_handle": "", "product": _clean(item.get("productModel")) or PRODUCT,
            "posted_on": _date(item.get("date")), "rating": _int(item.get("ratingScore")), "title": _clean(item.get("reviewTitle")) or None,
            "text": _clean(item.get("reviewDescription")), "likes": _int(item.get("reviewReaction")), "is_reply": 0, "parent_native": None,
            "sku": variant, "verified": 1 if verified else 0, "incentivized": 1 if vine else 0,
            "media_count": len(item.get("reviewImages") or []), "relevant": 1, "source_ref": item.get("productAsin"),
            "context": None, "url": item.get("reviewUrl")}


def _tiktok_shop(item):
    if not item.get("review_id"):
        return None
    incentivized = bool(item.get("is_incentivized_review"))
    verified = bool(item.get("is_verified_purchase"))
    product = str(item.get("product_id") or "")
    return {"native": str(item["review_id"]), "role": "incentivized_buyer" if incentivized else ("verified_buyer" if verified else "unverified_buyer"),
            "author_name": _clean(item.get("reviewer_name")), "author_handle": "", "product": _clean(item.get("productModel")) or PRODUCT,
            "posted_on": _date(item.get("review_time")), "rating": _int(item.get("review_rating")), "title": None,
            "text": _clean(item.get("review_text")), "likes": None, "is_reply": 0, "parent_native": None,
            "sku": re.sub(r"^Item:\s*", "", _clean(item.get("sku_specification"))) or None, "verified": 1 if verified else 0,
            "incentivized": 1 if incentivized else 0, "media_count": len(item.get("review_images") or []), "relevant": 1,
            "source_ref": product or None, "context": None,
            "url": f"https://www.tiktok.com/view/product/{product}" if product else None}


def _reddit(item):
    if item.get("dataType") not in (None, "comment") or not (item.get("id") or item.get("parsedId")):
        return None
    body = _clean(item.get("body"))
    community = re.sub(r"^r/", "", str(item.get("parsedCommunityName") or item.get("communityName") or "")).strip()
    return {"native": str(item.get("id") or item.get("parsedId")), "role": "peer", "posted_on": _date(item.get("createdAt")),
            "author_name": "", "author_handle": _clean(item.get("username")), "product": _clean(item.get("productModel")) or PRODUCT,
            "rating": None, "title": None, "text": body, "likes": None, "is_reply": 0, "parent_native": None, "sku": None,
            "verified": None, "incentivized": None, "media_count": 0,
            "relevant": 1 if re.search(r"sharpal|\b202h?\b", body, re.I) else 0,
            "source_ref": f"r/{community}" if community and community.lower() != "none" else None, "context": None, "url": item.get("url")}


def _facebook(item):
    native = item.get("id") or item.get("commentId")
    if not native:
        return None
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    return {"native": str(native), "role": "brand_account" if BRAND_PROFILE.search(str(item.get("profileName") or "")) else "viewer",
            "author_name": _clean(item.get("profileName") or author.get("name")),
            "author_handle": "", "product": _clean(item.get("productModel")) or PRODUCT,
            "posted_on": _date(item.get("date")), "rating": None, "title": None, "text": _clean(item.get("text")),
            "likes": _int(item.get("likesCount")), "is_reply": 1 if (_int(item.get("threadingDepth")) or 0) > 0 else 0, "parent_native": None,
            "sku": None, "verified": None, "incentivized": None, "media_count": len(item.get("attachments") or []), "relevant": 1,
            "source_ref": item.get("inputUrl"), "context": _clean(item.get("postTitle")) or None,
            "url": item.get("commentUrl") or item.get("inputUrl")}


PARSERS = {"tiktok_video": _tiktok_video, "amazon": _amazon, "tiktok_shop": _tiktok_shop, "reddit": _reddit, "facebook": _facebook}


def read_source(path, platform):
    """Parse one raw export. Returns (records, skipped_count)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ReviewError(f"无法读取（{type(error).__name__}）") from None
    if not isinstance(data, list):
        raise ReviewError("不是数据集导出（应为列表）")
    parse, records, skipped = PARSERS[platform], [], 0
    for item in data:
        record = parse(item) if isinstance(item, dict) else None
        if record is None:
            skipped += 1
        else:
            records.append(record)
    return records, skipped


def _modified(path):
    stat = Path(path).stat()
    return f"{stat.st_mtime_ns // 1000}:{stat.st_size}"


def _product_key(value):
    """Normalize product aliases for read filters; sku remains the purchased variant."""
    text = str(value or "").strip()
    models = set(re.findall(r"(?<![A-Z0-9])\d{3}[A-Z](?![A-Z0-9])", text.upper()))
    return next(iter(models)) if len(models) == 1 else text.casefold()


class ReviewStore:
    def __init__(self, runtime_root, sources=None):
        self.root = Path(runtime_root) / "reviews"
        self.database = self.root / "reviews.sqlite"
        self.sources = Path(sources) if sources else self.root / "sources"
        self.state = ReviewState(self.root / "state.json")
        self.state_error = None  # why the hand-made records could not be read, if they could not
        self.lock = threading.Lock()

    def connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, isolation_level=None, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.create_function("product_key", 1, _product_key, deterministic=True)
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 2:
            # Preserve the existing rows and manual links while the next sync
            # restores public names from the original exports.
            connection.execute("BEGIN IMMEDIATE")
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(reviews)")}
                for column in ("author_name", "author_handle"):
                    if column not in columns:
                        connection.execute(f"ALTER TABLE reviews ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
                connection.execute(f"PRAGMA user_version = {DB_VERSION}")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                connection.close()
                raise
        elif version != DB_VERSION:
            # A new file, or an older shape of the derived database: drop it, the next sync rebuilds it from the sources.
            connection.execute("BEGIN IMMEDIATE")
            for name in ("review_fts", "review_tags", "reviews", "imports", "meta"):
                connection.execute(f"DROP TABLE IF EXISTS {name}")
            connection.execute(f"PRAGMA user_version = {DB_VERSION}")
            connection.execute("COMMIT")
        connection.executescript(SCHEMA)
        try:
            connection.execute(FTS)
        except sqlite3.OperationalError:
            pass  # no FTS5/trigram in this SQLite build: searches fall back to LIKE
        return connection

    # ---- import ----------------------------------------------------------------
    def source_files(self):
        if not self.sources.is_dir():
            return []
        found = []
        for path in sorted(self.sources.rglob("*.json")):
            relative = path.relative_to(self.sources).parts
            if len(relative) >= 2 and relative[0] in PARSERS and not path.name.startswith(("~", ".")):
                found.append((relative[0], path))
        return found

    def _signature(self, files):
        listing = "|".join(f"{platform}:{path}:{_modified(path)}" for platform, path in files)
        return hashlib.sha1(f"{listing}|{CODE_SIGNATURE}".encode()).hexdigest()

    def revision(self):
        """Cheap read-only polling marker; never opens or rebuilds the library."""
        values = []
        for path in (self.database, Path(str(self.database) + "-wal"), self.state.path):
            try:
                stat = path.stat()
                values.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, getattr(stat, "st_ino", None)))
            except OSError:
                values.append((str(path), None))
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()

    def _source_records(self, files):
        merged, imports, failed = {}, [], []
        plugin_path = self.sources / "tiktok_video" / plugin_comments.SOURCE_NAME
        for platform, path in files:
            try:
                records, skipped = read_source(path, platform)
            except ReviewError as error:
                failed.append({"file": path.name, "error": str(error)})
                continue
            for record in records:
                identifier = review_id(platform, record["native"])
                previous = merged.get(identifier)
                chosen_path = path
                if previous:
                    # Plugin intake stores only supplied or already established
                    # fields. Prefer that merged capture over older exports,
                    # independently of the filename's alphabetical position.
                    if previous[1] == plugin_path and path != plugin_path:
                        fallback, record, chosen_path = record, dict(previous[2]), previous[1]
                    else:
                        fallback = previous[2]
                        record = dict(record)
                    for field in ("author_name", "author_handle"):
                        if not record.get(field):
                            record[field] = fallback.get(field) or ""
                    if chosen_path == plugin_path:
                        for field in ("likes", "created_at", "posted_on", "parent_native", "video_author"):
                            if record.get(field) in (None, ""):
                                record[field] = fallback.get(field)
                if platform == "tiktok_video":
                    # A later source may supply the author or parent missing from
                    # an old plugin cache; derived identity must follow that merge.
                    handle = (record.get("author_handle") or "").lower()
                    record["is_reply"] = 1 if record.get("parent_native") else 0
                    record["role"] = "brand_account" if handle in BRAND_ACCOUNTS or (
                        handle and handle == (record.get("video_author") or "").lower()) else "viewer"
                merged[identifier] = (platform, chosen_path, record)
            imports.append((path.relative_to(self.sources).as_posix(), platform, len(records), skipped, _modified(path)))
        return merged, imports, failed

    def ingest_comments(self, videos, products):
        """Persist partial plugin captures, then rebuild the same user-voice library.

        products must be the workbench's authoritative video-id -> product map.
        Counts describe comments; ignored_ids identifies videos absent from that
        map. An explicitly mapped empty product means unassociated, not 202H.
        """
        with self.lock:
            merged, _, failed = self._source_records(self.source_files())
            if failed:
                raise ReviewError("评论来源文件无法读取；请先修复后再推送，原始资料未改动。")
            existing = [record for platform, _, record in merged.values() if platform == "tiktok_video"]
            try:
                result = plugin_comments.ingest(self.sources / "tiktok_video" / plugin_comments.SOURCE_NAME, videos, products, existing)
            except plugin_comments.CommentIntakeError as error:
                raise ReviewError(str(error)) from None
        # sync takes the same lock. The durable sources remain the recovery
        # point if a rebuild is interrupted, and retries do not duplicate ids.
        self.sync()
        return result

    def _state(self):
        """The hand-made records. A file that cannot be read gives an empty record and a message for the page (state_error),
        never an error: reading the library must not depend on it, and writing to it is refused instead (see _edit_state)."""
        try:
            data = self.state.load()
            self.state_error = None
            return data
        except ReviewError as error:
            self.state_error = str(error)
            return self.state.empty()

    def _edit_state(self, change):
        """Apply change(record) to a fresh copy of the records and save it. Refuses when the file is unreadable, so that
        a damaged file is never overwritten with a nearly empty one."""
        data = self.state.load()
        change(data)
        self.state.save(data)
        self.state_error = None
        return data

    def coverage(self):
        path = self.sources / "coverage.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def sync(self, force=False):
        """Rebuild the library from the raw exports and apply again what people did to it (see ReviewState).
        Never raises for a bad file."""
        files = self.source_files()
        missing = not self.sources.is_dir()
        with self.lock:
            connection = self.connect()
            try:
                signature = self._signature(files)
                row = connection.execute("SELECT value FROM meta WHERE key='signature'").fetchone()
                if row and row[0] == signature and not force:
                    return {"changed": False, "reviews": self._count(connection), "files": len(files), "failed": [], "added": 0,
                            "missing_sources": [str(self.sources)] if missing else [], "state_error": self.state_error}
                merged, imports, failed = self._source_records(files)
                state = self._state()
                # The first sync with the records on only notes what is already there: none of it is "new". An unreadable
                # records file is treated the same way, so a damaged file cannot make everything look new.
                baseline = not self.state.exists() or bool(self.state_error)
                today = date.today().isoformat()
                seen, added = state["seen"], 0
                for identifier in merged:
                    if identifier not in seen:
                        seen[identifier] = "" if baseline else today
                        added += 0 if baseline else 1
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    for table in ("review_tags", "reviews", "imports"):
                        connection.execute(f"DELETE FROM {table}")
                    try:
                        connection.execute("DELETE FROM review_fts")
                        fts = True
                    except sqlite3.OperationalError:
                        fts = False
                    for identifier, (platform, path, record) in merged.items():
                        parent = review_id(platform, record["parent_native"]) if record["parent_native"] else None
                        nature, topics, language = classify(record["text"], title=record["title"] or "", rating=record["rating"],
                                                            role=record["role"], is_reply=bool(record["is_reply"]))
                        cursor = connection.execute(
                            "INSERT INTO reviews (id, platform, product, role, posted_on, rating, title, text, language, likes, is_reply, parent_id, sku, verified, incentivized, media_count, relevant, source_ref, context, url, source_file, first_seen, author_name, author_handle) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (identifier, platform, record.get("product", PRODUCT), record["role"], record["posted_on"], record["rating"], record["title"], record["text"], language,
                             record["likes"], record["is_reply"], parent, record["sku"], record["verified"], record["incentivized"], record["media_count"],
                             record["relevant"], record["source_ref"], record["context"], record["url"], path.name, seen.get(identifier) or None,
                             record.get("author_name") or "", record.get("author_handle") or ""))
                        named = competitors(f"{record['title'] or ''} {record['text']}")
                        connection.executemany("INSERT OR IGNORE INTO review_tags VALUES (?,?,?)",
                                               [(identifier, "nature", t) for t in sorted(nature)] + [(identifier, "topic", t) for t in sorted(topics)]
                                               + [(identifier, "competitor", t) for t in sorted(named)])
                        for kind, changes in (state["tags"].get(identifier) or {}).items():
                            if not isinstance(changes, dict):
                                continue
                            for tag, present in changes.items():
                                if tag not in TAG_KINDS.get(kind, {}):
                                    continue  # a tag the rules no longer have
                                if present:
                                    connection.execute("INSERT OR IGNORE INTO review_tags VALUES (?,?,?)", (identifier, kind, tag))
                                else:
                                    connection.execute("DELETE FROM review_tags WHERE review_id = ? AND kind = ? AND tag = ?", (identifier, kind, tag))
                        if fts:
                            connection.execute("INSERT INTO review_fts(rowid, title, text) VALUES (?,?,?)", (cursor.lastrowid, record["title"] or "", record["text"]))
                    connection.executemany("INSERT INTO imports VALUES (?,?,?,?,?)", imports)
                    connection.execute("INSERT OR REPLACE INTO meta VALUES ('signature', ?)", (signature,))
                    connection.execute("INSERT OR REPLACE INTO meta VALUES ('synced_at', ?)", (datetime.now().isoformat(timespec="seconds"),))
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
                if merged and (added or not self.state.exists()) and not self.state_error:
                    try:
                        self.state.save(state)
                    except OSError as error:
                        self.state_error = f"手动记录文件写不进去（{type(error).__name__}）：{self.state.path}"
                return {"changed": True, "reviews": len(merged), "files": len(files), "failed": failed, "added": added,
                        "missing_sources": [str(self.sources)] if missing else [], "state_error": self.state_error}
            finally:
                connection.close()

    @staticmethod
    def _count(connection):
        return connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]

    # ---- queries ---------------------------------------------------------------
    def _filters(self, query):
        """WHERE clause and parameters for a query dict (all values already strings/lists)."""
        where, params = ["1=1"], []

        def many(name):
            value = query.get(name) or []
            return [value] if isinstance(value, str) else [v for v in value if v]

        platforms = many("platform")
        if platforms:
            bad = [p for p in platforms if p not in PLATFORMS]
            if bad:
                raise ReviewError(f"未知平台：{bad[0]}")
            where.append(f"r.platform IN ({','.join('?' * len(platforms))})")
            params += platforms
        roles = many("role")
        if roles:
            where.append(f"r.role IN ({','.join('?' * len(roles))})")
            params += roles
        elif not query.get("include_brand"):
            where.append("r.role != 'brand_account'")
        if not query.get("include_irrelevant"):
            where.append("r.relevant = 1")
        if not query.get("include_empty"):
            where.append("LENGTH(r.text) > 0")
        for name, op in (("rating_min", ">="), ("rating_max", "<=")):
            value = query.get(name)
            if value not in (None, ""):
                try:
                    params.append(int(value))
                except (TypeError, ValueError):
                    raise ReviewError("星级筛选无效") from None
                where.append(f"r.rating {op} ?")
        if query.get("language") in {"en", "other"}:
            where.append("r.language = ?")
            params.append(query["language"])
        if query.get("sku"):
            where.append("r.sku = ?")
            params.append(query["sku"])
        if query.get("product"):
            where.append("product_key(r.product) = ?")
            params.append(_product_key(query["product"]))
        for kind, valid in TAG_KINDS.items():
            for tag in many(kind):
                if tag == "none":  # not tagged at all: the rules found nothing to say about it
                    where.append("NOT EXISTS (SELECT 1 FROM review_tags t WHERE t.review_id = r.id AND t.kind = ?)")
                    params.append(kind)
                    continue
                if tag not in valid:
                    raise ReviewError(f"未知标签：{tag}")
                where.append("EXISTS (SELECT 1 FROM review_tags t WHERE t.review_id = r.id AND t.kind = ? AND t.tag = ?)")
                params += [kind, tag]
        video = query.get("video")
        if video:
            if not re.fullmatch(r"\d{6,25}", str(video)):
                raise ReviewError("视频编号无效")
            where.append("(r.platform = 'tiktok_video' AND r.source_ref = ?)")
            params.append(str(video))
        since = query.get("since")
        if since:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(since)):
                raise ReviewError("日期应写成 YYYY-MM-DD")
            where.append("(r.first_seen IS NOT NULL AND r.first_seen >= ?)")  # reviews that were already there at the start have no date
            params.append(str(since))
        replied = query.get("replied")
        if replied:
            if replied not in {"yes", "no"}:
                raise ReviewError("回复情况筛选无效")
            # Only TikTok video comments record who answered whom, so the filter is limited to those.
            where.append(f"(r.platform = 'tiktok_video' AND r.is_reply = 0 AND {'' if replied == 'yes' else 'NOT '}EXISTS (SELECT 1 FROM reviews c WHERE c.parent_id = r.id))")
        for term in parse_query((query.get("q") or "").strip()):
            sql, values = _term_sql(term)
            where.append(sql)
            params += values
        return " AND ".join(where), params

    def _hydrate(self, connection, rows):
        ids = [r["id"] for r in rows]
        state = self._state()
        tags = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for tag in connection.execute(f"SELECT review_id, kind, tag FROM review_tags WHERE review_id IN ({','.join('?' * len(chunk))}) ORDER BY kind, tag", chunk):
                tags.setdefault(tag["review_id"], {kind: [] for kind in TAG_KINDS})[tag["kind"]].append(tag["tag"])
        # Who answered whom is recorded for TikTok video comments only; for the rest "replies" stays None (unknown).
        tops = [r["id"] for r in rows if r["platform"] == "tiktok_video" and not r["is_reply"]]
        replies = {}
        for i in range(0, len(tops), 500):
            chunk = tops[i:i + 500]
            for row in connection.execute(
                    f"SELECT parent_id, COUNT(*) AS n, SUM(role = 'brand_account') AS brand FROM reviews WHERE parent_id IN ({','.join('?' * len(chunk))}) GROUP BY parent_id", chunk):
                replies[row["parent_id"]] = {"total": row["n"], "brand": row["brand"] or 0}
        top_ids = set(tops)
        result = []
        for r in rows:
            item = dict(r)
            item["tags"] = tags.get(r["id"], {kind: [] for kind in TAG_KINDS})
            item["replies"] = replies.get(r["id"], {"total": 0, "brand": 0}) if r["id"] in top_ids else None
            # tag -> True / False for the tags somebody added / removed by hand on this review
            item["manual"] = {kind: dict(changes) for kind, changes in (state["tags"].get(r["id"]) or {}).items() if isinstance(changes, dict) and changes}
            pick = state["picks"].get(r["id"])
            item["pick"] = {"note": str(pick.get("note") or ""), "added": pick.get("added")} if isinstance(pick, dict) else None
            result.append(item)
        return result

    def search(self, query, limit=50, offset=0, sort="likes"):
        if sort not in SORTS:
            raise ReviewError("排序方式无效")
        limit = max(1, min(int(limit), 2000))
        offset = max(0, int(offset))
        with self.lock:
            connection = self.connect()
            try:
                where, params = self._filters(query)
                total = connection.execute(f"SELECT COUNT(*) FROM reviews r WHERE {where}", params).fetchone()[0]
                rows = connection.execute(f"SELECT r.* FROM reviews r WHERE {where} ORDER BY {SORTS[sort]}, r.id LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
                # source counts ignore the source filter itself, so choosing one source keeps the others' counts visible
                where_all_platforms, params_all_platforms = self._filters({**query, "platform": []})
                facets = {
                    "platform": {r[0]: r[1] for r in connection.execute(f"SELECT r.platform, COUNT(*) FROM reviews r WHERE {where_all_platforms} GROUP BY r.platform", params_all_platforms)},
                    "role": {r[0]: r[1] for r in connection.execute(f"SELECT r.role, COUNT(*) FROM reviews r WHERE {where} GROUP BY r.role", params)},
                    "rating": {str(r[0]): r[1] for r in connection.execute(f"SELECT r.rating, COUNT(*) FROM reviews r WHERE {where} AND r.rating IS NOT NULL GROUP BY r.rating", params)},
                }
                for kind in TAG_KINDS:
                    facets[kind] = {r[0]: r[1] for r in connection.execute(
                        f"SELECT t.tag, COUNT(*) FROM review_tags t JOIN reviews r ON r.id = t.review_id WHERE t.kind = ? AND {where} GROUP BY t.tag", [kind] + params)}
                    if kind != "competitor":  # "no competitor named" is the normal case, not a gap worth a chip
                        untagged = connection.execute(
                            f"SELECT COUNT(*) FROM reviews r WHERE {where} AND NOT EXISTS (SELECT 1 FROM review_tags t WHERE t.review_id = r.id AND t.kind = ?)", params + [kind]).fetchone()[0]
                        if untagged:
                            facets[kind]["none"] = untagged
                return {"total": total, "offset": offset, "limit": limit, "reviews": self._hydrate(connection, rows), "facets": facets,
                        "query": describe_query(query.get("q"))}
            finally:
                connection.close()

    def review(self, identifier):
        with self.lock:
            connection = self.connect()
            try:
                row = connection.execute("SELECT * FROM reviews WHERE id = ?", (identifier,)).fetchone()
                if not row:
                    return None
                item = self._hydrate(connection, [row])[0]
                # the rest of the conversation: the parent and its replies, or this comment's replies
                top = row["parent_id"] or row["id"]
                thread = connection.execute("SELECT * FROM reviews WHERE (id = ? OR parent_id = ?) AND id != ? ORDER BY is_reply, posted_on", (top, top, row["id"])).fetchall()
                item["thread"] = self._hydrate(connection, thread)
                return item
            finally:
                connection.close()

    def summary(self):
        with self.lock:
            connection = self.connect()
            try:
                total = self._count(connection)
                base = "LENGTH(r.text) > 0 AND r.relevant = 1 AND r.role != 'brand_account'"
                platforms = []
                for p in PLATFORMS:
                    row = connection.execute(
                        "SELECT COUNT(*) AS n, SUM(CASE WHEN LENGTH(text) > 0 THEN 1 ELSE 0 END) AS texts, "
                        "SUM(CASE WHEN role = 'brand_account' THEN 1 ELSE 0 END) AS brand, SUM(CASE WHEN relevant = 0 THEN 1 ELSE 0 END) AS irrelevant, "
                        "AVG(rating) AS rating, MIN(posted_on) AS first, MAX(posted_on) AS last FROM reviews WHERE platform = ?", (p,)).fetchone()
                    platforms.append({"platform": p, "label": PLATFORM_LABELS[p], "total": row["n"] or 0, "with_text": row["texts"] or 0, "brand": row["brand"] or 0,
                                      "irrelevant": row["irrelevant"] or 0, "avg_rating": round(row["rating"], 2) if row["rating"] is not None else None,
                                      "first": row["first"], "last": row["last"]})
                tags = {kind: {r[0]: r[1] for r in connection.execute(
                    f"SELECT t.tag, COUNT(*) FROM review_tags t JOIN reviews r ON r.id = t.review_id WHERE t.kind = ? AND {base} GROUP BY t.tag", (kind,))} for kind in TAG_KINDS}
                roles = {r[0]: r[1] for r in connection.execute("SELECT role, COUNT(*) FROM reviews GROUP BY role")}
                imports = [dict(r) for r in connection.execute("SELECT source_file, platform, rows, skipped FROM imports ORDER BY platform, source_file")]
                skus = [r[0] for r in connection.execute("SELECT DISTINCT sku FROM reviews WHERE sku IS NOT NULL ORDER BY sku")]
                products = [r[0] for r in connection.execute("SELECT DISTINCT product FROM reviews WHERE product != '' ORDER BY product")]
                meta = {r[0]: r[1] for r in connection.execute("SELECT key, value FROM meta")}
                state = self._state()
                since = (date.today() - timedelta(days=NEW_DAYS)).isoformat()
                recent = connection.execute(f"SELECT COUNT(*) FROM reviews r WHERE {base} AND r.first_seen IS NOT NULL AND r.first_seen >= ?", (since,)).fetchone()[0]
                edits = sum(len(changes) for kinds in state["tags"].values() if isinstance(kinds, dict) for changes in kinds.values() if isinstance(changes, dict))
                return {"total": total, "default_visible": connection.execute(f"SELECT COUNT(*) FROM reviews r WHERE {base}").fetchone()[0],
                        "platforms": platforms, "tags": tags, "roles": roles, "imports": imports, "skus": skus, "products": products, "synced_at": meta.get("synced_at"),
                        "coverage": self.coverage(), "videos": self._video_counts(connection), "new_recent": recent, "new_since": since, "new_days": NEW_DAYS,
                        "tag_edits": edits, "picks": len(state["picks"]), "state_error": self.state_error,
                        "labels": {"nature": NATURE, "topic": TOPIC, "competitor": COMPETITOR, "role": ROLE_LABELS, "platform": PLATFORM_LABELS}}
            finally:
                connection.close()

    @staticmethod
    def _video_counts(connection):
        """TikTok videos that have comments in the library, most commented first: how many comments (as the default view
        counts them, without brand replies) and how many questions nobody answered."""
        base = "r.relevant = 1 AND r.role != 'brand_account' AND LENGTH(r.text) > 0"
        rows = connection.execute(
            "SELECT r.source_ref AS video_id, COUNT(*) AS comments, MAX(r.posted_on) AS last_comment, "
            f"SUM(CASE WHEN r.is_reply = 0 AND {_tagged('nature', ('question',))} AND NOT EXISTS (SELECT 1 FROM reviews c WHERE c.parent_id = r.id) THEN 1 ELSE 0 END) AS open_questions "
            f"FROM reviews r WHERE r.platform = 'tiktok_video' AND r.source_ref IS NOT NULL AND {base} GROUP BY r.source_ref ORDER BY comments DESC, r.source_ref")
        return [{"video_id": r["video_id"], "comments": r["comments"], "open_questions": r["open_questions"] or 0, "last_comment": r["last_comment"]} for r in rows]

    def video_counts(self):
        """video id -> comment counts, for pages that list videos. Brings the library up to date first, the way the review pages
        do, when there are sources; with no sources and no library it is empty and creates nothing."""
        if self.sources.is_dir():
            self.sync()
        with self.lock:
            if not self.database.is_file():
                return {}
            connection = self.connect()
            try:
                return {v["video_id"]: v for v in self._video_counts(connection)}
            finally:
                connection.close()

    # ---- what people do to the library -----------------------------------------------------------
    @staticmethod
    def _rule_tags(row):
        """The tags the rules give a review, before anyone changed them by hand."""
        nature, topics, _ = classify(row["text"], title=row["title"] or "", rating=row["rating"], role=row["role"], is_reply=bool(row["is_reply"]))
        return {"nature": set(nature), "topic": set(topics), "competitor": competitors(f"{row['title'] or ''} {row['text']}")}

    def set_tag(self, identifier, kind, tag, present):
        """Add or remove one tag by hand. The change is kept in the records file, so a rebuild applies it again; a change that
        only restores what the rules say is dropped from it. Returns the review as the page shows it."""
        if kind not in TAG_KINDS or tag not in TAG_KINDS[kind]:
            raise ReviewError(f"未知标签：{kind}/{tag}")
        if not isinstance(present, bool):
            raise ReviewError("标签状态必须是 true 或 false")
        with self.lock:
            connection = self.connect()
            try:
                row = connection.execute("SELECT * FROM reviews WHERE id = ?", (identifier,)).fetchone()
                if not row:
                    raise ReviewError("评论库里没有这条评论")
                by_rules = tag in self._rule_tags(row)[kind]

                def change(data):
                    kinds = data["tags"].setdefault(identifier, {})
                    changes = kinds.setdefault(kind, {})
                    if present == by_rules:
                        changes.pop(tag, None)
                    else:
                        changes[tag] = present
                    if not changes:
                        kinds.pop(kind, None)
                    if not kinds:
                        data["tags"].pop(identifier, None)
                self._edit_state(change)
                if present:
                    connection.execute("INSERT OR IGNORE INTO review_tags VALUES (?,?,?)", (identifier, kind, tag))
                else:
                    connection.execute("DELETE FROM review_tags WHERE review_id = ? AND kind = ? AND tag = ?", (identifier, kind, tag))
                return self._hydrate(connection, [row])[0]
            finally:
                connection.close()

    def tag_edits(self):
        """Every tag changed by hand, with the review's text: what the rules got wrong, for tuning them."""
        with self.lock:
            if not self.database.is_file():
                return []
            connection = self.connect()
            try:
                result = []
                for identifier, kinds in sorted(self._state()["tags"].items()):
                    row = connection.execute("SELECT platform, text FROM reviews WHERE id = ?", (identifier,)).fetchone()
                    if not row or not isinstance(kinds, dict):
                        continue
                    for kind, changes in sorted(kinds.items()):
                        for tag, present in sorted(changes.items()) if isinstance(changes, dict) else ():
                            if tag in TAG_KINDS.get(kind, {}):
                                result.append({"id": identifier, "platform": row["platform"], "kind": kind, "tag": tag, "label": TAG_KINDS[kind][tag],
                                               "present": bool(present), "text": row["text"][:300]})
                return result
            finally:
                connection.close()

    def set_pick(self, identifier, picked, note=None):
        """Put a review in the basket of quotes for a script, or take it out; note=None keeps the note it has. Returns the
        pick (None once removed) and how many reviews are in the basket."""
        if not isinstance(picked, bool):
            raise ReviewError("摘录状态必须是 true 或 false")
        if note is not None and (not isinstance(note, str) or len(note) > NOTE_LIMIT):
            raise ReviewError(f"备注须是 {NOTE_LIMIT} 字以内的文字")
        with self.lock:
            connection = self.connect()
            try:
                if not connection.execute("SELECT 1 FROM reviews WHERE id = ?", (identifier,)).fetchone():
                    raise ReviewError("评论库里没有这条评论")
            finally:
                connection.close()
            outcome = {}

            def change(data):
                picks = data["picks"]
                if not picked:
                    picks.pop(identifier, None)
                    outcome["pick"] = None
                    return
                if identifier not in picks and len(picks) >= MAX_PICKS:
                    raise ReviewError(f"素材篮里的评论摘录最多 {MAX_PICKS} 条")
                current = picks.get(identifier) if isinstance(picks.get(identifier), dict) else {}
                picks[identifier] = {"note": str(current.get("note") or "") if note is None else note.strip(),
                                     "added": current.get("added") or datetime.now().isoformat(timespec="microseconds")}
                outcome["pick"] = picks[identifier]
            data = self._edit_state(change)
            return {"pick": outcome["pick"], "picks": len(data["picks"])}

    def replace_picks(self, changes):
        """Several basket changes in one write: None takes a pick out; a dict puts an earlier pick back exactly (note and
        time) unless the review is gone or was picked again meanwhile."""
        with self.lock:
            wanted = [identifier for identifier, pick in changes.items() if pick is not None]
            known = set()
            if wanted:
                connection = self.connect()
                try:
                    for i in range(0, len(wanted), 500):
                        chunk = wanted[i:i + 500]
                        known.update(row[0] for row in connection.execute(f"SELECT id FROM reviews WHERE id IN ({','.join('?' * len(chunk))})", chunk))
                finally:
                    connection.close()

            def change(data):
                picks = data["picks"]
                for identifier, pick in changes.items():
                    if pick is None:
                        picks.pop(identifier, None)
                    elif identifier in known and identifier not in picks:
                        if len(picks) >= MAX_PICKS:
                            raise ReviewError(f"素材篮里的评论摘录最多 {MAX_PICKS} 条")
                        picks[identifier] = {"note": str(pick.get("note") or ""),
                                             "added": pick.get("added") or datetime.now().isoformat(timespec="microseconds")}
            self._edit_state(change)

    def set_pick_notes(self, notes):
        """Set the notes of several picked quotes in one write; quotes no longer picked are left out."""
        if any(not isinstance(note, str) or len(note) > NOTE_LIMIT for note in notes.values()):
            raise ReviewError(f"备注须是 {NOTE_LIMIT} 字以内的文字")
        with self.lock:
            def change(data):
                for identifier, note in notes.items():
                    if isinstance(data["picks"].get(identifier), dict):
                        data["picks"][identifier]["note"] = note.strip()
            self._edit_state(change)

    def picks(self):
        """The picked reviews as the page shows them (notes included), the most recently picked first."""
        with self.lock:
            if not self.database.is_file():
                return []
            connection = self.connect()
            try:
                picks = self._state()["picks"]
                ids = sorted(picks, key=lambda i: str((picks[i] or {}).get("added") or "") if isinstance(picks[i], dict) else "", reverse=True)
                rows = []
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    rows += connection.execute(f"SELECT * FROM reviews WHERE id IN ({','.join('?' * len(chunk))})", chunk).fetchall()
                order = {identifier: n for n, identifier in enumerate(ids)}
                rows.sort(key=lambda r: order[r["id"]])
                return self._hydrate(connection, rows)
            finally:
                connection.close()

    # ---- digest for writing and review tasks -----------------------------------------------
    def covers(self, product):
        """True when this library holds reviews for the product a case names (a code like 202H, not part of a longer one)."""
        product = _product_key(product)
        if not product:
            return False
        with self.lock:
            if self.database.is_file():
                connection = sqlite3.connect(self.database.resolve().as_uri() + "?mode=ro", uri=True)
                connection.create_function("product_key", 1, _product_key, deterministic=True)
                try:
                    return connection.execute("SELECT 1 FROM reviews WHERE product_key(product)=? LIMIT 1", (product,)).fetchone() is not None
                finally:
                    connection.close()
            merged, _, _ = self._source_records(self.source_files())
            return any(_product_key(record.get("product", PRODUCT)) == product for _, _, record in merged.values())

    def digest(self, max_chars=DIGEST_CHARS, *, product=PRODUCT):
        """The library read for a writer or reviewer (see _digest); None when there is nothing to read yet."""
        with self.lock:
            if not self.database.is_file():
                return None
            connection = self.connect()
            try:
                return self._digest(connection, max_chars, product=product)
            finally:
                connection.close()

    def _digest(self, connection, max_chars=DIGEST_CHARS, *, product=PRODUCT):
        """A short, fixed-shape reading of the library: what viewers doubt, what buyers say is limited, which questions
        nobody answered, how buyers describe results and getting started. Quotes are cut to the sentence a rule matched.
        The compact AI reading deliberately omits reviewer names."""
        connection.create_function("product_key", 1, _product_key, deterministic=True)
        product = _product_key(product)
        base = "r.relevant = 1 AND r.role != 'brand_account' AND LENGTH(r.text) > 0 AND product_key(r.product) = ?"
        parameters = (product,)
        visible = connection.execute(f"SELECT COUNT(*) FROM reviews r WHERE {base}", parameters).fetchone()[0]
        if not visible:
            return None
        buyers = "r.role IN ('verified_buyer','unverified_buyer','incentivized_buyer')"
        trial_last = "(r.role = 'incentivized_buyer')"  # trial reviews have a stake in the product, so they come after the others
        by_likes = "r.likes IS NULL, r.likes DESC, r.id"
        unanswered = "NOT EXISTS (SELECT 1 FROM reviews c WHERE c.parent_id = r.id)"
        buckets = (
            ("buyer_limits", f"{buyers} AND {_tagged('nature', ('defect',))} AND {_tagged('topic', DIGEST_HARDWARE)}",
             f"{trial_last}, r.rating IS NULL, r.rating, {by_likes}", 6, (DEFECT, *(TOPIC_RULES[t] for t in DIGEST_HARDWARE)), 260),
            # a doubt, a question or a complaint about the demo: viewers describing their own sharpness test are not doubting it
            ("viewer_doubts", f"r.role IN ('viewer','peer') AND {_tagged('topic', ('proof', 'ai_voice', 'stability'))} AND {_tagged('nature', ('objection', 'question', 'defect'))} "
                              f"AND NOT {_tagged('nature', ('compare',))} AND NOT {_tagged('topic', ('handsharp',))}",
             by_likes, 6, (OBJECTION, *(TOPIC_RULES[t] for t in ("proof", "ai_voice", "stability"))), 200),
            # viewers saying a selling point is not needed (no mirror edge for daily use, just learn the stone): a stance a script can agree with
            ("viewer_pushback", f"r.role IN ('viewer','peer') AND {_tagged('topic', DIGEST_PUSHBACK)} AND {_tagged('nature', ('objection',))} "
                                f"AND NOT {_tagged('topic', ('proof', 'ai_voice', 'stability'))}",
             by_likes, 5, (OBJECTION, *(TOPIC_RULES[t] for t in DIGEST_PUSHBACK)), 200),
            ("unanswered_questions", f"r.platform = 'tiktok_video' AND r.is_reply = 0 AND {_tagged('nature', ('question',))} AND {unanswered}",
             f"CASE WHEN {_tagged('topic', DIGEST_ASKED_ABOUT)} THEN 0 ELSE 1 END, {by_likes}", 8, (), 160),
            ("buyer_results", f"{buyers} AND (r.rating IS NULL OR r.rating >= 4) AND ({_tagged('nature', ('result',))} OR {_tagged('topic', ('many_knives', 'repair', 'mirror'))})",
             f"{trial_last}, {by_likes}", 4, (RESULT, *(TOPIC_RULES[t] for t in ("many_knives", "repair", "mirror"))), 260),
            ("learning_curve", f"{buyers} AND {_tagged('topic', ('learning',))} AND NOT {_tagged('topic', ('wrong_sku',))}",
             f"{trial_last}, LENGTH(r.text) DESC, r.id", 3, (TOPIC_RULES["learning"],), 260),
        )
        picks = self._state()["picks"]
        picked = sorted((i for i in picks if isinstance(picks[i], dict)), key=lambda i: str(picks[i].get("added") or ""), reverse=True)
        picked_rows = {r["id"]: r for r in connection.execute(
            f"SELECT r.* FROM reviews r WHERE r.id IN ({','.join('?' * len(picked))}) AND product_key(r.product) = ?",
            picked + list(parameters))} if picked else {}
        picked = [identifier for identifier in picked if identifier in picked_rows][:8]
        digest, used = {}, set(picked)
        sources = {PLATFORM_LABELS[r[0]]: r[1] for r in connection.execute(f"SELECT r.platform, COUNT(*) FROM reviews r WHERE {base} GROUP BY r.platform ORDER BY 2 DESC, 1", parameters)}
        meta = {r[0]: r[1] for r in connection.execute("SELECT key, value FROM meta")}
        digest.update(product=product, synced_at=meta.get("synced_at"), reviews=visible, sources=sources,
                      coverage={PLATFORM_LABELS[p]: note for p, note in self.coverage().items() if PLATFORM_LABELS.get(p) in sources},
                      topics={TOPIC[r[0]]: r[1] for r in connection.execute(
                          f"SELECT t.tag, COUNT(*) AS n FROM review_tags t JOIN reviews r ON r.id = t.review_id WHERE t.kind = 'topic' AND {base} GROUP BY t.tag ORDER BY n DESC, t.tag LIMIT 10", parameters)})
        for name, where, order, limit, patterns, width in buckets:
            items, seen = [], set()
            for row in connection.execute(f"SELECT r.* FROM reviews r WHERE {base} AND {where} ORDER BY {order} LIMIT {limit * 4}", parameters):
                quote = excerpt(row["text"], *patterns, limit=width)
                key = re.sub(r"\W+", "", quote.lower())
                if row["id"] in used or key in seen:
                    continue  # the same review in two lists, or the same words posted twice
                used.add(row["id"])
                seen.add(key)
                item = {"id": row["id"], "source": PLATFORM_LABELS[row["platform"]], "who": ROLE_LABELS[row["role"]]}
                if row["rating"] is not None:
                    item["stars"] = row["rating"]
                if row["likes"]:
                    item["likes"] = row["likes"]
                items.append({**item, "date": row["posted_on"], "quote": quote})
                if len(items) == limit:
                    break
            digest[name] = items
        if picked:
            by_id = picked_rows
            digest["user_picks"] = [{"id": i, "source": PLATFORM_LABELS[by_id[i]["platform"]], "who": ROLE_LABELS[by_id[i]["role"]], "date": by_id[i]["posted_on"],
                                     "quote": excerpt(by_id[i]["text"], limit=260), "note": str(picks[i].get("note") or "")} for i in picked if i in by_id]
        digest["read_me"] = DIGEST_NOTES
        # Stay inside the size a prompt can carry: drop the tail of the longest list, keeping at least two in each.
        while len(json.dumps(digest, ensure_ascii=False)) > max_chars:
            name = max((b[0] for b in buckets), key=lambda b: len(digest[b]))
            if len(digest[name]) <= 2:
                break
            digest[name].pop()
        return digest
