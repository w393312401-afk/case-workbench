"""Reviewed historical variant links preserve source records and replay safely."""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from creation_store import CreationError, CreationStore
import test_server as fixture
from test_direction_catalog import install_points
from test_plan_variant_binding import variant_ref


class VariantLinkStoreTests(unittest.TestCase):
    def test_old_catalog_is_read_compatibly_without_being_rewritten(self):
        with tempfile.TemporaryDirectory() as root:
            store = CreationStore(root)
            data = store.empty()
            del data["variant_links"]
            store.write(data)
            before = store.path.read_bytes()
            self.assertEqual(store.public()["variant_links"], [])
            self.assertEqual(store.path.read_bytes(), before)
            data["variant_links"] = {}
            store.path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(CreationError):
                store.read()


class VariantLinkHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def ok(self, method, path, body=None):
        status, _, result = self.request(method, path, body)
        self.assertEqual(status, 200, result)
        return result

    def direction(self, code="S1"):
        install_points(self.skill)
        app = self.server.app
        catalog = app.direction_catalog("202H")
        point = next(row for row in catalog["selling_points"] if row["code"] == code)
        return app.save_direction_catalog("direction", {"revision": catalog["revision"], "request_id": uuid4().hex,
            "direction": {"product": "202H", "selling_point_id": point["id"], "concern": "先核刀宽",
                          "closing": "说明适用条件"}})["direction"]

    def script(self, direction=None, product="202H"):
        return self.ok("POST", "/api/scripts", {
            "revision": self.server.app.creation.public()["revision"], "request_id": uuid4().hex,
            "script": {"product": product, "direction_id": direction["direction_id"] if direction else "",
                       "title": "历史文案", "text": "Keep the original text.", "confirmed": True}})["script"]

    def source(self, kind, identifier, product="202H"):
        return self.ok("GET", "/api/variant-links/source?" + urlencode({"kind": kind, "id": identifier, "product": product}))

    def body(self, kind, identifier, direction, *, source=None):
        source = source or self.source(kind, identifier)
        return {"revision": source["revision"], "record_revision": source["record_revision"], "request_id": uuid4().hex,
                "link": {"kind": kind, "record_id": identifier, "product": "202H",
                         "direction_id": direction["direction_id"], "variant_ref": variant_ref(direction),
                         "evidence": {"note": "已逐项核对正式记录与完整变体链路。", "refs": [kind + ":" + identifier]}}}

    def save(self, body):
        return self.ok("POST", "/api/variant-links", body)

    def test_script_binding_preserves_source_and_keeps_rebinding_history(self):
        first, other = self.direction(), self.direction("S2")
        script = self.script()
        original = copy.deepcopy(self.server.app.creation.get_script(script["id"]))
        source = self.source("script", script["id"])
        saved = self.save(self.body("script", script["id"], first, source=source))
        link = saved["link"]
        self.assertEqual(link["history"], [])
        self.assertEqual(saved["catalog"]["variant_links"], [link])
        inspected = self.source("script", script["id"])
        self.assertEqual(inspected["existing_link"], link)
        rebound = self.save(self.body("script", script["id"], other))["link"]
        self.assertEqual(rebound["id"], link["id"])
        self.assertEqual(rebound["created_at"], link["created_at"])
        self.assertEqual(rebound["history"], [{key: value for key, value in link.items() if key != "history"}])
        self.assertEqual(self.server.app.creation.get_script(script["id"]), original)
        public = self.ok("GET", "/api/creation?product=202H")["scripts"][0]
        self.assertEqual(public["variant_link"]["resolved_index"], 0)
        self.assertEqual(public["variant_link"]["id"], rebound["id"])
        self.assertEqual(self.ok("GET", "/api/scripts/" + script["id"])["script"]["variant_link"], public["variant_link"])
        restarted = CreationStore(self.runtime)
        self.assertEqual(restarted.public()["variant_links"], [rebound])
        self.assertTrue(list((restarted.root / "backups").glob("*.json")))

    def test_script_public_binding_follows_identity_after_reorder_but_hides_ambiguous_or_changed_chain(self):
        direction = self.direction()
        script = self.script(direction)
        link = self.save(self.body("script", script["id"], direction))["link"]
        raw = self.server.app.directions.read()
        variant = raw["directions"][0]["variants"][0]
        variant["progression"] = "结果倒叙"
        variant["baseline"] = not variant.get("baseline", False)
        extra = {**variant, "concern": "另一个顾虑"}
        raw["directions"][0]["variants"].insert(0, extra)
        self.server.app.directions.write(raw)
        public = self.ok("GET", "/api/creation?product=202H")["scripts"][0]
        self.assertEqual(public["variant_link"]["resolved_index"], 1)
        self.assertEqual(public["variant_link"]["variant_ref"], link["variant_ref"])
        raw["directions"][0]["variants"].append(copy.deepcopy(variant))
        self.server.app.directions.write(raw)
        self.assertNotIn("variant_link", self.ok("GET", "/api/scripts/" + script["id"])["script"])
        raw["directions"][0]["variants"].pop()
        variant["concern"] = "正式链路已调整"
        self.server.app.directions.write(raw)
        self.assertNotIn("variant_link", self.ok("GET", "/api/creation?product=202H")["scripts"][0])

    def test_script_product_and_known_direction_cannot_be_overridden(self):
        first, other = self.direction(), self.direction("S2")
        script = self.script(first)
        status, _, result = self.request("POST", "/api/variant-links", self.body("script", script["id"], other))
        self.assertEqual(status, 409, result)
        status, _, result = self.request("GET", "/api/variant-links/source?" + urlencode(
            {"kind": "script", "id": script["id"], "product": "103N"}))
        self.assertEqual(status, 409, result)
        self.assertEqual(self.server.app.creation.public()["variant_links"], [])

    def test_duplicate_imported_script_links_are_never_exposed_by_input_order(self):
        first, other = self.direction(), self.direction("S2")
        script = self.script()
        link = self.save(self.body("script", script["id"], first))["link"]
        for duplicate in (copy.deepcopy(link), {**copy.deepcopy(link), "direction_id": other["direction_id"],
                                               "variant_ref": variant_ref(other)}):
            raw = self.server.app.creation.read()
            raw["variant_links"] = [copy.deepcopy(link), duplicate]
            self.server.app.creation.write(raw)
            self.assertNotIn("variant_link", self.ok("GET", "/api/creation?product=202H")["scripts"][0])
            self.assertNotIn("variant_link", self.ok("GET", "/api/scripts/" + script["id"])["script"])

    def test_direction_counts_and_detail_include_only_current_verified_script_links(self):
        first, other = self.direction(), self.direction("S2")
        linked = [self.script(), self.script()]
        unrelated = self.script()
        for script in linked:
            self.save(self.body("script", script["id"], first))
        self.save(self.body("script", unrelated["id"], other))
        path = "/api/direction-catalog/" + first["direction_id"] + "?product=202H&view=page&include_tasks=0"
        detail = self.ok("GET", path)
        self.assertEqual(detail["direction"]["script_count"], 2)
        self.assertEqual({row["id"] for row in detail["scripts"]}, {row["id"] for row in linked})
        self.assertTrue(all(row["direction_id"] == "" for row in detail["scripts"]))
        catalog = self.ok("GET", "/api/direction-catalog?product=202H")
        self.assertEqual({row["direction_id"]: row["script_count"] for row in catalog["directions"]},
                         {first["direction_id"]: 2, other["direction_id"]: 1})
        raw = self.server.app.directions.read()
        target = next(row for row in raw["directions"] if row["direction_id"] == first["direction_id"])
        target["variants"][0]["concern"] = "已改成另一条完整链路"
        self.server.app.directions.write(raw)
        detail = self.ok("GET", path)
        self.assertEqual(detail["direction"]["script_count"], 0)
        self.assertEqual(detail["scripts"], [])

    def test_both_source_and_creation_revisions_prevent_lost_updates(self):
        direction = self.direction()
        script = self.script(direction)
        body = self.body("script", script["id"], direction)
        data = self.server.app.creation.read()
        data["scripts"][0]["status"] = "filmed"
        self.server.app.creation.write(data)
        status, _, result = self.request("POST", "/api/variant-links", body)
        self.assertEqual(status, 409, result)
        self.assertIn("历史记录已更新", result["error"])
        body = self.body("script", script["id"], direction)
        self.script(direction)
        status, _, result = self.request("POST", "/api/variant-links", body)
        self.assertEqual(status, 409, result)
        self.assertIn("创作资料已更新", result["error"])

    def test_replay_is_durable_and_precedes_source_resolution(self):
        direction = self.direction()
        script = self.script(direction)
        body = self.body("script", script["id"], direction)
        saved = self.save(body)["link"]
        with patch.object(self.server.app, "_variant_link_source", side_effect=AssertionError("receipt before source")):
            self.assertEqual(self.save(body)["link"], saved)
            changed = copy.deepcopy(body)
            changed["link"]["evidence"]["note"] = "不能重复使用请求编号"
            status, _, result = self.request("POST", "/api/variant-links", changed)
        self.assertEqual(status, 409, result)
        self.assertEqual(len(self.server.app.creation.public()["variant_links"]), 1)

    def test_stale_snapshot_invalid_index_conflict_and_missing_evidence_are_rejected(self):
        direction = self.direction()
        script = self.script(direction)
        for change, expected in (({"index": 10}, 409), ({"index": True}, 400),
                                 ({"snapshot": {**variant_ref(direction)["snapshot"], "concern": "另一顾虑"}}, 409)):
            body = self.body("script", script["id"], direction)
            body["link"]["variant_ref"].update(change)
            status, _, result = self.request("POST", "/api/variant-links", body)
            self.assertEqual(status, expected, result)
        body = self.body("script", script["id"], direction)
        body["link"]["evidence"]["refs"] = []
        self.assertEqual(self.request("POST", "/api/variant-links", body)[0], 400)
        raw = self.server.app.directions.read()
        raw["directions"][0]["variants"].append({**raw["directions"][0]["variants"][0], "progression": "另一个推进"})
        self.server.app.directions.write(raw)
        status, _, result = self.request("POST", "/api/variant-links", self.body("script", script["id"], direction))
        self.assertEqual(status, 409, result)
        self.assertIn("相同的完整链路", result["error"])
        raw["directions"][0]["variants"].pop()
        raw["directions"][0]["variants"][0]["conflict"] = True
        self.server.app.directions.write(raw)
        status, _, result = self.request("POST", "/api/variant-links", self.body("script", script["id"], direction))
        self.assertEqual(status, 409, result)
        self.assertEqual(self.server.app.creation.public()["variant_links"], [])

    def test_video_and_carousel_jobs_use_frozen_records_and_original_script_direction(self):
        first, other = self.direction(), self.direction("S2")
        script = self.script(first)
        production = self.server.app.production
        data = production.read()
        for key, identifier in (("jobs", "PRD-000000000001"), ("carousels", "CAR-000000000002")):
            data[key].append({"id": identifier, "product": "202H", "direction_id": "", "script_id": script["id"],
                              "title": "历史制作单", "status": "draft", "source": {"variant_index": 7}})
        production.write(data)
        before = production.path.read_bytes()
        for identifier in ("PRD-000000000001", "CAR-000000000002"):
            source = self.source("production", identifier)
            self.assertEqual(source["record"]["direction_id"], first["direction_id"])
            status, _, result = self.request("POST", "/api/variant-links", self.body("production", identifier, other))
            self.assertEqual(status, 409, result)
            saved = self.save(self.body("production", identifier, first))["link"]
            self.assertEqual(saved["record_id"], identifier)
        self.assertEqual(production.path.read_bytes(), before)
        body = self.body("production", "PRD-000000000001", first)
        data["jobs"][0]["title"] = "核对后修改"
        production.write(data)
        self.assertEqual(self.request("POST", "/api/variant-links", body)[0], 409)

    def test_case_video_source_is_read_only_and_cannot_be_taken_by_another_direction(self):
        first, other = self.direction(), self.direction("S2")
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["test_direction"] = {"direction_id": first["direction_id"], **{
            key: first["variants"][0][key] for key in ("main_difference", "concern", "closing", "variant_type")}}
        self.store.append(self.identifier, {"kind": "editorial", "text": "方向核对", "current": current})
        video = current["metrics"]["video_id"]
        direction_bytes = self.server.app.directions.path.read_bytes()
        case_bytes = {str(path): path.read_bytes() for path in self.skill.rglob("*.json")}
        source = self.source("video", video)
        self.assertEqual(source["record"]["direction_id"], first["direction_id"])
        self.assertEqual(self.server.app.directions.path.read_bytes(), direction_bytes)
        self.assertEqual({str(path): path.read_bytes() for path in self.skill.rglob("*.json")}, case_bytes)
        self.assertEqual(self.request("POST", "/api/variant-links", self.body("video", video, other))[0], 409)
        self.save(self.body("video", video, first))
        self.assertEqual(self.server.app.directions.path.read_bytes(), direction_bytes)
        self.assertEqual({str(path): path.read_bytes() for path in self.skill.rglob("*.json")}, case_bytes)

    def test_video_product_data_only_supplements_exact_id_and_never_overrides_conflicts(self):
        first, other = self.direction(), self.direction("S2")
        video = "7777777777777777777"
        with patch.object(self.server.app.performance, "video_brief", return_value={video: {"product": "202H", "title": "历史视频"}}):
            self.save(self.body("video", video, first))
            raw = self.server.app.directions.read()
            raw["video_links"].append({"product": "202H", "video_id": video, "direction_id": other["direction_id"]})
            self.server.app.directions.write(raw)
            self.assertEqual(self.request("POST", "/api/variant-links", self.body("video", video, first))[0], 409)
        with patch.object(self.server.app.performance, "video_brief", return_value={video: {"product": "103N"}}):
            status, _, result = self.request("GET", "/api/variant-links/source?kind=video&id=" + video + "&product=202H")
            self.assertEqual(status, 409, result)
        self.assertEqual(self.request("GET", "/api/variant-links/source?kind=video&id=99999999&product=202H")[0], 404)

    def test_progress_rechecks_video_ownership_and_does_not_revive_a_missing_source(self):
        first, other = self.direction(), self.direction("S2")
        video = "7777777777777777777"
        path = "/api/direction-progress/" + first["direction_id"] + "?product=202H"
        with patch.object(self.server.app.performance, "video_brief", return_value={video: {"product": "202H"}}):
            self.save(self.body("video", video, first))
            self.assertEqual(self.ok("GET", path)["variants"][0]["records"]["videos"][0]["id"], video)
        with patch.object(self.server.app.performance, "video_brief", return_value={}):
            before = {str(file): file.read_bytes() for file in self.runtime.rglob("*.json")}
            result = self.ok("GET", path)
            self.assertEqual(result["variants"][0]["records"]["videos"], [])
            self.assertEqual(before, {str(file): file.read_bytes() for file in self.runtime.rglob("*.json")})
        with patch.object(self.server.app.performance, "video_brief", return_value={video: {"product": "103N"}}):
            self.assertEqual(self.ok("GET", path)["variants"][0]["records"]["videos"], [])
        raw = self.server.app.directions.read()
        raw["video_links"].append({"product": "202H", "video_id": video, "direction_id": other["direction_id"]})
        self.server.app.directions.write(raw)
        with patch.object(self.server.app.performance, "video_brief", return_value={video: {"product": "202H"}}):
            result = self.ok("GET", path)
            self.assertEqual(result["variants"][0]["records"]["videos"], [])
            self.assertNotIn(video, [row["id"] for row in result["unassigned"]["videos"]])
            self.assertEqual(len(self.server.app.creation.public()["variant_links"]), 1)

    def test_cross_direction_duplicate_video_links_stay_unassigned_only_while_source_exists(self):
        first, other = self.direction(), self.direction("S2")
        video = "7777777777777777777"
        with patch.object(self.server.app.performance, "video_brief", return_value={video: {"product": "202H"}}):
            link = self.save(self.body("video", video, first))["link"]
            raw = self.server.app.creation.read()
            raw["variant_links"].append({**copy.deepcopy(link), "direction_id": other["direction_id"],
                                        "variant_ref": variant_ref(other)})
            self.server.app.creation.write(raw)
            before = self.server.app.creation.path.read_bytes()
            for direction in (first, other):
                result = self.ok("GET", "/api/direction-progress/" + direction["direction_id"] + "?product=202H")
                self.assertEqual(result["variants"][0]["records"]["videos"], [])
                self.assertEqual([row["id"] for row in result["unassigned"]["videos"]], [video])
            self.assertEqual(self.server.app.creation.path.read_bytes(), before)
        with patch.object(self.server.app.performance, "video_brief", return_value={}):
            for direction in (first, other):
                result = self.ok("GET", "/api/direction-progress/" + direction["direction_id"] + "?product=202H")
                self.assertEqual(result["variants"][0]["records"]["videos"], [])
                self.assertEqual(result["unassigned"]["videos"], [])

    def test_source_requires_single_fields_and_unknown_records_do_not_write(self):
        before = self.server.app.creation.read()
        for query in ("kind=script&id=SCR-000000000000&product=202H&product=202H", "kind=script&product=202H"):
            self.assertEqual(self.request("GET", "/api/variant-links/source?" + query)[0], 400)
        self.assertEqual(self.request("GET", "/api/variant-links/source?kind=script&id=SCR-000000000000&product=202H")[0], 404)
        self.assertEqual(self.server.app.creation.read(), before)


if __name__ == "__main__":
    unittest.main()
