"""Durable review lessons, scoped retrieval and task snapshot selection."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

from creation_store import CreationError
from review_experiences import ReviewExperienceStore


class ReviewExperienceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-experiences-")
        self.addCleanup(self.temp.cleanup)
        self.store = ReviewExperienceStore(self.temp.name)
        self.source = {"review": {"product": "202H", "video_id": "7690000000000000001", "window_end": "2026-10-11",
                                  "reviewed_at": "2026-10-25T04:00:00.000000+00:00", "note": "先演示结果有初步信号。", "evidence": "preliminary"},
                       "video": {"title": "A Mirror Edge", "orders": 4, "last_data": "2026-10-18"},
                       "publications": [{"script_id": "SCR-111111111111", "script_version": 2}]}

    def body(self, **values):
        fields = {"product": "202H", "kind": "success", "dimension": "opening", "practice": "先展示磨后结果，再解释步骤。",
                  "conditions": "仅用于202H磨刀结果演示，需真实结果镜头。", "evidence_note": "来源视频4单，单条观察，不代表因果已验证。",
                  "confidence": "preliminary", "scope": "direction", "direction_id": "202H-S1", "status": "active", **values}
        return {"request_id": uuid4().hex, "revision": self.store.public()["revision"], "experience": fields}

    def add(self, **values):
        source = copy.deepcopy(self.source)
        source["review"]["product"] = values.get("product", "202H")
        return self.store.save(self.body(**values), source)

    def update(self, item, **values):
        return self.store.save({"request_id": uuid4().hex, "revision": self.store.public(status="all")["revision"],
                                "experience": {"id": item["id"], **values}})

    def test_empty_read_does_not_create_files_and_path_is_inside_creation(self):
        self.assertEqual(self.store.public()["items"], [])
        self.assertFalse(self.store.root.exists())
        self.assertEqual(self.store.path, Path(self.temp.name) / "creation" / "experiences" / "catalog.json")
        self.assertEqual(self.store.selected_for_task("202H"), [])

    def test_persists_and_source_is_frozen_independent_of_callers(self):
        source = copy.deepcopy(self.source)
        saved = self.store.save(self.body(), source)
        self.assertRegex(saved["item"]["id"], r"^EXP-[0-9a-f]{12}$")
        source["video"]["orders"] = 999
        saved["item"]["source"]["publications"][0]["script_version"] = 999
        self.store = ReviewExperienceStore(self.temp.name)
        item = self.store.public()["items"][0]
        self.assertEqual(item["source"], self.source)
        item["practice"] = "mutated reader"
        self.assertNotEqual(self.store.public()["items"][0]["practice"], "mutated reader")

    def test_optional_notes_stay_empty_without_invented_conditions_or_evidence(self):
        body = self.body()
        body["experience"].pop("conditions")
        body["experience"].pop("evidence_note")
        saved = self.store.save(body, self.source)["item"]
        self.assertEqual(saved["conditions"], "")
        self.assertEqual(saved["evidence_note"], "")
        self.assertEqual(saved["source"], self.source)
        # Reload using the canonical runtime; blanks survive persistence and selection.
        restored = ReviewExperienceStore(self.store.path.parents[2])
        selected = restored.selected_for_task("202H", "202H-S1")
        self.assertEqual(selected[0]["conditions"], "")
        self.assertEqual(selected[0]["evidence_note"], "")
        self.assertEqual(selected[0]["source"], self.source)

    def test_existing_notes_are_preserved_on_partial_edit_and_can_be_explicitly_cleared(self):
        original = self.add()["item"]
        edited = self.update(original, practice="先用真实近景展示磨后结果。")["item"]
        self.assertEqual(edited["conditions"], original["conditions"])
        self.assertEqual(edited["evidence_note"], original["evidence_note"])
        cleared = self.update(edited, conditions="  ", evidence_note="")["item"]
        self.assertEqual(cleared["conditions"], "")
        self.assertEqual(cleared["evidence_note"], "")
        self.assertEqual(cleared["history"][-1]["conditions"], original["conditions"])
        self.assertEqual(cleared["history"][-1]["evidence_note"], original["evidence_note"])
        self.assertEqual(cleared["source"], original["source"])

    def test_successful_retry_survives_restart_and_stale_revision(self):
        body = self.body()
        saved = self.store.save(body, self.source)
        self.add(practice="另一条不同做法。")
        self.store = ReviewExperienceStore(self.temp.name)
        self.assertEqual(self.store.save(body), saved)
        with self.assertRaises(CreationError) as caught:
            self.store.save({**body, "experience": {**body["experience"], "practice": "同请求不能换内容。"}}, self.source)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.public()["total"], 2)

    def test_stale_updates_do_not_overwrite_newer_versions(self):
        item = self.add()["item"]
        stale = {"request_id": uuid4().hex, "revision": self.store.public()["revision"],
                 "experience": {"id": item["id"], "conditions": "旧页面条件"}}
        self.update(item, conditions="新增更具体的使用限制。")
        with self.assertRaises(CreationError) as caught:
            self.store.save(stale)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.public()["items"][0]["conditions"], "新增更具体的使用限制。")

    def test_edit_preserves_source_and_archives_complete_prior_versions(self):
        original = self.add()["item"]
        changed = self.update(original, conditions="仅用于自然流量且有结果近景。", confidence="supported")["item"]
        self.assertEqual(changed["version"], 2)
        self.assertEqual(changed["source"], original["source"])
        self.assertEqual(changed["history"], [{key: value for key, value in original.items() if key != "history"}])
        self.assertEqual(changed["created_at"], original["created_at"])
        self.assertTrue(list((self.store.root / "backups").glob("*.json")))
        # A no-op is not another version, but its request still has a receipt.
        unchanged = self.update(changed, confidence="supported")
        self.assertTrue(unchanged["duplicate"])
        self.assertEqual(unchanged["item"]["version"], 2)

    def test_duplicates_do_not_multiply_evidence(self):
        original = self.add()
        duplicate = self.add()
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["item"], original["item"])
        self.assertEqual(duplicate["revision"], original["revision"])
        self.assertEqual(self.store.public()["total"], 1)
        with self.assertRaises(CreationError) as caught:
            self.add(conditions="同一做法的新条件应编辑原条目。")
        self.assertEqual(caught.exception.status, 409)
        self.add(practice="结果镜头要有真实可见的刃面。")
        self.add(kind="failure", practice="不能只展示整机而不展示结果。")
        self.assertEqual(self.store.public()["total"], 3)

    def test_existing_lesson_cannot_change_product_or_source(self):
        item = self.add()["item"]
        for update in ({"product": "129N"}, {"source": self.source}):
            with self.subTest(update=update), self.assertRaises(CreationError):
                self.update(item, **update)
        other_source = copy.deepcopy(self.source)
        other_source["review"]["video_id"] = "7690000000000000002"
        with self.assertRaises(CreationError):
            self.store.save({"request_id": uuid4().hex, "revision": self.store.public()["revision"],
                             "experience": {"id": item["id"], "status": "retired"}}, other_source)
        self.assertEqual(self.store.public()["items"][0]["version"], 1)

    def test_retire_and_restore_keep_history_and_change_future_selection(self):
        item = self.add()["item"]
        frozen = self.store.selected_for_task("202H", "202H-S1")
        retired = self.update(item, status="retired")["item"]
        self.assertEqual(self.store.public()["total"], 0)
        self.assertEqual(self.store.public(status="retired")["total"], 1)
        self.assertEqual(self.store.selected_for_task("202H", "202H-S1"), [])
        restored = self.update(retired, status="active")["item"]
        self.assertEqual(restored["version"], 3)
        self.assertEqual([row["version"] for row in restored["history"]], [1, 2])
        self.assertEqual(frozen[0]["version"], 1)
        self.assertEqual(frozen[0]["status"], "active")

    def test_search_includes_conditions_evidence_dimension_and_source(self):
        item = self.add()["item"]
        for query in ("磨后结果", "真实结果镜头", "单条观察", "OPENING", "7690000000000000001", "mirror edge"):
            with self.subTest(query=query):
                self.assertEqual([row["id"] for row in self.store.public(query=query)["items"]], [item["id"]])
        self.assertEqual(self.store.public(query="not present")["total"], 0)
        self.assertEqual(self.store.public(product="129N")["total"], 0)
        self.assertEqual(self.store.public(direction_id="202H-S2")["total"], 0)
        self.assertEqual(self.store.public(kind="failure")["total"], 0)
        self.assertEqual(self.store.public(product="SHARPAL 202H")["total"], 1)

    def test_selection_is_scoped_to_product_and_exact_direction(self):
        own = self.add()["item"]
        self.add(direction_id="202H-S2", practice="S2专用经验。")
        general = self.add(scope="product", direction_id="", practice="全产品通用经验。")["item"]
        self.add(product="129N", direction_id="129N-S1", practice="其他产品专用经验。")
        selected = self.store.selected_for_task("SHARPAL 202H", "202H-S1")
        self.assertEqual({row["id"] for row in selected}, {own["id"], general["id"]})
        self.assertEqual([row["id"] for row in self.store.selected_for_task("202H")], [general["id"]])
        self.assertEqual([row["id"] for row in self.store.selected_for_task("202H", "no-such-direction")], [general["id"]])
        self.assertNotIn("history", selected[0])
        self.assertEqual(self.store.selected_for_task("202H", limit=0), [])

    def test_selection_balances_success_and_failure_and_prefers_supported(self):
        for index in range(10):
            self.add(practice=f"成功做法{index}", confidence="supported" if index == 0 else "preliminary")
        failure = self.add(kind="failure", practice="应避免的具体做法。", confidence="supported")["item"]
        selected = self.store.selected_for_task("202H", "202H-S1", limit=4)
        self.assertEqual(len(selected), 4)
        self.assertIn(failure["id"], [row["id"] for row in selected])
        self.assertEqual([row["confidence"] for row in selected[:2]], ["supported", "supported"])
        self.assertEqual(selected, self.store.selected_for_task("202H", "202H-S1", limit=4))

    def test_source_filter_runs_before_limit_and_receives_isolated_item(self):
        retained = self.add(practice="仍可引用。", confidence="preliminary")["item"]
        for index in range(3):
            self.add(practice=f"来源已过期{index}。", confidence="supported")
        seen = []

        def current(item):
            seen.append(item["id"])
            item["source"]["video"]["orders"] = 999
            return item["id"] == retained["id"]

        selected = self.store.selected_for_task("202H", "202H-S1", limit=1, source_filter=current)
        self.assertEqual(len(seen), 4)
        self.assertEqual([item["id"] for item in selected], [retained["id"]])
        self.assertEqual(selected[0]["source"]["video"]["orders"], 4)
        self.assertTrue(all(row["source"]["video"]["orders"] == 4 for row in self.store.public()["items"]))

    def test_task_snapshots_do_not_change_after_edit_or_mutating_reader(self):
        original = self.add()["item"]
        frozen = self.store.selected_for_task("202H", "202H-S1")
        self.update(original, evidence_note="新增反例，原判断需要降级。", confidence="insufficient")
        self.assertEqual(frozen[0]["evidence_note"], original["evidence_note"])
        self.assertEqual(frozen[0]["confidence"], "preliminary")
        frozen[0]["source"]["review"]["note"] = "reader mutated"
        self.assertEqual(self.store.public()["items"][0]["source"], self.source)

    def test_manual_reference_value_filters_before_limit_and_freezes_selection(self):
        values = {}
        for value in ("weak", "medium", "high", "none", ""):
            row = self.add(practice=f"分类示例 {value or '待分类'}", confidence="supported")["item"]
            values[row["id"]] = value
        before = self.store.read()

        def reference(item):
            # Enrichment cannot mutate the stored lesson or its original source.
            item["source"]["review"]["note"] = "reader mutation"
            return values[item["id"]]

        selected = self.store.selected_for_task("202H", "202H-S1", limit=3, reference_value_for_item=reference)
        self.assertEqual([row["reference_value"] for row in selected], ["high", "medium", "weak"])
        self.assertEqual(self.store.read(), before)
        values.update(dict.fromkeys(values, "none"))
        self.assertEqual(self.store.selected_for_task("202H", "202H-S1", reference_value_for_item=reference), [])
        self.assertEqual(selected[0]["reference_value"], "high")
        self.assertNotEqual(selected[0]["source"]["review"]["note"], "reader mutation")

    def test_reference_value_does_not_drop_failure_lessons_or_upgrade_evidence(self):
        for index in range(6):
            self.add(practice=f"值得参考的成功做法{index}")
        failure = self.add(kind="failure", practice="仍值得记住的失败教训", confidence="insufficient")["item"]
        selected = self.store.selected_for_task("202H", "202H-S1", limit=2,
            reference_value_for_item=lambda item: "weak" if item["kind"] == "failure" else "high")
        self.assertEqual([row["kind"] for row in selected], ["success", "failure"])
        self.assertEqual(selected[1]["id"], failure["id"])
        self.assertEqual(selected[1]["confidence"], "insufficient")

    def test_invalid_reference_value_resolvers_are_rejected(self):
        self.add()
        for resolver in ("invalid", lambda item: None, lambda item: "certain"):
            with self.subTest(resolver=resolver), self.assertRaises(CreationError):
                self.store.selected_for_task("202H", "202H-S1", reference_value_for_item=resolver)

    def test_validation_rejects_missing_scope_and_untrusted_source(self):
        variants = ({"practice": ""}, {"conditions": "a" * 3001}, {"evidence_note": "a" * 4001}, {"confidence": "certain"},
                    {"scope": "direction", "direction_id": ""}, {"kind": "mixed"}, {"product": "all"},
                    {"status": "deleted"}, {"practice": "a" * 3001}, {"source": self.source})
        for values in variants:
            with self.subTest(values=values), self.assertRaises(CreationError):
                self.store.save(self.body(**values), self.source)
        for source in (None, {}, {"review": {}}, {**self.source, "review": {**self.source["review"], "product": "129N"}},
                       {**self.source, "review": {**self.source["review"], "window_end": "2026-02-31"}},
                       {**self.source, "review": {**self.source["review"], "reviewed_at": "2026-10-25"}},
                       {**self.source, "video": {"orders": float("nan")}}):
            with self.subTest(source=source), self.assertRaises(CreationError):
                self.store.save(self.body(), source)
        self.assertFalse(self.store.path.exists())

    def test_invalid_filters_and_limits_are_rejected(self):
        for kwargs in ({"kind": "mixed"}, {"status": "deleted"}, {"query": "a" * 501}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CreationError):
                self.store.public(**kwargs)
        for kwargs in ({"limit": -1}, {"limit": 21}, {"limit": True}, {"source_filter": "not-callable"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CreationError):
                self.store.selected_for_task("202H", **kwargs)

    def test_same_revision_concurrent_saves_have_one_winner(self):
        barrier = threading.Barrier(2)
        first, second = self.body(practice="第一条。"), self.body(practice="第二条。")

        def save(body):
            barrier.wait(timeout=5)
            try:
                self.store.save(body, self.source)
                return 200
            except CreationError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(save, (first, second))), [200, 409])
        self.assertEqual(self.store.public()["total"], 1)

    def test_corrupt_catalog_is_not_replaced_and_external_edits_invalidate_cache(self):
        self.add()
        valid = self.store.path.read_bytes()
        self.store.path.write_text('{"items": []}', encoding="utf-8")
        corrupt = self.store.path.read_bytes()
        with self.assertRaises(CreationError) as caught:
            self.store.public()
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(self.store.path.read_bytes(), corrupt)
        self.store.path.write_bytes(valid)
        self.assertEqual(self.store.public()["total"], 1)
        data = json.loads(valid)
        data["items"][0]["confidence"] = "invalid"
        self.store.path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(CreationError):
            self.store.public()


if __name__ == "__main__":
    unittest.main()
