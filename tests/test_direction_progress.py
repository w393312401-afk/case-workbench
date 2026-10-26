"""Variant progress never infers ownership from candidate indexes or manual status."""
import copy
import json
import unittest
from unittest.mock import patch

from direction_progress import build_direction_progress, variant_snapshot
from direction_store import fingerprint
from workbench_video_publish import reviewed_source
import test_direction_loading as loading


def variant(number=0):
    return {"main_difference": "S1 主打", "concern": f"A{number + 1} 顾虑", "closing": "S1 收口",
            "variant_type": "execution", "execution_change": f"改动 {number}",
            "progression": "问题-解决" if not number else "结果倒叙", "baseline": not number}


def direction():
    return {"product": "202H", "direction_id": "202H-S1", "status": "active", "variants": [variant(), variant(1)]}


def script(number=0, **values):
    return {"id": f"SCR-{number}", "product": "202H", "direction_id": "202H-S1", "title": "已存文案",
            "status": "adopted", "source": {"task_id": f"AIT-{number}", "variant_index": 7,
                "snapshot": {"product": "202H", "creation_context": {"direction": {"direction_id": "202H-S1"},
                    "writing_plan": {"variants": [variant(number)], "new_variant_count": 0}}}}, **values}


def task(*indexes, **values):
    return {"task_id": "AIT-1", "product": "202H", "action": "variants", "status": "completed",
            "context": {"direction_id": "202H-S1", "writing_plan": {
                "variants": [variant(index) for index in indexes], "new_variant_count": 0}}, **values}


def plan(**values):
    return {"id": "PLN-1", "product": "202H", "direction_id": "202H-S1", "status": "working",
            "variant_ref": {"index": 0, "snapshot": variant()}, "experiment": {
                "hypothesis": "先展示结果", "change": "改开头", "metric": "ctor_pct", "decision": "observe",
                "baseline_video_id": "123456", "test_video_id": "654321", "window_start": "2026-10-01",
                "window_end": "2026-10-01", "decision_note": ""}, **values}


