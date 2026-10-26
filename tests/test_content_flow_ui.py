"""Content workflow integration uses disposable business records and the real local API."""
from pathlib import Path
import json
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs
from uuid import uuid4

from playwright.sync_api import expect
import test_phase4_writing_ui as writing


ARTIFACTS = Path(__file__).resolve().parents[1] / ".test-artifacts"


def content_filter(page):
    return json.loads(parse_qs(page.url.partition("?")[2])["filter"][0])


def set_content_condition(page, field, values):
    """Edit a condition through the visible builder, retaining the other rules."""
    editor = page.locator("#content-flow-filter-editor")
    if not editor.is_visible():
        page.locator("#content-flow-filter-toggle").click()
    rows = editor.locator("[data-filter-row]")
    row = next((candidate for candidate in rows.all()
                if candidate.locator("[data-filter-field]").input_value() == field), None)
    if row is None:
        editor.locator("[data-filter-add]").click()
        row = rows.last
        row.locator("[data-filter-field]").select_option(field)
    if not row.locator("details").evaluate("node => node.open"):
        row.locator("[data-filter-values]").click()
    for choice in row.locator("[data-filter-value]").all():
        choice.set_checked(choice.get_attribute("data-filter-value") in values)
    with page.expect_response(lambda response: "/api/content?" in response.url):
        editor.locator("[data-filter-apply]").click()
    expect(editor).to_be_hidden()
    page.wait_for_function("""({field, values}) => {
        const filter = JSON.parse(new URLSearchParams(location.hash.split('?')[1]).get('filter') || '{}');
        return filter.conditions?.some(rule => rule.field === field
            && rule.values.length === values.length && values.every(value => rule.values.includes(value)));
    }""", arg={"field": field, "values": values})


