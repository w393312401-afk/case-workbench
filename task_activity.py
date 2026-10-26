"""Durable, non-authoritative observation of workbench runs.

Never execute or retry business work from this module. Connections are short lived so
the existing full-data restore can replace the database on Windows as well.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import quote, urlencode
from uuid import uuid4

from direction_store import product_key
from production_naming import production_title

ACTIVE = frozenset({"queued", "running", "stopping"})
TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted", "needs_attention"})
KINDS = frozenset({"ai", "voice", "timeline", "edit", "validation", "stills", "images", "image_review", "group_review", "package",
                   "table", "clips", "data", "case", "prepublish"})
RECOVERY_PHASES = frozenset({"save", "结果待恢复", "登记待恢复", "记录待恢复", "待核实外部结果", "结果已保存，正在登记"})
IDENTIFIER = re.compile(r"[A-Za-z0-9_:.\-]{1,300}")
UNAVAILABLE = "运行日志暂时不可用，任务仍按原流程处理。"
# Waiting on the service or on someone; the panel's default view keeps these in front. A failure stops
# asking once the same step of the same order has run again; that later run carries the outcome.
OPEN_RUN = ("(r.status IN ('queued','running','stopping') OR ((r.owner_archived=0 AND r.content_archived=0) AND r.attention_resolved=0 AND "
            "(r.status='needs_attention' OR (r.status IN ('failed','interrupted') AND NOT EXISTS "
            "(SELECT 1 FROM runs later WHERE later.owner_id=r.owner_id AND later.kind=r.kind "
            "AND r.kind NOT IN ('images','image_review','stills','clips') AND later.started_at>r.started_at)))))")
# The variant parser names an unlabeled variant 第 N 条, which says nothing once several orders share it.
GENERIC_TITLE = re.compile(r"第\s*\d+\s*条")


class ActivityError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def safe_error(error):
    """Classify errors without forwarding arbitrary external output to the feed."""
    value = str(error).lower()
    if isinstance(error, TimeoutError) or any(word in value for word in ("timeout", "timed out", "超时")):
        return "等待服务超时，请在原任务中核对结果。"
    if any(word in value for word in ("quota", "额度", "余额", "rate limit")):
        return "服务额度或请求频率受限，请核对服务状态。"
    if any(word in value for word in ("unauthorized", "credential", "授权", "登录", "密钥")):
        # Name the provider so the fix is obvious: Codex logs in inside its own app, Claude in 设置.
        if "codex" in value:
            return "本机 Codex 登录已失效，请在电脑上打开 Codex 重新登录后重试，或改用 Claude 模型。"
        if "claude" in value:
            return "Claude 订阅授权不可用，请到「设置」重新连接 Claude 订阅，或改用 Codex 模型。"
        return "服务连接或授权不可用，请检查设置。"
    if any(word in value for word in ("disk", "permission", "保存", "写入", "磁盘")):
        return "结果保存未完成，请在原任务中检查恢复入口。"
    if any(word in value for word in ("network", "connection", "网络", "连接")):
        return "服务连接中断，请在原任务中核对结果。"
    return "本次处理未完成，请打开原任务查看原因及恢复方式。"


def record_activity(owner, run_id, **fields):
    """Observability must never turn a completed external operation into a retry."""
    try:
        activity = getattr(owner, "activity", None)
        if activity is not None:
            return activity.record(run_id, **fields)
    except Exception:
        pass
    return None


def activity_label(item, fallback=""):
    """Video names share the content list's timeline and topic; carousel labels stay descriptive."""
    if str(item.get("id") or "").startswith("PRD-") or "timelines" in item or "lines" in item and "slides" not in item:
        return production_title(item)
    return _plain_label(item, fallback)


def _plain_label(item, fallback=""):
    title = str(item.get("title") or "").strip()
    if title and not GENERIC_TITLE.fullmatch(title):
        return title
    slides, lines = item.get("slides") or [], item.get("lines") or []
    if slides and isinstance(slides[0], dict):
        opening = slides[0].get("title_zh") or slides[0].get("body_zh") or slides[0].get("body")
    elif lines and isinstance(lines[0], dict):
        opening = lines[0].get("zh") or lines[0].get("spoken")
    else:
        opening = next((line for line in str(item.get("translation_zh") or item.get("text") or "").splitlines() if line.strip()), "")
    opening = " ".join(str(opening or "").split())
    if len(opening) > 24:
        opening = opening[:24].rstrip() + "…"
    name = " ".join(part for part in (str(item.get("direction_id") or "").strip(), title or str(fallback or item.get("id") or "")) if part)
    return f"{name}「{opening}」" if opening else name


def _since(value):
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value)[:40])
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise ActivityError("运行记录的时间范围无效。")
    # Stored stamps are UTC with milliseconds, so the bound compares as text.
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _number(value, default=0, maximum=None):
    if value is None:
        return default
    if isinstance(value, bool) or not re.fullmatch(r"\d{1,18}", str(value)):
        raise ActivityError("运行记录的分页参数无效。")
    result = int(value)
    if maximum is not None and not 1 <= result <= maximum:
        raise ActivityError("运行记录的分页数量无效。")
    return result


