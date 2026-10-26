"""Continuously publish the authorized text snapshot, using the existing local API.

This process never starts the workbench or an AI manager. Credentials remain in
Wrangler's own login store/environment; neither output, configuration nor logs
are copied into the published site. Run --status to read the local status file.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app_config import load_config
ACCOUNT_ID = ""
PROJECT = ""
AUTH_PROFILE = "case-workbench-text-sync"
PUBLIC_URL = ""
TZ = ZoneInfo("Asia/Shanghai")
STATES = {
    "starting": "自动同步已启动，正在检查本机文字资料。",
    "waiting_local_service": "等待本机工作台启动；恢复后会自动同步。",
    "pending": "已发现文字资料变化，正在合并更新。",
    "exporting": "正在导出已保存的文字资料。",
    "publishing": "正在更新公网文字版。",
    "synced": "公网文字版已同步，正在等待下一次更新。",
    "unchanged": "文字内容没有变化，无需再次发布。",
    "retry_wait": "本次同步未完成，稍后自动重试。",
    "stopped": "自动同步已停止。",
}
ERRORS = {
    "export_failed": "文字导出未完成，将重新检查完整资料后重试。",
    "deploy_failed": "公网发布未完成，将自动重试。",
    "auth_required": "Cloudflare 登录暂不可用；登录恢复后将自动重试。",
    "export_timeout": "文字导出超时，将自动重试。",
    "deploy_timeout": "公网发布超时，将自动重试。",
    "configuration": "同步配置或发布程序暂不可用，请检查本地设置。",
}


def timestamp():
    return datetime.now(TZ).isoformat(timespec="seconds")


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".status-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class AlreadyRunning(RuntimeError):
    pass


class ProcessLock:
    """A kernel-held lock; an unclean process exit releases it automatically."""
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.file.seek(0, os.SEEK_END) == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise AlreadyRunning("自动同步已经在运行。") from None
        return self

    def release(self):
        if self.file is None:
            return
        try:
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()
            self.file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()


@dataclass(frozen=True)
class FileMark:
    size: int
    modified: int
    derived: bool = False


def changed_files(before, after, *, business_only=False):
    changed = set()
    for path in before.keys() | after.keys():
        left, right = before.get(path), after.get(path)
        if left != right and (not business_only or not (right or left).derived):
            changed.add(path)
    return changed


class FingerprintScanner:
    """Stat only known text sources; source-file references are cached by stat."""
    def __init__(self, root):
        self.root = Path(root)
        self.references = {}

    def take(self):
        config_path = self.root / "config.json"
        config = load_config(self.root)
        skill, runtime = Path(config["skill_root"]), Path(config["runtime_root"])
        found = {}

        def add(path, derived=False):
            path = Path(path)
            try:
                stat = path.stat()
                if not path.is_file():
                    return
                # Empty/missing WALs carry no changed records. Their creation or
                # deletion by read connections should not trigger a new export.
                if path.name.endswith("-wal") and not stat.st_size:
                    return
                found[str(path.absolute())] = FileMark(stat.st_size, stat.st_mtime_ns, derived)
            except FileNotFoundError:
                return

        def walk(folder, suffixes, derived=False, names=None):
            folder = Path(folder)
            if not folder.is_dir():
                return
            for path in folder.rglob("*"):
                if path.suffix.lower() in suffixes and (names is None or path.name in names):
                    add(path, derived)

        add(config_path)
        add(self.root / "config.local.json")
        cases = skill / "cases"
        if cases.is_dir():
            for path in cases.rglob("*.json"):
                if path.name in {"original.json", "task.json"} or path.parent.name == "events":
                    add(path)
        walk(skill / "sources", {".md", ".txt", ".json"})
        walk(skill / "references", {".md", ".txt", ".json"})
        walk(skill / "templates", {".md", ".txt", ".json"})
        walk(runtime / "ai-tasks", {".json"}, names={"task.json"})
        for relative in ("directions/catalog.json", "creation/catalog.json", "reviews/state.json",
                         "navigation-assignments.json", "evidence/product-catalog.json"):
            add(runtime / relative)
        walk(runtime / "reviews" / "sources", {".json"})
        walk(runtime / "performance" / "uploads", {".xlsx"})
        for folder in config.get("performance_sources", []):
            walk(folder, {".xlsx"})
        for folder, name, derived in (("reviews", "reviews.sqlite", True),
                                       ("performance", "performance.sqlite", False)):
            add(runtime / folder / name, derived)
            add(runtime / folder / (name + "-wal"), derived)
        walk(self.root / "static", {".html", ".js", ".css"})
        walk(self.root / "deployment" / "text-only", {".js", ".css"})
        add(self.root / "deployment" / "export_text_site.py")
        # Case/source markdown can live outside the store. Never recursively
        # watch evidence directories or import media; only markdown/text source_analysis files.
        owners = {path for path in found if Path(path).name == "original.json" or Path(path).parent.name == "events"}
        self.references = {key: value for key, value in self.references.items() if key in owners}
        for path in owners:
            mark = found[path]
            if self.references.get(path, (None,))[0] != mark:
                refs = set()
                try:
                    if mark.size <= 8 * 1024 * 1024:
                        def references(value):
                            if isinstance(value, dict):
                                source = value.get("source_case")
                                if isinstance(source, dict) and isinstance(source.get("file_path"), str):
                                    target = Path(source["file_path"])
                                    if target.is_absolute() and target.suffix.lower() in {".md", ".txt"}:
                                        refs.add(str(target))
                                for child in value.values():
                                    references(child)
                            elif isinstance(value, list):
                                for child in value:
                                    references(child)
                        references(json.loads(Path(path).read_text(encoding="utf-8-sig")))
                except (ValueError, OSError):
                    # A half-written event will be detected again on its next
                    # stat change; the exporter itself rejects incomplete data.
                    pass
                self.references[path] = (mark, refs)
            for reference in self.references[path][1]:
                add(reference)
        return found


def content_digest(root):
    """Compare semantic data plus shipped code, excluding export-only clocks."""
    root = Path(root)
    bundle = json.loads((root / "deployment" / "text-snapshot.json").read_text(encoding="utf-8"))
    revision = bundle.get("meta", {}).get("content_revision")
    if isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{64}", revision):
        # The exporter covers sanitized data and all public page sources.
        return revision
    for key in ("exported_at", "generated_at"):
        bundle.get("meta", {}).pop(key, None)
        bundle.get("state", {}).pop(key, None)
    for key in ("signature", "digest", "token"):
        bundle.get("state", {}).pop(key, None)
    digest = hashlib.sha256(json.dumps(bundle, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode("utf-8"))
    site = root / "deployment" / "site"
    for path in sorted(site.rglob("*")):
        if not path.is_file() or (path.suffix.lower() not in {".html", ".js", ".css", ".txt"}
                                 and path.name not in {"_headers", "_routes.json"}):
            continue
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".html":
            text = re.sub(r'(<meta\s+name=["\']public-exported-at["\']\s+content=["\'])[^"\']*',
                          r"\1[export-time]", text, flags=re.I)
        digest.update(path.relative_to(site).as_posix().encode("utf-8") + b"\0")
        digest.update(text.encode("utf-8") + b"\0")
    return digest.hexdigest()


def local_api_ready(base):
    try:
        with urlopen(base.rstrip("/") + "/api/service/status", timeout=3) as response:
            value = json.load(response)
            return value.get("app") == "video-case-workbench" and not value.get("shutting_down")
    except (OSError, ValueError):
        return False


class StageFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(ERRORS[code])


def publish_settings(root=ROOT, *, required=True):
    """Resolve this installation's destination; a fresh clone has none."""
    config = load_config(root).get("text_public_sync", {})
    if not isinstance(config, dict):
        raise StageFailure("configuration")
    names = {"account_id": "CLOUDFLARE_ACCOUNT_ID", "project": "CASE_WORKBENCH_TEXT_PROJECT",
             "public_url": "CASE_WORKBENCH_TEXT_PUBLIC_URL", "auth_profile": "CASE_WORKBENCH_TEXT_AUTH_PROFILE"}
    value = {key: str(os.environ.get(env) or config.get(key) or "").strip() for key, env in names.items()}
    value["auth_profile"] = value["auth_profile"] or AUTH_PROFILE
    value["public_url"] = value["public_url"] or (f"https://{value['project']}.pages.dev" if value["project"] else "")
    if required:
        try:
            url = urlsplit(value["public_url"])
        except ValueError:
            raise StageFailure("configuration") from None
        if (not re.fullmatch(r"[a-fA-F0-9]{32}", value["account_id"])
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value["project"])
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value["auth_profile"])
                or url.scheme != "https" or not url.hostname or url.username or url.password
                or url.path not in {"", "/"} or url.query or url.fragment):
            raise StageFailure("configuration")
    return value


