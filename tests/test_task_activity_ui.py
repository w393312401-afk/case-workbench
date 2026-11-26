"""Isolated activity UI checks: no task execution or real notification permission."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import re
import unittest
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ACTIVE = {"queued", "running", "cancelling", "recovering"}
NOW = "2026-10-11T04:00:00Z"
# What a browser that never opened the panel starts with: the 要处理 tab, no week strip.
DEFAULT_PREFS = {"state": "needs", "dates": False}


class TaskActivityBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1000, "height": 800})
        self.addCleanup(self.context.close)
        self.requests, self.runs, self.events, self.changes, self.claims, self.read = [], {}, [], [], set(), set()
        self.epoch, self.version, self.available = "fixture-epoch", 0, True
        self.feed_failures = self.claim_failures = 0
        self.context.route("http://activity.test/", lambda route: route.fulfill(body="<!doctype html><html><body></body></html>", content_type="text/html"))
        self.context.route("**/api/activity**", self.respond)
        # The content views behind 待我处理 / 待发布 / 图文: one list per view.
        self.content = {"attention": [], "publish": []}
        self.context.route("**/api/content?**", self.content_view)
        # 图文 to-dos come from the 图文 overview, stuck failures from 需要你出手.
        self.photos = {"items": [], "counts": {}}
        self.stuck = {"items": []}
        self.context.route("**/api/photo-overview**", lambda route: route.fulfill(json=self.photos))
        self.context.route("**/api/needs-you**", lambda route: route.fulfill(json=self.stuck))

    def content_view(self, route):
        # The attention view also carries the posted videos still missing from the publish calendar.
        view = parse_qs(urlparse(route.request.url).query).get("view", [""])[0]
        body = {"items": self.content.get(view, [])}
        if view == "attention" and self.content.get("calendar_pending"):
            body["calendar_pending"] = self.content["calendar_pending"]
        route.fulfill(json=body)

    def add_run(self, run_id="ai:one", product="202H", status="running", *, notify=False, message=None):
        self.version += 1
        run = {"run_id": run_id, "kind": "ai", "owner_id": "AIT-one", "source_id": run_id,
               "product": product, "title": f"{product} 写稿第 1 轮", "status": status,
               "message": message or ("脚本已生成，待查看" if status == "completed" else "正在生成脚本"),
               "phase": "生成", "started_at": "2026-10-11T00:00:00Z", "updated_at": "2026-10-11T00:00:12Z",
               "finished_at": None if status in ACTIVE else "2026-10-11T00:00:12Z", "version": self.version,
               "result_count": 1 if status == "completed" else 0, "event_id": None, "read": False,
               "url": f"#/{product}/ai/AIT-one?round={run_id}"}
        event = None
        if notify:
            run["event_id"] = self.version
            event = {key: run[key] for key in ("event_id", "run_id", "status", "message", "title", "product", "url")}
            event["created_at"] = run["updated_at"]
            self.events.append(event)
        self.runs[run_id] = run
        self.changes.append({"version": self.version, "run": deepcopy(run), "event": event})
        return run

    def counts(self):
        return {"active": sum(row["status"] in ACTIVE for row in self.runs.values()),
                "queued": sum(row["status"] == "queued" for row in self.runs.values()),
                "unread": sum(row["event_id"] not in self.read for row in self.events)}

    def respond(self, route):
        parsed = urlparse(route.request.url)
        params = {key: value[0] for key, value in parse_qs(parsed.query).items()}
        body = route.request.post_data_json if route.request.method == "POST" else {}
        self.requests.append({"path": parsed.path, "params": params, "body": body})
        if parsed.path == "/api/activity" and self.feed_failures:
            self.feed_failures -= 1
            route.fulfill(status=503, json={"error": "fixture offline"})
            return
        if parsed.path.endswith("/notify-claim") and self.claim_failures:
            # What the server answers a tab still holding the token of a previous service start.
            self.claim_failures -= 1
            route.fulfill(status=403, json={"error": "页面连接已过期，请刷新后重试。"})
            return
        if parsed.path.endswith("/notify-claim"):
            claimed = []
            for event_id in body["event_ids"]:
                key = (body["browser_id"], event_id)
                if key not in self.claims and event_id not in self.read:
                    self.claims.add(key)
                    claimed.append(event_id)
            result = {"claimed": claimed}
        elif parsed.path.endswith("/read"):
            self.read.update(row["event_id"] for row in self.events if row["event_id"] <= body["before_event_id"]) if body.get("all") else self.read.update(body["event_ids"])
            result = {"ok": True}
        elif parsed.path.endswith("/logs"):
            run_id = unquote(parsed.path[len("/api/activity/"):-len("/logs")])
            if run_id not in self.runs:
                route.fulfill(status=404, json={"error": "not found"})
                return
            run = {**self.runs[run_id], "read": self.runs[run_id]["event_id"] in self.read}
            logs = [{"seq": n, "at": f"2026-10-11T00:00:{n:02}Z", "status": "running", "phase": "生成", "message": f"步骤 {n} <img src=x onerror=alert(1)>", "result_count": n} for n in range(1, 4)]
            if params.get("tail") or params.get("before_seq"):
                bound = int(params.get("before_seq", 10 ** 9))
                older = [row for row in logs if row["seq"] < bound]
                page = older[-2:]
                result = {"run": run, "logs": page, "has_more": False, "has_earlier": len(older) > 2,
                          "first_seq": page[0]["seq"] if page else bound, "next_seq": logs[-1]["seq"], "available": self.available}
            else:
                after = int(params.get("after_seq", 0))
                result = {"run": run, "logs": [row for row in logs if row["seq"] > after][:2], "has_more": after < 2,
                          "next_seq": min(after + 2, 3), "available": self.available}
        else:
            rows = [dict(row, read=row["event_id"] in self.read) for row in self.runs.values()]
            limit = int(params.get("limit", 50))
            if "after" in params:
                selected = [change for change in self.changes if change["version"] > int(params["after"])][:limit]
                current = selected[-1]["version"] if selected else self.version
                rows = list({change["run"]["run_id"]: change["run"] for change in selected}.values())
                events = [change["event"] for change in selected if change["event"]]
                result = {"items": rows, "events": events, "cursor": current, "has_more": current < self.version}
            else:
                for field in ("product", "owner_id", "source_id"):
                    if params.get(field):
                        rows = [row for row in rows if row[field] == params[field]]
                if params.get("filter") == "active":
                    rows = [row for row in rows if row["status"] in ACTIVE]
                if params.get("filter") == "unread":
                    rows = [row for row in rows if row["event_id"] and not row["read"]]
                if params.get("filter") == "current":
                    since = datetime.fromisoformat(params["since"]) if params.get("since") else None
                    rows = [row for row in rows if row["status"] not in {"completed", "cancelled"}
                            or (since and datetime.fromisoformat(row["finished_at"] or row["updated_at"]) >= since)]
                if params.get("filter") == "range":
                    since, until = datetime.fromisoformat(params["since"]), datetime.fromisoformat(params["until"])
                    rows = [row for row in rows if datetime.fromisoformat(row["started_at"]) < until
                            and (row["status"] in ACTIVE or datetime.fromisoformat(row["finished_at"] or row["updated_at"]) >= since)]
                if params.get("before"):
                    rows = [row for row in rows if row["version"] < int(params["before"])]
                rows.sort(key=lambda row: -row["version"])
                result = {"items": rows[:limit], "events": [], "cursor": self.version, "has_more": len(rows) > limit,
                          "next_before": rows[min(limit, len(rows)) - 1]["version"] if rows else None}
            result.update(counts=self.counts(), epoch=self.epoch, available=self.available)
        route.fulfill(json=result)

    def page(self, *, initialize=True, public=False, activity_prefs=None):
        page = self.context.new_page()
        page.set_default_timeout(5000)
        # The fixtures' runs happen on 2026-10-11 (UTC); the browser's calendar opens on that day.
        page.clock.install(time=NOW)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.goto("http://activity.test/")
        page.set_content('''<header class="topbar"><div class="topbar-right"><button class="secondary activity-trigger" id="task-activity-open">任务<span class="activity-count" data-activity-active hidden></span><span class="activity-count" data-activity-unread hidden></span></button></div></header><select id="global-product"><option value="">全部产品</option><option value="202H" selected>202H</option><option value="129N">129N</option></select><main id="content"></main>''')
        page.add_style_tag(path=str(ROOT / "static/style.css"))
        page.add_style_tag(path=str(ROOT / "static/buttons.css"))
        page.add_style_tag(path=str(ROOT / "static/activity.css"))
        page.add_script_tag(content='''
          window.activityFocused = true; window.activityHidden = false; window.permissionCalls = 0; window.notificationCalls = [];
          Object.defineProperty(document, 'hasFocus', {value: () => window.activityFocused});
          Object.defineProperty(document, 'visibilityState', {get: () => window.activityHidden ? 'hidden' : 'visible'});
          window.Notification = class { static permission = 'default'; static async requestPermission() {window.permissionCalls++; return Notification.permission = window.permissionResult || 'granted';} constructor(title, options) {this.title = title; this.options = options; window.notificationCalls.push(this);} close() {} };
          window.WorkbenchShell = {product:() => document.querySelector('#global-product').value};
          window.activityDelays=[];const activitySetTimeout=window.setTimeout;window.setTimeout=(callback,delay,...args)=>{activityDelays.push(delay);return activitySetTimeout(callback,delay,...args);};
          window.activityApiActive=0;window.activityApiMax=0;window.blockedCalls=[];
          const api = async (path, options = {}) => {window.activityApiActive++;window.activityApiMax=Math.max(activityApiMax,activityApiActive);try {if(window.blockActivity && path.includes('&after=')) await new Promise(resolve=>blockedCalls.push(resolve)); const response = await fetch(path, {...options, headers:{'Content-Type':'application/json'}, body:options.body ? JSON.stringify(options.body) : undefined}); const data = await response.json(); if (!response.ok) {const error = new Error(data.error); error.status = response.status; throw error;} return data;}finally{window.activityApiActive--;}};
        ''')
        if public:
            page.evaluate("window.PublicTextMode = {}")
        # Most tests read runs on the record's week strip, which the panel opens on request; they start there.
        # The panel's own default (要处理, no dates) is asked for with DEFAULT_PREFS; False stores nothing, as in a browser new to the panel.
        if activity_prefs is not False:
            stored = {"state": "history", "dates": True, **(activity_prefs or {})}
            page.evaluate("([prefs, force]) => { if (force || localStorage.getItem('workbench-activity-prefs-v1') === null) localStorage.setItem('workbench-activity-prefs-v1', JSON.stringify(prefs)); }", [stored, activity_prefs is not None])
        page.add_script_tag(path=str(ROOT / "static/activity.js"))
        if initialize:
            page.evaluate("TaskActivity.initialize()")
            page.clock.run_for(2)
            if not public:
                page.wait_for_function("localStorage.getItem('workbench-activity-cursor-v1') !== null")
        return page

    def pick_product(self, page, value):
        page.evaluate("value => { document.querySelector('#global-product').value = value; window.dispatchEvent(new Event('workbench:productchange')); }", value)

    def open_all(self, page):
        page.locator("#task-activity-open").click()

    def refresh(self, page):
        before = len(self.requests)
        page.evaluate("TaskActivity.refresh()")
        page.clock.run_for(2)
        page.wait_for_function("expected => JSON.parse(localStorage.getItem('workbench-activity-cursor-v1') || '{}').cursor === expected", arg=self.version)
        self.assertGreater(len(self.requests), before)

    def test_baseline_and_open_panel_never_clear_unread_and_default_to_current_product(self):
        self.add_run(status="completed", notify=True)
        self.add_run("ai:two", "129N", "completed", notify=True)["owner_id"] = "AIT-two"
        page = self.page()
        self.assertEqual(page.locator(".activity-notice").count(), 0)
        page.locator("#task-activity-open").click()
        # The panel opens on today's page of the calendar, filtered to the top-bar product.
        expect(page.locator(".activity-row")).to_have_count(1)
        # The list follows the top-bar product; there is no separate product picker in the panel.
        expect(page.locator("#activity-product")).to_have_count(0)
        self.assertEqual(self.read, set())
        self.pick_product(page, "")
        expect(page.locator(".activity-row")).to_have_count(2)
        # 全部已读 sits in the 更多 menu with the unread count, and is greyed out once nothing is unread.
        page.locator("[data-activity-menu]").click()
        read_all = page.locator("#activity-read-all")
        expect(read_all.locator("small")).to_have_text("2")
        read_all.click()
        expect(page.locator("#activity-menu")).to_be_hidden()
        expect(page.locator(".activity-row.is-unread")).to_have_count(0)
        expect(read_all).to_be_disabled()
        self.assertEqual(self.read, {1, 2})

    def claim_requests(self):
        return sum(row["path"].endswith("/notify-claim") for row in self.requests)

    def test_expired_page_token_is_renewed_once_before_claiming_again(self):
        self.add_run()
        page = self.page()
        page.evaluate("() => { window.renewCalls = 0; window.renewPageToken = async () => { window.renewCalls++; }; }")
        self.claim_failures = 1
        self.add_run(status="completed", notify=True)
        self.refresh(page)
        expect(page.locator(".activity-notice")).to_have_count(1)
        self.assertEqual((self.claim_requests(), page.evaluate("renewCalls")), (2, 1))

    def test_refused_claims_wait_until_the_tab_is_in_front_again(self):
        self.add_run()
        page = self.page()
        page.evaluate("() => { window.renewCalls = 0; window.renewPageToken = async () => { window.renewCalls++; };"
                      " window.activityFocused = false; window.activityHidden = true; }")
        self.claim_failures = 2
        self.add_run(status="completed", notify=True)
        page.evaluate("TaskActivity.refresh()")
        page.clock.run_for(2)
        page.wait_for_function("() => window.renewCalls === 1")
        page.wait_for_timeout(200)
        self.assertEqual(self.claim_requests(), 2)
        # Background polls keep reading the feed but no longer retry the refused claim every minute.
        for _ in range(3):
            page.clock.run_for(70000)
            page.wait_for_timeout(100)
        self.assertEqual(self.claim_requests(), 2)
        expect(page.locator(".activity-notice")).to_have_count(0)
        page.evaluate("() => { window.activityFocused = true; window.activityHidden = false; }")
        self.refresh(page)
        expect(page.locator(".activity-notice")).to_have_count(1)
        self.assertEqual((self.claim_requests(), page.evaluate("renewCalls")), (3, 1))

    def test_new_background_result_is_silent_clickable_and_claimed_once(self):
        self.add_run()
        page = self.page()
        page.evaluate("() => { localStorage.setItem('workbench-activity-desktop-v1','enabled'); Notification.permission='granted'; window.activityFocused=false; window.activityHidden=true; window.dispatchEvent(new Event('blur')); }")
        self.add_run(status="completed", notify=True)
        self.refresh(page)
        expect(page.locator(".activity-notice")).to_have_count(1)
        self.assertEqual(page.evaluate("notificationCalls.map(item => item.options.silent)"), [True])
        self.assertEqual(page.evaluate("permissionCalls"), 0)
        self.refresh(page)
        self.assertEqual(page.evaluate("notificationCalls.length"), 1)
        page.locator(".activity-notice-view").click()
        page.wait_for_url("**/#/202H/ai/AIT-one?round=ai:one")
        page.evaluate("TaskActivity.didNavigate(location.hash)")
        page.wait_for_timeout(50)
        self.assertIn(2, self.read)

    def stub_audio(self, page, state="running"):
        # Records each note's pitch. A "suspended" context stands for a page nobody has clicked yet.
        page.evaluate("""state => { window.chimes = []; window.audioContexts = 0; window.AudioContext = class { constructor() { window.audioContexts++; window.lastAudio = this; this.state = state; this.currentTime = 0; this.destination = {}; }
          // Like a real browser: a suspended context only resumes from inside a click or key press.
          resume() { if (['pointerdown', 'keydown'].includes(window.event?.type)) this.state = 'running'; return Promise.resolve(); }
          createGain() { return { gain: { setValueAtTime() {}, exponentialRampToValueAtTime() {} }, connect: target => target }; }
          createOscillator() { const tone = { type: '', frequency: { value: 0 }, connect: target => target, start: () => window.chimes.push(tone.frequency.value), stop() {} }; return tone; } }; }""", state)

    def add_done(self, run_id, status, message="结果"):
        run = self.add_run(run_id, "202H", status, notify=True, message=message)
        run["owner_id"] = f"AIT-{run_id}"
        return run

    def test_finished_task_plays_a_chime_and_colors_the_notice_by_outcome(self):
        self.add_run()
        page = self.page()
        self.stub_audio(page)
        self.add_run(status="completed", notify=True)
        self.refresh(page)
        expect(page.locator(".activity-notice.is-done")).to_have_count(1)
        expect(page.locator(".activity-notice-status")).to_have_text("已完成")
        # 完成 rises: the second note is higher.
        notes = page.evaluate("chimes")
        self.assertEqual(len(notes), 2)
        self.assertLess(notes[0], notes[1])
        self.add_run("ai:bad", "129N", "failed", notify=True, message="生成失败")["owner_id"] = "AIT-bad"
        self.refresh(page)
        expect(page.locator(".activity-notice.is-failed")).to_have_count(1)
        notes = page.evaluate("chimes")[2:]
        self.assertGreater(notes[0], notes[1])

    def test_sound_can_be_switched_off(self):
        self.add_run()
        page = self.page()
        self.stub_audio(page)
        page.evaluate("localStorage.setItem('workbench-activity-sound-v1','off')")
        self.add_run(status="completed", notify=True)
        self.refresh(page)
        expect(page.locator(".activity-notice")).to_have_count(1)
        self.assertEqual(page.evaluate("chimes.length"), 0)

    def test_needs_attention_and_interrupted_use_the_warning_chime(self):
        for status in ("needs_attention", "interrupted"):
            with self.subTest(status=status):
                self.runs.clear()
                self.add_run()
                page = self.page()
                self.stub_audio(page)
                self.add_done(f"ai:{status}", status)
                self.refresh(page)
                expect(page.locator(".activity-notice.is-warning")).to_have_count(1)
                # 等你处理: the same note twice.
                notes = page.evaluate("chimes")
                self.assertEqual(len(notes), 2)
                self.assertEqual(notes[0], notes[1])
                page.close()

    def test_cancelled_run_shows_a_grey_notice_without_sound(self):
        self.add_run()
        page = self.page()
        self.stub_audio(page)
        self.add_done("ai:stop", "cancelled", "已取消")
        self.refresh(page)
        expect(page.locator(".activity-notice.is-muted")).to_have_count(1)
        expect(page.locator(".activity-notice-status")).to_have_text("已取消")
        self.assertEqual(page.evaluate("chimes.length"), 0)

    def test_several_results_at_once_chime_once_and_a_failure_wins(self):
        self.add_run()
        page = self.page()
        self.stub_audio(page)
        self.add_done("ai:a", "completed")
        self.add_done("ai:b", "failed", "生成失败")
        self.add_done("ai:c", "completed")
        self.refresh(page)
        expect(page.locator(".activity-notice")).to_have_count(3)
        expect(page.locator(".activity-notice.is-failed")).to_have_count(1)
        expect(page.locator(".activity-notice.is-done")).to_have_count(2)
        notes = page.evaluate("chimes")
        self.assertEqual(len(notes), 2)
        self.assertGreater(notes[0], notes[1])
        # The same results are never announced twice.
        self.refresh(page)
        self.assertEqual(page.evaluate("chimes.length"), 2)

    def test_background_tab_chimes_and_keeps_the_desktop_notice_silent(self):
        self.add_run()
        page = self.page()
        self.stub_audio(page)
        page.evaluate("() => { localStorage.setItem('workbench-activity-desktop-v1','enabled'); Notification.permission='granted'; window.activityFocused=false; window.activityHidden=true; window.dispatchEvent(new Event('blur')); }")
        self.add_done("ai:bg", "completed")
        self.refresh(page)
        expect(page.locator(".activity-notice")).to_have_count(1)
        self.assertEqual(page.evaluate("notificationCalls.map(item => item.options.silent)"), [True])
        self.assertEqual(len(page.evaluate("chimes")), 2)

    def test_sound_waits_until_the_page_has_been_clicked(self):
        self.add_run()
        page = self.page()
        self.stub_audio(page, state="suspended")
        self.add_done("ai:early", "completed")
        self.refresh(page)
        expect(page.locator(".activity-notice")).to_have_count(1)
        # The browser has not allowed sound yet: the notice still shows, nothing errors.
        self.assertEqual(page.evaluate("chimes.length"), 0)
        page.mouse.click(5, 300)
        page.wait_for_function("() => window.lastAudio?.state === 'running'")
        self.assertEqual(page.evaluate("audioContexts"), 1)
        self.add_done("ai:late", "completed")
        self.refresh(page)
        expect(page.locator(".activity-notice")).to_have_count(2)
        self.assertEqual(len(page.evaluate("chimes")), 2)

    def mount_settings(self, page):
        page.evaluate("() => { const box = document.createElement('div'); box.id = 'settings-box'; box.innerHTML = TaskActivity.settingsMarkup(); document.querySelector('#content').append(box); TaskActivity.mountSettings(box); }")

    def test_settings_sound_switch_is_on_by_default_and_remembered(self):
        page = self.page()
        self.stub_audio(page)
        self.mount_settings(page)
        sound = page.locator("#settings-activity-sound")
        expect(sound).to_be_checked()
        sound.uncheck()
        self.assertEqual(page.evaluate("localStorage.getItem('workbench-activity-sound-v1')"), "off")
        self.assertEqual(page.evaluate("chimes.length"), 0)
        # Switching it back on plays a sample so the reader knows what to expect.
        sound.check()
        self.assertEqual(page.evaluate("localStorage.getItem('workbench-activity-sound-v1')"), "on")
        self.assertEqual(len(page.evaluate("chimes")), 2)
        page.evaluate("document.querySelector('#settings-box').remove()")
        page.evaluate("localStorage.setItem('workbench-activity-sound-v1','off')")
        self.mount_settings(page)
        expect(page.locator("#settings-activity-sound")).not_to_be_checked()

    def test_settings_trial_plays_all_three_sounds(self):
        page = self.page()
        self.stub_audio(page)
        self.mount_settings(page)
        page.get_by_role("button", name="试听").click()
        page.clock.run_for(1500)
        notes = page.evaluate("chimes")
        self.assertEqual(len(notes), 6)
        done, failed, warning = notes[0:2], notes[2:4], notes[4:6]
        self.assertLess(done[0], done[1])
        self.assertGreater(failed[0], failed[1])
        self.assertEqual(warning[0], warning[1])

    def background(self, page):
        page.evaluate("""() => { localStorage.setItem('workbench-activity-desktop-v1','enabled'); Notification.permission='granted';
          Notification.prototype.close = function () { this.closed = true; this.onclose?.(); };
          window.activityFocused=false; window.activityHidden=true; window.dispatchEvent(new Event('blur')); }""")

    def test_desktop_notice_stays_until_clicked_and_names_the_outcome(self):
        self.add_run()
        page = self.page()
        self.background(page)
        self.add_done("ai:stay", "failed", "生成失败")
        self.refresh(page)
        expect(page.locator(".activity-notice")).to_have_count(1)
        self.assertEqual(page.evaluate("notificationCalls.map(item => [item.title, item.options.requireInteraction])"), [["失败 · 202H 写稿第 1 轮", True]])
        page.clock.run_for(120000)
        self.assertFalse(page.evaluate("!!notificationCalls[0].closed"))

    def test_reading_the_result_in_the_page_clears_the_desktop_notice(self):
        self.add_run()
        page = self.page()
        self.background(page)
        self.add_done("ai:read", "completed")
        self.refresh(page)
        expect(page.locator(".activity-notice")).to_have_count(1)
        page.evaluate("() => { window.activityFocused=true; window.activityHidden=false; }")
        page.locator(".activity-notice-view").click()
        page.evaluate("TaskActivity.didNavigate(location.hash)")
        page.wait_for_function("() => notificationCalls[0].closed === true")

    def test_desktop_notice_can_go_back_to_closing_by_itself(self):
        self.add_run()
        page = self.page()
        self.background(page)
        page.evaluate("localStorage.setItem('workbench-activity-stay-v1','off')")
        self.add_done("ai:brief", "completed")
        self.refresh(page)
        expect(page.locator(".activity-notice")).to_have_count(1)
        self.assertEqual(page.evaluate("notificationCalls.map(item => item.options.requireInteraction)"), [False])
        page.clock.run_for(21000)
        self.assertTrue(page.evaluate("!!notificationCalls[0].closed"))

    def test_settings_stay_switch_shows_only_with_desktop_notices_on(self):
        page = self.page()
        self.mount_settings(page)
        expect(page.locator("#settings-activity-stay-row")).to_be_hidden()
        page.locator("#settings-activity-enable").click()
        stay = page.locator("#settings-activity-stay")
        expect(stay).to_be_visible()
        expect(stay).to_be_checked()
        page.get_by_role("button", name="发一条试试").click()
        self.assertEqual(page.evaluate("notificationCalls.map(item => [item.title, item.options.requireInteraction])"), [["已完成 · 桌面提醒试一下", True]])
        # Windows may still hide it, so the card says where to look.
        expect(page.locator("#settings-activity-status")).to_contain_text("系统 → 通知")
        stay.uncheck()
        self.assertEqual(page.evaluate("localStorage.getItem('workbench-activity-stay-v1')"), "off")
        page.locator("#settings-activity-disable").click()
        expect(page.locator("#settings-activity-stay-row")).to_be_hidden()

    def test_watch_is_global_while_panel_filters_current_product(self):
        self.add_run()
        page = self.page()
        page.locator("#task-activity-open").click()
        expect(page.locator(".activity-row")).to_have_count(1)
        self.add_run("ai:other", "129N", "failed", notify=True, message="生成失败，请重试")
        self.refresh(page)
        expect(page.locator(".activity-notice")).to_have_count(1)
        expect(page.locator(".activity-row")).to_have_count(1)
        self.assertIn("129N", page.locator(".activity-notice").inner_text())
        self.pick_product(page, "129N")
        expect(page.locator(".activity-row-title")).to_have_text("129N 写稿第 1 轮")

    def test_log_opens_at_newest_lines_pages_back_escaped_and_marks_only_opened_run(self):
        self.add_run(status="completed", notify=True)
        page = self.page(activity_prefs={"mode": "all"})
        page.evaluate("TaskActivity.showLogs('ai:one')")
        expect(page.locator(".activity-log-lines li")).to_have_count(2)
        self.assertEqual([line.split(" <")[0] for line in page.locator(".activity-log-text").all_text_contents()], ["步骤 2", "步骤 3"])
        self.assertEqual(page.locator(".activity-log-lines img").count(), 0)
        self.assertIn("12 秒", page.locator(".activity-log-summary").inner_text())
        page.locator("[data-log-more]").click()
        expect(page.locator(".activity-log-lines li")).to_have_count(3)
        self.assertEqual([line.split(" <")[0] for line in page.locator(".activity-log-text").all_text_contents()], ["步骤 1", "步骤 2", "步骤 3"])
        expect(page.locator("[data-log-more]")).to_have_count(0)
        self.assertEqual(self.read, {1})

    def test_opening_the_panel_reads_nothing_until_a_run_is_opened(self):
        self.add_run(status="completed", notify=True)
        page = self.page(activity_prefs={"mode": "all"})
        self.open_all(page)
        # No log is previewed: the list has the whole panel, and nothing is selected or read.
        expect(page.locator(".activity-row.is-unread")).to_have_count(1)
        expect(page.locator(".activity-row.is-selected")).to_have_count(0)
        expect(page.locator("#activity-detail")).to_be_hidden()
        self.assertEqual(self.read, set())
        page.locator("[data-activity-log='ai:one']").click()
        expect(page.locator("#activity-detail-title")).to_have_text("202H 写稿第 1 轮")
        expect(page.locator(".activity-log-lines li")).to_have_count(2)
        page.wait_for_function("activityApiActive === 0")
        self.assertEqual(self.read, {1})

    def test_search_hides_behind_an_icon_and_replaces_the_calendar(self):
        self.add_run("run:a", status="completed", message="完成")
        page = self.page()
        page.locator("#task-activity-open").click()
        expect(page.locator("#activity-kind")).to_have_count(0)
        search, week = page.locator("#activity-search"), page.locator("#activity-week")
        expect(search).to_be_hidden()
        expect(week).to_be_visible()
        page.locator("[data-activity-search]").click()
        expect(search).to_be_focused()
        search.fill("变体")
        page.clock.run_for(300)
        expect(week).to_be_hidden()
        page.wait_for_function("activityApiActive === 0")
        feed = next(row["params"] for row in reversed(self.requests) if row["params"].get("filter"))
        self.assertEqual((feed.get("q"), feed["filter"]), ("变体", "all"))
        # The search survives a fresh visit, then Esc closes it, clears it and brings the calendar back.
        page.close()
        page = self.page()
        page.locator("#task-activity-open").click()
        search, week = page.locator("#activity-search"), page.locator("#activity-week")
        expect(search).to_have_value("变体")
        expect(week).to_be_hidden()
        search.press("Escape")
        expect(search).to_be_hidden()
        expect(week).to_be_visible()
        expect(page.locator("#task-activity-dialog")).to_be_visible()

    def test_the_panel_is_one_column_and_widens_only_while_a_log_is_open(self):
        self.add_run("run:a", status="completed", message="完成")
        page = self.page()
        page.locator("#task-activity-open").click()
        width = "document.querySelector('#task-activity-dialog').getBoundingClientRect().width"
        self.assertLessEqual(page.evaluate(width), 520)
        # There is no wide mode to switch to: 更多 holds 置顶, 我的筛选 and 全部已读.
        page.locator("[data-activity-menu]").click()
        expect(page.locator("#activity-menu button")).to_have_count(3)
        expect(page.locator("#activity-menu [data-activity-expand]")).to_have_count(0)
        page.keyboard.press("Escape")
        # Opening a log widens the panel; the log sits left of the list, which keeps the right edge.
        page.locator("[data-activity-log='run:a']").click()
        expect(page.locator("#activity-detail-title")).to_be_visible()
        page.wait_for_function(f"{width} > 800")
        log, runs = page.locator("#activity-detail").bounding_box(), page.locator(".activity-runs").bounding_box()
        self.assertLess(log["x"], runs["x"])
        page.locator(".activity-detail-close").click()
        page.wait_for_function(f"{width} <= 520")
        expect(page.locator("#activity-detail")).to_be_hidden()
        # A narrow window shows the log in the list's place, with a way back.
        page.set_viewport_size({"width": 700, "height": 800})
        page.locator("[data-activity-log='run:a']").click()
        expect(page.locator(".activity-runs")).to_be_hidden()
        expect(page.locator(".activity-detail-close")).to_be_hidden()
        page.locator(".activity-back").click()
        expect(page.locator(".activity-runs")).to_be_visible()
        expect(page.locator("#activity-detail")).to_be_hidden()

    def set_times(self, run_id, started, finished):
        self.runs[run_id].update(started_at=started, finished_at=finished, updated_at=finished or started)

    def test_week_strip_marks_each_day_and_open_work_waits_in_its_own_tab(self):
        self.add_run("ai:wait", status="needs_attention", notify=True)["owner_id"] = "AIT-wait"
        self.add_run("ai:busy", status="running")["owner_id"] = "AIT-busy"
        self.add_run("ai:old", status="completed")["owner_id"] = "AIT-old"
        self.set_times("ai:old", "2026-10-08T03:00:00Z", "2026-10-08T03:05:00Z")
        page = self.page()
        page.locator("#task-activity-open").click()
        days = page.locator("[data-activity-date]")
        expect(days).to_have_count(7)
        expect(page.locator(".activity-week-label")).to_have_text("10 月 5–11 日")
        today = page.locator('[data-activity-date][aria-pressed="true"]')
        expect(today).to_contain_text("今天")
        # Each day shows its count, a yellow mark while something waits on you and an accent one while something runs.
        marks = lambda day: day.locator(".activity-week-count i").evaluate_all("nodes => nodes.map(node => node.className)")
        expect(today.locator(".activity-week-count")).to_have_text("2 项")
        self.assertEqual(marks(today), ["is-warning", "is-active"])
        expect(days.nth(3).locator(".activity-week-count")).to_have_text("1 项")
        self.assertEqual(marks(days.nth(3)), [])
        expect(page.locator(".activity-row")).to_have_count(2)
        expect(page.locator("[data-activity-week='1']")).to_be_disabled()
        # Another day of the week is already loaded. It lists that day only: what still waits is counted on its own tab.
        feeds = lambda: sum(1 for row in self.requests if row["params"].get("filter"))
        before = feeds()
        days.nth(3).click()
        expect(page.locator(".activity-section").first).to_contain_text("10 月 8 日 周四")
        expect(page.locator(".activity-row")).to_have_count(1)
        expect(page.locator('[data-activity-filter="needs"] small')).to_have_text("1")
        expect(page.locator('[data-activity-filter="running"] small')).to_have_text("1")
        self.assertEqual(feeds(), before)
        expect(page.locator("[data-activity-today]")).to_be_visible()
        # Another week is read from the service; the weekday is kept.
        page.locator("[data-activity-week='-1']").click()
        expect(page.locator(".activity-week-label")).to_have_text("9/28 – 10/4")
        page.wait_for_function("activityApiActive === 0")
        week = next(row["params"] for row in reversed(self.requests) if row["params"].get("filter") == "range")
        self.assertEqual((week["since"][:10], week["until"][:10]), ("2026-09-27", "2026-10-04"))
        expect(page.locator('[data-activity-date][aria-pressed="true"]')).to_contain_text("1")
        page.locator("[data-activity-today]").click()
        expect(page.locator('[data-activity-date][aria-pressed="true"]')).to_contain_text("今天")
        # 要处理 and 进行中 list the open runs whatever day they started.
        page.locator('[data-activity-filter="needs"]').click()
        expect(page.locator("#activity-week")).to_be_hidden()
        expect(page.locator(".activity-row")).to_have_count(1)
        expect(page.locator("[data-run-id='ai:wait']")).to_have_count(1)
        page.locator('[data-activity-filter="running"]').click()
        expect(page.locator("[data-run-id='ai:busy']")).to_have_count(1)
        self.assertEqual(self.read, set())

    def test_waiting_work_comes_from_the_content_views_and_names_the_next_step(self):
        self.add_run("edit:cut", status="completed")["owner_id"] = "PRD-cut"
        self.content["attention"] = [
            {"id": "video:PRD-cut", "title": "S2 变体待核对", "timeline_name": "Lab-1", "product": "202H", "stage_label": "待人工审查",
             "next_label": "核对剪辑结果", "next_hash": "#production/PRD-cut?step=edit", "updated_at": "2026-10-09T03:00:00Z", "production_ids": ["PRD-cut"]},
            {"id": "script:SCR-new", "title": "S1 #2 新稿", "product": "202H", "stage_label": "已采纳", "next_label": "开始制作",
             "next_hash": "#content/SCR-new", "updated_at": "2026-10-08T03:00:00Z"}]
        self.content["publish"] = [{"id": "video:PRD-cut", "title": "重复的同一单", "next_label": "登记发布", "production_ids": ["PRD-cut"]}]
        page = self.page(activity_prefs=DEFAULT_PREFS)
        page.locator("#task-activity-open").click()
        # 要处理 lists each to-do once, whatever day its order last ran, under a heading that names the next step.
        rows = page.locator(".activity-work-row")
        expect(rows).to_have_count(2)
        expect(page.locator(".activity-bucket")).to_have_text(["核对剪辑结果 · 1", "开始制作 · 1"])
        cut = rows.filter(has_text="Lab-1 · S2 变体待核对")
        expect(cut.locator("a")).to_have_attribute("href", "#production/PRD-cut?step=edit")
        expect(rows.filter(has_text="S1 #2 新稿").locator("a")).to_have_attribute("href", "#content/SCR-new")
        # A to-do with nothing more to read has no arrow.
        expect(rows.filter(has_text="S1 #2 新稿").locator("[data-activity-peek]")).to_have_count(0)
        # In the record, the order still says what to do next instead of its last run's outcome.
        page.locator('[data-activity-filter="history"]').click()
        row = page.locator("[data-run-id='edit:cut']")
        expect(row.locator(".activity-status")).to_have_text("核对剪辑结果")
        expect(page.locator(".activity-work-row")).to_have_count(0)
        page.locator("[data-activity-dates]").click()
        expect(page.locator('[data-activity-date][aria-pressed="true"] .activity-week-count i')).to_have_class("is-warning")

    def test_a_writing_run_showing_its_orders_next_step_opens_that_order(self):
        # The draft became an order that still needs a voice-over: the writing run says 制作配音, so it opens the order.
        run = self.add_run("ai:write", status="completed")
        run.update(owner_id="AIT-w", url="#/202H/ai/AIT-w?round=AIR-1")
        self.content["attention"] = [
            {"id": "video:PRD-v", "title": "第 2 稿｜视频", "product": "202H", "stage_label": "待配音", "next_label": "制作配音",
             "next_hash": "#production/PRD-v?step=voice", "updated_at": "2026-10-11T00:00:12Z",
             "production_ids": ["PRD-v"], "task_ids": ["AIT-w"]}]
        page = self.page()
        page.locator("#task-activity-open").click()
        row = page.locator("[data-run-id='ai:write']")
        expect(row.locator(".activity-status")).to_have_text("制作配音")
        go = row.locator("[data-activity-result]")
        expect(go).to_have_attribute("title", "制作配音")
        go.click()
        page.wait_for_function("location.hash === '#production/PRD-v?step=voice'")
        # Clicking the row itself also opens the order, not the writing log.
        page.evaluate("history.replaceState(null, '', '#ai')")
        page.locator("#task-activity-open").click()
        page.locator("[data-run-id='ai:write'] [data-activity-log]").click()
        page.wait_for_function("location.hash === '#production/PRD-v?step=voice'")

    def test_a_writing_task_with_several_rounds_opens_its_order_from_the_row(self):
        # Two rounds group under the task; its draft's order waits for review, so the row opens the order's edit step.
        for run_id in ("ai:r1", "ai:r2"):
            self.add_run(run_id, status="completed")["owner_id"] = "AIT-w"
        self.content["attention"] = [
            {"id": "video:PRD-c", "title": "第 3 稿｜视频", "product": "202H", "stage_label": "待人工审查", "next_label": "核对剪辑结果",
             "next_hash": "#production/PRD-c?step=edit", "updated_at": "2026-10-11T00:00:12Z",
             "production_ids": ["PRD-c"], "task_ids": ["AIT-w"]}]
        page = self.page()
        page.locator("#task-activity-open").click()
        row = page.locator("[data-activity-group='AIT-w']")
        expect(row).to_contain_text("核对剪辑结果")
        row.click()
        page.wait_for_function("location.hash === '#production/PRD-c?step=edit'")

    def test_a_writing_task_whose_order_has_its_own_row_opens_the_writing_task(self):
        # The order already shows 核对剪辑结果 on its own row; the writing task reads as itself and opens the writing task.
        write = self.add_run("ai:write", status="completed")
        write.update(owner_id="AIT-w", url="#/202H/ai/AIT-w?round=AIR-1")
        self.add_run("edit:cut", status="completed")["owner_id"] = "PRD-c"
        self.content["attention"] = [
            {"id": "video:PRD-c", "title": "第 3 稿｜视频", "product": "202H", "stage_label": "待人工审查", "next_label": "核对剪辑结果",
             "next_hash": "#production/PRD-c?step=edit", "updated_at": "2026-10-11T00:00:12Z",
             "production_ids": ["PRD-c"], "task_ids": ["AIT-w"]}]
        page = self.page()
        page.locator("#task-activity-open").click()
        expect(page.locator("[data-run-id='edit:cut'] .activity-status")).to_have_text("核对剪辑结果")
        row = page.locator("[data-run-id='ai:write']")
        expect(row.locator(".activity-status")).not_to_have_text("核对剪辑结果")
        expect(row.locator("[data-activity-log]")).to_have_attribute("title", "打开写稿任务")
        row.locator("[data-activity-log]").click()
        page.wait_for_function("location.hash === '#/202H/ai/AIT-w?round=AIR-1'")

    def test_an_orders_own_row_still_opens_its_log(self):
        self.add_run("edit:cut", status="completed")["owner_id"] = "PRD-cut"
        self.content["attention"] = [
            {"id": "video:PRD-cut", "title": "剪辑单", "product": "202H", "stage_label": "待人工审查", "next_label": "核对剪辑结果",
             "next_hash": "#production/PRD-cut?step=edit", "updated_at": "2026-10-11T00:00:12Z", "production_ids": ["PRD-cut"]}]
        page = self.page()
        page.locator("#task-activity-open").click()
        main = page.locator("[data-run-id='edit:cut'] [data-activity-log]")
        expect(main).not_to_have_attribute("data-activity-next", re.compile(".*"))
        main.click()
        expect(page.locator("[data-activity-visit]")).to_have_text("核对剪辑结果")

    def test_a_run_with_nothing_waiting_still_opens_its_own_result(self):
        self.add_run("ai:write", status="completed")
        page = self.page()
        page.locator("#task-activity-open").click()
        go = page.locator("[data-run-id='ai:write'] [data-activity-result]")
        expect(go).to_have_attribute("title", "查看结果")
        go.click()
        page.wait_for_function("location.hash === '#/202H/ai/AIT-one?round=ai:write'")

    def test_empty_today_opens_on_the_latest_day_with_runs(self):
        self.add_run("ai:old", status="completed")["owner_id"] = "AIT-old"
        self.set_times("ai:old", "2026-10-02T03:00:00Z", "2026-10-02T03:05:00Z")
        page = self.page()
        page.locator("#task-activity-open").click()
        expect(page.locator(".activity-week-label")).to_have_text("9/28 – 10/4")
        expect(page.locator('[data-activity-date][aria-pressed="true"]')).to_contain_text("2")
        expect(page.locator(".activity-day-note")).to_contain_text("今天还没有任务")
        expect(page.locator(".activity-row")).to_have_count(1)
        # Going back to today shows its empty page, and the note goes away.
        page.locator("[data-activity-today]").click()
        expect(page.locator("#activity-list .activity-empty")).to_have_text("这天没有任务")
        expect(page.locator(".activity-day-note")).to_have_count(0)

    def add_kinds(self):
        """One writing run, one video order that failed, and one 图文 step of another order, all today."""
        self.add_run("ai:write", status="completed")
        cut = self.add_run("edit:cut", status="failed")
        cut.update(kind="edit", owner_id="PRD-cut", title="AI 剪辑 · Lab-1｜剪辑单")
        photo = self.add_run("photo:plan:1", status="completed")
        photo.update(kind="photo", owner_id="PRD-pic", title="图文逐张表 · Lab-2｜图文单", message="3 版逐张表已写好，去选一版")
        self.set_times("photo:plan:1", "2026-10-11T01:00:00Z", "2026-10-11T01:04:00Z")

    def test_photo_work_stays_on_its_own_timeline_and_can_be_finished_in_the_panel(self):
        self.add_kinds()
        self.photos = {"counts": {"doing": 1}, "items": [
            {"job_id": "PRD-pic", "job_title": "图文单", "timeline_uid": "u9", "timeline_name": "Lab-9", "product": "202H", "state": "doing", "since": "2026-10-09T03:00:00Z"}]}
        posts = []
        def decision(route):
            posts.append(route.request.post_data_json)
            self.photos = {"counts": {}, "items": []} if posts[-1]["decision"] == "finish" else {"counts": {"doing": 1}, "items": original}
            route.fulfill(json={"state": "finished"})
        original = self.photos["items"]
        self.context.route("**/api/production/jobs/PRD-pic/photo-decision", decision)
        page = self.page(activity_prefs=DEFAULT_PREFS)
        page.locator("#task-activity-open").click()
        row = page.locator(".activity-work-row").filter(has_text="Lab-9 · 图文单")
        expect(page.locator(".activity-bucket").filter(has_text="继续做图文")).to_have_text("继续做图文 · 1")
        expect(row).to_have_count(1)
        row.hover()
        row.locator("[data-activity-work-finish]").click()
        expect(page.locator(".activity-work-row").filter(has_text="Lab-9")).to_have_count(0)
        expect(page.locator(".activity-finished-note")).to_contain_text("已完结 Lab-9")
        page.locator("[data-activity-work-reopen]").click()
        expect(page.locator(".activity-work-row").filter(has_text="Lab-9")).to_have_count(1)
        expect(page.locator(".activity-finished-note")).to_have_count(0)
        self.assertEqual([post["decision"] for post in posts], ["finish", "reopen"])
        self.assertEqual(posts[0]["timeline"], "u9")
        # Permanent delete is left to 内容进程 and 成片与发布, where it asks first.
        expect(page.locator("#task-activity-dialog").get_by_text("删除", exact=True)).to_have_count(0)
        # In the record, the order's own run (another timeline) does not borrow this timeline's to-do.
        page.locator('[data-activity-filter="history"]').click()
        run_row = page.locator(".activity-row").filter(has_text="Lab-2｜图文单")
        expect(run_row).to_have_count(1)
        expect(run_row).not_to_contain_text("继续做图文")

    def test_tabs_and_type_chips_narrow_the_list(self):
        self.add_kinds()
        self.photos = {"counts": {"pending": 4, "doing": 1}, "items": [
            {"job_id": "PRD-old", "job_title": "旧单", "timeline_uid": "u9", "timeline_name": "Lab-9", "product": "202H", "state": "doing", "since": "2026-10-09T03:00:00Z"},
            {"job_id": "PRD-new", "job_title": "新单", "timeline_uid": "u8", "timeline_name": "Lab-8", "product": "202H", "state": "pending", "since": "2026-10-09T03:00:00Z"}]}
        self.stuck = {"items": [{"kind": "preview", "kind_label": "预览导出失败", "job_id": "PRD-prev", "job_title": "预览单", "timeline_uid": "u7", "timeline_name": "Lab-7",
                                 "product": "202H", "message": "素材离线", "since": "2026-10-10T03:00:00Z", "hash": "#production/PRD-prev?step=publish"}]}
        page = self.page(activity_prefs=DEFAULT_PREFS)
        page.locator("#task-activity-open").click()
        # Three tabs, 要处理 first with its count; 出错 only shows while chosen (the content list's failure card opens it).
        tabs = page.locator("[data-activity-filter]")
        expect(tabs).to_have_text(["要处理3", "进行中", "记录"])
        kind = page.locator("[data-activity-kind]")
        expect(kind.first).to_have_text("全部")
        expect(kind.first).to_have_attribute("aria-pressed", "true")
        # Only kinds with something listed get a chip; nothing here is a 口播审查 or a backup, and no 写稿 waits.
        expect(kind).to_have_text(["全部", "视频2", "图文2"])
        # The 图文 in progress, the stuck export and the failed edit each sit under what they ask for, the newest first.
        expect(page.locator(".activity-bucket")).to_have_text(["AI 剪辑失败 · 1", "预览导出失败 · 1", "继续做图文 · 1"])
        rows = page.locator(".activity-work-row")
        expect(rows).to_have_count(3)
        expect(rows.filter(has_text="Lab-7 · 预览单")).to_contain_text("素材离线")
        expect(rows.filter(has_text="Lab-9 · 旧单")).to_have_count(1)
        # A failure's row opens its log, where the reason is.
        expect(rows.filter(has_text="Lab-1｜剪辑单").locator("[data-activity-log='edit:cut']")).to_have_count(1)
        page.locator('[data-activity-kind="photo"]').click()
        expect(page.locator('[data-activity-kind="photo"]')).to_have_attribute("aria-pressed", "true")
        # Videos nobody decided on are one line, only under 图文.
        expect(rows).to_have_count(2)
        digest = rows.filter(has_text="4 条成片还没决定做不做图文")
        expect(digest.locator("a")).to_have_attribute("href", "#content?view=carousel")
        # A second click on the chosen chip goes back to every kind.
        page.locator('[data-activity-kind="photo"]').click()
        expect(page.locator('[data-activity-kind="all"]')).to_have_attribute("aria-pressed", "true")
        expect(rows).to_have_count(3)
        page.locator('[data-activity-kind="photo"]').click()
        # 记录 lists runs, not to-dos; the chosen type goes with it.
        page.locator('[data-activity-filter="history"]').click()
        record = page.locator(".activity-row")
        expect(record).to_have_count(1)
        expect(record).to_contain_text("Lab-2｜图文单")
        expect(record).to_contain_text("图文逐张表：3 版逐张表已写好")
        expect(page.locator(".activity-work-row")).to_have_count(0)
        # 按日期 opens the week strip and the timeline, which follow the type too.
        expect(page.locator("#activity-week")).to_be_hidden()
        page.locator("[data-activity-dates]").click()
        expect(page.locator('[data-activity-date][aria-pressed="true"] .activity-week-count')).to_have_text("1 项")
        page.get_by_role("button", name="时间轴", exact=True).click()
        expect(page.locator(".activity-block")).to_have_count(1)
        page.get_by_role("button", name="清单", exact=True).click()
        page.locator('[data-activity-filter="running"]').click()
        expect(page.locator("#activity-list .activity-empty")).to_have_text("没有「图文 · 进行中」的任务")
        expect(page.locator("[data-activity-dates]")).to_have_count(0)
        # The chosen tab and type are remembered.
        page.locator('[data-activity-kind="all"]').click()
        page.evaluate("window.TaskActivity.open({state: 'failed'})")
        expect(page.locator('[data-activity-filter="failed"]')).to_have_attribute("aria-pressed", "true")
        page.close()
        page = self.page()
        page.locator("#task-activity-open").click()
        expect(page.locator('[data-activity-filter="failed"]')).to_have_attribute("aria-pressed", "true")
        expect(page.locator(".activity-row")).to_have_count(1)
        expect(page.locator(".activity-work-row")).to_have_count(0)
        expect(page.locator(".activity-row")).to_contain_text("Lab-1｜剪辑单")

    def test_posted_videos_missing_from_the_publish_calendar_wait_in_the_panel(self):
        posted = {"id": "video:PRD-post", "kind": "video", "title": "已发布单", "timeline_name": "Lab-5", "product": "202H", "published_on": "2026-10-09",
                  "next_hash": "#production/PRD-post?step=publish", "production_ids": ["PRD-post"], "updated_at": "2026-10-09T03:00:00Z"}
        self.content["attention"] = [{**posted, "next_label": "核对剪辑结果"}]
        self.content["calendar_pending"] = {"total": 3, "items": [posted]}
        page = self.page(activity_prefs=DEFAULT_PREFS)
        page.locator("#task-activity-open").click()
        rows = page.locator(".activity-work-row")
        expect(rows).to_have_count(3)
        expect(page.locator(".activity-bucket").filter(has_text="标记已上传发布日历")).to_have_text("标记已上传发布日历 · 1")
        mark = rows.filter(has_text="发布于 2026-10-09")
        expect(mark).to_contain_text("Lab-5 · 已发布单")
        expect(mark).to_contain_text("发布于 2026-10-09 · 还没上传到发布日历")
        expect(mark.locator("a")).to_have_attribute("href", "#production/PRD-post?step=publish")
        more = rows.filter(has_text="另有 2 条已发布、还没上传到发布日历")
        expect(more.locator("a")).to_have_attribute("href", "#content?view=all")
        # They belong to 视频, not to 图文. With a single type listed there is nothing to pick, so no type row at all.
        expect(page.locator("[data-activity-kind]")).to_have_count(0)

    def test_waiting_work_is_sorted_by_reason_and_a_backlog_folds_into_one_card(self):
        old, new = "2026-09-30T03:00:00Z", "2026-10-10T03:00:00Z"
        self.photos = {"counts": {"exported": 5}, "items": [
            {"job_id": f"PRD-{n}", "job_title": f"单 {n}", "timeline_uid": f"u{n}", "timeline_name": f"Lab-{n}", "product": "202H", "state": "exported",
             "since": old if n < 3 else new} for n in range(5)]}
        pick = lambda n, title, subtitle: {"id": f"script:SCR-{n}", "title": title, "subtitle": subtitle, "product": "202H", "stage_label": "待选稿",
                                           "next_label": "查看并选择候选", "next_hash": f"#content/SCR-{n}", "updated_at": new}
        self.content["attention"] = [pick(1, "未关联方向", "Hook A"), pick(2, "未关联方向", "Hook B"),
                                     pick(3, "Same Title", "First hook"), pick(4, "Same Title", "Second hook"),
                                     pick(6, "Twin", "Same hook"), pick(7, "Twin", "Same hook"),
                                     {**pick(5, "变体 #1`；`main_difference：S", ""), "next_label": "开始制作"}]
        posts = []
        self.context.route("**/api/production/jobs/*/photo-decision", lambda route: (posts.append(route.request.post_data_json), route.fulfill(json={"state": "finished"})))
        page = self.page(activity_prefs=DEFAULT_PREFS)
        page.locator("#task-activity-open").click()
        # The week strip and 按日期 belong to 记录.
        expect(page.locator("#activity-week")).to_be_hidden()
        expect(page.locator("[data-activity-dates]")).to_have_count(0)
        # One heading per next step, with every row of it in view.
        heading = page.locator(".activity-bucket").filter(has_text="查看并选择候选")
        expect(heading).to_contain_text("查看并选择候选 · 6")
        rows = page.locator(".activity-work-row")
        # Drafts with no direction are named by their opening line; the others carry theirs as a quiet hint.
        untitled = rows.filter(has_text="Hook A")
        expect(untitled.locator(".activity-row-title")).to_have_text("Hook A")
        expect(untitled.locator(".activity-row-hint")).to_have_text("未关联方向")
        first = rows.filter(has_text="First hook")
        expect(first.locator(".activity-row-title")).to_have_text("Same Title")
        expect(rows.filter(has_text="Second hook").locator(".activity-row-title")).to_have_text("Same Title")
        # Rows that would still read the same are numbered.
        self.assertEqual(rows.filter(has_text="Same hook").locator(".activity-row-title").all_text_contents(), ["Twin（1）", "Twin（2）"])
        expect(rows.filter(has_text="main_difference")).to_have_count(0)
        expect(rows.filter(has_text="变体 #1")).to_have_count(1)
        # Five 图文 waiting for Notion become one card below the rest; three of them are over a week old.
        card = page.locator(".activity-bulk")
        expect(card).to_contain_text("上传图文到 Notion · 5 条")
        expect(rows.filter(has_text="Lab-1")).to_have_count(0)
        card.get_by_role("button", name="逐条看").click()
        expect(rows.filter(has_text="Lab-")).to_have_count(5)
        page.get_by_role("button", name="收起").last.click()
        page.once("dialog", lambda dialog: dialog.accept())
        self.photos = {"counts": {"exported": 2}, "items": self.photos["items"][3:]}
        card.get_by_role("button", name="超过 7 天的都完结（3）").click()
        expect(page.locator(".activity-bulk")).to_have_count(0)
        self.assertEqual(sorted(post["timeline"] for post in posts), ["u0", "u1", "u2"])
        self.assertEqual({post["decision"] for post in posts}, {"finish"})

    def test_a_long_group_folds_after_its_first_rows(self):
        self.content["attention"] = [{"id": f"script:SCR-{n}", "title": f"稿 {n}", "product": "202H", "next_label": "开始制作",
                                      "next_hash": f"#content/SCR-{n}", "updated_at": f"2026-10-10T03:{n:02}:00Z"} for n in range(11)]
        page = self.page(activity_prefs=DEFAULT_PREFS)
        page.locator("#task-activity-open").click()
        rows = page.locator(".activity-work-row")
        expect(rows).to_have_count(8)
        # The newest comes first.
        expect(rows.first).to_contain_text("稿 10")
        page.get_by_role("button", name="还有 3 条").click()
        expect(rows).to_have_count(11)
        page.get_by_role("button", name="收起").click()
        expect(rows).to_have_count(8)

    def test_the_panel_opens_on_to_dos_and_shows_a_log_only_on_request(self):
        self.add_run(status="completed")
        # A browser that never opened the panel has nothing stored at all.
        page = self.page(activity_prefs=False)
        self.assertIsNone(page.evaluate("localStorage.getItem('workbench-activity-prefs-v1')"))
        page.locator("#task-activity-open").click()
        expect(page.locator('[data-activity-filter="needs"]')).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#activity-list .activity-empty")).to_have_text("现在没有要你处理的事")
        expect(page.locator("#activity-detail")).to_be_hidden()
        expect(page.locator("#activity-week")).to_be_hidden()
        # Only what is still open was read: no week of history until 记录 is asked for.
        feeds = [row["params"]["filter"] for row in self.requests if row["params"].get("filter")]
        self.assertEqual(set(feeds), {"current"})
        # 记录 lists every run by day, newest first. A finished run's log shows as soon as it is opened.
        page.locator('[data-activity-filter="history"]').click()
        expect(page.locator("[data-activity-day]")).to_contain_text("今天")
        expect(page.locator("[data-activity-dates]")).to_have_attribute("aria-pressed", "false")
        page.locator("[data-activity-log='ai:one']").click()
        expect(page.locator("#activity-detail-title")).to_have_text("202H 写稿第 1 轮")
        expect(page.locator("#activity-log-body .activity-log-lines li").first).to_be_visible()
        expect(page.locator("[data-log-toggle]")).to_have_count(0)
        reading = re.compile(r"\bis-reading\b")
        expect(page.locator("#task-activity-dialog")).to_have_class(reading)
        # Closing the log gives the panel back to the list.
        page.locator("[data-activity-back]:visible").click()
        expect(page.locator("#activity-detail")).to_be_hidden()
        expect(page.locator("#task-activity-dialog")).not_to_have_class(reading)

    def test_a_to_do_row_goes_to_the_work_and_its_arrow_shows_the_specifics(self):
        job = "PRD-0123456789ab"
        run = self.add_run("edit:one", status="needs_attention", notify=True)
        run.update(kind="edit", owner_id=job, source_id="EDT-0123456789ab", title="AI 剪辑 · Lab-1｜玩刀的都懂。", url=f"#/202H/production/{job}?step=edit")
        self.changes[-1]["run"] = deepcopy(run)
        self.content["attention"] = [{"id": f"video:{job}", "title": "S2 #4 结构", "subtitle": "玩刀的都懂。", "timeline_name": "Lab-1", "product": "202H",
                                      "stage_label": "待补素材", "next_label": "补齐素材", "next_hash": f"#production/{job}?step=edit",
                                      "updated_at": "2026-10-11T00:00:12Z", "production_ids": [job]}]
        record = {"id": job, "product": "202H", "selected_take": "TAK-one", "takes": [{"id": "TAK-one", "groups": []}],
                  "timelines": [{"unique_id": "timeline", "take_id": "TAK-one", "fps": "30"}],
                  "edits": [{"id": "EDT-0123456789ab", "timeline_uid": "timeline", "status": "needs_material",
                             "report": {"gaps": [{"start": 30, "end": 60, "need": "桌面上零散的旧磨石"}]}}]}
        reads = []
        self.context.route("**/api/production/jobs/**", lambda route: (reads.append(route.request.method), route.fulfill(json={"job": deepcopy(record)})))
        page = self.page(activity_prefs=DEFAULT_PREFS)
        page.add_script_tag(content="window.esc=value=>String(value??'').replace(/</g,'&lt;'); WorkbenchShell.matchesProduct=(a,b)=>a===b; WorkbenchShell.url=hash=>hash;")
        page.add_script_tag(path=str(ROOT / "static/production-needs.js"))
        page.locator("#task-activity-open").click()
        row = page.locator(".activity-work-row")
        expect(row).to_have_count(1)
        # The heading says what to do; the row says which one, on one line, and links straight to where it is done.
        expect(page.locator(".activity-bucket")).to_have_text("补齐素材 · 1")
        expect(row.locator(".activity-row-title")).to_have_text("Lab-1 · S2 #4 结构")
        expect(row.locator(".activity-row-hint")).to_have_text("玩刀的都懂。")
        expect(row.locator(".activity-status")).to_have_count(0)
        link = row.locator("a.activity-row-main")
        expect(link).to_have_attribute("href", f"#production/{job}?step=edit")
        self.assertLess(row.bounding_box()["height"], 40)
        # Nothing about the order is read until its row is opened.
        self.assertEqual(reads, [])
        expect(page.locator(".activity-peek")).to_have_count(0)
        row.locator("[data-activity-peek]").click()
        need = page.locator(".activity-peek [data-need-key]")
        expect(need).to_contain_text("缺素材")
        expect(need).to_contain_text("桌面上零散的旧磨石")
        expect(page.locator(".activity-peek a.primary")).to_have_text("补齐素材")
        self.assertEqual(reads, ["GET"])
        # A background refresh repaints the list from what was read, without asking again.
        self.refresh(page)
        expect(need).to_contain_text("桌面上零散的旧磨石")
        self.assertEqual(reads, ["GET"])
        self.assertEqual(self.read, set())
        # The arrow closes it again.
        row.locator("[data-activity-peek]").click()
        expect(page.locator(".activity-peek")).to_have_count(0)
        # Going where the work is done reads the order's run once the page has arrived.
        link.click()
        page.wait_for_function(f"location.hash === '#production/{job}?step=edit'")
        self.assertEqual(self.read, set())
        page.evaluate("TaskActivity.didNavigate(location.hash)")
        page.wait_for_function("activityApiActive === 0")
        self.assertEqual(self.read, {run["event_id"]})
        # 看日志, inside the opened row, shows the order's log beside the list.
        page.locator("#task-activity-open").click()
        page.locator("[data-activity-peek]").click()
        page.locator(".activity-peek").get_by_role("button", name="看日志").click()
        expect(page.locator("#activity-detail-title")).to_have_text("AI 剪辑 · Lab-1｜玩刀的都懂。")
        expect(page.locator(".activity-work-row.is-selected")).to_have_count(1)

    def edit_filters(self, page):
        page.locator("[data-activity-menu]").click()
        page.locator("[data-chip-edit]").click()
        expect(page.locator("#activity-menu")).to_be_hidden()

    def test_custom_filters_are_made_changed_and_removed_in_place(self):
        self.add_kinds()
        page = self.page()
        page.locator("#task-activity-open").click()
        self.edit_filters(page)
        expect(page.locator(".activity-chip-empty")).to_have_text("还没有自己的筛选。")
        page.locator("[data-chip-new]").click()
        form = page.locator("[data-chip-form]")
        # Nothing chosen yet: the form says what is missing instead of saving an empty filter.
        form.get_by_role("button", name="保存").click()
        expect(form.locator(".activity-chip-hint")).to_have_text("至少选一个类型或状态，或者填标题含。")
        form.locator('[data-chip-kind="photo"]').click()
        form.locator('[data-chip-kind="edit"]').click()
        form.locator('[data-chip-state="done"]').click()
        form.get_by_role("button", name="保存").click()
        # Named after its conditions when no name is given, and chosen at once in the type picker.
        made = page.locator(".activity-chip.is-custom [data-chip-open]")
        expect(made).to_have_text("剪辑/图文·已完成1")
        expect(page.locator("[data-chip-form]")).to_have_count(0)
        page.locator("[data-chip-done]").click()
        kind = page.locator("[data-activity-kind].is-custom")
        expect(kind).to_have_count(1)
        expect(kind).to_contain_text("剪辑/图文·已完成")
        expect(kind).to_have_attribute("aria-pressed", "true")
        expect(page.locator('[data-activity-filter="history"]')).to_have_attribute("aria-pressed", "true")
        self.assertTrue(kind.get_attribute("data-activity-kind").startswith("c-"))
        expect(page.locator(".activity-row")).to_have_count(1)
        expect(page.locator(".activity-row")).to_contain_text("图文单")
        # Rename it; the choice survives a fresh visit.
        self.edit_filters(page)
        page.locator(".activity-chip.is-custom [data-chip-open]").click()
        page.locator('[data-chip-form] [name="label"]').fill("做完的图文")
        page.locator('[data-chip-form] [name="label"]').press("Enter")
        expect(page.locator(".activity-chip.is-custom")).to_contain_text("做完的图文")
        page.locator("[data-chip-done]").click()
        page.close()
        page = self.page()
        page.locator("#task-activity-open").click()
        expect(page.locator('[data-activity-kind][aria-pressed="true"]')).to_contain_text("做完的图文")
        # Removing the chosen filter falls back to 全部类型.
        self.edit_filters(page)
        page.get_by_role("button", name="删除「做完的图文」").click()
        expect(page.locator(".activity-chip.is-custom")).to_have_count(0)
        page.locator("[data-chip-done]").click()
        expect(page.locator('[data-activity-kind="all"]')).to_have_attribute("aria-pressed", "true")
        expect(page.locator(".activity-row")).to_have_count(3)

    def test_timeline_merges_back_to_back_steps_of_one_order(self):
        for run_id, kind, start, end in (("voice:a", "voice", "00:00", "00:02"), ("timeline:a", "timeline", "00:03", "00:05"), ("edit:a", "edit", "00:06", "00:20"), ("edit:b", "edit", "02:00", "02:10")):
            row = self.add_run(run_id, status="completed")
            row.update(kind=kind, owner_id="PRD-a", title=f"{ {'voice': '配音', 'timeline': '建立时间线', 'edit': 'AI 剪辑'}[kind]} · Lab-9 | 1:1 复刻 | 标题")
            self.set_times(run_id, f"2026-10-11T{start}:00Z", f"2026-10-11T{end}:00Z")
        page = self.page()
        page.locator("#task-activity-open").click()
        page.get_by_role("button", name="时间轴", exact=True).click()
        blocks = page.locator(".activity-block")
        expect(blocks).to_have_count(2)
        expect(blocks.first).to_contain_text("Lab-9")
        expect(blocks.first).to_contain_text("配音 → 建立时间线 → AI 剪辑")
        expect(blocks.first).to_have_attribute("data-run-id", "edit:a")

    def test_timeline_places_runs_by_clock_and_opens_their_logs(self):
        self.add_run("ai:early", status="completed")["owner_id"] = "AIT-early"
        self.set_times("ai:early", "2026-10-11T00:00:00Z", "2026-10-11T00:20:00Z")
        self.add_run("ai:late", status="completed")["owner_id"] = "AIT-late"
        self.set_times("ai:late", "2026-10-11T03:10:00Z", "2026-10-11T03:30:00Z")
        page = self.page()
        page.locator("#task-activity-open").click()
        page.get_by_role("button", name="时间轴", exact=True).click()
        blocks = page.locator(".activity-block")
        expect(blocks).to_have_count(2)
        # 09:00 to 11:00 local had nothing running, so it folds into one thin row.
        expect(page.locator(".activity-hour.is-gap")).to_have_text("09–11 点没有任务")
        early, late = page.locator("[data-run-id='ai:early']").bounding_box(), page.locator("[data-run-id='ai:late']").bounding_box()
        self.assertLess(early["y"] + early["height"], late["y"])
        expect(page.locator("[data-run-id='ai:late']")).to_contain_text("11:10–11:30")
        expect(page.locator(".activity-now")).to_have_count(1)
        page.locator("[data-run-id='ai:late']").click()
        expect(page.locator("[data-run-id='ai:late']")).to_have_class("activity-block is-done is-selected")
        # The view is remembered.
        page.close()
        page = self.page()
        page.locator("#task-activity-open").click()
        expect(page.get_by_role("button", name="时间轴", exact=True)).to_have_attribute("aria-pressed", "true")
        expect(page.locator(".activity-block")).to_have_count(2)

    def test_scoped_history_folds_days_and_returns_to_the_calendar(self):
        self.add_run(status="needs_attention", notify=True)
        page = self.page()
        page.evaluate("TaskActivity.open({owner_id:'AIT-one',label:'当前制作单'})")
        day = page.locator("[data-activity-day]")
        expect(page.locator("#activity-week")).to_be_hidden()
        expect(day).to_have_attribute("aria-expanded", "true")
        expect(page.locator(".activity-row.is-unread")).to_have_count(1)
        day.click()
        expect(day).to_have_attribute("aria-expanded", "false")
        page.locator("[data-activity-unscope]").click()
        expect(page.locator("#activity-week")).to_be_visible()
        expect(day).to_have_count(0)
        expect(page.locator(".activity-row")).to_have_count(1)
        self.assertEqual(self.read, set())

    def test_repeated_progress_folds_and_filters_keep_ai_lines(self):
        self.add_run(status="completed")
        page = self.page()
        rows = ["正在核对参考资料。", "正在梳理案例信息。"] * 3 + ["AI：已连接时间线。"] + ["同一步骤"] * 4
        logs = [{"seq": n + 1, "at": f"2026-10-11T00:00:{n:02}Z", "status": "running", "phase": "edit", "message": text, "result_count": None} for n, text in enumerate(rows)]
        logs.append({"seq": len(logs) + 1, "at": "2026-10-11T00:00:40Z", "status": "completed", "phase": "finished", "message": "剪辑已结束。", "result_count": 1})
        self.context.route("**/api/activity/ai%3Aone/logs**", lambda route: route.fulfill(json={"run": self.runs["ai:one"], "logs": logs, "has_more": False, "has_earlier": False, "first_seq": 1, "next_seq": len(logs)}))
        page.evaluate("TaskActivity.showLogs('ai:one')")
        # The log opens on its key lines; one button switches to the full log.
        expect(page.locator("[data-log-mode]")).to_contain_text("看完整日志")
        page.locator("[data-log-mode]").click()
        page.locator("[data-log-group='1']").click()
        expect(page.locator(".activity-log-fold")).to_have_count(2)
        self.assertEqual(page.locator(".activity-log-fold b").all_text_contents(), ["×5", "×4"])
        page.locator(".activity-log-fold button").first.click()
        expect(page.locator(".activity-log-fold.is-open")).to_have_count(1)
        page.get_by_role("button", name="只看关键节点", exact=True).click()
        expect(page.locator(".activity-log-fold")).to_have_count(0)
        self.assertIn("AI：已连接时间线。", page.locator(".activity-log-body").inner_text())
        self.assertNotIn("同一步骤", page.locator(".activity-log-body").inner_text())
        page.locator("[data-log-mode]").click()
        # A one-line phase has no fold to open, so its label sits on the line instead of a phase header.
        self.assertEqual(page.locator(".activity-log-phase strong, .activity-single-phase").all_text_contents(), ["AI 剪辑", "执行结束"])

    def test_queued_task_has_its_own_status_counts_and_becomes_running(self):
        def task(run_id, kind, status):
            row = self.add_run(run_id, status=status)
            row.update(kind=kind, title="202H-S2 变体 1", phase="queued" if status == "queued" else kind)
            self.changes[-1]["run"] = deepcopy(row)
            return row

        task("timeline:one", "timeline", "queued")
        self.add_run("ai:done", status="completed", notify=True)["owner_id"] = "AIT-other"
        page = self.page()
        page.evaluate("""() => {for (const [id, source] of [['owner-log', ''], ['task-log', 'timeline:one']]) {const host=document.createElement('div');host.id=id;document.querySelector('#content').append(host);TaskActivity.mountLogs(host,{owner_id:'AIT-one',source_id:source});}}""")
        expect(page.locator("[data-activity-active]")).to_be_hidden()
        expect(page.locator("[data-activity-queued]")).to_have_text("1 排队")
        expect(page.locator("#task-activity-open")).to_have_attribute("aria-label", "任务，0 项进行中，1 项排队，1 条未读")
        expect(page.locator("#owner-log .activity-strip-status")).to_have_text("本制作单 1 次运行，1 项排队")
        expect(page.locator("#owner-log .activity-strip-text")).to_have_text("排队：时间线")
        expect(page.locator("#task-log .activity-strip-status")).to_have_text("排队")
        self.assertEqual(page.evaluate("activityDelays.at(-1)"), 3000)

        page.locator("#task-activity-open").click()
        expect(page.locator("[data-run-id='timeline:one'] .activity-status")).to_have_text("排队")
        page.locator("[data-activity-log='timeline:one']").click()
        expect(page.locator(".activity-detail-status")).to_have_text("排队")
        task("edit:one", "edit", "running")
        self.refresh(page)
        expect(page.locator("[data-activity-active]")).to_have_text("1 进行中")
        expect(page.locator("[data-activity-queued]")).to_have_text("1 排队")
        expect(page.locator("#owner-log .activity-strip-status")).to_have_text("本制作单 2 次运行，1 项进行中，1 项排队")
        expect(page.locator("#owner-log .activity-strip-text")).to_have_text("进行中：AI 剪辑；排队：时间线")
        page.locator("[data-activity-group]").click()
        expect(page.locator("[data-run-id='timeline:one'] .activity-status")).to_have_text("排队")

        # All three badges remain distinct in the compact phone header.
        page.keyboard.press("Escape")
        page.set_viewport_size({"width": 390, "height": 844})
        badges = [page.locator(f"[data-activity-{key}]").bounding_box() for key in ("active", "queued", "unread")]
        self.assertTrue(all(badges))
        for index, a in enumerate(badges):
            for b in badges[index + 1:]:
                self.assertTrue(a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"]
                                or a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"])

        task("timeline:one", "timeline", "running")
        self.refresh(page)
        expect(page.locator("[data-activity-queued]")).to_be_hidden()
        expect(page.locator("[data-activity-active]")).to_have_text("2 进行中")
        expect(page.locator("#task-activity-open")).to_have_attribute("aria-label", "任务，2 项进行中，1 条未读")
        expect(page.locator("#owner-log .activity-strip-status")).to_have_text("本制作单 2 次运行，2 项进行中")
        expect(page.locator("#owner-log .activity-strip-text")).to_have_text("进行中：时间线、AI 剪辑")
        expect(page.locator("#task-log .activity-strip-status")).to_have_text("进行中")
        page.locator("#task-activity-open").click()
        # On a phone, opening an order only lists its runs; a run's log opens from its own line.
        page.locator("[data-activity-group]").click()
        expect(page.locator("[data-run-id='timeline:one'] .activity-status")).to_have_text("进行中")
        expect(page.locator("#activity-detail")).to_be_hidden()
        page.locator("[data-run-id='timeline:one'] [data-activity-log]").click()
        expect(page.locator(".activity-detail-status")).to_have_text("进行中")

    def test_opening_folded_identical_runs_reads_every_run_it_stands_for(self):
        # Four identical failed retries of one draft fold into one ×4 line; it shows unread while any of them is.
        for index in range(4):
            self.add_run(f"ai:retry{index}", status="failed", notify=True, message="模型无响应")
        page = self.page()
        self.open_all(page)
        page.locator("[data-activity-group]").click()
        folded = page.locator(".activity-sub", has=page.locator("[data-activity-unfold]"))
        expect(folded).to_have_count(1)
        expect(folded.locator(".activity-sub-repeat")).to_have_text("×4")
        folded.locator(".activity-row-main").click()
        expect(page.locator(".activity-row.is-unread")).to_have_count(0)
        self.assertEqual(self.read, {1, 2, 3, 4})
        expect(page.locator("#task-activity-open")).to_have_attribute("aria-label", "任务，0 项进行中，0 条未读")

    def test_opening_an_order_reads_every_run_in_it_not_just_the_lead(self):
        # Two different finished runs of one order: the order shows unread, and opening it must clear both,
        # not only the lead it selects, or its unread dot stays after the user has looked.
        self.add_run("ai:first", status="completed", notify=True, message="第 1 张已改好")
        self.add_run("ai:second", status="completed", notify=True, message="第 2 张已改好")
        page = self.page()
        self.open_all(page)
        page.locator("[data-activity-group]").click()
        expect(page.locator(".activity-sub")).to_have_count(2)
        expect(page.locator(".activity-row.is-unread")).to_have_count(0)
        self.assertEqual(self.read, {1, 2})
        expect(page.locator("#task-activity-open")).to_have_attribute("aria-label", "任务，0 项进行中，0 条未读")

    def test_legacy_resource_waits_display_as_queued_without_queue_counts(self):
        original_counts = self.counts
        self.counts = lambda: {key: value for key, value in original_counts().items() if key != "queued"}
        examples = [
            ("timeline:wait", "timeline", "等待达芬奇", "queued"),
            ("edit:wait", "edit", "等待达芬奇：整理候选画面", "queued"),
            ("edit:selection", "edit", "等待同项目的其他剪辑选完素材，以免重复用到同一段画面。", "queued"),
            ("edit:project", "edit", "waiting_same_project", "queued"),
            ("ai:ordinary", "ai", "等待达芬奇", "running"),
            ("edit:ordinary", "edit", "分析素材", "running"),
        ]
        for run_id, kind, phase, _ in examples:
            row = self.add_run(run_id, message="达芬奇正在执行另一项任务")
            row.update(kind=kind, phase=phase, owner_id=run_id)
            self.changes[-1]["run"] = deepcopy(row)
        page = self.page()
        page.evaluate("TaskActivity.mountLogs(document.querySelector('#content'),{owner_id:'timeline:wait'})")
        expect(page.locator(".activity-strip-status")).to_have_text("本制作单 1 次运行，1 项排队")
        expect(page.locator("[data-activity-active]")).to_have_text("2 进行中")
        expect(page.locator("[data-activity-queued]")).to_have_text("4 排队")
        page.evaluate("TaskActivity.showLogs('timeline:wait')")
        expect(page.locator(".activity-detail-status")).to_have_text("排队")
        for run_id, _, _, status in examples:
            expect(page.locator(f"[data-run-id='{run_id}'] .activity-status")).to_have_text("排队" if status == "queued" else "进行中")
        # Returning to a concrete execution phase restores the running display.
        row = self.add_run("timeline:wait")
        row.update(kind="timeline", phase="建立时间线", owner_id="timeline:wait")
        self.changes[-1]["run"] = deepcopy(row)
        self.refresh(page)
        expect(page.locator(".activity-detail-status")).to_have_text("进行中")
        expect(page.locator("[data-run-id='timeline:wait'] .activity-status")).to_have_text("进行中")
        expect(page.locator("[data-activity-active]")).to_have_text("3 进行中")
        expect(page.locator("[data-activity-queued]")).to_have_text("3 排队")
        expect(page.locator(".activity-strip-status")).to_have_text("本制作单 1 次运行，1 项进行中")

    def test_inline_strips_share_one_owner_read_and_old_round_has_no_fabricated_history(self):
        self.add_run()
        page = self.page()
        page.evaluate("""() => {for (const [id, source] of [['old-round', 'missing-old-round'], ['live-round', 'ai:one'], ['all-runs', '']]) {const host=document.createElement('div'); host.id=id; document.querySelector('#content').append(host); TaskActivity.mountLogs(host,{owner_id:'AIT-one',source_id:source});}}""")
        expect(page.locator("#live-round .activity-strip")).to_contain_text("正在生成脚本")
        expect(page.locator("#all-runs .activity-strip")).to_contain_text("1 次运行")
        expect(page.locator("#old-round")).to_be_hidden()
        self.assertEqual(sum(1 for row in self.requests if row["params"].get("owner_id")), 1)
        page.locator("#live-round .activity-strip").click()
        expect(page.locator("#task-activity-dialog")).to_be_visible()
        expect(page.locator("#activity-scope")).to_contain_text("202H 写稿第 1 轮")
        expect(page.locator("#activity-detail-title")).to_have_text("202H 写稿第 1 轮")
        self.assertEqual(next(row["params"] for row in reversed(self.requests) if row["params"].get("filter"))["source_id"], "ai:one")

    def test_desktop_permission_is_requested_only_by_settings_click(self):
        page = self.page()
        page.evaluate("() => {document.querySelector('#content').innerHTML=TaskActivity.settingsMarkup();TaskActivity.mountSettings(document.querySelector('#content'));}")
        self.assertEqual(page.evaluate("permissionCalls"), 0)
        page.locator("#settings-activity-enable").click()
        self.assertEqual(page.evaluate("permissionCalls"), 1)
        expect(page.locator("#settings-activity-status")).to_contain_text("桌面提醒已开启")
        page.locator("#settings-activity-disable").click()
        expect(page.locator("#settings-activity-status")).to_contain_text("未开启")
        page.evaluate("Notification.permission='default';window.permissionResult='denied';TaskActivity.mountSettings(document.querySelector('#content'))")
        page.locator("#settings-activity-enable").click()
        expect(page.locator("#settings-activity-enable")).to_be_disabled()
        expect(page.locator("#settings-activity-status")).to_contain_text("浏览器已阻止")

    def test_restore_epoch_rebaselines_without_replaying_historical_alerts(self):
        self.add_run()
        page = self.page()
        self.epoch = "restored-epoch"
        self.add_run(status="completed", notify=True)
        self.refresh(page)
        expect(page.locator(".activity-notice")).to_have_count(0)
        self.assertEqual(len(self.claims), 0)

    def test_duplicate_tabs_share_browser_claim_and_reload_baselines(self):
        self.add_run()
        first = self.page()
        second = self.page()
        self.add_run(status="completed", notify=True)
        self.refresh(first)
        self.refresh(second)
        self.assertEqual(first.locator(".activity-notice").count() + second.locator(".activity-notice").count(), 1)
        self.assertEqual(len(self.claims), 1)
        third = self.page()
        self.assertEqual(third.locator(".activity-notice").count(), 0)

    def test_cancelled_never_sends_desktop_and_unavailable_feed_keeps_records(self):
        self.add_run()
        page = self.page()
        page.evaluate("() => { localStorage.setItem('workbench-activity-desktop-v1','enabled'); Notification.permission='granted'; window.activityFocused=false; window.dispatchEvent(new Event('blur')); }")
        self.add_run(status="cancelled", notify=True)
        self.refresh(page)
        expect(page.locator(".activity-notice")).to_have_count(1)
        self.assertEqual(page.evaluate("notificationCalls.length"), 0)
        self.open_all(page)
        expect(page.locator(".activity-row")).to_have_count(1)
        self.available = False
        page.evaluate("TaskActivity.refresh()")
        page.clock.run_for(2)
        expect(page.locator("#activity-connection")).to_contain_text("暂时不可用")
        expect(page.locator(".activity-row")).to_have_count(1)

    def test_mobile_keyboard_dialog_and_public_mode(self):
        self.add_run()
        page = self.page()
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#task-activity-open").focus()
        page.keyboard.press("Enter")
        expect(page.locator("#task-activity-dialog")).to_be_visible()
        expect(page.locator(".activity-row")).to_have_count(1)
        self.assertTrue(page.evaluate("document.querySelector('#task-activity-dialog').scrollWidth <= document.querySelector('#task-activity-dialog').clientWidth"))
        page.keyboard.press("Escape")
        expect(page.locator("#task-activity-open")).to_be_focused()
        # A separate normal page may poll when focus changes; measure only the public page below.
        page.wait_for_function("activityApiActive === 0")
        page.close()
        self.requests.clear()
        public = self.page(public=True)
        public.evaluate("TaskActivity.open();TaskActivity.showLogs('ai:one');TaskActivity.mountLogs(document.querySelector('#content'),{owner_id:'AIT-one'});")
        public.clock.run_for(60000)
        self.assertEqual(self.requests, [])
        expect(public.locator("#task-activity-open")).to_be_hidden()

    def test_poll_backoff_and_repeated_focus_never_overlap_requests(self):
        self.add_run()
        page = self.page()
        self.feed_failures = 2
        page.evaluate("activityDelays.length=0;TaskActivity.refresh()")
        page.clock.run_for(2)
        page.wait_for_function("activityDelays.at(-1) === 3000")
        page.clock.run_for(3001)
        page.wait_for_function("activityDelays.at(-1) === 6000")
        page.clock.run_for(6001)
        page.wait_for_function("activityDelays.at(-1) === 3000")
        self.assertEqual(page.locator(".activity-notice").count(), 0)
        page.evaluate("blockActivity=true;TaskActivity.refresh()")
        page.clock.run_for(2)
        page.wait_for_function("blockedCalls.length === 1")
        page.evaluate("for(let n=0;n<8;n++) window.dispatchEvent(new Event('focus'))")
        page.clock.run_for(15000)
        self.assertEqual(page.evaluate("blockedCalls.length"), 1)
        self.assertEqual(page.evaluate("activityApiMax"), 1)
        page.evaluate("blockActivity=false;blockedCalls[0]()")
        page.wait_for_function("activityApiActive === 0")

    def test_slow_background_poll_does_not_block_opening_the_list_or_log(self):
        self.add_run()
        page = self.page()
        page.evaluate("blockActivity=true;TaskActivity.refresh()")
        page.clock.run_for(2)
        page.wait_for_function("blockedCalls.length === 1")
        try:
            page.locator("#task-activity-open").click()
            expect(page.locator(".activity-row")).to_have_count(1)
            # The opened run's log finishes while the background poll is still waiting.
            page.locator("[data-activity-log='ai:one']").click()
            expect(page.locator("[data-log-mode]")).to_contain_text("2+ 行")
            page.wait_for_function("activityApiActive === 1")
            self.assertEqual(page.evaluate("blockedCalls.length"), 1)
        finally:
            page.evaluate("blockActivity=false;blockedCalls[0]()")
            page.wait_for_function("activityApiActive === 0")

    def test_calendar_starts_current_runs_and_waiting_work_before_the_week_returns(self):
        self.add_run()
        page = self.page()
        held, started = [], []
        page.on("request", lambda request: started.append(urlparse(request.url).path))

        def hold_week(route):
            params = parse_qs(urlparse(route.request.url).query)
            if params.get("filter") == ["range"]:
                held.append(route)
            else:
                self.respond(route)

        self.context.route("**/api/activity?**", hold_week)
        page.locator("#task-activity-open").click()
        try:
            page.wait_for_function("activityApiActive === 1")
            self.assertEqual(len(held), 1)
            self.assertTrue(any(row["params"].get("filter") == "current" for row in self.requests))
            self.assertEqual(started[:2], ["/api/activity", "/api/activity"])
            self.assertEqual(started.count("/api/content"), 2)
            self.assertIn("/api/photo-overview", started)
            self.assertIn("/api/needs-you", started)
        finally:
            for route in held:
                self.respond(route)
        expect(page.locator(".activity-row")).to_have_count(1)

    def test_previous_products_week_cannot_replace_the_new_list_or_choose_its_day(self):
        self.add_run("ai:old", "202H", "completed")["owner_id"] = "AIT-old"
        self.set_times("ai:old", "2026-10-02T03:00:00Z", "2026-10-02T03:05:00Z")
        self.add_run("ai:new", "129N", "completed")["owner_id"] = "AIT-new"
        page = self.page()
        held = []

        def hold_previous_week(route):
            params = parse_qs(urlparse(route.request.url).query)
            if params.get("filter") == ["range"] and params.get("product") == ["202H"]:
                held.append(route)
            else:
                self.respond(route)

        self.context.route("**/api/activity?**", hold_previous_week)
        page.locator("#task-activity-open").click()
        try:
            page.wait_for_function("activityApiActive === 1")
            self.pick_product(page, "129N")
            expect(page.locator('[data-run-id="ai:new"]')).to_have_count(1)
            expect(page.locator('[data-activity-date][aria-pressed="true"]')).to_contain_text("今天")
        finally:
            for route in held:
                self.respond(route)
        page.wait_for_function("activityApiActive === 0")
        expect(page.locator('[data-run-id="ai:old"]')).to_have_count(0)
        expect(page.locator('[data-run-id="ai:new"]')).to_have_count(1)
        expect(page.locator('[data-activity-date][aria-pressed="true"]')).to_contain_text("今天")
        self.assertFalse(any(row["params"].get("filter") == "all" and row["params"].get("product") == "202H" for row in self.requests))

    def test_late_log_response_keeps_the_newer_completed_status(self):
        previous = deepcopy(self.add_run())
        page = self.page()
        held = []
        self.context.route("**/api/activity/ai%3Aone/logs**", lambda route: held.append(route))
        page.locator("#task-activity-open").click()
        expect(page.locator(".activity-row")).to_have_count(1)
        page.locator("[data-activity-log='ai:one']").click()
        page.wait_for_function("activityApiActive === 1")
        self.assertEqual(len(held), 1)
        self.add_run(status="completed")
        self.refresh(page)
        expect(page.locator(".activity-row .activity-status")).to_have_text("已完成")
        held[0].fulfill(json={"run": previous, "logs": [], "has_more": False, "has_earlier": False,
                              "first_seq": None, "next_seq": 0, "available": True})
        page.wait_for_function("activityApiActive === 0")
        expect(page.locator(".activity-detail-status")).to_have_text("已完成")
        expect(page.locator(".activity-row .activity-status")).to_have_text("已完成")

    def test_old_waiting_work_cannot_replace_a_newer_load_after_switching_back(self):
        self.add_run()
        page = self.page(activity_prefs=DEFAULT_PREFS)
        held = []
        fresh = {"id": "script:new", "title": "最新待处理稿", "product": "202H", "next_label": "开始制作"}

        def waiting_work(route):
            params = parse_qs(urlparse(route.request.url).query)
            if params.get("view") == ["attention"] and params.get("product") == ["202H"]:
                if not held:
                    held.append(route)
                else:
                    route.fulfill(json={"items": [fresh]})
            else:
                route.fulfill(json={"items": []})

        self.context.route("**/api/content?**", waiting_work)
        page.locator("#task-activity-open").click()
        expect(page.locator("#task-activity-dialog")).to_be_visible()
        page.wait_for_function("activityApiActive === 1")
        self.pick_product(page, "129N")
        page.wait_for_function("activityApiActive === 1")
        self.pick_product(page, "202H")
        expect(page.locator(".activity-work-row")).to_contain_text("最新待处理稿")
        held[0].fulfill(json={"items": [{**fresh, "id": "script:old", "title": "过期待处理稿"}]})
        page.wait_for_function("activityApiActive === 0")
        expect(page.locator(".activity-work-row")).to_have_count(1)
        expect(page.locator(".activity-work-row")).to_contain_text("最新待处理稿")

    def test_rejected_navigation_does_not_mark_notification_read(self):
        self.add_run(status="completed", notify=True)
        page = self.page()
        page.evaluate("() => {history.replaceState(null,'','#draft');window.rejectNavigation=true;window.addEventListener('hashchange',()=>{if(rejectNavigation)history.replaceState(null,'','#draft');else TaskActivity.didNavigate(location.hash);});}")
        self.open_all(page)
        page.locator("[data-activity-result]").click()
        page.wait_for_function("location.hash === '#draft'")
        self.assertEqual(self.read, set())
        page.evaluate("rejectNavigation=false")
        self.open_all(page)
        with page.expect_response("**/api/activity/read"):
            page.locator("[data-activity-result]").click()
        page.wait_for_url("**/#/202H/ai/AIT-one?round=ai:one")
        page.wait_for_function("activityApiActive === 0")
        self.assertEqual(self.read, {1})

    def test_unsupported_desktop_notifications_keep_settings_usable(self):
        page = self.page()
        page.evaluate("() => {delete window.Notification;document.querySelector('#content').innerHTML=TaskActivity.settingsMarkup();TaskActivity.mountSettings(document.querySelector('#content'));}")
        expect(page.locator("#settings-activity-enable")).to_be_disabled()
        expect(page.locator("#settings-activity-status")).to_contain_text("此浏览器不支持")

    def test_photo_work_opens_its_drawer_without_leaving_the_page(self):
        self.add_kinds()
        self.photos = {"counts": {"doing": 1}, "items": [
            {"job_id": "PRD-pic", "job_title": "图文单", "timeline_uid": "u9", "timeline_name": "Lab-9", "product": "202H", "state": "doing", "since": "2026-10-09T03:00:00Z"}]}
        page = self.page(activity_prefs=DEFAULT_PREFS)
        page.evaluate("() => {window.opened = [];window.PhotoCarousel = {open: args => window.opened.push([args.job.id, args.uid, args.timelineName])};}")
        before = page.evaluate("location.hash")
        page.locator("#task-activity-open").click()
        link = page.locator(".activity-work-row").filter(has_text="Lab-9 · 图文单").locator("[data-activity-work]")
        self.assertIn("photo=u9", link.get_attribute("href"))  # Ctrl / middle click still opens the order page in a new tab
        link.click()
        self.assertEqual(page.evaluate("window.opened"), [["PRD-pic", "u9", "Lab-9"]])
        self.assertEqual(page.evaluate("location.hash"), before)

    def test_photo_task_result_opens_its_drawer_without_leaving_the_page(self):
        run = self.add_run("photo:plan:bbbbbbbbbbbb", status="completed", notify=True)
        run.update(kind="photo", owner_id="PRD-pic", source_id="PHS-aaaaaaaaaaaa", title="图文逐张表 · Lab-8",
                   url="#/202H/production/PRD-pic?step=cut&run=PHS-aaaaaaaaaaaa")
        self.events[-1]["url"] = run["url"]
        page = self.page()
        page.evaluate("""() => {window.opened = [];window.PhotoCarousel = {open: () => {}, openFor: async args => {window.opened.push([args.jobId, args.source]);return true;}};}""")
        before = page.evaluate("location.hash")
        self.open_all(page)
        with page.expect_response("**/api/activity/read"):
            page.locator("[data-activity-result]").click()
        self.assertEqual(page.evaluate("window.opened"), [["PRD-pic", "PHS-aaaaaaaaaaaa"]])
        self.assertEqual(page.evaluate("location.hash"), before)
        self.assertEqual(self.read, {run["event_id"]})

    def test_photo_task_whose_timeline_is_gone_falls_back_to_the_order(self):
        run = self.add_run("photo:textless:aaaaaaaaaaaa", status="completed")
        run.update(kind="photo", owner_id="PRD-pic", source_id="gone", url="#/202H/production/PRD-pic?step=cut&run=gone")
        page = self.page()
        page.evaluate("() => {window.PhotoCarousel = {open: () => {}, openFor: async () => false};}")
        self.open_all(page)
        page.locator("[data-activity-result]").click()
        page.wait_for_function("location.hash.includes('production/PRD-pic')")


if __name__ == "__main__":
    unittest.main()
