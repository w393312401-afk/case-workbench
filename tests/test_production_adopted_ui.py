"""Orders keep their exact adopted text visible while related revisions stay accessible."""
import unittest
from pathlib import Path
from uuid import uuid4

from test_production_carousel_ui import CarouselBrowserTests


def save_script(app, text, parent=None, chinese="采用稿中文。", source=None):
    values = {"product": "202H", "title": "采用稿与自动历史", "text": text,
              "translation_zh": chinese, "confirmed": True}
    if parent:
        values.update(parent_id=parent["id"], direction_id=parent.get("direction_id", ""))
    return app.creation.mutate("script", {"request_id": uuid4().hex,
        "revision": app.creation.public()["revision"], "script": values}, source)["script"]


def snapshot_mobile(page, name):
    folder = Path.cwd() / ".test-artifacts" / "script-flow"
    folder.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 390, "height": 740})
    for close in page.locator(".toast-close").all():
        if close.is_visible():
            close.click()
    for scheme in ("light", "dark"):
        page.emulate_media(color_scheme=scheme)
        adopted = page.locator("#job-adopted-script, #carousel-adopted-script")
        adopted.evaluate("node => window.scrollTo(0, node.getBoundingClientRect().top + window.scrollY - 82)")
        page.screenshot(path=str(folder / f"{name}-{scheme}-mobile.png"))


class VideoAdoptedBrowserTests(CarouselBrowserTests):
    def test_real_second_version_prefills_editor_and_editing_keeps_the_order_on_its_original_copy(self):
        source = self.app.creation.get_script(self.carousel["script_id"])["source"]
        first = save_script(self.app, "One kit.", source={**source, "variant_index": 0})
        script = save_script(self.app, "One kit.", first, chinese="这一单采用的二版中文。")
        job = self.app.create_production_job({"request_id": uuid4().hex,
            "revision": self.app.production.public()["revision"], "job": {"script_id": script["id"]}})["job"]
        page = self.open_page(f"#production/{job['id']}")
        page.set_viewport_size({"width": 390, "height": 740})
        page.locator('[data-step="lines"]').click()
        adopted = page.locator("#job-adopted-script")
        page.locator(f'#job-adopted-script[data-adopted-script-id="{script["id"]}"]').wait_for()
        self.assertEqual(adopted.locator(".section-title-row h2").inner_text(), "本次采用稿 · 第 2 版")
        # The lines table carries every sentence, so the order's full copy waits behind one fold.
        fold = adopted.locator("[data-adopted-copy-fold]")
        self.assertFalse(fold.evaluate("node => node.open"))
        self.assertNotIn("One kit.", adopted.inner_text())
        self.assertTrue(adopted.locator("[data-adopted-copy]").is_visible())
        fold.locator(":scope > summary").click()
        self.assertIn("One kit.", adopted.inner_text())
        self.assertIn(script["translation_zh"], adopted.inner_text())
        self.assertEqual(page.locator('.page-heading a[href*="/scripts/"]').count(), 0)
        original_link = adopted.locator(f'[data-script-version="{script["id"]}"] [data-adopted-version-detail]')
        self.assertEqual(original_link.get_attribute("href"), f'#/202H/content/{script["id"]}')
        self.assertFalse(original_link.is_visible())
        page.locator("#job-script-original > summary").click()
        self.assertIn("One kit.", page.locator("#job-script-body").inner_text())
        self.assertIn("One kit.", page.locator("#job-script-panel .production-table").inner_text())
        adopted.locator("[data-adopted-edit]").click()
        editor = page.locator("#script-editor-dialog")
        self.assertEqual(editor.locator('[name="text"]').input_value(), script["text"])
        self.assertEqual(editor.locator('[name="translation_zh"]').input_value(), script["translation_zh"])
        editor.locator('[name="text"]').fill("A new spoken draft for another production.")
        editor.locator('[name="translation_zh"]').fill("新的三版中文。")
        editor.get_by_role("button", name="保存采用稿", exact=True).click()
        page.locator("#content-adopted-script .adopted-copy > pre").filter(
            has_text="A new spoken draft for another production.").first.wait_for()
        versions = [row for row in self.app.creation.public()["scripts"] if row["family_id"] == script["family_id"]]
        self.assertEqual(len(versions), 3)
        later = max(versions, key=lambda row: row["version"])
        self.assertEqual((later["parent_id"], later["translation_zh"]), (script["id"], "新的三版中文。"))
        self.assertEqual(self.app.production.job(job["id"])["script_id"], script["id"])
        self.assertEqual(self.app.production.job(job["id"])["lines"][0]["spoken"], "One kit.")
        page.evaluate("hash => { location.hash = hash; }", f"#production/{job['id']}")
        page.locator('[data-step="lines"]').click()
        page.locator(f'#job-adopted-script[data-adopted-script-id="{script["id"]}"]').wait_for()
        history = adopted.locator("[data-script-versions]")
        history.locator(":scope > summary").click()
        version = history.locator(f'[data-script-version="{later["id"]}"]')
        version.locator(":scope > summary").click()
        self.assertIn(later["text"], version.inner_text())
        snapshot_mobile(page, "video-adopted")
        page.locator('[data-step="voice"]').click()
        self.assertFalse(adopted.is_visible())
        self.assertEqual(self.app.creation.get_script(script["id"])["text"], "One kit.")

    def test_script_page_without_a_video_has_no_empty_performance_card(self):
        script = save_script(self.app, "Not posted yet.")
        page = self.open_page(f"#content/{script['id']}")
        page.locator("#content-adopted-script .adopted-copy > pre").first.wait_for()
        # The side panel already says the video ID is not registered; no second empty card below.
        self.assertEqual(page.locator("#script-sales").inner_html(), "")

    def test_single_version_order_shows_only_the_version_page_link(self):
        script = save_script(self.app, "Only one version.")
        job = self.app.create_production_job({"request_id": uuid4().hex,
            "revision": self.app.production.public()["revision"], "job": {"script_id": script["id"]}})["job"]
        page = self.open_page(f"#production/{job['id']}")
        page.locator('[data-step="lines"]').click()
        adopted = page.locator(f'#job-adopted-script[data-adopted-script-id="{script["id"]}"]')
        link = adopted.locator(".adopted-version-single")
        link.wait_for()
        self.assertEqual(link.get_attribute("href"), f'#/202H/content/{script["id"]}')
        self.assertEqual(adopted.locator("[data-script-versions]").count(), 0)
        self.assertNotIn("Only one version.", adopted.inner_text())
        hint = adopted.locator(".section-title-row h2 .production-hint")
        hint.locator("summary").click()
        title, body = adopted.locator(".section-title-row h2").bounding_box(), hint.locator(".production-hint-body").bounding_box()
        self.assertLess(abs(body["y"] - (title["y"] + title["height"])), 24)
        self.assertEqual(adopted.locator(".section-title-row h2").evaluate("node => node.firstChild.textContent"), "本次采用稿 · 第 1 版")


