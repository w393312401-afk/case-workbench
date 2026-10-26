"""Seek an order timeline using fakes only; these tests never connect to Resolve."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import test_server as fixture
import resolve_worker as worker
from elevenlabs_client import VoiceError
from test_production_resolve import FakeProject, FakeResolve
from workbench_production import ProductionWorkbenchMixin


RECORD = {"project": "202H", "unique_id": "timeline-1", "name": "Lab261014-202-1", "frames": 400}


class OrderOpenFrameTests(unittest.TestCase):
    def setUp(self):
        self.bench = SimpleNamespace(production=Mock(), resolve=Mock())
        self.bench.production.job.return_value = {"timelines": [RECORD]}
        self.bench.resolve.run.return_value = {"ok": True, "name": RECORD["name"]}

    def open(self, body):
        return ProductionWorkbenchMixin.open_timeline(self.bench, "PRD-0123456789ab", body)

    def test_optional_relative_frame_is_forwarded_and_absence_keeps_original_payload(self):
        for frame in (None, 0, 399, 500):
            with self.subTest(frame=frame):
                body = {"unique_id": RECORD["unique_id"]}
                payload = {key: RECORD[key] for key in ("project", "unique_id", "name")}
                if frame is not None:
                    body["frame"] = payload["frame"] = frame
                self.assertTrue(self.open(body)["ok"])
                self.bench.resolve.run.assert_called_with("open", payload, timeout=30)
        # A live timeline can be longer than its old build record; bounds belong in the worker.

    def test_invalid_frame_does_not_invoke_resolve(self):
        for frame in (-1, True, False, None, "0", 1.0, 1.5):
            with self.subTest(frame=frame):
                with self.assertRaises(VoiceError) as caught:
                    self.open({"unique_id": RECORD["unique_id"], "frame": frame})
                self.assertEqual(caught.exception.status, 400)
        self.bench.resolve.run.assert_not_called()

    def test_frame_cannot_open_a_timeline_outside_this_order(self):
        with self.assertRaises(VoiceError) as caught:
            self.open({"unique_id": "another-order", "frame": 0})
        self.assertEqual(caught.exception.status, 404)
        self.bench.resolve.run.assert_not_called()


class WorkerOpenFrameTests(unittest.TestCase):
    def setUp(self):
        self.project = FakeProject(names=["当前时间线", RECORD["name"]])
        self.target = self.project.items[1]
        self.project.current = self.project.items[0]
        self.resolve = FakeResolve(self.project)
        self.request = {"project": "202H", "unique_id": self.target.uid, "name": self.target.name}

    def configure(self, *, start=108000, end=108400, fps="30", drop=False):
        self.target.GetStartFrame = Mock(return_value=start)
        self.target.GetEndFrame = Mock(return_value=end)
        self.target.GetSetting = Mock(side_effect=lambda key: {
            "timelineFrameRate": fps, "timelineDropFrameTimecode": "1" if drop else "0"}[key])

    def test_without_frame_switches_only_and_leaves_the_playhead(self):
        with patch.object(self.target, "SetCurrentTimecode") as seek:
            result = worker.open_timeline(self.resolve, self.request)
        self.assertEqual(result, {"name": self.target.name})
        seek.assert_not_called()
        self.assertIs(self.project.current, self.target)
        self.assertEqual(self.resolve.page, "edit")

    def test_relative_offset_uses_live_start_and_fractional_drop_frame_timecode(self):
        cases = [
            ("30", False, 108000, 63, "01:00:02:03"),
            ("23.976", False, 86400, 49, "01:00:02:01"),
            ("29.97", False, 108000, 31, "01:00:01:01"),
            ("29.97", True, 107892, 0, "01:00:00;00"),
            ("29.97", True, 107892, 1800, "01:01:00;02"),
            ("59.94", True, 215784, 3600, "01:01:00;04"),
        ]
        for fps, drop, start, offset, expected in cases:
            with self.subTest(fps=fps, drop=drop, offset=offset):
                self.configure(start=start, end=start + 4000, fps=fps, drop=drop)
                result = worker.open_timeline(self.resolve, {**self.request, "frame": offset})
                self.assertEqual(result, {"name": self.target.name, "frame": offset, "timecode": expected})

    def test_live_bounds_reject_out_of_range_before_switching_and_allow_final_frame(self):
        self.configure()
        original = self.project.current
        for offset in (-1, 400, 100000, True, 1.5, "0", None):
            with self.subTest(frame=offset):
                with self.assertRaises(worker.WorkerError):
                    worker.open_timeline(self.resolve, {**self.request, "frame": offset})
                self.assertIs(self.project.current, original)
                self.assertIsNone(self.resolve.page)
        result = worker.open_timeline(self.resolve, {**self.request, "frame": 399})
        self.assertEqual(result["timecode"], "01:00:13:09")

    def test_empty_timeline_and_invalid_rate_do_not_move_the_user(self):
        for settings in ({"end": 108000}, {"fps": "invalid"}, {"fps": "0"}, {"fps": "-30"}):
            with self.subTest(settings=settings):
                self.configure(**settings)
                with self.assertRaises(worker.WorkerError):
                    worker.open_timeline(self.resolve, {**self.request, "frame": 0})
                self.assertIs(self.project.current, self.project.items[0])
                self.assertIsNone(self.resolve.page)

    def test_failed_seek_or_wrong_readback_reports_failure(self):
        self.configure()
        for accepted in (False, True):
            with self.subTest(accepted=accepted), patch.object(self.target, "SetCurrentTimecode", return_value=accepted):
                with self.assertRaises(worker.WorkerError) as caught:
                    worker.open_timeline(self.resolve, {**self.request, "frame": 60})
                self.assertIn("播放头", str(caught.exception))

    def test_drop_frame_separator_normalization_preserves_same_frame(self):
        self.configure(start=107892, end=111892, fps="29.97", drop=True)
        seek = self.target.SetCurrentTimecode
        with patch.object(self.target, "SetCurrentTimecode", side_effect=lambda code: seek(code.replace(";", ":"))):
            result = worker.open_timeline(self.resolve, {**self.request, "frame": 1800})
        self.assertEqual(result["timecode"], "01:01:00:02")


class OpenFrameHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def test_route_forwards_frame_and_rejects_malformed_values(self):
        self.server.app.production.job = Mock(return_value={"timelines": [RECORD]})
        self.server.app.resolve = Mock()
        self.server.app.resolve.run.return_value = {"ok": True, "name": RECORD["name"], "frame": 15, "timecode": "01:00:00:15"}
        route = "/api/production/jobs/PRD-0123456789ab/open"
        status, _, result = self.request("POST", route, {"unique_id": RECORD["unique_id"], "frame": 15})
        self.assertEqual((status, result["frame"]), (200, 15))
        self.server.app.resolve.run.assert_called_once_with("open", {
            "project": RECORD["project"], "unique_id": RECORD["unique_id"], "name": RECORD["name"], "frame": 15}, timeout=30)
        self.server.app.resolve.run.reset_mock()
        for frame in (-1, False, "15", 15.5, None):
            with self.subTest(frame=frame):
                self.assertEqual(self.request("POST", route, {"unique_id": RECORD["unique_id"], "frame": frame})[0], 400)
        self.server.app.resolve.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
