"""图文逐张表：定稿口播稿的意群表交给图文技能，解析预览后才建图文制作单。"""
import json
from pathlib import Path
import sys
import time
import unittest
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ai_tasks import FakeBridge

COPY = {"direction_id": "202H-S2", "content_type": "photo_carousel", "variant": "#1 回应质疑",
        "slides": [{"number": n, "title_lines": [f"Hook {n}", "line two"], "body": f"Body {n}.", "title_zh": f"标题{n}", "body_zh": f"正文{n}。"}
                   for n in (1, 2, 3)],
        "caption_en": "A full kit, used as needed. #SHARPAL202H #knifesharpening", "caption_zh": "整套按需用。"}
SOURCE = "\n".join([
    "**变体 #1 · 回应质疑**", "", "**口播（配音用）**", "", "```", "Seven plates. Overkill?", "The clamp holds the angle.", "```", "",
    "**对应中文**", "", "```", "七块磨板，太夸张？", "夹具固定角度。", "```", "",
    "**意群表**", "", "| # | 口播 | 画面要证明什么 | 素材标签 | 证据 |", "|---|---|---|---|---|",
    "| 1 | Seven plates. Overkill? | 整套磨板一字排开 | 开箱 | 展示 |",
    "| 2 | The clamp holds the angle. | 夹具夹住刀、角度固定 | 组装 | 动作演示 |", ""])
TABLE = "\n".join([
    "**图文版**", "", "| 张 | 作用 | 叠字 | 正文 | 中文 | 素材来源 | 禁写 |", "|---|---|---|---|---|---|---|",
    "| 1 | 钩子 | Hook 1 / line two | Body 1. | 标题1 | 实拍，意群 1 | 最全 |",
    "| 2 | 机制 | Hook 2 / line two | Body 2. | 标题2 | 实拍，意群 2 | 免调 |",
    "| 3 | 收口 | Hook 3 / line two | Body 3. | 标题3 | 实拍主体 + 生成背景 | 价格 |", "",
    "```json", json.dumps(COPY, ensure_ascii=False), "```", ""])


class CarouselTableTests(unittest.TestCase):
    import test_server as fixture
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def setUp(self):
        self.fixture.WorkbenchHTTPTests.setUp(self)
        photo = Path(self.server.app.edit_skill_dir).parent / "tiktok-photo-commerce"
        (photo / "references").mkdir(parents=True, exist_ok=True)
        (photo / "SKILL.md").write_text("---\nname: tiktok-photo-commerce\n---\n# 图文", encoding="utf-8")
        (photo / "references" / "deliverables.md").write_text("# copy.json", encoding="utf-8")

    def ok(self, method, path, body=None):
        status, _, data = self.request(method, path, body)
        self.assertEqual(status, 200, data)
        return data

    def script(self):
        creation = self.server.app.creation
        source = {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 1, "product": "202H",
                  "case_id": None, "candidate_text": SOURCE, "candidate_sha256": "x", "snapshot": {}}
        return creation.mutate("script", {"request_id": uuid4().hex, "revision": creation.public()["revision"],
            "script": {"product": "202H", "direction_id": "202H-S2", "title": "回应质疑", "confirmed": True,
                       "text": "Seven plates. Overkill?\nThe clamp holds the angle.", "translation_zh": "七块磨板，太夸张？\n夹具固定角度。"}}, source)["script"]

    def wait(self, table_id):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            table = self.ok("GET", f"/api/production/carousel-tables/{table_id}")["table"]
            if table["status"] != "running":
                return table
            time.sleep(.05)
        self.fail("逐张表任务没有结束")

    def start(self, script, **extra):
        return self.ok("POST", "/api/production/carousel-tables",
                       {"request_id": uuid4().hex, "script_id": script["id"], **extra})["table"]

    def test_photo_skill_builds_the_table_from_the_meaning_groups_and_the_job_links_to_the_script(self):
        bridge = FakeBridge(output=TABLE)
        self.server.app.ai.bridge = bridge
        script = self.script()
        table = self.wait(self.start(script, instruction="只做 3 张")["id"])
        self.assertEqual(table["status"], "completed", table)
        self.assertEqual([slide["role"] for slide in table["carousels"][0]["slides"]], ["钩子", "机制", "收口"])
        call = bridge.calls[-1]
        self.assertEqual([item["name"] for item in call["skills"]], ["tiktok-photo-commerce"])
        for fragment in ("意群表", "整套磨板一字排开", "夹具夹住刀、角度固定", "只做 3 张", "copy.json"):
            self.assertIn(fragment, call["prompt"])
        created = self.ok("POST", "/api/production/carousels", {"revision": self.ok("GET", "/api/production")["revision"],
            "request_id": uuid4().hex, "carousel": {"script_id": script["id"], "table_id": table["id"]}})["carousel"]
        self.assertEqual((created["script_id"], created["source"]["table_id"], len(created["slides"])), (script["id"], table["id"], 3))
        self.assertEqual(created["caption_en"], COPY["caption_en"])

    def test_a_reply_without_a_parsable_table_fails_and_cannot_create_a_job(self):
        self.server.app.ai.bridge = FakeBridge(output="好的，已经做好了。")
        script = self.script()
        table = self.wait(self.start(script)["id"])
        self.assertEqual(table["status"], "failed")
        self.assertIn("逐张表", table["error"])
        status, _, data = self.request("POST", "/api/production/carousels", {
            "revision": self.ok("GET", "/api/production")["revision"], "request_id": uuid4().hex,
            "carousel": {"script_id": script["id"], "table_id": table["id"]}})
        self.assertEqual(status, 409, data)
        self.assertEqual(self.ok("GET", "/api/production")["carousels"], [])

    def test_a_table_for_another_script_is_refused(self):
        self.server.app.ai.bridge = FakeBridge(output=TABLE)
        table = self.wait(self.start(self.script())["id"])
        other = self.script()
        status, _, data = self.request("POST", "/api/production/carousels", {
            "revision": self.ok("GET", "/api/production")["revision"], "request_id": uuid4().hex,
            "carousel": {"script_id": other["id"], "table_id": table["id"]}})
        self.assertEqual(status, 409, data)

    def test_unknown_table_ids_are_not_found(self):
        self.assertEqual(self.request("GET", "/api/production/carousel-tables/TBL-000000000000")[0], 404)


if __name__ == "__main__":
    unittest.main()
