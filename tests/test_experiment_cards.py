"""Experiment persistence and honest common-window arithmetic in disposable records."""
import copy
import tempfile
import unittest
from uuid import uuid4

from creation_store import CreationError, CreationStore
from experiment_cards import compare_experiment


def card(**values):
    return {"hypothesis": "先给结果能提高转化", "change": "开头改为展示结果", "metric": "ctor_pct",
            "baseline_video_id": "123456", "test_video_id": "654321", "window_start": "2026-10-05",
            "window_end": "2026-10-06", "decision": "observe", "decision_note": "", **values}


def video(identifier, rows=None, **values):
    return {"video_id": identifier, "product": "202H", "account": "account-a", "series": rows or [
        {"start": "2026-10-05", "end": "2026-10-05", "views": 1000, "clicks": 1, "orders": 1, "gmv": 10},
        {"start": "2026-10-06", "end": "2026-10-06", "views": 9000, "clicks": 99, "orders": 9, "gmv": 90}], **values}


class ExperimentComparisonTests(unittest.TestCase):
    def setUp(self):
        self.videos = {identifier: video(identifier) for identifier in ("123456", "654321")}

    def compare(self, **values):
        return compare_experiment({"id": "PLN-test", "product": "202H", "experiment": card(**values)}, self.videos.get)

    def test_ratio_uses_totals_and_same_window_not_average_or_all_time(self):
        self.videos["123456"]["ctor_pct"] = 99
        self.videos["123456"]["series"].append({"start": "2026-09-20", "end": "2026-09-20", "orders": 1000, "clicks": 1000})
        self.videos["654321"]["series"][1]["orders"] = 19
        result = self.compare()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["baseline"]["value"], 10)
        self.assertEqual(result["test"]["value"], 20)
        self.assertEqual(result["delta"], 10)
        self.assertEqual(result["relative_change_pct"], 100)

    def test_other_video_coverage_never_fills_missing_video_dates(self):
        self.videos["654321"]["series"].pop()
        result = self.compare()
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["test"]["missing_dates"], ["2026-10-06"])
        self.assertIsNone(result["delta"])
        self.assertIsNone(result["test"]["value"])

    def test_partial_week_does_not_prorate_or_assume_zero(self):
        self.videos["654321"]["series"] = [{"start": "2026-10-05", "end": "2026-10-11", "orders": 7, "clicks": 70}]
        result = self.compare()
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(any("跨越" in issue for issue in result["issues"]))
        self.assertIsNone(result["test"]["value"])

    def test_missing_required_metric_and_zero_denominator_do_not_compare(self):
        for value in (None, -1, float("nan")):
            with self.subTest(value=value):
                self.videos["654321"]["series"][0]["clicks"] = value
                self.assertEqual(self.compare()["status"], "incomplete")
        for row in self.videos["654321"]["series"]:
            row["clicks"] = 0
        self.assertTrue(any("分母" in issue for issue in self.compare()["issues"]))

    def test_product_and_account_must_match(self):
        for field, value in (("product", "OTHER"), ("account", "account-b"), ("account", "")):
            with self.subTest(field=field, value=value):
                self.videos["654321"] = video("654321", **{field: value})
                result = self.compare()
                self.assertEqual(result["status"], "incompatible")
                self.assertIsNone(result["delta"])

    def test_pending_and_overlap_are_not_successes(self):
        self.assertEqual(self.compare(test_video_id="")["status"], "pending")
        self.assertEqual(self.compare(window_start="")["status"], "pending")
        self.videos["654321"]["series"].append(copy.deepcopy(self.videos["654321"]["series"][0]))
        self.assertEqual(self.compare()["status"], "incomplete")

    def test_gpm_and_zero_baseline_difference(self):
        result = self.compare(metric="gpm")
        self.assertEqual(result["baseline"]["value"], 10)
        for row in self.videos["123456"]["series"]:
            row["orders"] = 0
        result = self.compare(metric="orders")
        self.assertEqual(result["delta"], 10)
        self.assertIsNone(result["relative_change_pct"])


class ExperimentPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="experiment-test-")
        self.addCleanup(self.root.cleanup)
        self.store = CreationStore(self.root.name)

    def body(self, plan):
        return {"request_id": uuid4().hex, "revision": self.store.public()["revision"], "plan": plan}

    def plan(self, **values):
        return {"product": "202H", "direction_id": "202H-S1", "action": "execution", "experiment": card(), **values}

    def test_receipt_replay_and_status_edit_preserve_card_without_duplicate_history(self):
        body = self.body(self.plan())
        saved = self.store.mutate("plan", body)["plan"]
        self.assertEqual(len(saved["experiment_history"]), 1)
        restarted = CreationStore(self.root.name)
        self.assertEqual(restarted.mutate("plan", body)["plan"], saved)
        updated = restarted.mutate("plan", self.body({"id": saved["id"], "status": "done"}))["plan"]
        self.assertEqual(updated["experiment"], card())
        self.assertEqual(len(updated["experiment_history"]), 1)

    def test_edit_keeps_old_hypothesis_decision_and_rejects_stale_revision(self):
        saved = self.store.mutate("plan", self.body(self.plan()))["plan"]
        body = self.body({"id": saved["id"], "experiment": card(decision="scale", decision_note="共同窗口完整，继续增加样本")})
        result = self.store.mutate("plan", body)["plan"]
        self.assertEqual(result["experiment_history"][0]["experiment"]["decision"], "observe")
        self.assertEqual(len(result["experiment_history"]), 2)
        body["request_id"] = uuid4().hex
        with self.assertRaises(CreationError) as error:
            self.store.mutate("plan", body)
        self.assertEqual(error.exception.status, 409)

    def test_validation_rejects_invalid_windows_ids_and_unexplained_decisions(self):
        cases = ({"hypothesis": ""}, {"baseline_video_id": "bad"}, {"baseline_video_id": "654321"},
                 {"window_start": "2026-10-07"}, {"window_start": ""}, {"window_start": "2026-99-99"},
                 {"metric": "rating"}, {"decision": "scale"})
        for values in cases:
            with self.subTest(values=values), self.assertRaises(CreationError):
                self.store.mutate("plan", self.body(self.plan(experiment=card(**values))))
        self.assertEqual(self.store.public()["plans"], [])

    def test_old_plans_stay_compatible_and_draft_can_be_created_before_publishing(self):
        ordinary = self.plan()
        del ordinary["experiment"]
        saved = self.store.mutate("plan", self.body(ordinary))["plan"]
        self.assertNotIn("experiment", saved)
        draft = card(baseline_video_id="", test_video_id="", window_start="", window_end="")
        saved = self.store.mutate("plan", self.body(self.plan(experiment=draft)))["plan"]
        self.assertEqual(saved["experiment"], draft)


if __name__ == "__main__":
    unittest.main()
