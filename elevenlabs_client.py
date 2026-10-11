"""Minimal ElevenLabs calls for workbench voiceovers: voice lookup, quota, speech with timing, alignment.

The key is read from the user environment variable ELEVENLABS_API_KEY on every call, so a corrected
key works without restarting. It is only sent in the request header, never logged or returned.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

API = "https://api.elevenlabs.io"
KEY_NAME = "ELEVENLABS_API_KEY"
VOICE_ID = re.compile(r"[A-Za-z0-9]{10,64}")
OUTPUT_FORMAT = "mp3_44100_128"
# Settings each model accepts. Eleven v4 exposes only stability and similarity (no style or speed).
MODELS = [
    {"id": "eleven_v4", "name": "Eleven v4", "settings": ["stability", "similarity_boost"], "max_chars": 10000},
    {"id": "eleven_v4_turbo", "name": "Eleven v4 Turbo", "settings": ["stability", "similarity_boost"], "max_chars": 10000},
    {"id": "eleven_multilingual_v2", "name": "Eleven Multilingual v2",
     "settings": ["stability", "similarity_boost", "style", "speed"], "max_chars": 10000},
]
DEFAULT_MODEL = "eleven_v4"
SETTING_RANGES = {"stability": (0.0, 1.0, 0.5), "similarity_boost": (0.0, 1.0, 0.75),
                  "style": (0.0, 1.0, 0.0), "speed": (0.7, 1.2, 1.0)}


class VoiceError(ValueError):
    def __init__(self, status, message, *, retry_safe=False, provider_status=None):
        super().__init__(message)
        self.status = status
        self.retry_safe = retry_safe
        self.provider_status = provider_status


def model(identifier):
    found = next((item for item in MODELS if item["id"] == identifier), None)
    if not found:
        raise VoiceError(400, "配音模型无效，请重新选择。")
    return found


def clean_settings(model_id, settings):
    """Keep only the settings this model accepts, inside their documented ranges."""
    allowed, result = model(model_id)["settings"], {}
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise VoiceError(400, "配音参数格式无效。")
    for key in allowed:
        low, high, default = SETTING_RANGES[key]
        value = settings.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise VoiceError(400, f"配音参数 {key} 须在 {low}–{high} 之间。")
        result[key] = round(float(value), 3)
    return result


def read_key():
    value = None
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
                value = winreg.QueryValueEx(handle, KEY_NAME)[0]
        except OSError:
            value = None
    return str(value or os.environ.get(KEY_NAME) or "").strip()


def _message(raw):
    try:
        detail = json.loads(raw).get("detail")
    except (ValueError, AttributeError):
        return "", ""
    if isinstance(detail, dict):
        return str(detail.get("status") or detail.get("code") or ""), str(detail.get("message") or "")
    if isinstance(detail, list) and detail:
        first = detail[0] if isinstance(detail[0], dict) else {}
        return "validation", str(first.get("msg") or "")
    return "", str(detail or "")


class ElevenLabsClient:
    def __init__(self, *, key_provider=read_key, transport=None, timeout=180):
        self.key_provider = key_provider
        self.transport = transport or self._urllib
        self.timeout = timeout

    def key(self):
        value = self.key_provider()
        if not value:
            raise VoiceError(503, f"没有找到 ElevenLabs 密钥。请把以 sk_ 开头的密钥设为用户环境变量 {KEY_NAME}。", retry_safe=True)
        if not value.startswith("sk_"):
            raise VoiceError(503, f"环境变量 {KEY_NAME} 里的值不是以 sk_ 开头的密钥，可能填成了密钥 ID。请在 ElevenLabs 后台新建或轮换密钥，复制完整密钥后重新设置。", retry_safe=True)
        return value

    def _urllib(self, method, url, headers, data, timeout):
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers or {}), exc.read()

    def call(self, method, path, *, query=None, payload=None, multipart=None):
        headers = {"xi-api-key": self.key(), "Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif multipart is not None:
            boundary = "----workbench" + uuid4().hex
            parts = []
            for name, value in multipart.items():
                if isinstance(value, tuple):
                    filename, content, content_type = value
                    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                                 f"Content-Type: {content_type}\r\n\r\n".encode() + content + b"\r\n")
                else:
                    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                                 + str(value).encode("utf-8") + b"\r\n")
            data = b"".join(parts) + f"--{boundary}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        try:
            status, response_headers, raw = self.transport(method, url, headers, data, self.timeout)
        except (OSError, TimeoutError) as exc:
            raise VoiceError(502, "连接 ElevenLabs 失败，请检查网络后重试。") from exc
        if status >= 400:
            code, message = _message(raw)
            refusal = {"retry_safe": 400 <= status < 500, "provider_status": status}
            if status in {401, 403} or "api_key" in code or "invalid_api_key" in message:
                raise VoiceError(503, "ElevenLabs 拒绝了密钥，请确认环境变量里是以 sk_ 开头、仍然有效的完整密钥。", **refusal)
            if status == 404 or code in {"voice_not_found", "not_found"}:
                raise VoiceError(404, "ElevenLabs 找不到这个音色 ID，请核对后再试。", **refusal)
            if "quota" in code or "quota" in message.lower() or status == 402:
                raise VoiceError(409, "ElevenLabs 额度不足，本次没有生成。", **refusal)
            if status == 429:
                raise VoiceError(429, "ElevenLabs 当前请求过多，请稍后重试。", **refusal)
            raise VoiceError(502, f"ElevenLabs 返回错误（{status}）{('：' + message[:300]) if message else ''}", **refusal)
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise VoiceError(502, "ElevenLabs 返回的内容无法读取。") from exc
        lowered = {str(key).lower(): value for key, value in (response_headers or {}).items()}
        return body, lowered

    def voice(self, voice_id):
        if not isinstance(voice_id, str) or not VOICE_ID.fullmatch(voice_id):
            raise VoiceError(400, "音色 ID 格式无效。")
        body, _ = self.call("GET", f"/v1/voices/{voice_id}")
        labels = body.get("labels") if isinstance(body.get("labels"), dict) else {}
        return {"voice_id": voice_id, "name": str(body.get("name") or "")[:200], "category": str(body.get("category") or "")[:50],
                "labels": {str(key)[:50]: str(value)[:100] for key, value in list(labels.items())[:12]},
                "preview_url": str(body.get("preview_url") or "")[:1000]}

    def subscription(self):
        body, _ = self.call("GET", "/v1/user/subscription")
        return {key: body.get(key) for key in ("tier", "character_count", "character_limit", "next_character_count_reset_unix", "status")}

    def speech(self, voice_id, text, model_id, settings):
        body, headers = self.call("POST", f"/v1/text-to-speech/{voice_id}/with-timestamps", query={"output_format": OUTPUT_FORMAT},
                                  payload={"text": text, "model_id": model_id, "voice_settings": settings})
        try:
            audio = base64.b64decode(body["audio_base64"], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise VoiceError(502, "ElevenLabs 没有返回音频。") from exc
        if not audio:
            raise VoiceError(502, "ElevenLabs 返回的音频是空的。")
        return {"audio": audio, "alignment": body.get("alignment"), "normalized_alignment": body.get("normalized_alignment"),
                "request_id": str(headers.get("request-id") or headers.get("x-request-id") or "")[:200]}

    def align(self, audio, filename, text):
        body, _ = self.call("POST", "/v1/forced-alignment", multipart={"file": (filename, audio, "audio/mpeg"), "text": text})
        return body
