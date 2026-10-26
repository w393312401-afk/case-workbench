"""Content origin stays in the append-only case snapshot; all writes use fixtures."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from uuid import uuid4

import test_server as fixture


class ContentOriginTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    detail = fixture.WorkbenchHTTPTests.detail
    body = fixture.WorkbenchHTTPTests.body
    save = fixture.WorkbenchHTTPTests.save
    record_bytes = fixture.WorkbenchHTTPTests.record_bytes
    assert_old_records_unchanged = fixture.WorkbenchHTTPTests.assert_old_records_unchanged

    def test_set_change_clear_round_trip_preserves_history_and_reading(self):
        before = self.record_bytes()
        original = copy.deepcopy(self.detail()["original"])
        expected = [("self_produced", "自制内容参考"),
                    ("overseas_creator", "达人内容参考"), (None, "未标注")]
        seen_origins = []
        for origin, label in expected:
            with self.subTest(origin=origin):
                previous = self.detail()
                old_state = self.request("GET", "/api/state")[2]
                body = self.body(previous)
                if origin:
                    body["current"]["content_origin"] = origin
                else:
                    body["current"].pop("content_origin")
                status, _, result = self.save(body)
                self.assertEqual(status, 200, result)
                self.assertFalse(result.get("warning"), result)
                current = self.detail()
                self.assertEqual(current["current"].get("content_origin"), origin)
                self.assertEqual(current["original"], original)
                self.assertNotEqual(current["revision"], previous["revision"])
                stored = self.store.read_case(self.identifier)
                self.assertEqual(stored["current"], current["current"])
                self.assertEqual(len(stored["events"]), len(previous["events"]) + 1)
                seen_origins.append(stored["events"][-1]["current"].get("content_origin"))
                fresh_state = self.request("GET", "/api/state")[2]
                self.assertNotEqual(fresh_state["digest"], old_state["digest"])
                entry = next(row for row in fresh_state["cases"] if row["case_id"] == self.identifier)
                self.assertEqual(entry["current"].get("content_origin"), origin)
                reading = (self.runtime / "library" / self.identifier / "case.md").read_text(encoding="utf-8")
                index = (self.runtime / "library" / "INDEX.md").read_text(encoding="utf-8")
                self.assertIn(f"**内容来源：{label}**", reading)
                self.assertIn("| 内容来源 |", index)
                self.assertIn(f"| {label} |", index)
                self.assert_old_records_unchanged(before)
                if origin:
                    self.assertEqual(self.store.search(query=origin, scope="current")[0]["case_id"], self.identifier)
                    self.assertEqual(self.store.search(query=label, scope="current")[0]["case_id"], self.identifier)
        self.assertEqual(seen_origins, ["self_produced", "overseas_creator", None])
        self.assertNotIn("content_origin", self.detail()["current"])
        self.assertEqual(self.store.search(query="self_produced", scope="current"), [])
        self.assertEqual(self.store.search(query="达人内容参考", scope="current"), [])
        self.assertEqual(self.store.search(query="self_produced", scope="history")[0]["case_id"], self.identifier)
        history = (self.runtime / "library" / self.identifier / "history.md").read_text(encoding="utf-8")
        self.assertIn('"content_origin": "self_produced"', history)
        self.assertIn('"content_origin": "overseas_creator"', history)

    def test_new_case_api_and_cli_show_preserve_explicit_origin(self):
        current = fixture.new_case_snapshot(self.video)
        current["content_origin"] = "overseas_creator"
        status, _, result = self.request("POST", "/api/cases", {
            "request_id": uuid4().hex, "product": "Other product", "current": current,
            "note": "明确记录海外本土达人制作，产品归属不影响来源分类。",
        })
        self.assertEqual(status, 200, result)
        identifier = result["case"]["case_id"]
        script = fixture.APP_ROOT / "scripts" / "case_store.py"
        command = subprocess.run([sys.executable, "-X", "utf8", str(script), "--root", str(self.skill), "show", identifier],
                                 capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertEqual(json.loads(command.stdout)["current"]["content_origin"], "overseas_creator")
        indexed = next(row for row in self.store.read_index()["cases"] if row["case_id"] == identifier)
        self.assertEqual(indexed["current"]["content_origin"], "overseas_creator")
        self.assertEqual(self.store.search(query="达人内容参考", scope="current")[0]["case_id"], identifier)

    def test_invalid_origin_is_rejected_without_appending_records(self):
        before = self.record_bytes()
        index_before = (self.skill / "cases" / "index.json").read_bytes()
        for invalid in ("", "unknown", "own_products", "native", "domestic_creator", None, True, 3, [], {}):
            with self.subTest(origin=invalid):
                body = self.body()
                body["current"]["content_origin"] = invalid
                status, _, result = self.save(body)
                self.assertEqual(status, 400, result)
                self.assertIn("content_origin", result["error"])
                self.assertEqual(self.record_bytes(), before)
                self.assertEqual((self.skill / "cases" / "index.json").read_bytes(), index_before)

    def test_origin_is_not_inferred_from_product_directory_native_entries_or_navigation(self):
        source = self.skill / "sources" / "video-cases" / "own_products" / "fixture.md"
        source.parent.mkdir(parents=True)
        source.write_text("Test source only.", encoding="utf-8")
        body = self.body()
        body["current"]["source_case"] = {
            "library_id": "VSL-T0-099", "file_path": str(source), "tier": "T0",
            "transcript_status": "verified", "case_type": "video_analysis",
        }
        body["current"]["native_reference"] = {
            "language": "en", "market": "未核实", "duration_sec": 10,
            "scene": "产品演示", "speaker": "未核实", "audience": "工具使用者",
            "entries": [{"id": "a", "channel": "speech", "start_sec": 0, "end_sec": 2,
                         **{key: "测试内容" for key in ("original", "meaning_zh", "function", "context", "analysis", "adaptation", "boundary")}}],
        }
        status, _, result = self.save(body)
        self.assertEqual(status, 200, result)
        status, _, moved = self.request("POST", "/api/navigation/move", {
            "case_id": self.identifier, "target": "native", "previous": None,
            "revision": result["case"]["revision"],
        })
        self.assertEqual(status, 200, moved)
        self.assertNotIn("content_origin", self.detail()["current"])
        self.assertNotIn("content_origin", self.store.read_index()["cases"][0]["current"])
        self.assertIn("**内容来源：未标注**", (self.runtime / "library" / self.identifier / "case.md").read_text(encoding="utf-8"))
        self.assertEqual(self.store.search(query="overseas_creator", scope="current"), [])

    def test_unmarked_legacy_index_keeps_its_existing_shape(self):
        index = self.store.read_index()
        from case_store import INDEX_SCHEMA_VERSION
        self.assertEqual(index["schema_version"], INDEX_SCHEMA_VERSION)
        for row in index["cases"]:
            self.assertNotIn("content_origin", row["current"])
            searchable = json.loads(row["current_search_text"])
            self.assertNotIn("content_origin_label", searchable)
        fixture.SERVER.render_library(self.skill, self.runtime / "library")
        self.assertEqual(self.store.read_index(), index)
        self.assertIn("**内容来源：未标注**", (self.runtime / "library" / self.identifier / "case.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
