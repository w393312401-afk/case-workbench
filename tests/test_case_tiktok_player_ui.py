"""案例详情优先用 TikTok 官方播放器播放；打不开时改用本地视频，文案逐句跳转照常可用。"""
from __future__ import annotations

import copy
import unittest

import test_server as fixture
import test_ui_regressions as browser


VIDEO_ID = "7664986913497009439"

# 假的 TikTok 播放器：按官方 postMessage 协议回报状态，并把收到的指令回显给页面，便于断言。
FAKE_PLAYER = """<!doctype html><meta charset="utf-8"><body style="margin:0;background:#000"><script>
const send = message => parent.postMessage({...message, "x-tiktok-player": true}, "*");
let time = 0, timer = 0;
const tick = () => send({type: "onCurrentTime", value: {currentTime: time, duration: 30}});
addEventListener("message", event => {
  const data = event.data || {};
  send({type: "__echo", value: {type: data.type, value: data.value, flag: data["x-tiktok-player"]}});
  if (data.type === "play") { send({type: "onStateChange", value: 1}); clearInterval(timer); timer = setInterval(() => { time += 0.25; tick(); }, 250); }
  if (data.type === "pause") { clearInterval(timer); send({type: "onStateChange", value: 2}); }
  if (data.type === "seekTo") { time = Number(data.value); tick(); }
});
MODE
</script>"""
READY = 'send({type: "onPlayerReady"}); tick();'
BROKEN = 'send({type: "onPlayerError", value: {errorCode: 1001, errorType: "INVALID_VIDEO"}});'


class CaseTikTokPlayerTests(unittest.TestCase):
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    setUp = browser.WorkbenchBrowserTests.setUp
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video
    reveal_player = browser.WorkbenchBrowserTests.reveal_player
    set_comparison_view = browser.WorkbenchBrowserTests.set_comparison_view

    def open_case(self, mode=READY, init_script=""):
        context = self.browser.new_context(viewport={"width": 1080, "height": 900})
        self.addCleanup(context.close)
        self.requested = []

        def fulfill(route):
            self.requested.append(route.request.url)
            route.fulfill(body=FAKE_PLAYER.replace("MODE", mode), content_type="text/html")

        context.route("https://www.tiktok.com/player/v1/**", fulfill)
        if init_script:
            context.add_init_script(init_script)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        page.add_init_script("""window.__tiktok = []; addEventListener("message", e => {
            if (e.data && e.data.type === "__echo") window.__tiktok.push(e.data.value); });""")
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#case/{self.identifier}")
        page.locator("#connection-text").get_by_text("案例库同步").wait_for()
        self.reveal_player(page)
        return page

    def timed_transcript(self):
        original = self.store.read_case(self.identifier)
        current = copy.deepcopy(original["current"])
        current["transcript"]["text"] = "\n".join(f"[{i * 3}.00s - {i * 3 + 2.5:.2f}s] Line number {i + 1}." for i in range(8))
        current.pop("translation_zh", None)
        self.store.append(self.identifier, {"kind": "editorial", "text": "带时间戳文案", "current": current})

    def commands(self, page, kind):
        return page.evaluate(f"window.__tiktok.filter(c => c.type === {kind!r})")

    def test_plays_tiktok_by_default_and_transcript_drives_it(self):
        self.timed_transcript()
        page = self.open_case()
        frame = page.locator("#case-player.tiktok-player iframe")
        frame.wait_for()
        self.assertIn(f"/player/v1/{VIDEO_ID}?", frame.get_attribute("src"))
        page.wait_for_function("() => document.querySelector('#case-player').dataset.ready === 'true'")
        self.assertEqual(page.locator("#case-video-source").inner_text(), "改用本地")
        self.assertTrue(page.locator(".segment-speed-label").is_hidden())

        self.set_comparison_view(page, False)
        page.locator("#transcript-original .cue").nth(2).click()
        page.wait_for_function("() => window.__tiktok.some(c => c.type === 'seekTo')")
        seek = self.commands(page, "seekTo")[-1]
        self.assertAlmostEqual(seek["value"], 6.01, delta=0.05)
        self.assertTrue(seek["flag"])
        page.locator("#transcript-original .cue.is-active").get_by_text("Line number 3.").wait_for()

        # 点时间段循环：跳到起点并开始播放，播放器回报的进度推动高亮。
        page.locator("#transcript-original .cue").nth(4).locator(".cue-time").click()
        page.wait_for_function("() => window.__tiktok.some(c => c.type === 'play')")
        page.wait_for_function("() => !document.querySelector('#case-player').paused")
        page.locator("#transcript-original .cue.is-active").get_by_text("Line number 5.").wait_for()

        # 收起视频会暂停 TikTok 播放器。
        page.get_by_role("button", name="收起", exact=True).click()
        page.wait_for_function("() => window.__tiktok.some(c => c.type === 'pause')")

    def test_falls_back_to_local_video_when_tiktok_fails(self):
        page = self.open_case(mode=BROKEN)
        page.locator("video#case-player").wait_for()
        self.assertEqual(page.locator(".video-panel .player-wrap").get_attribute("data-source"), "local")
        self.assertEqual(page.locator("#case-video-source").inner_text(), "改用 TikTok")
        page.get_by_text("TikTok 播放器没打开，已改用本地视频（原因：TikTok 报错 1001 INVALID_VIDEO）。").wait_for()
        self.assertEqual(page.locator("iframe").count(), 0)

    def test_switch_is_remembered_across_visits(self):
        page = self.open_case()
        page.locator("#case-player.tiktok-player").wait_for()
        page.locator("#case-video-source").click()
        page.locator("video#case-player").wait_for()
        self.assertEqual(page.evaluate("localStorage.getItem('case-video-source')"), "local")
        page.reload()
        page.locator("#connection-text").get_by_text("案例库同步").wait_for()
        self.reveal_player(page)
        page.locator("video#case-player").wait_for()
        self.assertEqual(page.locator("#case-video-source").inner_text(), "改用 TikTok")
        page.locator("#case-video-source").click()
        page.locator("#case-player.tiktok-player iframe").wait_for()
        self.assertEqual(page.evaluate("localStorage.getItem('case-video-source')"), "tiktok")

    def test_case_without_local_video_still_plays_tiktok(self):
        self.video.unlink()
        page = self.open_case()
        page.locator("#case-player.tiktok-player iframe").wait_for()
        self.assertTrue(page.locator("#case-video-source").is_hidden())

    def test_page_policy_allows_only_the_tiktok_player_frame(self):
        status, headers, _ = self.request("GET", "/")
        policy = headers["Content-Security-Policy"]
        self.assertIn("frame-src https://www.tiktok.com;", policy)
        self.assertIn("frame-ancestors 'none'", policy)


if __name__ == "__main__":
    unittest.main()
