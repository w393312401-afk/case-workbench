"""Durable plugin comments, identities, product separation and review-state recovery."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import plugin_comments
from review_store import ReviewError, ReviewStore, review_id

VIDEO = "7654572167790611743"
OTHER = "7679604579134197006"
UNKNOWN = "7683152075529653518"
CID = "7654572167790611001"
REPLY = "7654572167790611002"


def comment(identifier=CID, **fields):
    return {"id": identifier, "videoId": VIDEO, "text": "Does it hold a short blade?", "likes": 0, **fields}


def video(*comments, identifier=VIDEO, **fields):
    return {"id": identifier, "author": "demonstrator", "comments": list(comments), **fields}


class PluginCommentsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="plugin-comments-")
        self.addCleanup(self.temp.cleanup)
        self.store = ReviewStore(Path(self.temp.name) / "runtime")
        self.path = self.store.sources / "tiktok_video" / plugin_comments.SOURCE_NAME

    def push(self, *comments, products=None):
        return self.store.ingest_comments([video(*comments)], products if products is not None else {VIDEO: "202H"})

    def record(self, identifier=CID):
        return self.store.review(review_id("tiktok_video", identifier))

    def raw(self, rows, name="z-legacy.json"):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        target = self.path.parent / name
        target.write_text(json.dumps(rows), encoding="utf-8")
        return target

    def test_partial_capture_is_idempotent_and_later_author_is_backfilled(self):
        result = self.push(comment(), comment(REPLY, parentId=CID, text="A useful question."))
        self.assertEqual((result["saved"], result["updated"], result["invalid"]), (2, 0, 0))
        self.assertEqual((self.record()["author_name"], self.record()["likes"]), ("", 0))
        signature, revision = self.path.stat().st_mtime_ns, self.store.revision()
        self.assertEqual(self.push(comment())["unchanged"], 1)
        self.assertEqual((self.path.stat().st_mtime_ns, self.store.revision()), (signature, revision))
        self.assertEqual(self.push(comment(authorName="Knife Friend", authorHandle="@knife.friend", createdAt=1792886400))["updated"], 1)
        before = self.record()
        self.assertEqual((before["author_name"], before["author_handle"], before["likes"]), ("Knife Friend", "knife.friend", 0))
        self.assertTrue(before["posted_on"])
        self.assertEqual(self.push(comment(authorName="", authorHandle="", likes=None))["unchanged"], 1)
        self.assertEqual(self.store.search({})["total"], 2)
        self.assertEqual(self.record(REPLY)["parent_id"], before["id"])
        self.assertNotEqual(self.store.revision(), revision)

    def test_existing_author_and_manual_state_survive_cross_source_merge_and_rebuild(self):
        self.raw([{"cid": CID, "videoWebUrl": f"https://www.tiktok.com/@demonstrator/video/{VIDEO}",
                   "text": "Does it hold a short blade?", "diggCount": 8, "uniqueId": "old.public.handle",
                   "nickname": "Original Public Name", "createTimeISO": "2026-09-01T12:00:00Z", "uid": "private-id", "avatar": "private-picture"}])
        self.store.sync()
        identifier = self.record()["id"]
        self.store.set_tag(identifier, "topic", "price", True)
        self.store.set_pick(identifier, True, "Keep my hand-written note")
        before = self.record()
        self.assertEqual(self.push(comment(), comment(REPLY, parentId=CID, authorName="Reply Name"))["updated"], 1)
        actual = self.record()
        self.assertEqual((actual["author_name"], actual["author_handle"], actual["likes"]), ("Original Public Name", "old.public.handle", 0))
        for key in ("id", "first_seen", "pick", "manual"):
            self.assertEqual(actual[key], before[key])
        self.assertIn("price", actual["tags"]["topic"])
        self.assertEqual(actual["replies"]["total"], 1)
        self.assertEqual(self.store.search({})["total"], 2)
        saved_source = self.path.read_text(encoding="utf-8")
        self.assertNotIn("private-id", saved_source)
        self.assertNotIn("private-picture", saved_source)
        self.store.sync(force=True)
        self.assertEqual(self.record(), actual)
        self.assertEqual(self.record(REPLY)["parent_id"], identifier)

    def test_later_export_recovers_reply_and_creator_role_from_anonymous_plugin_cache(self):
        self.push(comment(), comment(REPLY, text="Thanks for watching!"))
        self.raw([{"cid": REPLY, "text": "Thanks for watching!", "uniqueId": "bushcraftedge",
                   "repliesToId": CID, "videoWebUrl": f"https://www.tiktok.com/@bushcraftedge/video/{VIDEO}"}])
        self.store.sync()
        reply = self.record(REPLY)
        self.assertTrue(reply["is_reply"])
        self.assertEqual(reply["parent_id"], self.record()["id"])
        self.assertEqual(reply["author_handle"], "bushcraftedge")
        self.assertEqual(reply["role"], "brand_account")
        self.assertEqual(self.store.search({})["total"], 1)

    def test_unknown_product_is_explicit_and_never_defaults_to_202h(self):
        result = self.store.ingest_comments([
            video(comment()),
            video(comment(REPLY, videoId=OTHER, text="Does the 195H fit this knife?"), identifier=OTHER),
            video(comment("7654572167790611003", videoId=UNKNOWN, text="Where can I buy this?"), identifier=UNKNOWN),
        ], {VIDEO: "202H", OTHER: "195H", UNKNOWN: ""})
        self.assertEqual(result["saved"], 3)
        self.assertEqual(self.store.search({})["total"], 3)
        self.assertEqual(self.store.search({"product": "202H"})["total"], 1)
        self.assertEqual(self.store.search({"product": "195H"})["total"], 1)
        self.assertEqual(self.record("7654572167790611003")["product"], "")
        self.store.sync(force=True)
        self.assertEqual(self.record("7654572167790611003")["product"], "")
        self.assertTrue(self.store.covers("SHARPAL 195H"))
        self.assertFalse(self.store.covers(""))
        self.assertFalse(self.store.covers("303H"))
        self.store.set_pick(self.record(REPLY)["id"], True, "195H only")
        default = self.store.digest()
        other = self.store.digest(product="SHARPAL 195H")
        self.assertEqual((default["product"], default["reviews"], other["product"], other["reviews"]), ("202H", 1, "195H", 1))
        self.assertNotIn("195H only", json.dumps(default))
        self.assertIn("195H only", json.dumps(other))
        self.assertNotIn("Where can I buy", json.dumps(default) + json.dumps(other))

    def test_legacy_absent_product_defaults_but_explicit_empty_stays_unknown(self):
        self.raw([{"cid": CID, "text": "Question one?", "videoWebUrl": f"https://www.tiktok.com/@a/video/{VIDEO}"},
                  {"cid": REPLY, "text": "Question two?", "product": "", "videoWebUrl": f"https://www.tiktok.com/@a/video/{OTHER}"}])
        self.store.sync()
        self.assertEqual((self.record()["product"], self.record(REPLY)["product"]), ("202H", ""))
        self.assertEqual(self.store.search({"product": "202H"})["total"], 1)

    def test_digest_pick_limit_is_applied_within_the_selected_product(self):
        self.push(comment())
        self.store.set_pick(self.record()["id"], True, "Older 202H pick")
        newer = [comment(str(int(CID) + n), videoId=OTHER, text=f"Question {n} about the other product?") for n in range(1, 10)]
        self.store.ingest_comments([video(*newer, identifier=OTHER)], {OTHER: "195H"})
        for row in newer:
            self.store.set_pick(self.record(row["id"])["id"], True, "Newer 195H pick")
        self.assertEqual([r["note"] for r in self.store.digest()["user_picks"]], ["Older 202H pick"])
        self.assertEqual(len(self.store.digest(product="195H")["user_picks"]), 8)

    def test_only_video_ids_absent_from_authoritative_map_are_ignored(self):
        result = self.push(comment(), comment(REPLY), products={})
        self.assertEqual((result["ignored"], result["ignored_ids"], result["saved"]), (2, [VIDEO], 0))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.push(comment(), products={VIDEO: ""})["saved"], 1)

    def test_bad_comments_do_not_block_valid_comments_or_write_private_fields(self):
        bad = [comment(id="not-cid"), comment(videoId=OTHER), comment(text=""), comment(text="x" * 10001),
               comment(likes=True), comment(likes=-1), comment(parentId=CID), comment(createdAt="yesterday"),
               comment(authorName={"name": "invalid"})]
        result = self.push(*bad, comment(authorName="<b>Valid</b> Name", uid="secret", avatar="secret"))
        self.assertEqual((result["invalid"], result["saved"]), (len(bad), 1))
        self.assertEqual(self.record()["author_name"], "Valid Name")
        self.assertNotIn("secret", self.path.read_text(encoding="utf-8"))
        result = self.store.ingest_comments([video(comment(videoId=OTHER), identifier=OTHER)], {OTHER: "195H"})
        self.assertEqual((result["invalid"], result["saved"]), (1, 0))

    def test_batch_bounds_and_invalid_envelopes_leave_source_unchanged(self):
        self.push(comment())
        original = self.path.read_bytes()
        malformed = [[], {}, [video(comment())] * 201, [{"id": VIDEO, "comments": "not a list"}],
                     [video(*([comment()] * 10001))]]
        for payload in malformed:
            with self.subTest(payload_type=type(payload)), self.assertRaises(ReviewError):
                self.store.ingest_comments(payload, {VIDEO: "202H"})
            self.assertEqual(self.path.read_bytes(), original)
        with patch.object(plugin_comments, "MAX_PAYLOAD_BYTES", 32), self.assertRaises(ReviewError):
            self.push(comment())
        self.assertEqual(self.path.read_bytes(), original)

    def test_failed_atomic_replace_keeps_original_source(self):
        self.push(comment())
        original = self.path.read_bytes()
        with patch.object(plugin_comments.os, "replace", side_effect=OSError("disk busy")), self.assertRaises(OSError):
            self.push(comment(authorName="New Name"))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.path.parent.glob(".plugin-comments-*.tmp")))
        self.assertEqual(self.record()["author_name"], "")

    def test_version_two_migration_preserves_rows_and_restores_names_on_sync(self):
        self.raw([{"cid": CID, "text": "Does this fit?", "nickname": "Restored Name", "uniqueId": "restored.handle"}])
        self.store.sync()
        identifier = self.record()["id"]
        self.store.set_pick(identifier, True, "Preserve this")
        connection = sqlite3.connect(self.store.database)
        connection.execute("ALTER TABLE reviews DROP COLUMN author_name")
        connection.execute("ALTER TABLE reviews DROP COLUMN author_handle")
        connection.execute("PRAGMA user_version=2")
        connection.execute("DELETE FROM meta WHERE key='signature'")
        connection.commit()
        connection.close()
        self.assertEqual(self.record()["id"], identifier)
        self.assertEqual(self.record()["author_name"], "")
        self.store.sync()
        self.assertEqual((self.record()["author_name"], self.record()["author_handle"]), ("Restored Name", "restored.handle"))
        self.assertEqual(self.record()["pick"]["note"], "Preserve this")

    def test_revision_and_coverage_are_read_only_before_initialization(self):
        revision = self.store.revision()
        self.assertEqual(self.store.revision(), revision)
        self.assertFalse(self.store.root.exists())
        self.assertFalse(self.store.covers("202H"))
        self.assertFalse(self.store.root.exists())


if __name__ == "__main__":
    unittest.main()
