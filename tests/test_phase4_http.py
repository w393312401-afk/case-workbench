"""Phase four HTTP integration uses isolated data; no real AI service is called."""
import copy
import json
import os
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

    def test_ticked_variants_freeze_into_the_task_and_make_the_request_optional(self):
        direction = self.direction()
        catalog = self.server.app.direction_catalog("202H")
        self.server.app.save_direction_catalog("direction", {"revision": catalog["revision"], "request_id": uuid4().hex,
            "direction": {"product": "202H", "direction_id": direction["direction_id"], "selling_point_id": direction["selling_point_id"],
                          "progression": "逐件对照", "concern": "旧夹具换成 S1", "closing": "核对挂车款", "execution_change": "逐件替换"}})
        variants = self.server.app.direction_catalog("202H")["directions"][0]["variants"]
        self.assertEqual(len(variants), 2)
        sample = "\n".join(["**202H-S1 · 变体 #1 基准**", "", "**口播（配音用）**", "", "```", "One kit, replacing this jig.",
                               "Full 202H kit's below.", "```", "", "**变体 #2 · 逐件对照**", "**口播(配音用)**", "```",
                               "Out goes the jig.", "```", "", "**意群表**", "```", "Not counted here.", "```"])
        bridge = FakeBridge(output=sample)
        self.server.app.ai.bridge = bridge
        base = {"product": "202H", "direction_id": direction["direction_id"], "action": "variants", "skill_selection": "none"}
        rejected = [
            ({"variants": [{"index": 1, "progression": "结果倒叙"}]}, 409),
            ({"variants": [{"index": 5, "progression": None}]}, 400),
            ({"variants": [], "new_variant_count": 0}, 400),
            ({"variants": [], "new_variant_count": 6}, 400),
            ({"variants": [{"index": 1, "progression": "逐件对照"}], "action": "review"}, 400),
            ({"variants": [{"index": 1, "progression": "逐件对照"}], "carousel": True}, 400),
            ({"instruction": "核对资料", "action": "review", "carousel": True}, 400),
        ]
        for extra, code in rejected:
            status, _, data = self.request("POST", "/api/ai/tasks", {**base, "request_id": uuid4().hex, **extra})
            self.assertEqual(status, code, (extra, data))
        self.assertEqual(self.request("GET", "/api/ai/tasks")[2]["tasks"], [])
        body = {**base, "request_id": uuid4().hex, "instruction": "  ",
                "variants": [{"index": 0, "progression": None}, {"index": 1, "progression": "逐件对照"}], "new_variant_count": 1}
        status, _, data = self.request("POST", "/api/ai/tasks", body)
        self.assertEqual(status, 200, data)
        task = data["task"]
        deadline = time.monotonic() + 4
        while task["busy"] and time.monotonic() < deadline:
            time.sleep(.02)
            task = self.server.app.ai.get(task["task_id"])
        self.assertEqual(task["status"], "completed")
        plan = task["context"]["writing_plan"]
        self.assertEqual([item["number"] for item in plan["variants"]], [1, 2])
        self.assertEqual(plan["variants"][1]["execution_change"], "逐件替换")
        self.assertEqual((plan["new_variant_count"], plan["used_progressions"]), (1, ["逐件对照"]))
        self.assertEqual(task["rounds"][0]["instruction"], "按所选 2 条已登记变体（#1、#2）各写一条，另提出 1 条建议新增变体。")
        self.assertNotIn("carousel", task["context"])
        self.assertEqual(task["rounds"][0]["read_estimates"],
                         [{"label": "变体 #1 基准", "words": 9, "seconds": 4}, {"label": "变体 #2 · 逐件对照", "words": 4, "seconds": 2}])
        prompt = bridge.calls[-1]["prompt"]
        self.assertNotIn("图文版", prompt)
        self.assertIn("意群表", prompt)
        self.assertNotIn("简明原生度评分", prompt)
        self.assertIn("每条变体各写一条候选", prompt)
        self.assertIn("以主打卖点为准修正那一环", prompt)
        self.assertEqual(self.request("POST", "/api/ai/tasks", body)[2]["task"]["task_id"], task["task_id"])

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

    def test_basket_reports_duplicates_names_theme_targets_and_shares_limits(self):
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["native_reference"] = {"language": "en", "market": "US", "duration_sec": 12, "scene": "现场演示", "speaker": "演示者", "audience": "磨刀用户",
            "entries": [{"id": "phrase-1", "channel": "speech", "original": "Keep it right there.", "meaning_zh": "保持在那里。", "function": "动作提示", "context": "当前演示", "analysis": "短句", "adaptation": "借用节奏", "boundary": "不复制亲历", "start_sec": 1, "end_sec": 3}]}
        self.store.append(self.identifier, {"kind": "editorial", "text": "记录表达", "current": current})
        def add(note):
            return {"revision": self.materials()["revision"], "request_id": uuid4().hex, "operation": "add",
                    "item": {"type": "expression", "source_id": self.identifier, "product": "202H", "text": "Keep it right there.", "note": note}}
        status, _, first = self.request("POST", "/api/materials", add("首次备注"))
        self.assertEqual(status, 200, first)
        self.assertNotIn("existing", first)
        duplicate = add("第二次备注")
        status, _, second = self.request("POST", "/api/materials", duplicate)
        self.assertEqual(status, 200, second)
        self.assertTrue(second["existing"])
        self.assertEqual([item["note"] for item in second["items"]], ["首次备注"])
        self.assertTrue(self.request("POST", "/api/materials", duplicate)[2]["existing"])
        limits = self.materials()["limits"]
        self.assertEqual(limits, {"note": 2000, "hook_text": 6000, "prompt_chars": 180_000})
        long_note = {"revision": self.materials()["revision"], "request_id": uuid4().hex, "operation": "note",
                     "item": {**first["items"][0], "note": "长" * (limits["note"] + 1)}}
        self.assertEqual(self.request("POST", "/api/materials", long_note)[0], 400)
        self.assertEqual(self.materials()["items"][0]["note"], "首次备注")
        self.direction()
        point = self.server.app.direction_catalog("202H")["selling_points"][0]
        build_sources(self.runtime / "reviews" / "sources")
        self.request("GET", "/api/reviews/summary")
        review = self.request("GET", "/api/reviews")[2]["reviews"][0]
        theme = {"revision": self.materials()["revision"], "request_id": uuid4().hex,
                 "review_id": review["id"], "product": "202H", "role": "selling_point", "target_id": point["id"]}
        self.assertEqual(self.request("POST", "/api/material-themes", {**theme, "request_id": uuid4().hex, "note": "长" * (limits["note"] + 1)})[0], 400)
        status, _, themed = self.request("POST", "/api/material-themes", theme)
        self.assertEqual(status, 200, themed)
        self.assertEqual(themed["theme_links"][0]["target_label"], f"{point['code']} · {point['title']}")

    def basket_with_expression_and_quote(self):
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["native_reference"] = {"language": "en", "market": "US", "duration_sec": 12, "scene": "现场演示", "speaker": "演示者", "audience": "磨刀用户",
            "entries": [{"id": "phrase-1", "channel": "speech", "original": "Keep it right there.", "meaning_zh": "保持在那里。", "function": "动作提示", "context": "当前演示", "analysis": "短句", "adaptation": "借用节奏", "boundary": "不复制亲历", "start_sec": 1, "end_sec": 3}]}
        self.store.append(self.identifier, {"kind": "editorial", "text": "记录表达", "current": current})
        build_sources(self.runtime / "reviews" / "sources")
        self.request("GET", "/api/reviews/summary")
        review = self.request("GET", "/api/reviews")[2]["reviews"][0]
        self.server.app.reviews.set_pick(review["id"], True, "评论备注")
        body = {"revision": self.materials()["revision"], "request_id": uuid4().hex, "operation": "add",
                "item": {"type": "expression", "source_id": self.identifier, "product": "202H", "text": "Keep it right there.", "note": "表达备注"}}
        self.assertEqual(self.request("POST", "/api/materials", body)[0], 200)
        return review

    def test_note_conflicts_only_with_a_change_to_the_same_item(self):
        self.basket_with_expression_and_quote()
        snapshot = self.materials()
        expression = next(item for item in snapshot["items"] if item["type"] == "expression")
        quote = next(item for item in snapshot["items"] if item["type"] == "review")
        # A saved script and another window's note on a different item used to invalidate the whole basket.
        self.script()
        other = {"revision": snapshot["revision"], "request_id": uuid4().hex, "operation": "note", "item": {**quote, "note": "另一窗口的评论备注"}}
        self.assertEqual(self.request("POST", "/api/materials", other)[0], 200)
        mine = {"revision": snapshot["revision"], "request_id": uuid4().hex, "operation": "note", "item": {**expression, "note": "我的表达备注"}}
        status, _, saved = self.request("POST", "/api/materials", mine)
        self.assertEqual(status, 200, saved)
        stale = {"revision": saved["revision"], "request_id": uuid4().hex, "operation": "note", "item": {**expression, "note": "过时窗口的备注"}}
        self.assertEqual(self.request("POST", "/api/materials", stale)[0], 409)
        self.assertEqual({item["type"]: item["note"] for item in self.materials()["items"]}, {"expression": "我的表达备注", "review": "另一窗口的评论备注"})

    def test_batch_removal_is_one_step_and_undo_restores_exact_items(self):
        review = self.basket_with_expression_and_quote()
        picked = self.server.app.reviews.review(review["id"])["pick"]
        before = sorted(self.materials()["items"], key=lambda item: item["id"])
        ids = [item["id"] for item in before]
        body = {"request_id": uuid4().hex, "product": "202H", "ids": ids}
        self.assertEqual(self.request("POST", "/api/materials/remove", {**body, "request_id": uuid4().hex, "product": "129N"})[0], 409)
        self.assertEqual(len(self.materials()["items"]), 2)
        status, _, removed = self.request("POST", "/api/materials/remove", body)
        self.assertEqual(status, 200, removed)
        self.assertEqual(removed["items"], [])
        self.assertEqual(sorted(removed["removed"]), ids)
        self.assertEqual(self.server.app.reviews.picks(), [])
        self.assertEqual(self.request("POST", "/api/materials/remove", body)[2]["removal_id"], removed["removal_id"])
        self.assertEqual(self.request("POST", "/api/materials/remove", {**body, "request_id": uuid4().hex})[2]["removed"], [])
        undo = {"request_id": uuid4().hex, "removal_id": removed["removal_id"]}
        status, _, restored = self.request("POST", "/api/materials/restore", undo)
        self.assertEqual(status, 200, restored)
        self.assertEqual((sorted(restored["restored"]), restored["skipped"]), (ids, 0))
        self.assertEqual(sorted(restored["items"], key=lambda item: item["id"]), before)
        self.assertEqual(self.server.app.reviews.review(review["id"])["pick"], picked)
        self.assertEqual(sorted(self.request("POST", "/api/materials/restore", undo)[2]["restored"]), ids)
        self.assertEqual(len(self.materials()["items"]), 2)

    def test_undo_does_not_bring_back_an_expression_from_a_deleted_case(self):
        self.basket_with_expression_and_quote()
        ids = [item["id"] for item in self.materials()["items"]]
        status, _, removed = self.request("POST", "/api/materials/remove", {"request_id": uuid4().hex, "product": "202H", "ids": ids})
        self.assertEqual(status, 200, removed)
        self.server.app.cleanup_creation_case(self.identifier)
        record = (self.server.app.creation.root / "operations" / (removed["removal_id"] + ".json")).read_text(encoding="utf-8")
        self.assertNotIn("Keep it right there.", record)
        status, _, restored = self.request("POST", "/api/materials/restore", {"request_id": uuid4().hex, "removal_id": removed["removal_id"]})
        self.assertEqual(status, 200, restored)
        self.assertEqual(([item["type"] for item in restored["items"]], restored["skipped"]), (["review"], 1))

    def test_theme_link_removal_is_by_id_and_needs_no_basket_revision(self):
        review = self.basket_with_expression_and_quote()
        direction = self.direction()
        theme = {"revision": self.materials()["revision"], "request_id": uuid4().hex,
                 "review_id": review["id"], "product": "202H", "role": "concern", "target_id": direction["direction_id"]}
        status, _, themed = self.request("POST", "/api/material-themes", theme)
        self.assertEqual(status, 200, themed)
        self.script()
        remove = {"request_id": uuid4().hex, "operation": "remove", "id": themed["theme_links"][0]["id"]}
        status, _, result = self.request("POST", "/api/material-themes", remove)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["theme_links"], [])
        self.assertEqual(self.request("POST", "/api/material-themes", remove)[0], 200)
        self.assertEqual(self.request("POST", "/api/material-themes", {**remove, "request_id": uuid4().hex})[0], 200)
        self.assertEqual(self.request("POST", "/api/material-themes", {"request_id": uuid4().hex, "operation": "remove"})[0], 400)
        self.assertEqual(len(self.materials()["items"]), 2)

    def test_basket_reads_are_cached_but_see_every_write(self):
        review = self.basket_with_expression_and_quote()
        app = self.server.app
        first = app.material_catalog()
        with patch.object(app.reviews, "picks", side_effect=AssertionError("an unchanged basket must come from the cache")):
            self.assertEqual(app.material_catalog()["revision"], first["revision"])
        # Same length and within one clock tick: only the in-process write count can tell this apart.
        app.reviews.set_pick(review["id"], True, "评论备忘")
        self.assertEqual({item["type"]: item["note"] for item in app.material_catalog()["items"]}["review"], "评论备忘")
        returned = app.material_catalog()
        returned["existing"] = True
        returned["items"][0]["note"] = "只改了副本"
        fresh = app.material_catalog()
        self.assertNotIn("existing", fresh)
        self.assertNotEqual(fresh["items"][0]["note"], "只改了副本")

    def test_batch_note_save_is_all_or_nothing(self):
        self.basket_with_expression_and_quote()
        items = {item["type"]: item for item in self.materials()["items"]}
        body = {"request_id": uuid4().hex, "notes": [{"id": items["review"]["id"], "note": "批量评论", "item_revision": items["review"]["item_revision"]},
                                                      {"id": items["expression"]["id"], "note": "批量表达", "item_revision": items["expression"]["item_revision"]}]}
        status, _, saved = self.request("POST", "/api/materials/notes", body)
        self.assertEqual(status, 200, saved)
        self.assertEqual({item["type"]: item["note"] for item in saved["items"]}, {"review": "批量评论", "expression": "批量表达"})
        self.assertEqual(self.request("POST", "/api/materials/notes", body)[0], 200)
        current = {item["type"]: item for item in saved["items"]}
        # One stale note blocks the whole batch, including the note that was up to date.
        stale = {"request_id": uuid4().hex, "notes": [{"id": current["expression"]["id"], "note": "新表达", "item_revision": current["expression"]["item_revision"]},
                                                       {"id": current["review"]["id"], "note": "过时评论", "item_revision": items["review"]["item_revision"]}]}
        self.assertEqual(self.request("POST", "/api/materials/notes", stale)[0], 409)
        self.assertEqual({item["type"]: item["note"] for item in self.materials()["items"]}, {"review": "批量评论", "expression": "批量表达"})
        missing = {"request_id": uuid4().hex, "notes": [{"id": "MAT-000000000000", "note": "x", "item_revision": "r"}]}
        self.assertEqual(self.request("POST", "/api/materials/notes", missing)[0], 404)

    def test_old_receipts_finished_journals_and_surplus_backups_are_pruned(self):
        creation = self.server.app.creation
        self.script()
        data = creation.read()
        data["requests"]["old-request-0001"] = {"hash": "h", "result": {}, "at": "2020-01-01T00:00:00.000000+00:00"}
        data["requests"]["legacy-request-01"] = {"hash": "h", "result": {}}
        creation.write(data)
        self.script()
        requests = creation.read()["requests"]
        self.assertNotIn("old-request-0001", requests)
        self.assertIn("at", requests["legacy-request-01"])
        operations, old = creation.root / "operations", time.time() - 40 * 86400
        operations.mkdir(parents=True, exist_ok=True)
        for name, record in {"done": {"complete": True}, "open": {"prepared": True}, "gone": {"source_deleted": True}}.items():
            path = operations / f"{name}-journal-0001.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            os.utime(path, (old, old))
        creation.prune_operations()
        self.assertEqual(sorted(path.stem for path in operations.glob("*-journal-0001.json")), ["gone-journal-0001", "open-journal-0001"])
        backups, now = creation.root / "backups", time.time()
        for name, stamp in [*((f"today-{index:02}", now - 60 - index) for index in range(55)),
                            *((f"recent-{index}", now - 5 * 86400 - index * 60) for index in range(3)),
                            *((f"ancient-{index}", now - 40 * 86400 - index * 60) for index in range(2))]:
            path = backups / f"{name}.json"
            path.write_text("{}", encoding="utf-8")
            os.utime(path, (stamp, stamp))
        creation._prune_backups()
        kept = {path.stem for path in backups.glob("*.json")}
        self.assertEqual(len(kept), 51)
        # The newest 50 are today's; of older days only the newest backup of each day within 30 days stays.
        self.assertEqual(sorted(name for name in kept if name.startswith(("recent-", "ancient-"))), ["recent-0"])

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
