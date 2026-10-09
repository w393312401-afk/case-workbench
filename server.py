"""Local case workbench; uses the existing append-only CaseStore as its only database."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse
from uuid import uuid4

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = json.loads((APP_ROOT / "config.json").read_text(encoding="utf-8"))
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(Path(DEFAULT_CONFIG["skill_root"]) / "scripts"))
from case_store import CaseStore, StoreError, add_schema, current_schema, current_sources, encode
from render_case_library import render_library
from validate_case_structure import DEFAULT_TEMPLATE, validate_case_structure
from case_deletion import DeletionManager, DeletionError
from managed_video import ManagedVideoFiles, VideoFileError
from ai_tasks import AITaskManager, AITaskError
from performance_store import PerformanceStore
from sales_data import SalesDataResolver
from review_store import ReviewError, ReviewStore
from direction_store import DirectionError, DirectionStore, product_key
from creation_store import CreationError, CreationStore
from workbench_creation import CreationWorkbenchMixin

EXTENSIONS = {
    "video": {".mp4", ".webm", ".mov", ".m4v"},
    "screenshot": {".png", ".jpg", ".jpeg", ".webp"},
    "contact_sheet": {".png", ".jpg", ".jpeg", ".webp"},
    "transcript": {".txt", ".md", ".json"},
    "source_manifest": {".json"},
    "performance": {".xlsx"},
}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def file_key(path):
    """Cheap change marker for one file: size, modified and created time (None when absent)."""
    try:
        stat = os.stat(path)
    except (OSError, ValueError):
        return None
    return stat.st_size, stat.st_mtime_ns, getattr(stat, "st_birthtime_ns", stat.st_ctime_ns)


class CachedCaseStore(CaseStore):
    """CaseStore that reuses its last full index verification while the record files are unchanged.

    read_index() re-reads and validates every case. Pages poll it every few seconds, and those
    thousands of file calls per poll starve the other request threads. The verification is
    re-run whenever a scan of the case folders (names, sizes, modified and created times) differs.
    Callers treat the returned index as read-only.
    """

    def __init__(self, root):
        super().__init__(root)
        self.index_key = None
        self._index_cache = None

    def fingerprint(self):
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

        for case, is_dir in scan(self.root / "cases"):
            if is_dir:
                for part, part_is_dir in scan(case, {"original.json", "events"}):
                    if part_is_dir:
                        scan(part)
        return tuple(sorted(entries, key=repr))

    def read_index(self):
        key = self.fingerprint()
        cached = self._index_cache
        if cached is not None and cached[0] == key:
            self.index_key = key
            return cached[1]
        index = super().read_index()
        # The key was taken before reading, so a change made during the read forces a re-check next time.
        self._index_cache, self.index_key = (key, index), key
        return index


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


class Workbench(CreationWorkbenchMixin):
    def __init__(self, skill_root, runtime_root, allow_open=True, ai_bridge=None, performance_sources=()):
        self.store = CachedCaseStore(skill_root)
        self.runtime = Path(runtime_root).resolve()
        self.library = self.runtime / "library"
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.allow_open = allow_open
        self.requests = {}
        self.structure_cache = {}
        self.state_cache = None
        self.thumbnail_lock = threading.Lock()
        self.deletion = DeletionManager(self.store.root, self.runtime)
        self.video_files = ManagedVideoFiles(self.runtime)
        self.reviews = ReviewStore(self.runtime)
        self.performance = PerformanceStore(self.runtime, performance_sources)
        self.sales = SalesDataResolver(self.performance)
        self.directions = DirectionStore(self.runtime, self.store.root, self.lock)
        self.creation = CreationStore(self.runtime, self.lock)
        self.ai = AITaskManager(self.store, self.runtime, self.lock, bridge=ai_bridge,
                                review_digest=self.review_digest, sales_resolver=self.sales)

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
        return self.reviews.digest()

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

    def import_performance_uploads(self, body):
        paths = body.get("paths")
        if not isinstance(paths, list) or not paths or len(paths) > 100:
            raise ApiError(400, "请选择要导入的后台导出表格。")
        kept = []
        for raw in paths:
            staged = self.video_files.safe(Path(str(raw)))
            if not staged.is_relative_to(self.video_files.imports) or staged.suffix.lower() != ".xlsx" or not staged.is_file():
                raise ApiError(400, "导入文件已失效，请重新选择。")
            kept.append(self.performance.keep_upload(staged, staged.name))
        return self.performance.import_paths(kept, skip_unchanged=False)

    def detail(self, identifier, sales_data=None, direction_catalog=None):
        case = self.store.read_case(identifier)
        result = copy.deepcopy(case)
        result["revision"] = revision(case)
        result["sales_data"] = copy.deepcopy(sales_data) if sales_data is not None else self.sales.resolve_case(case)
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
        catalog = self.directions.sync(self.store.read_index()["cases"])
        for group in summary["groups"]:
            entity = self.directions.find_direction(catalog, group["product"], group["direction_id"])
            if entity:
                # Per-case wording remains a historical definition. Only an
                # incompatible lead recorded by the catalog is a conflict.
                group["definition_conflict"] = bool(entity.get("definition_conflict"))
        return summary

    def direction_catalog(self, product=None):
        with self.lock:
            cases = self.store.read_index()["cases"]
            data = self.directions.sync(cases)
            result = self.directions.public(data, product)
            independent_scripts = self.creation.public(product)["scripts"]
            direction_videos = {}
            for direction in result["directions"]:
                selected = [case for case in cases if product_key(case.get("product")) == direction["product"] and
                            ((case.get("current") or {}).get("test_direction") or {}).get("direction_id") == direction["direction_id"]]
                videos = {str(((case.get("current") or {}).get("metrics") or {}).get("video_id") or "") for case in selected} - {""}
                videos = {video for video in videos if not (link := self.directions.find_link(data, direction["product"], video))
                          or link["direction_id"] == direction["direction_id"]}
                videos.update(link["video_id"] for link in data["video_links"] if link["product"] == direction["product"] and link["direction_id"] == direction["direction_id"])
                scripts = [script for script in independent_scripts if script["product"] == direction["product"] and script.get("direction_id") == direction["direction_id"]]
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

    def direction_detail(self, identifier, product=None):
        with self.lock:
            catalog = self.direction_catalog(product)
            matches = [item for item in catalog["directions"] if item["direction_id"] == identifier]
            if not matches:
                raise ApiError(404, "这条方向不存在。")
            if len(matches) != 1:
                raise ApiError(400, "多个产品使用此方向编号，请先选择产品。")
            entity = matches[0]
            product = entity["product"]
            group = next(iter(self.direction_summary(product=product, direction_id=identifier, archive="all")["groups"]), None)
            cases = [self.detail(case["case_id"]) for case in self.store.read_index()["cases"]
                     if product_key(case.get("product")) == product and
                     ((case.get("current") or {}).get("test_direction") or {}).get("direction_id") == identifier]
            links = {link["video_id"]: link for link in catalog["video_links"] if link["product"] == product and link["direction_id"] == identifier}
            case_by_video = {str(((case.get("current") or {}).get("metrics") or {}).get("video_id") or ""): case["case_id"] for case in cases}
            videos = (set(links) | set(case_by_video)) - {""}
            by_video = {link["video_id"]: link for link in catalog["video_links"] if link["product"] == product}
            videos = {video for video in videos if video not in by_video or by_video[video]["direction_id"] == identifier}
            brief = self.performance.video_brief(videos)
            scripts = [{"case_id": case["case_id"], "title": case["current"]["title"],
                        "script_version": (case["current"].get("production") or {}).get("script_version"),
                        "text": case["current"]["transcript"]["text"]}
                       for case in cases if (case.get("current") or {}).get("transcript")]
            creation = self.creation_catalog(product)
            independent = [script for script in creation["scripts"] if script.get("direction_id") == identifier]
            scripts.extend(independent)
            videos.update(script["video_id"] for script in independent if script.get("video_id"))
            brief = self.performance.video_brief(videos)
            tasks = []
            for task in self.ai.list_tasks():
                # Tasks belong to their frozen direction, even if the case later moves.
                snapshot = self.ai._read(task["task_id"])["snapshot"]
                frozen = (snapshot.get("current") or {}).get("test_direction") or {}
                frozen_id = ((snapshot.get("creation_context") or {}).get("direction") or {}).get("direction_id") or frozen.get("direction_id")
                if product_key(snapshot.get("product")) == product and frozen_id == identifier:
                    tasks.append(task)
            return {"revision": catalog["revision"], "direction": entity,
                    "selling_point": next((point for point in catalog["selling_points"] if point["id"] == entity.get("selling_point_id")), None),
                    "group": group, "cases": cases, "scripts": scripts, "tasks": tasks,
                    "creation_revision": creation["revision"],
                    "plans": [plan for plan in creation["plans"] if plan.get("direction_id") == identifier],
                    "theme_links": [link for link in creation["theme_links"] if link.get("direction_id") == identifier or link.get("selling_point_id") == entity.get("selling_point_id")],
                    "videos": [{"video_id": video, **brief.get(video, {}), "product": product, "direction_id": identifier,
                                "case_id": case_by_video.get(video)} for video in sorted(videos)]}

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
            result = self.directions.mutate(kind, body)
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
        with self.lock:
            # Read records afresh: changes from the existing intake tools appear without restarting.
            index = self.store.read_index()
            self.directions.sync(index["cases"])
            known = {entry["case_id"] for entry in index["cases"]}
            self.requests = {key: value for key, value in self.requests.items()
                if not value[1].get("case") or value[1]["case"]["case_id"] in known}
            # Everything the snapshot reads besides the verified records: navigation, template, assets, sources.
            watched = [self.runtime / "navigation-assignments.json", DEFAULT_TEMPLATE, self.directions.path, self.creation.path]
            for entry in index["cases"]:
                current = entry.get("current") or {}
                watched.extend(Path(raw) for raw in (current.get("assets") or {}).values())
                if current.get("source_case"):
                    watched.append(Path(current["source_case"]["file_path"]))
            sales_signature = self.performance.signature()
            key = (self.store.index_key, tuple((str(path), file_key(path)) for path in watched), sales_signature)
            if self.state_cache is not None and self.state_cache[0] == key:
                return self.state_cache[1]
            sales = self.sales.resolve_cases(index["cases"])
            cases = [self.detail(entry["case_id"], sales[entry["case_id"]]) for entry in index["cases"]]
            signature = hashlib.sha256("".join(item["revision"] + json.dumps(item["assets"], sort_keys=True)
                                             + item.get("source_analysis", {}).get("digest", "")
                                             + json.dumps(item["sales_data"], sort_keys=True, ensure_ascii=False)
                                             + json.dumps(item["resolved_direction"], sort_keys=True, ensure_ascii=False) for item in cases).encode()).hexdigest()
            navigation = self.navigation()
            signature = hashlib.sha256((signature + json.dumps(navigation, sort_keys=True)).encode()).hexdigest()
            summaries = []
            for case in cases:
                current = case.get("current")
                summaries.append({key: case[key] for key in ("case_id", "product", "revision", "assets", "current", "sales_data", "resolved_direction", "current_event_id", "archived", "archive_event_id") if key in case} | {
                    "title": current["title"] if current else case["title"],
                    "updated_at": case["events"][-1]["created_at"],
                    "event_count": len(case["events"]),
                    "search_text": next(x["current_search_text"] for x in index["cases"] if x["case_id"] == case["case_id"]),
                })
            paths = {"records": str(self.store.root / "cases"), "evidence": str(self.runtime / "evidence"), "library": str(self.library), "directions": str(self.directions.root), "creation": str(self.creation.root)}
            # Lets the polling page ask "anything new since this digest?" and get a tiny reply instead of every case.
            direction_revision = self.directions.revision(self.directions.read())
            creation_revision = self.creation.revision(self.creation.read())
            signature = hashlib.sha256((signature + direction_revision + creation_revision).encode()).hexdigest()
            digest = hashlib.sha256(json.dumps([navigation, summaries, paths, direction_revision, creation_revision], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
            snapshot = {"app": "video-case-workbench", "token": self.token, "signature": signature, "digest": digest,
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

    @property
    def app(self):
        return self.server.app

    # Page polling hits these every few seconds; successful polls would bury
    # real requests and errors in server.log.
    QUIET_POLLS = ("/api/state", "/api/ai/tasks", "/api/ai/status", "/api/ai/claude/login")

    def log_request(self, code="-", size="-"):
        path = urlparse(getattr(self, "path", "") or "").path
        if self.command == "GET" and str(code).startswith(("2", "3")) and path.startswith(self.QUIET_POLLS):
            return
        super().log_request(code, size)

    def headers_common(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")

    def output(self, status, content, content_type="application/json; charset=utf-8"):
        if not isinstance(content, bytes):
            content = json.dumps(content, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.headers_common()
        self.send_header("Content-Type", content_type)
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
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise ApiError(403, "不接受跨站请求。")
        if write and not secrets.compare_digest(self.headers.get("X-Workbench-Token", ""), self.app.token):
            raise ApiError(403, "页面连接已过期，请刷新后重试。")

    def dispatch(self):
        post_admitted = False
        post_body_unread = self.command == "POST"
        try:
            self.guard(self.command == "POST")
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query)
            if self.command == "POST" and path != "/api/service/shutdown":
                self.server.begin_write()
                post_admitted = True
            if self.command in {"GET", "HEAD"}:
                if path == "/api/service/status":
                    return self.output(200, {"app": "video-case-workbench", "pid": os.getpid(),
                        "token": self.app.token, "shutdown_supported": True, "shutting_down": self.server.shutting_down,
                        "paths": {"records": str(self.app.store.root / "cases"), "library": str(self.app.library)}})
                if path == "/api/state":
                    snapshot = self.app.state()
                    if query.get("since", [None])[0] == snapshot["digest"]:
                        return self.output(200, {key: snapshot[key] for key in ("app", "token", "signature", "digest")} | {"unchanged": True})
                    return self.output(200, snapshot)
                if path == "/api/ai/status":
                    return self.output(200, self.app.ai.status())
                if path == "/api/ai/claude/login":
                    self.guard(write=True)
                    return self.output(200, self.app.claude_login_state())
                if path == "/api/creation":
                    return self.output(200, self.app.creation_catalog(query.get("product", [None])[0]))
                if path == "/api/materials":
                    return self.output(200, self.app.material_catalog(query.get("product", [None])[0]))
                script_match = re.fullmatch(r"/api/scripts/(SCR-[0-9a-f]{12})", path)
                if script_match:
                    return self.output(200, self.app.script_detail(script_match.group(1)))
                if path == "/api/direction-catalog" or path.startswith("/api/direction-catalog/"):
                    selected = query.get("product", [None])
                    if len(selected) != 1:
                        raise ApiError(400, "方向产品筛选只能出现一次。")
                    if path == "/api/direction-catalog":
                        return self.output(200, self.app.direction_catalog(selected[0]))
                    return self.output(200, self.app.direction_detail(path.removeprefix("/api/direction-catalog/"), selected[0]))
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
                    return self.output(200, self.app.ai.options())
                ai_stream = re.fullmatch(r"/api/ai/tasks/(AIT-[0-9a-f]{12})/stream", path)
                if ai_stream:
                    return self.event_stream(ai_stream.group(1))
                if path == "/api/ai/tasks":
                    selected = query.get("case_id", [None])
                    if len(selected) != 1:
                        raise ApiError(400, "案例筛选格式无效。")
                    return self.output(200, {"tasks": self.app.ai.list_tasks(selected[0])})
                ai_match = re.fullmatch(r"/api/ai/tasks/(AIT-[0-9a-f]{12})", path)
                if ai_match:
                    return self.output(200, {"task": self.app.ai.get(ai_match.group(1))})
                if path == "/api/performance/summary":
                    return self.output(200, self.app.performance.summary())
                if path == "/api/performance/analytics":
                    return self.output(200, self.app.performance.analytics())
                if path == "/api/performance/videos":
                    videos = self.app.performance.videos(self.app.video_case_links())
                    commented = self.app.reviews.video_counts()
                    for video in videos:
                        found = commented.get(video["video_id"])
                        if found:
                            video["review_comments"], video["review_open"] = found["comments"], found["open_questions"]
                    return self.output(200, {"videos": videos})
                video_match = re.fullmatch(r"/api/performance/videos/(\d{6,25})", path)
                if video_match:
                    video = self.app.performance.video(video_match.group(1), self.app.video_case_links())
                    if not video:
                        raise ApiError(404, "后台数据里没有这条视频。")
                    return self.output(200, {"video": video})
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
                files = {"/": "index.html", "/app.js": "app.js", "/navigation.js": "navigation.js", "/library.js": "library.js", "/bulk.js": "bulk.js", "/detail.js": "detail.js", "/transcript-sync.js": "transcript-sync.js", "/delete.js": "delete.js", "/directions.js": "directions.js", "/editor.js": "editor.js", "/native.js": "native.js", "/ai.js": "ai.js", "/performance.js": "performance.js", "/perf-dashboard.js": "perf-dashboard.js", "/reviews.js": "reviews.js", "/style.css": "style.css", "/favicon.svg": "favicon.svg"}
                files.update({"/" + name: name for name in ("shell.js", "shell.css", "settings.js", "settings.css", "global-search.js", "global-search.css", "navigation.css", "library.css", "detail.css", "inline-editor.js", "inline-editor.css", "direction-picker.js", "direction-picker.css", "home.js", "home.css", "material-basket.js", "material-basket.css", "scripts.js", "scripts.css", "writing.css", "reviews-layout.css")})
                if path in files:
                    return self.file(APP_ROOT / "static" / files[path])
                if path == "/directions.css":
                    return self.file(APP_ROOT / "static" / "directions.css")
                raise ApiError(404, "页面不存在。")
            if self.command == "POST":
                if path == "/api/upload":
                    post_body_unread = False  # upload handles its own bounded rejection drain.
                    return self.upload(query)
                length = self.content_length(4 * 1024 * 1024)
                try:
                    raw_body = self.rfile.read(length)
                    post_body_unread = False
                    body = json.loads(raw_body)
                except (ValueError, UnicodeError):
                    raise ApiError(400, "提交内容不是有效数据。")
                if not isinstance(body, dict):
                    raise ApiError(400, "提交内容格式错误。")
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
                if path == "/api/ai/tasks":
                    return self.output(200, {"task": self.app.create_ai_task(body)})
                if path == "/api/scripts":
                    return self.output(200, self.app.save_script(body))
                script_action = re.fullmatch(r"/api/scripts/(SCR-[0-9a-f]{12})/(status|case)", path)
                if script_action:
                    identifier, action = script_action.groups()
                    return self.output(200, self.app.save_script_status(identifier, body) if action == "status" else self.app.script_to_case(identifier, body))
                if path == "/api/materials":
                    return self.output(200, self.app.save_material(body))
                if path == "/api/material-themes":
                    return self.output(200, self.app.save_material_theme(body))
                if path == "/api/plans":
                    return self.output(200, self.app.save_plan(body))
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
        except ApiError as exc:
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
            self.output(exc.status, {"error": str(exc)})
        except DirectionError as exc:
            return self.output(exc.status, {"error": str(exc)})
        except CreationError as exc:
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

    def file(self, path, download=False):
        total = path.stat().st_size
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
        self.headers_common()
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
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = stream.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)


class WorkbenchHTTPServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR permits another listener on the same address. Keep
    # one owner for the workbench port while retaining normal POSIX restarts.
    allow_reuse_address = os.name != "nt"
    allow_reuse_port = False

    def __init__(self, *args, **kwargs):
        self._write_gate = threading.Condition()
        self._active_writes = 0
        self._shutting_down = False
        super().__init__(*args, **kwargs)

    @property
    def shutting_down(self):
        with self._write_gate:
            return self._shutting_down

    def begin_write(self):
        with self._write_gate:
            if self._shutting_down:
                raise ApiError(409, "工作台正在重新启动，请稍后重试。")
            self._active_writes += 1

    def end_write(self):
        with self._write_gate:
            self._active_writes -= 1
            self._write_gate.notify_all()

    def prepare_shutdown(self):
        with self._write_gate:
            if self._shutting_down:
                raise ApiError(409, "工作台已经在关闭，请等待完成。")
            if self._active_writes:
                raise ApiError(409, "工作台正在保存或处理操作，请完成后重试。")
            # Reserve admission before checking AI. No gate is held while
            # taking the app/AI lock, so finishing writes cannot deadlock it.
            self._shutting_down = True
        try:
            tasks = self.app.ai.list_tasks()
            if any(task.get("status") in {"queued", "running", "stopping"}
                   or task.get("busy") or task.get("stopping") for task in tasks):
                raise ApiError(409, "AI 协作仍在运行或停止中，请完成后再重新启动。")
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
            self.app.ai.shutdown()
            close = getattr(self.app.ai.bridge, "close", None)
            if callable(close):
                close()
        super().server_close()


def make_server(skill_root, runtime_root, port=8767, allow_open=True, ai_bridge=None, performance_sources=None):
    server = WorkbenchHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    if performance_sources is None:
        performance_sources = DEFAULT_CONFIG.get("performance_sources", [])
    try:
        server.app = Workbench(skill_root, runtime_root, allow_open, ai_bridge=ai_bridge,
                               performance_sources=performance_sources)
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
