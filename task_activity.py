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

ACTIVE = frozenset({"queued", "running", "stopping"})
TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted", "needs_attention"})
KINDS = frozenset({"ai", "voice", "timeline", "edit", "validation", "stills", "images", "image_review", "package"})
RECOVERY_PHASES = frozenset({"save", "结果待恢复", "登记待恢复", "记录待恢复", "待核实外部结果", "结果已保存，正在登记"})
IDENTIFIER = re.compile(r"[A-Za-z0-9_:.\-]{1,300}")
UNAVAILABLE = "运行日志暂时不可用，任务仍按原流程处理。"


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
        return f"#/{product}/production/{owner}?" + urlencode({"step": run["step"], "run": run["source_id"]})

    @classmethod
    def _public(cls, row):
        result = dict(row)
        result.pop("reconcile_pending", None)
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
            row = dict(previous) if previous else dict(run_id=run_id, kind="", owner_id="", source_id="", product="", title="", case_id="", status="queued", phase="", message="", step="", started_at=at, updated_at=at, finished_at=None, result_count=None, version=0, event_id=None, reconcile_pending=0)
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

    def feed(self, *, after=None, limit=50, before=None, product=None, filter="all", owner_id=None, source_id=None):
        limit = _number(limit,50,200)
        after = _number(after) if after is not None else None
        before = _number(before) if before is not None else None
        if filter not in {"all", "active", "unread"}:
            raise ActivityError("运行记录筛选无效。")
        try:
            with self.lock, self._connect() as db:
                maximum = db.execute("SELECT COALESCE(MAX(id),0) FROM changes").fetchone()[0]
                epoch = db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
                counts = {"active": db.execute("SELECT COUNT(*) FROM runs WHERE status IN ('queued','running','stopping')").fetchone()[0],
                          "unread": db.execute("SELECT COUNT(*) FROM notifications n JOIN runs r ON r.event_id=n.event_id WHERE n.read=0").fetchone()[0]}
                where, values = ["1"], []
                if product is not None and product:
                    where.append("r.product=?"); values.append(product_key(product))
                if owner_id:
                    where.append("r.owner_id=?"); values.append(owner_id)
                if source_id:
                    where.append("r.source_id=?"); values.append(source_id)
                if filter == "active":
                    where.append("r.status IN ('queued','running','stopping')")
                elif filter == "unread":
                    where.append("n.read=0")
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
                    for row in self._rows(db, "n.event_id>? AND n.event_id<=? AND n.read=0 AND r.status!='cancelled'", (after,cursor), "ORDER BY n.event_id"):
                        public = self._public(row)
                        events.append({key:public[key] for key in ("event_id","run_id","status","message","title","product","url")} | {"created_at":public["finished_at"]})
                else:
                    if before is not None:
                        where.append("r.version<?"); values.append(before)
                    rows = self._rows(db, " AND ".join(where), (*values,limit+1), "ORDER BY r.version DESC LIMIT ?")
                    has_more = len(rows) > limit
                    rows = rows[:limit]
                return {"available":True,"items":[self._public(row) for row in rows],"events":events,"removed":removed,"cursor":cursor,"epoch":epoch,"has_more":has_more,"next_before":rows[-1]["version"] if rows else None,"counts":counts}
        except (OSError, sqlite3.Error, ActivityError):
            self.error = UNAVAILABLE
            return {"available":False,"error":UNAVAILABLE,"items":[],"events":[],"removed":[],"counts":{"active":0,"unread":0},"cursor":0,"epoch":"","has_more":False,"next_before":None}

    def logs(self, run_id, *, after_seq=0, limit=100):
        after_seq, limit = _number(after_seq), _number(limit,100,200)
        try:
            with self.lock, self._connect() as db:
                runs = self._rows(db,"r.run_id=?",(run_id,))
                if not runs:
                    raise ActivityError("此前未记录运行日志，或所属任务已删除。",404)
                rows = db.execute("SELECT seq,at,status,phase,message,result_count FROM logs WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",(run_id,after_seq,limit+1)).fetchall()
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
                    valid=db.execute("SELECT 1 FROM notifications n JOIN runs r ON r.event_id=n.event_id WHERE n.event_id=? AND n.read=0 AND r.status!='cancelled'",(event_id,)).fetchone()
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
                if row["kind"] == "ai":
                    if row["owner_id"] not in app.ai._locations:
                        activity.delete_owner(row["owner_id"])
                        continue
                    task = app.ai._read(row["owner_id"])
                    source = next((turn for turn in task["rounds"] if turn["round_id"]==row["source_id"]),None)
                elif owners is not None:
                    owner = owners.get(row["owner_id"])
                    if owner is None:
                        activity.delete_owner(row["owner_id"])
                        continue
                    collection={"edit":"edits","validation":"edits","stills":"searches","images":"generations","voice":"takes","timeline":"timelines","package":"deliveries"}.get(row["kind"])
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
                elif source and status=="completed" and row["kind"] in {"stills","images","image_review","edit"}:
                    status="needs_attention"
                    message={"stills":"候选底图已保存，待逐张选用。","images":"本次成图已保存，待逐张人工确认。",
                             "image_review":"图片核对已结束，待人工核对结果。","edit":"剪辑已结束，请核对工程核验与人工审查状态。"}[row["kind"]]
                elif source and status=="approved" and row["kind"]=="validation":
                    status="completed"
                if (source or {}).get("result_pending"):
                    status="needs_attention"
                    pending=True
                    message="运行已结束，结果资料仍待恢复，请打开原任务核对。"
                if status in {"awaiting_review","awaiting_first_approval","needs_material","needs_repair","verification_failed","result_pending"}:
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
                record_activity(app,row["run_id"],status=status,phase="reconciled",message=message,reconcile_pending=pending,
                                event_key=f"recovery-reconciled:{row['version']}" if row["reconcile_pending"] and not pending else None,
                                **recovered_fields)
            except Exception:
                # A corrupt/unavailable business record is not proof of completion.
                continue
