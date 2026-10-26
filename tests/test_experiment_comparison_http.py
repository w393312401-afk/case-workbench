"""A real comparison request cannot mix generations during concurrent sales import."""
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_server as fixture


class ExperimentComparisonHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def test_two_video_reads_share_generation_while_import_waits(self):
        app = self.server.app
        plan = app.creation.mutate("plan", {
            "request_id": uuid4().hex, "revision": app.creation.public()["revision"],
            "plan": {"product": "202H", "direction_id": "202H-S1", "action": "execution",
                     "experiment": {"hypothesis": "同一代数据应相等", "change": "测试并发导入",
                                    "baseline_video_id": "123456", "test_video_id": "654321",
                                    "metric": "orders", "window_start": "2026-10-05", "window_end": "2026-10-05"}},
        })["plan"]
        first_read, probe_finished, writer_finished = (threading.Event() for _ in range(3))
        generation, reads, errors = [1], [], []
        writer_was_blocked = []

        def read_video(identifier):
            snapshot = generation[0]
            reads.append((identifier, snapshot))
            if identifier == "123456":
                first_read.set()
                if not probe_finished.wait(3):
                    raise RuntimeError("writer did not attempt the performance lock")
            return {"video_id": identifier, "account": "same-account", "product": "202H",
                    "series": [{"start": "2026-10-05", "end": "2026-10-05", "orders": snapshot * 10}]}

        def import_next_generation():
            try:
                if not first_read.wait(3):
                    raise RuntimeError("comparison did not read the baseline")
                acquired = app.performance.lock.acquire(timeout=0.15)
                writer_was_blocked.append(not acquired)
                if acquired:
                    try:
                        generation[0] = 2
                    finally:
                        app.performance.lock.release()
                    probe_finished.set()
                else:
                    # Let comparison continue while the import still waits for its shared lock.
                    probe_finished.set()
                    with app.performance.lock:
                        generation[0] = 2
            except BaseException as error:
                errors.append(error)
                probe_finished.set()
            finally:
                writer_finished.set()

        writer = threading.Thread(target=import_next_generation, daemon=True)
        writer.start()
        with patch.object(app.performance, "video", side_effect=read_video):
            status, _, result = self.request("GET", f"/api/plans/{plan['id']}/comparison")
        self.assertTrue(writer_finished.wait(3), "import should finish after the comparison releases its lock")
        writer.join(1)
        self.assertEqual(errors, [])
        self.assertEqual(status, 200, result)
        self.assertEqual(writer_was_blocked, [True])
        self.assertEqual(reads, [("123456", 1), ("654321", 1)])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["delta"], 0)
        self.assertEqual(generation[0], 2)


if __name__ == "__main__":
    unittest.main()
