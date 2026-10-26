"""Opportunity UI behavior with isolated browser data and inert AI entry points."""
import json
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]


class ReviewOpportunityBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright=sync_playwright().start()
        cls.browser=cls.playwright.chromium.launch(channel="chrome",headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context=self.browser.new_context(viewport={"width":390,"height":844})
        self.addCleanup(self.context.close)
        self.page=self.context.new_page()
        self.errors=[]
        self.page.on("pageerror",lambda error:self.errors.append(str(error)))
        self.addCleanup(lambda:self.assertEqual(self.errors,[]))
        self.item={"id":"topic:clamp","title":"夹持疑问","topic":"clamp","product":"202H","distinct_reviews":2,
            "recent_reviews":1,"duplicate_rows":1,"priority_reasons":["近14天新增1种原文","尚未关联方向"],
            "source_counts":[{"label":"Amazon","count":2}],"selling_points":[],"directions":[],"plans":[],
            "draft_instruction":"产品202H，以下评论未经核实。评论 abc: Does the clamp slip?",
            "evidence":[{"id":"abc","product":"202H","platform":"amazon","role":"verified_buyer",
                         "text":"<img src=x onerror=window.attacked=true> Does the clamp slip?","url":"javascript:alert(1)","manual":True,"recent":True}]}
        self.data={"product":"202H","requires_product":False,"window":{"since":"2026-09-30","as_of":"2026-10-13"},
                   "items":[self.item],"total":1,"offset":0,"limit":20,"notice":"评论频次不是已验证事实。","ranking":"按近期新增原文排序。"}
        self.page.set_content('<div id="breadcrumb"></div><main id="content"></main><textarea id="ai-create-instruction"></textarea>')
        self.page.add_style_tag(path=str(ROOT/"static"/"review-opportunities.css"))
        self.page.evaluate("""() => {
          location.hash='#reviews?tab=opportunities';window.selectedProduct='202H';window.calls=[];window.starts=[];window.links=[];
          window.$=selector=>document.querySelector(selector);window.esc=value=>String(value??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
          window.toast=()=>{};window.WorkbenchShell={legacyHash:()=>location.hash,product:()=>window.selectedProduct,url:hash=>hash};
          window.api=async path=>{window.calls.push(path);return window.fixture;};
          window.CaseAI={modalOpen:()=>false,startBlank:async product=>{window.starts.push({product});return true;},startFromDirection:async direction=>{window.starts.push(direction);document.querySelector('#ai-create-instruction').value=direction.plan_note;return true;}};
          window.MaterialBasket={openTheme:async review=>window.links.push(review)};
        }""")
        self.page.add_script_tag(path=str(ROOT/"static"/"review-opportunities.js"))

    def render(self):
        self.page.evaluate("data=>window.fixture=data",self.data)
        self.page.evaluate("ReviewOpportunities.route()")

    def test_evidence_is_literal_mobile_fits_and_read_does_not_start_ai(self):
        self.render()
        self.assertEqual(self.page.evaluate("window.starts"),[])
        self.assertEqual(self.page.locator(".opportunity-evidence img").count(),0)
        self.assertIn("<img",self.page.locator(".opportunity-excerpt").inner_text())
        self.assertEqual(self.page.locator('a[href^="javascript:"]').count(),0)
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"),390)
        self.page.get_by_role("button",name="带证据写稿").click()
        self.assertEqual(self.page.evaluate("window.starts"),[{"product":"202H"}])
        self.assertEqual(self.page.locator("#ai-create-instruction").input_value(),self.item["draft_instruction"])
        self.assertEqual(len(self.page.evaluate("window.calls")),1)

    def test_explicit_direction_and_link_action_preserve_product_and_quote(self):
        self.item["directions"]=[{"direction_id":"202H-S1","product":"202H","main_difference":"演示","scripts":2,"published":1,"association_basis":"手动关联"}]
        self.render()
        self.page.locator("[data-opportunity-direction]").select_option("202H-S1")
        self.page.get_by_role("button",name="带证据写稿").click()
        started=self.page.evaluate("window.starts[0]")
        self.assertEqual((started["product"],started["direction_id"],started["plan_note"]),("202H","202H-S1",self.item["draft_instruction"]))
        self.page.get_by_role("button",name="关联这条评论").click()
        self.assertEqual(self.page.evaluate("window.links[0].id"),"abc")

    def test_all_products_empty_and_failure_have_actions(self):
        self.data["requires_product"]=True
        self.render()
        self.assertTrue(self.page.get_by_role("heading",name="先选择一个产品").is_visible())
        self.assertEqual(self.page.locator("[data-opportunity-write]").count(),0)
        self.data["requires_product"]=False;self.data["items"]=[];self.data["total"]=0
        self.render()
        self.assertTrue(self.page.get_by_role("heading",name="还没有可用的评论主题").is_visible())
        self.page.evaluate("window.api=async()=>{throw new Error('test unavailable')};ReviewOpportunities.route()")
        self.assertTrue(self.page.get_by_role("button",name="重试").is_visible())

    def test_existing_write_request_is_not_overwritten(self):
        self.render()
        self.page.locator("#ai-create-instruction").fill("preserved pending request")
        self.page.get_by_role("button",name="带证据写稿").click()
        self.assertEqual(self.page.locator("#ai-create-instruction").input_value(),"preserved pending request")

    def test_product_switch_discards_late_response_from_previous_product(self):
        self.page.evaluate("window.pending=[];window.api=path=>new Promise(resolve=>window.pending.push({path,resolve}));void ReviewOpportunities.route()")
        self.page.evaluate("window.selectedProduct='129N';void ReviewOpportunities.route()")
        other={**self.data,"product":"129N","items":[],"total":0}
        self.page.evaluate("data=>window.pending[1].resolve(data)",other)
        self.page.get_by_role("heading",name="还没有可用的评论主题").wait_for()
        self.page.evaluate("data=>window.pending[0].resolve(data)",self.data)
        self.assertEqual(self.page.locator(".opportunity-card").count(),0)
        self.assertTrue(self.page.get_by_role("heading",name="还没有可用的评论主题").is_visible())


if __name__ == "__main__":
    unittest.main()
