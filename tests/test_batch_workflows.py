"""Batch behavior through existing single-case HTTP routes; temporary data only."""
from __future__ import annotations

import copy
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_server as fixtures


class BatchWorkflowHTTPTests(unittest.TestCase):
    # Reuse only the fixture plumbing, without rerunning its individual tests.
    setUp = fixtures.WorkbenchHTTPTests.setUp
    stop_server = fixtures.WorkbenchHTTPTests.stop_server
    request = fixtures.WorkbenchHTTPTests.request

    def detail(self, identifier):
        status, _, detail = self.request("GET", f"/api/cases/{identifier}")
        self.assertEqual(status, 200, detail)
        return detail

    def add_case(self, name, video=None):
        current = fixtures.snapshot(video or self.video)
        current["title"] = name
        return self.store.add({"title": name, "product": "TEST PRODUCT",
            "original": {"text": "Disposable batch fixture"},
            "feedback": {"kind": "editorial", "text": "Test setup"},
            "current": current})["case_id"]

    def archive_body(self, identifier, archived=True):
        return {"request_id": uuid4().hex, "revision": self.detail(identifier)["revision"],
                "archived": archived, "note": "Batch workflow fixture"}

    def archive(self, identifier, body=None, archived=True):
        return self.request("POST", f"/api/cases/{identifier}/archive",
                            body or self.archive_body(identifier, archived))

    def preview(self, identifier):
        status, _, plan = self.request("GET", f"/api/cases/{identifier}/delete-preview")
        self.assertEqual(status, 200, plan)
        return plan

    def delete_body(self, identifier, plan=None):
        plan = plan or self.preview(identifier)
        return {"request_id": uuid4().hex, "revision": plan["revision"],
                "plan_token": plan["plan_token"], "confirmation": identifier, "include_source": False}

    def delete(self, identifier, body=None):
        return self.request("POST", f"/api/cases/{identifier}/delete", body or self.delete_body(identifier))

    def owned_case(self, identifier):
        owned, _ = self.server.app.video_files.copy_for_case(self.video, identifier)
        current = copy.deepcopy(self.detail(identifier)["current"])
        current["assets"]["video"] = str(owned)
        self.store.append(identifier, {"kind": "editorial", "text": "Fixture-owned video", "current": current})
        return owned

    def test_archive_restore_multiple_preserves_each_current_and_original(self):
        identifiers = [self.identifier, self.add_case("second"), self.add_case("third")]
        currents = {identifier: self.detail(identifier)["current"] for identifier in identifiers}
        originals = {identifier: (self.store.folder(identifier) / "original.json").read_bytes()
                     for identifier in identifiers}
        for archived in (True, False):
            for identifier in identifiers:
                status, _, result = self.archive(identifier, archived=archived)
                self.assertEqual(status, 200, result)
                self.assertEqual(result["case"]["archived"], archived)
                self.assertEqual(result["case"]["current"], currents[identifier])
                self.assertEqual((self.store.folder(identifier) / "original.json").read_bytes(), originals[identifier])
            status, _, state = self.request("GET", "/api/state")
            self.assertEqual(status, 200, state)
            self.assertEqual({case["archived"] for case in state["cases"]}, {archived})

    def test_one_archive_conflict_does_not_prevent_other_items(self):
        identifiers = [self.identifier, self.add_case("second"), self.add_case("third")]
        bodies = {identifier: self.archive_body(identifier) for identifier in identifiers}
        self.store.append(identifiers[0], {"kind": "editorial", "text": "A separate writer changed this item"})
        outcomes = [self.archive(identifier, bodies[identifier])[0] for identifier in identifiers]
        self.assertEqual(outcomes, [409, 200, 200])
        self.assertEqual([self.detail(identifier)["archived"] for identifier in identifiers], [False, True, True])

    def test_shared_owned_attachment_remains_until_last_reference_and_tokens_refresh(self):
        first = self.identifier
        shared = self.owned_case(first)
        second = self.add_case("shared second", shared)
        for identifier in (first, second):
            self.assertEqual(self.archive(identifier)[0], 200)
        first_plan, second_plan = self.preview(first), self.preview(second)
        self.assertIn(str(shared), [file["path"] for file in first_plan["retained"]])
        self.assertIn(str(shared), [file["path"] for file in second_plan["retained"]])
        first_body = self.delete_body(first, first_plan)
        status, _, first_result = self.delete(first, first_body)
        self.assertEqual(status, 200, first_result)
        self.assertTrue(shared.is_file())
        self.assertEqual(shared.read_bytes(), self.video_bytes)
        self.assertEqual(self.delete(second, self.delete_body(second, second_plan))[0], 409)
        self.assertTrue(self.store.folder(second).is_dir())
        final_plan = self.preview(second)
        self.assertIn(str(shared), [file["path"] for file in final_plan["files"]])
        self.assertEqual(self.delete(second, self.delete_body(second, final_plan))[0], 200)
        self.assertFalse(shared.exists())
        self.assertEqual(self.video.read_bytes(), self.video_bytes)
        self.assertEqual(self.request("GET", "/api/state")[2]["cases"], [])
        # Cached deletion results survive later batch items and state refreshes.
        status, _, replay = self.delete(first, first_body)
        self.assertEqual(status, 200, replay)
        self.assertEqual(replay, first_result)

    def test_shared_attachment_outside_selected_batch_is_preserved(self):
        first = self.identifier
        shared = self.owned_case(first)
        second = self.add_case("selected second", shared)
        outside = self.add_case("unselected reference", shared)
        for identifier in (first, second):
            self.assertEqual(self.archive(identifier)[0], 200)
        for identifier in (first, second):
            status, _, result = self.delete(identifier)
            self.assertEqual(status, 200, result)
            self.assertIn(str(shared), [file["path"] for file in result["retained"]])
        self.assertTrue(shared.is_file())
        self.assertFalse(self.detail(outside)["archived"])
        status, _, video = self.request("GET", f"/media/{outside}/video")
        self.assertEqual((status, video), (200, self.video_bytes))

    def test_failed_item_preserves_records_and_does_not_block_later_delete(self):
        first = self.identifier
        locked = self.owned_case(first)
        second = self.add_case("deletable second")
        second_owned = self.owned_case(second)
        for identifier in (first, second):
            self.assertEqual(self.archive(identifier)[0], 200)
        before = {str(path): path.read_bytes() for path in self.store.folder(first).rglob("*.json")}
        actual_unlink = self.server.app.deletion._unlink

        def fail_locked(file):
            if Path(file["path"]) == locked:
                raise PermissionError("Simulated file held open by another app")
            return actual_unlink(file)

        with patch.object(self.server.app.deletion, "_unlink", side_effect=fail_locked):
            self.assertEqual(self.delete(first)[0], 500)
            self.assertEqual(self.delete(second)[0], 200)
        self.assertTrue(self.store.folder(first).is_dir())
        self.assertTrue(locked.exists())
        self.assertFalse(self.store.folder(second).exists())
        self.assertFalse(second_owned.exists())
        for raw, data in before.items():
            self.assertEqual(Path(raw).read_bytes(), data)
        self.assertTrue(self.detail(first)["archived"])
        # The failed item's fresh plan can be retried independently.
        self.assertEqual(self.delete(first)[0], 200)
        self.assertFalse(locked.exists())

    def test_successful_delete_replays_without_deleted_case_body_or_cross_item_cache(self):
        identifiers = [self.identifier, self.add_case("second")]
        archive_bodies = {}
        for identifier in identifiers:
            archive_bodies[identifier] = self.archive_body(identifier)
            self.assertEqual(self.archive(identifier, archive_bodies[identifier])[0], 200)
        bodies, responses = {}, {}
        for identifier in identifiers:
            bodies[identifier] = self.delete_body(identifier)
            status, _, responses[identifier] = self.delete(identifier, bodies[identifier])
            self.assertEqual(status, 200, responses[identifier])
        self.assertEqual(self.request("GET", "/api/state")[2]["cases"], [])
        for identifier in identifiers:
            status, _, replay = self.delete(identifier, bodies[identifier])
            self.assertEqual(status, 200, replay)
            self.assertEqual(replay, responses[identifier])
            self.assertNotIn("case", replay)
            self.assertNotEqual(self.archive(identifier, archive_bodies[identifier])[0], 200)
        reused_for_other = dict(bodies[identifiers[1]], request_id=bodies[identifiers[0]]["request_id"])
        self.assertEqual(self.delete(identifiers[1], reused_for_other)[0], 409)


if __name__ == "__main__":
    unittest.main()
