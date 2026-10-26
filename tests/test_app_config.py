"""Configuration must work from a fresh clone without exposing local settings."""
import json
from pathlib import Path
import tempfile
import unittest

from app_config import load_config


class AppConfigTests(unittest.TestCase):
    def test_defaults_are_absolute_and_do_not_create_business_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            value = load_config(root)
            self.assertEqual(value["skill_root"], str(root / ".local" / "case-library"))
            self.assertEqual(value["runtime_root"], str(root / ".local" / "runtime"))
            self.assertEqual(list(root.iterdir()), [])

    def test_local_overrides_survive_and_relative_paths_follow_application(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / "config.json").write_text(json.dumps({"port": 8767, "skill_root": "."}), encoding="utf-8")
            local = {"port": 9876, "runtime_root": "private/runtime", "performance_sources": ["private/sales"],
                     "image_generation": {"parameters_file": "private/image.json"}}
            path = root / "config.local.json"
            path.write_text(json.dumps(local), encoding="utf-8-sig")
            before = path.read_bytes()
            value = load_config(root)
            self.assertEqual(value["port"], 9876)
            self.assertEqual(value["skill_root"], str(root))
            self.assertEqual(value["runtime_root"], str(root / "private" / "runtime"))
            self.assertEqual(value["performance_sources"], [str(root / "private" / "sales")])
            self.assertEqual(value["image_generation"]["parameters_file"], str(root / "private" / "image.json"))
            self.assertEqual(path.read_bytes(), before)

    def test_rejects_non_object_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "config.json").write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(folder)
