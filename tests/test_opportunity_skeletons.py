"""AI three-part skeletons: only catalog points survive, gaps are explicit, ranking and the writing brief use them."""
from contextlib import closing
from datetime import date
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from opportunity_skeletons import OpportunitySkeletons, SkeletonError, normalize, parse_json, settle
from review_opportunities import opportunity_catalog
from review_store import ReviewStore


POINTS = {"S1": {"id": "p-s1", "code": "S1", "tier": "S", "title": "不用猜角度", "status": "testing"},
          "A9": {"id": "p-a9", "code": "A9", "tier": "A", "title": "候选卖点", "status": "candidate"},
          "S6": {"id": "p-s6", "code": "S6", "tier": "S", "title": "已并入", "status": "merged"},
          "B3": {"id": "p-b3", "code": "B3", "tier": "B", "title": "4 年质保", "status": "testing"}}


class FakeBridge:
    def __init__(self, output):
        self.output, self.prompts, self.release = output, [], threading.Event()
        self.release.set()

    def run(self, prompt, *, cwd, cancel_event, on_progress, **options):
        self.prompts.append(prompt)
        self.release.wait(5)
        return self.output


class NormalizeTests(unittest.TestCase):
    def test_unknown_merged_and_wrong_tier_codes_become_gaps(self):
        raw = {"opening": [{"pain_zh": "角度一直靠猜", "quotes": ["q1", "made-up"]}],
               "middle": [{"code": "S9", "answer_zh": "编的卖点"}, {"code": "S6", "answer_zh": "并入的"}, {"code": "B3", "answer_zh": "B 不能做中段"}],
               "closing": {"code": "S1", "reason_zh": "S 不能做收口"},
               "verdict": {"kind": "existing", "direction_id": "202H-S404"}}
        result = normalize(raw, points=POINTS, directions={"202H-S1": {}}, quote_ids={"q1"})
        self.assertEqual(result["middle"], [])
        self.assertIsNone(result["closing"])
        self.assertEqual(result["opening"][0]["quotes"], ["q1"])
        self.assertEqual({row["kind"] for row in result["gaps"]}, {"no_point", "no_closing"})
        self.assertEqual(result["verdict"]["kind"], "new")
        self.assertEqual(result["state"], "gaps")

    def test_complete_chain_and_candidate_marks_unverified(self):
        raw = {"opening": [{"pain_zh": "角度一直靠猜"}], "middle": [{"code": "s1", "answer_zh": "固定角度", "shot_zh": "上色后看接触"}],
               "closing": {"code": "B3", "reason_zh": "怕坏"}, "verdict": {"kind": "existing", "direction_id": "202H-S1"}}
        result = normalize(raw, points=POINTS, directions={"202H-S1": {}}, quote_ids=set())
        self.assertEqual((result["state"], result["middle"][0]["point_id"], result["verdict"]["direction_id"]), ("complete", "p-s1", "202H-S1"))
        raw["middle"].append({"code": "A9", "answer_zh": "候选"})
        result = normalize(raw, points=POINTS, directions={}, quote_ids=set())
        self.assertEqual(result["state"], "gaps")
        self.assertEqual([row["kind"] for row in result["gaps"]], ["unverified"])
        raw["gaps"] = [{"kind": "unverified", "text_zh": "A9 没有实测依据"}]
        result = normalize(raw, points=POINTS, directions={}, quote_ids=set())
        self.assertEqual([row["text"] for row in result["gaps"]], ["A9 没有实测依据"])

    def test_boundary_reminders_become_notes_and_do_not_block(self):
        # Every part is filled with verified points: reminders and an old-style "gap" about a
        # filled part are notes, a note about an unused point is dropped, the chain is complete.
        raw = {"opening": [{"pain_zh": "角度一直靠猜"}], "middle": [{"code": "S1", "answer_zh": "固定角度"}],
               "closing": {"code": "B3", "reason_zh": "怕坏"}, "verdict": {"kind": "existing", "direction_id": "202H-S1"},
               "gaps": [{"kind": "no_point", "text_zh": "锯齿刀能不能磨库里没依据"},
                        {"kind": "no_closing", "text_zh": "B3 须按店铺政策核对"}],
               "notes": ["A1 角度范围依据未写", "别说 S30V 都能磨", "别说 S30V 都能磨"]}
        result = normalize(raw, points=POINTS, directions={"202H-S1": {}}, quote_ids=set())
        self.assertEqual((result["state"], result["gaps"]), ("complete", []))
        self.assertEqual(result["notes"], ["锯齿刀能不能磨库里没依据", "B3 须按店铺政策核对", "别说 S30V 都能磨"])
        # A skeleton saved before the split (reminders stored as gaps) re-sorts the same way.
        saved = {key: value for key, value in result.items() if key != "notes"} | {"state": "gaps", "gaps": [
            {"kind": "no_point", "label": "缺卖点", "text": "锯齿刀能不能磨库里没依据"},
            {"kind": "unverified", "label": "待核实", "text": "A9 还是待核候选卖点"}]}
        self.assertEqual(settle(saved)["state"], "complete")
        self.assertEqual(settle(saved)["notes"], ["锯齿刀能不能磨库里没依据"])
        # A used candidate point still holds the chain back.
        saved["middle"] = saved["middle"] + [{"code": "A9", "status": "candidate"}]
        self.assertEqual((settle(saved)["state"], [row["kind"] for row in settle(saved)["gaps"]]), ("gaps", ["unverified"]))

    def test_parse_rejects_non_json(self):
        self.assertEqual(parse_json('```json\n{"opening": []}\n```'), {"opening": []})
        with self.assertRaises(SkeletonError):
            parse_json("sorry")


class SkeletonFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="opportunity-skeletons-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.reviews = ReviewStore(root)
        self.catalog = {"selling_points": [dict(row, product="202H") for row in POINTS.values()],
                        "directions": [{"direction_id": "202H-S1", "product": "202H", "main_difference": "S1 不用猜", "status": "testing"}]}
        self.bridge = FakeBridge(json.dumps({"opening": [{"pain_zh": "角度一直靠猜", "quotes": ["one"]}],
                                             "middle": [{"code": "S1", "answer_zh": "固定角度", "shot_zh": "上色看接触"}],
                                             "closing": {"code": "B3", "reason_zh": "怕磨坏"},
                                             "verdict": {"kind": "existing", "direction_id": "202H-S1", "reason_zh": "对得上"}}))
        self.app = SimpleNamespace(lock=threading.RLock(), reviews=self.reviews,
                                   creation=SimpleNamespace(public=lambda product: {"theme_links": [], "scripts": [], "plans": []}),
                                   directions=SimpleNamespace(read=lambda: self.catalog),
                                   ai=SimpleNamespace(bridge=self.bridge, preferences=lambda: {}, skill_code_root=str(root / "none")))
        self.app.opportunity_skeletons = OpportunitySkeletons(self.app, self.reviews.root)
        today = date.today().isoformat()
        with closing(self.reviews.connect()) as connection:
            for identifier, text, topic in (("one", "I always guess the angle.", "angle"), ("two", "Price is high.", "price")):
                connection.execute("INSERT INTO reviews (id, platform, product, role, text, relevant, first_seen) VALUES (?,?,?,?,?,?,?)",
                                   (identifier, "amazon", "202H", "verified_buyer", text, 1, today))
                connection.execute("INSERT INTO review_tags VALUES (?,?,?)", (identifier, "topic", topic))
            connection.commit()

    def wait(self):
        for _ in range(100):
            if not self.app.opportunity_skeletons.states("202H")["running"]:
                return
            time.sleep(0.05)
        self.fail("skeleton never finished")

    def test_generate_ranks_first_and_joins_brief(self):
        before = [item["id"] for item in opportunity_catalog(self.app, "202H")["items"]]
        self.assertEqual(before, ["topic:angle", "topic:price"])  # tie broken by id
        self.app.opportunity_skeletons.start({"product": "202H", "themes": ["topic:price"]})
        self.wait()
        items = opportunity_catalog(self.app, "202H")["items"]
        self.assertEqual(items[0]["id"], "topic:price")
        price = items[0]
        self.assertEqual((price["skeleton"]["state"], price["skeleton"]["stale"]), ("complete", False))
        self.assertIn("AI 三段骨架", price["draft_instruction"])
        self.assertIn("B3 4 年质保", price["draft_instruction"])
        self.assertIn("不是口播逐项清单", price["draft_instruction"])
        self.assertIn("每句都应推进同一个购买理由", price["draft_instruction"])
        self.assertIn("结合具体使用动作或选择说明其作用", price["draft_instruction"])
        self.assertIn("不因出现配置或数字就一律删除", price["draft_instruction"])
        self.assertIn("回应要符合评论是在表达感受、陈述、提问还是质疑", price["draft_instruction"])
        self.assertIn("同一信息即使隔句换词复述", price["draft_instruction"])
        self.assertIn("不以篇幅更短代替清楚解释", price["draft_instruction"])
        self.assertIn("购买理由已讲清时不必再次复述", price["draft_instruction"])
        self.assertIn("用户明确指定结尾或单列时点时按其要求", price["draft_instruction"])
        self.assertIn("用户指定开箱清点", price["draft_instruction"])
        self.assertIn("未核实当前销售渠道与适用范围时，不写进口播或屏显", price["draft_instruction"])
        self.assertIn("开头、中段与收口", self.bridge.prompts[0])
        self.assertIn("不能只罗列配置和材质", self.bridge.prompts[0])
        self.assertIn("Price is high.", self.bridge.prompts[0])
        self.assertNotIn("已并入", self.bridge.prompts[0])
        # A new quote in the theme marks the saved skeleton out of date, without dropping it.
        with closing(self.reviews.connect()) as connection:
            connection.execute("INSERT INTO reviews (id, platform, product, role, text, relevant, first_seen) VALUES (?,?,?,?,?,?,?)",
                               ("three", "amazon", "202H", "verified_buyer", "Too expensive for me.", 1, date.today().isoformat()))
            connection.execute("INSERT INTO review_tags VALUES (?,?,?)", ("three", "topic", "price"))
            connection.commit()
        price = next(item for item in opportunity_catalog(self.app, "202H")["items"] if item["id"] == "topic:price")
        self.assertTrue(price["skeleton"]["stale"])

    def test_skip_verdict_ranks_last_and_bad_requests_fail(self):
        self.bridge.output = json.dumps({"opening": [{"pain_zh": "x"}], "verdict": {"kind": "skip", "reason_zh": "零散"}})
        self.app.opportunity_skeletons.start({"product": "202H", "themes": ["topic:angle"]})
        self.wait()
        self.assertEqual(opportunity_catalog(self.app, "202H")["items"][-1]["id"], "topic:angle")
        for body in ({"product": "all", "themes": ["topic:angle"]}, {"product": "202H", "themes": []},
                     {"product": "202H", "themes": ["topic:unclassified"]}):
            with self.assertRaises(SkeletonError):
                self.app.opportunity_skeletons.start(body)

    def test_restart_marks_interrupted_runs_failed_and_stop_clears_queue(self):
        self.bridge.release.clear()
        self.app.opportunity_skeletons.start({"product": "202H", "themes": ["topic:angle", "topic:price"]})
        states = self.app.opportunity_skeletons.stop({"product": "202H"})
        self.assertIn(states["themes"]["topic:price"]["status"], {"cancelled", "running"})
        reopened = OpportunitySkeletons(self.app, self.reviews.root)
        self.assertFalse(reopened.states("202H")["running"])
        self.bridge.release.set()
        self.app.opportunity_skeletons.worker.join(5)
        self.assertEqual(reopened.states("202H")["themes"]["topic:angle"]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
