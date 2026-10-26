"""Automatic membership and existing-order backfill use disposable libraries."""
import copy
import unittest
from unittest.mock import patch

import test_server as fixture
import test_tk_candidates as candidates

CARD, REFERENCE = candidates.CARD, candidates.REFERENCE


class AutomaticCandidateHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    extension = candidates.CandidateHTTPTests.extension

    def install_points(self):
        path = self.skill / "references" / "products" / "202H-selling-point-grading.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("| 编号 | 卖点 | 为什么是这一级 | 依据状态 | 状态 |\n"
                        "|---|---|---|---|---|\n"
                        "| S8 | 贵刀风险控制 | 用途 | 已核 | 在测 |\n"
                        "| A2 | 定角 | 用途 | 已核 | 在测 |\n"
                        "| B4 | 价格 | 用途 | 已核 | 在测 |\n", encoding="utf-8")

    def produce(self):
        self.install_points()
        status, _, result = self.extension("/api/public-stats/direction-produce",
                                          {"product": "202H", "reference": REFERENCE, "direction": CARD})
        self.assertEqual(status, 200, result)
        self.assertFalse(result["candidate"]["direction_error"], result)
        return result

    def assert_progress(self, candidate, script_id, job_id):
        status, _, progress = self.request("GET", "/api/direction-progress/202H-S8?product=202H")
        self.assertEqual(status, 200, progress)
        variant = progress["variants"][candidate["variant_ref"]["index"]]
        self.assertIn(script_id, [row["id"] for row in variant["records"]["scripts"]])
        self.assertIn(job_id, [row["id"] for row in variant["records"]["productions"]])
        self.assertEqual(progress["unassigned"]["scripts"], [])
        self.assertEqual(progress["unassigned"]["productions"], [])

    def test_saving_a_new_card_automatically_registers_creates_and_associates_its_order(self):
        self.install_points()
        status, _, saved = self.extension("/api/public-stats/direction-candidates",
            {"product": "202H", "reference": REFERENCE, "directions": [CARD]})
        self.assertEqual(status, 200, saved)
        card = saved["candidates"][0]
        self.assertEqual((card["status"], card["direction_id"]), ("produced", "202H-S8"))
        self.assertFalse(card["direction_error"])
        app = self.server.app
        self.assertEqual(len(app.creation.read()["scripts"]), 1)
        self.assertEqual(len(app.production.records()["jobs"]), 1)
        self.assertTrue(card["script_id"])
        self.assertTrue(card["job_id"])
        self.assert_progress(card, card["script_id"], card["job_id"])
        entity = app.directions.read()["directions"][0]
        variant = entity["variants"][card["variant_ref"]["index"]]
        self.assertEqual(variant["audience"], CARD["audience"])
        self.assertEqual(variant["concern"], "怕磨坏 → A2 定角 → B4 价格")
        self.assertEqual(variant["closing"], "送修三次的钱就回来了")
        self.assertEqual(variant["sources"][0]["direction_card"]["hook_visual"], CARD["hookVisual"])
        before = app.directions.path.read_bytes()
        self.extension("/api/public-stats/direction-candidates",
                       {"product": "202H", "reference": REFERENCE, "directions": [CARD]})
        self.assertEqual(before, app.directions.path.read_bytes())
        self.assertEqual(len(app.creation.read()["scripts"]), 1)
        self.assertEqual(len(app.production.records()["jobs"]), 1)
        status, _, reference = self.extension("/api/public-stats/reference-status", {"video_id": REFERENCE["id"]})
        self.assertEqual(status, 200, reference)
        self.assertEqual(reference["directions"][0]["direction_id"], "202H-S8")

    def test_direct_production_links_the_exact_variant_and_retries_do_not_duplicate_work(self):
        made = self.produce()
        self.assertFalse(made["reused"])
        app = self.server.app
        script = app.creation.get_script(made["script_id"])
        job = app.production.job(made["job"]["id"])
        self.assertEqual((script["direction_id"], job["direction_id"]), ("202H-S8", "202H-S8"))
        self.assertEqual(script["text"], "This is how I stopped paying for sharpening.\nGrab yours below.")
        self.assertEqual([row["zh"] for row in job["lines"]], ["我就是这样不再花钱送磨的。", "下面链接入手。"])
        self.assertEqual(job["supplement"]["hook_visual"], CARD["hookVisual"])
        self.assert_progress(made["candidate"], script["id"], job["id"])
        paths = [app.tk_candidates.path, app.directions.path, app.creation.path, app.production.path]
        before = [path.read_bytes() for path in paths]
        status, _, reused = self.request("POST", "/api/tk-candidates/produce",
                                        {"candidate_id": made["candidate"]["candidate_id"]})
        self.assertEqual((status, reused["reused"], reused["job"]["id"]), (200, True, job["id"]), reused)
        self.assertEqual(before, [path.read_bytes() for path in paths])
        self.assertEqual(app.reconcile_tk_candidates(), {"associated": 1, "errors": []})
        self.assertEqual(before, [path.read_bytes() for path in paths])

    def test_new_batch_keeps_a_missing_voiceover_pending_and_still_processes_the_next_card(self):
        self.install_points()
        silent = {**CARD, "name": "只有中文口播", "lines": [{"zh": "只有中文"}], "cta": {"zh": "现在入手"}}
        valid = {**CARD, "name": "下一条完整稿"}
        status, _, saved = self.extension("/api/public-stats/direction-candidates",
            {"product": "202H", "reference": REFERENCE, "directions": [silent, valid]})
        self.assertEqual(status, 200, saved)
        pending, done = saved["candidates"]
        self.assertEqual((pending["status"], pending["direction_id"], pending["job_id"]), ("new", "202H-S8", ""))
        self.assertIn("没有英文口播", pending["production_error"])
        self.assertFalse(pending["direction_error"])
        self.assertEqual(done["status"], "produced")
        self.assertFalse(done["production_error"])
        self.assert_progress(done, done["script_id"], done["job_id"])
        self.assertEqual(len(self.server.app.creation.read()["scripts"]), 1)

    def test_plugin_replay_does_not_reopen_an_archived_order_or_restart_ignored_and_writing_cards(self):
        self.install_points()
        app = self.server.app
        status, _, saved = self.extension("/api/public-stats/direction-candidates",
            {"product": "202H", "reference": REFERENCE, "directions": [CARD]})
        self.assertEqual(status, 200, saved)
        card = saved["candidates"][0]
        app.production.mutate("job_archive", {"request_id": "archive-tkc-test", "job_id": card["job_id"],
                                              "revision": app.production.current_revision(), "archived": True})
        ignored, writing, old_writing = [{**CARD, "name": name} for name in ("已忽略", "旧写稿中", "已采纳旧写稿")]
        prepared = app.tk_candidates.add("202H", REFERENCE, [ignored, writing, old_writing])["candidates"]
        app.tk_candidates.set_status(prepared[0]["candidate_id"], "dismissed")
        app.tk_candidates.link(prepared[1]["candidate_id"], writing_task_id="AIT-old")
        app.tk_candidates.set_status(prepared[2]["candidate_id"], "adopted", "202H-S8")
        app.tk_candidates.link(prepared[2]["candidate_id"], writing_task_id="AIT-old-adopted")
        status, _, replay = self.extension("/api/public-stats/direction-candidates",
            {"product": "202H", "reference": REFERENCE, "directions": [CARD, ignored, writing, old_writing]})
        self.assertEqual(status, 200, replay)
        self.assertEqual(replay["candidates"][0]["job_id"], card["job_id"])
        self.assertEqual([i["status"] for i in replay["candidates"][1:]], ["dismissed", "writing", "adopted"])
        self.assertTrue(all(not i["job_id"] for i in replay["candidates"][1:]))
        self.assertEqual(len(app.creation.read()["scripts"]), 1)
        self.assertEqual(len(app.production.records()["jobs"]), 1)

    def test_retrying_a_saved_direction_finishes_automatic_production_once_missing_points_are_available(self):
        status, _, saved = self.extension("/api/public-stats/direction-candidates",
            {"product": "202H", "reference": REFERENCE, "directions": [CARD]})
        self.assertEqual(status, 200, saved)
        card = saved["candidates"][0]
        self.assertTrue(card["direction_error"])
        self.assertFalse(card["job_id"])
        self.install_points()
        status, _, linked = self.request("POST", "/api/tk-candidates/associate", {"candidate_id": card["candidate_id"]})
        self.assertEqual(status, 200, linked)
        done = linked["candidate"]
        self.assertEqual(done["status"], "produced")
        self.assertFalse(done["direction_error"])
        self.assert_progress(done, done["script_id"], done["job_id"])

    def legacy_production(self):
        self.install_points()
        app = self.server.app
        item = app.tk_candidates.add("202H", REFERENCE, [CARD])["candidates"][0]
        # Exercise the former direct-production path with no membership yet.
        with patch.object(app, "_associate_tk_candidate", side_effect=app.tk_candidates.get):
            made = app.produce_candidate(item["candidate_id"])
        return made

    def test_existing_order_is_backfilled_and_survives_a_restart_without_new_versions(self):
        made = self.legacy_production()
        app = self.server.app
        old_script = app.creation.get_script(made["script_id"])
        old_job = app.production.job(made["job"]["id"])
        self.assertFalse(old_job["direction_id"])
        recovered = fixture.SERVER.Workbench(self.skill, self.runtime, allow_open=False)
        card = recovered.tk_candidates.get(made["candidate"]["candidate_id"])
        self.assertFalse(card["direction_error"])
        self.assertEqual(card["status"], "produced")
        self.assertEqual(card["job_id"], old_job["id"])
        script = recovered.creation.get_script(old_script["id"])
        job = recovered.production.job(old_job["id"])
        for key in ("text", "translation_zh", "version", "source", "family_id"):
            self.assertEqual(script[key], old_script[key])
        for key in ("lines", "takes", "timelines", "edits", "source", "text_sha256"):
            self.assertEqual(job[key], old_job[key])
        self.assertEqual((script["direction_id"], job["direction_id"]), ("202H-S8", "202H-S8"))
        self.assertEqual(len(recovered.creation.read()["scripts"]), 1)
        self.assertEqual(len(recovered.production.records()["jobs"]), 1)
        self.assert_progress(card, script["id"], job["id"])

    def test_a_real_record_conflict_is_reported_without_partially_reassigning_the_script(self):
        made = self.legacy_production()
        app = self.server.app
        data = app.production.read()
        data["jobs"][0]["direction_id"] = "202H-S2"
        app.production.write(data)
        script_before = app.creation.path.read_bytes()
        job_before = app.production.path.read_bytes()
        status, _, error = self.request("POST", "/api/tk-candidates/associate",
                                        {"candidate_id": made["candidate"]["candidate_id"]})
        self.assertEqual(status, 409, error)
        self.assertIn("制作单已有其他方向", error["error"])
        self.assertEqual(app.creation.path.read_bytes(), script_before)
        self.assertEqual(app.production.path.read_bytes(), job_before)
        self.assertIn("制作单已有其他方向", app.tk_candidates.get(made["candidate"]["candidate_id"])["direction_error"])

    def test_explicit_production_variant_association_cannot_be_overridden(self):
        made = self.legacy_production()
        app = self.server.app
        item = app.tk_candidates.get(made["candidate"]["candidate_id"])
        registered = app.directions.register_candidate(item)
        ref = copy.deepcopy(registered["variant_ref"])
        ref["snapshot"]["execution_change"] = "人工核对的另一执行"
        data = app.creation.read()
        app.creation._variant_link(data, {"link": {"kind": "production", "record_id": made["job"]["id"],
            "product": "202H", "direction_id": "202H-S8", "variant_ref": ref,
            "evidence": {"note": "原人工归属", "refs": ["manual"]}}})
        app.creation.write(data)
        before = (app.creation.path.read_bytes(), app.production.path.read_bytes())
        status, _, error = self.request("POST", "/api/tk-candidates/associate", {"candidate_id": item["candidate_id"]})
        self.assertEqual(status, 409, error)
        self.assertIn("明确关联", error["error"])
        self.assertEqual(before, (app.creation.path.read_bytes(), app.production.path.read_bytes()))

    def test_resume_after_script_save_reuses_the_same_version(self):
        self.install_points()
        app = self.server.app
        item = app.tk_candidates.add("202H", REFERENCE, [CARD])["candidates"][0]
        with patch.object(app, "create_production_job", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                app.produce_candidate(item["candidate_id"])
        original = app.tk_candidates.get(item["candidate_id"])
        self.assertTrue(original["script_id"])
        self.assertEqual(original["association_version"], 0)
        result = app.produce_candidate(item["candidate_id"])
        self.assertEqual(result["script_id"], original["script_id"])
        self.assertEqual(len(app.creation.read()["scripts"]), 1)
        self.assertEqual(len(app.production.records()["jobs"]), 1)

    def test_resume_after_job_save_recovers_the_order_and_hook_without_duplication(self):
        self.install_points()
        app = self.server.app
        item = app.tk_candidates.add("202H", REFERENCE, [CARD])["candidates"][0]
        original_link = app.tk_candidates.link
        def interrupted(identifier, **kwargs):
            if kwargs.get("job_id"):
                raise RuntimeError("interrupted")
            return original_link(identifier, **kwargs)
        with patch.object(app.tk_candidates, "link", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                app.produce_candidate(item["candidate_id"])
        original_job = app.production.records()["jobs"][0]
        data = app.production.read()
        data["jobs"][0]["supplement"] = {"hook_visual": "保留人工开头", "captions": ["Keep this caption."]}
        app.production.write(data)
        result = app.produce_candidate(item["candidate_id"])
        self.assertTrue(result["reused"])
        self.assertEqual(result["job"]["id"], original_job["id"])
        self.assertEqual(len(app.creation.read()["scripts"]), 1)
        self.assertEqual(len(app.production.records()["jobs"]), 1)
        job = app.production.job(original_job["id"])
        self.assertEqual(job["supplement"]["hook_visual"], "保留人工开头")
        self.assertEqual(job["supplement"]["captions"], ["Keep this caption."])
        self.assertEqual(job["supplement"]["hook_text"], CARD["hookText"]["en"])
        self.assert_progress(result["candidate"], result["script_id"], job["id"])

    def test_resume_after_script_receipt_before_candidate_link_does_not_create_another_version(self):
        self.install_points()
        app = self.server.app
        item = app.tk_candidates.add("202H", REFERENCE, [CARD])["candidates"][0]
        with patch.object(app.tk_candidates, "link", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                app.produce_candidate(item["candidate_id"])
        self.assertFalse(app.tk_candidates.get(item["candidate_id"]).get("script_id"))
        original = app.creation.read()["scripts"][0]
        result = app.produce_candidate(item["candidate_id"])
        self.assertEqual(result["script_id"], original["id"])
        self.assertEqual(len(app.creation.read()["scripts"]), 1)

    def test_existing_order_read_failure_or_cross_product_conflict_does_not_create_or_open_another(self):
        made = self.produce()
        app = self.server.app
        with patch.object(app.production, "job", side_effect=fixture.SERVER.VoiceError(500, "读取故障")):
            with self.assertRaises(fixture.SERVER.VoiceError) as raised:
                app.produce_candidate(made["candidate"]["candidate_id"])
            self.assertEqual(raised.exception.status, 500)
        data = app.production.read()
        data["jobs"][0]["product"] = "103N"
        app.production.write(data)
        status, _, error = self.request("POST", "/api/tk-candidates/produce",
                                        {"candidate_id": made["candidate"]["candidate_id"]})
        self.assertEqual(status, 409, error)
        self.assertEqual(len(app.creation.read()["scripts"]), 1)
        self.assertEqual(len(app.production.records()["jobs"]), 1)

    def test_real_missing_facts_do_not_block_production_and_can_be_retried_without_a_wizard(self):
        status, _, made = self.extension("/api/public-stats/direction-produce",
                                        {"product": "202H", "reference": REFERENCE, "direction": CARD})
        self.assertEqual(status, 200, made)
        self.assertIn("S8", made["candidate"]["direction_error"])
        identifier = made["candidate"]["candidate_id"]
        self.assertEqual(self.request("POST", "/api/tk-candidates/associate", {"candidate_id": identifier},
                                      authenticated=False)[0], 403)
        self.install_points()
        status, _, linked = self.request("POST", "/api/tk-candidates/associate", {"candidate_id": identifier})
        self.assertEqual(status, 200, linked)
        self.assertEqual(linked["candidate"]["direction_id"], "202H-S8")
        self.assertEqual(linked["candidate"]["job_id"], made["job"]["id"])
        self.assert_progress(linked["candidate"], made["script_id"], made["job"]["id"])
        self.request("POST", "/api/tk-candidates/status", {"candidate_id": identifier, "status": "dismissed"})
        self.request("POST", "/api/tk-candidates/status", {"candidate_id": identifier, "status": "new"})
        self.assertEqual(self.server.app.tk_candidates.get(identifier)["direction_id"], "202H-S8")


if __name__ == "__main__":
    unittest.main()
