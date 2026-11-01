"""Background edits and carousel steps share one cancellable Resolve queue."""
import json
import threading
import types
import unittest
from unittest import mock

from codex_bridge import BridgeCancelled
from elevenlabs_client import VoiceError
from resolve_bridge import ResolveBridge


class ResolveAccessTests(unittest.TestCase):
    def setUp(self):
        self.bridge = ResolveBridge(probe=lambda: True)

    def test_background_steps_are_fifo_and_exclusive(self):
        bridge = self.bridge
        order, errors, threads = [], [], []
        first_waiting, second_waiting = threading.Event(), threading.Event()
        first_entered, release_first, second_entered = threading.Event(), threading.Event(), threading.Event()

        def work(name, waiting, entered, release=None):
            try:
                with bridge.access(timeout=3, on_wait=waiting.set):
                    order.append(name)
                    self.assertTrue(bridge.busy())
                    entered.set()
                    if release is not None and not release.wait(3):
                        raise RuntimeError('queue test timed out')
            except Exception as exc:
                errors.append(exc)

        bridge.lock.acquire()
        try:
            threads.append(threading.Thread(target=work, args=('edit', first_waiting, first_entered, release_first)))
            threads[-1].start()
            self.assertTrue(first_waiting.wait(2))
            threads.append(threading.Thread(target=work, args=('stills', second_waiting, second_entered)))
            threads[-1].start()
            self.assertTrue(second_waiting.wait(2))
            self.assertEqual(order, [])
        finally:
            bridge.lock.release()
        try:
            self.assertTrue(first_entered.wait(2))
            self.assertFalse(second_entered.is_set())
        finally:
            release_first.set()
            for thread in threads:
                thread.join(4)
        self.assertFalse(errors, errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(order, ['edit', 'stills'])
        self.assertFalse(bridge.busy())
        self.assertEqual(bridge._access_waiters, [])

    def test_a_request_can_go_ahead_of_named_waiting_requests_only(self):
        bridge, order, threads = self.bridge, [], []
        editing, other = object(), object()

        def work(name, waiting, **options):
            with bridge.access(timeout=3, on_wait=waiting.set, **options):
                order.append(name)

        bridge.lock.acquire()
        try:
            for name, options in (("edit", {"ticket": editing}), ("stills", {"ticket": other}),
                                  ("snapshot", {"ahead_of": [object(), editing]}), ("late", {"ahead_of": [object()]})):
                waiting = threading.Event()
                threads.append(threading.Thread(target=work, args=(name, waiting), kwargs=options))
                threads[-1].start()
                self.assertTrue(waiting.wait(2))
            # The snapshot sits just before the named request; a request naming nobody waiting goes last.
            self.assertEqual(bridge._access_waiters[1:3], [editing, other])
            self.assertEqual(len(bridge._access_waiters), 4)
        finally:
            bridge.lock.release()
            for thread in threads:
                thread.join(4)
        self.assertEqual(order, ["snapshot", "edit", "stills", "late"])
        self.assertEqual(bridge._access_waiters, [])

    def test_cancelled_waiter_never_releases_foreign_lock_or_blocks_next_step(self):
        bridge, cancel, waiting = self.bridge, threading.Event(), threading.Event()
        outcomes = []

        def work():
            try:
                with bridge.access(cancel_event=cancel, timeout=3, on_wait=waiting.set):
                    outcomes.append('unexpected admission')
            except BridgeCancelled:
                outcomes.append('cancelled')

        bridge.lock.acquire()
        thread = threading.Thread(target=work)
        try:
            thread.start()
            self.assertTrue(waiting.wait(2))
            cancel.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes, ['cancelled'])
            self.assertTrue(bridge.busy())
            self.assertEqual(bridge._access_waiters, [])
        finally:
            cancel.set()
            bridge.lock.release()
            thread.join(4)
        with bridge.access(timeout=1):
            self.assertTrue(bridge.busy())
        self.assertFalse(bridge.busy())

    def test_timeout_and_operation_error_both_clean_up_the_queue(self):
        bridge = self.bridge
        bridge.lock.acquire()
        try:
            with self.assertRaises(VoiceError) as caught:
                with bridge.access(timeout=.01):
                    self.fail('must wait for the foreign lock')
            self.assertEqual(caught.exception.status, 504)
            self.assertTrue(bridge.busy())
            self.assertEqual(bridge._access_waiters, [])
        finally:
            bridge.lock.release()
        with self.assertRaisesRegex(RuntimeError, 'step failed'):
            with bridge.access(timeout=1):
                raise RuntimeError('step failed')
        self.assertFalse(bridge.busy())
        self.assertEqual(bridge._access_waiters, [])


