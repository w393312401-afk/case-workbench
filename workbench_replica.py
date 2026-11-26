"""The 照原片做视频 entry of a case: its transcript as an adopted script and a production job carrying the original's shots."""
from __future__ import annotations

from pathlib import Path
import re
import shutil
import sys
import tempfile
from uuid import uuid4

from case_breakdown_runner import run_prompt
from case_insights import funnel
from case_replica import case_source, keep_written, line_records, needs_table, optimize_focus, parse_titles, snapshot, title_prompt, write_frames, detect_shots
from case_store import StoreError
from creation_store import CreationError
from direction_store import product_key
from elevenlabs_client import VoiceError
from retro_draft import PrepareError
from variant_parse import spoken_lines
from task_activity import safe_error

SOURCE_KIND = "case_replica"
MAX_OPTIMIZED = 5
# Carries production_store.MISSING_TABLE, so the note leaves once every line has its 画面要求.
BARE_TABLE = "意群表还只有原片镜头位置，每句该放什么画面正由 AI 看原片补写（没在补时点「AI 补全脚本信息」，或在制作单里补填）；补完前只能用 1:1 复刻模式剪。"


def _redacted(exc):
    detail = re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|token|authorization)\s*[:=]\s*)[^\s,;]+", r"\1[已隐藏]", str(exc))
    detail = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[已隐藏]", detail)[:500]
    return f"{type(exc).__name__}: {detail}"


