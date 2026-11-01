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
        row("video:running", "video", "rendering", busy=True, workflow_complete=False),
        row("video:approved", "video", "awaiting_publication", workflow_complete=True),
        row("task:attention", "task", "pending_selection", needs_attention=True),
        row("video:posted", "video", "published", history=True, calendar_pending=True, published_on="2026-10-22"),
        row("video:archived", "video", "awaiting_review", archived=True, needs_attention=True),
    ]


class MemoryContent(ContentWorkbenchMixin):
    def __init__(self):
        self.items = rows()
        self.lock = threading.RLock()
        self.delete_job_order = Mock(return_value={"warnings": []})
        self.saved_views = []
        self.content_views = Mock(public=lambda product="": {"views": self.saved_views})

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

    def test_saved_views_are_counted_over_all_rows_with_their_search(self):
        self.app.saved_views = [
            {"id": "videos", "name": "视频", "q": "", "filter": custom(rule("kind", "video"))},
            {"id": "none", "name": "无结果", "q": "不存在的词", "filter": custom(rule("kind", "video"))}]
        result = self.catalog(view="attention")
        videos = {item["id"] for item in self.app.items if item["kind"] == "video"}
        self.assertEqual(result["view_counts"], {"videos": len(videos), "none": 0})

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
