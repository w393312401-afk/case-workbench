"""Actual picker phases are readable during the call and cleaned on every exit."""
from __future__ import annotations

from datetime import datetime
import json
import threading
import unittest
from unittest.mock import patch

from elevenlabs_client import VoiceError
import test_production_carousel_clips as fixture


@unittest.skipUnless(fixture.HAVE_FFMPEG, '需要本机 ffmpeg/ffprobe')
class CarouselClipRuntimeTests(unittest.TestCase):
    setUp = fixture.ClipTests.setUp

    def start_picker(self):
        done, outcome = threading.Event(), {}

        def pick():
            try:
                outcome['result'] = self.bench.ai_pick_clips(self.item['id'], {'materials': self.materials})
            except Exception as exc:
                outcome['error'] = exc
            finally:
                done.set()

        thread = threading.Thread(target=pick, name='clip-picker-test')
        thread.start()
        return done, outcome, thread

    def use_first_clip(self, prompt):
        first = next(iter(next(iter(self.bench._clip_cache.values()))['clips']))
        return json.dumps({'slides': {'1': [{'key': first, 'reason': '标签相符'}]}, 'gaps': []})

    def test_waiting_materials_are_public_and_resolve_is_released_before_ai_choice(self):
        waiting, phases = threading.Event(), []
        identifier = self.item['id']
        runner = self.bench.resolve.runner

        def record(app, run_id, **values):
            phases.append(values.get('phase'))
            if values.get('phase') == '等待达芬奇':
                waiting.set()

        def read_materials(*args, **kwargs):
            self.assertTrue(self.bench.resolve.busy())
            self.assertEqual(self.bench._clip_ai_live[identifier]['stage'], 'clip_materials')
            return runner(*args, **kwargs)

        def choose(prompt):
            # AI does not retain Resolve while considering the cached labels.
            self.assertTrue(self.bench.resolve.lock.acquire(blocking=False))
            self.bench.resolve.lock.release()
            self.assertEqual(self.bench._clip_ai_live[identifier]['stage'], 'clip_choice')
            return self.use_first_clip(prompt)

        self.bench.resolve.runner = read_materials
        self.bench.fake_ai.answer = choose
        self.bench.resolve.lock.acquire()
        with patch('workbench_carousel_clips.record_activity', side_effect=record):
            done, outcome, thread = self.start_picker()
            try:
                self.assertTrue(waiting.wait(3))
                self.assertFalse(done.is_set())
                public = self.bench.carousel_detail(identifier)['carousel']['clip_picking_live']
                self.assertEqual(public['stage'], 'waiting_resolve')
                self.assertIn('其他任务完成后', public['progress'])
                self.assertEqual(self.bench.runner.calls, [])
                self.assertEqual(self.bench.fake_ai.calls, [])
            finally:
                self.bench.resolve.lock.release()
                thread.join(3)
        self.assertTrue(done.is_set())
        self.assertNotIn('error', outcome)
        self.assertGreater(outcome['result']['count'], 0)
        self.assertIn('等待达芬奇', phases)
        self.assertIn('读取素材清单', phases)
        self.assertIn('AI 按标签挑片段', phases)
        self.assertNotIn(identifier, self.bench._clip_ai_busy)
        self.assertNotIn(identifier, self.bench._clip_ai_live)

    def test_warm_clip_cache_goes_to_ai_while_another_task_holds_resolve(self):
        self.bench.carousel_clips(self.item['id'], self.materials)
        calls = len(self.bench.runner.calls)
        self.bench.fake_ai.answer = self.use_first_clip
        self.bench.resolve.lock.acquire()
        done, outcome, thread = self.start_picker()
        try:
            self.assertTrue(done.wait(3), '已有清单缓存仍在等待达芬奇')
            self.assertTrue(self.bench.resolve.busy())
            self.assertEqual(len(self.bench.runner.calls), calls)
            self.assertNotIn('error', outcome)
            self.assertGreater(outcome['result']['count'], 0)
        finally:
            self.bench.resolve.lock.release()
            thread.join(3)

    def test_material_queue_timeout_cleans_runtime_without_releasing_another_task_lock(self):
        identifier = self.item['id']
        self.bench.resolve.lock.acquire()
        try:
            with patch('workbench_carousel_clips.CLIP_WAIT_TIMEOUT', .1):
                with self.assertRaises(VoiceError) as caught:
                    self.bench.ai_pick_clips(identifier, {'materials': self.materials})
            self.assertEqual(caught.exception.status, 504)
            self.assertTrue(self.bench.resolve.busy())
            self.assertEqual(self.bench.runner.calls, [])
            self.assertEqual(self.bench.fake_ai.calls, [])
            self.assertNotIn(identifier, self.bench._clip_ai_busy)
            self.assertNotIn(identifier, self.bench._clip_ai_live)
        finally:
            self.bench.resolve.lock.release()

    def test_plain_clip_browsing_keeps_existing_busy_refusal(self):
        self.bench.resolve.lock.acquire()
        try:
            with self.assertRaises(VoiceError) as caught:
                self.bench.carousel_clips(self.item['id'], self.materials)
            self.assertEqual(caught.exception.status, 409)
            self.assertTrue(self.bench.resolve.busy())
        finally:
            self.bench.resolve.lock.release()

    def test_live_materials_choice_progress_public_copies_and_cleanup_after_success_and_failure(self):
        identifier = self.item['id']
        listing = self.bench.carousel_clips
        phases = []

        def observed_listing(*args, **kwargs):
            live = self.bench._clip_ai_live[identifier]
            phases.append(live['stage'])
            self.assertEqual(live['status'], 'running')
            self.assertIsNotNone(datetime.fromisoformat(live['started_at']).tzinfo)
            public = self.bench.carousel_detail(identifier)['carousel']['clip_picking_live']
            self.assertEqual(public['stage'], 'clip_materials')
            public['progress'] = 'must not mutate the live task'
            self.assertNotEqual(live['progress'], public['progress'])
            catalog = next(item for item in self.bench.production_catalog()['carousels'] if item['id'] == identifier)
            self.assertEqual(catalog['clip_picking_live']['id'], live['id'])
            return listing(*args, **kwargs)

        def answer(prompt):
            call = self.bench.fake_ai.calls[-1]
            live = self.bench._clip_ai_live[identifier]
            phases.append(live['stage'])
            call['on_progress']('AI 正在筛选第 1 张的标签')
            self.assertEqual(live['progress'], 'AI 正在筛选第 1 张的标签')
            self.assertGreaterEqual(live['updated_at'], live['started_at'])
            public = self.bench.carousel_detail(identifier)['carousel']['clip_picking_live']
            self.assertEqual(public['stage'], 'clip_choice')
            key = next(iter(self.bench._clip_cache.values()))['clips']
            first = next(iter(key))
            return json.dumps({'slides': {'1': [{'key': first, 'reason': '标签相符'}]}, 'gaps': []})

        self.bench.fake_ai.answer = answer
        with patch.object(self.bench, 'carousel_clips', side_effect=observed_listing):
            result = self.bench.ai_pick_clips(identifier, {'materials': self.materials})
        self.assertGreater(result['count'], 0)
        self.assertEqual(phases, ['clip_materials', 'clip_choice'])
        self.assertNotIn(identifier, self.bench._clip_ai_busy)
        self.assertNotIn(identifier, self.bench._clip_ai_live)
        self.assertIsNone(self.bench.carousel_detail(identifier)['carousel']['clip_picking_live'])
        saved = self.bench.carousel_clip_picks(identifier)

        def failed_answer(prompt):
            self.assertEqual(self.bench._clip_ai_live[identifier]['stage'], 'clip_choice')
            raise RuntimeError('isolated picker failure')

        self.bench.fake_ai.answer = failed_answer
        with self.assertRaises(VoiceError):
            self.bench.ai_pick_clips(identifier, {'materials': self.materials})
        self.assertNotIn(identifier, self.bench._clip_ai_busy)
        self.assertNotIn(identifier, self.bench._clip_ai_live)
        catalog = next(item for item in self.bench.production_catalog()['carousels'] if item['id'] == identifier)
        self.assertIsNone(catalog['clip_picking_live'])
        self.assertEqual(self.bench.carousel_clip_picks(identifier), saved)


if __name__ == '__main__':
    unittest.main()
