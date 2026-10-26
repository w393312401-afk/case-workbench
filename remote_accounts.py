"""Local-only setup for the public workbench's password login.

Only this module writes credentials. Public responses contain metadata, never the
password, derived key, salt, or credential identifier.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import unicodedata
from uuid import uuid4


SCRYPT = {"n": 32768, "r": 8, "p": 3}
HOSTNAME = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")


class RemoteAccountError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _private_file(path):
    """Restrict an empty temporary file before any credential bytes are written."""
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = wintypes.BOOL
    apply = advapi.SetFileSecurityW
    apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    apply.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    # Protected DACL: file owner and SYSTEM only; never inherit broad folder access.
    if not convert("D:P(A;;FA;;;OW)(A;;FA;;;SY)", 1, ctypes.byref(descriptor), None):
        raise OSError("private_file_permissions_unavailable")
    try:
        if not apply(str(path), 0x80000004, descriptor):
            raise OSError("private_file_permissions_unavailable")
    finally:
        kernel.LocalFree(descriptor)


def atomic_private_bytes(path, content):
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            _private_file(temporary)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _encode(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def valid_account(value):
    return (isinstance(value, dict) and type(value.get("version")) is int and value["version"] == 1
            and isinstance(value.get("username"), str) and 3 <= len(value["username"]) <= 64
            and value["username"] == value["username"].strip()
            and not any(unicodedata.category(char).startswith("C") for char in value["username"])
            and value.get("scrypt") == SCRYPT
            and isinstance(value.get("updated_at"), str) and 1 <= len(value["updated_at"]) <= 64
            and all(isinstance(value.get(key), str) and re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value[key])
                    for key, length in (("salt", 64), ("password_hash", 128), ("credential_id", 32))))


class RemoteAccounts:
    def __init__(self, runtime):
        self.directory = Path(runtime) / "remote-access"
        self.config = self.directory / "config.json"
        self.account = self.directory / "account.json"

    def _config(self):
        try:
            value = json.loads(self.config.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict) or not isinstance(value.get("hostname"), str) or not HOSTNAME.fullmatch(value["hostname"]):
                raise ValueError()
            return value
        except FileNotFoundError:
            raise RemoteAccountError("尚未配置公网连接，请先完成公网隧道配置。", 409) from None
        except (OSError, ValueError, TypeError):
            raise RemoteAccountError("公网连接配置暂时无法读取，请检查后重试。", 409) from None

    def metadata(self):
        try:
            config = self._config()
        except RemoteAccountError as error:
            return {"configured": False, "username": "", "auth_mode": None, "public_url": None,
                    "remote_configured": False, "guest_enabled": False, "public_ready": False, "error": str(error)}
        account = None
        try:
            candidate = json.loads(self.account.read_text(encoding="utf-8"))
            if valid_account(candidate):
                account = candidate
        except (OSError, ValueError, TypeError):
            pass
        mode = config.get("authMode", "cloudflare")
        public_ready = False
        try:
            deployment = json.loads((self.directory / "deployment.json").read_text(encoding="utf-8-sig"))
            public_ready = isinstance(deployment, dict) and deployment.get("auth_method") == "password_with_guest"
        except (OSError, ValueError, TypeError):
            pass
        return {"configured": account is not None, "username": account["username"] if account else "",
                "auth_mode": mode if mode in ("password", "cloudflare") else None, "public_url": "https://" + config["hostname"],
                "remote_configured": True, "guest_enabled": config.get("guestEnabled") is True, "public_ready": public_ready}

    def save(self, body):
        # Check a real existing connection before deriving or creating credentials.
        config = self._config()
        username, password, confirmation = (body.get(key) for key in ("username", "password", "confirm_password"))
        if not isinstance(username, str):
            raise RemoteAccountError("账号名称须为 3–64 个字符。")
        username = username.strip()
        if not 3 <= len(username) <= 64 or any(unicodedata.category(char).startswith("C") for char in username):
            raise RemoteAccountError("账号名称须为 3–64 个字符，且不能包含控制字符。")
        if not isinstance(password, str) or len(password) < 12:
            raise RemoteAccountError("密码至少需要 12 个字符。")
        try:
            encoded_password = password.encode("utf-8")
        except UnicodeError:
            raise RemoteAccountError("密码包含无效字符。") from None
        if len(encoded_password) > 256:
            raise RemoteAccountError("密码不能超过 256 个 UTF-8 字节。")
        if not isinstance(confirmation, str) or password != confirmation:
            raise RemoteAccountError("两次输入的密码不一致。")
        salt = secrets.token_bytes(32)
        try:
            derived = hashlib.scrypt(encoded_password, salt=salt, **SCRYPT, dklen=64, maxmem=64 * 1024 * 1024)
        except (ValueError, MemoryError):
            raise RemoteAccountError("密码保护暂时不可用，请稍后重试。", 503) from None
        account = {"version": 1, "username": username, "salt": salt.hex(), "password_hash": derived.hex(),
                   "scrypt": dict(SCRYPT), "credential_id": secrets.token_hex(16),
                   "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        reload_path = self.directory / "reload.request"
        request = None
        try:
            status = json.loads((self.directory / "status.json").read_text(encoding="utf-8"))
            instance = status.get("instance_id")
            if isinstance(instance, str) and re.fullmatch(r"[0-9a-f]{32}", instance):
                request = {"instance_id": instance, "request_id": uuid4().hex}
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        writes = [(self.account, _encode(account)), (self.config, _encode({**config, "authMode": "password"}))]
        if request:
            writes.append((reload_path, _encode(request)))
        originals, written = {}, []
        try:
            for path, _ in writes:
                originals[path] = path.read_bytes() if path.exists() else None
            for path, content in writes:
                atomic_private_bytes(path, content)
                written.append(path)
        except OSError:
            # Restore config before removing/reverting credentials. If a rollback
            # cannot complete, keep a valid account for whichever mode is present.
            rollback_failed = False
            for path in reversed(written):
                if rollback_failed and path == self.account:
                    continue
                try:
                    if originals[path] is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_private_bytes(path, originals[path])
                except OSError:
                    rollback_failed = True
            raise RemoteAccountError("账号设置未能完整保存，请检查本机文件权限后重试。", 500) from None
        metadata = self.metadata()
        notice = ("公网账号已更新，已登录的设备需要重新登录；访客浏览不受影响。" if metadata["public_ready"] else
                  "本机账号密码已就绪；Cloudflare 公网入口仍需切换后才能直接使用账号密码登录。修改账号密码后，旧登录会话将失效。")
        return {**metadata, "saved": True, "reload_requested": request is not None, "notice": notice}
