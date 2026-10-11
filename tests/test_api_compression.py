"""Large JSON is smaller on the wire while every API response remains fresh."""
from __future__ import annotations

import gzip
import http.client
import json
import unittest

import test_server as fixture


class APICompressionTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def read_raw(self, path, *, method="GET", encoding=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, headers={"Accept-Encoding": encoding or "identity"})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_json_gzip_roundtrip_keeps_data_and_freshness_headers(self):
        status, plain_headers, plain = self.read_raw("/api/state")
        packed_status, headers, packed = self.read_raw("/api/state", encoding="gzip")
        self.assertEqual((status, packed_status), (200, 200))
        self.assertEqual(gzip.decompress(packed), plain)
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Vary"], "Accept-Encoding")
        self.assertEqual(int(headers["Content-Length"]), len(packed))
        self.assertNotIn("Content-Encoding", plain_headers)
        self.assertLess(len(packed), len(plain) * 0.7)
        self.assertEqual(json.loads(gzip.decompress(packed))["cases"][0]["case_id"], self.identifier)

    def test_head_matches_compressed_get_without_returning_a_body(self):
        status, headers, packed = self.read_raw("/api/state", encoding="gzip")
        head_status, head_headers, body = self.read_raw("/api/state", method="HEAD", encoding="gzip")
        self.assertEqual(head_status, status)
        self.assertEqual(head_headers["Content-Encoding"], headers["Content-Encoding"])
        self.assertEqual(int(head_headers["Content-Length"]), len(packed))
        self.assertEqual(body, b"")

    def test_explicit_gzip_opt_out_wins_over_wildcard(self):
        _, _, plain = self.read_raw("/api/state")
        for encoding in ("gzip;q=0, *;q=1", "gzip;q=invalid", "gzip;q=2", "identity"):
            with self.subTest(encoding=encoding):
                _, headers, body = self.read_raw("/api/state", encoding=encoding)
                self.assertNotIn("Content-Encoding", headers)
                self.assertEqual(body, plain)
        for encoding in ("br, GZIP;q=0.5", "*;q=1"):
            with self.subTest(encoding=encoding):
                _, headers, body = self.read_raw("/api/state", encoding=encoding)
                self.assertEqual(headers["Content-Encoding"], "gzip")
                self.assertEqual(gzip.decompress(body), plain)

    def test_small_error_responses_stay_readable_and_uncached(self):
        status, headers, body = self.read_raw("/api/no-such-endpoint", encoding="gzip")
        self.assertEqual(status, 404)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Content-Encoding", headers)
        self.assertIn("error", json.loads(body))


if __name__ == "__main__":
    unittest.main()
