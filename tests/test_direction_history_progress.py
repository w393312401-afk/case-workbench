"""Reviewed historical associations preserve immutable provenance and honest progress."""
import copy
import unittest

from direction_progress import build_direction_progress, variant_snapshot


def variant(number):
    return {"main_difference": "S2 一套磨到镜面", "concern": f"顾虑 {number}",
            "closing": "下方看整套", "variant_type": "execution",
            "execution_change": f"执行变化 {number}", "progression": "清单演示",
            "baseline": number == 0}


def direction():
    return {"product": "202H", "direction_id": "202H-S2", "variants": [variant(0), variant(1)]}


def script(identifier="SCR-old", **values):
    return {"id": identifier, "product": "202H", "direction_id": "", "title": "历史已采用稿",
            "text": "Original accepted words.", "status": "adopted",
            "source": {"variant_index": 0, "candidate_text": "Original candidate words.",
                       "snapshot": {"product": "202H", "creation_context": {
                           "direction": {"direction_id": "202H-S2"},
                           "writing_plan": {"variants": [variant(0), variant(1)]}}}}, **values}


def link(kind, record_id, index=1, **values):
    return {"id": f"VL-{kind}-{record_id}", "kind": kind, "record_id": record_id,
            "product": "202H", "direction_id": "202H-S2",
            "variant_ref": {"index": index, "snapshot": variant_snapshot(variant(index))},
            "evidence": {"note": "逐条核对冻结来源与正式案例", "refs": ["case:PVR-reviewed"]}, **values}


def plan(**values):
    return {"id": "PLN-history", "product": "202H", "direction_id": "202H-S2", "status": "pending",
            "experiment": {"hypothesis": "先展示结果", "change": "改开头", "metric": "ctor_pct",
                           "decision": "observe", "baseline_video_id": "111111", "test_video_id": "222222",
                           "window_start": "2026-10-01", "window_end": "2026-10-01", "decision_note": ""}, **values}


