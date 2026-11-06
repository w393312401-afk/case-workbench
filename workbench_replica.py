"""The 照原片做视频 entry of a case: its transcript as an adopted script and a production job carrying the original's shots."""
from __future__ import annotations

from pathlib import Path
import shutil
from uuid import uuid4

from case_replica import case_source, line_records, snapshot, write_frames, detect_shots
from case_store import StoreError
from creation_store import CreationError
from direction_store import product_key
from elevenlabs_client import VoiceError
from retro_draft import PrepareError
from variant_parse import spoken_lines

SOURCE_KIND = "case_replica"
MAX_OPTIMIZED = 5


class ReplicaWorkbenchMixin:
    """Needs self.store, self.creation, self.production and self.lock from the workbench."""

    def _replica_case(self, case_id):
        try:
            case = self.store.read_case(case_id)
        except Exception as exc:
            raise VoiceError(404, "这条案例不存在或已删除。") from exc
        product = product_key(case.get("product"))
        if not product or product == "all":
            raise VoiceError(409, "这条案例还没有绑定产品，请先在案例里选择产品。")
        try:
            return case, product, case_source(case)
        except PrepareError as exc:
            raise VoiceError(409, str(exc)) from exc

    def _replica_jobs(self, case_id):
        jobs = self.production.records()["jobs"]
        return [job for job in jobs if not job.get("archived") and (job.get("replica") or {}).get("case_id") == case_id]

    def case_replica_preview(self, case_id):
        """What the dialog shows before anything is created."""
        _, product, source = self._replica_case(case_id)
        try:
            video = detect_shots(source["video"])
        except PrepareError as exc:
            raise VoiceError(409, str(exc)) from exc
        chinese = source["chinese"]
        return {"case_id": case_id, "title": source["title"], "product": product, "origin": source["origin"],
                "timed": source["timed"], "machine_transcript": source["machine_transcript"],
                "duration": video["duration"], "shots": len(video["shots"]),
                "lines": [{**line, "zh": chinese[index] if chinese else ""} for index, line in enumerate(source["lines"])],
                "jobs": [{"id": job["id"], "title": job["title"], "status": job.get("status", ""), "product": job["product"]}
                         for job in self._replica_jobs(case_id)]}

    def _replica_script(self, case_id, product, source):
        """The case's words as an adopted script; the same words from the same case keep one script."""
        direction = source["direction_id"]
        if direction:
            try:
                self._creation_direction(product, direction, optional=True)
            except CreationError:
                direction = ""
        result = self.creation.mutate("script", {
            "request_id": "replica-" + uuid4().hex, "revision": self.creation.revision(self.creation.read()),
            "script": {"title": "1:1 复刻｜" + source["title"], "text": "\n".join(line["spoken"] for line in source["lines"]),
                       "translation_zh": "\n".join(source["chinese"]), "product": product, "direction_id": direction, "confirmed": True}},
            {"kind": SOURCE_KIND, "case_id": case_id, "case_title": source["title"]})
        self._link_script_plan(result["script"])
        self.state_cache = None
        return result["script"]

    def start_case_replica(self, case_id, body):
        """Create the job (or return the one already open for this case unless another was asked for), and when
        `optimize` asks for it, start a 写稿 task writing that many line-for-line optimized scripts of the same case."""
        if not isinstance(body, dict):
            raise VoiceError(400, "参数格式无效。")
        count = body.get("optimize", 0)
        if type(count) is not int or not 0 <= count <= MAX_OPTIMIZED:
            raise VoiceError(400, f"优化稿条数须为 0–{MAX_OPTIMIZED}。")
        _, product, source = self._replica_case(case_id)
        with self.lock:
            existing = self._replica_jobs(case_id)
            reused = bool(existing) and body.get("new") is not True
            if not reused:
                script = self._replica_script(case_id, product, source)
                revision = self.production.revision(self.production.records())
        result = ({"job": existing[-1], "reused": True} if reused else
                  self.create_production_job({"request_id": "replica-" + uuid4().hex, "revision": revision, "job": {"script_id": script["id"]}}))
        if count:
            # The original's job stands on its own: a task that cannot start (AI unavailable) is reported, not raised.
            try:
                result["task"] = self.create_ai_task({"request_id": "replica-" + uuid4().hex, "case_id": case_id,
                                                      "revision": self.detail(case_id)["revision"], "action": "variants",
                                                      "instruction": "", "writing_mode": {"mode": "replica", "count": count}})
            except (CreationError, StoreError) as exc:
                result["task_error"] = str(exc)
        return result

    def _replica_origin(self, script):
        """(case_id, aligned) when a script belongs to a case's 1:1 replica: the case's own words, or a script adopted
        from that case's 「照原片优化」 task; None otherwise."""
        origin = script.get("source") or {}
        if origin.get("kind") == SOURCE_KIND and origin.get("case_id"):
            return origin["case_id"], False
        if not origin.get("task_id") or not hasattr(self, "ai"):
            return None
        try:
            task = self.ai.get(origin["task_id"])
        except Exception:
            return None
        mode = (task.get("context") or {}).get("writing_mode") or {}
        return (task["case_id"], True) if mode.get("mode") == "replica" and task.get("case_id") else None

    def _replica_snapshot(self, script_id):
        """The reference for a job made from a replica script, read outside the lock since it decodes the video.

        None for every other script, and for a replica whose case is gone: that job continues as an ordinary one."""
        try:
            script = self.creation.get_script(script_id)
        except CreationError:
            return None
        found = self._replica_origin(script)
        if not found:
            return None
        case_id, aligned = found
        try:
            source = self._replica_case(case_id)[2]
            reference = snapshot(case_id, source, spoken_lines(script["text"]), aligned=aligned)
        except (VoiceError, PrepareError):
            return None
        return reference, source

    def apply_replica(self, resolved, spoken, chinese, found):
        """Turn a job about to be created into a replica job."""
        reference, source = found
        resolved.update(variant_label="1:1 复刻" if reference.get("wording") == "original" else "1:1 复刻·优化稿", replica=reference, lines=line_records(spoken, chinese, reference),
                        problems=list(reference["notes"]), supplement={"captions": [source["caption"]] if source["caption"] else []})
        return resolved

    def _replica_folder(self, identifier):
        return Path(self.production.root) / "replica" / identifier

    def forget_replica_case(self, case_id):
        """The images are frames of the case's video, so they leave with it; its jobs keep their shot times."""
        root = Path(self.production.root)
        for job in self.production.records()["jobs"]:
            if (job.get("replica") or {}).get("case_id") == case_id:
                shutil.rmtree(self._replica_folder(job["id"]), ignore_errors=True)
                for edit in job.get("edits", []):
                    shutil.rmtree(root / "edits" / str(edit.get("id")) / "reference", ignore_errors=True)

    def replica_frames(self, job):
        """The folder of reference images for this job's edits; made once, again only if it went missing."""
        folder = self._replica_folder(job["id"])
        if all((folder / f"shot-{shot['number']:02d}.jpg").is_file() for shot in job["replica"]["shots"]):
            return folder
        try:
            source = case_source(self.store.read_case(job["replica"]["case_id"]))
            return write_frames(source["video"], job["replica"], folder)
        except PrepareError as exc:
            raise VoiceError(409, f"没能准备原片参考图：{exc}") from exc
        except Exception as exc:
            raise VoiceError(409, "原案例或原视频已不在，这张制作单不能再用 1:1 复刻模式，请换其他模式。") from exc
