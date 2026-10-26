"""Real thread coordination with fake media/client boundaries, never live Resolve."""
import copy
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_production_editing as fixture
from codex_bridge import BridgeCancelled
from elevenlabs_client import VoiceError
from workbench_editing import project_key


class EditConcurrencyTests(unittest.TestCase):
    mutate = fixture.EditingTests.mutate
    timeline_job = fixture.EditingTests.timeline_job
    start = fixture.EditingTests.start
    wait = fixture.EditingTests.wait

    def setUp(self):
        fixture.EditingTests.setUp(self)
        self.addCleanup(self.finish_workers)

    def finish_workers(self):
        self.bench.stop_edits()
        deadline = time.monotonic() + 6
        while self.bench.edit_running() and time.monotonic() < deadline:
            time.sleep(.02)

    def two_timelines(self):
        job = self.timeline_job()
        with self.bench.lock:
            data = self.bench.production.read()
            current = next(row for row in data["jobs"] if row["id"] == job["id"])
            second = copy.deepcopy(current["timelines"][-1])
            second.update(unique_id="uid-2", name="Lab261010-202-2")
            current["timelines"].append(second)
            self.bench.production._write(data)
        return self.bench.production.job(job["id"])

    def test_busy_resolve_accepts_multiple_targets_and_cancel_does_not_release_foreign_lock(self):
        job = self.two_timelines()
        self.bench.resolve.lock.acquire()
        try:
            first = self.start(job, timeline="uid-1")["edit"]
            second = self.start(job, timeline="uid-2")["edit"]
            self.assertNotEqual(first["id"], second["id"])
            self.assertTrue(self.bench.edit_running())
            self.assertEqual(self.editor.calls, [])
            for edit in (first, second):
                self.bench.cancel_edit(job["id"], edit["id"])
            self.wait(job["id"])
            self.assertTrue(self.bench.resolve.busy())
            self.assertEqual(self.editor.calls, [])
            self.assertEqual([row["status"] for row in self.bench.production.job(job["id"])["edits"]], ["cancelled", "cancelled"])
        finally:
            self.bench.resolve.lock.release()

    def test_same_target_rejected_but_identical_request_replays_while_queued(self):
        job = self.timeline_job()
        body = {"request_id": uuid4().hex, "timeline": "uid-1", "materials": []}
        self.bench.resolve.lock.acquire()
        try:
            first = self.bench.start_edit(job["id"], body)
            again = self.bench.start_edit(job["id"], body)
            self.assertEqual(first["edit"]["id"], again["edit"]["id"])
            with self.assertRaisesRegex(VoiceError, "目标时间线"):
                self.start(job)
            self.assertEqual(len(self.bench.production.job(job["id"])["edits"]), 1)
            self.bench.cancel_edit(job["id"], first["edit"]["id"])
            self.wait(job["id"])
        finally:
            self.bench.resolve.lock.release()

    def test_same_project_selection_queues_but_editors_are_exclusive(self):
        job = self.two_timelines()
        planning, plan_entered = [], threading.Event()
        finish_plan, writer_entered, finish_writer = threading.Event(), threading.Event(), threading.Event()
        writers, count_lock = [], threading.Lock()
        counts = {"active": 0, "maximum": 0}

        def plan(edit_id, *args):
            planning.append(edit_id)
            plan_entered.set()
            if not finish_plan.wait(5):
                raise RuntimeError("planning test timed out")
            return {"workers": 3, "completed": 3, "failed": 0}

        def write(prompt, *, cwd, **options):
            with count_lock:
                counts["active"] += 1
                counts["maximum"] = max(counts["maximum"], counts["active"])
                writers.append(Path(cwd).name)
            try:
                self.assertTrue(self.bench.resolve.busy())
                writer_entered.set()
                if not finish_writer.wait(5):
                    raise RuntimeError("writer test timed out")
                return fixture.REPORT
            finally:
                with count_lock:
                    counts["active"] -= 1

        with patch.object(self.bench, "_plan_edit", side_effect=plan), patch.object(self.editor, "run", side_effect=write):
            try:
                first = self.start(job, timeline="uid-1")["edit"]
                second = self.start(job, timeline="uid-2")["edit"]
                self.assertTrue(plan_entered.wait(5))
                time.sleep(.2)
                # The second snapshot waits until the first plan is registered.
                self.assertEqual(len(planning), 1)
                self.assertFalse(self.bench.resolve.busy())
                finish_plan.set()
                self.assertTrue(writer_entered.wait(5))
                time.sleep(.15)
                self.assertEqual(len(writers), 1)
                finish_writer.set()
                self.wait(job["id"])
                self.assertEqual(counts["maximum"], 1)
                self.assertCountEqual(writers, [first["id"], second["id"]])
                self.assertEqual(planning, [first["id"], second["id"]])
                self.assertEqual([row["status"] for row in self.bench.production.job(job["id"])["edits"]], ["completed", "completed"])
            finally:
                finish_plan.set()
                finish_writer.set()

    def test_next_edit_snapshots_before_the_planned_edit_places_shots_and_plans_alongside_it(self):
        job = self.two_timelines()
        events, lock = [], threading.Lock()
        second_planning, first_placing = threading.Event(), threading.Event()
        footage = {"key": "ZS1-aaaa", "media_id": "m1", "media_path": "", "source": [10, 20]}
        original = fixture.Runner.__call__

        def log(*event):
            with lock:
                events.append(event)

        def runner(runner_self, args, input, **options):
            request = json.loads(input.decode("utf-8"))
            if request["command"] == "candidates":
                held = [entry["timeline"] for project in getattr(self.bench, "_edit_occupied", {}).values() for entry in project.values()]
                log("snapshot", request["target_uid"], held)
            return original(runner_self, args, input, **options)

        def plan(edit_id, folder, *args):
            log("plan", edit_id)
            if len([event for event in events if event[0] == "plan"]) == 1:
                # The second edit is already waiting for this project's selection when the first one finishes planning.
                deadline = time.monotonic() + 5
                while not any(len(queue) == 2 for queue in self.bench._edit_selection_waiters.values()):
                    if time.monotonic() > deadline:
                        raise RuntimeError("the second edit never queued for selection")
                    time.sleep(.01)
                Path(folder, "candidates.json").write_text(json.dumps({"candidates": [footage]}), encoding="utf-8")
                Path(folder, "planning-results.json").write_text(json.dumps(
                    [{"worker": 1, "status": "completed", "output": "首选 ZS1-aaaa", "picks": ["ZS1-aaaa"], "unknown_picks": 0}]), encoding="utf-8")
            else:
                second_planning.set()
                # The second plan runs while the first edit is placing shots.
                if not first_placing.wait(5):
                    raise RuntimeError("the first edit never started placing shots")
            return {"workers": 1, "completed": 1, "failed": 0}

        def write(prompt, *, cwd, **options):
            log("place", Path(cwd).name)
            first_placing.set()
            if len([event for event in events if event[0] == "place"]) == 1 and not second_planning.wait(5):
                raise RuntimeError("the second edit did not plan alongside the first one")
            return fixture.REPORT

        phases, original_phase = [], self.bench._edit_phase

        def phase(edit_id, name, message):
            phases.append((edit_id, name, message))
            return original_phase(edit_id, name, message)

        with patch.object(fixture.Runner, "__call__", runner), patch.object(self.bench, "_plan_edit", side_effect=plan), \
             patch.object(self.editor, "run", side_effect=write), patch.object(self.bench, "_edit_phase", side_effect=phase):
            first = self.start(job, timeline="uid-1")["edit"]
            second = self.start(job, timeline="uid-2")["edit"]
            self.wait(job["id"])
        # While the next edit prepares, the first one says what it is waiting for; the last edit of the queue does not.
        waiting = {edit_id: message for edit_id, name, message in phases if name == "waiting_editing"}
        self.assertEqual(waiting[first["id"]], "等待达芬奇：同项目下一条剪辑先整理候选画面，之后开始剪辑。")
        self.assertEqual(waiting[second["id"]], "等待达芬奇：执行达芬奇剪辑")
        order = [event[:2] for event in events]
        # First snapshot and plan, then the next edit's snapshot, and only then does the first edit place shots.
        # The second plan and the first placement overlap (each waited for the other above), so either may log first.
        self.assertEqual(order[:3], [("snapshot", "uid-1"), ("plan", first["id"]), ("snapshot", "uid-2")])
        self.assertCountEqual(order[3:5], [("plan", second["id"]), ("place", first["id"])])
        self.assertEqual(order[5:], [("place", second["id"])])
        # That snapshot already saw the first edit's first choices as being cut.
        self.assertEqual(events[2][2], [job["timelines"][0]["name"]])
        self.assertEqual([row["status"] for row in self.bench.production.job(job["id"])["edits"]], ["completed", "completed"])

    def test_a_later_edit_snapshots_before_a_planned_edit_still_waiting_to_place_shots(self):
        job = self.two_timelines()
        events, lock = [], threading.Lock()
        hold, held = threading.Event(), threading.Event()
        original = fixture.Runner.__call__

        def log(*event):
            with lock:
                events.append(event)

        def runner(runner_self, args, input, **options):
            request = json.loads(input.decode("utf-8"))
            if request["command"] == "candidates":
                log("snapshot", request["target_uid"])
            return original(runner_self, args, input, **options)

        def foreign():
            # Another project's edit or a carousel step holds DaVinci while the first edit finishes planning.
            with self.bench.resolve.access(timeout=5):
                held.set()
                hold.wait(5)

        def plan(edit_id, folder, *args):
            log("plan", edit_id)
            if len([event for event in events if event[0] == "plan"]) == 1:
                threading.Thread(target=foreign, daemon=True).start()
                if not held.wait(5):
                    raise RuntimeError("the foreign task never got DaVinci")
            return {"workers": 1, "completed": 1, "failed": 0}

        def write(prompt, *, cwd, **options):
            log("place", Path(cwd).name)
            return fixture.REPORT

        def until(condition):
            deadline = time.monotonic() + 5
            while not condition():
                if time.monotonic() > deadline:
                    raise AssertionError("timed out")
                time.sleep(.01)

        try:
            with patch.object(fixture.Runner, "__call__", runner), patch.object(self.bench, "_plan_edit", side_effect=plan), \
                 patch.object(self.editor, "run", side_effect=write):
                first = self.start(job, timeline="uid-1")["edit"]
                # Nobody waited at the gate, so the first edit queues to place shots behind the foreign task.
                until(lambda: getattr(self.bench, "_edit_editing_places", {}) and len(self.bench.resolve._access_waiters) == 1)
                second = self.start(job, timeline="uid-2")["edit"]
                # The second snapshot is queued ahead of the first edit's waiting request.
                until(lambda: len(self.bench.resolve._access_waiters) == 2)
                hold.set()
                self.wait(job["id"])
        finally:
            hold.set()
        order = [event[:2] for event in events]
        self.assertEqual(order[:3], [("snapshot", "uid-1"), ("plan", first["id"]), ("snapshot", "uid-2")])
        self.assertLess(order.index(("snapshot", "uid-2")), order.index(("place", first["id"])))
        self.assertIn(("place", second["id"]), order)
        self.assertEqual(getattr(self.bench, "_edit_editing_places", {}), {})

    def test_handover_waits_only_for_the_next_edit_to_take_its_place(self):
        self.bench._reserve("EDT-handover")
        self.addCleanup(self.bench._forget, "EDT-handover")
        waiters = self.bench.__dict__.setdefault("_edit_selection_waiters", {})
        hand_over = lambda: self.bench._hand_over_selection("EDT-handover", "202H")

        def timed(prepare):
            successor = SimpleNamespace(edit_id="EDT-next", queued=threading.Event())
            waiters["202H"] = [successor]
            timer = threading.Timer(.15, prepare, args=(successor,))
            timer.start()
            started = time.monotonic()
            try:
                handed = hand_over()
            finally:
                timer.cancel()
                waiters.pop("202H", None)
            return time.monotonic() - started, handed

        # Nobody waiting for this project: no wait at all, and nobody queues ahead.
        started = time.monotonic()
        self.assertIs(hand_over(), False)
        self.assertLess(time.monotonic() - started, .05)
        # The next edit takes its place in the Resolve queue (it now goes first), or stops before reaching it.
        elapsed, handed = timed(lambda successor: successor.queued.set())
        self.assertLess(elapsed, 1)
        self.assertIs(handed, True)
        elapsed, handed = timed(lambda successor: waiters["202H"].remove(successor))
        self.assertLess(elapsed, 1)
        self.assertIs(handed, False)
        # A next edit that never queues holds this one up for HANDOVER_WAIT at most, and a stop request ends the wait.
        with patch("workbench_editing.HANDOVER_WAIT", .3):
            elapsed, handed = timed(lambda successor: None)
        self.assertGreaterEqual(elapsed, .3)
        self.assertLess(elapsed, 1)
        self.assertIs(handed, False)
        elapsed, handed = timed(lambda successor: self.bench._edit_cancel["EDT-handover"].set())
        self.assertLess(elapsed, 1)
        self.assertIs(handed, False)

    def test_only_a_selection_that_ended_normally_hands_over(self):
        self.bench._reserve("EDT-gate")
        self.addCleanup(self.bench._forget, "EDT-gate")
        with patch.object(self.bench, "_hand_over_selection", return_value=True) as hand_over:
            with self.assertRaises(RuntimeError):
                with self.bench._edit_selection_access("EDT-gate", "202H"):
                    raise RuntimeError("planning broke")
            hand_over.assert_not_called()
            with self.bench._edit_selection_access("EDT-gate", "202H") as turn:
                self.assertFalse(turn.queued.is_set())
                self.assertIs(turn.handed_over, False)
            hand_over.assert_called_once_with("EDT-gate", "202H")
            self.assertIs(turn.handed_over, True)
        self.assertEqual(getattr(self.bench, "_edit_selection_waiters", {}).get("202H"), None)

    def test_same_named_projects_of_different_channels_are_different_projects(self):
        lab = {"project": "202H", "creator": "Lab", "folder": "A本土店渠道号-Lab", "project_id": "id-lab"}
        self.assertNotEqual(project_key(lab), project_key({**lab, "creator": "Zack", "folder": "B跨境店渠道号-zack", "project_id": "id-zack"}))
        # Records of one channel agree whether or not they recorded the folder and id (only built since 2026-10-19).
        self.assertEqual(project_key(lab), project_key({"project": "202H", "creator": "lab"}))
        self.assertNotEqual(project_key(lab), project_key({**lab, "project": "135"}))
        # Without a creator the folder, then the project id, tells projects apart.
        self.assertEqual(project_key({"project": "202H", "folder": "C英国渠道号", "project_id": "id-uk"}), ("202H", "C英国渠道号"))
        self.assertEqual(project_key({"project": "202H", "project_id": "id-uk"}), ("202H", "id-uk"))

    def test_another_channel_does_not_wait_for_this_channels_selection(self):
        job = self.two_timelines()
        with self.bench.lock:
            data = self.bench.production.read()
            current = next(row for row in data["jobs"] if row["id"] == job["id"])
            current["timelines"][-1].update(creator="Zack")
            self.bench.production._write(data)
        job = self.bench.production.job(job["id"])
        planned, other_snapshot, overlapped = threading.Event(), threading.Event(), []
        original = fixture.Runner.__call__

        def runner(runner_self, args, input, **options):
            request = json.loads(input.decode("utf-8"))
            if request["command"] == "candidates" and request["target_uid"] == "uid-2":
                other_snapshot.set()
            return original(runner_self, args, input, **options)

        def plan(edit_id, folder, *args):
            if not planned.is_set():
                planned.set()
                # Zack's snapshot happens while Lab is still planning: different projects share no selection queue.
                overlapped.append(other_snapshot.wait(3))
            return {"workers": 1, "completed": 1, "failed": 0}

        with patch.object(fixture.Runner, "__call__", runner), patch.object(self.bench, "_plan_edit", side_effect=plan):
            self.start(job, timeline="uid-1")
            self.assertTrue(planned.wait(5))
            self.start(job, timeline="uid-2")
            self.wait(job["id"])
        self.assertEqual(overlapped, [True])
        self.assertEqual([row["status"] for row in self.bench.production.job(job["id"])["edits"]], ["completed", "completed"])

    def test_failed_planning_falls_back_and_releases_application_for_next_task(self):
        job = self.two_timelines()
        with patch.object(self.bench, "_plan_edit", side_effect=RuntimeError("planner offline")):
            self.start(job, timeline="uid-1")
            self.start(job, timeline="uid-2")
            self.wait(job["id"])
        edits = self.bench.production.job(job["id"])["edits"]
        self.assertEqual(len(self.editor.calls), 2)
        self.assertTrue(all(edit["status"] == "completed" and edit["planning"]["error"] == "planner offline" for edit in edits))
        self.assertFalse(self.bench.resolve.busy())

    def test_planned_footage_is_held_until_the_edit_ends(self):
        job = self.timeline_job()
        entered, finish = threading.Event(), threading.Event()
        footage = {"key": "ZS1-aaaa", "media_id": "m1", "media_path": "", "source": [10, 20]}

        def plan(edit_id, folder, *args):
            Path(folder, "candidates.json").write_text(json.dumps({"candidates": [footage]}), encoding="utf-8")
            Path(folder, "planning.md").write_text("建议用 ZS1-aaaa", encoding="utf-8")
            Path(folder, "planning-results.json").write_text(json.dumps(
                [{"worker": 1, "status": "completed", "output": "建议用 ZS1-aaaa", "picks": ["ZS1-aaaa"], "unknown_picks": 0}]), encoding="utf-8")
            return {"workers": 1, "completed": 1, "failed": 0}

        def write(*args, **options):
            entered.set()
            finish.wait(5)
            return fixture.REPORT

        with patch.object(self.bench, "_plan_edit", side_effect=plan), patch.object(self.editor, "run", side_effect=write):
            edit = self.start(job)["edit"]
            self.assertTrue(entered.wait(5))
            held = [entry["spans"] for project in self.bench._edit_occupied.values() for entry in project.values()]
            self.assertEqual(held, [[{"media_id": "m1", "media_path": "", "source": [10, 20]}]])
            finish.set()
            self.wait(job["id"])
        self.assertEqual(getattr(self.bench, "_edit_occupied", {}), {})
        planning = self.bench.production.job(job["id"])["edits"][0]["planning"]
        self.assertEqual((planning["occupied"], planning["unpicked"]), (1, 0))
        self.assertEqual(edit["status"], "running")

    def test_footage_only_mentioned_in_the_plan_is_not_held(self):
        job = self.timeline_job()
        entered, finish = threading.Event(), threading.Event()
        chosen = {"key": "ZS1-aaaa", "media_id": "m1", "media_path": "", "source": [10, 20]}
        mentioned = {"key": "ZS2-bbbb", "media_id": "m1", "media_path": "", "source": [20, 30]}

        def plan(edit_id, folder, *args):
            Path(folder, "candidates.json").write_text(json.dumps({"candidates": [chosen, mentioned]}), encoding="utf-8")
            Path(folder, "planning.md").write_text("首选 ZS1-aaaa，备选 ZS2-bbbb，不要用 ZS2。", encoding="utf-8")
            Path(folder, "planning-results.json").write_text(json.dumps([
                {"worker": 1, "status": "completed", "output": "首选 ZS1-aaaa，备选 ZS2-bbbb", "picks": ["ZS1-aaaa"], "unknown_picks": 0},
                {"worker": 2, "status": "completed", "output": "ZS2-bbbb 可用", "picks": None, "unknown_picks": 0},
                {"worker": 3, "status": "failed", "error": "offline", "picks": ["ZS2-bbbb"]},
                # A list whose every entry named nothing is no usable list; an honest empty list is.
                {"worker": 4, "status": "completed", "output": "…", "picks": [], "unknown_picks": 2},
                {"worker": 5, "status": "completed", "output": "本组无首选", "picks": [], "unknown_picks": 0}]), encoding="utf-8")
            return {"workers": 3, "completed": 2, "failed": 1}

        def write(*args, **options):
            entered.set()
            finish.wait(5)
            return fixture.REPORT

        with patch.object(self.bench, "_plan_edit", side_effect=plan), patch.object(self.editor, "run", side_effect=write):
            self.start(job)
            self.assertTrue(entered.wait(5))
            held = [entry["spans"] for project in self.bench._edit_occupied.values() for entry in project.values()]
            # Only worker 1's list counts: worker 2 gave none, worker 3 did not finish, workers 4 and 5 named nothing.
            self.assertEqual(held, [[{"media_id": "m1", "media_path": "", "source": [10, 20]}]])
            finish.set()
            self.wait(job["id"])
        planning = self.bench.production.job(job["id"])["edits"][0]["planning"]
        self.assertEqual((planning["occupied"], planning["unpicked"]), (1, 2))

    def test_cancel_during_planning_never_starts_editor(self):
        job = self.timeline_job()
        entered = threading.Event()

        def plan(edit_id, *args):
            entered.set()
            self.bench._edit_cancel[edit_id].wait(5)
            raise BridgeCancelled("cancelled")

        with patch.object(self.bench, "_plan_edit", side_effect=plan):
            edit = self.start(job)["edit"]
            self.assertTrue(entered.wait(5))
            self.bench.cancel_edit(job["id"], edit["id"])
            self.assertEqual(self.wait(job["id"])["status"], "cancelled")
        self.assertEqual(self.editor.calls, [])
        self.assertFalse(self.bench.resolve.busy())

    def test_pending_result_is_not_ignored_while_another_task_is_live(self):
        job = self.two_timelines()
        self.bench._reserve("EDT-other")
        try:
            with patch.object(self.bench, "recover_edit_results", return_value={"problems": [{"edit_id": "EDT-prior"}]}):
                with self.assertRaisesRegex(VoiceError, "尚未写入"):
                    self.start(job)
            self.assertEqual(self.bench.production.job(job["id"])["edits"], [])
        finally:
            self.bench._forget("EDT-other")
