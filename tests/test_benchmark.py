"""对标约束: contrasts pushed by the TK 看板 extension, frozen into writing tasks, and the AI check of finished drafts."""
from __future__ import annotations

import base64
import json
import time
import unittest
from uuid import uuid4

import test_ai_http as ai_http
from benchmark import BenchmarkError, check_prompt, clean_contrast, freeze, line_times, parse_check, rules_of

CONTRAST = {"hit": 4, "ctrl": 7, "diffs": [
    {"dim": "第 1 秒画面", "measure": "第一帧", "hit": "结果画面", "ctrl": "包装", "hit_count": 4, "hit_total": 4, "ctrl_count": 1, "ctrl_total": 7,
     "evidence": [{"video": "出圈1", "time": "00:00", "quote": "tomato"}], "advice": "第一秒放钝刀切番茄的结果"},
    {"dim": "证明出现", "hit": "第 5 秒", "ctrl": "第 14 秒", "hit_count": 3, "hit_total": 4, "advice": ""},
    {"dim": "", "hit": "dropped"}], "same": ["都挂车"], "caveats": ["样本少"], "secret": "x",
    "videos": [{"label": "出圈1", "id": "7550000000000000001", "author": "a", "group": "hit"}]}

DRAFT = """**202H-S2 · 变体 #1 · 质疑回应**

**口播（配音用）**

```
Everyone says these wreck good knives.
So I grabbed my most expensive one.
Paper test. Clean cut, no tearing.
```

**对应中文**

```
都说这种会把好刀磨坏。
所以我拿了最贵的那把。
切纸测试，切口干净。
```

**意群表**

| # | 口播 | 画面要求 |
| --- | --- | --- |
| 1 | Everyone says these wreck good knives. | 钝刀压扁番茄 |
| 2 | So I grabbed my most expensive one. | 拿起日式刀 |
| 3 | Paper test. Clean cut, no tearing. | 切纸 |
"""


class BenchmarkUnitTests(unittest.TestCase):
    def test_contrast_cleaning_rules_and_freeze(self):
        clean = clean_contrast(CONTRAST)
        self.assertEqual(len(clean["diffs"]), 2)
        self.assertNotIn("secret", clean)
        rules = rules_of(clean)
        self.assertEqual([rule["text"] for rule in rules], ["第一秒放钝刀切番茄的结果", "证明出现：第 5 秒"])
        self.assertEqual(rules[0]["basis"], "出圈 4/4 · 对照 1/7")
        self.assertEqual(rules[1]["basis"], "出圈 3/4")
        with self.assertRaises(BenchmarkError):
            clean_contrast({"diffs": []})
        contrast = {**clean, "id": "CON-1", "rules": rules, "created_at": "t"}
        frozen = freeze(contrast, [2], {"video_id": "7550000000000000009", "author": "bladeguy", "type": "质疑回应",
                                        "lines": [{"en": "Will this ruin my knives?"}]})
        self.assertEqual([rule["n"] for rule in frozen["rules"]], [1, 2])
        self.assertEqual(frozen["rules"][0]["text"], "证明出现：第 5 秒")
        self.assertIn("@bladeguy", frozen["rules"][1]["text"])
        self.assertEqual(frozen["source"]["id"], "CON-1")
        for bad in ([9], [1, 1], "1"):
            with self.assertRaises(BenchmarkError):
                freeze(contrast, bad)
        with self.assertRaises(BenchmarkError):
            freeze(None, [], None)

    def test_check_prompt_times_and_answer_parsing(self):
        lines = [{"number": 1, "spoken": "One two three four five.", "zh": "一", "proof": "番茄"}, {"number": 2, "spoken": "Six seven.", "zh": "二"}]
        self.assertEqual(line_times(lines), [0, 2])
        prompt = check_prompt({"rules": [{"n": 1, "text": "第一秒放结果", "basis": "出圈 4/4"}]}, [{"label": "#1", "lines": lines}])
        self.assertIn("〈对标约束〉\n1. 第一秒放结果（依据：出圈 4/4）", prompt)
        self.assertIn("第 1 句 [约 0 秒] One two three four five.｜一｜画面：番茄", prompt)
        answer = 'ok ```{"variants":[{"variant":1,"items":[{"rule":1,"ok":false,"where":"第 2 句","fix":"挪前"},{"rule":5,"ok":true},{"rule":1,"ok":"yes"}]},{"variant":3,"items":[]}]}```'
        self.assertEqual(parse_check(answer, 2, 1), [{"variant": 1, "items": [{"rule": 1, "ok": False, "where": "第 2 句", "fix": "挪前"}]}])
        self.assertIsNone(parse_check("no json", 1, 1))


class CheckBridge(ai_http.FakeBridge):
    def run(self, prompt, *, cwd, on_progress, cancel_event, **options):
        self.prompts.append(prompt)
        self.started.set()
        if "〈对标约束〉" in prompt:
            return json.dumps({"variants": [{"variant": 1, "items": [
                {"rule": 1, "ok": True, "where": "第 1 句（约 0 秒）", "fix": ""},
                {"rule": 2, "ok": False, "where": "第 3 句（约 4 秒）", "fix": "把切纸挪到第 2 句"}]}]}, ensure_ascii=False)
        return DRAFT


