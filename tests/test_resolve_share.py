"""Same-project edits share DaVinci per scripting call; never touches a live Resolve."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from codex_bridge import BridgeCancelled
from resolve_bridge import ResolveBridge
from resolve_share import GATE, ProjectShares, TurnServer


class Runner:
    """Answers resolve_worker requests; the screen shows whatever timeline was last restored."""

    def __init__(self):
        self.commands, self.view = [], {"uid": "material", "timecode": "01:00:00:00"}

    def __call__(self, args, input, **options):
        request = json.loads(input.decode("utf-8"))
        self.commands.append(request)
        if request["command"] == "status":
            reply = {"ok": True, "project": "Lab-202H", "project_id": "p1", "current_timeline": self.view["uid"],
                     "current_timeline_uid": self.view["uid"], "current_timecode": self.view["timecode"], "page": "edit"}
        elif request["command"] == "restore":
            self.view = {"uid": request["unique_id"], "timecode": request["timecode"]}
            reply = {"ok": True}
        else:
            reply = {"ok": True}
        return type("Done", (), {"stdout": json.dumps(reply).encode("utf-8")})()


class ShareTests(unittest.TestCase):
    def setUp(self):
        self.runner = Runner()
        self.bridge = ResolveBridge(runner=self.runner, probe=lambda: True)
        self.foreign = False
        self.shares = ProjectShares(self.bridge, foreign_waiting=lambda: self.foreign, linger=.3)
        self.cancel = threading.Event()

    def session(self, key, owner):
        return self.shares.session(key, owner, cancel=self.cancel, timeout=5)

    def test_same_project_joins_while_another_project_waits_for_all_of_it(self):
        entered = threading.Event()

        def other():
            with self.session("Q", "c"):
                entered.set()

        with self.session("P", "a"):
            with self.session("P", "b"):
                self.assertTrue(self.bridge.busy())
                worker = threading.Thread(target=other)
                worker.start()
                time.sleep(.1)
                # Another project waits in the bridge's own line until the whole share ends.
                self.assertTrue(self.bridge._access_waiters)
            time.sleep(.1)
            self.assertFalse(entered.is_set())
        worker.join(5)
        self.assertTrue(entered.is_set())
        self.assertFalse(self.bridge.busy())

    def test_joins_only_when_nobody_waits(self):
        with self.session("P", "a") as first:
            with self.session("P", "b") as second:
                self.assertIs(first, second)
                self.assertEqual(self.shares.active("P"), 2)
            self.assertTrue(self.bridge.busy())
        self.assertFalse(self.bridge.busy())
        self.assertEqual(self.shares.active("P"), 0)

    def test_a_waiting_build_is_not_overtaken_by_new_same_project_edits(self):
        order = []

        def late():
            with self.session("P", "b"):
                order.append("b")

        with self.session("P", "a"):
            self.foreign = True
            worker = threading.Thread(target=late)
            worker.start()
            time.sleep(.15)
            self.assertEqual(order, [])
            self.assertEqual(self.shares.active("P"), 1)
            order.append("a ends")
        worker.join(5)
        self.assertEqual(order, ["a ends", "b"])

    def test_turns_are_exclusive_and_each_editor_gets_its_view_back(self):
        with self.session("P", "a") as share, self.session("P", "b"):
            with self.shares.turn(share, "a", cancel=self.cancel, timeout=5, view=True) as note:
                self.assertEqual(note, "")
                self.runner.view = {"uid": "target-a", "timecode": "01:00:05:00"}
            inside, done = threading.Event(), threading.Event()

            def b_turn():
                with self.shares.turn(share, "b", cancel=self.cancel, timeout=5, view=True):
                    inside.set()
                    self.runner.view = {"uid": "target-b", "timecode": "01:00:09:00"}
                done.set()

            with self.shares.turn(share, "a", cancel=self.cancel, timeout=5, view=True):
                worker = threading.Thread(target=b_turn)
                worker.start()
                time.sleep(.1)
                self.assertFalse(inside.is_set())
            worker.join(5)
            self.assertTrue(done.is_set())
            with self.shares.turn(share, "a", cancel=self.cancel, timeout=5, view=True) as note:
                self.assertEqual(note, "")
                self.assertEqual(self.runner.view, {"uid": "target-a", "timecode": "01:00:05:00"})
        commands = [request["command"] for request in self.runner.commands]
        # b's turn read a's view; a's next turn read b's view and put a's back.
        self.assertEqual(commands, ["status", "status", "restore"])
        restore = self.runner.commands[-1]
        self.assertEqual((restore["unique_id"], restore["timecode"], restore["project_id"]), ("target-a", "01:00:05:00", "p1"))

    def test_an_editor_keeps_its_turn_for_a_moment_after_a_call(self):
        order = []
        with self.session("P", "a") as share, self.session("P", "b"):
            def b_turn():
                with self.shares.turn(share, "b", cancel=self.cancel, timeout=5):
                    order.append("b")

            with self.shares.turn(share, "a", cancel=self.cancel, timeout=5, linger=.3):
                worker = threading.Thread(target=b_turn)
                worker.start()
                time.sleep(.05)
            with self.shares.turn(share, "a", cancel=self.cancel, timeout=5, linger=.3):
                order.append("a again")
            worker.join(5)
        self.assertEqual(order, ["a again", "b"])

    def test_cancel_while_waiting_for_a_turn(self):
        with self.session("P", "a") as share:
            with self.shares.turn(share, "a", cancel=self.cancel, timeout=5):
                stop = threading.Event()
                stop.set()
                with self.assertRaises(BridgeCancelled):
                    with self.shares.turn(share, "b", cancel=stop, timeout=5):
                        pass
            self.assertEqual(share.queue, [])


FAKE_MCP = r'''
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" in message:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                                     "result": {"content": [{"type": "text", "text": "ran " + message["params"]["name"]}]}}) + "\n")
        sys.stdout.flush()
'''


class GateTests(unittest.TestCase):
    def setUp(self):
        self.runner = Runner()
        self.bridge = ResolveBridge(runner=self.runner, probe=lambda: True)
        self.shares = ProjectShares(self.bridge, linger=0)
        self.cancel = threading.Event()
        folder = Path(tempfile.mkdtemp())
        (folder / "fake_mcp.py").write_text(FAKE_MCP, encoding="utf-8")
        self.fake = folder / "fake_mcp.py"

    def call(self, process, number, name):
        process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": number, "method": "tools/call",
                                         "params": {"name": name, "arguments": {}}}) + "\n").encode("utf-8"))
        process.stdin.flush()

    def test_scripting_calls_wait_for_the_turn_but_documentation_does_not(self):
        with self.shares.session("P", "a", cancel=self.cancel, timeout=5) as share, \
                self.shares.session("P", "b", cancel=self.cancel, timeout=5):
            server = TurnServer(self.shares, share, "a", self.cancel, 5)
            config = server.mcp_config({"command": sys.executable, "args": ["-I", str(self.fake)]})
            self.assertEqual(config["args"][3], str(GATE))
            process = subprocess.Popen([config["command"], *config["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            replies = []
            reader = threading.Thread(target=lambda: [replies.append(json.loads(line)) for line in process.stdout])
            reader.start()
            try:
                other = self.shares.turn(share, "b", cancel=self.cancel, timeout=5)
                other.__enter__()
                self.call(process, 1, "run_script")
                self.call(process, 2, "get_scripting_docs")
                time.sleep(1.0)
                self.assertEqual([reply["id"] for reply in replies], [])  # the docs call queues behind the waiting script
                other.__exit__(None, None, None)
                deadline = time.monotonic() + 5
                while len(replies) < 2 and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertCountEqual([reply["id"] for reply in replies], [1, 2])
                deadline = time.monotonic() + 5
                while share.holder is not None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertIsNone(share.holder)
                self.assertEqual(server.stats["calls"], 1)
                # After b used DaVinci, a's next call carries a note when its view could not be put back.
                share.views["a"] = {"error": "boom"}
                share.view_owner = "b"
                self.call(process, 3, "run_script")
                deadline = time.monotonic() + 5
                while len(replies) < 3 and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertIn("没能完全切回", replies[-1]["result"]["content"][0]["text"])
                self.assertEqual(replies[-1]["result"]["content"][1]["text"], "ran run_script")
            finally:
                process.stdin.close()
                process.wait(5)
                reader.join(5)
                server.close()
            self.assertIsNone(share.holder)

    def test_a_stopped_edit_answers_calls_without_running_them(self):
        with self.shares.session("P", "a", cancel=self.cancel, timeout=5) as share:
            server = TurnServer(self.shares, share, "a", self.cancel, 5)
            config = server.mcp_config({"command": sys.executable, "args": ["-I", str(self.fake)]})
            process = subprocess.Popen([config["command"], *config["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            try:
                self.cancel.set()
                self.call(process, 7, "run_script")
                reply = json.loads(process.stdout.readline())
                self.assertTrue(reply["result"]["isError"])
                self.assertIn("剪辑已停止", reply["result"]["content"][0]["text"])
            finally:
                process.stdin.close()
                process.wait(5)
                server.close()


if __name__ == "__main__":
    unittest.main()
