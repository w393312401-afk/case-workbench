"""Content queue and archive routes over real HTTP and disposable business stores only."""
from __future__ import annotations

from pathlib import Path
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4
import zipfile

import test_server as fixture
from test_ai_tasks import FakeBridge
import test_phase4_http as creation_fixture
import test_production_carousel_publish as carousel_publish_fixture
from test_production import CANDIDATE
from test_video_publication import VIDEO
from data_backup import BackupManager


class ContentFlowHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    direction = creation_fixture.CreationHTTPTests.direction

    def ok(self, method, path, body=None):
        status, _, response = self.request(method, path, body)
        self.assertEqual(status, 200, response)
        return response

    def content(self, **query):
        return self.ok("GET", "/api/content" + ("?" + urlencode(query) if query else ""))

    def completed_task(self, direction_id=""):
        self.server.app.ai.bridge = FakeBridge(output=CANDIDATE)
        task = self.ok("POST", "/api/ai/tasks", {"request_id": uuid4().hex,
            "product": "202H", "action": "review", "instruction": "临时内容测试，只核对这条候选。",
            "direction_id": direction_id, "skill_selection": "none"})["task"]
        deadline = time.monotonic() + 5
        while task["busy"] and time.monotonic() < deadline:
            time.sleep(.02)
            task = self.ok("GET", "/api/ai/tasks/" + task["task_id"])["task"]
        self.assertEqual(task["status"], "completed")
        self.assertFalse(task["busy"])
        return task

    def script(self, task=None, **values):
        body = {"revision": self.ok("GET", "/api/creation")["revision"], "request_id": uuid4().hex,
            "script": {"product": "202H", "title": "相同内容标题", "text": "One kit.\nOpen this case.",
                       "confirmed": True, **values}}
        if task:
            body["source"] = {"task_id": task["task_id"], "round_id": task["rounds"][-1]["round_id"],
                              "task_revision": task["revision"]}
        return self.ok("POST", "/api/scripts", body)["script"]

    def job(self, script):
        return self.ok("POST", "/api/production/jobs", {
            "revision": self.ok("GET", "/api/production")["revision"], "request_id": uuid4().hex,
            "job": {"script_id": script["id"]}})["job"]

    def management_body(self, identifier, **management):
        return {"revision": self.content(view="all")["revision"], "request_id": uuid4().hex,
                "id": identifier, "management": management}

    def manage(self, identifier, **management):
        return self.ok("POST", "/api/content/manage", self.management_body(identifier, **management))

    def stored_business(self):
        paths = [self.server.app.creation.path, self.server.app.production.path,
                 self.server.app.content_management.path]
        return {str(path): path.read_bytes() if path.exists() else None for path in paths}

    def test_exact_provenance_keeps_candidates_versions_and_each_deliverable_separate(self):
        direction = self.direction()["direction_id"]
        task = self.completed_task(direction)
        other_task = self.completed_task(direction)
        first = self.script(task, direction_id=direction)
        second = self.script(task, direction_id=direction, text="A second adopted reading.")
        newer = self.script(parent_id=first["id"], direction_id=direction, text="New exact version.")
        unrelated = self.script(direction_id=direction)
        one, two, three = self.job(first), self.job(first), self.job(second)
        before = self.stored_business()
        queue = self.content(product="202H", view="all")
        by_id = {row["id"]: row for row in queue["items"]}
        self.assertEqual(set(by_id), {"task:" + other_task["task_id"], "script:" + first["family_id"],
            "script:" + unrelated["family_id"], *("video:" + job["id"] for job in (one, two, three))})
        self.assertNotIn("task:" + task["task_id"], by_id)
        self.assertEqual(by_id["video:" + one["id"]]["script_ids"], [first["id"]])
        self.assertEqual(by_id["video:" + two["id"]]["production_ids"], [two["id"]])
        self.assertEqual(by_id["video:" + three["id"]]["script_ids"], [second["id"]])
        family = by_id["script:" + first["family_id"]]
        self.assertEqual(family["script_ids"], [first["id"], newer["id"]])
        self.assertEqual(family["version_count"], 2)
        self.assertEqual(family["next_hash"], f"#ai/{task['task_id']}?round={task['rounds'][-1]['round_id']}&adopted={newer['id']}")
        self.assertEqual(by_id["script:" + unrelated["family_id"]]["task_ids"], [])
        self.assertEqual(self.content(product="103N", view="all")["items"], [])
        self.assertEqual(self.stored_business(), before, "Aggregation must not write business status or text")

    def test_adopting_the_same_candidate_text_again_reuses_its_script_and_settles_the_task(self):
        task = self.completed_task()
        self.assertEqual(task["management_state"], "pending_selection")
        first = self.script(task)
        settled = self.ok("GET", "/api/ai/tasks/" + task["task_id"])["task"]
        # The label changes without a new revision, so the open page can adopt the next variant.
        self.assertEqual((settled["management_state"], settled["revision"]), ("adopted", task["revision"]))
        body = {"revision": self.ok("GET", "/api/creation")["revision"], "request_id": uuid4().hex,
                "script": {"product": "202H", "title": "相同内容标题 · 图文", "text": "One kit.\nOpen this case.", "confirmed": True},
                "source": {"task_id": task["task_id"], "round_id": task["rounds"][-1]["round_id"], "task_revision": task["revision"]}}
        again = self.ok("POST", "/api/scripts", body)
        self.assertTrue(again["reused"])
        self.assertEqual((again["script"]["id"], again["script"]["title"]), (first["id"], first["title"]))
        self.assertEqual(len([row for row in self.ok("GET", "/api/creation")["scripts"] if row["source"] and row["source"]["task_id"] == task["task_id"]]), 1)
        edited = self.script(task, text="An edited reading.")
        self.assertNotEqual(edited["id"], first["id"])
        self.ok("POST", "/api/scripts/" + first["id"] + "/status", {
            "revision": self.ok("GET", "/api/creation")["revision"], "request_id": uuid4().hex, "script_status": {"archived": True}})
        self.assertNotEqual(self.script(task)["id"], first["id"])
        # A state chosen by hand is not overridden by a later adoption.
        current = self.ok("GET", "/api/ai/tasks/" + task["task_id"])["task"]
        self.ok("POST", "/api/ai/tasks/manage", {"request_id": uuid4().hex, "management_state": "needs_revision",
                                                 "items": [{"task_id": task["task_id"], "revision": current["revision"]}]})
        self.script(self.ok("GET", "/api/ai/tasks/" + task["task_id"])["task"], text="Another reading.")
        self.assertEqual(self.ok("GET", "/api/ai/tasks/" + task["task_id"])["task"]["management_state"], "needs_revision")

    def test_authenticated_revision_and_idempotent_retry_survive_changed_source_rows(self):
        task = self.completed_task()
        identifier = "task:" + task["task_id"]
        body = self.management_body(identifier, archived=True)
        before = self.stored_business()
        self.assertEqual(self.request("POST", "/api/content/manage", body, authenticated=False)[0], 403)
        self.assertEqual(self.stored_business(), before)
        saved = self.ok("POST", "/api/content/manage", body)
        adopted = self.script(task)
        self.assertNotIn(identifier, {row["id"] for row in self.content(view="all")["items"]})
        self.assertEqual(self.ok("POST", "/api/content/manage", body), saved)
        # An archived candidate does not archive the script adopted from it.
        script_row = next(row for row in self.content()["items"] if row["id"] == "script:" + adopted["family_id"])
        self.assertFalse(script_row["archived"])
        self.manage("script:" + adopted["family_id"], archived=True)
        self.assertEqual(self.ok("POST", "/api/content/manage", body), saved)
        stale = {**body, "request_id": uuid4().hex, "management": {"archived": False}}
        before = self.stored_business()
        self.assertEqual(self.request("POST", "/api/content/manage", stale)[0], 409)
        self.assertEqual(self.request("POST", "/api/content/manage", {**body, "management": {"archived": False}})[0], 409)
        self.assertEqual(self.stored_business(), before)

    def test_scheduling_fields_unknown_fields_and_bad_request_shapes_never_write(self):
        script = self.script()
        identifier = "script:" + script["family_id"]
        self.manage(identifier, archived=True)
        before = self.stored_business()
        for fields in ({"target_date": "2026-10-20"}, {"priority": "high"}, {"paused": True}, {"note": "备注"},
                       {"archived": 1}, {"archived": None}, {"status": "published"}):
            with self.subTest(fields=fields):
                status, _, response = self.request("POST", "/api/content/manage", self.management_body(identifier, **fields))
                self.assertEqual(status, 400, response)
                self.assertEqual(self.stored_business(), before)
        valid = self.management_body(identifier, archived=False)
        for changed in ({**valid, "extra": True}, {**valid, "id": "script:does-not-exist"},
                        {**valid, "request_id": []}, {**valid, "request_id": {}}):
            with self.subTest(body=changed):
                expected = 404 if changed.get("id") == "script:does-not-exist" else 400
                self.assertEqual(self.request("POST", "/api/content/manage", changed)[0], expected)
                self.assertEqual(self.stored_business(), before)
        self.manage(identifier, archived=False)

    def test_content_archive_waits_for_running_and_stopping_ai_without_changing_execution(self):
        bridge = FakeBridge(blocked=True, ignore_cancel=True)
        self.addCleanup(bridge.gate.set)
        self.server.app.ai.bridge = bridge
        task = self.ok("POST", "/api/ai/tasks", {"request_id": uuid4().hex,
            "product": "202H", "action": "review", "instruction": "只用于临时运行状态核对。",
            "skill_selection": "none"})["task"]
        self.assertTrue(bridge.started.wait(3))
        identifier = "task:" + task["task_id"]
        body = self.management_body(identifier, archived=True)
        before = self.server.app.content_management.public()
        self.assertEqual(self.request("POST", "/api/content/manage", body)[0], 409)
        self.assertEqual(self.server.app.content_management.public(), before)
        current = self.ok("GET", "/api/ai/tasks/" + task["task_id"])["task"]
        self.assertTrue(current["busy"])
        cancelled = self.ok("POST", "/api/ai/tasks/" + task["task_id"] + "/cancel", {
            "request_id": uuid4().hex, "round_id": current["rounds"][-1]["round_id"]})["task"]
        self.assertTrue(cancelled["stopping"])
        self.assertEqual(self.request("POST", "/api/content/manage", body)[0], 409)
        self.assertEqual(self.server.app.content_management.public(), before)
        bridge.gate.set()
        deadline = time.monotonic() + 5
        while self.server.app.ai.get(task["task_id"])["busy"] and time.monotonic() < deadline:
            time.sleep(.02)
        settled = self.server.app.ai.get(task["task_id"])
        self.assertFalse(settled["busy"])
        self.ok("POST", "/api/content/manage", body)
        self.assertEqual(self.content()["items"], [])
        self.assertTrue(self.content(view="all")["items"][0]["archived"])
        self.assertEqual(self.server.app.ai.get(task["task_id"])["status"], settled["status"])

    def test_views_search_kind_and_pagination_are_stable_and_validate_input(self):
        first = self.script(title="Alpha Queue")
        second = self.script(title="Beta Queue", text="Beta text.")
        third = self.script(title="Gamma Queue", text="Gamma text.")
        ids = ["script:" + row["family_id"] for row in (first, second, third)]
        queue = self.content(page_size=2)
        self.assertEqual([row["id"] for row in queue["items"]], [ids[2], ids[1]])
        self.assertEqual((queue["total"], queue["page"], queue["pages"]), (3, 1, 2))
        self.assertEqual(queue["counts"], {"attention": 3, "running": 0, "publish": 0, "all": 3})
        second_page = self.content(page=999, page_size=2)
        self.assertEqual((second_page["page"], [row["id"] for row in second_page["items"]]), (2, [ids[0]]))
        self.assertEqual([row["id"] for row in self.content(q="beta")["items"]], [ids[1]])
        self.assertEqual([row["id"] for row in self.content(q=first["id"])["items"]], [ids[0]])
        self.assertEqual(self.content(kind="video")["items"], [])
        self.assertEqual(self.content(view="publish")["items"], [])
        for query in ("page=0", "page=bad", "page_size=101", "kind=wrong", "view=wrong", "view=week",
                      "view=active", "view=all&view=all", "status=wrong", "status=all&status=all"):
            with self.subTest(query=query):
                self.assertEqual(self.request("GET", "/api/content?" + query)[0], 400)

    def test_running_and_status_filters_include_live_builds_without_writing_business_data(self):
        waiting = self.job(self.script(title="正在建线"))
        draft = self.job(self.script(title="等我配音"))
        archived = self.job(self.script(title="已归档制作"))
        self.ok("POST", "/api/production/jobs/" + archived["id"] + "/archive", {
            "request_id": uuid4().hex, "revision": self.ok("GET", "/api/production")["revision"], "archived": True})
        self.server.app._timeline_live()[waiting["id"]] = {
            "request_id": uuid4().hex, "stage": "waiting", "progress": "等待达芬奇建线"}
        before = self.stored_business()
        running = self.content(view="running")
        self.assertEqual([row["id"] for row in running["items"]], ["video:" + waiting["id"]])
        self.assertEqual(running["counts"], {"attention": 1, "running": 1, "publish": 0, "all": 3})
        self.assertEqual(running["items"][0]["progress_text"], "等待达芬奇建线")
        self.assertEqual([row["id"] for row in self.content(view="all", status="needs_attention")["items"]], ["video:" + draft["id"]])
        self.assertEqual({row["id"] for row in self.content(view="all", status="in_progress")["items"]},
                         {"video:" + waiting["id"], "video:" + draft["id"]})
        self.assertEqual([row["id"] for row in self.content(view="all", status="archived")["items"]], ["video:" + archived["id"]])
        self.assertEqual({row["id"] for row in self.content(view="all", status="workflow_incomplete")["items"]},
                         {"video:" + waiting["id"], "video:" + draft["id"]})
        for status in ("awaiting_review", "approved", "published"):
            self.assertEqual(self.content(view="all", status=status)["items"], [])
        self.assertEqual(self.stored_business(), before)

    def test_incomplete_workflow_filter_combines_both_formats_and_keeps_other_filters(self):
        # Completion facts are covered by the current-source tests below. A
        # fixed snapshot keeps this test about HTTP filtering and pagination.
        def record(identifier, kind, **values):
            return {"id": identifier, "kind": kind, "product": "202H", "title": "待办 Shared",
                    "direction_id": "202H-S1", "stage": "draft", "stage_label": "待制作",
                    "archived": False, "history": False, "busy": False, "needs_attention": True,
                    "workflow_complete": False, "updated_at": "2026-10-20T10:00:00Z",
                    "script_ids": [], "task_ids": [], "production_ids": [], **values}

        rows = [
            record("video:VIDEO-draft", "video"),
            record("video:VIDEO-busy", "video", busy=True, needs_attention=False),
            record("video:VIDEO-publish", "video", stage="awaiting_publication", needs_attention=False),
            record("carousel:CAR-draft", "carousel"),
            record("carousel:CAR-busy", "carousel", busy=True, needs_attention=False),
            record("carousel:CAR-publish", "carousel", stage="awaiting_publication", needs_attention=False),
            record("video:VIDEO-done", "video", workflow_complete=True, history=True, stage="published",
                   related_workflows=[{"id": "carousel:CAR-draft", "complete": False}]),
            record("carousel:CAR-done", "carousel", workflow_complete=True, history=True, stage="published"),
            record("video:VIDEO-archived", "video", archived=True, history=True),
            record("carousel:CAR-archived", "carousel", archived=True, history=True),
            record("script:SCRIPT-unused", "script"),
            record("task:TASK-candidate", "task"),
            record("video:VIDEO-unknown", "video", workflow_complete=None),
            record("video:VIDEO-other", "video", product="103N"),
        ]
        management = self.server.app.content_management.public()

        def snapshot(product=None):
            return ([row for row in rows if not product or row["product"] == product], management)

        before = self.stored_business()
        with patch.object(self.server.app, "_content_snapshot", side_effect=snapshot) as read:
            query = {"view": "all", "status": "workflow_incomplete"}
            expected = {row["id"] for row in rows[:6]}
            self.assertEqual({row["id"] for row in self.content(**query)["items"]}, expected | {"video:VIDEO-other"})
            combined = self.content(**query, product="202H")
            self.assertEqual({row["id"] for row in combined["items"]}, expected)
            read.assert_called_with("202H")
            self.assertEqual(combined["total"], 6)
            self.assertEqual({row["kind"] for row in combined["items"]}, {"video", "carousel"})
            self.assertNotIn("video:VIDEO-done", {row["id"] for row in combined["items"]},
                             "An unfinished linked carousel does not reopen the video's own completed chain")
            for kind in ("video", "carousel"):
                with self.subTest(kind=kind):
                    filtered = self.content(**query, product="202H", kind=kind, q="sHaReD", page_size=2)
                    matching = sorted((identifier for identifier in expected if identifier.startswith(kind + ":")), reverse=True)
                    self.assertEqual((filtered["total"], filtered["pages"], filtered["page"]), (3, 2, 1))
                    self.assertEqual([row["id"] for row in filtered["items"]], matching[:2])
                    last = self.content(**query, product="202H", kind=kind, q="shared", page_size=2, page=999)
                    self.assertEqual((last["page"], [row["id"] for row in last["items"]]), (2, matching[2:]))
            self.assertEqual(self.content(**query, product="202H", q="VIDEO-publish")["items"][0]["id"], "video:VIDEO-publish")
            self.assertEqual(self.content(**query, product="202H", q="does not exist")["items"], [])
            self.assertEqual(self.content(**query, kind="script")["items"], [])
            self.assertEqual(self.content(**query, kind="task")["items"], [])
        self.assertEqual(self.stored_business(), before, "Filtering must leave the content and production stores unchanged")

    def test_unified_restore_unarchives_task_script_and_video_sources_idempotently(self):
        task = self.completed_task()
        self.ok("POST", "/api/ai/tasks/manage", {"request_id": uuid4().hex, "archived": True,
            "items": [{"task_id": task["task_id"], "revision": task["revision"]}]})
        script = self.script(title="源记录已归档的稿")
        self.ok("POST", "/api/scripts/" + script["id"] + "/status", {
            "request_id": uuid4().hex, "revision": self.ok("GET", "/api/creation")["revision"],
            "script_status": {"archived": True}})
        job = self.job(self.script(title="源记录已归档的视频"))
        self.ok("POST", "/api/production/jobs/" + job["id"] + "/archive", {
            "request_id": uuid4().hex, "revision": self.ok("GET", "/api/production")["revision"], "archived": True})
        identifiers = ("task:" + task["task_id"], "script:" + script["family_id"], "video:" + job["id"])
        for identifier in identifiers:
            self.manage(identifier, archived=True)
            row = next(row for row in self.content(view="all")["items"] if row["id"] == identifier)
            self.assertTrue(row["source_archived"])
            body = self.management_body(identifier, archived=False)
            before = self.stored_business()
            # An invalid or stale list request must never restore its source.
            self.assertEqual(self.request("POST", "/api/content/manage", {**body, "extra": True})[0], 400)
            self.assertEqual(self.request("POST", "/api/content/manage", {**body, "revision": "stale"})[0], 409)
            self.assertEqual(self.stored_business(), before)
            saved = self.ok("POST", "/api/content/manage", body)
            row = next(row for row in self.content(view="all")["items"] if row["id"] == identifier)
            self.assertFalse(row["source_archived"])
            self.assertFalse(row["archived"])
            after = self.stored_business()
            self.assertEqual(self.ok("POST", "/api/content/manage", body), saved)
            self.assertEqual(self.stored_business(), after)
        self.assertFalse(self.server.app.ai.get(task["task_id"])["archived"])
        self.assertEqual(self.server.app.creation.get_script(script["id"])["lifecycle_status"], "adopted")

    def test_source_restore_finishes_after_lost_metadata_write_without_duplicate_history(self):
        job = self.job(self.script())
        self.ok("POST", "/api/production/jobs/" + job["id"] + "/archive", {
            "request_id": uuid4().hex, "revision": self.ok("GET", "/api/production")["revision"], "archived": True})
        identifier = "video:" + job["id"]
        self.manage(identifier, archived=True)
        body = self.management_body(identifier, archived=False)
        with patch("content_management.durable_json", side_effect=OSError("temporary metadata write failure")):
            with self.assertRaises(OSError):
                self.server.app.save_content_management(body)
        restored = self.server.app.production.job(job["id"])
        self.assertFalse(restored["archived"])
        self.assertTrue(self.server.app.content_management.public()["items"][identifier]["archived"])
        self.ok("POST", "/api/content/manage", body)
        self.assertEqual(self.server.app.production.job(job["id"])["history"], restored["history"])
        self.assertFalse(next(row for row in self.content(view="all")["items"] if row["id"] == identifier)["archived"])

    def test_published_and_archived_scripts_live_in_history_without_replacing_business_status(self):
        published, archived, active = self.script(), self.script(), self.script()
        self.ok("POST", "/api/scripts/" + published["id"] + "/status", {
            "revision": self.ok("GET", "/api/creation")["revision"], "request_id": uuid4().hex,
            "script_status": {"status": "published", "video_id": "7999999999999999911", "published_on": "2026-10-01"}})
        self.ok("POST", "/api/scripts/" + archived["id"] + "/status", {
            "revision": self.ok("GET", "/api/creation")["revision"], "request_id": uuid4().hex,
            "script_status": {"archived": True}})
        current = "script:" + active["family_id"]
        self.manage(current, archived=True)
        before = self.server.app.creation.path.read_bytes()
        self.assertEqual(self.content()["items"], [])
        history = {row["id"]: row for row in self.content(view="all")["items"] if row["history"]}
        self.assertEqual(len(history), 3)
        self.assertEqual(history["script:" + published["family_id"]]["stage"], "published")
        self.assertTrue(history["script:" + archived["family_id"]]["source_archived"])
        self.assertFalse(history[current]["source_archived"])
        self.assertEqual(self.server.app.creation.path.read_bytes(), before)
        self.manage(current, archived=False)
        self.assertEqual([row["id"] for row in self.content()["items"]], [current])
        self.assertEqual(self.server.app.creation.get_script(published["id"])["status"], "published")
        self.assertEqual(self.server.app.creation.get_script(archived["id"])["lifecycle_status"], "adopted")

    def test_video_publication_is_history_only_for_the_current_reviewed_source(self):
        script = self.script()
        job = self.job(script)
        take = "TAK-111111111111"
        data = self.server.app.production.read()
        row = next(row for row in data["jobs"] if row["id"] == job["id"])
        row.update(selected_take=take, takes=[{"id": take, "model_id": "fixture", "voice_id": "fixture", "text": script["text"]}],
            timelines=[{"unique_id": "test-timeline", "name": "Reviewed", "take_id": take, "saved": True}],
            manual_completion={"at": "2026-10-01", "take_id": take, "timeline_uid": "test-timeline", "basis": "human_review"})
        self.server.app.production.write(data)
        before_publish = self.content(view="all")["items"][0]
        self.assertTrue(next(step for step in before_publish["workflow_steps"] if step["key"] == "edit")["done"])
        self.assertFalse(before_publish["workflow_complete"])
        identifier = "video:" + job["id"]
        self.assertEqual([row["id"] for row in self.content(view="all", status="workflow_incomplete")["items"]], [identifier])
        path = self.root / "published.mp4"
        path.write_bytes(VIDEO)
        endpoint = "/api/production/jobs/" + job["id"] + "/publication"
        detail = self.ok("GET", endpoint)
        self.ok("POST", endpoint, {"request_id": uuid4().hex, "revision": detail["revision"],
            "source_signature": detail["source_signature"], "post_id": "7999999999999999912",
            "published_on": "2026-10-01", "account": "fixture", "file_path": str(path), "file_confirmed": True})
        self.assertEqual(self.content(view="publish")["items"], [])
        self.assertEqual(self.content(view="all")["items"][0]["stage"], "published")
        self.assertTrue(self.content(view="all")["items"][0]["workflow_complete"])
        self.assertEqual(self.content(view="all", status="workflow_incomplete")["items"], [])
        data = self.server.app.production.read()
        row = next(row for row in data["jobs"] if row["id"] == job["id"])
        row["manual_completion"]["at"] = "2026-10-02"
        self.server.app.production.write(data)
        # The reviewed source changed, so the old registration no longer counts: it waits to be posted again.
        self.assertEqual(self.content()["items"], [])
        waiting = self.content(view="publish")["items"]
        self.assertEqual([row["id"] for row in waiting], [identifier])
        self.assertEqual(waiting[0]["stage"], "awaiting_publication")
        self.assertFalse(waiting[0]["workflow_complete"])
        self.assertFalse(next(step for step in waiting[0]["workflow_steps"] if step["key"] == "publish")["done"])
        self.assertEqual([row["id"] for row in self.content(view="all", status="workflow_incomplete")["items"]], [identifier])
        self.assertFalse(any(row["history"] for row in self.content(view="all")["items"]))
        self.assertEqual(self.server.app.creation.get_script(script["id"])["status"], "published")
        self.assertEqual(self.ok("GET", endpoint)["publication"]["post_id"], "7999999999999999912")

    def test_carousel_publication_checks_current_package_and_preserves_publication_when_archived(self):
        # The existing fixture makes and confirms real temporary images and a
        # checked ZIP using fake generation, then exposes the actual HTTP app.
        scenario = carousel_publish_fixture.CarouselPublishTests()
        self.addCleanup(scenario.doCleanups)
        scenario.setUp()
        scenario.publish()
        identifier = "carousel:" + scenario.item["id"]

        def queue(view="attention", **filters):
            status, _, response = scenario.request("GET", "/api/content?" + urlencode({"view": view, **filters}))
            self.assertEqual(status, 200, response)
            return response["items"]

        self.assertEqual(queue(), [])
        published = queue("all")
        self.assertEqual([(row["id"], row["stage"], row["history"]) for row in published], [(identifier, "published", True)])
        self.assertTrue(published[0]["workflow_complete"])
        self.assertTrue(all(step["done"] for step in published[0]["workflow_steps"]))
        self.assertEqual(queue("all", status="workflow_incomplete"), [])
        original_bytes = scenario.stored_bytes()
        self.assertTrue(queue("all")[0]["workflow_complete"])
        self.assertEqual(scenario.stored_bytes(), original_bytes, "Completion checks must be read-only")
        original_publication = scenario.current()["publication"]
        scenario.pick_new_base(1)
        revised = queue("all")
        self.assertEqual([row["id"] for row in revised], [identifier])
        self.assertNotEqual(revised[0]["stage"], "published")
        self.assertFalse(revised[0]["history"])
        steps = {step["key"]: step["done"] for step in revised[0]["workflow_steps"]}
        self.assertFalse(steps["images"])
        self.assertFalse(steps["confirm"])
        self.assertFalse(steps["package"])
        self.assertFalse(steps["publish"])
        self.assertEqual([row["id"] for row in queue("all", status="workflow_incomplete")], [identifier])
        scenario.bench.archive_carousel(scenario.item["id"], scenario.body(archived=True))
        self.assertEqual(queue(), [])
        self.assertTrue(queue("all")[0]["source_archived"])
        self.assertEqual(queue("all", status="workflow_incomplete"), [])
        self.assertEqual(scenario.current()["publication"], original_publication)
        self.assertEqual(scenario.script()["status"], "published")
        body = {"request_id": uuid4().hex, "revision": scenario.bench.content_management.public()["revision"],
                "id": identifier, "management": {"archived": False}}
        status, _, restored = scenario.request("POST", "/api/content/manage", body)
        self.assertEqual(status, 200, restored)
        self.assertFalse(queue("all")[0]["archived"])
        self.assertFalse(scenario.current()["archived"])
        self.assertEqual(scenario.current()["publication"], original_publication)
        self.assertEqual(scenario.request("POST", "/api/content/manage", body)[2], restored)

    def test_management_metadata_and_request_receipts_survive_full_backup_restore(self):
        script = self.script(title="备份后仍可继续的内容")
        identifier = "script:" + script["family_id"]
        body = self.management_body(identifier, archived=True)
        saved = self.ok("POST", "/api/content/manage", body)
        metadata = self.server.app.content_management.path.read_bytes()
        manager = BackupManager(self.skill, self.runtime, self.root / "app",
            locks=(self.server.app.lock,), validator=fixture.SERVER.Workbench.validate_imported_data)
        archive = Path(manager.create_archive(self.root / "archives")["path"])
        with zipfile.ZipFile(archive) as contents:
            self.assertIn("runtime/creation/content-management.json", contents.namelist())
        self.manage(identifier, archived=False)
        manager.restore_archive(archive, self.runtime / "data-transfer" / "recovery")
        self.server.app.reload_imported_data()
        self.assertEqual(self.server.app.content_management.path.read_bytes(), metadata)
        restored = self.content(view="all")["items"][0]
        self.assertEqual(restored["id"], identifier)
        self.assertTrue(restored["archived"])
        self.assertEqual(self.ok("POST", "/api/content/manage", body), saved)

    def test_archived_rows_delete_without_backup_and_used_scripts_stay(self):
        unused = self.script(title="没人用的旧脚本")
        self.script(title="没人用的旧脚本 v2", parent_id=unused["id"])
        self.manage("script:" + unused["family_id"], archived=True)
        posted = self.script(title="已登记视频的脚本")
        self.ok("POST", "/api/scripts/" + posted["id"] + "/status", {
            "revision": self.ok("GET", "/api/creation")["revision"], "request_id": uuid4().hex,
            "script_status": {"status": "published", "video_id": "7999999999999999912", "published_on": "2026-10-01"}})
        self.manage("script:" + posted["family_id"], archived=True)
        used = self.script(title="旧版被制作单用到")
        self.job(used)
        self.script(title="旧版被制作单用到 v2", parent_id=used["id"])
        self.manage("script:" + used["family_id"], archived=True)
        task = self.completed_task()
        self.manage("task:" + task["task_id"], archived=True)
        job = self.job(self.script(title="要删除的制作单"))
        self.manage("video:" + job["id"], archived=True)
        active = self.job(self.script(title="没有归档的制作单"))
        targets = sorted(["script:" + unused["family_id"], "task:" + task["task_id"], "video:" + job["id"]])
        self.assertEqual(self.content(product="202H", view="all")["archived_deletable"], 3)
        data = self.server.app.data_transfer
        self.assertEqual(data.config["settings"]["directory"], "")
        # Missing or broken backup configuration must not affect this explicit deletion action.
        with patch.object(data, "status", side_effect=RuntimeError("备份状态无法读取")) as status, \
                patch.object(data, "backup", side_effect=RuntimeError("备份不可用")) as backup:
            preview = self.ok("POST", "/api/content/delete-archived", {"product": "202H", "preview": True})
            self.assertEqual(sorted(item["id"] for item in preview["items"]), targets)
            self.assertEqual(preview["counts"], {"task": 1, "video": 1, "carousel": 0, "script": 1})
            self.assertEqual(preview["kept"], {"script": 2, "busy": 0})
            self.assertNotIn("backup_ready", preview)
            asked = targets + ["video:" + active["id"], "script:" + posted["family_id"]]
            result = self.ok("POST", "/api/content/delete-archived", {"product": "202H", "ids": asked})
            status.assert_not_called()
            backup.assert_not_called()
        self.assertEqual((result["deleted"], result["failed"]), (3, 2))
        refused = {entry["id"]: entry["error"] for entry in result["results"] if not entry["ok"]}
        self.assertEqual(refused, {"video:" + active["id"]: "已不在归档里，没有删除。",
                                   "script:" + posted["family_id"]: "有版本已登记视频 ID，保留。"})
        self.assertIsNone(result["backup"])
        remaining = {row["id"] for row in self.content(product="202H", view="all")["items"]}
        self.assertTrue({"script:" + posted["family_id"], "script:" + used["family_id"], "video:" + active["id"]} <= remaining)
        self.assertFalse(set(targets) & remaining)
        families = {row["family_id"] for row in self.server.app.creation.public()["scripts"]}
        self.assertNotIn(unused["family_id"], families)
        self.assertTrue({posted["family_id"], used["family_id"]} <= families)
        self.assertEqual(self.request("GET", "/api/ai/tasks/" + task["task_id"])[0], 404)
        self.assertNotIn(job["id"], [row["id"] for row in self.ok("GET", "/api/production")["jobs"]])
        self.assertEqual(self.content(product="202H", view="all")["archived_deletable"], 0)


