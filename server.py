"""Local case workbench; uses the existing append-only CaseStore as its only database."""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
from datetime import datetime
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

APP_ROOT = Path(__file__).resolve().parent


def code_signature():
    """Newest modification time of the program's own Python files; compared with the value at start-up to notice
    that the running process predates an update (static pages are read from disk on every load, the backend is not)."""
    newest = 0
    for path in APP_ROOT.glob("*.py"):
        try:
            newest = max(newest, path.stat().st_mtime_ns)
        except OSError:
            continue
    return newest


CODE_AT_START = code_signature()
from app_config import load_config
DEFAULT_CONFIG = load_config(APP_ROOT)
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "scripts"))
sys.path.insert(0, str(Path(DEFAULT_CONFIG["skill_root"]) / "scripts"))
from case_store import CaseStore, StoreError, add_schema, current_schema, current_sources, encode
from render_case_library import render_library
from validate_case_structure import DEFAULT_TEMPLATE, validate_case_structure
from case_deletion import DeletionManager, DeletionError
from managed_video import ManagedVideoFiles, VideoFileError
from ai_tasks import AITaskManager, AITaskError
from performance_store import DEFAULT_SHOP, PerformanceStore, SHOPS, product_short
from sales_data import SalesDataResolver
from public_stats import PublicStatsError, PublicStatsStore, export_workbook
from variant_parse import parse_variants, same_lines, variant_script
from tk_candidates import CandidateError, CandidateStore, candidate_instruction, voiceover
from review_store import ReviewError, ReviewStore
from direction_store import DirectionError, DirectionStore, fingerprint, product_key
from case_products import ProductCatalog, ProductLabels
from direction_progress import build_direction_progress
from creation_store import CreationError, CreationStore
from content_management import ContentManagementStore
from campaign_store import CampaignStore
from content_views import ContentViewsStore
from workbench_content import ContentWorkbenchMixin, _parse_content_filter
from review_followups import ReviewFollowupStore
from review_policy import review_today
from workbench_feedback import FeedbackWorkbenchMixin
from review_experiences import ReviewExperienceStore
from workbench_experiences import ReviewExperiencesWorkbenchMixin
from workbench_creation import CreationWorkbenchMixin
from workbench_tk_candidates import TKCandidateWorkbenchMixin
from production_store import ProductionStore
from elevenlabs_client import ElevenLabsClient, VoiceError
from workbench_production import ProductionWorkbenchMixin
from resolve_bridge import ResolveBridge
from workbench_editing import EditingWorkbenchMixin
from workbench_carousel import CarouselWorkbenchMixin
from workbench_carousel_table import CarouselTableMixin
from workbench_carousel_stills import CarouselStillsMixin
from workbench_carousel_clips import CarouselClipsMixin
from workbench_order_delete import OrderDeleteMixin
from workbench_carousel_images import CarouselImagesMixin
from workbench_carousel_import import CarouselFinishedImportMixin
from workbench_carousel_delivery import CarouselDeliveryMixin
from workbench_carousel_publish import CarouselPublishMixin
from workbench_video_publish import VideoPublishMixin, VideoPublicationStore
from workbench_prepublish import PrepublishWorkbenchMixin
from workbench_video_carousel import VideoCarouselMixin
from workbench_carousel_autopilot import CarouselAutopilotMixin
from workbench_carousel_group import CarouselGroupMixin
from workbench_carousel_cutout import CarouselCutoutMixin
from codex_image_bridge import CodexImageBridge
from cockpit_image_bridge import CockpitImageBridge
from codex_stills_bridge import CodexStillsBridge
from codex_edit_bridge import CodexEditBridge
from data_transfer import DataTransfer, TransferError, pick_directory
from data_backup import BackupError, MAX_BYTES
from data_lease import ServerLease
from task_activity import ActivityStore, ActivityError, reconcile_activity, refresh_activity_projection
from case_auto_breakdown import AutoBreakdown
from case_breakdown_runner import CaseBreakdownRunner, transcribe_case
from case_insights import tag_stats
from remote_accounts import RemoteAccounts, RemoteAccountError

EXTENSIONS = {
    "video": {".mp4", ".webm", ".mov", ".m4v"},
    "screenshot": {".png", ".jpg", ".jpeg", ".webp"},
    "contact_sheet": {".png", ".jpg", ".jpeg", ".webp"},
    "transcript": {".txt", ".md", ".json"},
    "source_manifest": {".json"},
    "performance": {".xlsx"},
}


class ApiError(Exception):
    def __init__(self, status, message, **details):
        super().__init__(message)
        self.status = status
        self.details = details


# POSTs from the TK 看板 browser extension: checked by origin and pairing key instead of the page token.
EXTENSION_PATHS = {"/api/public-stats/ping", "/api/public-stats/push", "/api/public-stats/products", "/api/public-stats/product-brief",
                   "/api/public-stats/direction-candidates", "/api/public-stats/reference-status", "/api/public-stats/direction-produce",
                   "/api/public-stats/comments"}
STATIC_REFERENCE = re.compile(r'((?:src|href)="/)([A-Za-z0-9_.-]+\.(?:js|css|svg))(")')
HOME_VIDEO_FIELDS = (
    "video_id", "product", "headline", "title", "account", "posted_on", "window_end",
    "status", "status_label", "missing_window", "reference", "flags", "window_views", "orders", "views",
    "last_data", "observation_coverage_complete", "observation_missing_days", "review_context", "registered", "registered_on", "registration_only",
    "review_status",
)


def static_signature():
    """Change marker for every page asset, so the versioned index follows any js/css/svg edit."""
    folder = APP_ROOT / "static"
    return tuple((path.name, file_key(path)) for path in sorted(folder.iterdir()) if path.suffix in {".js", ".css", ".svg"})


def versioned_index(raw):
    """Stamp each page asset with a content hash: the browser keeps ?v= files and stops revalidating them
    one by one, which over the public tunnel cost a round trip per file on every page open."""
    def stamp(match):
        try:
            digest = hashlib.sha256((APP_ROOT / "static" / match[2]).read_bytes()).hexdigest()[:12]
        except OSError:
            return match[0]
        return f"{match[1]}{match[2]}?v={digest}{match[3]}"
    return STATIC_REFERENCE.sub(stamp, raw.decode("utf-8")).encode("utf-8")


def file_key(path):
    """Cheap change marker for one file: size, modified and created time (None when absent)."""
    try:
        stat = os.stat(path)
    except (OSError, ValueError):
        return None
    return stat.st_size, stat.st_mtime_ns, getattr(stat, "st_birthtime_ns", stat.st_ctime_ns)


def accepts_gzip(value):
    """Honor an explicit gzip preference before the wildcard fallback."""
    encodings = {}
    for part in value.lower().split(","):
        name, *parameters = part.strip().split(";")
        quality = 1.0
        for parameter in parameters:
            if parameter.strip().startswith("q="):
                try:
                    quality = float(parameter.strip()[2:])
                except ValueError:
                    quality = 0.0
        encodings[name] = quality if 0 <= quality <= 1 else 0.0
    return encodings.get("gzip", encodings.get("*", 0)) > 0


class CachedCaseStore(CaseStore):
    """CaseStore that reuses its last full index verification while the record files are unchanged.

    read_index() re-reads and validates every case. Pages poll it every few seconds, and those
    thousands of file calls per poll starve the other request threads. The verification is
    re-run whenever a scan of the case folders (names, sizes, modified and created times) differs.
    Callers treat the returned index as read-only.
    """

    def __init__(self, root):
        super().__init__(root)
        self._index_key = None
        self._index_cache = None
        self._case_cache = {}
        self._cache_lock = threading.RLock()
        self._index_context = threading.local()
        # Unified product names (case_products.py); set by the workbench. Records and index.json keep the original names.
        self.product_labels = None
        self._raw_reads = threading.local()
        self._labelled_index = None
        self._raw_products = {}

    @property
    def index_key(self):
        snapshot = getattr(self._index_context, "snapshot", None)
        key = snapshot[0] if snapshot is not None else self._index_key
        return (key, self.product_labels.key()) if self.product_labels is not None else key

    @contextmanager
    def _raw(self):
        depth = getattr(self._raw_reads, "depth", 0)
        self._raw_reads.depth = depth + 1
        try:
            yield
        finally:
            self._raw_reads.depth = depth

    def _labelling(self):
        return self.product_labels is not None and not getattr(self._raw_reads, "depth", 0)

    def build_index(self):
        # The persisted index must describe the records exactly as stored.
        with self._raw():
            return super().build_index()

    def direction_summary(self, *args, **kwargs):
        # Test-direction groups show each observation's original product name ("原产品名").
        with self._raw():
            return super().direction_summary(*args, **kwargs)

    def raw_product(self, identifier):
        return self._raw_products.get(identifier)

    def _label_index(self, index):
        if not self._labelling():
            return index
        key = (id(index), self.product_labels.key())
        cached = self._labelled_index
        if cached is not None and cached[0] == key and cached[1] is index:
            return cached[2]
        labelled = dict(index)
        labelled["cases"] = []
        for entry in index["cases"]:
            self._raw_products[entry["case_id"]] = entry.get("product")
            product = self.product_labels.label(entry["case_id"], entry.get("product"))
            labelled["cases"].append(entry if product == entry.get("product") else {**entry, "product": product})
        self._labelled_index = (key, index, labelled)
        return labelled

    @contextmanager
    def index_snapshot(self):
        """Share one verified index within an explicitly read-only operation.

        A new outer scope still scans for external changes. Nested scopes and
        helper reads use that same index and change marker; other threads keep
        validating independently. Individual read_case() checks are unchanged.
        """
        previous = getattr(self._index_context, "snapshot", None)
        if previous is None:
            with self._cache_lock, self._raw():
                index = self.read_index()
                snapshot = (self._index_key, index)
        else:
            snapshot = previous
        self._index_context.snapshot = snapshot
        try:
            yield self._label_index(snapshot[1])
        finally:
            if previous is None:
                del self._index_context.snapshot
            else:
                self._index_context.snapshot = previous

    def fingerprint(self, identifier=None):
        """Kinds, sizes and times of the entries the index is built from:
        cases/ itself (case folders, index.json), and each case's original.json and events/."""
        entries = []

        def scan(folder, keep=None):
            found = []
            try:
                with os.scandir(folder) as listing:
                    for entry in listing:
                        if keep and entry.name not in keep:
                            continue
                        # On Windows the directory listing already carries these fields: no extra file call.
                        stat = entry.stat(follow_symlinks=False)
                        is_dir = entry.is_dir(follow_symlinks=False)
                        link = entry.is_symlink() or getattr(entry, "is_junction", lambda: False)()
                        entries.append((entry.path, is_dir, link, stat.st_size, stat.st_mtime_ns,
                                        getattr(stat, "st_birthtime_ns", stat.st_ctime_ns)))
                        found.append((entry.path, is_dir))
            except OSError:
                entries.append((str(folder), "unreadable"))
            return found

        if identifier is not None:
            folder = self.folder(identifier)
            entries.append((str(folder), file_key(folder)))
            folders = [(str(folder), True)]
        else:
            folders = scan(self.root / "cases")
        for case, is_dir in folders:
            if is_dir:
                for part, part_is_dir in scan(case, {"original.json", "events"}):
                    if part_is_dir:
                        scan(part)
        return tuple(sorted(entries, key=repr))

    def read_index(self):
        snapshot = getattr(self._index_context, "snapshot", None)
        if snapshot is not None:
            return self._label_index(snapshot[1])
        with self._cache_lock:
            key = self.fingerprint()
            cached = self._index_cache
            if cached is not None and cached[0] == key:
                self._index_key = key
                return self._label_index(cached[1])
            with self._raw():
                index = super().read_index()
            # The key was taken before reading, so a change made during the read forces a re-check next time.
            self._index_cache, self._index_key = (key, index), key
            known = {case["case_id"] for case in index["cases"]}
            self._case_cache = {identifier: value for identifier, value in self._case_cache.items() if identifier in known}
            return self._label_index(index)

    def read_case(self, identifier):
        # Retain the store's identifier/path/link checks on cache hits. Scan every event so
        # external appends, edits, removals and invalid entries still invalidate validation.
        self.folder(identifier)
        with self._cache_lock:
            key = self.fingerprint(identifier)
            cached = self._case_cache.get(identifier)
            if cached is None or cached[0] != key:
                case = super().read_case(identifier)
                self._case_cache[identifier] = (key, case)
            else:
                case = cached[1]
            # Existing consumers may edit their returned current snapshot before saving.
            case = copy.deepcopy(case)
        if self._labelling():
            self._raw_products[identifier] = case.get("product")
            case["product"] = self.product_labels.label(identifier, case.get("product"))
        return case


