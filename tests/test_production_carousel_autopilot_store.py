"""Persistent AI carousel runs, immutable authorization and reversible base picks."""
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from direction_store import now
from elevenlabs_client import VoiceError
from production_store import ProductionStore


class CarouselAutopilotStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="carousel-autopilot-store-")
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name)
        self.store = ProductionStore(self.runtime)
        self.item = self.store.mutate("carousel", self.body(), {
            "script_id": "SCR-0123456789ab", "script_version": 1, "product": "202H", "title": "图文",
            "direction_id": "202H-S1", "source": {}, "slides": [
                {"number": number, "title_lines": [f"Title {number}"], "body": "", "role": "内容",
                 "source": "实拍", "forbidden": ""} for number in (1, 2, 3)]})["carousel"]

    def body(self, **values):
        body = {"request_id": uuid4().hex, "revision": self.store.public()["revision"], **values}
        if hasattr(self, "item"):
            body.setdefault("carousel_id", self.item["id"])
        return body

    def record(self, **values):
        return {"id": "AUT-" + uuid4().hex[:12], "status": "running", "phase": "准备制作",
                "message": "开始处理", "started_at": now(), "actor": "assistant_on_user_authorization",
                "authorization": {"scope": "create_review_confirm_package", "instruction": "AI 全程介入"},
                "input_signature": "original-input", **values}

    def start(self, record=None):
        return self.store.mutate("carousel_autopilot_start", self.body(), record or self.record())["run"]

    def update(self, run, **fields):
        return self.store.mutate("carousel_autopilot_update", {
            "request_id": uuid4().hex, "carousel_id": self.item["id"], "run_id": run["id"]}, fields)["run"]

    def error(self, expected_status, call, *args, **kwargs):
        with self.assertRaises(VoiceError) as raised:
            call(*args, **kwargs)
        self.assertEqual(raised.exception.status, expected_status)

    def test_start_persists_authorization_and_replays_without_another_run(self):
        body, record = self.body(), self.record()
        result = self.store.mutate("carousel_autopilot_start", body, record)
        replay = self.store.mutate("carousel_autopilot_start", body, self.record())
        self.assertEqual(replay, result)
        self.assertEqual(result["run"]["request_id"], body["request_id"])
        record["authorization"]["scope"] = "changed-outside"
        result["run"]["authorization"]["scope"] = "also-changed-outside"
        loaded = ProductionStore(self.runtime).carousel(self.item["id"])["autopilot_runs"]
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["authorization"]["scope"], "create_review_confirm_package")

    def test_start_rejects_a_stale_revision_and_another_running_task(self):
        stale = self.body()
        self.start()
        self.error(409, self.store.mutate, "carousel_autopilot_start", stale, self.record())
        self.error(409, self.start)
        self.assertEqual(len(self.store.carousel(self.item["id"])["autopilot_runs"]), 1)

    def test_start_rejects_archived_invalid_and_reused_run_records(self):
        self.store.mutate("carousel_archive", self.body(archived=True))
        self.error(409, self.start)
        self.store.mutate("carousel_archive", self.body(archived=False))
        for fields in ({"id": "bad"}, {"status": "completed"}, {"started_at": ""}):
            self.error(500, self.start, self.record(**fields))
        record = self.record()
        run = self.start(record)
        self.update(run, status="completed")
        self.error(409, self.start, record)

    def test_update_without_revision_retains_identity_authorization_and_source(self):
        run = self.start()
        updated = self.update(run, phase="逐张核对", message="正在核对第 2 张", slide=2,
                              completed_slides=[1], generation_id="IMG-0123456789ab")
        self.assertEqual(updated["phase"], "逐张核对")
        self.assertEqual(updated["completed_slides"], [1])
        for key in ("id", "request_id", "started_at", "actor", "authorization", "input_signature"):
            self.assertEqual(updated[key], run[key])
            self.error(400, self.update, run, **{key: "replacement"})
        for value in ("", None, []):
            self.error(400, self.update, run, status=value)

    def test_terminal_statuses_cannot_be_reopened_or_have_late_results_replaced(self):
        for status in ("completed", "failed", "cancelled", "interrupted", "needs_attention"):
            with self.subTest(status=status):
                run = self.start()
                final = self.update(run, status=status, message="结束", result={"kept": True})
                self.assertTrue(final["finished_at"])
                self.assertEqual(self.update(run, status="running", message="迟到", result={"kept": False}), final)
        saved = ProductionStore(self.runtime).carousel(self.item["id"])["autopilot_runs"]
        self.assertEqual([run["status"] for run in saved], ["completed", "failed", "cancelled", "interrupted", "needs_attention"])

    def test_restart_interrupts_only_live_runs_and_preserves_saved_work(self):
        old = self.start()
        completed = self.update(old, status="completed", delivery_id="DLV-0123456789ab")
        run = self.start()
        run = self.update(run, completed_slides=[1], generation_id="IMG-0123456789ab")
        restarted = ProductionStore(self.runtime)
        restarted.interrupt_running()
        first = restarted.carousel(self.item["id"])
        self.assertEqual(first["autopilot_runs"][0], completed)
        interrupted = first["autopilot_runs"][1]
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertTrue(interrupted["finished_at"])
        self.assertEqual(interrupted["completed_slides"], [1])
        self.assertEqual(interrupted["generation_id"], run["generation_id"])
        self.assertEqual(interrupted["authorization"], run["authorization"])
        restarted.interrupt_running()
        self.assertEqual(restarted.carousel(self.item["id"]), first)
        self.assertEqual(self.update(run, status="completed")["status"], "interrupted")

    def test_running_autopilot_prevents_order_deletion(self):
        run = self.start()
        self.error(409, self.store.mutate, "carousel_delete", self.body())
        self.update(run, status="cancelled")
        result = self.store.mutate("carousel_delete", self.body())
        self.assertEqual(result["deleted_carousel"], self.item["id"])

    def test_base_selection_can_preserve_alternatives_without_changing_manual_default(self):
        candidates = [{"id": "CND-" + uuid4().hex[:12], "file": f"candidates/{number}.png",
                       "thumb": f"thumbs/{number}.jpg"} for number in (1, 2)]
        for candidate in candidates:
            self.store.mutate("carousel_import", self.body(slide=1), candidate)
        selected = self.store.mutate("carousel_base", self.body(slide=1, candidate=candidates[0]["id"],
                                                                preserve_candidates=True))
        self.assertEqual(selected["removed_candidates"], [])
        self.assertEqual(selected["carousel"]["slides"][0]["candidates"], candidates)
        self.assertEqual(selected["carousel"]["slides"][0]["base"], candidates[0])
        selected = self.store.mutate("carousel_base", self.body(slide=1, candidate=candidates[1]["id"]))
        self.assertEqual(selected["carousel"]["slides"][0]["candidates"], [candidates[1]])
        self.assertEqual(selected["removed_candidates"], [candidates[0]])


if __name__ == "__main__":
    unittest.main()
