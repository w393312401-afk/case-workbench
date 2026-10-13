"""Serve the real activity assets against an isolated temporary workbench."""
import unittest
from playwright.sync_api import expect

import test_ui_regressions as browser
import test_server as fixture


class ActivityIntegrationBrowserTests(unittest.TestCase):
    setUp = browser.WorkbenchBrowserTests.setUp
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video
    open_page = browser.WorkbenchBrowserTests.open_page

    def test_real_header_feed_logs_settings_and_mobile_layout(self):
        activity = self.server.app.activity
        activity.record("ai:fixture:round1", kind="ai", owner_id="AIT-fixture", source_id="round1", product="202H",
                        title="测试写稿第一轮", status="stopping", phase="stopping", message="正在安全停止。")
        activity.record("edit:fixture", kind="edit", owner_id="PROD-fixture", source_id="edit-fixture", product="202H",
                        title="测试剪辑", status="completed", phase="finished", message="剪辑已结束，待人工审查。")
        page = self.open_page("#/202H/library")
        expect(page.locator("#task-activity-open")).to_have_attribute("aria-label", "任务，1 项进行中，1 条未读")
        page.locator("#task-activity-open").click()
        expect(page.locator(".activity-row")).to_have_count(2)
        page.get_by_role("button", name="进行中", exact=True).click()
        expect(page.locator(".activity-row")).to_have_count(1)
        expect(page.locator(".activity-status")).to_have_text("正在停止")
        page.get_by_role("button", name="全部", exact=True).click()
        page.locator('[data-activity-log="edit:fixture"]').click()
        expect(page.locator("#activity-log-body .activity-log-lines li")).to_have_count(1)
        expect(page.locator("#activity-log-body")).to_contain_text("执行结束")
        expect(page.locator("#activity-log-body")).to_contain_text("待人工审查")
        page.get_by_role("button", name="关闭运行日志", exact=True).click()
        page.get_by_role("button", name="关闭任务面板", exact=True).click()
        page.set_viewport_size({"width": 390, "height": 844})
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)
        button_box = page.locator("#task-activity-open").bounding_box()
        product_box = page.locator("#global-product").bounding_box()
        # The task button shares the single phone header row, to the right of the product picker.
        self.assertGreaterEqual(button_box["x"], product_box["x"] + product_box["width"])
        self.assertLess(abs(button_box["y"] + button_box["height"] / 2 - product_box["y"] - product_box["height"] / 2), 4)
        page.locator("#task-activity-open").click()
        expect(page.locator("#task-activity-dialog")).to_be_visible()
        self.assertTrue(page.evaluate("document.querySelector('#task-activity-dialog').scrollWidth <= document.querySelector('#task-activity-dialog').clientWidth"))
        page.keyboard.press("Escape")
        page.evaluate("location.hash = WorkbenchShell.url('#settings')")
        expect(page.locator("#settings-activity-heading")).to_be_visible()
        expect(page.locator("#settings-activity-status")).to_contain_text("未开启")


if __name__ == "__main__":
    unittest.main()
