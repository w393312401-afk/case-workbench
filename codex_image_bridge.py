"""Isolated Codex CLI image jobs, using the user's client configuration and login.

The caller prepares reference images in a dedicated job directory and tells the
agent where to copy generated images. This bridge never scans generated_images
or resumes an existing session; only its own final-answer file is read back.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import signal
import subprocess
import threading
import time
from uuid import uuid4

from codex_bridge import BridgeCancelled, BridgeError, BridgeUnavailable, _friendly_error, locate_codex

_WINDOWS = os.name == "nt"
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_ANSWER_BYTES = 4 * 1024 * 1024
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


def _image_error(error):
    return _friendly_error(error, "图片任务").replace("只读运行环境", "图片任务运行环境")


def _terminate_process_tree(process):
    """Own the standalone CLI's tree, including tools still running on cancel."""
    if process.poll() is not None:
        return
    try:
        if _WINDOWS:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if not _WINDOWS:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass


class CodexImageBridge:
    def __init__(self, executable=None, *, popen_factory=None):
        self.executable = executable
        self.popen_factory = popen_factory or subprocess.Popen

    def status(self):
        if locate_codex(self.executable):
            return {"available": True, "label": "可连接本机 Codex，使用内置生图", "model": None,
                    "generation_source": "codex_builtin", "review_available": True}
        return {"available": False, "label": "未找到本机 Codex",
                "error": "请先安装或启动 Codex，或配置 CODEX_EXECUTABLE。"}

    def run(self, prompt, *, cwd, images, cancel_event, on_progress, on_event=None,
            timeout=1800, schema=None, read_only=False, reasoning_effort=None):
        directory = Path(cwd).expanduser().resolve()
        if not directory.is_dir():
            raise BridgeError("图片任务目录不存在，请重新准备任务。")
        if not isinstance(prompt, str) or not prompt.strip():
            raise BridgeError("图片任务要求不能为空。")
        if reasoning_effort is not None and (not isinstance(reasoning_effort, str) or reasoning_effort not in REASONING_EFFORTS):
            raise BridgeError("图片任务推理强度无效。")
        if read_only:
            prompt = ("这是独立的只读图片理解任务。仅查看已提供的图片，按下文要求完成视觉分析、提示词策划或核对，并返回指定结构化结果；"
                      "不得调用图片生成工具，不得编辑或重新生成图片，不得写入或修改图片文件。"
                      "以下是本次任务要求：\n\n" + prompt)
        if not isinstance(images, (list, tuple)):
            raise BridgeError("图片参考文件列表无效。")
        references = []
        for value in images:
            if not isinstance(value, (str, os.PathLike)):
                raise BridgeError("图片参考文件无效。")
            path = Path(value).expanduser()
            path = (path if path.is_absolute() else directory / path).resolve()
            if not path.is_relative_to(directory) or not path.is_file():
                raise BridgeError("图片参考文件必须是本次任务目录里的快照。")
            references.append(str(path))
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise BridgeError("图片任务超时时间无效。")
        if schema is not None and not isinstance(schema, dict):
            raise BridgeError("图片任务输出格式无效。")
        cancel_event = cancel_event or threading.Event()
        deadline = time.monotonic() + timeout

        def check():
            if cancel_event.is_set():
                raise BridgeCancelled("本次图片任务已取消。")
            if time.monotonic() >= deadline:
                raise BridgeError("Codex 响应超时，本次图片任务已停止，请稍后重试。")

        def progress(message):
            try:
                on_progress(message)
            except Exception:
                pass  # A display failure must not strand the running CLI.

        def emit(event):
            if on_event is not None:
                on_event(copy.deepcopy(event))

        check()
        executable = locate_codex(self.executable)
        if not executable:
            raise BridgeUnavailable(self.status()["error"])
        try:
            private = directory / ".codex-image-runs" / uuid4().hex
            private.mkdir(parents=True)
            answer_path = (private / "final.txt").resolve()
            args = [executable, "-a", "never", "--no-daemon", "exec", "--json", "--ephemeral",
                    "-s", "read-only" if read_only else "workspace-write", "--skip-git-repo-check",
                    "-C", str(directory), "--output-last-message", str(answer_path)]
            if reasoning_effort is not None:
                # Per-call CLI configuration only; never change the selected model or the user's config file.
                args.extend(["-c", "model_reasoning_effort=" + json.dumps(reasoning_effort)])
            if schema is not None:
                schema_path = (private / "schema.json").resolve()
                schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
                args.extend(["--output-schema", str(schema_path)])
            for path in references:
                args.extend(["-i", path])
            args.append("-")
        except (OSError, TypeError, ValueError):
            raise BridgeError("没能准备图片任务文件，请检查任务目录后重试。") from None

        messages = queue.Queue(maxsize=512)
        closing = threading.Event()
        process, threads = None, []
        last_error, eof = None, False
        receipt = {"source": "codex_cli", "read_only": bool(read_only), "model": None,
                   "reasoning_effort_override": reasoning_effort,
                   "thread_id": None, "started_at": None, "finished_at": None, "pid": None,
                   "status": "prepared"}

        def enqueue(kind, value=None):
            while not closing.is_set():
                try:
                    messages.put((kind, value), timeout=.1)
                    return
                except queue.Full:
                    continue

        def read_stdout():
            try:
                while not closing.is_set():
                    line = process.stdout.readline(MAX_LINE_BYTES + 1)
                    if not line:
                        break
                    if len(line) > MAX_LINE_BYTES:
                        raise ValueError()
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError()
                    enqueue("event", event)
            except (OSError, ValueError, UnicodeError):
                enqueue("invalid")
            finally:
                enqueue("eof")

        def drain_stderr():
            try:
                while not closing.is_set() and process.stderr.read(64 * 1024):
                    pass  # Do not retain diagnostics containing paths or credentials.
            except (OSError, ValueError):
                pass

        def write_prompt():
            try:
                pending = memoryview(prompt.encode("utf-8"))
                while pending and not closing.is_set():
                    written = process.stdin.write(pending)
                    if not written:
                        raise OSError()
                    pending = pending[written:]
                if not closing.is_set():
                    process.stdin.flush()
                    process.stdin.close()
            except (OSError, ValueError):
                enqueue("input_error")

        try:
            check()
            options = {"cwd": str(directory), "stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                       "stderr": subprocess.PIPE, "bufsize": 0, "shell": False}
            if _WINDOWS:
                options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            else:
                options["start_new_session"] = True
            progress("正在启动本机 Codex 图片任务。")
            try:
                process = self.popen_factory(args, **options)
            except OSError:
                raise BridgeUnavailable("无法启动本机 Codex，请打开客户端后重试。") from None
            receipt.update(started_at=datetime.now(timezone.utc).isoformat(), pid=process.pid, status="running")
            (private / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
            emit({"type": "process", "pid": process.pid, "status": "running", "started_at": receipt["started_at"]})
            threads = [threading.Thread(target=read_stdout, daemon=True),
                       threading.Thread(target=drain_stderr, daemon=True),
                       threading.Thread(target=write_prompt, daemon=True)]
            for thread in threads:
                thread.start()
            while True:
                check()
                if eof and process.poll() is not None:
                    break
                try:
                    kind, event = messages.get(timeout=.1)
                except queue.Empty:
                    continue
                if kind == "eof":
                    eof = True
                    continue
                if kind == "invalid":
                    raise BridgeError("本机 Codex 返回的数据不完整，请更新客户端后重试。")
                if kind == "input_error":
                    raise BridgeError("没能把图片任务要求交给本机 Codex，请重试。")
                event_type = event.get("type")
                if event_type == "thread.started":
                    receipt["thread_id"] = event.get("thread_id")
                    emit({"type": "session", "thread_id": event.get("thread_id"),
                          "model": None, "reasoning_effort": reasoning_effort, "skills": []})
                elif event_type == "turn.started":
                    progress("Codex 正在处理图片要求。")
                elif event_type == "turn.failed":
                    raise BridgeError(_image_error(event.get("error") or event))
                elif event_type == "error":
                    last_error = event.get("error") or event
                    progress(_image_error(last_error) + " Codex 尚未结束，正在等待最终结果。")
                elif event_type in {"item.started", "item.updated", "item.completed"}:
                    item = event.get("item") or {}
                    if not isinstance(item, dict):
                        raise BridgeError("本机 Codex 返回的数据不完整，请更新客户端后重试。")
                    if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                        emit({"type": "message", "item_id": item.get("id"),
                              "phase": item.get("phase") if item.get("phase") in {"commentary", "final_answer"} else "unknown",
                              "text": item["text"], "complete": event_type == "item.completed"})
                    elif item.get("type") in {"image_generation", "image_generation_call"}:
                        if read_only:
                            raise BridgeError("只读核对任务尝试再次生图，本次核对已停止；成图保留。")
                        progress("正在生成图片。")
                    elif read_only and ("imagegen" in str(item.get("tool", "")).lower() or
                                        "image_gen" in str(item.get("tool", "")).lower()):
                        raise BridgeError("只读核对任务尝试再次生图，本次核对已停止；成图保留。")
                    elif item.get("type") in {"command_execution", "file_change"}:
                        progress("正在读取并核对原图与成图。" if read_only else "正在整理本次图片文件。")
            check()
            if process.poll() != 0:
                raise BridgeError(_image_error(last_error))
            try:
                if not answer_path.resolve().is_relative_to(private.resolve()):
                    raise OSError()
                with answer_path.open("rb") as stream:
                    answer = stream.read(MAX_ANSWER_BYTES + 1)
                if len(answer) > MAX_ANSWER_BYTES:
                    raise ValueError()
                text = answer.decode("utf-8-sig").strip()
            except (OSError, ValueError, UnicodeError):
                raise BridgeError("Codex 已结束，但没能读到完整的最终回答，请重试。") from None
            if not text:
                raise BridgeError("Codex 已结束，但没有返回最终回答，请重试。")
            emit({"type": "message", "item_id": "codex-image-final", "phase": "final_answer",
                  "text": text, "complete": True})
            progress("Codex 图片任务已完成。")
            receipt["status"] = "completed"
            return text
        finally:
            closing.set()
            if process is not None:
                _terminate_process_tree(process)
                for stream in (process.stdin, process.stdout, process.stderr):
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
            for thread in threads:
                thread.join(timeout=.2)
            receipt.update(finished_at=datetime.now(timezone.utc).isoformat())
            if receipt["status"] != "completed":
                receipt["status"] = "cancelled" if cancel_event.is_set() else "failed"
            try:
                (private / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass
