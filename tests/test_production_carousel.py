"""Photo carousel phase one: parse a variant's 图文版, adopt it as a script and keep a carousel production job."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))

from elevenlabs_client import VoiceError
from production_store import ProductionStore
from test_production import Bench, FakeEleven
from variant_parse import carousel_text, parse_carousels
from workbench_carousel import CarouselWorkbenchMixin

COPY = {"direction_id": "202H-S2", "content_type": "photo_carousel", "variant": "9-送礼",
        "slides": [{"number": n, "title_lines": [f"Title {n}", "line two"], "body": f"Body {n}.", "title_zh": f"标题{n}", "body_zh": f"正文{n}。"}
                   for n in (1, 2, 3)],
        "caption_en": "Gift it. #SHARPAL202H", "caption_zh": "送它。"}
CANDIDATE = "\n".join([
    "**变体 #7 · 逐件对照**", "", "**口播(配音用)**", "", "```", "One kit.", "```", "",
    "---", "", "**变体 #9 · 送礼**", "", "**口播（配音用）**", "", "```", "Gift for the knife nerd?", "Open this case.", "```", "",
    "**图文版(3 张)**", "", "| 张 | 作用 | 叠字 | 正文 | 中文 | 素材来源 | 禁写 |", "|---|---|---|---|---|---|---|",
    "| 1 | 钩子 | Title 1 / line two | Body 1. | 标题1 | 实拍(打开的箱子) | 最佳礼物 |",
    "| 2 | 内容 | Title 2 / line two | Body 2. | 标题2 | 实拍抠图 + 排版 | 最全 |",
    "| 3 | 收口 | Title 3 / line two | Body 3. | 标题3 | 实拍主体 + 生成背景 | 价格数字 |", "",
    "```json", json.dumps(COPY, ensure_ascii=False, indent=2), "```", ""])


class CarouselBench(Bench, CarouselWorkbenchMixin):
    pass


class ParseTests(unittest.TestCase):
    def test_only_variants_with_a_carousel_are_returned_with_merged_slides(self):
        found = parse_carousels(CANDIDATE)
        self.assertEqual([(item["index"], item["label"]) for item in found], [(1, "变体 #9 · 送礼")])
        slide = found[0]["slides"][0]
        self.assertEqual((slide["role"], slide["source"], slide["forbidden"], slide["title_lines"]),
                         ("钩子", "实拍(打开的箱子)", "最佳礼物", ["Title 1", "line two"]))
        self.assertEqual(found[0]["problems"], [])
        self.assertTrue(carousel_text(found[0]).startswith("01 Title 1 / line two\nBody 1."))
        self.assertIn("发布文案：送它。", carousel_text(found[0], chinese=True))

    def test_missing_table_is_reported_not_guessed(self):
        text = CANDIDATE.replace("| 张 | 作用 | 叠字", "| 编号 | 作用 | 标题")
        found = parse_carousels(text)
        self.assertEqual(found[0]["slides"][0]["source"], "")
        self.assertTrue(any("逐张表" in problem for problem in found[0]["problems"]))


class CarouselTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="production-carousel-")
        self.addCleanup(temp.cleanup)
        self.bench = CarouselBench(temp.name, FakeEleven())

    def adopt(self, text):
        source = {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 1, "product": "202H",
                  "case_id": None, "candidate_text": CANDIDATE, "candidate_sha256": "x", "snapshot": {}}
        return self.bench.creation.mutate("script", {"request_id": uuid4().hex, "revision": self.bench.creation.public()["revision"],
            "script": {"product": "202H", "direction_id": "202H-S2", "title": "送礼 · 图文", "text": text, "confirmed": True}}, source)["script"]

    def body(self, **values):
        return {"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"], **values}

    def test_adopted_carousel_becomes_a_job_and_only_making_notes_change(self):
        item = parse_carousels(CANDIDATE)[0]
        script = self.adopt(carousel_text(item))
        body = self.body(carousel={"script_id": script["id"], "variant_index": 0})  # wrong hint, found by text
        created = self.bench.create_carousel(body)["carousel"]
        self.assertEqual((created["variant_label"], created["source"]["variant_index"], created["status"]), ("变体 #9 · 送礼", 1, "draft"))
        self.assertEqual([slide["base"] for slide in created["slides"]], [None, None, None])
        self.assertEqual(self.bench.create_carousel(body)["carousel"]["id"], created["id"])
        slides = [{"number": slide["number"], "role": slide["role"], "source": "达芬奇导出", "forbidden": slide["forbidden"],
                   "title_lines": ["Changed"]} for slide in created["slides"]]
        updated = self.bench.save_carousel_slides(created["id"], self.body(slides=slides))["carousel"]
        self.assertEqual((updated["slides"][0]["source"], updated["slides"][0]["title_lines"]), ("达芬奇导出", ["Title 1", "line two"]))
        archived = self.bench.archive_carousel(created["id"], self.body(archived=True))["carousel"]
        self.assertTrue(archived["archived"])
        self.assertEqual(self.bench.production.public("202H")["carousels"][0]["id"], created["id"])
        self.assertEqual(self.bench.carousel_detail(created["id"])["script"]["id"], script["id"])

    def test_scripts_not_adopted_from_a_carousel_are_refused(self):
        script = self.adopt("Gift for the knife nerd?\nOpen this case.")
        with self.assertRaises(VoiceError) as caught:
            self.bench.create_carousel(self.body(carousel={"script_id": script["id"]}))
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.bench.production.public()["carousels"], [])

    def test_catalogs_from_before_carousels_still_load(self):
        store = self.bench.production
        store.write({key: value for key, value in store.empty().items() if key != "carousels"})
        self.assertEqual(ProductionStore(store.root.parent).public()["carousels"], [])


class CarouselHTTPTests(unittest.TestCase):
    import test_server as fixture
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def ok(self, method, path, body=None):
        status, _, data = self.request(method, path, body)
        self.assertEqual(status, 200, data)
        return data

    def test_variant_to_carousel_job_over_http(self):
        from test_ai_tasks import FakeBridge
        self.server.app.ai.bridge = FakeBridge(output=CANDIDATE)
        task = self.ok("POST", "/api/ai/tasks", {"request_id": uuid4().hex, "product": "202H", "action": "review",
                                                 "instruction": "核对资料", "skill_selection": "none"})["task"]
        deadline = time.monotonic() + 4
        while task["busy"] and time.monotonic() < deadline:
            time.sleep(.02)
            task = self.server.app.ai.get(task["task_id"])
        round_id = task["rounds"][0]["round_id"]
        found = self.ok("GET", f"/api/production/carousel-variants?task_id={task['task_id']}&round_id={round_id}")["carousels"]
        script = self.ok("POST", "/api/scripts", {"revision": self.ok("GET", "/api/creation?product=202H")["revision"], "request_id": uuid4().hex,
            "script": {"product": "202H", "title": "送礼 · 图文", "text": found[0]["text"], "translation_zh": found[0]["translation_zh"], "confirmed": True},
            "source": {"task_id": task["task_id"], "round_id": round_id, "task_revision": task["revision"]}})["script"]
        created = self.ok("POST", "/api/production/carousels", {"revision": self.ok("GET", "/api/production")["revision"], "request_id": uuid4().hex,
                                                                "carousel": {"script_id": script["id"], "variant_index": found[0]["index"]}})["carousel"]
        detail = self.ok("GET", f"/api/production/carousels/{created['id']}")
        self.assertEqual(len(detail["carousel"]["slides"]), 3)
        self.assertEqual(len(self.ok("GET", "/api/production?product=202H")["carousels"]), 1)
        self.assertEqual(self.request("GET", "/api/production/carousels/CAR-000000000000")[0], 404)


if __name__ == "__main__":
    unittest.main()
