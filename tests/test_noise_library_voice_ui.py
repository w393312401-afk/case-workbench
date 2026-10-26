"""Noise regressions with inert APIs and synthetic records; never opens the real library."""
from pathlib import Path
import os
import unittest

from playwright.sync_api import sync_playwright
import test_settings_ui as settings_fixture
import test_review_opportunities_ui as opportunities_fixture

ROOT = Path(__file__).resolve().parents[1]
PATHS = {key: f"C:/fixture/{key}" for key in ("records", "directions", "creation", "evidence", "library")}

def capture_fixture(page, name, selector=None):
    if os.environ.get("CASE_NOISE_UI_CAPTURE") != "1":
        return
    folder=ROOT/"reports/information-noise-implementation-2026-10-23/fixture-screenshots"
    folder.mkdir(parents=True,exist_ok=True)
    if selector:
        page.locator(selector).screenshot(path=str(folder/name))
    else:
        page.screenshot(path=str(folder/name),full_page=False)


class NoiseSettingsTests(unittest.TestCase):
    setUpClass = classmethod(settings_fixture.SettingsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(settings_fixture.SettingsBrowserTests.tearDownClass.__func__)
    open_page = settings_fixture.SettingsBrowserTests.open_page

    def test_direct_settings_read_hydrates_paths_without_replacing_unsaved_form(self):
        page = self.open_page({"/api/settings/paths": {"deferred_fixture": True}})
        page.evaluate("state.paths={};WorkbenchSettings.render()")
        page.wait_for_function("window.deferredRequests.length === 2")
        self.assertEqual(page.locator('[data-location][data-state="loading"]').count(), 5)
        self.assertNotIn("读取失败", page.locator(".settings-locations").inner_text())
        page.locator("#settings-backup-directory").fill("D:/unsaved-choice")
        page.evaluate("paths=>window.deferredRequests[1].resolve({paths})", PATHS)
        page.wait_for_function("document.querySelectorAll('[data-location][data-state=ready]').length === 5")
        self.assertEqual(page.locator("#settings-backup-directory").input_value(), "D:/unsaved-choice")
        capture_fixture(page,"settings-paths-fixture.png","#settings-group-files")
        page.locator('[data-settings-copy="directions"]').click()
        self.assertIn({"copy": PATHS["directions"]}, page.evaluate("window.actions"))
        # A late read from an obsolete rendering cannot replace the current paths.
        page.evaluate("window.deferredRequests[0].resolve({paths:{records:'C:/old'}})")
        self.assertIn(PATHS["records"], page.locator('[data-location="records"]').inner_text())

    def test_missing_configuration_failure_and_retry_are_different_states(self):
        page = self.open_page({"/api/settings/paths": {"paths": {**PATHS, "creation": None}}})
        self.assertEqual(page.locator('[data-location="creation"]').get_attribute("data-state"), "unconfigured")
        self.assertIn("未配置", page.locator('[data-location="creation"]').inner_text())
        page.evaluate("state.paths={};window.responses['/api/settings/paths']={error_fixture:'fixture unavailable'};WorkbenchSettings.render()")
        self.assertEqual(page.locator('[data-location][data-state="failed"]').count(), 5)
        self.assertIn("fixture unavailable", page.locator("[data-location-error]").inner_text())
        page.evaluate("paths=>window.responses['/api/settings/paths']={paths}", PATHS)
        page.get_by_role("button", name="重新读取路径").click()
        self.assertEqual(page.locator('[data-location][data-state="ready"]').count(), 5)
        self.assertTrue(page.locator("[data-location-error]").is_hidden())

    def test_complete_cached_paths_can_survive_api_failure(self):
        page = self.open_page({"/api/settings/paths": {"paths": PATHS}})
        page.evaluate("paths=>{state.paths=paths;window.responses['/api/settings/paths']={error_fixture:'offline'};WorkbenchSettings.render()}", PATHS)
        self.assertEqual(page.locator('[data-location][data-state="ready"]').count(), 5)
        self.assertTrue(page.locator("[data-location-error]").is_hidden())


class NoiseOpportunityTests(unittest.TestCase):
    setUpClass = classmethod(opportunities_fixture.ReviewOpportunityBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(opportunities_fixture.ReviewOpportunityBrowserTests.tearDownClass.__func__)
    render = opportunities_fixture.ReviewOpportunityBrowserTests.render

    def setUp(self):
        opportunities_fixture.ReviewOpportunityBrowserTests.setUp(self)
        for name in ("style.css", "reviews-layout.css", "review-opportunities.css"):
            self.page.add_style_tag(path=str(ROOT/"static"/name))
        # This isolated page has no navigation shell or real writing dialog.
        self.page.add_style_tag(content="main{margin-left:0}#review-opportunities{padding:12px}#ai-create-instruction{display:none}")

    def test_zero_recent_is_not_repeated_and_unclassified_is_an_evidence_pool(self):
        self.item.update(topic="none", id="topic:unclassified", title="未归类", recent_reviews=0,
                         priority_reasons=["近 14 天首次入库 0 种不同原文", "尚未关联方向"])
        self.render()
        self.assertEqual(self.page.locator(".opportunity-grid .opportunity-card").count(), 0)
        self.assertEqual(self.page.locator(".opportunity-pool .opportunity-card").count(), 1)
        self.assertNotIn("近 14 天新增 0", self.page.locator("#opportunity-results").inner_text())
        self.assertNotIn("首次入库 0", self.page.locator("#opportunity-results").inner_text())
        self.assertIn("待整理证据池", self.page.locator(".opportunity-pool").inner_text())
        self.page.get_by_role("button", name="带证据写稿").click()
        self.assertEqual(self.page.locator("#ai-create-instruction").input_value(), self.item["draft_instruction"])

    def test_long_quote_keeps_identity_source_and_full_original(self):
        original = "A verified buyer asks about the clamp. " * 80
        self.item["evidence"][0].update(text=original, url="https://example.test/review/abc", role_label="试用/有激励买家")
        self.render()
        self.assertIn("试用/有激励买家", self.page.locator(".opportunity-evidence>summary").inner_text())
        self.assertLessEqual(self.page.locator(".opportunity-excerpt").bounding_box()["height"], 75)
        capture_fixture(self.page,"opportunity-mobile-fixture.png","#review-opportunities")
        self.page.locator(".opportunity-full>summary").click()
        self.assertEqual(self.page.locator(".opportunity-full blockquote").inner_text(), original)
        self.assertTrue(self.page.locator(".opportunity-excerpt").is_hidden())
        self.assertEqual(self.page.get_by_role("link", name="打开原帖").get_attribute("href"), "https://example.test/review/abc")
        self.assertEqual(self.page.evaluate("window.starts"), [])


class NoiseCaseReadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        context = self.browser.new_context(viewport={"width":1280,"height":800})
        self.addCleanup(context.close)
        self.page = context.new_page()
        self.page.set_content('<main id="content"></main><div id="detail-other"></div>')
        for name in ("style.css", "library.css", "detail.css"):
            self.page.add_style_tag(path=str(ROOT/"static"/name))
        self.page.add_script_tag(content=r'''
          const $=(q,root=document)=>root.querySelector(q), $$=(q,root=document)=>[...root.querySelectorAll(q)];
          const state={selection:new Set(),selecting:false,sort:'updated',sortDir:'desc'};
          const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
          const icon=()=>'',displayValue=x=>esc(x??'—'),shortTitle=i=>i.current.title,readableDate=x=>x||'';
          const caseIdentity=i=>i.current.metrics||{},salesMetrics=i=>i.sales_data?.metrics||{},salesEvidence=i=>i.sales_data?.evidence||{};
          const badge=i=>`<span class="badge">${esc(i.current.category)}</span>`,archiveBadge=()=>'<span>已归档</span>',sourceBadges=()=>'<span class="source-type">历史视频拆解</span><span class="source-tier">历史T3</span>';
          const directionLabel=i=>i.current.test_direction?.main_difference||'—',directionChain=()=>'',salesStatusLabel=()=> '未匹配';
          const salesDataURL=()=> '#videos',metrics=[['views_display','播放量'],['ctor_display','CTOR'],['items_sold','售出']];
          const mediaURL=(id,kind,event)=>`/fixture/${id}/${kind}/${event||''}`;
          const renderTestDirection=()=>'<section class="section-card">方向内容</section>';
          const transcriptDisplay=i=>({heading:'完整文案',text:i.current.transcript?.text||'',status:'机器转写 · 未人工听校'});
          const historicalTranscriptStatus=x=>x||'机器转写 · 未人工听校';
          const kindNames={editorial:'内容判断'},assetLabels={video:'视频'};
          window.item={case_id:'PVR-123456abcdef',product:'202H',updated_at:'2026-10-23',created_at:'2026-10-01',original:{text:'original source'},events:[],current:{title:'A synthetic reference',category:'待验证与整理',conclusion:'历史播放 100',metrics:{video_id:'7000000000000000001'},transcript:{text:'spoken words',status:'未人工听校'}}};
        ''')
        for name in ("library.js", "detail.js", "native.js"):
            self.page.add_script_tag(path=str(ROOT/"static"/name))

    def test_selection_column_exists_only_in_selection_mode(self):
        self.page.evaluate("document.querySelector('#content').innerHTML=listView([window.item])")
        self.assertEqual(self.page.locator(".case-selection-column").count(), 0)
        self.page.locator(".table-case-identity>summary").click()
        self.assertIn("7000000000000000001", self.page.locator(".table-case-identity").inner_text())
        self.page.evaluate("state.selecting=true;document.querySelector('#content').innerHTML=listView([window.item])")
        self.assertEqual(self.page.locator(".case-selection-column").count(), 2)
        self.assertTrue(self.page.locator("[data-case-select]").is_visible())
        self.page.locator("[data-case-select]").check()
        self.assertTrue(self.page.locator("[data-case-select]").is_checked())
        capture_fixture(self.page,"case-list-selection-fixture.png")

    def test_completed_placeholder_is_neutral_without_changing_saved_judgment(self):
        result = self.page.evaluate('''() => {
          Object.assign(item.current,{category:'批量收录 · 待拆解',conclusion:'从视频数据批量收录：累计 10 单、107,867 播放；拆解完成前不作结论。',source_case:{file_path:'fixture.md'}});
          const before=JSON.stringify(item);const reading=caseReadingState(item),html=card(item);
          const unchanged=before===JSON.stringify(item);item.current.conclusion='人工判断仍需验证';
          return {reading,html,unchanged,human:caseReadingState(item)};
        }''')
        self.assertTrue(result["reading"]["neutral"])
        self.assertTrue(result["unchanged"])
        self.assertIn("已有拆解", result["html"])
        self.assertFalse(result["human"]["neutral"])
        self.assertEqual(result["human"]["conclusion"], "人工判断仍需验证")

    def test_public_counts_and_historical_conclusion_keep_different_provenance(self):
        self.page.evaluate('''() => {
          item.sales_data={status:'matched',video:{views_source:'public'},metrics:{views_display:200,ctor_display:'1.12%',items_sold:25},evidence:{window:'10-01—10-18',as_of:'10-18'}};
          document.querySelector('#content').innerHTML=renderDetailSections(item,'current');
        }''')
        self.assertIn("历史播放 100", self.page.locator(".conclusion").inner_text())
        self.assertIn("播放来自公开页", self.page.locator(".case-summary-label").inner_text())
        self.assertIn("200", self.page.locator(".case-summary-metrics").inner_text())
        self.assertIn("10-01—10-18", self.page.locator(".case-summary-item").inner_text())
        self.page.locator(".consistency-note>summary").click()
        self.assertIn("不一定是错误", self.page.locator(".consistency-note").inner_text())
        self.assertEqual(self.page.locator("#detail-section-purchase-reason,#detail-section-test-direction").count(), 0)
        self.assertEqual(self.page.locator('[data-inline-section="direction"]:empty').count(), 1)
        capture_fixture(self.page,"case-data-provenance-fixture.png")

    def test_history_is_compact_until_open_and_preserves_revision_raw_record(self):
        self.page.evaluate('''() => {item.events=[{created_at:'2026-10-22',event_id:'one',feedback:{kind:'editorial',text:'Original review text'},current:{title:'Old title',conclusion:'Old decision',transcript:{text:'old spoken words'},assets:{video:'fixture'}}}];renderHistory(item);}''')
        self.assertEqual(self.page.locator(".history-item[open]").count(), 0)
        self.page.locator(".history-item>summary").first.click()
        self.assertIn("Original review text", self.page.locator(".history-expanded").first.inner_text())
        self.page.get_by_text("查看当时的当前版本", exact=True).click()
        self.assertIn("Old decision", self.page.locator(".history-expanded").first.inner_text())
        self.page.get_by_text("完整修订记录", exact=True).click()
        self.assertIn('"event_id": "one"', self.page.locator(".history-item").first.locator("pre").last.inner_text())

    def test_comparison_starts_with_metrics_and_keeps_window_visible(self):
        self.page.add_script_tag(content='''
          window.productFilterGroup=i=>i.product;window.contentOriginLabel=()=>"自制内容参考";
          window.scriptSentences=text=>[text];window.transcriptComparison=()=>[];
          window.api=async path=>structuredClone(state.cases.find(i=>path.endsWith(i.case_id)));
        ''')
        self.page.evaluate('''async () => {
          const second=structuredClone(item);second.case_id='PVR-abcdef123456';
          item.sales_data={status:'matched',metrics:{views_display:200,ctor_display:'1%',items_sold:2},evidence:{window:'10-01—10-18',as_of:'10-18'}};
          second.sales_data=structuredClone(item.sales_data);second.sales_data.metrics.views_display=300;
          state.cases=[item,second];state.selection=new Set(state.cases.map(i=>i.case_id));
          window.beforeComparison=JSON.stringify(state.cases);await openComparison();
        }''')
        self.assertEqual(self.page.locator("#case-comparison tbody tr:not([hidden])>th").first.inner_text(), "播放量")
        self.assertTrue(self.page.locator("#case-comparison tr", has=self.page.get_by_text("统计窗口",exact=True)).is_visible())
        self.assertGreater(self.page.locator("[data-comparison-common-row][hidden]").count(),0)
        self.page.locator("[data-comparison-common]").click()
        self.assertEqual(self.page.locator("[data-comparison-common-row][hidden]").count(),0)
        self.assertTrue(self.page.evaluate("beforeComparison===JSON.stringify(state.cases)"))


if __name__ == "__main__":
    unittest.main()