class ActivityStore:
    def __init__(self, runtime):
        self.path = Path(runtime) / "task-activity.sqlite"
        self.lock = threading.RLock()
        self.error = ""
        try:
            self._initialize()
        except (OSError, sqlite3.Error, ActivityError):
            self.error = UNAVAILABLE

    @contextmanager
    def _connect(self):
        # Follow the business-store boundary: no sidecar outside the runtime root.
        if self.path.is_symlink() or (self.path.exists() and self.path.stat().st_nlink > 1):
            raise ActivityError(UNAVAILABLE, 503)
        connection = sqlite3.connect(self.path, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner_id TEXT NOT NULL,
                    source_id TEXT NOT NULL DEFAULT '', product TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '', case_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL, phase TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
                    step TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    finished_at TEXT, result_count INTEGER, version INTEGER NOT NULL DEFAULT 0,
                    event_id INTEGER);
                CREATE INDEX IF NOT EXISTS runs_owner ON runs(owner_id, source_id);
                CREATE INDEX IF NOT EXISTS runs_status ON runs(status);
                CREATE INDEX IF NOT EXISTS runs_product_version ON runs(product, version);
                CREATE TABLE IF NOT EXISTS changes (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL, kind TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS logs (seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    at TEXT NOT NULL, status TEXT NOT NULL, phase TEXT NOT NULL,
                    message TEXT NOT NULL, result_count INTEGER, event_key TEXT,
                    UNIQUE(run_id, event_key));
                CREATE INDEX IF NOT EXISTS logs_run ON logs(run_id, seq);
                CREATE TABLE IF NOT EXISTS notifications (event_id INTEGER PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS claims (browser_id TEXT NOT NULL,
                    event_id INTEGER NOT NULL REFERENCES notifications(event_id) ON DELETE CASCADE,
                    PRIMARY KEY(browser_id, event_id));
            """)
            db.execute("INSERT OR IGNORE INTO meta VALUES('epoch', ?)", (uuid4().hex,))
            db.execute("INSERT OR IGNORE INTO meta VALUES('schema', '1')")
            if "reconcile_pending" not in {row["name"] for row in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN reconcile_pending INTEGER NOT NULL DEFAULT 0")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)")}
            for name, definition in (("owner_archived", "INTEGER NOT NULL DEFAULT 0"),
                                     ("content_archived", "INTEGER NOT NULL DEFAULT 0"),
                                     ("attention_resolved", "INTEGER NOT NULL DEFAULT 0"),
                                     ("attention_note", "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
        self.error = ""

    def reload(self, reset_epoch=False):
        try:
            self._initialize()
            if reset_epoch:
                with self.lock, self._connect() as db:
                    db.execute("UPDATE meta SET value=? WHERE key='epoch'", (uuid4().hex,))
        except (OSError, sqlite3.Error, ActivityError):
            self.error = UNAVAILABLE

    def close(self):
        pass  # No retained connection or file handle.

    @staticmethod
    def _url(run):
        product = quote(run["product"] or "all", safe="")
        owner = quote(run["owner_id"], safe="")
        if run["kind"] == "ai":
            return f"#/{product}/ai/{owner}?" + urlencode({"round": run["source_id"]})
        if run["kind"] == "table":
            return f"#/{product}/scripts/{owner}?" + urlencode({"table": run["source_id"]})
        if run["kind"] == "data":
            return "#/all/settings"
        if run["kind"] == "case":
            return f"#/{product}/case/{owner}"
        return f"#/{product}/production/{owner}?" + urlencode({"step": run["step"], "run": run["source_id"]})

    @classmethod
    def _public(cls, row):
        result = dict(row)
        result.pop("reconcile_pending", None)
        content_archived = result.pop("content_archived", False)
        result["owner_archived"] = bool(result.get("owner_archived") or content_archived)
        result["attention_resolved"] = bool(result.get("attention_resolved"))
        result["read"] = bool(result.pop("notification_read", 1))
        result["url"] = cls._url(result)
        return result

    def record(self, run_id, **fields):
        if not isinstance(run_id, str) or not IDENTIFIER.fullmatch(run_id):
            raise ActivityError("运行编号无效。")
        allowed = {"kind", "owner_id", "source_id", "product", "title", "case_id", "status", "phase", "message", "result_count", "step", "event_key", "reconcile_pending"}
        if fields.keys() - allowed:
            raise ActivityError("运行记录包含未知字段。")
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if previous and previous["status"] in TERMINAL and fields.get("status") == "stopping":
                return None
            event_key = fields.pop("event_key", None)
            if event_key is not None:
                event_key = str(event_key)[:300]
                if db.execute("SELECT 1 FROM logs WHERE run_id=? AND event_key=?", (run_id, event_key)).fetchone():
                    if previous and previous["status"] in ACTIVE and fields.get("status") in TERMINAL:
                        # An explicit retry may reach the same durable terminal state again.
                        # Its new transition must settle the run, while repeated callbacks
                        # within one terminal state remain idempotent.
                        event_key = f"{event_key}:attempt-{previous['version']}"
                    else:
                        return None
            at = stamp()
            row = dict(previous) if previous else dict(run_id=run_id, kind="", owner_id="", source_id="", product="", title="", case_id="", status="queued", phase="", message="", step="", started_at=at, updated_at=at, finished_at=None, result_count=None, version=0, event_id=None, reconcile_pending=0, owner_archived=0, content_archived=0, attention_resolved=0, attention_note="")
            for key, value in fields.items():
                if key == "reconcile_pending":
                    row[key] = int(bool(value))
                elif key == "result_count":
                    if value is not None and (type(value) is not int or value < 0):
                        raise ActivityError("运行结果数量无效。")
                    row[key] = value
                else:
                    row[key] = str(value or "")[:(500 if key == "message" else 300)]
            row["product"] = product_key(row["product"])
            if "reconcile_pending" not in fields:
                row["reconcile_pending"] = int(row["status"]=="needs_attention" and row["phase"] in RECOVERY_PHASES)
            if row["kind"] not in KINDS or row["status"] not in ACTIVE | TERMINAL or not IDENTIFIER.fullmatch(row["owner_id"]):
                raise ActivityError("运行记录类型或状态无效。")
            changed = not previous or bool(event_key) or any(row[key] != previous[key] for key in fields)
            if not changed:
                return None
            status_changed = not previous or row["status"] != previous["status"]
            row["updated_at"] = at
            if row["status"] in ACTIVE:
                row["finished_at"] = None
                row["event_id"] = None
                row["attention_resolved"] = 0
                row["attention_note"] = ""
            elif status_changed:
                row["finished_at"] = at
            version = db.execute("INSERT INTO changes(run_id,kind) VALUES(?, 'run')", (run_id,)).lastrowid
            row["version"] = version
            terminal_event = row["status"] in TERMINAL and (status_changed or bool(event_key))
            if terminal_event:
                row["event_id"] = version
                row["finished_at"] = at
            columns = tuple(row)
            db.execute(f"INSERT INTO runs({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) ON CONFLICT(run_id) DO UPDATE SET " + ','.join(f"{key}=excluded.{key}" for key in columns if key != "run_id"), tuple(row.values()))
            # Metadata-only changes do not manufacture another processing step.
            if not previous or any(row[key] != previous[key] for key in ("status", "phase", "message", "result_count")) or event_key:
                db.execute("INSERT INTO logs(run_id,at,status,phase,message,result_count,event_key) VALUES(?,?,?,?,?,?,?)", (run_id,at,row["status"],row["phase"],row["message"],row["result_count"],event_key))
            if terminal_event:
                db.execute("INSERT INTO notifications VALUES(?,?,?,?)", (version,run_id,at,int(row["status"] == "cancelled")))
            self.error = ""
            return version

    def _rows(self, db, where="1", values=(), suffix=""):
        return db.execute("SELECT r.*, COALESCE(n.read,1) AS notification_read FROM runs r LEFT JOIN notifications n ON n.event_id=r.event_id WHERE " + where + " " + suffix, values).fetchall()

    def feed(self, *, after=None, limit=50, before=None, product=None, filter="all", owner_id=None, source_id=None, kind=None, q=None, group=None, since=None):
        limit = _number(limit,50,200)
        after = _number(after) if after is not None else None
        before = _number(before) if before is not None else None
        if filter not in {"all", "active", "unread", "attention", "current"}:
            raise ActivityError("运行记录筛选无效。")
        if group not in (None, "", "owner"):
            raise ActivityError("运行记录分组无效。")
        since = _since(since)
        if kind and kind not in KINDS:
            raise ActivityError("运行记录类型筛选无效。")
        if q is not None and len(q) > 100:
            raise ActivityError("运行记录搜索词过长。")
        try:
            with self.lock, self._connect() as db:
                maximum = db.execute("SELECT COALESCE(MAX(id),0) FROM changes").fetchone()[0]
                epoch = db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
                counts = {"active": db.execute("SELECT COUNT(*) FROM runs WHERE status IN ('queued','running','stopping')").fetchone()[0],
                          "queued": db.execute("SELECT COUNT(*) FROM runs WHERE status='queued'").fetchone()[0],
                          "unread": db.execute("SELECT COUNT(*) FROM notifications n JOIN runs r ON r.event_id=n.event_id WHERE n.read=0 AND (r.owner_archived=0 AND r.content_archived=0) AND r.attention_resolved=0").fetchone()[0]}
                where, values = ["1"], []
                if product is not None and product:
                    # Runs without a product, such as data backups, belong to every product's view.
                    where.append("(r.product=? OR r.product='')"); values.append(product_key(product))
                if owner_id:
                    where.append("r.owner_id=?"); values.append(owner_id)
                if source_id:
                    where.append("r.source_id=?"); values.append(source_id)
                if kind:
                    where.append("r.kind=?"); values.append(kind)
                if q and q.strip():
                    where.append("r.title LIKE ? ESCAPE '\\'")
                    values.append("%" + re.sub(r"([\\%_])", r"\\\1", q.strip()) + "%")
                if filter == "active":
                    where.append("r.status IN ('queued','running','stopping')")
                elif filter == "unread":
                    where.append("n.read=0 AND (r.owner_archived=0 AND r.content_archived=0) AND r.attention_resolved=0")
                elif filter == "attention":
                    where.append("r.status IN ('needs_attention','failed','interrupted')")
                    where.append("(r.owner_archived=0 AND r.content_archived=0) AND r.attention_resolved=0")
                elif filter == "current":
                    # What is running or waiting on someone, plus what ended since the reader's start of day.
                    if since:
                        where.append(f"({OPEN_RUN} OR COALESCE(r.finished_at,r.updated_at)>=?)"); values.append(since)
                    else:
                        where.append(OPEN_RUN)
                    where.append("(r.status IN ('queued','running','stopping') OR (r.owner_archived=0 AND r.content_archived=0))")
                events, removed = [], []
                cursor = maximum
                if after is not None:
                    changes = db.execute("SELECT * FROM changes WHERE id>? ORDER BY id LIMIT ?", (after,limit+1)).fetchall()
                    has_more = len(changes) > limit
                    page = changes[:limit]
                    cursor = page[-1]["id"] if has_more and page else maximum
                    ids = list(dict.fromkeys(change["run_id"] for change in page))
                    removed = list(dict.fromkeys(change["run_id"] for change in page if change["kind"] == "deleted"))
                    where.append("r.run_id IN (" + ','.join('?' for _ in ids) + ")" if ids else "0")
                    rows = self._rows(db, " AND ".join(where), (*values,*ids), "ORDER BY r.version DESC")
                    if filter in {"current", "attention", "unread"}:
                        removed.extend(run_id for run_id in ids if run_id not in {row["run_id"] for row in rows})
                    for row in self._rows(db, "n.event_id>? AND n.event_id<=? AND n.read=0 AND r.status!='cancelled' AND (r.owner_archived=0 AND r.content_archived=0) AND r.attention_resolved=0", (after,cursor), "ORDER BY n.event_id"):
                        public = self._public(row)
                        events.append({key:public[key] for key in ("event_id","run_id","status","message","title","product","url")} | {"created_at":public["finished_at"]})
                elif group == "owner":
                    # A page holds whole orders: every matching run of each owner, newest owners first,
                    # so an order's run count never splits across pages.
                    bound = (" HAVING MAX(r.version)<?", (before,)) if before is not None else ("", ())
                    owners = db.execute("SELECT r.owner_id, MAX(r.version) AS top FROM runs r LEFT JOIN notifications n ON n.event_id=r.event_id WHERE "
                                        + " AND ".join(where) + " GROUP BY r.owner_id" + bound[0] + " ORDER BY top DESC LIMIT ?", (*values,*bound[1],limit+1)).fetchall()
                    has_more = len(owners) > limit
                    owners = owners[:limit]
                    ids = [row["owner_id"] for row in owners]
                    where.append("r.owner_id IN (" + ','.join('?' for _ in ids) + ")" if ids else "0")
                    rows = self._rows(db, " AND ".join(where), (*values,*ids), "ORDER BY r.version DESC")
                    next_before = owners[-1]["top"] if owners else None
                else:
                    if before is not None:
                        where.append("r.version<?"); values.append(before)
                    rows = self._rows(db, " AND ".join(where), (*values,limit+1), "ORDER BY r.version DESC LIMIT ?")
                    has_more = len(rows) > limit
                    rows = rows[:limit]
                if after is not None or group != "owner":
                    next_before = rows[-1]["version"] if rows else None
                return {"available":True,"items":[self._public(row) for row in rows],"events":events,"removed":removed,"cursor":cursor,"epoch":epoch,"has_more":has_more,"next_before":next_before,"counts":counts}
        except (OSError, sqlite3.Error, ActivityError):
            self.error = UNAVAILABLE
            return {"available":False,"error":UNAVAILABLE,"items":[],"events":[],"removed":[],"counts":{"active":0,"queued":0,"unread":0},"cursor":0,"epoch":"","has_more":False,"next_before":None}

    def logs(self, run_id, *, after_seq=0, limit=100, before_seq=None, tail=None):
        """Forward pages follow a live run; `tail` opens at the newest lines and `before_seq` pages back from there."""
        after_seq, limit = _number(after_seq), _number(limit,100,500)
        before_seq = _number(before_seq) if before_seq is not None else None
        if tail not in (None, "1") or (tail and before_seq is not None):
            raise ActivityError("运行日志分页参数无效。")
        try:
            with self.lock, self._connect() as db:
                runs = self._rows(db,"r.run_id=?",(run_id,))
                if not runs:
                    raise ActivityError("此前未记录运行日志，或所属任务已删除。",404)
                columns = "SELECT seq,at,status,phase,message,result_count FROM logs WHERE run_id=?"
                if tail or before_seq is not None:
                    bound = (" AND seq<?", (run_id,before_seq,limit+1)) if before_seq is not None else ("", (run_id,limit+1))
                    rows = db.execute(columns + bound[0] + " ORDER BY seq DESC LIMIT ?", bound[1]).fetchall()
                    page = list(reversed(rows[:limit]))
                    newest = db.execute("SELECT COALESCE(MAX(seq),0) FROM logs WHERE run_id=?",(run_id,)).fetchone()[0]
                    return {"run":self._public(runs[0]),"logs":[dict(row) for row in page],"has_more":False,
                            "has_earlier":len(rows)>limit,"first_seq":page[0]["seq"] if page else before_seq,
                            "next_seq":newest if tail else (page[-1]["seq"] if page else after_seq)}
                rows = db.execute(columns + " AND seq>? ORDER BY seq LIMIT ?",(run_id,after_seq,limit+1)).fetchall()
                return {"run":self._public(runs[0]),"logs":[dict(row) for row in rows[:limit]],"has_more":len(rows)>limit,"next_seq":rows[min(len(rows),limit)-1]["seq"] if rows else after_seq}
        except (OSError, sqlite3.Error) as exc:
            raise ActivityError(UNAVAILABLE,503) from exc

    @staticmethod
    def _event_ids(body):
        values = body.get("event_ids")
        if not isinstance(values,list) or len(values)>500 or any(type(value) is not int or value<1 for value in values):
            raise ActivityError("提醒事件编号无效。")
        return list(dict.fromkeys(values))

    def read(self, body):
        if set(body) - {"event_ids","all","before_event_id"}:
            raise ActivityError("已读参数无效。")
        all_events = body.get("all") is True
        if all_events and ("event_ids" in body or "before_event_id" not in body):
            raise ActivityError("全部已读需要当前事件边界。")
        ids = None if all_events else self._event_ids(body)
        bound = _number(body.get("before_event_id"))
        try:
            with self.lock, self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                rows = db.execute("SELECT event_id,run_id FROM notifications WHERE read=0 AND " + ("event_id<=?" if all_events else "event_id IN ("+','.join('?' for _ in ids)+")"), (bound,) if all_events else ids).fetchall()
                for row in rows:
                    db.execute("UPDATE notifications SET read=1 WHERE event_id=?",(row["event_id"],))
                    version = db.execute("INSERT INTO changes(run_id,kind) VALUES(?,'read')",(row["run_id"],)).lastrowid
                    db.execute("UPDATE runs SET version=? WHERE run_id=?",(version,row["run_id"]))
                return {"ok":True,"read":[row["event_id"] for row in rows]}
        except (OSError, sqlite3.Error) as exc:
            raise ActivityError(UNAVAILABLE,503) from exc

    def claim(self, body):
        if set(body) != {"browser_id","event_ids"} or not isinstance(body.get("browser_id"),str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,100}",body["browser_id"]):
            raise ActivityError("提醒浏览器标识无效。")
        ids = self._event_ids(body)
        try:
            with self.lock, self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                claimed=[]
                for event_id in ids:
                    valid=db.execute("SELECT 1 FROM notifications n JOIN runs r ON r.event_id=n.event_id WHERE n.event_id=? AND n.read=0 AND r.status!='cancelled' AND (r.owner_archived=0 AND r.content_archived=0) AND r.attention_resolved=0",(event_id,)).fetchone()
                    if valid and db.execute("INSERT OR IGNORE INTO claims VALUES(?,?)",(body["browser_id"],event_id)).rowcount:
                        claimed.append(event_id)
                return {"claimed":claimed}
        except (OSError, sqlite3.Error) as exc:
            raise ActivityError(UNAVAILABLE,503) from exc

    def delete_owner(self, owner_id):
        return self._delete("owner_id",owner_id)

    def delete_case(self, case_id):
        return self._delete("case_id",case_id)

    def delete_source(self, owner_id, source_id):
        try:
            with self.lock, self._connect() as db:
                for row in db.execute("SELECT run_id FROM runs WHERE owner_id=? AND source_id=?",(owner_id,source_id)).fetchall():
                    db.execute("INSERT INTO changes(run_id,kind) VALUES(?,'deleted')",(row["run_id"],))
                    db.execute("DELETE FROM runs WHERE run_id=?",(row["run_id"],))
        except Exception:
            self.error=UNAVAILABLE

    def delete_run(self, run_id):
        return self._delete("run_id",run_id)

    def delete_owner(self, owner_id):
        return self._delete("owner_id",owner_id)

    def _delete(self, field, value):
        try:
            with self.lock, self._connect() as db:
                for row in db.execute(f"SELECT run_id FROM runs WHERE {field}=?",(value,)).fetchall():
                    db.execute("INSERT INTO changes(run_id,kind) VALUES(?,'deleted')",(row["run_id"],))
                    db.execute("DELETE FROM runs WHERE run_id=?",(row["run_id"],))
        except Exception:
            self.error=UNAVAILABLE

    def existing(self):
        try:
            with self.lock,self._connect() as db:
                return [dict(row) for row in db.execute("SELECT * FROM runs")]
        except (OSError,sqlite3.Error,ActivityError):
            return []

    def attention(self, owner_id):
        try:
            with self.lock,self._connect() as db:
                return [dict(row) for row in db.execute("SELECT run_id,kind,source_id FROM runs WHERE owner_id=? AND status='needs_attention' AND reconcile_pending=0",(owner_id,))]
        except (OSError,sqlite3.Error,ActivityError):
            return []

    def archive_owner(self, owner_id, archived, *, source="owner"):
        """Project a reversible business archive without changing any execution result."""
        field = "content_archived" if source == "content" else "owner_archived"
        try:
            with self.lock, self._connect() as db:
                for row in db.execute(f"SELECT run_id FROM runs WHERE owner_id=? AND {field}!=?",
                                      (owner_id, int(bool(archived)))).fetchall():
                    version = db.execute("INSERT INTO changes(run_id,kind) VALUES(?,'run')", (row["run_id"],)).lastrowid
                    db.execute(f"UPDATE runs SET {field}=?,version=? WHERE run_id=?",
                               (int(bool(archived)), version, row["run_id"]))
        except (OSError, sqlite3.Error, ActivityError):
            self.error = UNAVAILABLE

    def project_attention(self, owner_id, resolved):
        """Resolve a request for attention, not its historical failure. Recompute on version changes."""
        try:
            with self.lock, self._connect() as db:
                rows = db.execute("SELECT * FROM runs WHERE owner_id=? AND status IN ('needs_attention','failed','interrupted')", (owner_id,)).fetchall()
                for row in rows:
                    note = str(resolved.get(row["run_id"]) or "")[:500] if not row["reconcile_pending"] else ""
                    if row["attention_resolved"] == bool(note) and row["attention_note"] == note:
                        continue
                    version = db.execute("INSERT INTO changes(run_id,kind) VALUES(?,'run')", (row["run_id"],)).lastrowid
                    db.execute("UPDATE runs SET attention_resolved=?,attention_note=?,version=? WHERE run_id=?",
                               (int(bool(note)), note, version, row["run_id"]))
        except (OSError, sqlite3.Error, ActivityError):
            self.error = UNAVAILABLE

    def settle(self, run_ids, message):
        """Close attention a later step already resolved. The old alert is read; no new one is raised."""
        ids = [run_id for run_id in dict.fromkeys(run_ids) if isinstance(run_id,str) and IDENTIFIER.fullmatch(run_id)]
        if not ids:
            return []
        message = str(message)[:500]
        try:
            with self.lock,self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                settled = []
                for run_id in ids:
                    row = db.execute("SELECT status,reconcile_pending,result_count FROM runs WHERE run_id=?",(run_id,)).fetchone()
                    if not row or row["status"] != "needs_attention" or row["reconcile_pending"]:
                        continue
                    at = stamp()
                    version = db.execute("INSERT INTO changes(run_id,kind) VALUES(?,'run')",(run_id,)).lastrowid
                    db.execute("UPDATE runs SET status='completed',phase='settled',message=?,updated_at=?,version=? WHERE run_id=?",(message,at,version,run_id))
                    db.execute("UPDATE notifications SET read=1 WHERE run_id=?",(run_id,))
                    db.execute("INSERT INTO logs(run_id,at,status,phase,message,result_count,event_key) VALUES(?,?,?,?,?,?,NULL)",(run_id,at,"completed","settled",message,row["result_count"]))
                    settled.append(run_id)
                self.error = ""
                return settled
        except (OSError,sqlite3.Error,ActivityError):
            self.error = UNAVAILABLE
            return []

    def retitle(self, run_id, title):
        """Rename a run in place: its time, status and log stay as they were."""
        title = str(title or "")[:300]
        try:
            with self.lock,self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT title FROM runs WHERE run_id=?",(run_id,)).fetchone()
                if not row or row["title"] == title:
                    return False
                version = db.execute("INSERT INTO changes(run_id,kind) VALUES(?,'run')",(run_id,)).lastrowid
                db.execute("UPDATE runs SET title=?,version=? WHERE run_id=?",(title,version,run_id))
                return True
        except (OSError,sqlite3.Error,ActivityError):
            self.error = UNAVAILABLE
            return False


def settle_resolved_activity(app, kind, item, runs=None):
    """Project current attention separately from a run's immutable execution outcome.

    runs is one earlier read of every run, shared by a whole list pass: an item whose runs already
    show the right archive mark and attention note then costs no database access at all.
    """
    activity = getattr(app, "activity", None)
    if activity is None or not isinstance(item, dict) or not item.get("id"):
        return
    try:
        mine = None if runs is None else [row for row in runs if row["owner_id"] == item["id"]]
        archived = int(bool(item.get("archived", False)))
        if mine is None or any(row.get("owner_archived") != archived for row in mine):
            activity.archive_owner(item["id"], item.get("archived", False))
        rows = [row for row in (activity.existing() if mine is None else mine)
                if row["owner_id"] == item["id"] and row["status"] in {"needs_attention", "failed", "interrupted"}]
        if mine is not None and not rows:
            return
        resolved = {}
        if kind == "carousel":
            slides = {slide.get("number"): slide for slide in item.get("slides", [])}
            def confirmed(number):
                slide = slides.get(number) or {}
                output = next((entry for entry in slide.get("outputs", []) if entry.get("id") == slide.get("output_id")), None)
                return bool(output and output.get("approved") and not output.get("stale"))
            generations = {run.get("id"): run for run in item.get("generations", [])}
            autopilots = {run.get("id"): run for run in item.get("autopilot_runs", [])}
            searches = {run.get("id"): run for run in item.get("searches", [])}
            reviewed = {review.get("id"): slide.get("number") for slide in item.get("slides", [])
                        for output in slide.get("outputs", []) for review in output.get("review_runs", [])}
            for row in rows:
                source = row["source_id"]
                if row["kind"] == "images":
                    autopilot = row["run_id"] == "autopilot:" + source
                    run = (autopilots if autopilot else generations).get(source)
                    numbers = list(slides) if autopilot and run else (run or {}).get("requested_slides") or (run or {}).get("slides") or []
                    if run and run.get("status") != "running" and numbers and all(confirmed(number) for number in numbers):
                        resolved[row["run_id"]] = "这些张的当前成图已人工确认；原运行结果保留。"
                elif row["kind"] == "image_review" and source in reviewed and confirmed(reviewed[source]):
                    resolved[row["run_id"]] = "这一张的当前成图已人工确认；原核对记录保留。"
                elif row["kind"] in {"stills", "clips"}:
                    search = searches.get(source)
                    numbers = (search or {}).get("slides") or []
                    # Clip selection covers the entire group; a still search covers only its requested slides.
                    if row["kind"] == "clips":
                        numbers = list(slides)
                    if numbers and all((slides.get(number) or {}).get("base") for number in numbers):
                        if row["kind"] == "clips" or search and search.get("status") != "running":
                            resolved[row["run_id"]] = "所需底图已选齐；原运行结果保留。"
        elif kind == "job":
            takes = {take.get("id"): take for take in item.get("takes", [])}
            chosen = takes.get(item.get("selected_take")) or {}
            archived_timelines = {timeline.get("unique_id") for timeline in item.get("timelines", []) if timeline.get("archived")}
            archived_edits = {edit.get("id") for edit in item.get("edits", []) if edit.get("timeline_uid") in archived_timelines}
            for row in rows:
                if row["kind"] in {"edit", "validation"} and row["source_id"] in archived_edits:
                    resolved[row["run_id"]] = "对应时间线已归档；原运行结果保留，恢复后重新显示待处理事项。"
                elif item.get("status") == "approved" and row["kind"] in {"edit", "validation", "timeline", "voice"}:
                    resolved[row["run_id"]] = "当前制作版本已人工完成；原运行结果保留。"
                elif row["kind"] == "voice" and chosen:
                    take = takes.get(row["source_id"])
                    if take and str(take.get("created_at") or "") <= str(chosen.get("created_at") or ""):
                        resolved[row["run_id"]] = "已选用配音；原运行结果保留。"
        if mine is not None and all(
                bool(row.get("attention_resolved")) == bool(note := "" if row.get("reconcile_pending") else str(resolved.get(row["run_id"]) or "")[:500])
                and row.get("attention_note") == note for row in rows):
            return
        activity.project_attention(item["id"], resolved)
    except Exception:
        # Observation only: a business read never fails because the feed could not be projected.
        return


def refresh_activity_projection(app):
    """Recheck current artifacts even when the user stays in the list/task panel.

    No business writes or retries: image integrity helpers annotate a detached
    catalog. A failed check withdraws earlier resolution, never invents success.
    """
    activity = getattr(app, "activity", None)
    if activity is None:
        return
    owners = {row["owner_id"] for row in activity.existing()
              if row["status"] in {"needs_attention", "failed", "interrupted"}
              and row["owner_id"].startswith(("PRD-", "CAR-"))}
    if not owners:
        return
    try:
        with app.lock:
            catalog = app.production.public()
        for kind, key in (("job", "jobs"), ("carousel", "carousels")):
            for item in catalog.get(key, []):
                if item.get("id") not in owners:
                    continue
                try:
                    if kind == "carousel":
                        app.carousel_images_live(item)
                    else:
                        app._production_edit_state(item)
                    with app.lock:
                        if app.production.current_revision() == catalog["revision"]:
                            settle_resolved_activity(app, kind, item)
                        else:
                            activity.project_attention(item["id"], {})
                except Exception:
                    activity.project_attention(item["id"], {})
    except Exception:
        for owner in owners:
            activity.project_attention(owner, {})


def reconcile_activity(app):
    """Reconcile only runs that were observed before; never backfill old history."""
    activity = getattr(app, "activity", None)
    if activity is None:
        return
    rows = activity.existing()
    if not rows:
        return
    with app.lock:
        try:
            catalog = app.production.read()
            owners = {item["id"]:item for key in ("jobs","carousels") for item in catalog.get(key,[])}
        except Exception:
            owners = None
        try:
            content_marks = app.content_management.read().get("items", {})
            for row in rows:
                content_kind = "task" if row["kind"] == "ai" else "carousel" if row["owner_id"].startswith("CAR-") else "video"
                mark = content_marks.get(f"{content_kind}:{row['owner_id']}") or {}
                activity.archive_owner(row["owner_id"], mark.get("archived", False), source="content")
        except Exception:
            pass  # Missing/invalid archive evidence is never a reason to close attention.
        operations = {}
        operation_root = app.runtime / "production" / "operations"
        if any(row["kind"] in {"voice","timeline"} and (row["status"] in ACTIVE or row["reconcile_pending"]) for row in rows):
            for path in operation_root.glob("*.json"):
                try:
                    operation = json.loads(path.read_text(encoding="utf-8"))
                    operations[(operation.get("job_id"),operation.get("kind"),operation.get("request_id"))] = operation
                except (OSError,ValueError):
                    continue
        for row in rows:
            try:
                source = None
                recovered_fields = {}
                autopilot = row["kind"] == "images" and row["run_id"] == "autopilot:" + row["source_id"]
                if row["kind"] == "ai":
                    if row["owner_id"] not in app.ai._locations:
                        activity.delete_owner(row["owner_id"])
                        continue
                    task = app.ai._read(row["owner_id"])
                    activity.archive_owner(row["owner_id"], task.get("archived", False))
                    from content_naming import task_display_metadata
                    label = {"variants": "AI 写稿", "review": "AI 审稿", "retrospective": "AI 复盘"}.get(task.get("action"), "AI 协作")
                    round_index = next((index for index, turn in enumerate(task.get("rounds", []), 1) if turn.get("round_id") == row["source_id"]), None)
                    display = task_display_metadata(task)
                    task_label = display["display_title"] + (f" · {display['task_started_label']}" if display.get("task_started_label") else "")
                    title = f"{label} · {task_label}" + (f" · 第 {round_index} 轮" if round_index else "")
                    activity.retitle(row["run_id"], title)
                    source = next((turn for turn in task["rounds"] if turn["round_id"]==row["source_id"]),None)
                elif row["kind"] == "table":
                    # A table belongs to a script rather than a production job; its saved folder is the record.
                    state = app.production.root / "carousel_tables" / row["source_id"] / "state.json"
                    if not state.is_file():
                        activity.delete_run(row["run_id"])
                        continue
                    source = json.loads(state.read_text(encoding="utf-8"))
                elif row["kind"] in {"data", "case"}:
                    source = None
                elif owners is not None:
                    owner = owners.get(row["owner_id"])
                    if owner is None:
                        activity.delete_owner(row["owner_id"])
                        continue
                    activity.archive_owner(row["owner_id"], owner.get("archived", False))
                    # Runs recorded under an earlier name of the order (generic 第 N 条, its title, an older timeline) take its current one.
                    prefix, separator, named = row["title"].partition(" · ")
                    earlier = {str(owner.get("title") or owner["id"]), _plain_label(owner)} | {str(item.get("name") or "").strip() for item in owner.get("timelines") or [] if isinstance(item, dict)}
                    earlier.add(production_title({**owner, "timelines": []}))
                    earlier.update(production_title({**owner, "timelines": [item]}) for item in owner.get("timelines") or [] if isinstance(item, dict))
                    if separator and named in earlier and named != activity_label(owner):
                        activity.retitle(row["run_id"], f"{prefix} · {activity_label(owner)}")
                    collection={"edit":"edits","validation":"edits","stills":"searches","images":"generations","group_review":"group_reviews","voice":"takes","timeline":"timelines","package":"deliveries"}.get(row["kind"])
                    if autopilot:
                        collection = "autopilot_runs"
                    candidates = owner.get(collection,[]) if collection else []
                    if row["kind"] == "image_review":
                        candidates = [review for slide in owner.get("slides",[]) for output in slide.get("outputs",[]) for review in output.get("review_runs",[])]
                    source = next((item for item in candidates if (item.get("id") or item.get("delivery_id") or item.get("unique_id"))==row["source_id"]),None)
                    if row["kind"] in {"validation","package"}:
                        request_id=row["run_id"].split(":",2)[-1]
                        saved_request=catalog.get("requests",{}).get(request_id)
                        source=(saved_request.get("result",{}).get("edit" if row["kind"]=="validation" else "delivery") if saved_request else None)
                        if source and row["kind"]=="validation" and saved_request["result"].get("approved"):
                            source={**source,"status":"approved"}
                if row["status"] not in ACTIVE and not row["reconcile_pending"]:
                    continue
                pending=False
                status = (source or {}).get("status", "interrupted")
                message = "服务已重启，本次处理已中断；请打开原任务核对。"
                if row["kind"] in {"voice","timeline"}:
                    kind = "take" if row["kind"]=="voice" else "timeline"
                    request_id = row["run_id"].split(":",2)[-1]
                    operation=operations.get((row["owner_id"],kind,request_id))
                    if operation and operation.get("state")=="committed":
                        result=operation.get("result") or source or {}
                        attention=(result.get("quality",{}).get("status")!="passed" if row["kind"]=="voice" else
                                   bool(result.get("warnings")) or result.get("saved") is not True)
                        status="needs_attention" if attention else "completed"
                        message="配音已保存，意群对齐待核对。" if row["kind"]=="voice" else "时间线已建立，待核对工程。"
                        recovered_fields={"source_id":operation.get("take_id") or result.get("unique_id") or row["source_id"],"result_count":1}
                    elif operation and operation.get("state")=="safe_failed":
                        status="failed"
                    else:
                        status="needs_attention"
                        message="服务已重启，请在原制作单核对已取得的结果并恢复登记。"
                        pending=True
                    if row["kind"]=="voice" and (source or {}).get("quality",{}).get("status")=="failed":
                        status="needs_attention"
                        message="配音已保存，但意群对齐未通过，请试听核对。"
                elif source and row["kind"]=="package":
                    status="completed"
                elif source and status=="completed" and row["kind"] in {"stills","images","image_review","edit"} and not autopilot:
                    status="needs_attention"
                    message={"stills":"候选底图已保存，待逐张选用。","images":"本次成图已保存，待逐张人工确认。",
                             "image_review":"图片核对已结束，待人工核对结果。","edit":"剪辑已结束，请核对工程核验与人工审查状态。"}[row["kind"]]
                elif source and status=="approved" and row["kind"]=="validation":
                    status="completed"
                if (source or {}).get("result_pending"):
                    status="needs_attention"
                    pending=True
                    message="运行已结束，结果资料仍待恢复，请打开原任务核对。"
                if status in {"awaiting_review","awaiting_first_approval","needs_material","needs_verification","needs_repair","verification_failed","result_pending"}:
                    message={"awaiting_review":"剪辑已结束，待人工审查。","awaiting_first_approval":"首张已生成，待确认。"}.get(status,"本次运行已结束，仍有待处理事项，请打开原任务核对。")
                    status="needs_attention"
                elif status not in TERMINAL:
                    status="interrupted"
                if status=="completed":
                    message="已核对本次保存结果，处理完成。"
                elif status=="failed":
                    message="本次处理未完成，请打开原任务查看原因及恢复方式。"
                elif status=="cancelled":
                    message="本次处理已取消。"
                if row["kind"]=="case":
                    message="工作台重启时自动下载与拆解还没做完，请打开案例点「重试」。"
                if row["kind"]=="data":
                    # Also reached after a restore: the archive was written while this record still said running.
                    message="服务重启或数据恢复时这次处理已不在运行；结果以设置中的备份记录为准。"
                phase = "reconciled"
                if autopilot and source and source.get("status") in TERMINAL:
                    phase = source.get("phase") or phase
                    message = source.get("message") or message
                    completed = source.get("completed_slides")
                    if isinstance(completed, list):
                        recovered_fields["result_count"] = len(completed)
                record_activity(app,row["run_id"],status=status,phase=phase,message=message,reconcile_pending=pending,
                                event_key=f"recovery-reconciled:{row['version']}" if row["reconcile_pending"] and not pending else None,
                                **recovered_fields)
            except Exception:
                # A corrupt/unavailable business record is not proof of completion.
                continue
    refresh_activity_projection(app)
