"""Isolated activity UI checks: no task execution or real notification permission."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
        self.feed_failures = self.claim_failures = 0
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
        page.clock.install()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.goto("http://activity.test/")
        page.set_content('''<header class="topbar"><div class="topbar-right"><button class="secondary activity-trigger" id="task-activity-open">任务<span class="activity-count" data-activity-active hidden></span><span class="activity-count" data-activity-unread hidden></span></button></div></header><select id="global-product"><option value="">全部产品</option><option value="202H" selected>202H</option><option value="129N">129N</option></select><main id="content"></main>''')
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
        if activity_prefs is not None:
            page.evaluate("prefs => localStorage.setItem('workbench-activity-prefs-v1', JSON.stringify(prefs))", activity_prefs)
        page.add_script_tag(path=str(ROOT / "static/activity.js"))
        if initialize:
            page.evaluate("TaskActivity.initialize()")
            page.clock.run_for(2)
            if not public:
                page.wait_for_function("localStorage.getItem('workbench-activity-cursor-v1') !== null")
        return page

    def open_all(self, page):
        page.locator("#task-activity-open").click()
        page.get_by_role("button", name="全部", exact=True).click()

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
        # The default view holds only what is running or waiting; finished history sits under 全部.
        expect(page.locator("#activity-list .activity-empty")).to_have_text("当前没有进行中或需处理的任务")
        page.get_by_role("button", name="全部", exact=True).click()
        expect(page.locator(".activity-row")).to_have_count(1)
        self.assertEqual(page.locator("#activity-product").input_value(), "202H")
        self.assertEqual(self.read, set())
        page.locator("#activity-product").select_option("")
        expect(page.locator(".activity-row")).to_have_count(2)
        page.get_by_role("button", name="全部已读", exact=True).click()
        expect(page.locator(".activity-row.is-unread")).to_have_count(0)
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
        expect(page.locator(".activity-row-title")).to_have_text("129N 写稿第 1 轮")

    def test_log_opens_at_newest_lines_pages_back_escaped_and_marks_only_opened_run(self):
        self.add_run(status="completed", notify=True)
        page = self.page()
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

    def test_opening_panel_previews_newest_run_without_reading_it(self):
        self.add_run(status="completed", notify=True)
        page = self.page()
        self.open_all(page)
        expect(page.locator(".activity-row.is-selected")).to_have_count(1)
        expect(page.locator("#activity-detail-title")).to_have_text("202H 写稿第 1 轮")
        expect(page.locator(".activity-log-lines li")).to_have_count(2)
        self.assertEqual(self.read, set())
        page.locator("[data-activity-log='ai:one']").click()
        page.wait_for_function("activityApiActive === 0")
        self.assertEqual(self.read, {1})

    def test_runs_group_by_order_and_current_view_folds_what_finished_today(self):
        now = datetime.now(timezone.utc)
        stamp = lambda minutes: (now - timedelta(minutes=minutes)).isoformat(timespec="milliseconds")
        def run(run_id, owner, title, status, minutes):
            row = self.add_run(run_id, status=status, message=f"{run_id} 的进度")
            row.update(owner_id=owner, kind="images", title=title, started_at=stamp(minutes + 1), updated_at=stamp(minutes),
                       finished_at=None if status in ACTIVE else stamp(minutes), url=f"#/202H/production/{owner}?step=images")
        run("img:a1", "CAR-a", "出图 · 202H-S2 变体 #4 · 身份认同", "completed", 30)
        run("img:a2", "CAR-a", "出图 · 202H-S2 变体 #4 · 身份认同", "needs_attention", 20)
        run("pkg:a", "CAR-a", "成品打包 · 202H-S2 变体 #4 · 身份认同", "completed", 10)
        run("img:b1", "CAR-b", "出图 · 第 1 条「从粗到细」", "completed", 25)
        run("img:b2", "CAR-b", "出图 · 第 1 条「从粗到细」", "completed", 15)
        run("img:c", "CAR-c", "出图 · 202H-S1 变体 #2", "completed", 3 * 24 * 60)
        page = self.page()
        page.locator("#task-activity-open").click()
        # One row per order still waiting on someone; it leads with the run that needs attention.
        expect(page.locator(".activity-group-row")).to_have_count(1)
        expect(page.locator(".activity-group-row .activity-row-title")).to_have_text("202H-S2 变体 #4 · 身份认同")
        expect(page.locator(".activity-group-count")).to_have_text("3 次")
        expect(page.locator(".activity-group-row .activity-row-meta")).to_contain_text("待处理")
        expect(page.locator(".activity-group-row .activity-row-meta")).to_contain_text("出图：img:a2 的进度")
        expect(page.locator("#activity-detail-title")).to_have_text("出图 · 202H-S2 变体 #4 · 身份认同")
        current = next(row["params"] for row in reversed(self.requests) if row["params"].get("filter") == "current")
        self.assertEqual(current["group"], "owner")
        self.assertIn("since", current)
        # Orders that finished today fold into one line.
        expect(page.locator(".activity-done-fold")).to_have_text("今天已完成 1 项")
        expect(page.get_by_text("第 1 条「从粗到细」")).to_have_count(0)
        page.locator(".activity-done-fold").click()
        expect(page.locator(".activity-group-row .activity-row-title")).to_have_text(["202H-S2 变体 #4 · 身份认同", "第 1 条「从粗到细」"])
        page.locator("[data-activity-group='CAR-a']").click()
        expect(page.locator(".activity-group-runs .activity-sub-text b")).to_have_text(["成品打包", "出图", "出图"])
        expect(page.locator(".activity-group-runs .activity-sub.is-selected")).to_have_attribute("data-run-id", "img:a2")
        page.locator("[data-activity-group='CAR-a']").click()
        expect(page.locator(".activity-group-runs")).to_have_count(0)
        page.get_by_role("button", name="全部", exact=True).click()
        expect(page.locator(".activity-day")).to_have_count(2)
        expect(page.locator(".activity-row-title")).to_have_text(["202H-S2 变体 #4 · 身份认同", "第 1 条「从粗到细」", "202H-S1 变体 #2"])
        # A run opened from elsewhere opens its order so the selection is visible.
        page.evaluate("TaskActivity.showLogs('img:b1')")
        expect(page.locator(".activity-sub.is-selected")).to_have_attribute("data-run-id", "img:b1")
        expect(page.locator(".activity-group-runs")).to_have_count(1)
        # A day folds under its header, and that fold plus the chosen filter survive a reload.
        page.locator("[data-activity-day]").nth(1).click()
        expect(page.locator("[data-activity-day]").nth(1)).to_have_attribute("aria-expanded", "false")
        expect(page.get_by_text("202H-S1 变体 #2")).to_have_count(0)
        page.close()
        page = self.page()
        page.locator("#task-activity-open").click()
        expect(page.get_by_role("button", name="全部", exact=True)).to_have_attribute("aria-pressed", "true")
        expect(page.locator("[data-activity-day]")).to_have_count(2)
        expect(page.locator("[data-activity-day]").nth(1)).to_have_attribute("aria-expanded", "false")
        expect(page.get_by_text("202H-S1 变体 #2")).to_have_count(0)
        page.locator("[data-activity-day]").nth(1).click()
        expect(page.get_by_text("202H-S1 变体 #2")).to_have_count(1)

    def test_each_status_tab_keeps_its_own_product_type_and_search(self):
        self.add_run("run:a", status="completed", message="完成")
        page = self.page()
        page.locator("#task-activity-open").click()
        page.get_by_role("button", name="全部", exact=True).click()
        page.locator("#activity-kind").select_option("images")
        page.locator("#activity-search").fill("变体")
        page.get_by_role("button", name="当前", exact=True).click()
        expect(page.locator("#activity-kind")).to_have_value("")
        expect(page.locator("#activity-search")).to_have_value("")
        page.locator("#activity-kind").select_option("voice")
        page.get_by_role("button", name="全部", exact=True).click()
        expect(page.locator("#activity-kind")).to_have_value("images")
        expect(page.locator("#activity-search")).to_have_value("变体")
        # The split survives a fresh visit.
        page.close()
        page = self.page()
        page.locator("#task-activity-open").click()
        expect(page.get_by_role("button", name="全部", exact=True)).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#activity-kind")).to_have_value("images")
        page.get_by_role("button", name="当前", exact=True).click()
        expect(page.locator("#activity-kind")).to_have_value("voice")

    def test_date_folds_are_independent_per_status_tab_and_survive_reload(self):
        self.add_run(status="needs_attention", notify=True)
        page = self.page()
        page.locator("#task-activity-open").click()
        day = page.locator("[data-activity-day]")
        expect(day).to_have_attribute("aria-expanded", "true")
        day.click()
        expect(day).to_have_attribute("aria-expanded", "false")
        page.get_by_role("button", name="全部", exact=True).click()
        expect(day).to_have_attribute("aria-expanded", "true")
        expect(page.locator(".activity-row")).to_have_count(1)
        day.click()
        expect(day).to_have_attribute("aria-expanded", "false")
        # Both other tabs can be folded while unread still starts with its tasks visible.
        page.get_by_role("button", name="未读", exact=True).click()
        expect(day).to_have_attribute("aria-expanded", "true")
        expect(page.locator(".activity-row.is-unread")).to_have_count(1)
        self.assertEqual(self.read, set())
        day.click()
        expect(day).to_have_attribute("aria-expanded", "false")
        page.get_by_role("button", name="当前", exact=True).click()
        expect(day).to_have_attribute("aria-expanded", "false")
        day.click()
        expect(day).to_have_attribute("aria-expanded", "true")
        page.get_by_role("button", name="全部", exact=True).click()
        expect(day).to_have_attribute("aria-expanded", "false")
        page.get_by_role("button", name="未读", exact=True).click()
        expect(day).to_have_attribute("aria-expanded", "false")
        page.close()
        page = self.page()
        page.locator("#task-activity-open").click()
        day = page.locator("[data-activity-day]")
        expect(page.get_by_role("button", name="未读", exact=True)).to_have_attribute("aria-pressed", "true")
        expect(day).to_have_attribute("aria-expanded", "false")
        page.get_by_role("button", name="当前", exact=True).click()
        expect(day).to_have_attribute("aria-expanded", "true")
        page.get_by_role("button", name="全部", exact=True).click()
        expect(day).to_have_attribute("aria-expanded", "false")
        self.assertEqual(self.read, set())

    def test_legacy_date_folds_migrate_only_to_the_saved_non_unread_tab(self):
        self.add_run(status="needs_attention", notify=True)
        page = self.page(activity_prefs={"filter": "all", "collapsed": ["2026-9-11"]})
        page.locator("#task-activity-open").click()
        day = page.locator("[data-activity-day]")
        expect(page.get_by_role("button", name="全部", exact=True)).to_have_attribute("aria-pressed", "true")
        expect(day).to_have_attribute("aria-expanded", "false")
        page.get_by_role("button", name="未读", exact=True).click()
        expect(day).to_have_attribute("aria-expanded", "true")
        expect(page.locator(".activity-row.is-unread")).to_have_count(1)
        page.get_by_role("button", name="当前", exact=True).click()
        expect(day).to_have_attribute("aria-expanded", "true")
        page.get_by_role("button", name="全部", exact=True).click()
        expect(day).to_have_attribute("aria-expanded", "false")
        self.assertEqual(self.read, set())

    def test_legacy_unread_date_fold_is_reset_to_visible_tasks(self):
        self.add_run(status="needs_attention", notify=True)
        page = self.page(activity_prefs={"filter": "unread", "collapsed": ["2026-9-11"]})
        page.locator("#task-activity-open").click()
        expect(page.get_by_role("button", name="未读", exact=True)).to_have_attribute("aria-pressed", "true")
        expect(page.locator("[data-activity-day]")).to_have_attribute("aria-expanded", "true")
        expect(page.locator(".activity-row.is-unread")).to_have_count(1)
        page.close()
        page = self.page()
        page.locator("#task-activity-open").click()
        expect(page.locator("[data-activity-day]")).to_have_attribute("aria-expanded", "true")
        expect(page.locator(".activity-row.is-unread")).to_have_count(1)
        self.assertEqual(self.read, set())

    def test_scoped_log_date_fold_does_not_change_saved_status_tab(self):
        self.add_run(status="needs_attention", notify=True)
        page = self.page()
        self.open_all(page)
        day = page.locator("[data-activity-day]")
        day.click()
        expect(day).to_have_attribute("aria-expanded", "false")
        page.evaluate("TaskActivity.open({owner_id:'AIT-one',label:'当前制作单'})")
        expect(day).to_have_attribute("aria-expanded", "true")
        expect(page.locator(".activity-row.is-unread")).to_have_count(1)
        day.click()
        expect(day).to_have_attribute("aria-expanded", "false")
        page.evaluate("TaskActivity.open({owner_id:'AIT-one',label:'当前制作单'})")
        expect(day).to_have_attribute("aria-expanded", "true")
        page.locator("[data-activity-unscope]").click()
        expect(page.get_by_role("button", name="全部", exact=True)).to_have_attribute("aria-pressed", "true")
        expect(day).to_have_attribute("aria-expanded", "false")
        self.assertEqual(self.read, set())

    def test_retried_failure_settles_and_identical_runs_share_one_line(self):
        now = datetime.now(timezone.utc)
        stamp = lambda minutes: (now - timedelta(minutes=minutes)).isoformat(timespec="milliseconds")
        def run(run_id, owner, status, message, minutes):
            row = self.add_run(run_id, status=status, message=message)
            row.update(owner_id=owner, kind="images", title="出图 · 变体 #9", started_at=stamp(minutes + 1), updated_at=stamp(minutes),
                       finished_at=None if status in ACTIVE else stamp(minutes))
        run("img:old", "CAR-e", "failed", "出图未全部完成。", 3 * 24 * 60)
        for minutes, run_id in enumerate(("img:1", "img:2", "img:3")):
            run(run_id, "CAR-e", "completed", "已人工确认。", minutes + 1)
        page = self.page()
        page.locator("#task-activity-open").click()
        # The later image runs carry the outcome, so the old failure no longer keeps the order in front.
        expect(page.locator("#activity-list .activity-empty")).to_have_text("当前没有进行中或需处理的任务")
        expect(page.locator(".activity-done-fold")).to_have_text("今天已完成 1 项")
        page.get_by_role("button", name="全部", exact=True).click()
        page.locator("[data-activity-group='CAR-e']").click()
        expect(page.locator(".activity-group-runs .activity-sub")).to_have_count(2)
        expect(page.locator(".activity-sub-repeat")).to_have_text("×3")
        page.locator(".activity-sub-repeat").click()
        expect(page.locator(".activity-group-runs .activity-sub")).to_have_count(4)

    def test_repeated_progress_folds_and_filters_keep_ai_lines(self):
        self.add_run(status="completed")
        page = self.page()
        rows = ["正在核对参考资料。", "正在梳理案例信息。"] * 3 + ["AI：已连接时间线。"] + ["同一步骤"] * 4
        logs = [{"seq": n + 1, "at": f"2026-10-11T00:00:{n:02}Z", "status": "running", "phase": "edit", "message": text, "result_count": None} for n, text in enumerate(rows)]
        logs.append({"seq": len(logs) + 1, "at": "2026-10-11T00:00:40Z", "status": "completed", "phase": "finished", "message": "剪辑已结束。", "result_count": 1})
        self.context.route("**/api/activity/ai%3Aone/logs**", lambda route: route.fulfill(json={"run": self.runs["ai:one"], "logs": logs, "has_more": False, "has_earlier": False, "first_seq": 1, "next_seq": len(logs)}))
        page.evaluate("TaskActivity.showLogs('ai:one')")
        page.locator("[data-log-group='1']").click()
        expect(page.locator(".activity-log-fold")).to_have_count(2)
        self.assertEqual(page.locator(".activity-log-fold b").all_text_contents(), ["×5", "×4"])
        page.locator(".activity-log-fold button").first.click()
        expect(page.locator(".activity-log-fold.is-open")).to_have_count(1)
        page.get_by_role("button", name="AI 说明", exact=True).click()
        expect(page.locator(".activity-log-fold")).to_have_count(0)
        self.assertIn("AI：已连接时间线。", page.locator(".activity-log-body").inner_text())
        self.assertNotIn("同一步骤", page.locator(".activity-log-body").inner_text())
        page.get_by_role("button", name="完整", exact=True).click()
        self.assertEqual(page.locator(".activity-log-phase strong").all_text_contents(), ["AI 剪辑", "执行结束"])

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
        page.locator("[data-activity-group]").click()
        expect(page.locator("[data-run-id='timeline:one'] .activity-status")).to_have_text("进行中")
        expect(page.locator(".activity-detail-status")).to_have_text("进行中")

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


if __name__ == "__main__":
    unittest.main()
