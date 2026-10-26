"""Durable, cancellable carousel production under the user's AI completion authorization."""
from __future__ import annotations

import copy
import json
import shutil
import threading
from uuid import uuid4

from codex_bridge import BridgeCancelled, BridgeError
from direction_store import now
from elevenlabs_client import VoiceError
from production_store import (carousel_confirmation_current, carousel_output_current,
                              carousel_review_current, carousel_subject_preservation_current, SUBJECT_RENDER_CONTRACT,
                              carousel_requirements, carousel_requires_layered_layout)
from task_activity import activity_label, record_activity


def review_allows_autopilot(item, slide, output):
    """Only complete, byte-bound vision findings can authorize a current output."""
    if not carousel_output_current(item, slide, output) or not carousel_review_current(item, slide, output):
        return False
    review = output.get("review") or {}
    if not review.get("completed") or review.get("issues"):
        return False
    checks = review.get("checks") or []
    if len(checks) != 4 or {entry.get("id") for entry in checks} != {"visual", "text", "product", "evidence"}:
        return False
    for entry in checks:
        if entry.get("status") == "pass":
            continue
        if entry.get("id") != "evidence" or entry.get("status") != "uncertain":
            return False
        if (entry.get("limitation_code") == "generated_visual_fidelity" and
                (output.get("requirements_snapshot") or {}).get("mode") == "generated_visual"):
            continue
        # A bounded static-image limitation is distinct from an unsupported claim
        # or uncertain comparison. Old free-text reviews must be reviewed again.
        assessment = review.get("evidence_assessment") or {}
        if (entry.get("limitation_code") == "static_image_limitations" and
                assessment == {"claim_type": "appearance_or_instruction", "claim_support": "supported",
                               "reference_match": "consistent", "limitation": "static_image_limitations"} and
                any(binding.get("role") == "product_reference" and binding.get("sha256")
                    for binding in review.get("reference_bindings", []))):
            continue
        return False
    return True


def evidence_only_fix(output):
    """A checked layout whose only failure is the copy's evidence: laying it out again cannot change that."""
    review = (output or {}).get("review") or {}
    checks = {entry.get("id"): entry.get("status") for entry in review.get("checks") or []}
    return (review.get("status") == "needs_fix" and checks.get("evidence") == "fail" and
            all(status == "pass" for key, status in checks.items() if key != "evidence"))


class EvidenceHeld(VoiceError):
    """One slide waits for a person; the run goes on with the others and packages nothing."""