class DirectionHistoryProgressTests(unittest.TestCase):
    def build(self, **values):
        return build_direction_progress(values.pop("direction", direction()), catalog_revision="reviewed", **values)

    def test_reviewed_blank_direction_script_and_production_inherit_without_mutating_history(self):
        saved = script()
        job = {"id": "PRD-old", "product": "202H", "direction_id": "", "script_id": saved["id"],
               "source": {"variant_index": 0}, "selected_take": "take", "timelines": []}
        links = [link("script", saved["id"])]
        before = copy.deepcopy((saved, job, links))
        result = self.build(scripts=[saved], production={"jobs": [job]}, variant_links=links)
        self.assertEqual((saved, job, links), before)
        first, second = result["variants"]
        self.assertEqual(first["records"]["scripts"], [])
        self.assertEqual(first["records"]["productions"], [])
        self.assertEqual([row["id"] for row in second["records"]["scripts"]], [saved["id"]])
        self.assertEqual([row["id"] for row in second["records"]["productions"]], [job["id"]])
        self.assertEqual(second["records"]["productions"][0]["association"]["inherited_from"], saved["id"])
        self.assertEqual(second["next_action"]["id"], job["id"])

    def test_production_can_have_reviewed_owner_without_inventing_a_script(self):
        job = {"id": "PRD-standalone", "product": "202H", "direction_id": "", "script_id": "SCR-missing"}
        row = self.build(production={"jobs": [job]}, variant_links=[link("production", job["id"])])["variants"][1]
        self.assertEqual(len(row["records"]["productions"]), 1)
        self.assertEqual(row["records"]["scripts"], [])
        self.assertEqual(row["stages"]["writing"]["status"], "empty")

    def test_publication_inherits_reviewed_production_only_for_its_frozen_script(self):
        job = {"id": "PRD-standalone", "product": "202H", "direction_id": "", "script_id": "SCR-missing"}
        current = {"post_id": "222222", "published_on": "2026-10-01", "script_id": "SCR-missing"}
        old = {"post_id": "111111", "published_on": "2026-09-01", "script_id": "SCR-other"}
        result = self.build(production={"jobs": [job]}, variant_links=[link("production", job["id"])],
                            video_publications={"jobs": {job["id"]: {"publication": current, "history": [old]}}})
        self.assertEqual([row["video_id"] for row in result["variants"][1]["records"]["publications"]], ["222222"])
        self.assertEqual([row["video_id"] for row in result["unassigned"]["publications"]], ["111111"])
        self.assertEqual(result["variants"][1]["records"]["scripts"], [])

    def test_reviewed_association_never_overrides_explicit_other_product_or_direction(self):
        for changes in ({"product": "129N"}, {"direction_id": "202H-S1"}):
            with self.subTest(changes=changes):
                saved = script(**changes)
                job = {"id": "PRD-other", "script_id": saved["id"], "product": "202H", "direction_id": "", **changes}
                result = self.build(scripts=[saved], production={"jobs": [job]},
                                    variant_links=[link("script", saved["id"]), link("production", job["id"])])
                self.assertEqual(sum(row["counts"]["scripts"] for row in result["variants"]), 0)
                self.assertEqual(sum(row["counts"]["productions"] for row in result["variants"]), 0)
                self.assertEqual(result["summary"]["unassigned_count"], 0)

    def test_other_scope_links_do_not_adopt_blank_direction_history(self):
        for changes in ({"product": "129N"}, {"direction_id": "202H-S1"}):
            with self.subTest(changes=changes):
                result = self.build(scripts=[script()], variant_links=[link("script", "SCR-old", **changes)])
                self.assertEqual(result["summary"]["variants_with_scripts"], 0)
                self.assertEqual(result["summary"]["unassigned_count"], 0)

    def test_reordered_snapshot_is_resolved_by_identity_not_stored_position(self):
        entity = direction()
        entity["variants"].reverse()
        entity["variants"][0].update(baseline=True, progression="回应质疑")
        result = self.build(direction=entity, scripts=[script()], variant_links=[link("script", "SCR-old")])
        self.assertEqual([row["counts"]["scripts"] for row in result["variants"]], [1, 0])

    def test_changed_snapshot_does_not_fall_back_to_candidate_or_source_reference(self):
        saved = script(direction_id="202H-S2")
        saved["source"]["variant_ref"] = {"index": 0, "snapshot": variant_snapshot(variant(0))}
        explicit = link("script", saved["id"])
        explicit["variant_ref"]["snapshot"]["execution_change"] = "已删除的旧执行"
        result = self.build(scripts=[saved], variant_links=[explicit])
        self.assertEqual([row["counts"]["scripts"] for row in result["variants"]], [0, 0])
        self.assertEqual([row["id"] for row in result["unassigned"]["scripts"]], [saved["id"]])

    def test_duplicate_catalog_chain_cannot_receive_reviewed_source(self):
        entity = direction()
        entity["variants"].append(copy.deepcopy(entity["variants"][1]))
        result = self.build(direction=entity, scripts=[script()], variant_links=[link("script", "SCR-old")])
        self.assertEqual([row["counts"]["scripts"] for row in result["variants"]], [0, 0, 0])
        self.assertEqual(len(result["unassigned"]["scripts"]), 1)

    def test_conflicting_duplicate_associations_never_use_the_last_input(self):
        first, second = link("script", "SCR-old", index=0), link("script", "SCR-old", index=1)
        second["id"] = "VL-conflicting"
        for links in ([first, second], [second, first]):
            with self.subTest(order=[row["id"] for row in links]):
                result = self.build(scripts=[script()], variant_links=links)
                self.assertEqual([row["counts"]["scripts"] for row in result["variants"]], [0, 0])
                self.assertEqual(len(result["unassigned"]["scripts"]), 1)

    def test_cross_direction_duplicate_associations_do_not_silently_split_ownership(self):
        result = self.build(scripts=[script()], variant_links=[link("script", "SCR-old"),
            link("script", "SCR-old", direction_id="202H-S1")])
        self.assertEqual([row["counts"]["scripts"] for row in result["variants"]], [0, 0])
        self.assertEqual(len(result["unassigned"]["scripts"]), 1)

    def test_video_association_exposes_next_test_without_inventing_date_or_production(self):
        explicit = link("video", "222222")
        result = self.build(videos=["222222", "222222"], variant_links=[explicit])
        row = result["variants"][1]
        self.assertEqual(len(row["records"]["videos"]), 1)
        self.assertEqual(row["records"]["publications"], [])
        self.assertEqual(row["records"]["productions"], [])
        self.assertEqual(row["stages"]["publishing"]["label"], "已关联 1 条视频")
        self.assertEqual(row["stages"]["production"]["status"], "unknown")
        self.assertEqual(row["next_action"], {"kind": "test", "label": "建立测试卡", "video_id": "222222"})
        self.assertEqual(result["summary"]["variants_published"], 0)

    def test_old_plan_follows_reviewed_video_and_conflicting_script_stays_unassigned(self):
        explicit = link("video", "222222")
        saved_plan = plan()
        result = self.build(plans=[saved_plan], variant_links=[explicit])
        self.assertEqual([row["counts"]["plans"] for row in result["variants"]], [0, 1])
        self.assertEqual(result["variants"][1]["next_action"]["id"], saved_plan["id"])
        saved_plan["script_id"] = "SCR-old"
        result = self.build(scripts=[script()], plans=[saved_plan],
                            variant_links=[explicit, link("script", "SCR-old", index=0)])
        self.assertEqual([row["counts"]["plans"] for row in result["variants"]], [0, 0])
        self.assertEqual(len(result["unassigned"]["plans"]), 1)

    def test_stale_explicit_video_binding_blocks_plan_fallback_to_inferred_owner(self):
        saved = script(direction_id="202H-S2", video_id="222222")
        saved["source"]["variant_ref"] = {"index": 0, "snapshot": variant_snapshot(variant(0))}
        explicit = link("video", "222222")
        explicit["variant_ref"]["snapshot"]["execution_change"] = "已失效的旧执行"
        result = self.build(scripts=[saved], videos=["222222"], plans=[plan()], variant_links=[explicit])
        self.assertEqual(len(result["unassigned"]["videos"]), 1)
        self.assertEqual([row["counts"]["plans"] for row in result["variants"]], [0, 0])
        self.assertEqual(len(result["unassigned"]["plans"]), 1)


if __name__ == "__main__":
    unittest.main()
