"""Register existing finished carousels without inventing generation calls or approvals."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
from uuid import uuid4

from PIL import Image, ImageChops, ImageStat

from direction_store import fingerprint, now
from elevenlabs_client import VoiceError
from production_store import CAROUSEL_ID, carousel_input_signature, carousel_requirements
from workbench_carousel_stills import _thumbnail

MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_ARCHIVE_BYTES = 300 * 1024 * 1024
NUMBERED_IMAGE = re.compile(r"(\d{1,3})\.(png|jpe?g|webp)", re.I)


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _checked_image(path, aspect=None):
    try:
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
            raise ValueError()
        with Image.open(path) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
                raise ValueError()
            width, height = image.size
            if min(width, height) < 32 or width * height > 40_000_000:
                raise ValueError()
            image.verify()
        with Image.open(path) as image:
            image.load()  # verify alone does not decode every pixel.
        if aspect and abs(width / height - {"3:4": .75, "9:16": .5625}[aspect]) / {"3:4": .75, "9:16": .5625}[aspect] > .025:
            raise VoiceError(400, f"{path.name} 的比例为 {width}×{height}，不符合 {aspect}。")
        return width, height
    except (OSError, ValueError, KeyError, Image.DecompressionBombError) as exc:
        raise VoiceError(400, f"{path.name} 无法完整解码，或文件大小、尺寸不符合要求。") from exc


def _locked_copy(item):
    return {"script_id": item["script_id"], "script_version": item["script_version"],
            "slides": [{key: slide.get(key) for key in ("number", "title_lines", "body", "title_zh", "body_zh")} for slide in item["slides"]],
            "caption_en": item.get("caption_en", ""), "caption_zh": item.get("caption_zh", "")}


class CarouselFinishedImportMixin:
    """Needs production, the shared lock and carousel_finished_root (read only)."""

    def _finished_folder(self, value):
        if not isinstance(value, str) or not value.strip() or len(value) > 1000 or "\x00" in value:
            raise VoiceError(400, "请选择已有成品所在的目录。")
        root = Path(self.carousel_finished_root).resolve()
        try:
            folder = (root / value).resolve()
            if not folder.is_relative_to(root) or not folder.is_dir():
                raise ValueError()
        except (OSError, ValueError):
            raise VoiceError(400, "成品目录须在工作台的图文成品目录内，不能越界读取。") from None
        return root, folder

    @staticmethod
    def _finished_file(folder, relative):
        try:
            path = (folder / relative).resolve()
            if not path.is_relative_to(folder) or not path.is_file():
                raise ValueError()
        except (OSError, ValueError):
            raise VoiceError(400, "成品文件不存在或越出了所选目录。") from None
        return path

    def finished_carousel_folders(self):
        root = Path(self.carousel_finished_root).resolve()
        folders = []
        if root.is_dir():
            for path in sorted(root.iterdir()):
                if path.is_dir() and path.resolve().is_relative_to(root) and (path / "copy.json").is_file():
                    folders.append({"directory": path.relative_to(root).as_posix(), "name": path.name})
        return {"root": str(root), "folders": folders[:200]}

    def import_finished_images(self, identifier, body):
        if not CAROUSEL_ID.fullmatch(str(identifier)) or not isinstance(body, dict):
            raise VoiceError(400, "导入成品参数无效。")
        body = {**body, "carousel_id": identifier}
        prior = self.production.replay("carousel_import_finished", body)
        if prior:
            return prior
        started_at = now()
        _, folder = self._finished_folder(body.get("directory", body.get("folder")))
        item = self.production.carousel(identifier)
        if item.get("archived"):
            raise VoiceError(409, "制作单已归档，请恢复后再导入。")
        copy_path = self._finished_file(folder, "copy.json")
        try:
            if copy_path.stat().st_size > 1_000_000:
                raise ValueError()
            source_copy = json.loads(copy_path.read_text(encoding="utf-8-sig"))
            if not isinstance(source_copy, dict) or not isinstance(source_copy.get("slides"), list):
                raise ValueError()
        except (OSError, ValueError):
            raise VoiceError(400, "copy.json 无法读取，请保留已锁定的原文案。") from None
        if source_copy.get("direction_id", item.get("direction_id")) != item.get("direction_id"):
            raise VoiceError(409, "这套成品的方向与当前制作单不一致。")
        expected = _locked_copy(item)
        compare_keys = ("number", "title_lines", "body", "title_zh", "body_zh")
        received = [{key: slide.get(key) for key in compare_keys} for slide in source_copy["slides"] if isinstance(slide, dict)]
        if (received != expected["slides"] or source_copy.get("caption_en", "") != expected["caption_en"] or
                source_copy.get("caption_zh", "") != expected["caption_zh"]):
            raise VoiceError(409, "copy.json 与当前制作单的逐张文案或发布文案不一致，未导入。")
        manifest = {}
        manifest_path = folder / "manifest.json"
        if manifest_path.exists():
            manifest_path = self._finished_file(folder, "manifest.json")
            try:
                if manifest_path.stat().st_size > 2_000_000:
                    raise ValueError()
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                if not isinstance(manifest, dict):
                    raise ValueError()
            except (OSError, ValueError):
                raise VoiceError(400, "来源 manifest.json 无法读取。") from None
            if manifest.get("carousel_reference", manifest.get("carousel_id", identifier)) != identifier:
                raise VoiceError(409, "来源记录绑定了另一张制作单，未导入。")
            if manifest.get("copy_sha256") and manifest["copy_sha256"] != _hash(copy_path):
                raise VoiceError(409, "copy.json 已变化，与来源记录的校验值不一致。")
        snapshot_path = folder / "variant-snapshot.json"
        if snapshot_path.exists():
            try:
                snapshot = json.loads(self._finished_file(folder, "variant-snapshot.json").read_text(encoding="utf-8-sig"))
                if snapshot.get("script_id") != item["script_id"] or snapshot.get("script_version") != item["script_version"]:
                    raise VoiceError(409, "来源快照的脚本版本与当前制作单不一致。")
            except (ValueError, AttributeError):
                raise VoiceError(400, "来源快照无法读取。") from None
        generation_source = manifest.get("image_generation") or {}
        manifest_model = generation_source.get("image_model") if isinstance(generation_source, dict) else None
        manifest_model = manifest_model.strip() if isinstance(manifest_model, str) and manifest_model.strip() else None
        provided_model = body.get("model")
        if provided_model is not None and (not isinstance(provided_model, str) or len(provided_model) > 100):
            raise VoiceError(400, "模型说明须是 100 字以内的文字，未知时留空。")
        provided_model = provided_model.strip() if provided_model else None
        if manifest_model and provided_model and manifest_model != provided_model:
            raise VoiceError(409, "填写的模型与来源回执记录不一致，请核对后填写。")
        model = manifest_model or provided_model
        model_source = "source_manifest" if manifest_model else "operator_reported" if provided_model else "unknown"
        requirements = body.get("requirements", item.get("requirements"))
        if requirements is not None:
            if not isinstance(requirements, dict):
                raise VoiceError(400, "制作要求格式无效。")
            requirements = copy.deepcopy(requirements)
            hand_policy = manifest.get("hand_policy") or {}
            if isinstance(hand_policy, dict) and requirements.get("mode") == "generated_visual":
                if "authorized_hand_slides" not in requirements and isinstance(hand_policy.get("authorized_change_slides"), list):
                    requirements["authorized_hand_slides"] = hand_policy["authorized_change_slides"]
                if "no_hand_slides" not in requirements and type(hand_policy.get("no_hand_slide")) is int:
                    requirements["no_hand_slides"] = [hand_policy["no_hand_slide"]]
            requirements = carousel_requirements({**item, "requirements": requirements})
        bound_item = {**item, **({"requirements": requirements, "image_aspect": requirements["aspect"]} if requirements is not None else {})}
        aspect = bound_item.get("image_aspect", "3:4")
        wanted = body.get("slides", [slide["number"] for slide in item["slides"]])
        numbers = {slide["number"] for slide in item["slides"]}
        if (not isinstance(wanted, list) or not wanted or any(type(n) is not int or n not in numbers for n in wanted) or len(set(wanted)) != len(wanted)):
            raise VoiceError(400, "要导入的张号无效。")
        grouped = {}
        for subfolder in (folder / "PNG", folder / "JPG", folder):
            if not subfolder.is_dir():
                continue
            for path in sorted(subfolder.iterdir()):
                match = NUMBERED_IMAGE.fullmatch(path.name)
                if match and path.is_file():
                    path = self._finished_file(folder, path.relative_to(folder))
                    grouped.setdefault(int(match[1]), []).append(path)
        if set(grouped) - numbers:
            raise VoiceError(400, "成品目录包含制作单以外的张号，请核对顺序。")
        if any(number not in grouped for number in wanted):
            raise VoiceError(400, "实际成品张数不足，PNG/JPG 发布副本各算同一张；未凑成完整组。")
        ready = []
        image_records = manifest.get("images", [])
        if not isinstance(image_records, list) or any(not isinstance(entry, dict) or type(entry.get("number")) is not int for entry in image_records):
            raise VoiceError(400, "来源图片记录须逐张列出正确编号。")
        if len({entry["number"] for entry in image_records}) != len(image_records) or any(entry["number"] not in numbers for entry in image_records):
            raise VoiceError(400, "来源图片记录有重复或制作单以外的张号。")
        manifest_images = {entry["number"]: entry for entry in image_records}
        specification = manifest.get("required_specification") or {}
        if not isinstance(specification, dict):
            raise VoiceError(400, "来源图片规格格式无效。")
        if specification.get("aspect") and specification["aspect"] != aspect:
            raise VoiceError(409, "来源图片比例与当前制作要求不一致。")
        for number in sorted(wanted):
            paths = sorted(grouped[number], key=lambda p: (p.suffix.lower() != ".png", p.parent.name != "PNG", p.name))
            primary = paths[0]
            size = _checked_image(primary, aspect)
            declared_size = (specification.get("width"), specification.get("height"))
            if all(type(dimension) is int for dimension in declared_size) and size != declared_size:
                raise VoiceError(409, f"第 {number} 张的实际尺寸与来源规格不一致。")
            image_info = manifest_images.get(number, {})
            if image_info.get("width") is not None and image_info.get("height") is not None and size != (image_info["width"], image_info["height"]):
                raise VoiceError(409, f"第 {number} 张的实际尺寸与来源图片记录不一致。")
            hashes = []
            for path in paths:
                if _checked_image(path, aspect) != size:
                    raise VoiceError(400, f"第 {number} 张的 PNG/JPG 副本尺寸不一致。")
                with Image.open(primary) as original, Image.open(path) as duplicate:
                    delta = ImageStat.Stat(ImageChops.difference(original.convert("RGB"), duplicate.convert("RGB")))
                    if max(delta.rms) > 16:
                        raise VoiceError(400, f"第 {number} 张有同编号但不同画面的文件，请先确认发布副本。")
                digest = _hash(path)
                entry = manifest_images.get(number, {})
                key = "png_sha256" if path.suffix.lower() == ".png" else "jpg_sha256"
                if entry.get(key) and digest != entry[key]:
                    raise VoiceError(409, f"第 {number} 张的文件已变化，与来源记录不一致。")
                hashes.append({"source_file": path.relative_to(folder).as_posix(), "sha256": digest, "size": path.stat().st_size})
            ready.append((number, primary, size, hashes))
        archives, total = [], 0
        for name in ("native", "references", "qa", "sources"):
            subfolder = folder / name
            if subfolder.is_dir():
                for path in sorted(subfolder.rglob("*")):
                    if path.is_file():
                        relative = path.relative_to(folder).as_posix()
                        path = self._finished_file(folder, relative)
                        total += path.stat().st_size
                        if len(archives) >= 600 or total > MAX_ARCHIVE_BYTES:
                            raise VoiceError(400, "来源档案过大，请整理本次制作单的必要资料。")
                        archives.append({"file": relative, "path": str(path), "sha256": _hash(path), "size": path.stat().st_size})
        for name in ("copy.json", "manifest.json", "variant-snapshot.json", "caption_en.txt", "caption_zh.txt"):
            if (folder / name).is_file():
                path = self._finished_file(folder, name)
                archives.append({"file": name, "path": str(path), "sha256": _hash(path), "size": path.stat().st_size})
        content_signature = fingerprint({"carousel_id": identifier, "copy_sha256": _hash(copy_path), "requirements": requirements,
                "inputs": fingerprint(expected), "images": [{"number": number, "copies": hashes} for number, _, _, hashes in ready],
                "archive_files": [{"file": entry["file"], "sha256": entry["sha256"]} for entry in archives], "model": model, "model_source": model_source})
        existing = next((entry for entry in item.get("finished_imports", []) if entry.get("content_signature") == content_signature), None)
        if existing:
            # Use the store transaction for a durable replay; do not create duplicate files.
            return self.production.mutate("carousel_import_finished", body, {"import": existing})
        import_id, stamp = "IMP-" + uuid4().hex[:12], now()
        destination = self.production.root / "carousels" / identifier / "imports" / import_id
        destination.mkdir(parents=True, exist_ok=False)
        shutil.copyfile(copy_path, destination / "copy.json")
        outputs = []
        by_number = {slide["number"]: slide for slide in item["slides"]}
        source_archive = {"kind": "external_archive", "root": str(folder), "files": archives}
        for number, primary, size, hashes in ready:
            output_id = "OUT-" + uuid4().hex[:12]
            name = f"{number:02d}{primary.suffix.lower()}"
            target = destination / "images" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(primary, target)
            primary_hash = _hash(primary)
            if _hash(target) != primary_hash:
                raise VoiceError(409, "复制期间成品文件发生变化，未登记。")
            thumb = destination / "thumbs" / f"{number:02d}.jpg"
            _thumbnail(target, thumb)
            copies = []
            for entry in hashes:
                source = self._finished_file(folder, entry["source_file"])
                copy_target = destination / "copies" / entry["source_file"]
                copy_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, copy_target)
                if _hash(copy_target) != entry["sha256"]:
                    raise VoiceError(409, "复制期间发布副本发生变化，未登记。")
                copies.append({**entry, "file": copy_target.relative_to(self.production.root / "carousels" / identifier).as_posix(), "width": size[0], "height": size[1]})
            refs = []
            ref = folder / "sources" / f"{number:02d}.png"
            if ref.is_file():
                ref = self._finished_file(folder, ref.relative_to(folder))
                ref_size = _checked_image(ref)
                reference_target = destination / "product-references" / f"{number:02d}.png"
                reference_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ref, reference_target)
                refs.append({"kind": "product_reference", "file": reference_target.relative_to(self.production.root / "carousels" / identifier).as_posix(),
                             "sha256": _hash(reference_target), "source_file": ref.relative_to(folder).as_posix(), "width": ref_size[0], "height": ref_size[1],
                             "source_candidate_id": manifest_images.get(number, {}).get("source_candidate_id")})
            slide = by_number[number]
            outputs.append({"id": output_id, "slide": number, "generation_id": None, "origin": "external_generated_layout", "import_id": import_id,
                "file": target.relative_to(self.production.root / "carousels" / identifier).as_posix(),
                "raw": target.relative_to(self.production.root / "carousels" / identifier).as_posix(),
                "thumb": thumb.relative_to(self.production.root / "carousels" / identifier).as_posix(), "width": size[0], "height": size[1], "sha256": primary_hash,
                "aspect": aspect, "base_id": (slide.get("base") or {}).get("id"), "base_sha256": None,
                # Imported artwork keeps its own contract below; it must not
                # become the next generation's working requirements.
                "input_signature": carousel_input_signature(item, slide, aspect), "input_changed": False, "created_at": stamp,
                "instruction": (requirements or {}).get("instruction", ""), "requirements_snapshot": carousel_requirements(bound_item, slide, aspect),
                "copy_sha256": _hash(copy_path), "copy_file": (destination / "copy.json").relative_to(self.production.root / "carousels" / identifier).as_posix(),
                "source_copy": copy.deepcopy(source_copy), "copies": copies, "product_reference_files": refs,
                "provenance": {"kind": "external_generated_layout", "model": model, "model_source": model_source, "imported_at": stamp, "source_folder": str(folder),
                     "source_manifest_sha256": _hash(manifest_path) if manifest_path.is_file() else None,
                     "source_note": "外部完成的生成及排版结果；本次只复制登记，未调用生图。", "source_product_pixel_preservation_claimed": False},
                "source_archive": copy.deepcopy(source_archive), "archive_refs": copy.deepcopy(archives), "approved": False,
                "stages": {"prepare": {"status": "completed", "started_at": started_at, "finished_at": stamp},
                    "generation": {"status": "skipped", "note": "外部已完成；本次未调用生图。"},
                    "layout": {"status": "skipped", "note": "原成品直接登记，未改动图片。"},
                    "review": {"status": "pending"}, "confirmation": {"status": "pending"}, "package": {"status": "pending"}},
                "review": {"status": "pending", "summary": "已保存现有成品，等待独立核对。", "checks": [], "issues": []}})
        if _hash(copy_path) != _hash(destination / "copy.json"):
            raise VoiceError(409, "导入期间 copy.json 发生变化，未登记。")
        finished_at = now()
        first_output = next((output for output in outputs if output["slide"] == 1), None)
        for output in outputs:
            output["stages"]["prepare"]["finished_at"] = finished_at
            output["requirements_signature"] = fingerprint(output["requirements_snapshot"])
            if first_output and output["slide"] != 1:
                output["style_reference"] = {"output_id": first_output["id"], "sha256": first_output["sha256"], "scope": "style_only"}
        record = {"id": import_id, "content_signature": content_signature, "imported_at": finished_at, "started_at": started_at, "finished_at": finished_at, "source_folder": str(folder),
                  "script_id": item["script_id"], "script_version": item["script_version"], "logical_count": len(outputs), "copy_sha256": _hash(copy_path),
                  "source": "external_generated_layout", "model": model, "status": "awaiting_review", "output_ids": [entry["id"] for entry in outputs]}
        return self.production.mutate("carousel_import_finished", body, {"import": record, "outputs": outputs, "expected_inputs": fingerprint(expected),
            "expected_requirements": fingerprint(item.get("requirements"))})

    def save_carousel_requirements(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "制作要求参数无效。")
        return self.production.mutate("carousel_requirements", {**body, "carousel_id": identifier})

    def select_carousel_output(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "选版参数无效。")
        return self.production.mutate("carousel_select_output", {**body, "carousel_id": identifier})
