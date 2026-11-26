"""The away-mode execution pool: orders set up ahead are voiced once, chosen, then cut, and wait for review."""
import json
import tempfile
import unittest
from pathlib import Path

from away_mode import MAX_ATTEMPTS, AwayError, AwayMode
from test_away_mode import Clock, make_app

VOICE = {"voice_id": "abcdefghij12", "model_id": "eleven_v4", "settings": {"stability": 0.5, "bad": "x"}, "market": "us"}


def voiced(app, quality="passed", error=None):
    """generate_take adds one take to the order, as the workbench does; select_take records the choice."""
    app.takes, app.selects = [], []

    def generate(job_id, body):
        app.takes.append((job_id, body))
        if error:
            raise RuntimeError(error)
        job = app.production.jobs[job_id]
        job.setdefault("takes", []).append({"id": f"TAK-{len(app.takes)}", "created_at": "2026-11-19T01:00:00", "duration": 31.5,
                                            "quality": {"status": quality}})
        return {"job": job}

    def select(job_id, body):
        app.selects.append((job_id, body))
        app.production.jobs[job_id]["selected_take"] = body["take_id"]
        return {"job": app.production.jobs[job_id]}
    app.generate_take, app.select_take = generate, select
    app.production.revision = lambda data: "rev-1"


class PoolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = make_app()
        self.away = AwayMode(self.app, self.root, clock=Clock(), start_thread=False)
        self.away._themes = lambda product: []
        self.away._catalog = lambda product: []

    def add(self, job_id="PRD-1", voice=VOICE):
        return self.away.pool_change({"op": "add", "job_id": job_id, "voice": voice})

    def item(self, kind):
        return next(row for row in self.away.current()["items"] if row["kind"] == kind)

    def settle(self):
        for holder in list(self.away._voices.values()) + list(self.away._edit_starts.values()):
            holder["thread"].join(2)
        self.away.tick()

    def test_add_keeps_clean_voice_settings_and_update_and_remove_work(self):
        pool = self.add()["pool"]
        self.assertEqual(pool[0]["voice"]["settings"], {"stability": 0.5, "similarity_boost": 0.75})  # the model's own keys, defaults filled
        self.assertEqual(pool[0]["product"], "202H")
        with self.assertRaises(AwayError):
            self.add(voice={**VOICE, "voice_id": "__new"})
        self.away.pool_change({"op": "update", "job_id": "PRD-1", "creator": "Zack", "materials": ["迷你刀", " "]})
        self.assertEqual(self.away.pool("202H")[0]["materials"], ["迷你刀"])
        self.away.pool_change({"op": "remove", "job_id": "PRD-1"})
        self.assertEqual(self.away.pool(), [])

    def test_only_orders_marked_ready_start_ticked(self):
        self.add()
        job = self.away.plan("202H")["pool"]["jobs"][0]
        self.assertEqual((job["ready"], job["default"]), (False, False))
        with self.assertRaises(AwayError):  # nothing ticked to cut from yet
            self.away.pool_change({"op": "update", "job_id": "PRD-1", "ready": True, "materials": []})
        self.away.pool_change({"op": "update", "job_id": "PRD-1", "ready": True, "creator": "Zack", "materials": ["迷你刀"]})
        job = self.away.plan("202H")["pool"]["jobs"][0]
        self.assertEqual((job["ready"], job["default"], job["materials"]), (True, True, ["迷你刀"]))
        self.away.pool_change({"op": "update", "job_id": "PRD-1", "ready": False})
        self.assertFalse(self.away.plan("202H")["pool"]["jobs"][0]["ready"])

    def test_plan_shows_the_pool_and_voiced_orders_waiting_to_be_put_in(self):
        plan = self.away.plan("202H")
        self.assertNotIn("edit", [row["kind"] for row in plan["items"]])
        self.assertNotIn("tags", [row["kind"] for row in plan["items"]])
        self.assertEqual([row["id"] for row in plan["pool"]["waiting"]], ["PRD-2"])
        self.away.pool_change({"op": "add", "job_id": "PRD-2", "voice": None})
        plan = self.away.plan("202H")
        self.assertEqual([(row["id"], row["voiced"], row["voice"]) for row in plan["pool"]["jobs"]], [("PRD-2", True, "已配音，直接剪")])
        self.assertEqual(plan["pool"]["waiting"], [])
        self.assertIn("执行池以外的配音", plan["never"])
        with self.assertRaises(AwayError):  # not voiced yet and no voice in the library to start from
            self.away.pool_change({"op": "add", "job_id": "PRD-1"})

    def test_put_in_from_the_order_starts_from_the_default_voice_and_is_edited_in_the_pool(self):
        records = self.app.production.records
        self.app.production.records = lambda: {**records(), "defaults": {"202H": "defaultvoice01"}, "voices": [
            {"voice_id": "othervoice0001", "name": "别的"},
            {"voice_id": "defaultvoice01", "name": "美国厨房-男", "model_id": "eleven_multilingual_v2", "settings": {"speed": 1.1}}]}
        entry = self.away.pool_change({"op": "add", "job_id": "PRD-1"})["pool"][0]
        self.assertEqual((entry["voice"]["voice_id"], entry["voice"]["model_id"], entry["voice"]["settings"]["speed"]),
                         ("defaultvoice01", "eleven_multilingual_v2", 1.1))
        voiced = self.away.pool_change({"op": "add", "job_id": "PRD-2"})["pool"][1]
        self.assertIsNone(voiced["voice"], "a voiced order starts as 'cut it as it is'")
        self.away.pool_change({"op": "update", "job_id": "PRD-2", "voice": VOICE})
        self.assertEqual(self.away.pool()[1]["voice"]["voice_id"], "abcdefghij12")
        self.away.pool_change({"op": "add", "job_id": "PRD-2"})  # put in again from the order or the script page
        self.assertEqual(self.away.pool()[1]["voice"]["voice_id"], "abcdefghij12", "what was set in the pool stays")
        with self.assertRaises(AwayError):
            self.away.pool_change({"op": "update", "job_id": "PRD-1", "voice": None})  # nothing chosen to cut with
        with self.assertRaises(AwayError):
            self.away.pool_change({"op": "update", "job_id": "PRD-1", "voice": {**VOICE, "settings": {"stability": 3}}})
        plan = self.away.plan("202H")["pool"]
        self.assertIn("eleven_v4", [row["id"] for row in plan["models"]])
        self.assertEqual(plan["jobs"][0]["voice_config"]["voice_id"], "defaultvoice01")
        self.assertEqual([row["can_direct"] for row in plan["jobs"]], [False, True])

    def test_a_voiced_order_is_cut_straight_away_and_a_closed_davinci_is_not_retried(self):
        voiced(self.app)
        self.away.pool_change({"op": "add", "job_id": "PRD-2", "voice": None})
        self.app.resolve_closed = True
        self.away.start({"product": "202H", "items": [{"kind": "pool", "jobs": [{"id": "PRD-2", "creator": "Lab", "materials": ["主素材"]}]}]})
        self.assertEqual([row["kind"] for row in self.away.current()["items"]], ["edit", "check"])
        self.away.tick()
        edit = self.item("edit")
        self.assertEqual((edit["status"], edit["attempts"]), ("failed", MAX_ATTEMPTS))
        self.assertIn("达芬奇没有打开", edit["error"])
        self.assertEqual([row["job_id"] for row in self.away.pool()], ["PRD-2"], "nothing started, so it stays in the pool")
        self.away.tick()
        self.assertEqual(self.away.current()["status"], "done")
        self.app.resolve_closed = False
        self.away.start({"product": "202H", "items": [{"kind": "pool", "jobs": [{"id": "PRD-2", "creator": "Lab", "materials": ["主素材"]}]}]})
        self.away.tick()
        self.settle()
        self.assertEqual(self.app.takes, [], "voiced already: nothing is voiced again")
        self.assertEqual(self.app.builds[0][0], "PRD-2")
        self.assertEqual(self.away.pool(), [])

    def test_a_new_order_gets_a_creator_and_that_creators_latest_footage(self):
        self.app.production.jobs["PRD-1"]["lines"] = [{"zh": "一句"}]
        self.app.production.jobs["PRD-9"] = {"id": "PRD-9", "product": "202H", "title": "Lab 剪过", "selected_take": "T1",
            "timelines": [{"unique_id": "TL-9", "creator": "Lab", "project": "Lab-202H", "built_at": "2026-11-18"}],
            "edits": [{"timeline_uid": "TL-9", "status": "completed", "started_at": "2026-11-18T00:00:00", "materials": ["维氏厨", "配件-瓷砖"]}]}
        self.add()
        row = self.away.plan("202H")["pool"]["jobs"][0]
        self.assertEqual(row["script"], [{"say": "一句", "types": [], "shot": ""}])
        self.assertEqual(row["creator"], "Zack")
        self.assertEqual(row["materials"], [])
        self.assertEqual(row["latest"]["Lab"], ["维氏厨", "配件-瓷砖"])
        self.away.pool_change({"op": "update", "job_id": "PRD-1", "creator": "Lab"})
        row = self.away.plan("202H")["pool"]["jobs"][0]
        self.assertEqual((row["creator"], row["materials"]), ("Lab", ["维氏厨", "配件-瓷砖"]))
        self.assertEqual([item["name"] for item in row["libraries"]["Lab"]["names"]], ["维氏厨", "配件-瓷砖"])
        self.assertEqual(row["libraries"]["Zack"]["names"], [])

    def test_pool_order_is_voiced_chosen_then_cut_and_leaves_the_pool(self):
        voiced(self.app)
        self.add()
        with self.assertRaises(AwayError):  # a creator and footage are needed to build the timeline
            self.away.start({"product": "202H", "items": [{"kind": "pool", "jobs": [{"id": "PRD-1"}]}]})
        self.away.start({"product": "202H", "items": [{"kind": "pool", "jobs": [{"id": "PRD-1", "creator": "Zack", "materials": ["迷你刀"]}]}]})
        self.away.tick()
        self.assertEqual(self.item("voice")["status"], "running")
        self.assertEqual(self.item("edit")["status"], "pending", "the cut waits for its own voiceover")
        self.assertEqual(self.away.pool(), [], "it leaves the pool once voicing starts")
        self.settle()
        job, body = self.app.takes[0]
        self.assertEqual((job, body["voice_id"], body["settings"]), ("PRD-1", "abcdefghij12", {"stability": 0.5, "similarity_boost": 0.75}))
        self.assertNotIn("market", body)
        self.assertEqual(self.app.selects[0][1]["take_id"], "TAK-1")
        self.assertEqual(self.app.selects[0][1]["revision"], "rev-1")
        self.assertEqual(self.item("voice")["status"], "done")
        self.settle()
        self.assertEqual(self.app.builds[0][0], "PRD-1")
        self.assertEqual(self.app.builds[0][1]["creator"], "Zack")
        job, body = self.app.edit_starts[0]
        self.assertEqual((job, body["materials"], body.get("recut_from")), ("PRD-1", ["迷你刀"], None))
        self.app.production.jobs["PRD-1"]["edits"][-1]["status"] = "awaiting_review"
        self.settle()
        report = self.away.current()["report"]
        self.assertTrue(any("配了一条" in row["text"] or "配好了" in row["text"] for row in report["done"]))
        self.assertTrue(any("剪完了" in row["text"] for row in report["waiting"]))

    def test_a_failed_voiceover_is_not_retried_and_its_cut_is_skipped(self):
        voiced(self.app, quality="failed")
        self.add()
        self.away.start({"product": "202H", "items": [{"kind": "pool", "jobs": [{"id": "PRD-1", "creator": "Zack", "materials": ["迷你刀"]}]}]})
        self.away.tick()
        self.settle()
        voice = self.item("voice")
        self.assertEqual(voice["status"], "failed")
        self.assertIn("对齐没过", voice["error"])
        self.assertEqual(voice["attempts"], MAX_ATTEMPTS)
        self.away.tick()
        self.assertEqual(self.item("edit")["status"], "skipped")
        self.assertEqual(len(self.app.takes), 1)
        self.assertEqual(self.app.selects, [])
        self.assertEqual(self.app.edit_starts, [])
        back = self.away.pool()
        self.assertEqual([(row["job_id"], row["creator"], row["materials"], row["ready"]) for row in back], [("PRD-1", "Zack", ["迷你刀"], False)],
                         "back in the pool with its settings, but not ticked: voicing again costs money")
        self.assertEqual(back[0]["voice"]["voice_id"], "abcdefghij12")

    def test_stopping_after_the_voiceover_puts_the_order_back_to_be_cut(self):
        voiced(self.app)
        self.add()
        self.away.start({"product": "202H", "items": [{"kind": "pool", "jobs": [{"id": "PRD-1", "creator": "Zack", "materials": ["迷你刀"]}]}]})
        self.away.tick()
        self.away.stop()  # stopped while voicing: the voiceover finishes, the cut never starts
        self.settle()
        self.assertEqual(self.item("voice")["status"], "done")
        self.assertEqual(self.item("edit")["status"], "cancelled")
        back = self.away.pool()
        self.assertEqual([(row["job_id"], row["creator"], row["materials"], row["voice"], row["ready"]) for row in back],
                         [("PRD-1", "Zack", ["迷你刀"], None, True)])
        self.away.tick()
        self.assertEqual(len(self.away.pool()), 1, "put back once")

    def test_a_stopped_run_leaves_unstarted_orders_in_the_pool(self):
        voiced(self.app)
        self.add()
        self.away.start({"product": "202H", "items": [{"kind": "pool", "jobs": [{"id": "PRD-1", "creator": "Zack", "materials": ["迷你刀"]}]}]})
        self.away.stop()
        self.away.tick()
        self.assertEqual([row["job_id"] for row in self.away.pool()], ["PRD-1"])
        self.assertEqual(self.app.takes, [])

    def test_a_restart_never_voices_again(self):
        voiced(self.app)
        self.add()
        self.away.start({"product": "202H", "items": [{"kind": "pool", "jobs": [{"id": "PRD-1", "creator": "Zack", "materials": ["迷你刀"]}]}]})
        data = json.loads((self.root / "away-mode.json").read_text(encoding="utf-8"))
        data["current"]["items"][0].update(status="running", attempts=1)
        (self.root / "away-mode.json").write_text(json.dumps(data), encoding="utf-8")
        again = AwayMode(self.app, self.root, clock=Clock(), start_thread=False)
        voice = next(row for row in again.current()["items"] if row["kind"] == "voice")
        self.assertEqual(voice["status"], "failed")
        self.assertIn("扣费", voice["error"])


    def test_cuts_of_one_creator_run_together_grouped_and_another_creator_waits(self):
        for job_id in ("PRD-3", "PRD-4"):
            self.app.production.jobs[job_id] = {"id": job_id, "product": "202H", "title": job_id, "selected_take": "T1"}
        for job_id in ("PRD-2", "PRD-3", "PRD-4"):
            self.away.pool_change({"op": "add", "job_id": job_id, "voice": None})
        jobs = [{"id": "PRD-2", "creator": "Lab", "materials": ["主素材"]}, {"id": "PRD-3", "creator": "Zack", "materials": ["迷你刀"]},
                {"id": "PRD-4", "creator": "Lab", "materials": ["主素材"]}]
        self.away.start({"product": "202H", "items": [{"kind": "pool", "jobs": jobs}]})
        self.away.tick()
        self.settle()
        # Lab's two orders share Lab's DaVinci project; Zack's, put in second, waits for both
        self.assertEqual([job for job, _ in self.app.builds], ["PRD-2", "PRD-4"])
        cuts = {row["params"]["job_id"]: row for row in self.away.current()["items"] if row["kind"] == "edit"}
        self.assertEqual(cuts["PRD-3"]["status"], "pending")
        self.app.production.jobs["PRD-2"]["edits"][-1]["status"] = "awaiting_review"
        self.settle()
        self.assertEqual(len(self.app.builds), 2, "one Lab cut still runs, so Zack keeps waiting")
        self.app.production.jobs["PRD-4"]["edits"][-1]["status"] = "awaiting_review"
        self.settle()
        self.settle()
        self.assertEqual([job for job, _ in self.app.builds], ["PRD-2", "PRD-4", "PRD-3"])

    # ---------- steps already done are not done again (fly 11-23) ----------
    def passed_take(self, job_id="PRD-1", take_id="TAK-old", sha=None, voice="abcdefghij12"):
        take = {"id": take_id, "created_at": "2026-11-23T02:03:08", "voice_id": voice, "model_id": "eleven_v4",
                "settings": {"stability": 0.5, "similarity_boost": 0.75}, "market": "us", "quality": {"status": "passed"}}
        if sha:
            take["tts_sha256"] = sha
        self.app.production.jobs[job_id].setdefault("takes", []).append(take)
        return take

    def run_pool(self, job_id="PRD-1", creator="Zack", materials=("迷你刀",)):
        self.away.plan("202H")
        self.away.start({"product": "202H", "items": [{"kind": "pool", "jobs": [{"id": job_id, "creator": creator, "materials": list(materials)}]}]})
        self.away.tick()
        self.settle()

    def test_a_voiceover_made_but_not_chosen_is_chosen_not_paid_for_again(self):
        voiced(self.app)
        self.add()  # put in with a voice before the take was made (or the same voice as the take)
        self.passed_take()
        row = self.away.plan("202H")["pool"]["jobs"][0]
        self.assertEqual((row["voiced"], row["voice"], row["voice_config"], row["can_direct"]), (True, "已配音，直接剪", None, True))
        self.assertIn("自动选用", row["take_note"])
        self.run_pool()
        self.assertEqual(self.app.takes, [], "nothing voiced again")
        self.assertEqual([row["kind"] for row in self.away.current()["items"]], ["edit", "check"])
        self.assertEqual(self.app.selects[0][1]["take_id"], "TAK-old")
        self.assertEqual(self.app.production.jobs["PRD-1"]["selected_take"], "TAK-old")
        self.assertEqual(self.app.builds[0][0], "PRD-1")
        self.assertEqual(self.away.pool(), [])

    def test_a_voiceover_of_an_older_script_is_voiced_again(self):
        voiced(self.app)
        self.app._tts = lambda job, readings: {"text": "new script"}
        self.app.production.readings = lambda product: {}
        self.add()
        self.passed_take(sha="0" * 64)  # read from a script that has changed since
        self.assertFalse(self.away.plan("202H")["pool"]["jobs"][0]["voiced"])
        self.run_pool()
        self.assertEqual(len(self.app.takes), 1)

    def test_putting_in_a_voiced_order_takes_its_voiceover_and_its_own_footage(self):
        self.passed_take()
        self.app.production.jobs["PRD-1"].update(timelines=[{"unique_id": "TL-a", "creator": "Lab", "take_id": "TAK-x"}],
                                                 edits=[{"timeline_uid": "TL-a", "status": "failed", "materials": ["维氏厨", "配件-瓷砖"]}])
        entry = self.away.pool_change({"op": "add", "job_id": "PRD-1"})["pool"][0]
        self.assertEqual((entry["voice"], entry["creator"], entry["materials"], entry["ready"]), (None, "Lab", ["维氏厨", "配件-瓷砖"], True))
        row = self.away.plan("202H")["pool"]["jobs"][0]
        self.assertEqual((row["creator"], row["materials"], row["default"]), ("Lab", ["维氏厨", "配件-瓷砖"], True))

    def test_a_timeline_built_for_the_voiceover_is_cut_on_instead_of_building_another(self):
        voiced(self.app)
        job = self.app.production.jobs["PRD-2"]
        job["timelines"] = [{"unique_id": "TL-done", "name": "Lab-202-1", "creator": "Lab", "take_id": "T1", "task_status": "completed"},
                            {"unique_id": "TL-zack", "name": "Zack-202-1", "creator": "Zack", "take_id": "T1"},
                            {"unique_id": "TL-old", "name": "Lab-202-2", "creator": "Lab", "take_id": "T0"},
                            {"unique_id": "TL-built", "name": "Lab-202-3", "creator": "Lab", "take_id": "T1", "task_status": "pending"}]
        self.away.pool_change({"op": "add", "job_id": "PRD-2", "voice": None})
        row = self.away.plan("202H")["pool"]["jobs"][0]
        self.assertEqual(row["built"], {"Zack": "Zack-202-1", "Lab": "Lab-202-3"})
        self.run_pool("PRD-2", "Lab", ["主素材"])
        self.assertEqual(self.app.builds, [], "built already: nothing built again")
        self.assertEqual(self.app.edit_starts[0][1]["timeline"], "TL-built")
        self.app.production.jobs["PRD-2"]["edits"][-1]["status"] = "awaiting_review"
        self.settle()
        edit = self.item("edit")
        self.assertEqual(edit["result"]["reused_timeline"], "Lab-202-3")
        self.assertTrue(any("已建好的时间线 Lab-202-3" in row["text"] for row in self.away.current()["report"]["waiting"]))

    def test_a_timeline_already_cut_or_finished_by_hand_is_not_cut_again(self):
        job = self.app.production.jobs["PRD-2"]
        job["timelines"] = [{"unique_id": "TL-cut", "creator": "Lab", "take_id": "T1"}, {"unique_id": "TL-hand", "creator": "Lab", "take_id": "T1"}]
        job["edits"] = [{"timeline_uid": "TL-cut", "status": "failed"}]
        job["manual_completion"] = {"timeline_uid": "TL-hand"}
        self.away.pool_change({"op": "add", "job_id": "PRD-2", "voice": None})
        self.run_pool("PRD-2", "Lab", ["主素材"])
        self.assertEqual(self.app.builds[0][0], "PRD-2")
        self.assertEqual(self.app.edit_starts[0][1]["timeline"], "TL-new")

    def test_a_new_voice_picked_by_hand_for_a_voiced_order_is_voiced(self):
        voiced(self.app)
        self.passed_take()
        self.away.pool_change({"op": "add", "job_id": "PRD-1"})
        self.assertIsNone(self.away.pool()[0]["voice"])
        self.away.pool_change({"op": "update", "job_id": "PRD-1", "voice": VOICE})  # picked a voice in the pool
        self.assertFalse(self.away.plan("202H")["pool"]["jobs"][0]["voiced"])
        self.run_pool()
        self.assertEqual(len(self.app.takes), 1)
        self.away.pool_change({"op": "add", "job_id": "PRD-1"})
        self.away.pool_change({"op": "update", "job_id": "PRD-1", "voice": None})  # back to 「已配音，直接剪」
        self.assertTrue(self.away.plan("202H")["pool"]["jobs"][0]["voiced"])

if __name__ == "__main__":
    unittest.main()
