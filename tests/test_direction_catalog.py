"""Direction business records and migration use disposable libraries only."""
from __future__ import annotations

import copy
import json
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_server as fixture
from direction_store import DirectionStore


def install_points(root):
    path = root / "references" / "products" / "202H-selling-point-grading.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    codes = ["S1", "S2", "S3", "A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4", "B5"]
    rows = [f"| {code} | {code}的独立购买理由 | {code}的分级用途 | {code}的证据边界 |" for code in codes]
    path.write_text("# 宽口径分级\n\n| 编号 | 卖点 | 为什么是这一级 | 依据状态 |\n|---|---|---|---|\n" +
                    "\n".join(rows) + "\n\n| 编号 | 卖点 | 为什么是这一级 | 依据状态 |\n|---|---|---|---|\n| S99 | 不导入第二表 | 不导入 | 不导入 |\n", encoding="utf-8")
    return path


def direction(code="S1", suffix=""):
    return {"direction_id": "202H-" + code, "main_difference": code + " 旧案例短标题",
            "concern": "装夹是否麻烦" + suffix, "closing": "按实际挂车价判断" + suffix, "variant_type": "direction"}


class DirectionCatalogTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    detail = fixture.WorkbenchHTTPTests.detail
    body = fixture.WorkbenchHTTPTests.body
    save = fixture.WorkbenchHTTPTests.save
    record_bytes = fixture.WorkbenchHTTPTests.record_bytes

    def catalog(self, product="202H"):
        status, _, result = self.request("GET", "/api/direction-catalog" + ("?product=" + product if product else ""))
        self.assertEqual(status, 200, result)
        return result

    def setup_direction(self, code="S1"):
        install_points(self.skill)
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["test_direction"] = direction(code)
        self.store.append(self.identifier, {"kind": "editorial", "text": "原方向来源", "current": current})
        return self.catalog()

    def mutate(self, path, **values):
        body = {"revision": self.catalog("")["revision"], "request_id": uuid4().hex, **values}
        return self.request("POST", path, body)

    def test_first_table_and_eight_cases_import_once_without_rewriting_history(self):
        source = install_points(self.skill)
        for index, code in enumerate(("S1", "S1", "S1", "S2", "S2", "A2", "A2", "A2")):
            current = fixture.snapshot(self.video)
            current["metrics"]["video_id"] = str(7000000000000000000 + index)
            current["test_direction"] = direction(code, str(index))
            if index == 0:
                self.store.append(self.identifier, {"kind": "editorial", "text": "补齐原方向", "current": current})
            else:
                self.store.add({"title": "迁移原件", "product": "202H" if index % 2 else "SHARPAL 202H",
                                "original": {"text": "原文不能改"}, "feedback": {"kind": "editorial", "text": "首次收录"}, "current": current})
        before = {str(path): path.read_bytes() for path in (self.skill / "cases").rglob("*.json")}
        catalog = self.catalog()
        self.assertEqual(len(catalog["selling_points"]), 12)
        self.assertEqual({item["direction_id"] for item in catalog["directions"]}, {"202H-S1", "202H-S2", "202H-A2"})
        self.assertEqual(sum(item["case_count"] for item in catalog["directions"]), 8)
        self.assertEqual(len(catalog["video_links"]), 8)
        self.assertEqual(sum(len(item["variants"]) for item in catalog["directions"]), 8)
        self.assertEqual(catalog["migration"]["conflicts"], [])
        point = next(item for item in catalog["selling_points"] if item["code"] == "S3")
        self.assertEqual(point["rationale"], "S3的分级用途")
        self.assertEqual(point["evidence"], "S3的证据边界")
        self.assertEqual(point["source"]["path"], str(source))
        saved = self.server.app.directions.path.read_bytes()
        self.assertEqual(self.catalog()["revision"], catalog["revision"])
        self.assertEqual(self.server.app.directions.path.read_bytes(), saved)
        self.assertEqual({str(path): path.read_bytes() for path in (self.skill / "cases").rglob("*.json")}, before)

    def test_independent_create_variant_edit_and_persistent_replay(self):
        install_points(self.skill)
        catalog = self.catalog()
        point = next(item for item in catalog["selling_points"] if item["code"] == "S3")
        body = {"revision": catalog["revision"], "request_id": uuid4().hex,
                "direction": {"product": "202H", "selling_point_id": point["id"], "concern": "先核刀宽", "closing": "说明适用条件", "status": "paused", "notes": "保留暂停原因"}}
        status, _, created = self.request("POST", "/api/direction-catalog", body)
        self.assertEqual(status, 200, created)
        self.assertEqual(created["direction"]["direction_id"], "202H-S3")
        self.assertEqual(created["catalog"]["directions"][0]["case_count"], 0)
        self.assertEqual(self.request("POST", "/api/direction-catalog", body)[0], 200)
        fresh_store = DirectionStore(self.runtime, self.skill)
        self.assertEqual(fresh_store.mutate("direction", body)["direction"], created["direction"])
        variant = {**body["direction"], "concern": "改另一条顾虑", "status": "testing", "notes": "不能覆盖旧备注",
                   "progression": "结果倒叙", "execution_change": "先给结果再讲原因"}
        status, _, changed = self.mutate("/api/direction-catalog", direction=variant)
        self.assertEqual(status, 200, changed)
        self.assertEqual(changed["direction"]["status"], "paused")
        self.assertEqual(changed["direction"]["notes"], "保留暂停原因")
        self.assertEqual(changed["direction"]["concern"], "先核刀宽")
        self.assertEqual(len(changed["direction"]["variants"]), 2)
        status, _, updated = self.mutate("/api/direction-catalog", operation="update", direction={**changed["direction"], "concern": "新的默认链", "status": "active"})
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["direction"]["concern"], "新的默认链")
        self.assertEqual(updated["direction"]["status"], "active")
        self.assertEqual(len(updated["direction"]["variants"]), 2)
        self.assertEqual(updated["direction"]["history"][-1]["concern"], "先核刀宽")
        status, _, detail = self.request("GET", "/api/direction-catalog/202H-S3?product=202H")
        self.assertEqual(status, 200, detail)
        self.assertEqual(detail["cases"], [])
        self.assertEqual(detail["scripts"], [])
        self.assertTrue(list((self.runtime / "directions" / "backups").glob("*.json")))

    def test_concurrent_writers_and_changed_idempotency_payload_conflict(self):
        catalog = self.setup_direction()
        point = copy.deepcopy(catalog["selling_points"][0])
        bodies = [{"revision": catalog["revision"], "request_id": uuid4().hex, "point": {**point, "title": "并发编辑" + str(index)}} for index in range(2)]
        barrier = threading.Barrier(2)
        results = []
        def write(body):
            barrier.wait()
            results.append(self.request("POST", "/api/selling-points", body))
        threads = [threading.Thread(target=write, args=(body,)) for body in bodies]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(row[0] for row in results), [200, 409])
        success = next(row[2] for row in results if row[0] == 200)
        original = next(body for body in bodies if body["point"]["title"] == success["point"]["title"])
        self.assertEqual(self.request("POST", "/api/selling-points", original)[0], 200)
        altered = {**original, "point": {**original["point"], "title": "不能复用操作标识"}}
        self.assertEqual(self.request("POST", "/api/selling-points", altered)[0], 409)

    def test_creating_an_existing_point_code_does_not_overwrite_it(self):
        catalog = self.setup_direction()
        original = copy.deepcopy(catalog["selling_points"][0])
        duplicate = {key: value for key, value in original.items() if key != "id"}
        duplicate["title"] = "误填为已有编号的新卖点"
        status, _, result = self.mutate("/api/selling-points", point=duplicate)
        self.assertEqual(status, 409, result)
        self.assertEqual(next(point for point in self.catalog()["selling_points"] if point["id"] == original["id"]), original)

    def test_case_derived_video_assignment_follows_changed_or_cleared_identity(self):
        catalog = self.setup_direction()
        before = self.detail()
        old_video = before["current"]["metrics"]["video_id"]
        new_video = "7999999999999999999"
        body = self.body(before)
        body["current"]["metrics"]["video_id"] = new_video
        self.assertEqual(self.save(body)[0], 200)
        catalog = self.catalog()
        links = {link["video_id"]: link for link in catalog["video_links"]}
        self.assertEqual(links[old_video]["direction_id"], "")
        self.assertEqual(links[new_video]["direction_id"], "202H-S1")
        self.assertEqual(catalog["directions"][0]["video_count"], 1)
        body = self.body()
        del body["current"]["metrics"]["video_id"]
        self.assertEqual(self.save(body)[0], 200)
        catalog = self.catalog()
        self.assertTrue(all(not link["direction_id"] for link in catalog["video_links"]))
        self.assertEqual(catalog["directions"][0]["video_count"], 0)

    def test_definition_edits_preserve_case_snapshots_and_allow_ordinary_save(self):
        catalog = self.setup_direction()
        original = self.detail()
        before = self.record_bytes()
        entity = catalog["directions"][0]
        point = next(point for point in catalog["selling_points"] if point["id"] == entity["selling_point_id"])
        self.assertEqual(self.mutate("/api/selling-points", point={**point, "title": "正式更新的主打表述"})[0], 200)
        main = "S1 正式更新的主打表述"
        status, _, result = self.mutate("/api/direction-catalog", operation="update", direction={**entity, "main_difference": main, "concern": "正式更新的顾虑"})
        self.assertEqual(status, 200, result)
        latest = self.detail()
        self.assertEqual(latest["revision"], original["revision"])
        self.assertEqual(latest["current"], original["current"])
        self.assertEqual(latest["resolved_direction"]["main_difference"], main)
        self.assertEqual(self.record_bytes(), before)
        body = self.body(latest)
        self.assertEqual(self.save(body)[0], 200)
        self.assertEqual(self.detail()["current"]["test_direction"], original["current"]["test_direction"])
        body = self.body()
        body["current"]["test_direction"]["main_difference"] = "S2 偷换主购买理由"
        self.assertEqual(self.save(body)[0], 409)
        current = fixture.new_case_snapshot(self.video)
        current["test_direction"] = {**direction(), "main_difference": main}
        self.store.add({"title": "使用规范新表述", "product": "202H", "original": {"text": "另一案例"},
                        "feedback": {"kind": "editorial", "text": "首次收录"}, "current": current})
        summary = self.request("GET", "/api/directions?product=202H")[2]
        self.assertFalse(summary["groups"][0]["definition_conflict"])
        self.assertEqual(len(summary["groups"][0]["definitions"]), 2)

    def test_catalog_failure_after_case_commit_returns_saved_warning_and_safe_replay(self):
        self.setup_direction()
        before = self.store.read_case(self.identifier)
        body = self.body()
        with patch.object(self.server.app.directions, "write", side_effect=OSError("fixture disk error")):
            status, _, result = self.save(body)
            self.assertEqual(status, 200, result)
            self.assertIn("案例已保存", result["warning"])
            self.assertEqual(self.save(body)[2], result)
        after = self.store.read_case(self.identifier)
        self.assertEqual(len(after["events"]), len(before["events"]) + 1)
        self.assertEqual(after["current"], result["case"]["current"])
        self.assertEqual(after["current"]["conclusion"], body["current"]["conclusion"])
        self.catalog()  # A later read retries the safe, independent import.

    def test_archive_detail_does_not_write_pending_catalog_migration(self):
        self.setup_direction()
        before = self.detail()
        path = self.skill / "references" / "products" / "202H-selling-point-grading.md"
        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n新的资料说明，留待目录同步。\n")
        body = {"revision": before["revision"], "request_id": uuid4().hex, "archived": True, "note": "归档案例"}
        with patch.object(self.server.app.directions, "write", side_effect=OSError("fixture disk error")):
            status, _, archived = self.request("POST", f"/api/cases/{self.identifier}/archive", body)
            self.assertEqual(status, 200, archived)
            self.assertTrue(archived["case"]["archived"])
            self.assertEqual(self.request("POST", f"/api/cases/{self.identifier}/archive", body)[2], archived)

    def test_external_conflict_is_preserved_and_state_digest_changes_with_catalog(self):
        self.setup_direction()
        initial = self.request("GET", "/api/state")[2]
        current = fixture.new_case_snapshot(self.video)
        current["test_direction"] = {**direction(), "main_difference": "S2 外部工具误用了同编号"}
        self.store.add({"title": "冲突原件", "product": "202H", "original": {"text": "原文"}, "feedback": {"kind": "editorial", "text": "外部收录"}, "current": current})
        catalog = self.catalog()
        self.assertTrue(catalog["directions"][0]["definition_conflict"])
        self.assertEqual(len(catalog["migration"]["conflicts"]), 1)
        self.assertTrue(any(item["conflict"] for item in catalog["directions"][0]["variants"]))
        state = self.request("GET", "/api/state?since=" + initial["digest"])[2]
        self.assertNotIn("unchanged", state)
        old_digest = state["digest"]
        point = catalog["selling_points"][0]
        self.assertEqual(self.mutate("/api/selling-points", point={**point, "evidence": "独立证据更新"})[0], 200)
        state = self.request("GET", "/api/state?since=" + old_digest)[2]
        self.assertNotIn("unchanged", state)
        self.assertEqual(state["paths"]["directions"], str(self.runtime / "directions"))

    def test_video_product_validation_inheritance_and_explicit_unlink(self):
        catalog = self.setup_direction()
        entity = catalog["directions"][0]
        video = "7999999999999999999"
        with patch.object(self.server.app.performance, "video_brief", return_value={video: {"product": "202H", "title": "未收录视频"}}):
            self.assertEqual(self.mutate("/api/video-directions", product="999H", video_id=video, direction_id=entity["direction_id"])[0], 409)
            status, _, result = self.mutate("/api/video-directions", product="202H", video_id=video, direction_id=entity["direction_id"])
            self.assertEqual(status, 200, result)
            current = fixture.snapshot(self.video)
            current["metrics"]["video_id"] = video
            status, _, added = self.request("POST", "/api/cases", {"request_id": uuid4().hex, "current": current, "product": "202H", "note": "收录已绑定方向的视频"})
            self.assertEqual(status, 200, added)
            self.assertEqual(added["case"]["current"]["test_direction"]["direction_id"], entity["direction_id"])
            # An existing case may explicitly clear its direction. A later plain
            # edit must not reapply the video default intended for new intake.
            cleared = copy.deepcopy(added["case"]["current"])
            del cleared["test_direction"]
            new_id = added["case"]["case_id"]
            status, _, saved = self.request("POST", f"/api/cases/{new_id}", {"request_id": uuid4().hex,
                "revision": added["case"]["revision"], "current": cleared, "note": "明确清空案例方向"})
            self.assertEqual(status, 200, saved)
            cleared["conclusion"] += " 普通修改"
            status, _, saved = self.request("POST", f"/api/cases/{new_id}", {"request_id": uuid4().hex,
                "revision": saved["case"]["revision"], "current": cleared, "note": "后续普通修改"})
            self.assertEqual(status, 200, saved)
            self.assertNotIn("test_direction", saved["case"]["current"])
            status, _, unlinked = self.mutate("/api/video-directions", product="202H", video_id=video, direction_id="")
            self.assertEqual(status, 200, unlinked)
            self.assertEqual(unlinked["link"]["direction_id"], "")
            links = self.catalog()["video_links"]
            self.assertEqual(next(link for link in links if link["video_id"] == video)["direction_id"], "")
        with patch.object(self.server.app.performance, "video_brief", return_value={}):
            self.assertEqual(self.mutate("/api/video-directions", product="202H", video_id=video, direction_id=entity["direction_id"])[0], 400)

    def test_permanent_delete_removes_case_provenance_and_internal_backup_copies(self):
        catalog = self.setup_direction()
        entity = catalog["directions"][0]
        self.assertEqual(self.mutate("/api/direction-catalog", operation="update", direction={**entity, "notes": "独立方向仍保留"})[0], 200)
        original = self.detail()
        status, _, archived = self.request("POST", f"/api/cases/{self.identifier}/archive", {"request_id": uuid4().hex, "revision": original["revision"], "archived": True, "note": "准备彻底删除"})
        self.assertEqual(status, 200, archived)
        plan = self.request("GET", f"/api/cases/{self.identifier}/delete-preview")[2]
        status, _, deleted = self.request("POST", f"/api/cases/{self.identifier}/delete", {"request_id": uuid4().hex, "revision": archived["case"]["revision"], "plan_token": plan["plan_token"], "confirmation": self.identifier})
        self.assertEqual(status, 200, deleted)
        self.assertTrue(deleted["deleted"])
        self.assertEqual(len(self.catalog()["directions"]), 1)
        for path in (self.runtime / "directions").rglob("*.json"):
            self.assertNotIn(self.identifier, path.read_text(encoding="utf-8"), str(path))



