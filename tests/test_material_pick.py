"""Each creator's material library for the execution pool (material_pick.py)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import material_pick as mp


def timeline(uid, creator="Lab", project="Lab-202H", project_id="P-LAB", name=None, built="2026-11-01T00:00:00+00:00", materials=()):
    return {"unique_id": uid, "creator": creator, "project": project, "project_id": project_id, "name": name or uid,
            "built_at": built, "materials": list(materials)}


def records():
    return {"settings": {"products": {"202H": {"project": "202H"}}}, "jobs": [
        {"id": "PRD-old", "product": "202H", "lines": [{"zh": "厨刀两面都要磨", "evidence": "动作演示"}],
         "timelines": [timeline("TL-1", name="Lab261101-202-1", materials=["维氏黑柄厨房刀成品素材", "禁用-蓝点折刀成品素材"])],
         "edits": [{"timeline_uid": "TL-1", "started_at": "2026-11-02T00:00:00+00:00", "materials": ["维氏黑柄厨房刀成品素材"],
                    "timeline_name": "Lab261101-202-1 AI剪辑"}]},
        {"id": "PRD-mid", "product": "202H", "lines": [{"zh": "主厨刀和小折刀用同一套"}],
         "timelines": [timeline("TL-2", built="2026-11-10T00:00:00+00:00")],
         "edits": [{"timeline_uid": "TL-2", "started_at": "2026-11-10T00:00:00+00:00", "materials": ["藤次郎厨", "配件-瓷砖"]}]},
        {"id": "PRD-zack", "product": "202H", "lines": [{"zh": "别人家的"}],
         "timelines": [timeline("TL-Z", creator="Zack", project="Zack-202H", project_id="P-ZACK")],
         "edits": [{"timeline_uid": "TL-Z", "started_at": "2026-11-11T00:00:00+00:00", "materials": ["水果刀"]}]},
        {"id": "PRD-new", "product": "202H", "selected_take": "T1",
         "lines": [{"zh": "我的主厨刀和 EDC 折刀都能磨。", "evidence": "展示", "proof": "主厨刀和小折刀放在箱子旁"},
                   {"zh": "照着原片来", "evidence": "照原片 镜头 1–2（0.2–2.0 秒）"}],
         "timelines": [timeline("TL-3", built="2026-11-18T00:00:00+00:00")]}]}


def make_app(listing=None, busy=False):
    data = records()
    app = SimpleNamespace(production=SimpleNamespace(records=lambda: data))
    calls = []

    def run(command, payload=None, timeout=180, held=False, launch=True):
        calls.append((command, payload, launch))
        return {"ok": True, **(listing or {})}
    app.resolve = SimpleNamespace(busy=lambda: busy, closed=lambda: listing is None, run=run, calls=calls)
    return app, data


DAVINCI = {"project": "Lab-202H", "project_id": "P-LAB",
           "timelines": ["维氏黑柄厨房刀成品素材", "藤次郎厨", "配件-瓷砖", "Lab261101-202-1", "Lab261101-202-1 AI剪辑", "Bk110折", "Bk110折 备份", "diamond261110-202-1", "FT261110-202-1"]}


class OptionsTests(unittest.TestCase):
    def test_history_only_knows_picked_names_of_this_creators_project(self):
        data = records()
        found = mp.options(data, data["jobs"][3])
        names = [row["name"] for row in found["options"]]
        self.assertEqual(names, ["藤次郎厨", "配件-瓷砖"])  # newest pick first; no banned, no Zack
        self.assertFalse(found["listed"])
        self.assertEqual([row["materials"] for row in found["examples"]], [["藤次郎厨", "配件-瓷砖"]])

    def test_davinci_adds_never_picked_footage_but_not_finished_videos_or_backups(self):
        data = records()
        found = mp.options(data, data["jobs"][3], listing=DAVINCI)
        self.assertTrue(found["listed"])
        names = [row["name"] for row in found["options"]]
        self.assertEqual(names[0], "Bk110折")
        self.assertNotIn("Lab261101-202-1", names)
        self.assertNotIn("Lab261101-202-1 AI剪辑", names)
        self.assertNotIn("Bk110折 备份", names)
        self.assertNotIn("FT261110-202-1", names)  # no Chinese: a finished cut, as on the order page
        self.assertNotIn("diamond261110-202-1", names)

    def test_another_open_project_is_not_read_as_this_ones(self):
        data = records()
        found = mp.options(data, data["jobs"][3], listing={**DAVINCI, "project_id": "P-ZACK"})
        self.assertFalse(found["listed"])
        self.assertNotIn("Bk110折", [row["name"] for row in found["options"]])

    def test_storyboard_lines_skip_replica_shot_times(self):
        lines = mp.script_lines(records()["jobs"][3])
        self.assertEqual(lines[0], {"say": "我的主厨刀和 EDC 折刀都能磨。", "types": [{"label": "展示", "kind": "show"}], "shot": "主厨刀和小折刀放在箱子旁"})
        mixed = mp.script_lines({"lines": [{"zh": "一句", "evidence": "结果证据(须实拍) + 动作演示"}]})[0]["types"]
        self.assertEqual(mixed, [{"label": "须实拍", "kind": "proof"}, {"label": "动作演示", "kind": "act"}])
        self.assertEqual(lines[1], {"say": "照着原片来", "types": [], "shot": ""})

    def test_latest_cut_of_each_creator(self):
        data = records()
        self.assertEqual(mp.latest_materials(data, data["jobs"][3], "Lab"), ["藤次郎厨", "配件-瓷砖"])
        self.assertEqual(mp.latest_materials(data, data["jobs"][3], "Zack"), ["水果刀"])
        self.assertEqual(mp.latest_materials(data, data["jobs"][3], "Nobody"), [])


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def names(self, result):
        return [row["name"] for row in result["names"]]

    def test_davinci_read_is_kept_and_listed_while_davinci_is_elsewhere(self):
        app, data = make_app(listing=DAVINCI)
        library = mp.MaterialLibrary(app, self.folder.name)
        job = data["jobs"][3]
        first = library.library(data, job, "Lab", library.listing())
        self.assertEqual(self.names(first), ["藤次郎厨", "配件-瓷砖", "Bk110折"])  # DaVinci's order
        self.assertTrue(first["seen_at"])
        self.assertEqual(app.resolve.calls[0][0], "status")
        self.assertFalse(app.resolve.calls[0][2])  # never launches DaVinci
        later = library.library(data, job, "Lab", {**DAVINCI, "project_id": "P-ZACK"})
        self.assertEqual(self.names(later), self.names(first), "kept from the stored read")
        saved = json.loads((Path(self.folder.name) / "away-material-library.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["202h|lab"]["names"][-1], "Bk110折")

    def test_without_a_read_the_library_is_what_was_picked_before(self):
        app, data = make_app(listing=None)
        library = mp.MaterialLibrary(app, self.folder.name)
        result = library.library(data, data["jobs"][3], "Lab", library.listing())
        self.assertEqual(self.names(result), ["藤次郎厨", "配件-瓷砖"])  # the old 成品素材 series is never listed
        self.assertEqual(result["seen_at"], "")
        self.assertEqual(result["names"][0]["used"], 1)

    def test_busy_davinci_is_not_asked(self):
        app, _ = make_app(listing=DAVINCI, busy=True)
        self.assertIsNone(mp.MaterialLibrary(app, self.folder.name).listing())
        self.assertEqual(app.resolve.calls, [])


if __name__ == "__main__":
    unittest.main()
