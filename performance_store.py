"""TikTok Shop video-analytics store for the workbench.

A separate SQLite file under the runtime folder; the CaseStore stays the only
record of cases. Imports are idempotent: a file's date or range replaces the
rows previously imported for it.

Export shapes, recognised by the header row:
- per-video ("Creator name, ..., Video ID, ...") for one date range. One-day
  ranges go to `daily`, longer ranges to `periods` with `span_days`.
- all-video totals ("Time, Video-attributed GMV, ...") with one row per day,
  kept in `totals_daily`; not mixed into per-video figures.
- all-creator videos (the per-video header plus "Creator country"), usually one
  month per file, kept in `creator_periods` for the overseas creator view; not
  mixed into the self-made figures either.

- photo (图文) per-product ("Name of product, Photo attributed GMV ($), ..."), one export range per file, kept in
  `photo_periods`. It has no views or per-post rows, and its GPM is per thousand impressions, so it is never mixed
  with the video figures.

- photo items (逐条图文) per post ("Photo item ID, ..."), one export range per file (usually a month), kept in
  `photo_items`. Unlike the per-product table it has views and the post's own ID, so a registered photo post can be
  found by it. The file carries two columns both headed "Photo attributed items sold"; only the first is read.

Shops: every table carries a `shop` column. A file's shop is the first folder under a source folder when that folder
is a shop folder (本土店 / 跨境店), otherwise the local shop. Deleting a re-imported range only touches its own shop.

Per-video figures combine daily rows and 7-day periods without double
counting: a day covered by a weekly export uses the weekly rows. Monthly
ranges are kept for reference only.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
import math
import statistics
import re
import shutil
import sqlite3
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import openpyxl

from public_stats import COUNTS as PUBLIC_COUNTS

# Internal traffic bands for comparable data and coverage checks. These are
# measurements only; the workbench displays the separate human review_status.
THRESHOLDS = ((10000, "focus"), (3000, "follow"), (1000, "watch"), (0, "drop"))
STATUS_LABELS = {"focus": "窗口播放 ≥10,000", "follow": "窗口播放 3,000–9,999", "watch": "窗口播放 1,000–2,999", "drop": "窗口播放 <1,000",
                 "pending": "待数据", "incomplete": "数据不全"}
# Internal data signals; they do not classify a video's reference value.
# - late: watch/drop that later reached the focus view bar or LATE_ORDERS orders.
# - no_orders: focus/follow still at zero orders NO_ORDER_DAYS after posting.
LATE_VIEWS, LATE_ORDERS, NO_ORDER_DAYS = 10000, 5, 30
FLAG_LABELS = {"late": "后起", "no_orders": "有播放没出单"}
# Reference for videos whose first two weeks were never exported, from cumulative orders.
REFERENCE = ((10, "strong", "累计出单多"), (1, "sold", "有出单"), (0, "none", "覆盖期内 0 单"))

COLUMNS = {
    "Creator name": "account", "Creator ID": "creator_id", "Video Info": "title", "Video ID": "video_id",
    "Time": "posted_at", "Products": "products",
    "VV": "views", "Video views": "views", "Likes": "likes", "Comments": "comments", "Shares": "shares",
    "New followers": "new_followers", "V-to-L clicks": "v2l_clicks",
    "Product Impressions": "impressions", "Product Clicks": "clicks", "Unique customers": "customers",
    "Attributed SKU orders": "orders", "Video SKU orders": "orders_direct", "Video indirect SKU orders": "orders_indirect",
    "Video-attributed items sold": "items", "Video items sold": "items_direct", "Video indirect items sold": "items_indirect",
    "Video-attributed GMV ($)": "gmv", "Video GMV ($)": "gmv_direct", "Video indirect GMV ($)": "gmv_indirect",
    "GPM ($)": "gpm", "Click-through rate (Video)": "ctr_pct", "V-to-L rate": "v2l_rate_pct",
    "Video Finish Rate": "finish_rate_pct", "CTOR (SKU order)": "ctor_pct", "Diagnosis": "diagnosis",
}
# All-video daily totals; its rates are fractions (0.0244) and stored as percent.
TOTAL_COLUMNS = {
    "Time": "date", "Video-attributed GMV ($)": "gmv", "Video GMV ($)": "gmv_direct", "Video indirect GMV ($)": "gmv_indirect",
    "Video views": "views", "GPM ($)": "gpm", "Attributed SKU orders": "orders", "Video SKU orders": "orders_direct",
    "Video indirect SKU orders": "orders_indirect", "Avg. daily customers": "customers", "Product viewers": "product_viewers",
    "Product Impressions": "impressions", "Product Clicks": "clicks",
    "Click-through rate (Video)": "ctr_pct", "CTOR (SKU order)": "ctor_pct",
}
# Folder names under a source folder that name a shop, and the content-type folders inside them.
SHOPS = {"local": "本土店", "cross": "跨境店"}
SHOP_FOLDERS = {name: key for key, name in SHOPS.items()}
DEFAULT_SHOP = "local"
KIND_FOLDERS = {"video": "视频", "totals": "销售", "creators": "达人", "photo": "图文", "photo_items": "图文"}
# The per-product photo export; its rates are fractions (0.0138) and stored as percent.
PHOTO_COLUMNS = {
    "Name of product": "product", "Product ID": "product_id", "Photo attributed GMV ($)": "gmv",
    "Attributed SKU orders": "orders", "Product Impressions": "impressions",
    "Click-through rate": "ctr_pct", "CTOR (SKU order)": "ctor_pct", "GPM ($)": "gpm",
}
PHOTO_METRICS = [c for c in PHOTO_COLUMNS.values() if c not in {"product", "product_id"}]
# The per-post photo export. Rates come as "2.18%" text (or fractions) and are stored as percent.
PHOTO_ITEM_COLUMNS = {
    "Creator name": "account", "Creator ID": "creator_id", "Photo info": "title", "Photo item ID": "item_id", "Time": "posted_at",
    "Products": "products", "Photo attributed GMV ($)": "gmv", "Photo attributed items sold": "sold", "CTOR (SKU order)": "ctor_pct",
    "Views": "views", "Product Impressions": "impressions", "Click-through rate": "ctr_pct", "Likes": "likes", "Shares": "shares",
    "Comments": "comments", "GPM": "gpm",
}
PHOTO_ITEM_TEXT = ["account", "creator_id", "title", "posted_at", "products"]
PHOTO_ITEM_METRICS = [c for c in PHOTO_ITEM_COLUMNS.values() if c not in {*PHOTO_ITEM_TEXT, "item_id"}]
# The all-creator export: every account that sold through a video, ours included.
CREATOR_COLUMNS = {**COLUMNS, "Creator country": "country"}
TEXT = {"account", "creator_id", "title", "video_id", "posted_at", "products", "diagnosis", "date", "country", "product", "product_id", "item_id"}
METRICS = [c for c in dict.fromkeys(COLUMNS.values()) if c not in TEXT]
TOTAL_METRICS = [c for c in TOTAL_COLUMNS.values() if c != "date"]
VIDEO_TEXT = ["account", "creator_id", "title", "posted_at", "products"]
CREATOR_TEXT = ["account", "creator_id", "country", "title", "posted_at", "products"]
# Handles holding the brand name are our own (official) accounts even when no self-made export lists them.
BRAND = "sharpal"
SUMMED = ["views", "likes", "comments", "shares", "impressions", "clicks", "orders", "items", "gmv"]
FACT_COLUMNS = ("video_id", "start", "end", "account", "title", "posted_at", "products", *SUMMED, "source_file", "shop")
MAX_CACHE_BYTES = 64 * 1024 * 1024
RATE_MIN_VIEWS = 300  # a week below this has rates too noisy to serve as a reference
MAX_CACHE_ENTRIES = 128
_MISS = object()


def _memory_size(value):
    """Conservative retained Python size, including nested containers and shared keys."""
    pending, seen, total = [value], set(), 0
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            pending.extend(item)
    return total

def _totals_table(name="totals_daily"):
    return (f"CREATE TABLE IF NOT EXISTS {name} (shop TEXT NOT NULL DEFAULT 'local', date TEXT, "
            f"{', '.join(f'{m} REAL' for m in TOTAL_METRICS)}, source_file TEXT, PRIMARY KEY (shop, date));")


TOTALS_TABLE = _totals_table()
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS daily (
    video_id TEXT, date TEXT, {", ".join(f"{c} TEXT" for c in VIDEO_TEXT)},
    {", ".join(f"{m} REAL" for m in METRICS)}, diagnosis TEXT, source_file TEXT, shop TEXT NOT NULL DEFAULT 'local',
    PRIMARY KEY (video_id, date)
);
CREATE TABLE IF NOT EXISTS periods (
    video_id TEXT, range_start TEXT, range_end TEXT, span_days INTEGER, {", ".join(f"{c} TEXT" for c in VIDEO_TEXT)},
    {", ".join(f"{m} REAL" for m in METRICS)}, diagnosis TEXT, source_file TEXT, shop TEXT NOT NULL DEFAULT 'local',
    PRIMARY KEY (video_id, range_start, range_end)
);
CREATE TABLE IF NOT EXISTS creator_periods (
    video_id TEXT, range_start TEXT, range_end TEXT, span_days INTEGER, {", ".join(f"{c} TEXT" for c in CREATOR_TEXT)},
    {", ".join(f"{m} REAL" for m in METRICS)}, diagnosis TEXT, source_file TEXT, shop TEXT NOT NULL DEFAULT 'local',
    PRIMARY KEY (video_id, range_start, range_end)
);
{TOTALS_TABLE}
CREATE TABLE IF NOT EXISTS photo_periods (
    shop TEXT NOT NULL, range_start TEXT, range_end TEXT, span_days INTEGER, product_id TEXT, product TEXT,
    {", ".join(f"{m} REAL" for m in PHOTO_METRICS)}, source_file TEXT,
    PRIMARY KEY (shop, range_start, range_end, product_id)
);
CREATE TABLE IF NOT EXISTS photo_items (
    shop TEXT NOT NULL, item_id TEXT, range_start TEXT, range_end TEXT, span_days INTEGER,
    {", ".join(f"{c} TEXT" for c in PHOTO_ITEM_TEXT)}, {", ".join(f"{m} REAL" for m in PHOTO_ITEM_METRICS)}, source_file TEXT,
    PRIMARY KEY (shop, item_id, range_start, range_end)
);
CREATE TABLE IF NOT EXISTS imports (
    source_file TEXT PRIMARY KEY, kind TEXT, range_start TEXT, range_end TEXT, rows INTEGER, modified TEXT, imported_at TEXT,
    shop TEXT NOT NULL DEFAULT 'local', shop_name TEXT
);
CREATE INDEX IF NOT EXISTS daily_date ON daily(date);
CREATE INDEX IF NOT EXISTS periods_span ON periods(span_days, range_start);
"""


