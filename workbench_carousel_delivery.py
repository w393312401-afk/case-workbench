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
from production_store import CAROUSEL_ID, carousel_input_signature, carousel_review_current
from workbench_carousel_stills import IMAGE_TYPES

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
                not carousel_review_current(item, slide, output)):
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


class CarouselDeliveryMixin:
    """Needs the production store, shared lock and carousel image mixins."""

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
        path = self._delivery_path(identifier, record)
        try:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != record.get("sha256"):
                raise VoiceError(409, "这份图文领取包的文件已变化，请重新整理；旧记录仍保留。")
            if complete:
                with zipfile.ZipFile(path) as archive:
                    expected = TEXT_FILES | {slide["file"] for slide in record["slides"]} | {entry["file"] for entry in record.get("attachments", [])}
                    if len(archive.namelist()) != len(expected) or set(archive.namelist()) != expected:
                        raise ValueError()
                    if json.loads(archive.read("manifest.json")) != _manifest(identifier, record):
                        raise ValueError()
                    for slide in record["slides"]:
                        if hashlib.sha256(archive.read(slide["file"])).hexdigest() != slide["sha256"]:
                            raise ValueError()
                    for entry in record.get("attachments", []):
                        if hashlib.sha256(archive.read(entry["file"])).hexdigest() != entry["sha256"]:
                            raise ValueError()
                    for language in ("en", "zh"):
                        if archive.read(f"caption_{language}.txt").decode("utf-8") != record[f"caption_{language}"]:
                            raise ValueError()
                    archive.read("领取说明.txt")
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
            delivery["current"] = bool(signature and delivery.get("selection_signature") == signature)
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
                    "message": "整组已确认，可整理领取包。" if complete else "首张已确认，可沿用风格继续余图；整组尚未达到领取条件。" if first_ready else "请先核对并确认首张；已有成图可单独重新核对。"}

    def collect_carousel_delivery(self, identifier, delivery_id):
        with self.lock:
            self.carousel_delivery_file(identifier, delivery_id)
            return self.production.mutate("carousel_collection", {"carousel_id": identifier, "delivery_id": delivery_id,
                "request_id": "download-" + uuid4().hex}, {"collected_at": now()})

    def _delivery_records(self, item, selected):
        """Package current words and reviews; original production archives remain explicit external references."""
        payloads, archives = [], {}
        requirements = copy.deepcopy(item.get("requirements", {}))
        source_copy = next((output.get("source_copy") for _, output in selected if output.get("source_copy")), None)
        locked_copy = copy.deepcopy(source_copy or item.get("copy") or {})
        locked_copy.update(content_type="photo_carousel", direction_id=item.get("direction_id", ""),
                           caption_en=item.get("caption_en", ""), caption_zh=item.get("caption_zh", ""))
        locked_copy["slides"] = [{key: copy.deepcopy(slide.get(key)) for key in
                                 ("number", "title_lines", "body", "title_zh", "body_zh")} for slide, _ in selected]
        records = {"copy.json": locked_copy, "records/requirements.json": requirements}
        for slide, output in selected:
            review_runs = [{key: copy.deepcopy(run[key]) for key in
                           ("id", "status", "file_sha256", "input_signature", "requirements_signature", "scope", "started_at", "finished_at", "error", "stale") if key in run}
                           for run in output.get("review_runs", [])]
            records[f"records/reviews/{slide['number']:02d}.json"] = {
                "output_id": output["id"], "file_sha256": output["sha256"],
                "input_signature": output["input_signature"], "review": output.get("review"),
                "reviews": review_runs, "approval": {"approved": output.get("approved"), "at": output.get("approved_at"),
                                                      "actor": output.get("approval_actor"), "note": output.get("note", "")},
                "provenance": output.get("provenance"), "origin": output.get("origin")}
            latest = next((run for run in output.get("review_runs", []) if run.get("id") == output.get("latest_review_id")), None)
            if latest and latest.get("folder"):
                root = self._carousel_dir(item["id"]).resolve()
                review_folder = (root / latest["folder"]).resolve()
                if not review_folder.is_relative_to(root / "reviews"):
                    raise VoiceError(409, "核对记录路径无效，请重新核对后整理。")
                for filename in ("input.json", "prompt.txt", "receipt.json"):
                    source = (review_folder / filename).resolve()
                    if not source.is_relative_to(review_folder) or not source.is_file() or source.stat().st_size > 4 * 1024 * 1024:
                        raise VoiceError(409, "本次核对记录不完整，请重新核对后整理。")
                    payloads.append((f"records/reviews/{slide['number']:02d}-{filename}", source.read_bytes()))
            for entry in output.get("archive_refs", (output.get("source_archive") or {}).get("files", [])):
                path = Path(entry.get("path", ""))
                if not path.is_absolute() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("sha256"):
                    raise VoiceError(409, "来源档案已变化或缺失，请重新登记后核对；已有领取包仍保留。")
                archives[str(path)] = {"kind": "external_archive", "path": str(path), "sha256": entry["sha256"],
                                       "size": path.stat().st_size, "included_in_package": False}
        for name, value in records.items():
            payloads.append((name, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")))
        return payloads, list(archives.values())

    def create_carousel_delivery(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "领取参数格式无效。")
        body = {**body, "carousel_id": identifier}
        with self.lock:
            prior = self.production.replay("carousel_delivery", body)
            if prior:
                return prior
            data = self.production.read()
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
                raise VoiceError(409, "请先逐张核对并人工确认整组当前成图，失效或比例不一致的版本需重新出图。")
            signature = carousel_selection_signature(item)
            for previous in reversed(item.get("deliveries", [])):
                if previous.get("selection_signature") == signature:
                    try:
                        self._checked_delivery(identifier, previous, complete=True)
                    except VoiceError:
                        continue
                    record = {key: value for key, value in previous.items() if key not in {"current", "stale"}}
                    return self.production.mutate("carousel_delivery", body, record)
            delivery_id = "DLV-" + uuid4().hex[:12]
            folder = self._carousel_dir(identifier) / "deliveries" / delivery_id
            filename = _filename(item, delivery_id)
            record = {"id": delivery_id, "created_at": now(), "script_id": item["script_id"], "script_version": item["script_version"],
                      "aspect": item.get("image_aspect", "3:4"), "caption_en": item.get("caption_en", ""),
                      "caption_zh": item.get("caption_zh", ""), "slides": [], "selection_signature": signature,
                      "zip_file": f"deliveries/{delivery_id}/{filename}", "filename": filename,
                      "packaging": {"status": "running", "started_at": now()}}
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
                    payloads.append((name, payload))
                if any(output.get("origin") or output.get("provenance") for _, output in selected):
                    records, archives = self._delivery_records(item, selected)
                    record.update(schema_version=2, requirements=copy.deepcopy(item.get("requirements", {})),
                                  attachments=[{"file": name, "sha256": hashlib.sha256(payload).hexdigest()} for name, payload in records],
                                  external_archives=archives, source="当前选定成品与锁定文案；来源制作资料保留在外部本地档案。",
                                  verification_scope="生成视觉、文字、商品对照与实拍证据分别记录；不声称原实拍商品逐像素保真。",
                                  publication_status="unpublished_at_export")
                    for name, payload in records:
                        destination = folder / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(payload)
                    payloads.extend(records)
                manifest = json.dumps(_manifest(identifier, record), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
                instructions = ("按 01、02… 的张序上传图片，使用 caption_en.txt 的发布文案。\n"
                                "caption_zh.txt 是中文对照；请在发布平台自行选择商品链接。\n"
                                "本包只保存整理时已人工确认的图片与文案，重做不会改变此包。\n"
                                "发布后可回工作台登记帖子信息。\n")
                texts = {"caption_en.txt": record["caption_en"], "caption_zh.txt": record["caption_zh"],
                         "manifest.json": manifest, "领取说明.txt": instructions}
                for name, content in texts.items():
                    with (folder / name).open("x", encoding="utf-8", newline="") as stream:
                        stream.write(content)
                temporary = folder / ".package.tmp"
                with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                    for name, payload in payloads:
                        archive.writestr(name, payload)
                    for name, content in texts.items():
                        archive.writestr(name, content.encode("utf-8"))
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
            except VoiceError:
                raise
            except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile, RuntimeError):
                raise VoiceError(500, "领取包没有整理完成，原成图和已有领取包仍保留，请重试。") from None
            return self.production.mutate("carousel_delivery", body, record)

    def production_deliveries(self, product=None):
        with self.lock:
            catalog = self.production.public(product)
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
                timelines = {entry.get("unique_id"): entry for entry in job.get("timelines", [])}
                # A later attempt supersedes the earlier result for that same
                # timeline, including a running or failed repair. Never revive
                # a previous pass by filtering failures out before selecting.
                latest = {}
                for entry in job.get("edits", []):
                    latest[entry.get("timeline_uid")] = entry
                edit = next((entry for entry in reversed(job.get("edits", []))
                             if latest.get(entry.get("timeline_uid")) is entry and
                             entry.get("status") == "awaiting_review" and
                             (entry.get("validation") or {}).get("status") == "awaiting_review" and
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
