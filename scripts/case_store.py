#!/usr/bin/env python3
"""Append-only local review cases. Standard library only; JSON in, JSON out."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from datetime import date, datetime, timezone
from uuid import uuid4

CASE_RE = re.compile(r"PVR-[0-9a-f]{12}\Z")
KINDS = {"preference", "fact", "editorial", "performance", "review_correction"}
POLARITIES = {"positive", "negative", "mixed", "unknown"}
STATUSES = {"unverified", "user_reported", "source_checked"}
INDEX_SCHEMA_VERSION = 4
CONTENT_ORIGIN_LABELS = {"self_produced": "自制内容参考", "overseas_creator": "达人内容参考"}
# Explicit aliases already used for this product. Do not infer a product from
# a SKU mentioned in an accessory/compatibility label or from a case title.
DIRECTION_PRODUCT_ALIASES = {
    "202h": "SHARPAL 202H",
    "sharpal 202h": "SHARPAL 202H",
    "sharpal 202h 手动定角磨刀系统": "SHARPAL 202H",
    "sharpal 202h precision knife sharpening system": "SHARPAL 202H",
}


def direction_product_group(product):
    if not isinstance(product, str):
        return product
    key = re.sub(r"\s+", " ", product.strip()).casefold()
    return DIRECTION_PRODUCT_ALIASES.get(key, product)


def direction_lead_code(direction_id):
    """Lead selling point at the end of a direction id, e.g. 202H-S1 -> S1; None when the id has no S/A/B code."""
    match = re.search(r"-([SAB][0-9]+)$", direction_id or "")
    return match.group(1) if match else None


CURRENT_FIELDS = {"content_origin", "title", "category", "conclusion", "purchase_reason", "kind", "assessments", "metrics", "evidence", "assets", "transcript", "translation_zh", "highlights", "limitations", "history_note", "source_event_ids", "source_case", "native_reference", "test_direction", "production", "publish"}
METRIC_TEXT_FIELDS = {"video_id", "creator_handle", "published_title", "views_display", "product_impressions_display", "product_clicks_display", "ctor_display"}
ASSET_FIELDS = {"video", "screenshot", "transcript", "source_manifest", "contact_sheet"}
SOURCE_CASE_FIELDS = {"library_id", "file_path", "tier", "transcript_status", "case_type"}
CASE_TYPES = {"video_analysis", "structure_template", "traffic_reference"}
TEST_DIRECTION_REQUIRED = {"direction_id", "main_difference", "concern", "closing", "variant_type"}
TEST_DIRECTION_FIELDS = TEST_DIRECTION_REQUIRED | {"execution_change", "script_version"}
VARIANT_TYPES = {"direction", "execution"}
PRODUCTION_FIELDS = {"script_version", "davinci_project", "davinci_timeline", "voiceover", "source_ledger"}
PERFORMANCE_FIELDS = {"views_display", "product_impressions_display", "product_clicks_display", "items_sold", "ctor_display"}


class StoreError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise StoreError(message)


def obj(value, allowed, required=(), label="object"):
    require(isinstance(value, dict), f"{label} must be a JSON object")
    require(not (set(value) - set(allowed)), f"{label}: unknown fields {sorted(set(value) - set(allowed))}")
    require(not (set(required) - set(value)), f"{label}: missing fields {sorted(set(required) - set(value))}")


def string(value, label):
    require(isinstance(value, str) and bool(value.strip()), f"{label} must be a non-empty string")


def iso_date(value, label):
    string(value, label)
    require(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value), f"{label} must use YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise StoreError(f"{label} must be a valid date") from exc


def case_id(value):
    require(isinstance(value, str) and CASE_RE.fullmatch(value), "invalid case ID; expected PVR- followed by 12 lowercase hex digits")
    return value


def feedback_schema(value):
    obj(value, {"kind", "text", "assessments", "analysis", "scope", "evidence"}, {"kind", "text"}, "feedback")
    require(isinstance(value["kind"], str) and value["kind"] in KINDS, "feedback.kind is invalid")
    string(value["text"], "feedback.text")
    for field in ("analysis", "scope"):
        if field in value:
            string(value[field], "feedback." + field)
    assessments_schema(value.get("assessments", []), "feedback.assessments")
    if "evidence" in value:
        evidence = value["evidence"]
        obj(evidence, {"status", "source", "window", "metrics", "context"}, {"status"}, "evidence")
        require(isinstance(evidence["status"], str) and evidence["status"] in STATUSES, "evidence.status is invalid")
        for field in ("source", "window", "context"):
            if field in evidence:
                string(evidence[field], "evidence." + field)
        if "metrics" in evidence:
            require(isinstance(evidence["metrics"], dict), "evidence.metrics must be an object; preserve supplied values and units")


def assessments_schema(assessments, label):
    require(isinstance(assessments, list), f"{label} must be a list")
    for assessment in assessments:
        obj(assessment, {"dimension", "polarity", "reason"}, {"dimension", "polarity"}, "assessment")
        string(assessment["dimension"], "assessment.dimension")
        require(isinstance(assessment["polarity"], str) and assessment["polarity"] in POLARITIES, "assessment.polarity is invalid")
        if "reason" in assessment:
            string(assessment["reason"], "assessment.reason")


def native_reference_schema(value):
    """Scene-bound expression references live in the same current snapshot."""
    fields = {"language", "market", "duration_sec", "scene", "speaker", "audience", "entries"}
    obj(value, fields, fields, "current.native_reference")
    for field in fields - {"duration_sec", "entries"}:
        string(value[field], "native_reference." + field)
    duration = value["duration_sec"]
    require(type(duration) in (int, float) and math.isfinite(duration) and duration > 0,
            "native_reference.duration_sec must be a positive finite number")
    entries = value["entries"]
    require(isinstance(entries, list) and 1 <= len(entries) <= 50, "native_reference.entries requires 1–50 entries")
    ids = set()
    text_fields = {"id", "channel", "original", "meaning_zh", "function", "context", "analysis", "adaptation", "boundary"}
    for entry in entries:
        obj(entry, text_fields | {"start_sec", "end_sec"}, text_fields | {"start_sec", "end_sec"}, "native_reference.entry")
        for field in text_fields:
            string(entry[field], "native_reference.entry." + field)
        require(entry["id"] not in ids, "native_reference entry IDs must be unique")
        ids.add(entry["id"])
        require(entry["channel"] in {"speech", "on_screen"}, "native_reference.channel must be speech or on_screen")
        for field in ("start_sec", "end_sec"):
            require(type(entry[field]) in (int, float) and math.isfinite(entry[field]), "native_reference timestamps must be finite numbers")
        require(0 <= entry["start_sec"] < entry["end_sec"] <= duration, "native_reference timestamps must be ordered and within the video")


def test_direction_schema(value):
    """Keep the user-supplied test classification bound to its current snapshot."""
    obj(value, TEST_DIRECTION_FIELDS, TEST_DIRECTION_REQUIRED, "current.test_direction")
    for field, data in value.items():
        string(data, "current.test_direction." + field)
    require(value["variant_type"] in VARIANT_TYPES, "current.test_direction.variant_type must be direction or execution")


def current_schema(value):
    """Validate an explicit complete snapshot; never infer one from feedback."""
    obj(value, CURRENT_FIELDS, {"title", "category", "conclusion", "kind", "assessments"}, "current")
    for field in ("title", "category", "conclusion", "purchase_reason", "history_note"):
        if field in value:
            string(value[field], "current." + field)
    require(isinstance(value["kind"], str) and value["kind"] in KINDS, "current.kind is invalid")
    assessments_schema(value["assessments"], "current.assessments")
    if "content_origin" in value:
        require(isinstance(value["content_origin"], str) and value["content_origin"] in {"self_produced", "overseas_creator"}, "current.content_origin is invalid")
    if "native_reference" in value:
        native_reference_schema(value["native_reference"])
    if "test_direction" in value:
        test_direction_schema(value["test_direction"])
    if "production" in value:
        obj(value["production"], PRODUCTION_FIELDS, label="current.production")
        require(bool(value["production"]), "current.production must contain at least one field")
        for field, data in value["production"].items():
            string(data, "current.production." + field)
    if "publish" in value:
        obj(value["publish"], {"published_on"}, {"published_on"}, "current.publish")
        iso_date(value["publish"]["published_on"], "current.publish.published_on")
    if "metrics" in value:
        metrics = value["metrics"]
        obj(metrics, METRIC_TEXT_FIELDS | {"items_sold", "ticket_value"}, label="current.metrics")
        for field in METRIC_TEXT_FIELDS & set(metrics):
            string(metrics[field], "current.metrics." + field)
        if "items_sold" in metrics:
            require(type(metrics["items_sold"]) is int and metrics["items_sold"] >= 0, "current.metrics.items_sold must be a non-negative integer")
        if "ticket_value" in metrics:
            ticket = metrics["ticket_value"]
            if isinstance(ticket, str):
                string(ticket, "current.metrics.ticket_value")
            else:
                require(type(ticket) in (int, float) and math.isfinite(ticket) and ticket >= 0, "current.metrics.ticket_value must be a non-negative finite number or non-empty string")
    if "evidence" in value:
        evidence = value["evidence"]
        obj(evidence, {"status", "source", "window", "context", "as_of"}, {"status"}, "current.evidence")
        require(isinstance(evidence["status"], str) and evidence["status"] in STATUSES, "current.evidence.status is invalid")
        for field in ("source", "window", "context"):
            if field in evidence:
                string(evidence[field], "current.evidence." + field)
        if "as_of" in evidence:
            iso_date(evidence["as_of"], "current.evidence.as_of")
            if "publish" in value:
                require(evidence["as_of"] >= value["publish"]["published_on"], "current.evidence.as_of cannot precede the publish date")
    if "assets" in value:
        obj(value["assets"], ASSET_FIELDS, label="current.assets")
        for field, path in value["assets"].items():
            string(path, "current.assets." + field)
            require(Path(path).is_absolute() and not path.startswith(("\\\\", "//")), f"current.assets.{field} must be an absolute local filesystem path")
    if "transcript" in value:
        obj(value["transcript"], {"text", "status", "source"}, {"text", "status", "source"}, "current.transcript")
        for field, text in value["transcript"].items():
            string(text, "current.transcript." + field)
    if "translation_zh" in value:
        translation = value["translation_zh"]
        obj(translation, {"source_text", "text", "channel"}, {"source_text", "text", "channel"}, "current.translation_zh")
        for field in ("source_text", "text"):
            string(translation[field], "current.translation_zh." + field)
        require(translation["channel"] in {"transcript", "on_screen", "historical_excerpt"},
                "current.translation_zh.channel is invalid")
        if translation["channel"] == "transcript":
            require("transcript" in value and translation["source_text"] == value["transcript"]["text"],
                    "current.translation_zh.source_text must match the current transcript")
    for field in ("highlights", "limitations", "source_event_ids"):
        if field in value:
            require(isinstance(value[field], list), f"current.{field} must be a list")
            for entry in value[field]:
                string(entry, "current." + field)
    if "source_event_ids" in value:
        require(len(set(value["source_event_ids"])) == len(value["source_event_ids"]), "current.source_event_ids contains duplicates")
    if "source_case" in value:
        source = value["source_case"]
        obj(source, SOURCE_CASE_FIELDS, SOURCE_CASE_FIELDS, "current.source_case")
        for field, data in source.items():
            string(data, "current.source_case." + field)
        require(source["case_type"] in CASE_TYPES, "current.source_case.case_type is invalid")
        path = source["file_path"]
        require(Path(path).is_absolute() and not path.startswith(("\\\\", "//")) and Path(path).suffix.lower() == ".md", "current.source_case.file_path must be an absolute local Markdown path")


def current_sources(value, known_events):
    require(set(value.get("source_event_ids", [])) <= set(known_events), "current.source_event_ids must reference existing earlier events in the same case")


def add_schema(value):
    obj(value, {"title", "product", "audience", "original", "feedback", "related_case_ids", "current"}, {"title", "original", "feedback"}, "case")
    string(value["title"], "title")
    for field in ("product", "audience"):
        if field in value:
            string(value[field], field)
    original = value["original"]
    obj(original, {"text", "url", "source"}, label="original")
    require("text" in original or "url" in original, "original requires text or url")
    for field, data in original.items():
        string(data, "original." + field)
    feedback_schema(value["feedback"])
    if "current" in value:
        current_schema(value["current"])
    related = value.get("related_case_ids", [])
    require(isinstance(related, list), "related_case_ids must be a list")
    for related_id in related:
        case_id(related_id)
    require(len(set(related)) == len(related), "related_case_ids contains duplicates")


def append_schema(value):
    require(isinstance(value, dict), "append input must be an object")
    feedback = {key: data for key, data in value.items() if key not in {"draft_version", "current", "lifecycle"}}
    if feedback or "lifecycle" not in value:
        feedback_schema(feedback)
    if "lifecycle" in value:
        lifecycle_schema(value["lifecycle"])
    if "current" in value:
        current_schema(value["current"])
    if "draft_version" in value:
        obj(value["draft_version"], {"label", "text"}, {"label", "text"}, "draft_version")
        for key, data in value["draft_version"].items():
            string(data, "draft_version." + key)


def lifecycle_schema(value):
    obj(value, {"archived"}, {"archived"}, "lifecycle")
    require(type(value["archived"]) is bool, "lifecycle.archived must be a boolean")


def lifecycle_feedback(archived, reason=None):
    action = "归档案例" if archived else "恢复案例"
    return {"kind": "editorial", "text": action + (f"：{reason}" if reason else "。")}


def encode(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def load_json(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def bad_constant(value):
        raise StoreError(f"invalid JSON numeric constant: {value}")

    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=unique_pairs, parse_constant=bad_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StoreError(f"cannot read JSON {path}: {exc}") from exc


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


class CaseStore:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def safe(self, *parts):
        path = self.root.joinpath(*parts)
        require(path.resolve().is_relative_to(self.root), f"path escapes store root: {path}")
        # Reject links even when they resolve inside the store: immutable records
        # must not alias other records.
        cursor = path
        while cursor != self.root:
            require(not cursor.is_symlink(), f"symlinks are not allowed in case store: {cursor}")
            require(not getattr(cursor, "is_junction", lambda: False)(), f"junctions are not allowed in case store: {cursor}")
            cursor = cursor.parent
        return path

    def folder(self, identifier):
        return self.safe("cases", case_id(identifier))

    def exclusive_write(self, path, value):
        data = encode(value)
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    def read_case(self, identifier):
        folder = self.folder(identifier)
        require(folder.is_dir(), f"case does not exist: {identifier}")
        original = load_json(self.safe("cases", identifier, "original.json"))
        obj(original, {"case_id", "created_at", "title", "product", "audience", "original", "related_case_ids"}, {"case_id", "created_at", "title", "original"}, "stored original")
        require(original["case_id"] == identifier, "stored case ID does not match directory")
        string(original["created_at"], "created_at")
        # Reuse add validation without adding an inferred feedback event.
        add_schema({**{key: data for key, data in original.items() if key not in {"case_id", "created_at"}}, "feedback": {"kind": "editorial", "text": "schema validation"}})
        events_path = self.safe("cases", identifier, "events")
        require(events_path.is_dir(), f"missing events directory: {identifier}")
        events = []
        for path in sorted(events_path.iterdir()):
            require(path.suffix == ".json" and path.is_file(), f"unexpected events entry: {path.name}")
            path = self.safe("cases", identifier, "events", path.name)
            event = load_json(path)
            obj(event, {"event_id", "created_at", "feedback", "draft_version", "current", "lifecycle"}, {"event_id", "created_at", "feedback"}, "stored event")
            require(event["event_id"] == path.stem, f"event ID does not match filename: {path}")
            string(event["created_at"], "event.created_at")
            feedback_schema(event["feedback"])
            append_schema({**event["feedback"], **{key: event[key] for key in ("draft_version", "current", "lifecycle") if key in event}})
            if "current" in event:
                current_sources(event["current"], [earlier["event_id"] for earlier in events])
            events.append(event)
        require(events, f"case has no feedback events: {identifier}")
        snapshot_event = next((event for event in reversed(events) if "current" in event), None)
        archive_event = next((event for event in reversed(events) if "lifecycle" in event), None)
        return {**original, "events": events,
                "current": snapshot_event["current"] if snapshot_event else None,
                "current_event_id": snapshot_event["event_id"] if snapshot_event else None,
                "current_state": "present" if snapshot_event else "missing",
                "archived": archive_event["lifecycle"]["archived"] if archive_event else False,
                "archive_event_id": archive_event["event_id"] if archive_event else None}

    def build_index(self):
        cases_path = self.safe("cases")
        cases = []
        if cases_path.exists():
            for path in sorted(cases_path.iterdir()):
                if path.is_dir():
                    cases.append(self.read_case(case_id(path.name)))
                elif path.name != "index.json":
                    require(path.name.startswith(".index-") and path.suffix == ".tmp", f"unexpected case store entry: {path.name}")
        known = {item["case_id"] for item in cases}
        entries = []
        for item in cases:
            for related in item.get("related_case_ids", []):
                require(related in known, f"{item['case_id']}: related case does not exist: {related}")
                require(related != item["case_id"], "case cannot reference itself")
            feedbacks = [event["feedback"] for event in item["events"]]
            assessments = [assessment for feedback in feedbacks for assessment in feedback.get("assessments", [])]
            current = item["current"]
            identity = {key: item[key] for key in ("case_id", "product", "audience") if key in item}
            # Historical notes and filesystem names are not the content of the
            # current version. Searching old copy belongs to history scope.
            current_search = {**identity, "current": {key: data for key, data in (current or {}).items() if key not in {"history_note", "source_event_ids", "assets"}}}
            if (current or {}).get("content_origin") in CONTENT_ORIGIN_LABELS:
                current_search["content_origin_label"] = CONTENT_ORIGIN_LABELS[current["content_origin"]]
            if "production" in current_search["current"]:
                # Linked file names are provenance, not current creative copy.
                current_search["current"]["production"] = {key: data for key, data in current_search["current"]["production"].items() if key in {"script_version", "davinci_project", "davinci_timeline"}}
            entries.append({
                "case_id": item["case_id"], "title": current["title"] if current else item["title"],
                "original_title": item["title"],
                "product": item.get("product"), "audience": item.get("audience"),
                "related_case_ids": item.get("related_case_ids", []),
                "event_count": len(item["events"]),
                "current": current,
                "test_direction": (current or {}).get("test_direction"),
                "current_event_id": item["current_event_id"],
                "current_state": item["current_state"],
                "archived": item["archived"],
                "archive_event_id": item["archive_event_id"],
                "historical_kinds": sorted({feedback["kind"] for feedback in feedbacks}),
                "historical_dimensions": sorted({assessment["dimension"] for assessment in assessments}),
                "historical_polarities": sorted({assessment["polarity"] for assessment in assessments}),
                "historical_evidence_statuses": sorted({feedback["evidence"]["status"] for feedback in feedbacks if "evidence" in feedback}),
                "search_text": json.dumps(item, ensure_ascii=False).casefold(),
                "current_search_text": json.dumps(current_search, ensure_ascii=False).casefold(),
            })
        return {"schema_version": INDEX_SCHEMA_VERSION, "cases": entries}

    def rebuild_index(self):
        index = self.build_index()  # Validate all records before touching index.
        cases_path = self.safe("cases")
        cases_path.mkdir(parents=True, exist_ok=True)
        destination = self.safe("cases", "index.json")
        descriptor, temporary = tempfile.mkstemp(prefix=".index-", suffix=".tmp", dir=cases_path)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encode(index))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return index

    def read_index(self):
        expected = self.build_index()
        path = self.safe("cases", "index.json")
        if not path.exists():
            require(not expected["cases"], "index missing; run rebuild-index")
            return expected
        try:
            actual = load_json(path)
            require(isinstance(actual, dict) and actual.get("schema_version") == INDEX_SCHEMA_VERSION, f"index schema requires version {INDEX_SCHEMA_VERSION}; migration required")
            require(actual == expected, "index is stale or inconsistent")
        except StoreError as exc:
            raise StoreError(f"{exc}; run rebuild-index") from exc
        return actual

    def event(self, feedback, draft=None, current=None, lifecycle=None):
        timestamp = utc_now()
        event = {"event_id": timestamp + "-" + uuid4().hex, "created_at": timestamp, "feedback": feedback}
        if draft is not None:
            event["draft_version"] = draft
        if current is not None:
            event["current"] = current
        if lifecycle is not None:
            event["lifecycle"] = lifecycle
        return event

    def update_after_write(self, identifier):
        try:
            self.rebuild_index()
        except (OSError, StoreError) as exc:
            raise StoreError(f"case {identifier} was saved, but index update failed: {exc}; run rebuild-index before retrying any add/append") from exc

    def add(self, value, *, identifier=None):
        add_schema(value)
        if "current" in value:
            current_sources(value["current"], [])
        encode(value)
        self.read_index()  # Fail visibly on an already corrupt/stale index.
        for related in value.get("related_case_ids", []):
            self.read_case(related)
        cases_path = self.safe("cases")
        cases_path.mkdir(parents=True, exist_ok=True)
        if identifier is not None:
            folder = self.folder(identifier)
            require(not folder.exists(), f"case already exists: {identifier}")
        else:
            while True:
                identifier = "PVR-" + uuid4().hex[:12]
                folder = self.folder(identifier)
                if not folder.exists():
                    break
        original = {"case_id": identifier, "created_at": utc_now(), **{key: data for key, data in value.items() if key not in {"feedback", "current"}}}
        event = self.event(value["feedback"], current=value.get("current"))
        # A case becomes visible only after BOTH original and first feedback
        # exist. Staging lives outside cases/ so interrupted adds cannot poison
        # index scans. The store is intentionally a single-writer interface.
        staging_base = self.safe(".case-staging")
        staging_base.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=staging_base))
        try:
            self.exclusive_write(self.safe(".case-staging", staging.name, "original.json"), original)
            self.safe(".case-staging", staging.name, "events").mkdir()
            self.exclusive_write(self.safe(".case-staging", staging.name, "events", event["event_id"] + ".json"), event)
            require(not folder.exists(), f"case appeared during add: {identifier}; serialize writers")
            staging.rename(folder)  # Same filesystem; publish complete case.
        finally:
            # After a successful rename this path no longer exists. Never
            # remove the committed folder, including on later index failure.
            cleanup_path = self.safe(".case-staging", staging.name)
            require(cleanup_path.resolve().is_relative_to(staging_base.resolve()) and staging.name.startswith(".pending-"), "unsafe staging cleanup path")
            if cleanup_path.exists():
                shutil.rmtree(cleanup_path)
        self.update_after_write(identifier)
        return self.read_case(identifier)

    def append(self, identifier, value):
        case_id(identifier)
        append_schema(value)
        encode(value)
        self.read_index()
        existing = self.read_case(identifier)
        if "current" in value:
            current_sources(value["current"], [event["event_id"] for event in existing["events"]])
        feedback = {key: data for key, data in value.items() if key not in {"draft_version", "current", "lifecycle"}}
        if not feedback:
            feedback = lifecycle_feedback(value["lifecycle"]["archived"])
        event = self.event(feedback, value.get("draft_version"), value.get("current"), value.get("lifecycle"))
        # Publish only a complete event so interrupted writes cannot make an
        # existing case unreadable. Staging stays outside the case index.
        staging_base = self.safe(".case-staging")
        staging_base.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=staging_base))
        try:
            pending = self.safe(".case-staging", staging.name, "event.json")
            self.exclusive_write(pending, event)
            destination = self.safe("cases", identifier, "events", event["event_id"] + ".json")
            require(not destination.exists(), "event ID collision; existing feedback was not changed")
            pending.rename(destination)
        finally:
            cleanup_path = self.safe(".case-staging", staging.name)
            require(cleanup_path.resolve().is_relative_to(staging_base.resolve()) and staging.name.startswith(".pending-"), "unsafe staging cleanup path")
            if cleanup_path.exists():
                shutil.rmtree(cleanup_path)
        self.update_after_write(identifier)
        return self.read_case(identifier)

    def set_archived(self, identifier, archived, reason=None):
        case_id(identifier)
        lifecycle_schema({"archived": archived})
        if reason is not None:
            string(reason, "reason")
        self.read_index()
        existing = self.read_case(identifier)
        if existing["archived"] == archived:
            return existing
        return self.append(identifier, {**lifecycle_feedback(archived, reason), "lifecycle": {"archived": archived}})

    def search(self, query=None, product=None, kind=None, dimension=None, polarity=None, evidence_status=None, scope="history", archive="active", native_only=False, direction_id=None, variant_type=None):
        require(scope in {"current", "history"}, "search scope must be current or history")
        require(isinstance(archive, str) and archive in {"active", "archived", "all"}, "search archive must be active, archived, or all")
        if direction_id is not None:
            string(direction_id, "direction_id")
        require(variant_type is None or isinstance(variant_type, str) and variant_type in VARIANT_TYPES, "variant_type must be direction or execution")

        def direction_qualifies(current):
            direction = (current or {}).get("test_direction") or {}
            return (direction_id is None or direction.get("direction_id") == direction_id) and (variant_type is None or direction.get("variant_type") == variant_type)

        def qualifies(feedback):
            if kind and feedback["kind"] != kind:
                return False
            if evidence_status and feedback.get("evidence", {}).get("status") != evidence_status:
                return False
            if dimension or polarity:
                return any((not dimension or assessment["dimension"].casefold() == dimension.casefold()) and (not polarity or assessment["polarity"] == polarity) for assessment in feedback.get("assessments", []))
            return True

        matches = []
        for item in self.read_index()["cases"]:
            if archive != "all" and item["archived"] != (archive == "archived"):
                continue
            if native_only and not (item.get("current") or {}).get("native_reference"):
                continue
            search_field = "current_search_text" if scope == "current" else "search_text"
            if query and query.casefold() not in item[search_field]:
                continue
            if product and product.casefold() not in (item["product"] or "").casefold():
                continue
            if scope == "current":
                current = item["current"]
                if not direction_qualifies(current):
                    continue
                if current is None:
                    if any((kind, dimension, polarity, evidence_status)):
                        continue
                elif not qualifies(current):
                    continue
            else:
                # Qualifiers must belong to the same historical event;
                # dimension/polarity must belong to the same assessment.
                case = self.read_case(item["case_id"])
                if not any(qualifies(event["feedback"]) and direction_qualifies(event.get("current")) for event in case["events"]):
                    continue
            matches.append({**{key: data for key, data in item.items() if key not in {"search_text", "current_search_text"}}, "match_scope": scope})
        return matches

    def direction_summary(self, product=None, direction_id=None, archive="active"):
        """Describe current test groups without treating mixed windows as a metric total.

        A group is a known explicit product alias + stable direction_id, not an
        inferred cluster. Raw product labels remain in each observation. Historical
        snapshots and separate feedback metrics never supply
        missing current observations. Cohorts preserve supplied windows/version
        labels; even a matching cohort is not proof of comparable exposure.
        """
        if direction_id is not None:
            string(direction_id, "direction_id")
        all_selected = self.search(scope="current", archive=archive)
        product_group = direction_product_group(product)
        known_filter = product is not None and re.sub(r"\s+", " ", product.strip()).casefold() in DIRECTION_PRODUCT_ALIASES
        selected = [item for item in all_selected if not product or (
            direction_product_group(item.get("product")) == product_group if known_filter
            else product.casefold() in (item.get("product") or "").casefold())]
        products = sorted({direction_product_group(item.get("product")) for item in all_selected if item.get("product")}, key=str.casefold)
        groups = {}
        unclassified = []
        video_assignments = {}
        for item in selected:
            current = item.get("current") or {}
            video = (current.get("metrics") or {}).get("video_id")
            if video:
                video_assignments.setdefault(video, []).append({"case_id": item["case_id"],
                    "product": item.get("product"), "product_group": direction_product_group(item.get("product")),
                    "direction_id": (current.get("test_direction") or {}).get("direction_id")})
        for item in selected:
            current = item.get("current") or {}
            direction = current.get("test_direction")
            if not direction:
                unclassified.append(item["case_id"])
                continue
            if direction_id is not None and direction["direction_id"] != direction_id:
                continue
            key = (direction_product_group(item.get("product")), direction["direction_id"])
            group = groups.setdefault(key, {"product": key[0], "direction_id": key[1], "lead_code": direction_lead_code(key[1]), "case_count": 0,
                "product_aliases": [],
                "variant_counts": {"direction": 0, "execution": 0}, "definitions": [], "definition_conflict": False,
                "missing_counts": {"metrics": 0, "evidence_window": 0, "script_version": 0, "video_id": 0},
                "metric_availability": {field: 0 for field in sorted(PERFORMANCE_FIELDS)},
                "version_conflict_count": 0, "duplicate_video_ids": [], "cross_direction_video_count": 0, "observations": [], "cohorts": []})
            if item.get("product") not in group["product_aliases"]:
                group["product_aliases"].append(item.get("product"))
            definition = {field: direction[field] for field in ("main_difference", "concern", "closing")}
            if definition not in group["definitions"]:
                group["definitions"].append(definition)
            # Cases sharing a lead selling point are execution variants; only a different lead is a conflict.
            group["definition_conflict"] = len({entry["main_difference"] for entry in group["definitions"]}) > 1
            metrics = current.get("metrics") or {}
            evidence = current.get("evidence") or {}
            production = current.get("production") or {}
            binding = direction.get("script_version")
            production_version = production.get("script_version")
            version = binding or production_version
            conflict = bool(binding and production_version and binding != production_version)
            observation = {"case_id": item["case_id"], "title": item["title"],
                "product": item.get("product"), "product_group": key[0],
                "current_event_id": item["current_event_id"], "archived": item["archived"],
                "test_direction": direction, "metrics": metrics, "evidence": evidence,
                "production": production, "publish": current.get("publish") or {},
                "script_version": version,
                "version_source": "test_direction" if binding else "production" if production_version else None,
                "version_conflict": conflict}
            group["observations"].append(observation)
            group["case_count"] += 1
            group["variant_counts"][direction["variant_type"]] += 1
            group["version_conflict_count"] += int(conflict)
            group["missing_counts"]["metrics"] += int(not (PERFORMANCE_FIELDS & set(metrics)))
            group["missing_counts"]["evidence_window"] += int(not evidence.get("window"))
            group["missing_counts"]["script_version"] += int(not version)
            group["missing_counts"]["video_id"] += int(not metrics.get("video_id"))
            for field in PERFORMANCE_FIELDS & set(metrics):
                group["metric_availability"][field] += 1
            cohort_key = {"evidence_window": evidence.get("window"), "evidence_status": evidence.get("status"),
                "data_as_of": evidence.get("as_of"), "script_version": version,
                "variant_type": direction["variant_type"], "version_conflict": conflict}
            cohort = next((value for value in group["cohorts"] if all(value[field] == data for field, data in cohort_key.items())), None)
            if cohort is None:
                cohort = {**cohort_key, "case_ids": [], "case_count": 0}
                group["cohorts"].append(cohort)
            cohort["case_ids"].append(item["case_id"])
            cohort["case_count"] += 1
        # S-led directions first, then A, then B (by number); ids without a selling-point code come last.
        def order(group):
            lead = group["lead_code"]
            return ((group["product"] or "").casefold(), "SAB".index(lead[0]) if lead else 3,
                    int(lead[1:]) if lead else 0, group["direction_id"])
        ordered = sorted(groups.values(), key=order)
        duplicates = [{"video_id": video, "case_ids": [entry["case_id"] for entry in assignments],
            "assignments": assignments,
            "cross_direction": len({(entry["product_group"], entry["direction_id"]) for entry in assignments if entry["direction_id"] is not None}) > 1}
            for video, assignments in sorted(video_assignments.items()) if len(assignments) > 1]
        visible_videos = set()
        for group in ordered:
            group_videos = {entry["metrics"].get("video_id") for entry in group["observations"]} - {None}
            visible_videos.update(group_videos)
            group["duplicate_video_ids"] = [entry for entry in duplicates if entry["video_id"] in group_videos]
            group["cross_direction_video_count"] = sum(entry["cross_direction"] for entry in group["duplicate_video_ids"])
        return {"scope": "current", "archive": archive, "filters": {"product": product_group, "product_input": product, "direction_id": direction_id},
            "products": products,
            "case_count": len(selected), "classified_case_count": sum(group["case_count"] for group in ordered),
            "unclassified_case_ids": unclassified, "groups": ordered,
            "lead_tier_counts": {tier: sum(1 for group in ordered if (group["lead_code"] or "")[:1] == tier) for tier in ("S", "A", "B")}
                | {"uncoded": sum(1 for group in ordered if not group["lead_code"])},
            "duplicate_video_ids": [entry for entry in duplicates if direction_id is None or entry["video_id"] in visible_videos],
            "limitations": ["只按当前快照中人工登记的产品与方向编号分组，旧案例不推补方向。已知 202H 产品别名归为 SHARPAL 202H，原始产品名逐条保留；其他名称不按型号提及推断。",
                "指标保留来源原值；缺失值不填 0，不跨案例加总指标或平均 CTOR。",
                "窗口、版本、数据截至与证据状态分开列示；相同标签不证明流量、账号或投放条件可比。",
                "方向版本与制作版本冲突、方向定义冲突或视频重复须先核对；本汇总不判定因果赢家或核心卖点。"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="skill root (put before command)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("add", "append"):
        command = commands.add_parser(name)
        if name == "append":
            command.add_argument("case_id")
        command.add_argument("--input", required=True, type=Path, help="UTF-8 JSON input file")
    command = commands.add_parser("show")
    command.add_argument("case_id")
    for name in ("archive", "restore"):
        command = commands.add_parser(name)
        command.add_argument("case_id")
        command.add_argument("--reason")
    search = commands.add_parser("search")
    search.add_argument("--query")
    search.add_argument("--product")
    search.add_argument("--kind", choices=sorted(KINDS))
    search.add_argument("--dimension")
    search.add_argument("--polarity", choices=sorted(POLARITIES))
    search.add_argument("--evidence-status", choices=sorted(STATUSES))
    search.add_argument("--scope", choices=("current", "history"), default="current", help="current snapshot (default), or all historical events")
    search.add_argument("--archive", choices=("active", "archived", "all"), default="active", help="active cases (default), archived cases, or all cases")
    search.add_argument("--native-only", action="store_true", help="only current native-expression references")
    search.add_argument("--direction-id", help="exact test direction ID; respects current/history scope")
    search.add_argument("--variant-type", choices=sorted(VARIANT_TYPES))
    directions = commands.add_parser("directions", help="group current cases by product/direction; preserve raw metrics and evidence windows")
    directions.add_argument("--product")
    directions.add_argument("--direction-id")
    directions.add_argument("--archive", choices=("active", "archived", "all"), default="active")
    commands.add_parser("rebuild-index")
    commands.add_parser("validate")
    args = parser.parse_args(argv)
    store = CaseStore(args.root)
    try:
        if args.command == "add":
            result = store.add(load_json(args.input))
        elif args.command == "append":
            result = store.append(args.case_id, load_json(args.input))
        elif args.command == "show":
            result = store.read_case(args.case_id)
        elif args.command in {"archive", "restore"}:
            result = store.set_archived(args.case_id, args.command == "archive", args.reason)
        elif args.command == "search":
            result = store.search(args.query, args.product, args.kind, args.dimension, args.polarity, args.evidence_status, scope=args.scope, archive=args.archive, native_only=args.native_only, direction_id=args.direction_id, variant_type=args.variant_type)
        elif args.command == "directions":
            result = store.direction_summary(product=args.product, direction_id=args.direction_id, archive=args.archive)
        elif args.command == "rebuild-index":
            result = {"ok": True, "case_count": len(store.rebuild_index()["cases"])}
        else:
            result = {"ok": True, "case_count": len(store.read_index()["cases"])}
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        return 0
    except (OSError, StoreError, TypeError, ValueError) as exc:
        sys.stderr.write(f"case_store: {exc}\n")
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
