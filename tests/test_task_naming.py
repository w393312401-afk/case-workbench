"""Names for writing tasks (task_naming.py), on disposable stores only."""
import json
import unittest
from uuid import uuid4

from content_naming import content_title, task_display_metadata
from task_naming import TaskNamingMixin, clean_name, parse_name, rule_name
import test_ai_tasks


def task(instruction, product="202H", direction=""):
    return {"product": product, "context": {"direction_id": direction}, "rounds": [{"instruction": instruction}]}


class RuleTests(unittest.TestCase):
    def test_request_states_the_name(self):
        self.assertEqual(rule_name(task("产品：202H。围绕用户评论主题「镜面与抛光」写带货口播稿")), "202H 镜面与抛光")
        named = rule_name(task("顺着这条评论写稿（站在观众这边）。评论 2808cef824ffec6d｜某人 · @x｜TikTok 视频评论｜观众：\nToo expensive"))
        self.assertEqual(named, "202H 回应「Too expensive」")
        self.assertEqual(rule_name(task("按所选 2 条已登记变体（#7、#9）各写一条。", direction="202H-S2")), "202H-S2 #7 #9")
        self.assertEqual(rule_name(task("按所选 5 条已登记变体（#1、#2、#3、#4、#5）各写一条。", direction="202H-S2")), "202H-S2 5 条变体")
        detail = task("按写稿技能的口播稿交付格式……", direction="202H-S10")
        detail["context"]["direction_title"] = "S10 一套起步、以后能升级"
        self.assertEqual(rule_name(detail), "202H-S10 一套起步、以后能升级")

    def test_model_answer_is_cleaned_and_generic_names_refused(self):
        self.assertEqual(parse_name('好的\n{"name": "202H 回应「太贵了」"}'), "202H 回应「太贵了」")
        self.assertEqual(parse_name("不是 JSON"), "")
        self.assertEqual(clean_name("未命名内容"), "")
        self.assertEqual(clean_name("202H 镜面｜候选稿"), "202H 镜面")
        self.assertLessEqual(len(clean_name("很" * 80)), 24)

    def test_own_name_keeps_its_direction_code(self):
        row = {"title": "202H-S2 #10 清单演示", "title_source": "ai", "action": "variants"}
        self.assertEqual(content_title(row, "task", direction="202H-S2"), "202H-S2 #10 清单演示｜候选稿")


class App(TaskNamingMixin):
    def __init__(self, ai):
        self.ai = ai


class NamingTests(unittest.TestCase):
    setUp, tearDown, new_manager, wait = test_ai_tasks.TaskTests.setUp, test_ai_tasks.TaskTests.tearDown, test_ai_tasks.TaskTests.new_manager, test_ai_tasks.TaskTests.wait

    def finished(self, instruction="产品：202H。围绕用户评论主题「价格与性价比」写带货口播稿"):
        body = {"request_id": uuid4().hex, "action": "variants", "instruction": instruction}
        context = {"kind": "blank", "product": "202H", "title": "空白创作", "materials": []}
        return self.wait(self.manager.create(None, body, context)["task_id"])

    def stored(self, task_id):
        return json.loads((self.runtime / "ai-tasks" / task_id / "task.json").read_text(encoding="utf-8"))

    def test_ai_name_is_saved_without_a_new_revision_and_shown(self):
        done = self.finished()
        self.assertTrue(task_display_metadata(self.stored(done["task_id"]))["display_title"].startswith("未命名内容"))
        self.bridges[0].output = '{"name": "202H 回应「太贵了」"}'
        self.assertEqual(App(self.manager).name_task(done["task_id"]), "202H 回应「太贵了」")
        stored = self.stored(done["task_id"])
        self.assertEqual((stored["title"], stored["title_source"], stored["revision"]), ("202H 回应「太贵了」", "ai", done["revision"]))
        detail = self.manager.get(done["task_id"])
        self.assertEqual(detail["display_title"], "202H 回应「太贵了」｜候选稿")
        self.assertIn("价格与性价比", self.bridges[0].calls[-1]["prompt"])

    def test_failed_answer_falls_back_and_names_are_not_replaced_automatically(self):
        done = self.finished()
        self.bridges[0].output = "没法起名"
        self.assertEqual(App(self.manager).name_task(done["task_id"]), "202H 价格与性价比")
        self.assertEqual(self.stored(done["task_id"])["title_source"], "rule")
        self.bridges[0].output = '{"name": "另一个名字"}'
        self.assertEqual(App(self.manager).name_task(done["task_id"]), "")
        self.assertFalse(self.manager.set_title(done["task_id"], "又一个", source="ai"))
        self.assertTrue(self.manager.set_title(done["task_id"], "手改的名字", source="manual"))
        self.assertEqual(self.stored(done["task_id"])["title"], "手改的名字")


if __name__ == "__main__":
    unittest.main()