class ContentFlowBrowserTests(unittest.TestCase):
    setUpClass = classmethod(writing.WritingLifecycleBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(writing.WritingLifecycleBrowserTests.tearDownClass.__func__)
    setUp = writing.WritingLifecycleBrowserTests.setUp
    stop_server = writing.WritingLifecycleBrowserTests.stop_server
    request = writing.WritingLifecycleBrowserTests.request
    open_page = writing.WritingLifecycleBrowserTests.open_page

    def test_idle_refresh_keeps_content_nodes_and_open_menu_then_drops_archived_rows(self):
        script = self.script()
        page = self.content_page()
        page.locator(".flow-menu > summary").click()
        page.locator("#content-flow-query").focus()
        page.evaluate("window.retainedFlowRow = document.querySelector('.flow-row')")
        page.evaluate("ContentFlow.route(true)")
        self.assertTrue(page.evaluate("retainedFlowRow === document.querySelector('.flow-row')"))
        self.assertTrue(page.locator(".flow-menu").evaluate("node => node.open"))
        self.assertEqual(page.evaluate("document.activeElement.id"), "content-flow-query")
        self.manage("script:" + script["family_id"], {"archived": True})
        page.evaluate("ContentFlow.route(true)")
        expect(page.locator(".flow-row")).to_have_count(0)

    def test_archived_rows_are_deleted_for_good_after_confirming_their_count(self):
        job = self.finished_job("归档后删除的制作单")
        unused = self.script("归档后删除的脚本", text="Unused script.")
        posted = self.script("已登记视频的脚本", text="Posted script.")
        self.server.app.creation.mutate("script_status", {"request_id": uuid4().hex,
            "revision": self.server.app.creation.public()["revision"], "script_status": {
                "script_id": posted["id"], "status": "published", "video_id": "7999999999999999913", "published_on": "2026-10-01"}})
        for identifier in ("video:" + job["id"], "script:" + unused["family_id"], "script:" + posted["family_id"]):
            self.manage(identifier, {"archived": True})
        transfer = self.server.app.data_transfer
        with patch.object(transfer, "backup") as backup:
            page = self.content_page()
            self.view(page, "all")
            button = page.locator("#content-flow-delete-archived")
            expect(button).to_have_text("删除已归档 2 项")
            button.click()
            dialog = page.locator(".flow-delete-dialog")
            expect(dialog.locator(".context-note")).to_contain_text("将永久删除 2 项已归档内容：1 条脚本、1 张视频制作单。脚本会连同它的全部版本一起删除。")
            expect(dialog).to_contain_text("1 条已归档的脚本被制作单、案例、视频 ID 或写稿计划用到，会保留在归档里。")
            expect(dialog.get_by_role("button", name="永久删除 2 项", exact=True)).to_be_disabled()
            backup.assert_not_called()
            dialog.locator('[name="sure"]').check()
            dialog.get_by_role("button", name="永久删除 2 项", exact=True).click()
            dialog.wait_for(state="detached")
            expect(page.locator("#toast .toast-item").last).to_have_text("已永久删除 2 项")
            for gone in ("video:" + job["id"], "script:" + unused["family_id"]):
                expect(page.locator(f'[data-content-id="{gone}"]')).to_have_count(0)
            expect(page.locator(f'[data-content-id="script:{posted["family_id"]}"]')).to_have_count(1)
            expect(button).to_be_hidden()
            backup.assert_not_called()
        self.assertNotIn(job["id"], [row["id"] for row in self.server.app.production.public()["jobs"]])
        self.assertNotIn(unused["family_id"], [row["family_id"] for row in self.server.app.creation.public()["scripts"]])

    def test_delete_confirmation_needs_only_checked_consent_without_a_backup_directory(self):
        script = self.script("无需备份目录即可确认删除的归档脚本")
        self.manage("script:" + script["family_id"], {"archived": True})
        transfer = self.server.app.data_transfer
        existing = {"enabled": False, "directory": "", "interval_days": 7, "time": "04:35", "keep": 9}
        transfer.config["settings"] = dict(existing)
        with patch.object(transfer, "backup") as backup, patch.object(transfer, "save_settings") as save_settings:
            page = self.content_page()
            data_posts = []
            page.on("request", lambda request: data_posts.append(request.url)
                    if request.method == "POST" and "/api/data/" in request.url else None)
            self.view(page, "all")
            page.locator("#content-flow-delete-archived").click()
            dialog = page.locator(".flow-delete-dialog")
            confirmation, submit = dialog.locator('[name="sure"]'), dialog.locator("[data-submit]")
            expect(confirmation).to_be_enabled()
            expect(submit).to_be_disabled()
            expect(dialog.locator("[data-backup-setup], #flow-backup-directory")).to_have_count(0)
            self.assertNotIn("备份", dialog.inner_text())
            page.emulate_media(color_scheme="dark")
            self.screenshot(page, "content-delete-dark.png")
            confirmation.check()
            expect(confirmation).to_be_checked()
            expect(submit).to_be_enabled()
            page.set_viewport_size({"width": 390, "height": 900})
            self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))
            self.screenshot(page, "content-delete-mobile.png")
            page.set_viewport_size({"width": 1280, "height": 900})
            page.emulate_media(color_scheme="light")
            self.screenshot(page, "content-delete-light.png")
            confirmation.uncheck()
            expect(submit).to_be_disabled()
            confirmation.check()
            expect(submit).to_be_enabled()
            backup.assert_not_called()
            self.assertEqual(self.server.app.creation.get_script(script["id"])["text"], script["text"])
            submit.click()
            dialog.wait_for(state="detached")
            expect(page.locator("#toast .toast-item").last).to_have_text("已永久删除 1 项")
            expect(page.locator(f'[data-content-id="script:{script["family_id"]}"]')).to_have_count(0)
            self.assertEqual(transfer.status()["settings"], existing)
            backup.assert_not_called()
            save_settings.assert_not_called()
            self.assertEqual(data_posts, [])

    def test_failed_delete_preserves_archived_records_and_can_retry(self):
        script = self.script("删除失败后可以重试的归档脚本")
        self.manage("script:" + script["family_id"], {"archived": True})
        transfer = self.server.app.data_transfer
        before = self.server.app.creation.path.read_bytes()
        with patch.object(transfer, "backup") as backup:
            page = self.content_page()
            deletions = []

            def fail_first_delete(route):
                body = route.request.post_data_json
                if body.get("preview"):
                    return route.continue_()
                deletions.append(body)
                if len(deletions) == 1:
                    return route.fulfill(status=503, json={"error": "隔离测试：删除暂时失败，请重试。"})
                return route.continue_()

            page.route("**/api/content/delete-archived", fail_first_delete)
            self.view(page, "all")
            page.locator("#content-flow-delete-archived").click()
            dialog = page.locator(".flow-delete-dialog")
            confirmation, submit = dialog.locator('[name="sure"]'), dialog.locator("[data-submit]")
            expect(submit).to_be_disabled()
            confirmation.check()
            expect(submit).to_be_enabled()
            submit.click()
            expect(dialog.locator("[data-error]")).to_contain_text("删除暂时失败，请重试")
            expect(confirmation).to_be_enabled()
            expect(confirmation).to_be_checked()
            expect(submit).to_be_enabled()
            self.assertEqual(self.server.app.creation.path.read_bytes(), before)
            self.assertTrue(next(row for row in self.flow()["items"] if row["id"] == "script:" + script["family_id"])["archived"])
            submit.click()
            dialog.wait_for(state="detached")
            expect(page.locator("#toast .toast-item").last).to_have_text("已永久删除 1 项")
            expect(page.locator(f'[data-content-id="script:{script["family_id"]}"]')).to_have_count(0)
            self.assertEqual(len(deletions), 2)
            self.assertEqual(deletions[0], deletions[1])
            backup.assert_not_called()

    def test_idle_content_refresh_preserves_adopted_copy_focus_and_open_version_history(self):
        first = self.script(text="Old temporary version.")
        self.server.app.creation.mutate("script", {"request_id": uuid4().hex,
            "revision": self.server.app.creation.public()["revision"], "script": {
                "title": "Second version", "product": "202H", "text": "New temporary version.",
                "parent_id": first["id"], "confirmed": True}})
        page = self.open_page(route=f"#/202H/content/{first['id']}")
        page.locator("[data-script-versions] > summary").click()
        page.locator(f'[data-script-version="{first["id"]}"] > summary').click()
        page.locator("[data-adopted-copy]").focus()
        page.evaluate("window.retainedAdoptedCopy = document.querySelector('#content-adopted-script')")
        page.evaluate("CaseScripts.route(true)")
        self.assertTrue(page.evaluate("retainedAdoptedCopy === document.querySelector('#content-adopted-script')"))
        self.assertTrue(page.locator("[data-script-versions]").evaluate("node => node.open"))
        self.assertTrue(page.locator(f'[data-script-version="{first["id"]}"]').evaluate("node => node.open"))
        self.assertTrue(page.evaluate("document.activeElement.hasAttribute('data-adopted-copy')"))

    def script(self, title="目标内容", text="Temporary selected script."):
        return self.server.app.creation.mutate("script", {"request_id": uuid4().hex,
            "revision": self.server.app.creation.public()["revision"], "script": {
                "title": title, "product": "202H", "text": text, "confirmed": True}})["script"]

    def finished_job(self, title):
        """A reviewed and approved order that has not been posted: it waits under 待发布."""
        script = self.script(title, text=f"{title} spoken line.")
        job = self.server.app.create_production_job({"request_id": uuid4().hex,
            "revision": self.server.app.production.public()["revision"], "job": {"script_id": script["id"]}})["job"]
        take = "TAK-" + uuid4().hex[:12]
        data = self.server.app.production.read()
        row = next(row for row in data["jobs"] if row["id"] == job["id"])
        row.update(selected_take=take, takes=[{"id": take, "model_id": "fixture", "voice_id": "fixture", "text": script["text"]}],
                   timelines=[{"unique_id": "timeline-" + take, "name": "Reviewed", "take_id": take, "saved": True}],
                   manual_completion={"at": "2026-10-01", "take_id": take, "timeline_uid": "timeline-" + take, "basis": "human_review"})
        self.server.app.production.write(data)
        return job

    def flow(self, query="product=202H&view=all"):
        code, _, data = self.request("GET", "/api/content?" + query)
        self.assertEqual(code, 200, data)
        return data

    def manage(self, identifier, patch):
        code, _, data = self.request("POST", "/api/content/manage", {"request_id": uuid4().hex,
            "revision": self.flow()["revision"], "id": identifier, "management": patch})
        self.assertEqual(code, 200, data)
        return data

    def content_page(self, width=1280):
        page = self.open_page("#/202H/content", width=width)
        page.locator("#content-flow-list .flow-row, #content-flow-list .empty").first.wait_for()
        return page

    def view(self, page, name):
        page.locator(f'[data-flow-view="{name}"]').click()
        expect(page.locator(f'[data-flow-view="{name}"]')).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#content-flow-list")).to_have_attribute("data-view", name)

    def screenshot(self, page, name):
        ARTIFACTS.mkdir(exist_ok=True)
        for close in page.locator(".toast-close").all():
            close.click()
        page.screenshot(path=str(ARTIFACTS / name), full_page=True)

    def test_one_list_keeps_secondary_records_in_more_and_sidebar_always_returns_to_content(self):
        self.script("磨刀器 · 新开头待制作")
        produced = self.script("磨刀器 · 操作演示", text="Another script.")
        self.server.app.create_production_job({"request_id": uuid4().hex,
            "revision": self.server.app.production.public()["revision"], "job": {"script_id": produced["id"]}})
        page = self.open_page("#/202H/library")
        if not page.locator("#ai-nav").is_visible():
            page.locator("#sidebar-toggle").click()
        page.locator("#ai-nav").click()
        expect(page.locator("#content-flow-list .flow-row")).to_have_count(2)
        expect(page.locator(".scripts-tabs")).to_have_count(0)
        self.assertEqual(page.locator("[data-flow-view]").all_text_contents(), ["待处理2", "进行中0", "待发布0", "全部2"])
        page.locator(".flow-main-menu > summary").click()
        self.assertEqual(page.locator(".flow-main-menu [role=menuitem]").all_inner_texts(), ["协作记录", "制作设置", "批量管理"])
        self.screenshot(page, "unified-content-desktop.png")
        page.locator(".flow-main-menu").get_by_role("menuitem", name="协作记录").click()
        page.locator("#ai-task-list").wait_for()
        expect(page.locator(".scripts-tabs")).to_have_count(0)
        page.locator("#navigate-up").click()
        page.locator("#content-flow-list").wait_for()
        page.evaluate("() => { const badge = document.querySelector('#ai-active-count'); badge.textContent = '2'; badge.hidden = false; }")
        page.locator("#ai-nav").click()
        page.locator("#content-flow-list").wait_for()
        self.assertEqual(page.evaluate("location.hash"), "#/202H/content")

    def test_unproduced_script_rows_stay_compact_with_records_in_the_menu_and_fit_phones(self):
        self.script("磨刀器 · 切纸效果")
        page = self.content_page()
        row = page.locator(".flow-row")
        self.assertLessEqual(row.bounding_box()["height"], 48)
        expect(row.locator(".flow-stage")).to_have_text("待制作")
        expect(row.locator(".flow-next")).to_have_text("开始制作")
        expect(page.locator("[data-plan], #content-plan-dialog")).to_have_count(0)
        row.locator(".flow-menu > summary").click()
        expect(row.locator(".flow-menu [role=menuitem]")).to_have_text(["采用稿版本详情 · 第 1 版", "归档"])
        page = self.content_page(width=390)
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))
        self.screenshot(page, "content-flow-mobile.png")
        page.locator(".flow-main-menu > summary").click()
        page.locator("#content-flow-batch-open").click()
        for scheme in ("light", "dark"):
            page.emulate_media(color_scheme=scheme)
            self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))
            self.screenshot(page, f"unified-content-batch-{scheme}-mobile.png")

    def test_unproduced_adopted_copy_opens_writing_with_the_exact_copy_and_secondary_version_link(self):
        status, _, response = self.request("POST", "/api/ai/tasks", {"request_id": uuid4().hex,
            "product": "202H", "action": "review", "instruction": "临时测试采用稿与制作入口。", "skill_selection": "none"})
        self.assertEqual(status, 200, response)
        task = response["task"]
        deadline = time.monotonic() + 5
        while task["busy"] and time.monotonic() < deadline:
            time.sleep(.02)
            task = self.server.app.ai.get(task["task_id"])
        self.assertEqual(task["status"], "completed")
        status, _, response = self.request("POST", "/api/scripts", {
            "revision": self.server.app.creation.public()["revision"], "request_id": uuid4().hex,
            "script": {"title": "已选择的确切采用稿", "text": "The exact adopted copy for this production.",
                       "translation_zh": "本次制作采用的正文。", "product": "202H", "confirmed": True},
            "source": {"task_id": task["task_id"], "round_id": task["rounds"][-1]["round_id"], "task_revision": task["revision"]}})
        self.assertEqual(status, 200, response)
        script = response["script"]
        page = self.content_page()
        row = page.locator(f'[data-content-id="script:{script["family_id"]}"]')
        expect(row.locator(".flow-kind")).to_have_text("采用稿")
        target = f"#/202H/ai/{task['task_id']}?round={task['rounds'][-1]['round_id']}&adopted={script['id']}"
        expect(row.locator(".flow-next")).to_have_attribute("href", target)
        row.locator(".flow-menu > summary").click()
        expect(row.get_by_role("menuitem", name="采用稿版本详情 · 第 1 版", exact=True)).to_have_attribute(
            "href", f"#/202H/content/{script['id']}")
        row.locator(".flow-menu > summary").click()
        row.locator(".flow-next").click()
        expect(page.locator(f'[data-task-adopted="{script["id"]}"]')).to_be_visible()
        expect(page.locator(f'[data-task-adopted="{script["id"]}"]')).to_contain_text(script["text"])
        expect(page.locator("#script-production-toggle")).to_have_count(0)

    def test_archive_restore_preserve_original_business_record(self):
        item = self.script("归档后仍保留正文和关联")
        business_bytes = self.server.app.creation.path.read_bytes()
        page = self.content_page()
        page.locator(".flow-menu > summary").click()
        page.locator('[data-archive]').click()
        expect(page.locator(".flow-row")).to_have_count(0)
        self.view(page, "all")
        expect(page.locator(".flow-row")).to_have_count(1)
        expect(page.locator(".flow-row .flow-stage")).to_have_text("已归档")
        page.locator(".flow-menu > summary").click()
        expect(page.locator('[data-archive]')).to_have_text("恢复")
        page.locator('[data-archive]').click()
        expect(page.locator(".flow-row .flow-stage")).to_have_text("待制作")
        self.view(page, "attention")
        expect(page.locator(".flow-row")).to_have_count(1)
        self.assertEqual(self.server.app.creation.path.read_bytes(), business_bytes)
        self.assertEqual(self.server.app.creation.get_script(item["id"])["text"], item["text"])

    def test_publish_view_holds_finished_orders_and_archives_a_selection(self):
        first, second = self.finished_job("已完成待发布 A"), self.finished_job("已完成待发布 B")
        self.script("仍待制作的稿", text="Still to make.")
        page = self.content_page()
        self.assertEqual(page.locator("[data-flow-view]").all_text_contents(), ["待处理1", "进行中0", "待发布2", "全部3"])
        expect(page.locator("#content-flow-select")).to_be_hidden()
        self.view(page, "publish")
        expect(page.locator(".flow-row")).to_have_count(2)
        expect(page.locator(".flow-row .flow-stage")).to_have_count(2)
        expect(page.locator(".flow-row .flow-next")).to_have_count(2)
        page.locator(".flow-main-menu > summary").click()
        page.locator("#content-flow-batch-open").click()
        page.locator(f'[data-flow-select="video:{first["id"]}"]').check()
        expect(page.locator("#content-flow-batch")).to_contain_text("已选 1 项")
        page.locator("[data-flow-archive-selected]").click()
        expect(page.locator(".flow-row")).to_have_count(1)
        expect(page.locator(".flow-row")).to_have_attribute("data-content-id", f"video:{second['id']}")
        expect(page.locator("#content-flow-batch")).to_be_hidden()
        archived = next(row for row in self.flow()["items"] if row["id"] == f"video:{first['id']}")
        self.assertTrue(archived["archived"])

    def test_bulk_restore_and_permanent_delete_use_the_unified_list(self):
        app = self.server.app
        first, second = self.finished_job("保留采用稿的制作记录"), self.finished_job("原制作列表归档记录")
        first_script = app.creation.get_script(first["script_id"])
        app.production.mutate("job_archive", {"request_id": uuid4().hex,
            "revision": app.production.public()["revision"], "job_id": second["id"], "archived": True})
        page = self.content_page()
        self.view(page, "all")
        page.locator(".flow-main-menu > summary").click()
        page.locator("#content-flow-batch-open").click()
        page.locator(f'[data-flow-select="video:{first["id"]}"]').check()
        page.locator(f'[data-flow-select="video:{second["id"]}"]').check()
        expect(page.locator("[data-flow-restore-selected]")).to_contain_text("恢复 1")
        page.locator("[data-flow-restore-selected]").click()
        expect(page.locator("#content-flow-batch")).to_be_hidden()
        self.assertFalse(app.production.job(second["id"])["archived"])
        page.locator(".flow-main-menu > summary").click()
        page.locator("#content-flow-batch-open").click()
        page.locator(f'[data-flow-select="video:{first["id"]}"]').check()
        page.locator("[data-flow-delete-selected]").click()
        dialog = page.locator(".production-dialog[open]")
        expect(dialog.locator("[data-submit]")).to_be_enabled()
        dialog.locator('[name="sure"]').check()
        dialog.locator("[data-submit]").click()
        expect(page.locator(f'[data-content-id="video:{first["id"]}"]')).to_have_count(0)
        expect(page.locator(f'[data-content-id="video:{second["id"]}"]')).to_have_count(1)
        self.assertEqual(app.creation.get_script(first_script["id"])["text"], first_script["text"])

    def test_partial_delete_failure_keeps_failed_items_selected_for_retry(self):
        first, second = self.finished_job("删除已完成"), self.finished_job("需要重试的记录")
        page = self.content_page()
        self.view(page, "all")
        page.route("**/api/production/orders/batch", lambda route: route.fulfill(json={"changed": 1,
            "skipped": 0, "results": [{"id": first["id"], "ok": True},
                {"id": second["id"], "ok": False, "error": "测试临时失败"}]}))
        page.locator(".flow-main-menu > summary").click()
        page.locator("#content-flow-batch-open").click()
        page.locator("[data-flow-select-all]").click()
        page.locator("[data-flow-delete-selected]").click()
        dialog = page.locator(".production-dialog[open]")
        dialog.locator('[name="sure"]').check()
        dialog.locator("[data-submit]").click()
        expect(page.locator("#content-flow-batch")).to_contain_text("已选 1 项")
        expect(page.locator(f'[data-flow-select="video:{second["id"]}"]')).to_be_checked()
        expect(page.locator(f'[data-flow-select="video:{first["id"]}"]')).not_to_be_checked()

    def test_running_view_keeps_progress_visible_and_state_filter_does_not_change_the_order(self):
        app = self.server.app
        job = self.finished_job("正在生成的配音")
        data = app.production.public()
        row = next(item for item in data["jobs"] if item["id"] == job["id"])
        row.update(voicing=True, voicing_live={"progress": "正在生成第 2 句配音"}, status="draft")
        with patch.object(app, "production_catalog", return_value=data):
            page = self.content_page()
            self.view(page, "running")
            expect(page.locator(".flow-row")).to_have_count(1)
            expect(page.locator(".flow-progress")).to_have_text("正在生成第 2 句配音")
            expect(page.locator(".flow-next")).to_contain_text("查看配音进度")
            set_content_condition(page, "status", ["published"])
            expect(page.locator(".flow-row")).to_have_count(0)
            set_content_condition(page, "status", ["in_progress"])
            expect(page.locator(".flow-row")).to_have_count(1)
            self.assertEqual(content_filter(page), {"match": "all", "conditions": [
                {"field": "view", "values": ["running"]}, {"field": "status", "values": ["in_progress"]}]})
        self.assertFalse(app.production.job(job["id"]).get("voicing", False))

    def test_failed_load_after_product_switch_cannot_reuse_other_products_batch_targets(self):
        job = self.finished_job("原产品的制作记录")
        page = self.content_page()
        self.view(page, "all")
        expect(page.locator(f'[data-content-id="video:{job["id"]}"]')).to_have_count(1)
        writes = []
        page.route("**/api/production/orders/batch", lambda route: (writes.append(route.request.post_data_json), route.abort()))
        page.route("**/api/content?product=129N&*", lambda route: route.fulfill(status=503, json={"error": "测试另一产品读取失败"}))
        page.evaluate("location.hash='#/129N/content'")
        expect(page.locator("#content-flow-error")).to_contain_text("测试另一产品读取失败")
        page.locator(".flow-main-menu > summary").click()
        page.locator("#content-flow-batch-open").click()
        expect(page.locator(".flow-row")).to_have_count(0)
        expect(page.locator("[data-flow-select], [data-flow-delete-selected]")).to_have_count(0)
        self.assertEqual(writes, [])
        self.assertEqual(self.server.app.production.job(job["id"])["id"], job["id"])

    def test_views_search_and_pagination_use_server_counts(self):
        scripts = [self.script(f"Batch content {number:02d}", text=f"Batch text {number}.") for number in range(35)]
        for item in scripts[:2]:
            self.manage("script:" + item["family_id"], {"archived": True})
        page = self.content_page()
        expect(page.locator(".flow-row")).to_have_count(30)
        expect(page.locator("#content-flow-pages")).to_contain_text("第 1 / 2 页")
        page.locator('[data-flow-page="2"]').click()
        expect(page.locator(".flow-row")).to_have_count(3)
        self.view(page, "all")
        expect(page.locator(".flow-row")).to_have_count(30)
        page.locator('[data-flow-page="2"]').click()
        expect(page.locator(".flow-row")).to_have_count(5)
        expect(page.locator(".flow-row.is-finished")).to_have_count(2)
        page.locator('#content-flow-query').fill("Batch content 04")
        expect(page.locator(".flow-row")).to_have_count(1)
        expect(page.locator(".flow-row .flow-title")).to_have_text("Batch content 04｜脚本")
        expect(page.locator("#content-flow-pages")).to_be_empty()
        page.reload()
        expect(page.locator(".flow-row")).to_have_count(1)
        expect(page.locator('#content-flow-query')).to_have_value("Batch content 04")
        expect(page.locator('[data-flow-view="all"]')).to_have_attribute("aria-pressed", "true")

    def test_return_from_script_detail_restores_content_scope_and_filters(self):
        item = self.script("只查这一条内容")
        self.script("另一个内容", text="Another script.")
        page = self.content_page()
        self.view(page, "all")
        set_content_condition(page, "kind", ["script"])
        page.locator("#content-flow-query").fill("只查这一条")
        expect(page.locator(".flow-row")).to_have_count(1)
        before = page.evaluate("location.hash")
        page.locator('.flow-row .flow-title').click()
        page.locator("#script-production-toggle").wait_for()
        self.assertTrue(page.evaluate("location.hash").endswith(item["id"]))
        self.assertEqual(page.locator("#navigate-up").get_attribute("href"), before)
        page.locator("#navigate-up").click()
        page.locator("#content-flow-query").wait_for()
        self.assertEqual(page.evaluate("location.hash"), before)
        expect(page.locator("#content-flow-query")).to_have_value("只查这一条")
        self.assertEqual(content_filter(page), {"match": "all", "conditions": [
            {"field": "kind", "values": ["script"]}]})
        expect(page.locator('[data-flow-view="all"]')).to_have_attribute("aria-pressed", "false")
        expect(page.locator(".flow-row")).to_have_count(1)


if __name__ == '__main__':
    unittest.main()
