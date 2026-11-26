"""Guards for the loading-speed work: each of these fails when a change quietly gives the speed back.

Timing itself is checked against the running workbench by scripts/check_speed.py; the tests here pin the
mechanisms, which do not depend on how busy the machine is.
"""
import copy
import inspect
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import ai_tasks  # noqa: E402
import app_config  # noqa: E402
import performance_store  # noqa: E402
import program_assets  # noqa: E402
import server  # noqa: E402
import service_gateway  # noqa: E402

STATIC = ROOT / "static"


def page_source(name):
    return (STATIC / name).read_text(encoding="utf-8").replace("\r\n", "\n")


class ProcessSpeedTests(unittest.TestCase):
    def test_engine_and_gateway_opt_out_of_background_throttling(self):
        # A windowless service left to the system can run on efficiency cores at several times the CPU cost.
        self.assertIn("keep_full_speed()", inspect.getsource(server.main))
        self.assertIn("keep_full_speed()", inspect.getsource(service_gateway.main))
        if os.name == "nt":
            self.assertIs(app_config.keep_full_speed(), True)

    def test_service_status_reports_whether_the_engine_runs_at_full_speed(self):
        self.assertIn('"full_speed": FULL_SPEED', inspect.getsource(server.WorkbenchHTTPServer.service_status))


class TransportTests(unittest.TestCase):
    def test_large_answers_use_strong_compression_and_every_answer_is_timed_and_tagged(self):
        source = inspect.getsource(server.Handler.output)
        self.assertRegex(source, r"compresslevel=6 if len\(content\) >= 16384")
        self.assertIn('"Server-Timing"', source)
        self.assertIn('"ETag"', source)
        self.assertIn("send_response(304)", source)

    def test_page_keeps_answers_only_in_memory_and_parses_shared_reads_without_copying(self):
        source = page_source("app.js")
        api = source[source.index("const heldAnswers"):source.index("function toast(")]
        self.assertIn('headers["If-None-Match"]', api)
        self.assertNotIn("structuredClone(", api)
        self.assertNotRegex(api, r"localStorage|sessionStorage|indexedDB|caches\.")

    def test_every_script_and_stylesheet_travels_in_the_two_merged_files(self):
        # A tag written differently from the rest (extra attribute, module script) is left out of the bundle
        # and becomes one more download on every first visit.
        page = program_assets.build_snapshot(STATIC).index.raw.decode("utf-8")
        scripts = re.findall(r'<script\b[^>]*\bsrc="([^"]+)"', page)
        styles = re.findall(r'<link\b[^>]*rel="stylesheet"[^>]*href="([^"]+)"', page)
        self.assertEqual([name.split("?")[0] for name in scripts], ["/workbench-program-0.js"], scripts)
        self.assertEqual([name.split("?")[0] for name in styles], ["/workbench-styles-0.css"], styles)
        self.assertTrue(all(re.search(r"\?v=[0-9a-f]{12}$", name) for name in scripts + styles))


class RecordCopyTests(unittest.TestCase):
    def test_clone_records_is_detached_and_equal_and_falls_back_for_other_values(self):
        rows = [{"id": 1, "tags": ["a", "b"], "figures": {"views": 1.5, "none": None, "flag": True}, "pair": (1, 2)}]
        cloned = performance_store.clone_records(rows)
        self.assertEqual(cloned, rows)
        cloned[0]["tags"].append("c")
        cloned[0]["figures"]["views"] = 2
        self.assertEqual(rows[0]["tags"], ["a", "b"])
        self.assertEqual(rows[0]["figures"]["views"], 1.5)
        odd = [{"path": Path("x")}]  # not a plain record: the general copy takes over
        self.assertEqual(performance_store.clone_records(odd), odd)


