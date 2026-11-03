"""片段标签只算落在自己可见范围内的标记。"""
import unittest

from resolve_worker import USAGE_MARKER, clip_markers


class Item:
    def __init__(self, left, markers):
        self.left, self.markers = left, markers

    def GetLeftOffset(self):
        return self.left

    def GetStart(self):
        return 100

    def GetEnd(self):
        return 160

    def GetMarkers(self):
        return self.markers


def names(item):
    return [info["name"] for _, info in clip_markers(item)]


class ClipMarkersTest(unittest.TestCase):
    def test_markers_of_other_clips_in_the_same_file_are_dropped(self):
        item = Item(0, {30: {"name": "own"}, 60: {"name": "next clip"}, 9247: {"name": "far away"}})
        self.assertEqual(names(item), ["own"])

    def test_usage_notes_and_nameless_markers_are_not_labels(self):
        item = Item(0, {1: {"name": "used", "customData": USAGE_MARKER}, 2: {"name": ""}})
        self.assertEqual(names(item), [])

    def test_offset_is_respected(self):
        self.assertEqual(names(Item(1000, {30: {"name": "before"}, 1030: {"name": "own"}})), ["own"])


if __name__ == "__main__":
    unittest.main()
