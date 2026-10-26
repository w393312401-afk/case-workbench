import copy
import unittest

from production_naming import production_title, production_topic, timeline_label
from production_store import public_job


class ProductionNamingTests(unittest.TestCase):
    def test_current_voice_timeline_and_concrete_topic(self):
        job = {"title": "202H-S2 别再拼零散磨具，按需要走完整路径", "direction_id": "202H-S2",
               "selected_take": "T2", "timelines": [{"name": "旧配音", "take_id": "T1"},
                   {"name": "Lab261021-202-1", "take_id": "T2"}, {"name": "其他试剪", "take_id": "T1"}]}
        self.assertEqual(production_title(job), "Lab261021-202-1｜别再拼零散磨具，按需要走完整路径")
        self.assertEqual(timeline_label({**job, "selected_take": "new"}), "其他试剪")
        self.assertEqual(timeline_label({**job, "selected_take": ""}), "其他试剪")

    def test_generic_labels_use_only_the_orders_own_opening(self):
        for label in ("变体 1", "变体 B", "执行变体 #2", "版本 #4", "版本 A", "第 3 条"):
            with self.subTest(label=label):
                job = {"title": "202H-S2 " + label, "direction_id": "202H-S2",
                       "lines": [{"zh": "  别再买\n零散的磨刀工具。 ", "spoken": "Wrong fallback."}]}
                self.assertEqual(production_title(job), "别再买 零散的磨刀工具。")
                self.assertEqual(production_topic({**job, "lines": [{"zh": " ", "spoken": "Start with a complete kit."}]}),
                                 "Start with a complete kit.")

    def test_archived_timeline_does_not_name_the_active_order(self):
        job = {"title": "当前正文", "selected_take": "T1", "timelines": [
            {"name": "保留时间线", "take_id": "T1"},
            {"name": "历史时间线", "take_id": "T1", "archived": True}]}
        self.assertEqual(production_title(job), "保留时间线｜当前正文")
        job["timelines"][0]["archived"] = True
        self.assertEqual(production_title(job), "当前正文")
        job["timelines"][1]["archived"] = False
        self.assertEqual(production_title(job), "历史时间线｜当前正文")

    def test_concrete_variant_suffix_is_preserved_and_code_match_is_exact(self):
        for label in ("变体 1 · 一套覆盖从粗磨到精磨", "执行变体 B｜一套覆盖从粗磨到精磨", "第 2 条：一套覆盖从粗磨到精磨"):
            with self.subTest(label=label):
                self.assertEqual(production_topic({"title": "202H-S2｜" + label, "direction_id": "202H-S2"}),
                                 "一套覆盖从粗磨到精磨")
        self.assertEqual(production_topic({"title": "202H-S20 修复旧刀", "direction_id": "202H-S2"}), "202H-S20 修复旧刀")
        self.assertEqual(production_topic({"title": "变体 12件工具的区别"}), "变体 12件工具的区别")

    def test_brief_topics_empty_timeline_and_missing_opening(self):
        chinese, english = "磨" * 50, "Sharpen your knives with a complete kit. " * 3
        self.assertEqual(len(production_topic({"title": chinese})), 36)
        self.assertEqual(len(production_topic({"title": english})), 72)
        self.assertTrue(production_topic({"title": english}).endswith("…"))
        self.assertEqual(production_title({"title": "变体 1", "timelines": [{"name": " "}]}), "变体 1")
        self.assertEqual(production_title({}), "未命名制作单")

    def test_public_name_is_derived_without_mutating_stored_facts(self):
        job = {"title": "202H-S2 变体 1", "direction_id": "202H-S2", "lines": [{"zh": "一套从粗到细", "spoken": "A complete kit."}],
               "timelines": [{"name": "Lab261021-202-1", "take_id": "T1"}], "selected_take": "T1", "takes": [], "edits": []}
        before = copy.deepcopy(job)
        result = public_job(job)
        self.assertEqual(result["display_title"], "Lab261021-202-1｜一套从粗到细")
        self.assertEqual(result["title"], before["title"])
        self.assertEqual(job, before)


if __name__ == "__main__":
    unittest.main()
