"""Product filtering is read-only and independent of a review's purchased SKU."""
import unittest
from urllib.parse import quote
import test_server as fixture
from test_review_store import build_sources


class ReviewProductContextTests(unittest.TestCase):
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        build_sources(self.runtime / "reviews" / "sources")

    def test_http_product_filter_keeps_aliases_sku_and_counts_separate(self):
        status, _, summary = self.request("GET", "/api/reviews/summary")
        self.assertEqual(status, 200)
        self.assertEqual(summary["products"], ["202H"])
        _, _, unfiltered = self.request("GET", "/api/reviews")
        _, _, same = self.request("GET", "/api/reviews?product=" + quote("SHARPAL 202H"))
        self.assertEqual(same["total"], unfiltered["total"])
        for product in ("129N", "1202H", "202H2", "' OR 1=1 --"):
            _, _, result = self.request("GET", "/api/reviews?product=" + quote(product))
            self.assertEqual(result["total"], 0)
        _, _, result = self.request("GET", "/api/reviews?product=202H&sku=" + quote("Leather Strop Refill"))
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["reviews"][0]["sku"], "Leather Strop Refill")
        self.assertEqual(self.request("GET", "/api/reviews?product=202H&product=129N")[0], 400)

    def test_nonmatching_product_does_not_change_records_or_manual_state(self):
        self.server.app.reviews.sync()
        connection = self.server.app.reviews.connect()
        try:
            before = list(connection.execute("SELECT id, product, sku FROM reviews ORDER BY id"))
            self.request("GET", "/api/reviews?product=129N")
            after = list(connection.execute("SELECT id, product, sku FROM reviews ORDER BY id"))
            self.assertEqual(before, after)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
