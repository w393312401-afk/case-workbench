import copy
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from datetime import date

from content_management import ContentManagementStore, _finished, build_content_items
from content_naming import content_heading, content_title, task_topic_hint, variant_labels
from creation_store import CreationError
from direction_store import fingerprint
from workbench_video_publish import reviewed_source


def task(identifier="AIT-one", round_id="AIR-one", **extra):
    return {"task_id": identifier, "product": "202H", "context": {"title": "候选稿", "direction_id": "DIR-one"},
            "status": "completed", "management_state": "pending_selection", "updated_at": "2026-10-13T12:00:00Z",
            "rounds": [{"round_id": round_id, "status": "completed"}], **extra}


def script(identifier="SCR-one", family="SCR-one", version=1, **extra):
    return {"id": identifier, "family_id": family, "version": version, "product": "202H", "title": "稿件",
            "direction_id": "DIR-one", "status": "adopted", "source": {"task_id": "AIT-one", "round_id": "AIR-one"},
            "updated_at": "2026-10-14T10:00:00Z", **extra}


def job(identifier="PRD-one", script_id="SCR-one", **extra):
    return {"id": identifier, "script_id": script_id, "script_version": 1, "product": "202H", "title": "视频",
            "status": "draft", "source": {"task_id": "AIT-one", "round_id": "AIR-one"}, **extra}


