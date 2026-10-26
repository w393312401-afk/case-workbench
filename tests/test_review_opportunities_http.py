"""Real HTTP/page wiring; all records and requests stay in disposable workbench fixtures."""
import unittest
import json

import test_server as fixture
from test_review_store import build_sources
import test_review_ui as reviews_ui


class ReviewOpportunityHTTPTests(unittest.TestCase):
    stop_server=fixture.WorkbenchHTTPTests.stop_server
    request=fixture.WorkbenchHTTPTests.request

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        build_sources(self.runtime/"reviews"/"sources")
        self.server.app.reviews.sync()

    def test_product_pagination_and_invalid_requests(self):
        status,_,data=self.request("GET","/api/reviews/opportunities?product=202H&limit=1")
        self.assertEqual(status,200,data)
        self.assertGreater(data["total"],1)
        self.assertEqual(len(data["items"]),1)
        self.assertTrue(all(row["product"]=="202H" for row in data["items"][0]["evidence"]))
        self.assertTrue(self.request("GET","/api/reviews/opportunities")[2]["requires_product"])
        self.assertEqual(self.request("GET","/api/reviews/opportunities?product=129N")[2]["items"],[])
        self.assertEqual(self.request("GET","/api/reviews/opportunities?limit=not-a-number")[0],400)
        self.assertEqual(self.request("GET","/api/reviews/opportunities?offset=-1")[0],400)


class ReviewOpportunityPageTests(unittest.TestCase):
    setUpClass=classmethod(reviews_ui.ReviewPageTests.setUpClass.__func__)
    tearDownClass=classmethod(reviews_ui.ReviewPageTests.tearDownClass.__func__)
    setUp=ReviewOpportunityHTTPTests.setUp
    stop_server=fixture.WorkbenchHTTPTests.stop_server
    request=fixture.WorkbenchHTTPTests.request

    def test_comment_library_entry_opens_evidence_and_real_ai_dialog_without_submitting(self):
        context=self.browser.new_context(viewport={"width":1200,"height":900})
        self.addCleanup(context.close)
        page=context.new_page()
        page.route("**/api/ai/status",lambda route:route.fulfill(content_type="application/json",body=json.dumps({"available":True})))
        page.route("**/api/ai/options*",lambda route:route.fulfill(content_type="application/json",body=json.dumps({"models":[],"skills":[],"default_settings":{}})))
        errors=[];writes=[]
        page.on("pageerror",lambda error:errors.append(str(error)))
        page.on("request",lambda request:writes.append(request.url) if request.method=="POST" and request.url.endswith("/api/ai/tasks") else None)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/reviews")
        page.get_by_role("link",name="评论选题机会",exact=True).click()
        page.locator(".opportunity-card").first.wait_for()
        self.assertIn("tab=opportunities",page.url)
        page.locator("[data-opportunity-write]").first.click()
        page.locator("#ai-create-dialog[open]").wait_for()
        page.wait_for_function("() => document.querySelector('#ai-create-instruction').value.includes('评论原文')")
        instruction=page.locator("#ai-create-instruction").input_value()
        self.assertIn("产品：202H",instruction)
        self.assertIn("未经核实",instruction)
        self.assertEqual(writes,[])
        self.assertEqual(errors,[])


if __name__ == "__main__":
    unittest.main()
