"""Explicit test-plan variant provenance, using only disposable stores and HTTP fixtures."""
import copy
import tempfile
import unittest
from uuid import uuid4

from creation_store import CreationError, CreationStore
import test_server as fixture
from test_direction_catalog import install_points


def variant_ref(direction, index=0):
    variant = direction["variants"][index]
    return {"index": index, "snapshot": {key: copy.deepcopy(variant[key]) for key in
            ("progression", "baseline", "variant_type", "main_difference", "concern", "closing", "execution_change")
            if key in variant}}


class PlanVariantPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(prefix="plan-variant-test-")
        self.addCleanup(self.root.cleanup)
        self.store = CreationStore(self.root.name)
        self.ref = {"index": 0, "snapshot": {"main_difference": "S1 核心理由", "concern": "先核刀宽",
                    "closing": "说明适用条件", "variant_type": "direction", "baseline": True}}

    def save(self, **values):
        return self.store.mutate("plan", {"revision": self.store.public()["revision"], "request_id": uuid4().hex,
                                          "plan": values})["plan"]

    def create(self, **values):
        return self.save(product="202H", direction_id="202H-S1", action="execution", **values)

    def test_binding_survives_partial_edit_restart_and_caller_mutation(self):
        saved = self.create(variant_ref=self.ref)
        self.ref["snapshot"]["concern"] = "调用方后来修改"
        updated = self.save(id=saved["id"], status="done")
        self.assertEqual(updated["variant_ref"], saved["variant_ref"])
        restarted = CreationStore(self.root.name)
        self.assertEqual(restarted.public()["plans"][0]["variant_ref"], saved["variant_ref"])
        self.assertEqual(updated["variant_ref"]["snapshot"]["concern"], "先核刀宽")

    def test_existing_binding_cannot_be_cleared_or_changed(self):
        saved = self.create(variant_ref=self.ref)
        for replacement in (None, {}, {**self.ref, "index": 1}):
            with self.subTest(replacement=replacement), self.assertRaises(CreationError) as error:
                self.save(id=saved["id"], variant_ref=replacement)
            self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.store.public()["plans"][0], saved)

    def test_malformed_refs_fail_without_writing(self):
        for ref in (None, {}, {**self.ref, "index": True}, {**self.ref, "index": -1},
                    {**self.ref, "snapshot": {}},
                    {**self.ref, "snapshot": {**self.ref["snapshot"], "baseline": "true"}},
                    {**self.ref, "snapshot": {**self.ref["snapshot"], "untrusted": "ignored?"}}):
            with self.subTest(ref=ref), self.assertRaises(CreationError) as error:
                self.create(variant_ref=ref)
            self.assertEqual(error.exception.status, 400)
        self.assertEqual(self.store.public()["plans"], [])

    def test_old_plan_edits_do_not_invent_a_binding(self):
        saved = self.create()
        self.assertNotIn("variant_ref", saved)
        updated = self.save(id=saved["id"], status="working")
        self.assertNotIn("variant_ref", updated)


class PlanVariantHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def direction(self, code="S1"):
        install_points(self.skill)
        app = self.server.app
        catalog = app.direction_catalog("202H")
        point = next(item for item in catalog["selling_points"] if item["code"] == code)
        return app.save_direction_catalog("direction", {"revision": catalog["revision"], "request_id": uuid4().hex,
            "direction": {"product": "202H", "selling_point_id": point["id"], "concern": "先核刀宽", "closing": "说明适用条件"}})["direction"]

    def body(self, **values):
        return {"revision": self.server.app.creation.public()["revision"], "request_id": uuid4().hex, "plan": values}

    def create_body(self, direction, **values):
        return self.body(product="202H", direction_id=direction["direction_id"], action="execution", **values)

    def save(self, body):
        status, _, result = self.request("POST", "/api/plans", body)
        self.assertEqual(status, 200, result)
        return result["plan"]

    def test_selected_variant_is_saved_exactly_and_replay_precedes_catalog_change(self):
        direction = self.direction()
        ref = variant_ref(direction)
        body = self.create_body(direction, variant_ref=ref)
        saved = self.save(body)
        self.assertEqual(saved["variant_ref"], ref)
        data = self.server.app.directions.read()
        data["directions"][0]["variants"][0]["progression"] = "结果倒叙"
        self.server.app.directions.write(data)
        self.assertEqual(self.save(body), saved)
        updated = self.save(self.body(id=saved["id"], status="done"))
        self.assertEqual(updated["variant_ref"], ref)
        self.assertEqual(len(self.server.app.creation.public()["plans"]), 1)

    def test_stale_index_snapshot_and_conflicting_variant_never_create_plan(self):
        direction = self.direction()
        ref = variant_ref(direction)
        stale = {"index": 0, "snapshot": {**ref["snapshot"], "concern": "旧目录的顾虑"}}
        for invalid in ({**ref, "index": 8}, stale):
            status, _, result = self.request("POST", "/api/plans", self.create_body(direction, variant_ref=invalid))
            self.assertEqual(status, 409, result)
        data = self.server.app.directions.read()
        data["directions"][0]["variants"][0]["conflict"] = True
        self.server.app.directions.write(data)
        status, _, result = self.request("POST", "/api/plans", self.create_body(direction, variant_ref=ref))
        self.assertEqual(status, 409, result)
        self.assertEqual(self.server.app.creation.public()["plans"], [])

    def test_snapshot_is_checked_against_selected_product_and_direction(self):
        first, other = self.direction(), self.direction("S2")
        for values in ({"direction_id": other["direction_id"]}, {"product": "103N"}):
            body = self.create_body(first, variant_ref=variant_ref(first))
            body["plan"].update(values)
            status, _, result = self.request("POST", "/api/plans", body)
            self.assertIn(status, (400, 409), result)
        self.assertEqual(self.server.app.creation.public()["plans"], [])

    def test_legacy_plan_stays_unbound_until_explicit_selection_then_binding_is_immutable(self):
        direction = self.direction()
        old = self.save(self.create_body(direction))
        updated = self.save(self.body(id=old["id"], status="working"))
        self.assertNotIn("variant_ref", updated)
        linked = self.save(self.body(id=old["id"], variant_ref=variant_ref(direction)))
        self.assertEqual(linked["variant_ref"], variant_ref(direction))
        for replacement in (None, {**variant_ref(direction), "index": 1}):
            status, _, result = self.request("POST", "/api/plans", self.body(id=old["id"], variant_ref=replacement))
            self.assertEqual(status, 409, result)
        self.assertEqual(self.server.app.creation.public()["plans"][0], linked)


if __name__ == "__main__":
    unittest.main()