class ContentQueueTests(unittest.TestCase):
    def queue(self, tasks=None, scripts=None, jobs=None, management=None):
        return build_content_items(tasks or [], scripts or [], jobs or [], management)

    def test_same_direction_tasks_use_creation_time_in_beijing_and_never_update_time(self):
        context = {"title": "202H-S1", "direction_id": "202H-S1", "writing_topic": "202H-S1 别再靠手腕猜刃角"}
        first = task(context=context, created_at="2026-10-19T05:57:42Z", updated_at="2026-10-22T01:00:00Z")
        second = task("AIT-two", context=context, created_at="2026-10-19T05:58:17+00:00")
        other = task("AIT-other-product", product="OTHER", context=context, created_at="2026-10-19T05:58:17Z")
        original = copy.deepcopy([first, second, other])
        rows = {row["id"]: row for row in self.queue(tasks=[first, second, other])}
        self.assertEqual(rows["task:AIT-one"]["title"], "别再靠手腕猜刃角｜候选稿")
        self.assertEqual(rows["task:AIT-one"]["task_started_label"], "2026/10/19 13:57 发起")
        self.assertEqual(rows["task:AIT-two"]["task_started_label"], "2026/10/19 13:58 发起")
        self.assertEqual(rows["task:AIT-other-product"]["task_started_label"], "")
        self.assertEqual([first, second, other], original)

    def test_task_start_collisions_include_seconds_then_stable_ids_and_missing_dates_stay_honest(self):
        tasks = [task("AIT-a", created_at="2026-10-19T13:57:42+08:00"),
                 task("AIT-b", created_at="2026-10-19T05:57:43Z"),
                 task("AIT-c", created_at="2026-10-19T05:57:43Z"),
                 task("AIT-d", created_at="invalid"), task("AIT-e")]
        rows = {row["id"]: row for row in self.queue(tasks=tasks)}
        self.assertEqual(rows["task:AIT-a"]["task_started_label"], "2026/10/19 13:57:42 发起")
        self.assertEqual(rows["task:AIT-b"]["task_started_label"], "2026/10/19 13:57:43 发起 · AIT-b")
        self.assertEqual(rows["task:AIT-c"]["task_started_label"], "2026/10/19 13:57:43 发起 · AIT-c")
        self.assertEqual(rows["task:AIT-d"]["task_started_label"], "发起时间未记录 · AIT-d")
        self.assertEqual(rows["task:AIT-e"]["task_started_label"], "发起时间未记录 · AIT-e")
        reversed_rows = {row["id"]: row for row in self.queue(tasks=list(reversed(tasks)))}
        self.assertEqual(rows, reversed_rows)

    def test_task_topics_use_explicit_first_request_titles_or_frozen_direction_labels(self):
        instruction = "请按格式交付，先核对资料。\n" * 20 + "**制作单：** 202H-S1 会手磨，也可以用导杆\n后面的交付要求"
        self.assertEqual(task_topic_hint(instruction), "202H-S1 会手磨，也可以用导杆")
        candidate = task(context={"title": "202H-S1", "direction_id": "202H-S1"},
                         rounds=[{"instruction": instruction}, {"instruction": "主题：这只是继续修改指令"}])
        self.assertEqual(self.queue(tasks=[candidate])[0]["title"], "会手磨，也可以用导杆｜候选稿")
        candidate.update(rounds=[], context={"title": "202H-S1", "direction_id": "202H-S1", "direction_title": "S1 定角不用猜"})
        self.assertEqual(self.queue(tasks=[candidate])[0]["title"], "定角不用猜｜候选稿")
        self.assertEqual(task_topic_hint("请先检查标题：是否清楚\n继续改一下。"), "")

    def test_concrete_titles_keep_exact_code_boundaries_and_generic_scripts_use_translation(self):
        self.assertEqual(content_title({"title": "202H-S20 修复旧刀"}, "video", direction="202H-S2"),
                         "202H-S20 修复旧刀｜视频")
        self.assertEqual(content_title({"title": "202H-S2 变体 B", "translation_zh": "本稿中文主题\n第二句", "text": "Other title."},
                                       "script", direction="202H-S2"), "本稿中文主题｜脚本方案 B")

    def test_multiple_candidates_and_deliveries_are_not_merged_by_product_direction_or_source(self):
        rows = self.queue([task(), task("AIT-unadopted")], [script(), script("SCR-two", "SCR-two")],
                          [job(), job("PRD-two"), job("PRD-other", "SCR-two")])
        self.assertEqual({row["id"] for row in rows}, {"video:PRD-one", "video:PRD-two", "video:PRD-other", "task:AIT-unadopted"})
        self.assertEqual(len(rows), 4)
        for row in rows:
            if row["kind"] == "video":
                self.assertEqual(len(row["production_ids"]), 1)
                self.assertEqual(row["task_ids"], ["AIT-one"])

    def test_multiple_script_families_from_one_candidate_remain_independent(self):
        rows = self.queue([task()], [script(), script("SCR-two", "SCR-two")])
        self.assertEqual({row["id"] for row in rows}, {"script:SCR-one", "script:SCR-two"})
        self.assertTrue(all(row["needs_attention"] for row in rows))

    def test_version_family_is_one_candidate_and_new_unproduced_version_stays_available(self):
        versions = [script(), script("SCR-v2", "SCR-one", 2)]
        rows = self.queue([task()], versions, [job()])
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(set(by_id), {"video:PRD-one", "script:SCR-one"})
        self.assertEqual(by_id["video:PRD-one"]["script_ids"], ["SCR-one"])
        self.assertEqual(by_id["script:SCR-one"]["script_ids"], ["SCR-one", "SCR-v2"])
        self.assertEqual(by_id["script:SCR-one"]["next_hash"], "#ai/AIT-one?round=AIR-one&adopted=SCR-v2")
        rows = self.queue([task()], versions, [job(script_id="SCR-v2")])
        self.assertEqual([row["id"] for row in rows], ["video:PRD-one"])

    def test_unproduced_copy_opens_its_exact_writing_round_with_version_details_secondary(self):
        origin = task(rounds=[{"round_id": "AIR-one"}, {"round_id": "AIR-newer"}])
        first = script()
        latest = script("SCR-v2", "SCR-one", 2)
        before = copy.deepcopy([origin, first, latest])
        rows = self.queue([origin], [first, latest])
        row = next(row for row in rows if row["kind"] == "script")
        self.assertEqual(row["next_hash"], "#ai/AIT-one?round=AIR-one&adopted=SCR-v2")
        self.assertEqual(row["next_label"], "开始制作")
        details = [link for link in row["links"] if link["label"].startswith("采用稿版本详情")]
        self.assertEqual(details, [
            {"label": "采用稿版本详情 · 第 1 版", "hash": "#content/SCR-one"},
            {"label": "采用稿版本详情 · 第 2 版", "hash": "#content/SCR-v2"}])
        self.assertEqual([origin, first, latest], before)

    def test_manual_missing_or_other_product_writing_source_keeps_copy_details_available(self):
        for origin, source in ((None, {}), (None, {"task_id": "AIT-one", "round_id": "AIR-one"}),
                               (task(product="OTHER"), {"task_id": "AIT-one", "round_id": "AIR-one"}),
                               (task(), {"task_id": "AIT-one"})):
            with self.subTest(origin=origin, source=source):
                rows = self.queue([origin] if origin else [], [script(source=source)])
                row = next(row for row in rows if row["kind"] == "script")
                self.assertEqual(row["next_hash"], "#content/SCR-one")
                self.assertTrue(any(link["hash"] == "#content/SCR-one" for link in row["links"]))

    def test_latest_copy_uses_its_own_source_instead_of_an_older_family_source(self):
        latest = script("SCR-v2", "SCR-one", 2, source={"task_id": "AIT-other", "round_id": "AIR-other"})
        rows = self.queue([task(), task("AIT-other", "AIR-other")], [script(), latest])
        row = next(row for row in rows if row["kind"] == "script")
        self.assertEqual(row["next_hash"], "#ai/AIT-other?round=AIR-other&adopted=SCR-v2")

    def test_latest_round_not_adopted_and_revision_task_keep_their_own_entry(self):
        newest = task(rounds=[{"round_id": "AIR-one"}, {"round_id": "AIR-two"}])
        self.assertEqual({row["id"] for row in self.queue([newest], [script()])}, {"task:AIT-one", "script:SCR-one"})
        compact = task(latest_round_id="AIR-two")
        compact.pop("rounds")
        self.assertEqual({row["id"] for row in self.queue([compact], [script()])}, {"task:AIT-one", "script:SCR-one"})
        self.assertIn("task:AIT-one", [row["id"] for row in self.queue([task(management_state="needs_revision")], [script()])])

    def test_family_uses_latest_unarchived_version_before_historical_latest(self):
        versions = [script(), script("SCR-v2", "SCR-one", 2, archived=True)]
        row = self.queue(scripts=versions)[0]
        self.assertEqual(row["next_hash"], "#content/SCR-one")
        self.assertFalse(row["archived"])
        self.assertEqual(row["script_ids"], ["SCR-one", "SCR-v2"])
        # A production of the active version already provides that entry;
        # a discarded later version does not create a pending candidate.
        rows = self.queue(scripts=versions, jobs=[job()])
        self.assertEqual([row["id"] for row in rows], ["video:PRD-one"])
        versions[0]["archived"] = True
        row = self.queue(scripts=versions)[0]
        self.assertEqual(row["next_hash"], "#content/SCR-v2")
        self.assertTrue(row["history"])

    def test_archived_script_preserves_its_underlying_business_stage(self):
        legacy = script(status="archived", video_id="1234567", published_on="2026-10-12",
                        history=[{"status": "adopted"}, {"status": "published"}, {"status": "archived"}])
        row = self.queue(scripts=[legacy])[0]
        self.assertTrue(row["source_archived"])
        self.assertTrue(row["archived"])
        self.assertEqual(row["stage"], "published")
        self.assertEqual(row["stage_label"], "已发布")

    def test_task_id_without_round_id_is_not_adoption_evidence(self):
        rows = self.queue([task()], [script(source={"task_id": "AIT-one"})])
        self.assertEqual({row["id"] for row in rows}, {"script:SCR-one", "task:AIT-one"})
        self.assertEqual(next(row for row in rows if row["kind"] == "script")["task_ids"], [])

    def test_publication_uses_each_actual_order_registration(self):
        published = {"post_id": "123456789", "published_on": "2026-10-12", "job_id": "PRD-one"}
        rows = self.queue(scripts=[script(status="published", video_id="123456789")],
                          jobs=[job(current_publication=published, status="editing"), job("PRD-two", status="approved")])
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id["video:PRD-one"]["stage"], "published")
        self.assertTrue(by_id["video:PRD-one"]["history"])
        self.assertEqual(by_id["video:PRD-two"]["stage"], "awaiting_publication")
        self.assertFalse(by_id["video:PRD-two"]["history"])
        # Waiting to be posted is listed under 待发布, not 待我处理.
        self.assertFalse(by_id["video:PRD-two"]["needs_attention"])

    def test_historical_video_registration_does_not_complete_new_editing_source(self):
        publication = {"post_id": "123456789", "published_on": "2026-10-12", "source_current": False}
        row = self.queue(jobs=[job(current_publication=publication, status="awaiting_review")])[0]
        self.assertEqual(row["stage"], "awaiting_review")
        self.assertFalse(row["history"])
        self.assertTrue(row["needs_attention"])
        self.assertTrue(any(link["label"] == "历史发布记录" and link["hash"].endswith("?step=publish") for link in row["links"]))

    def test_archive_marks_belong_to_their_own_row(self):
        metadata = {"revision": "human-r1", "items": {"task:AIT-one": {"archived": True}, "script:SCR-one": {"archived": True}}}
        rows = self.queue([task()], [script()], [job(), job("PRD-two")], management=metadata)
        self.assertEqual({row["id"] for row in rows}, {"video:PRD-one", "video:PRD-two"})
        self.assertFalse(any(row["archived"] for row in rows))
        self.assertEqual({row["management_revision"] for row in rows}, {"human-r1"})
        self.assertEqual(rows[0]["management"], {"archived": False})

    def test_archiving_one_order_does_not_archive_its_sibling_or_candidate(self):
        metadata = {"items": {"video:PRD-one": {"archived": True}}}
        rows = self.queue(scripts=[script(), script("SCR-v2", "SCR-one", 2)],
                          jobs=[job(), job("PRD-two")], management=metadata)
        by_id = {row["id"]: row for row in rows}
        self.assertTrue(by_id["video:PRD-one"]["history"])
        self.assertFalse(by_id["video:PRD-two"]["archived"])
        self.assertFalse(by_id["script:SCR-one"]["archived"])
        source = self.queue(jobs=[job(archived=True)], management={"items": {"video:PRD-one": {"archived": False}}})[0]
        self.assertTrue(source["source_archived"])
        self.assertTrue(source["archived"])

    def test_finished_orders_wait_to_be_posted_without_asking_the_reader(self):
        row = self.queue(jobs=[job(status="approved")])[0]
        self.assertEqual((row["stage"], row["stage_label"]), ("awaiting_publication", "待发布"))
        self.assertFalse(row["needs_attention"])
        self.assertFalse(row["history"])
        self.assertTrue(self.queue(jobs=[job(status="awaiting_review")])[0]["needs_attention"])
        posted = self.queue(jobs=[job(status="approved", current_publication={"post_id": "123456", "published_on": "2026-10-12"})])[0]
        self.assertEqual(posted["stage"], "published")
        self.assertTrue(posted["history"])

    def test_posted_videos_finish_seven_days_after_posting_and_skipped_ones_at_once(self):
        today = date(2026, 11, 5)
        self.assertFalse(_finished("video", "published", "2026-10-30", False, today))
        self.assertTrue(_finished("video", "published", "2026-10-29", False, today))
        self.assertTrue(_finished("video", "skipped", "", False, today))
        self.assertFalse(_finished("video", "published", "2026-10-01", True, today))
        self.assertFalse(_finished("video", "awaiting_publication", "", False, today))
        self.assertFalse(_finished("script", "published", "2026-10-01", False, today))
        self.assertFalse(_finished("video", "published", "bad-date", False, today))

    def test_skip_mark_settles_only_a_finished_unposted_video(self):
        marks = {"video:PRD-one": {"skip_publish": True, "skip_reason": "开头不行", "updated_at": "x"},
                 "video:PRD-two": {"skip_publish": True, "updated_at": "x"}}
        rows = {row["id"]: row for row in self.queue(jobs=[job(status="approved"), job("PRD-two", status="awaiting_review")],
                                                     management={"items": marks})}
        skipped = rows["video:PRD-one"]
        self.assertEqual((skipped["stage"], skipped["stage_label"], skipped["skip_reason"]), ("skipped", "不发了", "开头不行"))
        self.assertTrue(skipped["finished"])
        self.assertTrue(skipped["history"])
        self.assertFalse(skipped["needs_attention"])
        # A reopened edit shows its real stage again; the old mark does not hide it.
        self.assertEqual(rows["video:PRD-two"]["stage"], "awaiting_review")
        self.assertFalse(rows["video:PRD-two"]["finished"])
        self.assertEqual(rows["video:PRD-two"]["skip_reason"], "")

    def test_timeline_building_and_material_verification_have_correct_next_steps(self):
        waiting = job(timeline_building={"stage": "waiting", "progress": "等待达芬奇建线"})
        original = copy.deepcopy(waiting)
        row = self.queue(jobs=[waiting])[0]
        self.assertEqual((row["stage"], row["stage_label"]), ("timeline_building", "建线排队中"))
        self.assertTrue(row["busy"])
        self.assertFalse(row["needs_attention"])
        self.assertTrue(row["next_hash"].endswith("?step=timeline"))
        self.assertEqual(row["progress_text"], "等待达芬奇建线")
        self.assertEqual(waiting, original)
        row = self.queue(jobs=[job(status="needs_verification")])[0]
        self.assertEqual(row["stage_label"], "素材待核实")
        self.assertFalse(row["busy"])
        self.assertTrue(row["needs_attention"])

    def test_reviewed_variant_link_only_supplies_direction_and_does_not_merge(self):
        catalog = {"scripts": [script(direction_id="", source=None), script("SCR-two", "SCR-two", direction_id="", source=None)],
                   "variant_links": [{"kind": "script", "record_id": "SCR-one", "product": "202H", "direction_id": "DIR-reviewed"},
                                     {"kind": "script", "record_id": "SCR-two", "product": "202H", "direction_id": "DIR-reviewed"}]}
        rows = build_content_items([], catalog, [], [])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["direction_id"] == "DIR-reviewed" for row in rows))
        # creation_catalog supplies verified links on each public script.
        linked = script(direction_id="", source=None, variant_link={"kind": "script", "record_id": "SCR-one",
                        "product": "202H", "direction_id": "DIR-reviewed", "resolved_index": 0})
        row = self.queue(scripts=[linked])[0]
        self.assertEqual(row["direction_id"], "DIR-reviewed")
        row = self.queue(scripts=[linked], jobs=[job(direction_id="")])[0]
        self.assertEqual(row["direction_id"], "DIR-reviewed")


