"""Ephemeral Codex still searches with a verified, job-specific MCP allow list.

The normal analysis bridge is unchanged. This bridge uses its App Server session
protocol, but gives the search its own writable job directory and file tools.
"""
from __future__ import annotations

import base64
import copy
import fnmatch
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from codex_bridge import BridgeCancelled, BridgeError, BridgeUnavailable, CodexBridge

STILLS_TIMEOUT = 90 * 60
RESOLVE_TOOLS = {"run_script", "search_scripting_api", "get_scripting_api", "get_scripting_docs", "get_resolve_status", "get_whats_new"}
FILE_TOOLS = {"Read", "Write", "Edit", "Glob", "Grep"}
DISABLED_FEATURES = ("apps", "plugins", "browser_use", "browser_use_external", "computer_use", "hooks", "image_generation",
                     "multi_agent", "multi_agent_v2", "code_mode", "shell_tool", "unified_exec", "workspace_dependencies")
# Current Codex models may route tools through the stable host even with the
# optional code_mode feature off. Keep that dispatcher alive for the allowlist.
ENABLED_FEATURES = ("code_mode_host", "view_image")
MAX_FILE_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_SKILL_BYTES = 512 * 1024


def _toml(value):
    """Encode CLI config values as actual TOML, without involving a shell."""
    if type(value) is bool:
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_toml(part) for part in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key) + "=" + _toml(part) for key, part in value.items()) + "}"
    raise BridgeError("找底图的配置格式无效。")


