"""Durable recovery for production operations which change an external system.

These files are business data, not a transient retry cache. Speech is never sent again once
an attempt may have reached the provider. Resolve builds have a durable identity and are
recovered by the worker using that identity. File locks cover the request and its job across
processes; a process restart releases locks but does not discard the recorded operation.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from collections import OrderedDict
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import threading

from direction_store import fingerprint, now
from elevenlabs_client import VoiceError
from file_identity import file_identity

REQUEST_ID = re.compile(r"[\w-]{10,100}")
JOB_ID = re.compile(r"PRD-[0-9a-f]{12}")
TERMINAL = {"committed", "safe_failed"}


def durable_bytes(path, value):
    """Replace one file atomically, flushing its contents before it becomes visible."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".operation-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows refuses to replace a file another thread is reading, e.g. a page poll listing operations.
                if os.name != "nt" or attempt == 19:
                    raise
                time.sleep(.05)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def durable_json(path, value):
    durable_bytes(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                                   allow_nan=False) + "\n").encode("utf-8"))


@contextmanager
def file_lock(path, message):
    """An OS-held lock, automatically released on process exit; never delete the lock file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise VoiceError(409, message) from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise VoiceError(409, message) from exc
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


class ProductionOperations:
    def __init__(self, production_root, on_saved=None):
        self.root = Path(production_root) / "operations"
        self.on_saved = on_saved
        self._record_cache = OrderedDict()
        self._cache_lock = threading.RLock()
        self._cache_bytes = 0

    def clear_cache(self):
        with self._cache_lock:
            self._record_cache.clear()
            self._cache_bytes = 0

    @staticmethod
    def _file_key(path):
        try:
            return file_identity(path)
        except OSError:
            return None

    @staticmethod
    def validate(kind, body):
        if kind not in {"take", "timeline"} or not isinstance(body, dict):
            raise VoiceError(400, "制作操作参数格式无效。")
        key = body.get("request_id")
        if not isinstance(key, str) or not REQUEST_ID.fullmatch(key):
            raise VoiceError(400, "缺少保存标识，请刷新后重试。")
        if not isinstance(body.get("job_id"), str) or not JOB_ID.fullmatch(body["job_id"]):
            raise VoiceError(404, "这张制作单不存在。")
        try:
            # Reject non-finite or otherwise unserializable inputs before any external effect.
            json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False)
            request_hash = fingerprint([kind, body])
        except (TypeError, ValueError) as exc:
            raise VoiceError(400, "制作操作参数格式无效。") from exc
        return request_hash

    @staticmethod
    def _key(body):
        return hashlib.sha256(body["request_id"].encode("utf-8")).hexdigest()

    def path(self, body):
        return self.root / (self._key(body) + ".json")

    def folder(self, record):
        return self.root / record["operation_key"]

    def recovery_file(self, record):
        return self.folder(record) / "resolve.json"

    def _read(self, path):
        return copy.deepcopy(self._read_snapshot(Path(path)))

    def _read_snapshot(self, path):
        """Validated internal snapshot; callers of _read still receive independent records."""
        with self._cache_lock:
            stamp = self._file_key(path)
            cached = self._record_cache.get(str(path))
            if cached is not None and stamp is not None and cached[0] == stamp:
                self._record_cache.move_to_end(str(path))
                return cached[1]
            if cached is not None:
                self._cache_bytes -= cached[2]
                del self._record_cache[str(path)]
            result = self._read_file(path)
            retained = stamp[2] * 8 if stamp is not None else 0
            if stamp is not None and stamp == self._file_key(path) and retained <= 16 * 1024 * 1024:
                self._record_cache[str(path)] = (stamp, result, retained)
                self._cache_bytes += retained
                while self._cache_bytes > 32 * 1024 * 1024 or len(self._record_cache) > 128:
                    _, removed = self._record_cache.popitem(last=False)
                    self._cache_bytes -= removed[2]
            return result

    def _read_file(self, path):
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema_version") != 1:
                raise ValueError("invalid production operation")
            return value
        except (OSError, TypeError, ValueError) as exc:
            raise VoiceError(503, "制作恢复记录无法读取，请保留 operations 原文件并先核对外部结果，不能重新生成。") from exc

    def read(self, kind, body):
        request_hash = self.validate(kind, body)
        path = self.path(body)
        if not path.exists():
            return None
        record = self._read(path)
        if record.get("request_hash") != request_hash or record.get("request_id") != body["request_id"]:
            raise VoiceError(409, "保存标识已用于另一项修改，请使用原请求恢复。")
        return record

    def recovery_body(self, job_id, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "恢复参数格式无效。")
        probe = {"request_id": body.get("request_id"), "job_id": job_id}
        self.validate(body.get("kind"), probe)
        path = self.path(probe)
        if not path.exists():
            raise VoiceError(404, "这项制作恢复记录不存在。")
        record = self._read(path)
        original = record.get("body")
        if (record.get("job_id") != job_id or record.get("kind") != body["kind"] or
                not isinstance(original, dict) or original.get("job_id") != job_id or
                original.get("request_id") != body["request_id"]):
            raise VoiceError(409, "恢复记录与制作单不一致。")
        self.read(body["kind"], original)
        return copy.deepcopy(original)

    @contextmanager
    def guard(self, kind, body):
        self.validate(kind, body)
        with ExitStack() as stack:
            stack.enter_context(file_lock(self.root / "locks" / (self._key(body) + ".lock"),
                                          "这项制作请求正在处理，请等待完成后用原请求恢复。"))
            stack.enter_context(file_lock(self.root / "locks" / (body["job_id"] + ".lock"),
                                          "这张制作单正在处理另一项操作，请等待完成。"))
            record = self.read(kind, body)
            for path in self.root.glob("*.json"):
                other = self._read(path)
                if (other.get("job_id") == body["job_id"] and other.get("kind") == kind and
                        other.get("request_id") != body["request_id"] and other.get("state") not in TERMINAL):
                    if kind == "timeline" and not self.recovery_file(other).exists():
                        # This job's lock is held, so that build is not running; without its worker journal it
                        # never changed DaVinci and must not block a new build.
                        self.update(other, "safe_failed", error="这次建线没有开始，达芬奇里没有改动；已由新的建线请求取代。",
                                    cancelled=True)
                        continue
                    label = "配音" if kind == "take" else "建线"
                    raise VoiceError(409, f"这张制作单有一项{label}尚未恢复（请求 {other['request_id']}），请先恢复或核实该项结果。")
            yield record

    def prepare(self, kind, body, snapshot, *, take_id=""):
        self.validate(kind, body)
        record = {"schema_version": 1, "kind": kind, "request_id": body["request_id"],
                  "request_hash": fingerprint([kind, body]), "body": copy.deepcopy(body),
                  "operation_key": fingerprint(["production-operation", kind, body]),
                  "job_id": body["job_id"], "take_id": take_id, "snapshot": copy.deepcopy(snapshot),
                  "state": "prepared", "created_at": now(), "updated_at": now(), "error": ""}
        self.write(record)
        return record

    def write(self, record):
        durable_json(self.path(record["body"]), record)
        # Activity is deliberately outside the durable-operation contract: a
        # failed log must never make speech or Resolve work run a second time.
        if self.on_saved:
            try:
                self.on_saved(record)
            except Exception:
                pass

    def update(self, record, state, **values):
        saved = {**copy.deepcopy(record), **copy.deepcopy(values), "state": state, "updated_at": now()}
        self.write(saved)
        return saved

    def best_effort_uncertain(self, record, error):
        # If this write also fails, the already-durable inflight state still prevents speech replay.
        try:
            return self.update(record, "uncertain", error=str(error)[:1000])
        except (OSError, ValueError, VoiceError):
            return record

    def save_speech_response(self, record, result, audio_path):
        audio = result.get("audio")
        if not isinstance(audio, bytes) or not audio:
            raise VoiceError(503, "配音返回音频无效，结果不确定，请先核对服务商记录。")
        durable_bytes(audio_path, audio)
        response = {"schema_version": 1, "operation_key": record["operation_key"],
                    "audio": str(Path(audio_path).resolve()), "audio_sha256": hashlib.sha256(audio).hexdigest(),
                    "alignment": result.get("alignment"), "normalized_alignment": result.get("normalized_alignment"),
                    "request_id": str(result.get("request_id") or "")[:200]}
        durable_json(self.folder(record) / "speech_response.json", response)
        return self.update(record, "response_saved", provider_request_id=response["request_id"], error="")

    def speech_response(self, record):
        path = self.folder(record) / "speech_response.json"
        if not path.exists():
            return None
        value = self._read(path)
        audio = Path(value.get("audio", ""))
        expected = self.root.parent / "audio" / record["job_id"] / record["take_id"] / "voiceover.mp3"
        try:
            if (value.get("operation_key") != record["operation_key"] or audio.resolve() != expected.resolve() or
                    hashlib.sha256(audio.read_bytes()).hexdigest() != value.get("audio_sha256")):
                raise ValueError("incomplete speech response")
        except (OSError, ValueError) as exc:
            raise VoiceError(503, "已生成的配音文件未通过恢复核对，请保留原文件并先核对服务商记录，不能重新生成。") from exc
        return value

    def records(self):
        """One fresh journal read shared by all orders in one catalog response."""
        return [self._read_snapshot(path) for path in self.root.glob("*.json")]

    def latest(self, job_id, kind, *, records=None):
        """The most recently updated operation of one kind for a job, finished or not."""
        found = [record for record in (self.records() if records is None else records)
                 if record.get("job_id") == job_id and record.get("kind") == kind]
        return copy.deepcopy(max(found, key=lambda record: record.get("updated_at") or "", default=None))

    def public(self, job_id=None, *, records=None):
        result = []
        for record in self.records() if records is None else records:
            if (job_id is None or record.get("job_id") == job_id) and record.get("state") not in TERMINAL:
                state, kind = record.get("state"), record.get("kind")
                has_response = kind == "take" and (self.folder(record) / "speech_response.json").is_file()
                has_result = bool(record.get("result"))
                audio_folder = self.root.parent / "audio" / record["job_id"] / record.get("take_id", "")
                has_audio = kind == "take" and any(item.is_file() and item.stat().st_size > 0 for item in
                                                   (audio_folder / "voiceover.mp3", audio_folder / "voiceover.wav"))
                uncertain = kind == "take" and state in {"inflight", "uncertain"} and not has_response and not has_result
                message = ("已找到音频，请先核对并恢复或导入该音频，不能按未生成重新发送。" if uncertain and has_audio else
                           "配音结果不确定，请先核对服务商记录；工作台不会再次发送配音请求。" if uncertain else
                           "已保存外部结果，可用原请求恢复资料登记。" if state in {"response_saved", "result_ready"} or has_response or has_result else
                           "可用原请求恢复；建线将先核对已有时间线。" if kind == "timeline" and self.recovery_file(record).exists() else
                           "这次建线没有开始，达芬奇里没有改动；可按原请求继续，或直接重新建线。" if kind == "timeline" else
                           "尚未发送配音，可用原请求继续。")
                result.append({key: copy.deepcopy(record.get(key)) for key in
                               ("request_id", "job_id", "kind", "state", "take_id", "updated_at", "error")} |
                              {"body": copy.deepcopy(record["body"]), "message": message, "uncertain": uncertain,
                               "has_journal": kind == "timeline" and self.recovery_file(record).exists(),
                               "has_response": has_response, "has_result": has_result, "has_audio": has_audio,
                               "can_retry_not_generated": uncertain and not has_audio})
        return sorted(result, key=lambda item: item.get("updated_at") or "")
