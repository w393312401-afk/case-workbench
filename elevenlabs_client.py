"""Minimal ElevenLabs calls for workbench voiceovers: voice lookup, quota, speech with timing, alignment.

The key is read from the user environment variable ELEVENLABS_API_KEY on every call, so a corrected
key works without restarting. It is only sent in the request header, never logged or returned.
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import ssl
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
    {"id": "eleven_v4", "name": "Eleven v4", "settings": ["stability", "similarity_boost"], "max_chars": 10000, "language_code": True},
    {"id": "eleven_v4_turbo", "name": "Eleven v4 Turbo", "settings": ["stability", "similarity_boost"], "max_chars": 10000, "language_code": True},
    # ElevenLabs documents language_code as unsupported here; this model detects the language from the text.
    {"id": "eleven_multilingual_v2", "name": "Eleven Multilingual v2",
     "settings": ["stability", "similarity_boost", "style", "speed"], "max_chars": 10000, "language_code": False},
]
DEFAULT_MODEL = "eleven_v4"
SETTING_RANGES = {"stability": (0.0, 1.0, 0.5), "similarity_boost": (0.0, 1.0, 0.75),
                  "style": (0.0, 1.0, 0.0), "speed": (0.7, 1.2, 1.0)}


class VoiceError(ValueError):
    def __init__(self, status, message, *, retry_safe=False, provider_status=None, provider_code=""):
        super().__init__(message)
        self.status = status
        self.retry_safe = retry_safe
        self.provider_status = provider_status
        self.provider_code = provider_code


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
    # Saved from 设置 → 密钥与令牌, or set by hand as a user environment variable.
    import api_keys
    return api_keys.read(KEY_NAME)


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
        self.custom_transport = transport is not None
        self.transport = transport or self._urllib
        self.timeout = timeout

    def _preflight(self, seconds=15):
        """Open a TLS connection only (no request sent), so a dead network is known to be safe to retry."""
        host = urllib.parse.urlparse(API).hostname
        try:
            with socket.create_connection((host, 443), timeout=seconds) as raw:
                with ssl.create_default_context().wrap_socket(raw, server_hostname=host):
                    return
        except (OSError, TimeoutError) as exc:
            raise VoiceError(502, "连不上 ElevenLabs（网络或代理没有放行 api.elevenlabs.io），本次没有发出请求，可直接重试。", retry_safe=True) from exc

    def key(self):
        value = self.key_provider()
        if not value:
            raise VoiceError(503, f"没有找到 ElevenLabs 密钥。请在「设置 → 密钥与令牌」填入以 sk_ 开头的密钥（保存为用户环境变量 {KEY_NAME}）。", retry_safe=True)
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

    def call(self, method, path, *, query=None, payload=None, multipart=None, preflight=False, timeout=None):
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
        if preflight and not self.custom_transport:
            self._preflight()
        try:
            status, response_headers, raw = self.transport(method, url, headers, data, timeout or self.timeout)
        except (OSError, TimeoutError) as exc:
            raise VoiceError(502, f"ElevenLabs 请求发出后连接中断或超时（{type(exc).__name__}）。") from exc
        if status >= 400:
            code, message = _message(raw)
            refusal = {"retry_safe": 400 <= status < 500, "provider_status": status, "provider_code": code}
            if code == "missing_permissions":
                raise VoiceError(403, "ElevenLabs 密钥缺少本次操作所需的权限，请在官网密钥设置中检查音色读取、音色写入等权限。", **refusal)
            if status == 401 or "api_key" in code or "invalid_api_key" in message:
                raise VoiceError(503, "ElevenLabs 拒绝了密钥，请确认环境变量里是以 sk_ 开头、仍然有效的完整密钥。", **refusal)
            if code in {"maximum_voice_limit_reached", "voice_limit_reached"}:
                raise VoiceError(409, "ElevenLabs 音色名额已满，请先在官网整理已保存的音色，再重新添加。", **refusal)
            if code == "voice_not_found":
                raise VoiceError(404, "当前 ElevenLabs 账号无法访问这个音色；它可能尚未加入账号、已下架或不在此账号的授权范围内。", **refusal)
            if status == 404 or code == "not_found":
                raise VoiceError(404, "ElevenLabs 没有找到请求的资源，请确认该功能或音色仍然可用。", **refusal)
            if "quota" in code or "quota" in message.lower() or status == 402:
                raise VoiceError(409, "ElevenLabs 额度不足，本次没有生成。", **refusal)
            if status == 403:
                raise VoiceError(403, "ElevenLabs 拒绝访问此音色或操作，请检查密钥权限、账号套餐及音色访问范围。", **refusal)
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
        try:
            body, _ = self.call("GET", f"/v1/voices/{voice_id}")
        except VoiceError as exc:
            if not self._missing_voice(exc):
                raise
            # A library voice may exist publicly without belonging to this API account yet.
            # Resolve it by exact ID and add it before allowing any paid speech request.
            shared = self._shared_voice(voice_id)
            if not shared:
                raise VoiceError(404, "当前 ElevenLabs 账号和公开音色库均未查到这个 ID。音色可能已下架、未公开分享，或属于另一个账号；请确认官网与密钥使用同一账号。",
                                 retry_safe=True, provider_status=exc.provider_status, provider_code=exc.provider_code) from exc
            if shared.get("is_added_by_user") is True:
                raise VoiceError(409, "这个音色已在 ElevenLabs 账号中，但当前密钥无法读取。请检查音色读取权限及密钥所属账号。", retry_safe=True) from exc
            owner = shared.get("public_owner_id")
            if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9]{1,128}", owner):
                raise VoiceError(502, "ElevenLabs 音色库已找到这个音色，但未返回有效的分享者 ID，暂时无法加入账号。") from exc
            added, _ = self.call("POST", f"/v1/voices/add/{owner}/{voice_id}",
                                 payload={"new_name": str(shared.get("name") or voice_id)[:200]})
            try:
                body, _ = self.call("GET", f"/v1/voices/{voice_id}")
            except VoiceError as after_add:
                if not self._missing_voice(after_add):
                    raise
                added_id = added.get("voice_id")
                if isinstance(added_id, str) and VOICE_ID.fullmatch(added_id) and added_id != voice_id:
                    # Do not silently change request fingerprints, frozen takes or voice ownership.
                    self.call("GET", f"/v1/voices/{added_id}")
                    raise VoiceError(409, f"音色已加入 ElevenLabs，账号中的音色 ID 为 {added_id}，请改用此 ID 重新添加；尚未生成配音。", retry_safe=True) from after_add
                raise VoiceError(409, "音色已提交加入 ElevenLabs，但当前账号暂时仍无法读取。请在官网确认已加入，并核对密钥所属账号后重试；尚未生成配音。", retry_safe=True) from after_add
        labels = body.get("labels") if isinstance(body.get("labels"), dict) else {}
        return {"voice_id": voice_id, "name": str(body.get("name") or "")[:200], "category": str(body.get("category") or "")[:50],
                "labels": {str(key)[:50]: str(value)[:100] for key, value in list(labels.items())[:12]},
                "preview_url": str(body.get("preview_url") or "")[:1000]}

    @staticmethod
    def _missing_voice(error):
        return error.provider_code in {"voice_not_found", "not_found"} or error.provider_status == 404

    def _shared_voice(self, voice_id):
        for page in range(10):
            body, _ = self.call("GET", "/v1/shared-voices", query={"search": voice_id, "page_size": 100, "page": page,
                               "include_custom_rates": "true", "include_live_moderated": "true"})
            voices = body.get("voices")
            if not isinstance(voices, list):
                raise VoiceError(502, "ElevenLabs 公开音色库返回的列表无法读取，请稍后重试。")
            found = next((voice for voice in voices if isinstance(voice, dict) and voice.get("voice_id") == voice_id), None)
            if found:
                return found
            if not body.get("has_more"):
                return None
        raise VoiceError(502, "ElevenLabs 音色搜索返回过多结果，尚未核对完毕，请稍后重试。")

    def subscription(self):
        body, _ = self.call("GET", "/v1/user/subscription")
        return {key: body.get(key) for key in ("tier", "character_count", "character_limit", "next_character_count_reset_unix", "status")}

    def speech(self, voice_id, text, model_id, settings, language_code=""):
        payload = {"text": text, "model_id": model_id, "voice_settings": settings}
        if language_code and model(model_id)["language_code"]:
            payload["language_code"] = language_code
        body, headers = self.call("POST", f"/v1/text-to-speech/{voice_id}/with-timestamps", query={"output_format": OUTPUT_FORMAT},
                                  payload=payload, preflight=True, timeout=max(self.timeout, 300))
        try:
            audio = base64.b64decode(body["audio_base64"], validate=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise VoiceError(502, "ElevenLabs 没有返回音频。") from exc
        if not audio:
            raise VoiceError(502, "ElevenLabs 返回的音频是空的。")
        return {"audio": audio, "alignment": body.get("alignment"), "normalized_alignment": body.get("normalized_alignment"),
                "request_id": str(headers.get("request-id") or headers.get("x-request-id") or "")[:200]}

    def transcribe(self, audio, filename, models=("scribe_v2", "scribe_v1")):
        """Speech to text with word timings; tries the newest Scribe model first, then the older one."""
        last = None
        for model_id in models:
            try:
                body, _ = self.call("POST", "/v1/speech-to-text", multipart={
                    "file": (filename, audio, "audio/mpeg"), "model_id": model_id,
                    "timestamps_granularity": "word", "tag_audio_events": "false"})
                body["model_id"] = model_id
                return body
            except VoiceError as exc:
                last = exc
                if exc.provider_status not in {400, 422}:
                    raise
        raise last

    def align(self, audio, filename, text):
        body, _ = self.call("POST", "/v1/forced-alignment", multipart={"file": (filename, audio, "audio/mpeg"), "text": text})
        return body
