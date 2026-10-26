"""Standalone image CLI contract, entirely fake: never starts Codex or generates images."""
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import codex_image_bridge as module
from codex_bridge import BridgeCancelled, BridgeError, BridgeUnavailable


class Output:
    def __init__(self):
        self.queue = queue.Queue()
        self.closed = False

    def readline(self, limit):
        value = self.queue.get()
        return value or b""

    def emit(self, event):
        self.queue.put(json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n")

    def close(self):
        if not self.closed:
            self.closed = True
            self.queue.put(None)


class Input:
    def __init__(self, process):
        self.process, self.payload, self.closed = process, bytearray(), False

    def write(self, value):
        # Exercise partial writes as well as non-ASCII stdin input.
        chunk = bytes(value[:7])
        self.payload.extend(chunk)
        return len(chunk)

    def flush(self):
        pass

    def close(self):
        if not self.closed:
            self.closed = True
            self.process.scenario(self.process)


class Process:
    def __init__(self, args, scenario):
        self.args, self.scenario, self.returncode = args, scenario, None
        self.pid, self.killed, self.tree_terminated = 123456789, False, False
        self.stdout = Output()
        self.stderr = io.BytesIO(b"secret-token C:/private/path diagnostics")
        self.stdin = Input(self)
        self.answer_path = Path(args[args.index("--output-last-message") + 1])

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake-codex", timeout)
        return self.returncode

    def exit(self, code=0):
        self.returncode = code
        self.stdout.close()

    def kill(self):
        self.killed = True
        self.exit(-9)


def success(process):
    process.stdout.emit({"type": "thread.started", "thread_id": "isolated-thread"})
    process.stdout.emit({"type": "turn.started"})
    process.stdout.emit({"type": "item.completed", "item": {"id": "visible", "type": "agent_message", "text": "正在核对图片。"}})
    process.stdout.emit({"type": "item.started", "item": {"id": "private", "type": "command_execution", "command": "secret command"}})
    process.stdout.emit({"type": "turn.completed"})
    process.answer_path.write_text('{"file": "output/final.png"}', encoding="utf-8")
    process.exit()


class ImageBridgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="codex-image-fake-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cwd = self.root / "job"
        self.cwd.mkdir()
        self.image = self.cwd / "参考底图.png"
        self.image.write_bytes(b"fake-image")
        self.cancel, self.progress, self.events, self.launches = threading.Event(), [], [], []
        patcher = patch("codex_image_bridge.locate_codex", return_value="fake-codex.exe")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("codex_image_bridge._terminate_process_tree", side_effect=self.stop_tree)
        self.stop_mock = patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def stop_tree(process):
        if process.poll() is None:
            process.tree_terminated = True
            process.kill()

    def bridge(self, scenario=success):
        def factory(args, **options):
            process = Process(args, scenario)
            self.launches.append((args, options, process))
            return process
        return module.CodexImageBridge(popen_factory=factory)

    def run_bridge(self, bridge, **options):
        return bridge.run("请保留主体并优化背景。", cwd=self.cwd, images=[self.image],
                          cancel_event=self.cancel, on_progress=self.progress.append,
                          on_event=self.events.append, **options)

    def test_command_is_isolated_uses_stdin_images_schema_and_client_defaults(self):
        second = self.cwd / "第二张.jpg"
        second.write_bytes(b"fake")
        schema = {"type": "object", "properties": {"file": {"type": "string"}}}
        result = self.bridge().run("中文要求\n保留字面 $() 和反引号。", cwd=self.cwd,
                                   images=[self.image, second.name], cancel_event=self.cancel,
                                   on_progress=self.progress.append, schema=schema)
        args, options, process = self.launches[0]
        self.assertEqual(result, '{"file": "output/final.png"}')
        self.assertLess(args.index("-a"), args.index("exec"))
        self.assertEqual(args[args.index("-a") + 1], "never")
        self.assertIn("--no-daemon", args)
        self.assertIn("--ephemeral", args)
        self.assertIn("--json", args)
        self.assertIn("--skip-git-repo-check", args)
        self.assertEqual(args[args.index("-s") + 1], "workspace-write")
        self.assertEqual(args[args.index("-C") + 1], str(self.cwd.resolve()))
        self.assertEqual([args[i + 1] for i, value in enumerate(args) if value == "-i"],
                         [str(self.image.resolve()), str(second.resolve())])
        self.assertEqual(args[-1], "-")
        self.assertEqual(process.stdin.payload.decode("utf-8"), "中文要求\n保留字面 $() 和反引号。")
        self.assertFalse(any(value in args for value in ("--model", "-m", "--add-dir", "danger-full-access", "--oss")))
        self.assertNotIn("-c", args)
        self.assertFalse(options["shell"])
        self.assertEqual(options["cwd"], str(self.cwd.resolve()))
        self.assertTrue(process.answer_path.is_absolute())
        self.assertTrue(process.answer_path.is_relative_to(self.cwd.resolve()))
        schema_path = Path(args[args.index("--output-schema") + 1])
        self.assertTrue(schema_path.is_absolute())
        self.assertEqual(json.loads(schema_path.read_text(encoding="utf-8")), schema)
        if module._WINDOWS:
            self.assertEqual(options["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        else:
            self.assertTrue(options["start_new_session"])

    def test_read_only_review_retains_client_output_files_and_does_not_add_write_access(self):
        self.run_bridge(self.bridge(), read_only=True, schema={"type": "object"})
        args, _, process = self.launches[0]
        self.assertEqual(args[args.index("-s") + 1], "read-only")
        self.assertNotIn("workspace-write", args)
        self.assertTrue(process.answer_path.is_file())
        self.assertIn("不得调用图片生成工具", process.stdin.payload.decode("utf-8"))
        self.assertIn("不得编辑或重新生成图片", process.stdin.payload.decode("utf-8"))

    def test_reasoning_override_is_one_run_only_and_keeps_the_configured_model(self):
        bridge = self.bridge()
        self.run_bridge(bridge, read_only=True, reasoning_effort="low")
        args, _, process = self.launches[-1]
        self.assertEqual([args[index + 1] for index, value in enumerate(args) if value == "-c"],
                         ['model_reasoning_effort="low"'])
        self.assertNotIn("--model", args)
        self.assertNotIn("-m", args)
        self.assertEqual(args[args.index("-s") + 1], "read-only")
        receipt = json.loads((process.answer_path.parent / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["reasoning_effort_override"], "low")
        self.run_bridge(bridge)
        self.assertNotIn("-c", self.launches[-1][0])

    def test_invalid_reasoning_override_never_launches_a_process(self):
        for effort in ([], {}, True, 4, "LOW", "low\nmodel=x", 'low" --model other'):
            with self.subTest(effort=effort), self.assertRaisesRegex(BridgeError, "推理强度"):
                self.run_bridge(self.bridge(), reasoning_effort=effort)
        self.assertEqual(self.launches, [])

    def test_read_only_generation_attempt_is_stopped_and_receipt_names_actual_process(self):
        def forbidden(process):
            process.stdout.emit({"type": "thread.started", "thread_id": "review-thread"})
            process.stdout.emit({"type": "item.started", "item": {"type": "image_generation_call"}})
        with self.assertRaisesRegex(BridgeError, "只读核对任务尝试再次生图"):
            self.run_bridge(self.bridge(forbidden), read_only=True)
        process = self.launches[-1][2]
        self.assertTrue(process.tree_terminated)
        receipt = json.loads((process.answer_path.parent / "receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["pid"], process.pid)
        self.assertEqual(receipt["thread_id"], "review-thread")
        self.assertIsNone(receipt["model"])
        self.assertTrue(receipt["started_at"])
        self.assertTrue(receipt["finished_at"])

    def test_stream_exposes_agent_text_and_authoritative_final_but_no_commands_or_stderr(self):
        self.assertEqual(self.run_bridge(self.bridge()), '{"file": "output/final.png"}')
        visible = [event for event in self.events if event["type"] == "message"]
        self.assertEqual(visible[0]["text"], "正在核对图片。")
        self.assertEqual(visible[-1]["phase"], "final_answer")
        self.assertTrue(visible[-1]["complete"])
        self.assertNotIn("secret", json.dumps(self.events) + "".join(self.progress))

    def test_each_run_has_a_fresh_answer_file_and_never_accepts_previous_output(self):
        self.run_bridge(self.bridge())
        with self.assertRaisesRegex(BridgeError, "最终回答"):
            self.run_bridge(self.bridge(lambda process: process.exit()))
        self.assertNotEqual(self.launches[0][2].answer_path, self.launches[1][2].answer_path)

    def test_cancel_before_launch_and_during_running_job(self):
        self.cancel.set()
        with self.assertRaises(BridgeCancelled):
            self.run_bridge(self.bridge())
        self.assertEqual(self.launches, [])
        self.cancel.clear()
        def scenario(process):
            process.stdout.emit({"type": "turn.started"})
        def progress(message):
            if "正在处理" in message:
                self.cancel.set()
        with self.assertRaises(BridgeCancelled):
            self.bridge(scenario).run("test", cwd=self.cwd, images=[], cancel_event=self.cancel, on_progress=progress)
        self.assertTrue(self.launches[-1][2].tree_terminated)

    def test_timeout_terminates_process_tree(self):
        with self.assertRaisesRegex(BridgeError, "超时"):
            self.run_bridge(self.bridge(lambda process: None), timeout=.03)
        self.assertTrue(self.launches[0][2].tree_terminated)

    def test_nonzero_exit_even_with_answer_file_is_a_safe_failure(self):
        def scenario(process):
            process.answer_path.write_text("must not return partial output", encoding="utf-8")
            process.stdout.emit({"type": "error", "message": "unauthorized secret-token C:/private/path"})
            process.exit(1)
        with self.assertRaisesRegex(BridgeError, "登录") as caught:
            self.run_bridge(self.bridge(scenario))
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("C:/private", str(caught.exception))

    def test_failed_turn_stops_job_and_maps_quota_error_without_raw_message(self):
        def scenario(process):
            process.stdout.emit({"type": "turn.failed", "error": {"codexErrorInfo": "usageLimitExceeded", "message": "secret quota data"}})
        with self.assertRaisesRegex(BridgeError, "额度") as caught:
            self.run_bridge(self.bridge(scenario))
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(self.launches[0][2].tree_terminated)

    def test_invalid_stream_and_invalid_or_empty_final_output_are_safe_failures(self):
        for answer in (None, b"", b"\xff", b"too long"):
            with self.subTest(answer=answer):
                def scenario(process):
                    if answer is None:
                        process.stdout.queue.put(b"invalid-json secret\n")
                    else:
                        process.answer_path.write_bytes(answer)
                        process.exit()
                with patch("codex_image_bridge.MAX_ANSWER_BYTES", 3), self.assertRaises(BridgeError) as caught:
                    self.run_bridge(self.bridge(scenario))
                self.assertNotIn("secret", str(caught.exception))

    def test_event_consumer_failure_ends_running_job(self):
        def scenario(process):
            process.stdout.emit({"type": "item.completed", "item": {"id": "message", "type": "agent_message", "text": "progress"}})
        def broken(event):
            raise RuntimeError("storage full")
        with self.assertRaisesRegex(RuntimeError, "storage full"):
            self.bridge(scenario).run("test", cwd=self.cwd, images=[], cancel_event=self.cancel,
                                     on_progress=self.progress.append, on_event=broken)
        self.assertTrue(self.launches[0][2].tree_terminated)

    def test_reference_outside_job_directory_and_bad_arguments_never_launch(self):
        outside = self.root / "outside.png"
        outside.write_bytes(b"fake")
        for options in ({"images": [outside]}, {"images": "not-a-list"}, {"timeout": float("nan")}, {"schema": "bad"}):
            with self.subTest(options=options), self.assertRaises(BridgeError):
                values = {"images": [self.image], **options}
                self.bridge().run("test", cwd=self.cwd, cancel_event=self.cancel,
                                  on_progress=self.progress.append, **values)
        self.assertEqual(self.launches, [])

    def test_unavailable_status_or_launch_oserror_never_exposes_raw_diagnostics(self):
        bridge = self.bridge()
        with patch("codex_image_bridge.locate_codex", return_value=None):
            self.assertFalse(bridge.status()["available"])
            with self.assertRaises(BridgeUnavailable):
                self.run_bridge(bridge)
        self.assertEqual(self.launches, [])
        def fail(*args, **options):
            raise OSError("secret account path")
        with self.assertRaises(BridgeUnavailable) as caught:
            self.run_bridge(module.CodexImageBridge(popen_factory=fail))
        self.assertNotIn("secret", str(caught.exception))


class TreeTerminationTests(unittest.TestCase):
    def test_windows_uses_hidden_taskkill_with_pid_tree_and_force(self):
        process = Process(["--output-last-message", "unused"], lambda process: None)
        def kill_tree(*args, **options):
            process.exit(-9)
        with patch("codex_image_bridge._WINDOWS", True), patch("codex_image_bridge.subprocess.run", side_effect=kill_tree) as runner:
            module._terminate_process_tree(process)
        self.assertEqual(runner.call_args.args[0], ["taskkill", "/PID", str(process.pid), "/T", "/F"])
        self.assertEqual(runner.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

    def test_tree_kill_failure_falls_back_to_process_kill(self):
        process = Process(["--output-last-message", "unused"], lambda process: None)
        with patch("codex_image_bridge._WINDOWS", True), patch("codex_image_bridge.subprocess.run", side_effect=OSError("fake failure")):
            module._terminate_process_tree(process)
        self.assertTrue(process.killed)


if __name__ == "__main__":
    unittest.main()
