"""Full-workbench backup UI exercised with isolated browser and ZIP fixtures."""
from copy import deepcopy
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
STATUS = {
    "settings": {"enabled": False, "directory": "D:/isolated-backups", "interval_days": 1, "time": "02:00", "keep": 14},
    "last_backup": None,
    "last_error": "",
    "next_backup": None,
    "scheduler": {"installed": False, "mode": "app", "message": "测试环境使用服务内定时。"},
    "backups": [],
    "busy": False,
}
INSPECT = {"import_id": "fixture-import", "summary": {
    "created_at": "2026-10-11T02:00:00+08:00", "files": 7, "bytes": 1024,
    "components": ["全部产品", "案例", "媒体附件", "视图设置"],
}}
PREFERENCES = {
    "case-library-navigation-v1": "[{\"label\":\"收藏\"}]",
    "workbench-product-v1": "all",
    "workbench-sidebar-v1": "expanded",
    "native-library-mode": "entries",
    "case-workbench.ai.last-settings": "{}",
    "unrelated-account-secret": "outside-this-workbench",
}


class BackupBrowserTests(unittest.TestCase):
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

    def open_page(self, responses=None, *, controlled_clock=False):
        context = self.browser.new_context(viewport={"width": 1180, "height": 1000})
        self.addCleanup(context.close)
        page = context.new_page()
        if controlled_clock:
            page.clock.install()
        page.set_content('<div id="breadcrumb"></div><main id="content"></main>')
        page.add_style_tag(path=str(ROOT / "static" / "style.css"))
        page.add_style_tag(path=str(ROOT / "static" / "settings.css"))
        fixtures = {
            "/api/ai/status": {"available": True, "providers": {"codex": {"available": True}, "claude": {"available": True}}},
            "/api/performance/summary": {"sources": []},
            "/api/data/status": deepcopy(STATUS),
            "/api/data/preferences": {"ok": True},
            "/api/data/import/inspect": deepcopy(INSPECT),
            **(responses or {}),
        }
        page.evaluate("data => { window.responses = data; }", fixtures)
        page.evaluate("data => { window.preferences = data; }", deepcopy(PREFERENCES))
        page.add_script_tag(content=r'''
          const $ = (selector, root = document) => root.querySelector(selector);
          const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
          const esc = value => String(value ?? '').replace(/[&<>"']/g,
            c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
          const icon = () => '<svg viewBox="0 0 24 24"></svg>';
          const state = {token:'fixture-workbench-token',paths:{records:'C:/isolated/records'}};
          window.actions = [];
          window.requests = [];
          window.deferredRequests = [];
          window.unhandledErrors = [];
          window.addEventListener('unhandledrejection', event => window.unhandledErrors.push(event.reason?.message));
          Object.defineProperty(window, 'localStorage', {value:{
            get length() { return Object.keys(window.preferences).length; },
            key: index => Object.keys(window.preferences)[index] ?? null,
            getItem: key => window.preferences[key] ?? null,
            setItem: (key, value) => { window.preferences[key] = value; },
            removeItem: key => { delete window.preferences[key]; },
          }, configurable:true});
          const api = async (path, options = {}) => {
            const method = options.method || 'GET';
            const headers = {...options.headers};
            if (method === 'POST') headers['X-Workbench-Token'] = state.token;
            const body = options.body instanceof File ? {name:options.body.name, size:options.body.size, type:options.body.type} : options.body ?? null;
            window.requests.push({path, method, body, headers});
            const result = window.responses[`${path}#${method}`] ?? window.responses[path];
            if (result?.error_fixture) throw new Error(result.error_fixture);
            if (result?.deferred_fixture) return await new Promise(resolve => window.deferredRequests.push({path, resolve, signal:options.signal}));
            return structuredClone(result);
          };
          const copyText = value => window.actions.push({copy:value});
          const openFolder = value => window.actions.push({open:value});
          const readableDate = value => value;
          const toast = value => window.actions.push({toast:value});
          document.addEventListener('click', event => {
            if (event.target instanceof HTMLAnchorElement && event.target.hasAttribute('download')) {
              event.preventDefault();
              window.actions.push({download:event.target.getAttribute('href'),name:event.target.getAttribute('download')});
            }
          });
        ''')
        page.add_script_tag(path=str(ROOT / "static" / "settings.js"))
        page.evaluate("WorkbenchSettings.render()")
        return page

    def ready(self, page):
        page.get_by_text("尚未备份", exact=True).wait_for()

    def select_zip(self, page, name="workbench-data.zip"):
        page.locator("#settings-data-file").set_input_files({"name": name, "mimeType": "application/zip", "buffer": b"PK isolated fixture"})

    def requests(self, page, path):
        return page.evaluate("path => window.requests.filter(call => call.path === path)", path)

    def test_schedule_custom_directory_and_preferences_mirror(self):
        saved = deepcopy(STATUS)
        saved["settings"] = {"enabled": True, "directory": "E:/custom folder", "interval_days": 3, "time": "21:15", "keep": 0}
        saved["scheduler"] = {"installed": True, "mode": "windows", "message": "系统任务已保存。"}
        saved["next_backup"] = "2026-10-12T21:15:00+08:00"
        page = self.open_page({"/api/data/settings": saved, "/api/data/pick-directory": {"path": "E:/custom folder", "cancelled": False}})
        self.ready(page)
        self.assertEqual(page.get_by_label("备份间隔（天）").input_value(), "1")
        self.assertEqual(page.get_by_label("保留最近多少份").input_value(), "14")
        self.assertIn("全部产品", page.locator("#settings-data-card").inner_text())
        self.assertIn("自动备份未开启，仍可随时手动备份。", page.locator("#settings-backup-scheduler").inner_text())
        page.get_by_role("button", name="选择文件夹", exact=True).click()
        page.wait_for_function("document.querySelector('#settings-backup-directory').value === 'E:/custom folder'")
        page.get_by_label("开启自动备份", exact=True).check()
        page.get_by_label("备份间隔（天）").fill("3")
        page.get_by_label("备份时间（本机时间）").fill("21:15")
        page.get_by_label("保留最近多少份").fill("0")
        page.get_by_role("button", name="保存备份设置", exact=True).click()
        page.wait_for_function("window.actions.some(action => action.toast === '备份设置已保存。')")
        self.assertEqual(self.requests(page, "/api/data/settings")[0]["body"], saved["settings"])
        mirrors = self.requests(page, "/api/data/preferences")
        self.assertEqual(len(mirrors), 2)
        expected = {key: value for key, value in PREFERENCES.items() if key != "unrelated-account-secret"}
        self.assertTrue(all(request["body"] == {"preferences": expected} for request in mirrors))
        self.assertIn("工作台关闭后仍可自动备份", page.locator("#settings-backup-scheduler").inner_text())
        self.assertIn("登录当前 Windows 用户", page.locator("#settings-backup-scheduler").inner_text())
        self.assertFalse(page.locator("#settings-data-error").is_visible())

    def test_export_download_busy_lock_and_invalid_download_url(self):
        page = self.open_page({"/api/data/export": {"deferred_fixture": True}})
        self.ready(page)
        page.get_by_role("button", name="导出全部数据", exact=True).click()
        self.assertTrue(page.locator("#settings-data-export").is_disabled())
        self.assertTrue(page.locator("#settings-data-file").is_disabled())
        self.assertIn("正在打包全部产品和媒体", page.locator("#settings-data-progress").inner_text())
        page.evaluate("result => window.deferredRequests[0].resolve(result)", {"download_url": "/api/data/download/fixture_archive", "archive": {"name": "all-products.zip"}})
        page.wait_for_function("window.actions.some(action => action.download)")
        self.assertEqual(page.evaluate("window.actions.find(action => action.download)"), {"download": "/api/data/download/fixture_archive", "name": "all-products.zip"})
        self.assertNotIn("unrelated-account-secret", self.requests(page, "/api/data/export")[0]["body"]["preferences"])
        page.evaluate("window.responses['/api/data/export'] = {download_url:'javascript:window.injected=true'}")
        page.get_by_role("button", name="导出全部数据", exact=True).click()
        page.get_by_text("导出文件下载地址无效，请重新导出。", exact=True).wait_for()
        self.assertFalse(page.evaluate("Boolean(window.injected)"))
        self.assertTrue(page.locator("#settings-data-export").is_enabled())
        self.assertEqual(page.evaluate("window.unhandledErrors"), [])

    def test_import_requires_inspection_and_explicit_confirmation_then_restores_preferences(self):
        malicious = '<img src=x onerror="window.injected=true">'
        imported = deepcopy(INSPECT)
        imported["summary"]["components"].append(malicious)
        page = self.open_page({
            "/api/data/import/inspect": imported,
            "/api/data/import": {"message": "全部产品已恢复。", "recovery_path": "D:/recover/" + malicious, "preferences": {
                "case-library-layout": "list", "workbench-product-v1": "new-product", "outside-new-key": "rejected",
            }},
        })
        self.ready(page)
        self.select_zip(page)
        page.locator("#settings-data-import-preview").wait_for(state="visible")
        self.assertIn(malicious, page.locator("#settings-data-import-summary").inner_text())
        self.assertEqual(page.locator("#settings-data-import-preview img").count(), 0)
        self.assertTrue(page.locator("#settings-data-import").is_disabled())
        self.assertEqual(self.requests(page, "/api/data/import"), [])
        inspect = self.requests(page, "/api/data/import/inspect")[0]
        self.assertEqual(inspect["headers"], {"Content-Type": "application/zip", "X-Workbench-Token": "fixture-workbench-token"})
        self.assertEqual(inspect["body"]["name"], "workbench-data.zip")
        self.assertIn("整体替换", page.locator(".settings-data-import-preview .settings-data-warning").inner_text())
        page.get_by_label("我已核对文件，同意替换当前全部数据", exact=True).check()
        page.get_by_role("button", name="确认恢复总数据", exact=True).click()
        page.get_by_role("dialog").wait_for(state="visible")
        self.assertIn("导入前的数据已备份至", page.get_by_role("dialog").inner_text())
        self.assertIn(malicious, page.get_by_role("dialog").inner_text())
        self.assertEqual(page.get_by_role("dialog").locator("img").count(), 0)
        self.assertEqual(self.requests(page, "/api/data/import")[0]["body"], {"import_id": "fixture-import", "confirm": True})
        self.assertEqual(len(self.requests(page, "/api/data/preferences")), 2)
        self.assertEqual(page.evaluate("window.preferences"), {"case-library-layout": "list", "workbench-product-v1": "new-product", "unrelated-account-secret": "outside-this-workbench"})
        self.assertEqual(page.evaluate("window.unhandledErrors"), [])

    def test_new_file_invalidates_prior_import_id_and_safe_errors_remain_retryable(self):
        page = self.open_page()
        self.ready(page)
        self.select_zip(page)
        page.locator("#settings-data-import-preview").wait_for(state="visible")
        page.get_by_label("我已核对文件，同意替换当前全部数据", exact=True).check()
        error = '<img src=x onerror="window.injected=true"> ZIP 文件缺少清单。'
        page.evaluate("error => window.responses['/api/data/import/inspect'] = {error_fixture:error}", error)
        self.select_zip(page, "other.zip")
        page.get_by_text(error, exact=True).wait_for()
        self.assertFalse(page.locator("#settings-data-import-preview").is_visible())
        self.assertFalse(page.locator("#settings-data-import-confirm").is_checked())
        self.assertTrue(page.locator("#settings-data-import").is_disabled())
        self.assertEqual(page.locator("#settings-data-error img").count(), 0)
        self.assertEqual(self.requests(page, "/api/data/import"), [])
        self.assertTrue(page.locator("#settings-data-file").is_enabled())
        self.assertEqual(page.evaluate("window.unhandledErrors"), [])

    def test_manual_backup_mirrors_preferences_and_reports_last_result(self):
        completed = deepcopy(STATUS)
        completed["last_backup"] = {"name": "fixture.zip", "path": "D:/isolated-backups/fixture.zip", "created_at": "2026-10-11T09:00:00+08:00", "bytes": 1024}
        completed["backups"] = [completed["last_backup"]]
        page = self.open_page({"/api/data/backup": {"deferred_fixture": True}})
        self.ready(page)
        page.get_by_role("button", name="立即备份", exact=True).click()
        page.wait_for_function("window.deferredRequests.length === 1")
        self.assertTrue(page.get_by_role("button", name="保存备份设置", exact=True).is_disabled())
        self.assertEqual(len(self.requests(page, "/api/data/preferences")), 2)
        page.evaluate("result => window.deferredRequests[0].resolve(result)", {"backup": completed["last_backup"], "status": completed})
        page.wait_for_function("window.actions.some(action => action.toast === '总数据备份已保存到本地。')")
        self.assertEqual(page.locator("#settings-backup-last-path").inner_text(), completed["last_backup"]["path"])
        page.locator(".settings-backup-history summary").click()
        self.assertEqual(page.locator(".settings-backup-item strong").inner_text(), "fixture.zip")
        page.evaluate("window.responses['/api/data/backup'] = {error_fixture:'本地磁盘空间不足'}")
        page.get_by_role("button", name="立即备份", exact=True).click()
        page.get_by_text("本地磁盘空间不足", exact=True).wait_for()
        self.assertTrue(page.locator("#settings-data-backup").is_enabled())
        self.assertEqual(page.evaluate("window.unhandledErrors"), [])

    def test_poll_preserves_draft_schedule_and_stops_after_navigation(self):
        status = deepcopy(STATUS)
        status["busy"] = True
        page = self.open_page({"/api/data/status": status}, controlled_clock=True)
        self.ready(page)
        self.assertTrue(page.locator("#settings-data-export").is_disabled())
        page.evaluate("window.responses['/api/data/status'].busy = false")
        page.clock.run_for(5000)
        page.wait_for_function("window.requests.filter(call => call.path === '/api/data/status').length === 2")
        page.get_by_label("备份文件夹", exact=True).fill("E:/draft")
        page.clock.run_for(30000)
        page.wait_for_function("window.requests.filter(call => call.path === '/api/data/status').length === 3")
        self.assertEqual(page.get_by_label("备份文件夹", exact=True).input_value(), "E:/draft")
        page.evaluate("window.dispatchEvent(new Event('hashchange'))")
        count = len(self.requests(page, "/api/data/status"))
        page.clock.run_for(60000)
        self.assertEqual(len(self.requests(page, "/api/data/status")), count)
        self.assertEqual(page.evaluate("window.unhandledErrors"), [])

    def test_disconnected_inspection_cannot_apply_preview_or_import(self):
        page = self.open_page({"/api/data/import/inspect": {"deferred_fixture": True}})
        self.ready(page)
        self.select_zip(page)
        page.wait_for_function("window.deferredRequests.length === 1")
        page.evaluate("window.dispatchEvent(new Event('hashchange')); document.querySelector('#content').replaceChildren()")
        self.assertTrue(page.evaluate("window.deferredRequests[0].signal.aborted"))
        page.evaluate("result => window.deferredRequests[0].resolve(result)", INSPECT)
        self.assertEqual(page.locator("#settings-data-import-preview").count(), 0)
        self.assertEqual(self.requests(page, "/api/data/import"), [])
        self.assertEqual(page.evaluate("window.unhandledErrors"), [])

    def test_preference_sync_failure_does_not_hide_status_or_start_unsynced_backup(self):
        page = self.open_page({"/api/data/preferences": {"error_fixture": "视图设置写入失败"}})
        self.ready(page)
        self.assertIn("浏览器视图设置暂未同步", page.locator("#settings-data-error").inner_text())
        page.get_by_role("button", name="立即备份", exact=True).click()
        page.get_by_text("视图设置写入失败", exact=True).wait_for()
        self.assertEqual(self.requests(page, "/api/data/backup"), [])
        self.assertTrue(page.locator("#settings-data-backup").is_enabled())

    def test_invalid_directory_or_missing_enabled_directory_never_saves(self):
        page = self.open_page()
        self.ready(page)
        page.get_by_label("备份文件夹", exact=True).fill("relative/folder")
        page.get_by_role("button", name="保存备份设置", exact=True).click()
        page.get_by_text("请输入完整的本地文件夹路径，或点击「选择文件夹」。", exact=True).wait_for()
        self.assertEqual(self.requests(page, "/api/data/settings"), [])
        page.get_by_label("备份文件夹", exact=True).fill("")
        page.get_by_label("开启自动备份", exact=True).check()
        page.get_by_role("button", name="保存备份设置", exact=True).click()
        page.get_by_text("开启自动备份前，请选择本地备份文件夹。", exact=True).wait_for()
        self.assertEqual(self.requests(page, "/api/data/settings"), [])

    def test_enabled_app_fallback_explains_catchup_and_retention_upper_bound(self):
        status = deepcopy(STATUS)
        status["settings"]["enabled"] = True
        page = self.open_page({"/api/data/status": status})
        self.ready(page)
        self.assertIn("关闭服务后会在下次打开时补备份", page.locator("#settings-backup-scheduler").inner_text())
        self.assertEqual(page.locator("#settings-backup-keep").get_attribute("max"), "3650")
        page.get_by_label("保留最近多少份").fill("3651")
        page.get_by_role("button", name="保存备份设置", exact=True).click()
        self.assertEqual(self.requests(page, "/api/data/settings"), [])

    def test_disabled_schedule_does_not_show_stale_enabled_scheduler_message(self):
        status = deepcopy(STATUS)
        status["scheduler"] = {"installed": False, "mode": "windows", "message": "关闭工作台后也会备份"}
        page = self.open_page({"/api/data/status": status})
        self.ready(page)
        self.assertEqual(page.locator("#settings-backup-scheduler").inner_text(), "自动备份未开启，仍可随时手动备份。")

    def test_leaving_while_preferences_mirror_pending_does_not_start_status_read(self):
        page = self.open_page({"/api/data/preferences": {"deferred_fixture": True}}, controlled_clock=True)
        page.wait_for_function("window.deferredRequests.length === 1")
        page.evaluate("window.dispatchEvent(new Event('hashchange')); document.querySelector('#content').replaceChildren()")
        page.evaluate("window.deferredRequests[0].resolve({ok:true})")
        page.clock.run_for(60000)
        self.assertEqual(self.requests(page, "/api/data/status"), [])
        self.assertEqual(page.evaluate("window.unhandledErrors"), [])


if __name__ == "__main__":
    unittest.main()
