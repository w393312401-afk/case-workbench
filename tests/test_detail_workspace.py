"""Detail workspace regressions using the disposable workbench library.

These checks exercise viewport geometry, navigation and display-mode behavior.
Run separately with: python -m unittest discover -s tests -p test_detail_workspace.py -v
"""

from __future__ import annotations

import copy
import base64
import subprocess
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

import test_server as fixture


class DetailWorkspaceTests(unittest.TestCase):
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

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

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        self.server.daemon_threads = False
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
            "color=c=blue:s=360x640:r=10", "-t", "30", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-y", str(self.video),
        ], check=True, capture_output=True)
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["purchase_reason"] = "完整工具组合便于携带和收纳。"
        current["transcript"]["text"] = "\n".join(
            f"[{index}.00s - {index + 0.8:.2f}s] Sentence {index + 1}: "
            "The entire sharpening setup stays together in this carrying case."
            for index in range(25)
        )
        current["limitations"] = ["统计窗口未明确，不能据此推断文案与成交的因果关系。"]
        self.store.append(self.identifier, {
            "kind": "editorial", "text": "Disposable detail workspace fixture", "current": current,
        })

    def open_page(self, *, width=1600, height=900, tab=""):
        context = self.browser.new_context(viewport={"width": width, "height": height})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#case/{self.identifier}{tab}")
        page.locator(".case-dock").wait_for()
        return page

    def settle_layout(self, page):
        page.evaluate("""() => new Promise(resolve => {
          let previous = scrollY, stable = 0;
          function check() {
            stable = Math.abs(scrollY - previous) < 0.1 ? stable + 1 : 0;
            previous = scrollY;
            if (stable >= 5) resolve(); else requestAnimationFrame(check);
          }
          requestAnimationFrame(check);
        })""")

    def assert_fully_visible(self, page, selector, *, below_dock=False):
        geometry = page.locator(selector).evaluate("""(element) => {
          const r = element.getBoundingClientRect();
          const dock = document.querySelector('.case-dock').getBoundingClientRect();
          return {top:r.top, bottom:r.bottom, left:r.left, right:r.right,
                  width:r.width, height:r.height, dockBottom:dock.bottom,
                  viewportWidth:innerWidth, viewportHeight:innerHeight};
        }""")
        self.assertGreater(geometry["width"], 0)
        self.assertGreater(geometry["height"], 0)
        self.assertGreaterEqual(geometry["left"], -1)
        self.assertLessEqual(geometry["right"], geometry["viewportWidth"] + 1)
        self.assertGreaterEqual(geometry["top"], -1)
        self.assertLessEqual(geometry["bottom"], geometry["viewportHeight"] + 1)
        if below_dock:
            self.assertGreaterEqual(geometry["top"], geometry["dockBottom"] - 1)
        return geometry

    def section(self, page, heading):
        return page.locator(".detail-info:not([hidden]) section.section-card").filter(
            has=page.get_by_role("heading", name=heading, exact=True))

    def test_portrait_video_stays_visible_while_reading(self):
        page = self.open_page(tab="/script")
        page.locator("#case-player").wait_for()
        page.wait_for_function("() => document.querySelector('#case-player').videoHeight > 0")
        self.section(page, "完整文案").evaluate("element => scrollTo(0, Math.max(300, scrollY + element.getBoundingClientRect().top - 120))")
        page.wait_for_function("() => scrollY > 100")
        self.settle_layout(page)
        video = self.assert_fully_visible(page, "#case-player", below_dock=True)
        self.assertAlmostEqual(video["width"] / video["height"], 9 / 16, delta=0.03)
        self.section(page, "问题与适用边界").scroll_into_view_if_needed()
        self.settle_layout(page)
        self.assert_fully_visible(page, "#case-player", below_dock=True)

    def test_overview_tab_opens_current_content_from_a_history_deep_link(self):
        page = self.open_page(tab="/history")
        page.locator(".history-item").first.wait_for()
        page.get_by_role("tab", name="概要", exact=True).click()
        page.wait_for_url(f"**/#/202H/case/{self.identifier}")
        page.locator('#detail-body').wait_for()
        section = page.locator('.detail-info section.section-card').filter(
            has=page.get_by_role("heading", name="主要购买理由", exact=True)
        )
        section.get_by_text("完整工具组合便于携带和收纳。", exact=True).wait_for()
        self.assertIn("active", page.get_by_role("tab", name="概要", exact=True).get_attribute("class"))

    def test_compact_player_modes_pause_and_restore_without_horizontal_overflow(self):
        page = self.open_page(width=390, height=844)
        self.assertTrue(page.locator("#video-panel-body").evaluate("element => element.hidden"))
        page.get_by_role("button", name="悬浮", exact=True).click()
        page.locator("#case-player").wait_for()
        page.wait_for_function("() => document.querySelector('#case-player').videoHeight > 0")
        self.settle_layout(page)
        self.assert_fully_visible(page, "#case-player")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
        player = page.locator("#case-player")
        player.evaluate("video => { video.muted = true; return video.play(); }")
        page.wait_for_function("() => !document.querySelector('#case-player').paused")
        page.get_by_role("button", name="收起", exact=True).click()
        self.assertTrue(player.evaluate("video => video.paused"))
        self.assertTrue(page.locator("#video-panel-body").evaluate("element => element.hidden"))
        page.reload()
        page.locator(".case-dock").wait_for()
        self.assertTrue(page.locator("#video-panel-body").evaluate("element => element.hidden"))
        page.get_by_role("button", name="固定", exact=True).click()
        page.locator("#case-player").wait_for()
        page.reload()
        page.locator("#case-player").wait_for()
        self.assertEqual(page.get_by_role("button", name="固定", exact=True).get_attribute("aria-pressed"), "true")

    def test_empty_collaboration_history_does_not_occupy_case_space(self):
        page = self.open_page()
        page.wait_for_function("""() => {
          const history = document.querySelector('.ai-case-history');
          return history && history.querySelector('.ai-history-count')?.textContent.includes('0');
        }""")
        self.assertFalse(page.locator(".ai-case-history").is_visible())
        page.get_by_role("button", name="一起审稿", exact=True).wait_for()

    def test_empty_sales_keeps_table_link_and_regular_insights(self):
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["metrics"] = {"video_id": "7664986913497009439", "creator_handle": "@example"}
        current["highlights"] = ["没有本土表达条目时，保留这条可借鉴判断。"]
        self.store.append(self.identifier, {
            "kind": "editorial", "text": "Empty performance fixture", "current": current,
        })
        # Global product discovery can create an empty sales cache while opening
        # the shell. This scenario specifically verifies an unavailable source.
        fake = patch.object(self.server.app.sales, "resolve_cases", side_effect=lambda cases: {
            case["case_id"]: {"status": "unavailable", "source_label": "销售数据表",
                              "metrics": {"video_id": case["current"]["metrics"].get("video_id", "")},
                              "evidence": {"source": "销售数据表", "status": "unverified"}, "video": None}
            for case in cases
        })
        fake.start()
        self.addCleanup(fake.stop)
        before = self.store.read_case(self.identifier)
        page = self.open_page(tab="/data")
        performance = page.locator("#detail-section-performance")
        performance.get_by_text("销售数据表暂不可用", exact=True).wait_for()
        performance.get_by_role("link", name="查看销售数据表").wait_for()
        self.assertEqual(page.locator("#sales-import").count(), 0)
        self.assertEqual(performance.locator(".facts-grid, .evidence-block").count(), 0)
        page.get_by_role("tab", name="概要", exact=True).click()
        summary = page.locator('.detail-overview-metrics [data-summary="表现数据"]')
        summary.get_by_text("销售数据表暂不可用", exact=True).wait_for()
        self.assertEqual(summary.locator(".case-summary-metrics, small").count(), 0)
        summary.click()  # the overview's performance card opens the data tab at the sales section
        page.wait_for_function("""() => {
          const rect = document.querySelector('#detail-section-performance').getBoundingClientRect();
          const dock = document.querySelector('.case-dock').getBoundingClientRect();
          return rect.top >= dock.bottom - 1 && rect.top < innerHeight - 60;
        }""")
        page.get_by_role("tab", name="文案与拆解", exact=True).click()
        page.locator(".detail-info").get_by_role("heading", name="可借鉴的表达", exact=True).wait_for()
        page.locator(".detail-info").get_by_text(current["highlights"][0], exact=True).wait_for()
        self.assertEqual(page.locator(".detail-info").get_by_role(
            "heading", name="问题与适用边界", exact=True).count(), 1)
        self.assertEqual(self.store.read_case(self.identifier), before)

    def test_sales_zero_is_shown_and_historical_screenshot_is_not_fallback(self):
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["metrics"] = {"video_id": "7664986913497009439", "items_sold": 0}
        self.store.append(self.identifier, {
            "kind": "editorial", "text": "Zero is recorded data", "current": current,
        })
        self.server.app.performance.database.parent.mkdir(parents=True, exist_ok=True)
        self.server.app.performance.database.touch()
        fake = patch.object(self.server.app.performance, "video_brief", return_value={
            current["metrics"]["video_id"]: {"items": 0, "views": 0, "clicks": 0, "orders": 0,
                "first_data": "2026-10-01", "last_data": "2026-10-07"}})
        fake_rows = fake.start()
        self.addCleanup(fake.stop)
        page = self.open_page(tab="/data")
        performance = page.locator("#detail-section-performance")
        self.assertEqual(performance.locator(".facts-grid").count(), 1)
        performance.locator(".metric-value").get_by_text("0", exact=True).first.wait_for()
        self.assertEqual(performance.locator(".evidence-block").count(), 0)
        page.get_by_role("tab", name="概要", exact=True).click()
        summary = page.locator('.detail-overview-metrics [data-summary="表现数据"]')
        summary.locator(".case-summary-metrics").get_by_text("0", exact=True).first.wait_for()
        page.get_by_role("tab", name="数据", exact=True).click()

        screenshot = self.root / "performance.png"
        screenshot.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII="))
        current["metrics"] = {"video_id": "7664986913497009439"}
        current["assets"]["screenshot"] = str(screenshot)
        self.store.append(self.identifier, {
            "kind": "editorial", "text": "Screenshot-only performance fixture", "current": current,
        })
        fake_rows.return_value = {}
        page.reload()
        page.locator(".case-dock").wait_for()
        performance.get_by_text("销售数据表未匹配到此视频", exact=True).wait_for()
        self.assertEqual(performance.locator(".facts-grid, .evidence-block").count(), 0)
        self.assertEqual(performance.get_by_alt_text("此案例的表现数据截图").count(), 0)
        page.get_by_role("tab", name="概要", exact=True).click()
        summary.get_by_text("销售数据表未匹配到此视频", exact=True).wait_for()
        self.assertEqual(summary.locator(".case-summary-metrics").count(), 0)

    def test_content_origin_and_native_reference_badges_are_independent(self):
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["content_origin"] = "self_produced"
        current["native_reference"] = {
            "language": "en", "market": "未核实", "duration_sec": 30,
            "scene": "桌面演示", "speaker": "演示者", "audience": "需要磨刀的用户",
            "entries": [{
                "id": "origin-fixture-01", "channel": "speech", "start_sec": 0, "end_sec": 3,
                "function": "说明用途", "original": "Keep the whole setup together.",
                "meaning_zh": "把整套工具收在一起。", "context": "展示收纳盒时说明。",
                "analysis": "说明具体用途。", "adaptation": "Keep this kit in one place.",
                "boundary": "仅供表达结构参考。",
            }],
        }
        self.store.append(self.identifier, {
            "kind": "editorial", "text": "Self-produced content can contain native references", "current": current,
        })
        before = self.store.read_case(self.identifier)
        page = self.open_page()
        badges = page.locator(".case-dock-badges")
        badges.get_by_text("自制内容参考", exact=True).wait_for()
        badges.get_by_text("含本土表达", exact=True).wait_for()
        self.assertEqual(badges.get_by_text("达人内容参考", exact=True).count(), 0)
        self.assertEqual(self.store.read_case(self.identifier), before)
        page.set_viewport_size({"width": 390, "height": 844})
        self.settle_layout(page)
        for label in ["自制内容参考", "含本土表达"]:
            badge = badges.get_by_text(label, exact=True)
            self.assertTrue(badge.is_visible())
            bounds = badge.bounding_box()
            self.assertGreaterEqual(bounds["x"], -1)
            self.assertLessEqual(bounds["x"] + bounds["width"], 391)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
        page.set_viewport_size({"width": 1600, "height": 900})

        del current["content_origin"]
        self.store.append(self.identifier, {
            "kind": "editorial", "text": "Native references do not establish content origin", "current": current,
        })
        before = self.store.read_case(self.identifier)
        page.reload()
        page.locator(".detail-origin > summary").click()
        page.locator(".detail-origin").get_by_text("内容来源待确认", exact=True).wait_for()
        self.assertEqual(badges.get_by_text("内容来源待确认", exact=True).count(), 0)
        badges.get_by_text("含本土表达", exact=True).wait_for()
        self.assertEqual(badges.get_by_text("达人内容参考", exact=True).count(), 0)
        self.assertEqual(self.store.read_case(self.identifier), before)

        current["content_origin"] = "overseas_creator"
        del current["native_reference"]
        self.store.append(self.identifier, {
            "kind": "editorial", "text": "Creator content need not contain expression analysis", "current": current,
        })
        before = self.store.read_case(self.identifier)
        page.reload()
        badges.get_by_text("达人内容参考", exact=True).wait_for()
        self.assertEqual(badges.get_by_text("含本土表达", exact=True).count(), 0)
        self.assertEqual(badges.get_by_text("自制内容参考", exact=True).count(), 0)
        page.get_by_role("tab", name="文案与拆解", exact=True).click()
        page.locator(".detail-info").get_by_role("heading", name="可借鉴的表达", exact=True).wait_for()
        self.assertEqual(self.store.read_case(self.identifier), before)

    def test_native_reference_is_not_shown_in_detail(self):
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        duplicate = "演示时先说具体用途，再说明操作。"
        unique = "这条总览判断并未出现在任何本土表达条目中。"
        current["highlights"] = [duplicate, unique]
        current["native_reference"] = {
            "language": "en", "market": "未核实", "duration_sec": 30,
            "scene": "桌面演示", "speaker": "演示者", "audience": "需要磨刀的用户",
            "entries": [{
                "id": "native-fixture-01", "channel": "speech", "start_sec": 0, "end_sec": 3,
                "function": "说明用途", "original": "Keep the whole setup together.",
                "meaning_zh": "把整套工具收在一起。", "context": "展示收纳盒时说明。",
                "analysis": duplicate, "adaptation": "Keep this kit in one place.",
                "boundary": "仅供表达结构参考。",
            }],
        }
        self.store.append(self.identifier, {
            "kind": "editorial", "text": "Native unique highlights fixture", "current": current,
        })
        before = self.store.read_case(self.identifier)
        page = self.open_page(tab="/script")
        # 详情页不再显示本土表达块，文案区直接是时间轴文案；数据保持不变。
        page.locator("#detail-section-transcript").wait_for()
        self.assertEqual(page.locator(".native-entry").count(), 0)
        self.assertEqual(page.locator("#detail-section-native").count(), 0)
        page.locator(".detail-info").get_by_text(unique, exact=True).wait_for()
        self.assertEqual(self.store.read_case(self.identifier), before)

        expected = ["完整文案", "可借鉴的表达", "问题与适用边界", "原始拆解"]
        headings = page.locator(".detail-info:not([hidden]) section.section-card h2").all_text_contents()
        self.assertEqual([heading.strip() for heading in headings], expected)

    def test_four_tabs_keep_player_translation_and_expanded_history_nodes(self):
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["translation_zh"] = {"source_text": current["transcript"]["text"],
                                     "text": "\n".join(f"第 {i + 1} 句。" for i in range(25)), "channel": "transcript"}
        self.store.append(self.identifier, {"kind": "editorial", "text": "Tab continuity fixture", "current": current})
        before = self.store.read_case(self.identifier)
        page = self.open_page()
        self.assertEqual(page.locator('.detail-tabs [role="tab"]').evaluate_all(
            "tabs => tabs.map(tab => tab.dataset.tab)"), ["script", "current", "data", "history"])
        self.assertEqual(page.locator('.detail-tabs [aria-selected="true"]').inner_text(), "概要")
        player = page.locator("#case-player")
        player.evaluate("video => { window.__tabPlayer = video; video.muted = true; video.currentTime = 3; return video.play(); }")
        page.get_by_role("tab", name="文案与拆解", exact=True).click()
        page.locator(".transcript-zh").first.wait_for()
        page.evaluate("window.__comparison = document.querySelector('#transcript-comparison')")
        page.get_by_role("tab", name="数据", exact=True).click()
        page.locator("#detail-section-performance").wait_for()
        self.assertTrue(page.locator("#detail-section-performance").is_visible())
        self.assertFalse(page.locator("#detail-section-transcript").is_visible())
        page.locator('#detail-tab-history').click()
        page.locator(".history-item > summary").first.click()
        expanded = page.locator(".history-item details").first
        expanded.locator("summary").click()
        page.evaluate("window.__expandedHistory = document.querySelector('.history-item details')")
        page.get_by_role("tab", name="概要", exact=True).click()
        page.get_by_role("tab", name="文案与拆解", exact=True).click()
        self.assertTrue(page.evaluate("document.querySelector('#transcript-comparison') === window.__comparison"))
        self.assertEqual(page.locator("#toggle-transcript-zh").get_attribute("aria-expanded"), "true")
        page.locator('#detail-tab-history').click()
        self.assertTrue(page.evaluate("document.querySelector('.history-item details') === window.__expandedHistory"))
        self.assertTrue(expanded.evaluate("element => element.open"))
        self.assertTrue(page.evaluate("document.querySelector('#case-player') === window.__tabPlayer"))
        self.assertFalse(player.evaluate("video => video.paused"))
        self.assertGreater(player.evaluate("video => video.currentTime"), 3)
        self.assertEqual(self.store.read_case(self.identifier), before)

    def test_legacy_analysis_link_opens_script_with_original_document_and_history(self):
        source = self.root / "original-analysis.md"
        source.write_text("# 原拆解标题\n\n## 2. 钩子 (Hook) 拆解\n\n第一段原始判断。\n\n## 6. 视觉节拍详解\n\n| 镜头 | 作用 |\n| --- | --- |\n| 开场 | 结果先行 |\n", encoding="utf-8")
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["source_case"] = {"library_id": "VSL-T3-001", "file_path": str(source),
                                   "tier": "T3", "transcript_status": "verified", "case_type": "video_analysis"}
        self.store.append(self.identifier, {"kind": "editorial", "text": "Original analysis fixture", "current": current})
        before = source.read_bytes()
        page = self.open_page(tab="/analysis")
        page.wait_for_url(f"**/#/202H/case/{self.identifier}/script")
        self.assertEqual(page.locator('#detail-tab-script').get_attribute("aria-selected"), "true")
        page.locator("#detail-source-analysis .markdown-body").get_by_text("第一段原始判断。", exact=True).wait_for()
        # Chapters fold; 6 starts closed, opens from its chip, and stays open across tabs and reloads.
        beats = page.locator('details.source-chapter[data-chapter-key="6"]')
        self.assertFalse(beats.evaluate("element => element.open"))
        self.assertEqual(page.locator(".source-raw").count(), 0)
        page.locator(".source-chapter-chip", has_text="6 视觉节拍详解").click()
        beats.get_by_text("结果先行", exact=True).wait_for()
        page.get_by_role("tab", name="概要", exact=True).click()
        page.get_by_role("tab", name="文案与拆解", exact=True).click()
        self.assertTrue(beats.evaluate("element => element.open"))
        page.reload()
        page.locator("#detail-source-analysis .markdown-body").get_by_text("第一段原始判断。", exact=True).wait_for()
        self.assertTrue(beats.evaluate("element => element.open"))
        self.assertEqual(source.read_bytes(), before)

    def test_collaboration_completion_and_inline_sections_stay_with_the_case(self):
        page = self.open_page()
        actions = page.locator("#detail-ai-panel .ai-case-actions")
        actions.wait_for()
        self.assertEqual(page.locator(".ai-case-actions").count(), 1)
        rail = page.locator(".detail-rail")
        self.assertEqual(rail.locator("#detail-ai-panel .ai-case-actions").count(), 1)
        self.assertEqual(rail.locator(".video-panel").count(), 1)
        action_bounds = actions.bounding_box()
        main_bounds = page.locator(".detail-main").bounding_box()
        self.assertLessEqual(action_bounds["x"] + action_bounds["width"], main_bounds["x"])
        self.assertEqual(page.locator(".detail-completion .is-ready").count(), 0)
        self.assertGreaterEqual(page.locator(".detail-completion .is-missing").count(), 2)
        page.locator(".detail-completion > summary").click()
        page.locator('[data-completion-section="direction"]').click()
        self.assertEqual(page.locator('#detail-tab-current').get_attribute("aria-selected"), "true")
        editor = page.locator('[data-inline-section="direction"] .case-inline-editor')
        editor.wait_for()
        self.assertEqual(editor.get_attribute("data-edit-section"), "direction")
        editor.locator('[data-inline-cancel]').first.click()
        page.locator('#detail-tab-current').focus()
        page.keyboard.press("ArrowLeft")
        self.assertEqual(page.locator('#detail-tab-script').get_attribute("aria-selected"), "true")
        self.assertTrue(page.url.endswith("/script"))
        page.keyboard.press("End")
        self.assertEqual(page.locator('#detail-tab-history').get_attribute("aria-selected"), "true")
        self.assertTrue(page.url.endswith("/history"))

    def test_sales_refresh_updates_overview_and_data_without_losing_edit_controls(self):
        page = self.open_page()
        page.evaluate("""() => refreshDetailSalesData({case_id:state.detail.case_id, sales_data:{
          status:'matched', metrics:{views_display:'250',items_sold:0,ctor_display:'0%'},
          evidence:{as_of:'2026-10-08',window:'测试窗口'}, video:{}}})""")
        self.assertIn("0", page.locator(".detail-overview-metrics").inner_text())
        self.assertIn("250", page.locator(".detail-overview-metrics").inner_text())
        page.get_by_role("tab", name="数据", exact=True).click()
        self.assertIn("250", page.locator("#detail-section-performance").inner_text())
        self.assertEqual(page.locator('[data-inline-section="metrics"] [data-inline-edit="metrics"]').count(), 1)
        self.assertEqual(page.locator('[data-inline-section="release"] #release-edit').count(), 1)

    def test_unprocessed_case_retains_overview_edit_host_and_history(self):
        page = self.open_page()
        page.evaluate("() => {state.detail = {...state.detail,current:null};renderDetail(state.detail);}")
        self.assertTrue(page.locator('[data-inline-section="overview"]').is_visible())
        self.assertEqual(page.locator('.detail-tabs [role="tab"]').count(), 4)
        page.locator('#detail-tab-history').click()
        page.locator('.history-item').first.wait_for()
        page.locator('#detail-tab-current').click()
        page.locator('[data-inline-edit="overview"]').click()
        self.assertTrue(page.locator('#edit-dialog').evaluate('dialog => dialog.open'))


if __name__ == "__main__":
    unittest.main()
