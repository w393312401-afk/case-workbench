"""口播稿审查：产品专属 skill 发现、报告解析、候选稿/采用稿/制作单 HTTP 审查闭环与持久化恢复。"""
from __future__ import annotations

import threading
import time
import unittest
from uuid import uuid4

import test_ai_http as ai_http
from script_review import (ScriptReviewError, ScriptReviewStore, adoption_view, build_adoption, build_review_prompt,
                           parse_adoption, parse_review_result, skill_info)


SAMPLE_DRAFT = """**202H-S2 · 变体 #1 · 选品对比**

**开头屏显**：Modular vs All-in-One?（模块化还是整套？）

**口播（配音用）**

```
Torn between a modular sharpener and an all-in-one?
This case already packs 7 plates from coarse diamond to a leather strop.
It comes with a 4-year warranty.
```

**对应中文**

```
纠结选模块化磨刀器还是一步到位的整套？
这一箱里已经配齐 7 块磨板，从粗金刚石到皮革荡刀板。
它自带四年质保。
```

**意群表**

| # | 口播 | 画面要求 |
| --- | --- | --- |
| 1 | Torn between a modular sharpener and an all-in-one? | 开箱 7 块磨板整齐排开 |
| 2 | This case already packs 7 plates from coarse diamond to a leather strop. | 依次扫过 7 块磨板 |
| 3 | It comes with a 4-year warranty. | 整箱收纳定格 |
"""

SAMPLE_REPORT = """### 审稿模式与裁决总览

- 模式：`单稿深审`｜目标市场：`美区`｜核心人群与主 S 卖点：选品买家 / S2 整套覆盖
- 放行结论：`⚠️ 改完关键点再发`

| 关卡 | 状态 | 一句话判定（带原稿定位） |
|---|---|---|
| 1. 筛人钩子（抢注意力） | 通过 | 第 1 句精准筛出导向系统买家 |
| 2. 欲望聚焦（单点 S 卖点） | 通过 | 第 2 句点出 7 块磨板完整路径 |
| 3. 信任排雷（事实与防打假） | 需补强 | 需补画面防晃动提醒 |
| 4. 临门一脚（B 级收口） | 需补强 | 第 3 句缺少购物车行动指路 |

### 致命红线与优先修改清单

#### 1. `【转化增强/建议】` 第 3 句：补上清晰购物车指路
- 原句：`It comes with a 4-year warranty.`
- 问题与依据：有 B3 质保但缺少购物车入口提示。
- 转化影响：收口无指向会损失临门点击。
- 最小改法：
  - 英文：`It comes with a 4-year warranty — check the 202H in the cart below.`
  - 中文：它自带 4 年质保，直接点下方购物车看看 202H。

### 改动后逐句口播 × 最具效果对应画面

| 句序 | 改动后英文口播（含中文对照） | 最具效果的对应画面（动作 + 景别 + 声画配合） | 画面转化作用 & 避坑红线 |
|---|---|---|---|
| 1（钩子） | Torn between a modular sharpener and an all-in-one?<br>*中文：纠结选模块化磨刀器还是一步到位的整套？* | 俯拍开箱 7 块磨板整齐陈列 | 作用：立住秩序感；避坑：不晃镜 |
| 2 | This case already packs 7 plates from coarse diamond to a leather strop.<br>*中文：这一箱配齐 7 块磨板，从粗金刚石到皮革荡刀板。* | 依次扫过 7 块磨板 | 作用：强化配置密度；避坑：研磨膏不算第 8 块 |
| 3（收口） | It comes with a 4-year warranty — check the 202H in the cart below.<br>*中文：它自带 4 年质保，直接点下方购物车看看 202H。* | 成刃刀与 202H 稳定同框 2 秒 | 作用：清空最后顾虑；避坑：不写注册延保 |

*核对范围声明：本次仅审查文字与意群画面文本。*
"""


