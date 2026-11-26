"""开头库: hooks pushed by the TK 看板 extension, their classification, and the page's read and edit endpoints."""
from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

import test_server as http
from hook_bank import HookBank, HookBankError, classify

JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 64).decode()


def video(video_id="7550000000000000001", **fields):
    return {"id": video_id, "author": "bladeguy", "title": "Will this ruin my Japanese knives?",
            "stats": {"views": 512000, "followers": 100000, "likes": 12700, "publishedAt": "2026-11-10T10:00:00Z",
                      "commerce": True, "cookie": "never"},
            "hook": {"type": "质疑回应/结果先行", "lines": [{"time": "00:00", "en": "Will this ruin my Japanese knives?", "zh": "会毁刀吗？"}],
                     "screen": "Does it ruin good knives?", "why": "说出观众的怀疑"},
            "frame": JPEG, **fields}


class HookBankTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="hook-bank-")
        self.addCleanup(temp.cleanup)
        self.bank = HookBank(Path(temp.name))

    def test_classify_maps_free_text_types(self):
        self.assertEqual(classify("质疑回应"), "质疑回应")
        self.assertEqual(classify("结果先行"), "结果先行")
        self.assertEqual(classify("痛点提问"), "痛点提问")
        self.assertEqual(classify("身份点名"), "身份点名")
        self.assertEqual(classify("演示开场"), "演示开场")
        self.assertEqual(classify(""), "其他")
        self.assertEqual(classify("ASMR"), "其他")

    def test_save_cleans_input_keeps_frame_and_manual_type(self):
        result = self.bank.save("202H", [video(), {"id": "abc"}, video("7550000000000000002", hook={"lines": []})], now="t1")
        self.assertEqual((result["saved"], result["updated"], result["invalid"]), (1, 0, 2))
        item = self.bank.list("202h")[0]
        self.assertEqual(item["type"], "质疑回应")
        self.assertNotIn("cookie", item["stats"])
        self.assertEqual(item["stats"]["published_at"], "2026-11-10T10:00:00Z")
        self.assertTrue(item["frame"])
        self.assertIsNotNone(self.bank.frame_path("7550000000000000001"))
        self.bank.set_type("7550000000000000001", "反常识")
        again = self.bank.save("202H", [video(frame="not a jpeg", stats=None)], now="t2")
        self.assertEqual((again["saved"], again["updated"]), (0, 1))
        item = self.bank.list()[0]
        self.assertEqual((item["type"], item["type_manual"], item["saved_at"], item["updated_at"]), ("反常识", True, "t1", "t2"))
        self.assertEqual(item["stats"]["views"], 512000)
        self.assertTrue(item["frame"])
        self.assertEqual(self.bank.list("999Z"), [])

    def test_rejects_bad_requests_and_deletes(self):
        for product, videos in (("", [video()]), ("202H", []), ("202H", [video()] * 51), ("202H", "x")):
            with self.assertRaises(HookBankError):
                self.bank.save(product, videos)
        with self.assertRaises(HookBankError):
            self.bank.set_type("7550000000000000001", "反常识")
        self.bank.save("202H", [video()])
        with self.assertRaises(HookBankError):
            self.bank.set_type("7550000000000000001", "不存在的手法")
        self.bank.delete("7550000000000000001")
        self.assertEqual(self.bank.list(), [])
        self.assertIsNone(self.bank.frame_path("7550000000000000001"))
        self.assertIsNone(self.bank.frame_path("../x"))


class HookBankHTTPTests(unittest.TestCase):
    setUp = http.WorkbenchHTTPTests.setUp
    stop_server = http.WorkbenchHTTPTests.stop_server
    request = http.WorkbenchHTTPTests.request

    def extension(self, body, key=None, origin="chrome-extension://abcdefghijklmnop"):
        link = self.request("GET", "/api/public-stats/link")[2]
        return self.request("POST", "/api/public-stats/hooks", body, headers={"X-TKL-Key": key or link["key"], "Origin": origin},
                            authenticated=False)

    def test_extension_push_then_page_reads_edits_and_deletes(self):
        body = {"product": "202H", "videos": [video()], "extension_version": "0.14.0"}
        self.assertEqual(self.extension(body, key="wrong")[0], 403)
        self.assertEqual(self.extension(body, origin="https://www.tiktok.com")[0], 403)
        self.assertEqual(self.extension({**body, "product": "999Z"})[0], 404)
        self.assertEqual(self.extension({**body, "cookie": "x"})[0], 400)
        status, _, saved = self.extension(body)
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["saved"], 1)
        status, _, listed = self.request("GET", "/api/hooks?product=202H")
        self.assertEqual(status, 200, listed)
        self.assertEqual(len(listed["hooks"]), 1)
        hook = listed["hooks"][0]
        self.assertEqual((hook["type"], hook["own"], hook["orders"]), ("质疑回应", False, None))
        self.assertEqual(hook["frame_url"], "/api/hooks/frame?id=7550000000000000001")
        status, headers, content = self.request("GET", hook["frame_url"])
        self.assertEqual((status, headers.get("Content-Type")), (200, "image/jpeg"))
        self.assertTrue(content.startswith(bytes([0xFF, 0xD8, 0xFF])))
        self.assertIn("质疑回应", listed["types"])
        self.assertEqual(self.request("POST", "/api/hooks/type", {"video_id": "7550000000000000001", "type": "结果先行"})[0], 200)
        self.assertEqual(self.request("GET", "/api/hooks?product=202H")[2]["hooks"][0]["type"], "结果先行")
        self.assertEqual(self.request("POST", "/api/hooks/type", {"video_id": "7550000000000000001", "type": "乱写"})[0], 400)
        self.assertEqual(self.request("POST", "/api/hooks/delete", {"video_id": "7550000000000000001"})[0], 200)
        self.assertEqual(self.request("GET", "/api/hooks")[2]["hooks"], [])
        self.assertEqual(self.request("GET", "/api/hooks/frame?id=7550000000000000001")[0], 404)


if __name__ == "__main__":
    unittest.main()
