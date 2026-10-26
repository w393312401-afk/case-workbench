"""Archive is a reversible visibility setting, with frozen release/version evidence intact."""
import copy
import tempfile
import unittest
from uuid import uuid4

from creation_store import CreationError, CreationStore


class ScriptLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="script-lifecycle-")
        self.addCleanup(self.temp.cleanup)
        self.store = CreationStore(self.temp.name)

    def body(self, **values):
        return {"request_id": uuid4().hex, "revision": self.store.public()["revision"], **values}

    def script(self):
        return self.store.mutate("script", self.body(script={"product": "202H", "title": "已采用稿",
            "text": "Original frozen words.", "translation_zh": "原采用正文。", "confirmed": True}))["script"]

    def save(self, script, **values):
        return self.store.mutate("script_status", self.body(script_status={"script_id": script["id"], **values}))["script"]

    def test_archiving_and_restore_preserve_published_progress_and_frozen_version(self):
        item = self.script()
        published = self.save(item, status="published", video_id="7000000000000000001", published_on="2026-10-01")
        body = self.body(script_status={"script_id": item["id"], "archived": True, "note": "本月已交付"})
        archived = self.store.mutate("script_status", body)["script"]
        restarted = CreationStore(self.temp.name)
        self.assertEqual(restarted.mutate("script_status", body)["script"], archived)
        restored = self.save(item, archived=False)
        for field in ("status", "video_id", "published_on", "text", "translation_zh", "family_id", "version", "source"):
            self.assertEqual(archived[field], published[field])
            self.assertEqual(restored[field], published[field])
        self.assertEqual(restored["history"][:len(published["history"])], published["history"])
        self.assertEqual([row.get("event") for row in restored["history"][-2:]], ["archive", "restore"])
        self.assertFalse(restored["archived"])

    def test_legacy_archived_rows_are_read_only_until_explicit_restore(self):
        item = self.script()
        data = self.store.read()
        data["scripts"][0].update(status="archived", history=[{"status": "filmed", "at": "2026-10-01"},
                                                            {"status": "archived", "at": "2026-10-02"}])
        data["scripts"][0].pop("archived", None)
        self.store.write(data)
        frozen = self.store.path.read_bytes()
        legacy = self.store.public()["scripts"][0]
        self.assertTrue(legacy["archived"])
        self.assertEqual(legacy["lifecycle_status"], "filmed")
        self.assertEqual(self.store.get_script(item["id"])["lifecycle_status"], "filmed")
        self.assertEqual(self.store.path.read_bytes(), frozen)
        restored = self.save(item, archived=False)
        self.assertEqual(restored["status"], "filmed")
        self.assertFalse(restored["archived"])
        self.assertEqual(restored["history"][:2], data["scripts"][0]["history"])

    def test_legacy_missing_progress_does_not_invent_a_publication(self):
        item = self.script()
        data = self.store.read()
        data["scripts"][0].update(status="archived", history=[{"status": "published"}])
        self.store.write(data)
        restored = self.save(item, archived=False)
        self.assertEqual(restored["status"], "draft")
        self.assertEqual((restored["video_id"], restored["published_on"]), ("", ""))

    def test_legacy_archive_action_keeps_progress_and_conflicts_do_not_overwrite(self):
        item = self.script()
        stale = self.body(script_status={"script_id": item["id"], "archived": False})
        archived = self.save(item, status="archived")
        self.assertEqual(archived["status"], "adopted")
        self.assertTrue(archived["archived"])
        with self.assertRaises(CreationError) as caught:
            self.store.mutate("script_status", stale)
        self.assertEqual(caught.exception.status, 409)
        before = copy.deepcopy(self.store.read())
        with self.assertRaises(CreationError):
            self.save(item, archived="false")
        self.assertEqual(self.store.read(), before)


if __name__ == "__main__":
    unittest.main()
