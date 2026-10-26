"""Timed voice audition and coverage navigation use only isolated fixture media and fake Resolve replies."""
from __future__ import annotations

import io
import json
import re
import unittest
import wave

import test_production_validation_ui as fixture

JOB_ID = fixture.JOB_ID


class ProductionVideoVisualsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ProductionValidationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ProductionValidationBrowserTests.tearDownClass.__func__)
    setUp = fixture.ProductionValidationBrowserTests.setUp
    stop_server = fixture.ProductionValidationBrowserTests.stop_server
    request = fixture.ProductionValidationBrowserTests.request
    make_playable_video = fixture.ProductionValidationBrowserTests.make_playable_video
    edit = fixture.ProductionValidationBrowserTests.edit
    take = fixture.ProductionValidationBrowserTests.take

    def open_page(self, **options):
        # Register before navigation so inline-style CSP regressions are visible even when the UI renders.
        original_new_page = self.browser.new_context
        csp_errors = []
        def new_context(**kwargs):
            context = original_new_page(**kwargs)
            context.on("page", lambda page: page.on("console", lambda message: csp_errors.append(message.text)
                       if "Content Security Policy" in message.text or "violates the following" in message.text else None))
            return context
        self.browser.new_context = new_context
        try:
            page = fixture.ProductionValidationBrowserTests.open_page(self, **options)
        finally:
            self.browser.new_context = original_new_page
        self.addCleanup(lambda: self.assertEqual(csp_errors, [], "CSP console errors"))
        return page

    def voices(self):
        self.payload["job"]["lines"] = [{"number": 1, "spoken": "First idea.", "zh": "第一句。"},
                                         {"number": 2, "spoken": "Second idea.", "zh": "第二句。"}]
        groups = [{"number": 1, "start": 0, "end": 2, "coverage": 1},
                  {"number": 2, "start": 4, "end": 7, "coverage": .59}]
        self.payload["job"]["takes"] = [self.take("TAK-first", groups, duration=10),
                                          self.take("TAK-second", groups, duration=10)]
        self.payload["job"]["selected_take"] = "TAK-first"

    def audio(self, page):
        output = io.BytesIO()
        with wave.open(output, "wb") as sound:
            sound.setnchannels(1)
            sound.setsampwidth(2)
            sound.setframerate(8000)
            sound.writeframes(b"\x00\x00" * 8000 * 10)
        media = output.getvalue()
        def serve(route):
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", route.request.headers.get("range", ""))
            if match:
                start = int(match.group(1))
                end = min(int(match.group(2)) if match.group(2) else len(media)-1, len(media)-1)
                route.fulfill(status=206, content_type="audio/wav", body=media[start:end+1],
                              headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{len(media)}"})
            else:
                route.fulfill(status=200, content_type="audio/wav", body=media, headers={"Accept-Ranges": "bytes"})
        page.route("**/api/production/audio/**", serve)
        page.locator("#take-panel audio").evaluate_all("players => players.forEach(audio => audio.muted = true)")

    def coverage(self, *, verified=True):
        self.voices()
        self.payload["job"]["timelines"] = [{"unique_id": "uid-own", "name": "Lab261011-202-1", "creator": "Lab", "markers": 2,
            "frames": 300, "fps": "30", "take_id": "TAK-first", "built_at": "2026-10-11"}]
        edit = self.edit("needs_material", validated=verified)
        edit.update(take_id="TAK-first", completion_policy="skill_delivery", report={"saved": True, "shots": 3,
            "gaps": [{"start": 60, "end": 90, "need": "切纸结果特写"}]})
        if verified:
            edit["summary"] = {"fps": "30", "voice_frames": 300, "voice_seconds": 10, "shots": 3,
                "gaps": [{"start": 60, "end": 90, "name": "切纸结果特写", "seconds": 2}],
                "picture_gaps": [{"start": 60, "end": 90}, {"start": 180, "end": 210}],
                "uncovered": [{"start": 180, "end": 210, "seconds": 6}], "group_markers": 2, "groups_expected": 2}
        self.payload["job"]["edits"] = [edit]
        return edit

    def test_voice_strip_uses_timing_and_marks_low_coverage(self):
        self.voices()
        self.payload["job"]["takes"][0]["groups"].append({"number": 3, "start": 8, "end": 7, "coverage": 1})
        page = self.open_page(width=390)
        page.locator('.production-steps [data-step="voice"]').click()
        strip = page.locator('[data-take-id="TAK-first"] .voice-segment-bar')
        self.assertEqual(strip.locator("button").count(), 2)
        second = strip.locator('[data-play-group="1"]')
        self.assertIn("warning", second.get_attribute("class"))
        self.assertIn("59%", second.get_attribute("title"))
        self.assertEqual(second.evaluate("node => [node.style.left,node.style.width]"), ["40%", "30%"])
        sizes = second.evaluate("node => {const bar = node.parentElement.getBoundingClientRect(), segment = node.getBoundingClientRect(); return {width: segment.width/bar.width, left: (segment.left-bar.left)/bar.width};}")
        self.assertAlmostEqual(sizes["width"], .3, delta=.01)
        self.assertAlmostEqual(sizes["left"], .4, delta=.01)
        self.assertTrue(page.locator('[data-take-id="TAK-first"] [data-voice-row="2"] button').is_disabled())
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_segment_and_table_row_seek_and_stop_the_previous_version(self):
        self.voices()
        page = self.open_page()
        self.audio(page)
        page.locator('.production-steps [data-step="voice"]').click()
        first = page.locator('[data-take-id="TAK-first"]')
        second = page.locator('[data-take-id="TAK-second"]')
        first.locator('.voice-segment[data-play-group="1"]').click()
        page.wait_for_function("() => { const audio = document.querySelector('[data-take-id=TAK-first] audio'); return !audio.paused && audio.currentTime >= 4 && audio.currentTime < 5; }")
        second.locator("details > summary").click()
        second.locator('[data-voice-row="1"] td').nth(1).click()
        page.wait_for_function("() => { const audio = document.querySelector('[data-take-id=TAK-second] audio'); return !audio.paused && audio.currentTime >= 4 && audio.currentTime < 5; }")
        self.assertTrue(first.locator("audio").evaluate("audio => audio.paused"))
        second.locator("audio").evaluate("audio => {audio.currentTime = 6.8;}")
        page.wait_for_function("() => document.querySelector('[data-take-id=TAK-second] audio').paused")
        self.assertIn("试听结束", second.locator("[data-group-playback]").inner_text())

    def test_script_table_keyboard_plays_selected_voice_group(self):
        self.voices()
        page = self.open_page()
        self.audio(page)
        page.locator('.production-steps [data-step="lines"]').click()
        row = page.locator('#job-script-panel .production-table tbody tr').nth(1)
        row.focus()
        row.press("Enter")
        page.wait_for_function("() => { const audio = document.querySelector('[data-take-id=TAK-first] audio'); return !audio.paused && audio.currentTime >= 4 && audio.currentTime < 5; }")

    def test_coverage_shows_real_ranges_and_opens_relative_gap_frame(self):
        self.coverage()

        def opened(route):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "name": "Lab261011-202-1", "frame": 60}))

        page = self.open_page(width=390, post=opened)
        bar = page.locator(".edit-coverage-bar")
        self.assertEqual(bar.locator(".picture").count(), 3)
        self.assertEqual(bar.locator(".gap").count(), 1)
        self.assertEqual(bar.locator(".uncovered").count(), 1)
        self.assertEqual(bar.locator(".gap").evaluate("node => node.style.width"), "10%")
        self.assertAlmostEqual(bar.locator(".gap").evaluate("node => node.getBoundingClientRect().width/node.parentElement.getBoundingClientRect().width"), .1, delta=.01)
        bar.locator(".gap").click()
        page.wait_for_function("() => document.querySelector('.edit-coverage-bar .gap').disabled === false")
        self.assertEqual(self.calls[0]["body"], {"unique_id": "uid-own", "frame": 60})
        self.assertTrue(self.calls[0]["url"].endswith(f"/jobs/{JOB_ID}/open"))
        bar.locator(".uncovered").click()
        page.wait_for_function("() => document.querySelector('.edit-coverage-bar .uncovered').disabled === false")
        self.assertEqual(self.calls[1]["body"], {"unique_id": "uid-own", "frame": 180})
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_report_only_coverage_does_not_claim_unchecked_shots_or_empty_picture(self):
        self.coverage(verified=False)
        self.payload["job"]["takes"].append(self.take("TAK-new-selection", [], duration=3))
        self.payload["job"]["selected_take"] = "TAK-new-selection"
        page = self.open_page()
        bar = page.locator(".edit-coverage-bar")
        self.assertEqual(bar.locator(".picture,.uncovered").count(), 0)
        self.assertEqual(bar.locator(".unknown").count(), 2)
        self.assertEqual(bar.locator(".gap").evaluate("node => node.style.width"), "10%")
        self.assertIn("0–10.00 秒", page.locator(".edit-coverage-head").inner_text())
        # How to read the strip sits behind the ⓘ; the legend names only what the strip shows.
        self.assertEqual(page.locator(".edit-coverage .context-note").count(), 0)
        self.assertIn("尚未核对", page.locator(".edit-coverage .production-hint-body").text_content())
        self.assertEqual(page.locator(".edit-coverage-legend span").all_inner_texts(), ["缺素材", "覆盖待核对"])
        # The ⓘ sits by the title and opens just below it, not at the page edge.
        page.locator('.production-steps [data-step="edit"]').click()
        hint = page.locator(".edit-coverage-head strong .production-hint")
        hint.locator("summary").click()
        title, body = page.locator(".edit-coverage-head strong").bounding_box(), hint.locator(".production-hint-body").bounding_box()
        self.assertLess(abs(body["y"] - (title["y"] + title["height"])), 24)
        self.assertLess(abs(body["x"] - title["x"]), 4)

    def test_report_without_gaps_or_summary_shows_no_coverage_strip(self):
        edit = self.coverage(verified=False)
        edit["report"]["gaps"] = []
        page = self.open_page()
        page.locator(f'[data-edit-id="{edit["id"]}"]').wait_for(state="attached")
        self.assertEqual(page.locator(".edit-coverage").count(), 0)

    def test_gap_navigation_failure_restores_control_and_escapes_names(self):
        edit = self.coverage(verified=False)
        edit["report"]["gaps"][0]["need"] = '<img src=x onerror="window.__bad=true">'

        def rejected(route):
            route.fulfill(status=409, content_type="application/json", body=json.dumps({"error": "目标时间线已被改动"}, ensure_ascii=False))

        page = self.open_page(post=rejected)
        button = page.locator(".edit-coverage-bar .gap")
        button.click()
        page.wait_for_function("() => document.querySelector('.edit-coverage-bar .gap').disabled === false")
        self.assertFalse(page.evaluate("Boolean(window.__bad)"))
        self.assertEqual(page.locator("#edit-runs img").count(), 0)
        self.assertEqual(self.calls[0]["body"]["frame"], 60)


if __name__ == "__main__":
    unittest.main()
