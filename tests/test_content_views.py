"""Saved views are shared durable data, including safe one-time browser migration."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_server as fixture
from content_views import ContentViewsStore, MAX_VIEWS
from creation_store import CreationError
from workbench_content import _parse_content_filter


def view(identifier="test-view", name="待审视频", **changes):
    return {"id": identifier, "name": name, "q": "",
            "filter": {"match": "all", "conditions": [{"field": "kind", "values": ["video"]},
                                                       {"field": "status", "values": ["awaiting_review"]}]}, **changes}


class ContentViewsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="content-views-test-")
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name)
        self.store = self.reopen()

    def reopen(self):
        return ContentViewsStore(self.runtime, filter_validator=_parse_content_filter)

    def save(self, record=None, product="202H", revision=None):
        return self.store.mutate({"product": product, "action": "save", "view": record or view(),
                                 "revision": self.store.public(product)["revision"] if revision is None else revision})

    def delete(self, identifier="test-view", product="202H", revision=None):
        return self.store.mutate({"product": product, "action": "delete", "id": identifier,
                                 "revision": self.store.public(product)["revision"] if revision is None else revision})

    def migrate(self, records, product="202H"):
        return self.store.mutate({"product": product, "action": "import", "views": records})

    def assert_error(self, status, function, *args, **kwargs):
        with self.assertRaises(CreationError) as failure:
            function(*args, **kwargs)
        self.assertEqual(failure.exception.status, status)
        return str(failure.exception)

    def test_shared_persistence_normalized_product_and_independent_scopes(self):
        empty = self.store.public("202H")
        self.assertEqual(empty["views"], [])
        self.assertFalse(self.store.path.exists())
        saved = self.save(product="SHARPAL 202h")
        self.assertEqual(saved["product"], "202H")
        self.assertNotEqual(saved["revision"], empty["revision"])
        self.assertEqual(self.reopen().public("202h"), saved)
        self.assertEqual(self.store.public("205H")["views"], [])
        self.assertEqual(self.store.public()["views"], [])
        self.save(view("global-view", "全产品视图"), product="")
        self.assertEqual(len(self.reopen().public()["views"]), 1)
        self.assertEqual(self.store.public("202H"), saved)

    def test_updates_and_deletes_are_versioned_and_isolated_from_callers(self):
        initial = self.save()
        initial["views"][0]["filter"]["conditions"].clear()
        self.assertEqual(len(self.store.public("202H")["views"][0]["filter"]["conditions"]), 2)
        updated = self.save(view(name="待核对", q="202H-S1"))
        self.assertEqual(updated["views"], [view(name="待核对", q="202H-S1")])
        self.assert_error(409, self.save, view(name="旧浏览器改动"), revision=initial["revision"])
        self.assert_error(409, self.delete, revision=initial["revision"])
        self.assertEqual(self.reopen().public("202H"), updated)
        removed = self.delete()
        self.assertEqual(removed["views"], [])
        self.assertIn("test-view", self.reopen().read()["products"]["202H"]["known_ids"])
        self.assert_error(409, self.save, revision=removed["revision"])

    def test_retry_after_lost_response_is_idempotent_without_overwriting_newer_values(self):
        original_revision = self.store.public("202H")["revision"]
        saved = self.save(revision=original_revision)
        self.assertEqual(self.save(revision=original_revision), saved)
        updated = self.save(view(name="其他设备更新"))
        self.assert_error(409, self.save, revision=original_revision)
        removed = self.delete(revision=updated["revision"])
        self.assertEqual(self.delete(revision=updated["revision"]), removed)
        self.assert_error(404, self.delete, "missing-view")

    def test_import_is_additive_idempotent_and_never_overwrites_or_resurrects(self):
        legacy = view("legacy-view", "旧浏览器视图")
        original = self.migrate([legacy])
        self.assertEqual(self.migrate([legacy]), original)
        updated = self.save(view("legacy-view", "服务端修改", q="new"))
        self.assertEqual(self.migrate([legacy]), updated)
        removed = self.delete("legacy-view")
        self.assertEqual(self.reopen().mutate({"product": "202H", "action": "import", "views": [legacy]}), removed)
        native = self.save()
        self.assertEqual(self.migrate([view(name="原生视图被旧浏览器覆盖")]), native)
        removed = self.delete()
        self.assertEqual(self.migrate([view()]), removed)
        restored = self.migrate([view("another-view", "其他视图")])
        self.assertEqual(restored["views"], [view("another-view", "其他视图")])

    def test_import_keeps_distinct_legacy_records_with_conflicting_names(self):
        self.save()
        result = self.migrate([view("legacy-1"), view("legacy-2"), view("legacy-3", "全部")])
        self.assertEqual([row["name"] for row in result["views"]],
                         ["待审视频", "待审视频（导入）", "待审视频（导入 2）", "全部（导入）"])
        long_name = "长" * 40
        self.save(view("native-long", long_name))
        renamed = self.migrate([view("legacy-long", long_name)])["views"][-1]["name"]
        self.assertEqual(len(renamed), 40)
        self.assertTrue(renamed.endswith("（导入）"))

    def test_import_over_limit_is_atomic_and_can_retry_after_freeing_space(self):
        initial = self.migrate([view(f"full-{index}", f"视图 {index}") for index in range(MAX_VIEWS)])
        before = self.store.path.read_bytes()
        self.assertIn("30", self.assert_error(400, self.migrate, [view("pending-1"), view("pending-2", "另一条")]))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assert_error(400, self.save, view("extra", "额外视图"))
        self.assertEqual(self.store.public("202H"), initial)
        self.delete("full-0")
        self.delete("full-1")
        result = self.migrate([view("pending-1"), view("pending-2", "另一条")])
        self.assertEqual(len(result["views"]), MAX_VIEWS)
        self.assertEqual([row["id"] for row in result["views"]][-2:], ["pending-1", "pending-2"])

    def test_invalid_names_search_filter_and_identity_are_rejected_without_mutation(self):
        initial = self.save()
        bad_records = [[], {}, view(id=[]), view(id="../other"), view(name=[]), view(name=" "),
                       view(name="长" * 41), view(name="全部"), view(name="two\nlines"), view(q=None),
                       view(q="q" * 201), view(filter={}),
                       view(filter={"match": "any", "conditions": [{"field": "unknown", "values": ["video"]}]}),
                       {**view(), "extra": True}]
        for record in bad_records:
            with self.subTest(record=record):
                self.assert_error(400, self.store.mutate, {"product": "202H", "action": "save", "view": record,
                                                         "revision": initial["revision"]})
        self.assert_error(400, self.save, view("other-id", "待审视频"))
        self.assert_error(400, self.migrate, [view("same"), view("same", "另一个名称")])
        self.assert_error(400, self.migrate, [view("good", "有效"), view("bad", "")])
        self.assertEqual(self.store.public("202H"), initial)

    def test_malformed_mutation_envelopes_and_product_are_rejected(self):
        bad = [None, [], {}, {"action": []}, {"action": "unknown"},
               {"product": "202H", "action": "import", "views": {}},
               {"product": [], "action": "import", "views": []},
               {"product": "x" * 201, "action": "import", "views": []},
               {"product": "202H", "action": "import", "views": [], "revision": "wrong"},
               {"product": "202H", "action": "save", "view": view(), "revision": []}]
        for body in bad:
            with self.subTest(body=body):
                self.assert_error(400, self.store.mutate, body)
        self.assertFalse(self.store.path.exists())

    def test_corrupt_files_are_preserved_and_rejected_instead_of_reset(self):
        self.save()
        valid = self.store.read()
        unknown_filter = copy.deepcopy(valid)
        unknown_filter["products"]["202H"]["views"][0]["filter"]["conditions"][0]["field"] = "invalid"
        missing_known = copy.deepcopy(valid)
        missing_known["products"]["202H"]["known_ids"] = []
        for bad in ("{", json.dumps({"schema_version": 1, "products": []}),
                    json.dumps(unknown_filter), json.dumps(missing_known)):
            with self.subTest(bad=bad):
                self.store.path.write_text(bad, encoding="utf-8")
                self.assert_error(500, self.store.public, "202H")
                self.assert_error(500, self.migrate, [view("new-view")])
                self.assertEqual(self.store.path.read_text(encoding="utf-8"), bad)

    def test_failed_atomic_write_keeps_previous_data_and_does_not_mark_imported(self):
        initial = self.save()
        with patch("content_views.durable_json", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                self.migrate([view("legacy", "旧视图")])
        self.assertEqual(self.reopen().public("202H"), initial)
        self.assertNotIn("legacy", self.reopen().read()["products"]["202H"]["known_ids"])
        self.assertEqual(len(self.migrate([view("legacy", "旧视图")])["views"]), 2)


class ContentViewsHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def test_save_read_update_delete_are_shared_and_authenticated(self):
        status, _, empty = self.request("GET", "/api/content/views?product=202h")
        self.assertEqual(status, 200, empty)
        self.assertEqual(empty["product"], "202H")
        body = {"product": "202H", "action": "save", "view": view(), "revision": empty["revision"]}
        status, _, denied = self.request("POST", "/api/content/views", body, authenticated=False)
        self.assertEqual(status, 403, denied)
        status, _, saved = self.request("POST", "/api/content/views", body)
        self.assertEqual(status, 200, saved)
        status, _, observed = self.request("GET", "/api/content/views?product=202H")
        self.assertEqual((status, observed), (200, saved))
        status, _, conflict = self.request("POST", "/api/content/views", {**body, "view": view(name="另一个设备")})
        self.assertEqual(status, 409, conflict)
        status, _, result = self.request("POST", "/api/content/views", {
            "product": "202H", "action": "delete", "id": "test-view", "revision": saved["revision"]})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["views"], [])
        status, _, migrated = self.request("POST", "/api/content/views", {
            "product": "202H", "action": "import", "views": [view(), view("another-view", "另一个视图")]})
        self.assertEqual(status, 200, migrated)
        self.assertEqual([row["id"] for row in migrated["views"]], ["another-view"])

    def test_queries_and_invalid_filters_return_400_without_saved_data(self):
        for path in ("/api/content/views?product=202H&product=205H", "/api/content/views?extra=value"):
            status, _, result = self.request("GET", path)
            self.assertEqual(status, 400, result)
        revision = self.server.app.content_views.public("202H")["revision"]
        status, _, result = self.request("POST", "/api/content/views", {
            "product": "202H", "action": "save", "view": view(filter={}), "revision": revision})
        self.assertEqual(status, 400, result)
        self.assertFalse(self.server.app.content_views.path.exists())


if __name__ == "__main__":
    unittest.main()
