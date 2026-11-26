import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import away_mode
import test_server as fixture
from away_mode import AwayError, AwayMode


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, minutes):
        self.now += timedelta(minutes=minutes)


class Fail(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Scraper:
    def __init__(self):
        self.job, self.started = None, 0

    def overview(self, product):
        return {"token": True, "recommended_start": "2026-11-10", "busy": bool(self.job and self.job["status"] == "running"),
                "job": self.job, "platforms": [{"platform": "tiktok_video", "label": "TikTok 视频", "targets": ["https://x/1"]},
                                               {"platform": "reddit", "label": "Reddit", "targets": []}]}

    def start(self, body):
        self.started += 1
        self.body = body
        self.job = {"id": f"J{self.started}", "status": "running", "platforms": {"tiktok_video": {"status": "running"}}}
        return self.overview(body["product"])

    def finish(self, added=12):
        self.job.update(status="done", added=added)
        self.job["platforms"]["tiktok_video"].update(status="done", cost_usd=0.4)


class Worker:
    """Stands in for the translation and tag workers: busy until finish()."""
    def __init__(self, remaining=5):
        self.running, self.remaining, self.starts = False, remaining, 0
        self.run = {"status": "idle"}

    def busy(self):
        return self.running

    def status(self):
        return {"remaining": self.remaining, "run": self.run}

    def start(self, body):
        if not self.remaining:
            raise Fail(400, "nothing")
        self.starts += 1
        self.running = True
        return self.status()

    def finish(self):
        self.running = False
        self.run = {"status": "done", "done": self.remaining, "total": self.remaining, "failed": 0, "suggested": 3}
        self.remaining = 0


class Skeletons:
    def __init__(self):
        self.rows, self.calls = {}, []

    def start(self, body):
        self.calls.append(list(body["themes"]))
        for theme in body["themes"]:
            self.rows[theme] = {"status": "queued"}

    def states(self, product):
        return {"themes": self.rows}


class AI:
    def __init__(self):
        self.tasks, self.created = {}, []

    def get(self, task_id):
        return self.tasks[task_id]


class Production:
    def __init__(self):
        self.jobs = {row["id"]: row for row in [
            {"id": "PRD-1", "product": "202H", "title": "等配音的单"},
            {"id": "PRD-2", "product": "202H", "title": "配好了", "selected_take": "T1"},
            {"id": "PRD-3", "product": "202H", "title": "剪过", "selected_take": "T1", "edits": [{"status": "failed", "materials": ["迷你刀"]}],
             "timelines": [{"unique_id": "TL-3", "creator": "Lab", "materials": ["主素材"]}]},
            {"id": "PRD-5", "product": "202H", "title": "剪好了", "selected_take": "T1", "edits": [{"status": "awaiting_review"}],
             "timelines": [{"unique_id": "TL-5"}]},
            {"id": "PRD-4", "product": "202H", "title": "归档", "archived": True}]}

    def records(self):
        return {"jobs": list(self.jobs.values()), "settings": {"creators": ["Zack", "Lab"],
                "products": {"202H": {"material_timelines": ["迷你刀"]}}}}

    @staticmethod
    def material_combos(data, product):
        return [{"materials": ["组合素材"]}]

    def job(self, job_id):
        return self.jobs[job_id]


class Transfer:
    def __init__(self):
        self.gate = threading.Event()
        self.calls = 0

    def status(self):
        return {"last_backup": {"created_at": "2026-11-17T10:00:00"}}

    def backup(self):
        self.calls += 1
        self.gate.wait(5)
        return {"backup": {"path": "F:/备份/x.zip", "bytes": 10}}


def make_app():
    app = SimpleNamespace()
    app.directions = SimpleNamespace(read=lambda: {"directions": [
        {"direction_id": "202H-S1", "product": "202H", "status": "testing", "main_difference": "S1 定角不用猜"},
        {"direction_id": "202H-S2", "product": "202H", "status": "testing", "main_difference": "S2 一套磨到镜面"},
        {"direction_id": "202H-S9", "product": "202H", "status": "paused"},
        {"direction_id": "198H-S1", "product": "198H", "status": "testing"}]})
    app.review_scraper = Scraper()
    app.review_translations = Worker(remaining=5)
    app.review_tags = Worker(remaining=8)
    app.opportunity_skeletons = Skeletons()
    app.ai = AI()
    app.ai.list_tasks = lambda product=None: [
        {"action": "variants", "created_at": "2026-11-16T08:00:00+00:00", "instruction": "产品：202H。围绕用户评论主题「镜面」写"},
        {"action": "variants", "created_at": "2026-11-01T08:00:00+00:00", "instruction": "产品：202H。围绕用户评论主题「价格」写"}]
    app.creation = SimpleNamespace(public=lambda product, include_frozen_source=True: {"plans": [
        {"id": "PLN-1", "product": "202H", "direction_id": "202H-S2", "status": "pending", "action": "scale", "note": "复盘决定：加码", "created_at": "2026-10-22T00:00:00"},
        {"id": "PLN-2", "product": "202H", "direction_id": "202H-S3", "status": "pending", "action": "stop", "note": "停测"},
        {"id": "PLN-3", "product": "202H", "direction_id": "202H-S1", "status": "done", "note": "写过了"}]})
    app.data_transfer = Transfer()
    app.idle_error = None

    def ensure_idle():
        if app.idle_error:
            raise RuntimeError(app.idle_error)
        if any(task["status"] in {"queued", "running"} for task in app.ai.tasks.values()):
            raise RuntimeError("AI busy")
    app.ensure_data_idle = ensure_idle

    def create(body):
        task_id = f"AIT-{len(app.ai.created):012d}"
        app.ai.created.append(body)
        app.ai.tasks[task_id] = {"task_id": task_id, "status": "queued"}
        return app.ai.tasks[task_id]
    app.create_ai_task = create
    app.production = Production()
    app.resolve = SimpleNamespace(closed=lambda: app.resolve_closed)
    app.resolve_closed = False
    app.builds, app.edit_starts = [], []

    def build(job_id, body):
        app.builds.append((job_id, body))
        return {"timeline": {"unique_id": "TL-new"}}
    app.build_timeline = build

    def start_edit(job_id, body):
        app.edit_starts.append((job_id, body))
        if app.edit_error:
            raise RuntimeError(app.edit_error)
        edit = {"id": f"EDT-{len(app.edit_starts)}", "status": "running", "timeline_name": "Zack-202-1"}
        app.production.jobs[job_id].setdefault("edits", []).append(edit)
        return {"edit": edit}
    app.start_edit = start_edit
    app.edit_error = None
    app.needs_you = lambda product: {"items": [{"id": 1}]}
    return app


CATALOG = [
    {"id": "topic:price", "title": "价格", "distinct_reviews": 9, "priority_reasons": ["3 种原文想买但卡住"],
     "skeleton": {"generated_at": "2026-11-18T05:00:00+00:00", "state": "complete"}, "draft_instruction": "产品：202H。围绕用户评论主题「价格」写带货口播稿。"},
    {"id": "topic:mirror", "title": "镜面", "skeleton": {"generated_at": "2026-11-18T05:00:00+00:00", "state": "complete"},
     "draft_instruction": "产品：202H。围绕用户评论主题「镜面」写带货口播稿。"},
    {"id": "topic:angle", "title": "角度", "skeleton": {"generated_at": "2026-11-18T05:00:00+00:00", "state": "partial"},
     "draft_instruction": "产品：202H。围绕用户评论主题「角度」写带货口播稿。"},
    {"id": "topic:gift", "title": "送礼", "skeleton": {"generated_at": "2026-11-18T05:00:00+00:00", "state": "complete", "stale": True}},
    {"id": "topic:case", "title": "收纳", "skeleton": {"status": "none", "state": "none"}},
]

THEMES = [{"id": "topic:mirror", "title": "镜面", "reason": "评论有变化"},
          {"id": "topic:angle", "title": "角度", "reason": "还没有骨架"}]


class AwayModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.app = make_app()
        self.away = self.make()

    def tearDown(self):
        self.app.data_transfer.gate.set()
        self.tmp.cleanup()

    def make(self):
        away = AwayMode(self.app, self.root, clock=self.clock, start_thread=False)
        away._themes = lambda product: list(THEMES)
        away._catalog = lambda product: [dict(row) for row in CATALOG]
        return away

    def item(self, kind, label=None):
        session = self.away.current()
        return next(row for row in session["items"] if row["kind"] == kind and (label is None or label in row["label"]))

    def start(self, **extra):
        body = {"product": "202H", "minutes": 90, "ai_limit": 12,
                "items": [{"kind": "scrape"}, {"kind": "translate"}, {"kind": "skeletons"},
                          {"kind": "writing", "topics": ["topic:price", "topic:angle", "topic:mirror"]}]}
        body.update(extra)
        return self.away.start(body)

    def test_plan_offers_only_safe_work_and_ticks_writing_off(self):
        plan = self.away.plan("202H")
        kinds = {row["kind"]: row for row in plan["items"]}
        self.assertEqual(list(kinds), ["scrape", "translate", "skeletons", "writing", "check"])
        self.assertTrue(kinds["scrape"]["enabled"])
        writing = kinds["writing"]
        self.assertTrue(writing["enabled"])
        # 镜面 was written two days ago; 送礼 is stale and 收纳 has no skeleton yet
        self.assertEqual([row["id"] for row in writing["topics"]], ["topic:price", "topic:angle"])
        self.assertEqual([row["default"] for row in writing["topics"]], [True, True])
        self.assertEqual([row["id"] for row in writing["plans"]], ["PLN-1"])
        self.assertEqual(len(kinds["skeletons"]["themes"]), 2)
        self.assertIn("配音", plan["never"])
        with self.assertRaises(AwayError):
            self.away.plan("all")

    def test_start_refuses_more_ai_than_the_limit_and_a_second_session(self):
        with self.assertRaises(AwayError) as caught:
            self.start(ai_limit=3)  # translate 1 + 2 themes + 2 directions = 5
        self.assertIn("超过上限", str(caught.exception))
        session = self.start()
        self.assertEqual(session["ai_cost"], 5)
        self.assertEqual([row["kind"] for row in session["items"]],
                         ["scrape", "writing", "writing", "skeletons", "translate", "check"])
        self.assertEqual(self.item("writing", "价格")["params"]["topic"], "topic:price")
        with self.assertRaises(AwayError):
            self.start()

    def test_full_run_queues_lanes_and_reports(self):
        self.start()
        self.away.tick()
        app = self.app
        # scrape on the network lane; two AI slots go to the two directions; skeletons and translate wait
        self.assertEqual(app.review_scraper.started, 1)
        self.assertEqual(len(app.ai.created), 2)
        self.assertEqual(app.ai.created[0]["action"], "variants")
        self.assertEqual(app.ai.created[0]["instruction"], CATALOG[0]["draft_instruction"])
        self.assertNotIn("direction_id", app.ai.created[0])
        self.assertFalse(app.ai.created[0]["include_basket"])
        self.assertEqual(self.item("skeletons")["status"], "pending")
        self.assertEqual(self.item("translate")["status"], "pending")
        # first writing finishes: skeletons take the slot; translate still waits for the scrape
        app.ai.tasks["AIT-000000000000"]["status"] = "completed"
        self.clock.advance(7)
        self.away.tick()
        self.assertEqual(app.opportunity_skeletons.calls, [["topic:mirror", "topic:angle"]])
        self.assertEqual(self.item("translate")["status"], "pending")
        # scrape done; its sync started translating on its own, so the item follows that run
        app.review_scraper.finish()
        app.review_translations.running = True
        app.ai.tasks["AIT-000000000001"]["status"] = "completed"
        self.away.tick()
        self.assertEqual(self.item("scrape")["status"], "done")
        self.assertEqual(self.item("translate")["status"], "running")
        self.assertEqual(app.review_translations.starts, 0)
        app.review_translations.finish()
        for row in app.opportunity_skeletons.rows.values():
            row["status"] = "done"
        self.away.tick()
        self.assertEqual(self.item("translate")["status"], "done")
        self.assertEqual(self.item("skeletons")["status"], "done")
        # everything else finished: the check closes the session
        self.away.tick()
        self.away.tick()
        session = self.away.current()
        self.assertEqual(session["status"], "done")
        report = session["report"]
        texts = " | ".join(row["text"] for row in report["waiting"])
        self.assertIn("挑采用稿", texts)
        self.assertNotIn("等配音", texts)  # what to voice and cut is the pool's business now
        self.assertIn("1 件之前失败", texts)
        self.assertEqual(report["counts"]["failed"], 0)
        self.assertTrue(any("新增 12 条" in row["text"] for row in report["done"]))
        writing = next(row for row in report["waiting"] if "挑采用稿" in row["text"])
        self.assertEqual(writing["link"]["hash"], "#ai/AIT-000000000000")
        self.assertNotIn("refs", session["items"][0])
        self.away.seen({"id": session["id"]})
        self.assertTrue(self.away.current()["seen"])

    def test_a_failure_retries_once_then_is_skipped_without_blocking_others(self):
        self.away.start({"product": "202H", "minutes": 90, "items": [
            {"kind": "writing", "topics": ["topic:price"]}, {"kind": "translate"}]})
        self.away.tick()
        task = self.app.ai.tasks["AIT-000000000000"]
        task.update(status="failed", last_error="Claude 没连上")
        self.away.tick()
        self.assertEqual(len(self.app.ai.created), 2, "retried with a fresh task")
        self.assertNotEqual(self.app.ai.created[0]["request_id"], self.app.ai.created[1]["request_id"])
        self.app.ai.tasks["AIT-000000000001"].update(status="failed", last_error="Claude 没连上")
        self.away.tick()
        self.assertEqual(self.item("writing")["status"], "failed")
        self.assertEqual(self.item("writing")["error"], "Claude 没连上")
        self.assertEqual(self.item("translate")["status"], "running")
        self.app.review_translations.finish()
        self.away.tick()
        session = self.away.current()
        self.assertEqual(session["status"], "done")
        self.assertEqual(session["report"]["counts"]["failed"], 1)
        self.assertTrue(any("评论翻译完成" in row["text"] for row in session["report"]["done"]))

    def test_nothing_new_starts_near_return_time_and_stop_cancels_the_queue(self):
        self.away.start({"product": "202H", "minutes": 30, "ai_limit": 12, "items": [
            {"kind": "writing", "topics": ["topic:price"], "plans": ["PLN-1", "PLN-2"]}, {"kind": "skeletons"}]})
        self.away.tick()
        self.assertEqual(self.item("skeletons")["status"], "pending")
        self.clock.advance(16)  # 14 minutes left: under STOP_BEFORE
        self.app.ai.tasks["AIT-000000000000"]["status"] = "completed"
        self.away.tick()
        self.assertEqual(self.item("skeletons")["status"], "skipped")
        plan_body = self.app.ai.created[1]
        self.assertEqual((plan_body["plan_id"], plan_body["direction_id"]), ("PLN-1", "202H-S2"))
        self.assertIn("复盘决定：加码", plan_body["instruction"])
        self.assertEqual(len([row for row in self.away.current()["items"] if row["kind"] == "writing"]), 2, "the 停测 plan is not written")
        self.assertEqual(self.app.opportunity_skeletons.calls, [])
        self.assertEqual(self.away.current()["status"], "running", "the second writing task still runs")
        self.app.ai.tasks["AIT-000000000001"]["status"] = "completed"
        self.away.tick()
        self.assertEqual(self.away.current()["status"], "done")

        self.away.start({"product": "202H", "minutes": 90, "items": [{"kind": "scrape"}, {"kind": "translate"}]})
        self.away.tick()
        self.away.stop()
        self.assertEqual(self.item("translate")["status"], "cancelled")
        self.assertEqual(self.item("scrape")["status"], "running")
        self.app.review_scraper.finish()
        self.away.tick()
        session = self.away.current()
        self.assertEqual(session["status"], "done")
        self.assertTrue(session["stopped"])
        self.assertEqual(len(self.away._load()["history"]), 1)

    def test_backup_is_no_longer_run_even_if_asked(self):
        session = self.away.start({"product": "202H", "minutes": 90, "items": [{"kind": "scrape"}, {"kind": "backup"}]})
        self.assertNotIn("backup", [row["kind"] for row in session["items"]])

    def test_restart_keeps_writing_and_scrape_and_retries_in_process_work(self):
        self.away.start({"product": "202H", "minutes": 90, "items": [
            {"kind": "scrape"}, {"kind": "translate"}, {"kind": "writing", "topics": ["topic:price"]}]})
        self.away.tick()
        self.app.review_scraper.finish()
        self.app.review_translations.running = True
        self.away.tick()
        self.assertEqual(self.item("translate")["status"], "running")
        restarted = self.make()
        self.away = restarted
        self.assertEqual(self.item("translate")["status"], "pending")
        self.assertIn("重启", self.item("translate")["error"])
        self.assertEqual(self.item("writing", "价格")["status"], "running")


class AwayModeHTTPTests(unittest.TestCase):
    """The routes answer through the real server, and a bad request comes back as a readable error."""

    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def test_routes(self):
        status, _, body = self.request("GET", "/api/away")
        self.assertEqual((status, body), (200, {"current": None}))
        status, _, body = self.request("GET", "/api/away/plan?product=all")
        self.assertEqual(status, 400)
        self.assertIn("产品", body["error"])
        status, _, body = self.request("POST", "/api/away/stop", {})
        self.assertEqual(status, 409)
        status, _, body = self.request("POST", "/api/away/start", {"product": "202H", "minutes": 90, "items": []})
        self.assertEqual(status, 400)
        self.assertIn("勾", body["error"])
        status, _, _ = self.request("GET", "/away-mode.js")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
