"""Material timelines with short names are recorded at build time and offered while DaVinci is busy."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import resolve_worker
from production_store import ProductionStore
from workbench_production import material_hints


def catalog(jobs):
    return {"schema_version": 1, "voices": [], "readings": [], "jobs": jobs, "defaults": {}, "requests": {},
            "settings": {"creators": ["Zack", "Lab"], "products": {"202H": {"project": "202H"}}}}


def job(uid, project, creator, picks):
    return {"id": "PRD-" + uid, "product": "202H",
            "timelines": [{"unique_id": uid, "project": project, "creator": creator, "materials": []}],
            "edits": [{"timeline_uid": uid, "started_at": at, "materials": names} for at, names in picks]}


class MaterialHintTests(unittest.TestCase):
    def picks(self, jobs):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name) / "production"
        folder.mkdir()
        (folder / "catalog.json").write_text(json.dumps(catalog(jobs), ensure_ascii=False), encoding="utf-8")
        return ProductionStore(temp.name).material_picks()

    def test_hints_follow_the_creators_project_newest_first(self):
        picks = self.picks([
            job("a", "202H", "Zack", [("2026-10-19T00:00", ["超亮镜面折刀成品素材"])]),
            job("b", "Zack-202H", "Zack", [("2026-11-15T00:00", ["水果刀", "银柄折"])]),
            job("c", "Zack-202H", "Zack", [("2026-11-18T00:00", ["绿蜘蛛折", "银柄折"])]),
            job("d", "Lab-202H", "Lab", [("2026-11-18T01:00", ["维氏厨"])]),
        ])
        names, last = material_hints(picks, "Zack-202H", "Zack")
        self.assertEqual(names, ["绿蜘蛛折", "银柄折", "水果刀", "超亮镜面折刀成品素材"])
        self.assertEqual(last, ["绿蜘蛛折", "银柄折"])
        # The product's own project name reaches the same creator's project; another creator's never mixes in.
        self.assertEqual(material_hints(picks, "202H", "Zack")[0][:1], ["绿蜘蛛折"])
        self.assertEqual(material_hints(picks, "Lab-202H", "Lab")[0], ["维氏厨"])
        self.assertEqual(material_hints(picks, "Zack-202H", "diamond"), ([], []))

    def test_only_the_latest_edits_count(self):
        picks = self.picks([job("a", "Zack-202H", "Zack", [(f"2026-11-{day:02d}T00:00", [f"刀{day}"]) for day in range(1, 11)])])
        names, _ = material_hints(picks, "Zack-202H", "Zack", limit=3)
        self.assertEqual(names, ["刀10", "刀9", "刀8"])


class FakeTimeline:
    def __init__(self, name):
        self.name = name

    def GetName(self):
        return self.name


class FakeProject:
    def __init__(self, names):
        self.items = [FakeTimeline(name) for name in names]

    def GetTimelineCount(self):
        return len(self.items)

    def GetTimelineByIndex(self, index):
        return self.items[index - 1]


class WorkerLibraryTests(unittest.TestCase):
    def test_build_lists_short_named_material_timelines_picked_before(self):
        project = FakeProject(["Zack261118-202-1", "绿蜘蛛折", "水果刀", "户外素材", "旧素材备份", "Zack261117-202-1"])
        self.assertEqual(resolve_worker.library(project), ["户外素材"])
        self.assertEqual(resolve_worker.library(project, {"material_names": ["绿蜘蛛折", "水果刀", "不在工程里"]}),
                         ["绿蜘蛛折", "水果刀", "户外素材"])


if __name__ == "__main__":
    unittest.main()
