"""Phase four HTTP integration uses isolated data; no real AI service is called."""
import copy
import json
import time
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_server as fixture
from test_ai_tasks import FakeBridge
from test_direction_catalog import install_points
from test_review_store import build_sources


class CreationHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    detail = fixture.WorkbenchHTTPTests.detail

    def catalog(self):
        status, _, data = self.request("GET", "/api/creation?product=202H")
        self.assertEqual(status, 200, data)
        return data

    def materials(self):
        status, _, data = self.request("GET", "/api/materials")
        self.assertEqual(status, 200, data)
        return data

    def script(self, **values):
        body = {"revision": self.catalog()["revision"], "request_id": uuid4().hex,
                "script": {"product": "202H", "title": "独立文案", "text": "An explicitly chosen script.", "translation_zh": "明确选中的正文。", "confirmed": True, **values}}
        status, _, data = self.request("POST", "/api/scripts", body)
        self.assertEqual(status, 200, data)
        return data["script"]

    def direction(self):
        install_points(self.skill)
        catalog = self.server.app.direction_catalog("202H")
        point = catalog["selling_points"][0]
        return self.server.app.save_direction_catalog("direction", {"revision": catalog["revision"], "request_id": uuid4().hex,
            "direction": {"product": "202H", "selling_point_id": point["id"], "concern": "装夹步骤", "closing": "核对适用刀型"}})["direction"]

    def status(self, script, **values):
        body = {"revision": self.catalog()["revision"], "request_id": uuid4().hex, "script_status": values}
        return self.request("POST", "/api/scripts/" + script["id"] + "/status", body)

    def test_script_lifecycle_direction_views_and_default_case_creation(self):
        before = self.store.read_case(self.identifier)
        direction = self.direction()
        item = self.script(direction_id=direction["direction_id"])
        self.assertEqual(item["script_id"], item["id"])
        status, _, result = self.status(item, status="published", video_id="7999999999999999991", published_on="2026-10-08")
        self.assertEqual(status, 200, result)
        status, _, detail = self.request("GET", "/api/scripts/" + item["id"])
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["sales_data"]["metrics"]["video_id"], "7999999999999999991")
        body = {"revision": self.catalog()["revision"], "request_id": uuid4().hex, "conclusion": "用户记录此次方向观察。"}
        status, _, result = self.request("POST", "/api/scripts/" + item["id"] + "/case", body)
        self.assertEqual(status, 200, result)
        created = result["case"]
        self.assertNotEqual(created["case_id"], self.identifier)
        self.assertEqual(self.store.read_case(self.identifier), before)
        self.assertEqual(created["current"]["production"]["script_version"], item["id"])
        self.assertEqual(created["current"]["translation_zh"]["source_text"], item["text"])
        self.assertNotIn("assets", created["current"])
        replay = self.request("POST", "/api/scripts/" + item["id"] + "/case", body)
        self.assertEqual(replay[0], 200, replay[2])
        self.assertEqual(len(self.store.read_index()["cases"]), 2)
        duplicate = {**body, "revision": self.catalog()["revision"], "request_id": uuid4().hex}
        self.assertEqual(self.request("POST", "/api/scripts/" + item["id"] + "/case", duplicate)[0], 409)
        status, _, direction_view = self.request("GET", "/api/direction-catalog/" + direction["direction_id"] + "?product=202H")
        self.assertEqual(status, 200, direction_view)
        self.assertTrue(any(entry.get("script_id") == item["id"] for entry in direction_view["scripts"]))
        self.assertIn("creation_revision", direction_view)

    def test_writeback_guard_prevents_reference_video_wrongly_becoming_new_script(self):
        item = self.script()
        before = self.detail()
        body = {"revision": self.catalog()["revision"], "request_id": uuid4().hex, "conclusion": "新稿结论", "case_id": self.identifier, "case_revision": before["revision"]}
        status, _, response = self.request("POST", "/api/scripts/" + item["id"] + "/case", body)
        self.assertEqual(status, 409, response)
        self.assertEqual(self.detail()["revision"], before["revision"])
        self.assertIn("另一条视频", response["error"])

    def test_video_binding_is_unique_and_checks_known_product(self):
        first, second = self.script(), self.script(title="另一稿")
        status, _, data = self.status(first, status="published", video_id="7999999999999999992", published_on="2026-10-08")
        self.assertEqual(status, 200, data)
        self.assertEqual(self.status(second, status="filmed", video_id="7999999999999999992")[0], 409)
        with patch.object(self.server.app.performance, "video_brief", return_value={"7999999999999999993": {"product": "103N"}}):
            self.assertEqual(self.status(second, status="filmed", video_id="7999999999999999993")[0], 409)

    def test_case_journal_recovers_committed_case_when_script_link_write_failed(self):
        item = self.script()
        body = {"revision": self.catalog()["revision"], "request_id": uuid4().hex, "conclusion": "明确沉淀"}
        original = self.server.app.creation.mutate
        def fail_link(kind, *args, **kwargs):
            if kind == "script_status":
                raise OSError("simulated link interruption")
            return original(kind, *args, **kwargs)
        with patch.object(self.server.app.creation, "mutate", side_effect=fail_link):
            self.assertEqual(self.request("POST", "/api/scripts/" + item["id"] + "/case", body)[0], 500)
        self.assertEqual(len(self.store.read_index()["cases"]), 2)
        self.server.app.requests.clear()  # emulate loss of process-local save receipts
        status, _, result = self.request("POST", "/api/scripts/" + item["id"] + "/case", body)
        self.assertEqual(status, 200, result)
        self.assertEqual(len(self.store.read_index()["cases"]), 2)
        self.assertEqual(len(result["case"]["events"]), 1)

    def test_independent_ai_candidate_adoption_and_durable_replay(self):
        direction = self.direction()
        self.server.app.ai.bridge = FakeBridge(output="Candidate body and explanation.")
        body = {"request_id": uuid4().hex, "product": "202H", "direction_id": direction["direction_id"], "action": "review", "instruction": "核对资料", "skill_selection": "none"}
        status, _, data = self.request("POST", "/api/ai/tasks", body)
        self.assertEqual(status, 200, data)
        deadline = time.monotonic() + 4
        task = data["task"]
        while task["busy"] and time.monotonic() < deadline:
            time.sleep(.02)
            task = self.server.app.ai.get(task["task_id"])
        self.assertEqual(task["status"], "completed")
        body = {"revision": self.catalog()["revision"], "request_id": uuid4().hex,
                "script": {"product": "202H", "direction_id": direction["direction_id"], "title": "采纳稿", "text": "Only chosen candidate body.", "confirmed": True},
                "source": {"task_id": task["task_id"], "round_id": task["rounds"][0]["round_id"], "task_revision": task["revision"]}}
        status, _, saved = self.request("POST", "/api/scripts", body)
        self.assertEqual(status, 200, saved)
        self.server.app.ai.delete_tasks({"tasks": [{"task_id": task["task_id"], "revision": task["revision"]}]})
        status, _, replay = self.request("POST", "/api/scripts", body)
        self.assertEqual(status, 200, replay)
        self.assertEqual(saved["script"]["id"], replay["script"]["id"])
        public = self.catalog()["scripts"][0]
        self.assertNotIn("candidate_text", public["source"])
        detail = self.request("GET", "/api/scripts/" + public["id"])[2]
        self.assertTrue(detail["script"]["source"]["task_deleted"])
        self.assertEqual(len(self.store.read_index()["cases"]), 1)

    def test_plans_and_source_material_expressions_are_checked(self):
        direction = self.direction()
        body = {"revision": self.catalog()["revision"], "request_id": uuid4().hex,
                "plan": {"product": "202H", "direction_id": direction["direction_id"], "action": "execution"}}
        status, _, plan = self.request("POST", "/api/plans", body)
        self.assertEqual(status, 200, plan)
        status, _, result = self.request("POST", "/api/materials", {"revision": self.materials()["revision"], "request_id": uuid4().hex, "operation": "add",
            "item": {"type": "expression", "source_id": self.identifier, "product": "202H", "text": "Invented words"}})
        self.assertEqual(status, 409, result)
        self.assertEqual(self.request("GET", "/api/materials")[2]["items"], [])

    def test_reviews_share_pick_source_theme_links_and_frozen_ai_materials(self):
        direction = self.direction()
        build_sources(self.runtime / "reviews" / "sources")
        self.request("GET", "/api/reviews/summary")
        review = self.request("GET", "/api/reviews")[2]["reviews"][0]
        body = {"revision": self.materials()["revision"], "request_id": uuid4().hex, "operation": "add",
                "item": {"type": "review", "source_id": review["id"], "note": "冻结待核对的评论"}}
        status, _, materials = self.request("POST", "/api/materials", body)
        self.assertEqual(status, 200, materials)
        self.assertEqual(len(self.server.app.reviews.picks()), 1)
        self.assertEqual(self.server.app.creation.public()["materials"], [])
        theme = {"revision": self.materials()["revision"], "request_id": uuid4().hex,
                 "review_id": review["id"], "product": "202H", "role": "concern", "target_id": direction["direction_id"]}
        status, _, themed = self.request("POST", "/api/material-themes", theme)
        self.assertEqual(status, 200, themed)
        self.assertEqual(themed["theme_links"][0]["review_id"], review["id"])
        self.assertEqual(themed["theme_links"][0]["source_quote"], review["text"])
        duplicate_theme = {**theme, "revision": self.materials()["revision"], "request_id": uuid4().hex, "note": "补充用法"}
        self.assertEqual(self.request("POST", "/api/material-themes", duplicate_theme)[0], 200)
        self.assertEqual(len(self.materials()["theme_links"]), 1)
        self.assertEqual(self.materials()["theme_links"][0]["note"], "补充用法")
        self.server.app.ai.bridge = FakeBridge()
        request = {"request_id": uuid4().hex, "product": "202H", "direction_id": direction["direction_id"], "action": "review",
                   "instruction": "核对所选评论", "skill_selection": "none", "material_ids": [materials["items"][0]["id"]]}
        status, _, task = self.request("POST", "/api/ai/tasks", request)
        self.assertEqual(status, 200, task)
        self.server.app.reviews.set_pick(review["id"], False)
        self.assertEqual(self.request("GET", "/api/materials")[2]["items"], [])
        replay = self.request("POST", "/api/ai/tasks", request)
        self.assertEqual(replay[0], 200, replay[2])
        self.assertEqual(replay[2]["task"]["context"]["materials"][0]["text"], review["text"])

    def test_expression_receipt_recovers_after_catalog_commit_and_case_source_remains_exact(self):
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["native_reference"] = {"language": "en", "market": "US", "duration_sec": 12, "scene": "现场演示", "speaker": "演示者", "audience": "磨刀用户",
            "entries": [{"id": "phrase-1", "channel": "speech", "original": "Keep it right there.", "meaning_zh": "保持在那里。", "function": "动作提示", "context": "当前演示", "analysis": "短句", "adaptation": "借用节奏", "boundary": "不复制亲历", "start_sec": 1, "end_sec": 3}]}
        self.store.append(self.identifier, {"kind": "editorial", "text": "记录表达", "current": current})
        body = {"revision": self.materials()["revision"], "request_id": uuid4().hex, "operation": "add",
                "item": {"type": "expression", "source_id": self.identifier, "product": "202H", "text": "Keep it right there.", "note": "用于动作衔接"}}
        original = self.server.app.creation._replace
        def fail_receipt(path, data):
            if path.parent.name == "operations" and data.get("complete"):
                raise OSError("simulated receipt response lost")
            return original(path, data)
        with patch.object(self.server.app.creation, "_replace", side_effect=fail_receipt):
            self.assertEqual(self.request("POST", "/api/materials", body)[0], 500)
        self.assertEqual(len(self.server.app.creation.public()["materials"]), 1)
        status, _, result = self.request("POST", "/api/materials", body)
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["text"], current["native_reference"]["entries"][0]["original"])
        remove = {"revision": result["revision"], "request_id": uuid4().hex, "operation": "remove", "item": result["items"][0]}
        self.assertEqual(self.request("POST", "/api/materials", remove)[0], 200)
        self.assertEqual(self.request("POST", "/api/materials", remove)[0], 200)

    def test_review_note_compare_and_swap_rejects_stale_window(self):
        build_sources(self.runtime / "reviews" / "sources")
        self.request("GET", "/api/reviews/summary")
        review = self.request("GET", "/api/reviews")[2]["reviews"][0]
        self.server.app.reviews.set_pick(review["id"], True, "开始备注")
        snapshot = self.materials()
        one = {"revision": snapshot["revision"], "request_id": uuid4().hex, "operation": "note", "item": {**snapshot["items"][0], "note": "窗口一修改"}}
        two = {**one, "request_id": uuid4().hex, "item": {**one["item"], "note": "窗口二旧修改"}}
        self.assertEqual(self.request("POST", "/api/materials", one)[0], 200)
        self.assertEqual(self.request("POST", "/api/materials", two)[0], 409)
        self.assertEqual(self.materials()["items"][0]["note"], "窗口一修改")
        self.assertEqual(self.request("POST", "/api/materials", one)[0], 200)

    def test_review_lost_receipt_retry_does_not_overwrite_a_newer_note(self):
        build_sources(self.runtime / "reviews" / "sources")
        self.request("GET", "/api/reviews/summary")
        review = self.request("GET", "/api/reviews")[2]["reviews"][0]
        self.server.app.reviews.set_pick(review["id"], True, "旧备注")
        snapshot = self.materials()
        body = {"revision": snapshot["revision"], "request_id": uuid4().hex, "operation": "note", "item": {**snapshot["items"][0], "note": "第一窗口修改"}}
        original = self.server.app.creation._replace
        def fail_receipt(path, data):
            if path.parent.name == "operations" and data.get("complete"):
                raise OSError("lost receipt")
            return original(path, data)
        with patch.object(self.server.app.creation, "_replace", side_effect=fail_receipt):
            self.assertEqual(self.request("POST", "/api/materials", body)[0], 500)
        self.assertEqual(self.materials()["items"][0]["note"], "第一窗口修改")
        self.server.app.reviews.set_pick(review["id"], True, "另一窗口更新")
        self.assertEqual(self.request("POST", "/api/materials", body)[0], 409)
        self.assertEqual(self.materials()["items"][0]["note"], "另一窗口更新")

    def test_legacy_pick_write_waits_for_the_shared_material_lock(self):
        build_sources(self.runtime / "reviews" / "sources")
        self.request("GET", "/api/reviews/summary")
        review = self.request("GET", "/api/reviews")[2]["reviews"][0]
        entered, finished = threading.Event(), threading.Event()
        result = []
        original = self.server.app.reviews.set_pick
        def tracked(*args, **kwargs):
            entered.set()
            return original(*args, **kwargs)
        def write():
            try:
                result.append(self.request("POST", "/api/reviews/" + review["id"] + "/pick", {"picked": True, "note": "旧入口"}))
            finally:
                finished.set()
        with patch.object(self.server.app.reviews, "set_pick", side_effect=tracked):
            with self.server.app.lock:
                worker = threading.Thread(target=write)
                worker.start()
                deadline = time.monotonic() + 2
                while self.server._active_writes == 0 and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertGreater(self.server._active_writes, 0)
                self.assertFalse(entered.is_set())
            self.assertTrue(finished.wait(3))
            worker.join(timeout=3)
        self.assertEqual(result[0][0], 200, result)
        self.assertTrue(entered.is_set())

    def test_archived_direction_allows_ending_old_plan_but_not_new_work(self):
        direction = self.direction()
        plans = []
        for action in ("scale", "execution"):
            response = self.request("POST", "/api/plans", {"revision": self.catalog()["revision"], "request_id": uuid4().hex,
                "plan": {"product": "202H", "direction_id": direction["direction_id"], "action": action}})
            self.assertEqual(response[0], 200, response[2])
            plans.append(response[2]["plan"])
        catalog = self.server.app.direction_catalog("202H")
        self.server.app.save_direction_catalog("direction", {"revision": catalog["revision"], "request_id": uuid4().hex,
            "operation": "update", "direction": {**direction, "status": "archived"}})
        for plan, status in zip(plans, ("done", "cancelled")):
            response = self.request("POST", "/api/plans", {"revision": self.catalog()["revision"], "request_id": uuid4().hex,
                "plan": {"id": plan["id"], "status": status}})
            self.assertEqual(response[0], 200, response[2])
            self.assertEqual(response[2]["plan"]["status"], status)
        response = self.request("POST", "/api/plans", {"revision": self.catalog()["revision"], "request_id": uuid4().hex,
            "plan": {"product": "202H", "direction_id": direction["direction_id"], "action": "scale"}})
        self.assertEqual(response[0], 409, response[2])
        response = self.request("POST", "/api/ai/tasks", {"request_id": uuid4().hex, "product": "202H",
            "direction_id": direction["direction_id"], "instruction": "继续写稿", "action": "review", "skill_selection": "none"})
        self.assertEqual(response[0], 409, response[2])

    def test_plan_links_task_then_adopted_script_without_automatically_completing(self):
        direction = self.direction()
        status, _, planned = self.request("POST", "/api/plans", {"revision": self.catalog()["revision"], "request_id": uuid4().hex,
            "plan": {"product": "202H", "direction_id": direction["direction_id"], "action": "execution", "note": "只替换开头演示"}})
        self.assertEqual(status, 200, planned)
        plan = planned["plan"]
        self.server.app.ai.bridge = FakeBridge()
        body = {"request_id": uuid4().hex, "product": "202H", "direction_id": direction["direction_id"], "plan_id": plan["id"],
                "action": "review", "instruction": "按计划核对", "skill_selection": "none"}
        status, _, result = self.request("POST", "/api/ai/tasks", body)
        self.assertEqual(status, 200, result)
        task = result["task"]
        deadline = time.monotonic() + 4
        while task["busy"] and time.monotonic() < deadline:
            time.sleep(.02)
            task = self.server.app.ai.get(task["task_id"])
        self.assertEqual(self.catalog()["plans"][0]["task_id"], task["task_id"])
        self.assertEqual(self.catalog()["plans"][0]["status"], "working")
        source = {"task_id": task["task_id"], "round_id": task["rounds"][0]["round_id"], "task_revision": task["revision"]}
        status, _, saved = self.request("POST", "/api/scripts", {"revision": self.catalog()["revision"], "request_id": uuid4().hex,
            "script": {"product": "202H", "direction_id": direction["direction_id"], "title": "按计划采纳", "text": "Chosen words", "confirmed": True}, "source": source})
        self.assertEqual(status, 200, saved)
        self.assertEqual(self.catalog()["plans"][0]["script_id"], saved["script"]["id"])
        self.assertEqual(self.catalog()["plans"][0]["status"], "working")
        self.assertEqual(self.request("POST", "/api/ai/tasks", body)[0], 200)
        self.assertEqual(len(self.server.app.ai.list_tasks()), 1)
        invalid = {"revision": self.catalog()["revision"], "request_id": uuid4().hex, "plan": {"id": plan["id"], "script_id": "SCR-0123456789ab"}}
        self.assertEqual(self.request("POST", "/api/plans", invalid)[0], 404)


if __name__ == "__main__":
    unittest.main()