class TaskListCostTests(unittest.TestCase):
    """The task list is read by most pages and by the polls; its cost must not grow per ancestor or per copy."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="speed-guard-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = server.CaseStore(root / "skill")
        bridge = type("Bridge", (), {"status": lambda self: {"available": True}})()
        self.manager = ai_tasks.AITaskManager(self.store, root / "runtime", threading.RLock(), bridge=bridge)
        self.tasks = root / "runtime" / "ai-tasks"

    def write_tasks(self, start, count):
        for index in range(start, start + count):
            task_id = f"AIT-{index:012x}"
            folder = self.tasks / task_id
            folder.mkdir(parents=True)
            (folder / "task.json").write_text(json.dumps({
                "schema_version": 1, "task_id": task_id, "case_id": None, "case_title": "", "action": "variants",
                "status": "completed", "revision": 1, "base_revision": "", "base_event_id": None,
                "created_at": "2026-11-18T00:00:00+00:00", "updated_at": "2026-11-18T00:00:00+00:00", "progress": "",
                "snapshot": {"product": "202H", "creation_context": {"kind": "blank", "title": "fixture"}},
                "rounds": [{"round_id": f"AIR-{index:012x}", "status": "completed", "instruction": "write",
                            "output": "x" * 2000, "error": None}],
                "requests": []}), encoding="utf-8")

    def ancestor_walks(self, action):
        """How many times a path's whole ancestor chain was taken apart during the action."""
        import pathlib
        walks = []
        original = pathlib.PurePath.parents

        def counted(path):
            walks.append(path)
            return original.fget(path)

        with patch.object(pathlib.PurePath, "parents", property(counted)):
            action()
        return len(walks)

    def test_path_checks_walk_each_ancestor_chain_once_not_once_per_task(self):
        self.write_tasks(0, 6)
        few = self.ancestor_walks(self.manager.list_tasks)
        self.write_tasks(6, 24)
        many = self.ancestor_walks(self.manager.list_tasks)
        self.assertEqual(len(self.manager.list_tasks()), 30)
        # Walking and comparing every ancestor again for each task's folder and file was most of a list's time.
        self.assertLessEqual(many - few, 2, (few, many))

    def test_status_poll_reads_task_records_in_place(self):
        self.write_tasks(0, 8)
        with patch.object(self.manager, "_read", wraps=self.manager._read) as read, \
                patch.object(ai_tasks.copy, "deepcopy", wraps=copy.deepcopy) as copied:
            state = self.manager.status()
        self.assertEqual(state["queued"], 0)
        self.assertEqual(read.call_count, 8)
        self.assertTrue(all(call.kwargs.get("copy_record") is False for call in read.call_args_list))
        # Only the small connection state is copied, never the task records with their rounds and outputs.
        self.assertLessEqual(copied.call_count, 6)


class PageCodeTests(unittest.TestCase):
    """Front-end patterns that cost a second per page when they slip back in."""

    def test_select_refresh_only_writes_what_changed(self):
        source = page_source("shell.js")
        refresh = source[source.index("    function refresh() {\n      if (destroyed) return;"):]
        refresh = refresh[:refresh.index("function setOpen(")]
        # An unconditional write here makes the next select's style read recompute the whole page.
        self.assertNotRegex(refresh, r"\.textContent = ")
        self.assertNotRegex(refresh, r"(?<![(] )(?<!else )\btrigger\.setAttribute\(")
        self.assertIn("measuredDisplay ?? getComputedStyle(picker).display", refresh)

    def test_new_selects_are_measured_together_before_any_is_built(self):
        source = page_source("shell.js")
        body = source[source.index("function enhanceSelects("):source.index("function initializeSelects(")]
        self.assertLess(body.index("getBoundingClientRect"), body.index("createFilterPicker("))
        self.assertIn("if (fresh.length) enhanceSelects(fresh);", source)

    def test_data_pages_reuse_number_formatters(self):
        # toLocaleString builds a formatter per call; the sales boards format tens of thousands of numbers.
        for name in ("perf-dashboard.js", "performance.js", "perf-creators.js", "perf-negative.js", "reviews.js",
                     "review-followups.js"):
            numeric = [line.strip()[:120] for line in page_source(name).splitlines()
                       if "toLocaleString(" in line and "hour12" not in line]
            self.assertEqual(numeric, [], name)

    def test_content_and_sales_pages_ask_for_their_secondary_data_alongside_the_first_read(self):
        load = page_source("content-flow.js")
        load = load[load.index("async function load(seq"):]
        self.assertLess(load.index("/api/needs-you?"), load.index("await sharedRead('/api/content?'"))
        self.assertLess(load.index("/api/photo-overview?"), load.index("await sharedRead('/api/content?'"))
        load = page_source("performance.js")
        load = load[load.index("  function load(force = false)"):]
        self.assertLess(load.index("const analyticsRead = api("), load.index("const supplements = core.then"))
        route = page_source("reviews.js")
        route = route[route.index("      const initialRead = loadResults()"):]
        self.assertLess(route.index('startEarly("/api/reviews/translations/batch")'), route.index("await Promise.all([loadSummary(),initialRead])"))


if __name__ == "__main__":
    unittest.main()