class Publisher:
    def __init__(self, root, base, node=None, wrangler=None, timeout=600):
        self.root, self.base = Path(root), base
        self.node = node
        self.wrangler = Path(wrangler) if wrangler else self.root / "deployment" / "node_modules" / "wrangler" / "bin" / "wrangler.js"
        self.timeout = timeout

    def _run(self, command, stage):
        environment = os.environ.copy()
        environment.update(CLOUDFLARE_AUTH_USE_KEYRING="true",
                           CLOUDFLARE_SEND_METRICS="false", WRANGLER_SEND_METRICS="false", CI="1", PYTHONUTF8="1")
        if stage == "deploy":
            try:
                config = load_config(self.root)
                auth_root = Path(config["runtime_root"]) / "text-public-sync" / "cloudflare-auth"
                site = (self.root / "deployment" / "site").resolve()
                if not auth_root.is_absolute() or auth_root.resolve().is_relative_to(site):
                    raise ValueError()
            except (OSError, ValueError, KeyError):
                raise StageFailure("configuration") from None
            # A scheduled process can see a different AppData view from the
            # desktop app. The opaque encrypted profile lives in this shared
            # private runtime directory; the encryption key stays in keyring.
            environment["XDG_CONFIG_HOME"] = str(auth_root)
            environment["CLOUDFLARE_ACCOUNT_ID"] = publish_settings(self.root)["account_id"]
        try:
            result = subprocess.run(command, cwd=self.root, env=environment, shell=False,
                                    capture_output=True, encoding="utf-8", errors="replace",
                                    timeout=self.timeout,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired:
            raise StageFailure(stage + "_timeout") from None
        except OSError:
            raise StageFailure("configuration") from None
        if result.returncode:
            # Never persist subprocess output: it may include local paths,
            # account details or credentials. Only classify a known failure.
            output = (result.stdout + result.stderr).lower()
            if stage == "deploy" and any(word in output for word in ("authentication", "not logged", "login", "api token", "unauthorized")):
                raise StageFailure("auth_required")
            raise StageFailure(stage + "_failed")
        return result.stdout

    def export(self):
        self._run([sys.executable, "-X", "utf8", str(self.root / "deployment" / "export_text_site.py"),
                   "--base", self.base], "export")

    def deploy(self):
        destination = publish_settings(self.root)
        node = shutil.which(self.node or "node")
        if not node or not self.wrangler.is_file():
            raise StageFailure("configuration")
        output = self._run([str(Path(node).resolve()), str(self.wrangler.resolve()), "pages", "deploy",
                            str(self.root / "deployment" / "site"), "--project-name", destination["project"],
                            "--branch", "main", "--commit-dirty=true", "--profile", destination["auth_profile"]], "deploy")
        urls = re.findall(r"https://[a-z0-9.-]+\.pages\.dev\b", output, re.I)
        return urls[-1] if urls else destination["public_url"]


class SyncService:
    def __init__(self, root, state_root, publisher, *, poll=5, debounce=5, retry_max=300,
                 scanner=None, ready=None, clock=None, logger=None):
        self.root, self.state_root = Path(root), Path(state_root)
        self.publisher = publisher
        self.poll, self.debounce, self.retry_max = poll, debounce, retry_max
        self.scanner = scanner or FingerprintScanner(root)
        self.ready = ready or (lambda: local_api_ready(publisher.base))
        self.clock = clock or time.monotonic
        self.logger = logger or logging.getLogger("text_public_sync")
        self.baseline, self.pending, self.dirty_at, self.retry_at = None, True, float("-inf"), 0
        self.stopping = False
        previous = {}
        try:
            previous = json.loads((self.state_root / "status.json").read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
        self.status = {"schema": 1, "pid": os.getpid(), "started_at": timestamp(),
                       "state": "starting", "message": STATES["starting"],
                       "poll_seconds": poll, "debounce_seconds": debounce,
                       "public_url": publish_settings(root, required=False)["public_url"], "consecutive_failures": 0,
                       "last_published_digest": previous.get("last_published_digest"),
                       "last_published_at": previous.get("last_published_at"),
                       "last_deployment_url": previous.get("last_deployment_url")}
        self.update("starting")

    def update(self, state=None, **fields):
        if state:
            if self.status.get("state") != state:
                self.logger.info(STATES[state])
            self.status.update(state=state, message=STATES[state])
        self.status.update(fields, updated_at=timestamp())
        atomic_json(self.state_root / "status.json", self.status)

    def fail(self, code):
        failures = self.status["consecutive_failures"] + 1
        delay = min(self.retry_max, self.poll * 2 ** min(failures - 1, 10))
        self.retry_at = self.clock() + delay
        self.pending = True
        self.logger.warning("%s 下次重试约 %s 秒后。", ERRORS[code], delay)
        self.update("retry_wait", consecutive_failures=failures, last_error_code=code,
                    last_error_message=ERRORS[code],
                    next_retry_at=(datetime.now(TZ) + timedelta(seconds=delay)).isoformat(timespec="seconds"))

    def step(self, *, once=False):
        try:
            current = self.scanner.take()
        except (OSError, ValueError, KeyError):
            if self.clock() >= self.retry_at:
                self.fail("configuration")
            return False
        now = self.clock()
        if self.baseline is not None and current != self.baseline:
            self.pending, self.dirty_at = True, now
            self.update("pending", watched_files=len(current))
        self.baseline = current
        self.update(watched_files=len(current), last_checked_at=timestamp())
        if not self.pending:
            return True
        if now < self.retry_at or (not once and now - self.dirty_at < self.debounce):
            return False
        if not self.ready():
            self.update("waiting_local_service", next_retry_at=None)
            return False
        before = current
        try:
            self.update("exporting", next_retry_at=None)
            started = self.clock()
            self.publisher.export()
            self.update(last_export_seconds=round(self.clock() - started, 2))
            after = self.scanner.take()
            # Source/manual/record changes during export need another coherent
            # snapshot. Review DB changes caused by GET sync are absorbed here.
            if changed_files(before, after, business_only=True):
                self.baseline, self.dirty_at, self.pending = after, self.clock(), True
                self.update("pending", last_error_code=None, last_error_message=None)
                return False
            self.baseline = after
            digest = content_digest(self.root)
            self.update(last_content_digest=digest)
            if digest == self.status.get("last_published_digest"):
                self.pending = False
                self.retry_at = 0
                self.update("unchanged", consecutive_failures=0, last_error_code=None,
                            last_error_message=None, next_retry_at=None)
                return True
            self.update("publishing")
            started = self.clock()
            url = self.publisher.deploy()
            self.pending = False
            self.retry_at = 0
            self.update("synced", last_published_digest=digest, last_published_at=timestamp(),
                        last_deployment_url=url, last_deploy_seconds=round(self.clock() - started, 2),
                        consecutive_failures=0, last_error_code=None, last_error_message=None,
                        next_retry_at=None)
            # Keep the export baseline rather than taking a fresh baseline after
            # deployment: edits saved while uploading must be noticed next tick.
            return True
        except StageFailure as error:
            self.fail(error.code)
        except (OSError, ValueError, KeyError):
            self.fail("export_failed")
        return False

    def run(self, *, once=False):
        try:
            if once:
                return 0 if self.step(once=True) else 1
            while not self.stopping:
                self.step()
                # Short waits let shutdown interrupt promptly. Polling itself
                # stays at five seconds rather than hammering the local API.
                until = self.clock() + self.poll
                while not self.stopping and self.clock() < until:
                    time.sleep(min(0.5, max(0, until - self.clock())))
            return 0
        finally:
            if not once:
                self.update("stopped")


def read_status(state_root):
    try:
        return json.loads((Path(state_root) / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "not_started", "message": "自动同步尚未启动。"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="自动同步本机已保存文字到公网浏览版")
    parser.add_argument("--once", action="store_true", help="尝试同步一次后退出")
    parser.add_argument("--status", action="store_true", help="只显示本地同步状态")
    parser.add_argument("--base", help="本机工作台地址（默认读取 config.json 的端口）")
    parser.add_argument("--node", help="Node 程序的绝对路径")
    parser.add_argument("--wrangler", help="已安装 Wrangler 入口的绝对路径")
    parser.add_argument("--poll", type=float, default=5)
    parser.add_argument("--debounce", type=float, default=5)
    args = parser.parse_args(argv)
    try:
        config = load_config(ROOT)
        state_root = Path(config["runtime_root"]) / "text-public-sync"
        base = args.base or f"http://127.0.0.1:{config.get('port', 8767)}"
        url = urlsplit(base)
        if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment
                or args.poll < 1 or args.debounce < 0):
            raise ValueError()
    except (OSError, ValueError, KeyError):
        print(json.dumps({"state": "error", "message": ERRORS["configuration"]}, ensure_ascii=False))
        return 2
    if args.status:
        print(json.dumps(read_status(state_root), ensure_ascii=False, indent=2))
        return 0
    try:
        with ProcessLock(state_root / "process.lock"):
            logger = logging.getLogger("text_public_sync")
            logger.setLevel(logging.INFO)
            handler = RotatingFileHandler(state_root / "sync.log", maxBytes=128 * 1024,
                                          backupCount=2, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
            try:
                service = SyncService(ROOT, state_root, Publisher(ROOT, base, args.node, args.wrangler),
                                      poll=args.poll, debounce=args.debounce, logger=logger)
                def stop(*unused):
                    service.stopping = True
                signal.signal(signal.SIGINT, stop)
                signal.signal(signal.SIGTERM, stop)
                code = service.run(once=args.once)
                if args.once:
                    print(json.dumps(read_status(state_root), ensure_ascii=False, indent=2))
                return code
            finally:
                handler.close()
                logger.removeHandler(handler)
    except AlreadyRunning:
        print(json.dumps({"state": "already_running", "message": "自动同步已经在运行。"}, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