def revision(case):
    return hashlib.sha256(json.dumps(case, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def review_request(query):
    """URL query (parse_qs lists) -> (filters, limit, offset, sort) for ReviewStore.search."""
    def one(name, default=None):
        values = query.get(name, [])
        if len(values) > 1:
            raise ApiError(400, f"评论库：参数 {name} 只能出现一次。")
        return values[0] if values else default

    def numbers(name, default):
        try:
            return int(one(name, default))
        except (TypeError, ValueError):
            raise ApiError(400, f"评论库：参数 {name} 必须是整数。") from None

    filters = {name: [v for v in query.get(name, []) if v] for name in ("platform", "role", "nature", "topic", "competitor")}
    for name in ("q", "rating_min", "rating_max", "language", "product", "sku", "replied", "video", "since"):
        filters[name] = one(name, "")
    for name in ("include_brand", "include_irrelevant", "include_empty"):
        filters[name] = one(name) == "1"
    return filters, numbers("limit", 50), numbers("offset", 0), one("sort", "likes")


def job_fill_instruction(job):
    """The 写稿 request that fills a 制作单's script information around its own, unchangeable spoken lines."""
    count = len(job["lines"])
    lines = [
        f"按写稿技能的口播稿交付格式，为下面这张制作单的口播写出完整交付：开头画面、开头屏显、口播（配音用）代码块、读法（需要时）、"
        f"对应中文、意群表（# | 口播 | 画面要证明什么 | 素材标签 | 证据，结尾 CTA 行注明）、补拍清单、3 行发布文案，以及发布前核对。只写这 1 条，不出多稿。",
        f"这条口播已经进了制作单，可能已经配音、剪辑：口播（配音用）代码块必须逐字照抄下面 {count} 句，一句一行，不增删、不改写、不调顺序；"
        "意群表也正好 {count} 行，和这些句子一一对应。觉得哪句有事实或合规风险，写进发布前核对，不要改句子。".replace("{count}", str(count)),
        "", f"制作单：{job.get('title') or job['id']}", "口播（逐字保留）：",
    ]
    lines += [f"{number}. {line['spoken']}" + (f"（{line['zh']}）" if line.get("zh") else "") for number, line in enumerate(job["lines"], 1)]
    have = [f"{number}. {line['proof']}" for number, line in enumerate(job["lines"], 1) if line.get("proof")]
    if have:
        lines += ["已有的画面要求（可沿用或补强）：", *have]
    return "\n".join(lines)[:19000]


def job_fill_result(job, task):
    """What a finished fill round puts on the job: the 意群表 by row, the script markdown, and which rows it reworded.
    A round that failed, or wrote a different number of lines, leaves the job as it was with the reason."""
    if task["status"] != "completed":
        return {"error": "写稿任务没有完成（" + {"failed": "失败", "cancelled": "已取消", "interrupted": "被中断"}.get(task["status"], task["status"]) + "），可以再点一次补全。"}
    turn = next((entry for entry in reversed(task["rounds"]) if entry["status"] == "completed" and (entry.get("output") or "").strip()), None)
    output = turn["output"] if turn else ""
    spoken = [line["spoken"] for line in job["lines"]]
    variants = [entry for entry in parse_variants(output) if len(entry["lines"]) == len(spoken)] if output else []
    chosen = next((entry for entry in variants if same_lines([line["spoken"] for line in entry["lines"]], spoken)), None) or (variants[0] if variants else None)
    if not chosen:
        return {"error": f"写稿结果里没有正好 {len(spoken)} 句的口播和意群表，没有写回；可以到协作任务里查看这一轮，或再点一次补全。"}
    changed = [number for number, (mine, theirs) in enumerate(zip(spoken, chosen["lines"]), 1) if not same_lines([mine], [theirs["spoken"]])]
    return {"lines": [{key: line.get(key) or "" for key in ("proof", "tags", "evidence", "zh")} for line in chosen["lines"]],
            "markdown": variant_script(output, chosen["index"]), "changed": changed}


class Workbench(ReviewExperiencesWorkbenchMixin, TKCandidateWorkbenchMixin, FeedbackWorkbenchMixin, ContentWorkbenchMixin, CreationWorkbenchMixin, ProductionWorkbenchMixin, EditingWorkbenchMixin, CarouselWorkbenchMixin, CarouselTableMixin, CarouselStillsMixin, CarouselClipsMixin, OrderDeleteMixin, CarouselImagesMixin, CarouselFinishedImportMixin, CarouselDeliveryMixin, CarouselPublishMixin, VideoPublishMixin, PrepublishWorkbenchMixin, VideoCarouselMixin, CarouselAutopilotMixin, CarouselGroupMixin, CarouselCutoutMixin):
    def __init__(self, skill_root, runtime_root, allow_open=True, ai_bridge=None, performance_sources=()):
        self.store = CachedCaseStore(skill_root)
        self.runtime = Path(runtime_root).resolve()
        self.store.product_labels = ProductLabels(self.runtime / "evidence" / "case-products.json")
        self.product_catalog = ProductCatalog(self.runtime / "evidence" / "product-catalog.json", product_key)
        self.library = self.runtime / "library"
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.allow_open = allow_open
        self.requests = {}
        self.structure_cache = {}
        self.state_cache = None
        self.products_cache = None
        self.review_product_sources = None
        self.direction_sync_cache = None
        self.thumbnail_lock = threading.Lock()
        self.deletion = DeletionManager(self.store.root, self.runtime)
        self.video_files = ManagedVideoFiles(self.runtime)
        self.reviews = ReviewStore(self.runtime)
        self.performance = PerformanceStore(self.runtime, performance_sources)
        self.public_stats = PublicStatsStore(self.runtime)
        self.tk_candidates = CandidateStore(self.runtime)
        self.public_exports = self.runtime / "performance" / "public-exports"
        self.performance.public = self.public_stats
        self.sales = SalesDataResolver(self.performance)
        self.directions = DirectionStore(self.runtime, self.store.root, self.lock)
        self.creation = CreationStore(self.runtime, self.lock)
        self.content_management = ContentManagementStore(self.runtime, self.lock)
        self.content_views = ContentViewsStore(self.runtime, self.lock, filter_validator=_parse_content_filter)
        self.campaigns = CampaignStore(self.runtime, self.lock)
        self.review_followups = ReviewFollowupStore(self.runtime, self.lock)
        self.review_experiences = ReviewExperienceStore(self.runtime, self.lock)
        self.production = ProductionStore(self.runtime, self.lock)
        self.reconcile_tk_candidates()
        self.activity = ActivityStore(self.runtime)
        # 收为案例后自动取原视频并拆解；拆解步骤可替换（见 case_auto_breakdown.py）。
        self.auto_breakdown = AutoBreakdown(self, analyzer=CaseBreakdownRunner())
        # Cases left at 「视频已关联，拆解待接入」 (or cut off by the restart) are broken down without a click.
        resume = threading.Timer(8, self.auto_breakdown.resume_pending)
        resume.daemon = True
        resume.start()
        self.voices = ElevenLabsClient()
        self.resolve = ResolveBridge()
        self.ai = AITaskManager(self.store, self.runtime, self.lock, bridge=ai_bridge,
                                review_digest=self.review_digest, sales_resolver=self.sales, activity=self.activity)
        self.ai.on_finished = self._ai_round_finished
        self.ai.draft_media = self.draft_media
        # Editing uses the selected local client with a task-specific restricted tool profile.
        self.editor = getattr(self.ai.bridge, "claude", None)
        codex = getattr(self.ai.bridge, "codex", None)
        self.edit_codex = CodexEditBridge(executable=getattr(codex, "executable", None))
        self.still_codex = CodexStillsBridge(executable=getattr(codex, "executable", None))
        self.edit_skill_dir = Path(skill_root).resolve().parent / "davinci-product-video-editing"
        self.ledger_dir = self.runtime / "davinci-ledger"
        self.carousel_image_root = APP_ROOT / "图文素材"  # existing carousel folders, only read from
        self.carousel_finished_root = APP_ROOT / "图文成品"  # explicit finished-package import boundary
        image_config = DEFAULT_CONFIG.get("image_generation") or {}
        self.image_generator = CockpitImageBridge(parameters_file=image_config.get("parameters_file"),
                                                   auth_config=image_config.get("auth_config"))
        self.image_reviewer = CodexImageBridge()
        self.recover_edit_results()
        self.production.interrupt_running()
        self.delivery_retention = self.prune_all_carousel_deliveries()
        reconcile_activity(self)
        self.data_transfer = DataTransfer(self.store.root, self.runtime, APP_ROOT,
            locks=(self.lock, self.reviews.lock, self.performance.lock, self.activity.lock),
            validator=self.validate_imported_data, ensure_idle=self.ensure_data_idle,
            on_restore=self.reload_imported_data, extra_sources=performance_sources)
        self.data_transfer.activity = self.activity
        self.performance.sources = list(self.data_transfer.manager.extra_sources)

    @staticmethod
    def validate_imported_data(skill, runtime):
        store = CaseStore(skill)
        store.rebuild_index()
        store.read_index()
        DirectionStore(runtime, skill).read()
        CreationStore(runtime).read()
        ContentManagementStore(runtime).public()
        ContentViewsStore(runtime, filter_validator=_parse_content_filter).read()
        ReviewFollowupStore(runtime).read()
        ReviewExperienceStore(runtime).read()
        ProductionStore(runtime).read()
        VideoPublicationStore(Path(runtime) / "production").validate()

    def ensure_data_idle(self):
        with self.lock:
            tasks = self.ai.list_tasks()
            if any(task.get("busy") or task.get("stopping") or task.get("status") in {"queued", "running", "stopping"} for task in tasks):
                raise TransferError("AI 协作正在运行，请结束后再导入、导出或备份。", 409)
            if (getattr(self, "_voicing_jobs", set()) or getattr(self, "_timeline_building", {}) or self.edit_running() or
                    self.image_running() or self.autopilot_running() or self.carousel_table_running()):
                raise TransferError("配音、建时间线、剪辑或图文制作正在运行，请完成后再操作总数据。", 409)

    def reload_imported_data(self):
        with self.lock:
            self.activity.reload(reset_epoch=True)
            self.requests.clear()
            self.structure_cache.clear()
            self.state_cache = self.products_cache = self.review_product_sources = self.direction_sync_cache = None
            self.store._index_key = self.store._index_cache = None
            self.store._case_cache.clear()
            self.creation.version += 1
            self.creation.clear_cache()
            self.review_experiences.clear_cache()
            self.production.clear_cache()
            self.ai.clear_cache()
            operations_reader = getattr(self, "_operations_reader", None)
            if operations_reader is not None:
                operations_reader.clear_cache()
            self.ai._locations.clear()
            self.ai._live.clear()
            self.ai._live_saved.clear()
            self.ai._cancel_events.clear()
            self.ai._discover()
            from ai_tasks import ACTIVE, _now
            for identifier in list(self.ai._locations):
                task = self.ai._read(identifier)
                if task["status"] in ACTIVE:
                    task.update(status="interrupted", progress="数据已恢复，本轮已中断；可检查历史后继续。")
                    for turn in task["rounds"]:
                        if turn["status"] in ACTIVE:
                            turn.update(status="interrupted", error="数据恢复后未自动重试。", completed_at=_now())
                            self.ai._settle_stages(turn, "interrupted", turn["error"])
                    self.ai._write(task)
            self.ai._changed()
            with self.performance.lock:
                self.performance.sources = list(self.data_transfer.manager.extra_sources)
                self.performance._cache.clear()
                self.performance._cache_generation = None
                self.performance._cache_bytes = 0
            self.reviews.state_error = None
            self.reviews.state.saves += 1
            self.production.interrupt_running()
            reconcile_activity(self)
            render_library(self.store.root, self.library)

    def claude_settings(self, action, body):
        """Old clients cannot re-enable API billing or install an unused SDK."""
        raise ApiError(410, "Claude 写稿已改为使用本机 Claude Code 客户端额度，无需填写 API key；请刷新页面。")

    def claude_login(self, body):
        if body:
            raise ApiError(400, "连接 Claude 订阅只接受空对象。")
        claude = getattr(self.ai.bridge, "claude", None)
        if claude is None or not callable(getattr(claude, "start_login", None)):
            raise ApiError(503, "当前 AI 连接不支持 Claude 订阅授权。")
        from codex_bridge import BridgeError
        try:
            state = claude.start_login()
        except BridgeError as exc:
            raise ApiError(503, str(exc)) from None
        return {"claude": state, "login": self._claude_login_details(claude),
                "message": "Claude 订阅已连接。" if state.get("available") else "请完成浏览器授权；关闭窗口后可复制链接重新打开，或填写回调链接。"}

    @staticmethod
    def _claude_login_details(claude):
        details = getattr(claude, "login_details", None)
        return details() if callable(details) else {"attempt_id": None, "pending": False, "url": None,
                                                   "expires_at": None, "callback_submitted": False}

    def claude_login_state(self):
        claude = getattr(self.ai.bridge, "claude", None)
        if claude is None or not callable(getattr(claude, "login_details", None)):
            raise ApiError(503, "当前 AI 连接不支持恢复 Claude 授权。")
        return {"claude": claude.status(), "login": self._claude_login_details(claude)}

    def claude_login_action(self, action, body):
        expected = {"attempt_id", "callback_url"} if action == "callback" else {"attempt_id"}
        if set(body) != expected or not isinstance(body.get("attempt_id"), str) or not body["attempt_id"]:
            raise ApiError(400, "授权参数不完整，请刷新设置页后重试。")
        if action == "callback" and (not isinstance(body.get("callback_url"), str) or not body["callback_url"].strip()):
            raise ApiError(400, "请粘贴本次授权的回调链接或完整授权码。")
        claude = getattr(self.ai.bridge, "claude", None)
        method = getattr(claude, "submit_login_callback" if action == "callback" else "cancel_login", None)
        if not callable(method):
            raise ApiError(503, "当前 AI 连接不支持此授权操作。")
        from codex_bridge import BridgeError
        try:
            state = method(body["attempt_id"], body["callback_url"]) if action == "callback" else method(body["attempt_id"])
        except BridgeError as exc:
            raise ApiError(getattr(exc, "status", 503), str(exc)) from None
        return {"claude": state, "login": self._claude_login_details(claude),
                "message": "回调已提交，正在完成授权。" if action == "callback" else "已取消本次授权，可重新连接。"}

    def video_context(self, video_ids):
        """What the review page shows beside a TikTok video: title, account and sales figures from the video data (when it has
        them), and the case the video is linked to. Light on purpose: the full video list takes seconds to work out."""
        ids = {str(v) for v in video_ids if v}
        if not ids:
            return {}
        brief, links = self.performance.video_brief(ids), self.video_case_links()
        return {i: {**brief.get(i, {}), "case_id": links.get(i)} for i in ids}

    def review_digest(self, product):
        """The review-library digest for a writing or review task on a product the library covers; None for any other."""
        if not self.reviews.covers(product):
            return None
        self.reviews.sync()  # the same cheap check the review pages run: rebuilds only when a source file changed
        return self.reviews.digest(product=product)

    def push_public_stats(self, body):
        """Public page counts from the TK 看板 extension, kept for videos in the sales data or the case library."""
        if set(body) - {"videos", "extension_version"}:
            raise ApiError(400, "推送内容格式错误。")
        links = self.video_case_links()
        try:
            return self.public_stats.record(body.get("videos"),
                lambda ids: self.performance.known_video_ids(ids) | {video_id for video_id in ids if video_id in links})
        except PublicStatsError as exc:
            raise ApiError(400, str(exc))

    def push_public_comments(self, body):
        """Import actual comments with the product associated with their video."""
        if set(body) - {"videos", "extension_version"}:
            raise ApiError(400, "评论推送内容格式错误。")
        videos = body.get("videos")
        if not isinstance(videos, list) or not 0 < len(videos) <= 200:
            raise ApiError(400, "评论推送需提供 1 至 200 条视频。")
        ids = {str(video.get("id", "")) for video in videos if isinstance(video, dict)
               and re.fullmatch(r"\d{6,25}", str(video.get("id", "")))}
        candidates = {identifier: set() for identifier in ids}

        def associate(identifier, value):
            normalized = product_key(value) if isinstance(value, str) and value.strip() else ""
            if identifier in candidates and normalized:
                candidates[identifier].add(normalized)

        for identifier, video in self.performance.video_brief(ids).items():
            associate(identifier, video.get("product"))
        for entry in self.store.read_index()["cases"]:
            current = entry.get("current") or {}
            identifier = str((current.get("metrics") or {}).get("video_id") or "")
            associate(identifier, entry.get("product") or current.get("product"))
        if any(not products for products in candidates.values()):
            for video in self.performance.creators().get("videos", []):
                if video[0] in candidates:
                    for product in video[5]:
                        associate(video[0], product)
        # Explicit product selection can resolve a multi-product report. Other
        # comments stay unassigned and visible under all products, never under 202H by default.
        requested = {str(video.get("id", "")): product_key(video.get("product"))
                     for video in videos if isinstance(video, dict) and isinstance(video.get("product"), str)}
        available = {product_key(value) for value in self.products()["products"]} if requested else set()
        products = {identifier: next(iter(values)) for identifier, values in candidates.items() if len(values) == 1}
        for identifier, values in candidates.items():
            if len(values) > 1 and requested.get(identifier) in values:
                products[identifier] = requested[identifier]
            elif not values and requested.get(identifier) in available:
                products[identifier] = requested[identifier]
            elif identifier not in products:
                products[identifier] = ""
        try:
            return self.reviews.ingest_comments(videos, products)
        except ReviewError as exc:
            raise ApiError(400, str(exc)) from None

    def extension_products(self, body):
        """Product names the TK 看板 extension offers in its script-direction picker."""
        if body:
            raise ApiError(400, "请求格式错误。")
        return {"products": self.products()["products"]}

    def extension_product_brief(self, body):
        """What the extension's script-direction AI reads about one product: selling points, directions, a few cases and
        the review digest. Kept short on purpose: it all goes into one prompt."""
        product = body.get("product")
        if set(body) - {"product"} or not isinstance(product, str) or not product.strip() or len(product) > 200:
            raise ApiError(400, "请选择产品。")
        product = product.strip()
        if product not in self.products()["products"]:
            raise ApiError(404, "工作台里没有这个产品。")
        key = product_key(product)
        clip = lambda value, limit: str(value or "").strip()[:limit]
        with self.lock, self.store.index_snapshot():
            cases = self.store.read_index()["cases"]
            catalog = self.directions.public(self.synced_directions(cases), key)
        points = [{"code": item["code"], "tier": item["tier"], "title": clip(item.get("title"), 300),
                   "rationale": clip(item.get("rationale"), 300), "evidence": clip(item.get("evidence"), 200),
                   "status": item.get("status")}
                  for item in catalog["selling_points"] if item.get("status") != "merged"][:24]
        directions = [{"direction_id": item["direction_id"], "main": clip(item.get("main_difference"), 200),
                       "concern": clip(item.get("concern"), 300), "closing": clip(item.get("closing"), 200),
                       "audience": clip(item.get("audience"), 160), "variant_count": len(item.get("variants") or []),
                       "status": item.get("status")}
                      for item in catalog["directions"] if item.get("status") != "archived"][:12]
        chosen = [case for case in cases if product_key(case.get("product")) == key and not case.get("archived")
                  and case.get("current")][-6:]
        examples = [{"title": clip(case["current"].get("title") or case.get("title"), 120),
                     "category": clip(case["current"].get("category"), 40),
                     "conclusion": clip(case["current"].get("conclusion"), 240),
                     "highlights": [clip(item, 120) for item in (case["current"].get("highlights") or [])[:3]],
                     "opening": clip(((case["current"].get("transcript") or {}).get("text") or "").split("\n")[0], 200)}
                    for case in reversed(chosen)]
        reviews = None
        if self.reviews.covers(product):
            try:
                reviews = self.review_digest(product)
            except (OSError, sqlite3.Error, ReviewError):
                reviews = None
        return {"product": product, "selling_points": points, "directions": directions, "cases": examples,
                "combos": self.direction_combos(key),
                "reviews": json.dumps(reviews, ensure_ascii=False)[:2500] if reviews else ""}

    def _auto_produce_tk_candidate(self, item):
        """Finish a newly saved card; replays never reopen archived/deleted work."""
        if (item["status"] in {"dismissed", "writing"} or item.get("job_id")
                or item.get("writing_task_id") or item.get("direction_error")):
            return item
        try:
            self.produce_candidate(item["candidate_id"])
            return self.tk_candidates.remember_production_error(item["candidate_id"])
        except (ApiError, CandidateError, CreationError, VoiceError) as exc:
            if exc.status >= 500:
                raise
            return self.tk_candidates.remember_production_error(item["candidate_id"], str(exc))

    def extension_save_candidates(self, body, *, auto_produce=True):
        """Save new TK cards, register their directions and create associated orders."""
        if set(body) - {"product", "reference", "directions", "ask", "extension_version"}:
            raise ApiError(400, "保存内容格式错误。")
        product = body.get("product")
        if not isinstance(product, str) or not product.strip() or len(product) > 200:
            raise ApiError(400, "请选择产品。")
        if product.strip() not in self.products()["products"]:
            raise ApiError(404, "工作台里没有这个产品。")
        with self.lock:
            result = self.tk_candidates.add(product.strip(), body.get("reference"), body.get("directions"), ask=body.get("ask") or "")
            candidates = []
            for saved in result["candidates"]:
                item = self._associate_tk_candidate(saved["candidate_id"])
                if auto_produce:
                    item = self._auto_produce_tk_candidate(item)
                candidates.append(self.tk_candidates.brief(item))
            result["candidates"] = candidates
            return result

    def extension_reference_status(self, body):
        """What one reference video led to: its saved candidates, and for adopted ones the direction's own figures
        (every video linked to that direction, not only ones shot from this candidate)."""
        if set(body) - {"video_id"}:
            raise ApiError(400, "请求格式错误。")
        items = self.tk_candidates.for_reference(str(body.get("video_id") or ""))
        directions = []
        for product in sorted({item["product"] for item in items if item.get("direction_id")}):
            wanted = {item["direction_id"] for item in items if item.get("direction_id") and item["product"] == product}
            for entity in self.direction_catalog(product)["directions"]:
                if entity["direction_id"] in wanted:
                    perf = entity.get("performance") or {}
                    directions.append({"product": product, "direction_id": entity["direction_id"], "status": entity.get("status"),
                                       "video_count": perf.get("video_count", 0), "matched_count": perf.get("matched_count", 0),
                                       "orders": perf.get("orders", 0), "gmv": perf.get("gmv", 0), "data_through": perf.get("data_through")})
        return {"candidates": [self.tk_candidates.brief(item) for item in items], "directions": directions}

    def tk_candidate_list(self, product, status):
        return {"candidates": self.tk_candidates.list(product, status), "counts": self.tk_candidates.counts(product)}

    def tk_candidate_status(self, body):
        if set(body) - {"candidate_id", "status", "direction_id"} or not isinstance(body.get("candidate_id"), str):
            raise ApiError(400, "请求格式错误。")
        exists = lambda product, identifier: any(item["direction_id"] == identifier for item in self.direction_catalog(product)["directions"])
        with self.lock:
            item = self.tk_candidates.set_status(body["candidate_id"], body.get("status"), body.get("direction_id") or "",
                                                 direction_exists=exists)
            if item["status"] != "dismissed":
                item = self._associate_tk_candidate(item["candidate_id"])
            return {"candidate": item}

    def tk_candidate_produce(self, body):
        if set(body) - {"candidate_id"} or not isinstance(body.get("candidate_id"), str):
            raise ApiError(400, "请求格式错误。")
        return self.produce_candidate(body["candidate_id"])

    def extension_produce_candidate(self, body):
        """One card from the extension, saved as a candidate and turned straight into a video 制作单."""
        if set(body) - {"product", "reference", "direction", "ask", "extension_version"}:
            raise ApiError(400, "请求格式错误。")
        saved = self.extension_save_candidates({key: body[key] for key in ("product", "reference", "ask", "extension_version") if key in body}
                                               | {"directions": [body.get("direction")]}, auto_produce=False)
        return self.produce_candidate(saved["candidates"][0]["candidate_id"])

    def produce_candidate(self, identifier):
        """A candidate goes straight to a video 制作单, its 口播 and 中文 exactly as the card wrote them: no direction
        wizard and no 写稿 rewrite. The card's 开头画面 and 开头屏显 come along as the job's script information; the
        意群表's 画面要求 and the rest are left for the job page, where 「AI 补全脚本信息」 fills them around these
        unchanged lines. Direction membership and exact execution provenance are registered automatically.
        Asking again returns the same 制作单 while it is live."""
        with self.lock:
            item = self._associate_tk_candidate(identifier)
            if item.get("job_id"):
                try:
                    job = self.production.job(item["job_id"])
                    if not job.get("archived"):
                        if product_key(job["product"]) != product_key(item["product"]):
                            raise ApiError(409, "候选与原制作单的产品不同，未打开其他产品的制作单。")
                        if item.get("script_id") and job.get("script_id") != item["script_id"]:
                            raise ApiError(409, "候选保存的脚本与原制作单采用稿不同，请先核对原关联。")
                        return {"candidate": self.tk_candidates.brief(item), "script_id": item.get("script_id", ""),
                                "job": {"id": job["id"], "product": job["product"]}, "reused": True}
                except VoiceError as exc:
                    if exc.status != 404:
                        raise
            spoken, chinese = voiceover(item["direction"])
            if not spoken:
                raise ApiError(400, "这条方向没有英文口播，不能直接建制作单。")
            direction = item["direction"]
            wanted = item.get("direction_id") if not item.get("direction_error") else ""
            title = " ".join(part for part in (direction["code"], direction["name"]) if part) or "TK 看板方向"
            # Resume after a partial save using the exact already-adopted version.
            # A linked archived/deleted order is an explicit new-production path.
            script_id = item.get("script_id") if not item.get("job_id") else ""
            if script_id:
                script = self.creation.get_script(script_id)
                if product_key(script["product"]) != product_key(item["product"]):
                    raise ApiError(409, "候选与已保存脚本的产品不同，未另建制作单。")
            else:
                request_id = "tkc-script-" + fingerprint([identifier, item.get("job_id") or "initial"])[:32]
                prior = self.creation.read()["requests"].get(request_id)
                if prior:
                    script_id = prior["result"]["script"]["id"]
                    recovered = self.creation.get_script(script_id)
                    if (product_key(recovered["product"]) != product_key(item["product"])
                            or recovered["text"] != "\n".join(spoken)
                            or recovered.get("translation_zh", "") != "\n".join(chinese)):
                        raise ApiError(409, "这条候选的已保存脚本与原文不一致，未重复建稿。")
                else:
                    saved = self.save_script({"request_id": request_id, "revision": self.creation.public()["revision"],
                                              "script": {"product": item["product"], "title": title[:500], "text": "\n".join(spoken),
                                                         "translation_zh": "\n".join(chinese), "direction_id": wanted or "",
                                                         "confirmed": True}})
                    script_id = saved["script"]["id"]
                self.tk_candidates.link(identifier, script_id=script_id)
            matches = [row for row in self.production.records()["jobs"]
                       if not row.get("archived") and row.get("script_id") == script_id]
            if len(matches) > 1:
                raise ApiError(409, "这条脚本已有多张制作单，无法唯一恢复；请从内容列表打开原制作单。")
            reused = bool(matches)
            if matches:
                job = self.production.job(matches[0]["id"])
            else:
                created = self.create_production_job({"request_id": "tkc-" + uuid4().hex, "revision": self.production.public()["revision"],
                                                      "job": {"script_id": script_id}})
                job = created["job"]
            hook = {"hook_visual": direction.get("hook_visual") or "", "hook_text": (direction.get("hook_text") or {}).get("en") or "",
                    "hook_text_zh": (direction.get("hook_text") or {}).get("zh") or ""}
            missing_hook = {key: value[:300] if key != "hook_visual" else value[:1000]
                            for key, value in hook.items() if value and not (job.get("supplement") or {}).get(key)}
            if missing_hook:
                self.production.mutate("job_lines", {"request_id": "tkc-" + uuid4().hex, "revision": self.production.public()["revision"],
                                                     "job_id": job["id"], "supplement": {**(job.get("supplement") or {}), **missing_hook},
                                                     "lines": [{"number": line["number"]} for line in job["lines"]]})
            item = self.tk_candidates.link(identifier, job_id=job["id"])
            item = self._associate_tk_candidate(identifier)
            return {"candidate": self.tk_candidates.brief(item), "script_id": script_id,
                    "job": {"id": job["id"], "product": job["product"]}, "reused": reused}

    def _adopt_candidate_round(self, item, task):
        """Adopt the completed 写稿 round's first voiced script, with its 意群表, and create the 制作单 from it."""
        turn = next((entry for entry in reversed(task["rounds"]) if entry["status"] == "completed" and (entry.get("output") or "").strip()), None)
        variants = parse_variants(turn["output"]) if turn else []
        chosen = next((entry for entry in variants if entry["lines"]), None)
        if not chosen:
            raise ApiError(409, "写稿结果里没有可配音的口播；请到协作任务里查看这一轮，手动指定口播后建制作单。")
        identifier = item["candidate_id"]
        script_id = item.get("script_id") or ""
        if script_id:
            try:
                self.creation.get_script(script_id)
            except CreationError:
                script_id = ""
        if not script_id:
            spoken = [line["spoken"] for line in chosen["lines"]]
            chinese = [line.get("zh") or "" for line in chosen["lines"]]
            title = " ".join(part for part in (item["direction"]["code"], item["direction"]["name"]) if part) or "TK 看板方向"
            saved = self.save_script({"request_id": "tkc-" + uuid4().hex, "revision": self.creation.public()["revision"],
                                      "script": {"product": item["product"], "title": title[:500], "text": "\n".join(spoken),
                                                 "translation_zh": "\n".join(chinese) if all(chinese) else "",
                                                 "direction_id": item.get("writing_direction_id", ""),
                                                 "confirmed": True},
                                      "source": {"task_id": task["task_id"], "round_id": turn["round_id"],
                                                 "task_revision": task["revision"], "variant_index": chosen["index"]}})
            script_id = saved["script"]["id"]
            self.tk_candidates.link(identifier, script_id=script_id)
        created = self.create_production_job({"request_id": "tkc-" + uuid4().hex, "revision": self.production.public()["revision"],
                                              "job": {"script_id": script_id, "variant_index": chosen["index"]}})
        item = self.tk_candidates.link(identifier, job_id=created["job"]["id"])
        item = self._associate_tk_candidate(identifier)
        return {"candidate": self.tk_candidates.brief(item), "script_id": script_id,
                "job": {"id": created["job"]["id"], "product": created["job"]["product"]}, "reused": False}

    def _candidate_round_finished(self, task_id):
        """Called when any AI round ends: a candidate waiting on this round gets its 制作单 without another click."""
        try:
            waiting = [item for item in self.tk_candidates.list() if item.get("writing_task_id") == task_id and not item.get("job_id")]
            for item in waiting:
                self.produce_candidate(item["candidate_id"])
        except Exception as exc:  # the candidate keeps its task; asking again retries the adoption
            print(f"TK 候选自动建制作单未完成：{exc}", file=sys.stderr)

    def _ai_round_finished(self, task_id):
        self._candidate_round_finished(task_id)
        self._job_fill_finished(task_id)

    def fill_production_job(self, identifier, body):
        """「补全脚本信息」 on a 制作单: one 写稿 round, with the writing skill, writes the full delivery around the job's
        own spoken lines (开头画面、屏显、读法、对应中文、意群表的画面要求/素材标签/证据、补拍清单、发布文案、发布前核对).
        The spoken lines stay as they are, since the job may already be voiced and cut; the result is applied row by row."""
        if not isinstance(body, dict):
            raise VoiceError(400, "补全参数格式无效。")
        with self.lock:
            job = self.production.job(identifier)
            if job.get("archived"):
                raise VoiceError(409, "已归档的制作单不能补全。")
            fill = job.get("ai_fill") or {}
            if fill.get("status") == "running":
                try:
                    task = self.ai.get(fill["task_id"])
                except AITaskError:
                    task = None
                if task and task["status"] in ("queued", "running"):
                    return {"job": job, "fill": fill}
                if task:
                    self._job_fill_finished(task["task_id"])
                    return {"job": self.production.job(identifier), "fill": self.production.job(identifier).get("ai_fill")}
            wanted = job.get("direction_id") or ""
            known = any(entry["direction_id"] == wanted and entry.get("status") != "archived"
                        for entry in self.direction_catalog(job["product"])["directions"]) if wanted else False
            task = self.create_ai_task({"request_id": "fill-" + uuid4().hex, "action": "variants", "product": job["product"],
                                        **({"direction_id": wanted} if known else {}), "instruction": job_fill_instruction(job)})
            saved = self.production.mutate("job_fill_start", {"request_id": "fill-" + uuid4().hex, "job_id": identifier,
                                                              "task_id": task["task_id"]})
            return {"job": saved["job"], "fill": saved["job"]["ai_fill"]}

    def _job_fill_finished(self, task_id):
        """When a fill round ends, its result goes onto the 制作单 that asked for it."""
        try:
            waiting = [job for job in self.production.public()["jobs"]
                       if (job.get("ai_fill") or {}).get("task_id") == task_id and job["ai_fill"].get("status") == "running"]
            if not waiting:
                return
            task = self.ai.get(task_id)
            if task["status"] in ("queued", "running"):
                return
            for job in waiting:
                self.production.mutate("job_fill_finish", {"request_id": "fill-" + uuid4().hex, "job_id": job["id"],
                                                           "task_id": task_id, **job_fill_result(job, task)})
        except Exception as exc:  # the job keeps its running mark; asking again settles it
            print(f"制作单 AI 补全未写回：{exc}", file=sys.stderr)

    def direction_combos(self, product):
        """The grading file's "每个 S 各带一套组合" table: per lead point, who it is for, the concern chain and the proof.
        Paused rows are left out."""
        path = self.store.root / "references" / "products" / f"{product}-selling-point-grading.md"
        try:
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            return []
        rows, in_section = [], False
        for line in content.splitlines():
            if line.startswith("## "):
                in_section = "各带一套组合" in line
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")] if in_section and line.strip().startswith("|") else []
            if len(cells) != 4 or set(cells[0]) <= set("-: ") or cells[0] == "测试方向" or "暂停" in cells[0]:
                continue
            rows.append({"direction": cells[0][:120], "audience": cells[1][:300], "chain": cells[2][:400], "proof": cells[3][:200]})
        return rows[:16]

    def export_public_views(self, body):
        """An .xlsx of the listed videos with the export's and the public page's views side by side,
        kept outside the sales data folder so it is never imported as an export."""
        ids = body.get("video_ids")
        if (set(body) != {"video_ids"} or not isinstance(ids, list) or not 0 < len(ids) <= 5000
                or not all(isinstance(v, str) and re.fullmatch(r"\d{6,25}", v) for v in ids)):
            raise ApiError(400, "导出的视频列表无效。")
        by_id = {video["video_id"]: video for video in self.performance.videos()}
        videos = self.annotate_review_status([by_id[v] for v in dict.fromkeys(ids) if v in by_id])
        if not videos:
            raise ApiError(404, "销售数据表里没有这些视频。")
        public = self.public_stats.latest(video["video_id"] for video in videos)
        export_id = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        target = export_workbook(videos, public, self.public_exports / f"公开播放-{export_id}.xlsx")
        return {"name": target.name, "path": str(target), "rows": len(videos),
                "with_public": sum(1 for video in videos if (public.get(video["video_id"]) or {}).get("views") is not None),
                "download_url": f"/api/performance/public-export/{export_id}"}

    def video_case_links(self):
        """TikTok video ID -> case ID, from each case's current snapshot."""
        with self.lock:
            index = self.store.read_index()
        links = {}
        for entry in index["cases"]:
            video_id = str(((entry.get("current") or {}).get("metrics") or {}).get("video_id") or "").strip()
            if video_id:
                links[video_id] = entry["case_id"]
        return links

    def carousel_post_links(self):
        """TikTok photo post ID -> photo production ID, from every post a production has been registered with."""
        links = {}
        for item in self.production.public()["carousels"]:
            for record in (*item.get("publication_history", []), item.get("publication")):
                post = str((record or {}).get("post_id") or "").strip()
                if post:
                    links[post] = item["id"]
        return links

    def products(self):
        """Lightweight product inventory, without full lists or analytics."""
        # Preserve discovery of freshly supplied review exports without holding the
        # workbench lock during their import. Unchanged sources skip this entirely.
        review_files = self.reviews.source_files()
        source_key = tuple((platform, str(path), file_key(path)) for platform, path in review_files)
        review_key = (source_key, self.reviews.database.is_file())
        if self.review_product_sources != review_key:
            try:
                if review_files or self.reviews.database.is_file():
                    self.reviews.sync()
                self.review_product_sources = (source_key, self.reviews.database.is_file())
            except (OSError, sqlite3.Error, ReviewError):
                pass
        def database_signature(path):
            return file_key(path), file_key(Path(str(path) + "-wal"))

        def distinct(database, query, lock):
            if not database.is_file():
                return []
            # Open existing data read-only: discovery must not create/migrate a database.
            with lock:
                connection = None
                try:
                    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
                    return [row[0] for row in connection.execute(query)]
                except (OSError, sqlite3.Error):
                    return []
                finally:
                    if connection is not None:
                        connection.close()

        with self.lock:
            cases = self.store.read_index()["cases"]
            grading = tuple(sorted((str(path), file_key(path)) for path in
                (self.store.root / "references" / "products").glob("*-selling-point-grading.md")))
            try:
                creation_revision = self.creation.current_revision()
            except CreationError:
                creation_revision = file_key(self.creation.path)
            key = (self.store.index_key, self.product_catalog.key(), file_key(self.directions.path), creation_revision,
                   database_signature(self.performance.database), database_signature(self.reviews.database), grading)
            if self.products_cache is not None and self.products_cache[0] == key:
                return self.products_cache[1]
            values = {product_key(case.get("product")) for case in cases}
            for store, groups, error in ((self.directions, ("selling_points", "directions", "video_links"), DirectionError),
                                        (self.creation, ("scripts", "materials", "theme_links", "plans", "variant_links"), CreationError)):
                try:
                    if store is self.creation:
                        values.update(store.product_inventory())
                        continue
                    catalog = store.read()
                    values.update(product_key(item.get("product")) for group in groups for item in catalog.get(group, []))
                except error:
                    # A broken optional catalog must not hide products from other modules.
                    pass
            values.update(product_key(Path(path).name.removesuffix("-selling-point-grading.md")) for path, _ in grading)
            values.update(product_short(value) for value in distinct(self.performance.database,
                "SELECT products FROM daily WHERE products != '' UNION SELECT products FROM periods WHERE products != ''", self.performance.lock))
            values.update(product_key(value) for value in distinct(self.reviews.database,
                "SELECT DISTINCT product FROM reviews WHERE product != ''", self.reviews.lock))
            listed, removed = self.product_catalog.apply(values)
            result = {"products": listed, "removed": removed, "deleted": sorted(set(self.product_catalog.read()["deleted"]))}
            self.products_cache = (key, result)
            return result

    def change_product_catalog(self, body):
        action, name = body.get("action"), body.get("product")
        try:
            known = self.products()["products"] if action in {"remove", "restore", "delete"} and isinstance(name, str) and len(name) > 120 else ()
            product = self.product_catalog.change(action, name, known_products=known)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
        with self.lock:
            self.products_cache = None
        if action == "delete_hidden":
            return {"deleted_count": len(product), "deleted_products": product, **self.products()}
        return {"product": product, **self.products()}

    def set_case_product(self, identifier, product):
        """Manual product pick for one case (empty = back to the automatic name)."""
        # Product discovery can import review sources; do it before taking the
        # workbench lock. Only exact existing identifiers bypass the new-name cap.
        existing_long = isinstance(product, str) and len(product.strip()) > 120 and product.strip() in self.products()["products"]
        with self.lock:
            self.store.read_case(identifier)
            try:
                if existing_long:
                    catalog = self.product_catalog.read()
                    existing_long = product.strip() not in set(catalog["removed"]) | set(catalog["deleted"])
                self.store.product_labels.set(identifier, product, allow_existing=existing_long)
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None
            self.state_cache = None
            self.products_cache = None
            return {"case": self.detail(identifier)}

    def synced_directions(self, cases):
        """Reuse read-only migration results until records, catalog or grading files change."""
        grading = tuple(sorted((str(path), file_key(path)) for path in
            (self.store.root / "references" / "products").glob("*-selling-point-grading.md")))
        key = (self.store.index_key, file_key(self.directions.path), grading)
        if self.direction_sync_cache is not None and self.direction_sync_cache[0] == key:
            return self.direction_sync_cache[1]
        catalog = self.directions.sync(cases)
        # A pre-read key makes a concurrent external edit invalidate this result on
        # the next request, including when migration wrote the catalog itself.
        self.direction_sync_cache = (key, catalog)
        return catalog

    def import_performance_uploads(self, body):
        shop = body.get("shop")
        if shop is not None and shop not in SHOPS:
            raise ApiError(400, "店铺无效。")
        paths = body.get("paths")
        if not isinstance(paths, list) or not paths or len(paths) > 100:
            raise ApiError(400, "请选择要导入的后台导出表格。")
        kept = []
        for raw in paths:
            staged = self.video_files.safe(Path(str(raw)))
            if not staged.is_relative_to(self.video_files.imports) or staged.suffix.lower() != ".xlsx" or not staged.is_file():
                raise ApiError(400, "导入文件已失效，请重新选择。")
            kept.append(self.performance.keep_upload(staged, staged.name))
        # A file picked in the page has no folder to say which shop it is from; the page says so, else it is the local shop's.
        return self.performance.import_paths(kept, skip_unchanged=False, shop=shop or DEFAULT_SHOP)

    def detail(self, identifier, sales_data=None, direction_catalog=None, review_status_snapshot=None):
        case = self.store.read_case(identifier)
        result = copy.deepcopy(case)
        result["revision"] = revision(case)
        result["product_raw"] = self.store.raw_product(identifier) or case.get("product")
        result["product_manual"] = bool(self.store.product_labels and self.store.product_labels.is_manual(identifier))
        result["sales_data"] = copy.deepcopy(sales_data) if sales_data is not None else self.sales.resolve_case(case)
        sales_video = result["sales_data"].get("video") if isinstance(result["sales_data"], dict) else None
        if isinstance(sales_video, dict) and sales_video.get("video_id"):
            sales_video.setdefault("product", case.get("product"))
            result["sales_data"]["video"] = self.annotate_review_status([sales_video], review_status_snapshot)[0]
        result["assets"] = self.asset_info(case.get("current", {}).get("assets", {}) if case.get("current") else {}, identifier)
        result["paths"] = {"record": str(self.store.folder(identifier)), "reading": str(self.library / identifier / "case.md")}
        catalog = direction_catalog if direction_catalog is not None else self.directions.read()
        direction = (case.get("current") or {}).get("test_direction")
        entity = self.directions.find_direction(catalog, case.get("product"), direction["direction_id"]) if direction else None
        result["resolved_direction"] = copy.deepcopy(entity)
        source = (case.get("current") or {}).get("source_case")
        if source:
            path = Path(source["file_path"])
            analysis = {"path": str(path), "exists": False, "text": ""}
            try:
                if path.suffix.lower() == ".md" and path.is_file():
                    if path.stat().st_size > 2 * 1024 * 1024:
                        analysis["error"] = "来源文件过大，请从本地文件查看。"
                    else:
                        content = path.read_text(encoding="utf-8-sig")
                        analysis.update(exists=True, text=content, digest=hashlib.sha256(content.encode("utf-8")).hexdigest())
                        analysis["structure"] = self.structure_check(content, analysis["digest"])
            except (OSError, UnicodeError):
                analysis["error"] = "来源文件暂时无法读取；首次导入的资料仍保留在修订历史中。"
            result["source_analysis"] = analysis
        return result

    def direction_summary(self, **filters):
        summary = self.sales.direction_summary(self.store.direction_summary(**filters))
        catalog = self.synced_directions(self.store.read_index()["cases"])
        for group in summary["groups"]:
            entity = self.directions.find_direction(catalog, group["product"], group["direction_id"])
            if entity:
                # Per-case wording remains a historical definition. Only an
                # incompatible lead recorded by the catalog is a conflict.
                group["definition_conflict"] = bool(entity.get("definition_conflict"))
        return summary

    def direction_catalog(self, product=None):
        with self.lock, self.store.index_snapshot():
            cases = self.store.read_index()["cases"]
            data = self.synced_directions(cases)
            result = self.directions.public(data, product)
            independent_scripts = self.creation_catalog(product, include_usage=False)["scripts"]
            direction_videos = {}
            for direction in result["directions"]:
                selected = [case for case in cases if product_key(case.get("product")) == direction["product"] and
                            ((case.get("current") or {}).get("test_direction") or {}).get("direction_id") == direction["direction_id"]]
                videos = {str(((case.get("current") or {}).get("metrics") or {}).get("video_id") or "") for case in selected} - {""}
                videos = {video for video in videos if not (link := self.directions.find_link(data, direction["product"], video))
                          or link["direction_id"] == direction["direction_id"]}
                videos.update(link["video_id"] for link in data["video_links"] if link["product"] == direction["product"] and link["direction_id"] == direction["direction_id"])
                scripts = [script for script in independent_scripts if script["product"] == direction["product"] and
                           (script.get("direction_id") or (script.get("variant_link") or {}).get("direction_id")) == direction["direction_id"]]
                videos.update(script["video_id"] for script in scripts if script.get("video_id"))
                direction.update(case_count=len(selected), video_count=len(videos), script_count=len(scripts))
                direction_videos[(direction["product"], direction["direction_id"])] = videos
            # Lead-point totals: videos whose direction is led by the point, each video counted once.
            brief = self.performance.video_brief(set().union(*direction_videos.values())) if direction_videos else {}
            def totals(videos):
                matched = [brief[video] for video in videos if video in brief]
                orders = int(sum(item.get("orders") or 0 for item in matched))
                clicks = int(sum(item.get("clicks") or 0 for item in matched))
                return {"video_count": len(videos), "matched_count": len(matched), "orders": orders, "clicks": clicks,
                        "gmv": round(sum(item.get("gmv") or 0 for item in matched), 2),
                        "ctor_pct": round(orders * 100 / clicks, 2) if clicks else None,
                        "data_through": max((item.get("last_data") or "" for item in matched), default="") or None}
            for direction in result["directions"]:
                direction["performance"] = totals(direction_videos[(direction["product"], direction["direction_id"])])
            for point in result["selling_points"]:
                led = [videos for (product, _), videos in direction_videos.items() if product == point["product"] and any(
                    item["product"] == product and item["direction_id"] == _ and item.get("selling_point_id") == point["id"] for item in result["directions"])]
                point["performance"] = totals(set().union(*led)) if led else None
            return result

    def direction_detail(self, identifier, product=None, *, view="full", include_tasks=True):
        if view not in {"full", "page"}:
            raise ApiError(400, "方向详情显示范围无效。")
        with self.lock, self.store.index_snapshot():
            catalog = self.direction_catalog(product)
            matches = [item for item in catalog["directions"] if item["direction_id"] == identifier]
            if not matches:
                raise ApiError(404, "这条方向不存在。")
            if len(matches) != 1:
                raise ApiError(400, "多个产品使用此方向编号，请先选择产品。")
            entity = matches[0]
            product = entity["product"]
            group = next(iter(self.direction_summary(product=product, direction_id=identifier, archive="all")["groups"]), None)
            index = self.store.read_index()["cases"]
            selected = [case for case in index
                     if product_key(case.get("product")) == product and
                     ((case.get("current") or {}).get("test_direction") or {}).get("direction_id") == identifier]
            # The group already resolved these same video IDs in one sales query.
            sales = {item["case_id"]: item["sales_data"] for item in (group or {}).get("observations", [])
                     if item.get("sales_data") is not None}
            missing = [case for case in selected if case["case_id"] not in sales]
            if missing:
                sales.update(self.sales.resolve_cases(missing))
            if view == "page":
                # The direction page links to cases; their full histories,
                # source documents and asset status belong to the case page.
                # Build the displayed titles directly from this verified index.
                cases = [{"case_id": case["case_id"], "title": case["title"],
                          "archived": case["archived"],
                          "current": {"title": case["current"]["title"]} if case.get("current") else None}
                         for case in selected]
            else:
                raw_catalog = self.synced_directions(index)
                cases = [self.detail(case["case_id"], sales[case["case_id"]], raw_catalog) for case in selected]
            links = {link["video_id"]: link for link in catalog["video_links"] if link["product"] == product and link["direction_id"] == identifier}
            case_by_video = {str(((case.get("current") or {}).get("metrics") or {}).get("video_id") or ""): case["case_id"] for case in selected}
            videos = (set(links) | set(case_by_video)) - {""}
            by_video = {link["video_id"]: link for link in catalog["video_links"] if link["product"] == product}
            videos = {video for video in videos if video not in by_video or by_video[video]["direction_id"] == identifier}
            scripts = [{"case_id": case["case_id"], "title": case["current"]["title"],
                        "script_version": (case["current"].get("production") or {}).get("script_version"),
                        "text": case["current"]["transcript"]["text"]}
                       for case in selected if (case.get("current") or {}).get("transcript")]
            creation = self.creation_catalog(product)
            independent = [script for script in creation["scripts"] if
                           (script.get("direction_id") or (script.get("variant_link") or {}).get("direction_id")) == identifier]
            scripts.extend(independent)
            videos.update(script["video_id"] for script in independent if script.get("video_id"))
            brief = self.performance.video_brief(videos)
            # Filter frozen ownership before assembling task cards; live case moves
            # never reassign the task, and there is no second task-file read.
            defer_tasks = view == "page" and not include_tasks
            tasks = [] if defer_tasks else self.ai.list_tasks(product=product, direction_id=identifier)
            result = {"revision": catalog["revision"], "direction": entity, "progress_available": True,
                    "selling_point": next((point for point in catalog["selling_points"] if point["id"] == entity.get("selling_point_id")), None),
                    "selling_points": catalog["selling_points"],
                    "group": group, "cases": cases, "scripts": scripts, "tasks": tasks,
                    "creation_revision": creation["revision"],
                    "plans": [plan for plan in creation["plans"] if plan.get("direction_id") == identifier],
                    "theme_links": [link for link in creation["theme_links"] if link.get("direction_id") == identifier or link.get("selling_point_id") == entity.get("selling_point_id")],
                    "videos": [{"video_id": video, **brief.get(video, {}), "product": product, "direction_id": identifier,
                                "case_id": case_by_video.get(video)} for video in sorted(videos)]}
            if defer_tasks:
                result["tasks_pending"] = True
            return result

    def direction_progress(self, identifier, product=None):
        """Load optional execution evidence separately from the direction first paint."""
        with self.lock, self.store.index_snapshot():
            cases = self.store.read_index()["cases"]
            raw = self.synced_directions(cases)
            catalog = self.directions.public(raw, product)
            matches = [row for row in catalog["directions"] if row["direction_id"] == identifier]
            if not matches:
                raise ApiError(404, "这条方向不存在。")
            if len(matches) != 1:
                raise ApiError(400, "多个产品使用此方向编号，请先选择产品。")
            direction = matches[0]
            product = direction["product"]
            creation = self.creation.read()
            production = self.production.records()
            publications = self._video_publications().read()
            tasks = self.ai.list_tasks(product=product, direction_id=identifier)
            videos = {link["video_id"] for link in catalog["video_links"]
                      if link["product"] == product and link["direction_id"] == identifier}
            for case in cases:
                current = case.get("current") or {}
                if (product_key(case.get("product")) == product and
                        (current.get("test_direction") or {}).get("direction_id") == identifier):
                    video = str((current.get("metrics") or {}).get("video_id") or "")
                    link = self.directions.find_link(raw, product, video) if video else None
                    if video and (not link or link["direction_id"] == identifier):
                        videos.add(video)
            video_cache = {}
            def read_video(video):
                if video not in video_cache:
                    video_cache[video] = self.performance.video(video)
                return video_cache[video]
            variant_links, link_sources, link_counts = [], {}, {}
            for link in creation.get("variant_links", []):
                key = (link.get("kind"), link.get("record_id"))
                link_counts[key] = link_counts.get(key, 0) + 1
            for link in creation.get("variant_links", []):
                if link.get("kind") != "video":
                    variant_links.append(link)
                    continue
                if product_key(link.get("product")) != product or link.get("direction_id") != identifier:
                    continue
                video = link.get("record_id")
                if video not in link_sources:
                    try:
                        record, _ = self._variant_link_source("video", video, product, creation)
                    except CreationError as exc:
                        if exc.status not in {400, 404, 409}:
                            raise
                        record = None
                    link_sources[video] = record
                record = link_sources[video]
                if record and (not record.get("direction_id") or record["direction_id"] == identifier):
                    # Keep a valid-but-ambiguous record visible as unassigned,
                    # even when its duplicate belongs to another direction.
                    variant_links.append({**link, "variant_ref": {}} if
                                         link_counts[(link.get("kind"), video)] > 1 else link)
            return build_direction_progress(direction, catalog_revision=catalog["revision"], tasks=tasks,
                scripts=creation["scripts"], plans=creation["plans"], production=production,
                video_publications=publications, videos=sorted(videos), read_video=read_video,
                variant_links=variant_links)

    def save_direction_catalog(self, kind, body):
        with self.lock:
            self.direction_catalog()
            if kind == "video":
                video = str(body.get("video_id") or "")
                # Never trust the product supplied by a picker or a hand-written request.
                actual = self.performance.video_brief([video]).get(video)
                if not actual or not actual.get("product"):
                    raise ApiError(400, "销售数据中缺少这条视频的产品信息，请先同步并核对产品。")
                if product_key(actual["product"]) != product_key(body.get("product")):
                    raise ApiError(409, "视频所属产品与所选产品不一致，不能跨产品关联方向。")
                if body.get("direction_id"):
                    for case in self.store.read_index()["cases"]:
                        current = case.get("current") or {}
                        if str((current.get("metrics") or {}).get("video_id") or "") == video:
                            if product_key(case.get("product")) != product_key(body.get("product")):
                                raise ApiError(409, "这条视频已收录到另一产品，请先核对案例。")
                            assigned = (current.get("test_direction") or {}).get("direction_id")
                            if assigned and assigned != body["direction_id"]:
                                raise ApiError(409, "视频关联案例的方向不同。请先解除视频关联，在案例修改方向后再绑定。")
            references = []
            if kind == "point" and body.get("operation") == "delete":
                # Comment themes live in the creation store; the catalog cannot see them on its own.
                references = [(link["selling_point_id"], f"评论主题「{link.get('theme', '')[:40]}」关联了它")
                              for link in self.creation.public().get("theme_links", []) if link.get("selling_point_id")]
            result = self.directions.mutate(kind, body, references)
            self.state_cache = None
            result["catalog"] = self.direction_catalog()
            return result

    def structure_check(self, content, digest):
        """Read-only eight-chapter check against the live template; the template owns the rules."""
        try:
            stamp = DEFAULT_TEMPLATE.stat().st_mtime_ns
        except OSError:
            stamp = 0
        key = (digest, stamp)
        if key not in self.structure_cache:
            try:
                errors = validate_case_structure(content)
            except (OSError, ValueError) as exc:
                errors = ["结构检查未能运行：" + str(exc)[:120]]
            if len(self.structure_cache) > 500:
                self.structure_cache.clear()
            self.structure_cache[key] = {"ok": not errors, "count": len(errors), "errors": [error[:240] for error in errors[:8]]}
        return self.structure_cache[key]

    def asset_info(self, assets, identifier=None):
        result = {}
        for kind, raw in assets.items():
            path = Path(raw)
            try:
                size = path.stat().st_size if path.is_file() else None
            except OSError:
                size = None
            result[kind] = {"path": raw, "name": path.name, "exists": size is not None, "bytes": size}
            if kind == "video":
                result[kind].update(self.video_files.info(path, identifier))
        return result

    def state(self):
        with self.lock, self.store.index_snapshot():
            # Read records afresh: changes from the existing intake tools appear without restarting.
            index = self.store.read_index()
            direction_catalog = self.synced_directions(index["cases"])
            known = {entry["case_id"] for entry in index["cases"]}
            self.requests = {key: value for key, value in self.requests.items()
                if not value[1].get("case") or value[1]["case"]["case_id"] in known}
            # Everything the snapshot reads besides the verified records: navigation, template, assets, sources.
            watched = [self.runtime / "navigation-assignments.json", DEFAULT_TEMPLATE, self.directions.path]
            for entry in index["cases"]:
                current = entry.get("current") or {}
                watched.extend(Path(raw) for raw in (current.get("assets") or {}).values())
                if current.get("source_case"):
                    watched.append(Path(current["source_case"]["file_path"]))
            sales_signature = self.performance.signature()
            # Public pushes can affect videos without a case, so they also need
            # their own revision in the polling response and its digest.
            public_stats_signature = self.public_stats.signature()
            public_stats_revision = hashlib.sha256(json.dumps(public_stats_signature).encode()).hexdigest()
            reviews_revision = self.reviews.revision()
            creation_revision = self.creation.current_revision()
            followup_revision = self.review_followups.current_revision()
            publication_signature = file_key(self._video_publications().path)
            review_day = review_today().isoformat()
            key = (self.store.index_key, tuple((str(path), file_key(path)) for path in watched), sales_signature, public_stats_signature, creation_revision,
                   followup_revision, publication_signature, review_day, reviews_revision)
            if self.state_cache is not None and self.state_cache[0] == key:
                return self.state_cache[1]
            sales = self.sales.resolve_cases(index["cases"])
            review_status_snapshot = self.review_status_snapshot()
            cases = [self.detail(entry["case_id"], sales[entry["case_id"]], direction_catalog, review_status_snapshot) for entry in index["cases"]]
            signature = hashlib.sha256("".join(item["revision"] + json.dumps(item["assets"], sort_keys=True)
                                             + item.get("source_analysis", {}).get("digest", "")
                                             + json.dumps(item["sales_data"], sort_keys=True, ensure_ascii=False)
                                             + json.dumps(item["resolved_direction"], sort_keys=True, ensure_ascii=False) for item in cases).encode()).hexdigest()
            navigation = self.navigation()
            signature = hashlib.sha256((signature + json.dumps(navigation, sort_keys=True)).encode()).hexdigest()
            summaries = []
            indexed = {entry["case_id"]: entry for entry in index["cases"]}
            for case in cases:
                current = case.get("current")
                summaries.append({key: case[key] for key in ("case_id", "product", "revision", "assets", "current", "sales_data", "resolved_direction", "current_event_id", "archived", "archive_event_id") if key in case} | {
                    "title": current["title"] if current else case["title"],
                    "updated_at": case["events"][-1]["created_at"],
                    "event_count": len(case["events"]),
                    "search_text": indexed[case["case_id"]]["current_search_text"],
                })
            paths = {"records": str(self.store.root / "cases"), "evidence": str(self.runtime / "evidence"), "library": str(self.library), "directions": str(self.directions.root), "creation": str(self.creation.root)}
            # Lets the polling page ask "anything new since this digest?" and get a tiny reply instead of every case.
            direction_revision = self.directions.revision(direction_catalog)
            signature = hashlib.sha256((signature + direction_revision + creation_revision + followup_revision
                                        + json.dumps(publication_signature) + review_day + public_stats_revision + reviews_revision).encode()).hexdigest()
            digest = hashlib.sha256(json.dumps([navigation, summaries, paths, direction_revision, creation_revision,
                                               followup_revision, publication_signature, review_day, public_stats_revision, reviews_revision], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
            snapshot = {"app": "video-case-workbench", "token": self.token, "signature": signature, "digest": digest,
                        "public_stats_revision": public_stats_revision, "reviews_revision": reviews_revision,
                        "navigation": navigation, "cases": summaries, "paths": paths}
            self.state_cache = (key, snapshot)
            return snapshot

    def navigation(self):
        path = self.runtime / "navigation-assignments.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def move_navigation(self, body):
        with self.lock:
            identifier, target = body.get("case_id"), body.get("target")
            if not isinstance(identifier, str) or not re.fullmatch(r"PVR-[0-9a-f]{12}", identifier):
                raise ApiError(400, "案例编号无效。")
            if not isinstance(target, str) or (
                target not in {"all", "positive", "mixed", "pending", "native", "legacy"}
                and not re.fullmatch(r"column:(?:all|positive|mixed|pending|native|legacy|custom-[a-z0-9-]{8,80})", target)
            ):
                raise ApiError(400, "目标栏目无效。")
            case = self.store.read_case(identifier)
            if case.get("archived"):
                raise ApiError(409, "请先恢复案例，再调整栏目。")
            data = self.navigation()
            if body.get("revision") != revision(case) or body.get("previous") != data.get(identifier):
                raise ApiError(409, "案例或栏目已更新，请刷新后重新拖动。")
            data[identifier] = target
            self.runtime.mkdir(parents=True, exist_ok=True)
            path = self.runtime / "navigation-assignments.json"
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, path)
            return {"ok": True, "target": target}

    def check_assets(self, current, previous=None):
        previous = previous or {}
        for kind, value in current.get("assets", {}).items():
            # Existing missing attachments remain visible and editable; a newly linked file must exist.
            if value == previous.get(kind):
                continue
            path = Path(value)
            if not path.is_absolute() or value.startswith(("\\\\", "//")):
                raise ApiError(400, "请填写本机文件的完整路径。")
            if path.suffix.lower() not in EXTENSIONS[kind] or not path.is_file():
                raise ApiError(400, f"附件不存在或格式不支持：{kind}")

    def save(self, identifier, body):
        with self.lock:
            key = body.get("request_id")
            if not isinstance(key, str) or not re.fullmatch(r"[\w-]{10,100}", key):
                raise ApiError(400, "缺少保存标识，请刷新后重试。")
            request_hash = hashlib.sha256(json.dumps([identifier, body], sort_keys=True).encode()).hexdigest()
            if key in self.requests:
                old_hash, response = self.requests[key]
                if request_hash != old_hash:
                    raise ApiError(409, "保存标识已用于其他修改，请重新保存。")
                if response.get("case") and not self.store.folder(response["case"]["case_id"]).is_dir():
                    del self.requests[key]
                    raise ApiError(404, "这条案例已被删除，旧保存结果不再提供。")
                return response
            current = copy.deepcopy(body.get("current"))
            current_schema(current)
            video_id = str(current.get("metrics", {}).get("video_id", "")).strip()
            link_id = re.search(r"tiktok\.com/@[^/]+/video/(\d+)", str(body.get("url", "")))
            if link_id and video_id and video_id != link_id.group(1):
                raise ApiError(400, "视频 ID 与原视频链接不一致，请核对后保存。")
            video_id = video_id or (link_id.group(1) if link_id else "")
            if video_id:
                current.setdefault("metrics", {})["video_id"] = video_id
                for original_file in self.store.root.glob("cases/PVR-*/original.json"):
                    other_id = original_file.parent.name
                    if other_id == identifier:
                        continue
                    other = self.store.read_case(other_id)
                    other_video = str((other.get("current") or {}).get("metrics", {}).get("video_id", "")).strip()
                    if video_id == other_video:
                        raise ApiError(409, f"视频 ID 已收录到 {other_id}，请打开该案例补充数据；已归档案例也不重复收录。")
            note = body.get("note", "")
            if not isinstance(note, str) or not note.strip():
                raise ApiError(400, "请填写本次修改说明。")
            note = note.strip()
            feedback = {"kind": "editorial", "text": note, "scope": "用户在本地案例工作台保存；更新当前视图，原始记录保留。"}
            existing = None
            source_video = current.get("assets", {}).get("video")
            preserve_missing_video = False
            if identifier:
                existing = self.store.read_case(identifier)
                if body.get("revision") != revision(existing):
                    raise ApiError(409, "这条案例已在其他位置更新。你的编辑仍保留，请先查看最新记录再保存。")
                previous_assets = (existing.get("current") or {}).get("assets", {})
                self.check_assets(current, previous_assets)
                preserve_missing_video = bool(source_video and source_video == previous_assets.get("video") and not Path(source_video).is_file())
                current_sources(current, [event["event_id"] for event in existing["events"]])
            else:
                self.check_assets(current)
                product = body.get("product", "")
                if not isinstance(product, str) or not product.strip():
                    raise ApiError(400, "请填写产品。")
                current.pop("source_event_ids", None)
            direction_catalog = self.directions.sync(self.store.read_index()["cases"])
            self.directions.prepare_case(current, existing.get("product") if existing else product,
                                         existing.get("current") if existing else None, allow_inherit=existing is None)
            current_schema(current)
            # Surface index failure before saving. Once the event commits, never advise repeating it.
            self.store.read_index()
            add_value = None
            if not existing:
                url = body.get("url", "")
                if not isinstance(url, str):
                    raise ApiError(400, "原视频链接格式无效。")
                url = url.strip()
                if not current.get("transcript") and not url and not source_video:
                    raise ApiError(400, "新增案例请提供视频、文案或原视频链接。")
                original = {"source": "用户通过本地案例工作台收录"}
                if current.get("transcript"):
                    original["text"] = current["transcript"]["text"]
                elif source_video:
                    original["text"] = f"用户导入视频：{Path(source_video).name}。尚未提供口播原文。"
                if url:
                    if urlparse(url).scheme not in {"https", "http"}:
                        raise ApiError(400, "原视频链接须以 https:// 或 http:// 开头。")
                    original["url"] = url
                add_value = {"title": current["title"], "product": body["product"].strip(),
                             "original": original, "feedback": feedback, "current": current}
                add_schema(add_value)
                while True:
                    identifier = "PVR-" + uuid4().hex[:12]
                    if not self.store.folder(identifier).exists() and not (self.video_files.cases / identifier).exists():
                        break
            encode(add_value if add_value is not None else current)
            created_video = None
            if source_video and not preserve_missing_video:
                copied, created = self.video_files.copy_for_case(source_video, identifier)
                current["assets"]["video"] = str(copied)
                if created:
                    created_video = copied
            if existing and current == existing.get("current"):
                response = {"case": self.detail(identifier), "unchanged": True, "warning": None}
                self.requests[key] = (request_hash, response)
                return response
            warning = None
            try:
                if existing:
                    result = self.store.append(identifier, {**feedback, "current": current})
                else:
                    result = self.store.add(add_value, identifier=identifier)
            except Exception as exc:
                match = re.search(r"case (PVR-[0-9a-f]{12}) was saved, but index update failed", str(exc))
                if not match:
                    recovered = None
                    if created_video:
                        # A committed record must never lose its video, even if
                        # an unexpected post-commit filesystem failure occurs.
                        try:
                            committed = self.store.read_case(identifier) if self.store.folder(identifier).is_dir() else None
                            snapshots = [committed.get("current"), *(event.get("current") for event in committed["events"])] if committed else []
                            referenced = any((snapshot or {}).get("assets", {}).get("video") == str(created_video) for snapshot in snapshots)
                            if committed and committed.get("current") == current:
                                recovered = committed
                            elif not referenced:
                                self.video_files.remove_created(created_video, identifier)
                        except (OSError, StoreError, VideoFileError):
                            pass  # Retain uncertain copies instead of risking a saved attachment.
                    if recovered is None:
                        raise
                    result = recovered
                    warning = "案例和视频已保存，但后续处理未完成。请点击重新同步，不要重复保存。"
                else:
                    result = self.store.read_case(match.group(1))
                    warning = "案例已保存，但目录未同步。请点击重新同步，不要重复保存。"
            if source_video and created_video:
                try:
                    self.video_files.cleanup_stage(source_video, self.store)
                except (OSError, StoreError, VideoFileError):
                    # A saved independent copy is usable; a retained upload is
                    # harmless and may still be referenced by another case.
                    pass
            if not warning:
                try:
                    render_library(self.store.root, self.library)
                except Exception:
                    warning = "案例已保存，阅读目录生成失败。请点击重新同步。"
            try:
                direction_catalog = self.directions.sync([result])
            except (DirectionError, OSError):
                warning = "案例已保存，但方向目录未同步。请点击重新同步，不要重复保存。"
            response = {"case": self.detail(result["case_id"], direction_catalog=direction_catalog), "warning": warning}
            self.requests[key] = (request_hash, response)
            return response

    def set_archived(self, identifier, body):
        with self.lock:
            key = body.get("request_id")
            if not isinstance(key, str) or not re.fullmatch(r"[\w-]{10,100}", key):
                raise ApiError(400, "缺少操作标识，请刷新后重试。")
            request_hash = hashlib.sha256(json.dumps(["archive", identifier, body], sort_keys=True).encode()).hexdigest()
            if key in self.requests:
                old_hash, response = self.requests[key]
                if request_hash != old_hash:
                    raise ApiError(409, "操作标识已用于其他修改，请重新操作。")
                if response.get("case") and not self.store.folder(response["case"]["case_id"]).is_dir():
                    del self.requests[key]
                    raise ApiError(404, "这条案例已被删除，旧操作结果不再提供。")
                return response
            archived = body.get("archived")
            if type(archived) is not bool:
                raise ApiError(400, "归档状态必须是 true 或 false。")
            note = body.get("note", "用户在案例工作台归档案例。" if archived else "用户在案例工作台恢复案例。")
            if not isinstance(note, str) or not note.strip():
                raise ApiError(400, "请提供归档或恢复说明。")
            self.store.read_index()
            existing = self.store.read_case(identifier)
            if body.get("revision") != revision(existing):
                raise ApiError(409, "这条案例已在其他位置更新，请查看最新记录后再归档或恢复。")
            if existing.get("archived", False) == archived:
                response = {"case": self.detail(identifier), "unchanged": True, "warning": None}
                self.requests[key] = (request_hash, response)
                return response
            warning = None
            try:
                result = self.store.set_archived(identifier, archived, note.strip())
            except StoreError as exc:
                match = re.search(r"case (PVR-[0-9a-f]{12}) was saved, but index update failed", str(exc))
                if not match:
                    raise
                result = self.store.read_case(match.group(1))
                warning = "归档状态已保存，但目录未同步。请点击重新同步，无需重复操作。"
            if not warning:
                try:
                    render_library(self.store.root, self.library)
                except Exception:
                    warning = "归档状态已保存，阅读目录生成失败。请点击重新同步。"
            response = {"case": self.detail(result["case_id"]), "warning": warning}
            self.requests[key] = (request_hash, response)
            return response

    def asset_path(self, identifier, kind, event_id=None):
        case = self.store.read_case(identifier)
        current = case.get("current") or {}
        if event_id:
            event = next((e for e in case["events"] if e["event_id"] == event_id), None)
            if not event:
                raise ApiError(404, "历史记录不存在。")
            current = event.get("current") or {}
        raw = current.get("assets", {}).get(kind)
        if kind not in EXTENSIONS or not raw:
            raise ApiError(404, "这条案例未关联此附件。")
        path = Path(raw)
        if path.suffix.lower() not in EXTENSIONS[kind] or not path.is_file():
            raise ApiError(404, "附件不可用，请重新关联本地文件。")
        return path.resolve()

    def thumbnail_path(self, identifier, event_id=None):
        """Cache one JPEG frame outside the append-only case library."""
        video = self.asset_path(identifier, "video", event_id)
        try:
            stat = video.stat()
        except OSError as exc:
            raise ApiError(404, "视频暂不可用。") from exc
        identity = f"{identifier}\0{event_id or ''}\0{video}\0{stat.st_size}\0{stat.st_mtime_ns}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        cache_root = self.runtime / "thumbnail-cache"
        case_cache = cache_root / identifier
        thumbnail = case_cache / f"{digest}.jpg"
        if (cache_root.is_symlink() or case_cache.is_symlink()
                or cache_root.resolve().parent != self.runtime
                or case_cache.resolve().parent != cache_root.resolve()):
            raise ApiError(404, "视频缩略图暂不可用。")
        if thumbnail.is_file() and not thumbnail.is_symlink():
            return thumbnail
        # Requests for visible cards may arrive together. Generate each image
        # once and only expose a complete JPEG to readers.
        with self.thumbnail_lock:
            if thumbnail.is_file() and not thumbnail.is_symlink():
                return thumbnail
            try:
                case_cache.mkdir(parents=True, exist_ok=True)
                if case_cache.is_symlink():
                    raise ApiError(404, "视频缩略图暂不可用。")
                if not self.store.folder(identifier).exists():
                    raise ApiError(404, "案例已不存在。")
                if thumbnail.is_symlink():
                    thumbnail.unlink()
            except OSError as exc:
                raise ApiError(404, "视频缩略图暂不可用。") from exc
            temporary = thumbnail.with_name(f"{digest}-{uuid4().hex}.jpg")
            try:
                for moment in ("0.15", "0"):
                    result = subprocess.run(
                        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                         "-i", str(video), "-ss", moment, "-frames:v", "1", "-an", "-sn", "-dn",
                         "-vf", "scale=640:640:force_original_aspect_ratio=decrease",
                         "-q:v", "4", str(temporary)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=20,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                    if result.returncode == 0 and temporary.is_file() and temporary.stat().st_size > 4:
                        with temporary.open("rb") as stream:
                            is_jpeg = stream.read(3) == b"\xff\xd8\xff"
                        if is_jpeg:
                            temporary.replace(thumbnail)
                            return thumbnail
                    temporary.unlink(missing_ok=True)
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                temporary.unlink(missing_ok=True)
        raise ApiError(404, "视频缩略图暂不可用。")

    def clear_thumbnail_cache(self, identifier):
        """Remove only this case's generated JPEGs after permanent deletion."""
        if not isinstance(identifier, str) or not re.fullmatch(r"PVR-[0-9a-f]{12}", identifier):
            return
        cache_root = self.runtime / "thumbnail-cache"
        case_cache = cache_root / identifier
        if cache_root.is_symlink() or cache_root.resolve().parent != self.runtime:
            return
        with self.thumbnail_lock:
            if case_cache.is_symlink():
                case_cache.unlink()
                return
            if not case_cache.is_dir() or case_cache.resolve().parent != cache_root.resolve():
                return
            for item in case_cache.iterdir():
                if item.suffix == ".jpg" and (item.is_file() or item.is_symlink()):
                    item.unlink()
            case_cache.rmdir()

    def deletion_preview(self, identifier, include_source=False):
        with self.lock:
            if self.ai.active_for_case(identifier):
                raise ApiError(409, "此案例的 AI 协作仍在处理中，请先取消或等待完成，再查看删除清单。")
            try:
                preview = self.deletion.preview(identifier, include_source=include_source)
                preview["related_creation_note"] = "同时清理素材篮和独立任务中的此案例来源快照；已采纳的独立文案与发布记录保留。"
                return preview
            except DeletionError as exc:
                raise ApiError(exc.status, str(exc)) from exc

    def delete_case(self, identifier, body):
        with self.lock:
            key = body.get("request_id")
            if not isinstance(key, str) or not re.fullmatch(r"[\w-]{10,100}", key):
                raise ApiError(400, "缺少删除操作标识，请重新查看删除清单。")
            include_source = body.get("include_source", False)
            if type(include_source) is not bool:
                raise ApiError(400, "来源删除选项必须是 true 或 false。")
            request_hash = hashlib.sha256(json.dumps(["delete", identifier, body], sort_keys=True).encode()).hexdigest()
            if key in self.requests:
                old_hash, response = self.requests[key]
                if request_hash != old_hash:
                    raise ApiError(409, "操作标识已用于其他修改，请重新查看删除清单。")
                return response
            if self.ai.active_for_case(identifier):
                raise ApiError(409, "此案例的 AI 协作仍在处理中，请先取消或等待完成，再删除。")
            try:
                response = self.deletion.delete(identifier, revision=body.get("revision"),
                    plan_token=body.get("plan_token"), confirmation=body.get("confirmation"), include_source=include_source)
            except DeletionError as exc:
                raise ApiError(exc.status, str(exc)) from exc
            finally:
                if not self.store.folder(identifier).exists():
                    self.requests = {request: value for request, value in self.requests.items()
                        if value[1].get("case", {}).get("case_id") != identifier}
                    self.directions.forget_case(identifier)
                    self.activity.delete_case(identifier)
                    self.cleanup_creation_case(identifier)
                    self.state_cache = None
            # Do not keep deleted case bodies in old save/archive replay caches.
            if response.get("deleted"):
                self.requests = {request: value for request, value in self.requests.items()
                    if value[1].get("case", {}).get("case_id") != identifier}
                try:
                    self.clear_thumbnail_cache(identifier)
                except OSError:
                    response["warning"] = "案例已删除，但缩略图缓存未清理；请检查本机运行目录。"
            self.requests[key] = (request_hash, response)
            return response

    def open_location(self, body):
        if not self.allow_open:
            raise ApiError(403, "测试模式未启用系统文件操作。")
        target = body.get("target")
        if target in {"records", "evidence", "library", "directions", "creation"}:
            path = {"records": self.store.root / "cases", "evidence": self.runtime / "evidence", "library": self.library,
                    "directions": self.directions.root, "creation": self.creation.root}[target]
        elif target == "case":
            path = self.store.folder(body.get("case_id"))
        elif target == "asset":
            path = self.asset_path(body.get("case_id"), body.get("kind"), body.get("event_id"))
        elif target == "source":
            source = (self.store.read_case(body.get("case_id")).get("current") or {}).get("source_case")
            if not source:
                raise ApiError(404, "这条案例没有关联旧库来源。")
            path = Path(source["file_path"])
        else:
            raise ApiError(400, "未知文件位置。")
        if not path.exists():
            raise ApiError(404, "文件或目录不存在。")
        # Only reveal a registered file in Explorer; never execute files supplied by the browser.
        if os.name == "nt":
            subprocess.Popen(["explorer.exe", "/select,", str(path)] if path.is_file() else ["explorer.exe", str(path)])
        else:
            raise ApiError(400, "当前系统不支持打开文件夹。")
        return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    server_version = "CaseWorkbench/1.0"
    protocol_version = "HTTP/1.1"
    # The authenticated proxy reuses connections. Flush small header/body writes
    # promptly instead of waiting for Nagle plus the peer's delayed ACK.
    disable_nagle_algorithm = True

    @property
    def app(self):
        return self.server.app

    # Page polling hits these every few seconds; successful polls would bury
    # real requests and errors in server.log.
    QUIET_POLLS = ("/api/state", "/api/ai/tasks", "/api/ai/status", "/api/ai/claude/login", "/api/activity")

    def log_request(self, code="-", size="-"):
        path = urlparse(getattr(self, "path", "") or "").path
        if self.command == "GET" and str(code).startswith(("2", "3")) and path.startswith(self.QUIET_POLLS):
            return
        super().log_request(code, size)

    def headers_common(self, cache_control="no-store"):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")

    def output(self, status, content, content_type="application/json; charset=utf-8"):
        if not isinstance(content, bytes):
            content = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        json_response = content_type.startswith("application/json")
        compressed = json_response and len(content) >= 1024 and accepts_gzip(self.headers.get("Accept-Encoding", ""))
        if compressed:
            content = gzip.compress(content, compresslevel=1, mtime=0)
        self.send_response(status)
        self.headers_common()
        self.send_header("Content-Type", content_type)
        if json_response:
            self.send_header("Vary", "Accept-Encoding")
        if compressed:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(content)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def guard(self, write=False):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            raise ApiError(403, "仅允许通过本机地址访问。")
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in hosts}:
            raise ApiError(403, "请在本地工作台中操作。")
        # Opening the local app from another tab/app is a cross-site document
        # navigation in Chrome. Admit only the landing document; API requests,
        # embedded frames, other resources and writes retain the cross-site guard.
        landing_navigation = (self.command in {"GET", "HEAD"}
                              and self.headers.get("Sec-Fetch-Mode") == "navigate"
                              and self.headers.get("Sec-Fetch-Dest") == "document"
                              and urlparse(self.path).path in {"/", "/index.html"})
        if self.headers.get("Sec-Fetch-Site") == "cross-site" and not landing_navigation:
            raise ApiError(403, "不接受跨站请求。")
        if write and not secrets.compare_digest(self.headers.get("X-Workbench-Token", ""), self.app.token):
            raise ApiError(403, "页面连接已过期，请刷新后重试。")

    def extension_guard(self):
        """The TK 看板 extension posts from its own origin with the pairing key shown in settings."""
        port = self.server.server_port
        if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            raise ApiError(403, "仅允许通过本机地址访问。")
        if not (self.headers.get("Origin") or "").startswith("chrome-extension://"):
            raise ApiError(403, "只接受 TK 看板插件的推送。")
        if not self.app.public_stats.check_key(self.headers.get("X-TKL-Key", "")):
            raise ApiError(403, "连接码不对，请在工作台「设置 → TK 看板插件」复制最新连接码。")

    def dispatch(self):
        post_admitted = False
        post_body_unread = self.command == "POST"
        try:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if self.command == "POST" and path in EXTENSION_PATHS:
                self.extension_guard()
            else:
                self.guard(self.command == "POST")
            query = parse_qs(parsed.query)
            if path == "/api/remote-account" and (self.client_address[0] not in {"127.0.0.1", "::1"}
                    or any(self.headers.get(name) for name in ("Forwarded", "X-Forwarded-For", "CF-Connecting-IP"))):
                raise ApiError(403, "账号密码只能在运行工作台的电脑上设置。")
            if self.command == "POST" and path not in {"/api/service/shutdown", "/api/service/restart"}:
                self.server.begin_write()
                post_admitted = True
            if self.command in {"GET", "HEAD"}:
                if (self.server.data_maintenance and self.server.transfer_operation() == "restore"
                        and path.startswith("/api/") and path not in {"/api/data/status", "/api/service/status"}):
                    raise self.server.transfer_busy_error()
                if path == "/api/data/status":
                    return self.output(200, self.app.data_transfer.status())
                if path == "/api/remote-account":
                    with self.app.lock:
                        return self.output(200, RemoteAccounts(self.app.runtime).metadata())
                if path == "/api/activity":
                    accepted = {"after", "limit", "before", "product", "filter", "owner_id", "source_id", "kind", "q", "group", "since"}
                    activity_query = parse_qs(parsed.query, keep_blank_values=True)
                    if activity_query.keys() - accepted or any(len(values) != 1 for values in activity_query.values()):
                        raise ApiError(400, "运行记录筛选参数无效。")
                    refresh_activity_projection(self.app)
                    return self.output(200, self.app.activity.feed(**{key: values[0] for key, values in activity_query.items()}))
                activity_logs = re.fullmatch(r"/api/activity/([A-Za-z0-9_:.\-]{1,300})/logs", path)
                if activity_logs:
                    if query.keys() - {"after_seq", "limit", "before_seq", "tail"} or any(len(values) != 1 for values in query.values()):
                        raise ApiError(400, "运行日志分页参数无效。")
                    return self.output(200, self.app.activity.logs(activity_logs.group(1), **{key: values[0] for key, values in query.items()}))
                download = re.fullmatch(r"/api/data/download/([A-Za-z0-9_-]{32})", path)
                if download:
                    return self.file(self.app.data_transfer.download(download.group(1)), download=True)
                if path == "/api/service/status":
                    return self.output(200, {"app": "video-case-workbench", "pid": os.getpid(),
                        "token": self.app.token, "shutdown_supported": True, "shutting_down": self.server.shutting_down,
                        "code_stale": code_signature() > CODE_AT_START,
                        "paths": {"records": str(self.app.store.root / "cases"), "library": str(self.app.library)}})
                if path == "/api/settings/paths":
                    # Settings can be opened directly, before the case catalogue has loaded.
                    return self.output(200, {"paths": {
                        "records": str(self.app.store.root / "cases"),
                        "directions": str(self.app.directions.root),
                        "creation": str(self.app.creation.root),
                        "evidence": str(self.app.runtime / "evidence"),
                        "library": str(self.app.library)}})
                if path == "/api/state":
                    snapshot = self.app.state()
                    if query.get("since", [None])[0] == snapshot["digest"]:
                        return self.output(200, {key: snapshot[key] for key in ("app", "token", "signature", "digest")} | {"unchanged": True})
                    return self.output(200, snapshot)
                if path == "/api/products":
                    return self.output(200, self.app.products())
                if path == "/api/ai/status":
                    return self.output(200, self.app.ai.status())
                if path == "/api/ai/claude/login":
                    self.guard(write=True)
                    return self.output(200, self.app.claude_login_state())
                if path == "/api/creation":
                    return self.output(200, self.app.creation_catalog(query.get("product", [None])[0]))
                if path == "/api/content":
                    return self.output(200, self.app.content_catalog(query))
                if path == "/api/content/views":
                    return self.output(200, self.app.content_views_catalog(query))
                if path == "/api/campaigns":
                    if query:
                        raise ApiError(400, "活动日历不接受查询参数。")
                    return self.output(200, self.app.campaigns.public())
                if path == "/api/content/calendar":
                    return self.output(200, self.app.content_calendar(query))
                if path == "/api/review-followups":
                    return self.output(200, self.app.review_followups.public(query.get("product", [None])[0]))
                if path == "/api/review-experiences":
                    return self.output(200, self.app.review_experience_catalog(query))
                if path == "/api/feedback-chain":
                    return self.output(200, self.app.feedback_chain(query.get("video_id", [""])[0], query.get("product", [None])[0], query.get("window_end", [None])[0]))
                comparison = re.fullmatch(r"/api/plans/(PLN-[0-9a-f]{12})/comparison", path)
                if comparison:
                    from experiment_cards import compare_experiment
                    plan = next((item for item in self.app.creation.public()["plans"] if item["id"] == comparison[1]), None)
                    if not plan:
                        raise ApiError(404, "测试卡不存在。")
                    with self.app.performance.lock:
                        comparison_result = compare_experiment(plan, self.app.performance.video)
                    return self.output(200, comparison_result)
                if path == "/api/materials":
                    return self.output(200, self.app.material_catalog(query.get("product", [None])[0]))
                if path == "/api/production":
                    return self.output(200, self.app.production_catalog(query.get("product", [None])[0]))
                publication_download = re.fullmatch(r"/api/production/jobs/(PRD-[0-9a-f]{12})/publication/(VPU-[0-9a-f]{12})/download", path)
                if publication_download:
                    return self.file(self.app.video_publication_file(*publication_download.groups()), download=True)
                video_publication = re.fullmatch(r"/api/production/jobs/(PRD-[0-9a-f]{12})/publication", path)
                if video_publication:
                    return self.output(200, self.app.video_publication_detail(video_publication[1]))
                prepublish_sheet = re.fullmatch(r"/api/production/jobs/(PRD-[0-9a-f]{12})/prepublish/(PPR-[0-9a-f]{12})/sheets/((?:opening|detail\d{2}|shots\d{1,2})\.jpg)", path)
                if prepublish_sheet:
                    return self.file(self.app.prepublish_sheet(*prepublish_sheet.groups()))
                prepublish = re.fullmatch(r"/api/production/jobs/(PRD-[0-9a-f]{12})/prepublish", path)
                if prepublish:
                    return self.output(200, self.app.prepublish_reviews(prepublish[1]))
                video_carousel = re.fullmatch(r"/api/production/jobs/(PRD-[0-9a-f]{12})/video-carousel", path)
                if video_carousel:
                    return self.output(200, self.app.video_carousel_status(video_carousel[1]))
                if path == "/api/production/deliveries":
                    return self.output(200, self.app.production_deliveries(query.get("product", [None])[0]))
                if path == "/api/production/eleven":
                    return self.output(200, self.app.eleven_status())
                if path == "/api/production/resolve":
                    return self.output(200, self.app.resolve_status())
                if path == "/api/production/still-search":
                    return self.output(200, self.app.still_search_status())
                if path == "/api/production/edit-clients":
                    return self.output(200, self.app.edit_client_status())
                if path == "/api/production/codex-images":
                    return self.output(200, self.app.image_status())
                if path == "/api/production/variants":
                    task_id, round_id = query.get("task_id", [""]), query.get("round_id", [""])
                    if len(task_id) != 1 or len(round_id) != 1 or not re.fullmatch(r"AIT-[0-9a-f]{12}", task_id[0]) or not re.fullmatch(r"AIR-[0-9a-f]{12}", round_id[0]):
                        raise ApiError(400, "候选轮次参数无效。")
                    return self.output(200, self.app.production_variants(task_id[0], round_id[0]))
                if path == "/api/production/carousel-variants":
                    task_id, round_id = query.get("task_id", [""]), query.get("round_id", [""])
                    if len(task_id) != 1 or len(round_id) != 1 or not re.fullmatch(r"AIT-[0-9a-f]{12}", task_id[0]) or not re.fullmatch(r"AIR-[0-9a-f]{12}", round_id[0]):
                        raise ApiError(400, "候选轮次参数无效。")
                    return self.output(200, self.app.carousel_variants(task_id[0], round_id[0]))
                if path == "/api/production/carousel-tables":
                    script_id = query.get("script_id", [""])
                    if len(script_id) != 1 or not re.fullmatch(r"SCR-[0-9a-f]{12}", script_id[0]):
                        raise ApiError(400, "脚本版本参数无效。")
                    return self.output(200, self.app.carousel_tables(script_id[0]))
                table_read = re.fullmatch(r"/api/production/carousel-tables/(TBL-[0-9a-f]{12})", path)
                if table_read:
                    return self.output(200, {"table": self.app.carousel_table(table_read.group(1))})
                delivery_download = re.fullmatch(r"/api/production/carousels/(CAR-[0-9a-f]{12})/deliveries/(DLV-[0-9a-f]{12})/download", path)
                if delivery_download:
                    downloaded = self.file(self.app.carousel_delivery_file(*delivery_download.groups()), download=True)
                    if downloaded:
                        self.app.collect_carousel_delivery(*delivery_download.groups())
                    return
                carousel_output = re.fullmatch(r"/api/production/carousels/(CAR-[0-9a-f]{12})/outputs/(OUT-[0-9a-f]{12})", path)
                if carousel_output:
                    size = query.get("size", ["thumb"])[0]
                    return self.file(self.app.carousel_output(*carousel_output.groups(), "full" if size == "full" else "thumb"))
                carousel_image = re.fullmatch(r"/api/production/carousels/(CAR-[0-9a-f]{12})/images/(CND-[0-9a-f]{12})", path)
                if carousel_image:
                    size = query.get("size", ["thumb"])[0]
                    return self.file(self.app.carousel_image(*carousel_image.groups(), "full" if size == "full" else "thumb"))
                clip_frame = re.fullmatch(r"/api/production/carousels/(CAR-[0-9a-f]{12})/clips/([A-Za-z0-9]+-[0-9a-f]{8})/frame", path)
                if clip_frame:
                    frame, size = query.get("frame", [""]), query.get("size", ["view"])
                    if len(frame) != 1 or len(size) != 1:
                        raise ApiError(400, "帧参数无效。")
                    return self.file(self.app.clip_frame(clip_frame.group(1), clip_frame.group(2), frame[0], size[0]))
                clip_strip = re.fullmatch(r"/api/production/carousels/(CAR-[0-9a-f]{12})/clips/([A-Za-z0-9]+-[0-9a-f]{8})/strip", path)
                if clip_strip:
                    return self.file(self.app.clip_strip(clip_strip.group(1), clip_strip.group(2)))
                clip_info = re.fullmatch(r"/api/production/carousels/(CAR-[0-9a-f]{12})/clips/([A-Za-z0-9]+-[0-9a-f]{8})", path)
                if clip_info:
                    self.app.production.carousel(clip_info.group(1))
                    return self.output(200, self.app.clip_info(clip_info.group(2)))
                clip_list = re.fullmatch(r"/api/production/carousels/(CAR-[0-9a-f]{12})/clips", path)
                if clip_list:
                    return self.output(200, self.app.carousel_clips(clip_list.group(1), query.get("materials", []),
                                                                    query.get("refresh", [""])[0] == "1"))
                folder_list = re.fullmatch(r"/api/production/carousels/(CAR-[0-9a-f]{12})/folder-images", path)
                if folder_list:
                    return self.output(200, self.app.folder_images(folder_list.group(1)))
                if path == "/api/production/folder-thumb":
                    selected = query.get("path", [""])
                    if len(selected) != 1:
                        raise ApiError(400, "图片路径无效。")
                    return self.file(self.app.folder_thumb(selected[0]))
                carousel_match = re.fullmatch(r"/api/production/carousels/(CAR-[0-9a-f]{12})", path)
                if carousel_match:
                    return self.output(200, self.app.carousel_detail(carousel_match.group(1)))
                carousel_preflight = re.fullmatch(r"/api/production/carousels/(CAR-[0-9a-f]{12})/preflight", path)
                if carousel_preflight:
                    return self.output(200, self.app.carousel_delivery_preflight(carousel_preflight.group(1)))
                job_match = re.fullmatch(r"/api/production/jobs/(PRD-[0-9a-f]{12})", path)
                if job_match:
                    return self.output(200, self.app.production_job(job_match.group(1)))
                audio_match = re.fullmatch(r"/api/production/audio/(PRD-[0-9a-f]{12})/(TAK-[0-9a-f]{12})/(mp3|wav)", path)
                if audio_match:
                    return self.file(self.app.production_audio(*audio_match.groups()), download="download" in query)
                if path == "/api/variant-links/source":
                    selected = [query.get(key, []) for key in ("kind", "id", "product")]
                    if any(len(values) != 1 for values in selected):
                        raise ApiError(400, "历史记录类型、编号与产品须各填写一次。")
                    return self.output(200, self.app.variant_link_source(*(values[0] for values in selected)))
                script_match = re.fullmatch(r"/api/scripts/(SCR-[0-9a-f]{12})", path)
                if script_match:
                    return self.output(200, self.app.script_detail(script_match.group(1)))
                if path.startswith("/api/direction-progress/"):
                    selected = query.get("product", [None])
                    if len(selected) != 1:
                        raise ApiError(400, "方向产品筛选只能出现一次。")
                    return self.output(200, self.app.direction_progress(path.removeprefix("/api/direction-progress/"), selected[0]))
                if path == "/api/direction-catalog" or path.startswith("/api/direction-catalog/"):
                    selected = query.get("product", [None])
                    if len(selected) != 1:
                        raise ApiError(400, "方向产品筛选只能出现一次。")
                    if path == "/api/direction-catalog":
                        return self.output(200, self.app.direction_catalog(selected[0]))
                    views = query.get("view", ["full"])
                    if len(views) != 1:
                        raise ApiError(400, "方向详情显示范围只能出现一次。")
                    include_tasks = parse_qs(parsed.query, keep_blank_values=True).get("include_tasks", ["1"])
                    if len(include_tasks) != 1 or include_tasks[0] not in {"0", "1"}:
                        raise ApiError(400, "方向详情任务显示范围须是单个 0 或 1。")
                    return self.output(200, self.app.direction_detail(path.removeprefix("/api/direction-catalog/"), selected[0],
                        view=views[0], include_tasks=include_tasks[0] == "1"))
                if path == "/api/tk-candidates":
                    if query.keys() - {"product", "status"} or any(len(values) != 1 for values in query.values()):
                        raise ApiError(400, "候选方向筛选格式无效。")
                    return self.output(200, self.app.tk_candidate_list(query.get("product", [None])[0], query.get("status", [None])[0]))
                if path == "/api/directions":
                    values = {}
                    for key, default in (("product", None), ("direction_id", None), ("archive", "active")):
                        selected = query.get(key, [default])
                        if len(selected) != 1:
                            raise ApiError(400, "测试方向筛选格式无效。")
                        values[key] = selected[0]
                    with self.app.lock:
                        return self.output(200, self.app.direction_summary(**values))
                if path == "/api/ai/options":
                    return self.output(200, self.app.ai.options(refresh=query.get("refresh") == ["1"]))
                if path == "/api/ai/preferences":
                    return self.output(200, {"preferences": self.app.ai.preferences()})
                ai_stream = re.fullmatch(r"/api/ai/tasks/(AIT-[0-9a-f]{12})/stream", path)
                if ai_stream:
                    return self.event_stream(ai_stream.group(1))
                if path == "/api/ai/tasks":
                    values = {}
                    task_query = parse_qs(parsed.query, keep_blank_values=True)
                    for name in ("case_id", "product", "direction_id"):
                        selected = (query if name == "case_id" else task_query).get(name, [None])
                        if len(selected) != 1:
                            raise ApiError(400, "AI 任务筛选格式无效。")
                        values[name] = selected[0]
                    return self.output(200, {"tasks": self.app.ai.list_tasks(values["case_id"],
                        product=values["product"], direction_id=values["direction_id"])})
                ai_match = re.fullmatch(r"/api/ai/tasks/(AIT-[0-9a-f]{12})", path)
                if ai_match:
                    return self.output(200, {"task": self.app.ai.get(ai_match.group(1))})
                shops = query.get("shop", [DEFAULT_SHOP])
                if path.startswith("/api/performance/") and (len(shops) != 1 or (shops[0] not in SHOPS and shops[0] != "all")):
                    raise ApiError(400, "店铺无效。")
                # "all" reads every shop together, for the places that look a video up wherever it was posted.
                shop = None if shops[0] == "all" else shops[0]
                if path == "/api/performance/summary":
                    return self.output(200, self.app.performance.summary(shop))
                if path == "/api/public-stats/link":
                    return self.output(200, {"key": self.app.public_stats.link_key(),
                                             "address": f"http://127.0.0.1:{self.server.server_port}"})
                if path == "/api/performance/analytics":
                    return self.output(200, self.app.performance.analytics(shop))
                if path == "/api/performance/creators":
                    return self.output(200, self.app.performance.creators(self.app.video_case_links(), shop))
                if path == "/api/performance/photos":
                    return self.output(200, self.app.performance.photos(shop))
                if path == "/api/performance/photo-items":
                    return self.output(200, {**self.app.performance.photo_items(shop), "productions": self.app.carousel_post_links()})
                if path == "/api/performance/videos":
                    projection = query.get("projection", ["full"])
                    if len(projection) != 1 or projection[0] not in {"full", "home"}:
                        raise ApiError(400, "视频列表显示范围无效。")
                    if projection[0] == "home":
                        selected = query.get("product", [""])
                        if len(selected) != 1:
                            raise ApiError(400, "视频产品筛选只能出现一次。")
                        product = "" if selected[0].strip() == "all" else product_key(selected[0])
                        # Home needs every matching video's publication/window status,
                        # but no case mapping, comment aggregation or sales-table columns.
                        videos = self.app.review_videos()
                        return self.output(200, {"published_video_ids": [video["video_id"] for video in videos], "videos": [
                            {key: video[key] for key in HOME_VIDEO_FIELDS if key in video}
                            for video in videos
                            if not product or product_key(video.get("product")).lower() == product.lower()
                        ]})
                    videos = self.app.annotate_review_status(self.app.performance.videos(self.app.video_case_links(), shop))
                    commented = self.app.reviews.video_counts()
                    for video in videos:
                        found = commented.get(video["video_id"])
                        if found:
                            video["review_comments"], video["review_open"] = found["comments"], found["open_questions"]
                    return self.output(200, {"videos": videos})
                public_export = re.fullmatch(r"/api/performance/public-export/(\d{8}-\d{6}-[0-9a-f]{6})", path)
                if public_export:
                    target = self.app.public_exports / f"公开播放-{public_export.group(1)}.xlsx"
                    if not target.is_file():
                        raise ApiError(404, "导出文件不存在，请重新导出。")
                    return self.file(target, download=True)
                video_match = re.fullmatch(r"/api/performance/videos/(\d{6,25})", path)
                if video_match:
                    video = self.app.performance.video(video_match.group(1), self.app.video_case_links())
                    if not video:
                        raise ApiError(404, "后台数据里没有这条视频。")
                    comments = self.app.reviews.video_counts().get(video["video_id"])
                    if comments:
                        video["review_comments"], video["review_open"] = comments["comments"], comments["open_questions"]
                    return self.output(200, {"video": self.app.annotate_review_status([video])[0]})
                if path == "/api/reviews/opportunities":
                    from review_opportunities import opportunity_catalog
                    try:
                        limit, offset = int(query.get("limit", ["20"])[0]), int(query.get("offset", ["0"])[0])
                    except ValueError:
                        raise ApiError(400, "选题分页参数无效。")
                    if not 1 <= limit <= 100 or offset < 0:
                        raise ApiError(400, "选题分页参数无效。")
                    return self.output(200, opportunity_catalog(self.app, query.get("product", [None])[0], limit=limit, offset=offset))
                if path == "/api/reviews/summary":
                    self.app.reviews.sync()  # cheap: rebuilds only when a source file changed
                    summary = self.app.reviews.summary()
                    context = self.app.video_context(v["video_id"] for v in summary["videos"])
                    for video in summary["videos"]:
                        # Review counts describe the imported review library; sales
                        # exports may contain a different comments metric.
                        for key, value in context.get(video["video_id"], {}).items():
                            video.setdefault(key, value)
                    return self.output(200, summary)
                if path == "/api/reviews":
                    self.app.reviews.sync()
                    request = review_request(query)
                    result = self.app.reviews.search(*request)
                    shown = {r["source_ref"] for r in result["reviews"] if r["platform"] == "tiktok_video" and r["source_ref"]} | {request[0].get("video")}
                    result["videos"] = self.app.video_context(shown)
                    return self.output(200, result)
                if path == "/api/reviews/picks":
                    return self.output(200, {"picks": self.app.reviews.picks()})
                if path == "/api/reviews/tag-edits":
                    return self.output(200, {"edits": self.app.reviews.tag_edits()})
                review_match = re.fullmatch(r"/api/reviews/([0-9a-f]{16})", path)
                if review_match:
                    review = self.app.reviews.review(review_match.group(1))
                    if not review:
                        raise ApiError(404, "评论库里没有这条评论。")
                    return self.output(200, {"review": review})
                preview_match = re.fullmatch(r"/api/cases/(PVR-[0-9a-f]{12})/delete-preview", path)
                if preview_match:
                    option = query.get("include_source", ["false"])
                    if len(option) != 1 or option[0] not in {"true", "false"}:
                        raise ApiError(400, "来源删除选项无效。")
                    return self.output(200, self.app.deletion_preview(preview_match.group(1), option[0] == "true"))
                insights_read = re.fullmatch(r"/api/cases/(PVR-[0-9a-f]{12})/insights", path)
                if insights_read:
                    try:
                        return self.output(200, {"insights": self.app.auto_breakdown.insights(insights_read.group(1))})
                    except StoreError as exc:
                        raise ApiError(404, str(exc))
                if path == "/api/case-tags/stats":
                    product = (query.get("product") or [""])[0]
                    return self.output(200, {"stats": tag_stats(self.app.store, self.app.auto_breakdown.tags.read(),
                                                                self.app.auto_breakdown._videos(), product), "product": product})
                auto_read = re.fullmatch(r"/api/cases/(PVR-[0-9a-f]{12})/auto-breakdown", path)
                if auto_read:
                    return self.output(200, {"job": self.app.auto_breakdown.status(auto_read.group(1))})
                if path.startswith("/api/cases/"):
                    return self.output(200, self.app.detail(path.split("/")[-1]))
                if path.startswith("/media/"):
                    parts = path.strip("/").split("/")
                    if len(parts) != 3:
                        raise ApiError(404, "附件不存在。")
                    if parts[2] == "thumbnail":
                        events = query.get("event", [None])
                        if len(events) != 1:
                            raise ApiError(400, "视频修订编号无效。")
                        return self.file(self.app.thumbnail_path(parts[1], events[0]))
                    file = self.app.asset_path(parts[1], parts[2], query.get("event", [None])[0])
                    return self.file(file, download="download" in query)
                files = {"/": "index.html", "/app.js": "app.js", "/navigation.js": "navigation.js", "/library.js": "library.js", "/bulk.js": "bulk.js", "/detail.js": "detail.js", "/transcript-sync.js": "transcript-sync.js", "/delete.js": "delete.js", "/directions.js": "directions.js", "/editor.js": "editor.js", "/native.js": "native.js", "/ai.js": "ai.js", "/performance.js": "performance.js", "/perf-dashboard.js": "perf-dashboard.js", "/perf-date-picker.js": "perf-date-picker.js", "/perf-creators.js": "perf-creators.js", "/reviews.js": "reviews.js", "/style.css": "style.css", "/favicon.svg": "favicon.svg"}
                files.update({"/" + name: name for name in ("shell.js", "shell.css", "settings.js", "settings.css", "global-search.js", "global-search.css", "navigation.css", "library.css", "detail.css", "inline-editor.js", "inline-editor.css", "direction-picker.js", "direction-picker.css", "home.js", "home.css", "material-basket.js", "material-basket.css", "scripts.js", "scripts.css", "production.js", "production.css", "writing.css", "reviews-layout.css", "demo.js")})
                files.update({"/" + name: name for name in ("review-followups.js", "review-opportunities.js", "review-opportunities.css", "video-publication.js", "video-publication.css", "copy-id.js", "drawer-resize.js", "campaigns.js", "campaigns.css", "prepublish-review.js", "prepublish-review.css", "auto-breakdown.js", "auto-breakdown.css", "content-flow.js", "content-flow.css", "workflow-links.js", "workflow-links.css", "production-needs.js", "production-needs.css", "feedback-chain.js", "feedback-chain.css", "delivery-provenance.js", "delivery-provenance.css")})
                files.update({"/review-experiences.js": "review-experiences.js", "/review-experiences.css": "review-experiences.css"})
                files.update({"/index.html": "index.html", "/remote-session.js": "remote-session.js", "/remote-session.css": "remote-session.css",
                              "/tk-candidates.js": "tk-candidates.js", "/tk-candidates.css": "tk-candidates.css"})
                files.update({f"/caption-styles/{name}.png": f"caption-styles/{name}.png"
                              for name in ("yellow_impact", "orange_depth", "red_comic", "lime_pop")})
                if path in files:
                    return self.file(APP_ROOT / "static" / files[path], cache=True, immutable=path != "/" and "v" in query)
                if path in {"/activity.js", "/activity.css"}:
                    return self.file(APP_ROOT / "static" / path[1:], cache=True, immutable="v" in query)
                if path == "/directions.css":
                    return self.file(APP_ROOT / "static" / "directions.css", cache=True, immutable="v" in query)
                raise ApiError(404, "页面不存在。")
            if self.command == "POST":
                if path == "/api/upload":
                    post_body_unread = False  # upload handles its own bounded rejection drain.
                    return self.upload(query)
                carousel_upload = re.fullmatch(r"/api/production/carousels/(CAR-[0-9a-f]{12})/upload", path)
                if carousel_upload:
                    if self.app.autopilot_running(carousel_upload.group(1)):
                        raise VoiceError(409, "AI 正在全程制作这组图文，请先停止后再添加图片。")
                    post_body_unread = False  # the upload handler reads or drains its own body.
                    return self.carousel_upload(carousel_upload.group(1), query)
                if path == "/api/data/import/inspect":
                    try:
                        length = int(self.headers.get("Content-Length", "-1"))
                    except ValueError:
                        raise ApiError(400, "备份包长度无效。")
                    if not 0 < length <= MAX_BYTES + 1024**3:
                        raise ApiError(413, "备份包为空或超过 2 TB。")
                    self.connection.settimeout(300)
                    post_body_unread = False
                    return self.output(200, self.app.data_transfer.inspect_upload(self.rfile, length))
                length = self.content_length(4 * 1024 * 1024)
                try:
                    raw_body = self.rfile.read(length)
                    post_body_unread = False
                    body = json.loads(raw_body)
                except (ValueError, UnicodeError):
                    raise ApiError(400, "提交内容不是有效数据。")
                if not isinstance(body, dict):
                    raise ApiError(400, "提交内容格式错误。")
                if path == "/api/remote-account":
                    with self.app.lock:
                        return self.output(200, RemoteAccounts(self.app.runtime).save(body))
                if path == "/api/public-stats/ping":
                    return self.output(200, {"ok": True, "app": "video-case-workbench"})
                if path == "/api/public-stats/push":
                    return self.output(200, self.app.push_public_stats(body))
                if path == "/api/public-stats/comments":
                    return self.output(200, self.app.push_public_comments(body))
                if path == "/api/public-stats/products":
                    return self.output(200, self.app.extension_products(body))
                if path == "/api/public-stats/product-brief":
                    return self.output(200, self.app.extension_product_brief(body))
                if path == "/api/public-stats/direction-candidates":
                    return self.output(200, self.app.extension_save_candidates(body))
                if path == "/api/public-stats/reference-status":
                    return self.output(200, self.app.extension_reference_status(body))
                if path == "/api/public-stats/direction-produce":
                    return self.output(200, self.app.extension_produce_candidate(body))
                if path == "/api/tk-candidates/produce":
                    return self.output(200, self.app.tk_candidate_produce(body))
                if path == "/api/tk-candidates/status":
                    return self.output(200, self.app.tk_candidate_status(body))
                if path == "/api/tk-candidates/associate":
                    return self.output(200, self.app.tk_candidate_associate(body))
                if path == "/api/public-stats/link/rotate":
                    if body:
                        raise ApiError(400, "重置只接受空对象。")
                    return self.output(200, {"key": self.app.public_stats.rotate_key()})
                if path == "/api/activity/read":
                    return self.output(200, self.app.activity.read(body))
                if path == "/api/activity/notify-claim":
                    return self.output(200, self.app.activity.claim(body))
                if path == "/api/data/settings":
                    return self.output(200, self.app.data_transfer.save_settings(body))
                if path == "/api/data/preferences":
                    return self.output(200, self.app.data_transfer.save_preferences(body))
                if path == "/api/data/export":
                    return self.output(200, self.app.data_transfer.export(body))
                if path == "/api/data/import":
                    return self.output(200, self.app.data_transfer.restore(body))
                if path in {"/api/data/backup", "/api/data/backup/due"}:
                    if body:
                        raise ApiError(400, "备份操作只接受空对象。")
                    result = self.app.data_transfer.due_backup() if path.endswith("/due") else self.app.data_transfer.backup()
                    return self.output(200, result or {"status": self.app.data_transfer.status(), "unchanged": True})
                if path in {"/api/data/pick-directory", "/api/data/open-directory"}:
                    if body or not self.app.allow_open:
                        raise ApiError(403, "当前模式无法打开本地目录窗口。")
                    if path.endswith("pick-directory"):
                        return self.output(200, pick_directory())
                    directory = self.app.data_transfer.status()["settings"]["directory"]
                    if not directory or not Path(directory).is_dir():
                        raise ApiError(404, "请先保存可用的备份文件夹。")
                    if os.name != "nt":
                        raise ApiError(400, "当前系统不支持打开本地文件夹。")
                    subprocess.Popen(["explorer.exe", directory])
                    return self.output(200, {"ok": True})
                if path == "/api/service/restart":
                    if body:
                        raise ApiError(400, "重启服务只接受空对象。")
                    self.server.check_restart_ready()
                    creation = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
                    # The launcher asks this process to shut down through the same checks, starts the new one and exits.
                    subprocess.Popen([sys.executable, "-X", "utf8", str(APP_ROOT / "launcher.pyw"), "--no-browser"], cwd=str(APP_ROOT),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creation)
                    return self.output(200, {"ok": True, "pid": os.getpid()})
                if path == "/api/service/shutdown":
                    if body:
                        raise ApiError(400, "关闭服务只接受空对象。")
                    self.server.prepare_shutdown()
                    self.close_connection = True
                    try:
                        self.output(200, {"ok": True, "pid": os.getpid()})
                        self.wfile.flush()
                    finally:
                        # shutdown waits for serve_forever; never run it on the
                        # request thread before its response has been written.
                        threading.Thread(target=self.server.shutdown, name="workbench-shutdown", daemon=True).start()
                    return
                if path == "/api/sales-ocr":
                    image_path = self.app.video_files.safe(Path(str(body.get("path", ""))))
                    if not image_path.is_relative_to(self.app.video_files.imports) or image_path.suffix.lower() not in EXTENSIONS["screenshot"] or not image_path.is_file():
                        raise ApiError(400, "请先上传一张销售截图。")
                    try:
                        result = subprocess.run([sys.executable, "-X", "utf8", str(APP_ROOT / "sales_ocr.py"), str(image_path)], capture_output=True, text=True, encoding="utf-8", timeout=90, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                        if result.returncode:
                            raise ValueError("OCR failed")
                        draft = json.loads(result.stdout)
                    except Exception:
                        raise ApiError(400, "截图识别未完成，请重试或按图片手动填写，尚未保存案例。")
                    return self.output(200, draft)
                if path == "/api/ai/tasks/import":
                    from workbench_completed_import import import_completed_task
                    return self.output(200, import_completed_task(self.app, body))
                if path == "/api/ai/tasks":
                    return self.output(200, {"task": self.app.create_ai_task(body)})
                if path == "/api/ai/tasks/manage":
                    return self.output(200, self.app.ai.manage_tasks(body))
                if path == "/api/content/calendar":
                    return self.output(200, self.app.set_content_calendar(body))
                if path == "/api/content/views":
                    return self.output(200, self.app.save_content_views(body))
                if path == "/api/campaigns":
                    return self.output(200, self.app.campaigns.mutate(body))
                if path == "/api/content/manage":
                    return self.output(200, self.app.save_content_management(body))
                if path == "/api/content/delete-archived":
                    return self.output(200, self.app.delete_archived_content(body))
                if path == "/api/ai/preferences":
                    return self.output(200, {"preferences": self.app.ai.save_preferences(body)})
                if path == "/api/scripts":
                    return self.output(200, self.app.save_script(body))
                if path == "/api/variant-links":
                    return self.output(200, self.app.save_variant_link(body))
                script_action = re.fullmatch(r"/api/scripts/(SCR-[0-9a-f]{12})/(status|case)", path)
                if script_action:
                    identifier, action = script_action.groups()
                    return self.output(200, self.app.save_script_status(identifier, body) if action == "status" else self.app.script_to_case(identifier, body))
                if path == "/api/materials":
                    return self.output(200, self.app.save_material(body))
                if path == "/api/production/voices":
                    return self.output(200, self.app.save_voice(body))
                if path == "/api/production/readings":
                    return self.output(200, self.app.save_reading(body))
                if path == "/api/production/settings":
                    return self.output(200, self.app.save_production_settings(body))
                if path == "/api/production/jobs":
                    return self.output(200, self.app.create_production_job(body))
                if path == "/api/production/orders/batch":
                    return self.output(200, self.app.batch_orders(body))
                if path == "/api/production/carousel-tables":
                    return self.output(200, self.app.start_carousel_table(body))
                table_cancel = re.fullmatch(r"/api/production/carousel-tables/(TBL-[0-9a-f]{12})/cancel", path)
                if table_cancel:
                    return self.output(200, self.app.cancel_carousel_table(table_cancel.group(1)))
                if path == "/api/production/carousels":
                    return self.output(200, self.app.create_carousel(body))
                search_cancel = re.fullmatch(r"/api/production/carousels/(CAR-[0-9a-f]{12})/searches/(STS-[0-9a-f]{12})/cancel", path)
                if search_cancel:
                    return self.output(200, self.app.cancel_still_search(*search_cancel.groups()))
                search_delete = re.fullmatch(r"/api/production/carousels/(CAR-[0-9a-f]{12})/searches/(STS-[0-9a-f]{12})/delete", path)
                if search_delete:
                    if self.app.autopilot_running(search_delete.group(1)):
                        raise VoiceError(409, "AI 正在全程制作这组图文，请先停止后再调整素材。")
                    return self.output(200, self.app.delete_still_search(*search_delete.groups(), body))
                image_cancel = re.fullmatch(r"/api/production/carousels/(CAR-[0-9a-f]{12})/generations/(IMG-[0-9a-f]{12})/cancel", path)
                if image_cancel:
                    return self.output(200, self.app.cancel_image_generation(*image_cancel.groups()))
                review_cancel = re.fullmatch(r"/api/production/carousels/(CAR-[0-9a-f]{12})/(?:reviews|group-reviews)/((?:REV|GRV)-[0-9a-f]{12})/cancel", path)
                if review_cancel:
                    return self.output(200, self.app.cancel_image_generation(*review_cancel.groups()))
                autopilot_cancel = re.fullmatch(r"/api/production/carousels/(CAR-[0-9a-f]{12})/autopilot/(AUT-[0-9a-f]{12})/cancel", path)
                if autopilot_cancel:
                    return self.output(200, self.app.cancel_carousel_autopilot(*autopilot_cancel.groups(), body))
                carousel_action = re.fullmatch(r"/api/production/carousels/(CAR-[0-9a-f]{12})/(slides|archive|delete|search|clip-picks|base|tidy|import|compose|clip-frame|reveal|import-finished|generate|recheck|select-output|requirements|preflight|approval|approve-all|delivery|publication|calendar|autopilot|plan|group-review)", path)
                if carousel_action:
                    identifier, action = carousel_action.groups()
                    if action not in {"autopilot", "preflight", "calendar"} and self.app.autopilot_running(identifier):
                        raise VoiceError(409, "AI 正在全程制作这组图文，若要手动调整，请先停止全程制作。")
                    handler = {"slides": self.app.save_carousel_slides, "archive": self.app.archive_carousel,
                               "autopilot": self.app.start_carousel_autopilot,
                               "delete": self.app.delete_carousel_order, "clip-picks": self.app.ai_pick_clips,
                               "search": self.app.start_still_search, "base": self.app.choose_base, "tidy": self.app.tidy_candidates,
                               "import": self.app.import_folder_image, "compose": self.app.compose_candidates, "clip-frame": self.app.use_clip_frame,
                               "reveal": self.app.reveal_clip, "generate": self.app.start_image_generation,
                               "import-finished": self.app.import_finished_images, "recheck": self.app.start_carousel_review,
                               "select-output": self.app.select_carousel_output, "requirements": self.app.save_carousel_requirements,
                               "preflight": self.app.carousel_delivery_preflight,
                               "approval": self.app.approve_carousel_output, "approve-all": self.app.approve_carousel_outputs,
                               "delivery": self.app.create_carousel_delivery,
                               "publication": self.app.save_carousel_publication, "calendar": self.app.set_carousel_calendar_status,
                               "plan": self.app.save_carousel_plan,
                               "group-review": self.app.start_carousel_group_review}[action]
                    return self.output(200, handler(identifier, body))
                edit_cancel = re.fullmatch(r"/api/production/jobs/(PRD-[0-9a-f]{12})/edits/(EDT-[0-9a-f]{12})/cancel", path)
                if edit_cancel:
                    return self.output(200, self.app.cancel_edit(*edit_cancel.groups()))
                edit_recheck = re.fullmatch(r"/api/production/jobs/(PRD-[0-9a-f]{12})/edits/(EDT-[0-9a-f]{12})/recheck", path)
                if edit_recheck:
                    return self.output(200, self.app.recheck_edit(*edit_recheck.groups(), body))
                edit_delete = re.fullmatch(r"/api/production/jobs/(PRD-[0-9a-f]{12})/edits/(EDT-[0-9a-f]{12})/delete", path)
                if edit_delete:
                    return self.output(200, self.app.delete_edit(*edit_delete.groups(), body))
                edit_action = re.fullmatch(r"/api/production/jobs/(PRD-[0-9a-f]{12})/edits/(EDT-[0-9a-f]{12})/(approval|baseline)", path)
                if edit_action:
                    identifier, edit_id, action = edit_action.groups()
                    handler = {"approval": self.app.approve_edit, "baseline": self.app.adopt_edit_baseline}[action]
                    return self.output(200, handler(identifier, edit_id, body))
                video_carousel = re.fullmatch(r"/api/production/jobs/(PRD-[0-9a-f]{12})/video-carousel", path)
                if video_carousel:
                    return self.output(200, self.app.start_video_carousel(video_carousel[1], body))
                prepublish = re.fullmatch(r"/api/production/jobs/(PRD-[0-9a-f]{12})/prepublish(?:/(PPR-[0-9a-f]{12})/(findings|cancel))?", path)
                if prepublish:
                    identifier, review_id, action = prepublish.groups()
                    if not review_id:
                        return self.output(200, self.app.start_prepublish_review(identifier, body))
                    if action == "cancel":
                        return self.output(200, self.app.cancel_prepublish_review(identifier, review_id))
                    return self.output(200, self.app.resolve_prepublish_finding(identifier, review_id, body))
                video_publication = re.fullmatch(r"/api/production/jobs/(PRD-[0-9a-f]{12})/publication(/verify|/calendar)?", path)
                if video_publication:
                    handler = {"/verify": self.app.verify_video_publication, "/calendar": self.app.set_video_calendar_status}.get(video_publication[2], self.app.save_video_publication)
                    return self.output(200, handler(video_publication[1], body))
                timeline_cancel = re.fullmatch(r"/api/production/jobs/(PRD-[0-9a-f]{12})/timeline/cancel", path)
                if timeline_cancel:
                    return self.output(200, self.app.cancel_timeline(timeline_cancel.group(1), body))
                timeline_lifecycle = re.fullmatch(r"/api/production/jobs/(PRD-[0-9a-f]{12})/timeline/(archive|task-status)", path)
                if timeline_lifecycle:
                    identifier, action = timeline_lifecycle.groups()
                    handler = self.app.archive_timeline if action == "archive" else self.app.set_timeline_task_status
                    return self.output(200, handler(identifier, body))
                job_action = re.fullmatch(r"/api/production/jobs/(PRD-[0-9a-f]{12})/(takes|select|lines|fill|archive|delete|timeline|open|edit|handoff|recover|completion)", path)
                if job_action:
                    identifier, action = job_action.groups()
                    handler = {"takes": self.app.generate_take, "select": self.app.select_take,
                               "lines": self.app.save_job_lines, "fill": self.app.fill_production_job, "archive": self.app.archive_job, "delete": self.app.delete_job_order,
                               "timeline": self.app.build_timeline, "open": self.app.open_timeline,
                               "edit": self.app.start_edit, "handoff": self.app.edit_handoff,
                               "recover": self.app.recover_production_operation, "completion": self.app.complete_production_job}[action]
                    return self.output(200, handler(identifier, body))
                if path == "/api/materials/remove":
                    return self.output(200, self.app.remove_materials(body))
                if path == "/api/materials/notes":
                    return self.output(200, self.app.save_material_notes(body))
                if path == "/api/materials/restore":
                    return self.output(200, self.app.restore_materials(body))
                if path == "/api/material-themes":
                    return self.output(200, self.app.save_material_theme(body))
                if path == "/api/plans":
                    return self.output(200, self.app.save_plan(body))
                if path == "/api/review-followups":
                    prior = self.app.review_followups.replay("followup", body)
                    if prior:
                        return self.output(200, prior)
                    if body.get("classification_only") is True:
                        return self.output(200, self.app.review_followups.save(body, None))
                    video = next((item for item in self.app.review_videos() if item["video_id"] == body.get("video_id") and
                                  product_key(item.get("product")) == product_key(body.get("product"))), None)
                    return self.output(200, self.app.review_followups.save(body, video))
                if path == "/api/review-experiences":
                    return self.output(200, self.app.save_review_experience(body))
                if path == "/api/ai/tasks/delete":
                    return self.output(200, self.app.ai.delete_tasks(body))
                if path == "/api/ai/claude/login":
                    return self.output(200, self.app.claude_login(body))
                if path == "/api/ai/claude/callback":
                    return self.output(200, self.app.claude_login_action("callback", body))
                if path == "/api/ai/claude/login/cancel":
                    return self.output(200, self.app.claude_login_action("cancel", body))
                if path in {"/api/ai/claude/key", "/api/ai/claude/key/delete", "/api/ai/claude/install"}:
                    return self.output(200, self.app.claude_settings(path.rsplit("/", 1)[-1], body))
                ai_action = re.fullmatch(r"/api/ai/tasks/(AIT-[0-9a-f]{12})/(continue|cancel)", path)
                if ai_action:
                    identifier, action = ai_action.groups()
                    if action == "cancel" and (not isinstance(body.get("round_id"), str) or not re.fullmatch(r"AIR-[0-9a-f]{12}", body["round_id"])):
                        raise ApiError(400, "缺少要取消的轮次，请刷新任务后重试。")
                    task = self.app.ai.continue_task(identifier, body) if action == "continue" else self.app.ai.cancel(identifier, body)
                    return self.output(200, {"task": task})
                if path == "/api/navigation/move":
                    return self.output(200, self.app.move_navigation(body))
                if path in {"/api/selling-points", "/api/direction-catalog", "/api/video-directions"}:
                    kind = {"/api/selling-points": "point", "/api/direction-catalog": "direction", "/api/video-directions": "video"}[path]
                    return self.output(200, self.app.save_direction_catalog(kind, body))
                if path == "/api/performance/sync":
                    if body:
                        raise ApiError(400, "同步只接受空对象。")
                    return self.output(200, self.app.performance.sync())
                if path == "/api/performance/import":
                    return self.output(200, self.app.import_performance_uploads(body))
                if path == "/api/performance/public-export":
                    return self.output(200, self.app.export_public_views(body))
                if path == "/api/reviews/sync":
                    if body:
                        raise ApiError(400, "同步只接受空对象。")
                    return self.output(200, self.app.reviews.sync(force=True))
                review_write = re.fullmatch(r"/api/reviews/([0-9a-f]{16})/(pick|tag)", path)
                if review_write:
                    identifier, action = review_write.groups()
                    if action == "pick":
                        with self.app.lock:
                            return self.output(200, self.app.reviews.set_pick(identifier, body.get("picked"), body.get("note")))
                    return self.output(200, {"review": self.app.reviews.set_tag(identifier, body.get("kind"), body.get("tag"), body.get("present"))})
                if path == "/api/cases":
                    return self.output(200, self.app.save(None, body))
                delete_match = re.fullmatch(r"/api/cases/(PVR-[0-9a-f]{12})/delete", path)
                if delete_match:
                    return self.output(200, self.app.delete_case(delete_match.group(1), body))
                archive_match = re.fullmatch(r"/api/cases/(PVR-[0-9a-f]{12})/archive", path)
                if archive_match:
                    return self.output(200, self.app.set_archived(archive_match.group(1), body))
                if path == "/api/performance/batch-collect":
                    try:
                        return self.output(200, {"results": self.app.auto_breakdown.batch_collect(body.get("video_ids"))})
                    except ValueError as exc:
                        raise ApiError(400, str(exc))
                transcribe = re.fullmatch(r"/api/cases/(PVR-[0-9a-f]{12})/transcribe", path)
                if transcribe:
                    try:
                        transcript = transcribe_case(self.app, transcribe.group(1), replace=body.get("replace") is True,
                                                     translate_only=body.get("translate_only") is True)
                    except (ValueError, StoreError) as exc:
                        raise ApiError(400, str(exc))
                    return self.output(200, transcript)
                auto_start = re.fullmatch(r"/api/cases/(PVR-[0-9a-f]{12})/auto-breakdown", path)
                if auto_start:
                    try:
                        job = self.app.auto_breakdown.start(auto_start.group(1), video_id=body.get("video_id"), retry=body.get("retry") is True,
                                                            model=body.get("model"), reasoning_effort=body.get("reasoning_effort"))
                    except (ValueError, StoreError) as exc:
                        raise ApiError(400, str(exc))
                    return self.output(200, {"job": job})
                if path == "/api/products/catalog":
                    return self.output(200, self.app.change_product_catalog(body))
                product_set = re.fullmatch(r"/api/cases/(PVR-[0-9a-f]{12})/product", path)
                if product_set:
                    return self.output(200, self.app.set_case_product(product_set.group(1), body.get("product")))
                if path.startswith("/api/cases/"):
                    return self.output(200, self.app.save(path.split("/")[-1], body))
                if path == "/api/open":
                    return self.output(200, self.app.open_location(body))
                if path == "/api/sync":
                    with self.app.lock:
                        self.app.store.rebuild_index()
                        render_library(self.app.store.root, self.app.library)
                    return self.output(200, {"ok": True})
                raise ApiError(404, "操作不存在。")
            raise ApiError(405, "不支持此操作。")
        except (ApiError, ActivityError) as exc:
            # On Windows, closing with an unread POST body can reset the socket
            # before the browser receives the useful 403 response. Drain small,
            # bounded bodies only; never wait indefinitely for a rejected upload.
            if post_body_unread:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if 0 < length <= 4 * 1024 * 1024:
                        self.connection.settimeout(1)
                        self.rfile.read(length)
                except (OSError, ValueError):
                    pass
            self.close_connection = True
            self.output(exc.status, {"error": str(exc), **getattr(exc, "details", {})})
        except (TransferError, BackupError, RemoteAccountError) as exc:
            self.close_connection = True
            return self.output(exc.status, {"error": str(exc)})
        except (DirectionError, CandidateError) as exc:
            return self.output(exc.status, {"error": str(exc)})
        except CreationError as exc:
            return self.output(exc.status, {"error": str(exc)})
        except VoiceError as exc:
            return self.output(exc.status, {"error": str(exc)})
        except AITaskError as exc:
            return self.output(exc.status, {"error": str(exc)})
        except VideoFileError as exc:
            self.close_connection = True
            self.output(400, {"error": str(exc)})
        except ReviewError as exc:
            self.close_connection = True
            self.output(400, {"error": "评论库：" + str(exc)})
        except (StoreError, ValueError, KeyError, TypeError) as exc:
            self.close_connection = True
            self.output(400, {"error": "案例数据未通过核对：" + str(exc)})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as exc:
            self.close_connection = True
            self.log_error("%s", exc)
            self.output(500, {"error": "本地操作失败，请检查文件是否可用后重试。"})
        finally:
            if post_admitted:
                self.server.end_write()

    do_GET = do_HEAD = do_POST = dispatch

    def event_stream(self, identifier):
        packet = self.app.ai.stream_snapshot(identifier)
        self.send_response(200)
        self.headers_common()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        deadline = time.monotonic() + 45
        previous = None
        try:
            # A bounded write timeout avoids retaining a reader that stopped listening.
            self.connection.settimeout(10)
            while True:
                content = json.dumps({"task": packet["task"]}, ensure_ascii=False, separators=(",", ":"))
                if content != previous:
                    self.wfile.write(("data: " + content + "\n\n").encode("utf-8"))
                    previous = content
                else:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                if not packet["task"].get("busy") or time.monotonic() >= deadline or self.app.ai._closing.is_set():
                    break
                # Coalesce rapid token events without making readers wait for completion.
                time.sleep(0.075)
                packet = self.app.ai.stream_snapshot(identifier, packet["version"], timeout=2)
        except AITaskError as exc:
            # Headers are already sent; report deletion/unavailability within the stream.
            value = json.dumps({"error": str(exc), "status": exc.status}, ensure_ascii=False)
            try:
                self.wfile.write(("event: error\ndata: " + value + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except OSError:
                pass
        except (OSError, TimeoutError):
            pass  # Leaving or reloading a page does not cancel the user's task.

    def content_length(self, limit):
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            raise ApiError(400, "文件长度无效。")
        if not 0 <= length <= limit:
            raise ApiError(413, "文件过大或长度未知。视频支持最大 2GB，其他附件最大 30MB。")
        return length

    def carousel_upload(self, identifier, query):
        """One local picture (PNG/JPG/WebP, at most 25 MB) as a candidate base for a slide; the body is the raw file."""
        limit = 25 * 1024 ** 2
        try:
            try:
                slide = int(query.get("slide", [""])[0])
            except ValueError:
                raise ApiError(400, "要添加到哪一张无效。")
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 < length <= limit:
                raise ApiError(413, "图片为空或超过 25 MB。")
        except (ApiError, ValueError) as error:
            timeout = self.connection.gettimeout()
            try:
                rejected = int(self.headers.get("Content-Length", "0"))
                if 0 < rejected <= 4 * 1024 * 1024:
                    self.connection.settimeout(1)
                    self.rfile.read(rejected)
            except (OSError, ValueError):
                pass
            finally:
                self.connection.settimeout(timeout)
            if isinstance(error, ApiError):
                raise
            raise ApiError(400, "图片长度无效。")
        data = self.rfile.read(length)
        if len(data) != length:
            raise ApiError(400, "图片没有完整上传，请重试。")
        return self.output(200, self.app.import_uploaded_image(identifier, slide, query.get("name", [""])[0],
                                                               data, query.get("request_id", [""])[0]))

    def upload(self, query):
        kind = query.get("kind", [""])[0]
        raw_name = query.get("name", [""])[0]
        try:
            if kind not in EXTENSIONS or Path(raw_name).suffix.lower() not in EXTENSIONS[kind]:
                raise ApiError(400, "附件格式不支持。")
            length = self.content_length(2 * 1024**3 if kind == "video" else 30 * 1024**2)
            if not length:
                raise ApiError(400, "不能上传空文件。")
        except ApiError:
            # On Windows an unread upload body may reset the socket before its
            # format error reaches the browser. Drain only a small bounded body.
            timeout = self.connection.gettimeout()
            try:
                rejected_length = int(self.headers.get("Content-Length", "0"))
                if 0 < rejected_length <= 4 * 1024 * 1024:
                    self.connection.settimeout(1)
                    self.rfile.read(rejected_length)
            except (OSError, ValueError):
                pass
            finally:
                self.connection.settimeout(timeout)
            raise
        target = self.app.video_files.stage(self.rfile, length, raw_name)
        return self.output(200, {"path": str(target), "name": raw_name, "bytes": length,
                                 "managed": True, "temporary": True})

    def file(self, path, download=False, cache=False, immutable=False):
        content = None
        compressed = False
        if cache:
            with self.server.static_lock:
                index = path.name == "index.html"
                key = (file_key(path), static_signature()) if index else file_key(path)
                cached = self.server.static_cache.get(path)
                if cached is None or cached[0] != key:
                    raw = versioned_index(path.read_bytes()) if index else path.read_bytes()
                    packed = gzip.compress(raw, compresslevel=5, mtime=0) if len(raw) >= 1024 and path.suffix in {".js", ".css", ".html", ".svg"} else None
                    if packed is not None and len(packed) >= len(raw):
                        packed = None
                    cached = (key, raw, packed)
                    self.server.static_cache[path] = cached
            compressed = cached[2] is not None and accepts_gzip(self.headers.get("Accept-Encoding", "")) and not self.headers.get("Range")
            content = cached[2] if compressed else cached[1]
            etag = '"' + hashlib.sha256(content).hexdigest() + '"'
            supplied = self.headers.get("If-None-Match", "")
            if any(tag.strip().removeprefix("W/") in {etag, "*"} for tag in supplied.split(",")):
                self.send_response(304)
                self.headers_common("private, max-age=31536000, immutable" if immutable else "private, no-cache")
                self.send_header("ETag", etag)
                self.send_header("Vary", "Accept-Encoding")
                self.end_headers()
                return
        image_tag = None
        if not cache and not download and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            # Thumbnails and stills keep their bytes once written, so the browser keeps them and only asks
            # whether they changed; production pages redraw them on every refresh.
            stat = path.stat()
            image_tag = f'W/"{stat.st_size:x}-{stat.st_mtime_ns:x}"'
            if image_tag in {tag.strip() for tag in self.headers.get("If-None-Match", "").split(",")} and not self.headers.get("Range"):
                self.send_response(304)
                self.headers_common("private, no-cache")
                self.send_header("ETag", image_tag)
                self.end_headers()
                return
        total = len(content) if content is not None else path.stat().st_size
        start, end, status = 0, total - 1, 200
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not match or not any(match.groups()):
                raise ApiError(416, "无效的视频读取范围。")
            if match[1]:
                start = int(match[1]); end = min(int(match[2]), total - 1) if match[2] else total - 1
            else:
                start = max(0, total - int(match[2]))
            if start >= total or end < start:
                self.send_response(416); self.send_header("Content-Range", f"bytes */{total}"); self.send_header("Content-Length", "0"); self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.headers_common("private, max-age=31536000, immutable" if cache and immutable else "private, no-cache" if cache or image_tag else "no-store")
        if image_tag:
            self.send_header("ETag", image_tag)
        if cache:
            self.send_header("ETag", etag)
            self.send_header("Vary", "Accept-Encoding")
        if compressed:
            self.send_header("Content-Encoding", "gzip")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in {".md", ".txt"}:
            content_type = "text/plain; charset=utf-8"
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        if download:
            from urllib.parse import quote
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(path.name))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        if self.command != "HEAD":
            if content is not None:
                self.wfile.write(content[start:end + 1])
                return
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = stream.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
                if download and status == 200 and remaining == 0:
                    self.wfile.flush()
                    return True


class WorkbenchHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR permits another listener on the same address. Keep
    # one owner for the workbench port while retaining normal POSIX restarts.
    allow_reuse_address = os.name != "nt"
    allow_reuse_port = False

    def __init__(self, *args, **kwargs):
        self._write_gate = threading.Condition()
        self.static_lock = threading.Lock()
        self.static_cache = {}
        self._active_writes = 0
        self._shutting_down = False
        self.data_maintenance = False
        self._request_context = threading.local()
        super().__init__(*args, **kwargs)

    @property
    def shutting_down(self):
        with self._write_gate:
            return self._shutting_down

    def transfer_operation(self):
        transfer = getattr(self.app, "data_transfer", None)
        return getattr(transfer, "operation_kind", None) or ("restore" if self.data_maintenance else None)

    def transfer_busy_error(self):
        operation = self.transfer_operation()
        message = {"export": "总数据正在导出，完成后即可继续保存。",
                   "backup": "总数据正在备份，完成后即可继续保存。",
                   "restore": "总数据正在导入，完成后将自动恢复。",
                   "inspect": "备份包正在检查，请稍后重试。"}.get(operation, "总数据操作正在进行，请稍后重试。")
        return ApiError(409, message, maintenance=True, operation=operation)

    def begin_write(self):
        with self._write_gate:
            if self.data_maintenance:
                raise self.transfer_busy_error()
            if self._shutting_down:
                raise ApiError(409, "工作台正在重新启动，请稍后重试。")
            self._active_writes += 1
            self._request_context.writing = True
        self._content_write("content_write_started")

    def end_write(self):
        with self._write_gate:
            self._active_writes -= 1
            self._request_context.writing = False
            self._write_gate.notify_all()
        self._content_write("content_write_finished")

    def _content_write(self, event):
        # Content lists reuse a short-lived snapshot for filter changes; a write in flight ends that reuse.
        getattr(getattr(self, "app", None), event, lambda: None)()

    def begin_data_maintenance(self):
        with self._write_gate:
            own_request = int(bool(getattr(self._request_context, "writing", False)))
            if self._shutting_down or self.data_maintenance or self._active_writes > own_request:
                raise ApiError(409, "还有保存或上传操作正在进行，请完成后再导入总数据。")
            self.data_maintenance = True

    @contextmanager
    def data_operation(self):
        self.begin_data_maintenance()
        try:
            yield
        finally:
            self.end_data_maintenance()

    def end_data_maintenance(self):
        with self._write_gate:
            self.data_maintenance = False
            self._write_gate.notify_all()

    def _activity_blocker(self):
        """Why the service must not stop right now, or None. Never interrupts: running work is only reported."""
        if getattr(self.app, "autopilot_running", lambda: False)():
            return "AI 全程图文制作仍在运行，请等它完成或先停止，再重新启动。"
        tasks = self.app.ai.list_tasks()
        if any(task.get("status") in {"queued", "running", "stopping"}
               or task.get("busy") or task.get("stopping") for task in tasks):
            return "AI 协作仍在运行或停止中，请完成后再重新启动。"
        if self.app.edit_running():
            return "AI 剪辑仍在运行，请等它结束或先停止，再重新启动。"
        if getattr(self.app, "_voicing_jobs", set()):
            return "配音仍在运行，请等它完成后再重新启动。"
        if getattr(self.app, "_timeline_building", {}):
            return "建时间线正在排队或进行，请等它完成或先取消排队，再重新启动。"
        if getattr(self.app, "image_running", lambda: False)():
            return "图文出图仍在运行，请等它结束或先停止，再重新启动。"
        if getattr(self.app, "carousel_table_running", lambda: False)():
            return "图文逐张表仍在生成，请等它完成或先停止，再重新启动。"
        return None

    def check_restart_ready(self):
        """Refuse unless nothing is saving, importing or running; reserves nothing."""
        with self._write_gate:
            if self._shutting_down:
                raise ApiError(409, "工作台已经在重启，请稍候。")
            if self.data_maintenance or self._active_writes:
                raise ApiError(409, "工作台正在保存或处理操作，请完成后重试。")
            if getattr(self.app, "data_transfer", None) and self.app.data_transfer.busy:
                raise ApiError(409, "总数据备份或导入导出正在进行，请完成后重试。")
        message = self._activity_blocker()
        if message:
            raise ApiError(409, message)

    def prepare_shutdown(self):
        with self._write_gate:
            if self._shutting_down:
                raise ApiError(409, "工作台已经在关闭，请等待完成。")
            if self._active_writes:
                raise ApiError(409, "工作台正在保存或处理操作，请完成后重试。")
            if getattr(self.app, "data_transfer", None) and self.app.data_transfer.busy:
                raise ApiError(409, "总数据备份或导入导出正在进行，请完成后重试。")
            # Reserve admission before checking AI. No gate is held while
            # taking the app/AI lock, so finishing writes cannot deadlock it.
            self._shutting_down = True
        try:
            message = self._activity_blocker()
            if message:
                raise ApiError(409, message)
        except Exception:
            with self._write_gate:
                self._shutting_down = False
                self._write_gate.notify_all()
            raise

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def server_close(self):
        if hasattr(self, "app"):
            self.app.data_transfer.close()
            getattr(self.app, "stop_carousel_autopilots", lambda: None)()
            getattr(self.app, "stop_images", lambda: None)()
            self.app.stop_edits()
            getattr(self.app, "stop_carousel_tables", lambda: None)()
            self.app.ai.shutdown()
            close = getattr(self.app.ai.bridge, "close", None)
            if callable(close):
                close()
        super().server_close()
        if getattr(self, "data_lease", None):
            self.data_lease.close()


def make_server(skill_root, runtime_root, port=8767, allow_open=True, ai_bridge=None, performance_sources=None):
    server = WorkbenchHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    if performance_sources is None:
        performance_sources = DEFAULT_CONFIG.get("performance_sources", [])
    try:
        server.data_lease = ServerLease(runtime_root)
        if not server.data_lease.acquire():
            raise RuntimeError("此工作台正在运行或正在离线备份，请稍后重新打开。")
        server.app = Workbench(skill_root, runtime_root, allow_open, ai_bridge=ai_bridge,
                               performance_sources=performance_sources)
        server.app.data_transfer.maintenance = server.data_operation
        server.app.data_transfer.start(server.server_port)
    except Exception:
        server.server_close()
        raise
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-root", default=DEFAULT_CONFIG["skill_root"])
    parser.add_argument("--runtime-root", default=DEFAULT_CONFIG["runtime_root"])
    parser.add_argument("--port", type=int, default=DEFAULT_CONFIG["port"])
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    server = make_server(args.skill_root, args.runtime_root, args.port, not args.no_open)
    print(f"Case workbench: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
