"""Custom content filters share list, archive preview and delete boundaries."""
from __future__ import annotations

import copy
import json
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import test_server as fixture
from creation_store import CreationError
from workbench_content import ContentWorkbenchMixin, MAX_FILTER_LENGTH


def rule(field, *values):
    return {"field": field, "values": list(values)}


def custom(*conditions, match="all"):
    return {"match": match, "conditions": list(conditions)}


def rows():
    def row(identifier, kind, stage, **extra):
        return {"id": identifier, "kind": kind, "stage": stage, "title": identifier,
                "product": "202H", "updated_at": "2026-10-23", **extra}
    return [
        row("video:review", "video", "awaiting_review", needs_attention=True, workflow_complete=False),
        row("carousel:review", "carousel", "reviewing", needs_attention=True, workflow_complete=False),
        row("video:running", "video", "rendering", busy=True, workflow_complete=False),
        row("video:approved", "video", "awaiting_publication", workflow_complete=True),
        row("task:attention", "task", "pending_selection", needs_attention=True),
        row("video:posted", "video", "published", history=True, calendar_pending=True, published_on="2026-10-22"),
        row("video:archived", "video", "awaiting_review", archived=True, needs_attention=True),
        row("carousel:archived", "carousel", "awaiting_publication", archived=True),
    ]


class MemoryContent(ContentWorkbenchMixin):
    def __init__(self):
        self.items = rows()
        self.lock = threading.RLock()
        self.delete_job_order = Mock(return_value={"warnings": []})
        self.delete_carousel_order = Mock(return_value={"warnings": []})

    def _content_snapshot(self, product=None):
        return copy.deepcopy(self.items), {"revision": "filters-test", "items": {}}


