"""Immutable, checked carousel ZIPs and the shared collection list.

Only a complete group that a person confirmed can be packaged. The package is a
snapshot: subsequent image making changes its current flag, never its contents.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4
import zipfile

from direction_store import fingerprint, now
from elevenlabs_client import VoiceError
from production_store import (CAROUSEL_ID, carousel_input_signature, carousel_confirmation_current,
                              manual_completion_current, skill_delivery_reviewable)
from workbench_carousel_stills import IMAGE_TYPES
from task_activity import activity_label, record_activity, safe_error
from media_integrity_cache import INTEGRITY_CACHE, DigestMismatchError
from carousel_delivery_retention import delivery_folder, folder_size, remove_delivery_folder, receipt_file, _regular_path
from carousel_delivery_copies import cleanup_delivery_copies
from carousel_delivery_backups import cleanup_delivery_backups
from carousel_delivery_operations import cleanup_delivery_operations, operation_delivery_history

DELIVERY_ID = re.compile(r"DLV-[0-9a-f]{12}")
REQUEST_ID = re.compile(r"[\w-]{10,100}")
MAX_IMAGE_BYTES = 30 * 1024 * 1024
TEXT_FILES = {"caption_en.txt", "caption_zh.txt", "manifest.json", "领取说明.txt"}


def _selected_outputs(item):
    selected = []
    numbers = set()
    for slide in sorted(item.get("slides", []), key=lambda value: value["number"]):
        number = slide.get("number")
        output = next((entry for entry in slide.get("outputs", []) if entry.get("id") == slide.get("output_id")), None)
        aspect = item.get("image_aspect", "3:4")
        if (type(number) is not int or number < 1 or number in numbers or not output or
                (not slide.get("base") and output.get("origin") != "external_generated_layout") or
                output.get("approved") is not True or output.get("stale") or output.get("input_changed") or
                aspect not in {"3:4", "9:16"} or output.get("aspect") != aspect or
                output.get("input_signature") != carousel_input_signature(item, slide, aspect) or
                not carousel_confirmation_current(item, slide, output)):
            return None
        numbers.add(number)
        selected.append((slide, output))
    return selected or None


def carousel_selection_signature(item):
    """Bind the selected versions to all adopted words and current making notes."""
    rows = []
    for slide in sorted(item.get("slides", []), key=lambda value: value["number"]):
        output = next((entry for entry in slide.get("outputs", []) if entry.get("id") == slide.get("output_id")), None)
        if not output:
            return None
        rows.append({"number": slide["number"], "title_lines": slide.get("title_lines"), "body": slide.get("body"),
                     "input_signature": carousel_input_signature(item, slide, item.get("image_aspect", "3:4")),
                     "output_id": output["id"], "sha256": output["sha256"]})
    if not rows:
        return None
    return fingerprint({"script_id": item["script_id"], "script_version": item["script_version"],
                        "aspect": item.get("image_aspect", "3:4"), "caption_en": item.get("caption_en", ""),
                        "caption_zh": item.get("caption_zh", ""), "slides": rows})


def _filename(item, identifier):
    def clean(value):
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", str(value)).strip(" .")[:50] or "图文"
    return f"图文-{clean(item.get('product', ''))}-{clean(item.get('title', ''))}-{identifier}.zip"


def _manifest(identifier, record):
    value = {"schema_version": record.get("schema_version", 1), "carousel_id": identifier,
            **{key: copy.deepcopy(record[key]) for key in ("id", "created_at", "script_id", "script_version", "aspect",
               "caption_en", "caption_zh", "slides", "selection_signature", "filename")}}
    for key in ("requirements", "attachments", "external_archives", "source", "verification_scope", "publication_status"):
        if key in record:
            value[key] = copy.deepcopy(record[key])
    return value


def _reviewable_video_edit(edit):
    """Skill delivery and legacy engineering checks both require a saved, complete result."""
    if edit.get("completion_policy") == "skill_delivery":
        return skill_delivery_reviewable(edit)
    return (edit.get("status") == "awaiting_review" and
            (edit.get("validation") or {}).get("status") == "awaiting_review")


class CarouselDeliveryMixin:
    """Needs the production store, shared lock and carousel image mixins."""

    def _finish_delivery_retention(self, identifier, saved, previous=()):
        """Called only after a verified new package was durably registered."""
        try:
            result = self.prune_carousel_deliveries(identifier, retired=previous,
                receipt_refs=saved.get("receipt_refs", ()), removed_ids=saved.get("removed_ids", ()))
        except (VoiceError, OSError) as exc:
            saved["cleanup_warnings"] = [str(exc)]
            return saved
        if result["warnings"]:
            saved["cleanup_warnings"] = result["warnings"]
        return saved

    def prune_carousel_deliveries(self, identifier, *, body=None, dry_run=False,
                                 retired=(), receipt_refs=(), removed_ids=()):
        """Keep the latest checked package; remove only its retired, owned copies."""
        report = {"carousel_id": identifier, "removed_ids": [], "files": 0, "bytes": 0, "warnings": []}
        with self.lock, self.production.catalog_transaction():
            item = self.production.carousel(identifier)
            deliveries = item.get("deliveries", [])
            if not deliveries:
                return report
            keep = deliveries[-1]
            try:
                self._checked_delivery(identifier, keep, complete=True)
                parent = delivery_folder(self.production.root, identifier, keep["id"]).parent
            except (VoiceError, OSError) as exc:
                report["warnings"].append(str(exc))
                return report
            obsolete = {entry["id"]: entry for entry in (*retired, *deliveries[:-1])
                        if DELIVERY_ID.fullmatch(str(entry.get("id"))) and entry["id"] != keep["id"]}
            try:
                for entry in operation_delivery_history(self.production.root, identifier, keep["id"]):
                    obsolete.setdefault(entry["id"], entry)
            except (VoiceError, OSError, ValueError) as exc:
                report["warnings"].append(str(exc))
            unsafe = set()
            for entry in (*retired, *deliveries[:-1]):
                value = entry.get("id")
                if not DELIVERY_ID.fullmatch(str(value)):
                    report["warnings"].append("历史成品包编号无效，未按该记录删除文件。")
                elif entry.get("zip_file") != f"deliveries/{value}/{entry.get('filename', '')}":
                    unsafe.add(value)
                    report["warnings"].append("历史成品包文件记录超出所属目录，未删除。")
            candidates = set(obsolete) | {value for value in removed_ids if DELIVERY_ID.fullmatch(str(value))}
            if parent.is_dir():
                candidates.update(path.name for path in parent.iterdir()
                                  if DELIVERY_ID.fullmatch(path.name) and path.name != keep["id"])
            candidates.discard(keep["id"])
            candidates.difference_update(unsafe)
            journal = parent.parent / "delivery-cleanup.json"
            try:
                _regular_path(journal)
            except (VoiceError, OSError) as exc:
                report["warnings"].append(str(exc))
                return report
            pending = {}
            if journal.is_file() and not journal.is_symlink():
                try:
                    pending = json.loads(journal.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    report["warnings"].append("上次成品包清理记录不可读，未覆盖。")
                    return report
                candidates.update(value for value in pending.get("removed_ids", [])
                                  if DELIVERY_ID.fullmatch(str(value)) and value != keep["id"])
                for entry in pending.get("retired", []):
                    if isinstance(entry, dict) and entry.get("id") in candidates:
                        obsolete.setdefault(entry["id"], entry)
            refs = set(receipt_refs) | set(pending.get("receipt_refs", []))
            safe = []
            for value in sorted(candidates):
                try:
                    folder = delivery_folder(self.production.root, identifier, value)
                    count, size = folder_size(folder)
                except (VoiceError, OSError) as exc:
                    report["warnings"].append(str(exc))
                    continue
                safe.append(value)
                if value not in obsolete and folder.is_dir():
                    zips = [path for path in folder.glob("*.zip") if path.is_file() and not path.is_symlink()]
                    if len(zips) == 1:
                        _regular_path(zips[0])
                        digest = hashlib.sha256()
                        with zips[0].open("rb") as stream:
                            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                                digest.update(chunk)
                        obsolete[value] = {"id": value, "filename": zips[0].name, "sha256": digest.hexdigest(),
                                           "zip_file": f"deliveries/{value}/{zips[0].name}"}
                if dry_run:
                    report["files"] += count
                    report["bytes"] += size
            report["removed_ids"] = safe
            records = [{key: entry[key] for key in ("id", "filename", "sha256", "zip_file") if key in entry}
                       for value, entry in obsolete.items() if value in safe]
            if dry_run:
                copies = cleanup_delivery_copies(self.production.root, identifier, records,
                    download_roots=getattr(self, "carousel_download_roots", None), dry_run=True)
                report["files"] += copies["files"]
                report["bytes"] += copies["bytes"]
                report["warnings"].extend(copies["warnings"])
                backups = cleanup_delivery_backups(self.production.root, identifier, safe, dry_run=True)
                report["files"] += backups["files"]
                report["bytes"] += backups["bytes"]
                report["warnings"].extend(backups["warnings"])
                operations = cleanup_delivery_operations(self.production.root, identifier, safe, dry_run=True)
                report["files"] += operations["files"]
                report["bytes"] += operations["bytes"]
                report["warnings"].extend(operations["warnings"])
                return report
            if not safe and not refs:
                return report
            if len(deliveries) > 1:
                request = body or {"request_id": "delivery-prune-" + uuid4().hex,
                                   "revision": self.production.current_revision()}
                saved = self.production.mutate("carousel_delivery_prune", {**request,
                    "carousel_id": identifier, "keep_id": keep["id"], "old_ids": safe})
                refs.update(saved.get("receipt_refs", []))
            # A small retry journal is retained only when a local file cannot be removed.
            remaining = {"removed_ids": safe, "receipt_refs": sorted(refs), "retired": records}
            journal.write_text(json.dumps(remaining, ensure_ascii=False), encoding="utf-8")
            copies = cleanup_delivery_copies(self.production.root, identifier, records,
                download_roots=getattr(self, "carousel_download_roots", None))
            report["files"] += copies["files"]
            report["bytes"] += copies["bytes"]
            report["warnings"].extend(copies["warnings"])
            backups = cleanup_delivery_backups(self.production.root, identifier, safe)
            report["files"] += backups["files"]
            report["bytes"] += backups["bytes"]
            report["warnings"].extend(backups["warnings"])
            refs.update(backups["receipt_refs"])
            operations = cleanup_delivery_operations(self.production.root, identifier, safe)
            report["files"] += operations["files"]
            report["bytes"] += operations["bytes"]
            report["warnings"].extend(operations["warnings"])
            failed = []
            for value in safe:
                try:
                    count, size = remove_delivery_folder(self.production.root, identifier, value)
                    report["files"] += count
                    report["bytes"] += size
                except (VoiceError, OSError) as exc:
                    failed.append(value)
                    report["warnings"].append(str(exc))
            failed_refs = []
            referenced = {entry.get("result_ref", {}).get("file")
                          for entry in self.production.records().get("requests", {}).values() if isinstance(entry, dict)}
            backup_folder = self.production.root / "backups"
            for backup in backup_folder.glob("*.json"):
                try:
                    _regular_path(backup_folder)
                    _regular_path(backup)
                    catalog = json.loads(backup.read_text(encoding="utf-8"))
                    referenced.update((entry.get("result_ref") or {}).get("file")
                                      for entry in catalog.get("requests", {}).values() if isinstance(entry, dict))
                except (VoiceError, OSError, ValueError, AttributeError):
                    referenced.update(refs)
                    report["warnings"].append("有备份暂时无法核对，相关回执已保留。")
                    break
            for relative in sorted(refs):
                if relative in referenced:
                    continue
                try:
                    target = receipt_file(self.production.root, relative)
                    if target.is_file():
                        size = target.stat().st_size
                        target.unlink()
                        report["files"] += 1
                        report["bytes"] += size
                except (VoiceError, OSError) as exc:
                    failed_refs.append(relative)
                    report["warnings"].append(str(exc))
            if failed or failed_refs or backups["warnings"] or operations["warnings"] or copies["warnings"]:
                retry_all = bool(backups["warnings"] or operations["warnings"] or copies["warnings"])
                journal.write_text(json.dumps({"removed_ids": safe if retry_all else failed,
                    "receipt_refs": sorted(refs) if retry_all else failed_refs,
                    "retired": records}), encoding="utf-8")
            else:
                journal.unlink(missing_ok=True)
            return report

    def prune_all_carousel_deliveries(self, *, dry_run=False):
        reports = []
        for item in self.production.records().get("carousels", []):
            if item.get("deliveries"):
                try:
                    reports.append(self.prune_carousel_deliveries(item["id"], dry_run=dry_run))
                except (VoiceError, OSError) as exc:
                    reports.append({"carousel_id": item["id"], "removed_ids": [], "files": 0,
                                    "bytes": 0, "warnings": [str(exc)]})
        return {"items": reports, "removed_ids": [value for item in reports for value in item["removed_ids"]],
                "files": sum(item["files"] for item in reports),
                "bytes": sum(item["bytes"] for item in reports),
                "warnings": [warning for item in reports for warning in item["warnings"]]}

    def _delivery_path(self, identifier, record):
        if not CAROUSEL_ID.fullmatch(str(identifier)) or not DELIVERY_ID.fullmatch(str(record.get("id"))):
            raise VoiceError(404, "这份图文领取包不存在。")
        relative = record.get("zip_file")
        filename = record.get("filename")
        if (not isinstance(relative, str) or not isinstance(filename, str) or
                re.search(r'[<>:"/\\|?*\x00-\x1f]', filename) or filename != filename.strip(" .") or
                re.search(r'[\x00-\x1f]', relative) or not filename.endswith(".zip") or Path(relative).is_absolute()):
            raise VoiceError(404, "这份图文领取包的文件记录无效。")
        try:
            root = self._carousel_dir(identifier).resolve()
            folder = (root / "deliveries" / record["id"]).resolve()
            path = (root / relative).resolve()
        except (OSError, ValueError):
            raise VoiceError(404, "这份图文领取包的文件记录无效。") from None
        if not folder.is_relative_to(root) or not path.is_relative_to(folder) or path.name != filename or not path.is_file():
            raise VoiceError(404, "这份图文领取包的文件不见了，请按当前已确认版本重新整理。")
        return path

    def _checked_delivery(self, identifier, record, *, complete=False):
        record = copy.deepcopy(record)
        path = self._delivery_path(identifier, record)
        try:
            declaration = fingerprint({"kind": "carousel_delivery", "complete": complete,
                "manifest": _manifest(identifier, record) if complete else None,
                "images_only": record.get("images_only")})

            def validate(stream):
                with zipfile.ZipFile(stream) as archive:
                    expected = {slide["file"] for slide in record["slides"]}
                    if not record.get("images_only"):
                        expected |= TEXT_FILES | {entry["file"] for entry in record.get("attachments", [])}
                    if len(archive.namelist()) != len(expected) or set(archive.namelist()) != expected:
                        raise ValueError()
                    if not record.get("images_only") and json.loads(archive.read("manifest.json")) != _manifest(identifier, record):
                        raise ValueError()
                    for slide in record["slides"]:
                        if hashlib.sha256(archive.read(slide["file"])).hexdigest() != slide["sha256"]:
                            raise ValueError()
                    for entry in [] if record.get("images_only") else record.get("attachments", []):
                        if hashlib.sha256(archive.read(entry["file"])).hexdigest() != entry["sha256"]:
                            raise ValueError()
                    if not record.get("images_only"):
                        for language in ("en", "zh"):
                            if archive.read(f"caption_{language}.txt").decode("utf-8") != record[f"caption_{language}"]:
                                raise ValueError()
                        archive.read("领取说明.txt")
            INTEGRITY_CACHE.check(path, declaration, record.get("sha256"), validate if complete else None)
        except DigestMismatchError:
            raise VoiceError(409, "这份图文领取包的文件已变化，请重新整理；旧记录仍保留。") from None
        except VoiceError:
            raise
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError):
            raise VoiceError(409, "这份图文领取包不完整，请重新整理；旧记录仍保留。") from None
        return path

    def carousel_delivery_file(self, identifier, delivery_id):
        with self.lock:
            item = self.production.carousel(identifier)
            record = next((entry for entry in item.get("deliveries", []) if entry.get("id") == delivery_id), None)
            if not record:
                raise VoiceError(404, "这份领取包不属于这张图文制作单。")
        return self._checked_delivery(identifier, record)

    def carousel_delivery_live(self, item):
        selected = _selected_outputs(item)
        signature = carousel_selection_signature(item) if selected else None
        for delivery in item.get("deliveries", []):
            delivery["current"] = bool(signature and delivery.get("images_only") and delivery.get("selection_signature") == signature)
            delivery["stale"] = not delivery["current"]
        return item

    def carousel_delivery_preflight(self, identifier, body=None):
        """Read-only eligibility, including a useful first-image checkpoint."""
        with self.lock:
            item = self.production.carousel(identifier)
            self.carousel_images_live(item)
            first = next((slide for slide in item["slides"] if slide["number"] == 1), None)
            strict_first = self.carousel_first_preflight(identifier) if hasattr(self, "carousel_first_preflight") else None
            first_ready = bool(strict_first["ready"] if strict_first else first and _selected_outputs({**item, "slides": [first]}))
            complete = bool(_selected_outputs(item))
            return {"first_ready": first_ready, "complete": complete, "count": len(item["slides"]), "first_check": strict_first,
                    "message": "整组已确认，可整理领取包。" if complete else "首张已确认，可沿用风格继续余图；整组尚未达到领取条件。" if first_ready else "请先选用并确认首张。"}

    def collect_carousel_delivery(self, identifier, delivery_id):
        with self.lock:
            self.carousel_delivery_file(identifier, delivery_id)
            return self.production.mutate("carousel_collection", {"carousel_id": identifier, "delivery_id": delivery_id,
                "request_id": "download-" + uuid4().hex}, {"collected_at": now()})

    def create_carousel_delivery(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "领取参数格式无效。")
        body = {**body, "carousel_id": identifier}
        with self.lock, self.production.catalog_transaction():
            prior = self.production.replay("carousel_delivery", body)
            if prior:
                return self._finish_delivery_retention(identifier, prior)
            data = self.production.records()
            if body.get("revision") != self.production.revision(data):
                raise VoiceError(409, "制作资料已更新，请读取最新记录后重新整理。")
            if not isinstance(body.get("request_id"), str) or not REQUEST_ID.fullmatch(body["request_id"]):
                raise VoiceError(400, "缺少保存标识，请刷新后重试。")
            item = self.production.carousel(identifier)
            if item.get("archived"):
                raise VoiceError(409, "图文制作单已归档，请恢复后再整理领取包。")
            running = any(entry.get("status") == "running" for entry in item.get("generations", []))
            running = running or any(entry.get("carousel_id") == identifier for entry in getattr(self, "_image_live", {}).values())
            if running:
                raise VoiceError(409, "这张制作单还在出图，请结束后再整理领取包。")
            self.carousel_images_live(item)
            selected = _selected_outputs(item)
            if not selected:
                raise VoiceError(409, "请先逐张选用并确认整组当前成图，失效版本需重新出图。")
            for slide, output in selected:
                if output.get("workflow") == "subject_locked":
                    from carousel_subject_lock import verify_subject_lock
                    from codex_bridge import BridgeError
                    try:
                        verify_subject_lock(self._carousel_dir(identifier), output)
                    except BridgeError as exc:
                        raise VoiceError(409, f"第 {slide['number']} 张主体保护校验失败：{exc}") from None
            signature = carousel_selection_signature(item)
            activity_id = f"package:{identifier}:{body['request_id']}"
            record_activity(self, activity_id, kind="package", owner_id=identifier, source_id=body["request_id"],
                            product=item.get("product", ""), case_id=item.get("case_id"), title="成品打包 · " + activity_label(item, identifier), step="package",
                            status="running", phase="核对成品", message="正在核对已确认成图并整理领取包")
            for previous in reversed(item.get("deliveries", [])):
                if previous.get("selection_signature") == signature and previous.get("images_only"):
                    try:
                        self._checked_delivery(identifier, previous, complete=True)
                    except VoiceError:
                        continue
                    except Exception as exc:
                        record_activity(self, activity_id, status="failed", phase="核对领取包未完成", message=safe_error(exc))
                        raise
                    record = {key: value for key, value in previous.items() if key not in {"current", "stale"}}
                    record_activity(self, activity_id, source_id=record["id"])
                    try:
                        saved = self.production.mutate("carousel_delivery", body, record)
                    except Exception:
                        record_activity(self, activity_id, status="needs_attention", phase="登记待恢复", message="领取包已存在，领取记录暂未保存")
                        raise
                    record_activity(self, activity_id, source_id=record["id"], status="completed", phase="领取包已就绪",
                                    message="已核对现有领取包，可以领取", result_count=len(selected), event_key="saved")
                    return self._finish_delivery_retention(identifier, saved, item.get("deliveries", []))
            delivery_id = "DLV-" + uuid4().hex[:12]
            folder = self._carousel_dir(identifier) / "deliveries" / delivery_id
            filename = _filename(item, delivery_id)
            record = {"id": delivery_id, "created_at": now(), "script_id": item["script_id"], "script_version": item["script_version"],
                      "aspect": item.get("image_aspect", "3:4"), "caption_en": item.get("caption_en", ""),
                      "caption_zh": item.get("caption_zh", ""), "slides": [], "selection_signature": signature,
                      "zip_file": f"deliveries/{delivery_id}/{filename}", "filename": filename,
                      "images_only": True, "schema_version": 3,
                      "packaging": {"status": "running", "started_at": now()}}
            if any(output.get("workflow") in {"generation_first", "subject_locked"} for _, output in selected):
                record["workflow"] = ("subject_locked" if all(output.get("workflow") == "subject_locked" for _, output in selected)
                                      else "generation_first")
                record["review_deferred"] = any(output.get("review_deferred") for _, output in selected)
            # Keep the words and image identities actually packaged. This is a
            # catalog snapshot only; the images-only ZIP format stays unchanged.
            record["source"] = {"kind": "carousel", "carousel_id": identifier,
                "script": {key: copy.deepcopy(item.get(key)) for key in
                           ("script_id", "script_version", "product", "direction_id", "title")},
                "slides": []}
            record["source"]["script"]["source"] = {key: copy.deepcopy((item.get("source") or {}).get(key))
                for key in ("task_id", "round_id", "variant_index", "table_id") if key in (item.get("source") or {})}
            record_activity(self, activity_id, source_id=delivery_id)
            try:
                folder.mkdir(parents=True, exist_ok=False)
                payloads = []
                for slide, output in selected:
                    path = self.carousel_output(identifier, output["id"], "full")
                    if path.suffix.lower() not in IMAGE_TYPES:
                        raise VoiceError(409, "成图格式无效，请重新出图后核对。")
                    with path.open("rb") as stream:
                        payload = stream.read(MAX_IMAGE_BYTES + 1)
                    if len(payload) > MAX_IMAGE_BYTES or hashlib.sha256(payload).hexdigest() != output["sha256"]:
                        raise VoiceError(409, "成图文件已变化，请重新出图后核对。")
                    name = f"{slide['number']:02d}{path.suffix.lower()}"
                    with (folder / name).open("xb") as stream:
                        stream.write(payload)
                    record["slides"].append({"number": slide["number"], "output_id": output["id"], "sha256": output["sha256"], "file": name})
                    record["source"]["slides"].append({"number": slide["number"],
                        "title_lines": copy.deepcopy(slide.get("title_lines") or []), "body": slide.get("body", ""),
                        "output": {**{key: copy.deepcopy(output.get(key)) for key in
                                      ("id", "sha256", "aspect", "origin", "generation_id", "approved_at",
                                       "workflow", "ai_generated", "review_deferred", "subject_preservation")},
                                   "version": next((index + 1 for index, entry in enumerate(slide.get("outputs") or [])
                                                    if entry.get("id") == output["id"]), None)},
                        "base": {key: copy.deepcopy((slide.get("base") or {}).get(key))
                                 for key in ("id", "sha256") if key in (slide.get("base") or {})}})
                    payloads.append((name, payload))
                temporary = folder / ".package.tmp"
                with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                    for name, payload in payloads:
                        archive.writestr(name, payload)
                # Windows FlushFileBuffers needs a handle opened for writing.
                with temporary.open("r+b") as stream:
                    os.fsync(stream.fileno())
                os.replace(temporary, folder / filename)
                digest = hashlib.sha256()
                with (folder / filename).open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                record["sha256"] = digest.hexdigest()
                self._checked_delivery(identifier, record, complete=True)
                record["packaging"].update(status="completed", finished_at=now())
            except VoiceError as exc:
                record_activity(self, activity_id, status="failed", phase="打包未完成", message=safe_error(exc))
                raise
            except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile, RuntimeError) as exc:
                record_activity(self, activity_id, status="failed", phase="打包未完成", message=safe_error(exc))
                raise VoiceError(500, "领取包没有整理完成，原成图和已有领取包仍保留，请重试。") from None
            try:
                saved = self.production.mutate("carousel_delivery", body, record)
            except Exception:
                record_activity(self, activity_id, source_id=delivery_id, status="needs_attention", phase="登记待恢复",
                                message="领取包已整理，领取记录暂未保存", event_key="result_pending")
                raise
            record_activity(self, activity_id, source_id=delivery_id, status="completed", phase="领取包已保存",
                            message="成品领取包已整理完成", result_count=len(record["slides"]), event_key="saved")
            return self._finish_delivery_retention(identifier, saved, item.get("deliveries", []))

    def production_deliveries(self, product=None):
        with self.lock:
            catalog = self.production.public(product)
        return self._delivery_rows(catalog)

    def _delivery_rows(self, catalog):
        # Media verification uses this independent catalog snapshot, outside the write lock.
        rows = []
        for item in catalog.get("carousels", []):
            if item.get("archived"):
                continue
            self.carousel_images_live(item)
            self.carousel_delivery_live(item)
            deliveries = item.get("deliveries", [])
            ready_to_package = bool(_selected_outputs(item))
            if not ready_to_package and not deliveries:
                continue
            current = next((entry for entry in reversed(deliveries) if entry.get("current")), None)
            delivery = current or (deliveries[-1] if deliveries else None)
            row = {"kind": "carousel", **{key: item.get(key, "") for key in
                   ("id", "product", "title", "direction_id", "updated_at")}}
            # Keep historical delivery_id/published for older clients, but
            # collection requires a checked package of the current selection.
            row.update(ready_to_package=ready_to_package,
                       current_delivery_id=current["id"] if current else "",
                       delivery_available=False, delivery_error="")
            if current:
                try:
                    self._checked_delivery(item["id"], current, complete=True)
                except VoiceError as exc:
                    row["delivery_error"] = str(exc)
                else:
                    row["delivery_available"] = True
            if delivery:
                row["delivery_id"] = delivery["id"]
            publication = item.get("publication") or {}
            row["published"] = bool(delivery and publication.get("delivery_id") == delivery["id"] and
                                    publication.get("post_id") and publication.get("published_on"))
            rows.append(row)
        for job in catalog.get("jobs", []):
            if job.get("archived"):
                continue
            if manual_completion_current(job):
                receipt = job["manual_completion"]
                rows.append({"kind": "video", **{key: job.get(key, "") for key in
                            ("id", "product", "title", "direction_id", "updated_at")},
                            "timeline_name": receipt["timeline_name"], "timeline_uid": receipt["timeline_uid"],
                            "edit_id": None, "status": "approved", "approved_at": receipt["at"],
                            "completion_basis": "human_review"})
                continue
            timelines = {entry.get("unique_id"): entry for entry in job.get("timelines", []) if not entry.get("archived")}
            # A later attempt supersedes the earlier result for that same
            # timeline, including a running or failed repair. Never revive
            # a previous pass by filtering failures out before selecting.
            latest = {}
            for entry in job.get("edits", []):
                latest[entry.get("timeline_uid")] = entry
            edit = next((entry for entry in reversed(job.get("edits", []))
                         if latest.get(entry.get("timeline_uid")) is entry and
                         _reviewable_video_edit(entry) and
                         entry.get("timeline_uid") in timelines and entry.get("timeline_name") and
                         job.get("selected_take") and
                         timelines[entry["timeline_uid"]].get("take_id") == job["selected_take"]), None)
            if edit:
                rows.append({"kind": "video", **{key: job.get(key, "") for key in
                            ("id", "product", "title", "direction_id", "updated_at")},
                            "timeline_name": edit["timeline_name"], "timeline_uid": edit["timeline_uid"],
                            "edit_id": edit.get("id"), "status": "awaiting_review",
                            "approved_at": (edit.get("approval") or {}).get("at", "")})
        rows.sort(key=lambda value: (value.get("updated_at", ""), value["id"]), reverse=True)
        return {"items": rows}
