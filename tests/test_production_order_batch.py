"""内容制作批量管理：归档、恢复、永久删除按单判断并逐张报告。"""
import sys
import time
import unittest
from pathlib import Path
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ai_tasks import FakeBridge
from test_production_carousel_table import SOURCE, TABLE, CarouselTableTests


class OrderBatchTests(unittest.TestCase):
    import test_server as fixture
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    setUp = CarouselTableTests.setUp
    ok = CarouselTableTests.ok
    script = CarouselTableTests.script
    wait = CarouselTableTests.wait
    start = CarouselTableTests.start

    def make_carousel(self):
        table = self.wait(self.start(self.script())["id"])
        script_id = table["script_id"]
        return self.ok("POST", "/api/production/carousels", {
            "revision": self.ok("GET", "/api/production")["revision"], "request_id": uuid4().hex,
            "carousel": {"script_id": script_id, "table_id": table["id"]}})["carousel"]

    def batch(self, action, items, **extra):
        return self.ok("POST", "/api/production/orders/batch", {"request_id": uuid4().hex, "action": action, "items": items, **extra})

    def archived(self, identifier):
        return next(item for item in self.ok("GET", "/api/production")["carousels"] if item["id"] == identifier)["archived"]

    def test_archive_restore_and_delete_report_each_order_on_its_own(self):
        self.server.app.ai.bridge = FakeBridge(output=TABLE)
        first, second = self.make_carousel(), self.make_carousel()
        refs = [{"kind": "carousel", "id": first["id"]}, {"kind": "carousel", "id": second["id"]}]
        archived = self.batch("archive", refs)
        self.assertEqual((archived["changed"], archived["failed"], archived["skipped"]), (2, 0, 0))
        self.assertTrue(self.archived(first["id"]) and self.archived(second["id"]))
        again = self.batch("archive", refs)
        self.assertEqual((again["changed"], again["skipped"]), (0, 2))
        restored = self.batch("restore", [refs[0]])
        self.assertEqual(restored["changed"], 1)
        self.assertFalse(self.archived(first["id"]))
        # Archiving is not needed to delete: the restored order goes too; the unknown id fails alone.
        mixed = self.batch("delete", [refs[0], {"kind": "carousel", "id": "CAR-ffffffffffff"}])
        self.assertEqual((mixed["changed"], mixed["failed"]), (1, 1))
        remaining = [item["id"] for item in self.ok("GET", "/api/production")["carousels"]]
        self.assertEqual(remaining, [second["id"]])
        direct = self.batch("delete", [refs[1]])
        self.assertEqual(direct["changed"], 1)
        self.assertEqual(self.ok("GET", "/api/production")["carousels"], [])

    def test_invalid_batches_are_rejected_before_anything_changes(self):
        for body in ({"action": "archive", "items": []},
                     {"action": "explode", "items": [{"kind": "carousel", "id": "CAR-000000000000"}]},
                     {"action": "archive", "items": [{"kind": "carousel", "id": "not-an-id"}]},
                     {"action": "archive", "items": [{"kind": "case", "id": "CAR-000000000000"}]}):
            status, _, data = self.request("POST", "/api/production/orders/batch", {"request_id": uuid4().hex, **body})
            self.assertEqual(status, 400, (body, data))


if __name__ == "__main__":
    unittest.main()