class CarouselAutopilotMixin:
    def _autopilot_tasks(self):
        if not hasattr(self, "_autopilot_live"):
            self._autopilot_live = {}
        return self._autopilot_live

    def autopilot_running(self, identifier=None):
        with self.lock:
            return any(identifier is None or task["carousel_id"] == identifier
                       for task in self._autopilot_tasks().values())

    def _autopilot_body(self, **fields):
        return {"request_id": uuid4().hex, "revision": self.production.current_revision(), **fields}

    def start_carousel_autopilot(self, identifier, body, *, cancel_event=None, on_progress=None):
        if not isinstance(body, dict):
            raise VoiceError(400, "全程制作参数无效。")
        item = self.production.carousel(identifier)
        requirements = carousel_requirements(item)
        workflow = body.get("workflow", requirements.get("workflow", "review_first"))
        if not isinstance(workflow, str) or workflow not in {"subject_locked", "generation_first", "review_first"}:
            raise VoiceError(400, "图文制作流程无效。")
        body = {**body, "carousel_id": identifier, "workflow": workflow}
        batch_workflow = workflow in {"subject_locked", "generation_first"}
        subject_locked = workflow == "subject_locked"
        with self.lock:
            prior = self.production.replay("carousel_autopilot_start", body)
            if prior:
                return prior
            item = self.production.carousel(identifier)
            if self.autopilot_running(identifier) or self._carousel_image_running(identifier):
                raise VoiceError(409, "这张图文正在制作，请等待完成或先停止。")
            if any(search.get("status") == "running" and self.edit_live(search.get("id"))
                   for search in item.get("searches", [])):
                raise VoiceError(409, "这张图文正在排队或寻找底图，请等待完成或先停止。")
            ready = all(self._autopilot_generated_output(item, slide, workflow) if batch_workflow else
                        carousel_confirmation_current(item, slide, self._autopilot_output(item, slide))
                        for slide in item["slides"])
            if not ready:
                status = self.image_status()
                if (requirements["mode"] != "layout_only" and not status.get("available")) or not status.get("review_available"):
                    raise VoiceError(503, ("AI 出图需要可用的图片渠道和 AI 提示词通道，请检查图片渠道及本机 Codex。"
                        if batch_workflow else "全程制作需要可用的出图和 AI 核对通道，请检查图片渠道及本机 Codex。"))
                missing = [str(s["number"]) for s in item["slides"] if not s.get("base") and
                           not s.get("candidates") and not (self._autopilot_generated_output(item, s, workflow)
                               if batch_workflow else self._autopilot_output(item, s))]
                if missing:
                    raise VoiceError(409, "第 " + "、".join(missing) + " 张还没有可用画面，请先提供素材或从视频转入。")
            run_id = "AUT-" + uuid4().hex[:12]
            stamp = now()
            layered = not batch_workflow and carousel_requires_layered_layout(requirements)
            record = {"id": run_id, "status": "running", "phase": "准备制作", "message": "AI 正在接手当前图文。",
                      "error": "", "started_at": stamp, "updated_at": stamp, "finished_at": "", "delivery_id": "",
                      "workflow": workflow, "review_deferred": batch_workflow,
                      "actor": "assistant_on_user_authorization", "authorization": (
                          "用户授权 AI 编写提示词、并行图生图；商品主体必须使用原图像素并通过保护验证，其他区域可以修改，验证后选用与打包。"
                          if subject_locked else
                          "用户启动 AI 全程制作，授权 AI 编写提示词、生成整组图片、选用与打包；产品一致性核对暂缓。"
                          if batch_workflow else
                          "用户启动 AI 全程制作，授权选图、在原图上把英文花字排在商品和手后层、核对、确认与打包；不改背景、手和商品。"
                          if layered else "用户启动 AI 全程制作，授权选图、出图、核对、确认与打包。"),
                      "completed_slides": [], "source": body.get("source", "workbench")}
            result = self.production.mutate("carousel_autopilot_start", body, record)
            cancel = cancel_event if cancel_event is not None else threading.Event()
            task = {"carousel_id": identifier, "cancel": cancel, "child_id": None, "callback": on_progress}
            self._autopilot_tasks()[run_id] = task
            record_activity(self, "autopilot:" + run_id, kind="images", owner_id=identifier, source_id=run_id,
                            product=item.get("product", ""), title=("图文分层排版 · " if layered else "AI 全程图文 · ") + activity_label(item, identifier),
                            step="images", status="running", phase=record["phase"], message=record["message"])
            thread = threading.Thread(target=self._run_carousel_autopilot, args=(identifier, run_id, cancel),
                                      name="carousel-autopilot-" + run_id, daemon=True)
            task["handle"] = thread
            try:
                thread.start()
            except Exception:
                self._autopilot_tasks().pop(run_id, None)
                self._autopilot_update(identifier, run_id, status="failed", phase="启动失败", finished_at=now(),
                                       error="全程制作未能启动，请重试。", message="全程制作未能启动，请重试。")
                raise
            return result

    @staticmethod
    def _autopilot_output(item, slide):
        choices = sorted(slide.get("outputs", []), key=lambda o: o.get("id") == slide.get("output_id"))
        return next((output for output in reversed(choices) if carousel_output_current(item, slide, output)), None)

    def _autopilot_generated_output(self, item, slide, workflow="generation_first"):
        """Only the requested workflow and valid protection receipts can be reused.

        The newest valid image wins: a redo must not be shadowed by an older picked version, because the
        group's final visual check has to look at what was just made. Approval then moves the pick to it."""
        for output in reversed(slide.get("outputs", [])):
            if (output.get("workflow") != workflow or output.get("ai_generated") is not True or
                    not carousel_output_current(item, slide, output)):
                continue
            if workflow == "subject_locked":
                if not carousel_subject_preservation_current(output):
                    continue
                from carousel_subject_lock import verify_subject_lock
                try:
                    verify_subject_lock(self._carousel_dir(item["id"]), output)
                except (BridgeError, VoiceError, OSError, ValueError):
                    continue
            return output
        return None

    def _autopilot_update(self, identifier, run_id, **fields):
        with self.lock:
            result = self.production.mutate("carousel_autopilot_update",
                {"request_id": uuid4().hex, "carousel_id": identifier, "run_id": run_id}, {"updated_at": now(), **fields})
            task = self._autopilot_tasks().get(run_id)
        if fields.get("message"):
            record_activity(self, "autopilot:" + run_id, status=fields.get("status", "running"),
                            phase=fields.get("phase", "制作中"), message=fields["message"],
                            result_count=len(fields.get("completed_slides", [])))
            if task and task.get("callback"):
                task["callback"](fields["message"], fields.get("phase"))
        return result["run"]

    @staticmethod
    def _autopilot_check_cancel(cancel):
        if cancel.is_set():
            raise BridgeCancelled("已停止全程制作，已完成的图片保留。")

    def _autopilot_wait_child(self, run_id, child_id, cancel):
        """Do not finish cancellation while an owned image/review worker can still write."""
        with self.lock:
            self._autopilot_tasks()[run_id]["child_id"] = child_id
        while True:
            with self.lock:
                task = self._image_tasks().get(child_id)
                if not task:
                    break
                event = self._image_cancel.get(child_id)
                handle = task.get("handle")
                if cancel.is_set() and event:
                    event.set()
            if handle:
                handle.join(.15)
            else:
                threading.Event().wait(.15)
        with self.lock:
            if run_id in self._autopilot_tasks():
                self._autopilot_tasks()[run_id]["child_id"] = None
        self._autopilot_check_cancel(cancel)

    def _autopilot_choose(self, identifier, run_id, number, cancel, *, generation_first=False, workflow="generation_first"):
        item = self.production.carousel(identifier)
        slide = next(s for s in item["slides"] if s["number"] == number)
        if slide.get("base") or (self._autopilot_generated_output(item, slide, workflow)
                                 if generation_first else self._autopilot_output(item, slide)):
            return
        candidates = slide.get("candidates", [])
        # The video planner has already visually selected and ordered these frames.
        candidate = next((c for c in reversed(candidates) if c.get("origin") == "composite" and c.get("video_job_id")), None)
        if candidate is None and candidates and all(c.get("origin") == "video_frame" for c in candidates):
            candidate = candidates[0]
        if candidate is None and candidates and generation_first:
            # The batch prompt planner examines the selected references together.
            # Do not add a separate serial vision-review call for every reference.
            candidate = candidates[0]
        if candidate is None:
            folder = self._carousel_dir(identifier) / "autopilots" / run_id / f"pick-{number}"
            folder.mkdir(parents=True, exist_ok=True)
            choices = []
            for index, candidate_source in enumerate(candidates[:20], 1):
                source = self.carousel_image(identifier, candidate_source["id"], "full")
                snapshot_file = folder / f"candidate-{index:02d}{source.suffix.lower()}"
                shutil.copyfile(source, snapshot_file)
                choices.append({"id": candidate_source["id"], "reason": candidate_source.get("reason", ""),
                                "file": str(snapshot_file.resolve())})
            snapshot = {"slide": {k: slide.get(k) for k in ("number", "title_lines", "body", "role", "forbidden")}, "candidates": choices}
            (folder / "input.json").write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            schema = {"type": "object", "properties": {"candidate_id": {"type": "string"}, "reason": {"type": "string"}},
                      "required": ["candidate_id", "reason"], "additionalProperties": False}
            answer = self._image_reviewer().run("逐张实际查看候选图，选择最能支撑本张文案、产品与证据清楚的一张。"
                "只返回 candidate_id 和 reason；不合适则 candidate_id 留空，不编造画面。资料：\n" + json.dumps(snapshot, ensure_ascii=False),
                cwd=folder, images=[c["file"] for c in choices], cancel_event=cancel, on_progress=lambda _: None,
                schema=schema, read_only=True)
            (folder / "answer.json").write_text(str(answer), encoding="utf-8")
            try:
                pick = json.loads(answer)["candidate_id"]
            except (ValueError, KeyError, TypeError):
                pick = None
            candidate = next((c for c in candidates if c["id"] == pick), None)
        if candidate is None:
            raise VoiceError(422, f"第 {number} 张没有能支撑文案的画面，AI 已保留现有素材。")
        self._autopilot_check_cancel(cancel)
        with self.lock:
            # Keep every candidate for inspection and future recovery.
            self.production.mutate("carousel_base", self._autopilot_body(carousel_id=identifier, slide=number,
                                    candidate=candidate["id"], preserve_candidates=True))

    def _autopilot_preserve(self, identifier, number):
        """Mark one slide for real-photo preservation. Only that slide's contract changes, so slides already confirmed stay valid."""
        with self.lock:
            item = self.production.carousel(identifier)
            requirements = copy.deepcopy(item.get("requirements") or {})
            if carousel_requirements(item)["mode"] == "layout_only":
                return
            if number in (requirements.get("preserve_slides") or []):
                return
            requirements["preserve_slides"] = sorted({*requirements.get("preserve_slides", []), number})
            self.production.mutate("carousel_requirements", self._autopilot_body(carousel_id=identifier, requirements=requirements))

    def _autopilot_slide(self, identifier, run_id, number, cancel, state=None):
        """First attempt uses the group's normal channel. Once a whole-image redraw fails the check, this and every later slide keep
        the real photo pixels (generated background plus exact lettering) instead of retrying the same redraw."""
        state = state if state is not None else {}
        correction = ""
        for attempt in range(2):
            self._autopilot_check_cancel(cancel)
            item = self.production.carousel(identifier)
            slide = next(s for s in item["slides"] if s["number"] == number)
            output = self._autopilot_output(item, slide)
            # A recovered fallback must not change the contract of a finished slide.
            if output and carousel_confirmation_current(item, slide, output):
                self._check_output_files(identifier, item, number, output["id"])
                return
            layout_only = carousel_requirements(item)["mode"] == "layout_only"
            if layout_only and output and evidence_only_fix(output):
                evidence = next(entry for entry in output["review"]["checks"] if entry["id"] == "evidence")
                raise EvidenceHeld(422, evidence.get("note", "")[:300])
            review = (output or {}).get("review") or {}
            failed_redraw = bool(output and review.get("status") == "needs_fix" and (
                output.get("direct_image_edit") or
                (output.get("requirements_snapshot") or {}).get("mode") == "generated_visual"))
            if failed_redraw and not correction:
                correction = "请修正上一版核对发现的问题，保持锁定文案和产品结构：" + json.dumps(review, ensure_ascii=False)[:1600]
            if attempt or state.get("preserve") or failed_redraw:
                if layout_only:
                    raise VoiceError(422, f"第 {number} 张原图排版核对仍有提示，已保留成图和核对结果，请查看后决定；不会改背景、手或商品。")
                self._autopilot_preserve(identifier, number)
                state["preserve"] = True
                # Preservation changes this slide's input signature. Do not keep
                # reviewing or using an output from the old redraw contract.
                item = self.production.carousel(identifier)
                slide = next(s for s in item["slides"] if s["number"] == number)
                output = self._autopilot_output(item, slide)
            generated = False
            if not output or attempt or (output.get("review") or {}).get("status") == "needs_fix":
                self._autopilot_update(identifier, run_id, phase="AI 出图", slide=number, attempt=attempt + 1,
                                       message=f"AI 正在{'修正' if attempt else '制作'}第 {number} 张" +
                                               ("，改用保留实拍像素的方式。" if state.get("preserve") else "。"))
                with self.lock:
                    result = self.start_image_generation(identifier, self._autopilot_body(slides=[number],
                        aspect=item.get("image_aspect", "3:4"), redo=bool(output), instruction=correction))
                self._autopilot_wait_child(run_id, result["generation"]["id"], cancel)
                item = self.production.carousel(identifier)
                slide = next(s for s in item["slides"] if s["number"] == number)
                output = next((o for o in reversed(slide.get("outputs", [])) if o.get("generation_id") == result["generation"]["id"]), None)
                if output is None:
                    generation = next(g for g in item["generations"] if g["id"] == result["generation"]["id"])
                    raise VoiceError(502, generation.get("error") or f"第 {number} 张未能完成出图，已完成图片保留。")
                generated = True
            reviewed = 1 if generated and (output.get("review") or {}).get("execution_status") in {"completed", "failed"} else 0
            while not review_allows_autopilot(item, slide, output):
                self._autopilot_check_cancel(cancel)
                review = output.get("review") or {}
                if review.get("status") == "needs_fix":
                    break
                if reviewed >= 2:
                    raise VoiceError(422, f"第 {number} 张尚未取得可靠 AI 核对结果，现有成图已保留。" + str(review.get("summary", ""))[:300])
                self._autopilot_update(identifier, run_id, phase="AI 核对", slide=number,
                                       message=f"AI 正在逐项核对第 {number} 张的文字、产品和画面。")
                with self.lock:
                    result = self.start_carousel_review(identifier, self._autopilot_body(slide=number, output_id=output["id"]))
                review_id = result["review"]["id"]
                self._autopilot_wait_child(run_id, review_id, cancel)
                reviewed += 1
                item = self.production.carousel(identifier)
                slide = next(s for s in item["slides"] if s["number"] == number)
                output = next(o for o in slide["outputs"] if o["id"] == output["id"])
            if review_allows_autopilot(item, slide, output):
                self._autopilot_check_cancel(cancel)
                with self.lock:
                    fresh = self.production.carousel(identifier)
                    current_slide = next(s for s in fresh["slides"] if s["number"] == number)
                    current_output = next(o for o in current_slide["outputs"] if o["id"] == output["id"])
                    if not review_allows_autopilot(fresh, current_slide, current_output):
                        raise VoiceError(409, "制作资料已变化，AI 未确认过期成图。")
                    self.approve_carousel_output(identifier, self._autopilot_body(slide=number, output_id=output["id"], approved=True,
                        actor="assistant_on_user_authorization", note="用户授权 AI 全程制作；独立核对确认文案、画面和商品对照符合要求。仅按文案实际声明核对，不额外证明实际使用效果或逐像素保真。"))
                return
            correction = "请修正上一版核对发现的问题，保持锁定文案和产品结构：" + json.dumps(output.get("review"), ensure_ascii=False)[:1600]
        raise VoiceError(422, f"第 {number} 张自动修正后仍未通过核对，已保留图片和核对结果。")

    def _autopilot_generation_result(self, identifier, workflow="generation_first"):
        """Count usable saved AI images even when a batch failed or was cancelled."""
        item = self.production.carousel(identifier)
        available, generated = [], []
        for slide in item["slides"]:
            if any(output.get("workflow") == workflow and output.get("ai_generated") is True and
                   carousel_output_current(item, slide, output) for output in slide.get("outputs", [])):
                generated.append(slide["number"])
            output = self._autopilot_generated_output(item, slide, workflow)
            if output:
                try:
                    self._check_output_files(identifier, item, slide["number"], output["id"])
                except Exception:
                    continue
                available.append(slide["number"])
        result = {"images_generated": len(generated) if workflow == "subject_locked" else len(available),
                  "total_slides": len(item["slides"]), "review_deferred": True, "product_consistency_checked": False}
        if workflow == "subject_locked":
            result.update(subject_preserved_slides=sorted(available), subject_preserved_count=len(available),
                          product_preservation_checked=len(available) == len(item["slides"]), final_visual_checked=False)
        return sorted(available), result

    def _autopilot_generate_group(self, identifier, run_id, cancel, completed, workflow="generation_first", state=None):
        """Plan once and generate in parallel, enforcing subject preservation when requested."""
        subject_locked = workflow == "subject_locked"
        state = state if state is not None else {}
        self._autopilot_check_cancel(cancel)
        with self.lock:
            item = self.production.carousel(identifier)
            requirements = copy.deepcopy(item.get("requirements") or {})
            requirements.update(workflow=workflow, mode="photo_preserving" if subject_locked else "generated_visual")
            if subject_locked:
                requirements.update(protected_product=True, protected_evidence=True,
                    editable_regions=["background", "text", "hands"], hands_authorized=True,
                    render_contract=SUBJECT_RENDER_CONTRACT)
            requirements.pop("preserve_slides", None)
            if requirements != item.get("requirements"):
                self.production.mutate("carousel_requirements", self._autopilot_body(
                    carousel_id=identifier, requirements=requirements))
        item = self.production.carousel(identifier)
        self._autopilot_update(identifier, run_id, phase="准备参考图",
            message=("正在准备原图，AI 将统一写提示词并并行图生图；商品主体锁定，其他区域可以修改。" if subject_locked else
                     "正在准备整组参考图，随后由 AI 编写提示词并并行生图；产品一致性核对暂缓。"))
        for slide in sorted(item["slides"], key=lambda s: s["number"]):
            self._autopilot_check_cancel(cancel)
            self._autopilot_choose(identifier, run_id, slide["number"], cancel, generation_first=True, workflow=workflow)
        item = self.production.carousel(identifier)
        needed = [slide["number"] for slide in item["slides"]
                  if not self._autopilot_generated_output(item, slide, workflow)]
        generation_id = None
        if needed:
            self._autopilot_check_cancel(cancel)
            self._autopilot_update(identifier, run_id, phase="AI 写提示词并出图",
                message=(f"AI 正在统一编写整组提示词，并行编辑 {len(needed)} 张图片；完成后逐张验证商品主体像素未变。"
                         if subject_locked else f"AI 正在统一编写整组提示词，并行生成 {len(needed)} 张图片；暂不等待逐张一致性核对。"))
            with self.lock:
                self._autopilot_check_cancel(cancel)
                result = self.start_image_generation(identifier, self._autopilot_body(
                    slides=needed, aspect=item.get("image_aspect", "3:4"), redo=True,
                    workflow=workflow, review_deferred=True))
            generation_id = result["generation"]["id"]
            self._autopilot_update(identifier, run_id, generation_id=generation_id)
            self._autopilot_wait_child(run_id, generation_id, cancel)

        # Subject-locked images must pass the complete group's visual check
        # before any new approval. Failed or partial groups retain their files.
        missing, chosen = [], []
        item = self.production.carousel(identifier)
        for slide in sorted(item["slides"], key=lambda s: s["number"]):
            self._autopilot_check_cancel(cancel)
            output = self._autopilot_generated_output(item, slide, workflow)
            if output is None:
                missing.append(slide["number"])
                continue
            with self.lock:
                self._autopilot_check_cancel(cancel)
                if subject_locked:
                    from carousel_subject_lock import verify_subject_lock
                    verify_subject_lock(self._carousel_dir(identifier), output)
                    chosen.append((slide["number"], output))
                    if output.get("approved"):
                        self.approve_carousel_output(identifier, self._autopilot_body(
                            slide=slide["number"], output_id=output["id"], approved=False,
                            actor="assistant_on_user_authorization", workflow=workflow,
                            note="整组重新进行最终视觉核对，通过后重新选用。"))
                else:
                    self.approve_carousel_output(identifier, self._autopilot_body(
                        slide=slide["number"], output_id=output["id"], approved=True,
                        actor="assistant_on_user_authorization", workflow=workflow, review_deferred=True,
                        note="用户授权先出图；AI 已选用本次生成图片用于打包，尚未进行产品一致性核对。"))
            completed.append(slide["number"])
        if missing:
            generation = next((g for g in item.get("generations", []) if g["id"] == generation_id), {})
            error = generation.get("error", "")
            raise VoiceError(502, (f"已完成商品主体保护验证 {len(completed)}/{len(item['slides'])} 张；第 " if subject_locked else
                f"已生成 {len(completed)}/{len(item['slides'])} 张；第 ") + "、".join(map(str, missing)) +
                (" 张尚未通过图生图和主体保护验证，已有图片已保留，未打包。" if subject_locked else
                 " 张尚未出图，已有图片已保留。产品一致性尚未核对。") +
                (" " + str(error)[:500] if error else ""))
        if subject_locked:
            self._autopilot_check_cancel(cancel)
            self._autopilot_update(identifier, run_id, phase="整组最终视觉核对", completed_slides=[],
                message="图片已生成，AI 正在核对整组最终画面，确认没有产品副本、贴片边缘或其他明显视觉问题后再选用打包。")
            self.check_subject_locked_final_visual(identifier, generation_id, cancel)
            state["final_visual_checked"] = True
            self._autopilot_check_cancel(cancel)
            for number, output in chosen:
                with self.lock:
                    self._autopilot_check_cancel(cancel)
                    from carousel_subject_lock import verify_subject_lock
                    verify_subject_lock(self._carousel_dir(identifier), output)
                    self.approve_carousel_output(identifier, self._autopilot_body(
                        slide=number, output_id=output["id"], approved=True,
                        actor="assistant_on_user_authorization", workflow=workflow, review_deferred=True,
                        note="用户授权图生图；商品主体原图像素保护和整组最终视觉核对均已通过，AI 已选用图片用于打包。"))
        self._autopilot_check_cancel(cancel)
        completed[:], result_summary = self._autopilot_generation_result(identifier, workflow)
        if subject_locked:
            result_summary["final_visual_checked"] = True
        self._autopilot_update(identifier, run_id, phase="整理成品", completed_slides=completed,
            result=result_summary, message=("整组图生图已完成，商品主体像素保护和最终视觉核对通过，正在打包。" if subject_locked else
                                           "整组 AI 图片已生成，正在打包；产品一致性尚未核对。"))
        with self.lock:
            self._autopilot_check_cancel(cancel)
            result = self.create_carousel_delivery(identifier, self._autopilot_body(
                workflow=workflow, review_deferred=True))
        self._autopilot_update(identifier, run_id, status="completed",
            phase="图片已生成 · 主体已锁定" if subject_locked else "图片已生成 · 待核对",
            message=(f"{len(completed)} 张图生图已完成并打包；商品主体像素保护和整组最终视觉核对均已通过。"
                     if subject_locked else f"{len(completed)} 张 AI 图片已生成并打包，可以下载；产品一致性尚未核对。"),
            completed_slides=completed, result=result_summary,
            delivery_id=result["delivery"]["id"], finished_at=now())

    def _run_carousel_autopilot(self, identifier, run_id, cancel):
        completed, state = [], {}
        batch_workflow = False
        workflow = "review_first"
        try:
            item = self.production.carousel(identifier)
            run = next((entry for entry in item.get("autopilot_runs", []) if entry["id"] == run_id), {})
            workflow = run.get("workflow", "review_first")
            batch_workflow = workflow in {"subject_locked", "generation_first"}
            if batch_workflow:
                self._autopilot_generate_group(identifier, run_id, cancel, completed, workflow, state)
                return
            if (item.get("requirements") or {}).get("workflow") in {"generation_first", "subject_locked"}:
                # A deferred selection is not a completed strict review. Changing
                # the contract makes the strict path check or remake those files.
                with self.lock:
                    requirements = copy.deepcopy(item["requirements"])
                    requirements["workflow"] = "review_first"
                    item = self.production.mutate("carousel_requirements", self._autopilot_body(
                        carousel_id=identifier, requirements=requirements))["carousel"]
            state["preserve"] = bool((item.get("requirements") or {}).get("preserve_slides"))
            held = []
            for slide in sorted(item["slides"], key=lambda s: s["number"]):
                number = slide["number"]
                self._autopilot_check_cancel(cancel)
                self._autopilot_update(identifier, run_id, phase="AI 选图", slide=number,
                                       message=f"AI 正在确定第 {number} 张的画面。", completed_slides=completed)
                self._autopilot_choose(identifier, run_id, number, cancel)
                try:
                    self._autopilot_slide(identifier, run_id, number, cancel, state)
                except EvidenceHeld as exc:
                    # The layout passed; only the copy lacks a picture to back it. Keep going and let a person decide.
                    held.append({"slide": number, "reason": str(exc)})
                    continue
                completed.append(number)
            self._autopilot_check_cancel(cancel)
            if held:
                numbers = "、".join(str(entry["slide"]) for entry in held)
                message = (f"已完成 {len(completed)} 张；第 {numbers} 张排版、文字和商品都通过，只有实拍证据核对未通过"
                           "（文案声称缺少画面支持），未自动选用，整组未打包。请查看这些张，改文案或人工确认后再打包。")
                self._autopilot_update(identifier, run_id, status="needs_attention", phase="部分张待人看", message=message,
                                       completed_slides=completed, result={"held_slides": held}, finished_at=now())
                return
            self._autopilot_update(identifier, run_id, phase="整理成品", message="整组已核对，正在整理成品包。", completed_slides=completed)
            with self.lock:
                self._autopilot_check_cancel(cancel)
                result = self.create_carousel_delivery(identifier, self._autopilot_body())
            self._autopilot_update(identifier, run_id, status="completed", phase="成品已就绪", message=f"{len(completed)} 张图文已完成，可以下载成品包。",
                                   completed_slides=completed, delivery_id=result["delivery"]["id"], finished_at=now())
        except BridgeCancelled:
            fields = {}
            if batch_workflow:
                completed, fields["result"] = self._autopilot_generation_result(identifier, workflow)
                if workflow == "subject_locked":
                    fields["result"]["final_visual_checked"] = bool(state.get("final_visual_checked"))
            self._autopilot_update(identifier, run_id, status="cancelled", phase="已停止",
                message=(f"已停止图生图，已生成 {fields['result']['images_generated']} 张并保留；{len(completed)} 张商品主体保护验证通过。"
                         if workflow == "subject_locked" else
                         f"已停止 AI 出图，已生成 {len(completed)} 张图片并保留；产品一致性尚未核对。"
                         if batch_workflow else "已停止全程制作，已完成的图片保留。"),
                completed_slides=completed, finished_at=now(), **fields)
        except Exception as exc:
            error = str(exc)[:1000] or type(exc).__name__
            fields = {}
            if batch_workflow:
                completed, fields["result"] = self._autopilot_generation_result(identifier, workflow)
                if workflow == "subject_locked":
                    fields["result"]["final_visual_checked"] = bool(state.get("final_visual_checked"))
                if "已生成" not in error:
                    error = (f"已生成 {fields['result']['images_generated']} 张并保留；{len(completed)} 张商品主体保护验证通过。"
                             if workflow == "subject_locked" else
                             f"已生成 {len(completed)} 张图片并保留；产品一致性尚未核对。") + error
            self._autopilot_update(identifier, run_id, status="failed", phase="制作未完成", message=error,
                                   error=error, completed_slides=completed, finished_at=now(), **fields)
        finally:
            with self.lock:
                self._autopilot_tasks().pop(run_id, None)
            self._autopilot_group_review(identifier, run_id)

    def _autopilot_group_review(self, identifier, run_id):
        """A finished run is read once as a whole group; the review only advises and never changes the run."""
        if not hasattr(self, "start_carousel_group_review"):
            return
        try:
            item = self.production.carousel(identifier)
            run = next((entry for entry in item.get("autopilot_runs", []) if entry["id"] == run_id), {})
            if run.get("status") == "completed":
                self.start_carousel_group_review(identifier, {"request_id": "autopilot-group-" + run_id, "source": "autopilot"})
        except Exception:
            pass

    def cancel_carousel_autopilot(self, identifier, run_id, body=None):
        with self.lock:
            item = self.production.carousel(identifier)
            run = next((r for r in item.get("autopilot_runs", []) if r["id"] == run_id), None)
            if run is None:
                raise VoiceError(404, "这次全程制作不存在。")
            task = self._autopilot_tasks().get(run_id)
            if task:
                task["cancel"].set()
                child = self._image_cancel.get(task.get("child_id")) if hasattr(self, "_image_cancel") else None
                if child:
                    child.set()
            return {"carousel": item, "run": run}

    def stop_carousel_autopilots(self):
        with self.lock:
            tasks = list(self._autopilot_tasks().values())
            for task in tasks:
                task["cancel"].set()
                child = getattr(self, "_image_cancel", {}).get(task.get("child_id"))
                if child:
                    child.set()
        for task in tasks:
            handle = task.get("handle")
            if handle and handle is not threading.current_thread():
                handle.join(timeout=5)
