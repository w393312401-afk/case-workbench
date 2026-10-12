"""Direct image edits through the user's local Cockpit gateway.

Credentials stay in Cockpit's configuration. A request is sent once; cancelling
or losing the response never retries a potentially completed generation.
"""
from __future__ import annotations

import base64
import http.client
import io
import json
import os
from pathlib import Path
import queue
import socket
import threading
import time
import urllib.parse

from PIL import Image

from codex_bridge import BridgeCancelled, BridgeError

MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_RESPONSE_BYTES = 48 * 1024 * 1024
FORMATS = {"PNG": ("image/png", ".png"), "JPEG": ("image/jpeg", ".jpg"), "WEBP": ("image/webp", ".webp")}


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


class CockpitImageBridge:
    direct_image_edit = True
    generation_source = "cockpit_image_edit"
    source = "cockpit_image_edit"

    def __init__(self, *, parameters_file=None, auth_config=None, key_provider=None, transport=None):
        self.parameters_file = Path(parameters_file or Path.home() / "Desktop" / "Cockpit_Codex_已验证请求参数.json")
        self.auth_config = auth_config
        self.key_provider = key_provider or (lambda: read_cockpit_key(self.auth_config))
        self.transport = transport

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
                    raise BridgeError("图生图等待超时，本次没有自动重复请求，请查看结果后重试。")
                try:
                    ok, result = results.get(timeout=.1)
                    if not ok:
                        raise BridgeError("Cockpit 图生图连接中断，本次没有自动重复请求。") from None
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
            raise BridgeError("Cockpit 未返回可保存的图片，本次没有自动重复请求。")
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
            raise BridgeError("Cockpit 返回的图片无法读取或过大，请查看任务结果。") from None

    def run(self, prompt, *, cwd, images, cancel_event, on_progress, on_event=None,
            timeout=1800, schema=None, read_only=False):
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
        if snapshot.get("aspect") == "9:16" and "1152x2048" in sizes:
            body["size"] = "1152x2048"
        body.update(prompt=prompt, images=[self._reference(path, directory) for path in images], n=1, stream=False)
        if body.get("output_format") == "png":
            body.pop("output_compression", None)
        def progress(message):
            try:
                on_progress(message)
            except Exception:
                pass

        progress("正在通过 Cockpit 生成图片。")
        status, _, raw = self._request(url, {"Authorization": "Bearer " + key, "Content-Type": "application/json",
                                           "Accept": "application/json"},
                                       json.dumps(body, ensure_ascii=False).encode("utf-8"), timeout, cancel_event)
        if cancel_event.is_set():
            raise BridgeCancelled("本次图生图已停止。")
        if status in {401, 403}:
            raise BridgeError("Cockpit 拒绝了本机访问密钥，请检查渠道授权。")
        if status == 429:
            raise BridgeError("Cockpit 图生图额度或请求频率受限，请稍后重试。")
        if status < 200 or status >= 300:
            raise BridgeError(f"Cockpit 图生图请求未完成（HTTP {status}），请查看渠道状态。")
        if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
            raise BridgeError("Cockpit 图片响应过大或不完整。")
        try:
            response = json.loads(raw)
            image_value = response["data"][0]
            if not isinstance(image_value, dict):
                raise ValueError()
        except (ValueError, KeyError, IndexError, TypeError):
            raise BridgeError("Cockpit 未返回完整图片，本次没有自动重复请求。") from None
        image_bytes, suffix = self._image_bytes(image_value)
        target = directory / ("render" + suffix)
        temporary = target.with_suffix(suffix + ".tmp")
        if cancel_event.is_set():
            raise BridgeCancelled("本次图生图已停止。")
        temporary.write_bytes(image_bytes)
        temporary.replace(target)
        progress("图片已保存。")
        return json.dumps({"file": target.name, "summary": ""}, ensure_ascii=False)