class ContentSnapshotReuseTests(unittest.TestCase):
    """Filter, search and page changes may reuse a young snapshot; refresh and any write may not."""
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    ok = ContentFlowHTTPTests.ok
    content = ContentFlowHTTPTests.content

    def reads(self):
        return patch.object(self.server.app, "_content_snapshot", wraps=self.server.app._content_snapshot)

    def test_reuse_serves_a_recent_snapshot_but_refresh_and_other_products_rebuild(self):
        with self.reads() as read:
            self.content(product="202H", view="all")
            self.assertEqual(read.call_count, 1)
            self.content(product="202H", view="all", reuse=1, kind="video")
            self.content(product="202H", view="attention", reuse=1, q="x", page=2)
            self.assertEqual(read.call_count, 1, "Filter, search and page changes reuse the snapshot")
            self.content(product="202H", view="all")
            self.assertEqual(read.call_count, 2, "A request without reuse always rebuilds")
            self.content(product="103N", view="all", reuse=1)
            self.assertEqual(read.call_count, 3, "Another product never borrows this snapshot")

    def test_reuse_expires_and_rejects_unknown_values(self):
        with self.reads() as read, patch("workbench_content.SNAPSHOT_REUSE_SECONDS", 0):
            self.content(product="202H")
            time.sleep(.01)
            self.content(product="202H", reuse=1)
            self.assertEqual(read.call_count, 2)
        self.assertEqual(self.request("GET", "/api/content?reuse=yes")[0], 400)

    def test_a_write_request_ends_reuse_even_after_its_response_is_sent(self):
        app = self.server.app
        with self.reads() as read:
            self.content(product="202H")
            app.content_write_started()
            try:
                self.content(product="202H", reuse=1)
                self.content(product="202H", reuse=1)
                self.assertEqual(read.call_count, 3, "Nothing is reused or kept while a write is in flight")
            finally:
                app.content_write_finished()
            self.content(product="202H", reuse=1)
            self.assertEqual(read.call_count, 4, "The first read after the write rebuilds")
            self.content(product="202H", reuse=1)
            self.assertEqual(read.call_count, 4, "and later filter changes reuse that fresh snapshot")

    def test_an_http_write_discards_the_snapshot(self):
        with self.reads() as read:
            self.content(product="202H")
            self.content(product="202H", reuse=1)
            self.assertEqual(read.call_count, 1)
            self.ok("POST", "/api/content/views", {"product": "202H", "action": "import", "views": []})
            self.content(product="202H", reuse=1)
            self.assertEqual(read.call_count, 2)


if __name__ == "__main__":
    unittest.main()
