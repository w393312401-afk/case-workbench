"""Real restart endpoint, independent processes and browser progress; all records are disposable."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright

import launcher_control as launcher
from test_server import CHROMIUM_BLOCKED_PORTS


ROOT = Path(__file__).resolve().parents[1]
FINAL = "重启验证完整结果：第一段、第二段、第三段均已保存。"

# This runner is generated only inside the disposable installation. Production has no test routes or switches.
HOST_RUNNER = r'''
import json, os, threading, time
from pathlib import Path
import server

control = Path(__file__).parent / "controls"
control.mkdir(exist_ok=True)
journal_lock = threading.Lock()

def record(event, **fields):
    with journal_lock:
        with (control / "journal.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps({"event": event, "pid": os.getpid(), **fields}, ensure_ascii=False) + "\n")
            out.flush()
            os.fsync(out.fileno())

def wait(name, cancel=None):
    deadline = time.monotonic() + 180
    while not (control / name).exists():
        if cancel is not None and cancel.is_set():
            record("cancelled", gate=name)
            raise RuntimeError("The isolated task was unexpectedly cancelled")
        if time.monotonic() > deadline:
            raise TimeoutError("The isolated task's control gate was not released")
        time.sleep(.02)

class ControlledBridge:
    def status(self):
        return {"available": True, "label": "可控重启验证连接"}

    def run(self, prompt, *, cwd, on_progress, cancel_event, on_event=None, **options):
        record("bridge_started")
        if on_event:
            on_event({"type": "session", "model": "controlled-restart-test", "reasoning_effort": "low"})
        for step, marker in ((1, "PRE-01"), (2, "POST-02"), (3, "RELOADED-03")):
            if step > 1:
                wait("step-" + str(step), cancel_event)
            if on_event:
                on_event({"type": "message", "item_id": "continuity-progress", "phase": "commentary",
                          "text": marker + " 真实后台任务持续运行", "complete": True})
            on_progress(marker + " 真实后台任务持续运行")
            record("progress", marker=marker)
        wait("finish", cancel_event)
        answer = "重启验证完整结果：第一段、第二段、第三段均已保存。"
        if on_event:
            on_event({"type": "message", "item_id": "continuity-answer", "phase": "final_answer",
                      "text": answer, "complete": True})
        record("bridge_returned", output=answer)
        return answer

original_make = server.make_server

def make_server(*args, **kwargs):
    kwargs["ai_bridge"] = ControlledBridge()
    host = original_make(*args, **kwargs)
    original_finished = host.app.ai.on_finished
    def finished(task_id):
        record("callback_started", task_id=task_id)
        wait("callback-finish")
        original_finished(task_id)
        record("callback_saved", task_id=task_id)
    host.app.ai.on_finished = finished
    return host

server.make_server = make_server
server.main()
'''


@unittest.skipUnless(os.name == "nt", "The desktop restart launcher uses Windows process identities")
class ServiceRestartEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="service-restart-e2e-")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.close_installation)
        for source in [*ROOT.glob("*.py"), *ROOT.glob("*.pyw")]:
            shutil.copy2(source, self.root / source.name)
        for name in ("scripts", "static"):
            shutil.copytree(ROOT / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__"))
        for _ in range(32):
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            if port not in CHROMIUM_BLOCKED_PORTS and port != 8767:
                break
        else:
            self.fail("No isolated browser-safe port was available")
        self.config = {"port": port, "skill_root": str(self.root / "skill"),
                       "runtime_root": str(self.root / "runtime"), "task_host_enabled": True,
                       "performance_sources": []}
        (self.root / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        (self.root / "e2e_host.py").write_text(HOST_RUNNER, encoding="utf-8")
        self.control = self.root / "controls"
        self.control.mkdir()
        self.url = f"http://127.0.0.1:{port}"
        self.token = ""
        self.host_child = None
        self.statuses = []
        self.restarts = []
        with (self.root / "controlled-host.log").open("ab") as log:
            self.host_child = subprocess.Popen([sys.executable, "-u", str(self.root / "e2e_host.py"),
                "--task-host", "--port", "0", "--public-port", str(port),
                "--skill-root", self.config["skill_root"], "--runtime-root", self.config["runtime_root"],
                "--no-open"], cwd=str(self.root), stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW)
        self.until(lambda: launcher.task_host_manifest(self.root, self.config), "task-host manifest", timeout=30)
        launcher.restart(self.root, self.config, no_browser=True)
        status = self.request("/api/service/status")
        self.assertEqual(status["task_host_pid"], self.host_child.pid)
        self.token = status["token"]
        self.statuses.append(status)

    def until(self, read, label, timeout=20):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                value = read()
                if value:
                    return value
            except (OSError, ValueError) as error:
                last = type(error).__name__
            time.sleep(.05)
        self.fail(f"Timed out waiting for {label}; last error: {last}")

    def request(self, path, body=None, expected=200):
        request = urllib.request.Request(self.url + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", "X-Workbench-Token": self.token,
                     "Origin": self.url})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status, result = response.status, json.load(response)
        except urllib.error.HTTPError as error:
            status, result = error.code, json.load(error)
        self.assertEqual(status, expected, result)
        return result

    def journal(self):
        path = self.control / "journal.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def event(self, name):
        return [row for row in self.journal() if row["event"] == name]

    def signal(self, name):
        (self.control / name).touch()

    def stream(self, task_id):
        with urllib.request.urlopen(self.url + f"/api/ai/tasks/{task_id}/stream", timeout=10) as response:
            self.assertEqual(response.headers.get_content_type(), "text/event-stream")
            while line := response.readline():
                if line.startswith(b"data:"):
                    return json.loads(line[5:])
        self.fail("SSE returned no task snapshot")

    def restart(self, task_id, expected_status):
        before = self.request("/api/service/status")
        feed = self.request("/api/activity?owner_id=" + task_id)
        began = time.monotonic()
        accepted = self.request("/api/service/restart", {})
        self.assertTrue(accepted["ok"])
        self.assertTrue(accepted["restart_keeps_tasks"])

        def replacement():
            current = self.request("/api/service/status")
            return current if current["pid"] != before["pid"] else None

        after = self.until(replacement, "new gateway process", timeout=45)
        self.statuses.append(after)
        self.assertEqual(after["task_host_pid"], before["task_host_pid"])
        self.assertEqual(after["task_host_launch_id"], before["task_host_launch_id"])
        self.assertEqual(after["token"], before["token"])
        self.assertIsNone(self.host_child.poll())
        task = self.request("/api/ai/tasks/" + task_id)["task"]
        self.assertEqual(task["status"], expected_status)
        current_feed = self.request("/api/activity?owner_id=" + task_id)
        self.assertEqual(current_feed["epoch"], feed["epoch"])
        self.assertGreaterEqual(current_feed["cursor"], feed["cursor"])
        self.restarts.append({"old_gateway_pid": before["pid"], "new_gateway_pid": after["pid"],
            "task_host_pid": after["task_host_pid"], "task_status": task["status"],
            "seconds": round(time.monotonic() - began, 3), "token_stable": True, "activity_epoch_stable": True})
        return task

    def close_installation(self):
        if not hasattr(self, "control"):
            self.temporary.cleanup()
            return
        for name in ("step-2", "step-3", "finish", "callback-finish"):
            self.signal(name)
        if getattr(self, "url", None):
            try:
                status = self.request("/api/service/status")
                if launcher.correct_service(status, self.config):
                    owned = [row for row in launcher.matching_processes(self.root, self.config)
                             if row["ProcessId"] == status["pid"]]
                    self.token = status["token"]
                    self.request("/api/service/shutdown", {})
                    for process in owned:
                        launcher.wait_for_process_exit(process)
            except (OSError, ValueError, AssertionError):
                pass
        if self.host_child and self.host_child.poll() is None:
            try:
                host = launcher.task_host_manifest(self.root, self.config)
                if host and host["pid"] == self.host_child.pid:
                    status = launcher.task_host_status(self.root, self.config, host)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if launcher.task_host_request(host, "/api/service/ready")["ready"]:
                            break
                        time.sleep(.05)
                    launcher.task_host_request(host, "/api/service/shutdown", token=status["token"], body={})
            except (OSError, ValueError, RuntimeError):
                pass
            try:
                self.host_child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.host_child.terminate()  # Only this test's own disposable child.
                self.host_child.wait(timeout=10)
        self.temporary.cleanup()

    def test_task_progress_and_exactly_once_completion_survive_three_real_restarts(self):
        body = {"request_id": uuid4().hex, "product": "202H", "action": "review",
                "instruction": "隔离验证：跨重启保留同一任务、实时进度与完整结果。", "skill_selection": "none"}
        task = self.request("/api/ai/tasks", body)["task"]
        task_id, round_id = task["task_id"], task["rounds"][0]["round_id"]
        self.until(lambda: self.event("progress"), "initial backend progress")
        start_snapshot = self.stream(task_id)
        created_at = start_snapshot["task"]["rounds"][0]["created_at"]
        self.assertTrue(created_at)
        self.assertEqual(start_snapshot["task"]["status"], "running")
        errors = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(self.url + f"/#/202H/ai/{task_id}?round={round_id}", wait_until="domcontentloaded")
                expect(page.locator("#ai-task-status")).to_have_text("正在处理", timeout=15000)
                expect(page.locator("#ai-task-progress")).to_contain_text("PRE-01")
                page.evaluate("window.e2eDocumentIdentity = 'before-restart'")

                self.restart(task_id, "running")
                self.signal("step-2")
                # No page reload: the old SSE reader disconnects and the UI must reconnect/poll itself.
                expect(page.locator("#ai-task-progress")).to_contain_text("POST-02", timeout=20000)
                self.assertEqual(page.evaluate("e2eDocumentIdentity"), "before-restart")
                retry = self.request("/api/ai/tasks", body)["task"]
                self.assertEqual((retry["task_id"], retry["rounds"][0]["round_id"]), (task_id, round_id))
                self.request("/api/ai/tasks", {**body, "instruction": "不得创建第二个任务"}, expected=409)
                newer = self.stream(task_id)
                self.assertGreaterEqual(newer["task"]["revision"], start_snapshot["task"]["revision"])
                self.assertIn("POST-02", newer["task"]["progress"])

                self.restart(task_id, "running")
                page.reload(wait_until="domcontentloaded")
                expect(page.locator("#ai-task-progress")).to_contain_text("POST-02", timeout=15000)
                self.signal("step-3")
                expect(page.locator("#ai-task-progress")).to_contain_text("RELOADED-03", timeout=15000)
                self.assertEqual(page.locator(f'#ai-rounds > [data-round-id="{round_id}"]').count(), 1)

                self.signal("finish")
                expect(page.locator("#ai-task-status")).to_have_text("已完成", timeout=15000)
                self.until(lambda: self.event("callback_started"), "result callback")
                host = launcher.task_host_manifest(self.root, self.config)
                self.assertFalse(launcher.task_host_request(host, "/api/service/ready")["ready"])
                self.restart(task_id, "completed")  # Result saved; its completion callback is still in progress.
                page.reload(wait_until="domcontentloaded")
                output = page.locator(f'[data-round-id="{round_id}"] .ai-round-output')
                expect(output).to_have_attribute("data-source", FINAL, timeout=15000)
                expect(output).to_be_visible()
                self.assertEqual(page.locator(f'#ai-rounds > [data-round-id="{round_id}"]').count(), 1)
                self.assertEqual(page.locator(f'[data-round-id="{round_id}"] .ai-stream-message:visible').count(), 0)
                self.signal("callback-finish")
                self.until(lambda: self.event("callback_saved"), "completion callback saved")
                self.until(lambda: launcher.task_host_request(host, "/api/service/ready")["ready"], "host fully idle")
                completed = self.request("/api/ai/tasks", body)["task"]
                self.assertEqual(completed["task_id"], task_id)
                self.assertEqual(len(completed["rounds"]), 1)
                self.assertEqual(completed["rounds"][0]["output"], FINAL)
                self.assertEqual(completed["rounds"][0]["created_at"], created_at)

                files = [path for path in Path(self.config["runtime_root"]).rglob("task.json") if path.parent.name == task_id]
                self.assertEqual(len(files), 1)
                raw = json.loads(files[0].read_text(encoding="utf-8"))
                self.assertEqual(raw["status"], "completed")
                self.assertEqual(len(raw["rounds"]), 1)
                self.assertEqual(raw["rounds"][0]["round_id"], round_id)
                self.assertEqual(raw["rounds"][0]["output"], FINAL)
                self.assertTrue(raw["rounds"][0]["completed_at"])
                self.assertEqual(sum(row.get("item_id") == "continuity-answer" for row in raw["rounds"][0]["messages"]), 1)
                self.assertEqual(sum(row["request_id"] == body["request_id"] for row in raw["requests"]), 1)
                for name in ("bridge_started", "bridge_returned", "callback_started", "callback_saved"):
                    self.assertEqual(len(self.event(name)), 1, name)
                self.assertEqual(self.event("cancelled"), [])
                self.assertEqual({row["pid"] for row in self.journal()}, {self.host_child.pid})

                database = Path(self.config["runtime_root"]) / "task-activity.sqlite"
                with closing(sqlite3.connect(database)) as db:
                    runs = db.execute("SELECT run_id,status FROM runs WHERE owner_id=?", (task_id,)).fetchall()
                    self.assertEqual(len(runs), 1)
                    self.assertEqual(runs[0][1], "completed")
                    run_id = runs[0][0]
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM notifications WHERE run_id=?", (run_id,)).fetchone()[0], 1)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM logs WHERE run_id=? AND status='completed'", (run_id,)).fetchone()[0], 1)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM logs WHERE run_id=? AND status IN ('interrupted','cancelled','failed')", (run_id,)).fetchone()[0], 0)
                self.assertEqual(errors, [])
                report_path = os.environ.get("WORKBENCH_RESTART_E2E_REPORT")
                if report_path:
                    report = Path(report_path).resolve()
                    report.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(report.with_suffix(".png")), full_page=True)
                    report.write_text(json.dumps({"passed": True, "environment": "isolated temporary workbench",
                        "model": "controlled test bridge; no external model calls", "task_id": task_id, "round_id": round_id,
                        "progress_seen_in_browser": ["PRE-01", "POST-02", "RELOADED-03"], "restarts": self.restarts,
                        "bridge_calls": 1, "completion_callback_saves": 1, "persisted_results": 1, "completion_notifications": 1,
                        "result_sha256": hashlib.sha256(FINAL.encode()).hexdigest(), "browser_errors": errors},
                        ensure_ascii=False, indent=2), encoding="utf-8")
            finally:
                browser.close()
