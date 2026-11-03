"""Prepare a local environment and keep the server attached to the launch window."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import venv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_config import load_config
from codex_bridge import locate_codex as locate_configured_codex

HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
MACOS_APPLICATIONS = (Path("/Applications"), Path.home() / "Applications")


def locate_codex(configured=None):
    """Keep Mac bundle discovery in deployment; the shared core accepts an explicit executable."""
    found = locate_configured_codex(configured)
    if found or configured or os.environ.get("CODEX_EXECUTABLE"):
        return found
    if sys.platform == "darwin":
        for applications in MACOS_APPLICATIONS:
            for relative in ("Codex.app/Contents/Resources/codex",
                             "Codex.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
                             "ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"):
                candidate = applications / relative
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate.resolve())
    return None


def locate_resolve_mcp():
    """Resolve bundles a native MCP executable on Mac, without an .exe suffix."""
    explicit = os.environ.get("RESOLVE_MCP")
    if explicit:
        candidate = Path(explicit).expanduser()
        return str(candidate.resolve()) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    if sys.platform == "darwin":
        for applications in MACOS_APPLICATIONS:
            for relative in ("DaVinci Resolve/DaVinci Resolve.app/Contents/Applications/ResolveMCP",
                             "DaVinci Resolve.app/Contents/Applications/ResolveMCP"):
                candidate = applications / relative
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate.resolve())
    return None


def service_status(url, config):
    """Only reuse an instance serving this checkout's configured data folders."""
    try:
        with socket.create_connection(("127.0.0.1", config["port"]), timeout=.3):
            pass
    except ConnectionRefusedError:
        return None
    try:
        with HTTP.open(url + "/api/service/status", timeout=2) as response:
            state = json.load(response)
        paths = state.get("paths", {})
        if (state.get("app") == "video-case-workbench"
                and paths.get("records") and paths.get("library")
                and Path(paths["records"]).resolve() == Path(config["skill_root"]) / "cases"
                and Path(paths["library"]).resolve() == Path(config["runtime_root"]) / "library"):
            if state.get("shutting_down"):
                raise RuntimeError("工作台正在关闭，请稍后再双击启动。")
            return state
    except (OSError, ValueError, urllib.error.URLError, AttributeError, TypeError):
        pass
    raise RuntimeError(f"端口 {config['port']} 已被其他服务占用，请在 config.local.json 中更换 port。")


def prepare_environment():
    environment = ROOT / ".venv"
    python = environment / "bin" / "python"
    if not python.exists():
        if sys.version_info < (3, 12):
            raise RuntimeError("需要 Python 3.12 或更新版本，请安装后再次双击。")
        print("首次启动：正在创建项目专用 Python 环境…", flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    requirements = ROOT / "requirements.txt"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    marker = environment / ".workbench-requirements.sha256"
    check = [str(python), "-c", "import PIL, numpy, cv2, openpyxl"]
    installed = (marker.is_file() and marker.read_text().strip() == digest
                 and subprocess.run(check, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0)
    if not installed:
        print("正在安装项目依赖，首次启动可能需要几分钟…", flush=True)
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                        "-r", str(requirements)], cwd=ROOT, check=True)
        subprocess.run(check, check=True)
        marker.write_text(digest + "\n", encoding="utf-8")
    return python


def open_page(url, enabled):
    if enabled:
        subprocess.run(["/usr/bin/open", url], check=True)


def run_server(python, config, url, browser):
    environment = dict(os.environ)
    executable = locate_codex()
    if executable:
        environment["CODEX_EXECUTABLE"] = executable
    resolve_mcp = locate_resolve_mcp()
    if resolve_mcp:
        environment["RESOLVE_MCP"] = resolve_mcp
    child = subprocess.Popen([str(python), "-X", "utf8", "-u", str(ROOT / "server.py"),
                              "--port", str(config["port"]),
                              "--skill-root", config["skill_root"],
                              "--runtime-root", config["runtime_root"]],
                             cwd=ROOT, start_new_session=True, env=environment)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError("服务启动失败，请查看上方错误信息。")
            # The server binds before initializing its data stores; early status
            # requests can time out until initialization finishes.
            try:
                state = service_status(url, config)
            except RuntimeError:
                state = None
            if state and state.get("pid") == child.pid:
                print(f"\n工作台已启动：{url}", flush=True)
                print("保持此窗口打开；按 Ctrl+C 或关闭窗口可停止服务。", flush=True)
                open_page(url, browser)
                return child.wait()
            time.sleep(.25)
        raise RuntimeError("启动等待超过 60 秒，请查看上方错误信息后重试。")
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGINT)
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=5)


def interrupted(signum, frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description="案例工作台 macOS 一键启动")
    parser.add_argument("--no-browser", action="store_true", help="启动但不自动打开浏览器")
    args = parser.parse_args()
    signal.signal(signal.SIGHUP, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    config = load_config(ROOT)
    if type(config["port"]) is not int or not 1 <= config["port"] <= 65535:
        raise RuntimeError("config.local.json 中的 port 必须是 1 到 65535 之间的整数。")
    url = f"http://127.0.0.1:{config['port']}"
    if service_status(url, config):
        print(f"工作台已在运行，打开页面：{url}", flush=True)
        open_page(url, not args.no_browser)
        return 0
    folder = ROOT / ".local"
    folder.mkdir(exist_ok=True)
    with (folder / "launcher.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("工作台正在准备或启动，请稍候再双击。", flush=True)
            return 0
        # Another click may have completed startup between the first check and
        # acquiring the lock. Never start another writer for the same data.
        if service_status(url, config):
            open_page(url, not args.no_browser)
            return 0
        python = prepare_environment()
        print(f"正在启动案例工作台：{url}", flush=True)
        return run_server(python, config, url, not args.no_browser)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n工作台已停止。", flush=True)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"\n启动失败：{error}", file=sys.stderr, flush=True)
        sys.exit(1)
