"""Product list visibility survives reloads without changing case records."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import test_case_products as fixture
from case_products import ProductCatalog
from direction_store import product_key


class ProductCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="product-catalog-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "evidence" / "product-catalog.json"

    def catalog(self):
        return ProductCatalog(self.path, product_key)

    def test_unused_manual_product_can_be_hidden_restored_and_reloaded(self):
        self.catalog().change("add", "SHARPAL 205H")
        self.assertEqual(self.catalog().apply([]), (["205H"], []))

        self.catalog().change("remove", "205h")
        self.assertEqual(self.catalog().apply([]), ([], ["205H"]))

        self.catalog().change("restore", "SHARPAL 205H")
        self.assertEqual(self.catalog().apply([]), (["205H"], []))
        # A retried restore must not create duplicate rows in the persisted list.
        self.catalog().change("restore", "205H")
        self.assertEqual(self.catalog().read()["added"], ["205H"])
        self.assertEqual(self.catalog().apply([]), (["205H"], []))

    def test_previously_hidden_manual_name_is_restorable_without_source_data(self):
        self.path.parent.mkdir(parents=True)
        name = "白色电动磨刀器（品牌型号未核实）"
        self.path.write_text(json.dumps({"version": 1, "added": [], "removed": [name]},
                                        ensure_ascii=False), encoding="utf-8")
        self.catalog().change("restore", name)
        self.assertEqual(self.catalog().apply([]), ([name], []))

    def test_existing_long_name_can_be_hidden_restored_and_deleted(self):
        name = "电动磨刀器  " + "历史商品标题" * 89
        self.assertGreater(len(name), 120)
        self.catalog().change("remove", name, known_products=[name])
        self.assertEqual(self.catalog().apply([name]), ([], [name]))
        self.catalog().change("restore", name)
        self.assertEqual(self.catalog().apply([]), ([name], []))
        self.catalog().change("delete", name)
        self.assertEqual(self.catalog().apply([name]), ([], []))
        self.assertEqual(self.catalog().read()["deleted"], [name])

    def test_new_long_name_still_rejected_and_unknown_long_name_cannot_be_managed(self):
        name = "长" * 538
        for action in ("add", "remove", "restore", "delete"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "120"):
                self.catalog().change(action, name)
        self.assertFalse(self.path.exists())

    def test_deleted_name_stays_out_of_discovery_until_explicitly_added(self):
        self.catalog().change("delete", "198H")
        self.assertEqual(self.catalog().apply(["198H", "205H"]), (["205H"], []))
        self.assertEqual(self.catalog().read()["deleted"], ["198H"])
        with self.assertRaisesRegex(ValueError, "已删除"):
            self.catalog().change("restore", "SHARPAL 198H")
        self.catalog().change("add", "SHARPAL 198H")
        self.assertEqual(self.catalog().apply(["198H"]), (["198H"], []))
        self.assertEqual(self.catalog().read()["deleted"], [])

    def test_delete_hidden_clears_only_hidden_entries_and_persists_tombstones(self):
        self.catalog().change("add", "198H")
        self.catalog().change("add", "205H")
        self.catalog().change("remove", "205H")
        self.catalog().change("remove", "手动磨刀器")
        self.catalog().change("delete", "202H")
        self.assertEqual(self.catalog().change("delete_hidden"), ["205H", "手动磨刀器"])
        self.assertEqual(self.catalog().apply(["198H", "202H", "205H", "手动磨刀器"]), (["198H"], []))
        self.assertEqual(self.catalog().read(), {"added": ["198H"], "removed": [],
                                               "deleted": ["202H", "205H", "手动磨刀器"]})
        self.assertEqual(self.catalog().change("delete_hidden"), [])


class ProductCatalogHTTPTests(unittest.TestCase):
    setUp = fixture.CaseProductTests.setUp
    stop = fixture.CaseProductTests.stop
    request = fixture.CaseProductTests.request
    products = fixture.CaseProductTests.products

    def test_catalog_changes_preserve_original_records_index_and_case_products(self):
        paths = [self.skill / "cases" / "index.json", *sorted((self.skill / "cases").glob("*/original.json"))]
        originals = {path: path.read_bytes() for path in paths}
        labels = self.products()
        for action, product, visible in [
            ("add", "SHARPAL 205H", True),
            ("remove", "205H", False),
            ("restore", "205H", True),
            ("remove", "198H", False),
            ("restore", "198H", True),
        ]:
            with self.subTest(action=action, product=product):
                status, body = self.request("POST", "/api/products/catalog", {"action": action, "product": product})
                self.assertEqual(status, 200, body)
                key = product_key(product)
                self.assertEqual(key in body["products"], visible)
                self.assertEqual(key in body["removed"], not visible)
                status, listed = self.request("GET", "/api/products")
                self.assertEqual(status, 200, listed)
                self.assertEqual(key in listed["products"], visible)
                self.assertEqual(self.products(), labels)
                for path, original in originals.items():
                    self.assertEqual(path.read_bytes(), original, str(path))

    def test_long_source_name_can_be_managed_and_deleting_hidden_preserves_case_files(self):
        long_name = "历史产品标题  " + "便携磨刀器" * 106
        case_id = self.app.store.add({"title": "长名称产品", "product": long_name,
            "original": {"text": "原文不能改", "source": "测试"},
            "feedback": {"kind": "editorial", "text": "首次收录"},
            "current": fixture.snapshot("长名称产品")})["case_id"]
        # Also protect manual case labels: deletion belongs to the product list.
        self.app.store.product_labels.set(self.rival, "独立手动产品")
        labels = self.products()
        name = labels[case_id]
        self.assertGreater(len(name), 120)
        paths = [path for path in (self.skill / "cases").rglob("*") if path.is_file()]
        paths.append(self.runtime / "evidence" / "case-products.json")
        originals = {path: path.read_bytes() for path in paths}

        for action in ("remove", "restore", "delete"):
            status, body = self.request("POST", "/api/products/catalog", {"action": action, "product": name})
            self.assertEqual(status, 200, body)
            self.assertEqual(name in body["products"], action == "restore")
            self.assertEqual(name in body["removed"], action == "remove")
            self.assertEqual(name in body["deleted"], action == "delete")
        status, body = self.request("POST", "/api/products/catalog", {"action": "restore", "product": name})
        self.assertEqual(status, 400, body)
        self.assertIn("已删除", body["error"])
        for product in ("198H", "独立手动产品"):
            status, body = self.request("POST", "/api/products/catalog", {"action": "remove", "product": product})
            self.assertEqual(status, 200, body)
        status, body = self.request("POST", "/api/products/catalog", {"action": "delete_hidden"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["deleted_count"], 2)
        self.assertEqual(body["deleted_products"], ["198H", "独立手动产品"])
        self.assertEqual(body["removed"], [])
        self.assertEqual(set(body["deleted"]), {name, "198H", "独立手动产品"})
        status, listed = self.request("GET", "/api/products")
        self.assertEqual(status, 200, listed)
        self.assertEqual(listed["products"], [])
        self.assertEqual(listed["removed"], [])
        self.assertEqual(listed["deleted"], body["deleted"])
        # Force rediscovery to establish that deletion persists beyond a cache hit.
        self.app.products_cache = None
        self.assertEqual(self.request("GET", "/api/products")[1], listed)
        self.assertEqual(self.products(), labels)
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original, str(path))
        status, repeated = self.request("POST", "/api/products/catalog", {"action": "delete_hidden"})
        self.assertEqual(status, 200, repeated)
        self.assertEqual(repeated["deleted_count"], 0)

    def test_add_name_limit_remains_120_characters(self):
        status, body = self.request("POST", "/api/products/catalog", {"action": "add", "product": "名" * 120})
        self.assertEqual(status, 200, body)
        status, body = self.request("POST", "/api/products/catalog", {"action": "add", "product": "名" * 121})
        self.assertEqual(status, 400, body)
        self.assertIn("120", body["error"])


if __name__ == "__main__":
    unittest.main()
