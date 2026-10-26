"""Measured edit milestones use fake Resolve/client fixtures and disposable candidate grids."""
import unittest
from unittest.mock import patch

import test_edit_candidates as fixtures


class EditStageTests(unittest.TestCase):
    setUp = fixtures.PreparedEditTests.setUp
    run_edit = fixtures.PreparedEditTests.run_edit

    def record_stages(self):
        events = []
        original = self.bench._edit_stage
        def record(identifier, stage, message=None, **options):
            events.append({"stage": stage, "status": options.get("status", "running"), "grid": options.get("grid")})
            return original(identifier, stage, message, **options)
        self.bench._edit_stage = record
        return events

    def test_success_records_real_preparation_counts_and_save_source(self):
        events = self.record_stages()
        job = self.helper.timeline_job()
        edit = self.run_edit(job)
        started = [item["stage"] for item in events if item["status"] == "running"]
        self.assertEqual(list(dict.fromkeys(started)), ["materials", "grid", "choice", "placement", "saved"])
        self.assertEqual(edit["grid"], {"attempted": 2, "total": 2, "exported": 2, "deferred": 0})
        for stage in ("materials", "grid", "choice", "placement", "saved"):
            self.assertEqual(edit["stages"][stage]["status"], "completed", stage)
        self.assertEqual(edit["stages"]["saved"]["confirmation"], "client_report")
        self.assertIsNone(edit["validation"])
        self.assertIsNone(edit["summary"])

    def test_grid_budget_defers_actual_remaining_without_claiming_export_completion(self):
        job = self.helper.timeline_job()
        with patch("workbench_editing.PREPARE_BUDGET", -1):
            edit = self.run_edit(job)
        self.assertEqual(edit["grid"], {"attempted": 0, "total": 2, "exported": 0, "deferred": 2})
        self.assertEqual(edit["stages"]["grid"]["status"], "partial")
        self.assertEqual(edit["execution_status"], "completed")

    def test_failed_grid_compositions_count_attempts_but_not_successful_exports(self):
        job = self.helper.timeline_job()
        with patch("workbench_editing.compose_grid", side_effect=RuntimeError("isolated fixture")):
            edit = self.run_edit(job)
        self.assertEqual(edit["grid"], {"attempted": 2, "total": 2, "exported": 0, "deferred": 0})
        self.assertEqual(edit["stages"]["grid"]["status"], "partial")

    def test_reused_grids_are_explicitly_skipped(self):
        job = self.helper.timeline_job()
        self.run_edit(job)
        edit = self.run_edit(job)
        self.assertEqual(edit["stages"]["grid"]["status"], "skipped")
        self.assertEqual(edit["grid"], {"attempted": 0, "total": 0, "exported": 0, "deferred": 0})

    def test_failed_listing_and_unsaved_delivery_never_claim_success(self):
        job = self.helper.timeline_job()
        self.bench.runner.listing = {"error": "listing unavailable"}
        self.editor.output = '```json\n{"shots":0,"gaps":[],"saved":false}\n```'
        edit = self.run_edit(job)
        self.assertEqual(edit["stages"]["materials"]["status"], "failed")
        self.assertNotIn("grid", edit["stages"])
        self.assertEqual(edit["stages"]["saved"]["status"], "failed")
        self.assertEqual(edit["status"], "needs_repair")


if __name__ == "__main__":
    unittest.main()
