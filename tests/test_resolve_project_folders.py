"""Same-named DaVinci projects live in one folder per channel; the worker must open the creator's own."""
import unittest

import resolve_worker as worker

TREE = {"A本土店渠道号-Lab": ["202H"], "B跨境店渠道号-zack": ["202H"], "C英国渠道号": ["202H"], "说明书": ["135"]}
IDS = {("A本土店渠道号-Lab", "202H"): "id-lab", ("B跨境店渠道号-zack", "202H"): "id-zack"}


class Timeline:
    def __init__(self, uid):
        self.uid = uid

    def GetUniqueId(self):
        return self.uid


class Project:
    def __init__(self, folder, name):
        self.folder, self.name = folder, name

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return IDS.get((self.folder, self.name), f"id-{self.folder}-{self.name}")

    def GetTimelineCount(self):
        return 1

    def GetTimelineByIndex(self, index):
        return Timeline(f"tl-{self.folder}")


class Manager:
    def __init__(self, folder, name):
        self.current, self.at, self.calls = Project(folder, name), "", []

    def GetCurrentProject(self):
        return self.current

    def GotoRootFolder(self):
        self.at = ""
        return True

    def GotoParentFolder(self):
        self.at = ""
        return True

    def GetFolderListInCurrentFolder(self):
        return list(TREE) if not self.at else []

    def GetProjectListInCurrentFolder(self):
        return TREE.get(self.at, [])

    def OpenFolder(self, name):
        self.calls.append(("open", name))
        if name not in TREE:
            return False
        self.at = name
        return True

    def SaveProject(self):
        self.calls.append(("save",))
        return True

    def CloseProject(self, project):
        self.calls.append(("close", project.folder))
        self.current = None
        return True

    def LoadProject(self, name):
        self.calls.append(("load", self.at, name))
        if name not in TREE.get(self.at, []):
            return None
        self.current = Project(self.at, name)
        return self.current


class Resolve:
    def __init__(self, folder="A本土店渠道号-Lab", name="202H"):
        self.manager = Manager(folder, name)

    def GetProjectManager(self):
        return self.manager


class RequestProjectTests(unittest.TestCase):
    def test_without_creator_the_open_project_is_used_by_name(self):
        resolve = Resolve()
        self.assertEqual(worker.request_project(resolve, {"project": "202H"}).GetName(), "202H")
        with self.assertRaises(worker.WorkerError):
            worker.request_project(resolve, {"project": "135"})
        self.assertEqual(resolve.manager.calls, [])

    def test_creator_folder_is_found_by_name_and_opened_after_saving_and_closing(self):
        resolve = Resolve("A本土店渠道号-Lab")
        project = worker.request_project(resolve, {"project": "202H", "creator": "Zack"})
        self.assertEqual(project.folder, "B跨境店渠道号-zack")
        self.assertEqual(worker.located(project), {"project_id": "id-zack", "folder": "B跨境店渠道号-zack"})
        self.assertEqual([call[0] for call in resolve.manager.calls if call[0] in {"save", "close", "load"}], ["save", "close", "load"])

    def test_known_project_id_keeps_the_open_project_untouched(self):
        resolve = Resolve("B跨境店渠道号-zack")
        project = worker.request_project(resolve, {"project": "202H", "creator": "Zack", "project_id": "id-zack",
                                                   "folder": "B跨境店渠道号-zack"})
        self.assertIs(project, resolve.manager.current)
        self.assertEqual(resolve.manager.calls, [])

    def test_wrong_project_with_the_same_name_is_replaced_using_the_recorded_folder(self):
        resolve = Resolve("A本土店渠道号-Lab")
        project = worker.request_project(resolve, {"project": "202H", "creator": "Zack", "project_id": "id-zack",
                                                   "folder": "B跨境店渠道号-zack"})
        self.assertEqual(project.folder, "B跨境店渠道号-zack")

    def test_timeline_id_proves_the_open_project_without_reopening(self):
        resolve = Resolve("B跨境店渠道号-zack")
        worker.request_project(resolve, {"project": "202H", "creator": "Zack", "unique_id": "tl-B跨境店渠道号-zack"})
        self.assertEqual(resolve.manager.calls, [])
        with self.assertRaises(worker.WorkerError):
            worker.request_project(resolve, {"project": "202H", "creator": "Zack", "unique_id": "tl-A本土店渠道号-Lab"})
        self.assertIn(("close", "B跨境店渠道号-zack"), resolve.manager.calls)

    def test_recorded_id_that_no_longer_matches_stops_instead_of_editing_another_project(self):
        resolve = Resolve("A本土店渠道号-Lab")
        with self.assertRaises(worker.WorkerError):
            worker.request_project(resolve, {"project": "202H", "creator": "Zack", "project_id": "rebuilt",
                                             "folder": "B跨境店渠道号-zack"})

    def test_creator_without_a_folder_keeps_the_open_project(self):
        resolve = Resolve("A本土店渠道号-Lab")
        project = worker.request_project(resolve, {"project": "202H", "creator": "Amy"})
        self.assertIs(project, resolve.manager.current)
        self.assertFalse(any(call[0] in {"close", "load"} for call in resolve.manager.calls))

    def test_two_folders_naming_the_creator_are_refused(self):
        resolve = Resolve()
        with self.assertRaises(worker.WorkerError) as caught:
            worker.request_project(resolve, {"project": "202H", "creator": "渠道号"})
        self.assertIn("多个文件夹", str(caught.exception))

    def test_current_only_reads_never_close_or_browse_for_another_project(self):
        for request in ({"project": "202H", "creator": "Zack", "project_id": "id-zack", "folder": "B跨境店渠道号-zack"},
                        {"project": "202H", "creator": "Zack", "unique_id": "tl-B跨境店渠道号-zack"},
                        {"project": "202H", "creator": "Zack"}):
            with self.subTest(request=request):
                resolve = Resolve("A本土店渠道号-Lab")
                with self.assertRaises(worker.WorkerError) as caught:
                    worker.request_project(resolve, {**request, "current_only": True})
                self.assertIn("没有切换项目", str(caught.exception))
                self.assertEqual(resolve.manager.calls, [])
                self.assertEqual(resolve.manager.current.folder, "A本土店渠道号-Lab")

    def test_current_only_uses_the_open_project_when_it_is_the_one(self):
        resolve = Resolve("B跨境店渠道号-zack")
        project = worker.request_project(resolve, {"project": "202H", "creator": "Zack", "unique_id": "tl-B跨境店渠道号-zack",
                                                   "current_only": True})
        self.assertIs(project, resolve.manager.current)
        self.assertEqual(resolve.manager.calls, [])

    def test_failed_open_reports_that_the_old_project_was_closed(self):
        resolve = Resolve()
        with self.assertRaises(worker.WorkerError) as caught:
            worker.request_project(resolve, {"project": "202H", "folder": "不存在"})
        self.assertIn("不存在", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
