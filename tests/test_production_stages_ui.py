"""The order page shows three stages, and its header button does the next thing itself."""
import unittest

import test_production_validation_ui as fixture
from test_production_validation_ui import JOB_ID

GOOD = [{"number": 1, "start": 0, "end": 1.5, "coverage": 1}]
TIMELINE = {"unique_id": "uid-own", "name": "Lab261011-202-1", "creator": "Lab", "markers": 1, "frames": 60, "fps": "29.97",
            "take_id": "TAK-0123456789ab", "built_at": "2026-10-11"}
SELECTED = '.production-steps [aria-selected="true"]'


class ProductionStagesBrowserTests(unittest.TestCase):
    # Borrow the fixture's helpers without running its own tests a second time.
    setUpClass = classmethod(fixture.ProductionValidationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ProductionValidationBrowserTests.tearDownClass.__func__)
    setUp = fixture.ProductionValidationBrowserTests.setUp
    stop_server = fixture.ProductionValidationBrowserTests.stop_server
    request = fixture.ProductionValidationBrowserTests.request
    make_playable_video = fixture.ProductionValidationBrowserTests.make_playable_video
    open_page = fixture.ProductionValidationBrowserTests.open_page
    edit = fixture.ProductionValidationBrowserTests.edit
    take = fixture.ProductionValidationBrowserTests.take
    reviewable = fixture.ProductionValidationBrowserTests.reviewable
    skill_delivery = fixture.ProductionValidationBrowserTests.skill_delivery

    def stage(self, page):
        return page.locator(SELECTED).get_attribute("data-stage")

    def built(self):
        self.payload["job"]["takes"] = [self.take("TAK-0123456789ab", GOOD)]
        self.payload["job"]["selected_take"] = "TAK-0123456789ab"
        self.payload["job"]["timelines"] = [dict(TIMELINE)]

    def test_a_new_order_is_started_from_the_header_in_one_click(self):
        self.payload["job"]["status"] = "draft"
        self.payload["creators"] = ["Lab", "Zack"]

        def answer(route):
            if route.request.url.endswith("/produce"):
                self.payload["job"].update(voicing=True, producing={"stage": "voice", "running": True, "error": "", "note": "",
                                                                     "since": "2026-11-25T10:00:00", "request_id": "x"})
            route.fulfill(status=200, content_type="application/json", body="{}")
        page = self.open_page(post=answer)
        self.assertEqual(self.stage(page), "prep")
        # 备料 shows the script, the voice form and the timeline block together; the cut is another stage.
        for shown in ("#job-script-panel", "#voice-panel", "#timeline-panel"):
            self.assertTrue(page.locator(shown).is_visible(), shown)
        self.assertFalse(page.locator("#edit-panel").is_visible())
        page.locator('[data-stage="prep"] small').get_by_text("待配音", exact=True).wait_for()
        button = page.locator(".page-heading [data-next-action]")
        button.get_by_text("开始制作", exact=True).wait_for()
        self.assertIn("primary", button.get_attribute("class"))
        # The voice form's own button stays, as the way to voice without going on.
        self.assertIn("secondary", page.locator("#take-form").get_by_role("button", name="生成配音", exact=True).get_attribute("class"))
        page.locator("#take-form [data-produce-creator]").select_option("Zack")
        button.click()
        page.locator("[data-producing]").get_by_text("正在配音", exact=False).wait_for()
        started = [call for call in self.calls if call["url"].endswith("/produce")]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["body"]["creator"], "Zack")
        self.assertEqual(started[0]["body"]["voice_id"], "VOICE123456789")
        self.assertEqual(started[0]["body"]["model_id"], "test-model")
        self.assertRegex(started[0]["body"]["request_id"], r"^[\w-]{10,}$")
        page.locator('[data-stage="prep"] small').get_by_text("配音中", exact=True).wait_for()
        self.assertTrue(page.locator("#take-form [data-produce]").is_disabled())

    def test_a_stopped_start_says_why_and_can_be_dismissed(self):
        self.payload["job"]["status"] = "draft"
        self.payload["job"]["takes"] = [self.take("TAK-0123456789ab", GOOD)]
        self.payload["job"]["producing"] = {"stage": "voice", "running": False, "error": "", "request_id": "x", "since": "",
                                            "note": "配好了一条，但意群对齐没过核验，没有自动选用。试听后手动选用，再接着建线和剪辑。"}

        def answer(route):
            if route.request.url.endswith("/produce-dismiss"):
                self.payload["job"]["producing"] = None
            route.fulfill(status=200, content_type="application/json", body="{}")
        page = self.open_page(post=answer)
        note = page.locator("[data-producing]")
        note.get_by_text("没有自动选用", exact=False).wait_for()
        note.get_by_role("button", name="知道了", exact=True).click()
        page.locator("[data-producing]").wait_for(state="detached")
        self.assertEqual([call["url"].rsplit("/", 1)[-1] for call in self.calls], ["produce-dismiss"])

    def test_a_chosen_voiceover_goes_on_to_the_cut_from_the_timeline_block(self):
        self.payload["job"]["takes"] = [self.take("TAK-0123456789ab", GOOD)]
        self.payload["job"].update(selected_take="TAK-0123456789ab", status="voiced")
        page = self.open_page()
        self.assertEqual(self.stage(page), "prep")
        button = page.locator(".page-heading [data-next-action]")
        button.get_by_text("建线并开始剪辑", exact=True).wait_for()
        button.click()
        page.wait_for_function("() => document.querySelector('#toast')?.textContent.includes('已开始')")
        started = [call for call in self.calls if call["url"].endswith("/produce")]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["body"]["creator"], "Lab")
        self.assertNotIn("voice_id", started[0]["body"])

    def test_the_cut_is_confirmed_from_the_header_only_while_review_is_on_screen(self):
        edit = self.skill_delivery()
        self.payload["job"]["takes"] = [self.take("TAK-0123456789ab", GOOD)]

        def approve(route):
            edit["approval"] = {"at": "2026-10-14T10:00:00", "note": ""}
            self.payload["job"]["status"] = "approved"
            route.fulfill(status=200, content_type="application/json", body='{"approved": true}')
        page = self.open_page(post=approve)
        self.assertEqual(self.stage(page), "review")
        button = page.locator(".page-heading [data-next-action]")
        button.get_by_text("确认完成并导出", exact=True).wait_for()
        self.assertIn("primary", button.get_attribute("class"))
        # The card's own confirm stays, one step back behind the header's.
        self.assertIn("secondary", page.locator("[data-approve-edit]").get_attribute("class"))
        page.locator('[data-stage="prep"]').click()
        self.assertEqual(self.stage(page), "prep")
        button.click()
        # From another stage the button only shows the place; nothing is confirmed unseen.
        self.assertEqual(self.stage(page), "review")
        self.assertEqual(self.calls, [])
        button.click()
        page.locator('[data-revoke-edit="EDT-0123456789ab"]').wait_for()
        self.assertEqual([call["url"].rsplit("/", 1)[-1] for call in self.calls], ["approval"])

    def test_review_shows_the_result_and_the_finishing_tools_together(self):
        self.skill_delivery()
        self.payload["job"]["takes"] = [self.take("TAK-0123456789ab", GOOD)]
        page = self.open_page(width=1280)
        self.assertEqual(self.stage(page), "review")
        self.assertTrue(page.locator("#edit-panel").is_visible())
        panel = page.locator("#video-publication-panel")
        self.assertEqual(panel.get_attribute("data-viewing"), "cut")
        self.assertFalse(page.locator("#video-publication-panel").evaluate("node => node.hidden"))
        self.assertTrue(page.evaluate("document.querySelector('.production-review').contains(document.querySelector('#edit-panel')) && "
                                      "document.querySelector('.production-review').contains(document.querySelector('#video-publication-panel'))"))
        page.locator('[data-stage="publish"]').click()
        self.assertEqual(panel.get_attribute("data-viewing"), "publish")
        self.assertFalse(page.locator("#edit-panel").is_visible())

    def test_step_links_land_on_their_part_of_a_stage(self):
        self.built()
        page = self.open_page()
        for step, stage, panel in (("timeline", "prep", "#timeline-panel"), ("cut", "review", "#video-publication-panel"),
                                   ("publish", "publish", "#video-publication-panel"), ("lines", "prep", "#job-script-panel"),
                                   ("edit", "review", "#edit-panel")):
            page.evaluate("hash => { location.hash = hash; }", f"#production/{JOB_ID}?step={step}")
            page.locator(f'.production-steps [data-stage="{stage}"][aria-selected="true"]').wait_for()
            self.assertFalse(page.locator(panel).evaluate("node => node.hidden"), step)
        # The run log and the related links read last, below the working area.
        self.assertTrue(page.evaluate("""() => {
          const steps = document.querySelector('.production-steps').getBoundingClientRect().bottom;
          const shown = [...document.querySelectorAll('#production-activity-logs, #workflow-links')].filter(node => node.offsetParent !== null);
          return shown.length > 0 && shown.every(node => node.getBoundingClientRect().top >= steps);
        }"""))


if __name__ == "__main__":
    unittest.main()
