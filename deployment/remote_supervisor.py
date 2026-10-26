"""Own only this workbench's authenticated proxy and named-tunnel connector."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_config import load_config
DEFAULT_NODE = Path("C:/Program Files/nodejs/node.exe")
DEFAULT_CLOUDFLARED = Path.home() / ".antigravity_tools" / "bin" / "cloudflared.exe"


class ConfigurationError(ValueError):
    pass


class AlreadyRunning(RuntimeError):
    pass


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class ProcessLock:
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.file.seek(0, os.SEEK_END) == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise AlreadyRunning() from None
        return self

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None


class ChildJob:
    """Windows closes these exact children if the supervisor crashes or is killed."""
    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t),
                        ("maximum", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class Limits(ctypes.Structure):
            _fields_ = [("basic", BasicLimit), ("io", ctypes.c_ulonglong * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = self.kernel.CreateJobObjectW(None, None)
        limits = Limits()
        limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not handle or not self.kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            if handle:
                self.kernel.CloseHandle(handle)
            raise OSError("child_ownership_unavailable")
        self.handle = handle

    def add(self, process):
        if self.handle is not None and not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            process.terminate()
            process.wait(timeout=5)
            raise OSError("child_ownership_unavailable")

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


@dataclass(frozen=True)
class Settings:
    root: Path
    runtime: Path
    config: Path
    node: Path
    cloudflared: Path
    token: Path
    proxy: Path
    https_proxy: str = ""
    hostname: str = ""
    auth_mode: str = "cloudflare"
    guest_enabled: bool = False

    def child_kinds(self):
        # Start the snapshot independently; always stop the connector before the proxy.
        return ("guest_snapshot", "proxy", "tunnel") if self.guest_enabled else ("proxy", "tunnel")

    def commands(self):
        commands = {
            "proxy": [str(self.node), str(self.proxy), "--config", str(self.config)],
            "tunnel": [str(self.cloudflared), "tunnel", "--config", str(self.runtime / "cloudflared.yml"),
                       "--no-autoupdate", "--metrics", "127.0.0.1:0", "--loglevel", "error",
                       "run", "--protocol", "http2", "--token-file", str(self.token)],
        }
        if self.guest_enabled:
            executable = Path(sys.executable)
            windowless = executable.with_name("pythonw.exe")
            if os.name == "nt" and windowless.is_file():
                executable = windowless
            commands["guest_snapshot"] = [str(executable), "-B", str(self.root / "deployment" / "guest_snapshot.py")]
        return commands

    def environment(self, kind):
        # The connector must never inherit another tunnel's token/config or a global proxy.
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith("TUNNEL_") and key.upper() not in {
                           "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "NODE_USE_ENV_PROXY",
                           "NO_AUTOUPDATE", "TUNNEL_ORIGIN_CERT"}}
        if kind == "proxy" and self.https_proxy:
            environment.update(HTTPS_PROXY=self.https_proxy, NODE_USE_ENV_PROXY="1",
                               NO_PROXY="127.0.0.1,localhost,::1")
        return environment


def runtime_path(root=ROOT):
    try:
        config = load_config(root)
        runtime = Path(config["runtime_root"])
        if not runtime.is_absolute():
            raise ValueError()
        return runtime / "remote-access"
    except (OSError, ValueError, KeyError, TypeError):
        raise ConfigurationError("工作台运行目录配置无效。") from None


def validate_password_account(runtime):
    """Validate the fixed local credential format without returning or logging its contents."""
    try:
        with (runtime / "account.json").open("rb") as source:
            raw = source.read(8193)
        if not 2 <= len(raw) <= 8192:
            raise ValueError()
        account = json.loads(raw.decode("utf-8"))
        if (not isinstance(account, dict) or type(account.get("version")) is not int or account["version"] != 1
                or not isinstance(account.get("username"), str) or not account["username"].strip()
                or len(account["username"]) > 128 or re.search(r"[\x00-\x1f\x7f]", account["username"])
                or not isinstance(account.get("updated_at"), str) or not 1 <= len(account["updated_at"]) <= 64):
            raise ValueError()
        for field, length in (("salt", 64), ("password_hash", 128), ("credential_id", 32)):
            if not isinstance(account.get(field), str) or not re.fullmatch(r"[a-f0-9]{" + str(length) + "}", account[field]):
                raise ValueError()
        if account.get("scrypt") != {"n": 32768, "r": 8, "p": 3}:
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise ConfigurationError("公网账号配置缺失或无效；未启动连接。") from None


def load_settings(root=ROOT, config_path=None):
    runtime = runtime_path(root)
    config_path = Path(config_path) if config_path else runtime / "config.json"
    try:
        if not config_path.is_absolute():
            raise ValueError()
        data = json.loads(config_path.read_text(encoding="utf-8-sig"))
        hostname = data["hostname"]
        if not isinstance(hostname, str) or not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", hostname):
            raise ValueError()
        auth_mode = data.get("authMode", "cloudflare")
        if auth_mode not in {"cloudflare", "password"}:
            raise ValueError()
        guest_enabled = data.get("guestEnabled", False)
        if type(guest_enabled) is not bool or (guest_enabled and auth_mode != "password"):
            raise ValueError()
        if auth_mode == "password":
            # An unset password keeps private routes closed while the read-only guest snapshot is available.
            if not guest_enabled or os.path.lexists(runtime / "account.json"):
                validate_password_account(runtime)
        else:
            team = urlsplit(data["teamDomain"])
            if (team.scheme != "https" or not team.hostname or not team.hostname.endswith(".cloudflareaccess.com")
                    or team.username or team.password or team.port or team.path not in {"", "/"} or team.query or team.fragment):
                raise ValueError()
            if not isinstance(data["audience"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", data["audience"]):
                raise ValueError()
            emails = data["allowedEmails"]
            if not isinstance(emails, list) or not emails or not all(isinstance(email, str) and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) for email in emails):
                raise ValueError()
        node = Path(data.get("nodePath", DEFAULT_NODE))
        cloudflared = Path(data.get("cloudflaredPath", DEFAULT_CLOUDFLARED))
        proxy = root / "deployment" / "remote_proxy.mjs"
        if not all(path.is_absolute() and path.is_file() for path in (node, cloudflared, proxy)):
            raise ValueError()
        if guest_enabled and not (root / "deployment" / "guest_snapshot.py").is_file():
            raise ValueError()
        token = runtime / "tunnel-token.txt"
        if not 1 <= token.stat().st_size <= 16384 or not re.fullmatch(r"[A-Za-z0-9_+=/.-]+", token.read_text(encoding="utf-8").strip()):
            raise ValueError()
        https_proxy = data.get("httpsProxy", "")
        if https_proxy:
            local = urlsplit(https_proxy)
            if (local.scheme != "http" or local.hostname != "127.0.0.1" or not local.port
                    or local.username or local.password or local.path not in {"", "/"} or local.query or local.fragment):
                raise ValueError()
        return Settings(root, runtime, config_path, node, cloudflared, token, proxy,
                        https_proxy=https_proxy, hostname=hostname, auth_mode=auth_mode, guest_enabled=guest_enabled)
    except (OSError, ValueError, KeyError, TypeError):
        raise ConfigurationError("公网私有配置、程序或独立隧道令牌缺失或无效；未启动连接。") from None


class Supervisor:
    def __init__(self, settings, *, clock=time.monotonic, spawn=subprocess.Popen, job=None, logger=None):
        self.settings, self.clock, self.spawn, self.job = settings, clock, spawn, job
        self.logger = logger or logging.getLogger("remote-supervisor")
        self.instance = uuid4().hex
        self.stop_event = threading.Event()
        self.wakeup = threading.Event()
        self.lifecycle_lock = threading.RLock()
        self.children = {kind: {"process": None, "failures": 0, "retry_at": 0, "started": 0, "exit_code": None}
                         for kind in settings.child_kinds()}
        self.state = "starting"
        self.status_write_error = None
        self.stop_reason = "requested"
        self.reload_requests = set()
        self.last_reload_request_id = None

    def status(self):
        return {"schema": 1, "pid": os.getpid(), "instance_id": self.instance, "state": self.state,
                "auth_mode": self.settings.auth_mode, "last_reload_request_id": self.last_reload_request_id,
                "guest_enabled": self.settings.guest_enabled,
                "updated_at": timestamp(), "children": {
                    kind: {"pid": child["process"].pid if child["process"] is not None else None,
                           "failures": child["failures"], "last_exit_code": child["exit_code"]}
                    for kind, child in self.children.items()}}

    def write_status(self):
        # Antivirus/readers can briefly prevent replacement on Windows. Reporting
        # must never tear down a healthy authenticated proxy and connector.
        try:
            atomic_json(self.settings.runtime / "status.json", self.status())
        except OSError as error:
            error_type = type(error).__name__
            if self.status_write_error != error_type:
                self.logger.warning("status_write_failed exception=%s", error_type)
            self.status_write_error = error_type
            return False
        if self.status_write_error is not None:
            self.logger.info("status_write_recovered")
            self.status_write_error = None
        return True

    def should_stop(self):
        if self.stop_event.is_set():
            return True
        try:
            request = json.loads((self.settings.runtime / "stop.request").read_text(encoding="utf-8"))
            if request.get("instance_id") == self.instance:
                self.stop_reason = "stop_request"
                return True
            return False
        except (OSError, ValueError, AttributeError):
            return False

    def reload_if_requested(self):
        """Return True for an applied reload, False for fatal validation, None when idle."""
        try:
            with (self.settings.runtime / "reload.request").open("rb") as source:
                raw = source.read(2049)
            if len(raw) > 2048:
                return None
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict) or request.get("instance_id") != self.instance:
                return None
            request_id = request.get("request_id")
            if (not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id)
                    or request_id in self.reload_requests):
                return None
        except (OSError, ValueError):
            return None
        self.reload_requests.add(request_id)
        self.last_reload_request_id = request_id
        try:
            settings = load_settings(self.settings.root, self.settings.config)
            # The supervisor lock and owned children belong to this runtime only.
            if settings.runtime != self.settings.runtime:
                raise ConfigurationError("runtime_changed")
        except ConfigurationError:
            with self.lifecycle_lock:
                self.state = "configuration_error"
                self.stop_reason = "reload_configuration_invalid"
                self.stop_event.set()
                self.stop_children()
            self.logger.error("reload_configuration_invalid")
            self.write_status()
            return False
        with self.lifecycle_lock:
            self.stop_children()
            if self.stop_event.is_set():
                return False
            self.settings = settings
            self.children = {kind: {"process": None, "failures": 0, "retry_at": 0, "started": 0, "exit_code": None}
                             for kind in settings.child_kinds()}
            self.state = "starting"
        self.logger.info("configuration_reloaded")
        self.write_status()
        return True

    def _observe_proxy(self, process):
        readiness = {"ready": threading.Event(), "observed": threading.Event()}
        expected = ("Protected workbench proxy listening on 127.0.0.1:8768 for " + self.settings.hostname + "\n").encode("utf-8")

        def consume():
            try:
                while True:
                    line = process.stdout.readline(512)
                    if not line:
                        break
                    if line == expected:
                        readiness["ready"].set()
                        readiness["observed"].set()
            except (OSError, ValueError):
                pass
            finally:
                readiness["ready"].clear()
                readiness["observed"].set()
                # EOF or proxy exit removes the connector before any retry can occur.
                try:
                    with self.lifecycle_lock:
                        if self.children["proxy"]["process"] is process:
                            self._stop_child("tunnel")
                finally:
                    self.wakeup.set()

        threading.Thread(target=consume, name="protected-proxy-output", daemon=True).start()
        return readiness

    def _stop_child(self, kind):
        child = self.children[kind]
        process = child["process"]
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError) as error:
            self.logger.warning("%s_stop_pending exception=%s", kind, type(error).__name__)
            if process.poll() is None:
                # If a child handle cannot be stopped, revoke the whole owned job.
                # Never reconnect while a previous connector might still be alive.
                self.stop_event.set()
                self.stop_reason = "child_stop_failed"
                if self.job is not None:
                    self.job.close()
        finally:
            child["process"] = None
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except (OSError, ValueError) as error:
                    # An observer may already have seen EOF; cleanup of this dead
                    # handle must not abort cleanup/restart of the other child.
                    self.logger.warning("%s_output_close_failed exception=%s", kind, type(error).__name__)

    def tick(self):
        with self.lifecycle_lock:
            self._tick_locked()

    def _tick_locked(self):
        now = self.clock()
        for kind, child in self.children.items():
            process = child["process"]
            unhealthy = (kind == "proxy" and process is not None and
                         not child["readiness"]["ready"].is_set())
            if process is not None and (process.poll() is not None or unhealthy):
                if kind == "proxy":
                    self._stop_child("tunnel")
                child.update(exit_code=process.returncode, failures=child["failures"] + 1)
                self._stop_child(kind)
                child["retry_at"] = now + min(60, 2 ** min(child["failures"] - 1, 6))
                self.logger.warning("%s_exited code=%s", kind, process.returncode)
            elif process is not None and now - child["started"] >= 60:
                child["failures"] = 0
            if child["process"] is None and now >= child["retry_at"]:
                if self.stop_event.is_set():
                    continue
                if kind == "tunnel":
                    proxy = self.children["proxy"]
                    if (proxy["process"] is None or proxy["process"].poll() is not None or
                            not proxy["readiness"]["ready"].is_set()):
                        continue
                try:
                    process = self.spawn(self.settings.commands()[kind], cwd=self.settings.root,
                                         env=self.settings.environment(kind), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE if kind == "proxy" else subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, bufsize=0,
                                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    child.update(process=process, started=now)
                    if self.job is not None:
                        self.job.add(process)
                    if kind == "proxy":
                        readiness = child["readiness"] = self._observe_proxy(process)
                        readiness["observed"].wait(timeout=5)
                        if not readiness["ready"].is_set() or process.poll() is not None:
                            self._stop_child("tunnel")
                            self._stop_child("proxy")
                            raise OSError("proxy_not_ready")
                    self.logger.info("%s_started pid=%s", kind, process.pid)
                except (OSError, subprocess.SubprocessError) as error:
                    if kind == "proxy":
                        self._stop_child("tunnel")
                    self._stop_child(kind)
                    child["failures"] += 1
                    child["retry_at"] = now + min(60, 2 ** min(child["failures"] - 1, 6))
                    self.logger.error("%s_start_failed exception=%s", kind, type(error).__name__)
        self.state = "children_running" if all(child["process"] is not None for child in self.children.values()) else "retry_wait"
        self.write_status()

    def stop_children(self):
        # Popen handles identify our children; never enumerate or kill by executable name.
        with self.lifecycle_lock:
            for kind in reversed(self.children):
                self._stop_child(kind)

    def run(self):
        self.logger.info("supervisor_started pid=%s", os.getpid())
        try:
            self.settings.runtime.mkdir(parents=True, exist_ok=True)
            # Explicit empty config prevents discovery of another app's cloudflared YAML.
            (self.settings.runtime / "cloudflared.yml").write_text("{}\n", encoding="utf-8")
            self.write_status()
            while not self.should_stop():
                if self.reload_if_requested() is False:
                    break
                # Missing/deleted credentials must stop the connector, including during restart.
                missing_account = (self.settings.auth_mode == "password" and not self.settings.guest_enabled and
                                   not (self.settings.runtime / "account.json").is_file())
                if (not self.settings.config.is_file() or not self.settings.token.is_file()
                        or self.settings.token.stat().st_size == 0 or missing_account):
                    self.state = "configuration_error"
                    self.stop_reason = "configuration_missing"
                    self.logger.error("configuration_missing")
                    break
                self.tick()
                self.wakeup.wait(1)
                self.wakeup.clear()
        except Exception as error:
            self.state = "failed"
            self.stop_reason = "exception"
            # Exception messages/tracebacks can contain private paths or credentials.
            self.logger.error("supervisor_failed exception=%s", type(error).__name__)
            raise
        finally:
            try:
                self.stop_children()
            finally:
                if self.job is not None:
                    self.job.close()
            if self.state not in {"configuration_error", "failed"}:
                self.state = "stopped"
            self.write_status()
            self.logger.info("supervisor_stopped state=%s reason=%s", self.state, self.stop_reason)


def running_status(runtime):
    if not (runtime / "supervisor.lock").is_file():
        return {"state": "stopped", "running": False}
    lock = ProcessLock(runtime / "supervisor.lock")
    try:
        lock.acquire()
    except AlreadyRunning:
        running = True
    else:
        running = False
        lock.close()
    try:
        status = json.loads((runtime / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = {"state": "starting" if running else "stopped"}
    status["running"] = running
    if not running:
        status["state"] = "stopped"
    return status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="私有配置文件的绝对路径")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--status", action="store_true")
    actions.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    try:
        runtime = runtime_path()
        if args.status or args.stop:
            status = running_status(runtime)
            if args.stop and status["running"]:
                instance = status.get("instance_id")
                if not instance:
                    print("公网管理进程正在启动，请稍后重试。")
                    return 1
                atomic_json(runtime / "stop.request", {"instance_id": instance})
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and running_status(runtime)["running"]:
                    time.sleep(0.2)
                status = running_status(runtime)
                if status["running"]:
                    print("停止请求已发送，管理进程仍在退出。")
                    return 1
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0
        settings = load_settings(config_path=args.config)
        if args.check:
            print("公网配置检查通过；尚未启动进程。")
            return 0
        lock = ProcessLock(runtime / "supervisor.lock")
        try:
            lock.acquire()
        except AlreadyRunning:
            return 0
        try:
            logger = logging.getLogger("remote-supervisor")
            logger.setLevel(logging.INFO)
            handler = RotatingFileHandler(runtime / "supervisor.log", maxBytes=512 * 1024, backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
            job = ChildJob()
            supervisor = Supervisor(settings, job=job, logger=logger)
            def request_stop(signum, _frame):
                supervisor.stop_reason = "signal_" + str(signum)
                supervisor.stop_event.set()
                supervisor.wakeup.set()
            signal.signal(signal.SIGINT, request_stop)
            signal.signal(signal.SIGTERM, request_stop)
            supervisor.run()
        finally:
            lock.close()
        return 0
    except ConfigurationError as error:
        print(str(error))
        return 2
    except (OSError, subprocess.SubprocessError):
        print("公网管理进程未能运行；请检查运行目录及程序是否可用。")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
