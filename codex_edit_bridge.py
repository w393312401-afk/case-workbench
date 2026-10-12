"""Codex editing sessions with the existing isolated Resolve MCP transport.

The current personal skill owns editing and saving the target timeline. This
bridge grants only that job's file directory and the safe
Resolve scripting tools, while keeping native Codex model selection intact.
"""
from __future__ import annotations

from codex_bridge import BridgeError
from codex_stills_bridge import CodexStillsBridge, STILLS_TIMEOUT, _JobFiles, _file_specs

EDIT_TIMEOUT = 150 * 60


class CodexEditBridge(CodexStillsBridge):
    TASK_NAME = "剪辑"
    SERVER_NAME = "pvr_edit_resolve"
    TASK_RULES = (
        "\n只使用本次指定的达芬奇 MCP、文件工具和 view_image。按交接内容在已打开项目的目标时间线上剪辑并保存工程，"
        "保护配音轨和意群标记，只做本次授权的修改；不切换或新建项目，不导出成片、不排渲染。"
        "文件读写仅限本次任务目录，交接内容与技能参考只读。"
        "静帧只导出到任务目录的 stills/，view_image 只查看该目录内的图片。"
        "Glob/Grep 仅检索任务目录。"
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
        if profile.get("dirs"):
            raise BridgeError("剪辑文件权限仅限本次任务目录，不能增加源区间账本或其他目录。")
        settings = CodexStillsBridge._profile({**profile, "dirs": [], "timeout": min(timeout, STILLS_TIMEOUT)})
        return {**settings, "timeout": timeout}

    def _file_tools(self, settings):
        specs = _file_specs(settings["files"])
        for spec in specs:
            spec["description"] = "只处理本次剪辑目录内的文件；交接内容与技能参考只读，不执行命令或联网。"
        return specs

    def _file_handler(self, session, directory, settings):
        relay = _JobFiles(session, directory, settings["files"])

        def handler(request):
            params = request.get("params") or {}
            arguments = params.get("arguments")
            if (request.get("method") == "item/tool/call" and params.get("tool") in {"Glob", "Grep"}
                    and isinstance(arguments, dict) and "path" in arguments):
                try:
                    valid = isinstance(arguments["path"], str) and (directory / arguments["path"]).resolve() == directory
                except (OSError, ValueError):
                    valid = False
                if not valid:
                    session.send({"id": request["id"], "result": {"success": False, "contentItems": [
                        {"type": "inputText", "text": "文件检索仅限本次任务目录。"}]}})
                    return
            return relay(request)

        return handler

    def run(self, prompt, *, cwd, on_progress, cancel_event, model=None, reasoning_effort=None, skills=None, on_event=None, profile=None):
        try:
            return super().run(prompt, cwd=cwd, on_progress=on_progress, cancel_event=cancel_event, model=model,
                               reasoning_effort=reasoning_effort, skills=skills, on_event=on_event, profile=profile)
        except BridgeError as exc:
            # Keep the inherited cancellation/unavailability exception type,
            # while describing the editing task rather than a still search.
            raise type(exc)(str(exc).replace("找底图", "剪辑").replace("本次分析", "本次剪辑")) from None
