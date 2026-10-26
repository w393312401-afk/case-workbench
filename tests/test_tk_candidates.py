"""Script directions saved by the TK 看板 extension: storage, wizard suggestions and the reference-video loop."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

import test_server as http
from tk_candidates import CandidateError, CandidateStore, clean_direction, suggestion, voiceover

REFERENCE = {"id": "7550000000000000001", "author": "knifeguy", "title": "Sharpening a $300 gyuto", "views": 2400000,
             "likes": 91000, "comments": 812, "shares": None, "saves": 4100, "publishedAt": "2026-10-01T10:00:00.000Z",
             "cookie": "never stored"}
CARD = {"id": "202H-S8", "kind": "新方向", "name": "贵刀不敢磨", "audience": "有日式贵刀的家庭厨师", "compare": "送去磨刀店",
        "chain": ["S8 贵刀风险控制", "怕磨坏 → A2 定角", "B4 价格", "收口：送修三次的钱就回来了"],
        "hookVisual": "一把缺口的日式刀放在磨刀石旁", "hookText": {"en": "Scared to sharpen your $300 knife?", "zh": "不敢磨三百刀的刀？"},
        "lines": [{"en": "This is how I stopped paying for sharpening.", "zh": "我就是这样不再花钱送磨的。"}],
        "cta": {"en": "Grab yours below.", "zh": "下面链接入手。"}, "payback": "算账值回来", "check": ["送修费是否属实"],
        "extra": "<script>"}


class CandidateStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="tk-candidates-")
        self.addCleanup(temp.cleanup)
        self.store = CandidateStore(Path(temp.name))

    def test_saves_cards_once_and_keeps_status_on_resave(self):
        first = self.store.add("SHARPAL 202H", REFERENCE, [CARD, {**CARD, "id": "202H-S3", "name": "定角"}])
        self.assertEqual((first["saved"], first["duplicate"]), (2, 0))
        identifier = first["candidates"][0]["candidate_id"]
        self.store.set_status(identifier, "dismissed")
        again = self.store.add("SHARPAL 202H", {**REFERENCE, "views": 2500000}, [CARD])
        self.assertEqual((again["saved"], again["duplicate"], again["candidates"][0]["status"]), (0, 1, "dismissed"))
        listed = self.store.list("202H")
        self.assertEqual(len(listed), 2)
        saved = next(item for item in listed if item["candidate_id"] == identifier)
        self.assertEqual(saved["reference"]["views"], 2500000)
        self.assertNotIn("cookie", saved["reference"])
        self.assertNotIn("extra", saved["direction"])
        self.assertEqual(self.store.counts("202H"), {"new": 1, "adopted": 0, "dismissed": 1, "writing": 0, "produced": 0})
        self.assertEqual(self.store.list("999Z"), [])

    def test_rejects_bad_input(self):
        for reference, cards in ((REFERENCE, []), ({"id": "abc"}, [CARD]), (REFERENCE, [{"name": "only a name"}]),
                                 (REFERENCE, [CARD] * 9)):
            with self.assertRaises(CandidateError):
                self.store.add("202H", reference, cards)
        with self.assertRaises(CandidateError):
            self.store.set_status("TKC-000000000000", "adopted", "202H-S8")
        identifier = self.store.add("202H", REFERENCE, [CARD])["candidates"][0]["candidate_id"]
        with self.assertRaises(CandidateError):
            self.store.set_status(identifier, "adopted")
        with self.assertRaises(CandidateError):
            self.store.set_status(identifier, "adopted", "202H-S9", direction_exists=lambda product, direction: False)
        adopted = self.store.set_status(identifier, "adopted", "202H-S8", direction_exists=lambda product, direction: True)
        self.assertEqual((adopted["status"], adopted["direction_id"]), ("adopted", "202H-S8"))
        self.assertEqual(self.store.set_status(identifier, "new")["direction_id"], "202H-S8")

    def test_voiceover_and_produced_link(self):
        self.assertEqual(voiceover(clean_direction(CARD)), (["This is how I stopped paying for sharpening.", "Grab yours below."],
                                                            ["我就是这样不再花钱送磨的。", "下面链接入手。"]))
        partial = clean_direction({**CARD, "lines": [{"en": "One.", "zh": "一。"}, {"en": "Two."}, {"zh": "只有中文"}],
                                   "cta": {"en": "Two.", "zh": "二。"}})
        self.assertEqual(voiceover(partial), (["One.", "Two."], []))
        identifier = self.store.add("202H", REFERENCE, [CARD])["candidates"][0]["candidate_id"]
        with self.assertRaises(CandidateError):
            self.store.set_status(identifier, "produced")
        self.store.link(identifier, script_id="SCR-000000000001")
        self.assertEqual(self.store.get(identifier)["status"], "new")
        linked = self.store.link(identifier, job_id="PRD-000000000001")
        self.assertEqual((linked["status"], linked["script_id"], linked["job_id"]), ("produced", "SCR-000000000001", "PRD-000000000001"))
        self.assertEqual(self.store.counts("202H")["produced"], 1)

    def test_suggestion_splits_lead_concern_and_closing(self):
        value = suggestion(clean_direction(CARD))
        self.assertEqual(value, {"code": "S8", "concern": "怕磨坏 → A2 定角 → B4 价格", "closing": "送修三次的钱就回来了",
                                 "audience": "有日式贵刀的家庭厨师", "execution_change": "贵刀不敢磨"})
        bare = suggestion(clean_direction({"lines": [{"en": "x"}], "chain": ["顾虑：太贵"], "cta": {"zh": "现在下单"}}))
        self.assertEqual((bare["code"], bare["concern"], bare["closing"]), ("", "顾虑：太贵", "现在下单"))


class CandidateHTTPTests(unittest.TestCase):
    setUp = http.WorkbenchHTTPTests.setUp
    stop_server = http.WorkbenchHTTPTests.stop_server
    request = http.WorkbenchHTTPTests.request

    def extension(self, path, body, key=None):
        status, _, link = self.request("GET", "/api/public-stats/link")
        headers = {"X-TKL-Key": key or link["key"], "Origin": "chrome-extension://abcdefghijklmnop"}
        return self.request("POST", path, body, headers=headers, authenticated=False)

    def test_extension_saves_candidates_and_sees_what_they_became(self):
        products = self.skill / "references" / "products"
        products.mkdir(parents=True, exist_ok=True)
        (products / "202H-selling-point-grading.md").write_text(
            "# 202H\n\n| 编号 | 卖点 | 为什么是这一级 | 依据状态 | 状态 |\n|---|---|---|---|---|\n"
            "| S8 | 贵刀风险控制 | 对不放心把好刀交给拉槽的人 | 已核 | 在测 |\n", encoding="utf-8")
        body = {"product": "202H", "reference": REFERENCE, "directions": [CARD], "extension_version": "0.7.0"}
        self.assertEqual(self.extension("/api/public-stats/direction-candidates", body, key="wrong")[0], 403)
        self.assertEqual(self.request("POST", "/api/public-stats/direction-candidates", body,
                                      headers={"Origin": "https://www.tiktok.com"}, authenticated=False)[0], 403)
        self.assertEqual(self.extension("/api/public-stats/direction-candidates", {**body, "product": "999Z"})[0], 404)
        self.assertEqual(self.extension("/api/public-stats/direction-candidates", {**body, "x": 1})[0], 400)
        status, _, saved = self.extension("/api/public-stats/direction-candidates", body)
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["saved"], 1)
        identifier = saved["candidates"][0]["candidate_id"]

        status, _, listed = self.request("GET", "/api/tk-candidates?product=202H")
        self.assertEqual(status, 200, listed)
        self.assertEqual((listed["counts"]["new"], listed["candidates"][0]["suggestion"]["code"]), (1, "S8"))
        self.assertEqual(self.request("GET", "/api/tk-candidates?product=202H&x=1")[0], 400)

        # Adopting needs the page token and a direction that exists.
        self.assertEqual(self.request("POST", "/api/tk-candidates/status", {"candidate_id": identifier, "status": "dismissed"},
                                      authenticated=False)[0], 403)
        status, _, missing = self.request("POST", "/api/tk-candidates/status",
                                          {"candidate_id": identifier, "status": "adopted", "direction_id": "202H-S8"})
        self.assertEqual(status, 404, missing)
        status, _, catalog = self.request("GET", "/api/direction-catalog?product=202H")
        self.assertEqual(status, 200, catalog)
        point = next(item for item in catalog["selling_points"] if item["code"] == "S8")
        status, _, created = self.request("POST", "/api/direction-catalog", {
            "revision": catalog["revision"], "request_id": uuid4().hex, "operation": "add_variant",
            "direction": {"product": "202H", "selling_point_id": point["id"], "main_difference": "S8 贵刀风险控制",
                          "concern": "怕磨坏 → S8 贵刀风险控制", "closing": "送修三次的钱就回来了", "status": "testing"}})
        self.assertEqual(status, 200, created)
        status, _, adopted = self.request("POST", "/api/tk-candidates/status",
                                          {"candidate_id": identifier, "status": "adopted", "direction_id": "202H-S8"})
        self.assertEqual((status, adopted["candidate"]["status"]), (200, "adopted"), adopted)

        status, _, reference = self.extension("/api/public-stats/reference-status", {"video_id": REFERENCE["id"]})
        self.assertEqual(status, 200, reference)
        self.assertEqual([(item["code"], item["status"], item["direction_id"]) for item in reference["candidates"]],
                         [("202H-S8", "adopted", "202H-S8")])
        self.assertEqual(reference["directions"][0]["direction_id"], "202H-S8")
        self.assertIn("video_count", reference["directions"][0])
        self.assertEqual(self.extension("/api/public-stats/reference-status", {"video_id": "abc"})[0], 400)
        self.assertEqual(self.extension("/api/public-stats/reference-status", {"video_id": REFERENCE["id"], "x": 1})[0], 400)

    def test_card_goes_straight_to_a_production_job_with_its_own_lines(self):
        body = {"product": "202H", "reference": REFERENCE, "direction": CARD, "extension_version": "0.11.0"}
        self.assertEqual(self.extension("/api/public-stats/direction-produce", body, key="wrong")[0], 403)
        self.assertEqual(self.extension("/api/public-stats/direction-produce", {**body, "x": 1})[0], 400)
        status, _, made = self.extension("/api/public-stats/direction-produce", body)
        self.assertEqual(status, 200, made)
        self.assertNotIn("writing", made)
        self.assertEqual(made["candidate"]["status"], "produced")

        # The 口播 and 中文 are the card's, word for word; 开头画面 and 屏显 come along; 画面要求 waits for the job page.
        status, _, detail = self.request("GET", f"/api/production/jobs/{made['job']['id']}")
        self.assertEqual(status, 200, detail)
        job = detail["job"]
        self.assertEqual([line["spoken"] for line in job["lines"]], ["This is how I stopped paying for sharpening.", "Grab yours below."])
        self.assertEqual([line["zh"] for line in job["lines"]], ["我就是这样不再花钱送磨的。", "下面链接入手。"])
        self.assertEqual([line["proof"] for line in job["lines"]], ["", ""])
        self.assertEqual(job["supplement"]["hook_visual"], "一把缺口的日式刀放在磨刀石旁")
        self.assertIn("Scared to sharpen your $300 knife?", detail["variant"]["markdown"])
        self.assertEqual(job["direction_id"], "")
        status, _, script = self.request("GET", f"/api/scripts/{made['script_id']}")
        self.assertEqual((script.get("script", script)["title"]), "202H-S8 贵刀不敢磨")

        # Again, from the page or the extension: the same job comes back instead of a second one.
        identifier = made["candidate"]["candidate_id"]
        self.assertEqual(self.request("POST", "/api/tk-candidates/produce", {"candidate_id": identifier}, authenticated=False)[0], 403)
        status, _, again = self.request("POST", "/api/tk-candidates/produce", {"candidate_id": identifier})
        self.assertEqual((status, again["job"]["id"], again["reused"]), (200, made["job"]["id"], True), again)
        self.assertEqual(self.extension("/api/public-stats/direction-produce", body)[2]["job"]["id"], made["job"]["id"])
        status, _, listed = self.request("GET", "/api/tk-candidates?product=202H")
        self.assertEqual((listed["counts"]["produced"], listed["candidates"][0]["job_id"]), (1, made["job"]["id"]))
        status, _, reference = self.extension("/api/public-stats/reference-status", {"video_id": REFERENCE["id"]})
        self.assertEqual(reference["candidates"][0]["status"], "produced")

        silent = {**CARD, "name": "只有中文", "lines": [{"zh": "只有中文"}], "cta": {"zh": "下单"}}
        status, _, refused = self.extension("/api/public-stats/direction-produce", {**body, "direction": silent})
        self.assertEqual(status, 400, refused)

if __name__ == "__main__":
    unittest.main()