class DirectionProgressTests(unittest.TestCase):
    def build(self, **kwargs):
        return build_direction_progress(kwargs.pop("direction", direction()), catalog_revision="revision", **kwargs)

    def test_single_snapshot_binds_to_catalog_chain_not_candidate_index(self):
        data = self.build(scripts=[script()])
        self.assertEqual(data["variants"][0]["records"]["scripts"][0]["id"], "SCR-0")
        self.assertEqual(data["variants"][1]["records"]["scripts"], [])
        self.assertEqual(data["variants"][0]["stages"]["writing"]["status"], "done")
        self.assertEqual(data["variants"][0]["next_action"]["kind"], "script")

    def test_multi_variant_candidate_stays_unassigned_despite_index_and_manual_state(self):
        source = script()
        source["source"]["variant_index"] = 0
        source["source"]["snapshot"]["creation_context"]["writing_plan"]["variants"].append(variant(1))
        source["status"] = "published"
        data = self.build(scripts=[source], tasks=[task(0, 1)])
        self.assertEqual([row["counts"]["scripts"] for row in data["variants"]], [0, 0])
        self.assertEqual([row["counts"]["tasks"] for row in data["variants"]], [1, 1])
        self.assertTrue(all(row["stages"]["writing"]["label"] == "写稿任务完成" for row in data["variants"]))
        self.assertTrue(all(row["next_action"]["label"] == "查看写稿结果" for row in data["variants"]))
        self.assertEqual(len(data["unassigned"]["scripts"]), 1)
        self.assertTrue(all(row["stages"]["publishing"]["status"] == "empty" for row in data["variants"]))

    def test_new_variant_requests_remain_ambiguous_for_scripts(self):
        source = script()
        source["source"]["snapshot"]["creation_context"]["writing_plan"]["new_variant_count"] = 1
        self.assertEqual(len(self.build(scripts=[source])["unassigned"]["scripts"]), 1)

    def test_reordering_and_baseline_toggle_preserve_identity_but_chain_edit_does_not(self):
        entity = direction()
        entity["variants"].reverse()
        entity["variants"][1]["baseline"] = False
        entity["variants"][1]["progression"] = "身份认同"
        data = self.build(direction=entity, scripts=[script()])
        self.assertEqual(data["variants"][1]["counts"]["scripts"], 1)
        entity["variants"][1]["execution_change"] = "新执行"
        self.assertEqual(len(self.build(direction=entity, scripts=[script()])["unassigned"]["scripts"]), 1)

    def test_duplicate_or_conflicted_catalog_snapshot_never_guesses(self):
        entity = direction()
        entity["variants"].append(copy.deepcopy(entity["variants"][0]))
        self.assertEqual(len(self.build(direction=entity, scripts=[script()])["unassigned"]["scripts"]), 1)
        entity = direction()
        entity["variants"][0]["conflict"] = True
        self.assertEqual(len(self.build(direction=entity, tasks=[task(0)])["unassigned"]["tasks"]), 1)

    def test_manual_published_and_done_are_not_observed_progress(self):
        data = self.build(scripts=[script(status="published")], plans=[plan(status="done", experiment={})])
        stages = data["variants"][0]["stages"]
        self.assertEqual((stages["production"]["status"], stages["publishing"]["status"], stages["testing"]["status"]),
                         ("empty", "empty", "empty"))
        unlinked = self.build()["variants"][0]
        self.assertEqual([stage["label"] for stage in unlinked["stages"].values()],
                         ["未关联文案", "未关联制作", "未登记发布", "未关联测试卡"])
        self.assertEqual(unlinked["next_action"]["kind"], "write")

    def test_dated_publication_does_not_invent_missing_production(self):
        data = self.build(scripts=[script(video_id="654321", published_on="2026-10-01")])
        row = data["variants"][0]
        self.assertEqual(row["stages"]["publishing"]["status"], "done")
        self.assertEqual(row["stages"]["production"]["status"], "unknown")
        self.assertEqual(row["next_action"], {"kind": "test", "label": "建立测试卡", "video_id": "654321"})

    def test_production_and_publication_follow_script_not_source_index(self):
        job = {"id": "PRD-1", "script_id": "SCR-0", "product": "202H", "direction_id": "202H-S1",
               "source": {"variant_index": 1}, "selected_take": "TAK-1", "timelines": []}
        data = self.build(scripts=[script()], production={"jobs": [job]}, video_publications={"jobs": {
            "PRD-1": {"publication": {"id": "PUB-1", "script_id": "SCR-0", "post_id": "654321", "published_on": "2026-10-01"}, "history": []}}})
        self.assertEqual(data["variants"][0]["counts"]["productions"], 1)
        self.assertEqual(data["variants"][0]["counts"]["publications"], 1)
        self.assertEqual(data["variants"][1]["counts"]["productions"], 0)
        self.assertEqual(data["variants"][0]["stages"]["production"]["status"], "working")

    def test_cross_product_and_cross_direction_records_are_excluded(self):
        foreign = script(product="129N")
        moved = script(direction_id="202H-S2")
        task_other = task(0, product="129N")
        data = self.build(scripts=[foreign, moved], tasks=[task_other])
        self.assertEqual(data["summary"]["unassigned_count"], 0)
        self.assertEqual(data["summary"]["variants_with_scripts"], 0)

    def test_frozen_source_cannot_credit_a_later_script_direction(self):
        source = script()
        source["source"]["snapshot"]["creation_context"]["direction"]["direction_id"] = "202H-S2"
        self.assertEqual(len(self.build(scripts=[source])["unassigned"]["scripts"]), 1)

    def test_testing_observes_comparison_and_decision_not_plan_done(self):
        videos = {key: {"video_id": key, "product": "202H", "account": "same", "series": [
            {"start": "2026-10-01", "end": "2026-10-01", "clicks": 100, "orders": 10}]} for key in ("123456", "654321")}
        saved = plan(status="done")
        pending = self.build(plans=[saved])["variants"][0]
        self.assertNotEqual(pending["stages"]["testing"]["status"], "done")
        ready = self.build(plans=[saved], read_video=videos.get)["variants"][0]
        self.assertEqual(ready["records"]["plans"][0]["comparison_status"], "ready")
        self.assertEqual(ready["stages"]["testing"]["label"], "可比较，待记录决定")
        self.assertEqual(ready["next_action"]["kind"], "test")
        saved["experiment"].update(decision="scale", decision_note="已核对结果")
        decided = self.build(plans=[saved], read_video=videos.get)["variants"][0]
        self.assertEqual(decided["stages"]["testing"]["status"], "done")

    def test_explicit_plan_snapshot_and_legacy_video_link_resolve_without_broad_direction_assignment(self):
        legacy = plan()
        legacy.pop("variant_ref")
        data = self.build(scripts=[script(video_id="654321", published_on="2026-10-01")], plans=[legacy])
        self.assertEqual(data["variants"][0]["counts"]["plans"], 1)
        self.assertEqual(data["variants"][1]["counts"]["plans"], 0)
        data = self.build(plans=[legacy], videos=["654321"])
        self.assertEqual((len(data["unassigned"]["plans"]), len(data["unassigned"]["videos"])), (1, 1))

    def test_current_publication_is_next_test_target_and_history_keeps_original_script_owner(self):
        job = {"id": "PRD-1", "script_id": "SCR-0", "product": "202H", "direction_id": "202H-S1"}
        data = self.build(scripts=[script(), script(1)], production={"jobs": [job]}, video_publications={"jobs": {
            "PRD-1": {"publication": {"id": "new", "script_id": "SCR-0", "post_id": "222222", "published_on": "2026-10-02"},
                "history": [{"id": "old", "script_id": "SCR-1", "post_id": "111111", "published_on": "2026-10-01"}]}}})
        self.assertEqual([row["video_id"] for row in data["variants"][0]["records"]["publications"]], ["222222"])
        self.assertEqual([row["video_id"] for row in data["variants"][1]["records"]["publications"]], ["111111"])
        self.assertEqual(data["variants"][0]["next_action"]["kind"], "production")

    def test_carousel_publication_history_stays_visible_and_current_wins(self):
        carousel = {"id": "CAR-1", "script_id": "SCR-0", "product": "202H", "direction_id": "202H-S1",
                    "archived": True, "slides": [], "publication": {"id": "new", "post_id": "222222", "published_on": "2026-10-02"},
                    "publication_history": [{"id": "old", "post_id": "111111", "published_on": "2026-10-01"}]}
        data = self.build(scripts=[script()], production={"carousels": [carousel]})
        row = data["variants"][0]
        self.assertEqual(row["counts"]["publications"], 2)
        self.assertEqual(row["next_action"]["video_id"], "222222")

    def test_corrected_same_post_publication_keeps_current_receipt(self):
        job = {"id": "PRD-1", "script_id": "SCR-0", "product": "202H", "direction_id": "202H-S1",
               "selected_take": "take", "takes": [{"id": "take"}],
               "timelines": [{"unique_id": "timeline", "take_id": "take", "saved": True}],
               "manual_completion": {"at": "2026-10-02", "take_id": "take", "timeline_uid": "timeline"}}
        current = {"id": "new", "script_id": "SCR-0", "post_id": "222222", "published_on": "2026-10-02",
                   "source_signature": fingerprint(reviewed_source(job))}
        history = {**current, "id": "old", "source_signature": "old-source", "published_on": "2026-10-01"}
        data = self.build(scripts=[script()], production={"jobs": [job]}, video_publications={"jobs": {
            "PRD-1": {"publication": current, "history": [history]}}})
        rows = data["variants"][0]["records"]["publications"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "new")
        self.assertTrue(rows[0]["current"] and rows[0]["source_current"])
        self.assertEqual(data["variants"][0]["next_action"]["kind"], "test")
        job["selected_take"] = "different-take"
        changed = self.build(scripts=[script()], production={"jobs": [job]}, video_publications={"jobs": {
            "PRD-1": {"publication": current, "history": [history]}}})
        self.assertFalse(changed["variants"][0]["records"]["publications"][0]["source_current"])
        self.assertEqual(changed["variants"][0]["next_action"]["kind"], "production")

    def test_old_completed_production_does_not_hide_new_work_or_repair(self):
        old = {"id": "PRD-old", "script_id": "SCR-0", "product": "202H", "direction_id": "202H-S1",
               "selected_take": "take", "created_at": "2026-10-01", "takes": [{"id": "take"}],
               "timelines": [{"unique_id": "timeline", "take_id": "take", "saved": True}],
               "manual_completion": {"at": "2026-10-02", "take_id": "take", "timeline_uid": "timeline"}}
        newer = {"id": "PRD-new", "script_id": "SCR-0", "product": "202H", "direction_id": "202H-S1",
                 "selected_take": "take-new", "created_at": "2026-10-03", "timelines": []}
        for failed in (False, True):
            with self.subTest(failed=failed):
                if failed:
                    newer["timelines"] = [{"unique_id": "timeline-new", "take_id": "take-new", "saved": True}]
                    newer["edits"] = [{"timeline_uid": "timeline-new", "status": "failed"}]
                row = self.build(scripts=[script()], production={"jobs": [newer, old]})["variants"][0]
                self.assertEqual(row["stages"]["production"]["status"], "attention" if failed else "working")
                self.assertEqual(row["stages"]["production"]["completed_count"], 1)
                self.assertTrue(row["stages"]["production"]["history_label"])
                self.assertEqual(row["next_action"]["id"], "PRD-new")

    def test_old_test_decision_does_not_hide_new_undecided_card(self):
        old = plan(created_at="2026-10-01")
        old["experiment"].update(decision="scale", decision_note="已记录决定")
        new = plan(id="PLN-new", created_at="2026-10-03")
        new["experiment"].update(baseline_video_id="", test_video_id="", window_start="", window_end="")
        for manual_status in ("pending", "working", "done"):
            with self.subTest(manual_status=manual_status):
                new["status"] = manual_status
                row = self.build(plans=[new, old])["variants"][0]
                self.assertEqual(row["stages"]["testing"]["status"], "working")
                self.assertEqual(row["stages"]["testing"]["completed_count"], 1)
                self.assertTrue(row["stages"]["testing"]["history_label"])
                self.assertEqual(row["next_action"], {"kind": "test", "id": "PLN-new", "label": "补齐测试信息"})
        new["status"] = "cancelled"
        row = self.build(plans=[new, old])["variants"][0]
        self.assertEqual(row["stages"]["testing"]["status"], "done")
        self.assertEqual(row["stages"]["testing"]["completed_count"], 1)
        self.assertEqual(row["next_action"]["id"], old["id"])

    def test_undecided_card_without_publication_has_its_own_next_step(self):
        saved = plan(status="done")
        saved["experiment"].update(baseline_video_id="", test_video_id="", window_start="", window_end="")
        row = self.build(plans=[saved])["variants"][0]
        self.assertEqual(row["stages"]["testing"]["status"], "working")
        self.assertEqual(row["next_action"]["kind"], "test")
        self.assertEqual(row["next_action"]["id"], "PLN-1")
        self.assertEqual(row["next_action"]["label"], "补齐测试信息")
        saved["status"] = "cancelled"
        row = self.build(plans=[saved])["variants"][0]
        self.assertEqual(row["stages"]["testing"]["status"], "empty")
        self.assertEqual(row["stages"]["testing"]["completed_count"], 0)


