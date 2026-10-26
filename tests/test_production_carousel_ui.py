"""Carousel search, image import and picking through the real UI in an isolated library."""
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from PIL import Image

import test_server as fixture
import test_ui_regressions as browser
from test_production_carousel import CANDIDATE
from test_production_carousel_stills import CodexSearcher, Runner, Searcher
from resolve_bridge import ResolveBridge
from variant_parse import carousel_text, parse_carousels


class StillModelCatalog:
    """Keeps model discovery inside the isolated UI fixture, without native clients."""

    def __init__(self, codex):
        self.codex, self.error = codex, None

    def status(self):
        return {"available": True, "label": "Isolated model catalog"}

    def models(self):
        claude = [{"id": identifier, "name": name, "provider": "claude", "available": True}
                  for identifier, name in (("claude-default", "Claude 客户端默认"),
                                           ("claude-opus", "Claude Opus"), ("claude-sonnet", "Claude Sonnet"),
                                           ("claude-haiku", "Claude Haiku"))]
        codex = [] if self.error else [{**model, "provider": "codex"} for model in self.codex.models()["models"]]
        return {"models": claude + codex, "error": self.error}


class CarouselBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    open_page = browser.WorkbenchBrowserTests.open_page
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def setUp(self):
        browser.WorkbenchBrowserTests.setUp(self)
        self.app = self.server.app
        self.runner = Runner()
        self.searcher = Searcher()
        self.app.resolve = ResolveBridge(runner=self.runner)
        self.app.editor = self.searcher
        self.codex = CodexSearcher()
        self.app.still_codex = self.codex
        self.app.ai.bridge = self.catalog = StillModelCatalog(self.codex)
        mcp = patch("workbench_carousel_stills.locate_mcp", return_value="fixture-resolve-mcp")
        mcp.start()
        self.addCleanup(mcp.stop)
        self.app.carousel_image_root = self.root / "图文素材"
        folder = self.app.carousel_image_root / "202H-S2" / "原图"
        folder.mkdir(parents=True)
        self.source = folder / "03.jpg"
        Image.new("RGB", (120, 160), "white").save(self.source)
        source = {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 1,
                  "product": "202H", "case_id": None, "candidate_text": CANDIDATE,
                  "candidate_sha256": "x", "snapshot": {}}
        script = self.app.creation.mutate("script", {
            "request_id": uuid4().hex, "revision": self.app.creation.public()["revision"],
            "script": {"product": "202H", "direction_id": "202H-S2", "title": "送礼 · 图文",
                       "text": carousel_text(parse_carousels(CANDIDATE)[0]), "confirmed": True}}, source)["script"]
        self.carousel = self.app.create_carousel({"request_id": uuid4().hex,
            "revision": self.app.production.public()["revision"], "carousel": {"script_id": script["id"]},
            "requirements": {"mode": "photo_preserving"}})["carousel"]

    def test_historical_search_results_import_and_pick_each_slide_without_changing_script_or_source(self):
        source_bytes = self.source.read_bytes()
        script = self.app.creation.get_script(self.carousel["script_id"])
        # The old AI still-export form is retired. Seed its completed historical
        # result with fake clients, then exercise the supported history/pick UI.
        skill = Path(self.app.edit_skill_dir).parent / "tiktok-photo-commerce"
        (skill / "references").mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text("---\nname: tiktok-photo-commerce\n---\n# 图文证据边界", encoding="utf-8")
        self.app.start_still_search(self.carousel["id"], {
            "request_id": uuid4().hex, "materials": ["蓝点折刀成品素材"], "model": "codex-default"})
        deadline = time.monotonic() + 10
        while self.app.edit_running() and time.monotonic() < deadline:
            time.sleep(.02)
        history = self.app.production.carousel(self.carousel["id"])["searches"]
        self.assertEqual(history[-1]["status"], "completed", history[-1])
        page = self.open_page(f"#production/{self.carousel['id']}")
        self.assertEqual(page.locator("#still-form").count(), 0)
        page.locator('[data-step="pick"]').click()
        page.locator(".still-history > summary").click()
        page.locator('#search-runs .script-status').get_by_text("已完成", exact=True).wait_for(state="attached", timeout=15000)
        page.locator('#search-runs .edit-gaps').get_by_text("第 3 张：收口要看到一排刀", exact=True).wait_for()
        page.locator('[data-step="pick"]').click()
        page.locator('[data-pick="1"]').wait_for(timeout=15000)
        page.wait_for_function("() => [...document.querySelectorAll('.pick-card img')].every(img => img.complete && img.naturalWidth > 0)")
        self.assertEqual(page.locator('.pick-card img').first.evaluate("img => getComputedStyle(img).objectFit"), "contain")
        status, headers, original = self.request("GET", page.locator('.pick-card a').first.get_attribute("href"))
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertTrue(original.startswith(b"\x89PNG\r\n\x1a\n"))
        for number in (1, 2):
            page.locator(f'[data-pick="{number}"]').click()
            page.locator(f'[data-unpick="{number}"]').wait_for()
        page.locator('[data-import-slide="3"]').click()
        page.locator('.folder-pick img').wait_for()
        page.wait_for_function("() => document.querySelector('.folder-pick img')?.naturalWidth > 0")
        self.assertEqual(page.locator('.folder-pick img').evaluate("img => getComputedStyle(img).objectFit"), "contain")
        page.locator('.folder-pick').click()
        page.locator('[data-pick="3"]').click()
        page.locator('[data-unpick="3"]').wait_for(state="attached")
        page.locator('.page-heading .script-status').get_by_text("底图已选齐", exact=True).wait_for()
        self.assertEqual(self.source.read_bytes(), source_bytes)
        self.assertEqual(self.app.creation.get_script(script["id"]), script)
        self.assertEqual(self.app.production.carousel(self.carousel["id"])["status"], "ready")
        restored = [call for call in self.runner.calls if call["command"] == "restore"]
        self.assertEqual((restored[0]["unique_id"], restored[0]["timecode"]), ("uid-own", "00:00:12:03"))
        page.locator('[data-step="pick"]').click()
        page.locator('[data-unpick="3"]').click()
        page.locator('[data-pick="3"]').wait_for()
        self.assertIsNone(self.app.production.carousel(self.carousel["id"])["slides"][2]["base"])

    def test_archived_carousel_lives_in_content_history_and_direct_detail_restores_it(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.route("**/api/production/eleven", lambda route: route.fulfill(status=200,
            content_type="application/json", body=json.dumps({"ok": True})))
        page.locator('.production-more-actions > summary').click()
        page.locator('#carousel-archive').click()
        page.locator('#carousel-archive', has_text="恢复制作单").wait_for(state="attached")
        page.locator('#navigate-up').click()
        page.locator('#content-flow-list').wait_for()
        page.locator('#content-flow-status').wait_for(state='hidden')
        self.assertEqual(page.locator(f'[data-content-id="carousel:{self.carousel["id"]}"]').count(), 0)
        page.locator('[data-flow-view="all"]').click()
        archived = page.locator(f'[data-content-id="carousel:{self.carousel["id"]}"]')
        archived.get_by_text('已归档', exact=True).wait_for()
        self.assertEqual(self.app.production.public()['jobs'], [])
        self.assertTrue(self.app.production.carousel(self.carousel['id'])['archived'])

        # Archived records keep their details and history, and restoration returns the same content row.
        page.evaluate("id => { location.hash = '#production/' + id; }", self.carousel["id"])
        page.locator('#carousel-archive', has_text="恢复制作单").wait_for(state="attached")
        page.locator('.production-more-actions > summary').click()
        page.get_by_role("button", name="恢复制作单", exact=True).click()
        page.locator('#carousel-archive', has_text="归档制作单").wait_for(state="attached")
        self.assertFalse(self.app.production.carousel(self.carousel["id"])["archived"])
        self.assertEqual([item["id"] for item in self.app.production.public()["carousels"]], [self.carousel["id"]])
        page.locator('#navigate-up').click()
        page.locator('#content-flow-list').wait_for()
        restored = page.locator(f'[data-content-id="carousel:{self.carousel["id"]}"]')
        restored.wait_for()
        self.assertNotIn('已归档', restored.inner_text())
        self.assertEqual(page.locator('#production-queue, .scripts-tabs').count(), 0)



if __name__ == "__main__":
    unittest.main()
