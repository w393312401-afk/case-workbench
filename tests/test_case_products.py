"""案例产品名统一：按型号自动统一、手动选择、原始记录与索引不被改写。"""
from __future__ import annotations

import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("workbench_server_case_products", APP_ROOT / "server.py")
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)
import case_products as PRODUCTS  # noqa: E402


def snapshot(title):
    return {"title": title, "category": "转化正向参考", "conclusion": "测试结论。", "kind": "performance",
            "assessments": [{"dimension": "conversion", "polarity": "positive", "reason": "测试"}],
            "evidence": {"status": "user_reported", "source": "测试", "context": "测试"},
            "assets": {}, "highlights": [], "limitations": []}


class CanonicalTests(unittest.TestCase):
    def test_model_code_becomes_one_name(self):
        for raw in ("SHARPAL / Sharpeak 198H Electric Knife Sharpener with Diamond Wheels 3 Stage", "198H",
                    "SHARPAL 198H Electric Guided Knife Sharpener", " sharpal  198h "):
            self.assertEqual(PRODUCTS.canonical(raw), "SHARPAL 198H")

    def test_unidentified_or_competitor_names_stay(self):
        for raw in ("电动磨刀器（品牌型号未核实）", "Chef’sChoice 15XV 电动磨刀器", "历史记录为 129N 便携式硬质合金磨刀器（三件装）", ""):
            self.assertEqual(PRODUCTS.canonical(raw), raw)

    def test_typed_model_code_is_unified(self):
        self.assertEqual(PRODUCTS.clean("202h"), "SHARPAL 202H")
        self.assertIsNone(PRODUCTS.clean("  "))
        with self.assertRaises(ValueError):
            PRODUCTS.clean("x" * 121)


class CaseProductTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="case-products-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.skill, self.runtime = root / "skill", root / "runtime"
        store = SERVER.CaseStore(self.skill)
        long_name = "SHARPAL / Sharpeak 198H Electric Knife Sharpener with Diamond Wheels 3 Stage"
        self.own = store.add({"title": "自家", "product": long_name, "original": {"text": "原文", "source": "测试"},
                              "feedback": {"kind": "editorial", "text": "首次收录"}, "current": snapshot("自家")})["case_id"]
        self.rival = store.add({"title": "竞品", "product": "电动磨刀器（品牌型号未核实）", "original": {"text": "原文", "source": "测试"},
                                "feedback": {"kind": "editorial", "text": "首次收录"}, "current": snapshot("竞品")})["case_id"]
        self.index_before = (self.skill / "cases" / "index.json").read_bytes()
        self.silence = patch.object(SERVER.Handler, "log_message", lambda *args: None)
        self.silence.start()
        self.addCleanup(self.silence.stop)
        self.server = SERVER.make_server(self.skill, self.runtime, port=0, allow_open=False, performance_sources=[])
        self.app = self.server.app
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        _, state = self.request("GET", "/api/state")
        self.token = state["token"]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, body=None):
        headers, raw = {}, None
        if method == "POST":
            headers["X-Workbench-Token"] = getattr(self, "token", "")
            raw = json.dumps(body or {}).encode("utf-8")
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(raw))})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def products(self):
        _, state = self.request("GET", "/api/state")
        return {case["case_id"]: case["product"] for case in state["cases"]}

    def test_long_listing_name_reads_as_unified_name(self):
        self.assertEqual(self.products()[self.own], "SHARPAL 198H")
        status, detail = self.request("GET", f"/api/cases/{self.own}")
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["product"], "SHARPAL 198H")
        self.assertTrue(detail["product_raw"].startswith("SHARPAL / Sharpeak 198H"))
        self.assertFalse(detail["product_manual"])
        _, listed = self.request("GET", "/api/products")
        self.assertIn("198H", listed["products"])

    def test_manual_pick_applies_everywhere_and_can_be_undone(self):
        self.assertEqual(self.products()[self.rival], "电动磨刀器（品牌型号未核实）")
        status, body = self.request("POST", f"/api/cases/{self.rival}/product", {"product": "SHARPAL 198H"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["case"]["product"], "SHARPAL 198H")
        self.assertTrue(body["case"]["product_manual"])
        self.assertEqual(self.products()[self.rival], "SHARPAL 198H")
        _, listed = self.request("GET", "/api/products")
        self.assertNotIn("电动磨刀器（品牌型号未核实）", listed["products"])
        self.assertEqual(self.app.store.read_case(self.rival)["product"], "SHARPAL 198H")
        status, body = self.request("POST", f"/api/cases/{self.rival}/product", {"product": None})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["case"]["product"], "电动磨刀器（品牌型号未核实）")
        self.assertFalse(body["case"]["product_manual"])

    def test_records_and_index_keep_original_names(self):
        self.request("POST", f"/api/cases/{self.rival}/product", {"product": "SHARPAL 198H"})
        original = json.loads((self.skill / "cases" / self.rival / "original.json").read_text(encoding="utf-8"))
        self.assertEqual(original["product"], "电动磨刀器（品牌型号未核实）")
        # An append rebuilds index.json; it must still hold the stored names.
        case = self.app.store.read_case(self.own)
        self.app.store.append(self.own, {"kind": "editorial", "text": "改结论", "current": {**case["current"], "conclusion": "新结论。"}})
        index = json.loads((self.skill / "cases" / "index.json").read_text(encoding="utf-8"))
        names = {entry["case_id"]: entry["product"] for entry in index["cases"]}
        self.assertTrue(names[self.own].startswith("SHARPAL / Sharpeak 198H"))
        self.assertEqual(names[self.rival], "电动磨刀器（品牌型号未核实）")
        self.assertEqual(SERVER.CaseStore(self.skill).read_index()["cases"][0]["case_id"] in names, True)

    def test_bad_pick_is_rejected(self):
        status, body = self.request("POST", f"/api/cases/{self.rival}/product", {"product": "x" * 200})
        self.assertEqual(status, 400, body)
        status, body = self.request("POST", f"/api/cases/{self.rival}/product", {"product": 3})
        self.assertEqual(status, 400, body)

    def add_legacy_product(self, name):
        return SERVER.CaseStore(self.skill).add({
            "title": "历史产品", "product": name, "original": {"text": "原文", "source": "测试"},
            "feedback": {"kind": "editorial", "text": "首次收录"}, "current": snapshot("历史产品"),
        })["case_id"]

    def case_files(self):
        return {str(path.relative_to(self.skill)): path.read_bytes()
                for path in (self.skill / "cases").rglob("*") if path.is_file()}

    def test_existing_long_catalog_name_can_be_picked_without_rewriting_cases(self):
        name = "历史已有产品 " + "完整描述" * 35
        self.add_legacy_product(name)
        _, catalog = self.request("GET", "/api/products")
        self.assertIn(name, catalog["products"])
        before = self.case_files()

        # Direct label writes still enforce the new-name limit by default.
        with self.assertRaisesRegex(ValueError, "120"):
            self.app.store.product_labels.set(self.rival, name)
        status, body = self.request("POST", f"/api/cases/{self.rival}/product", {"product": name})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["case"]["product"], name)
        self.assertTrue(body["case"]["product_manual"])
        self.assertEqual(self.products()[self.rival], name)
        self.assertEqual(self.case_files(), before)

    def test_hidden_and_deleted_long_catalog_names_cannot_be_picked(self):
        for action in ("remove", "delete"):
            with self.subTest(action=action):
                name = action + " 历史产品 " + "完整描述" * 35
                self.add_legacy_product(name)
                _, catalog = self.request("GET", "/api/products")
                self.assertIn(name, catalog["products"])
                status, body = self.request("POST", "/api/products/catalog", {"action": action, "product": name})
                self.assertEqual(status, 200, body)
                self.assertNotIn(name, body["products"])
                before = self.case_files()

                status, body = self.request("POST", f"/api/cases/{self.rival}/product", {"product": name})
                self.assertEqual(status, 400, body)
                self.assertEqual(self.products()[self.rival], "电动磨刀器（品牌型号未核实）")
                self.assertFalse(self.app.store.product_labels.is_manual(self.rival))
                self.assertEqual(self.case_files(), before)

    def test_unknown_long_name_cannot_borrow_a_known_model_code(self):
        _, catalog = self.request("GET", "/api/products")
        self.assertIn("198H", catalog["products"])
        name = "SHARPAL 198H " + "不在目录的新标题" * 25
        status, body = self.request("POST", f"/api/cases/{self.rival}/product", {"product": name})
        self.assertEqual(status, 400, body)
        self.assertFalse(self.app.store.product_labels.is_manual(self.rival))

    def test_model_surrounded_by_long_whitespace_keeps_standard_name(self):
        name = " " * 100 + "198H" + " " * 100
        status, body = self.request("POST", f"/api/cases/{self.rival}/product", {"product": name})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["case"]["product"], "SHARPAL 198H")
        self.assertEqual(self.app.store.product_labels.picks()[self.rival], "SHARPAL 198H")


if __name__ == "__main__":
    unittest.main()