class CarouselAdoptedBrowserTests(CarouselBrowserTests):
    def test_adopted_text_and_history_stay_with_the_slides_step_without_changing_order_text(self):
        first = self.app.creation.get_script(self.carousel["script_id"])
        script = save_script(self.app, first["text"], first, chinese="图文二版采用稿中文。")
        carousel = self.app.create_carousel({"request_id": uuid4().hex,
            "revision": self.app.production.public()["revision"], "carousel": {"script_id": script["id"]}})["carousel"]
        later = save_script(self.app, "Later spoken draft for another production.", script)
        page = self.open_page(f"#production/{carousel['id']}")
        page.locator('[data-step="slides"]').click()
        adopted = page.locator("#carousel-adopted-script")
        page.locator(f'#carousel-adopted-script[data-adopted-script-id="{script["id"]}"]').wait_for()
        self.assertEqual(adopted.locator(".section-title-row h2").inner_text(), "本次采用稿 · 第 2 版")
        adopted.locator("[data-adopted-copy-fold] > summary").click()
        self.assertIn(script["text"], adopted.inner_text())
        self.assertIn(script["translation_zh"], adopted.inner_text())
        self.assertEqual(page.locator('.page-heading a[href*="/scripts/"]').count(), 0)
        original_link = adopted.locator(f'[data-script-version="{script["id"]}"] [data-adopted-version-detail]')
        self.assertEqual(original_link.get_attribute("href"), f'#/202H/content/{script["id"]}')
        self.assertFalse(original_link.is_visible())
        self.assertTrue(page.evaluate("""() => Boolean(document.querySelector('#carousel-adopted-script')
            .compareDocumentPosition(document.querySelector('.carousel-table')) & Node.DOCUMENT_POSITION_FOLLOWING)"""))
        history = adopted.locator("[data-script-versions]")
        history.locator(":scope > summary").click()
        version = history.locator(f'[data-script-version="{later["id"]}"]')
        version.locator(":scope > summary").click()
        self.assertIn(later["text"], version.inner_text())
        snapshot_mobile(page, "carousel-adopted")
        page.locator('[data-step="pick"]').click()
        self.assertFalse(adopted.is_visible())
        self.assertEqual(self.app.production.carousel(carousel["id"])["script_id"], script["id"])
        self.assertEqual(self.app.creation.get_script(script["id"])["text"], script["text"])


# Share the disposable fixtures without rediscovering inherited scenarios.
for case, base in ((VideoAdoptedBrowserTests, CarouselBrowserTests),
                   (CarouselAdoptedBrowserTests, CarouselBrowserTests)):
    for name in dir(base):
        if name.startswith("test_") and name not in case.__dict__:
            setattr(case, name, None)
del CarouselBrowserTests, case, base, name


if __name__ == "__main__":
    unittest.main()
