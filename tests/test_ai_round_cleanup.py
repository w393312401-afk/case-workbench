"""Conversation cleanup never touches real tasks, adopted scripts, or production files."""
from __future__ import annotations

import copy
import json
import unittest
from uuid import uuid4

from ai_tasks import AITaskError
import test_ai_tasks as fixture
import test_ai_http as http


class RoundCleanupTests(unittest.TestCase):
    setUp = fixture.TaskTests.setUp
    tearDown = fixture.TaskTests.tearDown
    new_manager = fixture.TaskTests.new_manager
    body = fixture.TaskTests.body
    wait = fixture.TaskTests.wait
    path = fixture.TaskTests.path

    def history(self, count=4, *, action="variants"):
        task = self.manager.create(self.identifier, {**self.body(action), "skill_selection": "none"})
        self.wait(task["task_id"])
        with self.manager.lock:
            raw = self.manager._read(task["task_id"])
            first = raw["rounds"][0]
            raw["rounds"] = [{**copy.deepcopy(first), "round_id": "AIR-" + uuid4().hex[:12],
                              "instruction": f"历史要求 {index}", "output": f"原始候选 {index}"}
                             for index in range(count)]
            self.manager._write(raw)
        return self.manager.get(task["task_id"])

    def clean(self, task, action="delete_unused", **extra):
        body = {"request_id": uuid4().hex, "task_revision": task["revision"], "action": action, **extra}
        return self.manager.cleanup_rounds(task["task_id"], body)

    def test_delete_is_durable_reversible_and_keeps_original_round_numbers(self):
        task = self.history()
        ids = [turn["round_id"] for turn in task["rounds"]]
        self.manager.round_cleanup_references = lambda: [{"task_id": task["task_id"], "round_id": ids[2]}]
        body = {"request_id": uuid4().hex, "task_revision": task["revision"], "action": "delete_unused"}
        removed = self.manager.cleanup_rounds(task["task_id"], body)
        self.assertEqual(removed["deleted_count"], 3)
        self.assertEqual([turn["number"] for turn in removed["task"]["rounds"]], [3])
        self.assertTrue(removed["task"]["rounds"][0]["cleanup"]["protected"])
        self.assertTrue(self.manager.cleanup_rounds(task["task_id"], body)["replayed"])
        durable = json.loads(self.path(task["task_id"]).read_text(encoding="utf-8"))
        self.assertEqual(len(durable["rounds"]), 4)
        self.assertEqual(durable["rounds"][0]["output"], "原始候选 0")
        reread = self.manager.get(task["task_id"])
        self.assertEqual(reread["round_cleanup"]["deleted_count"], 3)
        restored = self.clean(reread, "undo", batch_id=reread["round_cleanup"]["undo_batch"]["id"])
        self.assertEqual(restored["restored_count"], 3)
        self.assertEqual([turn["round_id"] for turn in restored["task"]["rounds"]], ids)
        self.assertIsNone(restored["task"]["round_cleanup"]["undo_batch"])

    def test_protection_includes_archived_versions_production_and_source_ancestors(self):
        task = self.history(count=5)
        ids = [turn["round_id"] for turn in task["rounds"]]
        with self.manager.lock:
            raw = self.manager._read(task["task_id"])
            raw["rounds"][3].update(kind="manual_edit", manual_edit={"source_round_id": ids[1], "variant_index": 1})
            raw["rounds"][1].update(kind="format", format_source=ids[0])
            self.manager._write(raw)
        # The provider deliberately includes every lifecycle state/version; no status filter belongs here.
        self.manager.round_cleanup_references = lambda: [
            {"task_id": task["task_id"], "round_id": ids[3], "variant_index": 1, "archived": True},
            {"task_id": task["task_id"], "round_id": ids[4], "kind": "production"}]
        latest = self.manager.get(task["task_id"])
        self.assertEqual(latest["round_cleanup"]["eligible_round_ids"], [ids[2]])
        self.assertEqual(latest["rounds"][0]["cleanup"]["reason"], "采用稿来源")
        self.assertEqual(self.clean(latest)["deleted_count"], 1)

    def test_carried_over_adopted_variant_protects_manual_edit_descendant(self):
        task = self.history(count=3)
        ids = [turn["round_id"] for turn in task["rounds"]]
        with self.manager.lock:
            raw = self.manager._read(task["task_id"])
            raw["rounds"][1].update(kind="manual_edit", manual_edit={"source_round_id": ids[0], "variant_index": 1})
            raw["rounds"][2].update(kind="manual_edit", manual_edit={"source_round_id": ids[1], "variant_index": 0})
            self.manager._write(raw)
        self.manager.round_cleanup_references = lambda: [{"task_id": task["task_id"], "round_id": ids[0], "variant_index": 0}]
        detail = self.manager.get(task["task_id"])
        self.assertEqual(detail["round_cleanup"]["eligible_round_ids"], [ids[2]])

    def test_stale_busy_and_unreadable_references_fail_without_writing(self):
        task = self.history()
        before = self.path(task["task_id"]).read_bytes()
        for revision in (task["revision"] - 1, True):
            with self.assertRaises(AITaskError):
                self.manager.cleanup_rounds(task["task_id"], {"request_id": uuid4().hex, "task_revision": revision, "action": "delete_unused"})
        self.manager._executing.add(task["task_id"])
        try:
            with self.assertRaises(AITaskError):
                self.clean(task)
        finally:
            self.manager._executing.discard(task["task_id"])
        def unavailable():
            raise OSError("temporary catalog failure")
        self.manager.round_cleanup_references = unavailable
        self.assertFalse(self.manager.get(task["task_id"])["round_cleanup"]["available"])
        with self.assertRaises(AITaskError):
            self.clean(task)
        self.assertEqual(self.path(task["task_id"]).read_bytes(), before)

    def test_deleted_rounds_cannot_be_adopted_edited_formatted_or_retried(self):
        task = self.history(count=1)
        round_id = task["rounds"][0]["round_id"]
        with self.manager.lock:
            raw = self.manager._read(task["task_id"])
            raw["status"] = raw["rounds"][0]["status"] = "failed"
            self.manager._write(raw)
        deleted = self.clean(self.manager.get(task["task_id"]))["task"]
        self.assertEqual((deleted["status"], deleted["rounds"]), ("completed", []))
        self.assertEqual(self.manager.list_tasks()[0]["round_count"], 0)
        with self.assertRaises(AITaskError):
            self.manager.candidate(task["task_id"], round_id, deleted["revision"])
        with self.assertRaises(AITaskError):
            self.manager.continue_task(task["task_id"], {"request_id": uuid4().hex, "task_revision": deleted["revision"], "instruction": ""})
        with self.assertRaises(AITaskError):
            self.manager.continue_task(task["task_id"], {"request_id": uuid4().hex, "task_revision": deleted["revision"], "format_round_id": round_id})
        with self.assertRaises(AITaskError):
            self.manager.continue_task(task["task_id"], {"request_id": uuid4().hex, "task_revision": deleted["revision"], "manual_edit": {"round_id": round_id}})
        self.manager.continue_task(task["task_id"], {"request_id": uuid4().hex, "task_revision": deleted["revision"], "instruction": "新的要求", "skill_selection": "none"})
        completed = self.wait(task["task_id"])
        self.assertEqual(completed["rounds"][0]["number"], 2)
        self.assertNotIn("原始候选 0", self.bridges[0].calls[-1]["prompt"])

    def test_undo_checks_batch_revision_busy_state_and_request_identity(self):
        task = self.history()
        removed = self.clean(task)["task"]
        batch = removed["round_cleanup"]["undo_batch"]
        with self.assertRaises(AITaskError):
            self.clean(removed, "undo", batch_id="wrong")
        self.manager._executing.add(task["task_id"])
        try:
            with self.assertRaises(AITaskError):
                self.clean(removed, "undo", batch_id=batch["id"])
        finally:
            self.manager._executing.discard(task["task_id"])
        body = {"request_id": uuid4().hex, "task_revision": removed["revision"], "action": "undo", "batch_id": batch["id"]}
        self.manager.cleanup_rounds(task["task_id"], body)
        self.assertTrue(self.manager.cleanup_rounds(task["task_id"], body)["replayed"])
        with self.assertRaises(AITaskError):
            self.manager.cleanup_rounds(task["task_id"], {**body, "action": "delete_unused"})

    def test_reference_read_failure_does_not_resurrect_deleted_conversations(self):
        task = self.history()
        removed = self.clean(task)["task"]
        def unavailable():
            raise OSError("temporary catalog failure")
        self.manager.round_cleanup_references = unavailable
        failed_read = self.manager.get(task["task_id"])
        self.assertEqual(failed_read["rounds"], [])
        self.assertFalse(failed_read["round_cleanup"]["available"])
        self.assertEqual(failed_read["round_cleanup"]["deleted_count"], 4)
        self.assertEqual(failed_read["round_cleanup"]["undo_batch"], removed["round_cleanup"]["undo_batch"])

    def test_nonwriting_task_is_not_eligible(self):
        task = self.history(action="review")
        self.assertNotIn("round_cleanup", task)
        with self.assertRaises(AITaskError):
            self.clean(task)


