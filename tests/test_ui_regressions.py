"""Browser regressions for the case workbench, using an isolated temporary library.

Run with: python -m unittest discover -s tests -p test_ui_regressions.py -v
The HTTP server binds an ephemeral port and never reads the user's case library.
"""

from __future__ import annotations

import copy
import re
import subprocess
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

import test_server as fixture


# The library's filters and sort live in a drawer; selection checkboxes appear in selection mode.
def open_library_filters(page):
    if not page.locator("#library-filter-drawer[open]").count():
        page.locator("#library-filter-open").click()
        page.locator("#library-filter-drawer[open]").wait_for()


def close_library_filters(page):
    if page.locator("#library-filter-drawer[open]").count():
        page.locator("#library-drawer-done").click()
        page.locator("#library-filter-drawer[open]").wait_for(state="detached")


def start_library_selection(page):
    if page.locator("#library-select-toggle").get_attribute("aria-pressed") != "true":
        page.locator("#library-select-toggle").click()
        page.locator("#selection-toolbar:not([hidden])").wait_for()



class WorkbenchBrowserTests(unittest.TestCase):
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        # Every browser page requests visible card thumbnails. Give those
        # requests a valid clip and wait for handlers before temp cleanup.
        self.make_playable_video()
        self.server.daemon_threads = False

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def open_page(self, hash_route="#library"):
        context = self.browser.new_context(viewport={"width": 1080, "height": 620})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/{hash_route}")
        page.locator("#connection-text").get_by_text("已连接本地案例库").wait_for()
        return page

    def add_cases(self, count=8):
        """Add enough distinct metrics and cards to make sorting and scrolling visible."""
        sold = [2, 7, 19, 4, 11, 3, 8, 1, 24, 6, 15, 5]
        added = []
        for index in range(count):
            current = fixture.snapshot(self.video)
            current["title"] = f"SHARPAL 浏览器回归案例 {index + 1:02d}"
            current["metrics"]["video_id"] = str(7000000000000000000 + index)
            current["metrics"]["views_display"] = f"{(index + 1) * 11}K"
            current["metrics"]["ctor_display"] = f"{index + 1}.2%"
            current["metrics"]["items_sold"] = sold[index]
            case = self.store.add({
                "title": current["title"],
                "product": "SHARPAL 202H",
                "original": {"text": "Fixture source", "source": "isolated UI test"},
                "feedback": {"kind": "editorial", "text": "Browser fixture"},
                "current": current,
            })
            added.append(case)
        return added

    def append_external_revision(self):
        original = self.store.read_case(self.identifier)
        current = copy.deepcopy(original["current"])
        current["conclusion"] += " 外部修订已到达。"
        self.store.append(self.identifier, {
            "kind": "fact", "text": "External browser regression update", "current": current,
        })

    def make_playable_video(self):
        # The shared HTTP fixture writes arbitrary bytes; replace its temp file
        # with a tiny, valid clip so these checks can observe actual playback.
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
            "color=c=red:s=64x64:r=10", "-t", "30", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-y", str(self.video),
        ], check=True, capture_output=True)

    def play_video(self, page):
        player = page.locator("#case-player")
        player.evaluate("video => { video.muted = true; return video.play(); }")
        player.evaluate("video => { video.currentTime = 10; }")
        page.wait_for_function("() => document.querySelector('#case-player')?.currentTime > 10.1")
        self.assertFalse(player.evaluate("video => video.paused"))

    def reveal_player(self, page):
        # Short viewports now start collapsed; playback scenarios choose a mode.
        page.get_by_role("button", name="固定", exact=True).click()
        page.locator("#case-player").wait_for()

    def set_comparison_view(self, page, enabled):
        page.locator('.detail-tabs [data-tab="script"]').click()
        # Saved translations now open by default, so select the intended view.
        toggle = page.locator("#toggle-transcript-zh")
        if (toggle.get_attribute("aria-expanded") == "true") != enabled:
            toggle.click()
        self.assertEqual(toggle.get_attribute("aria-expanded"), str(enabled).lower())

    def test_search_updates_results_without_replacing_toolbar(self):
        self.add_cases(3)
        page = self.open_page()
        page.locator(".case-card").first.wait_for()
        immediate = page.evaluate("""() => {
          const search = document.querySelector('#case-search');
          window.__searchNode = search;
          window.__firstCard = document.querySelector('.case-card');
          search.value = '浏览器回归案例 03';
          search.dispatchEvent(new Event('input', {bubbles: true}));
          return {
            cards: document.querySelectorAll('.case-card').length,
            firstCardKept: document.querySelector('.case-card') === window.__firstCard,
          };
        }""")
        # No card video is recreated synchronously for every keystroke.
        self.assertEqual(immediate["cards"], 4)
        self.assertTrue(immediate["firstCardKept"])
        page.locator("#result-count").get_by_text("1 条案例").wait_for()
        self.assertEqual(page.locator(".case-card").count(), 1)
        self.assertTrue(page.evaluate("document.querySelector('#case-search') === window.__searchNode"))

    def test_external_update_preserves_detail_player_and_expanded_content(self):
        self.make_playable_video()
        page = self.open_page(f"#case/{self.identifier}")
        self.reveal_player(page)
        self.play_video(page)
        page.evaluate("window.__playerNode = document.querySelector('#case-player')")
        self.set_comparison_view(page, True)
        self.assertTrue(page.locator("#transcript-comparison").is_visible())
        self.append_external_revision()
        page.get_by_text(re.compile("有更新")).first.wait_for(timeout=9000)
        self.assertTrue(page.evaluate("document.querySelector('#case-player') === window.__playerNode"))
        self.assertFalse(page.locator("#case-player").evaluate("video => video.paused"))
        self.assertGreater(page.locator("#case-player").evaluate("video => video.currentTime"), 10)
        self.assertEqual(page.locator("#toggle-transcript-zh").get_attribute("aria-expanded"), "true")
        self.assertTrue(page.locator("#transcript-comparison").is_visible())

    def test_external_update_preserves_unsaved_editor(self):
        page = self.open_page(f"#case/{self.identifier}")
        page.locator(".detail-heading").wait_for()
        page.evaluate("openEditor(state.detail, {advanced: true})")
        field = page.locator('[name="conclusion"]')
        field.fill("This unsaved edit must survive external changes")
        page.evaluate("window.__beforeSignature = state.signature")
        self.append_external_revision()
        page.wait_for_function("() => state.signature !== window.__beforeSignature", timeout=9000)
        self.assertTrue(page.locator("#edit-dialog").evaluate("element => element.open"))
        self.assertEqual(field.input_value(), "This unsaved edit must survive external changes")

    def test_saved_chinese_pairs_show_in_detail_and_comparison_and_expire_after_source_edit(self):
        original = self.store.read_case(self.identifier)
        current = copy.deepcopy(original["current"])
        current["content_origin"] = "self_produced"
        current["translation_zh"] = {
            "source_text": current["transcript"]["text"],
            "text": "整套东西都在一个盒子里。\n原话要保留。",
            "channel": "transcript",
        }
        self.store.append(self.identifier, {
            "kind": "editorial", "text": "补中文口语对照", "current": current,
        })
        screen = copy.deepcopy(current)
        screen["title"] = "屏显文字案例"
        screen["content_origin"] = "overseas_creator"
        screen["metrics"]["video_id"] = "7000000000000000701"
        del screen["transcript"]
        screen.pop("source_event_ids", None)
        screen["translation_zh"] = {
            "source_text": "Sharp in seconds.",
            "text": "几秒就磨锋利了。",
            "channel": "on_screen",
        }
        screen_case = self.store.add({
            "title": screen["title"], "product": "SHARPAL 202H",
            "original": {"text": "Screen text source", "source": "isolated UI test"},
            "feedback": {"kind": "editorial", "text": "收录屏显文字"},
            "current": screen,
        })

        page = self.open_page(f"#case/{self.identifier}")
        self.set_comparison_view(page, True)
        self.assertEqual(page.locator(".transcript-pair").count(), 2)
        page.locator(".transcript-zh").first.get_by_text("整套东西都在一个盒子里。").wait_for()
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#case/{screen_case['case_id']}")
        page.locator('.detail-tabs [data-tab="script"]').click()
        page.locator(".detail-info h2").get_by_text("屏显文字").wait_for()
        page.locator(".detail-info:not([hidden])").get_by_role("heading", name="屏显文字", exact=True).wait_for()
        self.assertEqual(page.locator(".detail-info:not([hidden])").get_by_role("heading", name="完整文案", exact=True).count(), 0)
        self.assertEqual(page.locator("#transcript-original").inner_text(), "Sharp in seconds.")
        self.set_comparison_view(page, True)
        page.locator(".transcript-zh").get_by_text("几秒就磨锋利了。").wait_for()

        page.goto(f"http://127.0.0.1:{self.server.server_port}/#library")
        page.evaluate("ids => { state.selection = new Set(ids); return openComparison(); }",
                      [self.identifier, screen_case["case_id"]])
        comparison = page.locator("#case-comparison")
        comparison.get_by_text("中文对照", exact=True).wait_for()
        comparison.get_by_text("屏显文字", exact=False).first.wait_for()
        comparison.get_by_text("几秒就磨锋利了。").wait_for()
        origin = comparison.get_by_role("row").filter(
            has=page.get_by_role("rowheader", name="内容来源", exact=True))
        headings = comparison.locator("thead th a").all_text_contents()
        self.assertEqual(dict(zip(headings, origin.locator("td").all_text_contents())), {
            current["title"]: "自制内容参考", screen["title"]: "达人内容参考",
        })
        comparison.locator("#close-comparison").click()

        page.goto(f"http://127.0.0.1:{self.server.server_port}/#case/{self.identifier}")
        page.locator(".detail-heading").wait_for()
        page.evaluate("openEditor(state.detail, {advanced: true})")
        page.locator('[name="transcript_text"]').fill("A changed original transcript.")
        self.assertEqual(page.locator('[name="translation_text"]').input_value(), "")
        page.locator('[name="translation_text"]').fill("一条更新后的原文。")
        page.locator('[name="note"]').fill("原文和中文译文同步更新")
        page.locator("#save-button").click()
        page.wait_for_function("() => !document.querySelector('#edit-dialog').open")
        saved = self.store.read_case(self.identifier)["current"]["translation_zh"]
        self.assertEqual(saved["source_text"], "A changed original transcript.")
        self.assertEqual(saved["text"], "一条更新后的原文。")
        # Saving closes the dialog before its asynchronous detail refresh finishes.
        # Reopen only once the newly saved revision is displayed.
        page.wait_for_function("""() => document.querySelector('#transcript-original')
            ?.textContent === 'A changed original transcript.'""")

        page.locator(".detail-heading").wait_for()
        page.evaluate("openEditor(state.detail, {advanced: true})")
        page.locator('[name="transcript_text"]').fill("Another changed original transcript.")
        self.assertEqual(page.locator('[name="translation_text"]').input_value(), "")
        page.locator('[name="note"]').fill("更新原文，旧译文作废")
        page.locator("#save-button").click()
        page.wait_for_function("() => !document.querySelector('#edit-dialog').open")
        self.assertNotIn("translation_zh", self.store.read_case(self.identifier)["current"])

    def test_transcript_highlights_and_scrolls_with_video_time(self):
        lines = [f"[{i * 3}.00s - {i * 3 + 2.5:.2f}s] Line number {i + 1}." for i in range(10)]
        original = self.store.read_case(self.identifier)
        current = copy.deepcopy(original["current"])
        current["transcript"]["text"] = "\n\n".join(lines)
        current["translation_zh"] = {
            "source_text": current["transcript"]["text"],
            "text": "\n".join(f"第 {i + 1} 句。" for i in range(10)),
            "channel": "transcript",
        }
        self.store.append(self.identifier, {
            "kind": "editorial", "text": "带时间戳文案", "current": current,
        })
        page = self.open_page(f"#case/{self.identifier}")
        self.reveal_player(page)
        self.set_comparison_view(page, False)
        active = page.locator("#transcript-original .cue.is-active")
        self.assertEqual(page.locator("#transcript-original .cue").count(), 10)
        active.get_by_text("Line number 1.").wait_for()

        page.locator("#case-player").evaluate("video => { video.currentTime = 20; }")
        active.get_by_text("Line number 7.").wait_for()
        self.assertEqual(active.count(), 1)
        page.wait_for_function(
            """() => { const r = document.querySelector('#transcript-original .cue.is-active')
                .getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }""")

        page.locator("#transcript-original .cue").nth(1).click()
        active.get_by_text("Line number 2.").wait_for()
        self.assertAlmostEqual(
            page.locator("#case-player").evaluate("video => video.currentTime"), 3.01, delta=0.3)

        page.get_by_role("button", name="中文对照").click()
        page.locator("#transcript-comparison .cue.is-active .transcript-zh").get_by_text("第 2 句。").wait_for()
        page.locator("#case-player").evaluate("video => { video.currentTime = 12; }")
        page.locator("#transcript-comparison .cue.is-active .transcript-zh").get_by_text("第 5 句。").wait_for()

        follow = page.locator("#toggle-transcript-follow")
        self.assertEqual(follow.get_attribute("aria-pressed"), "true")
        follow.click()
        self.assertEqual(follow.get_attribute("aria-pressed"), "false")

    def test_detail_tab_keeps_player_and_history_route_after_reload(self):
        self.make_playable_video()
        page = self.open_page(f"#case/{self.identifier}")
        self.reveal_player(page)
        self.play_video(page)
        page.evaluate("window.__playerNode = document.querySelector('#case-player')")
        page.locator('.detail-tabs [data-tab="history"]').click()
        self.assertTrue(page.evaluate("document.querySelector('#case-player') === window.__playerNode"))
        self.assertFalse(page.locator("#case-player").evaluate("video => video.paused"))
        self.assertGreater(page.locator("#case-player").evaluate("video => video.currentTime"), 10)
        self.assertTrue(page.url.endswith(f"#/202H/case/{self.identifier}/history"))
        page.reload()
        page.locator('.detail-tabs [data-tab="history"]').wait_for()
        self.assertIn("active", page.locator('.detail-tabs [data-tab="history"]').get_attribute("class"))
        page.locator('.detail-tabs [data-tab="current"]').click()
        self.assertIsNotNone(page.locator("#case-player").element_handle())

    def test_url_filters_and_return_restore_library_scroll(self):
        self.add_cases(9)
        page = self.open_page()
        page.locator('[data-library-preset="positive"]').click()
        page.locator("#case-search").fill("SHARPAL")
        page.locator("#result-count").get_by_text("10 条案例").wait_for()
        open_library_filters(page)
        sold_option = next(label for label in page.locator("#sort-filter option").all_text_contents() if "售出" in label)
        page.locator("#sort-filter").select_option(label=sold_option)
        close_library_filters(page)
        self.assertIn("category=positive", page.url)
        self.assertIn("q=", page.url)
        page.reload()
        self.assertEqual(page.locator("#case-search").input_value(), "SHARPAL")
        self.assertIn("售出", page.locator("#sort-filter").locator("option:checked").inner_text())
        self.assertEqual(page.locator('[data-library-preset="positive"]').get_attribute("aria-pressed"), "true")
        page.locator(".case-card").nth(6).scroll_into_view_if_needed()
        scroll_before = page.evaluate("window.scrollY")
        self.assertGreater(scroll_before, 100)
        page.locator(".case-card").nth(6).click()
        page.locator("#case-player").wait_for(state="attached")
        page.go_back()
        page.locator("#case-search").wait_for()
        self.assertEqual(page.locator("#case-search").input_value(), "SHARPAL")
        page.wait_for_function("expected => Math.abs(window.scrollY - expected) < 120", arg=scroll_before)
        self.assertLess(abs(page.evaluate("window.scrollY") - scroll_before), 120)

    def test_sold_header_toggles_numeric_sort_order(self):
        added = self.add_cases(5)
        # Current sorting reads the sales table, never the historical screenshot
        # metrics kept in each case. Include zero and missing independently.
        sold = dict(zip([self.identifier] + [case["case_id"] for case in added], [0, 13, 2, 100, 7, None]))
        before = {identifier: self.store.read_case(identifier) for identifier in sold}

        def sales(case):
            value = sold[case["case_id"]]
            metrics = {"video_id": case["current"]["metrics"]["video_id"]}
            if value is not None:
                metrics["items_sold"] = value
            return {"status": "matched" if value is not None else "not_found",
                    "source_label": "销售数据表", "metrics": metrics,
                    "evidence": {"source": "isolated sales fixture", "status": "source_checked"}, "video": None}

        for method, side_effect in (("resolve_case", sales), ("resolve_cases", lambda cases: {case["case_id"]: sales(case) for case in cases})):
            fake = patch.object(self.server.app.sales, method, side_effect=side_effect)
            fake.start()
            self.addCleanup(fake.stop)
        page = self.open_page()
        page.get_by_role("button", name="列表").click()
        header = page.get_by_role("columnheader", name=re.compile("售出件数"))
        header.click()
        def values():
            column = header.evaluate("element => Array.from(element.parentElement.children).indexOf(element) + 1")
            return [int(found.group()) if (found := re.search(r"\d+", cell)) else None
                    for cell in page.locator(f".case-table tbody tr td:nth-child({column})").all_text_contents()]
        first = values()
        self.assertEqual(first, [100, 13, 7, 2, 0, None])
        header.click()
        second = values()
        self.assertEqual(second, [0, 2, 7, 13, 100, None])
        self.assertEqual({identifier: self.store.read_case(identifier) for identifier in sold}, before)

    def test_explicit_positive_category_appears_in_matching_sidebar_group(self):
        current = fixture.snapshot(self.video)
        current["title"] = "仅按案例分类归入正向组"
        current["assessments"] = []
        current["metrics"]["video_id"] = "7000000000000000888"
        self.store.add({
            "title": current["title"], "product": "SHARPAL 202H",
            "original": {"text": "Category fixture", "source": "isolated UI test"},
            "feedback": {"kind": "editorial", "text": "Category routing fixture"},
            "current": current,
        })
        page = self.open_page()
        page.locator('[data-library-preset="positive"]').click()
        page.get_by_role("link", name="查看案例：仅按案例分类归入正向组").wait_for()

    def test_editing_category_updates_manual_native_assignment_and_keeps_history(self):
        current = fixture.snapshot(self.video)
        current["title"] = "人工归属从本土参考转正向"
        current["category"] = "待验证与整理"
        current["metrics"]["video_id"] = "7000000000000000777"
        case = self.store.add({
            "title": current["title"], "product": "SHARPAL 202H",
            "original": {"text": "Manual navigation fixture", "source": "isolated UI test"},
            "feedback": {"kind": "editorial", "text": "Initial classification"},
            "current": current,
        })
        case_id = case["case_id"]
        before = self.store.read_case(case_id)
        status, _, moved = self.request("POST", "/api/navigation/move", {
            "case_id": case_id, "target": "native",
            "revision": fixture.SERVER.revision(before), "previous": None,
        })
        self.assertEqual(status, 200, moved)

        page = self.open_page(f"#case/{case_id}")
        page.locator(".detail-heading").wait_for()
        page.evaluate("openEditor(state.detail, {advanced: true})")
        page.locator('[name="category"]').select_option(label="转化正向参考")
        page.locator('[name="note"]').fill("分类转正向，并同步人工栏目归属")
        page.locator("#save-button").click()
        page.wait_for_function("id => state.navigation[id] === 'positive'", arg=case_id)
        self.assertFalse(page.locator("#edit-dialog").evaluate("dialog => dialog.open"))

        _, _, state = self.request("GET", "/api/state")
        self.assertEqual(state["navigation"][case_id], "positive")
        after = self.store.read_case(case_id)
        self.assertEqual(after["current"]["category"], "转化正向参考")
        self.assertEqual(after["current"]["assessments"], before["current"]["assessments"])
        self.assertEqual(after["original"], before["original"])
        self.assertEqual(after["events"][:-1], before["events"])
        self.assertEqual(len(after["events"]), len(before["events"]) + 1)
        page.locator('[data-page="library"]').click()
        page.locator('[data-library-preset="positive"]').click()
        page.get_by_role("link", name="查看案例：人工归属从本土参考转正向").wait_for()

    def test_content_origin_editor_saves_and_clears_without_reclassifying_case(self):
        before = self.store.read_case(self.identifier)
        status, _, moved = self.request("POST", "/api/navigation/move", {
            "case_id": self.identifier, "target": "native",
            "revision": fixture.SERVER.revision(before), "previous": None,
        })
        self.assertEqual(status, 200, moved)
        page = self.open_page(f"#case/{self.identifier}")
        badges = page.locator(".case-dock-badges")
        # Manual sidebar placement is independent of the video's maker.
        badges.get_by_text("内容来源待确认", exact=True).wait_for()
        self.assertEqual(badges.get_by_text("达人内容参考", exact=True).count(), 0)

        previous_value = ""
        for value, label in [
            ("self_produced", "自制内容参考"),
            ("overseas_creator", "达人内容参考"),
            ("", "内容来源待确认"),
        ]:
            with self.subTest(content_origin=value):
                before = self.store.read_case(self.identifier)
                page.locator(".detail-heading").wait_for()
                page.evaluate("openEditor(state.detail, {advanced: true})")
                field = page.locator('[name="content_origin"]')
                self.assertEqual(field.evaluate("element => element.tagName"), "SELECT")
                self.assertEqual(field.input_value(), previous_value)
                field.select_option(value=value)
                page.locator('[name="note"]').fill(f"本轮只调整内容来源：{label}")
                page.locator("#save-button").click()
                page.wait_for_function("() => !document.querySelector('#edit-dialog').open")
                badges.get_by_text(label, exact=True).wait_for()
                after = self.store.read_case(self.identifier)
                if value:
                    self.assertEqual(after["current"]["content_origin"], value)
                else:
                    self.assertNotIn("content_origin", after["current"])
                # First save may create the managed video copy; content,
                # performance classification and prior records must survive.
                preserved = lambda record: {
                    key: item for key, item in record["current"].items()
                    if key not in {"content_origin", "assets"}
                }
                self.assertEqual(preserved(after), preserved(before))
                self.assertEqual(after["original"], before["original"])
                self.assertEqual(after["events"][:-1], before["events"])
                self.assertEqual(len(after["events"]), len(before["events"]) + 1)
                self.assertEqual(
                    fixture.Path(after["current"]["assets"]["video"]).read_bytes(),
                    self.video.read_bytes(),
                )
                _, _, server_state = self.request("GET", "/api/state")
                self.assertEqual(server_state["navigation"][self.identifier], "native")
                previous_value = value

    def test_keyboard_filters_do_not_rewrite_case_or_legacy_assignment(self):
        before = self.store.read_case(self.identifier)
        status, _, _ = self.request("POST", "/api/navigation/move", {
            "case_id": self.identifier, "target": "column:native",
            "revision": fixture.SERVER.revision(before), "previous": None,
        })
        self.assertEqual(status, 200)
        page = self.open_page()
        self.assertEqual(page.locator("[data-move-case]").count(), 0)
        open_library_filters(page)
        picker = page.locator("#archive-category-filter")
        picker.focus()
        picker.press("ArrowDown")
        picker.press("Enter")
        page.locator(f'.case-card[href="#case/{self.identifier}"]').wait_for()
        self.assertEqual(picker.input_value(), "positive")
        self.assertIn("category=positive", page.url)
        _, _, server_state = self.request("GET", "/api/state")
        self.assertEqual(server_state["navigation"][self.identifier], "column:native")
        self.assertEqual(self.store.read_case(self.identifier), before)

    def test_keyboard_global_search_return_and_case_navigation(self):
        self.add_cases(2)
        page = self.open_page()
        page.keyboard.press("/")
        page.wait_for_function("() => document.activeElement?.id === 'workbench-search-input'")
        self.assertTrue(page.locator("#workbench-search").evaluate("dialog => dialog.open"))
        page.keyboard.press("Escape")
        self.assertFalse(page.locator("#workbench-search").evaluate("dialog => dialog.open"))
        page.locator("#case-search").focus()
        self.assertTrue(page.locator("#case-search").evaluate("node => node === document.activeElement"))
        page.locator("#case-search").fill("SHARPAL")
        page.locator("#result-count").get_by_text("3 条案例").wait_for()
        page.keyboard.press("Escape")
        self.assertFalse(page.locator("#case-search").evaluate("node => node === document.activeElement"))
        links = page.locator(".case-card")
        first, second = ["#/202H/case/" + links.nth(index).get_attribute("href").split("/")[-1]
                         for index in (0, 1)]
        links.first.click()
        page.wait_for_url(f"**/{first}")
        page.keyboard.press("j")
        page.wait_for_url(f"**/{second}")
        page.keyboard.press("k")
        page.wait_for_url(f"**/{first}")
        page.keyboard.press("Escape")
        page.wait_for_url("**/#/202H/library?*")
        self.assertEqual(page.locator("#case-search").input_value(), "SHARPAL")
        page.locator(".case-card").first.click()
        page.wait_for_url(f"**/{first}")
        page.keyboard.press("/")
        page.wait_for_function("() => document.activeElement?.id === 'workbench-search-input'")
        self.assertTrue(page.url.endswith(first))
        page.keyboard.press("Escape")
        self.assertFalse(page.locator("#workbench-search").evaluate("dialog => dialog.open"))
        self.assertTrue(page.url.endswith(first))

    def test_card_thumbnail_loads_when_it_enters_viewport(self):
        self.add_cases(12)
        page = self.open_page()
        images = page.locator("[data-card-thumbnail]")
        self.assertEqual(images.count(), 13)
        page.wait_for_function("() => document.querySelector('[data-card-thumbnail]')?.hasAttribute('src')")
        page.wait_for_function("() => document.querySelector('[data-card-thumbnail]')?.naturalWidth > 0")
        last = images.last
        self.assertIsNone(last.get_attribute("src"))
        self.assertIsNotNone(last.get_attribute("data-src"))
        last.scroll_into_view_if_needed()
        page.wait_for_function("() => document.querySelectorAll('[data-card-thumbnail]')[12]?.hasAttribute('src')")
        page.wait_for_function("() => document.querySelectorAll('[data-card-thumbnail]')[12]?.naturalWidth > 0")
        self.assertIsNone(last.get_attribute("data-src"))

    def test_category_conflict_is_visible_and_legacy_value_is_preserved(self):
        page = self.open_page(f"#case/{self.identifier}")
        page.locator(".detail-heading").wait_for()
        page.evaluate("openEditor(state.detail, {advanced: true})")
        category = page.locator('[name="category"]')
        self.assertEqual(category.evaluate("element => element.tagName"), "SELECT")
        category.select_option(label="高播放低转化")
        page.locator("#edit-dialog").get_by_text(re.compile("冲突|不一致|矛盾")).first.wait_for()
        page.get_by_role("button", name="取消").click()
        page.get_by_role("button", name="放弃修改").click()

        current = fixture.snapshot(self.video)
        current["title"] = "历史自定义类别"
        current["category"] = "旧版自定义类别"
        current["assessments"] = []
        current["metrics"]["video_id"] = "7000000000000000999"
        old = self.store.add({
            "title": current["title"], "product": "SHARPAL 202H",
            "original": {"text": "Legacy source", "source": "isolated UI test"},
            "feedback": {"kind": "editorial", "text": "Legacy browser fixture"},
            "current": current,
        })
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#case/{old['case_id']}")
        page.get_by_text("旧版自定义类别").first.wait_for()
        page.locator(".detail-heading").wait_for()
        page.evaluate("openEditor(state.detail, {advanced: true})")
        self.assertEqual(page.locator('[name="category"]').input_value(), "旧版自定义类别")
        self.assertEqual(self.store.read_case(old["case_id"])["current"]["category"], "旧版自定义类别")

    def test_card_and_list_browse_without_moving_assignments(self):
        before = self.store.read_case(self.identifier)
        page = self.open_page()
        card = page.locator(f'.selectable-card[data-selection-card="{self.identifier}"]')
        card.wait_for()
        self.assertNotEqual(card.get_attribute("draggable"), "true")
        self.assertTrue(page.locator("#library-navigation").is_hidden())
        self.assertEqual(page.locator("[data-move-case]").count(), 0)
        page.get_by_role("button", name="列表", exact=True).click()
        row = page.locator(f'.case-table tr[data-selection-card="{self.identifier}"]')
        row.wait_for()
        self.assertNotEqual(row.get_attribute("draggable"), "true")
        _, _, server_state = self.request("GET", "/api/state")
        self.assertEqual(server_state["navigation"], {})
        self.assertEqual(self.store.read_case(self.identifier), before)


if __name__ == "__main__":
    unittest.main()
