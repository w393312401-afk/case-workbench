"""Codex editing sessions with the existing isolated Resolve MCP transport.

The current personal skill owns editing and saving the target timeline. This
bridge grants only that job's file directory and the safe
Resolve scripting tools, while keeping native Codex model selection intact.
"""
from __future__ import annotations

from codex_bridge import BridgeError
from codex_stills_bridge import CodexStillsBridge, STILLS_TIMEOUT, _JobFiles, _file_specs

EDIT_TIMEOUT = 150 * 60
PLANNER_TIMEOUT = 30 * 60
PLANNER_FILE_TOOLS = {"Read", "Glob", "Grep"}


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


class CodexEditPlannerBridge(CodexEditBridge):
    """Independent read-only planning sessions; no session receives Resolve tools.

    The caller supplies the candidate snapshot and task in ``prompt`` and stores
    the returned text. Concurrent calls own separate App Server processes, file
    relays, cancellation scopes and model contexts.
    """
    TASK_NAME = "剪辑选材分析"
    READ_ONLY_ANALYSIS = True
    TASK_RULES = (
        "\n只根据本次提供的交接内容、候选资料和任务目录内已有静帧进行剪辑选材分析。"
        "只使用 Read、Glob、Grep 和 view_image；所有文件均只读。"
        "view_image 仅查看本次任务目录内已有图片，不导出或生成静帧。"
        "不连接或操作达芬奇，不修改时间线、文件或项目，不保存分析结果文件。"
        "不联网、不生图、不执行系统命令、不操作界面，不创建其他会话或任务。"
        "直接返回分析文本，由工作台汇总后交给唯一的剪辑执行者。"
    )

    def __init__(self, executable=None, *, timeout=PLANNER_TIMEOUT, startup_timeout=45, popen_factory=None):
        super().__init__(executable, timeout=timeout, startup_timeout=startup_timeout, popen_factory=popen_factory)

    @staticmethod
    def _profile(profile):
        if profile is None:
            profile = {}
        if not isinstance(profile, dict):
            raise BridgeError("剪辑选材分析配置无效。")
        if profile.get("mcp") not in (None, {}) or profile.get("dirs"):
            raise BridgeError("剪辑选材分析不能连接达芬奇或增加可写目录。")
        allowed, denied = profile.get("allowed", sorted(PLANNER_FILE_TOOLS)), profile.get("denied", [])
        if (not isinstance(allowed, list) or not isinstance(denied, list)
                or any(not isinstance(name, str) for name in allowed + denied)
                or not set(allowed) <= PLANNER_FILE_TOOLS):
            raise BridgeError("剪辑选材分析仅允许读取和检索本次任务文件。")
        rules = profile.get("rules", "")
        if not isinstance(rules, str) or len(rules) > 16000:
            raise BridgeError("剪辑选材分析规则无效。")
        timeout = profile.get("timeout", PLANNER_TIMEOUT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= PLANNER_TIMEOUT:
            raise BridgeError("剪辑选材分析时限无效。")
        return {"files": set(allowed) - set(denied), "rules": rules, "timeout": timeout}

    def _file_tools(self, settings):
        specs = _file_specs(settings["files"])
        for spec in specs:
            spec["description"] = "只读取或检索本次剪辑任务目录内已有资料和图片；不写文件、不连接达芬奇、不执行命令或联网。"
        return specs
