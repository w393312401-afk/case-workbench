"""Run resolve_worker.py one operation at a time; DaVinci Resolve is a single shared application."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading

from elevenlabs_client import VoiceError

WORKER = Path(__file__).resolve().parent / "resolve_worker.py"
MCP_FILES = (r"D:\DaVinci Resolve\ResolveMCP.exe", r"C:\Program Files\Blackmagic Design\DaVinci Resolve\ResolveMCP.exe")


def locate_mcp():
    """DaVinci Resolve's own MCP server, used by the editing task."""
    found = next((path for path in (os.environ.get("RESOLVE_MCP"), *MCP_FILES) if path and Path(path).is_file()), None)
    if not found:
        raise VoiceError(503, "没有找到达芬奇自带的 MCP 服务（ResolveMCP.exe），无法发起 AI 剪辑。")
    return found


class ResolveBridge:
    def __init__(self, *, runner=subprocess.run, python=sys.executable, worker=WORKER):
        self.runner, self.python, self.worker = runner, python, worker
        self.lock = threading.Lock()

    def busy(self):
        return self.lock.locked()

    def run(self, command, payload=None, timeout=180, held=False):
        """`held` means the caller already holds the lock, as a running editing task does for its whole run."""
        if not held and not self.lock.acquire(blocking=False):
            raise VoiceError(409, "达芬奇正在执行另一项工作台操作（可能是 AI 剪辑），请完成后再试。")
        try:
            completed = self.runner([self.python, "-X", "utf8", "-I", str(self.worker)],
                                    input=json.dumps({**(payload or {}), "command": command}, ensure_ascii=False).encode("utf-8"),
                                    capture_output=True, timeout=timeout,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired as exc:
            raise VoiceError(504, "达芬奇长时间没有响应，本次操作已停止。请到达芬奇里核对时间线是否已经建好，再决定是否重试。") from exc
        except OSError as exc:
            raise VoiceError(503, "无法启动达芬奇操作进程。") from exc
        finally:
            if not held:
                self.lock.release()
        try:
            result = json.loads(completed.stdout.decode("utf-8"))
        except (ValueError, UnicodeError, AttributeError) as exc:
            raise VoiceError(502, "达芬奇操作没有返回可读结果，请到达芬奇里核对。") from exc
        if not result.get("ok"):
            raise VoiceError(409, result.get("error") or "达芬奇操作没有完成。")
        return result
