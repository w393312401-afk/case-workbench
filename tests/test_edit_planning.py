"""Concurrent shot planning over local snapshots; no model or Resolve calls."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_bridge import BridgeCancelled
from codex_edit_bridge import CodexEditPlannerBridge
from edit_planning import ANALYSIS_WORKERS, analyze_candidates, first_choices, prepare_workers


class FakePlanner:
    def __init__(self, response=None):
        self.calls = []
        self.lock = threading.Lock()
        self.response = response or (lambda call: "本组建议使用候选特写。")

    def run(self, prompt, **values):
        call = {"prompt": prompt, **values}
        call["snapshot"] = json.loads((Path(values["cwd"]) / "candidates.json").read_text(encoding="utf-8"))
        # Use the real planner's validation without starting any client process.
        call["settings"] = CodexEditPlannerBridge._profile(values["profile"])
        with self.lock:
            self.calls.append(call)
        return self.response(call)


class TrackingSlots:
    """Bounded semaphore with observable entry; no artificial scheduling sleeps."""
    def __init__(self, capacity=ANALYSIS_WORKERS, expected_waiters=0):
        self.semaphore = threading.BoundedSemaphore(capacity)
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.attempts = set()
        self.expected_waiters = expected_waiters
        self.waiters_arrived = threading.Event()

    def acquire(self, timeout):
        with self.lock:
            self.attempts.add(threading.get_ident())
            if len(self.attempts) >= self.expected_waiters:
                self.waiters_arrived.set()
        acquired = self.semaphore.acquire(timeout=timeout)
        if acquired:
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
        return acquired

    def release(self):
        with self.lock:
            self.active -= 1
        self.semaphore.release()


class EditPlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="edit-planning-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "job"
        self.folder.mkdir()
        self.handoff = "意群一：守住角度。\n意群二：刀刃特写。"
        self.cancel = threading.Event()
        self.progress = []

    def candidates(self, count=11, folder=None):
        folder = folder or self.folder
        rows = [{"id": f"candidate-{number}", "timeline": "source-timeline", "source": [number * 100, number * 100 + 50],
                 "label": f"画面 {number}", "grid": None} for number in range(count)]
        self.write_candidates(rows, folder)
        return rows

    def write_candidates(self, rows, folder=None):
        folder = folder or self.folder
        (folder / "candidates.json").write_text(json.dumps({"candidates": rows}, ensure_ascii=False), encoding="utf-8")

    def analyze(self, bridge, *, folder=None, slots=None, model="codex-default"):
        return analyze_candidates(folder or self.folder, self.handoff, bridge, cancel=self.cancel,
                                  model=model, effort="high", slots=slots or TrackingSlots(), progress=self.progress.append)

    def test_candidates_are_partitioned_exactly_once_without_changing_source(self):
        original = self.candidates()
        workers = prepare_workers(self.folder, self.handoff)
        self.assertEqual(len(workers), 3)
        rows = []
        for number, directory in enumerate(workers, 1):
            snapshot = json.loads((directory / "candidates.json").read_text(encoding="utf-8"))
            self.assertEqual((snapshot["worker"], snapshot["workers"]), (number, 3))
            self.assertEqual((directory / "handoff.md").read_text(encoding="utf-8"), self.handoff)
            rows.extend(snapshot["candidates"])
        # Each worker owns one unbroken run of the listing, in order.
        self.assertEqual(rows, original)
        self.assertEqual(len({row["id"] for row in rows}), len(original))
        source = json.loads((self.folder / "candidates.json").read_text(encoding="utf-8"))
        self.assertEqual(source["candidates"], original)

    def test_runs_are_contiguous_balanced_and_see_their_neighbours(self):
        original = self.candidates(11)
        snapshots = [json.loads((directory / "candidates.json").read_text(encoding="utf-8"))
                     for directory in prepare_workers(self.folder, self.handoff)]
        ids = lambda rows: [int(row["id"].split("-")[-1]) for row in rows]
        self.assertEqual([ids(item["candidates"]) for item in snapshots], [[0, 1, 2], [3, 4, 5, 6], [7, 8, 9, 10]])
        # Inking, wiping and checking are adjacent clips; a run cut between them still shows the other side.
        self.assertEqual([ids(item["before"]) for item in snapshots], [[], [0, 1, 2], [4, 5, 6]])
        self.assertEqual([ids(item["after"]) for item in snapshots], [[3, 4, 5], [7, 8, 9], []])
        self.assertEqual(len(original), 11)

    def test_neighbours_stop_at_another_material_timeline(self):
        rows = self.candidates(6)
        for row in rows[3:]:
            row["timeline"] = "other-timeline"
        self.write_candidates(rows)
        first, second, third = [json.loads((directory / "candidates.json").read_text(encoding="utf-8"))
                                for directory in prepare_workers(self.folder, self.handoff)]
        ids = lambda rows: [row["id"] for row in rows]
        self.assertEqual([ids(item["candidates"]) for item in (first, second, third)],
                         [["candidate-0", "candidate-1"], ["candidate-2", "candidate-3"], ["candidate-4", "candidate-5"]])
        self.assertEqual(ids(first["after"]), ["candidate-2"])
        self.assertEqual(ids(second["before"]), ["candidate-0", "candidate-1"])
        self.assertEqual(ids(second["after"]), ["candidate-4", "candidate-5"])
        self.assertEqual(ids(third["before"]), ["candidate-3"])
        self.assertEqual(ids(third["after"]), [])

    def test_planner_prompt_explains_runs_and_neighbours(self):
        self.candidates(3)
        bridge = FakePlanner()
        self.analyze(bridge)
        for call in bridge.calls:
            self.assertIn("连续的一段", call["prompt"])
            self.assertIn("before/after", call["prompt"])
            self.assertIn("不为它们出建议", call["prompt"])
            self.assertIn('{"picks": [{"group": 1, "key": "候选编号"}]}', call["prompt"])
            self.assertIn("被否定的片段或 before/after", call["prompt"])
            self.assertIn("一个意群需要几个镜头就按顺序列几项", call["prompt"])
            self.assertIn("末尾附任务要求的首选清单", call["profile"]["rules"])

    def keyed(self, count):
        rows = self.candidates(count)
        for number, row in enumerate(rows):
            row.update(key=f"DG{number:03d}-{number:08x}", media_id=f"m{number}", media_path="")
        self.write_candidates(rows)
        return rows

    def test_first_choices_count_only_listed_clips_the_planner_owns(self):
        self.keyed(6)
        def response(call):
            worker = call["snapshot"]["worker"]
            if worker == 1:
                # Prose mentions, an echoed example and the after-neighbour DG002 never count; the editor's json is no list.
                return ("意群1 选项A DG000，备选 DG001，不要用 DG002。\n```json\n" + json.dumps({"picks": [{"group": 1, "key": "候选编号"}]}) + "\n```\n"
                        "最终：\n```json\n" + json.dumps({"picks": [{"group": 1, "key": "DG000-00000000"}, {"group": 2, "key": "DG002-00000002"},
                                                                    "DG001"]}) + "\n```\n```json\n" + json.dumps({"timeline": "x", "saved": True}) + "\n```")
            if worker == 2:
                return "本组没有合适画面。\n```json\n" + json.dumps({"picks": []}) + "\n```"
            return "建议用 DG004、DG005，但没有附清单。"
        self.analyze(FakePlanner(response))
        results = {row["worker"]: row for row in json.loads((self.folder / "planning-results.json").read_text(encoding="utf-8"))}
        # Worker 1 owns DG000 and DG001; the placeholder and its neighbour DG002 are counted as naming nothing.
        self.assertEqual((results[1]["picks"], results[1]["unknown_picks"]), (["DG000-00000000", "DG001-00000001"], 2))
        self.assertEqual((results[2]["picks"], results[2]["unknown_picks"]), ([], 0))
        self.assertEqual((results[3]["picks"], results[3]["unknown_picks"]), (None, 0))

    def test_first_choices_accept_the_shapes_planners_actually_write(self):
        own = [{"key": f"DG00{number}-0000000{number}", "media_id": "m1", "media_path": "", "source": [number * 10, number * 10 + 10]}
               for number in range(4)]
        block = lambda value, fence="```json": f"{fence}\n{json.dumps(value)}\n```"
        picks = lambda text, foreign=(): (first_choices(text, own, foreign) or {}).get("picks")
        # One block per 意群, fenced in upper case or with no language tag (and CRLF line ends), all count.
        text = ("意群1\n" + block({"picks": [{"group": 1, "key": "DG000-00000000"}]}, "```JSON") + "\n意群2\r\n"
                + block({"picks": [{"group": 2, "key": "DG001-00000001"}]}, "```").replace("\n", "\r\n"))
        self.assertEqual(picks(text), ["DG000-00000000", "DG001-00000001"])
        # Several shots of one 意群 under "keys", or a list under "key".
        self.assertEqual(picks(block({"picks": [{"group": 1, "keys": ["DG002-00000002", "DG003-00000003"]}, {"group": 2, "key": ["DG000"]}]})),
                         ["DG002-00000002", "DG003-00000003", "DG000-00000000"])
        # A neighbour's exact key is not read as a mistyped key of an owned piece of the same clip.
        self.assertEqual(first_choices(block({"picks": ["DG000-0000aaaa"]}), own, {"DG000-0000aaaa"}), {"picks": [], "unknown": 1})
        self.assertEqual(picks(block({"picks": ["DG000-0000aaab"]})), ["DG000-00000000"])
        # Broken json or no picks list is no list at all.
        self.assertIsNone(first_choices('```json\n{"picks": [,]}\n```\n' + block({"shots": ["DG000"]}), own))
        # A brace-led note block that is not JSON, or an object followed by a comment, never swallows the real list.
        notes = '```\n{意群1} 用 DG001 → 检查\n```\n```json\n{"note": 1} // 草稿\n```\n'
        self.assertEqual(picks(notes + block({"picks": ["DG002-00000002"]})), ["DG002-00000002"])
        # jsonc tags, a list written on the fence line, a leading comment and one object per line all count.
        self.assertEqual(picks('```jsonc\n// 首选\n{"picks": ["DG000"]}\n```\n```json {"picks": ["DG001"]}```\n'
                               '```json\n{"picks": ["DG002"]}\n{"picks": ["DG003"]}\n```'),
                         ["DG000-00000000", "DG001-00000001", "DG002-00000002", "DG003-00000003"])

    def test_a_cut_reply_still_shows_its_list_to_the_editor(self):
        self.keyed(3)
        long = "详细理由。" * 5000 + '\n```json\n{"picks": ["DG000"]}\n```'
        self.analyze(FakePlanner(lambda call: long if call["snapshot"]["worker"] == 1 else "无"))
        first = json.loads((self.folder / "planning-results.json").read_text(encoding="utf-8"))[0]
        self.assertLess(len(first["output"]), len(long))
        self.assertEqual(first["picks"], ["DG000-00000000"])
        self.assertIn("以上为节选", first["output"])
        self.assertTrue(first["output"].endswith('```json\n{"picks": ["DG000-00000000"]}\n```'))
        self.assertIn('{"picks": ["DG000-00000000"]}', (self.folder / "planning.md").read_text(encoding="utf-8"))

    def test_missing_or_empty_candidates_do_not_start_workers(self):
        bridge = FakePlanner()
        self.assertEqual(self.analyze(bridge), {"workers": 0, "completed": 0, "failed": 0})
        self.candidates(0)
        self.assertEqual(self.analyze(bridge), {"workers": 0, "completed": 0, "failed": 0})
        self.assertEqual(bridge.calls, [])
        self.assertFalse((self.folder / "planning.md").exists())

    def test_planners_read_the_same_skill_snapshot_and_can_use_existing_descriptions(self):
        self.candidates(3)
        skill = self.folder / "skill"
        (skill / "references").mkdir(parents=True)
        (skill / "SKILL.md").write_text("当前剪辑规则", encoding="utf-8")
        (skill / "references" / "product.md").write_text("当前产品工序", encoding="utf-8")
        self.handoff = "本次模式：解释画面逻辑。旧补拍建议仅作选材线索。"
        bridge = FakePlanner()
        self.analyze(bridge)
        for call in bridge.calls:
            worker = Path(call["cwd"])
            self.assertEqual((worker / "skill" / "SKILL.md").read_text(encoding="utf-8"), "当前剪辑规则")
            self.assertEqual((worker / "skill" / "references" / "product.md").read_text(encoding="utf-8"), "当前产品工序")
            self.assertIn("先读取 skill/SKILL.md", call["prompt"])
            rules = call["profile"]["rules"]
            self.assertIn("和主剪辑使用同一套选材规则", rules)
            self.assertIn("已有画面描述均可作为", rules)
            self.assertIn("旧画面推荐和补拍清单不能增设硬门槛", rules)
            self.assertNotIn("没看过的画面必须标为待查看", rules)
            self.assertEqual(call["profile"]["mcp"], {})
        summary = (self.folder / "planning.md").read_text(encoding="utf-8")
        self.assertIn("待核实细节由主剪辑查看相关画面", summary)

    def test_image_copies_are_worker_local_and_reject_paths_outside_candidates(self):
        rows = self.candidates(4)
        original_dir = self.folder / "candidates"
        original_dir.mkdir()
        good = original_dir / "shot.jpg"
        good.write_bytes(b"original image")
        outside = self.root / "private.jpg"
        outside.write_bytes(b"outside job")
        sibling = self.folder / "unrelated.jpg"
        sibling.write_bytes(b"outside candidates")
        rows[0]["grid"] = "candidates/shot.jpg"
        rows[1]["grid"] = str(outside)
        rows[2]["grid"] = "unrelated.jpg"
        rows[3]["grid"] = "candidates/missing.jpg"
        self.write_candidates(rows)
        original = copy.deepcopy(rows)
        workers = prepare_workers(self.folder, self.handoff)
        seen = {}
        for directory in workers:
            snapshot = json.loads((directory / "candidates.json").read_text(encoding="utf-8"))
            for candidate in snapshot["candidates"]:
                seen[candidate["id"]] = candidate
                if candidate["grid"]:
                    copied = (directory / candidate["grid"]).resolve()
                    self.assertTrue(copied.is_relative_to(directory / "stills"))
                    self.assertNotEqual(copied, good)
                    self.assertEqual(copied.read_bytes(), b"original image")
                    copied.write_bytes(b"local copy")
        self.assertEqual(good.read_bytes(), b"original image")
        self.assertTrue(all(seen[f"candidate-{number}"]["grid"] is None for number in (1, 2, 3)))
        self.assertEqual(json.loads((self.folder / "candidates.json").read_text(encoding="utf-8"))["candidates"], original)

    def test_same_image_basename_from_different_sources_cannot_overwrite(self):
        rows = self.candidates(4)
        for number, directory in ((1, "first"), (2, "second"), (3, "third")):
            image = self.folder / "candidates" / directory / "shot.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(f"source {number}".encode())
            rows[number]["grid"] = image.relative_to(self.folder).as_posix()
        self.write_candidates(rows)
        workers = prepare_workers(self.folder, self.handoff)
        # Worker 2 owns clip 1 and sees clips 2 and 3 after it; worker 3 owns clips 2 and 3.
        for directory, keys in ((workers[1], ("candidates", "after")), (workers[2], ("candidates",))):
            snapshot = json.loads((directory / "candidates.json").read_text(encoding="utf-8"))
            listed = [row for key in keys for row in snapshot[key] if row["grid"]]
            self.assertGreaterEqual(len(listed), 2)
            self.assertEqual(len({row["grid"] for row in listed}), len(listed))
            for row in listed:
                number = int(row["id"].split("-")[-1])
                self.assertEqual((directory / row["grid"]).read_bytes(), f"source {number}".encode())

    def test_three_groups_really_overlap_with_only_read_tools_and_no_resolve(self):
        self.candidates(11)
        overlap = threading.Barrier(3)
        def response(call):
            overlap.wait(timeout=3)
            return f"第 {call['snapshot']['worker']} 组选材结果"
        bridge, slots = FakePlanner(response), TrackingSlots()
        stats = self.analyze(bridge, slots=slots)
        self.assertEqual(stats, {"workers": 3, "completed": 3, "failed": 0})
        self.assertEqual((slots.peak, slots.active), (3, 0))
        self.assertEqual(len({call["cwd"] for call in bridge.calls}), 3)
        for call in bridge.calls:
            self.assertEqual(call["settings"]["files"], {"Read", "Glob", "Grep"})
            self.assertEqual(call["profile"]["mcp"], {})
            self.assertEqual(call["profile"]["dirs"], [])
            self.assertNotIn("Write", call["profile"]["allowed"])
            self.assertNotIn("Edit", call["profile"]["allowed"])
            self.assertFalse(any(name.startswith("mcp__") for name in call["profile"]["allowed"]))
            self.assertEqual(call["skills"], [])
            self.assertIsNone(call["model"])
            self.assertEqual(call["reasoning_effort"], "high")
            self.assertIs(call["cancel_event"], self.cancel)
            self.assertIsNone(call["on_event"])
        results = json.loads((self.folder / "planning-results.json").read_text(encoding="utf-8"))
        self.assertEqual([row["worker"] for row in results], [1, 2, 3])
        summary = (self.folder / "planning.md").read_text(encoding="utf-8")
        self.assertIn("不能把建议当作已执行结果", summary)
        self.assertTrue(all(f"第 {number} 组选材结果" in summary for number in (1, 2, 3)))

    def test_failed_and_empty_groups_fall_back_without_losing_successful_results(self):
        self.candidates(3)
        def response(call):
            number = call["snapshot"]["worker"]
            if number == 2:
                raise RuntimeError("test analysis unavailable")
            return "可用选材结果" if number == 1 else "  "
        bridge, slots = FakePlanner(response), TrackingSlots()
        stats = self.analyze(bridge, slots=slots, model="selected-model")
        self.assertEqual(stats, {"workers": 3, "completed": 1, "failed": 2})
        self.assertEqual(slots.active, 0)
        self.assertTrue(all(call["model"] == "selected-model" for call in bridge.calls))
        results = json.loads((self.folder / "planning-results.json").read_text(encoding="utf-8"))
        self.assertEqual([row["status"] for row in results], ["completed", "failed", "failed"])
        self.assertEqual(results[0]["output"], "可用选材结果")
        summary = (self.folder / "planning.md").read_text(encoding="utf-8")
        self.assertEqual(summary.count("需由主剪辑自行查找本组候选"), 2)
        self.assertIn("可用选材结果", summary)

    def test_cancellation_while_waiting_starts_no_model_and_saves_no_partial_results(self):
        self.candidates(3)
        slots = TrackingSlots(capacity=0, expected_waiters=3)
        bridge = FakePlanner()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.analyze, bridge, slots=slots)
            try:
                self.assertTrue(slots.waiters_arrived.wait(timeout=3))
            finally:
                self.cancel.set()
            with self.assertRaises(BridgeCancelled):
                future.result(timeout=3)
        self.assertEqual(bridge.calls, [])
        self.assertEqual(slots.active, 0)
        self.assertFalse((self.folder / "planning-results.json").exists())
        self.assertFalse((self.folder / "planning.md").exists())

    def test_cancellation_after_model_output_releases_slot_and_does_not_publish(self):
        self.candidates(1)
        def response(call):
            self.cancel.set()
            return "取消时返回的内容"
        slots = TrackingSlots()
        with self.assertRaises(BridgeCancelled):
            self.analyze(FakePlanner(response), slots=slots)
        self.assertEqual(slots.active, 0)
        self.assertFalse((self.folder / "planning-results.json").exists())
        self.assertFalse((self.folder / "planning.md").exists())

    def test_multiple_jobs_share_global_limit_of_three_workers(self):
        self.candidates(3)
        second = self.root / "second-job"
        second.mkdir()
        self.candidates(3, second)
        slots = TrackingSlots(expected_waiters=6)
        first_wave = threading.Event()
        release = threading.Event()
        running_lock = threading.Lock()
        running = {"active": 0, "peak": 0}
        def response(call):
            with running_lock:
                running["active"] += 1
                running["peak"] = max(running["peak"], running["active"])
                if running["active"] == 3:
                    first_wave.set()
            try:
                if not release.wait(timeout=5):
                    raise RuntimeError("test timed out while checking global concurrency")
                return "选材建议"
            finally:
                with running_lock:
                    running["active"] -= 1
        bridge = FakePlanner(response)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.analyze, bridge, slots=slots)
            other = pool.submit(self.analyze, bridge, folder=second, slots=slots)
            try:
                self.assertTrue(first_wave.wait(timeout=3))
                self.assertTrue(slots.waiters_arrived.wait(timeout=3))
                with running_lock:
                    self.assertEqual(running["active"], 3)
                self.assertEqual(len(bridge.calls), 3)
                self.assertFalse(first.done())
                self.assertFalse(other.done())
            finally:
                release.set()
            self.assertEqual(first.result(timeout=3), {"workers": 3, "completed": 3, "failed": 0})
            self.assertEqual(other.result(timeout=3), {"workers": 3, "completed": 3, "failed": 0})
        self.assertEqual((slots.peak, slots.active), (3, 0))
        self.assertEqual((running["peak"], running["active"]), (3, 0))
        self.assertEqual(len(bridge.calls), 6)
        self.assertEqual(len({call["cwd"] for call in bridge.calls}), 6)
        self.assertTrue((self.folder / "planning.md").is_file())
        self.assertTrue((second / "planning.md").is_file())


if __name__ == "__main__":
    unittest.main()
