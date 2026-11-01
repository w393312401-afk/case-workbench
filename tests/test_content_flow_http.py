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

    def test_each_timeline_of_an_order_keeps_its_own_calendar_day(self):
        job = self.job(self.script())
        take = "TAK-111111111111"
        data = self.server.app.production.read()
        row = next(row for row in data["jobs"] if row["id"] == job["id"])
        row.update(selected_take=take, timelines=[{"unique_id": "timeline-1", "take_id": take, "saved": True},
                                                  {"unique_id": "timeline-2", "take_id": take, "saved": True}])
        self.server.app.production.write(data)
        order, other = "video:" + job["id"], "video:" + job["id"] + "_tl_timeline-1"
        self.ok("POST", "/api/content/calendar", {"id": order, "calendar_date": "2026-11-10"})
        self.ok("POST", "/api/content/calendar", {"id": other, "calendar_date": "2026-11-12"})
        days = [self.ok("GET", "/api/content/calendar?" + urlencode({"id": key}))["calendar_date"] for key in (order, other)]
        self.assertEqual(days, ["2026-11-10", "2026-11-12"])
        status, _, _ = self.request("POST", "/api/content/calendar", {"id": "video:" + job["id"] + "_tl_unknown", "calendar_date": "2026-11-12"})
        self.assertEqual(status, 404)

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

class ContentSnapshotReuseTests(unittest.TestCase):
    """Filter, search and page changes reuse a snapshot; an old one or one from before a write is marked stale."""
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
            self.content(product="202H", view="all", reuse=1)
            self.assertEqual(read.call_count, 3, "nor does reading it evict this product's snapshot")

    def test_reuse_expires_and_rejects_unknown_values(self):
        with self.reads() as read, patch("workbench_content.SNAPSHOT_REUSE_SECONDS", 0), \
                patch("workbench_content.SNAPSHOT_STALE_SECONDS", 0):
            self.content(product="202H")
            time.sleep(.01)
            self.content(product="202H", reuse=1)
            self.assertEqual(read.call_count, 2)
        self.assertEqual(self.request("GET", "/api/content?reuse=yes")[0], 400)

    def test_an_older_snapshot_answers_at_once_but_says_it_is_stale(self):
        with self.reads() as read, patch("workbench_content.SNAPSHOT_REUSE_SECONDS", 0):
            self.assertIs(self.content(product="202H")["stale"], False)
            time.sleep(.01)
            self.assertIs(self.content(product="202H", reuse=1, kind="video")["stale"], True)
            self.assertEqual(read.call_count, 1, "A filter change never waits for a rebuild")
            self.assertIs(self.content(product="202H", kind="video")["stale"], False)
            self.assertEqual(read.call_count, 2, "The page's follow-up read rebuilds")

    def test_a_write_marks_older_snapshots_stale_and_nothing_from_its_window_is_kept(self):
        app = self.server.app
        with self.reads() as read:
            self.content(product="202H")
            app.content_write_started()
            try:
                self.assertIs(self.content(product="202H", reuse=1)["stale"], True, "An older snapshot answers, marked stale")
                self.content(product="202H")
                self.assertEqual(read.call_count, 2)
                self.assertIs(self.content(product="202H", reuse=1)["stale"], True, "A read during the write is not kept")
            finally:
                app.content_write_finished()
            self.assertIs(self.content(product="202H", reuse=1)["stale"], True, "and stays stale after the response is sent")
            self.assertEqual(read.call_count, 2, "Filter changes never wait for a rebuild")
            self.assertIs(self.content(product="202H")["stale"], False, "The page's follow-up read rebuilds")
            self.assertIs(self.content(product="202H", reuse=1)["stale"], False, "and later filter changes reuse it")
            self.assertEqual(read.call_count, 3)

    def test_read_only_extension_calls_keep_the_snapshot_fresh(self):
        key = self.ok("GET", "/api/public-stats/link")["key"]
        headers = {"X-TKL-Key": key, "Origin": "chrome-extension://abcdefghijklmnop"}
        self.content(product="202H")
        for path in ("/api/public-stats/ping", "/api/public-stats/watch", "/api/public-stats/products"):
            self.assertEqual(self.request("POST", path, {}, headers=headers, authenticated=False)[0], 200, path)
        self.assertIs(self.content(product="202H", reuse=1)["stale"], False, "The TK 看板 extension's polling changes nothing")
        self.request("POST", "/api/public-stats/push", {"videos": []}, headers=headers, authenticated=False)
        self.assertIs(self.content(product="202H", reuse=1)["stale"], True, "Its pushes may register publications")

    def test_an_http_write_makes_the_snapshot_stale(self):
        with self.reads() as read:
            self.content(product="202H")
            self.assertIs(self.content(product="202H", reuse=1)["stale"], False)
            self.ok("POST", "/api/content/views", {"product": "202H", "action": "import", "views": []})
            self.assertIs(self.content(product="202H", reuse=1)["stale"], True)
            self.assertEqual(read.call_count, 1)

if __name__ == "__main__":
    unittest.main()
