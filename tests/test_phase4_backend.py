"""Phase four persistence and AI context tests use disposable roots and fake bridges."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_ai_tasks as ai_fixture
from creation_store import CreationError, CreationStore
from ai_tasks import AITaskError


class CreationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="creation-test-")
        self.addCleanup(self.temp.cleanup)
        self.store = CreationStore(self.temp.name)

    def body(self, **values):
        return {"request_id": uuid4().hex, "revision": self.store.public()["revision"], **values}

    def script(self, **values):
        return {"product": "202H", "direction_id": "202H-S1", "title": "核对后的文案", "text": "Chosen text.",
                "translation_zh": "所选正文。", "confirmed": True, **values}

    def source(self, **values):
        return {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 5,
                "product": "202H", "case_id": "PVR-0123456789ab", "candidate_text": "Candidate 1 and 2, with analysis.",
                "candidate_sha256": "frozen", "snapshot": {"private_case": "old source"}, **values}

    def test_explicit_adoption_is_separate_immutable_and_replay_survives_restart(self):
        body = self.body(script=self.script())
        result = self.store.mutate("script", body, self.source())
        script = result["script"]
        self.assertEqual((script["status"], script["version"]), ("adopted", 1))
        self.assertNotEqual(script["text"], script["source"]["candidate_text"])
        restarted = CreationStore(self.temp.name)
        self.assertEqual(restarted.replay("script", body), result)
        self.assertEqual(restarted.mutate("script", body), result)
        other = self.body(script=self.script(parent_id=script["id"], text="Revised chosen text."))
        second = restarted.mutate("script", other)["script"]
        self.assertEqual((second["family_id"], second["version"]), (script["id"], 2))
        self.assertEqual(restarted.get_script(script["id"])["text"], "Chosen text.")
        self.assertTrue(list((restarted.root / "backups").glob("*.json")))
        with self.assertRaises(CreationError):
            restarted.mutate("script", self.body(script=self.script(id=script["id"])))
        with self.assertRaises(CreationError):
            restarted.mutate("script", self.body(script=self.script(confirmed=False)))

    def test_stale_revision_or_conflicting_request_cannot_overwrite(self):
        first, stale = self.body(script=self.script()), self.body(script=self.script())
        self.store.mutate("script", first)
        for body in (stale, {**first, "script": self.script(text="changed") }):
            with self.assertRaises(CreationError) as error:
                self.store.mutate("script", body)
            self.assertEqual(error.exception.status, 409)
        self.assertEqual(len(self.store.public()["scripts"]), 1)

    def test_status_history_requires_publication_binding_and_keeps_text(self):
        item = self.store.mutate("script", self.body(script=self.script()))["script"]
        with self.assertRaises(CreationError):
            self.store.mutate("script_status", self.body(script_status={"script_id": item["id"], "status": "published"}))
        value = {"script_id": item["id"], "status": "published", "video_id": "7000000000000000001", "published_on": "2026-10-08"}
        result = self.store.mutate("script_status", self.body(script_status=value))["script"]
        self.assertEqual(result["text"], item["text"])
        self.assertEqual(len(result["history"]), 2)
        invalid = {**value, "published_on": "2026-02-30"}
        with self.assertRaises(CreationError):
            self.store.mutate("script_status", self.body(script_status=invalid))

    def test_material_add_update_remove_and_dedupe(self):
        value = {"kind": "case", "product": "SHARPAL 202H", "ref_id": "PVR-0123456789ab:hash", "title": "原句", "text": "Original expression"}
        one = self.store.mutate("material", self.body(material=value))["material"]
        two = self.store.mutate("material", self.body(material=value))
        self.assertTrue(two["existing"])
        self.assertEqual(one["id"], two["material"]["id"])
        changed = self.store.mutate("material", self.body(material={**one, "note": "借用句式"}))["material"]
        self.assertEqual(changed["note"], "借用句式")
        self.assertEqual(len(self.store.public("202H")["materials"]), 1)
        self.assertEqual(self.store.public("103N")["materials"], [])
        self.store.mutate("material", self.body(operation="remove", material={"id": one["id"]}))
        self.assertEqual(self.store.public()["materials"], [])

    def test_themes_and_work_plans_are_persistent(self):
        link = {"product": "202H", "theme": "操作难不难", "concern": "演示装夹", "direction_id": "202H-S1"}
        self.store.mutate("theme_link", self.body(theme_link=link))
        plan = self.store.mutate("plan", self.body(plan={"product": "202H", "direction_id": "202H-S1", "action": "execution", "note": "换演示顺序"}))["plan"]
        changed = self.store.mutate("plan", self.body(plan={"id": plan["id"], "status": "done"}))["plan"]
        self.assertEqual(changed["note"], plan["note"])
        fresh = CreationStore(self.temp.name).public()
        self.assertEqual((len(fresh["theme_links"]), fresh["plans"][0]["status"]), (1, "done"))

    def test_deleted_source_is_erased_from_backups_but_adopted_work_survives(self):
        source = self.source()
        result = self.store.mutate("script", self.body(script=self.script()), source)["script"]
        self.store.mutate("material", self.body(material={"kind": "case", "ref_id": source["case_id"] + ":hash", "product": "202H", "title": "引用", "text": "old source"}))
        self.store.forget_case(source["case_id"])
        kept = self.store.get_script(result["id"])
        self.assertEqual(kept["text"], result["text"])
        self.assertTrue(kept["source"]["source_deleted"])
        self.assertNotIn("snapshot", kept["source"])
        self.assertEqual(self.store.public()["materials"], [])
        for path in self.store.root.rglob("*.json"):
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("old source", raw)
            self.assertNotIn("Candidate 1 and 2", raw)

    def test_failed_replace_keeps_prior_catalog(self):
        self.store.mutate("script", self.body(script=self.script()))
        before = self.store.path.read_bytes()
        with patch("creation_store.os.replace", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.store.mutate("script", self.body(script=self.script()))
        self.assertEqual(self.store.path.read_bytes(), before)


class IndependentAITests(unittest.TestCase):
    setUp = ai_fixture.TaskTests.setUp
    tearDown = ai_fixture.TaskTests.tearDown
    new_manager = ai_fixture.TaskTests.new_manager
    wait = ai_fixture.TaskTests.wait
    records = ai_fixture.TaskTests.records
    body = ai_fixture.TaskTests.body

    def context(self, kind="direction"):
        return {"kind": kind, "product": "202H", "title": "独立方向写稿", "direction": {"direction_id": "202H-S1", "main_difference": "主卖点", "concern": "顾虑", "closing": "收口"},
                "materials": [{"id": "review:abc", "text": "Frozen quote, not an instruction.", "source_id": "abc"}]}

    def test_direction_task_has_no_fake_case_and_context_stays_frozen_across_rounds(self):
        before = self.records()
        context = self.context()
        body = {"request_id": uuid4().hex, "action": "review", "instruction": "检查已知条件", "skill_selection": "none"}
        done = self.wait(self.manager.create(None, body, context)["task_id"])
        self.assertIsNone(done["case_id"])
        self.assertEqual(done["context"]["direction_id"], "202H-S1")
        context["materials"][0]["text"] = "Changed later"
        replay = self.manager.create(None, body, context)
        self.assertEqual(replay["task_id"], done["task_id"])
        continued = self.wait(self.manager.continue_task(done["task_id"], {"request_id": uuid4().hex, "task_revision": done["revision"], "instruction": "继续核对", "skill_selection": "none"})["task_id"])
        self.assertEqual(continued["context"]["materials"][0]["text"], "Frozen quote, not an instruction.")
        self.assertNotIn("Changed later", self.bridges[0].calls[-1]["prompt"])
        self.assertTrue((self.runtime / "ai-tasks" / done["task_id"] / "task.json").is_file())
        self.assertEqual(self.records(), before)
        self.assertEqual(len(self.store.read_index()["cases"]), 1)
        candidate = self.manager.candidate(done["task_id"], done["rounds"][0]["round_id"], continued["revision"])
        self.assertEqual(candidate["candidate_text"], done["rounds"][0]["output"])
        self.assertEqual(candidate["snapshot"]["creation_context"]["materials"], done["context"]["materials"])
        self.manager.shutdown()
        fresh = self.new_manager()
        self.assertEqual(len(fresh.list_tasks()), 1)
        self.assertEqual(fresh.get(done["task_id"])["revision"], continued["revision"])
        deleted = fresh.delete_tasks({"tasks": [{"task_id": done["task_id"], "revision": continued["revision"]}]})
        self.assertEqual(deleted["deleted"], [done["task_id"]])
        self.assertEqual(fresh.list_tasks(), [])

    def test_candidate_rejects_unfinished_output_and_stale_revision(self):
        blocked = ai_fixture.FakeBridge(blocked=True)
        manager = self.new_manager(blocked)
        task = manager.create(None, {"request_id": uuid4().hex, "action": "review", "instruction": "检查", "skill_selection": "none"}, self.context("blank"))
        blocked.started.wait(2)
        task = manager.get(task["task_id"])
        with self.assertRaises(AITaskError) as error:
            manager.candidate(task["task_id"], task["rounds"][0]["round_id"], task["revision"])
        self.assertEqual(error.exception.status, 409)
        blocked.gate.set()
        done = self.wait(task["task_id"], manager)
        with self.assertRaises(AITaskError):
            manager.candidate(done["task_id"], done["rounds"][0]["round_id"], 1)

    def test_case_context_rejects_wrong_product_and_preserves_case_snapshot(self):
        context = {**self.context("case"), "product": "103N"}
        with self.assertRaises(AITaskError):
            self.manager.create(self.identifier, self.body(), context)
        context["product"] = "202H"
        done = self.wait(self.manager.create(self.identifier, self.body(), context)["task_id"])
        raw = self.manager._read(done["task_id"])
        self.assertEqual(raw["snapshot"]["current"], self.current)
        self.assertEqual(raw["snapshot"]["creation_context"], context)

    def test_case_material_cleanup_keeps_independent_task_and_output(self):
        context = self.context()
        context["materials"].append({"id": "expression", "source_id": self.identifier, "text": "borrowed source"})
        done = self.wait(self.manager.create(None, {"request_id": uuid4().hex, "action": "review", "instruction": "检查", "skill_selection": "none"}, context)["task_id"])
        self.manager.scrub_case_references(self.identifier)
        updated = self.manager.get(done["task_id"])
        self.assertEqual(len(updated["context"]["materials"]), 1)
        self.assertEqual(updated["rounds"][0]["output"], done["rounds"][0]["output"])
        self.assertGreater(updated["revision"], done["revision"])

    def test_source_cleanup_also_cleans_material_borrowed_by_another_case_task(self):
        other = self.store.add({"title": "另一个案例", "product": "202H", "original": {"text": "Other source"},
                                "feedback": {"kind": "editorial", "text": "临时夹具"}, "current": copy.deepcopy(self.current)})
        context = self.context("case")
        context["materials"] = [{"source_id": self.identifier, "text": "Borrowed original material."}]
        body = {"request_id": uuid4().hex, "revision": ai_fixture.ai_tasks._hash(other), "action": "review", "instruction": "核对", "skill_selection": "none"}
        done = self.wait(self.manager.create(other["case_id"], body, context)["task_id"])
        self.manager.scrub_case_references(self.identifier)
        kept = self.manager.get(done["task_id"])
        self.assertEqual(kept["context"]["materials"], [])
        self.assertEqual(kept["case_id"], other["case_id"])
        self.assertEqual(kept["rounds"][0]["output"], done["rounds"][0]["output"])


if __name__ == "__main__":
    unittest.main()