def title_failure(exc):
    """Why the AI call behind the replica titles failed, in words fly can act on; never the raw external output."""
    if re.search(r"login|oauth|auth|expired|401|登录|授权", str(exc), re.I):  # the CLI's own words are often English
        return "AI 模型的登录或授权失效了（Claude 请到「设置」重新连接订阅，Codex 请在电脑上打开 Codex 重新登录），之后重新打开这张制作单。"
    reason = safe_error(exc)
    if reason.startswith("本次处理未完成") or reason.startswith("等待服务超时") or reason.startswith("服务连接中断"):
        return "AI 这次没回复，可以稍后重新打开这张制作单再试；连续失败请到「设置」看后台小任务用的模型是否可用。"
    return reason


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
        `optimize` asks for it, start a 写稿 task writing that many line-for-line optimized scripts of the same case.

        `original: false` makes no job from the original's words: only the optimized scripts are written, and a job is
        made later from the one adopted. `model` / `reasoning_effort` pick the AI for the optimized scripts and for the
        original job's automatic 意群表 fill; left out, each uses the writing default."""
        if not isinstance(body, dict):
            raise VoiceError(400, "参数格式无效。")
        count = body.get("optimize", 0)
        if type(count) is not int or not 0 <= count <= MAX_OPTIMIZED:
            raise VoiceError(400, f"优化稿条数须为 0–{MAX_OPTIMIZED}。")
        original = body.get("original", True)
        if type(original) is not bool:
            raise VoiceError(400, "是否用原稿开制作单的参数无效。")
        if not original and not count:
            raise VoiceError(400, "不用原稿开制作单时，至少要写 1 条优化稿。")
        settings = self._replica_model(body)
        _, product, source = self._replica_case(case_id)
        result = {}
        if original:
            with self.lock:
                existing = self._replica_jobs(case_id)
                reused = bool(existing) and body.get("new") is not True
                if not reused:
                    script = self._replica_script(case_id, product, source)
                    revision = self.production.revision(self.production.records())
            result = ({"job": existing[-1], "reused": True} if reused else
                      self.create_production_job({"request_id": "replica-" + uuid4().hex, "revision": revision, "job": {"script_id": script["id"]}},
                                                 fill_settings=settings))
        if count:
            task_body = {"request_id": "replica-" + uuid4().hex, "case_id": case_id, "revision": self.detail(case_id)["revision"],
                         "action": "variants", "instruction": "", "writing_mode": {"mode": "replica", "count": count}, **settings}
            if not original:  # nothing else is made, so a task that cannot start is the answer
                try:
                    return {"task": self.create_ai_task(task_body)}
                except (CreationError, StoreError) as exc:
                    raise VoiceError(getattr(exc, "status", 409) or 409, str(exc)) from exc
            # The original's job stands on its own: a task that cannot start (AI unavailable) is reported, not raised.
            try:
                result["task"] = self.create_ai_task(task_body)
            except (CreationError, StoreError) as exc:
                result["task_error"] = str(exc)
        return result

    @staticmethod
    def _replica_model(body):
        """The model picked in the dialog as task-body keys; empty when the default is kept."""
        model = body.get("model")
        if model is None:
            return {}
        effort = body.get("reasoning_effort")
        if not isinstance(model, str) or not model.strip() or (effort is not None and not isinstance(effort, str)):
            raise VoiceError(400, "所选模型参数无效。")
        return {"model": model, "reasoning_effort": effort}

    def replica_focus(self, case):
        """Which stage the original won at (kept word for word) and which an optimized script may repair, from its
        sales against the same product's videos; None without a matched video or enough peers to tell."""
        video_id = str(((case.get("current") or {}).get("metrics") or {}).get("video_id") or "").strip()
        if not video_id:
            return None
        try:
            videos = self.performance.videos(self.video_case_links())
        except Exception:
            return None
        video = next((item for item in videos if item.get("video_id") == video_id), None)
        return optimize_focus(funnel(video, videos)) if video else None

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
        lines = keep_written(line_records(spoken, chinese, reference), resolved.get("lines") or [])
        resolved.update(variant_label="1:1 复刻" if reference.get("wording") == "original" else "1:1 复刻·优化稿", replica=reference, lines=lines,
                        problems=list(reference["notes"]), supplement={})
        if needs_table(resolved):
            resolved["problems"].append(BARE_TABLE)
        return resolved

    def auto_fill_replica(self, job, settings=None):
        """A replica job made without a real 意群表 (the case's own words) asks the writing AI, once, to describe what
        the original shows under each line, so the edit has more to go on than shot positions; `settings` is the model
        picked in the 照原片做视频 dialog, if any."""
        if not needs_table(job) or not getattr(self, "auto_replica_fill", True) or not getattr(getattr(self, "ai", None), "bridge", None):
            return
        try:
            self.fill_production_job(job["id"], dict(settings or {}))
        except Exception as exc:  # the job page still offers 「AI 补全脚本信息」
            print(f"照原片制作单没能自动补全意群表：{exc}", file=sys.stderr)

    @staticmethod
    def replica_titles_pending(job):
        """A replica job whose own 3 🔴 lines are not written yet: the original's post title (often cut off) is not used."""
        return isinstance(job.get("replica"), dict) and not job["replica"].get("titles_written")

    def write_replica_titles(self, identifier, body=None):
        """Write and keep three new 🔴 title lines for a replica job that has no written titles of its own."""
        job = self.production.job(identifier)
        if not job.get("replica"):
            raise VoiceError(409, "只有照原片做的制作单需要另写标题。")
        copy = self._video_publish_copy(job) or {}
        if not copy.get("titles_pending"):
            return {"titles": copy.get("titles") or []}
        prompt = title_prompt(job.get("product", ""), [line["spoken"] for line in job["lines"]])
        try:
            with tempfile.TemporaryDirectory() as workdir:
                output = run_prompt(self, prompt, workdir, raise_errors=True)
        except Exception as exc:  # the AI call itself failed: log the redacted cause and name it on the page
            print(f"照原片标题没写成功（{identifier}）：{_redacted(exc)}", file=sys.stderr)
            raise VoiceError(502, f"3 条标题没写成功：{title_failure(exc)}") from exc
        titles = parse_titles(output)
        if not titles:
            print(f"照原片标题格式不对（{identifier}）：{str(output or '')[:400]!r}", file=sys.stderr)
            raise VoiceError(502, "3 条标题没写成功：AI 回复的格式不对，可以稍后重新打开这张制作单再试。")
        saved = self.production.mutate("job_captions", {"request_id": "titles-" + uuid4().hex, "job_id": identifier, "captions": titles})
        self.state_cache = None
        return {"titles": saved["captions"]}

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