class BenchmarkHTTPTests(unittest.TestCase):
    setUp = ai_http.AIHTTPTests.setUp
    close = ai_http.AIHTTPTests.close
    request = ai_http.AIHTTPTests.request

    def extension(self, body, key=None):
        link = self.request("GET", "/api/public-stats/link")[1]
        return self.request("POST", "/api/public-stats/contrasts", body, authenticated=False,
                            headers={"X-TKL-Key": key or link["key"], "Origin": "chrome-extension://abcdefghijklmnop"})

    def stub_skills(self):
        for name in ("product-video-script-writing", "product-video-script-audit"):
            (self.root / name / "references").mkdir(parents=True, exist_ok=True)
            (self.root / name / "SKILL.md").write_text("\n".join(["---", f"name: {name}", "description: 测试用", "---", "测试用写作规则"]), encoding="utf-8")

    def test_contrast_to_writing_task_to_check(self):
        self.bridge.__class__ = CheckBridge
        body = {"product": "202H", "contrast": CONTRAST, "title": "磨刀器对照", "extension_version": "0.15.0"}
        self.assertEqual(self.extension(body, key="wrong")[0], 403)
        self.assertEqual(self.extension({**body, "product": "999Z"})[0], 404)
        status, saved = self.extension(body)
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["rules"], 2)
        status, listed = self.request("GET", "/api/benchmark/contrasts?product=202H")
        self.assertEqual(status, 200, listed)
        contrast = listed["contrasts"][0]
        self.assertEqual((contrast["id"], contrast["title"], len(contrast["rules"])), (saved["id"], "磨刀器对照", 2))
        task_body = {"request_id": uuid4().hex, "product": "202H", "action": "variants", "instruction": "按对标写一条。", "skill_selection": "none",
                     "benchmark": {"contrast_id": saved["id"], "rules": [1, 2]}}
        self.assertEqual(self.request("POST", "/api/ai/tasks", {**task_body, "request_id": uuid4().hex,
                                                                "benchmark": {"contrast_id": saved["id"], "rules": [7]}})[0], 409)
        status, created = self.request("POST", "/api/ai/tasks", task_body)
        self.assertEqual(status, 200, created)
        task_id = created["task"]["task_id"]
        self.assertEqual([rule["text"] for rule in created["task"]["context"]["benchmark"]["rules"]],
                         ["第一秒放钝刀切番茄的结果", "证明出现：第 5 秒"])
        task = self.finish(task_id)
        self.assertIn("本轮按对标写稿", self.bridge.prompts[0])
        self.assertIn("第一秒放钝刀切番茄的结果", self.bridge.prompts[0])
        round_id = task["rounds"][-1]["round_id"]
        self.assertEqual(self.request("GET", f"/api/benchmark/check?task_id={task_id}&round_id={round_id}")[1]["status"], "none")
        status, started = self.request("POST", "/api/benchmark/check", {"task_id": task_id, "round_id": round_id})
        self.assertEqual(status, 200, started)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = self.request("GET", f"/api/benchmark/check?task_id={task_id}&round_id={round_id}")[1]
            if result["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(result["status"], "done", result)
        self.assertEqual(result["results"][0]["items"][1], {"rule": 2, "ok": False, "where": "第 3 句（约 4 秒）", "fix": "把切纸挪到第 2 句"})
        self.assertIn("第 3 句 [约 5 秒] Paper test.", next(p for p in self.bridge.prompts if "〈对标约束〉" in p))

    def test_hook_only_task_and_tasks_without_benchmark_cannot_be_checked(self):
        hook = {"id": "7550000000000000009", "author": "bladeguy", "title": "t", "stats": {},
                "hook": {"type": "质疑回应", "lines": [{"time": "00:00", "en": "Will this ruin my knives?", "zh": "会毁刀吗"}]}}
        self.server.app.hook_bank.save("202H", [hook])
        status, created = self.request("POST", "/api/ai/tasks", {"request_id": uuid4().hex, "product": "202H", "action": "variants",
                                                                 "instruction": "按参考开头写。", "benchmark": {"hook_id": "7550000000000000009"}})
        self.assertEqual(status, 200, created)
        rules = created["task"]["context"]["benchmark"]["rules"]
        self.assertEqual(len(rules), 1)
        self.assertIn("Will this ruin my knives?", rules[0]["text"])
        self.assertEqual(self.request("POST", "/api/ai/tasks", {"request_id": uuid4().hex, "product": "202H", "action": "variants",
                                                                "instruction": "x", "benchmark": {"hook_id": "7550000000000000000"}})[0], 404)
        self.bridge.__class__ = CheckBridge
        plain = self.request("POST", "/api/ai/tasks", {"request_id": uuid4().hex, "product": "202H", "action": "variants", "instruction": "普通写稿", "skill_selection": "none"})[1]["task"]
        task = self.finish(plain["task_id"])
        status, value = self.request("POST", "/api/benchmark/check", {"task_id": plain["task_id"], "round_id": task["rounds"][-1]["round_id"]})
        self.assertEqual(status, 409, value)

    def finish(self, task_id):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            task = self.request("GET", f"/api/ai/tasks/{task_id}")[1]["task"]
            if task["status"] not in {"queued", "running"}:
                self.assertEqual(task["status"], "completed", task.get("rounds", [{}])[-1].get("error"))
                return task
            time.sleep(0.05)
        self.fail("writing task did not finish")


if __name__ == "__main__":
    unittest.main()
