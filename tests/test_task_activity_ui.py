"""Isolated activity UI checks: no task execution or real notification permission."""
from copy import deepcopy
from pathlib import Path
import json
import unittest
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ACTIVE = {"queued", "running", "cancelling", "recovering"}


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
        self.feed_failures = 0
        self.context.route("http://activity.test/", lambda route: route.fulfill(body="<!doctype html><html><body></body></html>", content_type="text/html"))
        self.context.route("**/api/activity**", self.respond)

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
                if params.get("before"):
                    rows = [row for row in rows if row["version"] < int(params["before"])]
                rows.sort(key=lambda row: -row["version"])
                result = {"items": rows[:limit], "events": [], "cursor": self.version, "has_more": len(rows) > limit,
                          "next_before": rows[min(limit, len(rows)) - 1]["version"] if rows else None}
            result.update(counts=self.counts(), epoch=self.epoch, available=self.available)
        route.fulfill(json=result)

    def page(self, *, initialize=True, public=False):
        page = self.context.new_page()
        page.set_default_timeout(5000)
        page.clock.install()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.goto("http://activity.test/")
        page.set_content('''<header class="topbar"><div class="topbar-right"><button class="secondary activity-trigger" id="task-activity-open">任务<span data-activity-active hidden></span><span data-activity-unread hidden></span></button></div></header><select id="global-product"><option value="">全部产品</option><option value="202H" selected>202H</option><option value="129N">129N</option></select><main id="content"></main>''')
        page.add_style_tag(path=str(ROOT / "static/style.css"))
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
        page.add_script_tag(path=str(ROOT / "static/activity.js"))
        if initialize:
            page.evaluate("TaskActivity.initialize()")
            page.clock.run_for(2)
            if not public:
                page.wait_for_function("localStorage.getItem('workbench-activity-cursor-v1') !== null")
        return page

    def refresh(self, page):
        before = len(self.requests)
        page.evaluate("TaskActivity.refresh()")
        page.clock.run_for(2)
        page.wait_for_function("expected => JSON.parse(localStorage.getItem('workbench-activity-cursor-v1') || '{}').cursor === expected", arg=self.version)
        self.assertGreater(len(self.requests), before)

    def test_baseline_and_open_panel_never_clear_unread_and_default_to_current_product(self):
        self.add_run(status="completed", notify=True)
        self.add_run("ai:two", "129N", "completed", notify=True)
        page = self.page()
        self.assertEqual(page.locator(".activity-notice").count(), 0)
        page.locator("#task-activity-open").click()
        expect(page.locator(".activity-row")).to_have_count(1)
        self.assertEqual(page.locator("#activity-product").input_value(), "202H")
        self.assertEqual(self.read, set())
        page.locator("#activity-product").select_option("")
        expect(page.locator(".activity-row")).to_have_count(2)
        page.get_by_role("button", name="全部已读", exact=True).click()
        expect(page.locator(".activity-row.is-unread")).to_have_count(0)
        self.assertEqual(self.read, {1, 2})

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
        page.locator("#activity-product").select_option("129N")
        expect(page.locator(".activity-row h3")).to_have_text("129N 写稿第 1 轮")

    def test_log_paging_is_ordered_escaped_and_marks_only_opened_run(self):
        self.add_run(status="completed", notify=True)
        page = self.page()
        page.evaluate("TaskActivity.showLogs('ai:one')")
        expect(page.locator(".activity-log-lines li")).to_have_count(2)
        self.assertEqual(page.locator(".activity-log-lines img").count(), 0)
        self.assertIn("12 秒", page.locator(".activity-log-summary").inner_text())
        page.locator("[data-log-more]").click()
        expect(page.locator(".activity-log-lines li")).to_have_count(3)
        self.assertEqual([line.split(" <")[0] for line in page.locator(".activity-log-lines p").all_text_contents()], ["步骤 1", "步骤 2", "步骤 3"])
        self.assertEqual(self.read, {1})

    def test_inline_logs_are_lazy_and_old_round_has_no_fabricated_history(self):
        self.add_run()
        page = self.page()
        page.evaluate("() => {const host=document.createElement('div'); host.id='round-logs';document.querySelector('#content').append(host);TaskActivity.mountLogs(host,{owner_id:'AIT-one',source_id:'missing-old-round'});}")
        self.assertFalse(any(row["params"].get("owner_id") for row in self.requests))
        page.locator("#round-logs summary").click()
        expect(page.locator("#round-logs")).to_contain_text("此前未记录运行日志")
        query = next(row["params"] for row in self.requests if row["params"].get("owner_id"))
        self.assertEqual(query["source_id"], "missing-old-round")

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
        page.locator("#task-activity-open").click()
        expect(page.locator(".activity-row")).to_have_count(1)
        self.available = False
        page.evaluate("TaskActivity.refresh()")
        page.clock.run_for(2)
        expect(page.locator("#activity-connection")).to_contain_text("暂时不可用")
        expect(page.locator(".activity-row")).to_have_count(1)

    def test_mobile_keyboard_dialog_and_public_mode(self):
        self.add_run(status="completed", notify=True)
        page = self.page()
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#task-activity-open").focus()
        page.keyboard.press("Enter")
        expect(page.locator("#task-activity-dialog")).to_be_visible()
        expect(page.locator(".activity-row")).to_have_count(1)
        self.assertTrue(page.evaluate("document.querySelector('#task-activity-dialog').scrollWidth <= document.querySelector('#task-activity-dialog').clientWidth"))
        page.keyboard.press("Escape")
        expect(page.locator("#task-activity-open")).to_be_focused()
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

    def test_rejected_navigation_does_not_mark_notification_read(self):
        self.add_run(status="completed", notify=True)
        page = self.page()
        page.evaluate("() => {history.replaceState(null,'','#draft');window.rejectNavigation=true;window.addEventListener('hashchange',()=>{if(rejectNavigation)history.replaceState(null,'','#draft');else TaskActivity.didNavigate(location.hash);});}")
        page.locator("#task-activity-open").click()
        page.locator("[data-activity-result]").click()
        page.wait_for_function("location.hash === '#draft'")
        self.assertEqual(self.read, set())
        page.evaluate("rejectNavigation=false")
        page.locator("#task-activity-open").click()
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


if __name__ == "__main__":
    unittest.main()
