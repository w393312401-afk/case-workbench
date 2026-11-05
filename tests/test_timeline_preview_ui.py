"""The publishing page plays current timeline previews without replacing unsaved publication inputs."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parents[1] / "static"


class TimelinePreviewBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="timeline-preview-ui-")
        cls.addClassCleanup(cls.temp.cleanup)
        video = Path(cls.temp.name) / "fixture.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=90x160:d=3",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(video)],
                       check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        cls.video = video.read_bytes()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.route("**/*", lambda route: route.fulfill(content_type="text/html", body='<main><section id="publication"></section></main>'))
        def serve_video(route):
            start, end, status = 0, len(self.video) - 1, 200
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", route.request.headers.get("range", ""))
            if match:
                start, end, status = int(match[1]), min(int(match[2]) if match[2] else end, end), 206
            headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1)}
            if status == 206:
                headers["Content-Range"] = f"bytes {start}-{end}/{len(self.video)}"
            route.fulfill(status=status, content_type="video/mp4", headers=headers, body=self.video[start:end + 1])
        self.page.route("**/preview/video?*", serve_video)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual([], self.errors))
        self.page.goto("http://127.0.0.1:43219/timeline-preview-test")
        for name in ("style.css", "buttons.css", "production.css", "video-publication.css"):
            self.page.add_style_tag(path=str(STATIC / name))
        self.page.add_script_tag(path=str(STATIC / "video-publication.js"))
        self.page.evaluate("""async () => {
          window.previewCalls = [];window.previews = {one:{status:'idle'},two:{status:'idle'}};window.toast = () => {};
          window.job = {id:'PRD-111111111111',timelines:[{unique_id:'one',name:'First',project:'202H'},{unique_id:'two',name:'Second',project:'202H'}]};
          window.api = async (path,options) => {
            if (path.startsWith('/api/content/calendar')) return {calendar_date:'2026-11-04'};
            if (path.startsWith('/api/performance/videos')) return {videos:[]};
            if (path.includes('/preview')) {
              const uid = options ? options.body.timeline_uid : new URL('http://local'+path).searchParams.get('timeline');
              if (options) {window.previewCalls.push(structuredClone(options.body));window.previews[uid] = {...window.previews[uid],status:'running',message:'正在导出视频预览…',percent:35};}
              return structuredClone(window.previews[uid]);
            }
            const uid = new URL('http://local'+path).searchParams.get('timeline') || 'two';
            return {revision:'revision',source_signature:'signature',timeline_uid:uid,can_register:true,history:[],
              timelines:[{unique_id:'two',name:'Second'},{unique_id:'one',name:'First'}],
              source:{script:{script_version:1},take:{id:'TAK-111111111111'},timeline:{unique_id:uid,name:uid==='one'?'First':'Second'}},copy:{titles:[]}};
          };
          await VideoPublication.mount(document.querySelector('#publication'),{job:window.job,api:window.api});
        }""")
        self.page.locator('[data-preview-import]').wait_for()

    def ready(self, uid="two", version="one", kind="半成品预览"):
        self.page.evaluate("""({uid,version,kind}) => {window.previews[uid] = {status:'done',message:'预览已更新。',video:{
          version,name:uid==='one'?'First.mp4':'Second.mp4',kind,generated_at:'2026-11-04T10:00:00Z',
          url:`/api/production/jobs/PRD-111111111111/preview/video?timeline=${uid}&version=${version}`}};}""", {"uid":uid,"version":version,"kind":kind})

    def test_import_plays_the_selected_partial_timeline_and_preserves_the_typed_post_id(self):
        self.page.locator('[name="post_id"]').fill('7999999999999999901')
        self.page.locator('[data-preview-import]').click()
        self.assertTrue(self.page.locator('[data-preview-import]').is_disabled())
        self.assertIn('35%', self.page.locator('[data-preview-progress]').inner_text())
        self.assertEqual(self.page.evaluate('window.previewCalls'), [{'timeline_uid':'two'}])
        self.ready()
        self.page.locator('[data-preview-kind]', has_text='半成品预览').wait_for()
        player = self.page.locator('[data-timeline-preview] video')
        self.page.wait_for_function("document.querySelector('[data-timeline-preview] video').readyState >= 2")
        player.evaluate('async (video) => {video.muted=true;await video.play();video.currentTime=1;}')
        self.page.wait_for_function("document.querySelector('[data-timeline-preview] video').currentTime > 1.05")
        self.assertEqual(self.page.locator('[name="post_id"]').input_value(), '7999999999999999901')
        self.assertIn('Second.mp4', self.page.locator('[data-preview-video]').inner_text())

    def test_final_preview_and_each_timeline_keep_their_own_video(self):
        self.ready('one', 'first-final-web1', '成片预览')
        self.page.evaluate("window.previews.one.video.download_url='/api/production/jobs/PRD-111111111111/preview/video?timeline=one&version=first-final'")
        self.page.locator('[data-publication-timeline="one"]').click()
        self.page.locator('[data-preview-kind]', has_text='成片预览').wait_for()
        self.assertIn('timeline=one', self.page.locator('[data-timeline-preview] video').get_attribute('src'))
        download = self.page.get_by_role('link', name='下载高清成片')
        self.assertTrue(download.get_attribute('href').endswith('version=first-final'))
        self.assertEqual(download.get_attribute('download'), 'First.mp4')
        self.page.locator('[data-publication-timeline="two"]').click()
        self.assertFalse(self.page.locator('[data-preview-video]').is_visible())
        self.assertIsNone(self.page.locator('[data-timeline-preview] video').get_attribute('src'))

    def test_automatic_export_reports_progress_and_updates_the_player_without_another_click(self):
        self.page.evaluate("window.previews.two={status:'running',automatic:true,message:'人工确认已保存，正在排队导出成片…'}")
        self.page.locator('[data-publication-timeline="one"]').click()
        self.page.locator('[data-publication-timeline="two"]').click()
        button = self.page.get_by_role('button', name='正在自动导出成片…', exact=True)
        self.assertTrue(button.is_disabled())
        self.assertIn('排队导出成片', self.page.locator('[data-preview-progress]').inner_text())
        self.ready(kind='成片预览')
        self.page.locator('[data-preview-kind]', has_text='成片预览').wait_for()
        self.assertTrue(self.page.get_by_role('link', name='下载高清成片').is_visible())
        self.assertEqual(self.page.evaluate('window.previewCalls'), [])

    def test_failed_update_keeps_previous_preview_and_retry_changes_the_video_url(self):
        self.ready()
        self.page.locator('[data-publication-timeline="one"]').click()
        self.page.locator('[data-publication-timeline="two"]').click()
        player = self.page.locator('[data-timeline-preview] video')
        old = player.get_attribute('src')
        self.page.locator('[data-preview-import]').click()
        self.page.evaluate("window.previews.two.status = 'failed';window.previews.two.error = '素材离线，请重新链接。'")
        self.page.locator('[data-preview-error]', has_text='素材离线').wait_for()
        self.assertEqual(player.get_attribute('src'), old)
        self.page.locator('[data-preview-import]').click()
        self.ready(version='second')
        self.page.wait_for_function("document.querySelector('[data-timeline-preview] video').getAttribute('src').includes('version=second')")

    def test_preview_fits_light_dark_and_narrow_windows(self):
        self.ready()
        self.page.locator('[data-publication-timeline="one"]').click()
        self.page.locator('[data-publication-timeline="two"]').click()
        player = self.page.locator('[data-timeline-preview] video')
        for scheme, width in (('light',1280),('dark',1280),('dark',390)):
            self.page.emulate_media(color_scheme=scheme)
            self.page.set_viewport_size({'width':width,'height':900})
            self.assertTrue(player.is_visible())
            self.assertTrue(player.evaluate('(el) => el.getBoundingClientRect().right <= innerWidth'))

    def test_optimized_playback_keeps_the_position_of_the_same_video(self):
        self.ready(version='original')
        self.page.evaluate("window.previews.two.status='optimizing';window.previews.two.message='正在准备流畅播放的视频…'")
        self.page.locator('[data-publication-timeline="one"]').click()
        self.page.locator('[data-publication-timeline="two"]').click()
        player = self.page.locator('[data-timeline-preview] video')
        self.assertEqual(player.get_attribute('preload'), 'auto')
        self.page.wait_for_function("document.querySelector('[data-timeline-preview] video').readyState >= 2")
        player.evaluate('video => video.currentTime = 1.2')
        self.page.wait_for_function("!document.querySelector('[data-timeline-preview] video').seeking")
        self.ready(version='original-web1')
        self.page.evaluate("window.previews.two.video.source_version='original'")
        self.page.wait_for_function("document.querySelector('[data-timeline-preview] video').getAttribute('src').includes('original-web1')")
        self.page.wait_for_function("document.querySelector('[data-timeline-preview] video').currentTime >= 1.15")
        self.assertTrue(player.evaluate('video => video.paused'))


if __name__ == '__main__':
    unittest.main()