class ContentWorkflowTests(unittest.TestCase):
    def video(self):
        return job(lines=[{"number": 1, "spoken": "A complete kit."}], selected_take="TAK-current",
                   takes=[{"id": "TAK-current"}, {"id": "TAK-history"}],
                   timelines=[{"unique_id": "timeline-current", "take_id": "TAK-current", "saved": True}],
                   manual_completion={"at": "2026-10-20", "take_id": "TAK-current", "timeline_uid": "timeline-current"},
                   status="approved")

    def queue(self, row, kind="video"):
        return build_content_items([], [], [row] if kind == "video" else [])[0]

    def done(self, row):
        return {step["key"]: step["done"] for step in row["workflow_steps"]}

    def test_posted_video_is_flagged_only_while_its_calendar_mark_is_not_uploaded(self):
        posted = {"post_id": "7561234567890123456", "published_on": "2026-10-22", "source_current": True}
        for mark, pending in ((False, True), (True, False), (None, False)):
            with self.subTest(mark=mark):
                publication = {**posted, **({} if mark is None else {"calendar_uploaded": mark})}
                row = self.queue({**self.video(), "current_publication": publication})
                self.assertEqual(row["stage"], "published")
                self.assertIs(row["calendar_pending"], pending)
                self.assertEqual(row["published_on"], "2026-10-22")
        archived = self.queue({**self.video(), "archived": True, "current_publication": {**posted, "calendar_uploaded": False}})
        self.assertFalse(archived["calendar_pending"])
        self.assertFalse(self.queue(self.video())["calendar_pending"])

    def test_manual_edit_completion_does_not_complete_video_before_publication(self):
        source = self.video()
        before = copy.deepcopy(source)
        item = self.queue(source)
        self.assertEqual(self.done(item), {"lines": True, "voice": True, "timeline": True, "edit": True, "publish": False})
        self.assertFalse(item["workflow_complete"])
        self.assertEqual(item["workflow_label"], "视频链未完结")
        self.assertIn("尚未登记发布", item["summary_note"])
        self.assertEqual([step["key"] for step in item["workflow_steps"] if step["active"]], ["publish"])
        self.assertEqual((item["unit_count"], item["unit_label"]), (1, "个意群"))
        self.assertEqual(source, before)

    def test_only_current_reviewed_video_publication_completes_the_chain(self):
        source = self.video()
        publication = {"post_id": "1234567", "published_on": "2026-10-20", "source_current": True,
                       "source_signature": fingerprint(reviewed_source(source))}
        source["current_publication"] = publication
        self.assertTrue(self.queue(source)["workflow_complete"])
        self.assertTrue(all(not step["active"] for step in self.queue(source)["workflow_steps"]))
        for changes in ({"source_current": False}, {"source_signature": "old-version"}, {"published_on": ""}):
            with self.subTest(changes=changes):
                source["current_publication"] = {**publication, **changes}
                item = self.queue(source)
                self.assertFalse(item["workflow_complete"])
                self.assertFalse(self.done(item)["publish"])

    def test_historical_voice_timeline_and_review_cannot_turn_current_steps_green(self):
        source = self.video()
        source["selected_take"] = "TAK-history"
        item = self.queue(source)
        self.assertTrue(self.done(item)["voice"])
        self.assertFalse(self.done(item)["timeline"])
        self.assertFalse(self.done(item)["edit"])
        source["selected_take"] = "TAK-missing"
        self.assertFalse(self.done(self.queue(source))["voice"])
        source = self.video()
        source["timelines"].append({"unique_id": "timeline-new", "take_id": source["selected_take"], "saved": True})
        source["edits"] = [{"id": "edit-old", "timeline_uid": "timeline-current", "status": "awaiting_review", "approval": {"at": "today"}}]
        self.assertFalse(self.done(self.queue(source))["edit"])
        source["manual_completion"]["timeline_uid"] = "timeline-new"
        self.assertTrue(self.done(self.queue(source))["edit"])
        source["edits"].append({"id": "edit-new", "timeline_uid": "timeline-new", "status": "running"})
        self.assertFalse(self.done(self.queue(source))["edit"])

class ContentManagementStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = ContentManagementStore(self.temporary.name)

    def body(self, patch, **extra):
        return {"request_id": uuid4().hex, "revision": self.store.public()["revision"],
                "id": "task:AIT-one", "management": patch, **extra}

    def test_store_saves_only_sparse_human_metadata_and_preserves_business_files(self):
        existing = Path(self.temporary.name) / "creation" / "catalog.json"
        existing.parent.mkdir(parents=True)
        existing.write_text('{"frozen": "正文与发布事实"}', encoding="utf-8")
        original = existing.read_bytes()
        initial = self.store.public()["revision"]
        saved = self.store.mutate(self.body({"archived": True}), {"task:AIT-one"})
        self.assertNotEqual(saved["revision"], initial)
        self.assertEqual(existing.read_bytes(), original)
        self.assertEqual(self.store.path.name, "content-management.json")
        self.assertEqual(set(saved["management"]), {"archived", "updated_at"})
        reloaded = ContentManagementStore(self.temporary.name)
        self.assertEqual(reloaded.public(), self.store.public())
        self.store.mutate(self.body({"archived": False}))
        self.assertFalse(self.store.public()["items"]["task:AIT-one"]["archived"])

    def test_revision_conflict_and_idempotent_retry_survive_restarts(self):
        body = self.body({"archived": True})
        first = self.store.mutate(body)
        stale = self.body({"archived": False}, revision=body["revision"])
        with self.assertRaises(CreationError) as conflict:
            self.store.mutate(stale)
        self.assertEqual(conflict.exception.status, 409)
        reloaded = ContentManagementStore(self.temporary.name)
        self.assertEqual(reloaded.mutate(body, allowed_ids=set()), first)
        self.assertEqual(reloaded.replay(body), first)
        different = {**body, "management": {"archived": False}}
        with self.assertRaises(CreationError) as conflict:
            reloaded.mutate(different)
        self.assertEqual(conflict.exception.status, 409)

    def test_only_an_explicit_archive_switch_is_accepted(self):
        invalid = [{"archived": "true"}, {"archived": 1}, {"archived": None}, {"priority": "high"},
                   {"target_date": "2026-10-18"}, {"paused": True}, {"note": "备注"}, {"script_text": "覆盖正文"}, {}]
        for patch in invalid:
            with self.subTest(patch=patch), self.assertRaises(CreationError) as error:
                self.store.mutate(self.body(patch))
            self.assertEqual(error.exception.status, 400)
        self.assertFalse(self.store.path.exists())
        self.store.mutate(self.body({"archived": False}))
        self.assertFalse(self.store.public()["items"]["task:AIT-one"]["archived"])


    def test_skip_mark_takes_a_switch_and_a_short_reason(self):
        for patch in ({"skip_publish": "yes"}, {"skip_reason": 3}, {"skip_reason": "长" * 201}):
            with self.subTest(patch=patch), self.assertRaises(CreationError) as error:
                self.store.mutate(self.body(patch))
            self.assertEqual(error.exception.status, 400)
        self.store.mutate(self.body({"skip_publish": True, "skip_reason": "同方向已发过"}))
        entry = self.store.public()["items"]["task:AIT-one"]
        self.assertEqual((entry["skip_publish"], entry["skip_reason"]), (True, "同方向已发过"))

    def test_unknown_entry_and_corrupt_store_fail_without_overwriting(self):
        with self.assertRaises(CreationError) as unknown:
            self.store.mutate(self.body({"archived": True}), allowed_ids={"task:AIT-other"})
        self.assertEqual(unknown.exception.status, 404)
        self.store.path.write_text('{"broken":', encoding="utf-8")
        before = self.store.path.read_bytes()
        with self.assertRaises(CreationError) as corrupted:
            self.store.public()
        self.assertEqual(corrupted.exception.status, 500)
        self.assertEqual(self.store.path.read_bytes(), before)


