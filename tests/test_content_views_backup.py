"""Saved views and migration tombstones travel with the workbench's full backup."""
import unittest

import test_server  # Configures the isolated workbench's skill module imports.
from content_views import ContentViewsStore
from data_backup import BackupManager
from workbench_content import _parse_content_filter
import test_data_backup as backup_fixture


class ContentViewsBackupTests(unittest.TestCase):
    setUp = backup_fixture.DataBackupTests.setUp
    write = staticmethod(backup_fixture.DataBackupTests.write)
    export = backup_fixture.DataBackupTests.export

    def test_other_workbench_restores_views_and_does_not_reimport_deleted_legacy_view(self):
        store = ContentViewsStore(self.runtime, filter_validator=_parse_content_filter)
        view = {"id": "backup-view-0001", "name": "待审视频", "q": "",
                "filter": {"match": "all", "conditions": [{"field": "kind", "values": ["video"]}]}}
        removed = {**view, "id": "backup-view-0002", "name": "旧视图"}
        result = store.mutate({"product": "202H", "action": "import", "views": [view, removed]})
        expected = store.mutate({"product": "202H", "action": "delete", "id": removed["id"],
                                 "revision": result["revision"]})
        archive = self.export()
        second = self.root / "second"
        backup = BackupManager(second / "skill", second / "runtime", second / "app")
        backup.restore_archive(archive, second / "recovery")
        restored = ContentViewsStore(second / "runtime", filter_validator=_parse_content_filter)
        self.assertEqual(restored.public("202H"), expected)
        self.assertEqual(restored.mutate({"product": "202H", "action": "import", "views": [view, removed]}), expected)
        self.assertEqual(restored.public("OTHER")["views"], [])


if __name__ == "__main__":
    unittest.main()
