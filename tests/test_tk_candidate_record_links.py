"""Checked TK candidate associations add metadata without editing finished records."""
import copy
import tempfile
import unittest
from unittest.mock import patch

from creation_store import CreationError, CreationStore
from elevenlabs_client import VoiceError
from production_store import ProductionStore


CANDIDATE = "TKC-0123456789ab"
SCRIPT = "SCR-0123456789ab"
JOB = "PRD-0123456789ab"
REFERENCE = "https://www.tiktok.com/@example/video/7550000000000000001"
REF = {"index": 0, "snapshot": {"main_difference": "S1 定角", "concern": "B1 检查刀口",
       "closing": "用于日常磨刀", "variant_type": "execution", "execution_change": "双夹配合",
       "progression": "回应质疑", "baseline": False}}


def metadata(*, production=False):
    result = {"candidate_id": CANDIDATE, "direction_id": "202H-S1", "variant_ref": copy.deepcopy(REF)}
    if production:
        result["script_id"] = SCRIPT
    return result


def annotation_change():
    result = copy.deepcopy(REF)
    result["index"] = 8
    result["snapshot"].update(progression="结果倒叙", baseline=True)
    return result


class CandidateCreationAssociationTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(prefix="tk-record-links-")
        self.addCleanup(root.cleanup)
        self.store = CreationStore(root.name)
        self.seed = {"id": SCRIPT, "family_id": SCRIPT, "version": 3, "parent_id": "SCR-aaaaaaaaaaaa",
            "product": "202H", "direction_id": "", "title": "原始稿", "text": "Keep every spoken word.",
            "translation_zh": "保留每一句口播。", "status": "adopted", "archived": False,
            "source": {"task_id": "task-original", "round_id": "round-original", "candidate_text": "原始候选",
                       "snapshot": {"direction": "写稿时背景"}},
            "created_at": "original-created", "updated_at": "original-updated",
            "history": [{"at": "original-created", "status": "adopted", "note": "原始采纳"}]}
        self.data = self.store.empty()
        self.data["scripts"].append(copy.deepcopy(self.seed))
        self.store.write(self.data)

    def associate(self, **changes):
        values = {"script_id": SCRIPT, "product": "SHARPAL 202H", "direction_id": "202H-S1",
                  "variant_ref": copy.deepcopy(REF), "reference_url": REFERENCE}
        values.update(changes)
        return self.store.associate_candidate(values.pop("candidate_id", CANDIDATE), **values)

    def save_seed(self):
        self.store.write(self.data)

    def test_saves_standard_link_once_and_preserves_body_version_and_frozen_source(self):
        revision = self.store.current_revision()
        result = self.associate()
        for key in ("text", "translation_zh", "version", "family_id", "parent_id", "source", "created_at"):
            self.assertEqual(result[key], self.seed[key])
        self.assertEqual(result["direction_id"], "202H-S1")
        self.assertEqual(result["candidate_direction"], metadata())
        saved = self.store.read()
        self.assertEqual(len(saved["scripts"]), 1)
        link = saved["variant_links"][0]
        self.assertEqual((link["kind"], link["record_id"], link["product"], link["direction_id"]),
                         ("script", SCRIPT, "202H", "202H-S1"))
        self.assertEqual(link["variant_ref"], REF)
        self.assertEqual(link["evidence"]["refs"], [CANDIDATE, REFERENCE])
        self.assertEqual(link["history"], [])
        self.assertNotEqual(self.store.current_revision(), revision)
        self.assertTrue(list((self.store.root / "backups").glob("*.json")))
        with patch.object(self.store, "write", wraps=self.store.write) as write:
            self.assertEqual(self.associate(), result)
            write.assert_not_called()
        self.assertEqual(CreationStore(self.store.root.parent).read(), saved)
        self.assertEqual(len([row for row in result["history"] if row.get("event") == "candidate_direction"]), 1)
        result["candidate_direction"]["variant_ref"]["snapshot"]["concern"] = "外部修改"
        self.assertEqual(self.store.read(), saved)

    def test_matching_manual_link_is_reused_in_full_despite_annotation_or_index_changes(self):
        manual_ref = annotation_change()
        self.data["scripts"][0]["source"]["variant_ref"] = copy.deepcopy(manual_ref)
        manual = self.store._variant_link(self.data, {"link": {"kind": "script", "record_id": SCRIPT,
            "product": "202H", "direction_id": "202H-S1", "variant_ref": manual_ref,
            "evidence": {"note": "人工逐项核对", "refs": ["script:" + SCRIPT]}}})["link"]
        self.save_seed()
        result = self.associate()
        self.assertEqual(self.store.read()["variant_links"], [manual])
        self.assertEqual(result["source"], self.data["scripts"][0]["source"])
        before = self.store.read()
        with patch.object(self.store, "write", wraps=self.store.write) as write:
            self.associate(variant_ref=manual_ref)
            write.assert_not_called()
        self.assertEqual(self.store.read(), before)

    def test_dry_run_checks_and_returns_metadata_without_writing_or_advancing_version(self):
        before, version = self.store.path.read_bytes(), self.store.version
        result = self.associate(dry_run=True)
        self.assertEqual(result["candidate_direction"], metadata())
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.version, version)
        self.assertEqual(self.store.read()["variant_links"], [])
        self.assertEqual(self.associate()["candidate_direction"], metadata())

    def test_known_conflicts_never_write_even_during_dry_run(self):
        cases = []
        different_product = copy.deepcopy(self.data)
        different_product["scripts"][0]["product"] = "103N"
        cases.append(different_product)
        different_direction = copy.deepcopy(self.data)
        different_direction["scripts"][0]["direction_id"] = "202H-S2"
        cases.append(different_direction)
        different_candidate = copy.deepcopy(self.data)
        different_candidate["scripts"][0]["candidate_direction"] = {**metadata(), "candidate_id": "TKC-aaaaaaaaaaaa"}
        cases.append(different_candidate)
        for field in ("main_difference", "concern", "closing", "variant_type", "execution_change"):
            different_variant = copy.deepcopy(self.data)
            association = metadata()
            association["variant_ref"]["snapshot"][field] = "另一身份"
            different_variant["scripts"][0]["candidate_direction"] = association
            cases.append(different_variant)
        frozen_variant = copy.deepcopy(self.data)
        frozen_variant["scripts"][0]["source"]["variant_ref"] = copy.deepcopy(REF)
        frozen_variant["scripts"][0]["source"]["variant_ref"]["snapshot"]["closing"] = "另一收口"
        cases.append(frozen_variant)
        for change in ({"product": "103N"}, {"direction_id": "202H-S2"},
                       {"variant_ref": {**REF, "snapshot": {**REF["snapshot"], "concern": "另一顾虑"}}}):
            linked = copy.deepcopy(self.data)
            self.store._variant_link(linked, {"link": {"kind": "script", "record_id": SCRIPT,
                "product": "202H", "direction_id": "202H-S1", "variant_ref": REF,
                "evidence": {"note": "人工核对", "refs": ["script:" + SCRIPT]}, **change}})
            cases.append(linked)
        duplicate = copy.deepcopy(cases[-1])
        duplicate["variant_links"].append(copy.deepcopy(duplicate["variant_links"][0]))
        cases.append(duplicate)
        for index, data in enumerate(cases):
            with self.subTest(case=index):
                self.store.write(data)
                before = self.store.path.read_bytes()
                for dry_run in (False, True):
                    with patch.object(self.store, "write") as write, self.assertRaises(CreationError) as error:
                        self.associate(dry_run=dry_run)
                    self.assertEqual(error.exception.status, 409)
                    write.assert_not_called()
                    self.assertEqual(self.store.path.read_bytes(), before)

    def test_bad_candidate_product_or_variant_shape_does_not_write(self):
        for change in ({"candidate_id": "CND-0123456789ab"}, {"candidate_id": "TKC-../../x"},
                       {"product": "all"}, {"product": ""}, {"variant_ref": {}},
                       {"variant_ref": {**REF, "index": True}}):
            with self.subTest(change=change), patch.object(self.store, "write") as write:
                with self.assertRaises(CreationError) as error:
                    self.associate(**change)
                self.assertEqual(error.exception.status, 400)
                write.assert_not_called()


class CandidateProductionAssociationTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory(prefix="tk-production-links-")
        self.addCleanup(root.cleanup)
        self.store = ProductionStore(root.name)
        self.seed = {"id": JOB, "script_id": SCRIPT, "script_version": 3, "product": "202H", "direction_id": "",
            "source": {"task_id": "original-task", "snapshot": {"direction": "写稿时背景"}},
            "lines": [{"number": 1, "spoken": "Keep every spoken word.", "zh": "保留每一句口播。", "visual": "原画面"}],
            "text_sha256": "original-digest", "supplement": {"hook_visual": "原开头"},
            "takes": [{"id": "TAK-aaaaaaaaaaaa", "path": "audio/original.mp3", "groups": [{"number": 1}]}],
            "selected_take": "TAK-aaaaaaaaaaaa", "timelines": [{"unique_id": "timeline-original", "saved": True}],
            "edits": [{"id": "EDT-aaaaaaaaaaaa", "status": "completed", "report": {"saved": True}}],
            "created_at": "original-created", "updated_at": "original-updated",
            "history": [{"at": "original-created", "event": "created", "note": "原始制作单"}]}
        self.data = self.store.empty()
        self.data["jobs"].append(copy.deepcopy(self.seed))
        self.store.write(self.data)

    def associate(self, **changes):
        values = {"job_id": JOB, "script_id": SCRIPT, "product": "SHARPAL 202H", "direction_id": "202H-S1",
                  "variant_ref": copy.deepcopy(REF)}
        values.update(changes)
        return self.store.associate_candidate(values.pop("candidate_id", CANDIDATE), **values)

    def test_preserves_voice_edit_and_source_records_and_retries_without_writing(self):
        result = self.associate()
        for key, value in self.seed.items():
            if key not in {"direction_id", "history", "updated_at"}:
                self.assertEqual(result[key], value)
        self.assertEqual(result["direction_id"], "202H-S1")
        self.assertEqual(result["candidate_direction"], metadata(production=True))
        self.assertEqual(len([row for row in result["history"] if row.get("event") == "candidate_direction"]), 1)
        before = self.store.path.read_bytes()
        with patch.object(self.store, "write", wraps=self.store.write) as write:
            self.assertEqual(self.associate(variant_ref=annotation_change()), result)
            write.assert_not_called()
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(ProductionStore(self.store.root.parent).read()["jobs"], [result])
        self.assertTrue(list((self.store.root / "backups").glob("*.json")))

    def test_dry_run_returns_full_association_without_saving(self):
        before = self.store.path.read_bytes()
        result = self.associate(dry_run=True)
        self.assertEqual(result["candidate_direction"], metadata(production=True))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.read()["jobs"], [self.seed])

    def test_record_and_frozen_source_conflicts_never_write(self):
        changes = [{"product": "103N"}, {"script_id": "SCR-aaaaaaaaaaaa"}, {"direction_id": "202H-S2"},
                   {"candidate_direction": {**metadata(production=True), "candidate_id": "TKC-aaaaaaaaaaaa"}},
                   {"candidate_direction": {**metadata(production=True), "script_id": "SCR-aaaaaaaaaaaa"}}]
        for field in ("main_difference", "concern", "closing", "variant_type", "execution_change"):
            association = metadata(production=True)
            association["variant_ref"]["snapshot"][field] = "另一身份"
            changes.append({"candidate_direction": association})
        changes.append({"source": {"variant_ref": {**REF, "snapshot": {**REF["snapshot"], "closing": "另一收口"}}}})
        for change in changes:
            with self.subTest(change=change):
                data = copy.deepcopy(self.data)
                data["jobs"][0].update(change)
                self.store.write(data)
                before = self.store.path.read_bytes()
                for dry_run in (False, True):
                    with patch.object(self.store, "write") as write, self.assertRaises(VoiceError) as error:
                        self.associate(dry_run=dry_run)
                    self.assertEqual(error.exception.status, 409)
                    write.assert_not_called()
                    self.assertEqual(self.store.path.read_bytes(), before)

    def test_invalid_association_uses_production_error_and_does_not_write(self):
        for change in ({"candidate_id": "CND-0123456789ab"}, {"product": "all"}, {"product": ""},
                       {"variant_ref": {}}, {"variant_ref": {**REF, "index": True}}):
            with self.subTest(change=change), patch.object(self.store, "write") as write:
                with self.assertRaises(VoiceError) as error:
                    self.associate(**change)
                self.assertEqual(error.exception.status, 400)
                write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
