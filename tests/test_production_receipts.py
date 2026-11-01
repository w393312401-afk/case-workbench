import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from production_store import ProductionStore


class ReceiptCompactionTests(unittest.TestCase):
    def catalog(self, at):
        job = {"id": "PRD-000000000001", "title": "now", "lines": [{"n": i} for i in range(200)]}
        data = ProductionStore.empty()
        data["jobs"] = [job]
        data["requests"] = {"old-save-0001": {"hash": "h", "result": {"job": {**job, "title": "then"}, "ok": True}, **at}}
        return data

    def test_month_old_receipt_is_dropped(self):
        data = self.catalog({"at": "2000-01-01T00:00:00+00:00"})
        ProductionStore._prune_requests(data)
        self.assertEqual(data["requests"], {})


if __name__ == "__main__":
    unittest.main()
