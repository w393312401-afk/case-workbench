"""开头库 page: technique ranking with medians, cards for the chosen technique, retyping and deleting an opening."""
from __future__ import annotations

import base64
import io
import os
import unittest

from playwright.sync_api import expect

import test_ui_regressions as browser

try:
    from PIL import Image
except ImportError:  # The page still works without a picture; only the frame check is skipped.
    Image = None


def frame():
    if Image is None:
        return ""
    buffer = io.BytesIO()
    Image.new("RGB", (72, 128), (185, 87, 58)).save(buffer, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def opening(number, kind, views, followers, likes, line):
    return {"id": f"75500000000000000{number:02d}", "author": f"acct{number}", "title": f"Video {number}",
            "stats": {"views": views, "followers": followers, "likes": likes, "publishedAt": "2026-11-10T10:00:00Z", "commerce": number % 2 == 0},
            "hook": {"type": kind, "lines": [{"time": "00:00", "en": line, "zh": "中文 " + str(number)}], "screen": "Screen " + str(number), "why": "说出观众的怀疑" if kind == "质疑回应" else ""},
            "frame": frame() if number == 1 else ""}


class HookBankBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    setUp = browser.WorkbenchBrowserTests.setUp
    stop_server = browser.WorkbenchBrowserTests.stop_server
    request = browser.WorkbenchBrowserTests.request
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def open_hooks(self, width=1280):
        context = self.browser.new_context(viewport={"width": width, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/hooks")
        page.locator("#hook-page").wait_for()
        return page

    def test_empty_library_explains_how_to_fill_it(self):
        page = self.open_hooks()
        expect(page.locator(".hook-empty strong")).to_have_text("还没有开头")
        expect(page.locator("#hooks-nav")).to_have_attribute("aria-current", "page")

    def test_ranking_cards_retype_and_delete(self):
        videos = [opening(1, "质疑回应", 1200000, 150000, 48000, "Will this ruin my knives?"),
                  opening(2, "质疑回应", 500000, 100000, 12000, "Everyone says these wreck blades."),
                  opening(3, "结果先行", 300000, 100000, 9000, "Look at this tomato."),
                  opening(4, "直接报产品", 20000, 40000, 300, "This is the SHARPAL 202H.")]
        self.assertEqual(self.server.app.hook_bank.save("202H", videos)["saved"], 4)
        page = self.open_hooks()
        rows = page.locator(".hook-rank tbody tr")
        expect(rows).to_have_count(3)
        expect(rows.nth(0).locator(".hook-type-pick")).to_have_text("质疑回应")
        expect(rows.nth(0).locator(".hook-bar-value")).to_have_text("6.5 倍")
        expect(rows.nth(0).locator(".hook-thin")).to_have_text("样本少")
        expect(page.locator(".hook-list h2")).to_contain_text("质疑回应 · 2 条")
        cards = page.locator(".hook-card")
        expect(cards).to_have_count(2)
        expect(cards.nth(0).locator(".hook-line")).to_contain_text("Will this ruin my knives?")
        expect(cards.nth(0).locator(".hook-card-foot")).to_contain_text("播粉比 8 倍")
        if Image is not None:
            self.assertGreater(cards.nth(0).locator("img.hook-frame").evaluate("img => img.complete && img.naturalWidth"), 0)
        if os.environ.get("HOOK_BANK_PREVIEW"):
            page.screenshot(path=os.environ["HOOK_BANK_PREVIEW"], full_page=True)
        page.locator("[data-hook-type='结果先行']").click()
        expect(page.locator(".hook-list h2")).to_contain_text("结果先行 · 1 条")
        page.locator("[data-hook-type='质疑回应']").click()
        page.locator("[data-hook-retype='7550000000000000002']").select_option("反常识")
        expect(page.locator(".hook-rank tbody tr")).to_have_count(4)
        self.assertEqual(next(item for item in self.server.app.hook_bank.list() if item["video_id"] == "7550000000000000002")["type"], "反常识")
        page.locator("[data-hook-commerce]").check()
        expect(page.locator(".hook-rank tbody tr")).to_have_count(2)
        page.locator("[data-hook-commerce]").uncheck()
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("[data-hook-delete='7550000000000000001']").click()
        expect(page.locator(".hook-rank tbody tr")).to_have_count(3)
        self.assertEqual(len(self.server.app.hook_bank.list()), 3)
        page.locator("[data-hook-source='own']").click()
        expect(page.locator(".hook-empty strong")).to_have_text("这个范围里没有开头")


if __name__ == "__main__":
    unittest.main()
