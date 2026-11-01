import importlib.util
import tempfile
import unittest
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("check_project_map", APP_ROOT / "scripts" / "check_project_map.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


class ProjectMapTests(unittest.TestCase):
    def test_map_lists_every_code_file(self):
        self.assertEqual(CHECK.check(APP_ROOT), ([], []))

    def test_reports_unmapped_and_stale_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "docs").mkdir()
            (root / "static").mkdir()
            (root / "scripts").mkdir()
            (root / "kept.py").write_text("", encoding="utf-8")
            (root / "new.py").write_text("", encoding="utf-8")
            (root / "static" / "page.js").write_text("", encoding="utf-8")
            (root / CHECK.MAP_PATH).write_text("`kept.py` `gone.py` `page.js` `xxx_store.py`", encoding="utf-8")
            self.assertEqual(CHECK.check(root), (["new.py"], ["gone.py"]))


if __name__ == "__main__":
    unittest.main()