class ImportError_(ValueError):
    """An export file that cannot be read as TikTok Shop video analytics."""


def _value(column, raw, fraction=False):
    if raw is None or raw == "":
        return None
    if column in TEXT:
        text = str(raw).strip()
        return text.replace("/", "-") if column in {"posted_at", "date"} else text
    text = str(raw).strip().replace(",", "")
    percent = text.endswith("%")
    try:
        number = float(text.rstrip("%"))
    except ValueError:
        return None
    return number * 100 if fraction and not percent and column.endswith("_pct") else number


def _load_rows(path):
    try:
        sheet = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    except Exception as error:
        raise ImportError_(f"无法打开表格（{type(error).__name__}）") from None
    return list(sheet.iter_rows(values_only=True))


def parse_export(rows):
    """(kind, start, end, records, shop_name). Line 1 is the date range, an optional "Shop Name:" line may follow, then the header."""
    match = re.search(r"(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", str(rows[0][0] if rows and rows[0] else ""))
    if not match or len(rows) < 3:
        raise ImportError_("缺少 [Date Range] 行，不像 TikTok Shop 视频分析导出")
    shop_name = None
    for line in rows[1:4]:
        found = re.match(r"\s*Shop(?: Name)?:\s*(.+?)\s*$", str(line[0] or "")) if line else None
        if found:
            shop_name = found.group(1)
    at = next((i for i, line in enumerate(rows) if i and sum(c not in (None, "") for c in line) >= 2), 2)
    header = list(rows[at])
    names = [h for h in header if h]
    if "Photo item ID" in names:
        kind, mapping, fraction = "photo_items", PHOTO_ITEM_COLUMNS, True
    elif "Name of product" in names:
        kind, mapping, fraction = "photo", PHOTO_COLUMNS, True
    elif "Video ID" in names and "Creator country" in names:
        kind, mapping, fraction = "creators", CREATOR_COLUMNS, False
    elif "Video ID" in names:
        kind, mapping, fraction = "video", COLUMNS, False
    elif names and names[0] == "Time":
        kind, mapping, fraction = "totals", TOTAL_COLUMNS, True
    else:
        raise ImportError_(f"无法识别的表头：{names[:5]}")
    unknown = [h for h in names if h not in mapping]
    if unknown:
        raise ImportError_(f"未识别的列：{unknown}")
    key = {"totals": "date", "photo": "product_id", "photo_items": "item_id"}.get(kind, "video_id")
    # A header written twice (the photo export's "items sold") keeps its first column; the later one is not read.
    skip = {i for i, name in enumerate(header) if name and header.index(name) != i} if kind == "photo_items" else set()
    records = []
    for row in rows[at + 1:]:
        record = {mapping[name]: _value(mapping[name], raw, fraction) for i, (name, raw) in enumerate(zip(header, row)) if name and i not in skip}
        if record.get(key):
            records.append(record)
    return kind, match.group(1), match.group(2), records, shop_name


def read_export(path):
    return parse_export(_load_rows(path))[:4]


def _monday(day):
    return day - timedelta(days=day.weekday())


def product_short(products):
    text = products or ""
    model = re.search(r"\b(\d{3}[A-Z])\b", text)
    return model.group(1) if model else (" ".join(text.replace("SHARPAL", "").split()[:3]) or "未标注")


def headline(title):
    """Title without the trailing hashtags the export appends."""
    return re.split(r"\s*#", title or "", maxsplit=1)[0].strip() or (title or "")


def product_models(products):
    """Every product a video links, as short names in listed order ("Name(id),Name(id)")."""
    names = [part.strip(" ,") for part in re.split(r"\(\d{10,}\)", products or "")]
    return list(dict.fromkeys(product_short(name) for name in names if name)) or [product_short(products)]


def _period_labels(periods):
    """One name per export range: the month when it lies inside one, else its dates; repeats fall back to dates."""
    labels = [f"{int(s[5:7])}月" if s[:7] == e[:7] else f"{s[5:]} 至 {e[5:]}" for s, e in periods]
    if len({s[:4] for s, _ in periods}) > 1:
        labels = [f"{s[2:4]}年{label}" if label.endswith("月") else label for (s, _), label in zip(periods, labels)]
    return [label if labels.count(label) == 1 else f"{s[5:]} 至 {e[5:]}" for (s, e), label in zip(periods, labels)]


