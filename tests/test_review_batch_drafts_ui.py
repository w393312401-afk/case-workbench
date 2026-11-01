"""Batch drafting follows list filters and safely retries an uncertain submission."""
import copy
from pathlib import Path
import unittest

import test_review_ui as fixture
import test_feedback_chain as feedback
from test_ai_tasks import FakeBridge

ARTIFACTS = Path(__file__).resolve().parents[1] / ".test-artifacts" / "review-batch-drafts"


class ReviewBatchDraftBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ReviewPageTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ReviewPageTests.tearDownClass.__func__)
    stop_server = fixture.ReviewPageTests.stop_server
    request = fixture.ReviewPageTests.request
    open_page = fixture.ReviewPageTests.open_page
    no_download = staticmethod(feedback.FeedbackChainHTTPTests.no_download)

    def setUp(self):
        fixture.ReviewPageTests.setUp(self)
        self.server.app.ai.bridge = FakeBridge(output="结论：人群：磨刀用户；S：无；A：无；B：无\n参考价值：弱（用于下一条）")
        self.server.app.draft_downloader = self.no_download

    def open_list(self, query="", width=1080):
        return self.open_page(width=width, route="#/202H/videos?tab=list" + query, ready="[data-batch-draft]")

    def test_batch_uses_current_account_filter_and_keeps_reviews_unsaved(self):
        page = self.open_list("&account=sharpal_us", width=390)
        button = page.locator("[data-batch-draft]")
        self.assertIn("2 条", button.inner_text())
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        button.evaluate("node => node.scrollIntoView({block:'center'})")
        page.locator(".perf-status-card").screenshot(path=str(ARTIFACTS / "batch-draft-light-390.png"))
        page.emulate_media(color_scheme="dark")
        page.locator(".perf-status-card").screenshot(path=str(ARTIFACTS / "batch-draft-dark-390.png"))
        with page.expect_response(lambda response: response.url.endswith("/api/performance/batch-draft") and response.request.method == "POST") as response:
            button.click()
        payload = response.value.request.post_data_json
        self.assertEqual({"7600000000000000002", "7600000000000000004"}, {row["video_id"] for row in payload["videos"]})
        self.assertEqual("local", payload["shop"])
        page.wait_for_function("() => document.querySelector('[data-batch-draft]')?.disabled === true")
        self.assertEqual(2, len(self.server.app.ai.list_tasks()))
        self.assertEqual([], self.server.app.review_followups.public()["entries"])
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))

    def test_response_lost_after_creation_retries_exact_request_and_no_duplicate(self):
        page = self.open_list("&account=sharpal_us")
        submitted = []

        def intercept(route):
            submitted.append(copy.deepcopy(route.request.post_data_json))
            result = route.fetch()
            if len(submitted) == 1:
                route.abort("failed")
            else:
                route.fulfill(response=result)

        page.route("**/api/performance/batch-draft", intercept)
        page.locator("[data-batch-draft]").click()
        retry = page.locator("[data-batch-draft]")
        page.wait_for_function("() => document.querySelector('[data-batch-draft]')?.textContent.includes('重试')")
        page.locator("#perf-account").select_option("bushcraftedge")
        page.wait_for_function("() => document.querySelector('[data-batch-draft]')?.textContent.includes('重试')")
        self.assertEqual("bushcraftedge", page.locator("#perf-account").input_value())
        with page.expect_response(lambda response: response.url.endswith("/api/performance/batch-draft") and response.request.method == "POST"):
            retry.click()
        page.wait_for_function("() => document.querySelector('[data-batch-draft]')?.textContent.startsWith('AI 批量起草')")
        self.assertEqual(2, len(submitted))
        self.assertEqual(submitted[0], submitted[1])
        self.assertEqual(2, len(self.server.app.ai.list_tasks()))
        self.assertEqual([], self.server.app.review_followups.public()["entries"])

    def test_candidates_beyond_the_200_visible_rows_are_included_and_capped_at_ten(self):
        page = self.open_list()
        _, _, payload = self.request("GET", "/api/performance/videos?shop=local")
        base = payload["videos"][0]
        payload["videos"] = [{**copy.deepcopy(base), "video_id": str(7800000000000000000 + index),
                              "title": f"Sample {index}", "headline": f"Sample {index}",
                              "review_draft": {"eligible": index >= 200, "reason": ""}}
                             for index in range(215)]
        submitted = []
        page.route("**/api/performance/videos?*", lambda route: route.fulfill(json=payload))

        def submit(route):
            body = route.request.post_data_json
            submitted.append(body)
            route.fulfill(json={"results": [{**row, "status": "queued", "task_id": "AIT-fixture"} for row in body["videos"]], "replayed": False})

        page.route("**/api/performance/batch-draft", submit)
        page.reload()
        page.wait_for_function("() => document.querySelectorAll('[data-video-row]').length === 200")
        self.assertIn("10 条", page.locator("[data-batch-draft]").inner_text())
        with page.expect_request(lambda request: request.url.endswith("/api/performance/batch-draft")):
            page.locator("[data-batch-draft]").click()
        self.assertEqual(10, len(submitted[0]["videos"]))
        self.assertTrue(all(int(row["video_id"]) >= 7800000000000000200 for row in submitted[0]["videos"]))

    def test_batch_completion_during_shop_switch_keeps_the_new_shop_load(self):
        page = self.open_list("&account=sharpal_us")
        held_batch, held_cross = [], []

        def batch(route):
            status, _, result = self.request("POST", "/api/performance/batch-draft", route.request.post_data_json)
            self.assertEqual(200, status)
            held_batch.append((route, result))

        def cross(route):
            status, _, result = self.request("GET", "/api/performance/videos?shop=cross")
            self.assertEqual(200, status)
            held_cross.append((route, result))

        page.route("**/api/performance/batch-draft", batch)
        page.route("**/api/performance/videos?shop=cross", cross)
        with page.expect_request(lambda request: request.url.endswith("/api/performance/batch-draft")):
            page.locator("[data-batch-draft]").click()
        page.wait_for_function("() => document.querySelector('[data-batch-draft]')?.textContent.includes('正在提交')")
        with page.expect_request(lambda request: request.url.endswith("/api/performance/videos?shop=cross")):
            page.locator('[data-perf-shop="cross"]').click()
        page.wait_for_function("() => location.hash.includes('shop=cross')")
        self.assertEqual(1, len(held_batch))
        self.assertEqual(1, len(held_cross))
        held_batch[0][0].fulfill(json=held_batch[0][1])
        page.locator("#toast").filter(has_text="已排队起草").wait_for()
        held_cross[0][0].fulfill(json=held_cross[0][1])
        page.locator('[data-perf-shop="cross"].is-active').wait_for()
        self.assertEqual(1, len(held_cross), "Completing the old batch must not replace the new shop's initial load")
        self.assertNotIn("带货数据暂不可用", page.locator("#content").inner_text())


if __name__ == "__main__":
    unittest.main()
