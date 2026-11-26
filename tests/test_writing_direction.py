"""A writing task freezes a direction without the catalog's bookkeeping."""
import json
import unittest

from workbench_creation import writing_direction


class WritingDirectionTests(unittest.TestCase):
    def test_chain_and_variants_stay_and_sources_history_and_figures_go(self):
        source = [{"kind": "case", "case_id": "PVR-1", "event_id": "E" * 60}] * 20
        direction = {"direction_id": "202H-S2", "product": "202H", "selling_point_id": "SP-1", "status": "testing",
                     "main_difference": "S2 一套磨到镜面", "concern": "全套会不会多余", "closing": "下方看整套",
                     "audience": "只有单块石的人", "notes": "核心证明：同一把刀", "source": source,
                     "history": [{"concern": "旧链", "replaced_at": "2026-10-01"}], "performance": {"orders": 9},
                     "case_count": 6, "video_count": 8, "pending_plan": {"action": "scale"},
                     "variants": [{"variant_type": "execution", "main_difference": "S2 一套磨到镜面", "concern": "先给结果",
                                   "closing": "下方看整套", "execution_change": "结果开头", "progression": "结果倒叙",
                                   "audience": "送礼的人", "baseline": True, "conflict": False, "sources": source}]}
        slim = writing_direction(direction)
        self.assertEqual({key: slim[key] for key in ("direction_id", "main_difference", "concern", "closing", "audience", "notes")},
                         {key: direction[key] for key in ("direction_id", "main_difference", "concern", "closing", "audience", "notes")})
        for key in ("source", "history", "performance", "case_count", "video_count", "pending_plan"):
            self.assertNotIn(key, slim)
        self.assertEqual(slim["variants"], [{key: value for key, value in direction["variants"][0].items() if key != "sources"}])
        self.assertLess(len(json.dumps(slim, ensure_ascii=False)), len(json.dumps(direction, ensure_ascii=False)) / 4)
        self.assertIn("source", direction)  # the catalog copy is untouched


if __name__ == "__main__":
    unittest.main()
