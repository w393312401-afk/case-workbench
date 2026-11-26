"""The photo window imports the selected timeline, reports progress and permits a retry after failure."""
import io
from pathlib import Path
import unittest

from PIL import Image

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parents[1] / "static"


class PhotoCarouselBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 850})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        # Like the real server, style="" attributes in markup are refused (style-src without inline), so a layout that
        # relies on them fails here too; the injected stylesheets are <style> elements and stay allowed.
        self.page.route("**/*", lambda route: route.fulfill(content_type="text/html", body='<main id="publication"></main>',
                                                            headers={"Content-Security-Policy": "style-src-elem 'self' 'unsafe-inline'; style-src-attr 'none'"}))
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual([], self.errors))
        self.page.goto("http://127.0.0.1:43219/photo-test")
        for name in ("style.css", "buttons.css", "production.css", "reviews-layout.css", "photo-carousel.css"):
            self.page.add_style_tag(path=str(STATIC / name))
        self.page.add_script_tag(path=str(STATIC / "review-zh.js"))
        self.page.add_script_tag(path=str(STATIC / "photo-carousel.js"))
        self.page.evaluate("""async () => {
          window.calls = [];window.detail = {exists:false};window.toast = () => {};
          window.job = {id:'PRD-111111111111',timelines:[{unique_id:'one',name:'First'},{unique_id:'two',name:'Second'}]};
          window.api = async (path,options) => {
            if (options) {
              window.calls.push({path,body:structuredClone(options.body)});
              window.detail.resolve_import = {status:'running',message:'正在导出无字版…',percent:12};
            }
            if (path.endsWith('/frames')) return {items:[]};
            return structuredClone(window.detail);
          };
          const root = document.querySelector('#publication');
          root.innerHTML = PhotoCarousel.markup('two');
          await PhotoCarousel.bind(root,{job:window.job,api:window.api,uid:'two'});
        }""")

    def open(self):
        self.page.locator('[data-photo-open]').click()
        return self.page.locator('dialog.photo-carousel-dialog')

    def step(self, dialog, key):
        dialog.locator(f'[data-photo-tab="{key}"]').click()
        dialog.locator(f'[data-photo-tab="{key}"][aria-selected="true"]').wait_for()

    def current_step(self, dialog):
        return dialog.locator('[data-photo-tab][aria-selected="true"]').get_attribute('data-photo-tab')

    def ready(self):
        self.page.evaluate("""() => {window.detail = {exists:true,id:'PHS-111111111111',video:{name:'Second-无字版.mp4',duration:3},
          frames_job:{status:'done',count:15},works:[],versions:[],ai:{status:'idle'},
          resolve_import:{status:'done',message:'无字版已导入，字幕和屏显轨已恢复。'}};}""")

    def test_import_uses_selected_timeline_and_polls_through_automatic_frames(self):
        dialog = self.open()
        button = dialog.get_by_role('button', name='从达芬奇导入无字版', exact=True)
        button.click()
        self.assertTrue(dialog.locator('[data-photo-resolve]').is_disabled())
        self.assertEqual(dialog.locator('[data-photo-resolve]').get_attribute('aria-busy'), 'true')
        self.assertIn('12%', dialog.locator('[data-photo-resolve-progress]').inner_text())
        self.assertEqual(dialog.locator('[data-photo-video]').count(), 0)
        self.assertEqual(self.page.evaluate('window.calls'), [{
            'path':'/api/production/jobs/PRD-111111111111/photo-set/from-resolve', 'body':{'timeline_uid':'two'}}])
        self.assertEqual(self.current_step(dialog), 'video')
        self.assertTrue(dialog.locator('[data-photo-tab="plan"]').is_disabled())
        self.ready()
        dialog.locator('[data-photo-plan]').wait_for()  # done importing moves on to 选版 by itself
        self.assertEqual(self.current_step(dialog), 'plan')
        self.assertIn('done', dialog.locator('[data-photo-tab="video"]').get_attribute('class'))
        self.step(dialog, 'video')
        self.assertIn('字幕和屏显轨已恢复', dialog.locator('[data-photo-resolve-progress]').inner_text())
        self.assertIn('Second-无字版.mp4', dialog.inner_text())
        self.assertTrue(dialog.locator('[data-photo-resolve]').is_enabled())
        self.assertEqual(len(self.page.evaluate('window.calls')), 1)

    def finishable(self):
        self.page.evaluate("""async () => {
          window.detail = {exists:true,id:'PHS-111111111111',video:{name:'v.mp4',duration:3},frames_job:{status:'done',count:15},works:[],versions:[],
            ai:{status:'idle'},can_finish:true,finished_at:''};
          window.api = async (path,options) => {
            if (options) {
              window.calls.push({path,body:structuredClone(options.body)});
              const finished = options.body.decision === 'finish';
              Object.assign(window.detail,{finished_at:finished ? '2026-11-08T12:00:00+00:00' : '',can_finish:!finished});
            }
            if (path.endsWith('/frames')) return {items:[]};
            return structuredClone(window.detail);
          };
          const root = document.querySelector('#publication');
          root.innerHTML = PhotoCarousel.markup('two');
          await PhotoCarousel.bind(root,{job:window.job,api:window.api,uid:'two'});
        }""")

    def test_row_finishes_then_window_is_read_only_until_undone(self):
        self.finishable()
        row = self.page.locator('[data-photo-carousel]')
        self.assertEqual(row.locator('[data-photo-open]').inner_text(), '继续做图文')
        self.assertFalse(row.locator('[data-photo-reopen]').is_visible())
        row.locator('[data-photo-finish]').click()
        row.locator('[data-photo-status]', has_text='已完结').wait_for()
        self.assertEqual(row.locator('[data-photo-open]').inner_text(), '查看图文')
        self.assertFalse(row.locator('[data-photo-finish]').is_visible())
        self.assertEqual(self.page.evaluate('window.calls'), [{
            'path':'/api/production/jobs/PRD-111111111111/photo-decision', 'body':{'timeline':'two','decision':'finish'}}])
        dialog = self.open()
        dialog.locator('.photo-finished-strip').wait_for()
        self.assertTrue(dialog.locator('[data-photo-plan]').is_disabled())
        dialog.locator('[data-photo-reopen]').click()
        dialog.locator('.photo-finished-strip').wait_for(state='detached')
        self.assertTrue(dialog.locator('[data-photo-plan]').is_enabled())
        self.assertEqual(self.page.evaluate('window.calls.at(-1).body.decision'), 'reopen')

    def test_delete_shows_the_cost_and_only_deletes_on_confirm(self):
        self.finishable()
        self.page.evaluate("""async () => {
          Object.assign(window.detail,{can_delete:true});
          const original = window.api;
          window.api = async (path,options) => {
            if (path.endsWith('/photo-delete')) {
              window.calls.push({path,body:structuredClone(options.body)});
              if (options.body.preview) return {versions:2,exports:1,files:40,bytes:1572864,notion_pages:1};
              window.detail = {exists:false};
              return {deleted:true,warnings:[]};
            }
            return original(path,options);
          };
          const root = document.querySelector('#publication');
          root.innerHTML = PhotoCarousel.markup('two');
          await PhotoCarousel.bind(root,{job:window.job,api:window.api,uid:'two'});
        }""")
        row = self.page.locator('[data-photo-carousel]')
        row.locator('[data-photo-delete]').click()
        confirm = self.page.locator('.photo-delete-confirm')
        confirm.wait_for()
        text = confirm.inner_text()
        for piece in ('2 版图文', '1 个成品文件夹', '40 个文件', '2 MB', '不能恢复', 'Notion 上的 1 个页面不会删'):
            self.assertIn(piece, text)
        confirm.locator('[data-photo-delete-cancel]').click()
        self.assertEqual(self.page.locator('.photo-delete-confirm').count(), 0)
        self.assertEqual([call['body'] for call in self.page.evaluate('window.calls')], [{'timeline':'two','preview':True}])
        row.locator('[data-photo-delete]').click()
        self.page.locator('[data-photo-delete-confirm]').click()
        row.locator('[data-photo-open]', has_text='做图文').wait_for()
        self.assertEqual(self.page.evaluate('window.calls.at(-1).body'), {'timeline':'two'})
        self.assertFalse(row.locator('[data-photo-delete]').is_visible())
        self.assertEqual(self.page.locator('.photo-delete-confirm').count(), 0)

    def test_all_versions_final_and_on_notion_ends_without_a_button(self):
        self.finishable()
        self.page.evaluate("""async () => {
          Object.assign(window.detail,{works:[{key:'B',status:'done',exports:1}],notion:{B:'completed'}});
          const root = document.querySelector('#publication');
          root.innerHTML = PhotoCarousel.markup('two');
          await PhotoCarousel.bind(root,{job:window.job,api:window.api,uid:'two'});
        }""")
        row = self.page.locator('[data-photo-carousel]')
        self.assertEqual(row.locator('[data-photo-status]').inner_text(), '已完结 · 1 版都已定稿 · 已上传 Notion B')
        self.assertEqual(row.locator('[data-photo-open]').inner_text(), '查看图文')
        self.assertFalse(row.locator('[data-photo-finish]').is_visible())
        self.assertFalse(row.locator('[data-photo-reopen]').is_visible())

    def test_closing_and_reopening_preserves_running_import(self):
        dialog = self.open()
        dialog.locator('[data-photo-resolve]').click()
        dialog.locator('[data-photo-resolve-progress]').wait_for()
        dialog.locator('[data-close]').click()
        self.assertEqual(self.page.locator('dialog.photo-carousel-dialog').count(), 0)
        dialog = self.open()
        dialog.locator('[data-photo-resolve-progress]').wait_for()
        self.assertTrue(dialog.locator('[data-photo-resolve]').is_disabled())
        self.assertEqual(len(self.page.evaluate('window.calls')), 1)
        self.ready()
        dialog.locator('[data-photo-plan]').wait_for()

    def test_failure_keeps_previous_video_and_retry_available(self):
        self.ready()
        dialog = self.open()
        self.step(dialog, 'video')
        dialog.locator('[data-photo-resolve]').click()
        self.page.evaluate("window.detail.resolve_import = {status:'failed',error:'素材离线，请重新链接后重试。'}")
        dialog.locator('[data-photo-resolve-error]', has_text='素材离线').wait_for()
        self.assertIn('Second-无字版.mp4', dialog.inner_text())
        self.assertTrue(dialog.locator('[data-photo-resolve]').is_enabled())
        dialog.locator('[data-photo-resolve]').click()
        self.assertEqual(len(self.page.evaluate('window.calls')), 2)

    def test_button_and_progress_fit_light_dark_and_narrow_windows(self):
        dialog = self.open()
        for scheme, width in (('light', 1280), ('dark', 1280), ('dark', 390)):
            self.page.emulate_media(color_scheme=scheme)
            self.page.set_viewport_size({'width':width,'height':850})
            button = dialog.locator('[data-photo-resolve]')
            self.assertTrue(button.is_visible())
            self.assertTrue(button.evaluate('(el) => el.getBoundingClientRect().right <= innerWidth'))
            self.assertTrue(dialog.evaluate('(el) => el.scrollWidth <= el.clientWidth'))


    def collage(self):
        self.page.evaluate("""() => {
          const slide = (layout,cells) => ({title_lines:['DULL / *SHARP*'],body_lines:['20° both sides'],title_zh:'钝 / 利',body_zh:'两边 20°',
            image:{kind:'frame',t:0.2},crop_x:.5,crop_y:.5,layout,cells,text_position:'middle',text_size:'l'});
          window.detail = {exists:true,id:'PHS-111111111111',video:{name:'v.mp4',duration:2},frames_job:{status:'done',count:10},ai:{status:'done'},
            versions:[],works:[{key:'A',name_zh:'对比',status:'doing',exports:0,openable:true}],current:'A',updated_at:'x',
            work:{key:'A',name_zh:'对比',status:'doing',style:'bar',cta:'',exports:[],can_undo:false,
                  slides:[slide('lr',[{image:{kind:'frame',t:1.6},crop_x:.1,crop_y:.5}]),slide('single',[])]},
            styles:{bar:'黑色字条'},positions:{split:'标题在上、第二行在下',middle:'全部在中间'},sizes:{l:'大'},
            layouts:{single:'单图',lr:'左右',tb:'上下',grid:'四格'}};
          window.api = async (path,options) => {
            if (path.endsWith('/frames')) return {items:[0,.2,.4,.6,.8,1,1.2,1.4,1.6,1.8,2,2.2].map((t,i) => ({n:i + 1,t,sharp:10}))};
            if (options) {
              window.calls.push({path,body:structuredClone(options.body)});
              const target = window.detail.work.slides[0];
              if (options.body.layout) target.layout = options.body.layout;
              if (options.body.image) (options.body.cell > 1 ? target.cells[options.body.cell - 2] : target).image = options.body.image;
            }
            return structuredClone(window.detail);
          };
          const root = document.querySelector('#publication');
          root.innerHTML = PhotoCarousel.markup('two');
          return PhotoCarousel.bind(root,{job:window.job,api:window.api,uid:'two'});
        }""")

    def test_collage_cells_are_picked_in_the_preview_and_swapped_one_at_a_time(self):
        self.collage()
        dialog = self.open()
        dialog.locator('[data-photo-cell]').first.wait_for()
        self.assertEqual(dialog.locator('[data-photo-cell]').count(), 2)
        self.assertEqual(dialog.locator('[data-photo-layout].is-current').inner_text(), '左右')
        self.assertIn('1. 视频 0.2 秒', dialog.locator('.photo-editor h3').inner_text())
        self.assertIn('换第 1 格', dialog.locator('.photo-control').nth(1).inner_text())
        dialog.locator('[data-photo-cell="2"]').click()
        self.assertIn('换第 2 格', dialog.locator('.photo-control').nth(1).inner_text())
        self.assertIn('is-active', dialog.locator('[data-photo-cell="2"]').get_attribute('class'))
        self.assertEqual(self.page.evaluate('window.calls'), [])  # selecting a cell saves nothing
        self.assertIn('/frame/9.jpg?full=1', dialog.locator('[data-photo-cell="2"] img').get_attribute('src'))  # cell 2's own frame
        dialog.locator('[data-photo-frame="0.4"]').click()
        dialog.locator('[data-photo-layout="grid"]').click()
        dialog.locator('[data-photo-cell="4"]').wait_for()
        self.assertEqual(self.page.evaluate('window.calls'), [
            {'path':'/api/photo-sets/PHS-111111111111/slides/1', 'body':{'image':{'kind':'frame','t':0.4}, 'cell':2}},
            {'path':'/api/photo-sets/PHS-111111111111/slides/1', 'body':{'layout':'grid'}}])
        self.assertIn('换第 1 格', dialog.locator('.photo-control').nth(1).inner_text())  # back to cell 1 after a layout change
        dialog.locator('[data-photo-select="2"]').click()
        self.assertIn('换图', dialog.locator('.photo-control').nth(1).inner_text())
        self.assertEqual(dialog.locator('[data-photo-cell]').count(), 1)
        self.assertEqual(dialog.locator('.photo-cell-no').count(), 0)

    def test_collage_cells_have_real_size_under_the_page_security_policy(self):
        self.collage()
        dialog = self.open()
        dialog.locator('[data-photo-cell="2"]').wait_for()
        crop = dialog.locator('[data-photo-crop]').bounding_box()
        boxes = dialog.locator('[data-photo-cell]').evaluate_all('(els) => els.map(el => {const r = el.getBoundingClientRect(); return [r.width, r.height];})')
        self.assertEqual(len(boxes), 2)
        for width, height in boxes:  # left/right halves of the preview, not 0x0 boxes
            self.assertAlmostEqual(width, crop['width'] / 2, delta=2)
            self.assertAlmostEqual(height, crop['height'], delta=2)
        position = dialog.locator('[data-photo-cell="2"] .photo-crop-raw').evaluate('(el) => getComputedStyle(el).objectPosition')
        self.assertEqual(position, '10% 50%')
        dialog.locator('[data-photo-select="2"]').click()  # a plain slide: one cell covering the whole preview
        box = dialog.locator('[data-photo-cell]').bounding_box()
        self.assertAlmostEqual(box['width'], crop['width'], delta=2)

    def test_collage_editor_fits_light_dark_and_narrow_windows(self):
        self.collage()
        dialog = self.open()
        dialog.locator('[data-photo-cell]').first.wait_for()
        for scheme, width in (('light', 1280), ('dark', 1280), ('dark', 390)):
            self.page.emulate_media(color_scheme=scheme)
            self.page.set_viewport_size({'width':width,'height':850})
            self.assertTrue(dialog.locator('[data-photo-layout="grid"]').is_visible())
            self.assertTrue(dialog.evaluate('(el) => el.scrollWidth <= el.clientWidth'))

    def editable(self, delay=0):
        """Three plain slides on real pictures (a 90×160 frame), and an api that applies each slide edit, after `delay` ms."""
        self.collage()
        jpeg = io.BytesIO()
        Image.new("RGB", (90, 160), (40, 120, 200)).save(jpeg, "JPEG")
        self.page.route("**/frame/**", lambda route: route.fulfill(content_type="image/jpeg", body=jpeg.getvalue()))
        self.page.evaluate("""(delay) => {
          window.detail.work.slides = ['ONE','TWO','THREE'].map(t => ({...window.detail.work.slides[1],title_lines:[t]}));
          const old = window.api;
          window.api = async (path,options) => {
            const m = path.match(/slides.([0-9]+)$/);
            if (m && options) {
              window.calls.push({path,body:structuredClone(options.body)});
              await new Promise(done => setTimeout(done,delay));
              const slide = window.detail.work.slides[Number(m[1]) - 1], {cell,...change} = options.body;
              Object.assign(slide,change);
              if (slide.zoom === 1) delete slide.zoom;
              window.detail.updated_at = String(Math.random());
              return structuredClone(window.detail);
            }
            return old(path,options);
          };
          const root = document.querySelector('#publication');
          root.innerHTML = PhotoCarousel.markup('two');
          return PhotoCarousel.bind(root,{job:window.job,api:window.api,uid:'two'});
        }""", delay)

    def test_an_edit_asks_again_only_for_the_slide_it_changed(self):
        self.editable()
        dialog = self.open()
        dialog.locator('[data-photo-select="3"]').wait_for()
        strip = lambda: dialog.locator('[data-photo-strip] img').evaluate_all('(els) => els.map(el => el.getAttribute("src"))')
        before = strip()
        self.assertEqual(len(set(before)), 3)
        dialog.locator('[data-photo-select="2"]').click()
        dialog.locator('[data-photo-position="split"]').click()
        dialog.locator('[data-photo-position="split"].is-current').wait_for()
        after = strip()
        self.assertEqual((after[0], after[2]), (before[0], before[2]))  # untouched slides keep their address: no new request
        self.assertNotEqual(after[1], before[1])
        self.assertIn('w=540', dialog.locator('.photo-preview').get_attribute('src'))

    def test_frame_clicks_while_saving_show_at_once_and_the_last_one_is_kept(self):
        self.editable(delay=400)
        dialog = self.open()
        dialog.locator('[data-photo-frame="0.4"]').wait_for()
        dialog.locator('[data-photo-frame="0.4"]').click()
        crop = dialog.locator('[data-photo-crop]')
        self.assertIn('is-pending', crop.get_attribute('class'))  # the picked frame is up before the save returns
        self.assertIn('/frame/3.jpg', crop.locator('.photo-crop-raw').get_attribute('src'))
        self.assertNotIn('is-busy', dialog.locator('[data-photo-body]').get_attribute('class'))
        dialog.locator('[data-photo-frame="0.8"]').click()
        dialog.locator('[data-photo-frame="1"]').click()
        self.page.wait_for_function('window.calls.length === 2')
        self.page.wait_for_timeout(500)
        self.assertEqual([call['body'] for call in self.page.evaluate('window.calls')],
                         [{'image':{'kind':'frame','t':0.4}}, {'image':{'kind':'frame','t':1}}])
        dialog.locator('[data-photo-frame="1"].is-current').wait_for()
        self.assertIn('视频 1 秒', dialog.locator('.photo-editor h3').inner_text())

    def test_zoom_by_slider_wheel_and_reset_scales_the_picture_and_saves(self):
        self.editable()
        dialog = self.open()
        crop = dialog.locator('[data-photo-crop]')
        raw = crop.locator('.photo-crop-raw')
        self.page.wait_for_function('document.querySelector(".photo-crop-raw").naturalWidth > 0')
        width = lambda: raw.evaluate('(el) => el.getBoundingClientRect().width')
        cell = crop.bounding_box()['width']
        self.assertAlmostEqual(width(), cell, delta=2)  # covers the 3:4 window at 1×
        self.assertTrue(dialog.locator('[data-photo-zoom-reset]').is_disabled())
        dialog.locator('[data-photo-zoom]').fill('2')
        dialog.locator('[data-photo-zoom-value]', has_text='2.0×').wait_for()
        self.page.wait_for_function('window.calls.length === 1')
        self.assertEqual(self.page.evaluate('window.calls[0]'), {'path':'/api/photo-sets/PHS-111111111111/slides/1', 'body':{'zoom':2}})
        dialog.locator('.photo-editor h3', has_text='放大 2.0×').wait_for()
        self.page.wait_for_function('document.querySelector(".photo-crop-raw").naturalWidth > 0')
        self.assertAlmostEqual(width(), 2 * cell, delta=3)
        crop.hover()
        self.page.mouse.wheel(0, -240)
        self.page.wait_for_function('window.calls.length === 2')
        self.assertGreater(self.page.evaluate('window.calls[1].body.zoom'), 2)
        self.assertNotIn('crop_x', self.page.evaluate('window.calls[1].body'))
        dialog.locator('[data-photo-zoom-reset]').click()
        self.page.wait_for_function('window.calls.length === 3')
        self.assertEqual(self.page.evaluate('window.calls[2].body'), {'zoom':1})
        dialog.locator('[data-photo-zoom-reset][disabled]').wait_for()
        crop.focus()
        self.page.keyboard.press('+')
        self.page.wait_for_function('window.calls.length === 4')
        self.assertEqual(self.page.evaluate('window.calls[3].body'), {'zoom':1.1})
        for scheme, size in (('light', 1280), ('dark', 1280), ('dark', 390)):
            self.page.emulate_media(color_scheme=scheme)
            self.page.set_viewport_size({'width':size,'height':850})
            self.assertTrue(dialog.locator('[data-photo-zoom]').is_visible())
            self.assertTrue(dialog.evaluate('(el) => el.scrollWidth <= el.clientWidth'))

    def arrangeable(self):
        self.collage()
        self.page.evaluate("""() => {
          window.detail.max_slides = 8;
          window.detail.work.slides = ['ONE','TWO','THREE'].map(t => ({...window.detail.work.slides[1],title_lines:[t]}));
          const old = window.api;
          window.api = async (path,options) => {
            const m = path.match(/slides.([0-9]+).arrange$/);
            if (m && options) {
              window.calls.push({path,body:structuredClone(options.body)});
              const s = window.detail.work.slides, n = Number(m[1]), b = options.body;
              if (b.action === 'duplicate') s.splice(n,0,structuredClone(s[n - 1]));
              if (b.action === 'delete') s.splice(n - 1,1);
              if (b.action === 'move') s.splice(b.to - 1,0,s.splice(n - 1,1)[0]);
              window.detail.updated_at = String(Math.random());
              return structuredClone(window.detail);
            }
            return old(path,options);
          };
          const root = document.querySelector('#publication');
          root.innerHTML = PhotoCarousel.markup('two');
          return PhotoCarousel.bind(root,{job:window.job,api:window.api,uid:'two'});
        }""")

    def test_slides_are_copied_deleted_moved_and_dragged_keeping_the_edited_slide_selected(self):
        self.arrangeable()
        dialog = self.open()
        title = lambda: dialog.locator('[data-photo-title]').input_value()
        dialog.locator('[data-photo-select="3"]').wait_for()
        self.assertTrue(dialog.locator('[data-photo-arrange="back"]').is_disabled())
        dialog.locator('[data-photo-select="2"]').click()
        dialog.locator('[data-photo-arrange="duplicate"]').click()
        dialog.locator('[data-photo-select="4"]').wait_for()
        self.assertIn('第 3 张', dialog.locator('.photo-slide-tools').inner_text())  # the new copy is selected
        self.assertEqual(title(), 'TWO')
        dialog.locator('[data-photo-arrange="forward"]').click()
        dialog.locator('.photo-slide-tools', has_text='第 4 张').wait_for()
        self.assertTrue(dialog.locator('[data-photo-arrange="forward"]').is_disabled())
        dialog.locator('[data-photo-select="1"]').drag_to(dialog.locator('[data-photo-select="3"]'))
        dialog.locator('.photo-slide-tools', has_text='第 3 张').wait_for()
        self.assertEqual(title(), 'ONE')
        dialog.locator('[data-photo-arrange="delete"]').click()
        dialog.locator('[data-photo-select="4"]').wait_for(state='detached')
        self.assertEqual(self.page.evaluate("window.detail.work.slides.map(s => s.title_lines[0])"), ['TWO','THREE','TWO'])
        self.assertEqual([c['body'] for c in self.page.evaluate('window.calls')], [
            {'action':'duplicate'}, {'action':'move','to':4}, {'action':'move','to':3}, {'action':'delete'}])
        self.assertEqual(self.page.evaluate('window.calls')[0]['path'], '/api/photo-sets/PHS-111111111111/slides/2/arrange')

    def test_copy_is_disabled_at_the_slide_limit_and_delete_at_one_slide(self):
        self.arrangeable()
        self.page.evaluate("window.detail.max_slides = 3")
        dialog = self.open()
        dialog.locator('[data-photo-select="3"]').wait_for()
        self.assertTrue(dialog.locator('[data-photo-arrange="duplicate"]').is_disabled())
        self.page.evaluate("window.detail.work.slides = window.detail.work.slides.slice(0,1)")
        dialog.locator('[data-close]').click()
        dialog = self.open()
        dialog.locator('[data-photo-select="1"]').wait_for()
        self.assertTrue(dialog.locator('[data-photo-arrange="delete"]').is_disabled())
        self.assertTrue(dialog.locator('[data-photo-arrange="duplicate"]').is_enabled())

    def test_chinese_meaning_asks_for_three_wordings_and_a_click_picks_one(self):
        self.collage()
        self.page.evaluate("""() => {
          window.detail.work.slides[0].title_zh = '钝 / 利';window.detail.work.slides[0].body_zh = '两边 20°';
          const old = window.api;
          window.api = async (path,options) => {
            if (/[/](rewrite|pick|rewrite-stop|undo)$/.test(path) && options) {
              window.calls.push({path,body:structuredClone(options.body)});
              if (path.endsWith('/rewrite')) window.detail.slide_ai = {status:'running',key:'A',number:1,zh:options.body.zh,note:options.body.note,progress:'AI 正在看画面…'};
              if (path.endsWith('/pick')) {window.detail.slide_ai.picked = options.body.index;Object.assign(window.detail.work.slides[0],{title_lines:['FINER EDGE?'],body_lines:[]});window.detail.work.can_undo = true;}
              if (path.endsWith('/undo')) {Object.assign(window.detail.work.slides[0],{title_lines:['DULL / *SHARP*'],body_lines:['20° both sides'],title_zh:'钝 / 利'});window.detail.work.can_redo = true;}
              return structuredClone(window.detail);
            }
            return old(path,options);
          };
          const root = document.querySelector('#publication');
          root.innerHTML = PhotoCarousel.markup('two');
          return PhotoCarousel.bind(root,{job:window.job,api:window.api,uid:'two'});
        }""")
        dialog = self.open()
        zh = dialog.locator('[data-photo-zh]')
        self.assertEqual(zh.input_value(), '钝 / 利\n两边 20°')
        dialog.locator('[data-photo-rewrite]').click()  # unchanged Chinese: same meaning, better wording
        dialog.locator('[data-photo-rewrite-stop]').wait_for()
        self.page.evaluate("window.detail.slide_ai = {status:'failed',key:'A',number:1,zh:'',error:'x'}")
        dialog.locator('[data-photo-rewrite]').wait_for(timeout=6000)
        zh.fill('想磨得更细？不用再买一块磨石')
        dialog.locator('[data-photo-rewrite-note]').fill('短一点')
        dialog.locator('[data-photo-rewrite]').click()
        dialog.locator('[data-photo-rewrite-stop]').wait_for()
        self.assertEqual([c['body'] for c in self.page.evaluate('window.calls')], [
            {'note':'', 'zh':''}, {'note':'短一点', 'zh':'想磨得更细？不用再买一块磨石'}])
        self.assertTrue(dialog.locator('[data-photo-title]').is_disabled())
        self.assertEqual(dialog.locator('[data-photo-zh]').input_value(), '想磨得更细？不用再买一块磨石')  # kept while AI writes
        dialog.locator('[data-photo-select="2"]').click()  # another slide stays editable and says which one is busy
        self.assertTrue(dialog.locator('[data-photo-title]').is_enabled())
        self.assertIn('第 1 张', dialog.locator('.photo-rewrite [role=status]').inner_text())
        dialog.locator('[data-photo-title]').click()
        dialog.locator('[data-photo-title]').fill('TYPING NOW')
        self.page.evaluate("""() => {Object.assign(window.detail.slide_ai,{status:'done',picked:null,options:[
          {title_lines:['WANT A *SHARPER* EDGE?'],body_lines:['Swap in the 1000-grit plate.'],title_zh:'想磨得更利？',body_zh:'换上 1000 目那块板',change_zh:'问句',label:'照你的意思'},
          {title_lines:['FINER EDGE?'],body_lines:[],title_zh:'更细？'},{title_lines:['STOP BUYING STONES'],body_lines:[]}]});}""")
        dialog.locator('[data-photo-rewrite]').wait_for(timeout=6000)  # the poll redraws when AI finishes
        self.assertEqual(dialog.locator('[data-photo-title]').input_value(), 'TYPING NOW')  # unsaved typing survives the redraw
        self.assertEqual(dialog.locator('[data-photo-pick]').count(), 0)  # options show on their own slide only
        dialog.locator('[data-photo-select="1"]').click()
        self.assertEqual(dialog.locator('[data-photo-pick]').count(), 3)
        self.assertIn('想磨得更利？｜换上 1000 目那块板', dialog.locator('[data-photo-pick="0"]').inner_text())
        self.assertIn('照你的意思', dialog.locator('[data-photo-pick="0"] .photo-option-kind').inner_text())
        self.assertEqual(dialog.locator('[data-photo-pick="1"] .photo-option-kind').count(), 0)
        self.assertEqual(dialog.locator('[data-photo-rewrite]').inner_text(), '再出 3 种')
        dialog.locator('[data-photo-pick="1"]').click()
        dialog.locator('[data-photo-pick="1"].is-current').wait_for()
        self.assertIn('已用', dialog.locator('[data-photo-pick="1"]').inner_text())
        self.assertEqual(self.page.evaluate('window.calls')[-1], {'path':'/api/photo-sets/PHS-111111111111/slides/1/pick', 'body':{'index':1}})
        self.assertEqual(dialog.locator('[data-photo-title]').input_value(), 'FINER EDGE?')
        self.assertEqual(dialog.locator('[data-photo-zh]').input_value(), '想磨得更细？不用再买一块磨石')  # the user's meaning stays
        dialog.locator('[data-photo-zh]').fill('我自己改的中文')
        dialog.locator('[data-photo-undo]').click()
        dialog.locator('[data-photo-redo]:not([disabled])').wait_for()
        self.assertEqual(dialog.locator('[data-photo-title]').input_value(), 'DULL / *SHARP*')
        self.assertEqual(dialog.locator('[data-photo-zh]').input_value(), '我自己改的中文')  # undo never wipes typed Chinese
        self.assertEqual(dialog.locator('.photo-option.is-current').count(), 0)  # 已用 follows the slide, not the last click

    def test_undo_and_redo_buttons_name_the_change_and_frames_are_a_grid(self):
        self.collage()
        self.page.evaluate("""() => {Object.assign(window.detail.work,{can_undo:true,undo_label:'第 1 张：文案',can_redo:false,redo_label:''});}""")
        dialog = self.open()
        dialog.locator('[data-photo-redo]').wait_for()
        self.assertEqual(dialog.locator('[data-photo-undo]').get_attribute('title'), '撤销：第 1 张：文案')
        self.assertTrue(dialog.locator('[data-photo-redo]').is_disabled())
        dialog.locator('[data-photo-redo]').evaluate('(el) => el.disabled = false')
        dialog.locator('[data-photo-redo]').click()
        self.assertEqual(self.page.evaluate('window.calls')[-1]['path'], '/api/photo-sets/PHS-111111111111/redo')
        grid = dialog.locator('[data-photo-frames]')
        self.assertEqual(grid.evaluate('(el) => getComputedStyle(el).display'), 'grid')
        self.assertTrue(grid.evaluate('(el) => el.scrollWidth <= el.clientWidth'))  # no sideways strip
        tops = dialog.locator('[data-photo-frame]').evaluate_all('(els) => els.map(el => Math.round(el.getBoundingClientRect().top))')
        self.assertEqual(len(tops), 12)
        self.page.set_viewport_size({'width':390,'height':850})
        tops = dialog.locator('[data-photo-frame]').evaluate_all('(els) => els.map(el => Math.round(el.getBoundingClientRect().top))')
        self.assertGreater(len(set(tops)), 1)  # wraps into rows on a phone
        self.assertTrue(dialog.evaluate('(el) => el.scrollWidth <= el.clientWidth'))

    def test_a_long_video_keeps_every_frame_tile_full_height(self):
        self.collage()
        self.page.evaluate("""() => {
          const old = window.api;
          window.api = async (path,options) => path.endsWith('/frames')
            ? {items:Array.from({length:154},(_,i) => ({n:i + 1,t:Math.round(i * 2) / 10,sharp:10}))} : old(path,options);
          const root = document.querySelector('#publication');
          root.innerHTML = PhotoCarousel.markup('two');
          return PhotoCarousel.bind(root,{job:window.job,api:window.api,uid:'two'});
        }""")
        dialog = self.open()
        dialog.locator('[data-photo-frame]').first.wait_for()
        sizes = dialog.locator('[data-photo-frame]').evaluate_all('(els) => els.map(el => [el.offsetWidth, el.offsetHeight])')
        self.assertGreater(len(sizes), 80)
        self.assertTrue(all(h > w * 1.6 for w, h in sizes), sizes[:3])  # 9:16 tiles, never squeezed into slivers
        self.assertGreaterEqual(min(w for w, _ in sizes), 84)

    def test_version_cards_name_their_writing_and_the_editor_says_second_line(self):
        self.collage()
        self.page.evaluate("""() => {window.detail.writings = {self:'达人自述',kit:'整套配置',qa:'顾虑问答',list:'理由清单'};
          window.detail.versions = [{key:'A',writing:'self',name_zh:'进阶',buyer_zh:'有磨石的人',slides:[{title_lines:['I FOUND IT'],body_lines:['One stone gets it sharp.'],frame_time:0.2}]},
                                    {key:'B',writing:'kit',name_zh:'整套',buyer_zh:'爱刀的人',slides:[{title_lines:['THE FULL KIT.'],body_lines:[],frame_time:0.2}]}];}""")
        dialog = self.open()
        self.step(dialog, 'plan')
        dialog.locator('.photo-version').first.wait_for()
        self.assertEqual(dialog.locator('.photo-chip.is-writing').all_inner_texts(), ['达人自述', '整套配置'])
        self.step(dialog, 'make')
        self.assertIn('第二行', dialog.locator('label:has([data-photo-body-lines])').inner_text())

    def test_backup_frames_swap_in_one_click_and_final_step_has_no_check_list(self):
        self.collage()
        self.page.evaluate("""() => {const s = window.detail.work.slides;
          Object.assign(s[0],{layout:'single',cells:[],alternates:[0.2,1.2,2]});}""")
        dialog = self.open()
        row = dialog.locator('.photo-alternates')
        row.wait_for()
        self.assertEqual(row.locator('[data-photo-frame]').count(), 3)
        self.assertIn('is-current', row.locator('[data-photo-frame="0.2"]').get_attribute('class'))
        row.locator('[data-photo-frame="1.2"]').click()
        self.assertEqual(self.page.evaluate('window.calls')[-1],
                         {'path':'/api/photo-sets/PHS-111111111111/slides/1', 'body':{'image':{'kind':'frame','t':1.2}}})
        self.step(dialog, 'final')
        dialog.locator('[data-photo-export]').wait_for()
        self.assertEqual(dialog.locator('[data-photo-checks]').count(), 0)  # pre-export reminder removed 2026-11-25
        self.assertNotIn('导出前', dialog.locator('.photo-step').last.inner_text())
        self.step(dialog, 'make')
        dialog.locator('.photo-editor').first.wait_for()
        dialog.locator('[data-photo-select="2"]').click()
        self.assertIn('第 2 张', dialog.locator('.photo-editor h3').inner_text())
        self.assertEqual(dialog.locator('.photo-alternates').count(), 0)  # slide 2 has no backups
        self.page.set_viewport_size({'width':390,'height':850})
        self.assertTrue(dialog.evaluate('(el) => el.scrollWidth <= el.clientWidth'))

    def test_plan_has_no_switch_for_more_writings(self):
        self.ready()
        dialog = self.open()
        dialog.locator('[data-photo-plan]').wait_for()
        self.assertEqual(dialog.locator('[data-photo-plan]').inner_text(), 'AI 出逐张表')
        self.assertEqual(dialog.locator('[data-photo-all]').count(), 0)  # both writings are always written
        dialog.locator('[data-photo-plan]').click()
        self.page.wait_for_function("() => window.calls.some(call => call.path.endsWith('/plan'))")
        call = [c for c in self.page.evaluate('window.calls') if c['path'].endswith('/plan')][-1]
        self.assertNotIn('all', call['body'])

    def test_plan_language_starts_from_the_set_and_goes_with_the_plan(self):
        self.ready()
        self.page.evaluate("""() => Object.assign(window.detail,{market:'uk',markets:[{id:'us',label:'美国 · 英语'},{id:'uk',label:'英国 · 英语'},{id:'de',label:'德国 · 德语'}]})""")
        dialog = self.open()
        select = dialog.locator('[data-photo-market]')
        select.wait_for()
        self.assertEqual(select.input_value(), 'uk')
        select.select_option('de')
        dialog.locator('[data-photo-plan]').click()
        self.page.wait_for_function("() => window.calls.some(call => call.path.endsWith('/plan'))")
        call = [c for c in self.page.evaluate('window.calls') if c['path'].endswith('/plan')][-1]
        self.assertEqual(call['body']['market'], 'de')

    def test_publish_copy_is_the_video_style_list_and_copies_english_or_both(self):
        self.collage()
        self.page.evaluate("""() => {Object.assign(window.detail.work,{titles:['🔴I Found the Next Level#SHARPAL202H #knife','🔴Second#SHARPAL202H'],
          titles_zh:['我找到了进阶方案',''],caption_en:'One stone gets it sharp.\\nThis gets the finish.',caption_zh:'一块磨石能磨利。'});
          window.copied = [];window.copyText = (text,message) => window.copied.push([text,message]);}""")
        dialog = self.open()
        self.step(dialog, 'final')
        dialog.locator('[data-photo-copy-title="0"]').wait_for()
        self.assertIn('我找到了进阶方案', dialog.locator('[data-photo-copy-title="0"]').inner_text())
        dialog.locator('[data-photo-copy-title="1"]').click()
        dialog.locator('[data-photo-copy-text]').click()
        self.assertEqual(self.page.evaluate('window.copied'), [['🔴Second#SHARPAL202H', '标题与主题标签已复制'],
                                                              ['一块磨石能磨利。\n\nOne stone gets it sharp.\nThis gets the finish.', '中英文案已复制']])
        self.assertEqual(dialog.locator('.photo-publish-copy .video-publish-copy-label').all_inner_texts(), ['标题', '文案'])

    def test_a_refused_action_also_shows_a_toast_where_the_user_is(self):
        self.collage()
        self.page.evaluate("""() => {window.toasts = [];window.toast = (message,bad) => window.toasts.push([message,bad]);
          const old = window.api;
          window.api = async (path,options) => {if (path.endsWith('/redo') && options) throw new Error('没有可以恢复的改动。');return old(path,options);};
          Object.assign(window.detail.work,{can_redo:true,redo_label:'x'});
          const root = document.querySelector('#publication');
          root.innerHTML = PhotoCarousel.markup('two');
          return PhotoCarousel.bind(root,{job:window.job,api:window.api,uid:'two'});}""")
        dialog = self.open()
        dialog.locator('[data-photo-redo]').click()
        self.page.wait_for_function('window.toasts.length > 0')
        self.assertEqual(self.page.evaluate('window.toasts'), [['没有可以恢复的改动。', True]])

    def test_failed_rewrite_is_shown_on_its_slide_only(self):
        self.collage()
        self.page.evaluate("window.detail.slide_ai = {status:'failed',key:'A',number:2,error:'AI 没按约定格式交回说法，可以再点一次。'}")
        dialog = self.open()
        dialog.locator('[data-photo-rewrite]').wait_for()
        self.assertEqual(dialog.locator('[data-photo-rewrite-error]').count(), 0)
        dialog.locator('[data-photo-select="2"]').click()
        self.assertIn('没按约定格式', dialog.locator('[data-photo-rewrite-error]').inner_text())

    def test_wording_options_fit_light_dark_and_narrow_windows(self):
        self.collage()
        self.page.evaluate("""() => {window.detail.slide_ai = {status:'done',key:'A',number:1,picked:0,options:[
          {title_lines:['A VERY LONG TITLE THAT WRAPS ON A PHONE SCREEN'],body_lines:['A long fact line about the 1000-grit plate'],title_zh:'很长的中文回译',change_zh:'问句'}]};}""")
        dialog = self.open()
        dialog.locator('[data-photo-pick]').wait_for()
        for scheme, width in (('light', 1280), ('dark', 1280), ('dark', 390)):
            self.page.emulate_media(color_scheme=scheme)
            self.page.set_viewport_size({'width':width,'height':850})
            self.assertTrue(dialog.locator('[data-photo-pick]').is_visible())
            self.assertTrue(dialog.evaluate('(el) => el.scrollWidth <= el.clientWidth'))

    def test_window_is_a_right_drawer_that_closes_from_the_page_beside_it(self):
        dialog = self.open()
        dialog.locator('[data-photo-resolve]').wait_for()
        self.page.wait_for_function("() => Math.round(document.querySelector('dialog.photo-carousel-dialog').getBoundingClientRect().right) === innerWidth")  # done sliding in
        box = dialog.bounding_box()
        self.assertAlmostEqual(box['x'] + box['width'], 1280, delta=1)
        self.assertAlmostEqual(box['width'], 960, delta=1)
        self.assertAlmostEqual(box['height'], 850, delta=1)
        self.page.mouse.click(100, 400)  # the dimmed page beside the drawer
        self.assertEqual(self.page.locator('dialog.photo-carousel-dialog').count(), 0)
        dialog = self.open()
        dialog.locator('[data-photo-resolve]').click()  # clicks inside keep it open
        self.assertEqual(self.page.locator('dialog.photo-carousel-dialog').count(), 1)
        self.page.keyboard.press('Escape')
        self.assertEqual(self.page.locator('dialog.photo-carousel-dialog').count(), 0)

    def test_opening_another_replaces_the_drawer(self):
        self.open().locator('[data-photo-resolve]').wait_for()
        self.page.evaluate("() => PhotoCarousel.open({job:window.job,api:window.api,uid:'one',timelineName:'First'})")
        self.assertEqual(self.page.locator('dialog.photo-carousel-dialog').count(), 1)
        self.assertEqual(self.page.locator('dialog.photo-carousel-dialog h2').inner_text(), 'First')

    def test_editor_columns_follow_the_drawer_width(self):
        self.collage()
        dialog = self.open()
        dialog.locator('[data-photo-crop]').wait_for()
        columns = lambda: dialog.locator('.photo-editor-grid').evaluate('(el) => getComputedStyle(el).gridTemplateColumns.split(" ").length')
        self.assertEqual(columns(), 2)
        dialog.evaluate("(el) => el.style.setProperty('--drawer-width', '600px')")
        self.page.wait_for_function("() => getComputedStyle(document.querySelector('.photo-editor-grid')).gridTemplateColumns.split(' ').length === 1")
        self.assertTrue(dialog.evaluate('(el) => el.scrollWidth <= el.clientWidth'))


    def test_unfinalize_and_delete_ask_first_inside_the_drawer(self):
        self.collage()
        self.page.evaluate("""() => {
          Object.assign(window.detail,{can_delete:true,works:[{key:'A',name_zh:'对比',status:'done',exports:1,openable:true}]});
          Object.assign(window.detail.work,{status:'done',exports:[{name:'PVR-20261115-A对比',at:'x'}]});
          const original = window.api;
          window.api = async (path,options) => {
            if (path.endsWith('/unfinalize')) {
              window.calls.push({path,body:structuredClone(options.body)});
              if (options.body.preview) return {key:'A',exports:1,files:24,bytes:3145728,notion_page:true};
              Object.assign(window.detail.work,{status:'doing',exports:[]});
              window.detail.works[0] = {...window.detail.works[0],status:'doing',exports:0};
              return {...structuredClone(window.detail),warnings:[]};
            }
            if (path.endsWith('/photo-delete')) {
              window.calls.push({path,body:structuredClone(options.body)});
              if (options.body.preview) return {versions:1,exports:0,files:30,bytes:2097152,notion_pages:0};
              window.detail = {exists:false};
              return {deleted:true,warnings:[]};
            }
            return original(path,options);
          };
          const root = document.querySelector('#publication');
          root.innerHTML = PhotoCarousel.markup('two');
          return PhotoCarousel.bind(root,{job:window.job,api:window.api,uid:'two'});
        }""")
        dialog = self.open()
        undo = dialog.locator('[data-photo-unfinalize]')
        undo.wait_for()
        self.assertTrue(dialog.locator('[data-photo-delete-all]').is_visible())
        undo.click()
        confirm = dialog.locator('.photo-delete-confirm')
        confirm.wait_for()
        for piece in ('撤销第 A 版定稿', '1 个成品文件夹', '24 个文件', '3 MB', '不能恢复', 'Notion 上的页面不会删'):
            self.assertIn(piece, confirm.inner_text())
        confirm.locator('[data-photo-unfinalize-cancel]').click()
        self.assertEqual(dialog.locator('.photo-delete-confirm').count(), 0)
        undo.click()
        dialog.locator('[data-photo-unfinalize-confirm]').click()
        dialog.locator('[data-photo-tab="make"][aria-selected="true"]').wait_for()  # back to being made, so back to 制作
        self.step(dialog, 'final')
        dialog.locator('[data-photo-export]', has_text='定稿导出').wait_for()
        self.assertEqual(dialog.locator('[data-photo-unfinalize]').count(), 0)
        self.assertEqual([call['body'] for call in self.page.evaluate('window.calls') if call['path'].endswith('/unfinalize')],
                         [{'key':'A','preview':True}, {'key':'A','preview':True}, {'key':'A'}])
        dialog.locator('[data-photo-delete-all]').click()
        confirm = dialog.locator('.photo-delete-confirm')
        confirm.wait_for()
        self.assertIn('30 个文件', confirm.inner_text())
        self.assertTrue(confirm.evaluate('(el) => el.getBoundingClientRect().left') >= dialog.evaluate('(el) => el.getBoundingClientRect().left'))
        confirm.locator('[data-photo-delete-confirm]').click()
        dialog.wait_for(state='detached')
        self.assertEqual(self.page.evaluate('window.calls.at(-1).body'), {'timeline':'two'})
        self.page.locator('[data-photo-carousel] [data-photo-open]', has_text='做图文').wait_for()

    def test_steps_land_where_the_set_is_and_follow_it_forward(self):
        self.page.evaluate("""() => {
          const version = (key,recommended) => ({key,name_zh:'版'+key,buyer_zh:'有磨石的人',recommended,slides:[{title_lines:['HI'],body_lines:[],frame_time:0.2}]});
          window.detail = {exists:true,id:'PHS-111111111111',video:{name:'v.mp4',duration:2},frames_job:{status:'done',count:10},ai:{status:'done'},
            versions:[version('A',false),version('B',true)],works:[],updated_at:'x',styles:{bar:'黑色字条'},positions:{},sizes:{},layouts:{single:'单图'}};
          window.api = async (path,options) => {
            if (path.endsWith('/frames')) return {items:[]};
            if (options) {
              window.calls.push({path,body:structuredClone(options.body)});
              if (path.endsWith('/choose')) Object.assign(window.detail,{works:[{key:'B',name_zh:'版B',status:'doing',exports:0,openable:true}],
                work:{key:'B',name_zh:'版B',status:'doing',style:'bar',exports:[],slides:[{title_lines:['HI'],body_lines:[],image:{kind:'frame',t:0.2},crop_x:.5,crop_y:.5}]}});
              if (path.endsWith('/export')) {Object.assign(window.detail.work,{status:'done',exports:[{name:'PVR-B'}]});window.detail.works[0].status = 'done';
                return {...structuredClone(window.detail),export:{name:'PVR-B'}};}
            }
            return structuredClone(window.detail);
          };
          const root = document.querySelector('#publication');
          root.innerHTML = PhotoCarousel.markup('two');
          return PhotoCarousel.bind(root,{job:window.job,api:window.api,uid:'two'});
        }""")
        dialog = self.open()
        dialog.locator('.photo-version').first.wait_for()
        self.assertEqual(self.current_step(dialog), 'plan')  # video in, nothing chosen yet
        self.assertEqual(dialog.locator('[data-photo-tab]').all_inner_texts()[1].split('\n')[:2], ['2', '选版'])
        for key in ('make', 'final'):
            tab = dialog.locator(f'[data-photo-tab="{key}"]')
            self.assertTrue(tab.is_disabled())
            self.assertEqual(tab.get_attribute('title'), '先在「选版」里选一版开始做')
        self.assertEqual(dialog.locator('[data-photo-resolve]').count(), 0)  # one step at a time
        dialog.locator('[data-photo-choose]').click()
        dialog.locator('[data-photo-tab="make"][aria-selected="true"]').wait_for()  # choosing moves on to 制作
        dialog.locator('.photo-editor').wait_for()
        self.assertIn('B 版 · 1 张', dialog.locator('[data-photo-tab="make"]').inner_text())
        self.assertIn('done', dialog.locator('[data-photo-tab="plan"]').get_attribute('class'))
        dialog.locator('[data-photo-next="final"]').click()
        dialog.locator('[data-photo-export]').click()
        dialog.locator('[data-photo-export]', has_text='再导出一次').wait_for()
        self.assertEqual(self.current_step(dialog), 'final')
        self.assertIn('1/1 版已定稿', dialog.locator('[data-photo-tab="final"]').inner_text())
        dialog.locator('[data-photo-tab="final"]').press('Home')  # arrow keys and Home/End move between open steps
        self.assertEqual(self.current_step(dialog), 'video')
        dialog.locator('[data-close]').click()
        dialog = self.open()
        dialog.locator('[data-photo-export]').wait_for()  # a finalized set opens on 定稿
        self.assertEqual(self.current_step(dialog), 'final')

    def test_step_bar_stays_pinned_and_finished_sets_can_still_move_between_steps(self):
        self.collage()
        self.page.evaluate("() => Object.assign(window.detail,{finished_at:'2026-11-08T12:00:00+00:00'})")
        dialog = self.open()
        dialog.locator('.photo-finished-strip').wait_for()
        self.assertTrue(dialog.locator('[data-photo-undo]').is_disabled())
        dialog.locator('[data-photo-body]').evaluate('(el) => el.scrollTop = el.scrollHeight')
        bar, body = dialog.locator('.photo-steps').bounding_box(), dialog.locator('[data-photo-body]').bounding_box()
        self.assertAlmostEqual(bar['y'], body['y'], delta=1)
        self.assertGreater(bar['height'], 60)  # a long step must not squeeze the bar
        self.step(dialog, 'final')
        self.step(dialog, 'video')
        self.assertTrue(dialog.locator('[data-photo-next="plan"]').count() == 0)
        self.page.set_viewport_size({'width':390,'height':850})
        self.assertTrue(dialog.evaluate('(el) => el.scrollWidth <= el.clientWidth'))


if __name__ == '__main__':
    unittest.main()
