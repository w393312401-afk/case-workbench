"""Real local mask/composition checks around isolated AI planning and image calls."""
import json
import hashlib
from pathlib import Path
import threading
import time
from types import MethodType
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops, ImageOps

import test_carousel_prompt_planning as planning_fixture
import test_production_carousel_images as image_fixture
from codex_bridge import BridgeError
from workbench_carousel_autopilot import CarouselAutopilotMixin
from workbench_carousel_cutout import CarouselCutoutMixin


class SubjectPlanner(planning_fixture.PromptPlanner):
    def __init__(self):
        super().__init__()
        self.coverage_ok = True
        self.include_regions = True

    def run(self, prompt, *, cwd, images, cancel_event, on_progress, schema, read_only, **kwargs):
        properties = schema['properties']['slides']['items']['properties']
        if 'covered' in properties:
            snapshot = json.loads((Path(cwd) / 'input.json').read_text(encoding='utf-8'))
            self.calls.append({'prompt': prompt, 'cwd': Path(cwd), 'images': images,
                               'snapshot': snapshot, 'schema': schema,
                               'read_only': read_only, **kwargs})
            return json.dumps({'slides': [
                {'number': slide['number'], 'covered': self.coverage_ok,
                 'issues': [] if self.coverage_ok else ['夹具边缘落在保护区外']}
                for slide in snapshot['slides']]}, ensure_ascii=False)
        answer = super().run(prompt, cwd=cwd, images=images, cancel_event=cancel_event,
                             on_progress=on_progress, schema=schema, read_only=read_only, **kwargs)
        plan = json.loads(answer)
        for slide in plan['slides']:
            slide['prompt'] = (f"Slide {slide['number']}: Edit the unprotected kitchen background with soft window light. "
                               "Keep the photographed product, accessories, and contact shadows unchanged. "
                               "Do not redraw the subject. Render the supplied English title only in the top title area.")
            if self.include_regions:
                slide['protected_regions'] = [[[300, 400], [700, 400], [700, 800], [300, 800]]]
        return json.dumps(plan, ensure_ascii=False)


class MaskImageGenerator(image_fixture.ImageGenerator):
    direct_image_edit = True
    review_available = False
    source = 'cockpit_image_edit'

    def run(self, prompt, *, mask=None, **kwargs):
        result = super().run(prompt, **kwargs)
        folder = Path(kwargs['cwd'])
        call = next(entry for entry in self.calls if entry['cwd'] == folder)
        call['mask'] = mask
        with Image.open(kwargs['images'][0]) as image:
            call['source_size'] = image.size
        if mask:
            with Image.open(mask) as image:
                call['mask_size'] = image.size
                call['mask_alpha_extrema'] = image.getchannel('A').getextrema()
            with Image.open(kwargs['images'][0]) as image:
                call['source_size'] = image.size
        return result


