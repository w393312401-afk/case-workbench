"""Recover saved table runs through read-only APIs, using disposable files and FakeBridge only."""
import json
import copy
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_production_carousel_table as table_fixture


class CarouselTableResumeTests(unittest.TestCase):
    fixture = table_fixture.CarouselTableTests.fixture
    setUp = table_fixture.CarouselTableTests.setUp
    stop_server = table_fixture.CarouselTableTests.stop_server
    request = table_fixture.CarouselTableTests.request
    ok = table_fixture.CarouselTableTests.ok
    script = table_fixture.CarouselTableTests.script
    start = table_fixture.CarouselTableTests.start
    wait = table_fixture.CarouselTableTests.wait

    def listing(self, script):
        return self.ok("GET", "/api/production/carousel-tables?script_id=" + script["id"])

    def test_script_list_requires_one_existing_script_version_and_starts_nothing(self):
        script = self.script()
        with patch.object(self.server.app.ai, "_available", side_effect=AssertionError("read-only list checked AI")), \
                patch.object(self.server.app, "start_carousel_table", side_effect=AssertionError("read-only list started AI")):
            listed = self.listing(script)
        self.assertEqual(listed, {"script_id": script["id"], "script_version": 1, "tables": []})
        for query in ("", "?script_id=bad", f"?script_id={script['id']}&script_id={script['id']}"):
            with self.subTest(query=query):
                self.assertEqual(self.request("GET", "/api/production/carousel-tables" + query)[0], 400)
        self.assertEqual(self.request("GET", "/api/production/carousel-tables?script_id=SCR-000000000000")[0], 404)

    def test_running_table_can_be_rediscovered_without_changing_or_stopping_the_run(self):
        bridge = table_fixture.FakeBridge(output=table_fixture.TABLE, blocked=True)
        self.server.app.ai.bridge = bridge
        self.addCleanup(bridge.gate.set)
        script = self.script()
        table = self.start(script)
        self.assertTrue(bridge.started.wait(1))
        path = self.server.app._table_dir(table["id"]) / "state.json"
        before = path.read_bytes()
        listed = self.listing(script)["tables"]
        self.assertEqual(len(listed), 1)
        self.assertEqual((listed[0]["id"], listed[0]["status"], listed[0]["carousel_id"]), (table["id"], "running", ""))
        self.assertNotIn("carousels", listed[0])
        self.assertNotIn("requests", listed[0])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(self.server.app._table_cancel[table["id"]].is_set())
        self.assertEqual(len(bridge.calls), 1)
        bridge.gate.set()
        self.wait(table["id"])

    def test_completed_summary_links_its_existing_carousel_and_orders_newest_first(self):
        bridge = table_fixture.FakeBridge(output=table_fixture.TABLE)
        self.server.app.ai.bridge = bridge
        script = self.script()
        first = self.wait(self.start(script)["id"])
        second = self.wait(self.start(script)["id"])
        created = self.ok("POST", "/api/production/carousels", {
            "revision": self.ok("GET", "/api/production")["revision"], "request_id": uuid4().hex,
            "carousel": {"script_id": script["id"], "table_id": first["id"]}})["carousel"]
        snapshots = {table["id"]: (self.server.app._table_dir(table["id"]) / "state.json").read_bytes() for table in (first, second)}
        listed = self.listing(script)["tables"]
        self.assertEqual([table["id"] for table in listed], [second["id"], first["id"]])
        self.assertEqual([table["carousel_id"] for table in listed], ["", created["id"]])
        self.assertTrue(all(table["status"] == "completed" and "carousels" not in table for table in listed))
        self.assertTrue(all(table["has_result"] and table["slide_count"] == 3 and not table["carousel_missing"] for table in listed))
        for identifier, snapshot in snapshots.items():
            self.assertEqual((self.server.app._table_dir(identifier) / "state.json").read_bytes(), snapshot)
        self.assertEqual(len(bridge.calls), 2)
        detail = self.ok("GET", "/api/production/carousel-tables/" + second["id"])["table"]
        self.assertEqual(len(detail["carousels"]), 1)
        self.assertEqual(detail["carousel_id"], "")
        linked_detail = self.ok("GET", "/api/production/carousel-tables/" + first["id"])["table"]
        self.assertEqual(linked_detail["carousel_id"], created["id"])

    def test_saved_result_remains_readable_after_later_stages_stop_without_changing_history(self):
        self.server.app.ai.bridge = table_fixture.FakeBridge(output=table_fixture.TABLE)
        script = self.script()
        table = self.wait(self.start(script)["id"])
        folder = self.server.app._table_dir(table["id"])
        record = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        production_path = self.server.app.production.path
        production_before = production_path.read_bytes() if production_path.exists() else None
        creation_before = self.server.app.creation.public()
        for status in ("needs_attention", "failed", "cancelled", "interrupted"):
            with self.subTest(status=status):
                record.update(status=status, error="后续图片阶段未完成。")
                self.server.app._write_table(folder, record)
                saved = (folder / "state.json").read_bytes()
                with patch.object(self.server.app, "_write_table", side_effect=AssertionError("view rewrote history")), \
                        patch.object(self.server.app.production, "mutate", side_effect=AssertionError("view created a production job")), \
                        patch.object(self.server.app.ai, "_available", side_effect=AssertionError("view started AI")):
                    listed = self.listing(script)["tables"][0]
                    detail = self.ok("GET", "/api/production/carousel-tables/" + table["id"])["table"]
                self.assertEqual(detail["status"], status)
                self.assertEqual(detail["error"], record["error"])
                self.assertEqual(detail["slide_count"], 3)
                self.assertTrue(detail["has_result"])
                self.assertEqual(len(detail["carousels"][0]["slides"]), 3)
                self.assertIn("Hook 1", detail["carousels"][0]["text"])
                self.assertIn("标题1", detail["carousels"][0]["translation_zh"])
                self.assertEqual(listed, {key: value for key, value in detail.items() if key != "carousels"})
                self.assertEqual((folder / "state.json").read_bytes(), saved)
                self.assertEqual(production_path.read_bytes() if production_path.exists() else None, production_before)
                self.assertEqual(self.server.app.creation.public(), creation_before)
                self.assertEqual(self.request("POST", "/api/production/carousels", {
                    "revision": self.ok("GET", "/api/production")["revision"], "request_id": uuid4().hex,
                    "carousel": {"script_id": script["id"], "table_id": table["id"]}})[0], 409)

    def test_empty_or_unparseable_saved_replies_have_no_result_in_lists_and_details(self):
        self.server.app.ai.bridge = table_fixture.FakeBridge(output=table_fixture.TABLE)
        script = self.script()
        table = self.wait(self.start(script)["id"])
        folder = self.server.app._table_dir(table["id"])
        record = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        empty_copy = '```json\n{"content_type":"photo_carousel","slides":[]}\n```'
        malformed_copy = '```json\n{"content_type":"photo_carousel","slides":[{"title_lines": 4}]}\n```'
        for status, output in (("failed", ""), ("needs_attention", "没有逐张表"),
                               ("completed", empty_copy), ("failed", malformed_copy)):
            with self.subTest(status=status, output=output):
                record.update(status=status, output=output)
                self.server.app._write_table(folder, record)
                listed = self.listing(script)["tables"][0]
                detail = self.ok("GET", "/api/production/carousel-tables/" + table["id"])["table"]
                self.assertFalse(listed["has_result"])
                self.assertEqual(listed["slide_count"], 0)
                self.assertEqual(detail["carousels"], [])
                self.assertEqual(detail["status"], status)
                self.assertEqual(listed, {key: value for key, value in detail.items() if key != "carousels"})

    def test_deleted_linked_carousel_does_not_hide_the_original_table_or_recreate_the_order(self):
        self.server.app.ai.bridge = table_fixture.FakeBridge(output=table_fixture.TABLE)
        script = self.script()
        table = self.wait(self.start(script)["id"])
        created = self.ok("POST", "/api/production/carousels", {
            "revision": self.ok("GET", "/api/production")["revision"], "request_id": uuid4().hex,
            "carousel": {"script_id": script["id"], "table_id": table["id"]}})["carousel"]
        folder = self.server.app._table_dir(table["id"])
        record = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        # A stale saved ID never overrides a real, correctly scoped source link.
        record.update(status="needs_attention", carousel_id="CAR-000000000000")
        self.server.app._write_table(folder, record)
        for value in (self.listing(script)["tables"][0], self.server.app.carousel_table(table["id"])):
            self.assertEqual(value["carousel_id"], created["id"])
            self.assertFalse(value["carousel_missing"])
        record["carousel_id"] = created["id"]
        self.server.app._write_table(folder, record)
        store = self.server.app.production
        store.mutate("carousel_delete", {"request_id": uuid4().hex, "carousel_id": created["id"]})
        saved, catalog = (folder / "state.json").read_bytes(), store.path.read_bytes()
        with patch.object(store, "mutate", side_effect=AssertionError("view recreated deleted order")), \
                patch.object(self.server.app, "_write_table", side_effect=AssertionError("view rewrote table")):
            listed = self.listing(script)["tables"][0]
            detail = self.ok("GET", "/api/production/carousel-tables/" + table["id"])["table"]
        self.assertTrue(detail["has_result"])
        self.assertTrue(detail["carousel_missing"])
        self.assertEqual(detail["carousel_id"], "")
        self.assertEqual(detail["status"], "needs_attention")
        self.assertEqual(len(detail["carousels"][0]["slides"]), 3)
        self.assertEqual(listed, {key: value for key, value in detail.items() if key != "carousels"})
        self.assertEqual((folder / "state.json").read_bytes(), saved)
        self.assertEqual(store.path.read_bytes(), catalog)
        self.assertEqual(store.records()["carousels"], [])

    def test_list_is_scoped_to_exact_script_id_and_version(self):
        self.server.app.ai.bridge = table_fixture.FakeBridge(output=table_fixture.TABLE)
        script = self.script()
        table = self.wait(self.start(script)["id"])
        creation = self.server.app.creation
        derived = creation.mutate("script", {"request_id": uuid4().hex, "revision": creation.public()["revision"],
            "script": {"product": "202H", "title": "第二版", "text": script["text"], "confirmed": True, "parent_id": script["id"]}})["script"]
        other = self.script(text="A different reading.\nOf the same candidate.")
        derived_table = self.wait(self.start(derived)["id"])
        self.wait(self.start(other)["id"])
        wrong_version = json.loads((self.server.app._table_dir(table["id"]) / "state.json").read_text(encoding="utf-8"))
        wrong_version.update(id="TBL-" + uuid4().hex[:12], script_version=99)
        folder = self.server.app._table_dir(wrong_version["id"])
        folder.mkdir()
        self.server.app._write_table(folder, wrong_version)
        self.assertEqual([item["id"] for item in self.listing(script)["tables"]], [table["id"]])
        self.assertEqual([item["id"] for item in self.listing(derived)["tables"]], [derived_table["id"]])

    def test_lost_process_state_is_visible_as_interrupted_without_rewriting_history(self):
        self.server.app.ai.bridge = table_fixture.FakeBridge(output=table_fixture.TABLE)
        script = self.script()
        table = self.wait(self.start(script)["id"])
        folder = self.server.app._table_dir(table["id"])
        record = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        record.update(status="running", finished_at="")
        self.server.app._write_table(folder, record)
        before = (folder / "state.json").read_bytes()
        listed = self.listing(script)["tables"]
        self.assertEqual(listed[0]["status"], "interrupted")
        self.assertEqual((folder / "state.json").read_bytes(), before)

    def test_same_table_with_a_new_request_reuses_the_order_and_keeps_durable_receipts(self):
        self.server.app.ai.bridge = table_fixture.FakeBridge(output=table_fixture.TABLE)
        script = self.script()
        table = self.wait(self.start(script)["id"])
        def body():
            return {"revision": self.ok("GET", "/api/production")["revision"], "request_id": uuid4().hex,
                    "carousel": {"script_id": script["id"], "table_id": table["id"]}}
        first = self.ok("POST", "/api/production/carousels", body())["carousel"]
        duplicate = body()
        second = self.ok("POST", "/api/production/carousels", duplicate)
        self.assertTrue(second["existing"])
        self.assertEqual(second["carousel"]["id"], first["id"])
        self.assertEqual(len(self.ok("GET", "/api/production")["carousels"]), 1)
        with patch.object(self.server.app, "carousel_from_table", side_effect=AssertionError("receipt must not resolve source again")):
            replay = self.ok("POST", "/api/production/carousels", duplicate)
            changed = copy.deepcopy(duplicate)
            changed["carousel"]["table_id"] = "TBL-000000000000"
            status, _, response = self.request("POST", "/api/production/carousels", changed)
        self.assertEqual(replay, second)
        self.assertEqual(status, 409, response)
        self.assertEqual(self.listing(script)["tables"][0]["carousel_id"], first["id"])

    def test_same_table_identifier_does_not_merge_other_script_versions_or_legacy_orders(self):
        self.server.app.ai.bridge = table_fixture.FakeBridge(output=table_fixture.TABLE)
        script = self.script()
        table = self.wait(self.start(script)["id"])
        first = self.ok("POST", "/api/production/carousels", {
            "revision": self.ok("GET", "/api/production")["revision"], "request_id": uuid4().hex,
            "carousel": {"script_id": script["id"], "table_id": table["id"]}})["carousel"]
        store = self.server.app.production
        fields = ("product", "direction_id", "script_id", "script_version", "script_family", "title", "variant_label",
                  "slides", "caption_en", "caption_zh", "copy", "problems", "source")
        resolved = {field: copy.deepcopy(first[field]) for field in fields}
        def create(value):
            return store.mutate("carousel", {"request_id": uuid4().hex, "revision": store.public()["revision"], "carousel": {}}, value)
        another_version = create({**resolved, "script_version": first["script_version"] + 1})["carousel"]
        self.assertNotEqual(another_version["id"], first["id"])
        another_script = create({**resolved, "script_id": "SCR-000000000000"})["carousel"]
        self.assertNotEqual(another_script["id"], first["id"])
        legacy = {**resolved, "source": {"task_id": "", "round_id": "", "variant_index": None}}
        legacy_first, legacy_second = create(legacy)["carousel"], create(legacy)["carousel"]
        self.assertNotEqual(legacy_first["id"], legacy_second["id"])


if __name__ == "__main__":
    unittest.main()
