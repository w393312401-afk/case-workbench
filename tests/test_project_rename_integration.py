"""A creator's renamed Resolve project remains bound to the same production and edit."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_production_editing as fixture
from elevenlabs_client import VoiceError
from workbench_editing import adopt_project_location, handoff_project_location, project_key, with_location
from workbench_production import known_location


class ProjectRenameIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.EditingTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bench = self.fixture.bench

    def test_new_build_records_actual_name_and_later_build_keeps_project_identity(self):
        original = self.bench.resolve.run

        def renamed(command, *args, **kwargs):
            result = original(command, *args, **kwargs)
            if command == "build":
                result.update(project="Lab-202H", project_id="id-lab", folder="A本土店渠道号-Lab")
            return result

        with patch.object(self.bench.resolve, "run", side_effect=renamed):
            job = self.fixture.timeline_job()
            first = job["timelines"][-1]
            self.assertEqual(first["project"], "Lab-202H")
            self.bench.build_timeline(job["id"], {"request_id": uuid4().hex, "creator": "Lab"})
        second_request = [call for call in self.bench.runner.calls if call["command"] == "build"][-1]
        self.assertEqual((second_request["project"], second_request["project_id"], second_request["folder"]),
                         ("202H", "id-lab", "A本土店渠道号-Lab"))
        self.assertEqual(known_location(self.bench.production.records(), "Zack", "202H"), {})
        config = with_location({"project": "202H"}, first)
        self.assertEqual(config["project"], "Lab-202H")
        self.assertEqual(project_key(config), project_key({"project": "202H", "creator": "Lab"}))

    def test_existing_timeline_edit_uses_live_renamed_project_in_handoff(self):
        job = self.fixture.timeline_job()
        original = self.bench.resolve.run

        def renamed(command, *args, **kwargs):
            result = original(command, *args, **kwargs)
            if command in {"candidates", "status"}:
                result.update(project="Lab-202H", project_id="id-lab", folder="A本土店渠道号-Lab")
            if command == "candidates":
                result.update(materials=[], missing=[], usage_counted=False)
            return result

        with patch.object(self.bench.resolve, "run", side_effect=renamed):
            self.fixture.start(job)
            edit = self.fixture.wait(job["id"])
        self.assertEqual(edit["execution_status"], "completed", edit.get("error"))
        prompt = self.fixture.editor.calls[-1]["prompt"]
        self.assertIn("- 达芬奇项目：Lab-202H（文件夹「A本土店渠道号-Lab」）", prompt)
        self.assertNotIn("- 达芬奇项目：202H", prompt)
        later = [call for call in self.bench.runner.calls if call["command"] == "status"][-1]
        self.assertEqual((later["project"], later["project_id"]), ("Lab-202H", "id-lab"))

    def test_second_rename_after_preparation_refreshes_header_by_project_id(self):
        config = {"project": "Lab-202H", "creator": "Lab", "project_id": "id-lab"}
        adopt_project_location(config, {"project": "Lab新版202H", "project_id": "id-lab"})
        text = "- 达芬奇项目：Lab-202H（已打开）；目标时间线：Lab123\n脚本原文：Lab-202H"
        self.assertEqual(handoff_project_location(text, config),
                         "- 达芬奇项目：Lab新版202H（已打开）；目标时间线：Lab123\n脚本原文：Lab-202H")

    def test_wrong_project_id_or_unverified_name_is_rejected(self):
        for config, state in (
            ({"project": "202H", "creator": "Lab", "project_id": "id-lab"},
             {"project": "Lab-202H", "project_id": "id-other"}),
            ({"project": "202H", "creator": "Lab", "project_id": "id-lab"}, {"project": "Lab-202H"}),
            ({"project": "202H", "creator": "Lab"}, {"project": "Zack-202H", "project_id": "id-zack"}),
        ):
            with self.subTest(config=config, state=state), self.assertRaises(VoiceError):
                adopt_project_location(config, state)


if __name__ == "__main__":
    unittest.main()
