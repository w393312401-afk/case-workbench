"""Codex editing sessions with the existing isolated Resolve MCP transport.

The workbench owns the target timeline and validates it after the run. This
bridge grants only that job's file directory, its source ledger and the safe
Resolve scripting tools, while keeping native Codex model selection intact.
"""
from __future__ import annotations

import copy
from pathlib import Path

from codex_bridge import BridgeError
from codex_stills_bridge import CodexStillsBridge, STILLS_TIMEOUT, _JobFiles, _file_specs

EDIT_TIMEOUT = 150 * 60


class _EditFiles:
    """Relay file tools to one verified root, never the common parent of roots."""

    def __init__(self, session, roots, names):
        self.session, self.roots, self.names = session, roots, names
        self.reject = session.reject_request

    def root_for(self, raw):
        if not isinstance(raw, str) or not raw or len(raw) > 1000 or "\x00" in raw:
            raise BridgeError("文件路径无效。")
        path = (self.roots[0] / raw).resolve()
        for root in self.roots:
            if path.is_relative_to(root):
                return root, path
        raise BridgeError("文件不在本次剪辑目录或源区间账本目录内。")

    def __call__(self, request):
        if request.get("method") != "item/tool/call":
            return self.reject(request)
        params = request.get("params") or {}
        if (params.get("threadId") != self.session.thread_id or params.get("turnId") != self.session.turn_id
                or params.get("tool") not in self.names or not isinstance(params.get("arguments"), dict)):
            self.session.send({"id": request["id"], "result": {"success": False, "contentItems": [
                {"type": "inputText", "text": "这项工具不属于本次剪辑任务。"}]}})
            raise BridgeError("Codex 请求了剪辑范围外的工具，本次已停止。")
        try:
            values = params["arguments"]
            if params["tool"] in {"Glob", "Grep"}:
                root, path = self.root_for(values.get("path", str(self.roots[0])))
                # Check against the original allowed root before narrowing a
                # search, so .codex and symlink escapes stay inaccessible.
                checker = _JobFiles(self.session, root, self.names)
                checker.path(str(path))
                if not path.is_dir():
                    raise BridgeError("检索目录不存在。")
                forwarded = copy.deepcopy(request)
                forwarded["params"]["arguments"].pop("path", None)
                relay = _JobFiles(self.session, path, self.names)
            else:
                root, path = self.root_for(values.get("file_path"))
                forwarded = copy.deepcopy(request)
                forwarded["params"]["arguments"]["file_path"] = str(path)
                relay = _JobFiles(self.session, root, self.names)
            relay.reject = self.reject
            return relay(forwarded)
        except (BridgeError, OSError, ValueError, TypeError):
            self.session.send({"id": request["id"], "result": {"success": False, "contentItems": [
                {"type": "inputText", "text": "文件不在本次可用目录内，或目录与路径无效。"}]}})


class CodexEditBridge(CodexStillsBridge):
    TASK_NAME = "剪辑"
    SERVER_NAME = "pvr_edit_resolve"
    TASK_RULES = (
        "\n只使用本次指定的达芬奇 MCP、文件工具和 view_image。按交接内容在已打开项目的目标时间线上剪辑并保存工程，"
        "保护配音轨和意群标记，只做本次授权的修改；不切换或新建项目，不导出成片、不排渲染。"
        "文件读写仅限本次任务目录和指定的源区间账本目录，交接内容与技能参考只读。"
        "静帧只导出到任务目录的 stills/，view_image 只查看该目录内的图片。"
        "Glob/Grep 默认检索任务目录；检索账本时用 path 指定账本目录。"
        "不联网、不生图、不执行系统命令、不操作界面，不恢复或创建其他任务。"
    )

    def __init__(self, executable=None, *, timeout=EDIT_TIMEOUT, startup_timeout=45, popen_factory=None):
        super().__init__(executable, timeout=timeout, startup_timeout=startup_timeout, popen_factory=popen_factory)

    @staticmethod
    def _profile(profile):
        if not isinstance(profile, dict):
            raise BridgeError("剪辑任务配置无效。")
        timeout = profile.get("timeout", EDIT_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= EDIT_TIMEOUT:
            raise BridgeError("剪辑时限无效。")
        dirs = profile.get("dirs")
        if not isinstance(dirs, list) or len(dirs) != 1 or not isinstance(dirs[0], str) or "\x00" in dirs[0]:
            raise BridgeError("剪辑必须指定一个源区间账本目录。")
        ledger = Path(dirs[0])
        if not ledger.is_absolute() or not ledger.is_dir():
            raise BridgeError("本次源区间账本目录不存在。")
        ledger = ledger.resolve()
        if ledger == Path(ledger.anchor):
            raise BridgeError("源区间账本目录不能覆盖整个磁盘。")
        settings = CodexStillsBridge._profile({**profile, "dirs": [], "timeout": min(timeout, STILLS_TIMEOUT)})
        return {**settings, "timeout": timeout, "ledger": ledger}

    def _writable_roots(self, directory, settings):
        ledger = settings["ledger"]
        if directory.is_relative_to(ledger):
            raise BridgeError("源区间账本目录不能包含本次任务目录。")
        return [directory, ledger] if not ledger.is_relative_to(directory) else [directory]

    def _file_tools(self, settings):
        specs = _file_specs(settings["files"])
        for spec in specs:
            spec["description"] = "只处理本次剪辑目录和指定源区间账本目录内的文件；交接内容与技能参考只读，不执行命令或联网。"
            if spec["name"] in {"Glob", "Grep"}:
                spec["inputSchema"]["properties"]["path"] = {
                    "type": "string", "description": "检索目录，默认当前任务目录；可指定交接内容中的账本目录。"}
        return specs

    def _file_handler(self, session, directory, settings):
        return _EditFiles(session, self._writable_roots(directory, settings), settings["files"])

    def run(self, prompt, *, cwd, on_progress, cancel_event, model=None, reasoning_effort=None, skills=None, on_event=None, profile=None):
        try:
            return super().run(prompt, cwd=cwd, on_progress=on_progress, cancel_event=cancel_event, model=model,
                               reasoning_effort=reasoning_effort, skills=skills, on_event=on_event, profile=profile)
        except BridgeError as exc:
            # Keep the inherited cancellation/unavailability exception type,
            # while describing the editing task rather than a still search.
            raise type(exc)(str(exc).replace("找底图", "剪辑").replace("本次分析", "本次剪辑")) from None
