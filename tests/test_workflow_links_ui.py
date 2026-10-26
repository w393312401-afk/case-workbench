"""Reciprocal workflow navigation uses disposable records and read-only browser requests."""
from __future__ import annotations

import copy
from pathlib import Path
import time
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from playwright.sync_api import expect

from elevenlabs_client import ElevenLabsClient
from test_ai_tasks import FakeBridge
from test_production import FakeEleven
from test_production_carousel_table import SOURCE, TABLE
import test_phase4_writing_ui as writing


class WorkflowLinksBrowserTests(unittest.TestCase):
    setUpClass = classmethod(writing.WritingLifecycleBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(writing.WritingLifecycleBrowserTests.tearDownClass.__func__)
    stop_server = writing.WritingLifecycleBrowserTests.stop_server
    request = writing.WritingLifecycleBrowserTests.request
    detail = writing.WritingLifecycleBrowserTests.detail
    create_task = writing.WritingLifecycleBrowserTests.create_task

    def setUp(self):
        writing.WritingLifecycleBrowserTests.setUp(self)
        self.app = self.server.app
        self.app.voices = ElevenLabsClient(key_provider=lambda: "sk_test", transport=FakeEleven())
        self.app.resolve = SimpleNamespace(status=lambda: {"ok": False, "error": "隔离测试不连接达芬奇"})
        self.app.ai.bridge = FakeBridge(output=SOURCE)
        self.task = self.create_task("仅供互通入口的隔离测试。")
        # The shared helper creates a review task; this fixture is a writing source.
        self.task["action"] = "variants"
        self.round_id = self.task["rounds"][0]["round_id"]
        source = {"task_id": self.task["task_id"], "round_id": self.round_id,
                  "task_revision": self.task["revision"], "product": "202H", "case_id": self.identifier,
                  "candidate_text": SOURCE, "candidate_sha256": "fixture-source", "snapshot": {},
                  "variant_index": 0}
        self.script = self.app.creation.mutate("script", {
            "request_id": uuid4().hex, "revision": self.app.creation.public()["revision"],
            "script": {"product": "202H", "direction_id": "202H-S2", "title": "同稿跨视频与图文",
                       "text": "Seven plates. Overkill?\nThe clamp holds the angle.",
                       "translation_zh": "七块磨板，太夸张？\n夹具固定角度。", "confirmed": True}}, source)["script"]
        self.jobs = [self.app.create_production_job({
            "request_id": uuid4().hex, "revision": self.app.production.public()["revision"],
            "job": {"script_id": self.script["id"]}})["job"] for _ in range(2)]

        # Generate the table with a local fake client, preserving a real table-to-carousel receipt.
        photo = Path(self.app.edit_skill_dir).parent / "tiktok-photo-commerce"
        (photo / "references").mkdir(parents=True, exist_ok=True)
        (photo / "SKILL.md").write_text("---\nname: tiktok-photo-commerce\n---\n# 图文", encoding="utf-8")
        (photo / "references" / "deliverables.md").write_text("# copy.json", encoding="utf-8")
        self.app.ai.bridge = FakeBridge(output=TABLE)
        table = self.app.start_carousel_table({"request_id": uuid4().hex, "script_id": self.script["id"]})["table"]
        deadline = time.monotonic() + 6
        while table["status"] == "running" and time.monotonic() < deadline:
            time.sleep(.02)
            table = self.request("GET", f"/api/production/carousel-tables/{table['id']}")[2]["table"]
        self.assertEqual(table["status"], "completed", table)
        self.table = {**table, "has_result": True, "slide_count": 3, "carousel_missing": False}
        self.carousel = self.app.create_carousel({
            "request_id": uuid4().hex, "revision": self.app.production.public()["revision"],
            "carousel": {"script_id": self.script["id"], "table_id": table["id"]}})["carousel"]

        self.variant_key = "authoritative-variant-key"
        records = {kind: [] for kind in ("scripts", "productions", "tasks", "publications", "plans", "videos")}
        linked_records = copy.deepcopy(records)
        linked_records["scripts"] = [{"id": self.script["id"], "title": self.script["title"]}]
        linked_records["productions"] = [
            {"id": row["id"], "kind": kind, "script_id": self.script["id"], "title": row["title"]}
            for row, kind in [(self.jobs[0], "video"), (self.jobs[1], "video"), (self.carousel, "carousel")]]
        linked_records["tasks"] = [{"id": self.task["task_id"], "title": "来源写稿任务", "status": "completed"}]
        self.progress = {"product": "202H", "direction_id": "202H-S2", "catalog_revision": "fixture-revision",
                         "variants": [{"index": 0, "key": "wrong-candidate-index-key", "snapshot": {}, "records": records},
                                      {"index": 1, "key": self.variant_key, "snapshot": {}, "records": linked_records}],
                         "unassigned": copy.deepcopy(records), "summary": {}}
        self.listing = self.app.production.public("202H")
        # The API is product scoped, but navigation should also reject malformed cross-product responses.
        unrelated = copy.deepcopy(self.listing["jobs"][0])
        unrelated.update(id="PRD-111111111111", script_id="SCR-unrelated", title="别的脚本制作单",
                         source={"task_id": "AIT-111111111111"})
        foreign = copy.deepcopy(self.listing["jobs"][0])
        foreign.update(id="PRD-222222222222", product="129N", title="另一产品制作单")
        self.listing["jobs"].extend([unrelated, foreign])

    def open_page(self, hash_route=None, width=1280, *, production_route=None, progress_route=None, extra_routes=(), links_open=True):
        context = self.browser.new_context(viewport={"width": width, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(12000)
        # Order pages fold the links into one line; a viewer who opened it once sees it open.
        if links_open:
            page.add_init_script("for (const kind of ['script','video','carousel','task']) localStorage.setItem(`workflow-links:${kind}:open`, '1'); localStorage.setItem('workflow-links:open', '1')")
        errors, posts = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        self.addCleanup(lambda: self.assertEqual(posts, [], "Workflow entry navigation must be read-only"))
        page.route("**/api/production?product=*", production_route or
                   (lambda route: route.fulfill(json=copy.deepcopy(self.listing))))
        page.route("**/api/direction-progress/**", progress_route or
                   (lambda route: route.fulfill(json=copy.deepcopy(self.progress))))
        task = copy.deepcopy(self.task)
        task["context"]["direction_id"] = "202H-S2"
        task["context"]["product"] = "202H"
        page.route(f"**/api/ai/tasks/{task['task_id']}", lambda route: route.fulfill(json={"task": task}))
        for pattern, handler in extra_routes:
            page.route(pattern, handler)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/{hash_route or self.script_url()}")
        page.locator("#connection-text").get_by_text("案例库同步").wait_for(state="attached")
        page.locator("#workflow-links").wait_for()
        return page

    def script_url(self):
        return f"#/202H/content/{self.script['id']}"

    @staticmethod
    def job_url(identifier):
        return f"#/202H/production/{identifier}"

    @staticmethod
    def links(page, kind):
        return page.locator(f'#workflow-links a.workflow-link[data-workflow-kind="{kind}"]')

    def link_for(self, page, kind, identifier):
        return page.locator(
            f'#workflow-links a.workflow-link[data-workflow-kind="{kind}"][href*="{identifier}"]').first

    def assert_ready(self, page, kind, count):
        expect(self.links(page, kind)).to_have_count(count)
        expect(self.links(page, kind).first).to_be_visible()

    def test_script_links_to_exact_sibling_jobs_and_authoritative_variant(self):
        self.listing["jobs"][1]["archived"] = True
        page = self.open_page(width=390)
        self.assert_ready(page, "video", 2)
        self.assert_ready(page, "carousel", 1)
        self.assert_ready(page, "variant", 1)
        href = self.links(page, "variant").first.get_attribute("href")
        self.assertEqual(parse_qs(href.split("?", 1)[1]).get("variant"), [self.variant_key])
        self.assertNotIn("wrong-candidate-index-key", href)
        self.assertIn("已归档", self.link_for(page, "video", self.jobs[1]["id"]).inner_text())
        self.assertEqual(self.links(page, "video").filter(has_text="别的脚本").count(), 0)
        self.assertEqual(self.links(page, "video").filter(has_text="另一产品").count(), 0)
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))

    def test_video_and_carousel_links_navigate_reciprocally_without_creating_jobs(self):
        page = self.open_page()
        self.assert_ready(page, "video", 2)
        self.link_for(page, "video", self.jobs[0]["id"]).click()
        page.locator("#job-archive").wait_for(state="attached")
        expect(self.links(page, "script")).to_have_count(0)
        self.assert_ready(page, "video", 1)
        self.assert_ready(page, "carousel", 1)
        self.assertEqual(self.link_for(page, "video", self.jobs[0]["id"]).count(), 0)
        self.link_for(page, "carousel", self.carousel["id"]).click()
        page.locator("#carousel-archive").wait_for(state="attached")
        expect(self.links(page, "script")).to_have_count(0)
        self.assert_ready(page, "video", 2)
        self.assertEqual(self.links(page, "carousel").count(), 0)
        page.locator('[data-step="slides"]').click()
        adopted = page.locator("#carousel-adopted-script")
        expect(adopted).to_contain_text(self.script["text"])
        # A single-version script needs no history fold in an order, only the way to its version page.
        adopted.locator(".adopted-version-single").click()
        page.locator("#script-production-toggle").wait_for()
        self.assert_ready(page, "carousel", 1)
        self.assertEqual(page.evaluate("location.hash"), self.script_url())
        self.assertEqual(len(self.app.production.public()["jobs"]), 2)
        self.assertEqual(len(self.app.production.public()["carousels"]), 1)

    def test_output_links_select_the_useful_video_and_carousel_steps(self):
        self.listing["jobs"][0]["edits"] = [{"id": "EDT-0123456789ab", "status": "awaiting_review"}]
        self.listing["carousels"][0]["slides"][0]["outputs"] = [{"id": "OUT-0123456789ab"}]
        page = self.open_page()
        self.assert_ready(page, "video", 2)
        video = self.link_for(page, "video", self.jobs[0]["id"])
        self.assertEqual(parse_qs(video.get_attribute("href").split("?", 1)[1]).get("step"), ["edit"])
        video.click()
        expect(page.locator('.production-steps [data-step="edit"]')).to_have_attribute("aria-selected", "true")
        self.assert_ready(page, "carousel", 1)
        carousel = self.link_for(page, "carousel", self.carousel["id"])
        self.assertEqual(parse_qs(carousel.get_attribute("href").split("?", 1)[1]).get("step"), ["images"])
        carousel.click()
        expect(page.locator('.production-steps [data-step="images"]')).to_have_attribute("aria-selected", "true")
        expect(self.links(page, "table")).to_have_count(0)

    def test_completed_carousel_link_opens_delivery(self):
        self.listing["carousels"][0].update(status="approved", publication={"post_id": "7654986913497009441"})
        page = self.open_page(width=390)
        self.assert_ready(page, "carousel", 1)
        link = self.links(page, "carousel").first
        self.assertEqual(parse_qs(link.get_attribute("href").split("?", 1)[1]).get("step"), ["delivery"])
        link.click()
        expect(page.locator('.production-steps [data-step="delivery"]')).to_have_attribute("aria-selected", "true")
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))

    def test_unbound_table_remains_accessible_without_duplicating_its_completed_order(self):
        pending = copy.deepcopy(self.table)
        pending.pop("carousel_id", None)
        pending["id"] = "TBL-111111111111"
        pending["finished_at"] = "2026-10-21T10:30:00Z"
        completed = {**self.table, "carousel_id": self.carousel["id"]}
        table_route = ("**/api/production/carousel-tables?script_id=*",
                       lambda route: route.fulfill(json={"tables": [completed, pending]}))
        page = self.open_page(extra_routes=[table_route])
        self.assert_ready(page, "carousel", 1)
        self.assert_ready(page, "table", 1)
        link = self.links(page, "table").first
        self.assertIn(self.script["id"], link.get_attribute("href"))
        self.assertEqual(parse_qs(link.get_attribute("href").split("?", 1)[1]).get("table"), [pending["id"]])
        expect(link).to_contain_text("3 张 · 待核对 · 2026-10-21")

    def test_video_keeps_real_carousel_outputs_without_loading_historical_tables(self):
        calls = []

        def tables(route):
            calls.append(route.request.url)
            route.fulfill(json={"tables": [{**self.table, "carousel_id": self.carousel["id"]}]})

        page = self.open_page(self.job_url(self.jobs[0]["id"]), extra_routes=[
            ("**/api/production/carousel-tables?script_id=*", tables)])
        self.assert_ready(page, "carousel", 1)
        expect(page.locator("#workflow-links [data-workflow-retry]")).to_be_enabled()
        expect(self.links(page, "table")).to_have_count(0)
        self.assertEqual(calls, [])
        self.link_for(page, "carousel", self.carousel["id"]).click()
        page.locator("#carousel-archive").wait_for(state="attached")
        self.assert_ready(page, "video", 2)
        expect(self.links(page, "table")).to_have_count(0)

    def test_table_entries_require_saved_results_or_a_running_task(self):
        entries = [
            {**self.table, "id": "TBL-111111111111", "status": "failed", "has_result": False},
            {**self.table, "id": "TBL-222222222222", "status": "completed", "has_result": False},
            {**self.table, "id": "TBL-333333333333", "status": "running", "has_result": False,
             "slide_count": 0, "finished_at": "", "started_at": "2026-10-22T10:30:00Z"},
            {**self.table, "id": "TBL-444444444444", "status": "interrupted", "has_result": True,
             "slide_count": 3, "finished_at": "2026-10-21T10:30:00Z", "carousel_missing": True},
        ]
        legacy_empty = {**entries[0], "id": "TBL-555555555555", "status": "completed"}
        legacy_empty.pop("has_result")
        entries.append(legacy_empty)
        for table in entries:
            table["carousel_id"] = ""
        page = self.open_page(extra_routes=[("**/api/production/carousel-tables?script_id=*",
                                             lambda route: route.fulfill(json={"tables": entries}))])
        self.assert_ready(page, "table", 2)
        running = self.link_for(page, "table", entries[2]["id"])
        expect(running).to_contain_text("查看逐张表进度")
        expect(running).to_contain_text("进行中 · 2026-10-22")
        saved = self.link_for(page, "table", entries[3]["id"])
        expect(saved).to_contain_text("查看原逐张表")
        expect(saved).to_contain_text("3 张 · 已中断 · 2026-10-21 · 关联制作单缺失 · 原表已保留")
        self.assertEqual(self.links(page, "table").filter(has_text="失败").count(), 0)

    def test_orphan_table_opens_the_saved_original_without_recreating_an_order(self):
        self.listing["carousels"] = []
        original = {**self.table, "carousel_id": "", "carousel_missing": True,
                    "status": "interrupted", "finished_at": "2026-10-21T10:30:00Z"}
        routes = [("**/api/production/carousel-tables?script_id=*",
                   lambda route: route.fulfill(json={"tables": [original]})),
                  (f"**/api/production/carousel-tables/{original['id']}",
                   lambda route: route.fulfill(json={"table": original}))]
        page = self.open_page(extra_routes=routes)
        self.assert_ready(page, "table", 1)
        expect(self.links(page, "table")).to_contain_text("关联制作单缺失")
        self.links(page, "table").click()
        dialog = page.locator(".production-dialog[open]")
        expect(dialog.get_by_role("heading", name="查看原逐张表", exact=True)).to_be_visible()
        expect(dialog.locator("tbody tr")).to_have_count(3)
        expect(dialog).to_contain_text("Hook 1")
        expect(dialog).to_contain_text("关联的图文制作单已不存在")
        expect(dialog.get_by_role("button", name="创建图文制作单", exact=True)).to_have_count(0)
        self.assertEqual(parse_qs(page.url.split("?", 1)[1]).get("table"), [original["id"]])

    def test_unverified_order_receipt_falls_back_to_the_original_table(self):
        original = {**self.table, "carousel_id": "CAR-999999999999", "carousel_missing": False}
        page = self.open_page(extra_routes=[("**/api/production/carousel-tables?script_id=*",
                                             lambda route: route.fulfill(json={"tables": [original]}))])
        self.assert_ready(page, "table", 1)
        href = self.links(page, "table").first.get_attribute("href")
        self.assertIn(f"/content/{self.script['id']}?table={original['id']}", href)
        self.assertNotIn(original["carousel_id"], href)

    def test_current_video_publication_opens_publish_and_retains_data_without_a_variant(self):
        publication = {"post_id": "7654986913497009441", "published_on": "2026-10-14", "source_current": True}
        self.listing["jobs"][0]["current_publication"] = publication
        for variant in self.progress["variants"]:
            variant["records"]["scripts"] = []
            variant["records"]["productions"] = []
        payload = self.app.production_job(self.jobs[0]["id"])
        payload["job"]["current_publication"] = publication
        detail_route = (f"**/api/production/jobs/{self.jobs[0]['id']}",
                        lambda route: route.fulfill(json=payload))
        page = self.open_page(extra_routes=[detail_route])
        self.assert_ready(page, "video", 2)
        link = self.link_for(page, "video", self.jobs[0]["id"])
        self.assertEqual(parse_qs(link.get_attribute("href").split("?", 1)[1]).get("step"), ["publish"])
        link.click()
        expect(page.locator('.production-steps [data-step="publish"]')).to_have_attribute("aria-selected", "true")
        self.assert_ready(page, "publication", 1)
        self.assertEqual(self.links(page, "publication").first.get_attribute("href"),
                         "#/202H/videos?video=7654986913497009441")
        self.assertEqual(self.links(page, "variant").count(), 0)

    def test_missing_source_script_retains_exact_sibling_production_links(self):
        source_route = (f"**/api/scripts/{self.script['id']}",
                        lambda route: route.fulfill(status=404, json={"error": "隔离测试：来源脚本已删除"}))
        page = self.open_page(self.job_url(self.jobs[0]["id"]), width=390, extra_routes=[source_route])
        self.assert_ready(page, "video", 1)
        self.assert_ready(page, "carousel", 1)
        self.assertEqual(self.links(page, "script").count(), 0)
        self.assertIn("来源采用稿已不可读", page.locator("#workflow-links").inner_text())
        self.assertIn(self.jobs[1]["id"], self.links(page, "video").first.get_attribute("href"))
        self.assertEqual(self.link_for(page, "video", self.jobs[0]["id"]).count(), 0)
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))

    def test_source_task_round_and_task_outputs_stay_connected(self):
        page = self.open_page()
        self.assert_ready(page, "task", 1)
        task_link = self.links(page, "task").first
        self.assertIn(self.task["task_id"], task_link.get_attribute("href"))
        self.assertEqual(parse_qs(task_link.get_attribute("href").split("?", 1)[1]).get("round"), [self.round_id])
        task_link.click()
        page.locator("#ai-rounds").wait_for()
        expect(self.links(page, "script")).to_have_count(0)
        self.assert_ready(page, "video", 2)
        self.assert_ready(page, "carousel", 1)
        adopted = page.locator("#ai-task-adopted .task-adopted-copy").first
        adopted.locator(":scope > summary").click()
        adopted.locator("[data-adopted-version-detail]").last.click()
        page.locator("#script-production-toggle").wait_for()
        self.assert_ready(page, "task", 1)

    def test_version_detail_keeps_text_first_and_a_single_secondary_source_section(self):
        page = self.open_page(width=1280, links_open=False)
        expect(page.locator("#content-adopted-script")).to_contain_text(self.script["text"])
        fold = page.locator("#workflow-links > details")
        expect(fold).not_to_have_attribute("open", "")
        self.assertTrue(page.evaluate("""() => Boolean(document.querySelector('.script-detail-grid')
            .compareDocumentPosition(document.querySelector('#workflow-links')) & Node.DOCUMENT_POSITION_FOLLOWING)"""))
        expect(page.locator(".script-detail-grid aside").get_by_role("heading", name="来源与版本", exact=True)).to_have_count(0)
        expect(page.locator('#content a[href*="/ai/"]')).to_have_count(1)
        expect(page.locator('#content a[href*="/case/"]')).to_have_count(1)
        fold.locator(":scope > summary").click()
        self.assert_ready(page, "task", 1)
        folder = Path(__file__).resolve().parents[1] / ".test-artifacts" / "script-flow"
        folder.mkdir(parents=True, exist_ok=True)
        for scheme in ("light", "dark"):
            page.emulate_media(color_scheme=scheme)
            page.screenshot(path=str(folder / f"version-detail-{scheme}.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 900})
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))
        page.screenshot(path=str(folder / "version-detail-mobile.png"), full_page=True)

    def test_same_task_round_navigation_keeps_the_hub_refreshable(self):
        task = copy.deepcopy(self.task)
        task["context"].update(direction_id="202H-S2", product="202H")
        second = copy.deepcopy(task["rounds"][0])
        second.update(round_id="AIR-111111111111", instruction="另一轮候选")
        task["rounds"].append(second)
        task_url = f"#/202H/ai/{task['task_id']}"
        task_route = (f"**/api/ai/tasks/{task['task_id']}", lambda route: route.fulfill(json={"task": task}))
        page = self.open_page(task_url, extra_routes=[task_route])
        self.assert_ready(page, "video", 2)
        page.evaluate("window.__workflowHubNode=document.querySelector('#workflow-links')")
        page.evaluate("hash=>{location.hash=hash}", task_url + "?round=" + second["round_id"])
        page.wait_for_function("round=>document.activeElement?.dataset.roundId===round", arg=second["round_id"])
        expect(page.locator(f'[data-round-id="{second["round_id"]}"] .ai-round-toggle')).to_have_attribute(
            "aria-expanded", "true")
        self.assertTrue(page.evaluate("document.querySelector('#workflow-links')===window.__workflowHubNode"))
        self.listing["jobs"][0]["title"] = "轮次跳转后读取的新制作标题"
        page.locator("#workflow-links [data-workflow-retry]").click()
        self.assert_ready(page, "video", 2)
        expect(self.link_for(page, "video", self.jobs[0]["id"])).to_contain_text("轮次跳转后读取的新制作标题")

    def test_blank_historical_direction_uses_only_the_existing_production_association(self):
        payload = self.app.production_job(self.jobs[0]["id"])
        payload["job"]["direction_id"] = ""
        payload["script"]["direction_id"] = ""
        script = copy.deepcopy(self.script)
        script["direction_id"] = ""
        script.pop("variant_link", None)
        queries = []

        def association(route):
            queries.append(parse_qs(urlparse(route.request.url).query))
            route.fulfill(json={"record": {"id": self.jobs[0]["id"], "product": "202H", "direction_id": ""},
                                "existing_link": {"kind": "production", "record_id": self.jobs[0]["id"],
                                                  "product": "202H", "direction_id": "202H-S2"}})

        routes = [(f"**/api/production/jobs/{self.jobs[0]['id']}", lambda route: route.fulfill(json=payload)),
                  (f"**/api/scripts/{self.script['id']}", lambda route: route.fulfill(json={"script": script})),
                  ("**/api/variant-links/source?*", association)]
        page = self.open_page(self.job_url(self.jobs[0]["id"]), extra_routes=routes)
        self.assert_ready(page, "variant", 1)
        self.assertIn(self.variant_key, self.links(page, "variant").first.get_attribute("href"))
        self.assertEqual(queries, [{"kind": ["production"], "id": [self.jobs[0]["id"]], "product": ["202H"]}])
        self.assert_ready(page, "carousel", 1)

    def test_late_script_progress_response_cannot_replace_the_new_production_hub(self):
        held = []

        def progress(route):
            if not held:
                held.append(route)
            else:
                route.fulfill(json=self.progress)

        page = self.open_page(progress_route=progress)
        expect(page.locator("#workflow-links [data-workflow-retry]")).to_be_disabled()
        self.assertEqual(len(held), 1)
        page.evaluate("hash=>{location.hash=hash}", self.job_url(self.jobs[0]["id"]))
        page.locator("#job-archive").wait_for(state="attached")
        page.evaluate("window.__newProductionHubNode=document.querySelector('#workflow-links')")
        # The request layer may share this in-flight GET with the new owner.
        # Complete it after the owner changed, then verify only the new owner renders.
        held[0].fulfill(json=self.progress)
        self.assert_ready(page, "video", 1)
        self.assert_ready(page, "carousel", 1)
        self.assertTrue(page.evaluate("document.querySelector('#workflow-links')===window.__newProductionHubNode"))
        self.assertEqual(self.links(page, "video").count(), 1)
        self.assertEqual(self.link_for(page, "video", self.jobs[0]["id"]).count(), 0)
        self.assertEqual(page.evaluate("location.hash"), self.job_url(self.jobs[0]["id"]))

    def test_failed_sibling_lookup_can_retry_without_a_write(self):
        calls = []
        available = False

        def listing(route):
            calls.append(route.request.url)
            route.fulfill(status=200 if available else 503,
                          json=self.listing if available else {"error": "隔离测试：关联制作单暂时不可读"})

        page = self.open_page(production_route=listing)
        expect(page.locator("#workflow-links .workflow-note, #workflow-links .workflow-alert")).to_contain_text("关联制作单暂时不可读")
        self.assertEqual(self.links(page, "video").count(), 0)
        available = True
        page.locator("#workflow-links [data-workflow-retry]").click()
        self.assert_ready(page, "video", 2)
        self.assert_ready(page, "carousel", 1)
        self.assertGreaterEqual(len(calls), 2)

    def test_failed_progress_keeps_useful_siblings_and_retries_the_exact_variant(self):
        available = False

        def progress(route):
            route.fulfill(status=200 if available else 503,
                          json=self.progress if available else {"error": "隔离测试：执行变体暂时不可读"})

        page = self.open_page(width=390, progress_route=progress)
        self.assert_ready(page, "video", 2)
        expect(page.locator("#workflow-links .workflow-note, #workflow-links .workflow-alert")).to_contain_text("执行变体暂时不可读")
        self.assertEqual(self.links(page, "variant").count(), 0)
        available = True
        page.locator("#workflow-links [data-workflow-retry]").click()
        self.assert_ready(page, "variant", 1)
        self.assertIn(self.variant_key, self.links(page, "variant").first.get_attribute("href"))
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))

    def test_case_and_publication_entries_use_existing_records(self):
        self.progress["variants"][1]["records"]["publications"] = [{
            "video_id": "7654986913497009441", "production_id": self.jobs[0]["id"],
            "published_on": "2026-10-14", "current": True}]
        page = self.open_page()
        self.assert_ready(page, "case", 1)
        self.assert_ready(page, "publication", 1)
        self.assertIn(self.identifier, self.links(page, "case").first.get_attribute("href"))
        self.assertEqual(self.links(page, "publication").first.get_attribute("href"),
                         "#/202H/videos?video=7654986913497009441")

    def test_ambiguous_or_unassigned_progress_does_not_guess_from_candidate_index(self):
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                progress = copy.deepcopy(self.progress)
                if ambiguous:
                    progress["variants"][0]["records"] = copy.deepcopy(progress["variants"][1]["records"])
                else:
                    for row in progress["variants"]:
                        row["records"]["scripts"] = []
                        row["records"]["productions"] = []
                page = self.open_page(progress_route=lambda route: route.fulfill(json=progress))
                self.assert_ready(page, "video", 2)
                self.assertEqual(self.links(page, "variant").count(), 0)
                # The attribution reason rides on the fold title instead of a banner above every step.
                expect(page.locator("#workflow-links > .workflow-alert")).to_have_count(0)
                expect(page.locator("#workflow-links .workflow-fold > summary small")).to_contain_text(
                    "需核对：对应多个变体" if ambiguous else "需核对：未核对到唯一执行变体")
                expect(page.locator("#workflow-links .workflow-fold-alert")).to_have_count(1)

    def test_task_may_link_to_multiple_variants_but_duplicate_keys_are_rejected(self):
        self.progress["variants"][0]["records"]["tasks"] = copy.deepcopy(
            self.progress["variants"][1]["records"]["tasks"])
        task_url = f"#/202H/ai/{self.task['task_id']}"
        page = self.open_page(task_url)
        self.assert_ready(page, "variant", 2)
        keys = self.links(page, "variant").evaluate_all(
            "links => links.map(link => new URLSearchParams(link.getAttribute('href').split('?')[1]).get('variant'))")
        self.assertEqual(set(keys), {"wrong-candidate-index-key", self.variant_key})
        self.progress["variants"][0]["key"] = self.variant_key
        page.locator("#workflow-links [data-workflow-retry]").click()
        self.assert_ready(page, "video", 2)
        expect(self.links(page, "script")).to_have_count(0)
        self.assertEqual(self.links(page, "variant").count(), 0)

    def test_plan_links_target_an_experiment_only_when_marked_as_an_experiment(self):
        self.progress["variants"][1]["records"]["plans"] = [
            {"id": "PLAN-ordinary", "title": "普通制作安排", "status": "working"},
            {"id": "PLAN-experiment", "title": "已登记测试卡", "status": "working", "experiment": True}]
        page = self.open_page()
        self.assert_ready(page, "plan", 2)
        ordinary = self.links(page, "plan").filter(has_text="普通制作安排").get_attribute("href")
        experiment = self.links(page, "plan").filter(has_text="已登记测试卡").get_attribute("href")
        self.assertEqual(parse_qs(ordinary.split("?", 1)[1]), {"variant": [self.variant_key]})
        self.assertEqual(parse_qs(experiment.split("?", 1)[1]),
                         {"variant": [self.variant_key], "plan": ["PLAN-experiment"]})


if __name__ == "__main__":
    unittest.main()
