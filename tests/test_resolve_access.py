"""Background edits and carousel steps share one cancellable Resolve queue."""
import threading
import unittest

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


if __name__ == '__main__':
    unittest.main()
