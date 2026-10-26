"""TK card registration imports evidence without making the user recreate it."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from creation_store import PLAN_VARIANT_SNAPSHOT_KEYS
from direction_store import DirectionError, DirectionStore
from tk_candidates import CandidateStore


REFERENCE = {"id": "7550000000000000001", "author": "knifeguy", "title": "Sharpening a knife", "views": 2400000}
CARD = {"id": "202H-S1", "kind": "执行变体", "name": "别再靠手腕猜刀角", "variantOf": "让导杆带着刀身前进",
        "audience": "每次磨刀都难以守住角度的家庭厨师", "compare": "徒手磨刀石",
        "chain": ["S1 夹住刀身，由导杆引导研磨", "B1 上色检查", "会不会夹不住或推磨不好操作 → A2 连续推磨",
                  "收口：让这套成为家里日常磨刀的入口"],
        "hookVisual": "刀身上的记号笔墨迹", "hookText": {"en": "Stop guessing.", "zh": "别再猜。"},
        "lines": [{"en": "The guide holds the angle.", "zh": "让导杆守住角度。"}],
        "cta": {"en": "Find it below.", "zh": "点下面看看。"}, "payback": "回到日常磨刀入口", "check": ["核对刀宽"]}


class CandidateCatalogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="tk-candidate-catalog-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = DirectionStore(self.root / "runtime", self.root / "skill")
        self.candidates = CandidateStore(self.root / "runtime")
        folder = self.root / "skill" / "references" / "products"
        folder.mkdir(parents=True)
        for product in ("202H", "999Z"):
            (folder / f"{product}-selling-point-grading.md").write_text(
                "| 编号 | 卖点 | 分级 | 依据 |\n|---|---|---|---|\n"
                "| S1 | 定角研磨 | 主打 | 产品结构 |\n| S2 | 双面研磨 | 主打 | 产品结构 |\n"
                "| A2 | 连续推磨 | 接顾虑 | 实拍 |\n| B1 | 上色检查 | 证据 | 实拍 |\n", encoding="utf-8")

    def candidate(self, card=None, reference=None, product="202H"):
        result = self.candidates.add(product, reference or REFERENCE, [copy.deepcopy(card or CARD)], ask="照着演示镜头改")
        return self.candidates.get(result["candidates"][0]["candidate_id"])

    def seed_direction(self):
        self.store.sync([])
        data = self.store.read()
        point = self.store.find_code(data, "202H", "S1")
        result = self.store._save_direction(data, {"product": "202H", "selling_point_id": point["id"],
            "direction_id": "202H-stable", "main_difference": "S1 人工维护的主打", "concern": "人工默认顾虑",
            "closing": "人工默认收口", "audience": "原目标人群", "status": "paused", "notes": "人工备注",
            "progression": "问题-解决"})
        self.store.write(data)
        return result["direction"]

    def test_import_keeps_fields_source_card_and_frozen_variant_reference(self):
        item = self.candidate()
        result = self.store.register_candidate(item)
        entity = result["direction"]
        self.assertEqual((entity["direction_id"], result["existing"], result["variant_ref"]["index"]), ("202H-S1", False, 0))
        self.assertEqual((entity["main_difference"], entity["concern"], entity["closing"], entity["audience"]),
                         (CARD["chain"][0], "B1 上色检查 → 会不会夹不住或推磨不好操作 → A2 连续推磨",
                          "让这套成为家里日常磨刀的入口", CARD["audience"]))
        variant = entity["variants"][0]
        self.assertEqual(variant["execution_change"], CARD["variantOf"])
        self.assertEqual(variant["audience"], CARD["audience"])
        self.assertNotIn("progression", variant)
        self.assertNotIn("baseline", variant)
        source = variant["sources"][0]
        self.assertEqual((source["direction_card"], source["reference"], source["ask"], source["saved_at"]),
                         (item["direction"], item["reference"], item["ask"], item["saved_at"]))
        self.assertEqual(result["variant_ref"]["snapshot"],
                         {key: variant[key] for key in PLAN_VARIANT_SNAPSHOT_KEYS if key in variant})

    def test_retry_is_idempotent_and_does_not_rewrite_catalog(self):
        item = self.candidate()
        first = self.store.register_candidate(item)
        raw = self.store.path.read_bytes()
        again = self.store.register_candidate(item)
        self.assertEqual(first["variant_ref"], again["variant_ref"])
        self.assertTrue(again["existing"])
        self.assertEqual(self.store.path.read_bytes(), raw)
        self.assertEqual(len(again["direction"]["variants"][0]["sources"]), 1)

    def test_same_chain_and_change_from_another_reference_adds_only_source(self):
        self.store.register_candidate(self.candidate())
        item = self.candidate(reference={**REFERENCE, "id": "7550000000000000002"})
        result = self.store.register_candidate(item)
        self.assertEqual(len(result["direction"]["variants"]), 1)
        self.assertEqual(len(result["direction"]["variants"][0]["sources"]), 2)

    def test_same_lead_and_chain_with_different_execution_keeps_both(self):
        self.store.register_candidate(self.candidate())
        card = {**CARD, "variantOf": "先给结果，再演示导杆", "name": "先看墨迹"}
        result = self.store.register_candidate(self.candidate(card))
        self.assertTrue(result["existing"])
        self.assertEqual(result["variant_ref"]["index"], 1)
        self.assertEqual(len(result["direction"]["variants"]), 2)

    def test_existing_definition_and_baseline_progression_stay_unchanged(self):
        original = self.seed_direction()
        data = self.store.read()
        item = self.candidate()
        variant = data["directions"][0]["variants"][0]
        variant.update(concern=item["suggestion"]["concern"], closing=item["suggestion"]["closing"],
                       execution_change=CARD["variantOf"])
        self.store.write(data)
        result = self.store.register_candidate(item)
        for key in ("direction_id", "main_difference", "concern", "closing", "audience", "status", "notes"):
            self.assertEqual(result["direction"][key], original[key])
        self.assertEqual(len(result["direction"]["variants"]), 1)
        self.assertEqual(result["variant_ref"]["snapshot"]["baseline"], True)
        self.assertEqual(result["variant_ref"]["snapshot"]["progression"], "问题-解决")

    def test_renumbered_point_reuses_stable_direction_and_original_snapshot(self):
        item = self.candidate()
        first = self.store.register_candidate(item)
        data = self.store.read()
        point = self.store.find_code(data, "202H", "S1")
        self.store._save_point(data, {**point, "code": "S7", "tier": "S"})
        self.store.write(data)
        result = self.store.register_candidate(item)
        self.assertEqual(result["direction"]["direction_id"], "202H-S1")
        self.assertEqual(result["variant_ref"], first["variant_ref"])
        self.assertEqual(len(result["direction"]["variants"]), 1)

    def test_former_code_id_with_current_code_chain_does_not_add_lead_to_concerns(self):
        item = self.candidate()
        original = self.store.register_candidate(item)
        data = self.store.read()
        point = self.store.find_code(data, "202H", "S1")
        self.store._save_point(data, {**point, "code": "S7", "tier": "S"})
        self.store.write(data)
        changed = copy.deepcopy(item)
        changed["candidate_id"] += "-new"
        changed["direction"]["chain"][0] = CARD["chain"][0].replace("S1", "S7")
        result = self.store.register_candidate(changed)
        self.assertEqual(result["variant_ref"], original["variant_ref"])
        self.assertEqual(len(result["direction"]["variants"]), 1)

    def test_explicit_association_with_another_lead_is_not_silently_changed(self):
        self.store.register_candidate(self.candidate())
        card = {**CARD, "id": "202H-S2", "chain": ["S2 双面研磨", "B1 上色检查", "收口：看看套装"]}
        item = self.candidate(card)
        item["direction_id"] = "202H-S1"
        with self.assertRaisesRegex(DirectionError, "另一主打卖点"):
            self.store.register_candidate(item)

    def test_explicit_association_missing_from_product_is_not_silently_changed(self):
        item = self.candidate()
        item["direction_id"] = "999Z-S1"
        with self.assertRaisesRegex(DirectionError, "原来关联的方向"):
            self.store.register_candidate(item)

    def test_product_and_card_number_conflict_is_rejected(self):
        item = self.candidate({**CARD, "id": "999Z-S1"})
        with self.assertRaisesRegex(DirectionError, "另一产品"):
            self.store.register_candidate(item)
        item = self.candidate()
        item["product_key"] = "999Z"
        with self.assertRaisesRegex(DirectionError, "产品编号不一致"):
            self.store.register_candidate(item)

    def test_same_lead_code_in_another_product_keeps_separate_direction(self):
        self.store.register_candidate(self.candidate())
        other = self.candidate({**CARD, "id": "999Z-S1"}, product="999Z")
        result = self.store.register_candidate(other)
        self.assertEqual((result["direction"]["product"], result["direction"]["direction_id"], result["existing"]),
                         ("999Z", "999Z-S1", False))

    def test_ambiguous_former_code_is_rejected(self):
        self.store.sync([])
        data = self.store.read()
        self.store.find_code(data, "202H", "S2")["former_codes"] = ["S1"]
        self.store.write(data)
        with self.assertRaisesRegex(DirectionError, "编号冲突"):
            self.store.register_candidate(self.candidate())

    def test_legacy_direction_without_point_link_keeps_stable_id(self):
        original = self.seed_direction()
        data = self.store.read()
        data["directions"][0]["selling_point_id"] = None
        self.store.write(data)
        result = self.store.register_candidate(self.candidate())
        self.assertEqual(result["direction"]["direction_id"], original["direction_id"])
        self.assertEqual(result["direction"]["main_difference"], original["main_difference"])
        self.assertIsNotNone(result["direction"]["selling_point_id"])

    def test_card_number_and_chain_lead_conflict_is_rejected(self):
        item = self.candidate({**CARD, "chain": ["S2 双面研磨", "B1 上色检查", "收口：看看套装"]})
        with self.assertRaisesRegex(DirectionError, "主打卖点冲突"):
            self.store.register_candidate(item)

    def test_occupied_direction_number_cannot_be_reused_for_another_point(self):
        self.store.sync([])
        data = self.store.read()
        point = self.store.find_code(data, "202H", "S2")
        data["directions"].append({"product": "202H", "direction_id": "202H-S1", "selling_point_id": point["id"],
            "main_difference": "S2 双面", "concern": "原顾虑", "closing": "原收口", "variants": [], "source": []})
        self.store.write(data)
        with self.assertRaisesRegex(DirectionError, "方向编号 202H-S1 已用于另一主打"):
            self.store.register_candidate(self.candidate())

    def test_missing_lead_point_and_incomplete_chain_have_specific_errors(self):
        cases = [({**CARD, "id": "", "chain": ["没有主打编号", "收口：看看套装"]}, "缺少主打卖点编号"),
                 ({**CARD, "id": "202H-S8", "chain": ["S8 未有卖点", "顾虑", "收口：看看套装"]}, "卖点表里没有 S8"),
                 ({**CARD, "chain": ["S1 定角", "收口：看看套装"]}, "缺少有效的顾虑链"),
                 ({**CARD, "chain": ["S1 定角", "顾虑"], "cta": {}}, "缺少有效的收口"),
                 ({**CARD, "chain": ["S1 定角", "A99 未知编号", "收口：看看套装"]}, "卖点表里没有 A99")]
        for card, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(DirectionError, error):
                self.store.register_candidate(self.candidate({**card, "name": CARD["name"] + error}))

    def test_explicit_merge_follows_destination_and_keeps_original_card(self):
        self.store.sync([])
        data = self.store.read()
        point = self.store.find_code(data, "202H", "S1")
        point.update(status="merged", merged_into="S2")
        self.store.write(data)
        item = self.candidate()
        result = self.store.register_candidate(item)
        self.assertEqual(result["direction"]["direction_id"], "202H-S2")
        self.assertEqual(result["direction"]["main_difference"], "S2 双面研磨")
        self.assertEqual(result["direction"]["source"][0]["direction_card"], item["direction"])

    def test_unclear_merge_is_reported_without_catalog_write(self):
        self.store.sync([])
        for target in ("", "S99", "S1"):
            data = self.store.read()
            self.store.find_code(data, "202H", "S1").update(status="merged", merged_into=target)
            self.store.write(data)
            raw = self.store.path.read_bytes()
            with self.subTest(target=target), self.assertRaisesRegex(DirectionError, "不明确"):
                self.store.register_candidate(self.candidate())
            self.assertEqual(self.store.path.read_bytes(), raw)

    def test_import_does_not_weaken_manual_variant_creation_checks(self):
        result = self.store.register_candidate(self.candidate())
        data = self.store.read()
        entity = result["direction"]
        with self.assertRaisesRegex(DirectionError, "请选择这条变体的推进结构"):
            self.store._save_direction(data, {**entity, "execution_change": "新的执行改动"})


if __name__ == "__main__":
    unittest.main()
