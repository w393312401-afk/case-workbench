"""Each creator's material library for the away-mode execution pool: every material timeline of the creator's
project, so the user ticks footage instead of typing names.

Known names come from earlier picks and build records; DaVinci adds the ones never picked, but only when the
creator's project is the one open and no other workbench operation holds DaVinci (nothing is opened or switched).
What DaVinci listed is kept in the runtime folder (away-material-library.json) for when it is closed or busy.
The latest cut's picks are what an order starts with ticked."""
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path


SKIPPED = re.compile(r"备份|草稿|backup|draft|__WB_", re.I)
BANNED = "禁用"
HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")  # same rule as the order page: material timelines have Chinese names


def is_footage(name):
    """A listable material timeline: a Chinese name (finished cuts such as diamond261110-202-1 have none), not banned or retired."""
    return bool(HAN.search(name)) and BANNED not in name and RETIRED not in name


RETIRED = "成品素材"  # the old footage series: the user dropped it on 2026-11-19, it is never listed or ticked
MAX_EXAMPLES, MAX_OPTIONS = 6, 60


def creator_project_base(project, creator):
    """A creator's copy of a product project may carry the creator as a prefix (Zack-202H); same as workbench_production."""
    name, prefix = str(project or ""), str(creator or "") + "-"
    return name[len(prefix):] if creator and name.casefold().startswith(prefix.casefold()) else name


def _day(value):
    return str(value or "")[:10]


def project_key(project, creator):
    return creator_project_base(project, creator).casefold(), str(creator or "").casefold()


def job_place(data, job, creator=None):
    """The creator, DaVinci project name and project id an order is (or will be) cut in."""
    timelines = [item for item in job.get("timelines") or [] if isinstance(item, dict)]
    last = timelines[-1] if timelines else {}
    creator = creator or last.get("creator") or job.get("creator") or ""
    settings = ((data.get("settings") or {}).get("products") or {}).get(job.get("product")) or {}
    project = last.get("project") if last and last.get("creator") == creator else ""
    project = project or settings.get("project") or str(job.get("product") or "")
    project_id = last.get("project_id") if last and last.get("creator") == creator else ""
    if not project_id:
        key = project_key(project, creator)
        built = [item for other in data.get("jobs", []) for item in other.get("timelines") or []
                 if isinstance(item, dict) and item.get("project_id") and project_key(item.get("project"), item.get("creator")) == key]
        project_id = max(built, key=lambda item: item.get("built_at", ""), default={}).get("project_id", "")
    return creator, project, project_id


SHOT_TYPES = (("须实拍", "proof"), ("动作", "act"), ("展示", "show"))


def script_lines(job):
    """Each voiceover line with its storyboard: {say, types: [{label, kind}], shot}. kind is proof / act / show / other."""
    rows = []
    for line in job.get("lines") or []:
        if not isinstance(line, dict):
            continue
        text = str(line.get("zh") or line.get("spoken") or "").strip()
        if not text:
            continue
        evidence = str(line.get("evidence") or "").strip().replace("(", "（").replace(")", "）")
        shot = str(line.get("proof") or "").strip()
        if re.match(r"^照原片", evidence):
            evidence = ""  # a replica's shot references say nothing about the knife
        if re.match(r"^照原片", shot):
            shot = ""
        types = []
        for label in [part.strip() for part in re.split(r"\s*\+\s*", evidence) if part.strip()]:
            kind = next((kind for word, kind in SHOT_TYPES if word in label), "other")
            types.append({"label": label.replace("结果证据（须实拍）", "须实拍")[:20], "kind": kind})
        rows.append({"say": text, "types": types, "shot": shot[:160]})
    return rows


def history(data, key, skip_job=None):
    """Every pick and build record for this creator's project: per name its use count and dates, and the latest picks."""
    names, picks = {}, []
    for job in data.get("jobs", []):
        by_uid = {item.get("unique_id"): item for item in job.get("timelines") or [] if isinstance(item, dict)}
        for item in by_uid.values():
            if project_key(item.get("project"), item.get("creator")) == key:
                for name in item.get("materials") or []:
                    if isinstance(name, str) and name and RETIRED not in name:
                        row = names.setdefault(name, {"used": 0, "first": "", "last": ""})
                        row["first"] = min(filter(None, (row["first"], _day(item.get("built_at")))), default="")
        for edit in job.get("edits") or []:
            timeline = by_uid.get(edit.get("timeline_uid")) if isinstance(edit, dict) else None
            chosen = [name for name in (edit.get("materials") or []) if isinstance(name, str) and name and RETIRED not in name] if timeline else []
            if not chosen or project_key(timeline.get("project"), timeline.get("creator")) != key:
                continue
            at = str(edit.get("started_at") or edit.get("created_at") or "")
            for name in chosen:
                row = names.setdefault(name, {"used": 0, "first": _day(at), "last": ""})
                row["used"] += 1
                row["first"] = min(filter(None, (row["first"], _day(at))), default="")
                row["last"] = max(row["last"], _day(at))
            if job.get("id") != skip_job:
                picks.append({"at": at, "job": job.get("id"), "lines": [line["say"] for line in script_lines(job)[:3]], "materials": chosen})
    picks.sort(key=lambda row: row["at"], reverse=True)
    latest = {}
    for row in picks:  # one example per order: the user's latest pick for it
        latest.setdefault(row["job"], row)
    return names, list(latest.values())[:MAX_EXAMPLES]