class RetiredPictureRecordsTests(unittest.TestCase):
    def test_stored_marks_keep_loading_and_cannot_be_written_again(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ContentManagementStore(folder)
            store.root.mkdir(parents=True, exist_ok=True)
            store.path.write_text(json.dumps({"schema_version": 1, "requests": {}, "items": {
                "carousel:CAR-091dc72062a9": {"calendar_uploaded": True, "updated_at": "2026-10-23T05:51:13+00:00"}}}), encoding="utf-8")
            self.assertIn("carousel:CAR-091dc72062a9", store.public()["items"])
            with self.assertRaises(CreationError):
                store.mutate({"request_id": "retired-mark-0001", "id": "carousel:CAR-091dc72062a9", "revision": store.public()["revision"],
                              "management": {"archived": True}})


if __name__ == "__main__":
    unittest.main()


class ContentVariantNamingTests(unittest.TestCase):
    """The list names a row by the catalog variant the direction page assigns it, numbered as that page numbers it."""

    direction = {"product": "202H", "direction_id": "202H-S2", "variants": [
        {"variant_type": "execution", "progression": "回应质疑"},
        {"variant_type": "direction", "progression": ""},
        {"variant_type": "execution", "progression": "其他：送礼"},
        {"variant_type": "execution"}]}

    def heading(self, row, kind="script", owners=frozenset(), direction="202H-S2"):
        return content_heading(row, kind, direction=direction, owners=owners, labels=variant_labels(self.direction))

    def test_labels_skip_the_definition_and_drop_the_other_prefix(self):
        self.assertEqual(variant_labels(self.direction), [("#1", "回应质疑"), ("方向定义", ""), ("#2", "送礼"), ("#3", "")])

    def test_one_owner_names_the_variant_and_the_opening_line_follows(self):
        script = {"title": "202H-S2 变体 #9 · 旧名", "translation_zh": "给爱刀的人挑礼物。\n第二句。", "text": "Gift."}
        self.assertEqual(self.heading(script, owners={2}), ("S2 #2 送礼", "给爱刀的人挑礼物。"))
        self.assertEqual(self.heading(script, owners={1})[0], "S2 方向定义")
        self.assertEqual(self.heading(script, owners={3})[0], "S2 #3")

    def test_unplaced_records_say_so_instead_of_trusting_old_titles(self):
        script = {"title": "202H-S2 变体 #4 · 身份认同", "text": "Hook."}
        self.assertEqual(self.heading(script, owners=set())[0], "S2 变体待核对")
        self.assertEqual(self.heading(script, owners={0, 2})[0], "S2 变体待核对")
        task = {"task_id": "AIT-000000000001", "action": "variants", "context": {"title": "写稿主题"}}
        self.assertEqual(self.heading(task, "task", owners={0, 2}), ("S2 写稿 · 2 条变体", "写稿主题"))
        self.assertEqual(self.heading(task, "task", owners=set())[0], "S2 写稿")

    def test_records_without_a_direction_keep_their_own_name(self):
        self.assertEqual(self.heading({"title": "手写的名字", "text": "Hook line."}, direction=""), ("手写的名字", "Hook line."))
        self.assertEqual(self.heading({"text": ""}, direction="")[0], "未关联方向")

    def test_unknown_ownership_and_reviews_keep_the_older_name(self):
        script = {"title": "身份认同", "text": "Hook."}
        self.assertEqual(self.heading(script, owners=None), (content_title(script, "script", direction="202H-S2"), ""))
        review = {"task_id": "AIT-000000000002", "action": "review", "context": {"title": "审一稿"}}
        self.assertEqual(self.heading(review, "task", owners={0})[0], "审一稿｜审稿结果")

    def test_build_uses_the_owner_map_and_its_direction(self):
        rows = build_content_items([], [{"id": "SCR-000000000001", "family_id": "SCR-000000000001", "product": "202H",
                                         "title": "x", "text": "Line one.", "status": "adopted", "version": 1,
                                         "created_at": "2026-10-01T00:00:00Z"}], [], [],
                                   variants={"owners": {("script", "SCR-000000000001"): {"direction": ("202H", "202H-S2"), "indexes": {0}}},
                                             "labels": {("202H", "202H-S2"): variant_labels(self.direction)}})
        self.assertEqual((rows[0]["title"], rows[0]["subtitle"], rows[0]["direction_id"]), ("S2 #1 回应质疑", "Line one.", "202H-S2"))