class ContentFilterTests(unittest.TestCase):
    def setUp(self):
        self.app = MemoryContent()

    def catalog(self, content_filter=None, **query):
        query = {"view": "all", **query}
        if content_filter is not None:
            query["filter"] = json.dumps(content_filter)
        return self.app.content_catalog({key: [str(value)] for key, value in query.items()})

    def ids(self, result):
        return {item["id"] for item in result["items"]}

    def test_multi_select_is_or_within_a_condition_and_all_across_conditions(self):
        result = self.catalog(custom(rule("kind", "video", "carousel"),
                                     rule("status", "awaiting_review", "approved")))
        self.assertEqual(self.ids(result), {"video:review", "carousel:review", "video:approved",
                                           "video:archived", "carousel:archived"})

    def test_any_combines_conditions_without_duplicating_rows(self):
        result = self.catalog(custom(rule("kind", "carousel"), rule("status", "awaiting_review"), match="any"))
        self.assertEqual(self.ids(result), {"carousel:review", "carousel:archived", "video:review", "video:archived"})
        self.assertEqual(result["total"], 4)

    def test_presets_use_the_existing_live_row_predicates(self):
        for view in ("attention", "running", "publish"):
            with self.subTest(view=view):
                legacy = self.catalog(view=view)
                explicit = self.catalog(custom(rule("view", view)))
                self.assertEqual(explicit, legacy)
        union = self.catalog(custom(rule("view", "attention", "running")))
        self.assertEqual(self.ids(union), {"video:review", "carousel:review", "task:attention", "video:running"})

    def test_filter_runs_before_pagination_and_keeps_global_counts_and_calendar(self):
        baseline = self.catalog()
        first = self.catalog(custom(rule("status", "awaiting_review")), page_size=1)
        second = self.catalog(custom(rule("status", "awaiting_review")), page_size=1, page=2)
        last = self.catalog(custom(rule("status", "awaiting_review")), page_size=1, page=99)
        self.assertEqual((first["total"], first["pages"], last["page"]), (3, 3, 3))
        self.assertEqual(last["items"][0]["id"], "video:archived")
        self.assertEqual(len(self.ids(first) | self.ids(second) | self.ids(last)), 3)
        self.assertEqual(first["counts"], baseline["counts"])
        self.assertEqual(first["calendar_pending"], baseline["calendar_pending"])
        self.assertEqual(first["archived_deletable"], 1)

    def test_legacy_scope_and_search_intersect_custom_conditions(self):
        result = self.catalog(custom(rule("status", "needs_attention", "approved")),
                              view="attention", kind="video", status="awaiting_review", q="REVIEW")
        self.assertEqual(self.ids(result), {"video:review"})

    def test_empty_conditions_match_all_for_both_modes(self):
        for match in ("all", "any"):
            self.assertEqual(self.catalog(custom(match=match)), self.catalog())

    def test_malformed_or_oversized_filter_is_rejected_before_reading_records(self):
        bad = [None, False, [], "", "{", "null", "[]", " " * (MAX_FILTER_LENGTH + 1),
               {}, {"match": "other", "conditions": []}, {"match": [], "conditions": []},
               {"match": "all", "conditions": {}}, {"match": "all", "conditions": [], "extra": 1},
               custom(*([rule("kind", "video")] * 13)), custom(None), custom({"field": "kind"}),
               custom(rule("unknown", "video")), custom(rule("kind", "all")), custom(rule("status", "unknown")),
               custom(rule("view", "all")), custom(rule("kind")), custom(rule("kind", {})),
               custom({"field": [], "values": ["video"]}), custom({"field": "kind", "values": "video"}),
               custom({"field": "kind", "values": ["video"], "extra": True}),
               custom(rule("view", *(["attention"] * 4)))]
        with patch.object(self.app, "_content_snapshot", side_effect=AssertionError("invalid filter reached records")):
            for value in bad:
                with self.subTest(value=repr(value)[:100]):
                    with self.assertRaises(CreationError) as error:
                        self.app.content_catalog({"filter": [value if isinstance(value, str) else json.dumps(value)]})
                    self.assertEqual(error.exception.status, 400)
            with self.assertRaises(CreationError):
                self.app.content_catalog({"filter": [json.dumps(custom()), json.dumps(custom())]})

    def test_archive_count_preview_and_delete_use_the_same_filter(self):
        content_filter = custom(rule("kind", "video"), rule("status", "archived"))
        catalog = self.catalog(content_filter)
        preview = self.app.delete_archived_content({"filter": content_filter, "preview": True})
        self.assertEqual(self.ids(catalog), {"video:archived"})
        self.assertEqual(self.ids(preview), self.ids(catalog))
        self.assertEqual(catalog["archived_deletable"], len(preview["items"]))
        result = self.app.delete_archived_content({"filter": content_filter,
                                                  "ids": ["video:archived", "carousel:archived", "video:review"]})
        self.assertEqual((result["deleted"], result["failed"]), (1, 2))
        self.app.delete_job_order.assert_called_once()
        self.assertEqual(self.app.delete_job_order.call_args.args[0], "archived")
        self.app.delete_carousel_order.assert_not_called()

    def test_archive_preview_respects_any_conditions_and_legacy_scope(self):
        content_filter = custom(rule("kind", "video"), rule("status", "approved"), match="any")
        preview = self.app.delete_archived_content({"filter": json.dumps(content_filter), "preview": True})
        self.assertEqual(self.ids(preview), {"video:archived", "carousel:archived"})
        preview = self.app.delete_archived_content({"filter": content_filter, "status": "approved", "preview": True})
        self.assertEqual(self.ids(preview), {"carousel:archived"})
        preview = self.app.delete_archived_content({"filter": custom(rule("view", "attention")), "preview": True})
        self.assertEqual(preview["items"], [])
        for value in (None, {}, custom(rule("status", "all"))):
            with self.subTest(value=value), self.assertRaises(CreationError):
                self.app.delete_archived_content({"filter": value, "preview": True})


class ContentFilterHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def test_json_query_and_archive_body_share_filter_over_http(self):
        content_filter = custom(rule("kind", "video"), rule("status", "archived"))
        query = urlencode({"view": "all", "filter": json.dumps(content_filter)})
        with patch.object(self.server.app, "_content_snapshot", return_value=(rows(), {"revision": "fixture"})):
            status, _, result = self.request("GET", "/api/content?" + query)
            self.assertEqual(status, 200, result)
            self.assertEqual([item["id"] for item in result["items"]], ["video:archived"])
            status, _, preview = self.request("POST", "/api/content/delete-archived",
                                               {"filter": content_filter, "preview": True})
            self.assertEqual(status, 200, preview)
            self.assertEqual([item["id"] for item in preview["items"]], ["video:archived"])
            self.assertEqual(result["archived_deletable"], len(preview["items"]))

    def test_invalid_json_or_fields_return_400_on_both_routes(self):
        invalid = custom(rule("kind", "unknown"))
        for value in ("{", json.dumps(invalid)):
            status, _, response = self.request("GET", "/api/content?" + urlencode({"filter": value}))
            self.assertEqual(status, 400, response)
        status, _, response = self.request("POST", "/api/content/delete-archived", {"filter": invalid, "preview": True})
        self.assertEqual(status, 400, response)


if __name__ == "__main__":
    unittest.main()
