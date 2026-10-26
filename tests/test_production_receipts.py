import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from production_store import ProductionStore


class ReceiptCompactionTests(unittest.TestCase):
    def catalog(self, at):
        carousel = {"id": "CAR-000000000001", "title": "now", "slides": [{"n": i} for i in range(200)]}
        data = ProductionStore.empty()
        data["carousels"] = [carousel]
        data["requests"] = {"old-save-0001": {"hash": "h", "result": {"carousel": {**carousel, "title": "then"}, "ok": True}, **at}}
        return data

    def test_legacy_receipt_keeps_ids_and_replays_current_record(self):
        data = self.catalog({})
        ProductionStore._prune_requests(data)
        receipt = data["requests"]["old-save-0001"]
        self.assertTrue(receipt["compact"])
        self.assertEqual(receipt["result"]["carousel"], {"id": "CAR-000000000001"})
        replayed = ProductionStore._replayed(data, receipt)
        self.assertEqual(replayed["carousel"]["title"], "now")
        self.assertTrue(replayed["ok"])

    def test_recent_receipt_stays_whole(self):
        data = self.catalog({"at": "9999-01-01T00:00:00+00:00"})
        ProductionStore._prune_requests(data)
        self.assertEqual(data["requests"]["old-save-0001"]["result"]["carousel"]["title"], "then")

    def test_month_old_receipt_is_dropped(self):
        data = self.catalog({"at": "2000-01-01T00:00:00+00:00"})
        ProductionStore._prune_requests(data)
        self.assertEqual(data["requests"], {})


if __name__ == "__main__":
    unittest.main()
