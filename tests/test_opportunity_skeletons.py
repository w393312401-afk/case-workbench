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

    def test_an_objection_opening_keeps_the_belief_the_concession_and_the_commenters_words(self):
        from opportunity_skeletons import brief
        raw = {"opening": [{"pain_zh": "干活的刀磨镜面没用", "kind": "objection", "belief_zh": "镜面撑不了几个纸箱",
                            "concede_zh": "拆纸箱确实用不着镜面", "need_zh": "每次补磨回到同一角度", "quotes": ["made-up", "q2"]},
                           {"pain_zh": "不知道怎么磨出镜面", "kind": "made-up", "quotes": []}]}
        result = normalize(raw, points={}, directions={}, quote_ids={"q1", "q2"}, quote_texts={"q2": "a  Gerber with a mirror edge"})
        first, second = result["opening"]
        self.assertEqual((first["kind"], first["kind_label"], first["belief_zh"], first["concede_zh"]),
                         ("objection", "质疑", "镜面撑不了几个纸箱", "拆纸箱确实用不着镜面"))
        self.assertEqual(first["quote"], {"id": "q2", "text": "a Gerber with a mirror edge"})
        self.assertFalse({"kind", "belief_zh", "quote"} & set(second))
        text = brief(result)
        for part in ("开头（质疑类评论）", "观众的看法：镜面撑不了几个纸箱", "能认同的部分：拆纸箱确实用不着镜面", "转向的需求：每次补磨回到同一角度", "对应原评论 q2：a Gerber"):
            self.assertIn(part, text)

    def test_a_b_point_marked_not_an_order_reason_cannot_close(self):
        points = {"S1": {"id": "p1", "code": "S1", "tier": "S", "title": "定角", "status": "testing"},
                  "B5": {"id": "p5", "code": "B5", "tier": "B", "title": "磨板可补买", "status": "testing",
                         "rationale": "用法：中段回应耗材顾虑；不当下单理由。"}}
        raw = {"opening": [{"pain_zh": "角度靠猜"}], "middle": [{"code": "S1", "answer_zh": "固定角度"}],
               "closing": {"code": "B5", "reason_zh": "可补买"}}
        result = normalize(raw, points=points, directions={}, quote_ids=set())
        self.assertIsNone(result["closing"])
        self.assertEqual((result["state"], result["gaps"][0]["kind"]), ("gaps", "no_closing"))
        self.assertIn("B5", result["gaps"][0]["text"])

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
        # A verified reason (the warranty) may close the script; only prices and shop terms wait for the listing.
        self.assertIn("写明已核的下单理由（例如质保年限、退货期限）可以直接写进收口", price["draft_instruction"])
        self.assertIn("没核对发布当天的挂车页时不写进口播或屏显", price["draft_instruction"])
        self.assertIn("不能因此不带下单理由", price["draft_instruction"])
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
        self.assertTrue(price["skeleton"]["opening"])
        self.assertNotIn("AI 三段骨架", price["draft_instruction"])

    def test_changed_selling_point_blocks_old_brief_until_regenerated(self):
        self.app.opportunity_skeletons.start({"product": "202H", "themes": ["topic:price"]})
        self.wait()
        point = next(row for row in self.catalog["selling_points"] if row["code"] == "B3")
        for field in ("title", "rationale", "evidence"):
            with self.subTest(field=field):
                point[field] = "更新后的资料：" + field
                items = opportunity_catalog(self.app, "202H")["items"]
                price = next(item for item in items if item["id"] == "topic:price")
                self.assertTrue(price["skeleton"]["stale"])
                self.assertEqual(price["skeleton"]["state"], "complete")
                self.assertIn("资料有变化", price["skeleton"]["stale_reason"])
                self.assertNotIn("AI 三段骨架", price["draft_instruction"])
                self.assertTrue(self.app.opportunity_skeletons.states("202H")["themes"]["topic:price"]["stale"])
                self.app.opportunity_skeletons.start({"product": "202H", "themes": ["topic:price"]})
                self.wait()
                price = next(item for item in opportunity_catalog(self.app, "202H")["items"] if item["id"] == "topic:price")
                self.assertFalse(price["skeleton"]["stale"])
                self.assertIn("AI 三段骨架", price["draft_instruction"])
                self.assertIn(point["title"], price["draft_instruction"])

    def test_legacy_skeleton_is_viewable_without_changing_saved_data_or_using_its_brief(self):
        skeletons = self.app.opportunity_skeletons
        skeletons.start({"product": "202H", "themes": ["topic:price"]})
        self.wait()
        data = json.loads(skeletons.path.read_text(encoding="utf-8"))
        data["products"]["202H"]["topic:price"].pop("source_basis")
        skeletons.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        saved = skeletons.path.read_bytes()
        price = next(item for item in opportunity_catalog(self.app, "202H")["items"] if item["id"] == "topic:price")
        self.assertTrue(price["skeleton"]["stale"])
        self.assertIn("缺少资料版本", price["skeleton"]["stale_reason"])
        self.assertEqual(price["skeleton"]["closing"]["code"], "B3")
        self.assertNotIn("AI 三段骨架", price["draft_instruction"])
        self.assertEqual(skeletons.path.read_bytes(), saved)

    def test_only_selected_point_and_direction_changes_invalidate_a_complete_chain(self):
        skeletons = self.app.opportunity_skeletons
        skeletons.start({"product": "202H", "themes": ["topic:price", "topic:angle"]})
        self.wait()
        # Catalog timestamps, unrelated products and unused sources do not change the chosen chain.
        self.catalog["selling_points"][0]["updated_at"] = "new timestamp"
        next(row for row in self.catalog["selling_points"] if row["code"] == "A9")["title"] = "另一个候选"
        self.catalog["selling_points"].append({"product": "198H", "code": "B3", "title": "另一个产品"})
        self.catalog["directions"].append({"product": "202H", "direction_id": "202H-S2", "status": "testing"})
        self.assertTrue(all(not item["skeleton"]["stale"] for item in opportunity_catalog(self.app, "202H")["items"]))
        # One chosen direction changes: both saved chains are now pending, not complete-ranked.
        self.catalog["directions"][0]["concern"] = "新的顾虑链"
        items = opportunity_catalog(self.app, "202H")["items"]
        self.assertTrue(all(item["skeleton"]["stale"] for item in items))
        self.assertTrue(all("AI 三段骨架" not in item["draft_instruction"] for item in items))
        # Regenerating one theme restores its complete-chain priority over the stale theme.
        skeletons.start({"product": "202H", "themes": ["topic:price"]})
        self.wait()
        items = opportunity_catalog(self.app, "202H")["items"]
        self.assertEqual(items[0]["id"], "topic:price")
        self.assertFalse(items[0]["skeleton"]["stale"])
        self.assertTrue(items[1]["skeleton"]["stale"])

    def test_catalog_change_while_ai_runs_does_not_get_signed_as_current(self):
        self.bridge.release.clear()
        skeletons = self.app.opportunity_skeletons
        skeletons.start({"product": "202H", "themes": ["topic:price"]})
        for _ in range(100):
            if self.bridge.prompts:
                break
            time.sleep(0.01)
        self.assertTrue(self.bridge.prompts)
        next(row for row in self.catalog["selling_points"] if row["code"] == "B3")["evidence"] = "生成期间资料已改变"
        self.bridge.release.set()
        self.wait()
        price = next(item for item in opportunity_catalog(self.app, "202H")["items"] if item["id"] == "topic:price")
        self.assertTrue(price["skeleton"]["stale"])
        self.assertNotIn("AI 三段骨架", price["draft_instruction"])

    def test_writing_rule_changes_invalidate_the_saved_brief(self):
        root = Path(self.app.ai.skill_code_root).parent / "product-video-script-writing"
        (root / "references").mkdir(parents=True)
        names = ("SKILL.md", "references/writing-and-native.md", "references/guidance.md")
        for name in names:
            (root / name).write_text("当前写作规则", encoding="utf-8")
        for name in names:
            with self.subTest(source=name):
                self.app.opportunity_skeletons.start({"product": "202H", "themes": ["topic:price"]})
                self.wait()
                (root / name).write_text("本次更新的写作规则", encoding="utf-8")
                price = next(item for item in opportunity_catalog(self.app, "202H")["items"] if item["id"] == "topic:price")
                self.assertTrue(price["skeleton"]["stale"])
                self.assertNotIn("AI 三段骨架", price["draft_instruction"])

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


class RetiredDirectionTests(unittest.TestCase):
    def test_direction_whose_point_was_merged_is_not_offered(self):
        catalog = {"selling_points": [{"id": "p1", "code": "S1", "product": "202H", "status": "testing"},
                                      {"id": "p9", "code": "S9", "product": "202H", "status": "merged", "merged_into": "S1"}],
                   "directions": [{"direction_id": "202H-S1", "product": "202H", "selling_point_id": "p1", "status": "testing"},
                                  {"direction_id": "202H-S9", "product": "202H", "selling_point_id": "p9", "status": "paused"},
                                  {"direction_id": "202H-S2", "product": "202H", "selling_point_id": "p2", "status": "archived"}]}
        skeletons = OpportunitySkeletons.__new__(OpportunitySkeletons)
        skeletons.app = SimpleNamespace(directions=SimpleNamespace(read=lambda: catalog))
        points, directions = skeletons._catalog("202H")
        self.assertEqual(set(points), {"S1", "S9"})
        self.assertEqual(set(directions), {"202H-S1"})


if __name__ == "__main__":
    unittest.main()
