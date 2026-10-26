"""Project renames keep creator routing and persistent identity checks intact."""
import unittest
from unittest.mock import patch

import resolve_worker as worker
import test_resolve_project_folders as fixture


class ProjectRenameTests(unittest.TestCase):
    def setUp(self):
        self.lab, self.zack = "A本土店渠道号-Lab", "B跨境店渠道号-zack"
        self.tree = {self.lab: ["Lab-202H"], self.zack: ["Zack-202H"]}
        self.ids = {(self.lab, "Lab-202H"): "id-lab", (self.zack, "Zack-202H"): "id-zack"}
        self.tree_patch = patch.dict(fixture.TREE, self.tree, clear=True)
        self.id_patch = patch.dict(fixture.IDS, self.ids, clear=True)
        self.tree_patch.start()
        self.id_patch.start()
        self.addCleanup(self.tree_patch.stop)
        self.addCleanup(self.id_patch.stop)

    def test_known_id_accepts_renamed_open_project_without_browsing(self):
        resolve = fixture.Resolve(self.lab, "Lab-202H")
        actual = worker.request_project(resolve, {"project": "202H", "creator": "Lab", "project_id": "id-lab",
                                                  "folder": self.lab, "current_only": True})
        self.assertIs(actual, resolve.manager.current)
        self.assertEqual(resolve.manager.calls, [])

    def test_identity_also_works_without_creator_or_folder(self):
        resolve = fixture.Resolve(self.lab, "Lab-202H")
        actual = worker.request_project(resolve, {"project": "202H", "project_id": "id-lab"})
        self.assertEqual(actual.GetName(), "Lab-202H")
        self.assertEqual(resolve.manager.calls, [])

    def test_early_record_without_project_id_uses_timeline_identity(self):
        resolve = fixture.Resolve(self.lab, "Lab-202H")
        actual = worker.request_project(resolve, {"project": "202H", "creator": "Lab",
                                                  "unique_id": "tl-" + self.lab, "current_only": True})
        self.assertIs(actual, resolve.manager.current)
        self.assertEqual(resolve.manager.calls, [])

    def test_new_build_and_saved_record_open_correct_renamed_creator_project(self):
        for creator, folder, project_id in (("Zack", self.zack, "id-zack"), ("Lab", self.lab, "id-lab")):
            for saved in (False, True):
                with self.subTest(creator=creator, saved=saved):
                    resolve = fixture.Resolve(self.lab if creator == "Zack" else self.zack,
                                              "Lab-202H" if creator == "Zack" else "Zack-202H")
                    request = {"project": "202H", "creator": creator}
                    if saved:
                        request.update(folder=folder, project_id=project_id)
                    actual = worker.request_project(resolve, request)
                    self.assertEqual(actual.GetName(), creator + "-202H")
                    self.assertEqual(worker.located(actual), {"folder": folder, "project_id": project_id})
                    operations = [call[0] for call in resolve.manager.calls if call[0] in {"save", "close", "load"}]
                    self.assertEqual(operations, ["save", "close", "load"])

    def test_rebuilt_project_with_same_new_name_is_rejected(self):
        resolve = fixture.Resolve(self.lab, "Lab-202H")
        with self.assertRaisesRegex(worker.WorkerError, "不是工作台记录"):
            worker.request_project(resolve, {"project": "202H", "creator": "Zack", "project_id": "other-id", "folder": self.zack})

    def test_two_possible_projects_are_rejected_before_closing(self):
        fixture.TREE[self.zack].append("202H")
        for known_folder in (False, True):
            resolve = fixture.Resolve(self.lab, "Lab-202H")
            with self.assertRaisesRegex(worker.WorkerError, "多个"):
                worker.request_project(resolve, {"project": "202H", "creator": "Zack",
                                                  **({"folder": self.zack} if known_folder else {})})
            self.assertFalse(any(call[0] in {"close", "load"} for call in resolve.manager.calls))

    def test_current_only_wrong_creator_does_not_browse_or_switch(self):
        resolve = fixture.Resolve(self.lab, "Lab-202H")
        with self.assertRaisesRegex(worker.WorkerError, "没有切换项目"):
            worker.request_project(resolve, {"project": "202H", "creator": "Zack", "project_id": "id-zack", "current_only": True})
        self.assertEqual(resolve.manager.calls, [])

    def test_save_failure_does_not_close_project(self):
        resolve = fixture.Resolve(self.lab, "Lab-202H")
        with patch.object(resolve.manager, "SaveProject", return_value=False):
            with self.assertRaisesRegex(worker.WorkerError, "没能保存"):
                worker.request_project(resolve, {"project": "202H", "creator": "Zack"})
        self.assertFalse(any(call[0] in {"close", "load"} for call in resolve.manager.calls))

    def test_missing_target_leaves_current_project_open(self):
        fixture.TREE[self.zack] = ["Zack-198H"]
        resolve = fixture.Resolve(self.lab, "Lab-202H")
        with self.assertRaisesRegex(worker.WorkerError, "找不到项目"):
            worker.request_project(resolve, {"project": "202H", "creator": "Zack", "folder": self.zack})
        self.assertFalse(any(call[0] in {"close", "load"} for call in resolve.manager.calls))


if __name__ == "__main__":
    unittest.main()