class RoundCleanupHTTPTests(unittest.TestCase):
    setUp = http.AIHTTPTests.setUp
    close = http.AIHTTPTests.close
    request = http.AIHTTPTests.request
    body = http.AIHTTPTests.body

    def test_endpoint_requires_auth_and_preserves_real_catalog_references(self):
        status, result = self.request("POST", "/api/ai/tasks", {**self.body("variants"), "skill_selection": "none"})
        self.assertEqual(status, 200, result)
        task_id = result["task"]["task_id"]
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = self.server.app.ai.get(task_id)
            if not task["busy"]:
                break
            time.sleep(.01)
        self.assertFalse(task["busy"])
        body = {"request_id": uuid4().hex, "task_revision": task["revision"], "action": "delete_unused"}
        path = f"/api/ai/tasks/{task_id}/round-cleanup"
        self.assertEqual(self.request("POST", path, body, authenticated=False)[0], 403)
        status, deleted = self.request("POST", path, body)
        self.assertEqual(status, 200, deleted)
        batch = deleted["task"]["round_cleanup"]["undo_batch"]
        status, restored = self.request("POST", path, {"request_id": uuid4().hex, "task_revision": deleted["task"]["revision"], "action": "undo", "batch_id": batch["id"]})
        self.assertEqual(status, 200, restored)
        self.assertEqual(restored["restored_count"], 1)
        current = restored["task"]
        status, adopted = self.request("POST", "/api/scripts", {
            "request_id": uuid4().hex, "revision": self.request("GET", "/api/creation")[1]["revision"],
            "script": {"product": "202H", "title": "临时采用稿", "text": "Keep the same angle.", "confirmed": True},
            "source": {"task_id": task_id, "round_id": current["rounds"][0]["round_id"], "task_revision": current["revision"]}})
        self.assertEqual(status, 200, adopted)
        status, archived = self.request("POST", f"/api/scripts/{adopted['script']['id']}/status", {
            "request_id": uuid4().hex, "revision": self.request("GET", "/api/creation")[1]["revision"], "script_status": {"archived": True}})
        self.assertEqual(status, 200, archived)
        protected = self.request("GET", f"/api/ai/tasks/{task_id}")[1]["task"]
        self.assertEqual(protected["round_cleanup"]["eligible_round_ids"], [])
        self.assertEqual(protected["rounds"][0]["cleanup"]["reason"], "已采用")
        self.assertEqual(self.request("POST", path, {"request_id": uuid4().hex, "task_revision": protected["revision"], "action": "delete_unused"})[0], 409)
