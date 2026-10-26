"""Group plan and group-level acceptance of a photo carousel.

The plan (route, 1–3 selling-point tags, each slide's stage and shot, music direction) is checked instantly from the
record. The AI group review reads every slide's current image in order through the read-only review channel and
judges the group as a whole. Both only advise: confirmation, delivery and publishing stay where they are.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import threading
from uuid import uuid4

from carousel_plan import (GROUP_REVIEW_SCHEMA, ROUTES, SHOTS, STAGES, empty_plan, group_review_prompt, group_signature,
                           normalize_group_review, slide_image, structure_checks)
from codex_bridge import BridgeCancelled, BridgeError
from direction_store import now
from elevenlabs_client import VoiceError
from production_store import carousel_output_current
from carousel_lineage import lineage_context, slide_lineage
from task_activity import activity_label, record_activity, safe_error
from workbench_carousel_images import IMAGE_TIMEOUT, image_hash


class CarouselGroupMixin:
    """Needs the carousel image mixin (tasks, reviewer, image files) and self.production from the workbench."""

    def save_carousel_plan(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "整组方案格式无效。")
        return self.production.mutate("carousel_plan", {**body, "carousel_id": identifier})

    def _base_hash(self, identifier):
        def digest(slide):
            try:
                return image_hash(self._base_path(identifier, slide))
            except VoiceError:
                return ""
        return digest

    def _group_images(self, item):
        return [slide_image(item, slide, carousel_output_current, self._base_hash(item["id"])) for slide in item["slides"]]

    def carousel_group_live(self, item):
        """Attach the instant checks and whether the latest group review still matches the group."""
        item.setdefault("plan", empty_plan())
        for slide in item["slides"]:
            slide.setdefault("stage", "")
            slide.setdefault("shot", "")
        images = self._group_images(item)
        signature = group_signature(item, images)
        with self.lock:
            live = {key: copy.deepcopy({name: value for name, value in task.items() if name != "handle"})
                    for key, task in self._image_tasks().items()
                    if task.get("kind") == "group_review" and task.get("handle") and task["handle"].is_alive()}
        stored = None
        for run in item.get("group_reviews", []):
            if run.get("status") == "running":
                if run["id"] in live:
                    run["live"] = live[run["id"]]
                    continue
                # The item may predate a review that finished a moment ago; its saved record wins over "interrupted".
                if stored is None:
                    try:
                        stored = {entry["id"]: entry for entry in self.production.carousel(item["id"]).get("group_reviews", [])}
                    except VoiceError:
                        stored = {}
                saved = stored.get(run["id"])
                if saved and saved.get("status") != "running":
                    run.update(copy.deepcopy(saved))
                else:
                    run.update(status="interrupted", error="这次整组验收的进程已不在运行，可重新验收。")
            run["current"] = run.get("signature") == signature
        item["group_check"] = {"structure": structure_checks(item), "signature": signature,
                               "missing": [slide["number"] for slide, image in zip(item["slides"], images) if not image],
                               "base_only": [slide["number"] for slide, image in zip(item["slides"], images)
                                             if image and image["kind"] == "base"]}
        return item

    def start_carousel_group_review(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "整组验收参数格式无效。")
        body = {**body, "carousel_id": identifier}
        with self.lock:
            prior = self.production.replay("carousel_group_review_start", body)
            if prior:
                return prior
        reviewer = self._image_reviewer()
        if bool(getattr(reviewer, "direct_image_edit", False)):
            raise VoiceError(409, "当前图片通道不提供 AI 核对，请连接独立核对通道。")
        if not self.image_status().get("review_available"):
            raise VoiceError(503, "本机 Codex 核对暂不可用，请检查登录后重试。")
        item = self.production.carousel(identifier)
        if item.get("archived"):
            raise VoiceError(409, "制作单已归档，请恢复后再验收。")
        self.carousel_images_live(item)
        images = self._group_images(item)
        missing = [str(slide["number"]) for slide, image in zip(item["slides"], images) if not image]
        if missing:
            raise VoiceError(409, f"第 {'、'.join(missing)} 张还没有底图或成图，选好后再做整组验收。")
        with self.lock:
            tasks = self._image_tasks()
            for task in tasks.values():
                if task.get("carousel_id") == identifier and task.get("handle") and task["handle"].is_alive():
                    if task.get("kind") == "group_review":
                        raise VoiceError(409, "这组图文正在整组验收，请等待本次结果。")
                    if task.get("kind") == "generation":
                        raise VoiceError(409, "这组图文正在出图，请等本次完成后再整组验收。")
            review_id = "GRV-" + uuid4().hex[:12]
            folder = self._carousel_dir(identifier) / "group-reviews" / review_id
            folder.mkdir(parents=True)
            paths = []
            for slide, image in zip(item["slides"], images):
                source = (self.carousel_output(identifier, image["id"], "full") if image["kind"] == "output"
                          else self._base_path(identifier, slide))
                target = folder / f"slide-{slide['number']:02d}{source.suffix.lower()}"
                shutil.copyfile(source, target)
                if image_hash(target) != image["sha256"]:
                    shutil.rmtree(folder, ignore_errors=True)
                    raise VoiceError(409, f"第 {slide['number']} 张的图片文件已变化，请刷新后重试。")
                paths.append(str(target))
            plan = item.get("plan") or empty_plan()
            snapshot = {"carousel": {key: item.get(key) for key in ("id", "product", "direction_id", "variant_label", "title", "script_id", "script_version")},
                        "script_context": lineage_context(item),
                        "plan": {"route": ROUTES.get(plan.get("route"), "未选"), "tags": plan.get("tags", []),
                                 "music": plan.get("music", "")},
                        "caption_en": item.get("caption_en", ""),
                        "slides": [{"number": slide["number"], "stage": STAGES.get(slide.get("stage"), "未标"),
                                    "shot": SHOTS.get(slide.get("shot"), "未标"), "role": slide.get("role", ""),
                                    "title_lines": slide.get("title_lines", []), "body": slide.get("body", ""),
                                    "source": slide.get("source", ""), "forbidden": slide.get("forbidden", ""),
                                    **slide_lineage(slide), "image": image["kind"]} for slide, image in zip(item["slides"], images)],
                        "images": images}
            prompt = group_review_prompt(snapshot)
            (folder / "input.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            (folder / "prompt.txt").write_text(prompt, encoding="utf-8")
            record = {"id": review_id, "status": "running", "started_at": now(), "finished_at": "", "error": "",
                      "signature": group_signature(item, images), "images": images, "review": None,
                      "source": body.get("source") if body.get("source") in {"workbench", "autopilot"} else "workbench",
                      "folder": str(folder.relative_to(self._carousel_dir(identifier)))}
            cancel = self._image_cancel[review_id] = threading.Event()
            tasks[review_id] = {"kind": "group_review", "carousel_id": identifier,
                                "progress": "正在按顺序查看整组图片。", "updated_at": now()}
            try:
                result = self.production.mutate("carousel_group_review_start", body, record)
                thread = threading.Thread(target=self._run_group_review, daemon=True, name=f"group-review-{review_id}",
                                          args=(identifier, record, snapshot, folder, paths, prompt, cancel))
                tasks[review_id]["handle"] = thread
                thread.start()
            except Exception:
                tasks.pop(review_id, None)
                self._image_cancel.pop(review_id, None)
                raise
        record_activity(self, "group_review:" + review_id, kind="group_review", owner_id=identifier, source_id=review_id,
                        product=item.get("product", ""), case_id=item.get("case_id"),
                        title="整组验收 · " + activity_label(item, identifier), step="images", status="running",
                        phase="整组验收", message=f"正在按顺序查看 {len(paths)} 张图片")
        return result

    def _run_group_review(self, identifier, record, snapshot, folder, paths, prompt, cancel):
        review_id, status, error, review = record["id"], "completed", "", None
        try:
            answer = self._image_reviewer().run(prompt, cwd=str(folder), images=paths, cancel_event=cancel,
                on_progress=lambda text: self._image_progress(review_id, f"整组验收：{text}"),
                timeout=IMAGE_TIMEOUT, schema=GROUP_REVIEW_SCHEMA, read_only=True)
            review = normalize_group_review(answer, [slide["number"] for slide in snapshot["slides"]], snapshot)
            for path, image in zip(paths, snapshot["images"]):
                if image_hash(Path(path)) != image["sha256"]:
                    raise BridgeError("验收期间图片发生变化，本次结果未采用。")
            if not review["completed"]:
                status, error = "failed", "整组验收没有返回完整结果（检查项或逐张缺项），可重新验收。"
        except BridgeCancelled:
            status, error = "cancelled", "整组验收已停止。"
        except Exception as exc:
            status, error = "failed", str(exc)[:2000] or "整组验收失败，可重新验收。"
        result = {"status": status, "finished_at": now(), "error": error, "review": review}
        try:
            (folder / "receipt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            self.production.mutate("carousel_group_review_finish", {"request_id": "group-review-finish-" + review_id,
                                   "carousel_id": identifier, "group_review_id": review_id}, result)
            if status == "completed":
                message = "整组验收全部通过，待人工确认发布" if review["status"] == "pass" else "整组验收已结束，有待处理的提示"
                record_activity(self, "group_review:" + review_id, status="needs_attention", phase="验收结果已保存",
                                message=message, result_count=1, event_key="saved:" + status)
            else:
                record_activity(self, "group_review:" + review_id, status=status, phase="验收已结束",
                                message=error if status == "cancelled" else "整组验收失败。" + safe_error(error))
        except Exception:
            record_activity(self, "group_review:" + review_id, status="needs_attention", phase="结果待恢复",
                            message="整组验收已结束，结果暂未保存", event_key="result_pending")
        finally:
            with self.lock:
                self._image_tasks().pop(review_id, None)
                self._image_cancel.pop(review_id, None)
