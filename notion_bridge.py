"""Notion MCP calls through the local Codex credential manager, without a model turn.

OAuth credentials stay with Codex. No API key, browser automation or inference is
needed by the workbench; the user follows the returned OAuth link once if needed.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
from uuid import uuid4

from codex_bridge import BridgeError, CodexBridge
from codex_stills_bridge import CodexStillsBridge, DISABLED_FEATURES
from elevenlabs_client import VoiceError


UPLOAD_LIMIT = 20 * 1024 * 1024


ACTION_LABELS = {"fetch": "读取页面", "query_data_sources": "查询发布日历", "create_pages": "新建排期页面",
                 "update_page": "更新排期页面", "create_file_upload": "准备上传成片", "get_tool_access": "检查访问权限"}


def unpack(result, action=""):
    if result.get("isError"):
        detail = " ".join(item.get("text", "") for item in result.get("content", []) if item.get("type") == "text")
        if "usage_limit_reached" in detail or "used up its Query Data Source allowance" in detail:
            raise VoiceError(429, "Notion 数据库查询额度暂时用完了，约 1 小时内恢复，到时再点上传即可；本地成片和文案都已保留。")
        # Say which step failed and what Notion said, so the cause is visible instead of a generic retry hint.
        step = ACTION_LABELS.get(action, action)
        said = re.sub(r"\s+", " ", detail).strip()[:240]
        raise VoiceError(502, f"Notion 未完成{'「' + step + '」' if step else '这次操作'}" + (f"：{said}" if said else "，请检查页面权限和连接后重试。"))
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    texts = [item.get("text", "") for item in result.get("content", []) if item.get("type") == "text"]
    for value in texts:
        try:
            data = json.loads(value)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
    raise VoiceError(502, "Notion 返回的数据不完整，未标记为上传成功。")


def notion_id(value):
    match = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})(?:[/?#]|$)", str(value or ""), re.I)
    if not match:
        raise VoiceError(400, "Notion 页面或数据库地址无效。")
    return match[1].replace("-", "").lower()


def page_properties(page):
    match = re.search(r"<properties>\s*(.*?)\s*</properties>", page.get("text", ""), re.S)
    try:
        return json.loads(match[1]) if match else {}
    except ValueError:
        raise VoiceError(502, "无法读取 Notion 页面的属性，未覆盖原有内容。") from None


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send_video(upload, path):
    """Send exactly one scoped multipart request; never expose the upload token."""
    path = Path(path)
    uid = notion_id(upload.get("file_upload_id"))
    url = urlsplit(upload.get("upload_url", ""))
    if (url.scheme != "https" or url.hostname != "api.notion.com" or url.port not in (None, 443)
            or url.username or url.password or url.query or url.fragment
            or url.path.replace("-", "") != f"/v1/mcp/file_uploads/{uid}/send"):
        raise VoiceError(502, "Notion 返回的上传地址无效，未发送成片。")
    data = path.read_bytes()
    if not 12 <= len(data) <= UPLOAD_LIMIT:
        raise VoiceError(400, "Notion 上传版须小于 20 MiB。")
    boundary = "workbench" + uuid4().hex
    name = re.sub(r'["/\\\r\n]', "_", str(upload.get("filename") or path.name))
    mime = upload.get("content_type") or "video/mp4"
    if mime not in {"video/mp4", "video/quicktime", "video/webm", "video/x-matroska", "video/x-msvideo"}:
        raise VoiceError(502, "Notion 返回了不匹配的视频类型。")
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    headers = dict(upload.get("upload_headers") or {})
    headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
    try:
        with build_opener(_NoRedirects()).open(Request(upload["upload_url"], data=body, headers=headers, method="POST"), timeout=180) as response:
            result = json.loads(response.read(1024 * 1024))
    except Exception:
        raise VoiceError(502, "成片上传未确认完成，请保留当前记录后重试。") from None
    if result.get("status") != "uploaded" or notion_id(result.get("file_upload_id")) != uid:
        raise VoiceError(502, "Notion 尚未确认收到完整视频，未标记为上传成功。")
    return result


class _Client:
    def __init__(self, session, server, tools):
        self.session, self.server, self.tools = session, server, tools
        self.warnings = []

    def call(self, action, arguments):
        suffix = re.sub(r"[^a-z]", "", action.lower())
        names = []
        for key, spec in self.tools.items():
            name = spec.get("name") or key
            if any(re.sub(r"[^a-z]", "", candidate.rsplit("__", 1)[-1].lower()) in {suffix, "notion" + suffix}
                   for candidate in (name, key)) and name not in names:
                names.append(name)
        if len(names) != 1:
            raise VoiceError(409, f"本机 Notion 连接尚不支持 {action}，请更新 Codex 的 Notion 连接后重试。")
        result = self.session.request("mcpServer/tool/call", {"threadId": self.session.thread_id,
            "server": self.server, "tool": names[0], "arguments": arguments}, startup=False)
        value = unpack(result, action)
        if value.get("warnings"):
            self.warnings.extend(str(item) for item in value["warnings"] if item)
        return value


CONNECTED_FOR = 7 * 24 * 3600  # A confirmed connection is trusted this long; each upload re-proves it.
TOOLS_WAIT = 30                  # A fresh Codex session lists the Notion tools only after the server finishes starting.


class NotionBridge:
    def __init__(self, executable=None, cache_path=None):
        self.bridge = CodexStillsBridge(executable=executable, startup_timeout=45)
        self.login = None
        self.lock = threading.Lock()
        # Remembered across dialog openings and restarts, so the upload window does not start a Codex session each time.
        self.cache_path = Path(cache_path) if cache_path else None
        self.known = None

    def _cached(self):
        if self.known is None and self.cache_path:
            try:
                self.known = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.known = {}
        known = self.known or {}
        if known.get("status") == "connected" and 0 <= time.time() - float(known.get("checked_at", 0)) < CONNECTED_FOR:
            return known
        return None

    def _remember(self, status):
        self.known = {"status": status, "checked_at": time.time()} if status == "connected" else {}
        if self.cache_path:
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.cache_path.with_name(self.cache_path.name + ".tmp")
                temporary.write_text(json.dumps(self.known), encoding="utf-8")
                temporary.replace(self.cache_path)
            except OSError:
                pass

    def _ready_state(self, session, server):
        deadline = time.monotonic() + TOOLS_WAIT
        while True:
            state = self._state(session, server)
            if state.get("tools") or time.monotonic() >= deadline or state.get("authStatus") not in {"oAuth", None}:
                return state
            time.sleep(1)

    @contextmanager
    def connection(self, *, thread=True, timeout=600):
        sessions, guards = [], []
        with tempfile.TemporaryDirectory(prefix="pvr-notion-") as directory:
            try:
                base = {"features": {name: False for name in DISABLED_FEATURES}, "web_search": "disabled"}
                def start(overrides):
                    session = self.bridge._restricted_session(Path(directory), threading.Event(), lambda _: None,
                        None, overrides, guards, timeout)
                    sessions.append(session)
                    CodexBridge._initialize(session)
                    return session
                probe = start(base)
                config = probe.request("config/read", {"cwd": directory, "includeLayers": False}).get("config") or {}
                servers = config.get("mcp_servers") or {}
                choices = [name for name, value in servers.items() if
                    urlsplit(value.get("url", "")).hostname == "mcp.notion.com" and value.get("enabled", True)]
                name = ("notion" if "notion" in choices else choices[0] if choices else "workbench_notion")
                overrides = {**base, "mcp_servers": {key: {"enabled": key == name} for key in servers}}
                if name not in servers:
                    overrides["mcp_servers"][name] = {"enabled": True, "url": "https://mcp.notion.com/mcp"}
                probe.close()
                session = start(overrides)
                if thread:
                    reply = session.request("thread/start", {"cwd": directory, "ephemeral": True,
                        "sandbox": "read-only", "approvalPolicy": "never", "config": overrides})
                    session.thread_id = (reply.get("thread") or {}).get("id")
                    if not session.thread_id:
                        raise VoiceError(502, "无法准备 Notion 连接，请更新本机 Codex 后重试。")
                yield session, name
            except BridgeError:
                raise VoiceError(503, "本机 Notion 连接暂不可用，请检查 Codex 登录或重新连接 Notion。") from None
            finally:
                for session in reversed(sessions):
                    session.close()
                for guard in reversed(guards):
                    guard.close()

    @staticmethod
    def _state(session, server):
        params = {"limit": 100}
        if session.thread_id:
            params["threadId"] = session.thread_id
        reply = session.request("mcpServerStatus/list", params, startup=False)
        return next((item for item in reply.get("data", []) if item.get("name") == server), {})

    @contextmanager
    def client(self):
        with ExitStack() as stack:
            # A Codex session that fails to start is retried once before the upload is reported as failed.
            for attempt in (0, 1):
                try:
                    session, server = stack.enter_context(self.connection())
                    break
                except VoiceError:
                    if attempt:
                        raise
            state = self._ready_state(session, server)
            if not state.get("tools"):
                if state.get("authStatus") != "oAuth":
                    self._remember("disconnected")
                    raise VoiceError(409, "请先连接 Notion，并授权访问「TK排期计划」及其发布日历。")
                raise VoiceError(503, "Notion 连接已授权，但这次没能加载完成，请稍后重试。")
            self._remember("connected")
            yield _Client(session, server, state["tools"])

    def status(self, refresh=False):
        with self.lock:
            if self.login and self.login.get("status") in {"waiting", "preparing"}:
                return dict(self.login)
            known = None if refresh else self._cached()
        if known:
            return {"status": "connected", "cached": True}
        try:
            with self.connection(thread=False, timeout=60) as (session, server):
                state = self._state(session, server)
                connected = bool(state.get("tools")) or state.get("authStatus") == "oAuth"
                with self.lock:
                    self._remember("connected" if connected else "disconnected")
                return {"status": "connected" if connected else "disconnected"}
        except VoiceError as exc:
            return {"status": "unavailable", "error": str(exc)}

    def connect(self):
        with self.lock:
            if self.login and self.login.get("status") in {"waiting", "preparing"}:
                return dict(self.login)
            ready = threading.Event()
            self.login = {"status": "preparing"}
            def run():
                try:
                    with self.connection(thread=False, timeout=330) as (session, server):
                        complete = {}
                        original = session.notification
                        def notification(method, params):
                            if method == "mcpServer/oauthLogin/completed" and params.get("name") == server:
                                complete.update(params)
                            original(method, params)
                        session.notification = notification
                        result = session.request("mcpServer/oauth/login", {"name": server, "timeoutSecs": 300})
                        url = result.get("authorizationUrl", "")
                        parsed = urlsplit(url)
                        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                            raise VoiceError(502, "Notion 授权地址无效。")
                        with self.lock:
                            self.login = {"status": "waiting", "authorization_url": url}
                        ready.set()
                        while not complete:
                            session.pump()
                        with self.lock:
                            self.login = {"status": "connected" if complete.get("success") else "disconnected"}
                            self._remember(self.login["status"])
                except Exception:
                    with self.lock:
                        self.login = {"status": "disconnected", "error": "Notion 授权未完成，请重新连接。"}
                finally:
                    ready.set()
            threading.Thread(target=run, daemon=True, name="notion-login").start()
        if not ready.wait(45):
            raise VoiceError(503, "Notion 连接准备超时，请稍后重试。")
        with self.lock:
            return dict(self.login)
