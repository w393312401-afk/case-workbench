"""Completed conversation imports preserve script text and never run a model."""
import copy
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_server as fixture
from test_ai_tasks import FakeBridge
from test_direction_catalog import install_points
from creation_store import CreationError
from variant_parse import parse_variants
from workbench_completed_import import import_completed_task


CANDIDATE = "\n\n".join(f"""**变体 #{number} · 对话稿 {number}**

**口播（配音用）**
```
Hold the knife {number}.
Follow the guide {number}.
```
**对应中文**
```
夹好刀具 {number}。
沿着导杆 {number}。
```
**意群表**
| # | 口播 | 画面要证明什么 | 素材标签 | 证据 |
|---|---|---|---|---|
| 1 | Hold the knife {number}. | 夹牢刀具 {number} | 装夹 | 实拍 |
| 2 | Follow the guide {number}. | 导杆引导 {number} | 研磨 | 展示 |
""" for number in range(1, 3))


class CompletedTaskImportTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def body(self):
        app = self.server.app
        install_points(self.skill)
        catalog = app.direction_catalog("202H")
        direction = app.save_direction_catalog("direction", {
            "revision": catalog["revision"], "request_id": uuid4().hex,
            "direction": {"product": "202H", "selling_point_id": catalog["selling_points"][0]["id"],
                          "concern": "装夹步骤", "closing": "核对适用刀型"}})["direction"]
        bindings = []
        for variant in parse_variants(CANDIDATE, loose=False):
            script = app.save_script({"revision": app.creation.public()["revision"], "request_id": uuid4().hex,
                "script": {"product": "202H", "direction_id": direction["direction_id"], "title": variant["label"],
                           "text": "\n".join(line["spoken"] for line in variant["lines"]),
                           "translation_zh": "\n".join(line["zh"] for line in variant["lines"]), "confirmed": True}})["script"]
            bindings.append({"script_id": script["id"], "variant_index": variant["index"]})
        return {"request_id": uuid4().hex, "revision": app.creation.public()["revision"], "confirmed": True,
                "product": "202H", "direction_id": direction["direction_id"], "title": "对话稿导入",
                "instruction": "按卖点正推执行变体", "output": CANDIDATE,
                "origin_label": "Codex 对话", "origin_reference": "用户提供的完整稿件", "bindings": bindings}

    def test_links_existing_scripts_and_inherits_exact_groups_without_execution(self):
        body, app = self.body(), self.server.app
        old = {item["id"]: item for item in app.creation.public()["scripts"]}
        bridge = app.ai.bridge = FakeBridge()
        with patch.object(app.ai._queue, "put", side_effect=AssertionError("import must not queue")):
            result = import_completed_task(app, body)
        self.assertEqual(bridge.calls, [])
        task = result["task"]
        self.assertEqual((task["status"], task["busy"], task["rounds"][0]["kind"]), ("completed", False, "external_import"))
        self.assertIsNone(task["rounds"][0]["model"])
        self.assertIsNone(task["rounds"][0]["stages"])
        self.assertEqual(task["rounds"][0]["output"], CANDIDATE)
        self.assertEqual(len(app.ai.list_tasks(product="202H", direction_id=body["direction_id"])), 1)
        for script, variant in zip(result["scripts"], parse_variants(CANDIDATE, loose=False)):
            for field in ("id", "family_id", "version", "parent_id", "text", "translation_zh", "status"):
                self.assertEqual(script[field], old[script["id"]][field])
            label, records, note = app._spoken_records(script)
            self.assertEqual((label, records, note), (variant["label"], variant["lines"], ""))
            self.assertEqual(script["source"]["task_id"], task["task_id"])

    def test_http_route_imports_task_and_verified_script_sources(self):
        body, app = self.body(), self.server.app
        app.ai.bridge = FakeBridge()
        status, _, result = self.request("POST", "/api/ai/tasks/import", body)
        self.assertEqual(status, 200, result)
        task = result["task"]
        self.assertEqual(task["rounds"][0]["kind"], "external_import")
        self.assertEqual(task["rounds"][0]["output"], CANDIDATE)
        self.assertEqual(app.ai.bridge.calls, [])
        for binding in body["bindings"]:
            status, _, detail = self.request("GET", "/api/scripts/" + binding["script_id"])
            self.assertEqual(status, 200, detail)
            source = detail["script"]["source"]
            self.assertEqual((source["task_id"], source["round_id"], source["variant_index"]),
                             (task["task_id"], task["rounds"][0]["round_id"], binding["variant_index"]))
            self.assertEqual(source["candidate_text"], CANDIDATE)

    def test_mismatched_english_or_chinese_leaves_no_task_or_journal(self):
        body, app = self.body(), self.server.app
        for old, new in (("Hold the knife 1.", "Change the knife 1."), ("夹好刀具 1。", "更改刀具 1。")):
            changed = {**body, "request_id": uuid4().hex, "output": CANDIDATE.replace(old, new)}
            with self.assertRaises(CreationError) as raised:
                import_completed_task(app, changed)
            self.assertEqual(raised.exception.status, 409)
        self.assertEqual(app.ai.list_tasks(product="202H"), [])
        self.assertFalse(list((app.creation.root / "operations").glob("*.json")))

    def test_retry_is_idempotent_and_reusing_request_for_changed_body_fails(self):
        body, app = self.body(), self.server.app
        first = import_completed_task(app, body)
        revision = app.creation.public()["revision"]
        second = import_completed_task(app, copy.deepcopy(body))
        self.assertTrue(second["replayed"])
        self.assertEqual(first["task"]["task_id"], second["task"]["task_id"])
        self.assertEqual(app.creation.public()["revision"], revision)
        self.assertEqual(len(app.ai.list_tasks(product="202H")), 1)
        with self.assertRaises(CreationError) as raised:
            import_completed_task(app, {**body, "title": "另一请求"})
        self.assertEqual(raised.exception.status, 409)

    def test_retry_after_task_write_recovers_without_rewriting_task(self):
        body, app = self.body(), self.server.app
        with patch.object(app.creation, "mutate", side_effect=OSError("commit interrupted")):
            with self.assertRaises(OSError):
                import_completed_task(app, body)
        task_id = app.ai.list_tasks(product="202H")[0]["task_id"]
        self.assertTrue(all(not item.get("source") for item in app.creation.public()["scripts"]))
        with patch.object(app.ai, "_write", side_effect=AssertionError("task already exists")):
            result = import_completed_task(app, body)
        self.assertEqual(result["task"]["task_id"], task_id)
        self.assertEqual(result["task"]["revision"], 1)
        self.assertEqual(len(app.ai.list_tasks(product="202H")), 1)

    def test_catalog_changes_after_interruption_are_not_automatically_merged(self):
        body, app = self.body(), self.server.app
        with patch.object(app.creation, "mutate", side_effect=OSError("commit interrupted")):
            with self.assertRaises(OSError):
                import_completed_task(app, body)
        app.creation.mutate("script_status", {"request_id": uuid4().hex,
            "revision": app.creation.public()["revision"],
            "script_status": {"script_id": body["bindings"][0]["script_id"], "status": "filmed"}})
        with self.assertRaises(CreationError) as raised:
            import_completed_task(app, body)
        self.assertEqual(raised.exception.status, 409)
        self.assertTrue(all(not item.get("source") for item in app.creation.public()["scripts"]))

    def test_committed_links_recover_when_final_journal_write_was_lost(self):
        body, app = self.body(), self.server.app
        replace = app.creation._replace

        def fail_completion(path, value):
            if value.get("kind") == "completed_task_import" and value.get("complete"):
                raise OSError("final journal interrupted")
            return replace(path, value)

        with patch.object(app.creation, "_replace", side_effect=fail_completion):
            with self.assertRaises(OSError):
                import_completed_task(app, body)
        before = app.creation.public()
        self.assertTrue(all(item.get("source") for item in before["scripts"]))
        result = import_completed_task(app, body)
        self.assertEqual(result["catalog"]["revision"], before["revision"])
        self.assertEqual(len(app.ai.list_tasks(product="202H")), 1)

    def test_retry_does_not_resurrect_deleted_import_task(self):
        body, app = self.body(), self.server.app
        with patch.object(app.creation, "mutate", side_effect=OSError("commit interrupted")):
            with self.assertRaises(OSError):
                import_completed_task(app, body)
        task = app.ai.list_tasks(product="202H")[0]
        app.ai.delete_tasks({"tasks": [{"task_id": task["task_id"], "revision": task["revision"]}]})
        with self.assertRaises(CreationError) as raised:
            import_completed_task(app, body)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(app.ai.list_tasks(product="202H"), [])

    def test_missing_publication_receipt_cannot_resurrect_deleted_task(self):
        body, app = self.body(), self.server.app
        replace = app.creation._replace

        def fail_published(path, value):
            if value.get("kind") == "completed_task_import" and value.get("task_published") and "sources" not in value:
                raise OSError("publication receipt interrupted")
            return replace(path, value)

        with patch.object(app.creation, "_replace", side_effect=fail_published):
            with self.assertRaises(OSError):
                import_completed_task(app, body)
        task = app.ai.list_tasks(product="202H")[0]
        app.ai.delete_tasks({"tasks": [{"task_id": task["task_id"], "revision": task["revision"]}]})
        with self.assertRaises(CreationError) as raised:
            import_completed_task(app, body)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(app.ai.list_tasks(product="202H"), [])


if __name__ == "__main__":
    unittest.main()
