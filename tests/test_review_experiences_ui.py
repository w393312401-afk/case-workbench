"""Experience extraction and reuse controls, with an isolated in-memory API."""
from pathlib import Path
import tempfile
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


class ReviewExperiencesUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def fixture(self, width=1080, scheme="light"):
        context = self.browser.new_context(viewport={"width": width, "height": 1000}, color_scheme=scheme)
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(5000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.route("**/*", lambda route: route.fulfill(content_type="text/html", body='<main id="panel"></main>'))
        page.goto("http://127.0.0.1:43220/experience-test")
        for name in ("style.css", "home.css", "feedback-chain.css", "review-experiences.css"):
            page.add_style_tag(path=str(ROOT / "static" / name))
        page.evaluate("""() => {
          window.esc = value => String(value ?? '').replace(/[&<>"']/g,
            c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
          window.messages = [];window.toast = text => messages.push(text);
          window.product = '202H';window.WorkbenchShell = {
            product:() => product,url:(hash,product) => hash + '&product=' + encodeURIComponent(product)};
          window.requests = [];window.gets = [];window.revision = 'r1';window.saves = 0;
          window.source = {product:'202H',video_id:'7691330683482017055',window_end:'2026-10-20',
            reviewed_at:'2026-10-24T12:00:00',note:'先展示关灯后仍然清楚的效果。'};
          window.chain = {product:'202H',video_id:source.video_id,window_end:source.window_end,
            current_window:true,review_revision:'review-r1',review:{state:'reviewed',outcome:'positive',
              evidence:'preliminary',note:source.note,at:source.reviewed_at,decision:'scale'},source_review:source,
            summary:{direction_id:'202H-S2',posted_on:'2026-10-01',metrics:{views:1500,orders:3}},
            draft:{status:'completed',created_at:'2026-10-25',draft:{conclusion:'未经确认的 AI 新草稿'}},
            retrospectives:[],next_tasks:[],plans:[],source_scripts:[],next_scripts:[],
            source_outputs:[],next_outputs:[],variants:[],cases:[],experiences:[],
            review_context:{observation:{observing:false,coverage_complete:true},evidence:{key:'preliminary'},closing_allowed:true}};
          window.item = {id:'EXP-aabbccddeeff',version:1,product:'202H',kind:'success',dimension:'证明',
            practice:'先展示关灯后的效果 <img src=x onerror=alert(1)>',conditions:'用于暗光演示，保持相同拍摄距离。',
            evidence_note:'同一方向两条视频；成交样本仍少。',confidence:'preliminary',scope:'direction',
            direction_id:'202H-S2',status:'active',source_status:'current',source:{review:source,
              video:{headline:'暗光实拍',account:'@demo',posted_on:'2026-10-01',last_data:'2026-10-20',
                captured_at:'2026-10-25T12:00:00',data_status:'available',metrics:{views:1500,orders:3}},
              publications:[{script_id:'SCR-1',script_version:2}]},history:[],created_at:'2026-10-25',updated_at:'2026-10-25'};
          window.items = [item];
          window.baseAPI = async (path,options={}) => {
            if (path.startsWith('/api/feedback-chain')) return structuredClone(chain);
            if (options.method === 'POST') {
              requests.push(structuredClone(options.body));
              const old = items.find(row => row.id === options.body.experience.id);
              const saved = {...(old || item),...options.body.experience,id:old?.id || 'EXP-001122334455',version:(old?.version || 0)+1};
              items = old ? items.map(row => row.id === old.id ? saved : row) : [...items,saved];
              revision = 'r' + (requests.length+1);return {item:saved,revision};
            }
            gets.push(path);const query = new URL(path,location.origin).searchParams;
            const filtered = items.filter(row => (!query.get('product') || row.product === query.get('product'))
              && (!query.get('kind') || row.kind === query.get('kind'))
              && (!query.get('status') || query.get('status') === 'all' || row.status === query.get('status'))
              && (!query.get('direction_id') || row.direction_id === query.get('direction_id'))
              && (!query.get('q') || JSON.stringify(row).includes(query.get('q'))));
            return {items:structuredClone(filtered),revision,total:filtered.length};
          };
          window.api = baseAPI;
        }""")
        page.add_script_tag(path=str(ROOT / "static" / "review-experiences.js"))
        return page

    def form(self, page):
        page.evaluate("ReviewExperiences.fromReview(chain,{onSaved:()=>saves++})")
        page.locator('.experience-form-dialog [type=submit]:not([disabled])').wait_for()
        return page.locator('.experience-form-dialog')

    def complete(self, form):
        form.locator('.experience-extra summary').click()
        form.locator('[name=conditions]').fill('仅适用于同一暗光演示场景，保持拍摄距离一致。')
        form.locator('[name=evidence_note]').fill('两条同方向视频有正向信号，成交样本仍少，需要复测。')

    def test_extracts_saved_review_with_separate_type_conditions_evidence_and_source(self):
        page = self.fixture()
        form = self.form(page)
        self.assertEqual(form.locator('[name=practice]').input_value(), page.evaluate('source.note'))
        self.assertNotIn('未经确认', form.inner_text())
        self.assertFalse(form.locator('[name=conditions]').is_visible())
        self.assertFalse(form.locator('[name=evidence_note]').is_visible())
        self.assertEqual(form.locator('[name=confidence], [name=scope], [name=direction_id], [name=dimension]').count(), 0)
        self.complete(form)
        form.locator('[name=kind][value=failure]').check()  # A local lesson can differ from overall outcome.
        form.locator('[type=submit]').click()
        page.wait_for_function('saves === 1')
        saved = page.evaluate('requests[0]')
        self.assertEqual(saved['source_review'], page.evaluate('source'))
        self.assertEqual(saved['experience']['kind'], 'failure')
        self.assertEqual(saved['experience']['direction_id'], '202H-S2')
        self.assertEqual(saved['experience']['confidence'], 'preliminary')
        self.assertEqual(saved['revision'], 'r1')
        self.assertNotIn('source', saved['experience'])

    def test_one_line_saves_with_optional_details_empty_and_source_attached(self):
        page = self.fixture()
        page.evaluate("chain.summary.direction_id='';chain.review.note='';source.note='';chain.review.outcome='unknown'")
        form = self.form(page)
        form.locator('[name=practice]').fill('先让用户看到结果。')
        form.locator('[type=submit]').click()
        self.assertEqual(page.evaluate('requests'), [])  # Choosing the lesson type is still deliberate.
        form.locator('[name=kind][value=success]').check()
        form.locator('[type=submit]').click()
        page.wait_for_function('saves === 1')
        saved = page.evaluate('requests[0]')
        self.assertEqual(saved['experience']['conditions'], '')
        self.assertEqual(saved['experience']['evidence_note'], '')
        self.assertEqual(saved['experience']['scope'], 'product')
        self.assertEqual(saved['experience']['direction_id'], '')
        self.assertEqual(saved['source_review'], page.evaluate('source'))

    def test_missing_source_and_read_only_modes_cannot_create_or_open_library(self):
        page = self.fixture()
        page.evaluate("ReviewExperiences.fromReview({...chain,source_review:null})")
        self.assertEqual(page.locator('.experience-form-dialog').count(), 0)
        page.evaluate("ReviewExperiences.fromReview({...chain,review:{state:'observing'}})")
        self.assertEqual(page.locator('.experience-form-dialog').count(), 0)
        for flag in ('PublicTextMode', 'WorkbenchGuest', 'GuestMode'):
            page.evaluate("flag=>{window[flag]=true;ReviewExperiences.open();ReviewExperiences.fromReview(chain);window[flag]=false}", flag)
        self.assertEqual(page.locator('dialog').count(), 0)
        self.assertEqual(page.evaluate('requests'), [])

    def test_source_status_search_filters_and_history_are_visible_and_escaped(self):
        page = self.fixture()
        page.evaluate("item.source_status='changed';item.source_status_label='来源复盘已修改';item.history=[{...item,version:0,practice:'之前的做法',history:[]}];ReviewExperiences.open({product:'202H'})")
        card = page.locator('.experience-card')
        card.wait_for()
        self.assertIn('来源待核对', card.inner_text())
        self.assertIn('暂不自动带入', card.inner_text())
        self.assertEqual(card.locator('img').count(), 0)
        self.assertNotIn('适用条件', card.inner_text())
        card.locator('.experience-evidence > summary').click()
        self.assertIn('适用条件', card.inner_text())
        self.assertIn('feedback_window=2026-10-20', card.locator('[data-experience-source]').get_attribute('href'))
        self.assertIn('product=202H', card.locator('[data-experience-source]').get_attribute('href'))
        card.get_by_text('提炼时的数据快照', exact=False).click()
        self.assertIn('订单 3', card.inner_text())
        card.locator('.experience-history summary').click()
        self.assertIn('之前的做法', card.inner_text())
        page.locator('[name=kind]').select_option('failure')
        page.locator('.experience-empty').wait_for()
        page.locator('[name=kind]').select_option('success')
        page.locator('[name=q]').fill('不存在的条件')
        page.wait_for_function("gets.some(path=>decodeURIComponent(path).includes('不存在的条件'))")
        self.assertEqual(page.locator('.experience-card').count(), 0)
        page.locator('[name=q]').fill('暗光')
        page.locator('.experience-card').wait_for()
        self.assertEqual(page.locator('[name=direction_id], [name=status]').count(), 0)
        page.locator('[name=q]').fill('202H-S9')
        page.locator('.experience-empty').wait_for()

    def test_edit_and_retire_restore_preserve_server_owned_provenance(self):
        page = self.fixture()
        page.evaluate("ReviewExperiences.open({product:'202H',status:'all'})")
        page.locator('[data-experience-edit]').click()
        form = page.locator('.experience-form-dialog')
        form.locator('[type=submit]:not([disabled])').wait_for()
        form.locator('[name=practice]').fill('补上相同曝光的对照。')
        form.locator('[type=submit]').click()
        page.wait_for_function('requests.length === 1')
        page.locator('.experience-form-dialog').wait_for(state='detached')
        saved = page.evaluate('requests[0]')
        self.assertNotIn('source_review', saved)
        self.assertNotIn('source', saved['experience'])
        self.assertEqual(saved['experience']['direction_id'], '202H-S2')
        self.assertEqual(saved['experience']['dimension'], '证明')
        self.assertEqual(saved['experience']['confidence'], 'preliminary')
        self.assertEqual(saved['experience']['id'], 'EXP-aabbccddeeff')
        page.locator('.experience-evidence > summary').click()
        page.locator('[data-experience-status]').click()
        page.wait_for_function("items[0].status === 'retired'")
        page.locator('.experience-evidence > summary').click()
        page.get_by_role('button', name='恢复使用', exact=True).click()
        page.wait_for_function("items[0].status === 'active'")
        self.assertEqual(page.evaluate('requests.length'), 3)

    def test_unknown_save_result_reuses_exact_request_even_after_reopening(self):
        page = self.fixture()
        page.evaluate("""window.api = async (path,options={}) => {
          if (options.method === 'POST') {
            requests.push(structuredClone(options.body));
            if(requests.length===1) throw new Error('连接中断');
            return {item:{...item,id:'EXP-new'},revision:'r2'};
          } return baseAPI(path,options);
        };void 0""")
        form = self.form(page)
        self.complete(form)
        form.locator('[type=submit]').click()
        form.locator('[data-error]').wait_for()
        self.assertTrue(form.locator('[name=practice]').is_disabled())
        form.locator('[data-close]').click()
        form.wait_for(state='detached')
        page.evaluate("ReviewExperiences.fromReview(chain,{onSaved:()=>saves++})")
        page.locator('.experience-form-dialog [type=submit]').click()
        page.wait_for_function('saves === 1')
        self.assertEqual(page.evaluate('requests[0]'), page.evaluate('requests[1]'))

    def test_conflict_reads_latest_and_preserves_user_input_until_acknowledged(self):
        page = self.fixture()
        page.evaluate("""window.api = async (path,options={}) => {
          if(options.method==='POST' && !window.conflicted) {
            window.conflicted=true;requests.push(structuredClone(options.body));
            items[0]={...item,version:2,practice:'另一页面的新做法',scope:'product',direction_id:'',status:'retired'};revision='r2';
            throw Object.assign(new Error('冲突'),{status:409});
          } return baseAPI(path,options);
        };ReviewExperiences.open({product:'202H'})""")
        page.locator('[data-experience-edit]').click()
        form = page.locator('.experience-form-dialog')
        form.locator('[type=submit]:not([disabled])').wait_for()
        form.locator('[name=practice]').fill('我正在完善的做法')
        form.locator('[type=submit]').click()
        form.locator('[data-accept-conflict]').wait_for()
        self.assertEqual(form.locator('[name=practice]').input_value(), '我正在完善的做法')
        self.assertTrue(form.locator('[type=submit]').is_disabled())
        self.assertIn('另一页面的新做法', form.locator('[data-conflict]').inner_text())
        form.locator('[data-accept-conflict]').click()
        form.locator('[type=submit]').click()
        page.locator('.experience-form-dialog').wait_for(state='detached')
        self.assertEqual(page.evaluate('requests[1].revision'), 'r2')
        self.assertEqual(page.evaluate('requests[1].experience.status'), 'retired')
        self.assertEqual(page.evaluate('requests[1].experience.scope'), 'product')
        self.assertEqual(page.evaluate('requests[1].experience.direction_id'), '')

    def test_uncertain_status_change_is_reconciled_before_another_toggle(self):
        page = self.fixture()
        page.evaluate("""window.api = async (path,options={}) => {
          if(options.method==='POST') {
            if(!window.firstStatusResult) {
              window.firstStatusResult=await baseAPI(path,options);throw new Error('连接中断');
            }
            requests.push(structuredClone(options.body));return firstStatusResult;
          }return baseAPI(path,options);
        };ReviewExperiences.open({product:'202H',status:'all'})""")
        page.locator('.experience-evidence > summary').click()
        page.locator('[data-experience-status]').click()
        page.get_by_role('button', name='核对上次操作', exact=True).wait_for()
        page.locator('.experience-library-dialog [data-close]').click()
        page.locator('.experience-library-dialog').wait_for(state='detached')
        page.evaluate("ReviewExperiences.open({product:'202H',status:'all'})")
        page.locator('.experience-evidence > summary').click()
        page.get_by_role('button', name='核对上次操作', exact=True).click()
        page.locator('.experience-evidence > summary').click()
        page.get_by_role('button', name='恢复使用', exact=True).wait_for()
        self.assertEqual(page.evaluate('requests[0]'), page.evaluate('requests[1]'))
        self.assertEqual(page.evaluate('items[0].status'), 'retired')

    def test_late_search_response_and_product_switch_cannot_replace_current_results(self):
        page = self.fixture()
        page.evaluate("""window.api=async (path,options={})=>{
          if(path.includes('q=slow')) return await new Promise(resolve=>window.finishSlow=resolve);
          return baseAPI(path,options);
        };ReviewExperiences.open({product:'202H'})""")
        page.locator('.experience-card').wait_for()
        page.locator('[name=q]').fill('slow')
        page.wait_for_function('!!window.finishSlow')
        page.locator('[name=q]').fill('没有匹配')
        page.locator('.experience-empty').wait_for()
        page.evaluate("finishSlow({items:[item],total:1,revision:'old'})")
        self.assertEqual(page.locator('.experience-card').count(), 0)
        page.locator('[data-close]').click()
        form = self.form(page)
        self.complete(form)
        page.evaluate("product='OTHER'")
        form.locator('[type=submit]').click()
        self.assertEqual(page.evaluate('requests'), [])
        self.assertIn('产品已切换', form.locator('[data-error]').inner_text())
        self.assertIn('暗光演示', form.locator('[name=conditions]').input_value())

    def test_feedback_panel_exposes_extraction_only_for_confirmed_reviews(self):
        page = self.fixture()
        page.add_script_tag(path=str(ROOT / 'static' / 'feedback-chain.js'))
        page.evaluate("chain.experiences=[item];FeedbackChain.mount(document.querySelector('#panel'),{video:{product:'202H',video_id:source.video_id,window_end:source.window_end}})")
        page.locator('[data-feedback-experience]').click()
        page.locator('.experience-form-dialog').wait_for()
        self.assertEqual(page.locator('[data-feedback-experiences]').inner_text(), '查看经验（1）')
        page.locator('.experience-form-dialog [data-close]').click()
        page.evaluate("chain.review.state='observing'")
        page.locator('[data-feedback-refresh]').click()
        page.locator('[data-feedback-note]').wait_for()
        self.assertEqual(page.locator('[data-feedback-experience]').count(), 0)

    def test_light_dark_narrow_layout_keeps_fields_and_actions_inside_dialog(self):
        screenshot_dir = Path(tempfile.gettempdir()) / 'review-experiences-ui'
        screenshot_dir.mkdir(exist_ok=True)
        for scheme in ('light', 'dark'):
            page = self.fixture(width=390, scheme=scheme)
            page.evaluate("ReviewExperiences.open({product:'202H'})")
            page.locator('.experience-card').wait_for()
            self.assertTrue(page.evaluate("[...document.querySelectorAll('dialog')].every(el=>el.scrollWidth<=el.clientWidth+1)"))
            page.screenshot(path=str(screenshot_dir / f'library-{scheme}.png'))
            page.locator('[data-experience-edit]').click()
            form = page.locator('.experience-form-dialog')
            form.locator('[type=submit]:not([disabled])').wait_for()
            self.assertTrue(form.evaluate('(el)=>el.scrollWidth<=el.clientWidth+1'))
            form.locator('[type=submit]').scroll_into_view_if_needed()
            page.screenshot(path=str(screenshot_dir / f'form-{scheme}.png'))


if __name__ == '__main__':
    unittest.main()