NEW_REPORT = """**模式：单稿深审**｜目标市场：美区｜核心人群与主 S 卖点：选品买家 / S2

**放行结论：⚠️ 改完关键点再发**

| 关卡 | 状态 | 一句话判定（带原稿定位） |
|---|---|---|
| 1. 筛人钩子（抢注意力） | 通过 | 第 1 句筛人准 |
| 2. 欲望聚焦（单点 S 卖点） | 通过 | 只讲 S2 |
| 3. 信任排雷（事实与防打假） | 需补强 | 开箱时序打架 |
| 4. 临门一脚（B 级收口） | 拦停 | 第 3 句没有购物车指路 |

## 2. 改动清单

| 编号 | 类型 | 句序 | 一句话问题 | 依据 |
|---|---|---|---|---|
| R1 | 必改 | 3 | 收口没有购物车指路 | 第四关 · 行动指路 |
| R2 | 画面 | 1 | 开箱一上来给全貌，没悬念 | 第三关 · 画面 |

### R1 · 必改 · 第 3 句：收口没有购物车指路
- **原句**：`It comes with a 4-year warranty.`
- **改法**：`It comes with a 4-year warranty. Check out the 202H below.`

## 3. 逐句采纳表

| 句序 | 改动 | 原句口播 | 改后口播 | 改后中文 | 画面 | 编号 |
|---|---|---|---|---|---|---|
| 1（钩子） | 画面 | 同原句 | Torn between a modular sharpener and an all-in-one? | 纠结选模块化磨刀器还是一步到位的整套？ | 箱盖掀开只露磨板边缘微距 | R2 |
| 2 | 未改 | 同原句 | This case already packs 7 plates from coarse diamond to a leather strop. | 这一箱里已经配齐 7 块磨板，从粗金刚石到皮革荡刀板。 | 依次扫过 7 块磨板 | |
| 3（收口） | 必改 | It comes with a 4-year warranty. | It comes with a 4-year warranty. Check out the 202H below. | 它自带四年质保。202H 就在下方。 | 整箱定格，手势指向购物车 | R1 |

## 4. 待核实备选

| 句序 | 默认用 | 可换成 | 改后中文 | 上线前核对 |
|---|---|---|---|---|
| 3 | It comes with a 4-year warranty. Check out the 202H below. | It's in the Black Friday deal, so check out the 202H below. | 它在黑五活动里，202H 就在下方。 | 挂车链接确实参加黑五 |

## 5. 拍摄细节与避坑

| 句序 | 景别 / 机位 / 声音 | 作用 | 避坑 | 素材 |
|---|---|---|---|---|
| 1 | 低角度微距，保留咔哒声 | 留悬念 | 不给整箱全貌 | 需补拍 |

**核对范围声明**：只审了文字。
"""

DRAFT_LINES = [
    {"spoken": "Torn between a modular sharpener and an all-in-one?", "zh": "纠结选模块化磨刀器还是一步到位的整套？", "proof": "开箱 7 块磨板整齐排开"},
    {"spoken": "This case already packs 7 plates from coarse diamond to a leather strop.", "zh": "这一箱里已经配齐 7 块磨板，从粗金刚石到皮革荡刀板。", "proof": "依次扫过 7 块磨板"},
    {"spoken": "It comes with a 4-year warranty.", "zh": "它自带四年质保。", "proof": "整箱收纳定格"},
]


