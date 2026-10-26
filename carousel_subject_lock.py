"""Deterministic photographed-subject protection around an AI image edit.

Node/Sharp performs every raster edit. Python validates bindings and persists receipts.
Pixel equality covers the supplied mask; semantic coverage is a separate vision result.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess

from codex_bridge import BridgeError

SIZES = {"3:4": (1536, 2048), "9:16": (1152, 2048)}
MAX_BYTES = 64 * 1024 * 1024
SCRIPT = Path(__file__).resolve().parent / "scripts" / "carousel_subject_lock.cjs"
# Bumped whenever the canvas layout changes: results made on an older layout are never valid for the new one.
RENDER_CONTRACT = "segmented_background_v4"


def _runtime():
    root = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node"
    executable = root / "bin" / ("node.exe" if os.name == "nt" else "node")
    sharp = root / "node_modules" / "sharp"
    if not executable.is_file() or not (sharp / "package.json").is_file() or not SCRIPT.is_file():
        raise BridgeError("主体保护所需的本机图像组件不可用，本次未替换成图。")
    return executable, sharp


def _run(operation, **values):
    executable, sharp = _runtime()
    args = {"operation": operation, **{key: str(value) if isinstance(value, Path) else value for key, value in values.items()}}
    options = {"input": json.dumps(args, ensure_ascii=False).encode("utf-8"), "stdout": subprocess.PIPE,
               "stderr": subprocess.PIPE, "timeout": 90, "shell": False}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        completed = subprocess.run([str(executable), str(SCRIPT), str(sharp)], **options)
        result = json.loads(completed.stdout)
        if not isinstance(result, dict) or completed.returncode or result.get("error"):
            code = result.get("error") if isinstance(result, dict) else None
            if code == "generated_aspect_mismatch":
                raise BridgeError("AI 图片比例偏差过大，不能安全恢复原主体；本次成图未采用。")
            if code in {"invalid_regions", "invalid_mask", "invalid_mask_coverage"}:
                raise BridgeError("主体保护蒙版为空、范围无效或覆盖超过 95%，请重新检查完整主体范围。")
            raise BridgeError("主体保护的图片、来源或像素核对未通过，本次未替换成图。")
        return result
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        raise BridgeError("主体保护图像处理未完成，请检查本机图像组件后重试。") from exc


def _digest(path):
    try:
        path = Path(path)
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValueError()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except (OSError, ValueError, TypeError) as exc:
        raise BridgeError("主体保护所绑定的图片不存在、过大或无法读取。") from exc


def _inside(root, value):
    try:
        if not isinstance(value, (str, os.PathLike)) or not str(value):
            raise ValueError()
        root = Path(root).resolve()
        path = Path(value)
        path = (path if path.is_absolute() else root / path).resolve()
        if not path.is_relative_to(root):
            raise ValueError()
        return path
    except (OSError, ValueError, TypeError) as exc:
        raise BridgeError("主体保护文件不属于本次图片任务。") from exc


def prepare_subject_canvas(base: Path, folder: Path, aspect: str, crop: dict | None = None) -> dict:
    """Cover-fit the base below the title strip; crop = {"crop_left", "crop_top"} moves the visible window."""
    if not isinstance(aspect, str) or aspect not in SIZES:
        raise BridgeError("主体保护画幅仅支持 3:4 或 9:16。")
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    base = _inside(folder, base)
    before = _digest(base)
    target = folder / "subject-source.png"
    hint = {key: int(crop[key]) for key in ("crop_left", "crop_top") if isinstance(crop, dict) and type(crop.get(key)) is int}
    result = _run("prepare", base=base, aspect=aspect, target=target, **hint)
    if _digest(base) != before:
        raise BridgeError("准备主体保护画布时原图发生变化，请重新出图。")
    return {"file": target.name, **result, "base_sha256": before, "sha256": _digest(target)}


def build_subject_mask(folder: Path, regions: list) -> dict:
    folder = Path(folder).resolve()
    source = folder / "subject-source.png"
    before = _digest(source)
    mask, preview = folder / "subject-mask.png", folder / "mask-preview.png"
    extra = {}
    snapshot_file = folder / "input.json"
    if snapshot_file.is_file():
        snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
        base_hash = snapshot.get("base_sha256", "")
        if isinstance(base_hash, str) and len(base_hash) == 64 and all(c in "0123456789abcdef" for c in base_hash):
            cache = folder.parents[2] / "subject-masks" / base_hash
            metadata = cache / "metadata.json"
            if metadata.is_file():
                record = json.loads(metadata.read_text(encoding="utf-8"))
                semantic = _inside(cache, record.get("mask_file"))
                if record.get("base_sha256") != base_hash or _digest(semantic) != record.get("mask_sha256"):
                    raise BridgeError("精细主体蒙版的原图或文件绑定已变化。")
                extra = {"semantic_path": semantic, "transform": snapshot["subject_lock"]["transform"],
                         "photo_rectangles": record["photo_rectangles"], "photo_frames": record.get("photo_frames", [])}
                # Keep this task's refinement evidence beside its immutable inputs.
                import shutil
                shutil.copyfile(semantic, folder / "refined-subject-mask.png")
                extra["semantic_path"] = folder / "refined-subject-mask.png"
                (folder / "refinement.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    result = _run("mask", source=source, regions=regions, target=mask, preview=preview, **extra)
    if _digest(source) != before:
        raise BridgeError("制作保护蒙版时原始画布发生变化，请重新策划。")
    return {"file": mask.name, "preview_file": preview.name, **result, "sha256": _digest(mask),
            "source_sha256": before, "regions": copy.deepcopy(regions)}


def create_subject_review_preview(source: Path, target: Path) -> dict:
    """A full-resolution JPEG for read-only vision; original PNGs stay untouched."""
    try:
        source, target = Path(source), Path(target)
        if (not source.is_absolute() or not target.is_absolute() or target.suffix.lower() != ".jpg" or
                source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"} or
                target.is_symlink() or not target.parent.is_dir()):
            raise ValueError()
        source, target = source.resolve(), target.resolve()
        if source == target or (target.exists() and os.path.samefile(source, target)):
            raise ValueError()
    except (OSError, TypeError, ValueError) as exc:
        raise BridgeError("核对预览须使用本机图片和独立的 .jpg 输出路径，不能覆盖原图。") from exc
    before = _digest(source)
    result = _run("preview", source=source, target=target)
    if _digest(source) != before:
        raise BridgeError("制作核对预览时原图发生变化，请重新准备核对。")
    return {"file": target.name, **result, "source_sha256": before, "sha256": _digest(target)}


def compose_subject_locked(base: Path, folder: Path, generated: Path, snapshot: dict) -> tuple[Path, dict]:
    folder = Path(folder).resolve()
    root = folder.parents[2]
    lock = snapshot.get("subject_lock") or {}
    if not isinstance(lock, dict) or not isinstance(lock.get("transform"), dict):
        raise BridgeError("本张缺少可验证的主体保护画布记录。")
    source = _inside(folder, lock.get("file"))
    mask = _inside(folder, lock.get("mask_file"))
    base, generated = _inside(folder, base), _inside(folder, generated)
    before = {"base": _digest(base), "source": _digest(source), "mask": _digest(mask), "generated": _digest(generated)}
    layout = Path(str(mask) + ".layout.json")
    layout_hash = _digest(layout)
    if before["base"] != snapshot.get("base_sha256") or not snapshot.get("input_signature"):
        raise BridgeError("主体保护原图或输入绑定已变化，本次成图未采用。")
    target = folder / "render-locked.png"
    slide = snapshot.get("slide") or {}
    caption = {"title_lines": slide.get("title_lines", []), "body": "" if snapshot.get("requirements", {}).get("overlay_only") else slide.get("body", ""),
               "caption_style": snapshot.get("requirements", {}).get("caption_style", "rugged")}
    details = _run("compose", source=source, mask=mask, generated=generated, target=target,
                   **({"caption": caption} if caption["title_lines"] else {}))
    receipt = {"method": "masked_source_pixels", "render_contract": RENDER_CONTRACT, "status": "passed", "base_sha256": before["base"],
               "input_signature": snapshot["input_signature"], "aspect": snapshot["aspect"],
               "number": (snapshot.get("slide") or {}).get("number", (lock.get("coverage") or {}).get("number")),
               "transform": copy.deepcopy(lock["transform"]), **details}
    for role, path in (("base", base), ("source", source), ("mask", mask), ("generated", generated)):
        if _digest(path) != before[role]:
            raise BridgeError("主体保护处理期间输入图片发生变化，本次成图未采用。")
        receipt[role + "_file"] = path.relative_to(root).as_posix()
        receipt[role + "_sha256"] = before[role]
    receipt.update(final_file=target.relative_to(root).as_posix(), final_sha256=_digest(target))
    if _digest(layout) != layout_hash:
        raise BridgeError("主体照片窗口在合成过程中发生变化，请重新出图。")
    receipt.update(layout_file=layout.relative_to(root).as_posix(), layout_sha256=layout_hash)
    if "coverage" in lock:
        receipt["mask_coverage"] = copy.deepcopy(lock["coverage"])
    output = {"file": receipt["final_file"], "sha256": receipt["final_sha256"], "base_sha256": before["base"],
              "input_signature": snapshot["input_signature"], "subject_preservation": receipt}
    verify_subject_lock(root, output)
    (folder / "subject-lock-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return target, receipt


def verify_subject_lock(root: Path, output: dict) -> dict:
    """Raise on any failed binding/pixel check; does not invent semantic mask coverage."""
    try:
        receipt = output.get("subject_preservation")
        if not isinstance(receipt, dict) or receipt.get("method") != "masked_source_pixels" or receipt.get("status") != "passed":
            raise ValueError()
        if receipt.get("render_contract") != RENDER_CONTRACT:
            raise ValueError()
        if (not receipt.get("input_signature") or receipt["input_signature"] != output.get("input_signature") or
                receipt.get("base_sha256") != output.get("base_sha256") or
                receipt.get("final_sha256") != output.get("sha256") or receipt.get("mismatch_pixels") != 0):
            raise ValueError()
        coverage = receipt.get("mask_coverage")
        if (not isinstance(coverage, dict) or coverage.get("status") != "passed" or
                type(coverage.get("number")) is not int or coverage["number"] < 1 or
                receipt.get("number") != coverage["number"] or
                coverage.get("source_sha256") != receipt.get("source_sha256") or
                coverage.get("mask_sha256") != receipt.get("mask_sha256")):
            raise ValueError()
        report_path = _inside(root, coverage.get("file"))
        if _digest(report_path) != coverage.get("sha256") or report_path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError()
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict) or not isinstance(report.get("slides"), list):
            raise ValueError()
        entries = [entry for entry in report["slides"] if isinstance(entry, dict) and
                   type(entry.get("number")) is int and entry["number"] == coverage["number"]]
        if len(entries) != 1 or entries[0].get("covered") is not True or entries[0].get("issues") != []:
            raise ValueError()
        paths = {}
        for role in ("base", "source", "mask", "generated", "final"):
            paths[role] = _inside(root, receipt.get(role + "_file"))
            if _digest(paths[role]) != receipt.get(role + "_sha256"):
                raise ValueError()
        layout_path = _inside(root, receipt.get("layout_file"))
        if _digest(layout_path) != receipt.get("layout_sha256") or layout_path.stat().st_size > 1024 * 1024:
            raise ValueError()
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        if (layout.get("source_sha256") != receipt["source_sha256"] or
                layout.get("mask_sha256") != receipt["mask_sha256"] or
                not isinstance(layout.get("rectangles"), list) or not layout["rectangles"]):
            raise ValueError()
        for rect in layout["rectangles"]:
            if (not isinstance(rect, dict) or any(type(rect.get(k)) is not int for k in ("left", "top", "width", "height")) or
                    rect["left"] < 0 or rect["top"] < 0 or rect["width"] < 1 or rect["height"] < 1 or
                    rect["left"] + rect["width"] > receipt["width"] or rect["top"] + rect["height"] > receipt["height"]):
                raise ValueError()
        final = _inside(root, output.get("file"))
        if _digest(final) != receipt["final_sha256"]:
            raise ValueError()
        details = _run("verify", base=paths["base"], source=paths["source"], mask=paths["mask"], final=final,
                       aspect=receipt["aspect"], transform=receipt["transform"])
        if any(details[key] != receipt.get(key) for key in ("width", "height", "protected_pixel_count", "mismatch_pixels")):
            raise ValueError()
        # Recheck after the raster read so changing files cannot validate an old receipt.
        if (any(_digest(path) != receipt[role + "_sha256"] for role, path in paths.items()) or
                _digest(final) != output["sha256"] or _digest(report_path) != coverage["sha256"] or
                _digest(layout_path) != receipt["layout_sha256"]):
            raise ValueError()
        return copy.deepcopy(receipt)
    except (KeyError, ValueError, TypeError, IndexError, OSError, UnicodeError) as exc:
        raise BridgeError("主体保护的文件、输入绑定或像素核对未通过，请重新生成。") from exc
