"""Restartable loopback HTTP gateway; the separate task host owns every business worker."""
from __future__ import annotations

import argparse
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import unquote, urlsplit

from app_config import load_config


APP_ROOT = Path(__file__).resolve().parent
CHUNK_SIZE = 64 * 1024
CONTROL_LIMIT = 1024 * 1024
ENGINE_REFRESH_RETRY_SECONDS = 15
GATEWAY_HEADER = "X-Workbench-Gateway"
FORWARDED_HEADER = "X-Workbench-Forwarded"
HOP_HEADERS = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                         "te", "trailer", "transfer-encoding", "upgrade"})


class GatewayError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def code_signature(root):
    newest = 0
    for path in Path(root).glob("*.py"):
        try:
            newest = max(newest, path.stat().st_mtime_ns)
        except OSError:
            pass
    return newest


class ServiceGateway(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = os.name != "nt"

    def __init__(self, skill_root, runtime_root, *, port=8767, root=APP_ROOT):
        self.skill_root = Path(skill_root).resolve()
        self.runtime_root = Path(runtime_root).resolve()
        self.root = Path(root).resolve()
        self.manifest = self.runtime_root / "service" / "task-host.json"
        self.started_version = code_signature(self.root)
        self.shutting_down = False
        self._control_lock = threading.Condition()
        self._active_writes = 0
        self._restart_pending = False
        self._restart_process = None
        self._engine_refreshes = {}
        super().__init__(("127.0.0.1", port), GatewayHandler)

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def host_manifest(self):
        try:
            folder = self.manifest.parent
            for path in (folder, self.manifest):
                metadata = path.lstat()
                if path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
                    raise ValueError("Linked control path")
            if (folder.resolve() != self.runtime_root / "service"
                    or self.manifest.stat().st_nlink > 1 or not stat.S_ISREG(self.manifest.stat().st_mode)):
                raise ValueError("Linked control file")
            value = json.loads(self.manifest.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or value.get("app") != "video-case-workbench"
                    or value.get("role") != "task-host" or value.get("protocol_version") != 1
                    or type(value.get("port")) is not int or not 1 <= value["port"] <= 65535
                    or value["port"] == self.server_port or value.get("public_port") != self.server_port
                    or type(value.get("pid")) is not int or value["pid"] <= 0
                    or not isinstance(value.get("launch_id"), str) or not value["launch_id"]
                    or not isinstance(value.get("proxy_secret"), str) or len(value["proxy_secret"]) < 32):
                raise ValueError("Invalid task-host manifest")
            paths = value.get("paths") or {}
            if (Path(paths["records"]).resolve() != self.skill_root / "cases"
                    or Path(paths["library"]).resolve() != self.runtime_root / "library"
                    or Path(paths["runtime"]).resolve() != self.runtime_root
                    or Path(paths["code_root"]).resolve() != self.root):
                raise ValueError("Unrelated task host")
            return value
        except (OSError, ValueError, TypeError, KeyError):
            raise GatewayError(503, "后台任务服务暂未连接，请稍后重试；现有任务不会因此被停止。") from None

    def host_status(self):
        host = self.host_manifest()
        connection = http.client.HTTPConnection("127.0.0.1", host["port"], timeout=5)
        try:
            # A control read uses the internal Host and no forwarded marker.
            connection.request("GET", "/api/service/status", headers={GATEWAY_HEADER: host["proxy_secret"]})
            response = connection.getresponse()
            body = response.read(CONTROL_LIMIT + 1)
            if response.status != 200 or len(body) > CONTROL_LIMIT:
                raise ValueError("Task host unavailable")
            value = json.loads(body)
            if (value.get("app") != "video-case-workbench" or value.get("role") != "task-host"
                    or value.get("pid") != host["pid"] or value.get("launch_id") != host["launch_id"]
                    or not isinstance(value.get("token"), str) or not value["token"]):
                raise ValueError("Task host identity changed")
            return host, value
        except (OSError, http.client.HTTPException, ValueError, TypeError):
            raise GatewayError(503, "后台任务服务暂未连接，请稍后重试；现有任务不会因此被停止。") from None
        finally:
            connection.close()

    def launch(self, *extra):
        creation = ((subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
                    if os.name == "nt" else 0)
        return subprocess.Popen([sys.executable, "-X", "utf8", str(self.root / "launcher.pyw"), "--no-browser", *extra],
                                cwd=str(self.root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=creation)

    def refresh_engine_when_idle(self, host, status):
        with self._control_lock:
            launch_id = host["launch_id"]
            # The manifest chooses the current owner. Retired helpers keep running independently.
            self._engine_refreshes = {key: value for key, value in self._engine_refreshes.items() if key == launch_id}
            if not status.get("code_stale"):
                self._engine_refreshes.pop(launch_id, None)
                return
            previous = self._engine_refreshes.get(launch_id)
            if previous is not None:
                if previous["process"].poll() is None:
                    return
                if previous["finished_at"] is None:
                    previous["finished_at"] = time.monotonic()
                if time.monotonic() - previous["finished_at"] < ENGINE_REFRESH_RETRY_SECONDS:
                    return
            try:
                child = self.launch("--refresh-task-host-on-idle")
            except OSError:
                return  # A later status read may retry a helper that failed to start.
            self._engine_refreshes[launch_id] = {"process": child, "finished_at": None}

    def status(self):
        host, status = self.host_status()
        self.refresh_engine_when_idle(host, status)
        with self._control_lock:
            self._refresh_restart_state_locked()
            restart_pending = self._restart_pending
        public = {key: value for key, value in status.items() if key not in {"proxy_secret", "gateway_secret"}}
        return {**public, "pid": os.getpid(), "role": "gateway", "port": self.server_port,
                "task_host_pid": host["pid"], "task_host_launch_id": host["launch_id"],
                "restart_keeps_tasks": True, "shutdown_supported": True, "shutting_down": self.shutting_down,
                "restart_pending": restart_pending,
                "task_host_code_stale": bool(status.get("code_stale")),
                "code_stale": code_signature(self.root) > self.started_version}

    def start_restart(self):
        with self._control_lock:
            self._refresh_restart_state_locked()
            if self.shutting_down:
                raise GatewayError(409, "网页服务已经在重启，请稍候。")
            if self._active_writes:
                raise GatewayError(409, "页面正在保存或上传，请完成后再重启；后台任务继续运行。")
            if self._restart_pending:
                return
            # Reserve write admission before the launcher starts; background host workers remain unaffected.
            self._restart_pending = True
            try:
                self._restart_process = self.launch()
            except OSError:
                self._restart_pending = False
                self._control_lock.notify_all()
                raise GatewayError(503, "网页服务重启尚未开始，请稍后重试；后台任务继续运行。") from None

    def _refresh_restart_state_locked(self):
        if (self._restart_pending and not self.shutting_down and self._restart_process is not None
                and self._restart_process.poll() is not None):
            # An exited helper did not close this still-live gateway (including a lost launch-lock race).
            self._restart_pending = False
            self._control_lock.notify_all()

    def begin_proxy_write(self):
        with self._control_lock:
            self._refresh_restart_state_locked()
            if self.shutting_down or self._restart_pending:
                raise GatewayError(409, "网页服务正在重新启动，请稍后再保存或上传；后台任务继续运行。")
            self._active_writes += 1

    def end_proxy_write(self):
        with self._control_lock:
            self._active_writes -= 1
            self._control_lock.notify_all()

    def prepare_shutdown(self):
        with self._control_lock:
            if self.shutting_down:
                raise GatewayError(409, "网页服务已经在关闭，请稍候。")
            if self._active_writes:
                raise GatewayError(409, "页面正在保存或上传，请完成后再重启；后台任务继续运行。")
            self.shutting_down = True


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "WorkbenchGateway/1.0"
    disable_nagle_algorithm = True

    def log_message(self, message, *args):
        if urlsplit(getattr(self, "path", "")).path != "/api/service/status":
            super().log_message(message, *args)

    def output(self, status, value):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def guard(self, write=False, token=""):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts or self.client_address[0] not in {"127.0.0.1", "::1"}:
            raise GatewayError(403, "仅允许通过本机地址访问。")
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in hosts}:
            raise GatewayError(403, "请在本地工作台中操作。")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise GatewayError(403, "不接受跨站请求。")
        if write:
            if any(self.headers.get(name) for name in ("Forwarded", "X-Forwarded-For", "CF-Connecting-IP")):
                raise GatewayError(403, "请在工作台电脑上重启网页服务。")
            if not secrets.compare_digest(self.headers.get("X-Workbench-Token", ""), token):
                raise GatewayError(403, "页面连接已过期，请刷新后重试。")

    def content_length(self):
        values = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(values) > 1:
            raise GatewayError(400, "请求内容长度无效。")
        if not values:
            return 0
        if not re.fullmatch(r"[0-9]{1,18}", values[0]):
            raise GatewayError(400, "请求内容长度无效。")
        return int(values[0])

    def empty_body(self):
        length = self.content_length()
        if length > 1024:
            raise GatewayError(400, "重启或关闭网页服务只接受空对象。")
        try:
            raw = self.rfile.read(length)
            self._body_read += len(raw)
            value = json.loads(raw) if length else None
        except (ValueError, UnicodeDecodeError):
            value = None
        if value != {}:
            raise GatewayError(400, "重启或关闭网页服务只接受空对象。")

    def drain_rejected_body(self):
        # An unread small POST can cause Windows to reset the connection before its useful 409 arrives.
        timeout = self.connection.gettimeout()
        try:
            left = self.content_length() - self._body_read
            if self.command == "POST" and 0 < left <= 4 * 1024 * 1024:
                self.connection.settimeout(1)
                self.rfile.read(left)
        except (OSError, GatewayError):
            pass
        finally:
            self.connection.settimeout(timeout)

    def proxy(self):
        # The host repeats its original business guards. Preserve Host/Origin, including extension origins.
        host = self.server.host_manifest()
        length = self.content_length()
        connection = http.client.HTTPConnection("127.0.0.1", host["port"], timeout=900)
        connection_tokens = {name.strip().lower() for name in self.headers.get("Connection", "").split(",")}
        blocked = HOP_HEADERS | connection_tokens | {GATEWAY_HEADER.lower(), FORWARDED_HEADER.lower(), "expect"}
        sent_headers = False
        try:
            connection.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            for key, value in self.headers.items():
                if key.lower() not in blocked:
                    connection.putheader(key, value)
            connection.putheader(GATEWAY_HEADER, host["proxy_secret"])
            connection.putheader(FORWARDED_HEADER, "1")
            connection.putheader("Connection", "close")
            connection.endheaders()
            left = length
            while left:
                chunk = self.rfile.read(min(CHUNK_SIZE, left))
                if not chunk:
                    raise ConnectionError("Incomplete request")
                self._body_read += len(chunk)
                connection.send(chunk)
                left -= len(chunk)
            response = connection.getresponse()
            self.send_response(response.status, response.reason)
            response_tokens = {name.strip().lower() for name in response.getheader("Connection", "").split(",")}
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS | response_tokens | {GATEWAY_HEADER.lower(), FORWARDED_HEADER.lower()}:
                    self.send_header(key, value)
            # Decode upstream chunk framing through HTTPResponse; closing delimits SSE and other unknown lengths.
            self.close_connection = True
            self.send_header("Connection", "close")
            self.end_headers()
            sent_headers = True
            if self.command != "HEAD":
                while chunk := response.read1(CHUNK_SIZE):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (OSError, http.client.HTTPException):
            self.close_connection = True
            if not sent_headers:
                raise GatewayError(503, "后台任务服务暂未连接，请稍后重试；现有任务不会因此被停止。") from None
        finally:
            connection.close()

    def dispatch(self):
        writing = False
        self._body_read = 0
        try:
            if self.headers.get(GATEWAY_HEADER) is not None or self.headers.get(FORWARDED_HEADER) is not None:
                raise GatewayError(403, "不接受自行声明的内部服务凭据。")
            path = unquote(urlsplit(self.path).path)
            if path == "/api/service/ready":
                raise GatewayError(404, "页面接口不存在。")
            if path == "/api/service/status" and self.command in {"GET", "HEAD"}:
                self.guard()
                return self.output(200, self.server.status())
            if path in {"/api/service/restart", "/api/service/shutdown"}:
                self.guard()
                if self.command != "POST":
                    raise GatewayError(405, "此操作需要提交请求。")
                _, status = self.server.host_status()
                self.guard(write=True, token=status["token"])
                self.empty_body()
                if path.endswith("/restart"):
                    self.server.start_restart()
                    return self.output(200, {"ok": True, "pid": os.getpid(), "restart_keeps_tasks": True})
                self.server.prepare_shutdown()
                self.close_connection = True
                try:
                    self.output(200, {"ok": True, "pid": os.getpid(), "restart_keeps_tasks": True})
                    self.wfile.flush()
                finally:
                    threading.Thread(target=self.server.shutdown, name="gateway-shutdown", daemon=True).start()
                return
            if self.command == "POST":
                self.server.begin_proxy_write()
                writing = True
            self.proxy()
        except GatewayError as error:
            self.drain_rejected_body()
            self.close_connection = True
            self.output(error.status, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        finally:
            if writing:
                self.server.end_proxy_write()

    do_GET = do_HEAD = do_POST = dispatch


def make_gateway(skill_root, runtime_root, *, port=8767, root=APP_ROOT):
    return ServiceGateway(skill_root, runtime_root, port=port, root=root)


def main(argv=None):
    config = load_config(APP_ROOT)
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-root", default=config["skill_root"])
    parser.add_argument("--runtime-root", default=config["runtime_root"])
    parser.add_argument("--port", type=int, default=config["port"])
    args = parser.parse_args(argv)
    gateway = make_gateway(args.skill_root, args.runtime_root, port=args.port)
    try:
        gateway.serve_forever(poll_interval=.05)
    except KeyboardInterrupt:
        pass
    finally:
        # Closing the gateway never signals, joins or cancels a task-host worker.
        gateway.server_close()


if __name__ == "__main__":
    main()
