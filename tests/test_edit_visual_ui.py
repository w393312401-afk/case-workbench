"""剪辑说明的时间轴对照：车道对齐、点镜头看对比、在达芬奇定位，全部用假接口。"""
from __future__ import annotations

import base64
import json
import unittest

from playwright.sync_api import expect

import test_production_validation_ui as fixture

JOB_ID = fixture.JOB_ID
EDIT_ID = "EDT-0123456789ab"
# 1x1 JPEG, enough for <img> to load.
JPEG = base64.b64decode("/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=")

VISUAL = {"available": True, "fps": 30, "total": 120, "timeline_uid": "uid-own", "replica": True,
          "counts": {"exact": 1, "close": 1, "substitute": 1, "missing": 0},
          "lines": [{"number": 1, "start": 0, "end": 60, "zh": "你的手腕不必充当角度导向器。", "spoken": "One"},
                    {"number": 2, "start": 60, "end": 120, "zh": "夹住刀，你只管推磨。", "spoken": "Two"}],
          "cut": [{"index": 0, "start": 0, "end": 30, "code": "MD109", "label": "切纸 MD109"},
                  {"index": 1, "start": 30, "end": 60, "code": "MD032", "label": ""},
                  {"index": 2, "start": 60, "end": 120, "code": "MD049", "label": ""}],
          "original": [{"number": 1, "start": 0, "end": 30}, {"number": 2, "start": 30, "end": 45},
                       {"number": 3, "start": 45, "end": 60}, {"number": 4, "start": 60, "end": 120}],
          "rows": [{"label": "1", "start": 0, "end": 30, "shots": [1], "status": "close", "original": "切纸", "used": "MD109 一刀切开", "note": "刀型，白纸"},
                   {"label": "2–3", "start": 30, "end": 60, "shots": [2, 3], "status": "exact", "original": "调角度", "used": "MD032 → MD033", "note": "对上"},
                   {"label": "4", "start": 60, "end": 120, "shots": [4], "status": "substitute", "original": "夹爪微距", "used": "MD049 拧紧", "note": "换了动作"}],
          "gaps": [],
          "text": "成片已铺满并保存，交人工审查。\n\n- **已用 3 次的片段：** MD043 已用 3 次。\n  - 只有它带号召手势"}


class EditVisualBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ProductionValidationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ProductionValidationBrowserTests.tearDownClass.__func__)
    stop_server = fixture.ProductionValidationBrowserTests.stop_server
    request = fixture.ProductionValidationBrowserTests.request
    make_playable_video = fixture.ProductionValidationBrowserTests.make_playable_video

    def setUp(self):
        fixture.ProductionValidationBrowserTests.setUp(self)
        self.payload["job"]["timelines"] = [{"unique_id": "uid-own", "name": "Lab261011-202-1", "creator": "Lab", "markers": 1,
                                             "frames": 120, "fps": "29.97", "take_id": "TAK-0123456789ab", "built_at": "2026-10-11"}]
        self.payload["job"]["takes"] = [fixture.ProductionValidationBrowserTests.take(self, "TAK-0123456789ab", [{"number": 1, "start": 0, "end": 2}])]
        self.payload["job"]["selected_take"] = "TAK-0123456789ab"
        edit = fixture.ProductionValidationBrowserTests.edit(self, "awaiting_review")
        edit.update(mode="replica", model="claude-default", output="成片已铺满并保存。\n\n| 镜头 | 原片 |\n|---|---|\n| 1 | 切纸 |")
        self.payload["job"]["edits"] = [edit]
        self.visual = json.loads(json.dumps(VISUAL, ensure_ascii=False))
        self.frames, self.opened = [], []

    def open_page(self, width=1080):
        context = self.browser.new_context(viewport={"width": width, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        # The write-up starts folded beside the preview; these tests read it as a user who keeps it open.
        if getattr(self, "keep_open", True):
            page.add_init_script("try { localStorage.setItem('production-edit-visual-open', '1'); } catch {}")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))

        def job_route(route):
            request, url = route.request, route.request.url
            if url.endswith(f"/edits/{EDIT_ID}/visual"):
                route.fulfill(status=200, content_type="application/json", body=json.dumps(self.visual, ensure_ascii=False))
            elif f"/edits/{EDIT_ID}/frame/" in url:
                self.frames.append(url.split("/frame/")[1])
                if url.endswith("/cut/1"):
                    route.fulfill(status=404, content_type="application/json", body='{"error":"素材原件不在"}')
                else:
                    route.fulfill(status=200, content_type="image/jpeg", body=JPEG)
            elif request.method == "GET":
                route.fulfill(status=200, content_type="application/json", body=json.dumps(self.payload, ensure_ascii=False))
            else:
                self.opened.append({"url": url, "body": request.post_data_json})
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"name": "Lab261011-202-1"}))

        page.route(f"**/api/production/jobs/{JOB_ID}**", job_route)
        page.route("**/api/production/resolve", lambda route: route.fulfill(status=200, content_type="application/json",
            body=json.dumps({"ok": True, "project": "202H", "version": "test", "timelines": ["素材时间线"]})))
        page.route("**/api/production/edit-clients", lambda route: route.fulfill(status=200, content_type="application/json",
            body=json.dumps({"providers": {"claude": {"available": True}, "codex": {"available": True}}, "busy": False})))
        page.route("**/api/ai/options", lambda route: route.fulfill(status=200, content_type="application/json",
            body=json.dumps({"default_settings": {"model": "claude-default", "reasoning_effort": None},
                             "models": [{"id": "claude-default", "name": "Claude 客户端默认", "available": True, "reasoning_efforts": ["high"]}], "skills": []})))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#production/{JOB_ID}")
        page.locator("[data-production-title]").wait_for()
        page.locator('.production-steps [data-step~="edit"]').click()
        return page

    def test_lanes_line_up_and_the_first_swapped_shot_is_shown(self):
        page = self.open_page()
        visual = page.locator(f'[data-edit-visual="{EDIT_ID}"]')
        expect(visual).to_have_attribute("open", "")
        expect(visual.locator("summary").first).to_have_text("Claude Code 客户端的剪辑说明")
        expect(visual.locator(".ev-lead")).to_contain_text("成片已铺满并保存")
        expect(visual.locator(".ev-chips")).to_contain_text("一致 2")  # 镜头 2–3 counts as two shots.
        expect(visual.locator(".ev-chips")).to_contain_text("换了动作 1")
        expect(visual.locator(".ev-chips small")).to_have_count(0)
        expect(visual.locator(".ev-line")).to_have_count(2)
        expect(visual.locator(".ev-row")).to_have_count(3)
        # Each lane is drawn to the same frame scale.
        widths = visual.locator(".ev-row").evaluate_all("rows => rows.map(row => row.style.width)")
        self.assertEqual(widths, ["25%", "25%", "50%"])
        self.assertEqual(visual.locator(".ev-row").nth(2).evaluate("row => row.style.left"), "50%")
        # A frame the source no longer has is drawn as a striped cell, not a broken image.
        expect(visual.locator(".ev-thumbs .ev-no-frame")).to_have_count(1)
        detail = visual.locator("[data-ev-detail]")
        expect(detail).to_contain_text("换了动作")
        expect(detail).to_contain_text("镜头 4")
        expect(detail).to_contain_text("夹住刀，你只管推磨。")
        expect(detail.locator(".ev-big")).to_have_count(2)
        # The write-up's remaining notes wait behind 完整说明 and are rendered, nested bullets included.
        expect(visual.locator(".ev-text strong").get_by_text("已用 3 次的片段：")).to_be_hidden()
        visual.locator(".ev-rest > summary").click()
        expect(visual.locator(".ev-text strong").get_by_text("已用 3 次的片段：")).to_be_visible()
        expect(visual.locator(".ev-text li li")).to_have_text("只有它带号召手势")
        self.assertNotIn("| 镜头 |", visual.inner_text())
        self.assertIn("orig/4", self.frames)

    def test_click_keys_and_davinci_jump(self):
        page = self.open_page()
        visual = page.locator(f'[data-edit-visual="{EDIT_ID}"]')
        visual.locator('[data-ev-row="1"]').click()
        detail = visual.locator("[data-ev-detail]")
        expect(detail).to_contain_text("镜头 2–3")
        expect(detail.locator(".ev-pics").first.locator(".ev-big")).to_have_count(2)
        page.keyboard.press("ArrowLeft")
        expect(detail).to_contain_text("镜头 1")
        expect(detail).to_contain_text("刀型，白纸")
        expect(detail.get_by_role("button", name="上一个")).to_be_disabled()
        detail.get_by_role("button", name="下一个").click()
        expect(detail).to_contain_text("镜头 2–3")
        detail.get_by_role("button", name="在达芬奇定位").click()
        page.wait_for_function("() => document.querySelector('#toast')?.textContent.includes('定位到第 1.00 秒')")
        self.assertEqual(self.opened[-1]["body"], {"unique_id": "uid-own", "frame": 30})

    def test_explain_cut_without_status_and_text_only_fallback(self):
        self.visual.update(replica=False, original=None, counts=None)
        for row in self.visual["rows"]:
            row["status"] = None
        page = self.open_page(width=390)
        visual = page.locator(f'[data-edit-visual="{EDIT_ID}"]')
        expect(visual.locator(".ev-row")).to_have_count(3)
        expect(visual.locator(".ev-chips")).to_have_count(0)
        expect(visual.locator(".ev-lane-name")).to_have_text(["配音意群", "成片", "镜头"])
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_the_write_up_starts_folded_and_remembers_being_opened(self):
        self.keep_open = False
        self.visual["text"] = "成片已铺满并保存。\n\n### 完成情况\n\n- 共 3 个镜头"
        page = self.open_page()
        visual = page.locator(f'[data-edit-visual="{EDIT_ID}"]')
        self.assertFalse(visual.evaluate("node => node.open"))
        self.assertEqual(self.frames, [])
        visual.locator("summary").first.click()
        expect(visual.locator(".ev-lead")).to_contain_text("成片已铺满并保存")
        self.assertEqual(page.evaluate("localStorage.getItem('production-edit-visual-open')"), "1")
        # The write-up's own `###` lines read as headings, not as raw marks.
        visual.locator(".ev-rest > summary").click()
        expect(visual.locator(".ev-rest h4.ev-heading")).to_have_text("完成情况")
        self.assertNotIn("###", visual.inner_text())
        page.reload()
        page.locator(f'[data-edit-visual="{EDIT_ID}"] .ev-lead').wait_for()

    def test_without_snapshot_the_write_up_is_rendered_as_text(self):
        self.visual = {"available": False, "reason": "这次剪辑没有交付快照，只能显示文字说明。",
                       "text": "剪完了。\n\n| 镜头 | 原片 |\n|---|---|\n| 1 | 切纸 |"}
        page = self.open_page()
        visual = page.locator(f'[data-edit-visual="{EDIT_ID}"]')
        expect(visual).to_contain_text("只能显示文字说明")
        expect(visual.locator("table td").first).to_have_text("1")
        expect(visual.locator(".ev-row")).to_have_count(0)


if __name__ == "__main__":
    unittest.main()
