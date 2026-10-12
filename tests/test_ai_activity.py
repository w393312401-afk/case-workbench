"""Activity records observe disposable AI tasks without affecting their execution."""
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_ai_tasks as fixture
from test_ai_tasks import FakeBridge, install_review_fixture
from ai_tasks import AITaskManager
from task_activity import ActivityStore


class AIActivityTests(unittest.TestCase):
    setUp = fixture.TaskTests.setUp
    tearDown = fixture.TaskTests.tearDown
    body = fixture.TaskTests.body
    wait = fixture.TaskTests.wait
    path = fixture.TaskTests.path

    def new_manager(self, bridge=None):
        bridge = bridge or FakeBridge()
        self.bridges.append(bridge)
        self.activity = ActivityStore(self.runtime)
        manager = AITaskManager(self.store, self.runtime, self.lock, bridge=bridge, activity=self.activity)
        self.managers.append(manager)
        return manager

    def test_replayed_request_and_continuation_have_one_record_per_round(self):
        body = self.body()
        first = self.wait(self.manager.create(self.identifier, body)["task_id"])
        self.manager.create(self.identifier, body)
        self.assertEqual(len(self.activity.feed()["items"]), 1)
        run = self.activity.feed()["items"][0]
        self.assertEqual(run["status"], "completed")
        self.assertIn("?round=" + first["rounds"][0]["round_id"], run["url"])
        before = self.path(first["task_id"]).read_bytes()
        self.activity.read({"event_ids": [run["event_id"]]})
        self.assertEqual(before, self.path(first["task_id"]).read_bytes())
        second = self.wait(self.manager.continue_task(first["task_id"], {
            "request_id": uuid4().hex, "task_revision": first["revision"],
            "instruction": "继续核对。", "skill_selection": "none"})["task_id"])
        records = self.activity.feed()["items"]
        self.assertEqual(len(records), 2)
        self.assertEqual(len({item["source_id"] for item in records}), 2)
        self.assertEqual(self.activity.feed()["counts"]["unread"], 1)
        deleted = self.manager.delete_tasks({"tasks": [{"task_id": second["task_id"], "revision": second["revision"]}]})
        self.assertEqual(deleted["deleted"], [second["task_id"]])
        self.assertEqual(self.activity.feed()["items"], [])

    def test_running_cancel_has_no_terminal_event_until_worker_stops(self):
        bridge = FakeBridge(blocked=True, ignore_cancel=True)
        manager = self.new_manager(bridge)
        task = manager.create(self.identifier, self.body())
        self.assertTrue(bridge.started.wait(3))
        manager.cancel(task["task_id"])
        run = self.activity.feed()["items"][0]
        self.assertEqual(run["status"], "stopping")
        self.assertIsNone(run["event_id"])
        bridge.gate.set()
        self.wait(task["task_id"], manager)
        run = self.activity.feed()["items"][0]
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(self.activity.feed()["counts"]["unread"], 0)

    def test_stage_logs_are_fixed_and_completion_follows_saved_result_and_stopped_worker(self):
        install_review_fixture(self.skill)
        observed = []
        original = self.activity.record
        def record(run_id, **fields):
            if fields.get("status") == "completed":
                task = self.manager._read(fields["owner_id"])
                observed.append((task["status"], bool(task["rounds"][-1]["output"]), self.manager._executing))
            return original(run_id, **fields)
        with patch.object(self.activity, "record", side_effect=record):
            task = self.wait(self.manager.create(self.identifier, self.body("variants", "SECRET_INSTRUCTION"))["task_id"])
        self.assertEqual(observed, [("completed", True, None)])
        run = self.activity.feed()["items"][0]
        logs = self.activity.logs(run["run_id"])["logs"]
        self.assertTrue({"queued", "draft", "review", "finished"}.issubset({row["phase"] for row in logs}))
        self.assertEqual(sum(row["status"] == "completed" for row in logs), 1)
        self.assertNotIn("SECRET_INSTRUCTION", json.dumps(logs, ensure_ascii=False))
        self.assertNotIn("测试助手已接单", json.dumps(logs, ensure_ascii=False))

    def test_failure_summary_does_not_copy_secrets_and_log_failure_does_not_retry(self):
        manager = self.new_manager(FakeBridge(error=RuntimeError("TOKEN_SECRET")))
        task = self.wait(manager.create(self.identifier, self.body())["task_id"], manager)
        self.assertEqual(task["status"], "failed")
        run = self.activity.feed()["items"][0]
        self.assertEqual(run["status"], "failed")
        self.assertNotIn("TOKEN_SECRET", json.dumps(self.activity.logs(run["run_id"]), ensure_ascii=False))
        with patch.object(self.manager.activity, "record", side_effect=OSError("database unavailable")):
            task = self.wait(self.manager.create(self.identifier, self.body())["task_id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(len(self.bridges[0].calls), 1)

    def test_result_write_failure_never_announces_completion(self):
        write = self.manager._write
        def fail_terminal(task, **kwargs):
            if task["status"] in {"completed", "failed"}:
                raise OSError("cannot save result")
            return write(task, **kwargs)
        with patch.object(self.manager, "_write", side_effect=fail_terminal):
            task = self.manager.create(self.identifier, self.body())
            self.manager._queue.join()
        records = self.activity.feed()["items"]
        self.assertEqual(records[0]["status"], "needs_attention")
        self.assertFalse(any(row["status"] == "completed" for row in self.activity.logs(records[0]["run_id"])["logs"]))
        self.assertEqual(len(self.bridges[0].calls), 1)


if __name__ == "__main__":
    unittest.main()
