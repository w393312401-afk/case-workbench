"""Media preparation stays windowless and retains text and raw frame outputs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

import retro_draft as review


class MediaProcessTests(unittest.TestCase):
    def test_windows_media_tools_have_no_console_or_interactive_input(self):
        completed = subprocess.CompletedProcess([], 0, "4.0\n", "")
        with patch.object(review.os, "name", "nt"), \
                patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                patch.object(review.subprocess, "run", return_value=completed) as run:
            result = review._run("ffprobe", "-v", "error", timeout=17)
        self.assertIs(completed, result)
        self.assertEqual(("ffprobe", "-v", "error"), run.call_args.args[0])
        self.assertEqual(0x08000000, run.call_args.kwargs["creationflags"])
        self.assertEqual(subprocess.DEVNULL, run.call_args.kwargs["stdin"])
        self.assertTrue(run.call_args.kwargs["capture_output"])
        self.assertEqual(17, run.call_args.kwargs["timeout"])

    def test_other_platforms_do_not_receive_windows_creation_flags(self):
        with patch.object(review.os, "name", "posix"), \
                patch.object(review.subprocess, "run") as run:
            review._run("ffmpeg", "-version")
        self.assertEqual(0, run.call_args.kwargs["creationflags"])
        self.assertEqual(subprocess.DEVNULL, run.call_args.kwargs["stdin"])

    def test_text_and_binary_outputs_remain_readable(self):
        text = review._run(sys.executable, "-c", "import sys; sys.stdout.buffer.write('画面准备完成'.encode('utf-8'))")
        self.assertEqual(0, text.returncode)
        self.assertEqual("画面准备完成", text.stdout)
        binary = review._run(sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes(range(256)))", text=False)
        self.assertEqual(0, binary.returncode)
        self.assertEqual(bytes(range(256)), binary.stdout)

    def test_shot_comparison_uses_windowless_binary_capture_for_both_frames(self):
        raw = bytes(range(256)) * 36  # Exactly one 72 x 128 grayscale frame, including non-UTF-8 bytes.
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.mp4"
            source.touch()
            clip = {"media_id": "M1", "media_path": str(source), "name": source.name,
                    "start": 0, "end": 60, "source_start": 0, "source_end": 60}
            timeline = {"tracks": {"video": [{"index": 1, "items": [clip]}]}}
            with patch.object(review.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, raw, b"")) as run:
                rows = review.label_shots(source, timeline, [], 30.0)
        self.assertEqual(2, run.call_count)
        for call in run.call_args_list:
            self.assertFalse(call.kwargs["text"])
            self.assertIsNone(call.kwargs["encoding"])
            self.assertIsNone(call.kwargs["errors"])
            self.assertEqual(subprocess.DEVNULL, call.kwargs["stdin"])
            self.assertEqual(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, call.kwargs["creationflags"])
            self.assertEqual(60, call.kwargs["timeout"])
        self.assertTrue(rows[0]["match"])
        self.assertEqual(1.0, rows[0]["similarity"])

    @unittest.skipUnless(os.name == "nt", "Windows console regression")
    def test_media_child_of_pythonw_has_no_console_window(self):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.is_file():
            self.skipTest("pythonw.exe is unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            driver, result = root / "driver.py", root / "result.json"
            driver.write_text("\n".join([
                "import ctypes, json, sys",
                "from pathlib import Path",
                f"sys.path.insert(0, {str(APP_ROOT)!r})",
                "from retro_draft import _run",
                "child = _run(sys.argv[1], '-c', 'import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())', timeout=20)",
                "Path(sys.argv[2]).write_text(json.dumps({'parent': ctypes.windll.kernel32.GetConsoleWindow(), 'child': child.stdout.strip(), 'returncode': child.returncode}), encoding='utf-8')",
            ]), encoding="utf-8")
            subprocess.run([str(pythonw), str(driver), sys.executable, str(result)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=30, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            data = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual({"parent": 0, "child": "0", "returncode": 0}, data)


if __name__ == "__main__":
    unittest.main()
