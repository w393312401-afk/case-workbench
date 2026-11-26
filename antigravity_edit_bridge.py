"""Run the existing restricted Claude Code tools against a separate API service."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from antigravity_bridge import AntigravityBridge, MODEL_PREFIX, select_model
from claude_bridge import ClaudeBridge, client_environment, locate_claude
from codex_bridge import BridgeCancelled, BridgeError


class AntigravityEditBridge(ClaudeBridge):
    supports_subscription_auth = False

    def __init__(self, runtime_root=None, *, api_bridge=None, **options):
        super().__init__(runtime_root, **options)
        self.api_bridge = api_bridge if api_bridge is not None else AntigravityBridge()

    @staticmethod
    def _client_defaults():
        return {}

    def status(self, refresh=False):
        state = self.api_bridge.status(refresh=refresh)
        executable = locate_claude(self.executable)
        available = bool(state.get("available")) and bool(executable) and not self._closing.is_set()
        result = {**state, "provider": "antigravity", "connection": "claude_cli_api",
                  "cli_installed": bool(executable), "available": available,
                  "usage_source": "antigravity_api", "supports_tools": True,
                  "label": "Antigravity API（本机 Claude Code 执行器）已配置" if available else "Antigravity API 剪辑尚未就绪"}
        if not executable:
            result["error"] = "未找到本机 Claude Code 执行器，无法通过 Antigravity API 剪辑。"
        elif self._closing.is_set():
            result["error"] = "Antigravity API 剪辑执行器正在关闭。"
        return result

    def models(self):
        catalog = self.api_bridge.models()
        state = self.status()
        return {**catalog, "available": state["available"], "error": state.get("error"),
                "models": [{**row, "available": bool(row.get("available")) and state["available"],
                            "supports_tools": True} for row in catalog.get("models", [])]}

    def refresh_models(self):
        self.api_bridge.refresh_models()

    def _run_configuration(self, model, reasoning_effort):
        settings, key = self.api_bridge._configuration()
        raw_model = select_model(settings, model, reasoning_effort)
        parsed = urlsplit(settings["base_url"])
        path = parsed.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        base_url = urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")
        environment = client_environment()
        for name in ("CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_SUBAGENT_MODEL", "MAX_THINKING_TOKENS",
                     "CLAUDE_CODE_API_BASE_URL", "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
                     "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST"):
            environment.pop(name, None)
        if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
            bypass = [entry.strip() for name in ("NO_PROXY", "no_proxy")
                      for entry in environment.get(name, "").split(",") if entry.strip()]
            value = ",".join(dict.fromkeys([*bypass, "localhost", "127.0.0.1", "::1"]))
            environment.update({"NO_PROXY": value, "no_proxy": value})
        environment.update({"ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_AUTH_TOKEN": key,
                            "ANTHROPIC_MODEL": raw_model,
                            "ANTHROPIC_SMALL_FAST_MODEL": raw_model,
                            "ANTHROPIC_DEFAULT_OPUS_MODEL": raw_model,
                            "ANTHROPIC_DEFAULT_SONNET_MODEL": raw_model,
                            "ANTHROPIC_DEFAULT_HAIKU_MODEL": raw_model,
                            "CLAUDE_CODE_DISABLE_LEGACY_MODEL_REMAP": "1"})
        return {"model": raw_model, "cli_model": raw_model,
                "effective": {"model": raw_model, **({"reasoning_effort": reasoning_effort} if reasoning_effort else {})},
                "child_env": environment,
                "actual_model": raw_model, "actual_effort": reasoning_effort, "env_effort": None,
                "isolate_client_config": True,
                "start_message": "正在连接 Antigravity API，通过本机 Claude Code 执行。"}

    @staticmethod
    def _safe_error(message):
        if "Antigravity" in message:
            return message
        if "授权" in message or "订阅" in message or "登录" in message:
            return "Antigravity API 授权暂不可用，请检查本地 API 服务和密钥后重试。"
        if "无法启动本机 Claude Code" in message:
            return "无法启动本机 Claude Code 执行器，Antigravity API 剪辑尚未开始。"
        if "本机 Claude Code" in message:
            return message.replace("本机 Claude Code", "Antigravity API 的本机 Claude Code 执行器")
        return message.replace("Claude 客户端", "Antigravity API").replace("Claude", "Antigravity API")

    def run(self, prompt, *, on_progress, on_event=None, **options):
        def progress(message):
            text = str(message)
            on_progress(text.replace("Claude", "Antigravity API") if text.startswith("Claude") else text)

        def emit(event):
            if on_event is None:
                return
            if event.get("type") in {"session", "usage"}:
                event = {**event, "provider": "antigravity"}
                model = event.get("model")
                if isinstance(model, str) and not model.startswith(MODEL_PREFIX):
                    event["model"] = MODEL_PREFIX + model
            on_event(event)

        try:
            return super().run(prompt, on_progress=progress, on_event=emit, **options)
        except BridgeCancelled:
            raise
        except BridgeError as error:
            raise type(error)(self._safe_error(str(error))) from None