class _OwnedProcessTree:
    """A Windows job owns only this CLI and the MCP children it starts."""
    def __init__(self, process):
        self.handle = None
        if os.name != "nt":
            return
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(int(process._handle))):
            if handle:
                kernel.CloseHandle(handle)
            raise BridgeUnavailable("无法建立找底图进程的独立关闭范围，本次未启动模型或连接达芬奇。")
        self.handle, self.kernel = handle, kernel

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _file_specs(names):
    specs = {
        "Read": {"file_path": {"type": "string"}, "offset": {"type": "integer", "minimum": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 4000}},
        "Write": {"file_path": {"type": "string"}, "content": {"type": "string"}},
        "Edit": {"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}},
        "Glob": {"pattern": {"type": "string"}},
        "Grep": {"pattern": {"type": "string"}, "glob": {"type": "string"}},
    }
    required = {"Read": ["file_path"], "Write": ["file_path", "content"], "Edit": ["file_path", "old_string", "new_string"], "Glob": ["pattern"], "Grep": ["pattern"]}
    return [{"type": "function", "name": name, "description": "只处理本次找底图目录内的资料和候选文件；不执行命令、联网或访问其他目录。",
             "inputSchema": {"type": "object", "properties": specs[name], "required": required[name], "additionalProperties": False}} for name in sorted(names)]


class _JobFiles:
    def __init__(self, session, root, names):
        self.session, self.root, self.names = session, root, names
        self.reject = session.reject_request

    def path(self, raw, *, write=False):
        if not isinstance(raw, str) or not raw or len(raw) > 1000 or "\x00" in raw:
            raise BridgeError("文件路径无效。")
        path = (self.root / raw).resolve()
        if not path.is_relative_to(self.root) or ".codex" in path.relative_to(self.root).parts:
            raise BridgeError("文件不在本次找底图目录内。")
        if write and (path.name in {"AGENTS.md", "input.json", "handoff.md"} or set(path.relative_to(self.root).parts) & {"refs", "skill", "candidates"}):
            raise BridgeError("交接和参考资料只能读取。")
        return path

    def files(self, pattern):
        if not isinstance(pattern, str) or not pattern or len(pattern) > 300 or Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise BridgeError("文件匹配范围无效。")
        found = []
        for path in self.root.rglob("*"):
            if len(found) >= 1000:
                break
            if path.is_file() and fnmatch.fnmatch(path.relative_to(self.root).as_posix(), pattern):
                try:
                    self.path(str(path))
                except BridgeError:
                    continue
                found.append(path)
        return sorted(found)

    def __call__(self, request):
        if request.get("method") != "item/tool/call":
            return self.reject(request)
        params = request.get("params") or {}
        if (params.get("threadId") != self.session.thread_id or params.get("turnId") != self.session.turn_id or params.get("tool") not in self.names or
                not isinstance(params.get("arguments"), dict)):
            self.session.send({"id": request["id"], "result": {"success": False, "contentItems": [{"type": "inputText", "text": "这项工具不属于本次找底图任务。"}]}})
            raise BridgeError("Codex 请求了找底图范围外的工具，本次已停止。")
        tool, values = params["tool"], params["arguments"]
        try:
            if tool == "Read":
                path = self.path(values.get("file_path"))
                if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                    if not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
                        raise BridgeError("图片过大或不存在，请使用原生 view_image 查看本目录的静帧。")
                    mime = "jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else path.suffix[1:].lower()
                    contents = [{"type": "inputImage", "imageUrl": "data:image/" + mime + ";base64," + base64.b64encode(path.read_bytes()).decode("ascii")}]
                else:
                    if path.stat().st_size > MAX_FILE_BYTES:
                        raise BridgeError("资料文件过大，请缩小读取范围。")
                    lines = path.read_text(encoding="utf-8-sig").splitlines()
                    offset, limit = values.get("offset", 1), values.get("limit", 2000)
                    if type(offset) is not int or offset < 1 or type(limit) is not int or not 1 <= limit <= 4000:
                        raise BridgeError("读取行数无效。")
                    contents = [{"type": "inputText", "text": "\n".join(lines[offset - 1:offset - 1 + limit])}]
            elif tool in {"Write", "Edit"}:
                path = self.path(values.get("file_path"), write=True)
                if tool == "Write":
                    value = values.get("content")
                else:
                    old, new = values.get("old_string"), values.get("new_string")
                    if not isinstance(old, str) or not old or not isinstance(new, str) or path.stat().st_size > MAX_FILE_BYTES:
                        raise BridgeError("替换内容无效。")
                    value = path.read_text(encoding="utf-8")
                    hits = value.count(old)
                    if not hits or (hits != 1 and values.get("replace_all") is not True):
                        raise BridgeError("待替换内容未找到或有多处，请缩小范围。")
                    value = value.replace(old, new, -1 if values.get("replace_all") is True else 1)
                if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_FILE_BYTES:
                    raise BridgeError("写入内容无效或过大。")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(value, encoding="utf-8")
                contents = [{"type": "inputText", "text": "已保存本次任务文件。"}]
            elif tool == "Glob":
                contents = [{"type": "inputText", "text": "\n".join(path.relative_to(self.root).as_posix() for path in self.files(values.get("pattern")))}]
            else:
                pattern = values.get("pattern")
                if not isinstance(pattern, str) or not pattern or len(pattern) > 200:
                    raise BridgeError("检索内容无效。")
                matches = []
                for path in self.files(values.get("glob", "*")):
                    if path.stat().st_size > MAX_FILE_BYTES:
                        continue
                    try:
                        for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
                            if pattern in line:
                                matches.append(f"{path.relative_to(self.root).as_posix()}:{number}:{line[:500]}")
                                if len(matches) >= 200:
                                    break
                    except UnicodeError:
                        continue
                    if len(matches) >= 200:
                        break
                contents = [{"type": "inputText", "text": "\n".join(matches)}]
            self.session.send({"id": request["id"], "result": {"success": True, "contentItems": contents}})
        except (BridgeError, OSError, ValueError, TypeError) as exc:
            message = str(exc) if isinstance(exc, BridgeError) else "本次任务文件无法读取或保存，请检查路径。"
            self.session.send({"id": request["id"], "result": {"success": False, "contentItems": [{"type": "inputText", "text": message}]}})


class CodexStillsBridge(CodexBridge):
    TASK_NAME = "找底图"
    SERVER_NAME = "pvr_stills_resolve"
    READ_ONLY_ANALYSIS = False
    TASK_RULES = "\n只使用本次指定的达芬奇 MCP 和任务目录文件工具。只读取既有项目；除切换查看时间线、播放头和导出本目录静帧外，不修改项目。实际查看静帧后再挑候选；没有合格画面就保存空候选清单并说明缺口。不联网、不生图、不操作界面，不恢复或创建其他任务。"

    def __init__(self, executable=None, *, timeout=STILLS_TIMEOUT, startup_timeout=45, popen_factory=None):
        super().__init__(executable, timeout=timeout, startup_timeout=startup_timeout, popen_factory=popen_factory)

    def _writable_roots(self, directory, settings):
        return [] if self.READ_ONLY_ANALYSIS else [directory]

    def _file_tools(self, settings):
        return _file_specs(settings["files"])

    def _file_handler(self, session, directory, settings):
        return _JobFiles(session, directory, settings["files"])

    @staticmethod
    def _profile(profile):
        if not isinstance(profile, dict) or not isinstance(profile.get("mcp"), dict) or set(profile["mcp"]) != {"resolve"}:
            raise BridgeError("找底图必须使用本次指定的达芬奇连接。")
        config = profile["mcp"]["resolve"]
        if not isinstance(config, dict) or set(config) - {"command", "args"}:
            raise BridgeError("达芬奇连接配置无效。")
        command, args = config.get("command"), config.get("args", [])
        if not isinstance(command, str) or not Path(command).is_absolute() or not Path(command).is_file() or not isinstance(args, list) or any(not isinstance(arg, str) or "\x00" in arg for arg in args):
            raise BridgeError("本次达芬奇连接程序不存在或参数无效。")
        allowed, denied = profile.get("allowed"), profile.get("denied", [])
        if not isinstance(allowed, list) or not isinstance(denied, list) or any(not isinstance(name, str) for name in allowed + denied):
            raise BridgeError("找底图工具白名单无效。")
        tools, files = set(), set()
        for name in allowed:
            if name in FILE_TOOLS:
                if name not in denied:
                    files.add(name)
            elif name.startswith("mcp__resolve__") and name[14:] in RESOLVE_TOOLS:
                if name not in denied:
                    tools.add(name[14:])
            else:
                raise BridgeError("找底图白名单包含无关或不安全的工具，未扩大权限。")
        if not tools or "run_script" not in tools or profile.get("dirs"):
            raise BridgeError("找底图不能增加额外可写目录，且须包含安全的达芬奇读取工具。")
        rules = profile.get("rules", "")
        if not isinstance(rules, str) or len(rules) > 16000:
            raise BridgeError("找底图规则无效。")
        timeout = profile.get("timeout", STILLS_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= STILLS_TIMEOUT:
            raise BridgeError("找底图时限无效。")
        return {"command": str(Path(command).resolve()), "args": args, "tools": sorted(tools), "files": files, "rules": rules, "timeout": timeout}

    def _restricted_session(self, directory, cancel, progress, on_event, overrides, guards, timeout):
        def spawn(arguments, **options):
            arguments = [*arguments, "--strict-config"]
            for key, value in overrides.items():
                arguments.extend(["-c", key + "=" + _toml(value)])
            process = self.popen_factory(arguments, **options)
            try:
                guards.append(_OwnedProcessTree(process))
            except Exception:
                process.terminate()
                process.wait(timeout=2)
                raise
            return process
        # Reuse the ordinary bridge's launch and protocol without mutating its factory.
        client = CodexBridge(self.executable, timeout=timeout, startup_timeout=self.startup_timeout, popen_factory=spawn)
        return client._session(directory, cancel, progress, on_event)

    @staticmethod
    def _skill_text(selected):
        """Codex loads only skills registered in its own skill roots, never the task-folder copy given as a skill
        input, so the copy's text goes into the developer instructions, as the Claude bridge does in its system prompt."""
        texts = []
        for skill in selected:
            path = Path(skill["path"])
            if path.stat().st_size > MAX_SKILL_BYTES:
                raise BridgeError("技能文件过大，未截断加载。")
            try:
                texts.append(f'\n\n<skill name="{skill["name"]}">\n{path.read_text(encoding="utf-8-sig")}\n</skill>')
            except (OSError, UnicodeError):
                raise BridgeError("技能文件当前无法读取，本次未启动。") from None
        return "".join(texts)

    def run(self, prompt, *, cwd, on_progress, cancel_event, model=None, reasoning_effort=None, skills=None, on_event=None, profile=None):
        if not isinstance(prompt, str) or not prompt.strip():
            raise BridgeError("请填写找底图的制作要求。")
        if cancel_event.is_set():
            raise BridgeCancelled("找底图已停止。")
        directory = Path(cwd)
        if not directory.is_absolute() or not directory.is_dir():
            raise BridgeError("本次找底图目录不可用。")
        directory = directory.resolve()
        settings = self._profile(profile)
        writable_roots = [str(path) for path in self._writable_roots(directory, settings)]
        deadline = time.monotonic() + min(self.timeout, settings["timeout"])
        for value in (model, reasoning_effort):
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 200 or any(char in value for char in "\r\n\x00")):
                raise BridgeError("模型或思考强度无效。")
        selected = self._skills(skills)
        if any(not Path(skill["path"]).is_relative_to(directory) for skill in selected):
            raise BridgeError("本次找底图只使用任务目录内的技能副本。")
        skill_text = self._skill_text(selected)
        sandbox_mode = "read-only" if self.READ_ONLY_ANALYSIS else "workspace-write"
        sandbox_policy = ({"type": "readOnly", "networkAccess": False} if self.READ_ONLY_ANALYSIS else
                          {"type": "workspaceWrite", "writableRoots": writable_roots, "networkAccess": False, "excludeSlashTmp": True, "excludeTmpdirEnvVar": True})
        base = {"features": {**{name: False for name in DISABLED_FEATURES}, **{name: True for name in ENABLED_FEATURES}}, "web_search": "disabled",
                "sandbox_mode": sandbox_mode, "approval_policy": "never",
                "sandbox_workspace_write": {"writable_roots": writable_roots, "network_access": False, "exclude_slash_tmp": True, "exclude_tmpdir_env_var": True}}
        sessions, guards = [], []
        def progress(value):
            on_progress(str(value).replace("分析", self.TASK_NAME))
        try:
            # An empty mcp_servers table MERGES with user config. Discover names without a thread/turn, then disable each one explicitly.
            probe = self._restricted_session(directory, cancel_event, progress, None, base, guards, min(deadline - time.monotonic(), 60))
            sessions.append(probe)
            self._initialize(probe)
            config = probe.request("config/read", {"cwd": str(directory), "includeLayers": False}).get("config")
            if not isinstance(config, dict) or not isinstance(config.get("mcp_servers", {}), dict):
                raise BridgeError("无法核实本机工具配置，未启动找底图。")
            server_name = None if self.READ_ONLY_ANALYSIS else self.SERVER_NAME
            if server_name is not None and server_name in config.get("mcp_servers", {}):
                server_name += "_" + uuid4().hex[:12]
            servers = {name: {"enabled": False} for name in config.get("mcp_servers", {})}
            if server_name is not None:
                servers[server_name] = {"command": settings["command"], "args": settings["args"], "cwd": str(directory), "enabled": True,
                    "enabled_tools": settings["tools"], "default_tools_approval_mode": "approve", "startup_timeout_sec": self.startup_timeout, "tool_timeout_sec": settings["timeout"]}
            probe.close()
            overrides = {**base, "mcp_servers": servers}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeError("找底图准备超时，本次未启动模型。")
            session = self._restricted_session(directory, cancel_event, progress, on_event, overrides, guards, remaining)
            sessions.append(session)
            # Dynamic job-only file tools are an experimental App Server capability.
            session.request("initialize", {"clientInfo": {"name": "product_video_case_workbench", "title": "视频案例工作台", "version": "1.0"}, "capabilities": {"experimentalApi": True}})
            session.send({"method": "initialized"})
            effective = session.request("config/read", {"cwd": str(directory), "includeLayers": False}).get("config") or {}
            effective_servers = effective.get("mcp_servers", {})
            if (not isinstance(effective_servers, dict) or any(value.get("enabled", True) is not False for name, value in effective_servers.items() if name != server_name) or
                    (server_name is not None and any((effective_servers.get(server_name) or {}).get(key) != value for key, value in servers[server_name].items()))):
                raise BridgeError("本次工具隔离配置未生效，未启动找底图。")
            if any((effective.get("features") or {}).get(name) is not False for name in DISABLED_FEATURES) or effective.get("web_search") != "disabled":
                raise BridgeError("本次工具隔离配置未生效，未启动找底图。")
            if any((effective.get("features") or {}).get(name) is not True for name in ENABLED_FEATURES):
                raise BridgeError("Codex 选图所需的工具运行或看图功能未启用，未启动找底图。")
            if (effective.get("sandbox_mode") != sandbox_mode or effective.get("approval_policy") != "never"
                    or effective.get("sandbox_workspace_write") != base["sandbox_workspace_write"]):
                raise BridgeError("本次文件范围或权限配置未生效，未启动找底图。")
            thread_config = copy.deepcopy(overrides)
            if reasoning_effort is not None:
                thread_config["model_reasoning_effort"] = reasoning_effort
            params = {"cwd": str(directory), "ephemeral": True, "sandbox": sandbox_mode, "approvalPolicy": "never", "config": thread_config,
                      "dynamicTools": self._file_tools(settings),
                      "developerInstructions": settings["rules"] + self.TASK_RULES + skill_text}
            if model is not None:
                params["model"] = model
            result = session.request("thread/start", params)
            session.thread_id = (result.get("thread") or {}).get("id")
            if not isinstance(session.thread_id, str) or not session.thread_id:
                raise BridgeError("Codex 未返回本次找底图会话编号。")
            actual_model, actual_effort = result.get("model"), result.get("reasoningEffort")
            session.session_info = {"type": "session", "model": actual_model if isinstance(actual_model, str) and actual_model else None,
                                    "reasoning_effort": actual_effort if isinstance(actual_effort, str) and actual_effort else None, "skills": selected}
            session.emit(session.session_info)
            session.reject_request = self._file_handler(session, directory, settings)
            turn = {"threadId": session.thread_id, "cwd": str(directory), "approvalPolicy": "never",
                    "sandboxPolicy": sandbox_policy,
                    "input": [{"type": "text", "text": prompt}, *({"type": "skill", **skill} for skill in selected)]}
            if actual_model is not None or model is not None:
                turn["model"] = session.session_info["model"] or model
            if reasoning_effort is not None:
                turn["effort"] = session.session_info["reasoning_effort"] or reasoning_effort
            started = session.request("turn/start", turn, startup=False).get("turn") or {}
            if session.turn_id is None:
                session.turn_id = started.get("id")
            elif session.turn_id != started.get("id"):
                raise BridgeError("Codex 返回的找底图任务编号不一致。")
            if not isinstance(session.turn_id, str) or not session.turn_id:
                raise BridgeError("Codex 未返回本次找底图任务编号。")
            while session.completed is None:
                session.pump()
            session._check(session.deadline)
            return session.final_text()
        finally:
            for session in reversed(sessions):
                session.close()
            for guard in reversed(guards):
                guard.close()