class DirectionRuleTests(unittest.TestCase):
    """Rules that keep directions and variants comparable."""
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    catalog = DirectionCatalogTests.catalog
    mutate = DirectionCatalogTests.mutate
    setup_direction = DirectionCatalogTests.setup_direction
    detail = fixture.WorkbenchHTTPTests.detail
    body = fixture.WorkbenchHTTPTests.body
    save = fixture.WorkbenchHTTPTests.save

    def created(self, code="S1", **extra):
        install_points(self.skill)
        catalog = self.catalog()
        point = next(item for item in catalog["selling_points"] if item["code"] == code)
        return point, self.mutate("/api/direction-catalog", direction={"product": "202H", "selling_point_id": point["id"],
                                  "concern": "装夹麻烦吗 → A3 一次装夹翻面继续", "closing": "B4 当前渠道价格", **extra})

    def test_variant_needs_a_new_progression_and_update_keeps_variants(self):
        point, (status, _, created) = self.created(progression="问题-解决")
        self.assertEqual(status, 200, created)
        self.assertTrue(created["direction"]["variants"][0]["baseline"])
        base = {"product": "202H", "selling_point_id": point["id"], "concern": "先看结果 → A2 双夹", "closing": "B4 当前渠道价格",
                "execution_change": "结果开头"}
        self.assertEqual(self.mutate("/api/direction-catalog", direction={**base, "progression": "问题-解决"})[0], 409)
        self.assertEqual(self.mutate("/api/direction-catalog", direction={**base, "progression": "随便"})[0], 400)
        status, _, added = self.mutate("/api/direction-catalog", direction={**base, "progression": "结果倒叙"})
        self.assertEqual(status, 200, added)
        self.assertEqual(added["direction"]["variants"][1]["progression"], "结果倒叙")
        entity = added["direction"]
        self.assertEqual(self.mutate("/api/direction-catalog", operation="update", direction={**entity, "concern": "1"})[0], 400)
        self.assertEqual(self.mutate("/api/direction-catalog", operation="update", direction={**entity, "concern": "S9 不存在 → B1"})[0], 400)
        self.assertEqual(self.mutate("/api/direction-catalog", operation="update", direction={**entity, "concern": "耐用吗 → 做工扎实（未编号）"})[0], 400)
        status, _, updated = self.mutate("/api/direction-catalog", operation="update", direction={**entity, "concern": "装夹麻烦吗 → A3 翻面"})
        self.assertEqual(status, 200, updated)
        self.assertEqual(len(updated["direction"]["variants"]), 2)
        status, _, marked = self.mutate("/api/direction-catalog", operation="variant_meta",
                                        direction={"product": "202H", "direction_id": "202H-S1", "index": 1, "baseline": True})
        self.assertEqual(status, 200, marked)
        self.assertEqual([bool(item.get("baseline")) for item in marked["direction"]["variants"]], [False, True])
        self.assertEqual(self.mutate("/api/direction-catalog", operation="variant_meta",
                                     direction={"product": "202H", "direction_id": "202H-S1", "index": 1, "progression": "问题-解决"})[0], 409)

    def test_non_s_lead_needs_a_reason(self):
        _, (status, _, result) = self.created("A2")
        self.assertEqual(status, 400, result)
        _, (status, _, result) = self.created("A2", lead_reason="只测装夹画面")
        self.assertEqual(status, 200, result)
        self.assertIn("非 S 主打理由：只测装夹画面", result["direction"]["notes"])
        self.assertEqual(result["catalog"]["directions"][0]["lead_tier"], "A")

    def test_grading_file_changes_are_flagged_not_applied(self):
        path = install_points(self.skill)
        self.catalog()
        path.write_text(path.read_text(encoding="utf-8").replace("| S1 | S1的独立购买理由 |", "| S1 | S1 改过的写法 |"), encoding="utf-8")
        point = next(item for item in self.catalog()["selling_points"] if item["code"] == "S1")
        self.assertEqual(point["title"], "S1的独立购买理由")
        self.assertTrue(point["grading_mismatch"])
        self.assertEqual(point["grading_row"]["title"], "S1 改过的写法")
        other = next(item for item in self.catalog()["selling_points"] if item["code"] == "S2")
        self.assertFalse(other["grading_mismatch"])
        self.assertIsNone(other["performance"])

    def test_point_delete_is_blocked_while_used_and_not_reimported(self):
        source = self.skill / "references" / "products" / "202H-selling-point-grading.md"
        catalog = self.setup_direction("S1")
        points = {item["code"]: item for item in catalog["selling_points"]}
        delete = lambda point: self.mutate("/api/selling-points", operation="delete", point={"id": point["id"], "product": "202H"})
        status, _, result = delete(points["S1"])
        self.assertEqual(status, 409, result)
        self.assertIn("202H-S1", result["error"])
        self.assertEqual(self.mutate("/api/selling-points", point={**points["A3"], "status": "merged", "merged_into": "A1"})[0], 200)
        status, _, result = delete(points["A1"])
        self.assertEqual(status, 409, result)
        self.assertIn("A3 并入了它", result["error"])
        status, _, result = delete(points["B5"])
        self.assertEqual(status, 200, result)
        self.assertNotIn("B5", {item["code"] for item in self.catalog()["selling_points"]})
        source.write_text(source.read_text(encoding="utf-8") + "\n分级表后来又改过。\n", encoding="utf-8")
        catalog = self.catalog()
        self.assertNotIn("B5", {item["code"] for item in catalog["selling_points"]})
        self.assertEqual([item["code"] for item in catalog["migration"]["deleted_points"]], ["B5"])
        self.assertEqual(delete(points["B5"])[0], 404)
        status, _, result = self.mutate("/api/selling-points", point={"product": "202H", "code": "B5", "title": "重新写的 B5", "status": "testing"})
        self.assertEqual(status, 200, result)
        self.assertNotEqual(result["point"]["id"], points["B5"]["id"])

    def test_renumbered_point_carries_references_and_keeps_direction_id(self):
        source = self.skill / "references" / "products" / "202H-selling-point-grading.md"
        catalog = self.setup_direction("S1")
        points = {item["code"]: item for item in catalog["selling_points"]}
        entity = catalog["directions"][0]
        self.assertEqual(self.mutate("/api/selling-points", point={**points["B1"], "same_fact_as": ["A2"]})[0], 200)
        self.assertEqual(self.mutate("/api/selling-points", point={**points["A3"], "status": "merged", "merged_into": "A2"})[0], 200)
        status, _, result = self.mutate("/api/direction-catalog", operation="update", direction={**entity, "concern": "装夹麻烦吗 → A2 双夹；A1 不受影响"})
        self.assertEqual(status, 200, result)
        status, _, result = self.mutate("/api/selling-points", point={**points["A2"], "code": "s5", "tier": "S"})
        self.assertEqual(status, 200, result)
        self.assertEqual((result["point"]["id"], result["point"]["code"], result["point"]["tier"], result["point"]["former_codes"]),
                         (points["A2"]["id"], "S5", "S", ["A2"]))
        catalog = self.catalog()
        renamed = {item["code"]: item for item in catalog["selling_points"]}
        self.assertEqual(renamed["B1"]["same_fact_as"], ["S5"])
        self.assertEqual(renamed["A3"]["merged_into"], "S5")
        entity = catalog["directions"][0]
        self.assertEqual(entity["concern"], "装夹麻烦吗 → S5 双夹；A1 不受影响")
        self.assertIn("A2 改为 S5", entity["history"][-1]["reason"])
        for code in ("A2", "S1"):
            with self.subTest(code=code):
                status, _, result = self.mutate("/api/selling-points", point={"product": "202H", "code": code, "title": "占用旧编号", "status": "testing"})
                self.assertEqual(status, 409, result)
        status, _, result = self.mutate("/api/selling-points", point={**renamed["S1"], "code": "S8"})
        self.assertEqual(status, 200, result)
        entity = next(item for item in self.catalog()["directions"] if item["selling_point_id"] == points["S1"]["id"])
        self.assertEqual(entity["direction_id"], "202H-S1")
        self.assertTrue(entity["main_difference"].startswith("S8 "))
        self.assertEqual(entity["chain_issues"], [])
        status, _, result = self.mutate("/api/direction-catalog", operation="update", direction={**entity, "closing": "改号后仍能编辑方向"})
        self.assertEqual(status, 200, result)
        self.assertEqual(self.save(self.body(self.detail()))[0], 200)
        source.write_text(source.read_text(encoding="utf-8") + "\n分级表后来又改过。\n", encoding="utf-8")
        codes = [item["code"] for item in self.catalog()["selling_points"]]
        self.assertNotIn("A2", codes)
        self.assertNotIn("S1", codes)
        self.assertEqual(len(codes), 12)

    def test_point_status_same_fact_and_audience(self):
        path = install_points(self.skill)
        content = path.read_text(encoding="utf-8").replace("| 编号 | 卖点 | 为什么是这一级 | 依据状态 |\n|---|---|---|---|\n| S1", "| 编号 | 卖点 | 为什么是这一级 | 依据状态 | 状态 |\n|---|---|---|---|---|\n| S1", 1)
        content = content.replace("| S3 | S3的独立购买理由 | S3的分级用途 | S3的证据边界 |", "| S3 | S3的独立购买理由 | S3的分级用途 | S3的证据边界 | 待核候选 |")
        path.write_text(content, encoding="utf-8")
        points = {item["code"]: item for item in self.catalog()["selling_points"]}
        self.assertEqual(points["S3"]["status"], "candidate")
        self.assertEqual(points["S1"]["status"], "testing")
        self.assertFalse(points["S3"]["grading_mismatch"])
        new = {"product": "202H", "code": "S7", "title": "坏刀修回来"}
        self.assertEqual(self.mutate("/api/selling-points", point={**new, "status": "verified"})[0], 400)
        status, _, result = self.mutate("/api/selling-points", point={**new, "status": "candidate", "same_fact_as": ["S2"]})
        self.assertEqual(status, 400, result)
        self.assertIn("目标人群", result["error"])
        status, _, result = self.mutate("/api/selling-points", point={**new, "status": "candidate", "same_fact_as": ["S2"], "motive_difference": "S2 讲多精，S7 讲能不能修"})
        self.assertEqual(status, 200, result)
        self.assertEqual(self.mutate("/api/selling-points", point={**points["S3"], "status": "merged", "merged_into": "S98"})[0], 400)
        status, _, result = self.mutate("/api/selling-points", point={**points["S3"], "status": "merged", "merged_into": "a1"})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["point"]["merged_into"], "A1")
        _, (status, _, result) = self.created("S3")
        self.assertEqual(status, 400, result)
        _, (status, _, created) = self.created(audience="守不住角度的人")
        self.assertEqual(status, 200, created)
        self.assertEqual(created["direction"]["audience"], "守不住角度的人")
        status, _, updated = self.mutate("/api/direction-catalog", operation="update", direction={**created["direction"], "audience": "家庭厨师"})
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["direction"]["audience"], "家庭厨师")
        self.assertEqual(updated["direction"]["history"][-1]["audience"], "守不住角度的人")
        status, _, marked = self.mutate("/api/direction-catalog", operation="variant_meta",
                                        direction={"product": "202H", "direction_id": "202H-S1", "index": 0, "audience": "节前刀座"})
        self.assertEqual(status, 200, marked)
        self.assertEqual(marked["direction"]["variants"][0]["audience"], "节前刀座")


if __name__ == "__main__":
    unittest.main(verbosity=2)
