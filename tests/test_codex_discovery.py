"""Executable discovery with a GUI-like PATH and disposable macOS app bundles."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

if os.name == "nt":
    raise unittest.SkipTest("The macOS launcher requires POSIX file locking.")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deployment.launch_macos import locate_codex, locate_resolve_mcp, run_server


class CodexDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-discovery-")
        self.addCleanup(temporary.cleanup)
        self.applications = Path(temporary.name)
        for context in (patch("deployment.launch_macos.sys.platform", "darwin"),
                        patch("deployment.launch_macos.MACOS_APPLICATIONS", (self.applications,)),
                        patch("codex_bridge.shutil.which", return_value=None),
                        patch.dict(os.environ, {"CODEX_EXECUTABLE": "", "RESOLVE_MCP": ""})):
            context.start()
            self.addCleanup(context.stop)

    def bundle_cli(self, relative):
        executable = self.applications / relative
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        return executable

    def test_finds_chatgpt_bundled_cli_without_a_path_entry(self):
        executable = self.bundle_cli("ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
        self.assertEqual(locate_codex(), str(executable.resolve()))

    def test_finds_standalone_codex_app_without_a_path_entry(self):
        executable = self.bundle_cli("Codex.app/Contents/Resources/codex")
        self.assertEqual(locate_codex(), str(executable.resolve()))

    def test_explicit_and_path_choices_take_precedence_over_app_bundles(self):
        self.bundle_cli("Codex.app/Contents/Resources/codex")
        selected = self.bundle_cli("custom/codex")
        self.assertEqual(locate_codex(str(selected)), str(selected.resolve()))
        with patch("codex_bridge.shutil.which", return_value=str(selected)):
            self.assertEqual(locate_codex(), str(selected.resolve()))

    def test_invalid_explicit_path_does_not_silently_select_another_cli(self):
        self.bundle_cli("Codex.app/Contents/Resources/codex")
        self.assertIsNone(locate_codex(str(self.applications / "missing-codex")))

    def test_non_executable_bundle_is_not_reported_as_connected(self):
        executable = self.bundle_cli("Codex.app/Contents/Resources/codex")
        executable.chmod(0o644)
        self.assertIsNone(locate_codex())

    def test_server_receives_bundled_cli_without_changing_parent_environment(self):
        executable = self.bundle_cli("Codex.app/Contents/Resources/codex")
        child = Mock(pid=12345)
        child.poll.side_effect = (None, 0)
        child.wait.return_value = 0
        config = {"port": 18768, "skill_root": "/unused/cases", "runtime_root": "/unused/runtime"}
        with patch("deployment.launch_macos.subprocess.Popen", return_value=child) as spawn, \
                patch("deployment.launch_macos.service_status", return_value={"pid": child.pid}), \
                patch("deployment.launch_macos.open_page"):
            self.assertEqual(run_server(Path(sys.executable), config, "http://127.0.0.1:18768", False), 0)
        self.assertEqual(spawn.call_args.kwargs["env"]["CODEX_EXECUTABLE"], str(executable.resolve()))
        self.assertEqual(os.environ.get("CODEX_EXECUTABLE"), "")

    def test_finds_resolve_mcp_in_standard_mac_installation(self):
        executable = self.bundle_cli("DaVinci Resolve/DaVinci Resolve.app/Contents/Applications/ResolveMCP")
        self.assertEqual(locate_resolve_mcp(), str(executable.resolve()))

    def test_finds_resolve_app_directly_in_applications(self):
        executable = self.bundle_cli("DaVinci Resolve.app/Contents/Applications/ResolveMCP")
        self.assertEqual(locate_resolve_mcp(), str(executable.resolve()))

    def test_resolve_mcp_explicit_override_takes_precedence(self):
        self.bundle_cli("DaVinci Resolve/DaVinci Resolve.app/Contents/Applications/ResolveMCP")
        selected = self.bundle_cli("custom/ResolveMCP")
        with patch.dict(os.environ, {"RESOLVE_MCP": str(selected)}):
            self.assertEqual(locate_resolve_mcp(), str(selected.resolve()))

    def test_invalid_resolve_mcp_override_is_not_silently_replaced(self):
        self.bundle_cli("DaVinci Resolve/DaVinci Resolve.app/Contents/Applications/ResolveMCP")
        with patch.dict(os.environ, {"RESOLVE_MCP": str(self.applications / "missing-ResolveMCP")}):
            self.assertIsNone(locate_resolve_mcp())

    def test_non_executable_resolve_mcp_is_not_reported_as_available(self):
        executable = self.bundle_cli("DaVinci Resolve/DaVinci Resolve.app/Contents/Applications/ResolveMCP")
        executable.chmod(0o644)
        self.assertIsNone(locate_resolve_mcp())

    def test_server_receives_resolve_mcp_without_changing_parent_environment(self):
        executable = self.bundle_cli("DaVinci Resolve/DaVinci Resolve.app/Contents/Applications/ResolveMCP")
        child = Mock(pid=12345)
        child.poll.side_effect = (None, 0)
        child.wait.return_value = 0
        config = {"port": 18768, "skill_root": "/unused/cases", "runtime_root": "/unused/runtime"}
        with patch("deployment.launch_macos.subprocess.Popen", return_value=child) as spawn, \
                patch("deployment.launch_macos.service_status", return_value={"pid": child.pid}), \
                patch("deployment.launch_macos.open_page"):
            self.assertEqual(run_server(Path(sys.executable), config, "http://127.0.0.1:18768", False), 0)
        self.assertEqual(spawn.call_args.kwargs["env"]["RESOLVE_MCP"], str(executable.resolve()))
        self.assertEqual(os.environ.get("RESOLVE_MCP"), "")
