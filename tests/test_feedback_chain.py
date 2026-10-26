"""Feedback provenance is explicit, frozen, scoped, and exercised in disposable runtimes."""
import copy
from pathlib import Path
import time
import unittest
from urllib.parse import urlencode
from unittest.mock import patch
from uuid import uuid4

import test_server as fixture
import test_performance as performance
from test_ai_tasks import FakeBridge
import test_plan_variant_binding as plan_fixture
from test_plan_variant_binding import variant_ref
from review_legacy_fixture import seed_legacy_followup


class FeedbackChainHTTPTests(unittest.TestCase):
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    direction = plan_fixture.PlanVariantHTTPTests.direction

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        performance.build_exports(self.root / "exports")
        self.server.app.performance.sources = [self.root / "exports"]
        self.server.app.performance.sync()
        self.server.app.ai.bridge = FakeBridge(output="A saved candidate for the next round.")
        # Drafts look for the posted video; tests never download from TikTok.
        self.server.app.draft_downloader = self.no_download
        self.video_id = performance.LINKED
        self.window = self.server.app.performance.video(self.video_id)["window_end"]

    @staticmethod
    def no_download(url, folder, video_id):
        raise RuntimeError("测试里不下载")

    def ok(self, method, path, body=None):
        status, _, result = self.request(method, path, body)
        self.assertEqual(status, 200, result)
        return result

    def chain(self, **values):
        return self.ok("GET", "/api/feedback-chain?" + urlencode({"product": "202H", "video_id": self.video_id, "window_end": self.window, **values}))

    def review(self, note="已复盘：下一輪先证明夹持稳定。", *, legacy=False, **values):
        if legacy:
            # Compatibility fixtures explicitly model records saved before retired controls.
            return seed_legacy_followup(self.server.app.review_followups,
                {"product": "202H", "video_id": self.video_id, "window_end": self.window}, note=note, **values)
        return self.ok("POST", "/api/review-followups", {"request_id": uuid4().hex,
            "revision": self.ok("GET", "/api/review-followups")["revision"], "product": "202H",
            "video_id": self.video_id, "window_end": self.window, "state": "reviewed", "note": note, **values})["entry"]

    def plan_body(self, direction, source=None):
        return {"request_id": uuid4().hex, "revision": self.server.app.creation.public()["revision"],
                "plan": {"product": "202H", "direction_id": direction["direction_id"], "action": "execution",
                         "note": "下一轮先演示装夹", "variant_ref": variant_ref(direction),
                         **({"source_review": source} if source else {})}}

    def task(self, **values):
        body = {"request_id": uuid4().hex, "product": "202H", "action": "retrospective",
                "instruction": "检查观察窗和下一轮可验证的改动。", "skill_selection": "none", **values}
        task = self.ok("POST", "/api/ai/tasks", body)["task"]
        for _ in range(100):
            task = self.ok("GET", "/api/ai/tasks/" + task["task_id"])["task"]
            if task["status"] not in {"queued", "running"}:
                break
            time.sleep(.02)
        self.assertEqual(task["status"], "completed", task)
        return task, body

    def test_read_only_graph_ignores_direction_only_plan_and_rejects_cross_product(self):
        direction = self.direction()
        self.ok("POST", "/api/plans", self.plan_body(direction))
        before = self.server.app.creation.path.read_bytes()
        data = self.chain()
        self.assertIsNone(data["review"])
        self.assertIsNone(data["source_review"])
        self.assertEqual(data["plans"], [])
        self.assertEqual(data["retrospectives"], [])
        self.assertEqual(self.server.app.creation.path.read_bytes(), before)
        self.assertEqual(self.server.app.ai.list_tasks(), [])
        self.assertEqual(self.request("GET", "/api/feedback-chain?" + urlencode({"product": "129N", "video_id": self.video_id}))[0], 404)

    def test_plan_source_is_validated_immutable_and_survives_conclusion_edit_and_replay(self):
        direction = self.direction()
        self.review()
        source = self.chain()["source_review"]
        for field, value in (("note", "伪造结论"), ("product", "129N"), ("window_end", "2026-09-20")):
            body = self.plan_body(direction, {**source, field: value})
            self.assertEqual(self.request("POST", "/api/plans", body)[0], 409)
        body = self.plan_body(direction, source)
        plan = self.ok("POST", "/api/plans", body)["plan"]
        self.review("后续修正判断；原计划保留当时结论。")
        self.assertEqual(self.ok("POST", "/api/plans", body)["plan"], plan)
        updated = self.ok("POST", "/api/plans", {"request_id": uuid4().hex,
            "revision": self.server.app.creation.public()["revision"], "plan": {"id": plan["id"], "status": "working"}})["plan"]
        self.assertEqual(updated["source_review"], source)
        self.assertFalse(self.chain()["plans"][0]["review_current"])
        for replacement in (None, {}, {**source, "note": "改写旧来源"}):
            self.assertEqual(self.request("POST", "/api/plans", {"request_id": uuid4().hex,
                "revision": self.server.app.creation.public()["revision"], "plan": {"id": plan["id"], "source_review": replacement}})[0], 409)

    def test_retrospective_freezes_exact_video_window_and_source_and_replays_after_review_change(self):
        self.review()
        source = self.chain()["source_review"]
        task, body = self.task(source_review=source)
        raw = self.server.app.ai._read(task["task_id"])
        context = raw["snapshot"]["creation_context"]
        self.assertEqual(context["source_review"], source)
        self.assertEqual(context["review_video"]["video_id"], self.video_id)
        self.assertEqual(context["sales_data"]["video"]["video_id"], self.video_id)
        self.assertEqual(task["context"]["source_review"], source)
        self.review("结论更新后不得改写旧任务。")
        replay = self.ok("POST", "/api/ai/tasks", body)["task"]
        self.assertEqual(replay["task_id"], task["task_id"])
        data = self.chain()
        self.assertEqual(len(data["retrospectives"]), 1)
        self.assertTrue(data["retrospectives"][0]["explicit_review"])
        self.assertEqual(data["retrospectives"][0]["source_review"], source)
        self.assertEqual(self.request("POST", "/api/ai/tasks", {**body, "request_id": uuid4().hex})[0], 409)

    def test_next_plan_task_script_and_variant_form_exact_chain(self):
        direction = self.direction()
        self.review()
        source = self.chain()["source_review"]
        plan = self.ok("POST", "/api/plans", self.plan_body(direction, source))["plan"]
        task, _ = self.task(action="variants", direction_id=direction["direction_id"], plan_id=plan["id"],
                            variants=[{"index": 0, "progression": direction["variants"][0].get("progression")}])
        source_info = self.server.app.ai.candidate(task["task_id"], task["rounds"][-1]["round_id"], task["revision"])
        script = self.server.app.creation.mutate("script", {"request_id": uuid4().hex,
            "revision": self.server.app.creation.public()["revision"], "script": {"product": "202H",
            "direction_id": direction["direction_id"], "title": "下一轮采用脚本", "text": "A saved candidate.", "confirmed": True}}, source_info)["script"]
        self.server.app._link_script_plan(script)
        data = self.chain()
        self.assertEqual([row["task_id"] for row in data["next_tasks"]], [task["task_id"]])
        self.assertEqual([row["id"] for row in data["next_scripts"]], [script["id"]])
        self.assertEqual([row["direction_id"] for row in data["variants"]], [direction["direction_id"]])
        self.assertEqual(self.server.app.creation.get_script(script["id"])["source"]["snapshot"]["creation_context"]["source_review"], source)

    def test_legacy_retrospective_requires_frozen_video_and_unknown_window_stays_explicit(self):
        task, _ = self.task(case_id=self.identifier, revision=self.server.app.detail(self.identifier)["revision"])
        raw = self.server.app.ai._read(task["task_id"])
        raw["snapshot"]["sales_data"] = {"status": "unavailable"}
        with self.server.app.lock:
            self.server.app.ai._write(raw)
        data = self.chain()
        self.assertEqual([row["task_id"] for row in data["retrospectives"]], [task["task_id"]])
        self.assertFalse(data["retrospectives"][0]["window_known"])
        self.assertFalse(data["retrospectives"][0]["explicit_review"])
        other = self.chain(video_id="7600000000000000002")
        self.assertEqual(other["retrospectives"], [])

    def test_imported_foreign_plan_is_excluded_even_when_review_ref_names_this_product(self):
        direction = self.direction()
        self.review()
        source = self.chain()["source_review"]
        self.ok("POST", "/api/plans", self.plan_body(direction, source))
        data = self.server.app.creation.read()
        data["plans"][0]["product"] = "129N"
        self.server.app.creation.write(data)
        self.assertEqual(self.chain()["plans"], [])
        self.assertEqual(self.chain()["variants"], [])

    def test_corrected_window_rejects_new_retro_but_historical_plan_retains_unavailable_window(self):
        direction = self.direction()
        self.review()
        source = self.chain()["source_review"]
        plan = self.ok("POST", "/api/plans", self.plan_body(direction, source))["plan"]
        changed = {**self.server.app.performance.video(self.video_id), "window_end": "2026-09-20"}
        with patch.object(self.server.app.performance, "video", return_value=changed):
            body = {"request_id": uuid4().hex, "product": "202H", "action": "retrospective",
                    "instruction": "不能混用观察窗。", "source_review": source, "skill_selection": "none"}
            status, _, result = self.request("POST", "/api/ai/tasks", body)
            self.assertEqual(status,409,result)
            self.assertIn("观察窗已更新",result["error"])
            task, _ = self.task(action="variants",direction_id=direction["direction_id"],plan_id=plan["id"],
                                case_id=self.identifier,revision=self.server.app.detail(self.identifier)["revision"])
        raw = self.server.app.ai._read(task["task_id"])
        context = raw["snapshot"]["creation_context"]
        self.assertEqual(context["source_review"],source)
        self.assertEqual(context["review_video"]["window_end"],self.window)
        self.assertEqual(context["review_video"]["current_window_end"],"2026-09-20")
        self.assertNotIn("gmv",context["review_video"])
        self.assertEqual(raw["snapshot"]["sales_data"]["status"],"unavailable")
        self.assertIn("历史窗口表现暂不可用",raw["snapshot"]["sales_data"]["evidence"]["context"])

    def test_chain_shows_figures_and_product_comparison_before_any_conclusion(self):
        data = self.chain()
        video = self.server.app.performance.video(self.video_id)
        summary = data["summary"]
        self.assertEqual(summary["metrics"]["window_views"], video["window_views"])
        self.assertEqual(summary["metrics"]["orders"], video["orders"])
        self.assertEqual(summary["status_label"], video["status_label"])
        if summary["product"]:
            self.assertGreaterEqual(summary["product"]["count"], 1)
        self.assertIsNone(data["draft"])

    def test_direction_set_on_the_video_itself_is_its_direction_for_peers(self):
        app = self.server.app
        other = next(row for row in app.performance.videos() if row["video_id"] != self.video_id
                     and app.directions.find_link(app.directions.read(), "202H", row["video_id"]) is None
                     and str(row.get("product") or "").endswith("202H"))
        direction = self.direction()
        catalog = self.ok("GET", "/api/direction-catalog?product=202H")
        self.assertNotIn(other["video_id"], app._video_directions("202H"))
        self.ok("POST", "/api/video-directions", {"request_id": uuid4().hex, "revision": catalog["revision"], "product": "202H",
                                                  "video_id": other["video_id"], "direction_id": direction["direction_id"]})
        self.assertEqual(direction["direction_id"], app._video_directions("202H")[other["video_id"]])
        if other.get("window_end"):
            funnel = self.ok("GET", "/api/feedback-chain?" + urlencode({"product": "202H", "video_id": other["video_id"]}))["funnel"]
            self.assertEqual(direction["direction_id"], funnel["direction_id"])

    def test_ai_drafts_a_conclusion_without_one_written_and_chain_reads_its_action(self):
        self.server.app.ai.bridge = FakeBridge(output="同方向里偏弱，开头没给出装夹结果。\n\n**结论：** 开头太慢，装夹证明来得晚。\n建议动作：换执行")
        window = {"product": "202H", "video_id": self.video_id, "window_end": self.window}
        task, _ = self.task(review_video=window)
        context = self.server.app.ai._read(task["task_id"])["snapshot"]["creation_context"]
        self.assertTrue(context["review_draft"])
        self.assertNotIn("source_review", context)
        self.assertEqual(context["review_video"]["video_id"], self.video_id)
        self.assertIn("metrics", context["review_summary"])
        self.assertIn("建议动作", self.server.app.ai.bridge.calls[0]["prompt"])
        data = self.chain()
        self.assertEqual(data["draft"]["task_id"], task["task_id"])
        self.assertEqual(data["draft"]["draft"], {"conclusion": "开头太慢，装夹证明来得晚。", "action": "execution", "card": None})
        self.assertIsNone(data["review"])
        for bad in ({**window, "window_end": "2026-09-20"}, {**window, "video_id": "x"}):
            self.assertEqual(self.request("POST", "/api/ai/tasks", {"request_id": uuid4().hex, "product": "202H", "action": "retrospective",
                "instruction": "起草", "review_video": bad, "skill_selection": "none"})[0] in {400, 409}, True)
        self.review()
        status, _, result = self.request("POST", "/api/ai/tasks", {"request_id": uuid4().hex, "product": "202H", "action": "retrospective",
            "instruction": "起草", "review_video": window, "source_review": self.chain()["source_review"], "skill_selection": "none"})
        self.assertEqual(status, 400, result)

    def test_draft_reviews_the_posted_video_against_its_figures(self):
        import workbench_prepublish
        from test_prepublish_review import fake_prepare
        seen = []

        class ImageBridge(FakeBridge):
            claude = type("Claude", (), {"status": staticmethod(lambda: {"available": True})})()

            def run(self, prompt, **options):
                seen.append(options.get("images"))
                return super().run(prompt, **options)

        def download(url, folder, video_id):
            seen.append(url)
            path = folder / f"{video_id}.mp4"
            path.write_bytes(b"video")
            return path

        self.server.app.ai.bridge = ImageBridge(output="对抗审查略。\n结论：开头太慢。\n建议动作：换执行")
        self.server.app.draft_downloader = download
        window = {"product": "202H", "video_id": self.video_id, "window_end": self.window}
        # The fixture case carries its own video; unlink it so the draft has to download the post.
        with patch.object(workbench_prepublish, "prepare", fake_prepare), patch.object(self.server.app, "video_case_links", return_value={}):
            task, _ = self.task(review_video=window)
            again, _ = self.task(review_video=window)
        self.assertIn(f"/video/{self.video_id}", seen[0])
        self.assertEqual(["opening.jpg", "shots1.jpg"], [Path(path).name for _, path in seen[1]])
        from prepublish_review import FRAMEWORK
        call = self.server.app.ai.bridge.calls[0]
        self.assertNotIn(FRAMEWORK, call["prompt"])
        self.assertIn("## 一句话：这条为什么出单", call["prompt"])
        self.assertIn("ctor_pct_selling", call["prompt"])
        self.assertIn("finish_rate_pct", call["prompt"])
        # Its own brief: none of the generic retrospective rules, no skills, and a folder with only the sheets.
        self.assertNotIn("direction_id 是同产品的长期分组编号", call["prompt"])
        self.assertEqual([], call["skills"])
        self.assertEqual("claude-opus", call["model"])
        self.assertTrue(call["cwd"].endswith(task["task_id"]))
        record = self.server.app.ai._read(task["task_id"])["rounds"][-1]["adversarial"]
        self.assertEqual(("attached", "从 TikTok 下载"), (record["status"], record["source"]))
        # The second draft reuses the downloaded copy instead of downloading again.
        self.assertEqual("之前起草时下载的原视频", self.server.app.ai._read(again["task_id"])["rounds"][-1]["adversarial"]["source"])
        self.assertEqual(1, sum(isinstance(item, str) for item in seen))

    def test_draft_without_footage_says_so(self):
        with patch.object(self.server.app, "video_case_links", return_value={}):
            task, _ = self.task(review_video={"product": "202H", "video_id": self.video_id, "window_end": self.window})
        record = self.server.app.ai._read(task["task_id"])["rounds"][-1]["adversarial"]
        self.assertEqual(("unavailable", "测试里不下载"), (record["status"], record["reason"]))
        self.assertIn("没看到成片，判断不了", self.server.app.ai.bridge.calls[0]["prompt"])

    def test_active_decision_is_recorded_and_retired_actions_are_rejected(self):
        entry = self.review(note="本条已结束。", decision="stop")
        self.assertEqual((entry["state"], entry["decision"]), ("reviewed", "stop"))
        self.assertEqual(self.review(note="方向有效。", decision="scale")["decision"], "scale")
        for body in ({"note": "", "decision": "scale"}, {"note": "x", "decision": "maybe"},
                     {"note": "x", "decision": "none"}, {"note": "x", "decision": "observe", "state": "observing"},
                     {"note": "x", "outcome": "negative"}, {"note": "x", "evidence": "preliminary"}):
            status, _, _ = self.request("POST", "/api/review-followups", {"request_id": uuid4().hex,
                "revision": self.ok("GET", "/api/review-followups")["revision"], "product": "202H",
                "video_id": self.video_id, "window_end": self.window, "state": "reviewed", **body})
            self.assertEqual(status, 400, body)


if __name__ == "__main__":
    unittest.main()
