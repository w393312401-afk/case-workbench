"""TikTok Shop video-analytics store for the workbench.

A separate SQLite file under the runtime folder; the CaseStore stays the only
record of cases. Imports are idempotent: a file's date or range replaces the
rows previously imported for it.

Export shapes, recognised by the header row:
- per-video ("Creator name, ..., Video ID, ...") for one date range. One-day
  ranges go to `daily`, longer ranges to `periods` with `span_days`.
- all-video totals ("Time, Video-attributed GMV, ...") with one row per day,
  kept in `totals_daily`; not mixed into per-video figures.

Per-video figures combine daily rows and 7-day periods without double
counting: a day covered by a weekly export uses the weekly rows. Monthly
ranges are kept for reference only.
"""
from __future__ import annotations

import math
import re
import shutil
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import openpyxl

# Follow-up rule, calibrated on 191 videos posted Apr–May 2026 with complete
# daily data: views in the posting week plus the next Monday-start week.
# <1k: 0–15% sold within 30 days (1% of GMV); 1k–3k: 22–46%; 3k–10k: ~70%;
# >=10k: 91% and 75% of GMV.
THRESHOLDS = ((10000, "focus"), (3000, "follow"), (1000, "watch"), (0, "drop"))
STATUS_LABELS = {"focus": "重点复制", "follow": "跟进", "watch": "观察", "drop": "放弃",
                 "pending": "待数据", "incomplete": "数据不全"}

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
TEXT = {"account", "creator_id", "title", "video_id", "posted_at", "products", "diagnosis", "date"}
METRICS = [c for c in dict.fromkeys(COLUMNS.values()) if c not in TEXT]
TOTAL_METRICS = [c for c in TOTAL_COLUMNS.values() if c != "date"]
VIDEO_TEXT = ["account", "creator_id", "title", "posted_at", "products"]
SUMMED = ["views", "likes", "comments", "shares", "impressions", "clicks", "orders", "items", "gmv"]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS daily (
    video_id TEXT, date TEXT, {", ".join(f"{c} TEXT" for c in VIDEO_TEXT)},
    {", ".join(f"{m} REAL" for m in METRICS)}, diagnosis TEXT, source_file TEXT,
    PRIMARY KEY (video_id, date)
);
CREATE TABLE IF NOT EXISTS periods (
    video_id TEXT, range_start TEXT, range_end TEXT, span_days INTEGER, {", ".join(f"{c} TEXT" for c in VIDEO_TEXT)},
    {", ".join(f"{m} REAL" for m in METRICS)}, diagnosis TEXT, source_file TEXT,
    PRIMARY KEY (video_id, range_start, range_end)
);
CREATE TABLE IF NOT EXISTS totals_daily (
    date TEXT PRIMARY KEY, {", ".join(f"{m} REAL" for m in TOTAL_METRICS)}, source_file TEXT
);
CREATE TABLE IF NOT EXISTS imports (
    source_file TEXT PRIMARY KEY, kind TEXT, range_start TEXT, range_end TEXT, rows INTEGER, modified TEXT, imported_at TEXT
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


def read_export(path):
    try:
        sheet = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    except Exception as error:
        raise ImportError_(f"无法打开表格（{type(error).__name__}）") from None
    rows = list(sheet.iter_rows(values_only=True))
    match = re.search(r"(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", str(rows[0][0] if rows and rows[0] else ""))
    if not match or len(rows) < 3:
        raise ImportError_("缺少 [Date Range] 行，不像 TikTok Shop 视频分析导出")
    header = list(rows[2])
    names = [h for h in header if h]
    if "Video ID" in names:
        kind, mapping, fraction = "video", COLUMNS, False
    elif names and names[0] == "Time":
        kind, mapping, fraction = "totals", TOTAL_COLUMNS, True
    else:
        raise ImportError_(f"无法识别的表头：{names[:5]}")
    unknown = [h for h in names if h not in mapping]
    if unknown:
        raise ImportError_(f"未识别的列：{unknown}")
    key = "video_id" if kind == "video" else "date"
    records = []
    for row in rows[3:]:
        record = {mapping[name]: _value(mapping[name], raw, fraction) for name, raw in zip(header, row) if name}
        if record.get(key):
            records.append(record)
    return kind, match.group(1), match.group(2), records


def _monday(day):
    return day - timedelta(days=day.weekday())


def product_short(products):
    text = products or ""
    model = re.search(r"\b(\d{3}[A-Z])\b", text)
    return model.group(1) if model else (" ".join(text.replace("SHARPAL", "").split()[:3]) or "未标注")


def headline(title):
    """Title without the trailing hashtags the export appends."""
    return re.split(r"\s*#", title or "", maxsplit=1)[0].strip() or (title or "")


class PerformanceStore:
    def __init__(self, runtime_root, sources=()):
        self.root = Path(runtime_root) / "performance"
        self.database = self.root / "performance.sqlite"
        self.uploads = self.root / "uploads"
        self.sources = [Path(s) for s in sources]
        self.lock = threading.Lock()

    def connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, isolation_level=None, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        return connection

    # ---- import -----------------------------------------------------------
    def _import_file(self, connection, path):
        kind, start, end, records = read_export(path)
        if kind == "totals":
            dates = [r["date"] for r in records]
            connection.execute(f"DELETE FROM totals_daily WHERE date IN ({','.join('?' * len(dates))})", dates)
            columns = ["date", *TOTAL_METRICS]
            connection.executemany(f"INSERT INTO totals_daily VALUES ({','.join('?' * (len(columns) + 1))})",
                                   [[r.get(c) for c in columns] + [str(path)] for r in records])
            table = "totals_daily"
        else:
            fields = [*VIDEO_TEXT, *METRICS, "diagnosis"]
            rows = [[r.get(c) for c in fields] for r in records]
            if start == end:
                table = "daily"
                connection.execute("DELETE FROM daily WHERE date = ?", (start,))
                connection.executemany(f"INSERT INTO daily VALUES ({','.join('?' * (len(fields) + 3))})",
                                       [[r["video_id"], start, *row, str(path)] for r, row in zip(records, rows)])
            else:
                table = "periods"
                span = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
                connection.execute("DELETE FROM periods WHERE range_start = ? AND range_end = ?", (start, end))
                connection.executemany(f"INSERT INTO periods VALUES ({','.join('?' * (len(fields) + 5))})",
                                       [[r["video_id"], start, end, span, *row, str(path)] for r, row in zip(records, rows)])
        connection.execute("INSERT OR REPLACE INTO imports VALUES (?, ?, ?, ?, ?, ?, ?)", (
            str(path), table, start, end, len(records), _modified(path), datetime.now().isoformat(timespec="seconds")))
        return {"file": path.name, "kind": table, "range": [start, end], "rows": len(records)}

    def import_paths(self, paths, skip_unchanged=True):
        """Import files; report per-file results. Never raises for a bad file."""
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
                        imported.append(self._import_file(connection, path))
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
    def _facts(self, connection):
        """Per-video rows without double counting daily and weekly exports."""
        # A shifted weekly export can overlap another week's totals. Choose
        # whole newer intervals per video; an older partial overlap cannot be
        # split into daily metrics. Every read path uses this same selection.
        selected, covered, excluded = [], {}, {}
        for row in connection.execute("SELECT video_id, range_start, range_end, source_file FROM periods WHERE span_days=7 ORDER BY video_id, range_end DESC, range_start DESC, source_file DESC"):
            video_id, start, end, source_file = row
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            days = {(first + timedelta(days=i)).isoformat() for i in range((last-first).days+1)}
            used = covered.setdefault(video_id, set())
            if days & used:
                excluded.setdefault(video_id, []).append({"start": start, "end": end, "source_file": source_file})
            else:
                selected.append((video_id, start, end))
                used.update(days)
        connection.executescript("""
            DROP TABLE IF EXISTS temp.week_ranges;
            CREATE TEMP TABLE week_ranges (video_id TEXT, s TEXT, e TEXT, PRIMARY KEY (video_id, s, e));
        """)
        connection.executemany("INSERT INTO week_ranges VALUES (?,?,?)", selected)
        connection.executescript("""
            DROP VIEW IF EXISTS temp.facts;
            CREATE TEMP VIEW facts AS
              SELECT video_id, date start, date end, account, title, posted_at, products,
                     views, likes, comments, shares, impressions, clicks, orders, items, gmv, source_file
              FROM daily d WHERE NOT EXISTS (SELECT 1 FROM week_ranges w
                  WHERE w.video_id = d.video_id AND d.date BETWEEN w.s AND w.e)
              UNION ALL
              SELECT video_id, range_start, range_end, account, title, posted_at, products,
                     views, likes, comments, shares, impressions, clicks, orders, items, gmv, source_file
              FROM periods p WHERE p.span_days = 7 AND EXISTS (SELECT 1 FROM week_ranges w
                  WHERE w.video_id = p.video_id AND w.s = p.range_start AND w.e = p.range_end);
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

    def videos(self, case_links=None):
        case_links = case_links or {}
        with self.lock:
            connection = self.connect()
            try:
                self._facts(connection)
                covered = self.coverage(connection)
                last_day = max(covered) if covered else None
                first_seen = dict(connection.execute("SELECT account, MIN(start) FROM facts GROUP BY account").fetchall())
                rows = connection.execute(f"""
                    SELECT video_id, MIN(account) account, MIN(title) title, MIN(posted_at) posted_at, MIN(products) products,
                           {", ".join(f"SUM({m}) {m}" for m in SUMMED)}, MIN(start) first_data, MAX(end) last_data,
                           COUNT(*) data_rows, COUNT(orders) order_rows, COUNT(clicks) click_rows
                    FROM facts GROUP BY video_id""").fetchall()
                window = {row[0]: row[1] for row in connection.execute("""
                    SELECT video_id, SUM(views) FROM facts
                    WHERE end <= date(substr(posted_at,1,10), '-' || ((strftime('%w', substr(posted_at,1,10)) + 6) % 7) || ' days', '+13 days')
                    GROUP BY video_id""")}
                weekly = {}
                for video_id, week, views, gmv in connection.execute("""
                        SELECT video_id, date(start, '-' || ((strftime('%w', start) + 6) % 7) || ' days') week, SUM(views), SUM(gmv)
                        FROM facts GROUP BY video_id, week"""):
                    weekly.setdefault(video_id, {})[week] = (views or 0, gmv or 0)
            finally:
                connection.close()
        this_week = _monday(date.fromisoformat(last_day)).isoformat() if last_day else None
        result = []
        for row in rows:
            item = dict(row)
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
                window_views = window.get(item["video_id"]) or 0
                span = [(posted_day + timedelta(days=i)).isoformat() for i in range((window_end - posted_day).days + 1)]
                if last_day and window_end.isoformat() > last_day:
                    status = "pending"
                elif any(day not in covered for day in span) or (first_seen.get(item["account"]) or "9999") > posted:
                    status = "incomplete"
                else:
                    status = next(label for floor, label in THRESHOLDS if window_views >= floor)
                item["window_end"] = window_end.isoformat()
            item.update({
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

    def video_brief(self, video_ids):
        """The few figures shown beside a video elsewhere (title, account, posting day, views, clicks, orders, GMV), for just
        these videos. A light cousin of videos(), which works out every video's follow-up status and takes seconds.
        Empty (and nothing is created) before any export was imported."""
        ids = sorted({str(v) for v in video_ids if v})
        if not ids or not self.database.is_file():
            return {}
        result = {}
        with self.lock:
            connection = self.connect()
            try:
                excluded = self._facts(connection)
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
                        item.update(headline=headline(item["title"]), product=product_short(item["products"]),
                                    posted_on=(latest["posted_at"] or "")[:10] or None,
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
                values.append((str(path), stat.st_size, stat.st_mtime_ns, getattr(stat, "st_ino", None)))
            except OSError:
                values.append((str(path), None))
        return tuple(values)

    def video(self, video_id, case_links=None):
        videos = self.videos(case_links)
        rows = [v for v in videos if v["video_id"] == video_id]
        if not rows:
            return None
        row = rows[0]
        # Where this video sits among the others: first-two-week views against videos whose window
        # is complete (the follow-up rule's basis), cumulative GMV against every imported video.
        # "Top N%": the share of videos at or above this one, rounded up, never below 1%.
        def top_pct(values, value):
            return max(1, math.ceil(100 * sum(other >= value for other in values) / len(values)))

        judged = [v["window_views"] for v in videos if v["status"] in {"focus", "follow", "watch", "drop"}]
        ranks = {"judged_videos": len(judged), "all_videos": len(videos),
                 "gmv_top_pct": top_pct([v["gmv"] or 0 for v in videos], row["gmv"] or 0)}
        if judged and row["status"] in {"focus", "follow", "watch", "drop"}:
            ranks["window_top_pct"] = top_pct(judged, row["window_views"])
        with self.lock:
            connection = self.connect()
            try:
                self._facts(connection)
                series = [dict(r) for r in connection.execute(
                    "SELECT start, end, views, clicks, orders, items, gmv FROM facts WHERE video_id = ? ORDER BY start", (video_id,))]
            finally:
                connection.close()
        return {**row, **ranks, "series": series}

    def analytics(self):
        """Compact figures behind the dashboard, so the page can re-slice them by account, product and period without asking again.

        weekly:  one row per video and Monday-start week (the facts view, so daily and weekly exports never double count).
        monthly: the same per video and month, in the report each month is best known from: a monthly export when there is
                 one, else the daily rows, else the weekly ones (a week belongs to the month holding its Thursday).
        shop_daily: all-video daily totals, which are a different caliber from the per-video rows and never mixed with them.
        Row layout is [video index, week|month, views, impressions, clicks, orders, items, gmv]."""
        measures = "SUM(views), SUM(impressions), SUM(clicks), SUM(orders), SUM(items), SUM(gmv)"
        with self.lock:
            connection = self.connect()
            try:
                self._facts(connection)
                weekly = connection.execute(f"""
                    SELECT video_id, date(start, '-' || ((strftime('%w', start) + 6) % 7) || ' days') week, {measures}
                    FROM facts GROUP BY video_id, week ORDER BY week, video_id""").fetchall()
                month_of = "CASE WHEN start = end THEN substr(start, 1, 7) ELSE substr(date(start, '+3 days'), 1, 7) END"
                derived = connection.execute(f"""
                    SELECT video_id, {month_of} month, start = end is_daily, {measures}
                    FROM facts GROUP BY video_id, month, is_daily""").fetchall()
                reported = connection.execute(f"""
                    SELECT video_id, substr(range_start, 1, 7) month, {measures}
                    FROM periods WHERE span_days >= 28 GROUP BY video_id, month""").fetchall()
                attributes = connection.execute("""
                    SELECT video_id, MIN(account), MIN(products), MIN(posted_at) FROM (
                        SELECT video_id, account, products, posted_at FROM daily
                        UNION ALL SELECT video_id, account, products, posted_at FROM periods) GROUP BY video_id""").fetchall()
                shop = connection.execute("SELECT date, gmv, orders, views, clicks FROM totals_daily ORDER BY date").fetchall()
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
            "shop_daily": [{"date": r[0], "gmv": round(r[1] or 0, 2), "orders": int(r[2] or 0), "views": int(r[3] or 0), "clicks": int(r[4] or 0)} for r in shop],
        }

    def summary(self):
        with self.lock:
            connection = self.connect()
            try:
                self._facts(connection)
                covered = sorted(self.coverage(connection))
                weeks = [dict(r) for r in connection.execute("""
                    SELECT date(start, '-' || ((strftime('%w', start) + 6) % 7) || ' days') week,
                           COUNT(DISTINCT video_id) videos, SUM(views) views, SUM(items) items, SUM(gmv) gmv
                    FROM facts GROUP BY week ORDER BY week""")]
                posted = dict(connection.execute("""
                    SELECT date(substr(p,1,10), '-' || ((strftime('%w', substr(p,1,10)) + 6) % 7) || ' days'), COUNT(*)
                    FROM (SELECT video_id, MIN(posted_at) p FROM facts GROUP BY video_id) GROUP BY 1""").fetchall())
                imports = connection.execute("SELECT COUNT(*), MAX(imported_at) FROM imports").fetchone()
                totals = connection.execute("SELECT MIN(date), MAX(date) FROM totals_daily").fetchone()
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
            "weeks": weeks, "imports": {"files": imports[0], "last": imports[1]},
            "totals_range": list(totals) if totals and totals[0] else None,
            "sources": [str(s) for s in self.sources],
            "rule": {"thresholds": [[floor, STATUS_LABELS[label]] for floor, label in THRESHOLDS],
                     "window": "发布当周 + 下一周（周一至周日）的播放量"},
        }


def _modified(path):
    """File version: modification time (to the microsecond) and size."""
    stat = Path(path).stat()
    return f"{datetime.fromtimestamp(stat.st_mtime_ns / 1e9).isoformat(timespec='microseconds')}|{stat.st_size}"
