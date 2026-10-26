"""Direct image edits through the user's local Cockpit gateway.

Credentials stay in Cockpit's configuration. A dropped connection, rate limit, gateway
error or unreadable image is retried a few times with a pause (the user asked for this;
a dropped request may still have been billed). Cancelling, timing out, a refused key or
a rejected request is never retried, and a late result is never saved after cancelling.
"""
from __future__ import annotations

import base64
import http.client
import io
import json
import os
from pathlib import Path
import queue
import re
import socket
import threading
import time
import urllib.parse

from PIL import Image

from codex_bridge import BridgeCancelled, BridgeError

MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_RESPONSE_BYTES = 48 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
FORMATS = {"PNG": ("image/png", ".png"), "JPEG": ("image/jpeg", ".jpg"), "WEBP": ("image/webp", ".webp")}
RETRY_DELAYS = (4, 12)  # pause before the 2nd and the 3rd attempt
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
# Match the carousel's five workers; the gateway has its own account-level limits.
MAX_PARALLEL = 5


class _Retryable(BridgeError):
    """A failure worth another attempt: the request may simply not have got through."""


def _known_channel_error(raw):
    """Recognize a bounded set of channel failures; never return upstream text."""
    if not isinstance(raw, bytes) or len(raw) > MAX_ERROR_BYTES:
        return None
    try:
        response = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(response, dict) or not isinstance(response.get("error"), dict):
        return None

    def messages(value, depth=0):
        if depth > 4:
            return
        if isinstance(value, dict):
            for field in ("message", "detail", "code", "type", "error"):
                yield from messages(value.get(field), depth + 1)
        elif isinstance(value, str):
            message = re.sub(r"^Error #\d{1,3}:\s*", "", value.strip())
            yield message
            try:
                nested = json.loads(message)
            except (ValueError, TypeError, RecursionError):
                return
            yield from messages(nested, depth + 1)

    for message in messages(response["error"]):
        if message == "auth_unavailable" or message.startswith("auth_unavailable:"):
            return "Cockpit 账号池不可用，请在 Cockpit 中检查账号池诊断。已停止自动重试。"
        if re.fullmatch(r"The '[A-Za-z0-9][A-Za-z0-9._-]{0,79}' model is not supported "
                        r"when using Codex with a ChatGPT account\.", message):
            return "Cockpit 图片编排模型不受当前 ChatGPT 账号支持，请在 Cockpit 中调整“生图主模型”。已停止自动重试。"
    return None


def read_cockpit_key(path=None):
    override = os.environ.get("COCKPIT_API_KEY", "").strip()
    if override:
        return override
    config = Path(path or Path.home() / ".cockpit_tools" / "codex_local_access.json")
    try:
        value = json.loads(config.read_text(encoding="utf-8-sig"))
        if value.get("enabled") is False:
            return ""
        keys = value.get("apiKeys")
        if isinstance(keys, list) and keys:
            return next((str(entry["key"]).strip() for entry in keys
                         if isinstance(entry, dict) and entry.get("enabled", True) and entry.get("key")), "")
        return str(value.get("apiKey") or "").strip()
    except (OSError, ValueError, TypeError, AttributeError):
        return ""


CUTOUT_SIZES = ("1024x1536", "1536x1024", "2048x2048", "1152x2048", "2048x1152")
CUTOUT_QUALITIES = ("low", "medium", "high")


