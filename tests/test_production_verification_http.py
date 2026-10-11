"""Engineering review and recovery routes over real HTTP, with only temporary business data."""
import copy
import json
from uuid import uuid4
import unittest

import test_server as fixture
import test_production_editing as editing
from resolve_bridge import ResolveBridge
from direction_store import now
from production_operations import ProductionOperations


class VerificationHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def seed_job(self):
        app = self.server.app
        self.runner = editing.Runner()
        app.resolve = ResolveBridge(runner=self.runner)
        identifier, edit_id, take_id = "PRD-0123456789ab", "EDT-0123456789ab", "TAK-0123456789ab"
        self.runner.job = identifier
        self.runner.inspect = editing.inspect_reply(shots=((0, 200), (200, 400)))
        baseline = json.loads(json.dumps(self.runner.inspect).replace("{job}", identifier))
        edit = {"id": edit_id, "timeline_uid": "uid-1", "timeline_name": "Lab261010-202-1", "take_id": take_id,
                "status": "needs_repair", "execution_status": "completed", "baseline": baseline, "materials": [],
                "started_at": now(), "finished_at": now(), "validation": {"status": "needs_repair", "issues": []}}
        data = app.production.read()
        data["jobs"].append({"id": identifier, "product": "202H", "title": "核验 HTTP", "archived": False,
            "script_id": "SCR-0123456789ab", "script_version": 1, "status": "timeline", "selected_take": take_id,
            "lines": [{"number": n, "spoken": f"Line {n}.", "zh": "", "proof": "", "tags": "", "evidence": ""} for n in range(1, 4)],
            "takes": [{"id": take_id, "voice_id": "VOICE123456789", "model_id": "eleven_v4"}],
            "timelines": [{"name": edit["timeline_name"], "unique_id": "uid-1", "take_id": take_id,
                           "frames": 400, "fps": "29.97", "project": "202H"}],
            "edits": [edit], "history": [], "updated_at": now()})
        app.production.write(data)
        return identifier, edit_id

    def test_recheck_route_saves_existing_project_and_replay_does_not_inspect_twice(self):
        job, edit = self.seed_job()
        path = f"/api/production/jobs/{job}/edits/{edit}/recheck"
        body = {"request_id": uuid4().hex}
        status, _, first = self.request("POST", path, body)
        self.assertEqual(status, 200, first)
        self.assertEqual(first["edit"]["status"], "awaiting_review")
        self.assertEqual(first["edit"]["execution_status"], "completed")
        self.assertEqual(first["job"]["status"], "awaiting_review")
        status, _, again = self.request("POST", path, body)
        self.assertEqual((status, again), (200, first))
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual((self.runner.calls[0]["command"], self.runner.calls[0]["save"]), ("inspect", True))
        status, _, ready = self.request("GET", "/api/production/deliveries")
        self.assertEqual(status, 200)
        self.assertEqual(ready["items"][0]["edit_id"], edit)

    def test_recheck_detects_changed_source_voice_and_removes_result_from_review_list(self):
        job, edit = self.seed_job()
        path = f"/api/production/jobs/{job}/edits/{edit}/recheck"
        self.assertEqual(self.request("POST", path, {"request_id": uuid4().hex})[0], 200)
        self.runner.final_inspect = copy.deepcopy(self.runner.inspect)
        self.runner.final_inspect["tracks"]["audio"][1]["items"][0]["media_path"] = "C:/other.wav"
        status, _, changed = self.request("POST", path, {"request_id": uuid4().hex})
        self.assertEqual(status, 200, changed)
        self.assertEqual(changed["edit"]["status"], "needs_repair")
        self.assertIn("voice_changed", [item["code"] for item in changed["edit"]["validation"]["issues"]])
        self.assertEqual(self.request("GET", "/api/production/deliveries")[2]["items"], [])

    def test_unauthorized_or_invalid_request_cannot_save_resolve(self):
        job, edit = self.seed_job()
        path = f"/api/production/jobs/{job}/edits/{edit}/recheck"
        self.assertEqual(self.request("POST", path, {"request_id": uuid4().hex}, authenticated=False)[0], 403)
        self.assertEqual(self.request("POST", path, {"request_id": "bad"})[0], 400)
        self.assertEqual(self.runner.calls, [])

    def test_saved_take_recovery_route_restores_catalog_without_voice_or_build_call(self):
        job, _ = self.seed_job()
        app = self.server.app
        request_id = uuid4().hex
        original = {"request_id": request_id, "job_id": job, "voice_id": "VOICE123456789"}
        operations = ProductionOperations(app.production.root)
        record = operations.prepare("take", original, {}, take_id="TAK-111111111111")
        operations.update(record, "result_ready", result={"id": "TAK-111111111111", "voice_id": "VOICE123456789",
            "created_at": now(), "audio": "unused.mp3", "groups": [], "model_id": "eleven_v4"})
        status, _, result = self.request("POST", f"/api/production/jobs/{job}/recover",
                                         {"request_id": request_id, "kind": "take"})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["take"]["id"], "TAK-111111111111")
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(operations.public(job), [])


if __name__ == "__main__":
    unittest.main()
