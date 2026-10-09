import copy
import unittest
from unittest.mock import patch
import test_server as fixture

class NavigationTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def test_move_persists_without_changing_case_and_detects_conflicts(self):
        before = self.store.read_case(self.identifier)
        _, _, first = self.request("GET", "/api/state")
        body = dict(case_id=self.identifier, target="native", revision=fixture.SERVER.revision(before), previous=None)
        status, _, result = self.request("POST", "/api/navigation/move", body)
        self.assertEqual(status, 200, result)
        self.assertEqual(self.store.read_case(self.identifier), before)
        _, _, after = self.request("GET", "/api/state")
        self.assertEqual(after["navigation"][self.identifier], "native")
        self.assertNotEqual(first["signature"], after["signature"])
        fresh = fixture.SERVER.Workbench(self.skill, self.runtime, allow_open=False)
        self.assertEqual(fresh.navigation()[self.identifier], "native")
        status, _, _ = self.request("POST", "/api/navigation/move", body)
        self.assertEqual(status, 409)
        for target in ["all", "positive", "mixed", "pending", "legacy"]:
            body.update(target=target, previous=fresh.navigation()[self.identifier])
            self.assertEqual(self.request("POST", "/api/navigation/move", body)[0], 200)
        body.update(target="unknown")
        self.assertEqual(self.request("POST", "/api/navigation/move", body)[0], 400)
        self.assertEqual(self.request("POST", "/api/navigation/move", body, authenticated=False)[0], 403)

    def test_archived_case_and_failed_write_are_safe(self):
        before = self.store.read_case(self.identifier)
        body = dict(case_id=self.identifier, target="pending", revision=fixture.SERVER.revision(before), previous=None)
        app = fixture.SERVER.Workbench(self.skill, self.runtime, allow_open=False)
        with patch.object(fixture.SERVER.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError): app.move_navigation(body)
        self.assertEqual(app.navigation(), {})
        self.store.set_archived(self.identifier, True, "test archive")
        body["revision"] = fixture.SERVER.revision(self.store.read_case(self.identifier))
        self.assertEqual(self.request("POST", "/api/navigation/move", body)[0], 409)

    def test_explicit_column_moves_replace_one_assignment_and_persist(self):
        before = self.store.read_case(self.identifier)
        records_before = {
            str(path.relative_to(self.skill)): path.read_bytes()
            for path in self.store.folder(self.identifier).rglob("*.json")
        }
        app = fixture.SERVER.Workbench(self.skill, self.runtime, allow_open=False)
        body = dict(case_id=self.identifier, revision=fixture.SERVER.revision(before), previous=None)
        # A built-in ID can name a custom collection, so it must retain its stable ID.
        targets = [
            "column:custom-01234567-89ab-cdef-0123-456789abcdef", "column:mixed",
            "column:custom-valid123", "column:positive", "column:pending",
            "column:native", "column:legacy", "column:all",
        ]
        for target in targets:
            with self.subTest(target=target):
                body["target"] = target
                status, _, result = self.request("POST", "/api/navigation/move", body)
                self.assertEqual(status, 200, result)
                self.assertEqual(result["target"], target)
                self.assertEqual(app.navigation(), {self.identifier: target})
                _, _, state = self.request("GET", "/api/state")
                self.assertEqual(state["navigation"], {self.identifier: target})
                self.assertEqual(self.store.read_case(self.identifier), before)
                body["previous"] = target
        fresh = fixture.SERVER.Workbench(self.skill, self.runtime, allow_open=False)
        self.assertEqual(fresh.navigation(), {self.identifier: "column:all"})
        self.assertEqual(records_before, {
            str(path.relative_to(self.skill)): path.read_bytes()
            for path in self.store.folder(self.identifier).rglob("*.json")
        })

    def test_invalid_column_targets_never_change_saved_assignment(self):
        before = self.store.read_case(self.identifier)
        body = dict(case_id=self.identifier, target="column:mixed", revision=fixture.SERVER.revision(before), previous=None)
        self.assertEqual(self.request("POST", "/api/navigation/move", body)[0], 200)
        body["previous"] = "column:mixed"
        path = self.runtime / "navigation-assignments.json"
        saved = path.read_bytes()
        invalid_targets = [
            None, True, 12, [], {}, ["column:mixed"],
            "column:", "column:archived", "column:unknown", "column:custom-short",
            "column:custom-" + "a" * 81, "column:custom-Uppercase", "column:custom-abcdefgh/other",
            "column:custom-abcdefgh\n", "column:mixed\n", "column:column:mixed",
            "custom-valid123", "collection:mixed", "archived",
        ]
        for target in invalid_targets:
            with self.subTest(target=target):
                body["target"] = target
                status, _, result = self.request("POST", "/api/navigation/move", body)
                self.assertEqual(status, 400, result)
                self.assertEqual(path.read_bytes(), saved)

    def test_explicit_column_move_detects_stale_assignment_and_revision(self):
        before = self.store.read_case(self.identifier)
        body = dict(case_id=self.identifier, target="column:custom-abcdefgh", revision=fixture.SERVER.revision(before), previous=None)
        self.assertEqual(self.request("POST", "/api/navigation/move", body)[0], 200)
        path = self.runtime / "navigation-assignments.json"
        saved = path.read_bytes()
        body["target"] = "column:mixed"
        self.assertEqual(self.request("POST", "/api/navigation/move", body)[0], 409)
        self.assertEqual(path.read_bytes(), saved)
        body.update(previous="column:custom-abcdefgh", revision="stale-revision")
        self.assertEqual(self.request("POST", "/api/navigation/move", body)[0], 409)
        self.assertEqual(path.read_bytes(), saved)

    def test_archived_case_rejects_column_move_and_failed_write_preserves_assignment(self):
        before = self.store.read_case(self.identifier)
        body = dict(case_id=self.identifier, target="column:custom-abcdefgh", revision=fixture.SERVER.revision(before), previous=None)
        self.assertEqual(self.request("POST", "/api/navigation/move", body)[0], 200)
        path = self.runtime / "navigation-assignments.json"
        saved = path.read_bytes()
        body.update(target="column:mixed", previous="column:custom-abcdefgh")
        app = fixture.SERVER.Workbench(self.skill, self.runtime, allow_open=False)
        with patch.object(fixture.SERVER.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                app.move_navigation(body)
        self.assertEqual(path.read_bytes(), saved)
        self.store.set_archived(self.identifier, True, "test archive")
        body["revision"] = fixture.SERVER.revision(self.store.read_case(self.identifier))
        self.assertEqual(self.request("POST", "/api/navigation/move", body)[0], 409)
        self.assertEqual(path.read_bytes(), saved)

if __name__ == "__main__": unittest.main()
