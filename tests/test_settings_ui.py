"""Settings interactions with isolated browser fixtures; no local library is opened."""
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
AUTH_URL = "https://claude.com/cai/oauth/authorize?client_id=example&state=fixture-state"
PENDING = {"available": False, "desktop_bundled": True, "login_pending": True}


def pending_login(**changes):
    return {"attempt_id": "fixture-attempt", "pending": True, "url": AUTH_URL,
            "expires_at": None, "callback_submitted": False, "callback_error": None, **changes}


class SettingsBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def open_page(self, responses, *, controlled_clock=False, origin=None):
        context = self.browser.new_context(viewport={"width": 920, "height": 800})
        self.addCleanup(context.close)
        page = context.new_page()
        if controlled_clock:
            page.clock.install()
        markup = '<div id="breadcrumb"></div><main id="content"></main>'
        if origin:
            page.route(origin + "/**", lambda route: route.fulfill(status=200, content_type="text/html", body=markup))
            page.goto(origin + "/")
        else:
            page.set_content(markup)
        page.add_style_tag(path=str(ROOT / "static" / "style.css"))
        page.add_style_tag(path=str(ROOT / "static" / "settings.css"))
        responses = {
            "/api/settings/paths": {"paths": {"records":"C:/isolated/records", "evidence":"C:/isolated/evidence", "library":"C:/isolated/library", "directions":"C:/isolated/directions", "creation":"C:/isolated/creation"}},
            "/api/ai/preferences": {"preferences": {"model": None, "reasoning_effort": None}},
            "/api/ai/options": {"models": [], "default_settings": {"model": None, "reasoning_effort": None}, "defaults_configured": True},
            "/api/data/preferences": {"ok": True},
            "/api/data/status": {"settings": {"enabled": False, "directory": "", "interval_days": 1, "time": "02:00", "keep": 14}, "backups": []},
            **responses,
        }
        page.evaluate("data => { window.responses = data; }", responses)
        page.add_script_tag(content=r'''
          const $ = (selector, root = document) => root.querySelector(selector);
          const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
          const esc = value => String(value ?? '').replace(/[&<>"']/g,
            c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
          const icon = () => '<svg viewBox="0 0 24 24"></svg>';
          const state = {token:'fixture-workbench-token',paths:{records:'C:/isolated/records',evidence:'C:/isolated/evidence',library:'C:/isolated/library'}};
          window.actions = [];
          window.calls = [];
          window.requests = [];
          window.aiStatuses = [];
          window.storageWrites = [];
          window.deferredRequests = [];
          Object.defineProperty(window, 'localStorage', {value:{getItem:() => null,setItem:(key,value) => window.storageWrites.push({key,value})}, configurable:true});
          window.CaseAI = {refreshAvailability: status => window.aiStatuses.push(status)};
          const api = async (path, options = {}) => {
            window.calls.push(path);
            const method = options.method || 'GET';
            window.requests.push({path, method, body:options.body ?? null, ...(options.headers ? {headers:options.headers} : {})});
            let response = window.responses[`${path}#${method}`] ?? window.responses[path];
            if (path === '/api/ai/claude/login' && method === 'GET' && !(path + '#GET' in window.responses)) {
              const claude = window.responses['/api/ai/status']?.providers?.claude || null;
              response = {claude,login:claude?.login_pending
                ? {attempt_id:'fixture-attempt',pending:true,url:'https://claude.com/cai/oauth/authorize?client_id=example&state=fixture-state',expires_at:null,callback_submitted:false}
                : {attempt_id:null,pending:false,url:null,expires_at:null,callback_submitted:false}};
            }
            if (response?.error_fixture) throw new Error(response.error_fixture);
            if (response?.deferred_fixture) return await new Promise(resolve => window.deferredRequests.push({path, resolve}));
            return response;
          };
          const copyText = (text, label) => window.actions.push({copy:text});
          const openFolder = body => window.actions.push({open:body.target});
          const readableDate = value => value;
          const toast = value => window.actions.push({toast:value});
          window.openNavigationSettings = () => window.actions.push({navigation:true});
        ''')
        page.add_script_tag(path=str(ROOT / "static" / "settings.js"))
        page.evaluate("WorkbenchSettings.render()")
        return page

    def test_remote_account_form_only_appears_on_loopback(self):
        page = self.open_page({}, origin="https://workbench.example.com")
        page.get_by_text("账号密码在运行工作台的电脑上设置。", exact=True).wait_for()
        self.assertEqual(page.locator("#settings-remote-form").count(), 0)
        self.assertNotIn("/api/remote-account", page.evaluate("window.calls"))

    def test_local_account_saves_once_and_clears_passwords_without_browser_storage(self):
        metadata = {"configured": False, "username": "", "auth_mode": "cloudflare", "remote_configured": True,
                    "public_url": "https://workbench.example.com"}
        page = self.open_page({"/api/remote-account#GET": metadata,
                               "/api/remote-account#POST": {"deferred_fixture": True}}, origin="http://127.0.0.1:8765")
        username = page.get_by_label("公网登录账号", exact=True)
        password = page.get_by_label("新密码", exact=True)
        confirm = page.get_by_label("确认新密码", exact=True)
        self.assertEqual(username.input_value(), "admin")
        self.assertEqual(password.get_attribute("autocomplete"), "new-password")
        self.assertEqual(confirm.get_attribute("autocomplete"), "new-password")
        username.fill("  fixture-admin  ")
        password.fill("fixture-password-123")
        confirm.fill("fixture-password-123")
        page.evaluate("() => { const form = document.querySelector('#settings-remote-form'); form.dispatchEvent(new Event('submit', {cancelable:true})); form.dispatchEvent(new Event('submit', {cancelable:true})); }")
        page.wait_for_function("window.deferredRequests.length === 1")
        self.assertTrue(password.is_disabled())
        posts = page.evaluate("window.requests.filter(call => call.path === '/api/remote-account' && call.method === 'POST')")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["body"], {"username": "fixture-admin", "password": "fixture-password-123", "confirm_password": "fixture-password-123"})
        page.evaluate("result => window.deferredRequests[0].resolve(result)",
                      {**metadata, "username": "fixture-admin", "configured": True, "saved": True, "auth_mode": "password"})
        page.get_by_text("本机账号密码已就绪；Cloudflare 公网入口仍需切换。", exact=True).wait_for()
        self.assertEqual(password.input_value(), "")
        self.assertEqual(confirm.input_value(), "")
        self.assertEqual(page.evaluate("window.storageWrites"), [])

    def test_local_account_confirmation_and_utf8_limits_prevent_submission(self):
        page = self.open_page({"/api/remote-account": {"configured": False, "username": "", "remote_configured": True}}, origin="http://localhost:8765")
        password = page.get_by_label("新密码", exact=True)
        confirm = page.get_by_label("确认新密码", exact=True)
        password.fill("fixture-password-123")
        confirm.fill("another-password-456")
        page.get_by_role("button", name="保存账号密码", exact=True).click()
        page.get_by_text("两次输入的密码不一致。", exact=True).wait_for()
        password.fill("密码" * 65)
        confirm.fill("密码" * 65)
        page.get_by_role("button", name="保存账号密码", exact=True).click()
        page.get_by_text("密码至少 12 个字符，最多 256 个 UTF-8 字节。", exact=True).wait_for()
        self.assertEqual(page.evaluate("window.requests.filter(call => call.path === '/api/remote-account' && call.method === 'POST')"), [])

    def test_public_guest_readiness_explains_password_unlock_and_save_keeps_guest_access(self):
        metadata = {"configured": False, "username": "", "auth_mode": "password", "remote_configured": True,
                    "guest_enabled": True, "public_ready": True}
        page = self.open_page({"/api/remote-account#GET": metadata,
                               "/api/remote-account#POST": {**metadata, "username": "admin", "configured": True, "saved": True}},
                              origin="http://127.0.0.1:8765")
        page.get_by_text("访客浏览已开放；设置账号密码后，可登录使用完整功能。", exact=True).wait_for()
        password = page.get_by_label("新密码", exact=True)
        confirm = page.get_by_label("确认新密码", exact=True)
        password.fill("fixture-password-123")
        confirm.fill("fixture-password-123")
        page.get_by_role("button", name="保存账号密码", exact=True).click()
        page.get_by_text("公网账号已更新，已登录的设备需要重新登录；访客浏览不受影响。", exact=True).wait_for()
        self.assertNotIn("Cloudflare", page.locator("#settings-remote-status").inner_text())
        self.assertEqual(password.input_value(), "")
        self.assertEqual(confirm.input_value(), "")
        self.assertEqual(page.evaluate("window.storageWrites"), [])
        page.evaluate("window.responses['/api/remote-account#GET'] = {...window.responses['/api/remote-account#GET'],configured:true,username:'admin'}")
        page.get_by_role("button", name="重新读取账号状态", exact=True).click()
        page.get_by_text("已设置公网账号：admin。访客无需登录即可浏览，登录后可使用完整功能。", exact=True).wait_for()

    def test_guest_configuration_without_verified_deployment_keeps_pending_notice(self):
        page = self.open_page({"/api/remote-account": {"configured": False, "username": "", "auth_mode": "password",
                                                      "remote_configured": True, "guest_enabled": True, "public_ready": False}},
                              origin="http://127.0.0.1:8765")
        page.get_by_text("尚未设置公网登录账号。填写并保存后，本机账号密码即可就绪；Cloudflare 公网入口仍需切换。", exact=True).wait_for()
        self.assertNotIn("访客浏览已开放", page.locator("#settings-remote-status").inner_text())

    def test_remote_account_metadata_is_escaped_and_missing_config_disables_save(self):
        name = '<img src=x onerror="window.injected=true">'
        page = self.open_page({"/api/remote-account": {"configured": True, "username": name, "auth_mode": "password", "remote_configured": True}}, origin="http://localhost:8765")
        page.wait_for_function("document.querySelector('#settings-remote-username').value.startsWith('<img')")
        self.assertIn(name, page.locator("#settings-remote-status").inner_text())
        self.assertEqual(page.locator("#settings-group-remote img").count(), 0)
        self.assertFalse(page.evaluate("Boolean(window.injected)"))
        page.evaluate("window.responses['/api/remote-account'] = {configured:false, username:'',remote_configured:false,error:'尚未配置公网连接，请先完成公网隧道配置。'}")
        page.get_by_role("button", name="重新读取账号状态", exact=True).click()
        page.get_by_text("尚未配置公网连接，请先完成公网隧道配置。", exact=True).wait_for()
        self.assertTrue(page.get_by_role("button", name="保存账号密码", exact=True).is_disabled())

    def test_existing_actions_paths_and_source_text_are_preserved(self):
        source = 'C:/sales/<img src=x onerror="window.injected=true">'
        page = self.open_page({
            "/api/ai/status": {"available": True, "label": "已找到测试 Codex", "running": 1, "queued": 2},
            "/api/performance/summary": {"sources": [source], "imports": {"last": "2026-10-08"}},
        })
        page.get_by_text("已找到本机 Codex", exact=True).wait_for()
        self.assertIn("1 项正在处理 · 2 项排队中", page.locator("#settings-codex-detail").inner_text())
        self.assertEqual(page.locator("#settings-data-sources code").inner_text(), source)
        self.assertEqual(page.locator("#settings-data-sources img").count(), 0)
        page.locator('[data-settings-copy="records"]').click()
        page.locator('[data-settings-open="evidence"]').click()
        page.get_by_role("button", name="管理保存的视图").click()
        page.locator('[data-settings-source="0"]').click()
        self.assertEqual(page.evaluate("window.actions"), [
            {"copy": "C:/isolated/records"}, {"open": "evidence"}, {"navigation": True}, {"copy": source},
        ])
        self.assertEqual(page.evaluate("window.calls.filter(path => !path.startsWith('/api/data/') && !['/api/ai/preferences','/api/ai/options'].includes(path))"), ["/api/ai/status", "/api/settings/paths", "/api/performance/summary", "/api/public-stats/link", "/api/ai/claude/login"])
        self.assertFalse(page.evaluate("Boolean(window.injected)"))

    def test_independent_failure_retries_and_shutdown_status(self):
        page = self.open_page({
            "/api/ai/status": {"error_fixture": "暂时离线"},
            "/api/performance/summary": {"error_fixture": "来源不可读"},
        })
        page.get_by_text("状态暂时无法读取", exact=True).wait_for()
        self.assertEqual(page.locator('[data-settings-copy="records"]').count(), 1)
        page.evaluate('''() => {
          window.responses['/api/ai/status'] = {available:true,shutting_down:true};
          window.responses['/api/performance/summary'] = {sources:[]};
        }''')
        page.get_by_role("button", name="重新检查", exact=True).click()
        page.get_by_role("button", name="重新读取", exact=True).click()
        page.get_by_text("服务正在关闭", exact=True).wait_for()
        page.get_by_text("尚未设置销售数据文件夹，可先在「带货数据」导入表格。", exact=True).wait_for()
        self.assertFalse(page.get_by_role("button", name="重新检查", exact=True).is_disabled())

    def test_claude_uses_client_login_and_subscription_without_key_configuration(self):
        label = '使用本机登录 <img src=x onerror="window.injected=true">'
        page = self.open_page({
            "/api/ai/status": {"available": True, "providers": {
                "codex": {"available": False, "error": "未安装 Codex"},
                "claude": {"available": True, "label": label, "desktop_bundled": True,
                           "auth_method": "claude.ai", "subscription_type": "max"},
            }},
            "/api/performance/summary": {"sources": []},
        })
        page.get_by_text("已连接 Claude 订阅", exact=True).wait_for()
        self.assertEqual(page.locator("#settings-claude-status").get_attribute("data-status"), "available")
        self.assertEqual(page.locator("#settings-codex-status").get_attribute("data-status"), "unavailable")
        self.assertIn(label, page.locator("#settings-claude-detail").inner_text())
        self.assertIn("Claude 账号登录 · 订阅：max", page.locator("#settings-claude-detail").inner_text())
        self.assertEqual(page.locator("#settings-claude-card input").count(), 0)
        self.assertFalse(page.locator("#settings-claude-recovery").is_visible())
        self.assertEqual(page.get_by_role("button", name="安装 Anthropic SDK").count(), 0)
        self.assertIn("客户端额度，无需填写 key", page.locator("#settings-claude-card").inner_text())
        self.assertIn("Claude 桌面版自带的调用程序", page.locator("#settings-claude-card").inner_text())
        self.assertIn("默认沿用本机调用设置", page.locator("#settings-claude-card").inner_text())
        self.assertFalse(page.locator("#settings-claude-login").is_visible())
        self.assertEqual(page.locator("#settings-claude-card img").count(), 0)
        self.assertFalse(page.evaluate("Boolean(window.injected)"))

    def test_claude_check_refreshes_shared_status_after_client_login(self):
        page = self.open_page({
            "/api/ai/status": {"available": True, "providers": {
                "codex": {"available": True, "label": "已找到本机 Codex"},
                "claude": {"available": False, "error": "请连接 Claude 订阅完成调用授权。", "desktop_bundled": True},
            }},
            "/api/performance/summary": {"sources": []},
        })
        page.get_by_text("Claude 订阅尚未连接", exact=True).wait_for()
        self.assertEqual(page.locator("#settings-claude-detail").inner_text(), "请连接 Claude 订阅完成调用授权。")
        self.assertTrue(page.get_by_role("button", name="连接 Claude 订阅", exact=True).is_enabled())
        page.evaluate('''() => {
          window.responses['/api/ai/status'].providers.claude = {
            available:true,label:'使用客户端额度',auth_method:'claude.ai',subscription_type:'pro'
          };
        }''')
        page.get_by_role("button", name="检查 Claude", exact=True).click()
        page.get_by_text("已连接 Claude 订阅", exact=True).wait_for()
        self.assertIn("订阅：pro", page.locator("#settings-claude-detail").inner_text())
        self.assertFalse(page.get_by_role("button", name="检查 Claude", exact=True).is_disabled())
        self.assertEqual(page.evaluate("window.calls.filter(path => !path.startsWith('/api/data/') && !['/api/ai/preferences','/api/ai/options'].includes(path))"), [
            "/api/ai/status", "/api/settings/paths", "/api/performance/summary", "/api/public-stats/link", "/api/ai/claude/login", "/api/ai/status",
        ])
        self.assertTrue(page.evaluate("window.aiStatuses.at(-1).providers.claude.available"))

    def test_claude_connect_posts_once_and_polls_until_subscription_is_ready(self):
        pending = {"available": False, "desktop_bundled": True, "login_pending": True}
        page = self.open_page({
            "/api/ai/status": {"available": True, "providers": {
                "codex": {"available": True},
                "claude": {"available": False, "desktop_bundled": True},
            }},
            "/api/ai/claude/login": {"claude": pending, "message": "已打开官方授权页"},
            "/api/performance/summary": {"sources": []},
        }, controlled_clock=True)
        page.get_by_role("button", name="连接 Claude 订阅", exact=True).wait_for(state="visible")
        page.evaluate("pending => { window.responses['/api/ai/status'].providers.claude = pending; }", pending)
        page.evaluate("() => { const button = document.querySelector('#settings-claude-login'); button.onclick(); button.onclick(); }")
        page.get_by_text("正在等待 Claude 账号授权", exact=True).wait_for()
        self.assertTrue(page.get_by_role("button", name="等待账号授权…", exact=True).is_disabled())
        page.wait_for_function("window.calls.filter(path => path === '/api/ai/status').length === 2")
        page.clock.run_for(2500)
        page.wait_for_function("window.calls.filter(path => path === '/api/ai/status').length === 3")
        page.evaluate("() => { window.responses['/api/ai/status'].providers.claude = {available:true,desktop_bundled:true,auth_method:'claude.ai',subscription_type:'pro'}; }")
        page.clock.run_for(2500)
        page.get_by_text("已连接 Claude 订阅", exact=True).wait_for()
        self.assertIn("订阅：pro", page.locator("#settings-claude-detail").inner_text())
        self.assertTrue(page.evaluate("window.aiStatuses.at(-1).providers.claude.available"))
        self.assertFalse(page.locator("#settings-claude-login").is_visible())
        completed_calls = page.evaluate("window.calls")
        page.clock.run_for(10000)
        self.assertEqual(page.evaluate("window.calls"), completed_calls)
        self.assertEqual(page.evaluate("window.requests.filter(call => call.path === '/api/ai/claude/login' && call.method === 'POST')"), [
            {"path": "/api/ai/claude/login", "method": "POST", "body": {}},
        ])

    def test_claude_pending_check_stops_on_navigation_without_another_login(self):
        page = self.open_page({
            "/api/ai/status": {"available": True, "providers": {
                "codex": {"available": True},
                "claude": {"available": False, "desktop_bundled": True, "login_pending": True},
            }},
            "/api/performance/summary": {"sources": []},
        }, controlled_clock=True)
        page.get_by_text("正在等待 Claude 账号授权", exact=True).wait_for()
        page.clock.run_for(2500)
        page.wait_for_function("window.calls.filter(path => path === '/api/ai/status').length === 2")
        page.evaluate("window.dispatchEvent(new Event('hashchange'))")
        calls_before_leaving = page.evaluate("window.calls")
        page.clock.run_for(15000)
        self.assertEqual(page.evaluate("window.calls"), calls_before_leaving)
        self.assertEqual(page.evaluate("window.requests.filter(call => call.method === 'POST' && !call.path.startsWith('/api/data/'))"), [])

    def test_claude_authorization_poll_expires_after_five_minutes(self):
        page = self.open_page({
            "/api/ai/status": {"available": True, "providers": {
                "codex": {"available": True},
                "claude": {"available": False, "desktop_bundled": True, "login_pending": True},
            }},
            "/api/performance/summary": {"sources": []},
        }, controlled_clock=True)
        page.get_by_text("正在等待 Claude 账号授权", exact=True).wait_for()
        page.clock.fast_forward(5 * 60 * 1000)
        page.get_by_text("等待授权已暂停。点击「检查 Claude」确认结果；如果已过期，可重新授权。", exact=True).wait_for()
        finished_calls = page.evaluate("window.calls")
        page.clock.run_for(10000)
        self.assertEqual(page.evaluate("window.calls"), finished_calls)
        self.assertTrue(page.get_by_role("button", name="检查 Claude", exact=True).is_enabled())

    def test_claude_connect_failure_is_safe_and_can_be_retried_without_key(self):
        error = '<img src=x onerror="window.injected=true">授权暂未开始'
        page = self.open_page({
            "/api/ai/status": {"available": True},
            "/api/ai/claude/login": {"error_fixture": error},
            "/api/performance/summary": {"sources": []},
        })
        page.get_by_role("button", name="连接 Claude 订阅", exact=True).click()
        self.assertEqual(page.locator("#settings-claude-detail").inner_text(), error)
        self.assertTrue(page.get_by_role("button", name="重新授权", exact=True).is_enabled())
        self.assertEqual(page.locator("#settings-claude-card input, #settings-claude-card img").count(), 0)
        self.assertFalse(page.evaluate("Boolean(window.injected)"))

    def test_pending_authorization_recovers_protected_link_and_keeps_callback_input_during_poll(self):
        page = self.open_page({
            "/api/ai/status": {"available": True, "providers": {"codex": {"available": True}, "claude": PENDING}},
            "/api/ai/claude/login#GET": {"claude": PENDING, "login": pending_login()},
            "/api/performance/summary": {"sources": []},
        }, controlled_clock=True)
        page.get_by_role("button", name="复制授权链接", exact=True).wait_for(state="visible")
        self.assertEqual(page.locator("#settings-claude-reopen").get_attribute("href"), AUTH_URL)
        self.assertEqual(page.locator("#settings-claude-reopen").get_attribute("rel"), "noopener noreferrer")
        page.get_by_role("button", name="复制授权链接", exact=True).click()
        self.assertIn({"copy": AUTH_URL}, page.evaluate("window.actions"))
        callback = "http://localhost:43210/callback?code=fixture-code&state=fixture-state"
        page.get_by_label("填写回调链接或授权码", exact=True).fill(callback)
        page.clock.run_for(2500)
        page.wait_for_function("window.calls.filter(path => path === '/api/ai/claude/login').length === 2")
        self.assertEqual(page.get_by_label("填写回调链接或授权码", exact=True).input_value(), callback)
        get_requests = page.evaluate("window.requests.filter(call => call.path === '/api/ai/claude/login')")
        self.assertTrue(all(request["method"] == "GET" for request in get_requests))
        self.assertTrue(all(request["headers"] == {"X-Workbench-Token": "fixture-workbench-token"} for request in get_requests))
        self.assertEqual(page.evaluate("window.storageWrites"), [])

    def test_reentry_restores_same_attempt_without_posting_another_login(self):
        page = self.open_page({
            "/api/ai/status": {"available": True, "providers": {"claude": PENDING}},
            "/api/ai/claude/login#GET": {"claude": PENDING, "login": pending_login()},
            "/api/performance/summary": {"sources": []},
        }, controlled_clock=True)
        page.get_by_role("button", name="复制授权链接", exact=True).wait_for(state="visible")
        page.get_by_label("填写回调链接或授权码", exact=True).fill("fixture-code#fixture-state")
        page.evaluate("window.dispatchEvent(new Event('hashchange')); WorkbenchSettings.render()")
        page.wait_for_function("window.requests.filter(call => call.path === '/api/ai/claude/login').length === 2")
        self.assertEqual(page.get_by_label("填写回调链接或授权码", exact=True).input_value(), "")
        self.assertEqual(page.locator("#settings-claude-reopen").get_attribute("href"), AUTH_URL)
        self.assertEqual(page.evaluate("window.requests.filter(call => call.method === 'POST' && !call.path.startsWith('/api/data/'))"), [])

    def test_callback_submits_current_attempt_once_and_keeps_input_until_connection_is_ready(self):
        page = self.open_page({
            "/api/ai/status": {"available": True, "providers": {"claude": PENDING}},
            "/api/ai/claude/login#GET": {"claude": PENDING, "login": pending_login()},
            "/api/ai/claude/callback": {"error_fixture": "授权码与本次授权不匹配。"},
            "/api/performance/summary": {"sources": []},
        }, controlled_clock=True)
        callback = "http://localhost:43210/callback?code=fixture-code&state=fixture-state"
        page.get_by_label("填写回调链接或授权码", exact=True).fill(callback)
        page.get_by_role("button", name="提交回调", exact=True).click()
        page.get_by_text("授权码与本次授权不匹配。", exact=True).wait_for()
        self.assertEqual(page.get_by_label("填写回调链接或授权码", exact=True).input_value(), callback)
        page.evaluate("result => { window.responses['/api/ai/claude/callback'] = result; window.responses['/api/ai/claude/login#GET'] = result; }",
                      {"claude": PENDING, "login": pending_login(callback_submitted=True)})
        page.evaluate("() => { const form = document.querySelector('#settings-claude-callback-form'); form.dispatchEvent(new Event('submit', {cancelable:true})); form.dispatchEvent(new Event('submit', {cancelable:true})); }")
        page.get_by_role("button", name="已提交，等待连接…", exact=True).wait_for()
        self.assertEqual(page.get_by_label("填写回调链接或授权码", exact=True).input_value(), callback)
        self.assertTrue(page.get_by_label("填写回调链接或授权码", exact=True).is_disabled())
        self.assertTrue(page.get_by_role("button", name="取消本次授权", exact=True).is_enabled())
        posts = page.evaluate("window.requests.filter(call => call.path === '/api/ai/claude/callback')")
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[-1]["body"], {"attempt_id": "fixture-attempt", "callback_url": callback})
        page.evaluate("window.responses['/api/ai/status'].providers.claude = {available:true,login_pending:false,auth_method:'claude.ai'}")
        page.clock.run_for(2500)
        page.get_by_text("已连接 Claude 订阅", exact=True).wait_for()
        self.assertEqual(page.get_by_label("填写回调链接或授权码", exact=True).input_value(), "")

    def test_background_callback_failure_shows_safe_error_and_preserves_input_for_retry(self):
        accepted = {"claude": PENDING, "login": pending_login(callback_submitted=True)}
        page = self.open_page({
            "/api/ai/status": {"available": True, "providers": {"claude": PENDING}},
            "/api/ai/claude/login#GET": {"claude": PENDING, "login": pending_login()},
            "/api/ai/claude/callback": accepted,
            "/api/performance/summary": {"sources": []},
        }, controlled_clock=True)
        callback = "http://localhost:43210/callback?code=fixture-code&state=fixture-state"
        input_field = page.get_by_label("填写回调链接或授权码", exact=True)
        input_field.fill(callback)
        page.evaluate("result => window.responses['/api/ai/claude/login#GET'] = result", accepted)
        page.get_by_role("button", name="提交回调", exact=True).click()
        page.get_by_role("button", name="已提交，等待连接…", exact=True).wait_for()
        self.assertEqual(input_field.input_value(), callback)
        error = '<img src=x onerror="window.injected=true">回调未送达，请重试。'
        page.evaluate("result => window.responses['/api/ai/claude/login#GET'] = result", {
            "claude": PENDING, "login": pending_login(callback_error=error),
        })
        page.clock.run_for(2500)
        page.get_by_text(error, exact=True).wait_for()
        self.assertEqual(input_field.input_value(), callback)
        self.assertTrue(input_field.is_enabled())
        self.assertTrue(page.get_by_role("button", name="提交回调", exact=True).is_enabled())
        self.assertEqual(page.locator("#settings-claude-login-error img").count(), 0)
        self.assertFalse(page.evaluate("Boolean(window.injected)"))
        page.evaluate("result => window.responses['/api/ai/claude/login#GET'] = result", accepted)
        page.get_by_role("button", name="提交回调", exact=True).click()
        page.get_by_role("button", name="已提交，等待连接…", exact=True).wait_for()
        posts = page.evaluate("window.requests.filter(call => call.path === '/api/ai/claude/callback')")
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[-1]["body"], {"attempt_id": "fixture-attempt", "callback_url": callback})
        self.assertEqual(input_field.input_value(), callback)

    def test_uncertain_background_callback_keeps_duplicate_submission_disabled_and_allows_cancel(self):
        accepted = {"claude": PENDING, "login": pending_login(callback_submitted=True)}
        page = self.open_page({
            "/api/ai/status": {"available": True, "providers": {"claude": PENDING}},
            "/api/ai/claude/login#GET": {"claude": PENDING, "login": pending_login()},
            "/api/ai/claude/callback": accepted,
            "/api/performance/summary": {"sources": []},
        }, controlled_clock=True)
        callback = "fixture-code#fixture-state"
        input_field = page.get_by_label("填写回调链接或授权码", exact=True)
        input_field.fill(callback)
        page.evaluate("result => window.responses['/api/ai/claude/login#GET'] = result", accepted)
        page.get_by_role("button", name="提交回调", exact=True).click()
        page.get_by_role("button", name="已提交，等待连接…", exact=True).wait_for()
        page.evaluate("result => window.responses['/api/ai/claude/login#GET'] = result", {
            "claude": PENDING, "login": pending_login(callback_submitted=True, callback_error="回调已发送，仍在等待连接；可继续等待或取消本次授权。"),
        })
        page.clock.run_for(2500)
        page.get_by_text("回调已发送，仍在等待连接；可继续等待或取消本次授权。", exact=True).wait_for()
        self.assertEqual(input_field.input_value(), callback)
        self.assertTrue(page.get_by_role("button", name="已提交，等待连接…", exact=True).is_disabled())
        self.assertTrue(page.get_by_role("button", name="取消本次授权", exact=True).is_enabled())

    def test_failed_attempt_preserves_input_until_a_new_attempt_replaces_it(self):
        page = self.open_page({
            "/api/ai/status": {"available": True, "providers": {"claude": PENDING}},
            "/api/ai/claude/login#GET": {"claude": PENDING, "login": pending_login()},
            "/api/performance/summary": {"sources": []},
        }, controlled_clock=True)
        input_field = page.get_by_label("填写回调链接或授权码", exact=True)
        input_field.fill("fixture-code#fixture-state")
        terminal = {"available": False, "login_pending": False, "error": "本次授权未能完成，请重新授权。"}
        page.evaluate("result => { window.responses['/api/ai/status'].providers.claude = result.claude; window.responses['/api/ai/claude/login#GET'] = result; }", {
            "claude": terminal, "login": pending_login(pending=False, url=None),
        })
        page.get_by_role("button", name="检查 Claude", exact=True).click()
        page.get_by_text("本次授权未能完成，请重新授权。", exact=True).wait_for()
        self.assertEqual(input_field.input_value(), "fixture-code#fixture-state")
        self.assertFalse(page.locator("#settings-claude-recovery").is_visible())
        page.evaluate("result => { window.responses['/api/ai/claude/login'] = result; window.responses['/api/ai/claude/login#GET'] = result; window.responses['/api/ai/status'].providers.claude = result.claude; }", {
            "claude": PENDING, "login": pending_login(attempt_id="fixture-next-attempt"),
        })
        page.get_by_role("button", name="重新授权", exact=True).click()
        page.get_by_text("正在等待 Claude 账号授权", exact=True).wait_for()
        self.assertEqual(input_field.input_value(), "")

    def test_cancel_or_expired_authorization_allows_new_attempt_and_removes_link(self):
        terminal = {"available": False, "login_pending": False, "error": "本次授权已结束，请重新授权。"}
        page = self.open_page({
            "/api/ai/status": {"available": True, "providers": {"claude": PENDING}},
            "/api/ai/claude/login#GET": {"claude": PENDING, "login": pending_login()},
            "/api/ai/claude/login/cancel": {"claude": terminal, "login": pending_login(pending=False, url=None)},
            "/api/performance/summary": {"sources": []},
        }, controlled_clock=True)
        page.get_by_role("button", name="取消本次授权", exact=True).wait_for(state="visible")
        page.get_by_label("填写回调链接或授权码", exact=True).fill("fixture-code#fixture-state")
        page.evaluate("terminal => { window.responses['/api/ai/status'].providers.claude = terminal; window.responses['/api/ai/claude/login#GET'] = {claude:terminal,login:{attempt_id:null,pending:false,url:null,expires_at:null,callback_submitted:false}}; }", terminal)
        page.get_by_role("button", name="取消本次授权", exact=True).click()
        page.get_by_role("button", name="重新授权", exact=True).wait_for(state="visible")
        self.assertTrue(page.get_by_role("button", name="重新授权", exact=True).is_enabled())
        self.assertFalse(page.locator("#settings-claude-recovery").is_visible())
        self.assertIsNone(page.locator("#settings-claude-reopen").get_attribute("href"))
        self.assertEqual(page.get_by_label("填写回调链接或授权码", exact=True).input_value(), "")
        self.assertEqual(page.evaluate("window.requests.filter(call => call.path === '/api/ai/claude/login/cancel')"), [
            {"path": "/api/ai/claude/login/cancel", "method": "POST", "body": {"attempt_id": "fixture-attempt"}},
        ])
        requests = page.evaluate("window.requests")
        page.clock.run_for(10000)
        self.assertEqual(page.evaluate("window.requests"), requests)

    def test_untrusted_authorization_link_is_never_opened_or_copied(self):
        for url in ["javascript:alert(1)", "https://claude.ai.evil.example/oauth", "https://user:secret@claude.ai/oauth", "http://claude.ai/oauth"]:
            with self.subTest(url=url):
                page = self.open_page({
                    "/api/ai/status": {"available": True, "providers": {"claude": PENDING}},
                    "/api/ai/claude/login#GET": {"claude": PENDING, "login": pending_login(url=url)},
                    "/api/performance/summary": {"sources": []},
                }, controlled_clock=True)
                page.get_by_role("button", name="复制授权链接", exact=True).wait_for(state="visible")
                self.assertTrue(page.get_by_role("button", name="复制授权链接", exact=True).is_disabled())
                self.assertFalse(page.locator("#settings-claude-reopen").is_visible())
                self.assertIsNone(page.locator("#settings-claude-reopen").get_attribute("href"))

    def test_late_login_poll_cannot_restore_an_attempt_that_was_cancelled(self):
        terminal = {"available": False, "login_pending": False, "error": "本次授权已取消。"}
        page = self.open_page({
            "/api/ai/status": {"available": True, "providers": {"claude": PENDING}},
            "/api/ai/claude/login#GET": {"claude": PENDING, "login": pending_login()},
            "/api/ai/claude/login/cancel": {"claude": terminal, "login": pending_login(pending=False, url=None)},
            "/api/performance/summary": {"sources": []},
        }, controlled_clock=True)
        page.get_by_role("button", name="取消本次授权", exact=True).wait_for(state="visible")
        page.evaluate("window.responses['/api/ai/claude/login#GET'] = {deferred_fixture:true}")
        page.get_by_role("button", name="检查 Claude", exact=True).click()
        page.wait_for_function("window.deferredRequests.length === 1")
        page.get_by_role("button", name="取消本次授权", exact=True).click()
        page.get_by_text("本次授权已取消。", exact=True).wait_for()
        page.evaluate("result => window.deferredRequests[0].resolve(result)", {"claude": PENDING, "login": pending_login()})
        page.wait_for_function("!document.querySelector('#settings-claude-check').disabled")
        self.assertFalse(page.locator("#settings-claude-recovery").is_visible())
        self.assertIsNone(page.locator("#settings-claude-reopen").get_attribute("href"))
        self.assertTrue(page.get_by_role("button", name="重新授权", exact=True).is_enabled())

    def test_transient_status_failure_preserves_the_pending_callback_input(self):
        page = self.open_page({
            "/api/ai/status": {"available": True, "providers": {"claude": PENDING}},
            "/api/ai/claude/login#GET": {"claude": PENDING, "login": pending_login()},
            "/api/performance/summary": {"sources": []},
        }, controlled_clock=True)
        input_field = page.get_by_label("填写回调链接或授权码", exact=True)
        input_field.fill("fixture-code#fixture-state")
        page.evaluate("window.responses['/api/ai/status'] = {error_fixture:'连接暂时中断'}")
        page.get_by_role("button", name="检查 Claude", exact=True).click()
        page.get_by_text("连接暂时中断", exact=True).wait_for()
        self.assertEqual(input_field.input_value(), "fixture-code#fixture-state")
        self.assertTrue(page.locator("#settings-claude-recovery").is_visible())

    def test_authorization_poll_uses_the_server_expiry_instead_of_starting_new_waits(self):
        page = self.open_page({
            "/api/ai/status": {"available": True, "providers": {"claude": PENDING}},
            "/api/ai/claude/login#GET": {"claude": PENDING, "login": pending_login()},
            "/api/performance/summary": {"sources": []},
        }, controlled_clock=True)
        page.get_by_role("button", name="复制授权链接", exact=True).wait_for(state="visible")
        page.evaluate("window.responses['/api/ai/claude/login#GET'].login.expires_at = new Date(Date.now() + 5000).toISOString()")
        page.get_by_role("button", name="检查 Claude", exact=True).click()
        page.clock.run_for(5000)
        page.get_by_text("等待授权已暂停。点击「检查 Claude」确认结果；如果已过期，可重新授权。", exact=True).wait_for()
        requests = page.evaluate("window.requests")
        page.clock.run_for(10000)
        self.assertEqual(page.evaluate("window.requests"), requests)


if __name__ == "__main__":
    unittest.main()