class ScriptReviewUnitTests(unittest.TestCase):
    def test_skill_discovery_matches_configured_products_only(self):
        for product, skill in (("SHARPAL 202H", "sharpal-202h-script-review"),
                               ("SHARPAL 198H", "sharpal-198h-script-review")):
            with self.subTest(product=product):
                found = skill_info(product)
                self.assertIsNotNone(found)
                self.assertEqual(found["name"], skill)
                self.assertTrue(found["path"].endswith("SKILL.md"))
                self.assertGreaterEqual(len(found["references"]), 3)
        self.assertIsNone(skill_info("SHARPAL 162N"))
        self.assertIsNone(skill_info(""))

    def test_prompt_names_the_product_own_skill(self):
        prompt = build_review_prompt({"kind": "script", "product": "SHARPAL 198H", "market": "us",
                                      "title": "测试稿", "lines": [{"spoken": "Line one."}]})
        self.assertIn("sharpal-198h-script-review", prompt)
        self.assertNotIn("sharpal-202h-script-review", prompt)

    def test_parse_review_result_extracts_verdict_gates_spoken_and_points(self):
        parsed = parse_review_result(SAMPLE_REPORT)
        self.assertEqual(parsed["verdict"], "revise")
        self.assertEqual(parsed["verdict_label"], "改完关键点再发")
        self.assertEqual(len(parsed["gates"]), 4)
        self.assertEqual([g["key"] for g in parsed["gates"]], ["pass", "pass", "warn", "warn"])
        self.assertEqual(
            parsed["revised_spoken"].splitlines(),
            [
                "Torn between a modular sharpener and an all-in-one?",
                "This case already packs 7 plates from coarse diamond to a leather strop.",
                "It comes with a 4-year warranty — check the 202H in the cart below.",
            ],
        )
        self.assertIn("第 3 句", parsed["revision_points"])

    def test_new_report_parses_into_line_by_line_adoption(self):
        parsed = parse_review_result(NEW_REPORT)
        self.assertEqual(parsed["adopt_format"], "rows")
        self.assertEqual([row["change"] for row in parsed["adopt_rows"]], ["画面", "未改", "必改"])
        self.assertEqual(parsed["adopt_rows"][2]["original"], "It comes with a 4-year warranty.")
        self.assertEqual(parsed["adopt_rows"][2]["refs"], ["R1"])
        self.assertEqual([item["id"] for item in parsed["changes"]], ["R1", "R2"])
        self.assertEqual(parsed["alternatives"][0]["no"], "3")
        # The revised spoken lines come from 改后口播, never from 原句口播.
        self.assertEqual(parsed["revised_spoken"].splitlines()[2], "It comes with a 4-year warranty. Check out the 202H below.")
        self.assertIn("R1", parsed["revision_points"])

        view = adoption_view({"status": "done", **parsed}, DRAFT_LINES)
        self.assertTrue(view["available"])
        items = view["items"]
        self.assertEqual([item["change"] for item in items], ["画面", "未改", "必改"])
        self.assertTrue(items[0]["proof_changed"])
        self.assertFalse(items[0]["spoken_changed"])
        self.assertFalse(items[1]["proof_changed"])
        self.assertTrue(items[2]["spoken_changed"])
        self.assertEqual(items[2]["reason"], "收口没有购物车指路")
        self.assertEqual(items[2]["alternative"]["spoken"], "It's in the Black Friday deal, so check out the 202H below.")

        built = build_adoption(view, {0: {"proof": True}, 2: {"spoken": True, "proof": True}})
        self.assertEqual(built["lines"], [
            {"source": 1, "proof": "箱盖掀开只露磨板边缘微距"},
            {"source": 2},
            {"source": 3, "spoken": "It comes with a 4-year warranty. Check out the 202H below.", "zh": "它自带四年质保。202H 就在下方。",
             "proof": "整箱定格，手势指向购物车"},
        ])
        alt = build_adoption(view, {2: {"spoken": True, "alt": True}})
        self.assertEqual(alt["lines"][2]["spoken"], "It's in the Black Friday deal, so check out the 202H below.")
        self.assertNotIn("proof", alt["lines"][2])
        with self.assertRaises(ScriptReviewError):
            build_adoption(view, {})

    def test_adoption_handles_new_deleted_merged_and_legacy_reports(self):
        report = """| 句序 | 改动 | 原句口播 | 改后口播 | 改后中文 | 画面 | 编号 |
|---|---|---|---|---|---|---|
| 1+2 | 合并 | A. B. | AB. | 甲乙。 | 合并画面 | R1 |
| 2a | 新增 | （无） | New line. | 新句。 | 新画面 | R2 |
| 3 | 删除 | C. | （删除） | （删除） | | R3 |
"""
        lines = [{"spoken": "A.", "zh": "甲。", "proof": "p1"}, {"spoken": "B.", "zh": "乙。", "proof": "p2"},
                 {"spoken": "C.", "zh": "丙。", "proof": "p3"}]
        view = adoption_view({"status": "done", **parse_adoption(report)}, lines)
        self.assertEqual([item["change"] for item in view["items"]], ["合并", "新增", "删除"])
        built = build_adoption(view, {0: {"spoken": True}, 1: {"spoken": True}, 2: {"spoken": True}})
        self.assertEqual(built["lines"], [{"source": 1, "spoken": "AB.", "zh": "甲乙。"},
                                          {"source": None, "spoken": "New line.", "zh": "新句。", "proof": "新画面"}])
        kept = build_adoption(view, {1: {"spoken": True}})
        self.assertEqual([line["source"] for line in kept["lines"]], [1, 2, None, 3])

        # A table that leaves out a sentence leaves that sentence as it is.
        short = adoption_view({"status": "done", **parse_adoption(report.replace("| 3 | 删除 | C. | （删除） | （删除） | | R3 |\n", ""))}, lines)
        self.assertTrue(short["available"])
        self.assertEqual(next(item for item in short["items"] if item["sources"] == [3])["change"], "未改")
        # A sentence split into 2.1 / 2.2 to give each half a picture is one sentence again.
        split = parse_adoption("""| 句序 | 改动 | 原句口播 | 改后口播 | 改后中文 | 画面 | 编号 |
|---|---|---|---|---|---|---|
| 1 | 未改 | 同原句 | A. | 甲。 | 甲画面 | |
| 2.1 | 画面 | 同原句 | Dull knife? | 刀钝了？ | 钝刀切纸 | R3 |
| 2.2 | 画面 | 同原句 | I start here. | 我从这开始。 | 装夹推磨 | R3 |
| 3.1 | 未改 | 同原句 | Yes. | 能。 | 特写 | |
| 3.2 | 建议 | Buy one plate. | You can buy that plate alone. | 那块可以单买。 | 换板 | R2 |
""")["adopt_rows"]
        self.assertEqual([row["sources"] for row in split], [[1], [2], [3]])
        self.assertEqual(split[1]["spoken"], "Dull knife? I start here.")
        self.assertEqual(split[1]["proof"], "钝刀切纸；装夹推磨")
        self.assertEqual(split[2]["original"], "Yes. Buy one plate.")
        self.assertEqual(split[2]["change"], "建议")
        split_view = adoption_view({"status": "done", "adopt_format": "rows", "adopt_rows": split, "changes": [], "alternatives": []},
                                   [{"spoken": "A.", "zh": "甲。"}, {"spoken": "Dull knife? I start here.", "zh": "刀钝了？我从这开始。"},
                                    {"spoken": "Yes. Buy one plate.", "zh": "能。买一块。"}])
        self.assertTrue(split_view["available"], split_view)
        self.assertEqual([item["change"] for item in split_view["items"]], ["画面", "画面", "建议"])

        # The older report only lists the revised lines; with the same count they line up one to one.
        legacy = adoption_view({"status": "done", "markdown": SAMPLE_REPORT}, DRAFT_LINES)
        self.assertTrue(legacy["available"])
        self.assertEqual([item["change"] for item in legacy["items"]], ["未改", "未改", "改动"])
        self.assertFalse(any(item["proof_changed"] for item in legacy["items"]))
        # A job takes spoken changes too (they go into a new version and a new job).
        job = adoption_view({"status": "done", **parse_adoption(NEW_REPORT)}, DRAFT_LINES, job=True)
        self.assertFalse(job["items"][2]["spoken_locked"])
        both = build_adoption(job, {0: {"proof": True}, 2: {"spoken": True}})
        self.assertEqual(both["spoken"], 1)
        spoken_only = adoption_view({"status": "done", "markdown": SAMPLE_REPORT}, DRAFT_LINES, job=True)
        self.assertTrue(spoken_only["available"])

    def test_prompt_contains_script_and_screen(self):
        prompt = build_review_prompt({
            "kind": "script",
            "product": "202H",
            "market": "us",
            "title": "测试稿",
            "screen": "Modular vs All-in-One?",
            "lines": [{"spoken": "Line one.", "zh": "第一句", "proof": "开箱"}],
            "scope_note": "含意群文本",
        })
        self.assertIn("sharpal-202h-script-review", prompt)
        self.assertIn("Modular vs All-in-One?", prompt)
        self.assertIn("1. Line one.  | 中文：第一句  | 画面：开箱", prompt)