def latest_materials(data, job, creator):
    """The material timelines of this creator's latest cut in the order's project; [] when the creator never cut there."""
    creator, project, _ = job_place(data, job, creator)
    examples = history(data, project_key(project, creator))[1]
    return [name for name in examples[0]["materials"] if is_footage(name)] if examples else []


def finished_names(data):
    """Timelines the workbench built or an edit delivered: finished videos, never footage."""
    names = set()
    for job in data.get("jobs", []):
        names.update(str(item.get("name")) for item in job.get("timelines") or [] if isinstance(item, dict) and item.get("name"))
        names.update(str(edit.get("timeline_name")) for edit in job.get("edits") or [] if isinstance(edit, dict) and edit.get("timeline_name"))
    return names


def options(data, job, creator=None, listing=None):
    """The material timelines to choose from, newest footage first, and whether DaVinci's own list was read."""
    creator, project, project_id = job_place(data, job, creator)
    key = project_key(project, creator)
    known, examples = history(data, key, skip_job=job.get("id"))
    rows = {name: {"name": name, **value, "in_davinci": None} for name, value in known.items()}
    listed = bool(listing and project_id and listing.get("project_id") == project_id)
    if listed:
        finished = finished_names(data)
        present = [name for name in listing.get("timelines") or [] if isinstance(name, str) and name]
        for order, name in enumerate(present):
            if name in finished or SKIPPED.search(name) or not is_footage(name):
                continue
            rows.setdefault(name, {"name": name, "used": 0, "first": "", "last": ""}).update(in_davinci=order)
        for row in rows.values():
            row["in_davinci"] = row["in_davinci"] if row["in_davinci"] is not None else False
    found = [row for row in rows.values() if is_footage(row["name"]) and row["in_davinci"] is not False]
    # new in DaVinci first (later in its list = added later), then by the last time each was picked, then old never-picked
    fresh = sorted((row for row in found if not row["used"] and not row["first"]), key=lambda row: -(row["in_davinci"] or 0))
    picked = sorted((row for row in found if row["used"]), key=lambda row: row["last"], reverse=True)
    idle = [row for row in found if not row["used"] and row["first"]]
    found = fresh + picked + idle
    return {"creator": creator, "project": project, "listed": listed, "options": found[:MAX_OPTIONS], "examples": examples}


class MaterialLibrary:
    """Each creator's material timelines, kept in the runtime folder so the dialog can list them while DaVinci is
    closed, busy or showing another creator's project.

    DaVinci's own list replaces the stored one whenever the creator's project is open and idle (footage removed
    there drops out); names picked for an edit after that read are added, since they exist in the project."""

    def __init__(self, app, runtime):
        self.app = app
        self.path = Path(runtime) / "away-material-library.json"
        self.lock = threading.Lock()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        temporary.replace(self.path)

    def listing(self):
        """The open DaVinci project's timeline names, only when that costs nothing: never launches, waits or switches."""
        try:
            if self.app.resolve.busy() or self.app.resolve.closed():
                return None
            return self.app.resolve.run("status", {}, timeout=20, launch=False)
        except Exception:
            return None

    def library(self, data, job, creator, listing=None):
        """{"names": [{name, used, last}], "seen_at": when DaVinci was last read for this project, or ""}."""
        found = options(data, job, creator, listing)
        creator, project, _ = job_place(data, job, creator)
        key = "|".join(project_key(project, creator))
        history = {row["name"]: row for row in found["options"]}
        with self.lock:
            stored = self._load()
            if found["listed"]:
                read = [row["name"] for row in sorted((row for row in found["options"] if isinstance(row.get("in_davinci"), int) and row["in_davinci"] is not False),
                                                      key=lambda row: row["in_davinci"])]
                stored[key] = {"names": read, "seen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                               "creator": creator, "project": project}
                self._save(stored)
            snapshot = stored.get(key) or {}
        seen = str(snapshot.get("seen_at") or "")
        names = [name for name in snapshot.get("names") or [] if is_footage(name)]
        for name, row in history.items():  # picked since that read, or never read at all: it is in the project
            if name not in names and (not seen or row["last"] >= seen[:10]):
                names.append(name)
        rows = [{"name": name, "used": (history.get(name) or {}).get("used", 0), "last": (history.get(name) or {}).get("last", "")}
                for name in names]
        return {"names": rows, "seen_at": seen}
