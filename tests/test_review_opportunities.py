"""Opportunity ranking stays read-only, product-scoped, deduplicated and evidence-based."""
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from review_opportunities import opportunity_catalog
from review_store import ReviewStore, ReviewError


class ReviewOpportunityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-opportunities-")
        self.addCleanup(self.temp.cleanup)
        self.reviews = ReviewStore(Path(self.temp.name))
        self.creation = {"theme_links": [], "scripts": [], "plans": []}
        self.catalog = {"selling_points": [], "directions": []}
        self.app = SimpleNamespace(lock=threading.RLock(), reviews=self.reviews,
            creation=SimpleNamespace(public=lambda product: {key:[row for row in values if row.get("product")==product]
                                                            for key,values in self.creation.items()}),
            directions=SimpleNamespace(read=lambda:self.catalog), performance=Mock())
        self.today = date.today().isoformat()

    def review(self, identifier, text, *, topic="clamp", product="202H", platform="amazon", seen=None,
               role="verified_buyer", relevant=1):
        with closing(self.reviews.connect()) as connection:
            connection.execute("""INSERT INTO reviews
                (id, platform, product, role, text, relevant, first_seen, url) VALUES (?,?,?,?,?,?,?,?)""",
                (identifier, platform, product, role, text, relevant, self.today if seen is None else seen, "https://example.test/review"))
            if topic:
                connection.execute("INSERT INTO review_tags VALUES (?,?,?)", (identifier,"topic",topic))

    def test_empty_or_all_product_never_creates_database(self):
        for selected in ("", "all", "202H"):
            data = opportunity_catalog(self.app, selected)
            self.assertEqual(data["items"], [])
            self.assertFalse(self.reviews.database.exists())

    def test_other_products_brand_and_irrelevant_comments_never_mix(self):
        self.review("one","This clamp slips.")
        self.review("other","Different product problem.",product="129N")
        self.review("brand","Buy it now.",role="brand_account")
        self.review("off-topic","Unrelated.",relevant=0)
        data = opportunity_catalog(self.app,"SHARPAL 202H")
        self.assertEqual(data["product"],"202H")
        self.assertEqual(data["items"][0]["distinct_reviews"],1)
        self.assertEqual([row["id"] for row in data["items"][0]["evidence"]],["one"])
        self.app.performance.assert_not_called()

    def test_duplicate_old_quote_is_not_new_even_when_reimported_with_another_topic(self):
        self.review("old","The clamp slips.",seen="",topic="clamp")
        self.review("duplicate","  the   clamp slips. ",platform="tiktok_video",topic="stability")
        self.review("fresh","Will the clamp fit this blade?",topic="clamp")
        data = opportunity_catalog(self.app,"202H")
        items = {row["topic"]:row for row in data["items"]}
        self.assertEqual(items["clamp"]["recent_reviews"],1)
        self.assertEqual(items["stability"]["recent_reviews"],0)
        self.review("same-topic","THE CLAMP SLIPS.",platform="reddit")
        item = next(row for row in opportunity_catalog(self.app,"202H")["items"] if row["topic"]=="clamp")
        self.assertEqual((item["distinct_reviews"],item["duplicate_rows"],item["recent_reviews"]),(2,1,1))
        self.assertEqual({row["platform"] for row in item["source_counts"]},{"amazon","reddit"})

    def test_only_explicit_links_supply_direction_coverage_and_plans(self):
        self.review("one","Does it clamp short blades?")
        self.catalog["selling_points"]=[{"id":"point","product":"202H","code":"S1","title":"夹持"}]
        self.catalog["directions"]=[{"direction_id":"202H-S1","product":"202H","selling_point_id":"point","main_difference":"夹持演示"},
                                   {"direction_id":"129N-S1","product":"129N","selling_point_id":"point"}]
        self.creation["theme_links"]=[{"product":"202H","review_id":"one","selling_point_id":"point","role":"selling_point","target_id":"point"}]
        self.creation["scripts"]=[{"product":"202H","direction_id":"202H-S1","status":"published"},
                                  {"product":"202H","direction_id":"202H-S1","status":"adopted"},
                                  {"product":"129N","direction_id":"202H-S1","status":"published"}]
        self.creation["plans"]=[{"id":"plan","product":"202H","direction_id":"202H-S1","status":"pending","experiment":{"hypothesis":"用夹持演示回答疑问"}}]
        item=opportunity_catalog(self.app,"202H")["items"][0]
        self.assertEqual([(row["direction_id"],row["scripts"],row["published"]) for row in item["directions"]],[("202H-S1",2,1)])
        self.assertEqual(item["plans"][0]["id"],"plan")
        self.assertIn("未经核实",item["draft_instruction"])
        self.assertIn("Does it clamp short blades?",item["draft_instruction"])
        self.creation["theme_links"].clear()
        self.assertEqual(opportunity_catalog(self.app,"202H")["items"][0]["directions"],[])

    def test_current_manual_tags_ranking_and_read_only_bytes(self):
        self.review("old","Older price question",topic="price",seen=(date.today()-timedelta(days=30)).isoformat())
        self.review("new","New clamp question",topic="clamp")
        self.reviews.state.save({"version":1,"seen":{},"tags":{"new":{"topic":{"clamp":True}}},"picks":{}})
        before={path:path.read_bytes() for path in self.reviews.root.rglob("*") if path.is_file()}
        first=opportunity_catalog(self.app,"202H",limit=1)
        self.assertEqual((first["total"],first["items"][0]["topic"],first["items"][0]["manual_reviews"]),(2,"clamp",1))
        self.assertEqual(opportunity_catalog(self.app,"202H",limit=1,offset=1)["items"][0]["topic"],"price")
        self.assertEqual(before,{path:path.read_bytes() for path in self.reviews.root.rglob("*") if path.is_file()})

    def test_invalid_pagination_is_rejected(self):
        with self.assertRaises(ReviewError):
            opportunity_catalog(self.app,"202H",limit="invalid")


if __name__ == "__main__":
    unittest.main()
