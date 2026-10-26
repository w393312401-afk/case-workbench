"""Delivered versions remain frozen when today's selections change; no live data or network."""
from __future__ import annotations

import copy
from pathlib import Path
import unittest

from playwright.sync_api import expect, sync_playwright


STATIC = Path(__file__).resolve().parents[1] / "static"
JOB = "PRD-111111111111"
CAROUSEL = "CAR-111111111111"
OLD_SCRIPT = "SCR-111111111111"
NEW_SCRIPT = "SCR-222222222222"
OLD_TAKE = "TAK-111111111111"
OLD_EDIT = "EDT-111111111111"
OLD_OUTPUT = "OUT-111111111111"
OLD_DELIVERY = "DLV-111111111111"


def publication(identifier="VPU-111111111111"):
    return {"id": identifier, "job_id": JOB, "script_id": OLD_SCRIPT, "script_version": 1,
            "account": "fixture", "published_on": "2026-10-14", "post_id": "7654986913497009441",
            "registered_at": "2026-10-14T12:00:00", "source_current": False, "file_available": True,
            "file": {"name": "frozen.mp4", "size": 100, "sha256": "a" * 64, "original_path": "C:/fixture/frozen.mp4"},
            "source": {"script": {"script_id": OLD_SCRIPT, "script_version": 1, "product": "202H",
                                    "title": "冻结旧稿", "lines": [{"spoken": "The exact old words."}]},
                       "take": {"id": OLD_TAKE, "voice_id": "frozen-voice", "text": "The exact old words."},
                       "timeline": {"unique_id": "uid-old", "name": "冻结旧时间线"},
                       "edit": {"id": OLD_EDIT, "finished_at": "2026-10-14T11:00:00",
                                "baseline": {"tracks": {"subtitle": 1}}}}}


def delivery(identifier=OLD_DELIVERY, output_id=OLD_OUTPUT, version=1):
    return {"id": identifier, "script_id": OLD_SCRIPT if version == 1 else NEW_SCRIPT, "script_version": version,
            "filename": "冻结成品包.zip", "sha256": "a" * 64 if version == 1 else "b" * 64,
            "caption_en": "Saved caption." if version == 1 else "Current caption.",
            "slides": [{"number": 1, "output_id": output_id, "sha256": "c" * 64}],
            "source": {"kind": "carousel", "carousel_id": CAROUSEL,
                       "script": {"script_id": OLD_SCRIPT if version == 1 else NEW_SCRIPT,
                                  "script_version": version, "product": "202H", "title": "冻结旧图文"},
                       "slides": [{"number": 1, "title_lines": ["Saved headline."], "body": "Saved body.",
                                   "output": {"id": output_id, "version": 2, "sha256": "c" * 64}}]}}


class DeliveryProvenanceBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 390, "height": 900})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors, self.posts = [], []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("request", lambda request: self.posts.append(request.url) if request.method == "POST" else None)
        self.addCleanup(lambda: self.assertEqual(self.errors, []))
        self.addCleanup(lambda: self.assertEqual(self.posts, []))
        self.page.route("**/*", lambda route: route.fulfill(content_type="text/html", body='<main style="padding:14px"><section id="provenance"></section></main>'))
        self.page.goto("http://127.0.0.1:43219/delivery-provenance-test")
        self.page.add_style_tag(content="body{margin:0;font-family:Arial,sans-serif} main,section{box-sizing:border-box;min-width:0}")
        self.page.add_style_tag(path=str(STATIC / "delivery-provenance.css"))
        self.page.add_script_tag(path=str(STATIC / "delivery-provenance.js"))
        self.page.evaluate("window.WorkbenchShell={url:(hash,product)=>'#/'+product+'/'+hash.slice(1)}")

    def mount(self, kind, record, owner, publication=None):
        self.page.evaluate("options=>DeliveryProvenance.mount(document.querySelector('#provenance'),options)",
                           {"kind": kind, "record": record, "owner": owner, "publication": publication})
        self.page.locator(".delivery-provenance > summary").click()

    def links(self, kind):
        return self.page.locator(f'[data-provenance-kind="{kind}"]')

    def test_video_links_use_saved_versions_and_exported_subtitles_remain_unknown(self):
        record = publication()
        owner = {"id": JOB, "product": "202H", "script_id": NEW_SCRIPT, "script_version": 99,
                 "selected_take": "TAK-222222222222", "takes": [{"id": "TAK-222222222222"}, {"id": OLD_TAKE}],
                 "subtitles": {"enabled": True}, "edits": [{"id": "EDT-222222222222"}]}
        self.mount("video", record, owner)
        self.assertEqual(self.links("script").get_attribute("href"), f"#/202H/content/{OLD_SCRIPT}")
        self.assertEqual(self.links("voice").get_attribute("href"), f"#/202H/production/{JOB}?step=voice&run={OLD_TAKE}")
        self.assertEqual(self.links("timeline").get_attribute("href"), f"#/202H/production/{JOB}?step=timeline&run=uid-old")
        self.assertEqual(self.links("edit").get_attribute("href"), f"#/202H/production/{JOB}?step=edit&run={OLD_EDIT}")
        expect(self.page.locator('[data-provenance-subtitles="unknown"]')).to_be_visible()
        self.assertNotIn("第 2 版", self.links("voice").inner_text())
        self.assertNotIn("99", self.page.locator(".delivery-provenance").inner_text())
        self.assertFalse(self.page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))

    def test_only_explicit_actual_file_subtitle_confirmation_changes_the_subtitle_label(self):
        for state, expected in (({"scope": "timeline", "confirmed": True, "present": True}, "unknown"),
                                ({"scope": "actual_file", "confirmed": False, "present": True}, "unknown"),
                                ({"scope": "actual_file", "confirmed": True, "present": True}, "present"),
                                ({"scope": "actual_file", "confirmed": True, "present": False}, "absent")):
            with self.subTest(expected=expected, state=state):
                record = publication()
                record["file"]["subtitles"] = state
                self.mount("video", record, {"id": JOB, "product": "202H"})
                expect(self.page.locator(f'[data-provenance-subtitles="{expected}"]')).to_be_visible()

    def test_manual_completion_does_not_claim_a_prior_failed_edit_is_the_final_edit(self):
        record = publication()
        record["source"]["manual_completion"] = {"at": "2026-10-14T12:00:00", "basis": "human_review"}
        record["source"]["edit"]["status"] = "failed"
        self.mount("video", record, {"id": JOB, "product": "202H"})
        text = self.page.locator(".delivery-provenance").inner_text()
        self.assertIn("人工完成", text)
        self.assertIn("此前剪辑记录", self.links("edit").inner_text())
        self.assertNotIn("剪辑版本 ·", self.links("edit").inner_text())

    def test_video_current_and_historical_receipts_render_their_own_frozen_versions(self):
        current, old = publication(), publication("VPU-222222222222")
        old["source"]["take"]["id"] = "TAK-333333333333"
        old["source"]["script"]["script_id"] = "SCR-333333333333"
        old["script_id"] = "SCR-333333333333"
        old.pop("file_available")
        old.pop("source_current")
        self.page.add_script_tag(path=str(STATIC / "video-publication.js"))
        self.page.evaluate("""async ({current,old})=>{
          window.publicationData={publication:current,history:[old],can_register:false,blocked_reason:'fixture'};
          window.owner={id:'PRD-111111111111',product:'202H',takes:[{id:'TAK-333333333333'},{id:'TAK-111111111111'}]};
          window.remount=()=>VideoPublication.mount(document.querySelector('#provenance'),{job:window.owner,api:async()=>structuredClone(publicationData)});
          await remount();
        }""", {"current": current, "old": old})
        expect(self.page.locator(".delivery-provenance")).to_have_count(2)
        self.page.locator(".video-publication-history > summary").click()
        self.page.locator(".delivery-provenance > summary").evaluate_all("nodes=>nodes.forEach(node=>node.click())")
        old_receipt = self.page.locator('[data-provenance-record="VPU-222222222222"]')
        self.assertIn("TAK-333333333333", old_receipt.inner_text())
        self.assertIn("SCR-333333333333", old_receipt.locator('[data-provenance-kind="script"]').get_attribute("href"))
        self.assertEqual(old_receipt.locator(".video-publication-warning").count(), 0)
        self.page.evaluate("async()=>{owner.takes.reverse();await remount();}")
        self.assertEqual(self.page.locator('[data-provenance-record="VPU-222222222222"] [data-provenance-kind="voice"]').text_content(),
                         "配音版本 · TAK-333333333333")

    def test_video_receipt_keeps_one_source_fold_with_readable_registration_facts(self):
        current, unbound = publication(), publication("VPU-222222222222")
        unbound.pop("file")
        self.page.add_script_tag(path=str(STATIC / "video-publication.js"))
        self.page.evaluate("""async ({current,unbound})=>{
          window.publicationData={publication:current,history:[unbound],can_register:false,blocked_reason:'fixture',
            script_registrations:['7654986913497009441','7654986913497009442']};
          await VideoPublication.mount(document.querySelector('#provenance'),{job:{id:'PRD-111111111111',product:'202H'},
            api:async()=>structuredClone(publicationData)});
        }""", {"current": current, "unbound": unbound})
        receipt = self.page.locator('.video-publication-receipt').first
        self.assertEqual(receipt.locator(":scope > details").count(), 1)
        self.assertEqual(receipt.locator("[data-script-registration]").inner_text(), "第 1/2 次登记")
        receipt.locator(".delivery-provenance > summary").click()
        body = receipt.locator(".delivery-provenance-body")
        self.assertIn("2026/10/14 12:00", body.inner_text())
        self.assertIn("C:/fixture/frozen.mp4", body.inner_text())
        # A fingerprint shows a prefix; the full value stays in the tooltip, once.
        self.assertNotIn("a" * 64, body.inner_text())
        self.assertEqual(body.locator(f'[title="{"a" * 64}"]').count(), 1)
        self.page.locator(".video-publication-history > summary").click()
        old = self.page.locator('.video-publication-history .video-publication-receipt')
        self.assertEqual(old.locator(".video-publication-aside", has_text="未绑定成片").count(), 1)
        self.assertEqual(old.locator(".context-note", has_text="只登记了视频 ID").count(), 0)

    def test_saved_utc_times_read_in_local_time_with_the_raw_value_kept(self):
        context = self.browser.new_context(timezone_id="Asia/Shanghai")
        self.addCleanup(context.close)
        page = context.new_page()
        page.route("**/*", lambda route: route.fulfill(content_type="text/html", body='<section id="provenance"></section>'))
        page.goto("http://127.0.0.1:43219/delivery-provenance-tz")
        page.add_script_tag(path=str(STATIC / "delivery-provenance.js"))
        record = publication()
        record["source"]["manual_completion"] = {"at": "2026-10-24T02:05:00.123456+00:00", "basis": "human_review"}
        page.evaluate("options=>DeliveryProvenance.mount(document.querySelector('#provenance'),options)",
                      {"kind": "video", "record": record, "owner": {"id": JOB, "product": "202H"}})
        page.locator(".delivery-provenance > summary").click()
        stamp = page.locator('.delivery-provenance-id[title="2026-10-24T02:05:00.123456+00:00"]')
        self.assertEqual(stamp.inner_text(), "2026/10/24 10:05")

    def test_cleaned_carousel_publication_uses_its_frozen_package_and_image_selection(self):
        old, newest = delivery(), delivery("DLV-222222222222", "OUT-222222222222", 2)
        post = {"id": "PUB-111111111111", "delivery_id": old["id"], "delivery_sha256": old["sha256"],
                "script_id": OLD_SCRIPT, "script_version": 1, "post_id": "7654986913497009441", "delivery_snapshot": old}
        owner = {"id": CAROUSEL, "product": "202H", "script_id": NEW_SCRIPT, "script_version": 2,
                 "deliveries": [newest], "slides": [{"number": 1, "output_id": "OUT-222222222222"}]}
        self.mount("carousel", post, owner)
        self.assertEqual(self.links("script").get_attribute("href"), f"#/202H/content/{OLD_SCRIPT}")
        self.assertEqual(self.links("image").get_attribute("href"), f"#/202H/production/{CAROUSEL}?step=images&run={OLD_OUTPUT}")
        self.assertIn("成图第 2 版", self.links("image").inner_text())
        self.assertEqual(self.links("delivery").count(), 0, "A frozen source is not a downloadable historical package.")
        self.assertIn(OLD_DELIVERY, self.page.locator(".delivery-provenance").inner_text())
        self.assertIn("原发布来源，文件已清理", self.page.locator(".delivery-provenance").inner_text())
        self.assertNotIn("OUT-222222222222", self.page.locator(".delivery-provenance").inner_text())
        copy = self.page.locator(".delivery-provenance-text").filter(has=self.page.get_by_text("查看保存时的逐张文案", exact=True))
        copy.locator("summary").click()
        expect(copy.locator("pre")).to_have_text("第 1 张\nSaved headline.\nSaved body.")
        caption = self.page.locator(".delivery-provenance-text").filter(has=self.page.get_by_text("查看这包保存的发布文案", exact=True))
        caption.locator("summary").click()
        expect(caption.locator("pre")).to_have_text("Saved caption.")
        self.assertFalse(self.page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))

    def test_missing_or_wrong_package_bytes_do_not_borrow_the_current_images(self):
        old = delivery()
        post = {"id": "PUB-111111111111", "delivery_id": old["id"], "delivery_sha256": "wrong-checksum",
                "script_id": OLD_SCRIPT, "script_version": 1, "post_id": "7654986913497009441"}
        self.mount("carousel", post, {"id": CAROUSEL, "product": "202H", "deliveries": [old]})
        self.assertEqual(self.links("image").count(), 0)
        self.assertIn("图片版本未能核对", self.page.locator(".delivery-provenance").inner_text())
        self.assertIn("wrong-checksum", self.page.locator(".delivery-provenance").inner_text())

    def test_a_snapshot_with_the_wrong_identity_or_bytes_never_borrows_the_newest_sources(self):
        for field, value in (("id", "DLV-333333333333"), ("sha256", "incorrect-bytes")):
            with self.subTest(field=field):
                old = delivery()
                snapshot = {**old, field: value}
                post = {"id": "PUB-111111111111", "delivery_id": old["id"], "delivery_sha256": old["sha256"],
                        "script_id": OLD_SCRIPT, "script_version": 1, "post_id": "7654986913497009441", "delivery_snapshot": snapshot}
                self.mount("carousel", post, {"id": CAROUSEL, "product": "202H",
                    "deliveries": [delivery("DLV-222222222222", "OUT-222222222222", 2)]})
                self.assertEqual(self.links("image").count(), 0)
                self.assertEqual(self.links("delivery").count(), 0)
                self.assertIn("图片版本未能核对", self.page.locator(".delivery-provenance").inner_text())
                self.assertNotIn("OUT-222222222222", self.page.locator(".delivery-provenance").inner_text())

    def test_a_new_package_does_not_inherit_the_old_packages_publication_annotation(self):
        new = delivery("DLV-222222222222", "OUT-222222222222", 2)
        post = {"id": "PUB-111111111111", "delivery_id": OLD_DELIVERY, "delivery_sha256": "a" * 64,
                "post_id": "7654986913497009441"}
        self.mount("carousel", new, {"id": CAROUSEL, "product": "202H"}, publication=post)
        self.assertNotIn("实际发布", self.page.locator(".delivery-provenance").inner_text())
        self.assertNotIn(OLD_DELIVERY, self.page.locator(".delivery-provenance").inner_text())

    def test_legacy_package_keeps_exact_ids_without_inventing_the_image_ordinal(self):
        record = delivery()
        record.pop("source")
        self.mount("carousel", record, {"id": CAROUSEL, "product": "202H",
                                       "slides": [{"number": 1, "outputs": [{"id": OLD_OUTPUT}]}]})
        self.assertIn(OLD_OUTPUT, self.links("image").inner_text())
        self.assertNotIn("成图第", self.links("image").inner_text())
        self.assertEqual(self.links("script").get_attribute("href"), f"#/202H/content/{OLD_SCRIPT}")

    def test_saved_titles_and_text_are_escaped(self):
        record = publication()
        record["source"]["script"]["title"] = '<img src=x onerror="window.__executed=true">'
        record["source"]["script"]["lines"][0]["spoken"] = '<script>window.__executed=true</script>'
        self.mount("video", record, {"id": JOB, "product": "202H"})
        self.page.locator(".delivery-provenance-text > summary").click()
        self.assertEqual(self.page.locator(".delivery-provenance img,.delivery-provenance script").count(), 0)
        self.assertFalse(self.page.evaluate("Boolean(window.__executed)"))


if __name__ == "__main__":
    unittest.main()
