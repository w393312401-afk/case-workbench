"""Posts pushed by TK 看板 or found in Shop exports register their waiting timeline by themselves; disposable stores only."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creation_store import CreationStore
from elevenlabs_client import VoiceError
from production_store import ProductionStore
from workbench_publication_match import PublicationMatchMixin, headline_key, posted_on
from workbench_video_publish import VideoPublishMixin

TAKE = "TAK-111111111111"
TAUGHT, WAITING, TWIN = "PRD-111111111111", "PRD-222222222222", "PRD-333333333333"
FIRST, NEW, LATER = "7697632860966030623", "7700962284323065118", "7701324000684805406"  # 2026-10-17, 10-26, 10-27
TITLE = "🔴Give Your Wrist a Guide#SHARPAL202H #knifesharpening"


class Performance:
    def __init__(self):
        self.rows = []

    def videos_readonly(self):
        return self.rows


class Bench(PublicationMatchMixin, VideoPublishMixin):
    def __init__(self, root):
        self.lock = threading.RLock()
        self.production = ProductionStore(root, self.lock)
        self.creation = CreationStore(root, self.lock)
        self.performance = Performance()
        self.state_cache = {}
        self.titles = {}

    def _video_publish_copy(self, job):
        return {"titles": self.titles.get(job["id"], [])}

    def save_script_status(self, identifier, body, *, video_job_id=None):
        body = copy.deepcopy(body)
        body["script_status"]["script_id"] = identifier
        return self.creation.mutate("script_status", body)


def job(identifier, script, creator="Lab", built="2026-10-25"):
    uid = identifier + "-tl"
    return {"id": identifier, "product": "202H", "script_id": script, "script_version": 1, "script_family": script,
            "title": "Test", "direction_id": "S1", "variant_label": "A", "text_sha256": "frozen",
            "lines": [{"number": 1, "spoken": "Try this.", "zh": "试试这个。"}],
            "selected_take": TAKE, "takes": [{"id": TAKE, "model_id": "model", "voice_id": "voice", "text": "Try this."}],
            "timelines": [{"unique_id": uid, "name": f"{creator}261025-202-1", "creator": creator, "built_at": built + "T06:00:00+00:00",
                           "take_id": TAKE, "saved": True}],
            "edits": [], "manual_completion": {"at": built, "take_id": TAKE, "timeline_uid": uid, "basis": "human_review"},
            "archived": False, "history": []}


def pushed(post, title=TITLE, author="sharpaledgelab"):
    return {"video_id": post, "author": author, "title": title, "published_at": ""}


class PublicationMatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bench = Bench(self.root)
        data = self.bench.production.empty()
        data["jobs"] = [job(TAUGHT, "SCR-111111111111", built="2026-10-15"), job(WAITING, "SCR-222222222222")]
        self.bench.production.write(data)
        data = self.bench.creation.empty()
        data["scripts"] = [{"id": f"SCR-{n * 12}", "version": 1, "product": "202H", "status": "adopted", "video_id": "",
                            "published_on": "", "case_id": "", "history": []} for n in "123"]
        self.bench.creation.write(data)
        self.bench.titles = {TAUGHT: ["🔴Mirror Edge on an EDC Knife? Here's the Setup#SHARPAL202H"], WAITING: [TITLE, "🔴See Where the Stone Touches#x"]}
        # The creator "Lab" has posted from sharpaledgelab before: that registration teaches the account.
        detail = self.bench.video_publication_detail(TAUGHT)
        self.bench.save_video_publication(TAUGHT, {"revision": detail["revision"], "source_signature": detail["source_signature"],
                                                   "request_id": uuid4().hex, "post_id": FIRST, "published_on": "2026-10-17",
                                                   "account": "sharpaledgelab"})

    def add_job(self, identifier, script, **kwargs):
        data = self.bench.production.read()
        data["jobs"].append(job(identifier, script, **kwargs))
        self.bench.production.write(data)

    def test_headline_key_compares_the_words_before_the_hashtags_only(self):
        self.assertEqual(headline_key("🔴Mirror Edge on an EDC Knife? Here's the Setup#SHARPAL202H #knife"),
                         headline_key("mirror edge on an EDC knife — here’s the setup #other"))
        self.assertEqual(headline_key("🔴Short#tag"), "")
        self.assertEqual(posted_on(FIRST), "2026-10-17")
        self.assertEqual(posted_on("12345678", "2026-10-02T03:00:00Z"), "2026-10-02")

    def test_a_unique_post_registers_by_itself_and_undo_returns_the_script(self):
        made = self.bench.auto_register_publications([pushed(NEW)])
        self.assertEqual(made, [{"job_id": WAITING, "timeline_uid": WAITING + "-tl", "post_id": NEW}])
        record = self.bench.video_publication_detail(WAITING)["publication"]
        self.assertEqual((record["post_id"], record["published_on"], record["account"], record["calendar_uploaded"]),
                         (NEW, "2026-10-26", "sharpaledgelab", True))
        self.assertEqual(record["post_url"], f"https://www.tiktok.com/@sharpaledgelab/video/{NEW}")
        self.assertEqual((record["auto"]["source"], record["auto"]["script_updated"]), ("tk_lens", True))
        self.assertEqual(self.bench.creation.get_script("SCR-222222222222")["video_id"], NEW)
        # Pushing the same post again registers nothing new.
        self.assertEqual(self.bench.auto_register_publications([pushed(NEW)]), [])

        result = self.bench.undo_auto_video_publication(WAITING, {"publication_id": record["id"]})
        self.assertEqual(result["warning"], "")
        detail = self.bench.video_publication_detail(WAITING)
        self.assertEqual((detail["publication"], detail["history"], detail["candidates"]), (None, [], []))
        script = self.bench.creation.get_script("SCR-222222222222")
        self.assertEqual((script["video_id"], script["published_on"], script["status"]), ("", "", "adopted"))
        # An undone post is never registered to that timeline again, but the timeline still takes another post.
        self.assertEqual(self.bench.auto_register_publications([pushed(NEW)]), [])
        self.assertEqual([item["post_id"] for item in self.bench.auto_register_publications([pushed(LATER)])], [LATER])

    def test_only_an_automatic_standing_registration_can_be_undone(self):
        manual = self.bench.video_publication_detail(TAUGHT)["publication"]
        with self.assertRaises(VoiceError) as caught:
            self.bench.undo_auto_video_publication(TAUGHT, {"publication_id": manual["id"]})
        self.assertEqual(caught.exception.status, 409)

    def test_a_title_two_orders_share_waits_as_a_candidate_on_both(self):
        self.add_job(TWIN, "SCR-333333333333")
        self.bench.titles[TWIN] = [TITLE]
        self.assertEqual(self.bench.auto_register_publications([pushed(NEW)]), [])
        one, two = (self.bench.video_publication_detail(identifier)["candidates"] for identifier in (WAITING, TWIN))
        self.assertEqual([(item["post_id"], item["account_known"], len(item["also"])) for item in one], [(NEW, True, 1)])
        self.assertEqual([item["post_id"] for item in two], [NEW])
        self.bench.dismiss_publication_candidate(TWIN, {"post_id": NEW, "timeline_uid": TWIN + "-tl"})
        self.assertEqual(self.bench.video_publication_detail(TWIN)["candidates"], [])
        # With the other order out of the way the post is unique again and registers by itself.
        self.assertEqual([item["job_id"] for item in self.bench.auto_register_publications()], [WAITING])

    def test_a_creator_without_a_known_account_only_gets_a_candidate(self):
        self.add_job(TWIN, "SCR-333333333333", creator="Zack")
        self.bench.titles[TWIN] = ["🔴A Closer Look at Every Pass#x"]
        made = self.bench.auto_register_publications([pushed(NEW, "🔴A Closer Look at Every Pass#y", "zackontheedge")])
        self.assertEqual(made, [])
        [candidate] = self.bench.video_publication_detail(TWIN)["candidates"]
        self.assertEqual((candidate["account"], candidate["account_known"], candidate["source_label"]), ("zackontheedge", False, "TK 看板"))

    def test_another_account_or_a_post_older_than_the_timeline_never_matches(self):
        self.assertEqual(self.bench.auto_register_publications([pushed(NEW, author="someoneelse")]), [])
        self.assertEqual(self.bench.auto_register_publications([pushed("7697632860966030624")]), [])
        self.assertEqual(self.bench.video_publication_detail(WAITING)["candidates"], [])

    def test_only_pushed_posts_with_a_waited_for_title_are_kept(self):
        self.bench.auto_register_publications([pushed(LATER, "🔴Someone else's video about knives#x", "other"), pushed(NEW, author="someoneelse")])
        kept = json.loads((self.bench._video_publications().root / "seen-posts.json").read_text(encoding="utf-8"))["posts"]
        self.assertEqual(list(kept), [NEW])

    def test_a_shop_export_row_registers_like_a_pushed_post(self):
        self.bench.performance.rows = [{"video_id": NEW, "account": "sharpaledgelab", "title": TITLE, "posted_on": "2026-10-26"}]
        made = self.bench.auto_register_publications()
        self.assertEqual([item["post_id"] for item in made], [NEW])
        self.assertEqual(self.bench.video_publication_detail(WAITING)["publication"]["auto"]["source"], "shop_export")

    def test_watch_list_names_the_accounts_registrations_used(self):
        self.assertEqual(self.bench.publication_watch_accounts(), {"accounts": ["sharpaledgelab"]})


if __name__ == "__main__":
    unittest.main()