class CarouselSubjectLockedImageTests(unittest.TestCase):
    carousel = planning_fixture.CarouselPromptPlanningTests.carousel
    body = planning_fixture.CarouselPromptPlanningTests.body
    current = planning_fixture.CarouselPromptPlanningTests.current
    pick_new_base = planning_fixture.CarouselPromptPlanningTests.pick_new_base
    start = planning_fixture.CarouselPromptPlanningTests.start
    generate = planning_fixture.CarouselPromptPlanningTests.generate
    stop = planning_fixture.CarouselPromptPlanningTests.stop

    def setUp(self):
        planning_fixture.CarouselPromptPlanningTests.setUp(self)
        self.planner = self.bench.image_reviewer = SubjectPlanner()
        self.addCleanup(self.planner.release.set)
        self.generator = self.bench.image_generator = MaskImageGenerator()

    def wait(self):
        # Real Sharp calls verify full-resolution rasters; the AI clients remain fakes.
        deadline = time.monotonic() + 30
        while self.bench.image_running() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.bench.image_running(), 'subject-locked worker did not finish')
        return self.current()

    def test_ai_prompts_and_masks_produce_changed_background_with_exact_protected_pixels(self):
        item = self.generate(workflow='subject_locked', redo=True)
        run = item['generations'][-1]
        self.assertEqual((run['status'], run['added']), ('completed', 3), run)
        self.assertEqual((len(self.planner.calls), len(self.generator.calls)), (2, 3))
        planning, coverage = self.planner.calls
        self.assertTrue(planning['read_only'])
        self.assertTrue(coverage['read_only'])
        self.assertEqual(planning['snapshot']['workflow'], 'subject_locked')
        self.assertIn('protected_regions', planning['schema']['properties']['slides']['items']['properties'])
        self.assertIn('covered', coverage['schema']['properties']['slides']['items']['properties'])
        self.assertEqual((len(planning['images']), len(coverage['images'])), (3, 6))
        root = self.bench._carousel_dir(item['id'])
        for slide in item['slides']:
            with self.subTest(slide=slide['number']):
                output = slide['outputs'][-1]
                call = next(entry for entry in self.generator.calls
                            if entry['snapshot']['slide']['number'] == slide['number'])
                self.assertIsNone(call['mask'])
                with Image.open(call['cwd'] / 'subject-mask.png') as mask:
                    self.assertEqual(mask.size, call['source_size'])
                    self.assertEqual(mask.getchannel('A').getextrema(), (0, 255))
                self.assertIn('BACKGROUND PLATE', call['prompt'])
                self.assertFalse(call['read_only'])
                self.assertEqual(output['workflow'], 'subject_locked')
                self.assertTrue(output['ai_generated'])
                self.assertEqual(output['generation_prompt'], call['prompt'])
                self.assertIn(output['prompt_plan']['prompt'], call['prompt'])
                self.assertEqual((call['cwd'] / 'prompt.txt').read_text(encoding='utf-8'), call['prompt'])
                self.assertNotEqual(output['origin'], 'local_layout')
                self.assertIsNone(output['layout_receipt'])
                self.assertEqual(output['review']['status'], 'deferred')
                self.assertFalse(output['review']['completed'])
                receipt = output['subject_preservation']
                self.assertEqual((receipt['status'], receipt['method']), ('passed', 'masked_source_pixels'))
                self.assertEqual(receipt['final_sha256'], output['sha256'])
                self.assertEqual(receipt['base_sha256'], output['base_sha256'])
                self.assertEqual(receipt['input_signature'], output['input_signature'])
                self.assertEqual(receipt['mask_coverage']['status'], 'passed')
                self.assertEqual(receipt['mismatch_pixels'], 0)
                with Image.open(root / output['file']) as image:
                    final = image.convert('RGBA')
                with Image.open(root / receipt['source_file']) as image:
                    source = image.convert('RGBA')
                with Image.open(root / receipt['mask_file']) as image:
                    protected = image.getchannel('A').point(lambda alpha: 255 if alpha == 255 else 0)
                self.assertEqual(final.size, source.size)
                self.assertEqual(protected.histogram()[255], receipt['protected_pixel_count'])
                self.assertGreater(receipt['protected_pixel_count'], 0)
                differences = ImageChops.difference(final, source).split()
                self.assertTrue(all(ImageChops.multiply(band, protected).getbbox() is None
                                    for band in differences), 'protected source pixels changed')
                editable = ImageOps.invert(protected)
                self.assertTrue(any(ImageChops.multiply(band, editable).getbbox() is not None
                                    for band in differences), 'background must be actually edited')
        self.assertTrue(all(path.read_bytes() == original for path, original in self.originals.items()))

    def test_failed_coverage_stops_before_image_calls_without_screenshot_fallback(self):
        self.planner.coverage_ok = False
        item = self.generate(workflow='subject_locked', redo=True)
        run = item['generations'][-1]
        self.assertEqual(run['status'], 'failed', run)
        self.assertIn('主体保护区尚未覆盖完整', run['error'])
        self.assertEqual(len(self.planner.calls), 2)
        self.assertEqual(self.generator.calls, [])
        self.assertTrue(all(not slide['outputs'] for slide in item['slides']))
        self.assertFalse(list(self.bench._carousel_dir(item['id']).rglob('render*.png')))

    def test_polygon_fallback_adds_missed_pixels_but_still_requires_coverage_approval(self):
        # A failed cutout has no semantic-mask cache. Its original polygons must still be repairable.
        self.bench._refine_subject_masks = lambda *args: {1: {'status': 'polygon'}}
        self.bench._lock_missed_boxes = MethodType(CarouselCutoutMixin._lock_missed_boxes, self.bench)
        original_run = self.planner.run
        observed_alpha = []
        box = {'x0': 790, 'y0': 650, 'x1': 850, 'y1': 710}

        def reviewer(prompt, *, cwd, images, schema, read_only, **kwargs):
            if 'covered' not in schema['properties']['slides']['items']['properties']:
                return original_run(prompt, cwd=cwd, images=images, schema=schema, read_only=read_only, **kwargs)
            self.assertTrue(read_only)
            self.assertEqual(self.generator.calls, [], 'no image generation before an independent coverage approval')
            with Image.open(Path(cwd).parent / '01' / 'subject-mask.png') as mask:
                observed_alpha.append(mask.getchannel('A').getpixel(
                    (round(820 * (mask.width - 1) / 1000), round(680 * (mask.height - 1) / 1000))))
            # Even after the requested pixels are added, a rejecting reviewer must block generation.
            return json.dumps({'slides': [{'number': 1, 'covered': False,
                'issues': ['夹具外轮廓仍待确认'], 'missing_boxes': [box]}]}, ensure_ascii=False)

        with patch.object(self.planner, 'run', side_effect=reviewer):
            item = self.generate(workflow='subject_locked', redo=True, slides=[1])

        run = item['generations'][-1]
        self.assertEqual(observed_alpha, [0, 255], 'the second independent review must see the repaired real raster')
        self.assertEqual(run['status'], 'failed', run)
        self.assertIn('主体保护区尚未覆盖完整', run['error'])
        self.assertEqual(self.generator.calls, [])
        self.assertEqual(item['slides'][0]['outputs'], [])
        root = self.bench._carousel_dir(item['id'])
        self.assertFalse((root / 'subject-masks').exists(), 'polygon repair must not fabricate a cutout cache')
        saved_plan = json.loads((root / run['prompt_plan_file']).read_text(encoding='utf-8'))
        self.assertEqual(len(saved_plan['slides'][0]['protected_regions']), 2, 'save the reviewed repair for a safe resume')

    def test_retry_reuses_ai_plan_but_rebuilds_mask_and_rechecks_coverage(self):
        self.planner.coverage_ok = False
        first = self.generate(workflow='subject_locked', redo=True, slides=[1])
        first_run = first['generations'][-1]
        self.assertEqual(first_run['status'], 'failed', first_run)
        self.assertEqual(self.generator.calls, [])
        root = self.bench._carousel_dir(first['id'])
        old_mask = root / 'generations' / first_run['id'] / '01' / 'subject-mask.png'
        self.assertTrue(old_mask.is_file())
        # A failed run's obsolete raster must never be reused as protection just
        # because its AI-authored plan remains valid for the unchanged inputs.
        obsolete = b'obsolete failed-run mask; rebuild from the saved AI regions'
        old_mask.write_bytes(obsolete)
        self.planner.coverage_ok = True
        second = self.generate(workflow='subject_locked', redo=True, slides=[1])
        second_run = second['generations'][-1]
        self.assertEqual((second_run['status'], second_run['added']), ('completed', 1), second_run)
        planning = [call for call in self.planner.calls
                    if 'protected_regions' in call['schema']['properties']['slides']['items']['properties']]
        coverage = [call for call in self.planner.calls
                    if 'covered' in call['schema']['properties']['slides']['items']['properties']]
        self.assertEqual(len(planning), 1, 'unchanged inputs should not require another AI planning turn')
        self.assertEqual(len(coverage), 2, 'each rebuilt mask needs a fresh visual coverage decision')
        self.assertEqual(coverage[0]['snapshot'], coverage[1]['snapshot'])
        self.assertNotEqual(coverage[0]['cwd'], coverage[1]['cwd'])
        self.assertTrue(all(Path(path).is_relative_to(coverage[1]['cwd']) for path in coverage[1]['images']))
        self.assertEqual(len(self.generator.calls), 1)
        call = self.generator.calls[0]
        new_mask = call['cwd'] / 'subject-mask.png'
        self.assertNotEqual(old_mask, new_mask)
        self.assertEqual(old_mask.read_bytes(), obsolete)
        self.assertEqual(new_mask.parent.parent.name, second_run['id'])
        self.assertIsNone(call['mask'])
        with Image.open(new_mask) as mask:
            self.assertEqual(mask.size, call['source_size'])
            self.assertEqual(mask.getchannel('A').getextrema(), (0, 255))
        output = second['slides'][0]['outputs'][-1]
        self.assertEqual(root / output['subject_preservation']['mask_file'], new_mask)
        self.assertEqual(output['subject_preservation']['mask_coverage']['status'], 'passed')
        self.assertEqual(output['subject_preservation']['mismatch_pixels'], 0)
        self.assertIn(output['prompt_plan']['prompt'], call['prompt'])

    def test_missing_regions_stops_before_coverage_or_image_generation(self):
        self.planner.include_regions = False
        item = self.generate(workflow='subject_locked', redo=True)
        run = item['generations'][-1]
        self.assertEqual(run['status'], 'failed', run)
        self.assertIn('主体保护蒙版', run['error'])
        self.assertEqual(len(self.planner.calls), 1)
        self.assertEqual(self.generator.calls, [])
        self.assertTrue(all(not slide['outputs'] for slide in item['slides']))
        self.assertFalse(list(self.bench._carousel_dir(item['id']).rglob('render*.png')))

    def test_final_visual_failure_preserves_report_and_success_binds_the_complete_group(self):
        item = self.generate(workflow='subject_locked', redo=True)
        run = item['generations'][-1]
        self.assertEqual((run['status'], run['added']), ('completed', 3), run)
        root = self.bench._carousel_dir(item['id'])
        expected = {slide['number']: slide['outputs'][-1] for slide in item['slides']}
        before = {number: (root / output['file']).read_bytes() for number, output in expected.items()}
        # The production Workbench includes this mixin; the standalone image
        # fixture only needs its real output selector for this integration call.
        self.bench._autopilot_generated_output = MethodType(
            CarouselAutopilotMixin._autopilot_generated_output, self.bench)
        failing = {2}
        calls = []

        def final_reviewer(prompt, *, cwd, images, schema, read_only, **kwargs):
            self.assertIn('passed', schema['properties']['slides']['items']['properties'])
            self.assertTrue(read_only)
            bindings = json.loads((Path(cwd) / 'input.json').read_text(encoding='utf-8'))
            calls.append({'folder': Path(cwd), 'images': images, 'bindings': bindings})
            return json.dumps({'slides': [{'number': binding['number'], 'passed': binding['number'] not in failing,
                'issues': ['产品边缘有明显贴回接缝'] if binding['number'] in failing else []}
                for binding in bindings]}, ensure_ascii=False)

        with patch.object(self.planner, 'run', side_effect=final_reviewer):
            with self.assertRaises(BridgeError) as caught:
                self.bench.check_subject_locked_final_visual(item['id'], run['id'], threading.Event())
            self.assertIn('第 2 张', str(caught.exception))
            failed_report = root / 'generations' / run['id'] / 'final-visual' / 'report.json'
            self.assertTrue(failed_report.is_file())
            failure_bytes = failed_report.read_bytes()
            failed_rows = json.loads(failure_bytes)['slides']
            self.assertFalse(next(row for row in failed_rows if row['number'] == 2)['passed'])

            failing.clear()
            result = self.bench.check_subject_locked_final_visual(item['id'], None, threading.Event())

        self.assertTrue(result['passed'])
        report = root / result['report_file']
        self.assertTrue(report.is_file())
        self.assertTrue(report.is_relative_to(root / 'reviews'))
        self.assertNotEqual(report, failed_report)
        self.assertEqual(result['report_sha256'], hashlib.sha256(report.read_bytes()).hexdigest())
        self.assertEqual(failed_report.read_bytes(), failure_bytes)
        self.assertEqual({row['number'] for row in result['outputs']}, {1, 2, 3})
        for binding in result['outputs']:
            output = expected[binding['number']]
            self.assertEqual((binding['output_id'], binding['sha256']), (output['id'], output['sha256']))
            self.assertEqual((root / output['file']).read_bytes(), before[binding['number']])
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(len(call['images']), 6)
            self.assertTrue(all(Path(path).is_file() and Path(path).is_relative_to(call['folder']) for path in call['images']))
        self.assertFalse(any(output.get('approved') for slide in self.current()['slides'] for output in slide['outputs']))


if __name__ == '__main__':
    unittest.main()
