"""Task updates retain adopted families and the reader's expanded version history."""
import json
import unittest
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import expect
import test_phase4_writing_ui as writing


class TaskAdoptedBrowserTests(unittest.TestCase):
    setUpClass = classmethod(writing.WritingLifecycleBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(writing.WritingLifecycleBrowserTests.tearDownClass.__func__)
    setUp = writing.WritingLifecycleBrowserTests.setUp
    stop_server = writing.WritingLifecycleBrowserTests.stop_server
    request = writing.WritingLifecycleBrowserTests.request
    catalog = writing.WritingLifecycleBrowserTests.catalog
    create_task = writing.WritingLifecycleBrowserTests.create_task
    detail = writing.WritingLifecycleBrowserTests.detail
    open_page = writing.WritingLifecycleBrowserTests.open_page

    def save(self, text, task=None, parent=None):
        body = {"request_id": uuid4().hex, "revision": self.catalog()["revision"], "script": {
            "title": "同一任务的采用稿", "text": text, "translation_zh": "采用稿中文", "product": "202H", "confirmed": True}}
        if task:
            body["source"] = {"task_id": task["task_id"], "round_id": task["rounds"][-1]["round_id"], "task_revision": task["revision"]}
        if parent:
            body["script"]["parent_id"] = parent["id"]
        code, _, result = self.request("POST", "/api/scripts", body)
        self.assertEqual(code, 200, result)
        return result["script"]

    def test_task_refresh_keeps_separate_families_and_expanded_history(self):
        task = self.create_task("用于采用稿关联的临时任务")
        first = self.save("First original words.", task=task)
        second = self.save("Second version words.", parent=first)
        independent = self.save("Different adopted content from the same task.", task=task)
        page = self.open_page(f"#/202H/ai/{task['task_id']}")
        panel = page.locator("#ai-task-adopted")
        expect(panel.locator(".task-adopted-copy")).to_have_count(2)
        family = panel.locator(f'[data-task-family="{first["family_id"]}"]')
        family.locator(":scope > summary").click()
        family.locator("[data-script-versions] > summary").click()
        original = family.locator(f'[data-script-version="{first["id"]}"]')
        original.locator(":scope > summary").click()
        expect(original).to_contain_text(first["text"])
        expect(family.locator(f'[data-script-version="{independent["id"]}"]')).to_have_count(0)
        page.evaluate("""task => CaseScripts.mountTaskAdopted(document.querySelector('#ai-task-adopted'),
            {...task, revision: 'fixture-poll-update'})""", task)
        self.assertTrue(family.evaluate("node => node.open"))
        self.assertTrue(family.locator("[data-script-versions]").evaluate("node => node.open"))
        self.assertTrue(original.evaluate("node => node.open"))
        latest = self.save("Third version after another edit.", parent=second)
        page.evaluate("""task => CaseScripts.mountTaskAdopted(document.querySelector('#ai-task-adopted'),
            {...task, revision: 'fixture-new-draft'})""", task)
        expect(family.locator(f'[data-task-adopted="{latest["id"]}"]')).to_have_count(1)
        self.assertTrue(family.evaluate("node => node.open"))
        self.assertTrue(family.locator("[data-script-versions]").evaluate("node => node.open"))
        self.assertTrue(original.evaluate("node => node.open"))
        expect(panel.locator(".task-adopted-copy")).to_have_count(2)
        self.assertEqual(self.server.app.creation.get_script(first["id"])["text"], first["text"])

    def test_saved_copy_can_start_production_directly_from_writing(self):
        task = self.create_task("已存采用稿直接制作")
        script = self.save("The exact words to put into production.", task=task)
        page = self.open_page(f"#/202H/ai/{task['task_id']}", width=390)
        family = page.locator(f'[data-task-family="{script["family_id"]}"]')
        family.locator(":scope > summary").click()
        details = family.locator(":scope > .task-adopted-actions [data-adopted-version-detail]")
        expect(details).to_have_text("版本详情")
        self.assertEqual(details.get_attribute("href"), f'#/202H/content/{script["id"]}')
        family.locator("button[data-task-production]").click()
        dialog = page.locator(".production-dialog[open]")
        dialog.wait_for()
        self.assertEqual(page.evaluate("location.hash"), f'#/202H/ai/{task["task_id"]}')
        self.assertEqual(self.server.app.production.public()["jobs"], [])
        dialog.locator("[data-submit]").click()
        page.locator("#job-archive").wait_for(state="attached")
        jobs = self.server.app.production.public()["jobs"]
        self.assertEqual((len(jobs), jobs[0]["script_id"]), (1, script["id"]))

    def test_requested_old_copy_survives_refresh_and_switching_without_writes(self):
        task = self.create_task("精确定位已采用版本")
        first = self.save("First exact adopted words.", task=task)
        latest = self.save("Newer words for a future production.", parent=first)
        job = self.server.app.create_production_job({"request_id": uuid4().hex,
            "revision": self.server.app.production.current_revision(), "job": {"script_id": first["id"]}})["job"]
        base = f'#/202H/ai/{task["task_id"]}?round={task["rounds"][0]["round_id"]}'
        page = self.open_page(f'{base}&adopted={first["id"]}', width=390)
        writes = []
        page.on("request", lambda request: writes.append(request.url)
                if request.method not in {"GET", "HEAD", "OPTIONS"} and "/api/" in request.url else None)
        family = page.locator(f'[data-task-family="{first["family_id"]}"]')
        target = family.locator(f'[data-task-adopted="{first["id"]}"]')
        expect(target.locator(".adopted-copy > pre").first).to_have_text(first["text"])
        self.assertTrue(family.evaluate("node => node.open"))
        expect(family.locator("[data-task-production]")).to_have_attribute("href", f'#/202H/production/{job["id"]}')
        expect(family.locator("[data-task-production]")).to_have_text("继续制作视频")
        page.reload()
        expect(target.locator(".adopted-copy > pre").first).to_have_text(first["text"])
        self.assertTrue(family.evaluate("node => node.open"))
        page.evaluate("hash => location.hash = hash", f'{base}&adopted={latest["id"]}')
        latest_body = family.locator(f'[data-task-adopted="{latest["id"]}"]')
        expect(latest_body.locator(".adopted-copy > pre").first).to_have_text(latest["text"])
        expect(family.locator("button[data-task-production]")).to_have_text("开始制作")
        self.assertEqual(family.locator(f'[data-task-adopted="{first["id"]}"]').count(), 0)
        for scheme in ("light", "dark"):
            page.emulate_media(color_scheme=scheme)
            self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))
            folder = Path.cwd() / ".test-artifacts" / "script-flow-ui"
            folder.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(folder / f"writing-adopted-{scheme}-mobile.png"), full_page=True)
        self.assertEqual(writes, [])
        family.locator(":scope > .task-adopted-actions [data-adopted-version-detail]").click()
        page.locator("#content-adopted-script").wait_for()
        self.assertEqual(page.locator(".script-detail-grid aside").get_by_text("来源与版本", exact=True).count(), 0)
        self.assertFalse(page.locator(".script-version-meta").evaluate("node => node.open"))
        self.assertEqual(self.server.app.production.job(job["id"])["script_id"], first["id"])
        self.assertEqual(len(self.server.app.production.public()["jobs"]), 1)

    def test_unknown_requested_copy_is_reported_without_substituting_latest(self):
        task = self.create_task("已丢失版本定位")
        script = self.save("The available adopted copy.", task=task)
        page = self.open_page(f'#/202H/ai/{task["task_id"]}?adopted=SCR-missing')
        expect(page.locator("[data-requested-adopted-missing]")).to_be_visible()
        family = page.locator(f'[data-task-family="{script["family_id"]}"]')
        self.assertFalse(family.evaluate("node => node.open"))
        self.assertEqual(page.locator('[data-task-adopted="SCR-missing"]').count(), 0)
        empty_task = self.create_task("没有采用稿的缺失定位")
        page.evaluate("hash => location.hash = hash", f'#/202H/ai/{empty_task["task_id"]}?adopted=SCR-missing')
        expect(page.locator("[data-requested-adopted-missing]")).to_be_visible()
        page.evaluate("""task => CaseScripts.mountTaskAdopted(document.querySelector('#ai-task-adopted'),
            {...task, revision: 'fixture-same-missing-copy-refresh'})""", empty_task)
        expect(page.locator("[data-requested-adopted-missing]")).to_be_visible()
        self.assertFalse(page.locator("#ai-task-adopted").evaluate("node => node.hidden"))

    def test_same_task_requested_version_changes_while_live_stream_skips_polling(self):
        task = self.create_task("实时写稿期间切换采用版本")
        first = self.save("The first exact adopted copy.", task=task)
        latest = self.save("The next exact adopted copy.", parent=first)
        page = self.open_page()
        page.evaluate("""() => { window.EventSource = class {
          constructor(url) { this.url = url; setTimeout(() => this.onopen?.({}), 0); }
          close() {}
        }; }""")
        reads = []

        def running_task(route):
            reads.append(True)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"task": {**task, "status": "running", "busy": True}}))

        page.route(f'**/api/ai/tasks/{task["task_id"]}', running_task)
        base = f'#/202H/ai/{task["task_id"]}?round={task["rounds"][0]["round_id"]}'
        page.goto(f'http://127.0.0.1:{self.server.server_port}/{base}&adopted={first["id"]}')
        expect(page.locator(f'[data-task-adopted="{first["id"]}"] .adopted-copy > pre').first).to_have_text(first["text"])
        expect(page.locator("#ai-stream-status")).to_have_text("实时连接 · 回复持续更新")
        read_count = len(reads)
        page.evaluate("hash => location.hash = hash", f'{base}&adopted={latest["id"]}')
        expect(page.locator(f'[data-task-adopted="{latest["id"]}"] .adopted-copy > pre').first).to_have_text(latest["text"])
        self.assertEqual(len(reads), read_count)
        family = page.locator(f'[data-task-family="{first["family_id"]}"]')
        self.assertTrue(family.evaluate("node => node.open"))


if __name__ == "__main__":
    unittest.main()
