"""Real carousel orchestration over disposable image workers, approvals and ZIP delivery."""
from collections import Counter
import copy
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
import zipfile

from codex_bridge import BridgeError
from elevenlabs_client import VoiceError
import test_production_carousel_images as fixture
from workbench_carousel_autopilot import CarouselAutopilotMixin, review_allows_autopilot


class AutopilotImages(fixture.ImageGenerator):
    """Independently inject content errors or transport errors for one slide."""

    def __init__(self):
        super().__init__()
        self.review_behavior = {}
        self.review_counts = Counter()

    def run(self, prompt, **options):
        answer = super().run(prompt, **options)
        if not options.get('read_only'):
            return answer
        snapshot = json.loads((Path(options['cwd']) / 'input.json').read_text(encoding='utf-8'))
        number = snapshot['slide']['number']
        self.review_counts[number] += 1
        behavior = self.review_behavior.get(number)
        if behavior == 'error' or (behavior == 'recover_error' and self.review_counts[number] == 1):
            raise BridgeError('隔离测试：视觉核对连接失败')
        if behavior == 'wrong_words':
            parsed = json.loads(answer)
            parsed['observed_title_lines'] = ['Visible words do not match the locked copy.']
            return json.dumps(parsed)
        return answer


class AutopilotBench(CarouselAutopilotMixin, fixture.ImageBench):
    pass


class CarouselAutopilotTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.CarouselImageTests()
        with patch.object(fixture, 'ImageBench', AutopilotBench), patch.object(fixture, 'ImageGenerator', AutopilotImages):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bench, self.generator = self.fixture.bench, self.fixture.generator
        self.identifier = self.fixture.item['id']
        self.addCleanup(self.stop)

    def stop(self):
        self.bench.stop_carousel_autopilots()
        self.generator.release.set()
        deadline = time.monotonic() + 10
        while (self.bench.autopilot_running() or self.bench.image_running()) and time.monotonic() < deadline:
            time.sleep(.01)

    def current(self):
        return self.fixture.current()

    def start(self, body=None):
        return self.bench.start_carousel_autopilot(self.identifier, {"workflow": "review_first", **(body or self.fixture.body())})

    def wait(self):
        deadline = time.monotonic() + 15
        while (self.bench.autopilot_running() or self.bench.image_running()) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.bench.autopilot_running(), 'autopilot worker did not finish')
        self.assertFalse(self.bench.image_running(), 'image worker did not finish')
        return self.current()

    def generation_counts(self):
        return Counter(call['snapshot']['slide']['number'] for call in self.generator.calls if not call['read_only'])

    def test_full_run_generates_reviews_authorizes_and_packages_real_outputs(self):
        started = self.start()
        self.assertRegex(started['run']['id'], r'^AUT-[0-9a-f]{12}$')
        item = self.wait()
        run = item['autopilot_runs'][-1]
        self.assertEqual(run['status'], 'completed', run)
        self.assertTrue(run['delivery_id'])
        self.assertEqual(self.generation_counts(), Counter({1: 1, 2: 1, 3: 1}))
        for slide in item['slides']:
            selected = next(output for output in slide['outputs'] if output['id'] == slide['output_id'])
            self.assertTrue(selected['approved'])
            self.assertEqual(selected['approval_actor'], 'assistant_on_user_authorization')
            self.assertTrue(selected['review']['completed'])
            self.assertGreaterEqual(self.generator.review_counts[slide['number']], 1)
        self.assertEqual(len(item['deliveries']), 1)
        with zipfile.ZipFile(self.bench.carousel_delivery_file(self.identifier, run['delivery_id'])) as archive:
            self.assertEqual(archive.namelist(), ['01.png', '02.png', '03.png'])
        self.assertEqual(self.bench.creation.public()['scripts'], self.fixture.script_before)
        self.assertEqual([(slide['title_lines'], slide['body']) for slide in item['slides']], self.fixture.words)

    def test_existing_approved_group_is_reused_without_generation(self):
        self.fixture.generate()
        before = self.fixture.approve_all()
        output_ids = [slide['output_id'] for slide in before['slides']]
        self.generator.calls.clear()
        self.start()
        item = self.wait()
        self.assertEqual(item['autopilot_runs'][-1]['status'], 'completed', item['autopilot_runs'][-1])
        self.assertEqual(self.generation_counts(), Counter())
        self.assertEqual([slide['output_id'] for slide in item['slides']], output_ids)
        self.assertEqual([len(slide['outputs']) for slide in item['slides']], [1, 1, 1])
        self.assertEqual(len(item['deliveries']), 1)

    def test_existing_bases_complete_while_editing_owns_resolve(self):
        self.bench._reserve('EDT-parallel')
        self.bench.resolve.lock.acquire()
        try:
            self.bench.runner.calls.clear()
            self.start()
            item = self.wait()
            self.assertEqual(item['autopilot_runs'][-1]['status'], 'completed', item['autopilot_runs'][-1])
            self.assertTrue(item['autopilot_runs'][-1]['delivery_id'])
            self.assertTrue(self.bench.resolve.busy())
            self.assertFalse(self.bench._edit_cancel['EDT-parallel'].is_set())
            self.assertEqual(self.bench.runner.calls, [])
        finally:
            self.bench.resolve.lock.release()
            self.bench._forget('EDT-parallel')

    def test_own_queued_still_search_still_prevents_conflicting_autopilot(self):
        search_id = 'STS-0123456789ab'
        self.bench._reserve(search_id)
        try:
            self.bench.production.mutate('carousel_search_start', self.fixture.body(carousel_id=self.identifier),
                                        {'id': search_id, 'status': 'running', 'slides': [1],
                                         'started_at': self.current()['updated_at']})
            with self.assertRaisesRegex(VoiceError, '排队或寻找底图'):
                self.start()
            self.assertFalse(self.bench.autopilot_running())
            self.assertEqual(self.generator.calls, [])
        finally:
            self.bench._forget(search_id)

    def test_policy_rejects_incomplete_stale_and_partial_review_receipts(self):
        self.fixture.generate(slides=[1])
        item = self.current()
        slide = item['slides'][0]
        original = slide['outputs'][-1]
        self.assertTrue(review_allows_autopilot(item, slide, original), original['review'])
        changes = {
            'incomplete': lambda output: output['review'].update(completed=False),
            'wrong_file': lambda output: output['review'].update(file_sha256='wrong-file'),
            'wrong_input': lambda output: output['review'].update(input_signature='old-input'),
            'partial_scope': lambda output: output['review'].update(scope=['text']),
            'execution_failure': lambda output: output['review'].update(execution_status='failed'),
            'unexplained_product_uncertainty': lambda output: next(check for check in output['review']['checks'] if check['id'] == 'product').update(status='uncertain'),
            'text_failure': lambda output: next(check for check in output['review']['checks'] if check['id'] == 'text').update(status='fail'),
        }
        for label, change in changes.items():
            with self.subTest(label=label):
                output = copy.deepcopy(original)
                change(output)
                self.assertFalse(review_allows_autopilot(item, slide, output), output['review'])

    def test_wrong_visible_words_are_never_approved_and_redo_is_bounded(self):
        self.generator.review_behavior[2] = 'wrong_words'
        self.start()
        item = self.wait()
        run = item['autopilot_runs'][-1]
        self.assertEqual(run['status'], 'failed', run)
        self.assertTrue(run['error'])
        self.assertFalse(run.get('delivery_id'))
        self.assertEqual(len(item.get('deliveries', [])), 0)
        second = item['slides'][1]
        self.assertEqual(self.generation_counts()[2], 2)
        self.assertEqual(len(second['outputs']), 2)
        self.assertFalse(any(output.get('approved') for output in second['outputs']))
        self.assertTrue(all(count <= 1 for number, count in self.generation_counts().items() if number != 2))

    def test_review_transport_errors_retry_review_without_regenerating(self):
        self.generator.review_behavior[2] = 'error'
        self.start()
        item = self.wait()
        self.assertEqual(item['autopilot_runs'][-1]['status'], 'failed', item['autopilot_runs'][-1])
        self.assertEqual(self.generation_counts()[2], 1)
        self.assertEqual(self.generator.review_counts[2], 2)
        self.assertEqual(len(item['slides'][1]['outputs']), 1)
        self.assertFalse(any(output.get('approved') for output in item['slides'][1]['outputs']))
        self.assertEqual(len(item.get('deliveries', [])), 0)

    def test_transient_review_failure_recovers_the_same_image(self):
        self.generator.review_behavior[2] = 'recover_error'
        self.start()
        item = self.wait()
        self.assertEqual(item['autopilot_runs'][-1]['status'], 'completed', item['autopilot_runs'][-1])
        self.assertEqual(self.generation_counts()[2], 1)
        self.assertEqual(self.generator.review_counts[2], 2)
        self.assertEqual(len(item['slides'][1]['outputs']), 1)

    def test_running_replay_and_duplicate_start_do_not_duplicate_work_and_cancel_is_idempotent(self):
        self.generator.block_stage = 'generation'
        body = self.fixture.body()
        started = self.start(body)
        run_id = started['run']['id']
        self.assertTrue(self.generator.entered.wait(5))
        replay = self.start(body)
        self.assertEqual(replay['run']['id'], run_id)
        try:
            duplicate = self.start()
        except VoiceError as exc:
            self.assertEqual(exc.status, 409)
        else:
            self.assertEqual(duplicate['run']['id'], run_id)
        self.assertEqual(len(self.current()['autopilot_runs']), 1)
        self.bench.cancel_carousel_autopilot(self.identifier, run_id)
        self.bench.cancel_carousel_autopilot(self.identifier, run_id)
        self.generator.release.set()
        item = self.wait()
        self.assertEqual(item['autopilot_runs'][-1]['status'], 'cancelled', item['autopilot_runs'][-1])
        self.assertEqual(len(item.get('deliveries', [])), 0)
        self.assertFalse(any(output.get('approved') for slide in item['slides'] for output in slide.get('outputs', [])))

    def test_shutdown_stops_work_and_preserves_the_image_saved_before_review(self):
        self.generator.block_stage = 'review'
        self.start()
        self.assertTrue(self.generator.entered.wait(5))
        before = self.current()
        saved_ids = {output['id'] for slide in before['slides'] for output in slide.get('outputs', [])}
        self.assertTrue(saved_ids)
        self.bench.stop_carousel_autopilots()
        self.generator.release.set()
        item = self.wait()
        self.assertIn(item['autopilot_runs'][-1]['status'], {'cancelled', 'interrupted'})
        after_ids = {output['id'] for slide in item['slides'] for output in slide.get('outputs', [])}
        self.assertTrue(saved_ids.issubset(after_ids))
        self.assertEqual(len(item.get('deliveries', [])), 0)


if __name__ == '__main__':
    unittest.main()
