"""Review lessons are selected once and remain traceable in writing/review tasks."""
import copy
import json
from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import Mock
from urllib.parse import urlencode
from uuid import uuid4

import test_server as fixture
from test_ai_tasks import FakeBridge
import test_plan_variant_binding as plans


def experience(**changes):
    return {"id": "EXP-123456789abc", "version": 2, "product": "202H", "kind": "success",
            "dimension": "证明", "practice": "先展示装夹，再说明适用刀型", "conditions": "已有装夹实拍，面向首次使用者",
            "evidence_note": "两条同方向视频有正向表现；尚不能认定因果", "confidence": "preliminary",
            "scope": "product", "direction_id": "", "source": {
                "review": {"product": "202H", "video_id": "7664986913497009439", "window_end": "2026-10-20",
                           "reviewed_at": "2026-10-21T01:00:00+00:00", "note": "先让观众看到装夹"},
                "video": {"headline": "装夹演示"}, "publications": []}, **changes}


class ExperienceTaskContextTests(unittest.TestCase):
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    direction = plans.PlanVariantHTTPTests.direction

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        self.app = self.server.app
        self.bridge = FakeBridge()
        self.app.ai.bridge = self.bridge
        self.app.ai.draft_media = Mock(side_effect=AssertionError("Footage draft prompt only belongs to retrospectives"))
        self.items = [experience()]
        # Source eligibility and ranking have their own store/API tests; this boundary verifies task capture.
        self.app.review_experiences_for_task = Mock(side_effect=lambda *args, **kwargs: self.items)

    def ok(self, method, path, body=None):
        status, _, result = self.request(method, path, body)
        self.assertEqual(status, 200, result)
        return result

    def wait(self, task):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = self.app.ai.get(task["task_id"])
            if task["status"] not in {"queued", "running"} and not task["busy"]:
                self.assertEqual(task["status"], "completed", task)
                return task
            time.sleep(.01)
        self.fail("The fake task did not finish")

    def task(self, **values):
        body = {"request_id": uuid4().hex, "product": "202H", "action": "review", "skill_selection": "none",
                "instruction": "核对做法及其适用条件。", **values}
        if body.get("case_id"):
            body["revision"] = self.app.detail(body["case_id"])["revision"]
        return self.wait(self.ok("POST", "/api/ai/tasks", body)["task"]), body

    def script(self, **values):
        return self.ok("POST", "/api/scripts", {"request_id": uuid4().hex,
            "revision": self.app.creation.public()["revision"], "script": {
                "product": "202H", "title": "装夹采用稿", "text": "Show the clamp first.",
                "translation_zh": "先展示装夹。", "confirmed": True, **values}})["script"]

    def test_writing_freezes_and_exposes_sources_for_both_independent_stages(self):
        original = copy.deepcopy(self.items)
        task, _ = self.task(action="variants")
        self.app.review_experiences_for_task.assert_called_once_with("202H", "", limit=8)
        self.assertEqual(task["context"]["review_experiences"], original)
        self.assertEqual(len(self.bridge.calls), 2)
        self.app.ai.draft_media.assert_not_called()
        for call in self.bridge.calls:
            prompt = call["prompt"]
            self.assertIn("复盘经验使用规则", prompt)
            self.assertIn("不能从单条视频结果认定因果", prompt)
            self.assertIn("内部编号不得写进口播", prompt)
            self.assertIn(original[0]["practice"], prompt)
            self.assertIn(original[0]["source"]["review"]["video_id"], prompt)
        self.assertIn("独立第二次内容复核", self.bridge.calls[1]["prompt"])
        self.items[0]["practice"] = "后来改成了另一条经验"
        task["context"]["review_experiences"][0]["conditions"] = "调用方改动"
        raw = self.app.ai._read(task["task_id"])
        self.assertEqual(raw["snapshot"]["creation_context"]["review_experiences"], original)

    def test_review_case_direct_direction_and_plan_select_only_bound_direction(self):
        direction = self.direction()
        direction_id = direction["direction_id"]
        self.task(direction_id=direction_id)
        self.app.review_experiences_for_task.assert_called_with("202H", direction_id, limit=8)
        case = self.store.read_case(self.identifier)
        current = copy.deepcopy(case["current"])
        current["test_direction"] = {"direction_id": direction_id, "variant_type": "direction",
                                     "main_difference": direction["main_difference"], "concern": direction["concern"],
                                     "closing": direction["closing"]}
        self.store.append(self.identifier, {"kind": "fact", "text": "登记方向", "current": current})
        self.task(case_id=self.identifier)
        self.app.review_experiences_for_task.assert_called_with("202H", direction_id, limit=8)
        plan = self.ok("POST", "/api/plans", {"request_id": uuid4().hex,
            "revision": self.app.creation.public()["revision"], "plan": {
                "product": "202H", "direction_id": direction_id, "action": "execution"}})["plan"]
        task, _ = self.task(plan_id=plan["id"])
        self.app.review_experiences_for_task.assert_called_with("202H", direction_id, limit=8)
        self.assertEqual(task["context"]["direction_id"], direction_id)

    def test_missing_notes_are_frozen_as_unknown_and_only_used_as_leads(self):
        self.items = [experience(conditions="", evidence_note="")]
        for action in ("variants", "review"):
            self.bridge.calls.clear()
            task, _ = self.task(action=action)
            selected = task["context"]["review_experiences"][0]
            self.assertEqual(selected["conditions"], "")
            self.assertEqual(selected["evidence_note"], "")
            self.assertEqual(selected["source"], self.items[0]["source"])
            self.assertEqual(len(self.bridge.calls), 2 if action == "variants" else 1)
            for call in self.bridge.calls:
                self.assertIn("只能作为待核对线索", call["prompt"])
                self.assertIn("不得视为通用做法或已验证结论", call["prompt"])
                self.assertIn("不代表本轮适用性已经确认", call["prompt"])

    def test_adopted_script_uses_its_direction_and_rejects_mismatched_product_or_direction(self):
        direction = self.direction()
        other = self.direction("S2")
        script = self.script(direction_id=direction["direction_id"])
        task, _ = self.task(script_id=script["id"])
        self.app.review_experiences_for_task.assert_called_with("202H", direction["direction_id"], limit=8)
        self.assertEqual(task["context"]["direction_id"], direction["direction_id"])
        self.app.ai.draft_media.assert_not_called()
        for extra in ({"product": "129N"}, {"direction_id": other["direction_id"]}):
            body = {"request_id": uuid4().hex, "product": "202H", "action": "review", "skill_selection": "none",
                    "script_id": script["id"], "instruction": "审稿", **extra}
            self.assertEqual(self.request("POST", "/api/ai/tasks", body)[0], 409)
        self.assertEqual(self.app.review_experiences_for_task.call_count, 1)

    def test_historical_script_uses_only_resolved_variant_link_or_product_scope(self):
        direction = self.direction()
        script = self.script()
        source = self.ok("GET", "/api/variant-links/source?" + urlencode({
            "kind": "script", "id": script["id"], "product": "202H"}))
        self.ok("POST", "/api/variant-links", {"revision": source["revision"],
            "record_revision": source["record_revision"], "request_id": uuid4().hex,
            "link": {"kind": "script", "record_id": script["id"], "product": "202H",
                "direction_id": direction["direction_id"], "variant_ref": plans.variant_ref(direction),
                "evidence": {"note": "已核对旧稿与变体链路。", "refs": ["script:" + script["id"]]}}})
        self.task(script_id=script["id"])
        self.app.review_experiences_for_task.assert_called_with("202H", direction["direction_id"], limit=8)
        data = self.app.directions.read()
        data["directions"][0]["variants"][0]["concern"] = "方向变体已变化，旧关联需要重新核对"
        self.app.directions.write(data)
        task, _ = self.task(script_id=script["id"])
        self.app.review_experiences_for_task.assert_called_with("202H", "", limit=8)
        self.assertFalse(task["context"]["direction_id"])

    def test_continue_and_request_replay_keep_original_even_when_library_is_unavailable(self):
        original = copy.deepcopy(self.items)
        task, body = self.task()
        self.app.review_experiences_for_task.side_effect = OSError("unavailable")
        self.items[0]["version"] = 3
        replay = self.ok("POST", "/api/ai/tasks", body)["task"]
        self.assertEqual(replay["task_id"], task["task_id"])
        continued = self.wait(self.app.ai.continue_task(task["task_id"], {
            "request_id": uuid4().hex, "task_revision": task["revision"], "instruction": "再核对一次证据。"}))
        self.assertEqual(continued["context"]["review_experiences"], original)
        self.app.review_experiences_for_task.assert_called_once()
        self.assertIn(original[0]["evidence_note"], self.bridge.calls[-1]["prompt"])
        count = len(self.app.ai.list_tasks())
        status, _, failure = self.request("POST", "/api/ai/tasks", {**body, "request_id": uuid4().hex})
        self.assertEqual(status, 503, failure)
        self.assertIn("复盘经验暂时无法读取", failure["error"])
        self.assertEqual(len(self.app.ai.list_tasks()), count)

    def test_empty_selection_is_distinct_from_legacy_and_retrospective_tasks(self):
        self.items = []
        task, _ = self.task()
        self.assertEqual(task["context"]["review_experiences"], [])
        old = self.wait(self.app.ai.create(None, {"request_id": uuid4().hex, "action": "review",
            "instruction": "历史任务", "skill_selection": "none"},
            {"kind": "blank", "product": "202H", "title": "历史任务", "materials": []}))
        self.assertNotIn("review_experiences", old["context"])
        retrospective, _ = self.task(action="retrospective")
        self.assertNotIn("review_experiences", retrospective["context"])
        self.app.review_experiences_for_task.assert_called_once()
        self.assertNotIn("复盘经验使用规则", self.bridge.calls[-1]["prompt"])

    def test_task_panel_uses_frozen_context_and_escapes_reference_content(self):
        source = (Path(__file__).resolve().parents[1] / "static" / "ai.js").read_text(encoding="utf-8")
        function = source.split("  function experienceContextMarkup(task) {", 1)[1].split("  function taskContextMarkup(task) {", 1)[0]
        item = experience(practice='<img src=x onerror="alert(1)">', kind="failure")
        missing = experience(conditions="", evidence_note="")
        script = """const window = {WorkbenchShell:{url:(hash,product)=>'#p/'+product+'/'+hash.slice(1)}};
const taskProduct = task => task.product;
const esc = value => String(value ?? '').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));
function experienceContextMarkup(task) {""" + function + "\nconsole.log(JSON.stringify([experienceContextMarkup({context:{}}),experienceContextMarkup({context:{review_experiences:[]}}),experienceContextMarkup(" + json.dumps({"product": "202H", "context": {"review_experiences": [item]}}, ensure_ascii=False) + "),experienceContextMarkup(" + json.dumps({"product": "202H", "context": {"review_experiences": [missing]}}, ensure_ascii=False) + ")]));"
        output = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
        old, empty, markup, incomplete = json.loads(output.stdout)
        self.assertEqual(old, "")
        self.assertIn("没有匹配到", empty)
        for value in ("失败教训", "版本 2", item["conditions"], item["evidence_note"], "feedback_window=2026-10-20"):
            self.assertIn(value, markup)
        self.assertIn("&lt;img", markup)
        self.assertNotIn("<img", markup)
        self.assertNotIn(item["id"], markup)
        self.assertNotIn(" open", markup)
        overview, basis = markup.split('<details class="ai-experience-basis">', 1)
        self.assertIn("来源复盘</a>", overview)
        self.assertNotIn(item["conditions"], overview)
        self.assertNotIn(item["evidence_note"], overview)
        self.assertIn("<summary>依据</summary>", basis)
        self.assertIn("适用条件：未补充，使用前需核对", incomplete)
        self.assertIn("证据补充：未补充", incomplete)
        self.assertNotIn("适用于本产品", incomplete)


if __name__ == "__main__":
    unittest.main()