class ReviewBridge(ai_http.FakeBridge):
    def __init__(self):
        super().__init__()
        self.calls = []

    def run(self, prompt, *, cwd, on_progress, cancel_event, **options):
        self.prompts.append(prompt)
        self.calls.append({"prompt": prompt, "options": options})
        self.started.set()
        if "sharpal-202h-script-review" in prompt:
            by_model = getattr(self, "reports", None) or {}
            return by_model.get(options.get("model")) or getattr(self, "report", SAMPLE_REPORT)
        return SAMPLE_DRAFT


class ScriptReviewHTTPTests(unittest.TestCase):
    close = ai_http.AIHTTPTests.close
    request = ai_http.AIHTTPTests.request
    body = ai_http.AIHTTPTests.body
    create = ai_http.AIHTTPTests.create
    wait = ai_http.AIHTTPTests.wait

    def setUp(self):
        ai_http.AIHTTPTests.setUp(self)
        self.bridge.__class__ = ReviewBridge
        self.bridge.calls = []

    def _wait_review(self, query):
        deadline = time.time() + 10
        while time.time() < deadline:
            status, data = self.request("GET", f"/api/script-reviews?{query}")
            self.assertEqual(status, 200, data)
            latest = data.get("latest")
            if latest and latest.get("status") != "running":
                return data
            time.sleep(0.05)
        self.fail("script review did not finish in time")

    def _make_candidate_task(self, product="202H"):
        created = self.create({**self.body(), "product": product, "skill_selection": "none", "action": "variants"})
        task = self.wait(created["task_id"])
        return task, task["rounds"][0]["round_id"]

    def _save_script(self, product="202H", title="测试采用稿", text="Torn between a modular sharpener and an all-in-one?\nIt comes with a 4-year warranty.", zh="纠结选哪套？\n自带四年质保。"):
        revision = self.server.app.creation.revision(self.server.app.creation.read())
        result = self.server.app.save_script({
            "request_id": f"req-{uuid4().hex}",
            "revision": revision,
            "script": {
                "product": product,
                "title": title,
                "text": text,
                "translation_zh": zh,
                "confirmed": True,
            },
        })
        return result["script"]["id"]

    def test_candidate_script_and_job_review_lifecycle(self):
        task, round_id = self._make_candidate_task("202H")

        # 1. 候选稿发起审查
        status, started = self.request("POST", "/api/script-reviews/start", {
            "kind": "candidate",
            "task_id": task["task_id"],
            "round_id": round_id,
            "variant_index": 0,
        })
        self.assertEqual(status, 200, started)
        self.assertTrue(started["available"])
        self.assertEqual(started["skill"], "sharpal-202h-script-review")

        done = self._wait_review(f"kind=candidate&task_id={task['task_id']}&round_id={round_id}&variant_index=0")
        self.assertEqual(done["latest"]["status"], "done")
        self.assertEqual(done["latest"]["verdict"], "revise")
        self.assertFalse(done["stale"])
        self.assertEqual(len(done["history"]), 1)
        self.assertIn("Torn between a modular sharpener", done["latest"]["revised_spoken"])
        review_call = next(c for c in self.bridge.calls if "sharpal-202h-script-review" in c["prompt"])
        self.assertEqual(review_call["options"]["skills"][0]["name"], "sharpal-202h-script-review")

        # 批量 summary 接口
        key = done["target_key"]
        status, summary = self.request("GET", f"/api/script-reviews/summary?keys={key}")
        self.assertEqual(status, 200, summary)
        self.assertEqual(summary["items"][key]["verdict"], "revise")

        # 2. 保存采用稿并审查
        script_id = self._save_script("202H")
        status, _ = self.request("POST", "/api/script-reviews/start", {"kind": "script", "script_id": script_id})
        self.assertEqual(status, 200)
        script_done = self._wait_review(f"kind=script&script_id={script_id}")
        self.assertEqual(script_done["latest"]["status"], "done")
        self.assertFalse(script_done["stale"])

        # 3. 建制作单并审查
        status, prod_list = self.request("GET", "/api/production")
        self.assertEqual(status, 200, prod_list)
        status, prod = self.request("POST", "/api/production/jobs", {
            "request_id": f"req-{uuid4().hex}",
            "revision": prod_list["revision"],
            "job": {"script_id": script_id},
        })
        self.assertEqual(status, 200, prod)
        job_id = prod["job"]["id"]

        status, _ = self.request("POST", "/api/script-reviews/start", {"kind": "job", "job_id": job_id})
        self.assertEqual(status, 200)
        job_done = self._wait_review(f"kind=job&job_id={job_id}")
        self.assertEqual(job_done["latest"]["status"], "done")

        # 重启恢复：模拟残留 running 记录被重新加载后自动转为 failed
        store = ScriptReviewStore(self.runtime)
        store.upsert_run("script:interrupted", {"id": "SR-test", "status": "running", "updated_at": "2026-11-21T00:00:00+00:00"})
        recovered = ScriptReviewStore(self.runtime).get("script:interrupted")
        self.assertEqual(recovered["latest"]["status"], "failed")
        self.assertIn("重启", recovered["latest"]["error"])

    def test_adopt_review_into_candidate_script_and_job(self):
        self.bridge.report = NEW_REPORT
        task, round_id = self._make_candidate_task("202H")
        query = f"kind=candidate&task_id={task['task_id']}&round_id={round_id}&variant_index=0"
        status, _ = self.request("POST", "/api/script-reviews/start", {"kind": "candidate", "task_id": task["task_id"],
                                                                        "round_id": round_id, "variant_index": 0})
        self.assertEqual(status, 200)
        done = self._wait_review(query)
        adoption = done["latest"]["adoption"]
        self.assertTrue(adoption["available"], adoption)
        review_id = done["latest"]["id"]
        picks = [{"index": 0, "proof": True}, {"index": 2, "spoken": True, "proof": True}]
        status, result = self.request("POST", "/api/script-reviews/adopt", {"kind": "candidate", "task_id": task["task_id"],
                                                                             "round_id": round_id, "variant_index": 0,
                                                                             "review_id": review_id, "picks": picks})
        self.assertEqual(status, 200, result)
        newest = result["task"]["rounds"][-1]
        self.assertEqual(newest["status"], "completed")
        self.assertIn("采纳口播审查", newest["instruction"])
        self.assertIn("It comes with a 4-year warranty. Check out the 202H below.", newest["output"])
        self.assertIn("| 箱盖掀开只露磨板边缘微距 |", newest["output"])
        self.assertIn("| 整箱定格，手势指向购物车 |", newest["output"])
        self.assertIn("| 依次扫过 7 块磨板 |", newest["output"])
        # The same click again is the same request, not a second round.
        status, again = self.request("POST", "/api/script-reviews/adopt", {"kind": "candidate", "task_id": task["task_id"],
                                                                            "round_id": round_id, "variant_index": 0,
                                                                            "review_id": review_id, "picks": picks})
        self.assertEqual(status, 200, again)
        self.assertEqual(len(again["task"]["rounds"]), len(result["task"]["rounds"]))
        status, err = self.request("POST", "/api/script-reviews/adopt", {"kind": "candidate", "task_id": task["task_id"],
                                                                          "round_id": round_id, "variant_index": 0,
                                                                          "review_id": review_id, "picks": []})
        self.assertEqual(status, 400, err)

        script_id = self._save_script("202H", text="\n".join(line["spoken"] for line in DRAFT_LINES),
                                      zh="\n".join(line["zh"] for line in DRAFT_LINES))
        self.request("POST", "/api/script-reviews/start", {"kind": "script", "script_id": script_id})
        script_done = self._wait_review(f"kind=script&script_id={script_id}")
        status, saved = self.request("POST", "/api/script-reviews/adopt", {"kind": "script", "script_id": script_id,
                                                                            "review_id": script_done["latest"]["id"],
                                                                            "picks": [{"index": 2, "spoken": True}]})
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["script"]["version"], 2)
        self.assertTrue(saved["script"]["text"].endswith("Check out the 202H below."))
        # The review now belongs to an older version of the script.
        status, stale = self.request("POST", "/api/script-reviews/adopt", {"kind": "script", "script_id": script_id,
                                                                           "review_id": script_done["latest"]["id"],
                                                                           "picks": [{"index": 2, "spoken": True}]})
        self.assertIn(status, (200, 409), stale)
        status, prod_list = self.request("GET", "/api/production")
        status, prod = self.request("POST", "/api/production/jobs", {"request_id": f"req-{uuid4().hex}",
                                                                     "revision": prod_list["revision"], "job": {"script_id": script_id}})
        self.assertEqual(status, 200, prod)
        job_id = prod["job"]["id"]
        self.bridge.report = NEW_REPORT.replace("Check out the 202H below.", "Tap below.").replace(
            "| 3（收口） | 必改 | It comes with a 4-year warranty. |", "| 3（收口） | 必改 | It comes with a 4-year warranty. Check out the 202H below. |")
        self.request("POST", "/api/script-reviews/start", {"kind": "job", "job_id": job_id})
        job_done = self._wait_review(f"kind=job&job_id={job_id}")
        self.assertFalse(job_done["latest"]["adoption"]["items"][2]["spoken_locked"])
        job_review = job_done["latest"]["id"]
        status, job_saved = self.request("POST", "/api/script-reviews/adopt", {"kind": "job", "job_id": job_id, "review_id": job_review,
                                                                                "picks": [{"index": 0, "proof": True}]})
        self.assertEqual(status, 200, job_saved)
        self.assertEqual(job_saved["job"]["lines"][0]["proof"], "箱盖掀开只露磨板边缘微距")
        # Taking the picture does not use the review up: its spoken changes can still be taken, and the badge says so.
        status, after = self.request("GET", f"/api/script-reviews?kind=job&job_id={job_id}")
        self.assertTrue(after["latest"]["adoption"]["available"], after["latest"]["adoption"])
        self.assertTrue(after["latest"]["adopted"])
        status, summary = self.request("GET", f"/api/script-reviews/summary?keys={after['target_key']}")
        self.assertTrue(summary["items"][after["target_key"]]["adopted"])
        # Spoken changes on a job: the adopted copy gets a new version and a new job is made from it.
        status, moved = self.request("POST", "/api/script-reviews/adopt", {"kind": "job", "job_id": job_id, "review_id": job_review,
                                                                            "picks": [{"index": 2, "spoken": True}]})
        self.assertEqual(status, 200, moved)
        self.assertTrue(moved["new_job"])
        new_job = moved["job"]
        self.assertNotEqual(new_job["id"], job_id)
        self.assertEqual(moved["script"]["version"], 3)
        self.assertEqual(new_job["script_id"], moved["script"]["id"])
        self.assertTrue(new_job["lines"][2]["spoken"].endswith("Tap below."))
        self.assertEqual(new_job["lines"][0]["proof"], "箱盖掀开只露磨板边缘微距")
        status, old_job = self.request("GET", f"/api/production/jobs/{job_id}")
        old_lines = (old_job.get("job") or old_job)["lines"]
        self.assertEqual(old_lines[2]["spoken"], "It comes with a 4-year warranty.", old_lines[2])
        # The same click again opens the job made then.
        status, again = self.request("POST", "/api/script-reviews/adopt", {"kind": "job", "job_id": job_id, "review_id": job_review,
                                                                            "picks": [{"index": 2, "spoken": True}]})
        self.assertEqual(status, 200, again)
        self.assertTrue(again["repeated"])
        self.assertEqual(again["job"]["id"], new_job["id"])

    def test_older_review_stays_adoptable_while_the_spoken_lines_are_the_same(self):
        from script_review import _same_script
        record = {"status": "done", "script_hash": "old", **parse_adoption(NEW_REPORT)}
        lines = [dict(line) for line in DRAFT_LINES]
        self.assertTrue(_same_script(record, {"script_hash": "new", "lines": lines}))
        lines[0]["proof"] = "换过的画面"
        self.assertTrue(_same_script(record, {"script_hash": "newer", "lines": lines}))
        lines[1]["spoken"] = "A different line."
        self.assertFalse(_same_script(record, {"script_hash": "newest", "lines": lines}))
        legacy = {"status": "done", "script_hash": "old", "markdown": SAMPLE_REPORT}
        self.assertFalse(_same_script(legacy, {"script_hash": "new", "lines": DRAFT_LINES}))

    def test_several_models_review_at_once_and_are_taken_sentence_by_sentence(self):
        other = NEW_REPORT.replace("It comes with a 4-year warranty. Check out the 202H below.", "Four-year warranty. Tap the orange cart.").replace(
            "它自带四年质保。202H 就在下方。", "四年质保。点下方橙色购物车。").replace("| 箱盖掀开只露磨板边缘微距 |", "| 手掀箱盖慢推 |")
        self.bridge.reports = {"claude-opus": NEW_REPORT, "gpt-6.1-terra": other}
        self.bridge.models = lambda: {"models": [{"id": "claude-opus", "available": True, "reasoning_efforts": ["medium", "high"]},
                                                 {"id": "gpt-6.1-terra", "available": True, "reasoning_efforts": ["medium", "high"]}]}
        script_id = self._save_script("202H", text="\n".join(line["spoken"] for line in DRAFT_LINES),
                                      zh="\n".join(line["zh"] for line in DRAFT_LINES))
        status, started = self.request("POST", "/api/script-reviews/start", {"kind": "script", "script_id": script_id, "models": [
            {"model": "claude-opus", "reasoning_effort": "medium"}, {"model": "gpt-6.1-terra", "reasoning_effort": "high"}]})
        self.assertEqual(status, 200, started)
        deadline = time.time() + 10
        while time.time() < deadline:
            status, state = self.request("GET", f"/api/script-reviews?kind=script&script_id={script_id}")
            batch = [item for item in state["history"] if item.get("batch")]
            if len(batch) == 2 and all(item["status"] == "done" for item in batch):
                break
            time.sleep(0.05)
        self.assertEqual(len(batch), 2)
        self.assertEqual(len({item["batch"] for item in batch}), 1)
        self.assertEqual({item["model"] for item in batch}, {"claude-opus", "gpt-6.1-terra"})
        while self.server.app.script_review_busy() and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.server.app.script_review_busy())
        by_model = {item["model"]: item for item in batch}
        claude, gpt = by_model["claude-opus"], by_model["gpt-6.1-terra"]
        # The same sentence rewritten two ways cannot be taken twice.
        status, err = self.request("POST", "/api/script-reviews/adopt", {"kind": "script", "script_id": script_id, "picks": [
            {"review_id": claude["id"], "index": 2, "spoken": True}, {"review_id": gpt["id"], "index": 2, "spoken": True}]})
        self.assertEqual(status, 409, err)
        status, saved = self.request("POST", "/api/script-reviews/adopt", {"kind": "script", "script_id": script_id, "picks": [
            {"review_id": gpt["id"], "index": 2, "spoken": True}, {"review_id": claude["id"], "index": 0, "proof": True}]})
        self.assertEqual(status, 200, saved)
        self.assertTrue(saved["script"]["text"].endswith("Four-year warranty. Tap the orange cart."))
        status, after = self.request("GET", f"/api/script-reviews?kind=script&script_id={script_id}")
        self.assertTrue(all(item.get("adopted") for item in after["history"] if item.get("batch")))

    def test_reviews_of_deleted_targets_leave_with_them(self):
        self.bridge.report = NEW_REPORT
        script_id = self._save_script("202H", text="\n".join(line["spoken"] for line in DRAFT_LINES),
                                      zh="\n".join(line["zh"] for line in DRAFT_LINES))
        self.request("POST", "/api/script-reviews/start", {"kind": "script", "script_id": script_id})
        kept = self._wait_review(f"kind=script&script_id={script_id}")
        app = self.server.app
        # A review whose production job no longer exists (deleted before deletes cleaned up after themselves).
        ghost = {**kept["latest"], "id": "SR-ghost00001", "target_key": "job:PRD-0123456789ab", "kind": "job"}
        app.script_reviews.upsert_run("job:PRD-0123456789ab", ghost)
        app.activity.record("review:SR-ghost00001", kind="review", owner_id="job:PRD-0123456789ab", source_id="SR-ghost00001",
                            title="已删的制作单", status="needs_attention", phase="审完", message="改完关键点再发")
        removed = app.prune_script_reviews()
        self.assertEqual(list(removed), ["job:PRD-0123456789ab"])
        self.assertIsNone(app.script_reviews.get("job:PRD-0123456789ab"))
        self.assertNotIn("review:SR-ghost00001", [row["run_id"] for row in self._review_runs()])
        # The live script's review stays, and deleting a job forgets its reviews by key.
        self.assertIsNotNone(app.script_reviews.get(kept["target_key"]))
        self.assertEqual(app.forget_script_reviews([kept["target_key"]]), {kept["target_key"]: [kept["latest"]["id"]]})
        self.assertIsNone(app.script_reviews.get(kept["target_key"]))

    def _review_runs(self):
        return [row for row in self.server.app.activity.feed(limit=200)["items"] if row["kind"] == "review"]

    def test_review_shows_in_task_panel_until_adopted(self):
        self.bridge.report = NEW_REPORT
        task, round_id = self._make_candidate_task("202H")
        target = {"kind": "candidate", "task_id": task["task_id"], "round_id": round_id, "variant_index": 0}
        self.request("POST", "/api/script-reviews/start", target)
        done = self._wait_review(f"kind=candidate&task_id={task['task_id']}&round_id={round_id}&variant_index=0")
        key = done["target_key"]
        # The record names the writing task, not only 第 1 稿 / 变体 #1.
        self.assertIn(" · ", done["latest"]["title"])
        deadline = time.time() + 5
        while time.time() < deadline and not any(row["status"] == "needs_attention" for row in self._review_runs()):
            time.sleep(0.05)
        run = self._review_runs()[0]
        self.assertEqual((run["run_id"], run["owner_id"], run["status"], run["step"]),
                         ("review:" + done["latest"]["id"], key, "needs_attention", key))
        self.assertTrue(run["title"].startswith("口播审查（"), run["title"])
        self.assertIn("打开审查", run["message"])
        self.assertIn(f"/ai/{task['task_id']}?round={round_id}&review=", run["url"])
        self.assertFalse(run["read"])
        # A second review of the same draft settles the first one; adopting settles the second without a new alert.
        self.request("POST", "/api/script-reviews/start", target)
        second = self._wait_review(f"kind=candidate&task_id={task['task_id']}&round_id={round_id}&variant_index=0")
        deadline = time.time() + 5
        while time.time() < deadline and sum(row["status"] == "needs_attention" for row in self._review_runs()) != 1:
            time.sleep(0.05)
        first = next(row for row in self._review_runs() if row["run_id"] == run["run_id"])
        self.assertEqual((first["status"], first["phase"]), ("completed", "settled"))
        status, result = self.request("POST", "/api/script-reviews/adopt", {**target, "review_id": second["latest"]["id"],
                                                                             "picks": [{"index": 2, "spoken": True}]})
        self.assertEqual(status, 200, result)
        latest = next(row for row in self._review_runs() if row["run_id"] == "review:" + second["latest"]["id"])
        self.assertEqual(latest["status"], "completed")
        self.assertIn("已采纳", latest["message"])
        self.assertTrue(latest["read"])

        # Script and order reviews open their own pages.
        script_id = self._save_script("202H")
        self.request("POST", "/api/script-reviews/start", {"kind": "script", "script_id": script_id})
        self._wait_review(f"kind=script&script_id={script_id}")
        deadline = time.time() + 5
        while time.time() < deadline and not any(row["owner_id"] == f"script:{script_id}" and row["status"] != "running" for row in self._review_runs()):
            time.sleep(0.05)
        script_run = next(row for row in self._review_runs() if row["owner_id"] == f"script:{script_id}")
        self.assertIn(f"/content/{script_id}?review=script%3A{script_id}", script_run["url"])

    def test_reviews_from_before_the_panel_are_added_once_at_their_own_times(self):
        self.bridge.report = NEW_REPORT
        task, round_id = self._make_candidate_task("202H")
        target = {"kind": "candidate", "task_id": task["task_id"], "round_id": round_id, "variant_index": 0}
        self.request("POST", "/api/script-reviews/start", target)
        done = self._wait_review(f"kind=candidate&task_id={task['task_id']}&round_id={round_id}&variant_index=0")
        app = self.server.app
        run_id = "review:" + done["latest"]["id"]
        deadline = time.time() + 5
        while time.time() < deadline and not any(row["run_id"] == run_id and row["status"] != "running" for row in self._review_runs()):
            time.sleep(0.05)
        # As if this review ran before the panel listed reviews; plus one whose writing task was deleted since.
        app.activity.delete_run(run_id)
        app.script_reviews.upsert_run("candidate:AIT-000000000000:AIR-000000000000:0", {
            "id": "SR-orphan0001", "target_key": "candidate:AIT-000000000000:AIR-000000000000:0", "status": "done",
            "verdict": "revise", "created_at": "2026-11-21T03:00:00+00:00", "finished_at": "2026-11-21T03:05:00+00:00"})
        self.assertEqual(app.backfill_review_activity(), (1, 1))
        row = next(item for item in self._review_runs() if item["run_id"] == run_id)
        self.assertEqual((row["status"], row["owner_id"], row["read"]), ("needs_attention", done["target_key"], True))
        self.assertEqual(row["started_at"], done["latest"]["created_at"])
        self.assertTrue(row["title"].startswith("口播审查（"), row["title"])
        self.assertIn(f"/ai/{task['task_id']}?round={round_id}&review=", row["url"])
        self.assertFalse([e for e in app.activity.feed(after=0, limit=200)["events"] if e["run_id"] == run_id],
                         "a backfilled review never raises an alert")
        self.assertEqual(app.backfill_review_activity(), (0, 1))
        # Adopted before the panel: listed as done, not as waiting.
        self.request("POST", "/api/script-reviews/adopt", {**target, "review_id": done["latest"]["id"],
                                                            "picks": [{"index": 2, "spoken": True}]})
        app.activity.delete_run(run_id)
        app.backfill_review_activity()
        row = next(item for item in self._review_runs() if item["run_id"] == run_id)
        self.assertEqual(row["status"], "completed")
        self.assertIn("已采纳过", row["message"])

    def test_review_interrupted_by_restart_leaves_the_panel(self):
        from task_activity import reconcile_activity
        app = self.server.app
        app.activity.record("review:SR-restart01", kind="review", owner_id="job:PRD-0123456789ab", source_id="SR-restart01",
                            product="202H", title="口播审查 · 测试单", step="job:PRD-0123456789ab", status="running")
        reconcile_activity(app)
        row = next((item for item in self._review_runs() if item["run_id"] == "review:SR-restart01"), None)
        self.assertEqual(row["status"], "interrupted")
        self.assertIn("重新审", row["message"])

    def test_unsupported_product_returns_unavailable_and_blocks_start(self):
        script_id = self._save_script("162N", title="162N 采用稿", text="Dual grit diamond stone.", zh="双面金刚石磨石。")

        status, info = self.request("GET", f"/api/script-reviews?kind=script&script_id={script_id}")
        self.assertEqual(status, 200, info)
        self.assertFalse(info["available"])
        self.assertIsNone(info["skill"])

        status, err = self.request("POST", "/api/script-reviews/start", {"kind": "script", "script_id": script_id})
        self.assertEqual(status, 409, err)

    def test_stop_running_review(self):
        script_id = self._save_script("202H", title="可停止测试稿", text="First line.\nSecond line.", zh="第一句。\n第二句。")

        started_gate = threading.Event()
        original_run = self.bridge.run

        def slow_run(*args, **kwargs):
            started_gate.set()
            cancel_event = kwargs.get("cancel_event")
            for _ in range(100):
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("cancelled")
                time.sleep(0.02)
            return SAMPLE_REPORT

        self.bridge.run = slow_run
        try:
            status, started = self.request("POST", "/api/script-reviews/start", {"kind": "script", "script_id": script_id})
            self.assertEqual(status, 200, started)
            self.assertTrue(started_gate.wait(3))
            status, stopped = self.request("POST", "/api/script-reviews/stop", {"target_key": started["target_key"]})
            self.assertEqual(status, 200, stopped)
            final = self._wait_review(f"kind=script&script_id={script_id}")
            self.assertEqual(final["latest"]["status"], "cancelled")
        finally:
            self.bridge.run = original_run


if __name__ == "__main__":
    unittest.main()