class CockpitImageBridge:
    direct_image_edit = True
    # Transparent-background cutouts give the subject lock a fine mask; the caller aligns and verifies it.
    supports_cutout = True
    generation_source = "cockpit_image_edit"
    source = "cockpit_image_edit"

    def __init__(self, *, parameters_file=None, auth_config=None, key_provider=None, transport=None,
                 retry_delays=RETRY_DELAYS, max_parallel=MAX_PARALLEL):
        self.parameters_file = Path(parameters_file or Path.home() / "Desktop" / "Cockpit_Codex_已验证请求参数.json")
        self.auth_config = auth_config
        self.key_provider = key_provider or (lambda: read_cockpit_key(self.auth_config))
        self.transport = transport
        self.retry_delays = tuple(retry_delays)
        self._slots = threading.BoundedSemaphore(max_parallel)

    def _settings(self):
        try:
            document = json.loads(self.parameters_file.read_text(encoding="utf-8-sig"))
            channel = document["通道"]["Cockpit本机API"]
            url = str(channel["基址"]).rstrip("/")
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.path != "/v1" or not parsed.port):
                raise ValueError()
            edit = channel["参考图编辑"]
            if edit["method"] != "POST" or edit["path"] != "/images/edits":
                raise ValueError()
            template = document["可复用请求形式"]["本机参考图编辑JSON"]
            keys = ("model", "size", "quality", "background", "output_format", "output_compression", "moderation")
            defaults = {key: template[key] for key in keys if key in template}
            if not isinstance(defaults.get("model"), str) or defaults.get("output_format") not in {"png", "webp", "jpeg"}:
                raise ValueError()
            sizes = document.get("已实测请求参数", {}).get("size", {}).get("tested_values", [])
            return url + "/images/edits", defaults, sizes
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise BridgeError("Cockpit 图生图参数无法读取，请检查已验证请求参数文件。") from None

    def status(self):
        status = {"label": "Cockpit 图生图", "generation_source": self.generation_source,
                  "direct_image_edit": True, "review_available": False, "model": None}
        try:
            url, defaults, _ = self._settings()
            if not self.key_provider():
                raise BridgeError("请在 Cockpit 中开启本机 API 并启用访问密钥。")
            parsed = urllib.parse.urlsplit(url)
            with socket.create_connection((parsed.hostname, parsed.port), timeout=.3):
                pass
            return {**status, "available": True, "model": defaults["model"]}
        except BridgeError as error:
            return {**status, "available": False, "error": str(error)}
        except OSError:
            return {**status, "available": False, "error": "Cockpit 本机 API 未连接，请启动 Cockpit。"}

    @staticmethod
    def _reference(path, directory):
        path = Path(path).resolve()
        if not path.is_relative_to(directory) or not path.is_file() or path.stat().st_size > MAX_IMAGE_BYTES:
            raise BridgeError("图生图参考文件无效，请重新选择底图。")
        try:
            with Image.open(path) as image:
                image.load()
                if image.format not in FORMATS:
                    raise ValueError()
                # The verified JSON request uses PNG data URLs, including for JPEG bases.
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                raw = buffer.getvalue()
                if len(raw) > MAX_IMAGE_BYTES:
                    raise ValueError()
            return {"image_url": "data:image/png;base64," + base64.b64encode(raw).decode("ascii")}
        except (OSError, ValueError, Image.DecompressionBombError):
            raise BridgeError("图生图参考图片无法读取或过大，请重新选择底图。") from None

    @staticmethod
    def _mask(path, directory, primary):
        """Native Cockpit accepts PNG mask data URLs: alpha 0 edits, alpha 255 protects.

        This is guidance to the image model. The caller still owns deterministic
        restoration and verification of protected source pixels in the final image.
        """
        try:
            if not isinstance(path, (str, os.PathLike)):
                raise ValueError()
            path = Path(path)
            path = (path if path.is_absolute() else directory / path).resolve()
            if (not path.is_relative_to(directory) or not path.is_file() or
                    path.suffix.lower() != ".png" or path.stat().st_size > MAX_IMAGE_BYTES):
                raise ValueError()
            with Image.open(primary) as original:
                size = original.size
            with Image.open(path) as mask:
                mask.load()
                if mask.format != "PNG" or mask.mode != "RGBA" or mask.size != size:
                    raise ValueError()
                # Both editable and protected pixels must exist. Intermediate alpha
                # values may describe an outer transition, but do not lock those pixels.
                if mask.getchannel("A").getextrema() != (0, 255):
                    raise ValueError()
                buffer = io.BytesIO()
                mask.save(buffer, format="PNG")
                raw = buffer.getvalue()
                if len(raw) > MAX_IMAGE_BYTES:
                    raise ValueError()
            return {"image_url": "data:image/png;base64," + base64.b64encode(raw).decode("ascii")}
        except (OSError, TypeError, ValueError, Image.DecompressionBombError):
            raise BridgeError("编辑蒙版须是本次任务目录内、与第一张参考图等大的 RGBA PNG，并同时包含透明可改区和不透明保护区。") from None

    @staticmethod
    def _close(connection):
        try:
            sock = connection.sock
            if sock:
                sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()

    def _request(self, url, headers, payload, timeout, cancel_event):
        results = queue.Queue(maxsize=1)
        connections = []
        closing = threading.Event()
        deadline = time.monotonic() + timeout

        def work():
            try:
                if closing.is_set() or cancel_event.is_set() or time.monotonic() >= deadline:
                    raise BridgeCancelled("本次图生图已停止。")
                if self.transport:
                    result = self.transport("POST", url, headers, payload, timeout)
                else:
                    parsed = urllib.parse.urlsplit(url)
                    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
                    connections.append(connection)
                    try:
                        if closing.is_set() or cancel_event.is_set():
                            raise BridgeCancelled("本次图生图已停止。")
                        connection.request("POST", parsed.path, body=payload, headers=headers)
                        response = connection.getresponse()
                        raw = response.read(MAX_RESPONSE_BYTES + 1)
                        result = (response.status, dict(response.headers), raw)
                    finally:
                        self._close(connection)
                results.put((True, result))
            except Exception as error:
                results.put((False, error))

        thread = threading.Thread(target=work, name="cockpit-image-request", daemon=True)
        thread.start()
        try:
            while True:
                if cancel_event.is_set():
                    raise BridgeCancelled("本次图生图已停止。")
                if time.monotonic() >= deadline:
                    raise BridgeError("图生图等待超时，没有再自动重试，请查看结果后重试。")
                try:
                    ok, result = results.get(timeout=.1)
                    if not ok:
                        if isinstance(result, BridgeCancelled):
                            raise result
                        raise _Retryable("Cockpit 图生图连接中断。") from None
                    return result
                except queue.Empty:
                    continue
        finally:
            closing.set()
            for connection in connections:
                self._close(connection)

    @staticmethod
    def _image_bytes(value):
        if isinstance(value.get("b64_json"), str):
            encoded = value["b64_json"]
        elif isinstance(value.get("url"), str) and value["url"].startswith("data:image/") and "," in value["url"]:
            encoded = value["url"].split(",", 1)[1]
        else:
            raise _Retryable("Cockpit 未返回可保存的图片。")
        try:
            raw = base64.b64decode(encoded, validate=True)
            if not raw or len(raw) > MAX_IMAGE_BYTES:
                raise ValueError()
            with Image.open(io.BytesIO(raw)) as image:
                suffix = FORMATS[image.format][1]
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
            return raw, suffix
        except (ValueError, KeyError, OSError, Image.DecompressionBombError):
            raise _Retryable("Cockpit 返回的图片无法读取或过大。") from None

    def _attempt(self, url, headers, payload, deadline, cancel_event):
        """One request, holding a gateway slot; returns the image or says whether trying again could help."""
        while not self._slots.acquire(timeout=.2):
            if cancel_event.is_set():
                raise BridgeCancelled("本次图生图已停止。")
            if time.monotonic() >= deadline:
                raise BridgeError("图生图排队等待超时，请稍后再试。")
        try:
            status, _, raw = self._request(url, headers, payload, max(.01, deadline - time.monotonic()), cancel_event)
        finally:
            self._slots.release()
        if cancel_event.is_set():
            raise BridgeCancelled("本次图生图已停止。")
        if status in {401, 403}:
            raise BridgeError("Cockpit 拒绝了本机访问密钥，请检查渠道授权。")
        if status < 200 or status >= 300:
            channel_error = _known_channel_error(raw)
            if channel_error:
                raise BridgeError(channel_error)
        if status == 429:
            raise _Retryable("Cockpit 图生图额度或请求频率受限。")
        if status in RETRY_STATUSES:
            raise _Retryable(f"Cockpit 图生图暂时出错（HTTP {status}）。")
        if status < 200 or status >= 300:
            raise BridgeError(f"Cockpit 图生图请求未完成（HTTP {status}），请查看渠道状态。")
        if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
            raise _Retryable("Cockpit 图片响应过大或不完整。")
        try:
            response = json.loads(raw)
            image_value = response["data"][0]
            if not isinstance(image_value, dict):
                raise ValueError()
        except (ValueError, KeyError, IndexError, TypeError):
            raise _Retryable("Cockpit 未返回完整图片。") from None
        return self._image_bytes(image_value)

    def run(self, prompt, *, cwd, images, cancel_event, on_progress, on_event=None,
            timeout=1800, schema=None, read_only=False, mask=None, cutout=None):
        if read_only:
            raise BridgeError("当前图生图渠道仅返回图片，请直接查看并选用成图。")
        directory = Path(cwd).resolve()
        if not directory.is_dir() or not isinstance(prompt, str) or not prompt.strip() or not images:
            raise BridgeError("图生图要求或参考图片不完整，请重新准备任务。")
        if type(timeout) not in (int, float) or not 0 < timeout <= 3600:
            raise BridgeError("图生图等待时间无效。")
        cancel_event = cancel_event or threading.Event()
        if cancel_event.is_set():
            raise BridgeCancelled("本次图生图已停止。")
        url, body, sizes = self._settings()
        key = self.key_provider()
        if not key:
            raise BridgeError("请在 Cockpit 中开启本机 API 并启用访问密钥。")
        try:
            snapshot = json.loads((directory / "input.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            snapshot = {}
        if snapshot.get("aspect") == "3:4":
            # tested_values records earlier experiments, not a provider allowlist.
            # Match the prepared 3:4 canvas instead of the template's 2:3 size.
            body["size"] = "1536x2048"
        elif snapshot.get("aspect") == "9:16" and "1152x2048" in sizes:
            body["size"] = "1152x2048"
        body.update(prompt=prompt, images=[self._reference(path, directory) for path in images], n=1, stream=False)
        if mask is not None:
            body["mask"] = self._mask(mask, directory, images[0])
        if snapshot.get("workflow", (snapshot.get("requirements") or {}).get("workflow")) == "subject_locked":
            body["output_format"] = "png"
        if cutout is not None:
            # A cutout asks for a real alpha channel at a size close to the crop's own shape.
            if (not isinstance(cutout, dict) or cutout.get("size") not in CUTOUT_SIZES or
                    cutout.get("quality", "high") not in CUTOUT_QUALITIES or mask is not None):
                raise BridgeError("抠图参数无效。")
            body.update(background="transparent", output_format="png", size=cutout["size"], quality=cutout.get("quality", "high"))
        if body.get("output_format") == "png":
            body.pop("output_compression", None)
        def progress(message):
            try:
                on_progress(message)
            except Exception:
                pass

        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json"}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        deadline, attempts = time.monotonic() + timeout, len(self.retry_delays) + 1
        for attempt in range(1, attempts + 1):
            progress("正在通过 Cockpit 生成图片。" if attempt == 1 else f"正在通过 Cockpit 重新生成（第 {attempt}/{attempts} 次）。")
            try:
                image_bytes, suffix = self._attempt(url, headers, payload, deadline, cancel_event)
                break
            except _Retryable as error:
                if attempt == attempts:
                    raise BridgeError(f"{error}已自动重试 {attempts - 1} 次仍未成功，请稍后再试。") from None
                delay = self.retry_delays[attempt - 1]
                if time.monotonic() + delay >= deadline:
                    raise BridgeError(f"{error}剩余等待时间不够再试一次，请稍后再试。") from None
                progress(f"{error}{delay} 秒后自动重试（第 {attempt + 1}/{attempts} 次）。")
                if cancel_event.wait(delay):
                    raise BridgeCancelled("本次图生图已停止。") from None
        target = directory / ("render" + suffix)
        temporary = target.with_suffix(suffix + ".tmp")
        if cancel_event.is_set():
            raise BridgeCancelled("本次图生图已停止。")
        temporary.write_bytes(image_bytes)
        temporary.replace(target)
        progress("图片已保存。")
        return json.dumps({"file": target.name, "summary": ""}, ensure_ascii=False)
