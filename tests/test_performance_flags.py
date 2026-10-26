"""Flags beside the follow-up status: 后起, 有播放没出单, and the reference for unjudged videos."""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from performance_store import COLUMNS, PerformanceStore

HEADER = [name for name in COLUMNS if name not in {"VV"}]
VIDEOS = {"7600000000000000011": "2026/08/31 05:00:00",  # drop in its window, takes off later
          "7600000000000000012": "2026/08/31 05:00:00",  # focus views, never sells
          "7600000000000000013": "2026/08/20 05:00:00"}  # first exports start after its window began


def row(video_id, views, orders=0):
    values = {"Creator name": "bushcraftedge", "Creator ID": "1", "Video Info": "Demo#knife", "Video ID": video_id,
              "Time": VIDEOS[video_id], "Products": "SHARPAL 202H", "Video views": str(views), "Product Clicks": "10",
              "Attributed SKU orders": str(orders), "Video-attributed GMV ($)": str(orders * 30)}
    return [values.get(name, "0") for name in HEADER]


def week(folder, start, rows):
    end = (date.fromisoformat(start) + timedelta(days=6)).isoformat()
    book = openpyxl.Workbook()
    sheet = book.active
    for line in ([f"[Date Range]: {start} ~ {end}\n"], [None], HEADER, *rows):
        sheet.append(line)
    folder.mkdir(parents=True, exist_ok=True)
    book.save(folder / f"{start}.xlsx")


class FlagTests(unittest.TestCase):
    def test_flags_and_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "exports"
            weeks = ["2026-08-31", "2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28"]
            for i, start in enumerate(weeks):
                week(folder, start, [row("7600000000000000011", 300 if i < 2 else 5000, 0 if i < 2 else 1),
                                     row("7600000000000000012", 8000 if i < 2 else 100),
                                     row("7600000000000000013", 100, 1 if i == 0 else 0)])
            store = PerformanceStore(Path(temp) / "runtime", [folder])
            store.sync()
            videos = {v["video_id"]: v for v in store.videos()}
            late, loud, early = (videos[f"760000000000000001{i}"] for i in (1, 2, 3))
            self.assertEqual((late["status"], [f["key"] for f in late["flags"]]), ("drop", ["late"]))
            self.assertEqual((loud["status"], [f["key"] for f in loud["flags"]]), ("focus", ["no_orders"]))
            self.assertEqual((early["status"], early["flags"], early["reference"]["key"]), ("incomplete", [], "sold"))
            self.assertEqual(early["missing_window"], ["2026-08-20", "2026-08-30", 11])


if __name__ == "__main__":
    unittest.main()