class PerformanceStore:
    def __init__(self, runtime_root, sources=()):
        self.root = Path(runtime_root) / "performance"
        self.database = self.root / "performance.sqlite"
        self.uploads = self.root / "uploads"
        self.sources = [Path(s) for s in sources]
        # Public-page counts overlay each video's display fields on read; exported values stay available separately.
        self.public = None
        self.lock = threading.RLock()
        self._cache = OrderedDict()
        self._cache_generation = None
        self._cache_bytes = 0

    def connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, isolation_level=None, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.executescript(SCHEMA)
            self._migrate(connection)
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _migrate(connection):
        """Databases made before shops existed: every row so far is the local shop's."""
        def columns(table):
            return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        pending = [t for t in ("daily", "periods", "creator_periods", "imports") if "shop" not in columns(t)]
        if not pending and "shop" in columns("totals_daily") and "shop_name" in columns("imports"):
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            for table in ("daily", "periods", "creator_periods", "imports"):
                if "shop" not in columns(table):
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN shop TEXT NOT NULL DEFAULT 'local'")
            if "shop_name" not in columns("imports"):
                connection.execute("ALTER TABLE imports ADD COLUMN shop_name TEXT")
            if "shop" not in columns("totals_daily"):
                # The key becomes (shop, date): the old one, date alone, would let one shop's day overwrite another's.
                names = ", ".join(["date", *TOTAL_METRICS, "source_file"])
                connection.execute(_totals_table("totals_daily_new"))
                connection.execute(f"INSERT INTO totals_daily_new ({names}) SELECT {names} FROM totals_daily")
                connection.execute("DROP TABLE totals_daily")
                connection.execute("ALTER TABLE totals_daily_new RENAME TO totals_daily")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise

    def _query_connection(self):
        """Pin one SQLite read snapshot; close it after each query to permit file replacement."""
        connection = self.connect()
        try:
            connection.execute("PRAGMA temp_store=MEMORY")
            before = self.signature()
            connection.execute("BEGIN")
            connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            generation = before if before == self.signature() else None
            return connection, generation
        except Exception:
            connection.close()
            raise

    def _cached_value(self, key):
        if key not in self._cache:
            return _MISS
        value, weight = self._cache.pop(key)
        self._cache[key] = (value, weight)
        return value

    def _remember(self, key, value, generation):
        # Results from a pinned old snapshot may still be returned to its reader,
        # but must never become the cache for newer commits made outside our lock.
        if generation != self._cache_generation or generation != self.signature():
            return
        weight = _memory_size((key, value))
        if weight > MAX_CACHE_BYTES:
            return
        previous = self._cache.pop(key, None)
        if previous is not None:
            self._cache_bytes -= previous[1]
        while self._cache and (self._cache_bytes + weight > MAX_CACHE_BYTES or len(self._cache) >= MAX_CACHE_ENTRIES):
            _, (_, removed) = self._cache.popitem(last=False)
            self._cache_bytes -= removed
        self._cache[key] = (value, weight)
        self._cache_bytes += weight

    def _read_cached(self, key, loader, clone=copy.deepcopy):
        # One lock coalesces identical misses and lets the different dashboard
        # queries reuse the normalized facts from this database generation.
        with self.lock:
            generation = self.signature()
            if generation != self._cache_generation:
                self._cache.clear()
                self._cache_bytes = 0
                self._cache_generation = generation
            value = self._cached_value(key)
            if value is _MISS:
                value = loader()
                self._remember(key, value, generation)
            return clone(value)

    # ---- import -----------------------------------------------------------
    def shop_of(self, path):
        """The shop a file belongs to: the folder directly under a source folder, when that names a shop."""
        for source in self.sources:
            try:
                parts = Path(path).relative_to(source).parts
            except ValueError:
                continue
            if len(parts) > 1 and parts[0] in SHOP_FOLDERS:
                return SHOP_FOLDERS[parts[0]]
        return DEFAULT_SHOP

    def _folder_kind(self, path):
        """The content-type folder (视频 / 销售 / 图文 / 达人) a shop file sits in, if it sits in one."""
        for source in self.sources:
            try:
                parts = Path(path).relative_to(source).parts
            except ValueError:
                continue
            if len(parts) > 2 and parts[0] in SHOP_FOLDERS and parts[1] in KIND_FOLDERS.values():
                return parts[1]
        return None

    @staticmethod
    def _insert(connection, table, columns, rows):
        connection.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({','.join('?' * len(columns))})", rows)

    def _import_file(self, connection, path, shop=None):
        kind, start, end, records, shop_name = parse_export(_load_rows(path))
        shop = shop or self.shop_of(path)
        folder = self._folder_kind(path)
        if folder and folder != KIND_FOLDERS[kind]:
            raise ImportError_(f"文件放在「{folder}」文件夹里，内容却是{KIND_FOLDERS[kind]}表，请放回对应文件夹")
        file = str(path)
        if kind == "totals":
            dates = [r["date"] for r in records]
            connection.execute(f"DELETE FROM totals_daily WHERE shop = ? AND date IN ({','.join('?' * len(dates))})", [shop, *dates])
            columns = ["date", *TOTAL_METRICS]
            self._insert(connection, "totals_daily", [*columns, "source_file", "shop"],
                         [[r.get(c) for c in columns] + [file, shop] for r in records])
            table = "totals_daily"
        elif kind == "photo_items":
            table = "photo_items"
            span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
            fields = [*PHOTO_ITEM_TEXT, *PHOTO_ITEM_METRICS]
            connection.execute("DELETE FROM photo_items WHERE shop = ? AND ((range_start = ? AND range_end = ?) OR source_file = ?)",
                               (shop, start, end, file))
            self._insert(connection, "photo_items", ["shop", "item_id", "range_start", "range_end", "span_days", *fields, "source_file"],
                         [[shop, r["item_id"], start, end, span, *(r.get(c) for c in fields), file] for r in records])
        elif kind == "photo":
            table = "photo_periods"
            span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
            connection.execute("DELETE FROM photo_periods WHERE shop = ? AND ((range_start = ? AND range_end = ?) OR source_file = ?)",
                               (shop, start, end, file))
            self._insert(connection, "photo_periods",
                         ["shop", "range_start", "range_end", "span_days", "product_id", "product", *PHOTO_METRICS, "source_file"],
                         [[shop, start, end, span, r["product_id"], r.get("product"), *(r.get(c) for c in PHOTO_METRICS), file] for r in records])
        elif kind == "creators":
            table = "creator_periods"
            fields = [*CREATOR_TEXT, *METRICS, "diagnosis"]
            for record in records:
                # The export writes the Unix epoch when a video's posting time is unknown.
                if (record.get("posted_at") or "")[:4] in {"1969", "1970"}:
                    record["posted_at"] = None
            span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
            # A re-exported file may cover another range; its earlier rows go with it.
            connection.execute("DELETE FROM creator_periods WHERE (shop = ? AND range_start = ? AND range_end = ?) OR source_file = ?",
                               (shop, start, end, file))
            self._insert(connection, "creator_periods", ["video_id", "range_start", "range_end", "span_days", *fields, "source_file", "shop"],
                         [[r["video_id"], start, end, span, *(r.get(c) for c in fields), file, shop] for r in records])
        else:
            fields = [*VIDEO_TEXT, *METRICS, "diagnosis"]
            rows = [[r.get(c) for c in fields] for r in records]
            if start == end:
                table = "daily"
                connection.execute("DELETE FROM daily WHERE shop = ? AND date = ?", (shop, start))
                self._insert(connection, "daily", ["video_id", "date", *fields, "source_file", "shop"],
                             [[r["video_id"], start, *row, file, shop] for r, row in zip(records, rows)])
            else:
                table = "periods"
                span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
                connection.execute("DELETE FROM periods WHERE shop = ? AND range_start = ? AND range_end = ?", (shop, start, end))
                self._insert(connection, "periods", ["video_id", "range_start", "range_end", "span_days", *fields, "source_file", "shop"],
                             [[r["video_id"], start, end, span, *row, file, shop] for r, row in zip(records, rows)])
        connection.execute(
            "INSERT OR REPLACE INTO imports (source_file, kind, range_start, range_end, rows, modified, imported_at, shop, shop_name) VALUES (?,?,?,?,?,?,?,?,?)",
            (file, table, start, end, len(records), _modified(path), datetime.now().isoformat(timespec="seconds"), shop, shop_name))
        return {"file": path.name, "kind": table, "range": [start, end], "rows": len(records), "shop": shop}

    def import_paths(self, paths, skip_unchanged=True, shop=None):
        """Import files; report per-file results. Never raises for a bad file.

        shop: force every file into one shop (a file picked in the page has no folder to tell); by default each file's folder decides."""
        if shop is not None and shop not in SHOPS:
            raise ImportError_(f"未知店铺：{shop}")
        imported, skipped, failed = [], 0, []
        with self.lock:
            connection = self.connect()
            try:
                for path in paths:
                    path = Path(path)
                    if skip_unchanged:
                        row = connection.execute("SELECT modified FROM imports WHERE source_file = ?", (str(path),)).fetchone()
                        if row and row[0] == _modified(path):
                            skipped += 1
                            continue
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        imported.append(self._import_file(connection, path, shop))
                        connection.execute("COMMIT")
                    except Exception as error:
                        connection.execute("ROLLBACK")
                        failed.append({"file": path.name, "error": str(error)})
            finally:
                connection.close()
        return {"imported": imported, "skipped": skipped, "failed": failed}

    def source_files(self):
        files = []
        for source in self.sources:
            if source.is_file():
                files.append(source)
            elif source.is_dir():
                files.extend(sorted(source.rglob("*.xlsx")))
        # Excel keeps "~$name.xlsx" lock files next to workbooks that are open.
        return [f for f in files if not f.name.startswith("~$")]

    def sync(self):
        missing = [str(s) for s in self.sources if not s.exists()]
        result = self.import_paths(self.source_files())
        return {**result, "sources": [str(s) for s in self.sources], "missing_sources": missing}

    def keep_upload(self, staged, name):
        """Copy an uploaded export next to the store so later syncs can re-read it."""
        self.uploads.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.\-一-鿿]+", "_", Path(name).name) or "export.xlsx"
        target = self.uploads / f"{datetime.now():%Y%m%d-%H%M%S}-{safe}"
        shutil.copyfile(staged, target)
        return target

    # ---- queries ----------------------------------------------------------
    @staticmethod
    def _check_shop(shop):
        if shop is not None and shop not in SHOPS:
            raise ValueError(f"未知店铺：{shop}")
        return shop

    def _facts(self, connection, generation=None, shop=None):
        """Per-video rows without double counting daily and weekly exports; `shop` keeps one shop's rows (default: all)."""
        self._check_shop(shop)
        cached = self._cached_value("facts") if generation is not None and generation == self._cache_generation else _MISS
        if cached is not _MISS:
            image, excluded = cached
            connection.execute("ATTACH DATABASE ':memory:' AS normalized")
            connection.deserialize(image, name="normalized")
            # The shop key was checked above, so it is safe to write into the view.
            connection.execute("CREATE TEMP VIEW facts AS SELECT * FROM normalized.facts" + (f" WHERE shop = '{shop}'" if shop else ""))
            return excluded
        connection.execute(f"CREATE TEMP TABLE facts ({', '.join(column + (' REAL' if column in SUMMED else ' TEXT') for column in FACT_COLUMNS)})")
        excluded = self._materialize_facts(connection)
        connection.execute("CREATE INDEX temp.facts_video ON facts(video_id, end, start, source_file)")
        connection.execute("CREATE INDEX temp.facts_dates ON facts(start, end)")
        # A SQLite image is far smaller and faster to reuse than thousands of
        # Python row objects. It contains only derived temporary data; each reader
        # restores its own in-memory copy alongside its pinned source snapshot.
        if hasattr(connection, "serialize") and hasattr(connection, "deserialize"):
            try:
                image = connection.serialize(name="temp")
            except sqlite3.NotSupportedError:
                pass
            else:
                self._remember("facts", (image, excluded), generation)
        if shop:
            connection.execute("DELETE FROM facts WHERE shop != ?", (shop,))
        return excluded

    def _materialize_facts(self, connection):
        # A shifted weekly export can overlap another week's totals. Choose
        # whole newer intervals per video; an older partial overlap cannot be
        # split into daily metrics. Every read path uses this same selection.
        selected, covered, excluded, intervals = [], {}, {}, {}
        for row in connection.execute("SELECT video_id, range_start, range_end, source_file FROM periods WHERE span_days=7 ORDER BY video_id, range_end DESC, range_start DESC, source_file DESC"):
            video_id, start, end, source_file = row
            interval = (start, end)
            days = intervals.get(interval)
            if days is None:
                first, last = date.fromisoformat(start), date.fromisoformat(end)
                days = {(first + timedelta(days=i)).isoformat() for i in range((last-first).days+1)}
                intervals[interval] = days
            used = covered.setdefault(video_id, set())
            if days & used:
                excluded.setdefault(video_id, []).append({"start": start, "end": end, "source_file": source_file})
            else:
                selected.append((video_id, start, end))
                used.update(days)
        connection.execute("CREATE TEMP TABLE week_ranges (video_id TEXT, s TEXT, e TEXT, PRIMARY KEY (video_id, s, e))")
        connection.executemany("INSERT INTO week_ranges VALUES (?,?,?)", selected)
        connection.execute("""
            INSERT INTO facts
              SELECT video_id, date start, date end, account, title, posted_at, products,
                     views, likes, comments, shares, impressions, clicks, orders, items, gmv, source_file, shop
              FROM daily d WHERE NOT EXISTS (SELECT 1 FROM week_ranges w
                  WHERE w.video_id = d.video_id AND d.date BETWEEN w.s AND w.e)
              UNION ALL
              SELECT video_id, range_start, range_end, account, title, posted_at, products,
                     views, likes, comments, shares, impressions, clicks, orders, items, gmv, source_file, shop
              FROM periods p WHERE p.span_days = 7 AND EXISTS (SELECT 1 FROM week_ranges w
                  WHERE w.video_id = p.video_id AND w.s = p.range_start AND w.e = p.range_end)
        """)
        return excluded

    def coverage(self, connection):
        days = set()
        for start, end in connection.execute("SELECT DISTINCT start, end FROM facts"):
            day, last = date.fromisoformat(start), date.fromisoformat(end)
            while day <= last:
                days.add(day.isoformat())
                day += timedelta(days=1)
        return days

    def _with_public_views(self, items):
        """Overlay the latest public counts while preserving the export values.

        Unknown public interactions stay unknown; they are not mixed with older
        Shop figures. Window views, follow-up status and rates keep the export.
        """
        if self.public is None or not items:
            return
        try:
            latest = self.public.latest(video_id for video_id, _ in items)
        except (sqlite3.Error, OSError):
            return
        for video_id, record in items:
            snapshot = latest.get(video_id)
            if snapshot is None:
                continue
            record["public_stats"] = {field: snapshot.get(field) for field in (*PUBLIC_COUNTS, "observed_at", "last_seen_at")}
            for field in PUBLIC_COUNTS[1:]:
                record[field + "_shop"] = record.get(field)
                record[field] = snapshot.get(field)
                record[field + "_source"] = "public"
            views = snapshot.get("views")
            if views is not None:
                record.update(views_shop=record.get("views"), views=views, views_source="public")

    def videos(self, case_links=None, shop=None):
        """Every video's figures and data bands, using its own shop's imported cutoff."""
        result = self._read_cached("videos", self._read_videos)
        if shop is not None:
            result = [video for video in result if video["shop"] == self._check_shop(shop)]
        case_links = case_links or {}
        for video in result:
            video["case_id"] = case_links.get(video["video_id"])
        self._with_public_views([(video["video_id"], video) for video in result])
        return result

    def _read_videos(self, source=None):
        # Case links belong to the workbench, rather than the sales database cache.
        case_links = {}
        with self.lock:
            connection, generation = source if source is not None else self._query_connection()
            try:
                self._facts(connection, generation)
                last_days = dict(connection.execute("SELECT shop, MAX(end) FROM facts GROUP BY shop").fetchall())
                rows = connection.execute(f"""
                    SELECT video_id, MIN(shop) shop, MIN(account) account, MIN(title) title, MIN(posted_at) posted_at, MIN(products) products,
                           {", ".join(f"SUM({m}) {m}" for m in SUMMED)}, MIN(start) first_data, MAX(end) last_data,
                           COUNT(*) data_rows, COUNT(orders) order_rows, COUNT(clicks) click_rows
                    FROM facts GROUP BY video_id""").fetchall()
                window = {row[0]: row[1] for row in connection.execute("""
                    SELECT video_id, SUM(views) FROM facts
                    WHERE end <= date(substr(posted_at,1,10), '-' || ((strftime('%w', substr(posted_at,1,10)) + 6) % 7) || ' days', '+13 days')
                    GROUP BY video_id""")}
                # Only this video's usable view counts can complete its observation
                # window. Other videos/accounts establish the imported cutoff, never
                # its missing days. Use the same whole periods as the view total:
                # a weekly export crossing the window end cannot be split into days.
                window_coverage, intervals = {}, {}
                for video_id, start, end, views in connection.execute("""
                        SELECT video_id, start, end, views FROM facts
                        WHERE end <= date(substr(posted_at,1,10), '-' || ((strftime('%w', substr(posted_at,1,10)) + 6) % 7) || ' days', '+13 days')"""):
                    if views is None or not math.isfinite(views) or views < 0:
                        continue
                    interval = (start, end)
                    if interval not in intervals:
                        first, last = date.fromisoformat(start), date.fromisoformat(end)
                        intervals[interval] = {(first + timedelta(days=i)).isoformat()
                                               for i in range((last - first).days + 1)}
                    window_coverage.setdefault(video_id, set()).update(intervals[interval])
                # Availability for each video's first 14 calendar days. This is
                # coverage only: a crossing weekly report is never apportioned
                # into invented daily figures or used to change window_views.
                observation_coverage = {}
                for video_id, start, end, views, orders in connection.execute(
                        "SELECT video_id, start, end, views, orders FROM facts"):
                    if any(value is None or not math.isfinite(value) or value < 0 for value in (views, orders)):
                        continue
                    first, last = date.fromisoformat(start), date.fromisoformat(end)
                    observation_coverage.setdefault(video_id, set()).update(
                        (first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1))
                weekly = {}
                for video_id, week, views, gmv in connection.execute("""
                        SELECT video_id, date(start, '-' || ((strftime('%w', start) + 6) % 7) || ' days') week, SUM(views), SUM(gmv)
                        FROM facts GROUP BY video_id, week"""):
                    weekly.setdefault(video_id, {})[week] = (views or 0, gmv or 0)
            finally:
                if source is None:
                    connection.close()
        result = []
        for row in rows:
            item = dict(row)
            last_day = last_days.get(item["shop"])
            this_week = _monday(date.fromisoformat(last_day)).isoformat() if last_day else None
            data_rows, order_rows, click_rows = (item.pop(field) for field in ("data_rows", "order_rows", "click_rows"))
            complete_rate = data_rows == order_rows == click_rows
            posted = (item["posted_at"] or "")[:10]
            try:
                posted_day = date.fromisoformat(posted)
            except ValueError:
                posted_day = None
            status, window_views = "incomplete", None
            if posted_day:
                window_end = _monday(posted_day) + timedelta(days=13)
                window_views = window.get(item["video_id"])
                span = [(posted_day + timedelta(days=i)).isoformat() for i in range((window_end - posted_day).days + 1)]
                if last_day and window_end.isoformat() > last_day:
                    status = "pending"
                elif (window_views is None or not math.isfinite(window_views) or
                      not set(span).issubset(window_coverage.get(item["video_id"], set()))):
                    status = "incomplete"
                else:
                    status = next(label for floor, label in THRESHOLDS if window_views >= floor)
                item["window_end"] = window_end.isoformat()
            first14 = [(posted_day + timedelta(days=i)).isoformat() for i in range(14)] if posted_day else []
            missing14 = [day for day in first14 if day not in observation_coverage.get(item["video_id"], set())]
            item["observation_coverage_complete"] = bool(first14) and not missing14
            item["observation_missing_days"] = [missing14[0], missing14[-1], len(missing14)] if missing14 else None
            flags, reference, missing_window = [], None, None
            orders = item["orders"]
            if status in {"watch", "drop"} and ((item["views"] or 0) >= LATE_VIEWS or (orders or 0) >= LATE_ORDERS):
                flags.append("late")
            if (status in {"focus", "follow"} and orders == 0 and order_rows == data_rows and last_day and posted_day
                    and (date.fromisoformat(last_day) - posted_day).days >= NO_ORDER_DAYS):
                flags.append("no_orders")
            if status == "incomplete":
                if orders is not None:
                    key, label = next((key, label) for floor, key, label in REFERENCE if orders >= floor)
                    reference = {"key": key, "label": f"参考：{label}", "orders": orders}
                if posted_day:
                    have = window_coverage.get(item["video_id"], set())
                    days = [d for d in span if d not in have]
                    if days:
                        missing_window = [days[0], days[-1], len(days)]
            item.update({
                "flags": [{"key": key, "label": FLAG_LABELS[key]} for key in flags],
                "reference": reference, "missing_window": missing_window,
                "posted_on": posted or None, "headline": headline(item["title"]), "product": product_short(item["products"]),
                "window_views": window_views, "status": status, "status_label": STATUS_LABELS[status],
                "ctor_pct": round(item["orders"] * 100 / item["clicks"], 2) if item["clicks"] and complete_rate else None,
                "ctr_pct": round(item["clicks"] * 100 / item["views"], 2) if item["views"] and item["clicks"] is not None else None,
                "gpm": round(item["gmv"] * 1000 / item["views"], 2) if item["views"] and item["gmv"] is not None else None,
                "this_week_gmv": (weekly.get(item["video_id"], {}).get(this_week) or (0, 0))[1] if this_week else 0,
                "case_id": case_links.get(item["video_id"]),
            })
            result.append(item)
        return result

    def known_video_ids(self, video_ids):
        """The subset of these video IDs present in any imported export, ours or the all-creator one."""
        ids = sorted({str(v) for v in video_ids if v})
        if not ids or not self.database.is_file():
            return set()
        found = set()
        with self.lock:
            connection, _ = self._query_connection()
            try:
                for start in range(0, len(ids), 300):
                    chunk = ids[start:start + 300]
                    marks = ",".join("?" * len(chunk))
                    found.update(row[0] for row in connection.execute(
                        f"SELECT video_id FROM daily WHERE video_id IN ({marks}) UNION SELECT video_id FROM periods WHERE video_id IN ({marks}) "
                        f"UNION SELECT video_id FROM creator_periods WHERE video_id IN ({marks})", chunk * 3))
            finally:
                connection.close()
        return found

    def video_brief(self, video_ids):
        """The few figures shown beside a video elsewhere (title, account, posting day, views, clicks, orders, GMV), for just
        these videos. A light cousin of videos(), which works out every video's follow-up status and takes seconds.
        Empty (and nothing is created) before any export was imported."""
        ids = tuple(sorted({str(v) for v in video_ids if v}))
        if not ids or not self.database.is_file():
            return {}
        result = self._read_cached(("brief", ids), lambda: self._read_video_brief(ids))
        self._with_public_views(list(result.items()))
        return result

    def _read_video_brief(self, ids):
        result = {}
        with self.lock:
            connection, generation = self._query_connection()
            try:
                excluded = self._facts(connection, generation)
                for start in range(0, len(ids), 500):
                    chunk = ids[start:start + 500]
                    # Keep each requested video's own coverage, not the union of
                    # dates on which some other video had data.
                    per_video = {}
                    for row in connection.execute(f"SELECT * FROM facts WHERE video_id IN ({','.join('?' * len(chunk))}) ORDER BY end, start, source_file", chunk):
                        per_video.setdefault(row["video_id"], []).append(dict(row))
                    for video_id, rows in per_video.items():
                        item = {metric: sum(values) if (values := [row[metric] for row in rows if row[metric] is not None]) else None for metric in SUMMED}
                        item.update(first_data=min(row["start"] for row in rows), last_data=max(row["end"] for row in rows),
                                    excluded_overlapping_periods=excluded.get(video_id, []))
                        result[video_id] = item
                        latest = rows[-1]
                        for field in ("account", "title", "products"):
                            item[field] = latest[field]
                        posted_at = min((row["posted_at"] for row in rows if row["posted_at"] is not None), default="")
                        item.update(headline=headline(item["title"]), product=product_short(item["products"]),
                                    posted_on=posted_at[:10] or None,
                                    source_files=sorted({row["source_file"] for row in rows if row["source_file"]}))
                        days = set()
                        for row in rows:
                            day, last = date.fromisoformat(row["start"]), date.fromisoformat(row["end"])
                            while day <= last:
                                days.add(day.isoformat())
                                day += timedelta(days=1)
                        first, last = date.fromisoformat(item["first_data"]), date.fromisoformat(item["last_data"])
                        span = [(first + timedelta(days=i)).isoformat() for i in range((last-first).days+1)]
                        posted = item.get("posted_on")
                        try:
                            posted_day = date.fromisoformat(posted) if posted else None
                        except ValueError:
                            posted_day = None
                        item["window_end"] = (_monday(posted_day) + timedelta(days=13)).isoformat() if posted_day else None
                        since_posting = [(posted_day + timedelta(days=i)).isoformat() for i in range((last-posted_day).days+1)] if posted_day and posted_day <= last else None
                        missing = [day for day in since_posting if day not in days] if since_posting is not None else None
                        item["coverage"] = {"covered_days": len(days), "span_days": len(span),
                            "gaps": [day for day in span if day not in days], "missing_since_posting": missing,
                            "complete_since_posting": not missing if missing is not None else None}
                        item["metric_coverage"] = {field: {"provided_rows": sum(row[field] is not None for row in rows),
                                                          "total_rows": len(rows)} for field in SUMMED}
                        item["ctor_pct"] = round(item["orders"] * 100 / item["clicks"], 2) if item["clicks"] and all(
                            item["metric_coverage"][field]["provided_rows"] == len(rows) for field in ("orders", "clicks")) else None
            finally:
                connection.close()
        return result

    def signature(self):
        """A cheap change marker including active SQLite WAL writes; never opens/creates a database."""
        values = []
        for path in (self.database, Path(str(self.database) + "-wal")):
            try:
                stat = path.stat()
                values.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, getattr(stat, "st_ino", None)))
            except OSError:
                values.append((str(path), None))
        return tuple(values)

    def video(self, video_id, case_links=None):
        result = self._read_cached(("video", video_id), lambda: self._read_video(video_id))
        if result is not None:
            result["case_id"] = (case_links or {}).get(video_id)
            self._with_public_views([(video_id, result)])
        return result

    def rate_series(self, video_id):
        """Each exported week's own rates (完播率, CTR, CTOR) for one video. Rates are never summed or averaged;
        a week exported twice keeps its newest file."""
        with self.lock:
            connection, _ = self._query_connection()
            try:
                rows = connection.execute(
                    "SELECT range_start, range_end, views, finish_rate_pct, ctr_pct, ctor_pct, source_file FROM periods "
                    "WHERE video_id = ? AND span_days = 7 ORDER BY range_start, source_file", (video_id,)).fetchall()
            finally:
                connection.close()
        weeks = {}
        for start, end, views, finish, ctr, ctor, _source in rows:
            weeks[(start, end)] = {"start": start, "end": end, "views": views, "finish_rate_pct": finish, "ctr_pct": ctr, "ctor_pct": ctor}
        return list(weeks.values())

    def rate_medians(self, video_ids, since=None, until=None):
        """Reference for the rates a draft reads: the median across videos of each video's busiest exported week
        (完播率, CTR, CTOR), counting only weeks with at least RATE_MIN_VIEWS views and, when given, weeks lying
        within since..until. Each video counts once; rates are never pooled."""
        ids = sorted({str(value) for value in video_ids if value})
        if not ids:
            return {}
        latest = {}
        with self.lock:
            connection, _ = self._query_connection()
            try:
                for start in range(0, len(ids), 500):
                    chunk = ids[start:start + 500]
                    marks = ",".join("?" * len(chunk))
                    for video_id, views, finish, ctr, ctor in connection.execute(
                            f"SELECT video_id, views, finish_rate_pct, ctr_pct, ctor_pct FROM periods "
                            f"WHERE span_days = 7 AND views >= ? AND range_start >= ? AND range_end <= ? AND video_id IN ({marks}) "
                            f"ORDER BY views", [RATE_MIN_VIEWS, since or "", until or "9999-12-31", *chunk]):
                        latest[video_id] = (finish, ctr, ctor)
            finally:
                connection.close()

        def median(index, selling=False):
            values = [row[index] for row in latest.values() if isinstance(row[index], (int, float)) and math.isfinite(row[index])
                      and (not selling or row[index] > 0)]
            return {"median": round(statistics.median(values), 2), "videos": len(values)} if values else None
        # Most videos sell nothing, so CTOR is also given among the videos that sold in that week.
        return {"finish_rate_pct": median(0), "ctr_pct": median(1), "ctor_pct": median(2), "ctor_pct_selling": median(2, True)}

    def _read_video(self, video_id):
        with self.lock:
            connection, generation = self._query_connection()
            try:
                videos = self._cached_value("videos") if generation is not None and generation == self._cache_generation else _MISS
                if videos is _MISS:
                    videos = self._read_videos((connection, generation))
                    self._remember("videos", videos, generation)
                else:
                    self._facts(connection, generation)
                row = next((video for video in videos if video["video_id"] == video_id), None)
                if row is None:
                    return None
                videos = [video for video in videos if video["shop"] == row["shop"]]
                # Ranks, totals and series all describe the same pinned snapshot.
                def top_pct(values, value):
                    return max(1, math.ceil(100 * sum(other >= value for other in values) / len(values)))

                judged = [video["window_views"] for video in videos if video["status"] in {"focus", "follow", "watch", "drop"}]
                ranks = {"judged_videos": len(judged), "all_videos": len(videos),
                         "gmv_top_pct": top_pct([video["gmv"] or 0 for video in videos], row["gmv"] or 0)}
                if judged and row["status"] in {"focus", "follow", "watch", "drop"}:
                    ranks["window_top_pct"] = top_pct(judged, row["window_views"])
                series = [dict(r) for r in connection.execute(
                    "SELECT start, end, views, clicks, orders, items, gmv FROM facts WHERE video_id = ? ORDER BY start", (video_id,))]
            finally:
                connection.close()
        return {**row, **ranks, "series": series}

    def analytics(self, shop=None):
        """Compact figures behind the dashboard, so the page can re-slice them by account, product and period without asking again.

        weekly:  one row per video and Monday-start week (the facts view, so daily and weekly exports never double count).
        monthly: the same per video and month, in the report each month is best known from: a monthly export when there is
                 one, else the daily rows, else the weekly ones (a week belongs to the month holding its Thursday).
        shop_daily: all-video daily totals, which are a different caliber from the per-video rows and never mixed with them.
        Row layout is [video index, week|month, views, impressions, clicks, orders, items, gmv]."""
        def clone(value):
            # All row cells are immutable scalars; copy the mutable containers.
            return {"videos": [row[:] for row in value["videos"]], "weekly": [row[:] for row in value["weekly"]],
                    "monthly": [row[:] for row in value["monthly"]], "months": dict(value["months"]),
                    "shop_daily": [dict(row) for row in value["shop_daily"]]}
        return self._read_cached(("analytics", self._check_shop(shop)), lambda: self._read_analytics(shop), clone)

    def _read_analytics(self, shop=None):
        scope = {"shop": shop}
        measures = "SUM(views), SUM(impressions), SUM(clicks), SUM(orders), SUM(items), SUM(gmv)"
        with self.lock:
            connection, generation = self._query_connection()
            try:
                self._facts(connection, generation, shop)
                weekly = connection.execute(f"""
                    SELECT video_id, date(start, '-' || ((strftime('%w', start) + 6) % 7) || ' days') week, {measures}
                    FROM facts GROUP BY video_id, week ORDER BY week, video_id""").fetchall()
                month_of = "CASE WHEN start = end THEN substr(start, 1, 7) ELSE substr(date(start, '+3 days'), 1, 7) END"
                derived = connection.execute(f"""
                    SELECT video_id, {month_of} month, start = end is_daily, {measures}
                    FROM facts GROUP BY video_id, month, is_daily""").fetchall()
                reported = connection.execute(f"""
                    SELECT video_id, substr(range_start, 1, 7) month, {measures}
                    FROM periods WHERE span_days >= 28 AND (:shop IS NULL OR shop = :shop) GROUP BY video_id, month""", scope).fetchall()
                attributes = connection.execute("""
                    SELECT video_id, MIN(account), MIN(products), MIN(posted_at) FROM (
                        SELECT video_id, account, products, posted_at FROM daily WHERE (:shop IS NULL OR shop = :shop)
                        UNION ALL SELECT video_id, account, products, posted_at FROM periods WHERE (:shop IS NULL OR shop = :shop))
                    GROUP BY video_id""", scope).fetchall()
                totals = connection.execute("""
                    SELECT date, SUM(gmv), SUM(orders), SUM(views), SUM(clicks) FROM totals_daily
                    WHERE (:shop IS NULL OR shop = :shop) GROUP BY date ORDER BY date""", scope).fetchall()
            finally:
                connection.close()
        index = {row[0]: i for i, row in enumerate(attributes)}
        reported_months = {row[1] for row in reported}
        basis = {}
        for row in derived:
            if row[1] not in reported_months:
                basis[row[1]] = "周报表合计" if (basis.get(row[1]) == "周报表合计" or not row[2]) else "日报表明细合计"
        for month in reported_months:
            basis[month] = "月报表"

        def rounded(row):
            return [*(int(v or 0) for v in row[:5]), round(row[5] or 0, 2)]
        monthly = [[index[r[0]], r[1], *rounded(r[2:])] for r in reported]
        monthly += [[index[r[0]], r[1], *rounded(r[3:])] for r in derived if r[1] not in reported_months]
        return {
            "videos": [[v, a, product_short(p), (t or "")[:10] or None] for v, a, p, t in attributes],
            "weekly": [[index[r[0]], r[1], *rounded(r[2:])] for r in weekly],
            "monthly": monthly, "months": dict(sorted(basis.items())),
            "shop_daily": [{"date": r[0], "gmv": round(r[1] or 0, 2), "orders": int(r[2] or 0), "views": int(r[3] or 0), "clicks": int(r[4] or 0)} for r in totals],
        }

    def creators(self, case_links=None, shop=None):
        """Overseas creator videos from the all-creator exports, in their own figures.

        periods: the export ranges counted, oldest first. Overlapping exports keep the one ending later (then the
                 longer one); the others are listed in excluded_periods and never added.
        videos:  [video_id, account, country, headline, posted_on, products, kind]; kind is "creator", "own" (an
                 account in the self-made exports or a brand handle) or "unnamed" (no creator name).
        rows:    [video index, period index, views, impressions, clicks, orders, items, gmv].
        cases:   video ID -> case ID for creator videos already collected.
        public_stats: latest public counts by video ID, separate from export-period rows."""
        result = self._read_cached(("creators", self._check_shop(shop)), lambda: self._read_creators(shop), dict)
        links = case_links or {}
        result["cases"] = {video[0]: links[video[0]] for video in result["videos"] if video[0] in links}
        result["public_stats"] = {}
        if self.public is not None and result["videos"]:
            try:
                latest = self.public.latest(video[0] for video in result["videos"])
                result["public_stats"] = {video_id: {field: snapshot.get(field)
                    for field in (*PUBLIC_COUNTS, "observed_at", "last_seen_at")} for video_id, snapshot in latest.items()}
            except (sqlite3.Error, OSError):
                pass
        return result

    def _read_creators(self, shop=None):
        scope = {"shop": shop}
        with self.lock:
            connection, _ = self._query_connection()
            try:
                ranges = connection.execute("""
                    SELECT range_start, range_end, GROUP_CONCAT(DISTINCT source_file) FROM creator_periods
                    WHERE (:shop IS NULL OR shop = :shop) GROUP BY range_start, range_end""", scope).fetchall()
                own = {row[0] for row in connection.execute(
                    "SELECT account FROM daily WHERE (:shop IS NULL OR shop = :shop) "
                    "UNION SELECT account FROM periods WHERE (:shop IS NULL OR shop = :shop)", scope) if row[0]}
                found = connection.execute("""
                    SELECT video_id, range_start, range_end, account, country, title, posted_at, products,
                           views, impressions, clicks, orders, items, gmv
                    FROM creator_periods WHERE (:shop IS NULL OR shop = :shop) ORDER BY range_start, video_id""", scope).fetchall()
            finally:
                connection.close()
        names = {(start, end): sorted({Path(f).name for f in (joined or "").split(",") if f}) for start, end, joined in ranges}
        chosen, excluded = [], []
        for start, end in sorted(names, key=lambda r: (r[1], (date.fromisoformat(r[1]) - date.fromisoformat(r[0])).days), reverse=True):
            if any(start <= e and s <= end for s, e in chosen):
                excluded.append({"start": start, "end": end, "files": names[(start, end)]})
            else:
                chosen.append((start, end))
        chosen.sort()
        position = {interval: i for i, interval in enumerate(chosen)}
        periods = [{"start": s, "end": e, "days": (date.fromisoformat(e) - date.fromisoformat(s)).days + 1,
                    "label": label, "files": names[(s, e)]} for (s, e), label in zip(chosen, _period_labels(chosen))]
        index, videos, rows = {}, [], []
        for row in found:
            period = position.get((row["range_start"], row["range_end"]))
            if period is None:
                continue
            account = row["account"] or ""
            kind = "unnamed" if not account else "own" if account in own or BRAND in account.lower() else "creator"
            # Rows come oldest period first: a later export's name, title and products replace earlier ones.
            attributes = [row["video_id"], account, row["country"] or "", headline(row["title"])[:160],
                          (row["posted_at"] or "")[:10] or None, product_models(row["products"]), kind]
            if row["video_id"] in index:
                videos[index[row["video_id"]]] = attributes
            else:
                index[row["video_id"]] = len(videos)
                videos.append(attributes)
            rows.append([index[row["video_id"]], period, *(int(row[m] or 0) for m in ("views", "impressions", "clicks", "orders", "items")),
                         round(row["gmv"] or 0, 2)])
        return {"periods": periods, "excluded_periods": excluded, "videos": videos, "rows": rows,
                "own_accounts": sorted({v[1] for v in videos if v[6] == "own"})}

    def photos(self, shop=None):
        """The photo (图文) exports of one shop, in their own figures.

        periods:  export ranges, oldest first. Seven-day ranges win over longer ones they overlap; the others are listed
                  in excluded_periods and never added.
        products: [product ID, short name, full name]; ID "-1" is sales the export could not tie to a product.
        rows:     [product index, period index, gmv, orders, impressions, clicks]. Clicks are worked back from the
                  rounded click-through rate, so they are close rather than exact.
        GPM here is per thousand impressions (the video one is per thousand views): the two are never compared."""
        return self._read_cached(("photos", self._check_shop(shop)), lambda: self._read_photos(shop), dict)

    def _read_photos(self, shop=None):
        scope = {"shop": shop}
        with self.lock:
            connection, _ = self._query_connection()
            try:
                ranges = connection.execute("""
                    SELECT range_start, range_end, span_days, GROUP_CONCAT(DISTINCT source_file) FROM photo_periods
                    WHERE (:shop IS NULL OR shop = :shop) GROUP BY range_start, range_end""", scope).fetchall()
                names = {row[0]: row[1] for row in connection.execute(
                    "SELECT source_file, shop_name FROM imports WHERE kind = 'photo_periods'")}
                found = connection.execute("""
                    SELECT range_start, range_end, product_id, product, gmv, orders, impressions, ctr_pct
                    FROM photo_periods WHERE (:shop IS NULL OR shop = :shop) ORDER BY range_start, product_id""", scope).fetchall()
            finally:
                connection.close()
        files = {(r[0], r[1]): sorted(f for f in (r[3] or "").split(",") if f) for r in ranges}
        span = {(r[0], r[1]): r[2] for r in ranges}
        chosen, excluded = [], []
        # Seven-day ranges first (newest first), then the longer ones: a long range overlapping a chosen week is left out.
        for start, end in sorted(files, key=lambda r: (span[r] != 7, tuple(-int(x) for x in r[1].split("-")))):
            if any(start <= e and s <= end for s, e in chosen):
                excluded.append({"start": start, "end": end, "files": [Path(f).name for f in files[(start, end)]]})
            else:
                chosen.append((start, end))
        chosen.sort()
        position = {interval: i for i, interval in enumerate(chosen)}
        periods = [{"start": s, "end": e, "days": span[(s, e)], "weekly": span[(s, e)] == 7, "label": f"{s[5:]} 至 {e[5:]}",
                    "files": [Path(f).name for f in files[(s, e)]],
                    "shop_names": sorted({names[f] for f in files[(s, e)] if names.get(f)})} for s, e in chosen]
        index, products, rows = {}, [], []
        for row in found:
            period = position.get((row["range_start"], row["range_end"]))
            if period is None:
                continue
            pid = row["product_id"]
            full = row["product"] or ""
            if pid not in index:
                index[pid] = len(products)
                products.append([pid, product_short(full) if full else "未归属商品", full])
            elif full and not products[index[pid]][2]:
                products[index[pid]][1:] = [product_short(full), full]
            impressions = int(row["impressions"] or 0)
            rows.append([index[pid], period, round(row["gmv"] or 0, 2), int(row["orders"] or 0), impressions,
                         round(impressions * (row["ctr_pct"] or 0) / 100)])
        return {"shop": shop, "periods": periods, "excluded_periods": excluded, "products": products, "rows": rows}

    def photo_items(self, shop=None):
        """Photo posts of one shop from the per-post exports, in their own figures (never added to the per-product table).

        periods: export ranges, oldest first; overlapping ones keep the export ending later (then the longer one) and the
                 rest go to excluded_periods.
        items:   [item ID, account, headline, posted_on, products]; the latest export's text wins.
        rows:    [item index, period index, views, impressions, clicks, sold, gmv, likes, shares, comments]. Clicks are worked
                 back from the rounded click-through rate. "sold" is the first of the file's two same-named columns."""
        return self._read_cached(("photo_items", self._check_shop(shop)), lambda: self._read_photo_items(shop), dict)

    def _read_photo_items(self, shop=None):
        scope = {"shop": shop}
        with self.lock:
            connection, _ = self._query_connection()
            try:
                ranges = connection.execute("""
                    SELECT range_start, range_end, GROUP_CONCAT(DISTINCT source_file) FROM photo_items
                    WHERE (:shop IS NULL OR shop = :shop) GROUP BY range_start, range_end""", scope).fetchall()
                names = {row[0]: row[1] for row in connection.execute("SELECT source_file, shop_name FROM imports WHERE kind = 'photo_items'")}
                found = connection.execute("""
                    SELECT item_id, range_start, range_end, account, title, posted_at, products, views, impressions, ctr_pct,
                           sold, gmv, likes, shares, comments
                    FROM photo_items WHERE (:shop IS NULL OR shop = :shop) ORDER BY range_start, item_id""", scope).fetchall()
            finally:
                connection.close()
        files = {(r[0], r[1]): sorted(f for f in (r[2] or "").split(",") if f) for r in ranges}
        chosen, excluded = [], []
        for start, end in sorted(files, key=lambda r: (r[1], (date.fromisoformat(r[1]) - date.fromisoformat(r[0])).days), reverse=True):
            if any(start <= e and s <= end for s, e in chosen):
                excluded.append({"start": start, "end": end, "files": [Path(f).name for f in files[(start, end)]]})
            else:
                chosen.append((start, end))
        chosen.sort()
        position = {interval: i for i, interval in enumerate(chosen)}
        periods = [{"start": s, "end": e, "days": (date.fromisoformat(e) - date.fromisoformat(s)).days + 1,
                    "label": f"{s[5:]} 至 {e[5:]}", "files": [Path(f).name for f in files[(s, e)]],
                    "shop_names": sorted({names[f] for f in files[(s, e)] if names.get(f)})} for s, e in chosen]
        index, items, rows = {}, [], []
        for row in found:
            period = position.get((row["range_start"], row["range_end"]))
            if period is None:
                continue
            attributes = [row["item_id"], row["account"] or "", headline(row["title"])[:160], (row["posted_at"] or "")[:10] or None,
                          product_models(row["products"])]
            if row["item_id"] in index:
                items[index[row["item_id"]]] = attributes
            else:
                index[row["item_id"]] = len(items)
                items.append(attributes)
            impressions = int(row["impressions"] or 0)
            rows.append([index[row["item_id"]], period, int(row["views"] or 0), impressions,
                         round(impressions * (row["ctr_pct"] or 0) / 100), int(row["sold"] or 0), round(row["gmv"] or 0, 2),
                         int(row["likes"] or 0), int(row["shares"] or 0), int(row["comments"] or 0)])
        return {"shop": shop, "periods": periods, "excluded_periods": excluded, "items": items, "rows": rows}

    def summary(self, shop=None):
        result = self._read_cached(("summary", self._check_shop(shop)), lambda: self._read_summary(shop))
        result["sources"] = [str(source) for source in self.sources]
        return result

    def _read_summary(self, shop=None):
        scope = {"shop": shop}
        with self.lock:
            connection, generation = self._query_connection()
            try:
                self._facts(connection, generation, shop)
                covered = sorted(self.coverage(connection))
                weeks = [dict(r) for r in connection.execute("""
                    SELECT date(start, '-' || ((strftime('%w', start) + 6) % 7) || ' days') week,
                           COUNT(DISTINCT video_id) videos, SUM(views) views, SUM(items) items, SUM(gmv) gmv
                    FROM facts GROUP BY week ORDER BY week""")]
                posted = dict(connection.execute("""
                    SELECT date(substr(p,1,10), '-' || ((strftime('%w', substr(p,1,10)) + 6) % 7) || ' days'), COUNT(*)
                    FROM (SELECT video_id, MIN(posted_at) p FROM facts GROUP BY video_id) GROUP BY 1""").fetchall())
                imports = connection.execute("SELECT COUNT(*), MAX(imported_at) FROM imports WHERE (:shop IS NULL OR shop = :shop)", scope).fetchone()
                totals = connection.execute("SELECT MIN(date), MAX(date) FROM totals_daily WHERE (:shop IS NULL OR shop = :shop)", scope).fetchone()
                shops = []
                for key, name in SHOPS.items():
                    files, first, last, photo = connection.execute(
                        "SELECT COUNT(*), MIN(range_start), MAX(range_end), SUM(kind IN ('photo_periods', 'photo_items')) FROM imports WHERE shop = ?", (key,)).fetchone()
                    names = [r[0] for r in connection.execute(
                        "SELECT DISTINCT shop_name FROM imports WHERE shop = ? AND shop_name IS NOT NULL ORDER BY 1", (key,))]
                    shops.append({"id": key, "name": name, "files": files, "first": first, "last": last,
                                  "photo_files": photo or 0, "shop_names": names})
            finally:
                connection.close()
        gaps, previous = [], None
        for day in covered:
            current = date.fromisoformat(day)
            if previous and (current - previous).days > 1:
                gaps.append([(previous + timedelta(days=1)).isoformat(), (current - timedelta(days=1)).isoformat()])
            previous = current
        for week in weeks:
            week["posted"] = posted.get(week["week"], 0)
            start = date.fromisoformat(week["week"])
            week["complete"] = all((start + timedelta(days=i)).isoformat() in covered for i in range(7))
        return {
            "coverage": {"first": covered[0] if covered else None, "last": covered[-1] if covered else None, "gaps": gaps},
            "shop": shop, "shops": shops,
            "weeks": weeks, "imports": {"files": imports[0], "last": imports[1]},
            "totals_range": list(totals) if totals and totals[0] else None,
            "sources": [str(s) for s in self.sources],
            "rule": {"thresholds": [[floor, STATUS_LABELS[label]] for floor, label in THRESHOLDS],
                     "window": "发布当周 + 下一周（周一至周日）的播放量",
                     "flags": {"late": f"窗口播放 <3,000，累计播放 ≥{LATE_VIEWS:,} 或累计出单 ≥{LATE_ORDERS} 单",
                               "no_orders": f"窗口播放 ≥3,000，发布 {NO_ORDER_DAYS} 天后仍 0 单"}},
        }


def _modified(path):
    """File version: modification time (to the microsecond) and size."""
    stat = Path(path).stat()
    return f"{datetime.fromtimestamp(stat.st_mtime_ns / 1e9).isoformat(timespec='microseconds')}|{stat.st_size}"