class ResolveLaunchTests(unittest.TestCase):
    """A closed DaVinci is started by the first operation that needs it instead of failing it."""

    def bridge(self, states, launched, *, started=True, answers=None, wait=5):
        calls = []
        answers = list(answers or [{"ok": True}])

        def runner(args, input, **options):
            calls.append(json.loads(input.decode("utf-8"))["command"])
            reply = answers.pop(0) if len(answers) > 1 else answers[0]
            return types.SimpleNamespace(stdout=json.dumps(reply).encode("utf-8"))

        def launch():
            launched.append(1)
            return started

        bridge = ResolveBridge(runner=runner, probe=lambda: states.pop(0) if len(states) > 1 else states[0], launch=launch, launch_wait=wait)
        return bridge, calls

    def test_closed_resolve_is_started_and_awaited_before_the_operation(self):
        launched = []
        bridge, calls = self.bridge([False, False, True], launched, answers=[{"ok": False, "error": "连不上"}, {"ok": True}])
        with mock.patch("resolve_bridge.LAUNCH_POLL", 0):
            self.assertTrue(bridge.run("status")["ok"])
        self.assertEqual(launched, [1])
        self.assertEqual(calls, ["ping", "ping", "status"])

    def test_no_scripting_call_until_the_started_resolve_shows_its_main_window(self):
        launched = []
        bridge, calls = self.bridge([False, True], launched, answers=[{"ok": False, "error": "连不上"}, {"ok": True}])
        windows = [False, False, True]
        seen = []

        def window_ready():
            seen.append(list(calls))
            return windows.pop(0)

        bridge.window_ready = window_ready
        with mock.patch("resolve_bridge.LAUNCH_POLL", 0):
            self.assertTrue(bridge.run("status")["ok"])
        self.assertEqual(seen, [["ping"]] * 3)
        self.assertEqual(calls, ["ping", "ping", "status"])

    def test_a_main_window_that_never_shows_does_not_stop_the_connection_for_good(self):
        bridge, calls = self.bridge([False, True], [], answers=[{"ok": False, "error": "连不上"}, {"ok": True}])
        bridge.window_ready = lambda: False
        with mock.patch("resolve_bridge.LAUNCH_POLL", 0), mock.patch("resolve_bridge.LAUNCH_QUIET", 0):
            self.assertTrue(bridge.run("status")["ok"])
        self.assertEqual(calls, ["ping", "ping", "status"])

    def test_a_resolve_the_process_list_missed_is_not_started_twice(self):
        launched = []
        bridge, calls = self.bridge([False], launched)
        bridge.run("status")
        self.assertEqual((launched, calls), ([], ["ping", "status"]))

    def test_open_resolve_is_not_started_again(self):
        launched = []
        bridge, calls = self.bridge([True], launched)
        bridge.run("status")
        self.assertEqual((launched, calls), ([], ["status"]))

    def test_missing_executable_asks_for_a_manual_start(self):
        bridge, calls = self.bridge([False], [], started=False, answers=[{"ok": False, "error": "连不上"}])
        with self.assertRaisesRegex(VoiceError, "没有找到 Resolve.exe"):
            bridge.run("status")
        self.assertEqual(calls, ["ping"])

    def test_a_resolve_that_never_answers_ends_with_a_clear_error(self):
        bridge, calls = self.bridge([False, True], [], answers=[{"ok": False, "error": "连不上"}], wait=0.05)
        with mock.patch("resolve_bridge.LAUNCH_POLL", 0.01):
            with self.assertRaises(VoiceError) as caught:
                bridge.run("status")
        self.assertEqual(caught.exception.status, 504)
        self.assertNotIn("status", calls)
        self.assertFalse(bridge.busy())


if __name__ == '__main__':
    unittest.main()
