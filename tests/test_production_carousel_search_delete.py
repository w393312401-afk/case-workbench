"""Deleting a finished 找底图 record: its folder and unchosen candidates go, a chosen base stays, running ones are refused."""
from pathlib import Path
import sys
import unittest
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from elevenlabs_client import VoiceError
import test_production_carousel_stills as stills


class SearchDeleteTests(stills.StillTests):
    """Reuses the still-search fixture (fake Claude, fake Resolve)."""

    def finished(self):
        self.start(per_slide=2)
        item = self.wait()
        return item, item["searches"][-1]

    def delete(self, search_id, **body):
        return self.bench.delete_still_search(self.item["id"], search_id, {"request_id": uuid4().hex, **body})

    def test_preview_then_delete_keeps_the_chosen_base_and_removes_the_rest(self):
        item, search = self.finished()
        first, second = item["slides"][0]["candidates"][0], item["slides"][1]["candidates"][0]
        self.bench.choose_base(self.item["id"], {"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"],
                                                  "slide": 1, "candidate": first["id"]})
        folder = self.bench._carousel_dir(self.item["id"]) / "searches" / search["id"]
        self.assertTrue(folder.is_dir())
        preview = self.bench.delete_still_search(self.item["id"], search["id"], {"preview": True})
        self.assertEqual((preview["candidates"], preview["kept_as_base"]), (1, 1))
        self.assertGreater(preview["bytes"], 0)
        self.assertTrue(folder.is_dir())
        result = self.delete(search["id"])
        self.assertEqual(result["deleted_search"], search["id"])
        self.assertEqual(result["warnings"], [])
        carousel = self.bench.production.carousel(self.item["id"])
        self.assertEqual(carousel["searches"], [])
        self.assertEqual([entry["id"] for entry in carousel["slides"][0]["candidates"]], [first["id"]])
        self.assertEqual(carousel["slides"][1]["candidates"], [])
        self.assertEqual(carousel["slides"][0]["base"]["id"], first["id"])
        self.assertFalse(folder.exists())
        root = self.bench._carousel_dir(self.item["id"])
        self.assertTrue((root / first["file"]).is_file())
        self.assertFalse((root / second["file"]).exists())
        self.assertFalse((root / second["thumb"]).exists())
        self.assertEqual(carousel["history"][-1]["event"], "search_deleted")

    def test_same_request_replays_and_a_second_delete_is_not_found(self):
        _, search = self.finished()
        body = {"request_id": uuid4().hex}
        first = self.bench.delete_still_search(self.item["id"], search["id"], body)
        again = self.bench.delete_still_search(self.item["id"], search["id"], body)
        self.assertEqual(first["deleted_search"], again["deleted_search"])
        with self.assertRaises(VoiceError) as caught:
            self.delete(search["id"])
        self.assertEqual(caught.exception.status, 404)

    def test_archived_order_and_unknown_search_are_refused(self):
        _, search = self.finished()
        with self.assertRaises(VoiceError) as caught:
            self.delete("STS-000000000000")
        self.assertEqual(caught.exception.status, 404)
        self.bench.production.mutate("carousel_archive", {"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"],
                                                          "carousel_id": self.item["id"], "archived": True})
        with self.assertRaises(VoiceError) as caught:
            self.delete(search["id"])
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.bench.production.carousel(self.item["id"])["searches"]), 1)

    def test_a_running_search_cannot_be_deleted(self):
        self.searcher.block = True
        self.start()
        search = self.bench.production.carousel(self.item["id"])["searches"][-1]
        with self.assertRaises(VoiceError) as caught:
            self.delete(search["id"])
        self.assertEqual(caught.exception.status, 409)
        self.bench.cancel_still_search(self.item["id"], search["id"])
        self.wait()


# The inherited still-search tests are covered in test_production_carousel_stills; only the delete tests run here.
for _name in [name for name in dir(SearchDeleteTests) if name.startswith("test_") and name not in {
        "test_preview_then_delete_keeps_the_chosen_base_and_removes_the_rest",
        "test_same_request_replays_and_a_second_delete_is_not_found",
        "test_archived_order_and_unknown_search_are_refused", "test_a_running_search_cannot_be_deleted"}]:
    setattr(SearchDeleteTests, _name, None)


if __name__ == "__main__":
    unittest.main()
