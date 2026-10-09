"""Sync uses only temporary stores and mocked publishing; never touches the live account."""
from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("text_sync_test_module", Path(__file__).resolve().parents[1] / "deployment" / "sync_text_site.py")
SYNC = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SYNC
SPEC.loader.exec_module(SYNC)


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds=5):
        self.value += seconds


class FakePublisher:
    base = "http://127.0.0.1:8767"

    def __init__(self, root):
        self.root = Path(root)
        self.exports = self.deploys = 0
        self.text = "first"
        self.after_export = self.after_deploy = None
        self.export_error = self.deploy_error = None

    def export(self):
        self.exports += 1
        if self.export_error:
            raise SYNC.StageFailure(self.export_error)
        deployment = self.root / "deployment"
        site = deployment / "site"
        site.mkdir(parents=True, exist_ok=True)
        bundle = {"meta": {"exported_at": str(self.exports)}, "state": {"exported_at": str(self.exports),
                  "signature": str(self.exports), "digest": str(self.exports), "cases": [{"text": self.text}]}}
        (deployment / "text-snapshot.json").write_text(json.dumps(bundle), encoding="utf-8")
        (site / "index.html").write_text(f'<head><meta name="public-exported-at" content="{self.exports}"></head>', encoding="utf-8")
        (site / "app.js").write_text("browse();", encoding="utf-8")
        if self.after_export:
            self.after_export()

    def deploy(self):
        self.deploys += 1
        if self.deploy_error:
            raise SYNC.StageFailure(self.deploy_error)
        if self.after_deploy:
            self.after_deploy()
        return "https://test.case-workbench-text.pages.dev"


class TextSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="text-sync-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skill, self.runtime = self.root / "skill", self.root / "runtime"
        self.source = self.root / "case-source.md"
        self.source.write_text("original source", encoding="utf-8")
        self.case = self.skill / "cases" / "PVR-111111111111" / "original.json"
        self.write(self.case, json.dumps({"case_id": "PVR-111111111111", "current": {"source_case": {"file_path": str(self.source)}}}))
        self.write(self.root / "config.json", json.dumps({"skill_root": str(self.skill), "runtime_root": str(self.runtime),
                                                        "performance_sources": [str(self.root / "sales-sources")]}))
        self.write(self.root / "static" / "app.js", "initial source")
        self.write(self.root / "deployment" / "text-only" / "_worker.js", "initial worker")
        self.write(self.root / "deployment" / "export_text_site.py", "# test exporter")
        self.clock = Clock()
        self.publisher = FakePublisher(self.root)
        self.state_root = self.runtime / "text-public-sync"
        self.scanner = SYNC.FingerprintScanner(self.root)
        self.available = True
        self.logger = logging.getLogger("text_sync_test")
        self.logger.handlers = [logging.NullHandler()]

    def write(self, path, value="new content"):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    def service(self):
        return SYNC.SyncService(self.root, self.state_root, self.publisher, scanner=self.scanner,
                                ready=lambda: self.available, clock=self.clock, logger=self.logger)

    def test_first_sync_and_restarted_same_content_skip_publish(self):
        service = self.service()
        self.assertTrue(service.step())
        self.assertEqual((self.publisher.exports, self.publisher.deploys), (1, 1))
        self.assertTrue(service.step())
        self.assertEqual(self.publisher.exports, 1)
        restarted = self.service()
        self.assertTrue(restarted.step())
        self.assertEqual((self.publisher.exports, self.publisher.deploys), (2, 1))
        self.assertEqual(restarted.status["state"], "unchanged")

    def test_debounce_coalesces_multiple_saves(self):
        service = self.service()
        service.step()
        self.publisher.text = "second"
        self.write(self.case, "changed once")
        self.assertFalse(service.step())
        self.clock.advance(3)
        self.write(self.case, "changed twice, latest data")
        self.assertFalse(service.step())
        self.clock.advance(4)
        self.assertFalse(service.step())
        self.assertEqual(self.publisher.deploys, 1)
        self.clock.advance(1)
        self.assertTrue(service.step())
        self.assertEqual(self.publisher.deploys, 2)

    def test_export_time_and_public_digest_dont_republish_same_text(self):
        service = self.service()
        service.step()
        self.write(self.case, "same public text but changed local metadata")
        service.step()
        self.clock.advance()
        self.assertTrue(service.step())
        self.assertEqual((self.publisher.exports, self.publisher.deploys), (2, 1))

    def test_waits_for_existing_api_then_syncs_without_starting_it(self):
        service = self.service()
        self.available = False
        self.assertFalse(service.step())
        self.assertEqual(service.status["state"], "waiting_local_service")
        self.assertEqual(self.publisher.exports, 0)
        self.available = True
        self.clock.advance()
        self.assertTrue(service.step())
        self.assertEqual(self.publisher.deploys, 1)

    def test_retry_backoff_then_recovers_and_remembers_success(self):
        service = self.service()
        self.publisher.deploy_error = "auth_required"
        self.assertFalse(service.step())
        self.assertEqual(service.status["consecutive_failures"], 1)
        self.clock.advance(4)
        self.assertFalse(service.step())
        self.assertEqual(self.publisher.deploys, 1)
        self.clock.advance(1)
        self.assertFalse(service.step())
        self.assertEqual(service.retry_at, self.clock() + 10)
        self.publisher.deploy_error = None
        self.clock.advance(10)
        self.assertTrue(service.step())
        self.assertEqual(service.status["consecutive_failures"], 0)
        self.assertIsNotNone(service.status["last_published_digest"])

    def test_derived_review_database_update_does_not_retrigger(self):
        service = self.service()
        database = self.runtime / "reviews" / "reviews.sqlite"
        self.publisher.after_export = lambda: self.write(database, "derived review content")
        self.assertTrue(service.step())
        self.clock.advance()
        self.assertTrue(service.step())
        self.assertEqual((self.publisher.exports, self.publisher.deploys), (1, 1))

    def test_business_change_during_export_requires_fresh_snapshot(self):
        service = self.service()
        self.publisher.after_export = lambda: self.write(self.case, "saved during export")
        self.assertFalse(service.step())
        self.assertEqual(service.status["state"], "pending")
        self.assertEqual(self.publisher.deploys, 0)
        self.publisher.after_export = None
        self.clock.advance()
        self.assertTrue(service.step())
        self.assertEqual((self.publisher.exports, self.publisher.deploys), (2, 1))

    def test_saves_during_deployment_are_detected_next_tick(self):
        service = self.service()
        self.publisher.after_deploy = lambda: self.write(self.case, "saved during upload")
        self.assertTrue(service.step())
        self.publisher.after_deploy = None
        self.publisher.text = "after upload save"
        self.assertFalse(service.step())
        self.clock.advance()
        self.assertTrue(service.step())
        self.assertEqual(self.publisher.deploys, 2)

    def test_scanner_covers_business_files_and_ignores_generated_media(self):
        before = self.scanner.take()
        entries = [self.runtime / "directions" / "catalog.json",
                   self.runtime / "creation" / "catalog.json",
                   self.runtime / "reviews" / "state.json",
                   self.runtime / "reviews" / "sources" / "amazon" / "batch.json",
                   self.runtime / "performance" / "performance.sqlite",
                   self.runtime / "performance" / "performance.sqlite-wal",
                   self.runtime / "ai-tasks" / "AIT-111111111111" / "task.json",
                   self.root / "sales-sources" / "new.xlsx"]
        for entry in entries:
            self.write(entry)
        after = self.scanner.take()
        self.assertTrue(all(str(entry.absolute()) in after for entry in entries))
        self.assertEqual(len(SYNC.changed_files(before, after, business_only=True)), len(entries))
        self.write(self.runtime / "evidence" / "video.mp4")
        self.write(self.runtime / "library" / "case.md")
        self.write(self.runtime / "text-public-sync" / "status.json")
        self.write(self.root / "deployment" / "site" / "app.js")
        self.write(self.root / "deployment" / "text-snapshot.json")
        self.write(self.skill / "cases" / "index.json")
        self.assertEqual(after, self.scanner.take())
        self.write(self.source, "source analysis edited")
        self.assertNotEqual(after, self.scanner.take())

    def test_empty_wal_lifecycle_is_ignored_but_real_sales_wal_is_business(self):
        before = self.scanner.take()
        wal = self.runtime / "performance" / "performance.sqlite-wal"
        self.write(wal, "")
        self.assertEqual(before, self.scanner.take())
        self.write(wal, "transaction with metrics")
        after = self.scanner.take()
        self.assertIn(str(wal.absolute()), SYNC.changed_files(before, after, business_only=True))

    def test_export_failure_is_safe_status_and_retry(self):
        service = self.service()
        self.publisher.export_error = "export_failed"
        self.assertFalse(service.step(once=True))
        state = SYNC.read_status(self.state_root)
        self.assertEqual(state["last_error_code"], "export_failed")
        self.assertEqual(self.publisher.deploys, 0)

    def test_lock_prevents_two_publishers_and_releases_after_exit(self):
        first, second = SYNC.ProcessLock(self.state_root / "process.lock"), SYNC.ProcessLock(self.state_root / "process.lock")
        with first:
            with self.assertRaises(SYNC.AlreadyRunning):
                second.acquire()
        with second:
            self.assertIsNotNone(second.file)

    def test_content_revision_is_used_when_exporter_provides_it(self):
        self.publisher.export()
        path = self.root / "deployment" / "text-snapshot.json"
        value = json.loads(path.read_text())
        value["meta"]["content_revision"] = "a" * 64
        self.write(path, json.dumps(value))
        self.assertEqual(SYNC.content_digest(self.root), "a" * 64)

    def test_deploy_command_uses_known_project_keyring_and_no_shell(self):
        wrangler = self.root / "deployment" / "node_modules" / "wrangler" / "bin" / "wrangler.js"
        self.write(wrangler, "test cli")
        node = self.root / "node.exe"
        publisher = SYNC.Publisher(self.root, FakePublisher.base, str(node))
        completed = subprocess.CompletedProcess([], 0, "Uploaded https://abc.case-workbench-text.pages.dev", "")
        with patch.object(SYNC.shutil, "which", return_value=str(node)), patch.object(SYNC.subprocess, "run", return_value=completed) as run:
            self.assertEqual(publisher.deploy(), "https://abc.case-workbench-text.pages.dev")
        command = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertEqual(command[0], str(node.resolve()))
        self.assertEqual(command[command.index("--project-name") + 1], "case-workbench-text")
        self.assertEqual(command[command.index("--profile") + 1], "case-workbench-text-sync")
        self.assertFalse(options["shell"])
        self.assertEqual(options["env"]["CLOUDFLARE_AUTH_USE_KEYRING"], "true")
        self.assertEqual(options["env"]["CLOUDFLARE_SEND_METRICS"], "false")
        self.assertEqual(options["env"]["CLOUDFLARE_ACCOUNT_ID"], SYNC.ACCOUNT_ID)
        self.assertEqual(options["env"]["XDG_CONFIG_HOME"], str(self.runtime / "text-public-sync" / "cloudflare-auth"))

    def test_deploy_auth_directory_uses_configured_external_runtime_only(self):
        external = self.root / "separate-private-runtime"
        value = json.loads((self.root / "config.json").read_text())
        value["runtime_root"] = str(external)
        self.write(self.root / "config.json", json.dumps(value))
        publisher = SYNC.Publisher(self.root, FakePublisher.base)
        completed = subprocess.CompletedProcess([], 0, "ok", "")
        with patch.object(SYNC.subprocess, "run", return_value=completed) as run:
            publisher._run(["mock"], "deploy")
        self.assertEqual(run.call_args.kwargs["env"]["XDG_CONFIG_HOME"], str(external / "text-public-sync" / "cloudflare-auth"))
        self.assertNotIn("deployment/site", run.call_args.kwargs["env"]["XDG_CONFIG_HOME"])
        with patch.dict(SYNC.os.environ, {"XDG_CONFIG_HOME": "caller-config"}), patch.object(SYNC.subprocess, "run", return_value=completed) as export:
            publisher._run(["mock"], "export")
        self.assertEqual(export.call_args.kwargs["env"]["XDG_CONFIG_HOME"], "caller-config")

    def test_relative_runtime_for_deploy_rejected_before_starting_program(self):
        value = json.loads((self.root / "config.json").read_text())
        value["runtime_root"] = "deployment/site"
        self.write(self.root / "config.json", json.dumps(value))
        publisher = SYNC.Publisher(self.root, FakePublisher.base)
        with patch.object(SYNC.subprocess, "run") as run:
            with self.assertRaises(SYNC.StageFailure) as error:
                publisher._run(["mock"], "deploy")
        self.assertEqual(error.exception.code, "configuration")
        run.assert_not_called()

    def test_failure_output_is_never_copied_to_status_or_exception(self):
        publisher = SYNC.Publisher(self.root, FakePublisher.base)
        secret = "Bearer secret-login-token-do-not-persist"
        completed = subprocess.CompletedProcess([], 1, "authentication " + secret, "")
        with patch.object(SYNC.subprocess, "run", return_value=completed):
            with self.assertRaises(SYNC.StageFailure) as error:
                publisher._run(["mock"], "deploy")
        self.assertEqual(error.exception.code, "auth_required")
        self.assertNotIn(secret, str(error.exception))
        with patch.object(SYNC.subprocess, "run", side_effect=subprocess.TimeoutExpired("mock", 1, output=secret)):
            with self.assertRaises(SYNC.StageFailure) as timeout:
                publisher._run(["mock"], "export")
        self.assertEqual(timeout.exception.code, "export_timeout")
        self.assertNotIn(secret, str(timeout.exception))

    def test_once_can_read_status_without_initializing_live_backends(self):
        service = self.service()
        self.assertEqual(service.run(once=True), 0)
        self.assertEqual(SYNC.read_status(self.state_root)["state"], "synced")
        self.assertEqual(SYNC.read_status(self.root / "not-started")["state"], "not_started")


if __name__ == "__main__":
    unittest.main()