class DirectionProgressHTTPTests(unittest.TestCase):
    setUp = loading.DirectionLoadingTests.setUp
    stop_server = loading.DirectionLoadingTests.stop_server
    request = loading.DirectionLoadingTests.request
    setup_direction = loading.DirectionLoadingTests.setup_direction
    write_task = loading.DirectionLoadingTests.write_task

    def test_endpoint_is_read_only_scoped_and_deferred_from_first_page(self):
        self.setup_direction()
        app = self.server.app
        app.direction_catalog("202H")
        catalog = app.directions.read()
        entity = catalog["directions"][0]
        entity["variants"] = [variant(), variant(1)]
        app.directions.write(catalog)
        saved = app.creation.empty()
        saved["scripts"] = [script()]
        app.creation.write(saved)
        task_id, path = self.write_task(1)
        raw_task = json.loads(path.read_text(encoding="utf-8"))
        raw_task["action"] = "variants"
        raw_task["snapshot"]["creation_context"] = {"direction": {"direction_id": "202H-S1"},
            "writing_plan": {"variants": [variant()], "new_variant_count": 0}}
        path.write_text(json.dumps(raw_task), encoding="utf-8")
        with patch.object(app, "direction_progress", side_effect=AssertionError("Must load separately")):
            status, _, first = self.request("GET", "/api/direction-catalog/202H-S1?product=202H&view=page&include_tasks=0")
        self.assertEqual(status, 200, first)
        self.assertTrue(first["progress_available"])
        before = {str(path): path.read_bytes() for path in self.runtime.rglob("*.json")}
        with patch.object(app.creation, "read", wraps=app.creation.read) as creation, \
             patch.object(app.production, "records", wraps=app.production.records) as production, \
             patch.object(app.ai, "list_tasks", wraps=app.ai.list_tasks) as tasks:
            status, _, progress = self.request("GET", "/api/direction-progress/202H-S1?product=202H")
        self.assertEqual(status, 200, progress)
        self.assertEqual((creation.call_count, production.call_count, tasks.call_count), (1, 1, 1))
        self.assertEqual(progress["catalog_revision"], first["revision"])
        self.assertEqual(progress["variants"][0]["records"]["tasks"][0]["id"], task_id)
        self.assertEqual(progress["variants"][0]["counts"]["scripts"], 1)
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.runtime.rglob("*.json")})
        self.assertEqual(self.request("GET", "/api/direction-progress/202H-S1?product=129N")[0], 404)
        self.assertEqual(self.request("GET", "/api/direction-progress/202H-S1?product=202H&product=129N")[0], 400)


if __name__ == "__main__":
    unittest.main()
