"""Loading regressions: validated record reuse, small product inventory and static caching."""
from __future__ import annotations

import gzip
import re
import unittest
from unittest.mock import patch

import test_server as fixture
from test_review_store import build_sources

SERVER = fixture.SERVER


class BackendLoadingTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def test_case_cache_reuses_validation_and_returns_independent_snapshots(self):
        cached = SERVER.CachedCaseStore(self.skill)
        original_read = SERVER.CaseStore.read_case
        with patch.object(SERVER.CaseStore, "read_case", autospec=True, side_effect=original_read) as read:
            first = cached.read_case(self.identifier)
            first["current"]["conclusion"] = "Unsaved local edit"
            second = cached.read_case(self.identifier)
            self.assertNotEqual(second["current"]["conclusion"], first["current"]["conclusion"])
            self.assertEqual(read.call_count, 1)
            current = second["current"]
            current["conclusion"] = "External append"
            self.store.append(self.identifier, {"kind": "editorial", "text": "External edit", "current": current})
            before = read.call_count
            self.assertEqual(cached.read_case(self.identifier)["current"]["conclusion"], "External append")
            self.assertEqual(read.call_count - before, 1)

    def test_case_cache_does_not_hide_invalid_events_or_originals(self):
        cached = SERVER.CachedCaseStore(self.skill)
        cached.read_case(self.identifier)
        unexpected = self.store.folder(self.identifier) / "events" / "unexpected.txt"
        unexpected.write_text("invalid event", encoding="utf-8")
        with self.assertRaises(SERVER.StoreError):
            cached.read_case(self.identifier)
        unexpected.unlink()
        cached.read_case(self.identifier)
        original = self.store.folder(self.identifier) / "original.json"
        original.write_text("{}", encoding="utf-8")
        with self.assertRaises(SERVER.StoreError):
            cached.read_case(self.identifier)

    def test_state_reuses_shared_direction_catalog_and_detects_grading_changes(self):
        app = self.server.app
        with patch.object(app.directions, "sync", wraps=app.directions.sync) as sync:
            app.state()
            app.state()
            self.assertLessEqual(sync.call_count, 1)
            folder = self.skill / "references" / "products"
            folder.mkdir(parents=True)
            (folder / "129N-selling-point-grading.md").write_text(
                "| 编号 | 卖点 | 用途 | 依据 |\n| S1 | New point | Fixture | Supplied |\n", encoding="utf-8")
            changed = app.state()
            self.assertGreaterEqual(sync.call_count, 1)
            self.assertTrue(any(point["product"] == "129N" for point in app.directions.read()["selling_points"]))
            self.assertTrue(changed["digest"])

    def test_products_use_distinct_inventory_without_loading_full_modules(self):
        app = self.server.app
        direction = app.directions.empty()
        direction["selling_points"].append({"product": "SHARPAL 129N"})
        app.directions.write(direction, backup=False)
        creation = app.creation.empty()
        creation["materials"].append({"product": "SHARPAL 333H"})
        creation["scripts"].append({"product": "SHARPAL 444H"})
        app.creation.write(creation, backup=False)
        connection = app.performance.connect()
        connection.execute("INSERT INTO daily (video_id, date, products) VALUES ('7600000000000000001', '2026-10-01', 'SHARPAL 555H Fixture')")
        connection.execute("INSERT INTO periods (video_id, range_start, range_end, span_days, products) VALUES ('7600000000000000002', '2026-10-01', '2026-10-07', 7, 'SHARPAL 666H Fixture')")
        connection.close()
        with patch.object(app.performance, "videos", side_effect=AssertionError("full sales list")), \
             patch.object(app.reviews, "summary", side_effect=AssertionError("full review summary")), \
             patch.object(app, "direction_catalog", side_effect=AssertionError("direction analytics")), \
             patch.object(app.creation, "public", side_effect=AssertionError("full creation list")):
            status, _, result = self.request("GET", "/api/products")
        self.assertEqual(status, 200)
        self.assertEqual(result["products"], ["129N", "202H", "333H", "444H", "555H", "666H"])
        self.assertIs(app.products(), app.products())
        creation["materials"].append({"product": "777H"})
        app.creation.write(creation, backup=False)
        self.assertIn("777H", self.request("GET", "/api/products")[2]["products"])

    def test_product_discovery_keeps_fresh_review_sources_and_does_not_create_sales_db(self):
        app = self.server.app
        self.assertFalse(app.performance.database.exists())
        self.assertFalse(app.reviews.database.exists())
        self.request("GET", "/api/products")
        self.assertFalse(app.performance.database.exists())
        self.assertFalse(app.reviews.database.exists())
        build_sources(app.reviews.sources)
        with patch.object(app.reviews, "sync", wraps=app.reviews.sync) as sync:
            result = self.request("GET", "/api/products")[2]
            self.assertIn("202H", result["products"])
            self.assertEqual(sync.call_count, 1)
            self.assertTrue(app.reviews.database.exists())
            self.request("GET", "/api/products")
            self.assertEqual(sync.call_count, 1)
        self.assertFalse(app.performance.database.exists())

    def test_static_gzip_roundtrip_head_and_conditional_revalidation(self):
        status, plain_headers, plain = self.request("GET", "/app.js")
        self.assertEqual(status, 200)
        status, headers, packed = self.request("GET", "/app.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "private, no-cache")
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertEqual(headers["Vary"], "Accept-Encoding")
        self.assertEqual(gzip.decompress(packed), plain)
        self.assertLess(len(packed), len(plain) * 0.6)
        self.assertNotEqual(plain_headers["ETag"], headers["ETag"])
        status, head, body = self.request("HEAD", "/app.js", headers={"Accept-Encoding": "gzip"})
        self.assertEqual((status, int(head["Content-Length"]), body), (200, len(packed), b""))
        status, checked, body = self.request("GET", "/app.js", headers={"Accept-Encoding": "gzip", "If-None-Match": "W/" + headers["ETag"]})
        self.assertEqual((status, body), (304, b""))
        self.assertEqual(checked["ETag"], headers["ETag"])
        _, uncompressed, body = self.request("GET", "/app.js", headers={"Accept-Encoding": "gzip;q=0, *;q=1"})
        self.assertNotIn("Content-Encoding", uncompressed)
        self.assertEqual(body, plain)

    def test_index_versions_assets_and_versioned_assets_are_immutable(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "private, no-cache")
        page = body.decode("utf-8")
        self.assertRegex(page, r'src="/app\.js\?v=[0-9a-f]{12}"')
        self.assertNotRegex(page, r'(?:src|href)="/[A-Za-z0-9_.-]+\.(?:js|css)"')
        version = re.search(r'src="/app\.js\?v=([0-9a-f]{12})"', page)[1]
        status, asset, _ = self.request("GET", f"/app.js?v={version}")
        self.assertEqual(status, 200)
        self.assertEqual(asset["Cache-Control"], "private, max-age=31536000, immutable")
        status, plain, _ = self.request("GET", "/app.js")
        self.assertEqual(plain["Cache-Control"], "private, no-cache")

    def test_static_changed_file_invalidates_body_and_etag(self):
        temporary = self.root / "static-fixture"
        (temporary / "static").mkdir(parents=True)
        path = temporary / "static" / "app.js"
        path.write_bytes(b"first fixture")
        with patch.object(SERVER, "APP_ROOT", temporary):
            _, first_headers, first = self.request("GET", "/app.js")
            path.write_bytes(b"second fixture with changed size")
            status, second_headers, second = self.request("GET", "/app.js", headers={"If-None-Match": first_headers["ETag"]})
        self.assertEqual(status, 200)
        self.assertNotEqual(first_headers["ETag"], second_headers["ETag"])
        self.assertNotEqual(first, second)
        self.assertEqual(second, path.read_bytes())

    def test_media_ranges_and_api_responses_keep_no_store(self):
        path = f"/media/{self.identifier}/video"
        status, headers, content = self.request("GET", path, headers={"Accept-Encoding": "gzip", "Range": "bytes=12-29"})
        self.assertEqual(status, 206)
        self.assertEqual(content, self.video_bytes[12:30])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Content-Encoding", headers)
        self.assertEqual(self.request("GET", "/api/state")[1]["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
