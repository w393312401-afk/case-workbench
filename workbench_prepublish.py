"""Pre-publish adversarial review of a finished production job's exported video.

Started by hand on a completed 制作单 with the exported file. The workbench prepares frames, transcript and
measurements (prepublish_review.prepare), a model judges, and the record keeps findings the user must close:
an unresolved 必须修 blocks 登记发布 until it is marked 已修改 or 接受风险. Reviews never read performance data;
their pre-mortem is handed to the AI 起草结论 of the published video so the retrospective can check it.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
import threading
from uuid import uuid4

from campaign_impact import product_weekly, ratio, weekly_impact
from codex_bridge import BridgeCancelled
from direction_store import now, product_key
from elevenlabs_client import VoiceError
from prepublish_review import (DRAFT_MODES, REVIEW_MODEL, PrepareError, draft_mode, draft_prompt, label_shots, parse, prepare,
                               prompt)
from production_validation import rate
from production_operations import durable_json, file_lock
from production_store import JOB_ID
from task_activity import activity_label, record_activity, safe_error
from workbench_video_publish import _local_video

REVIEW_ID = re.compile(r"PPR-[0-9a-f]{12}")
ELIGIBLE = {"approved", "awaiting_review"}
RESOLUTIONS = {"fixed": "已修改", "accepted": "接受风险"}
REVIEW_TIMEOUT = 20 * 60


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def blocking(review):
    """必须修 findings of a completed review that are not yet marked 已修改 or 接受风险."""
    if not review or review.get("status") != "completed":
        return []
    return [item for item in review.get("findings", []) if item["severity"] == "must_fix" and not item.get("resolution")]


class PrepublishWorkbenchMixin:
    """Needs self.production, self.editor, self.ai, self.lock and self.activity from the workbench."""

    def _prepublish_state(self):
        with self.lock:
            if not hasattr(self, "_prepublish_live"):
                self._prepublish_live = {}
            return self._prepublish_live

    def _prepublish_root(self, job_id=None):
        root = Path(self.production.root) / "prepublish"
        return root / job_id if job_id else root

    def _prepublish_records(self, job_id):
        folder = self._prepublish_root(job_id)
        records = []
        if folder.is_dir():
            for path in folder.glob("PPR-*/review.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if record.get("status") == "running" and record["id"] not in self._prepublish_state():
                    record = {**record, "status": "interrupted", "error": "工作台重启时这次审查还没做完，请重新发起。"}
                records.append(record)
        return sorted(records, key=lambda row: row.get("created_at") or "")

    def _prepublish_write(self, record):
        folder = self._prepublish_root(record["job_id"]) / record["id"]
        folder.mkdir(parents=True, exist_ok=True)
        durable_json(folder / "review.json", record)

    @staticmethod
    def _prepublish_public(record):
        result = copy.deepcopy(record)
        result.pop("report", None)
        result["report"] = {"duration": (record.get("report") or {}).get("duration")}
        result["report"]["shots"] = len((record.get("report") or {}).get("shots") or [])
        result["report"]["sheets"] = [row["file"] for row in (record.get("report") or {}).get("sheets") or []]
        return result

    def latest_prepublish_review(self, job_id):
        completed = [row for row in self._prepublish_records(job_id) if row.get("status") == "completed"]
        return completed[-1] if completed else None

    def prepublish_block(self, job_id):
        return blocking(self.latest_prepublish_review(job_id))

    def prepublish_reviews(self, identifier):
        if not JOB_ID.fullmatch(str(identifier)):
            raise VoiceError(400, "制作单标识无效。")
        records = self._prepublish_records(identifier)
        live = self._prepublish_state()
        rows = []
        for record in reversed(records):
            row = self._prepublish_public(record)
            if record["id"] in live:
                row["phase"] = live[record["id"]].get("phase") or row.get("phase")
            rows.append(row)
        latest = self.latest_prepublish_review(identifier)
        return {"reviews": rows, "blocking": len(blocking(latest)), "latest_id": latest["id"] if latest else None,
                "running": any(row["status"] == "running" for row in rows)}

    def start_prepublish_review(self, identifier, body):
        if not JOB_ID.fullmatch(str(identifier)) or not isinstance(body, dict):
            raise VoiceError(400, "参数格式无效。")
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[\w-]{10,100}", request_id):
            raise VoiceError(400, "缺少有效的请求标识，请刷新后重试。")
        video = _local_video(body.get("file_path"))
        if self._claude_ready():
            model, effort = REVIEW_MODEL, None
        else:
            settings = self.ai.resolve_default_settings({key: body[key] for key in ("model", "reasoning_effort") if key in body})
            model, effort = settings["model"], settings["reasoning_effort"]
        provider = "codex" if model is None or not str(model).startswith("claude-") else "claude"
        bridge = self.editor if provider == "claude" else getattr(self.ai.bridge, "codex", None)
        if bridge is None:
            raise VoiceError(503, "没有可用的 AI 客户端，无法发起发布前审查。")
        with file_lock(self._prepublish_root() / "locks" / (identifier + ".lock"), "这张制作单正在发起审查，请稍后重试。"):
            records = self._prepublish_records(identifier)
            prior = next((row for row in records if row.get("request_id") == request_id), None)
            if prior:
                return {"review": self._prepublish_public(prior)}
            if any(row["status"] == "running" for row in records):
                raise VoiceError(409, "这张制作单已有一次审查在进行，请等它结束。")
            job = self.production.job(identifier)
            if job.get("archived"):
                raise VoiceError(409, "制作单已归档，不能发起审查。")
            if job.get("status") not in ELIGIBLE:
                raise VoiceError(409, "制作单还没完成剪辑审查，导出成片后再发起发布前审查。")
            lines = [row for row in job.get("lines") or [] if str(row.get("spoken") or "").strip()]
            if not lines:
                raise VoiceError(409, "制作单没有口播原稿，无法对照审查。")
            timeline = next((row for row in reversed(job.get("timelines") or []) if not row.get("archived")), {}) or {}
            stat = video.stat()
            review_id = "PPR-" + uuid4().hex[:12]
            record = {"id": review_id, "job_id": identifier, "request_id": request_id, "status": "running", "phase": "排队中",
                      "created_at": now(), "finished_at": None, "provider": provider, "model": model, "reasoning_effort": effort,
                      "file": {"path": str(video), "name": video.name, "size": stat.st_size, "mtime": stat.st_mtime},
                      "timeline": timeline.get("name") or "", "creator": timeline.get("creator") or "",
                      "folder": timeline.get("folder") or "", "findings": [], "premortem": "", "summary": "", "error": ""}
            live = {"cancel": threading.Event(), "phase": "排队中"}
            with self.lock:
                self._prepublish_state()[review_id] = live
            try:
                self._prepublish_write(record)
            except Exception:
                with self.lock:
                    self._prepublish_state().pop(review_id, None)
                raise
        record_activity(self, "prepublish:" + review_id, kind="prepublish", owner_id=identifier, source_id=review_id,
                        product=job.get("product", ""), case_id=job.get("case_id"), title="发布前审查 · " + activity_label(job, identifier),
                        step="publish", status="running", phase="准备材料", message="正在抽帧和转写成片")
        worker = threading.Thread(target=self._run_prepublish, args=(identifier, review_id, job, lines, video, bridge, model, effort),
                                  name="prepublish-" + review_id, daemon=True)
        worker.start()
        return {"review": self._prepublish_public(record)}

    def _claude_ready(self):
        try:
            return bool(self.editor is not None and self.editor.status().get("available"))
        except Exception:
            return False

    def _shot_truth(self, job, video, cancel, progress=None):
        """The job's latest delivered AI edit, matched shot by shot to its human-checked material labels and to the
        exported video. [] when the job has no delivered edit (made by hand, or outside the workbench)."""
        if not job:
            return []
        root = Path(self.production.root) / "edits"
        for edit in reversed(job.get("edits") or []):
            folder = root / str(edit.get("id") or "")
            delivered, candidates = folder / "delivered-timeline.json", folder / "candidates.json"
            if not (delivered.is_file() and candidates.is_file()):
                continue
            try:
                if progress:
                    progress("正在核对镜头和素材标签")
                timeline = json.loads(delivered.read_text(encoding="utf-8"))["timeline"]
                rows = json.loads(candidates.read_text(encoding="utf-8")).get("candidates") or []
                return label_shots(video, timeline, rows, rate(timeline.get("fps")), cancel)
            except PrepareError:
                raise
            except Exception:  # A record that cannot be read leaves the review on its pictures alone.
                return []
        return []

    def _prepublish_phase(self, review_id, phase):
        live = self._prepublish_state().get(review_id)
        if live is not None:
            with self.lock:
                live["phase"] = phase
        record_activity(self, "prepublish:" + review_id, phase=phase, message=phase)

    def _run_prepublish(self, job_id, review_id, job, lines, video, bridge, model, effort):
        live = self._prepublish_state()[review_id]
        cancel = live["cancel"]
        folder = self._prepublish_root(job_id) / review_id
        record = next(row for row in self._prepublish_records(job_id) if row["id"] == review_id)
        status, error = "failed", ""
        try:
            record["file"]["sha256"] = _sha256(video)
            self._prepublish_phase(review_id, "准备材料")
            report = prepare(video, [row["spoken"] for row in lines], folder, cancel, on_step=lambda text: self._prepublish_phase(review_id, text))
            report["shot_labels"] = self._shot_truth(job, video, cancel, lambda text: self._prepublish_phase(review_id, text))
            record["report"] = report
            market = "英国" if "英国" in (record["folder"] + record["creator"]) else "美国（默认）"
            brief = prompt(job, lines, report, market=market, review_text=self._comment_digest(job.get("product")))
            (folder / "prompt.md").write_text(brief, encoding="utf-8")
            images = [(f"第 {n} 张：{sheet['label']}", folder / sheet["file"]) for n, sheet in enumerate(report["sheets"], 1)]
            self._prepublish_phase(review_id, "AI 正在审查")
            output = bridge.run(brief, cwd=str(folder.resolve()), on_progress=lambda text: None, cancel_event=cancel,
                                model=model, reasoning_effort=effort, images=images)
            (folder / "output.md").write_text(output or "", encoding="utf-8")
            result = parse(output)
            findings = result["findings"]
            for number, item in enumerate(findings, 1):
                item["id"] = f"F{number}"
                item["resolution"] = None
            record.update(findings=findings, premortem=result["premortem"], summary=result["summary"], ab_tests=result["ab_tests"])
            status = "completed"
        except (BridgeCancelled, PrepareError) as exc:
            status, error = ("cancelled", "本次审查已取消。") if cancel.is_set() else ("failed", str(exc)[:500])
        except ValueError as exc:
            error = f"审查回复格式不符，未保存结果：{exc}"
        except Exception as exc:
            error = safe_error(exc) if not isinstance(exc, VoiceError) else str(exc)
        record.update(status=status, error=error, finished_at=now(), phase="")
        try:
            self._prepublish_write(record)
        finally:
            with self.lock:
                self._prepublish_state().pop(review_id, None)
        must = len(blocking(record))
        message = {"completed": f"审查完成：{must} 条必须修待确认" if must else "审查完成，没有必须修的问题",
                   "cancelled": "本次审查已取消。"}.get(status, error or "审查未完成")
        record_activity(self, "prepublish:" + review_id, status="needs_attention" if status == "completed" and must else status,
                        phase="审查结束", message=message, result_count=len(record["findings"]), event_key="done:" + status)

    def prepublish_sheet(self, identifier, review_id, name):
        if not JOB_ID.fullmatch(str(identifier)) or not REVIEW_ID.fullmatch(str(review_id)) or not re.fullmatch(r"(?:opening|detail\d{2}|shots\d{1,2})\.jpg", name):
            raise VoiceError(400, "图片标识无效。")
        path = self._prepublish_root(identifier) / review_id / name
        if not path.is_file():
            raise VoiceError(404, "这张审查拼图已不存在。")
        return path

    def cancel_prepublish_review(self, identifier, review_id):
        live = self._prepublish_state().get(review_id)
        if not JOB_ID.fullmatch(str(identifier)) or not REVIEW_ID.fullmatch(str(review_id)):
            raise VoiceError(400, "审查标识无效。")
        if not live:
            raise VoiceError(409, "这次审查已经结束。")
        live["cancel"].set()
        return {"cancelled": True}

    def resolve_prepublish_finding(self, identifier, review_id, body):
        if not JOB_ID.fullmatch(str(identifier)) or not REVIEW_ID.fullmatch(str(review_id)) or not isinstance(body, dict):
            raise VoiceError(400, "参数格式无效。")
        resolution, note = body.get("resolution"), str(body.get("note") or "").strip()[:500]
        if resolution is not None and resolution not in RESOLUTIONS:
            raise VoiceError(400, "请选择「已修改」或「接受风险」。")
        with file_lock(self._prepublish_root() / "locks" / (identifier + ".lock"), "这张制作单的审查正在保存，请稍后重试。"):
            record = next((row for row in self._prepublish_records(identifier) if row["id"] == review_id), None)
            if not record or record.get("status") != "completed":
                raise VoiceError(404, "没有找到这次已完成的审查。")
            finding = next((item for item in record["findings"] if item["id"] == body.get("finding_id")), None)
            if not finding:
                raise VoiceError(404, "没有找到这条审查意见。")
            finding["resolution"] = {"state": resolution, "label": RESOLUTIONS[resolution], "note": note, "at": now()} if resolution else None
            self._prepublish_write(record)
        return {"review": self._prepublish_public(record), "blocking": len(self.prepublish_block(identifier))}

    def prepublish_for_video(self, video_id):
        """The latest completed review of the job published as this post, for the retrospective draft."""
        for job_id, entry in self._video_publications().read().get("jobs", {}).items():
            records = [entry.get("publication")] + list(entry.get("history") or [])
            if any(str((row or {}).get("post_id") or "") == str(video_id) for row in records):
                review = self.latest_prepublish_review(job_id)
                if review:
                    return {"reviewed_at": review["finished_at"], "file": review["file"]["name"], "summary": review["summary"],
                            "premortem": review["premortem"],
                            "findings": [{key: item.get(key) for key in ("severity", "layer", "at", "problem", "consequence", "fix")}
                                         | {"resolution": (item.get("resolution") or {}).get("label")} for item in review["findings"]]}
        return None

    def _published_job(self, video_id):
        """The production job registered under this post, if the video was made in the workbench."""
        for job_id, entry in self._video_publications().read().get("jobs", {}).items():
            records = [entry.get("publication")] + list(entry.get("history") or [])
            for record in records:
                if record and str(record.get("post_id") or "") == str(video_id):
                    try:
                        return self.production.job(job_id), record
                    except VoiceError:
                        return None, record
        return None, None

    def _published_video(self, video_id, video, record, cancel, progress):
        """The posted video, in order: the managed file of its registration, the case's attached video, a copy
        downloaded earlier for a draft, then a fresh download from TikTok. Returns (path, source label)."""
        if record and record.get("file"):
            try:
                path = self._video_publications().managed_path(record["file"])
                if path.is_file():
                    return path, "登记发布时保存的成片"
            except VoiceError:
                pass
        case_id = (video or {}).get("case_id") or (self.video_case_links().get(str(video_id)) if hasattr(self, "video_case_links") else None)
        if case_id:
            try:
                asset = ((self.store.read_case(case_id).get("current") or {}).get("assets") or {}).get("video")
                if asset and Path(asset).is_file():
                    return Path(asset), "案例里已关联的原视频"
            except Exception:
                pass
        cache = self._prepublish_root() / "videos" / str(video_id)
        found = sorted(cache.glob(f"{video_id}.*")) if cache.is_dir() else []
        if found:
            return found[0], "之前起草时下载的原视频"
        if cancel.is_set():
            raise PrepareError("本次起草已取消。")
        progress("正在从 TikTok 下载这条视频")
        account = str((record or {}).get("account") or (video or {}).get("account") or "").lstrip("@") or "_"
        url = (record or {}).get("post_url") or f"https://www.tiktok.com/@{account}/video/{video_id}"
        cache.mkdir(parents=True, exist_ok=True)
        downloader = getattr(self, "draft_downloader", None)
        if downloader is None:
            from case_auto_breakdown import download_tiktok as downloader
        return Path(downloader(url, cache, str(video_id))), "从 TikTok 下载"

    def _comment_digest(self, product):
        """The product's comment-library digest as text for layer 2 (likely comment-section doubts); '' when none."""
        try:
            digest = self.review_digest(str(product or "")) if hasattr(self, "review_digest") else None
        except Exception:
            digest = None
        if not digest:
            return ""
        keep = {key: digest.get(key) for key in ("viewer_doubts", "viewer_pushback", "buyer_limits", "unanswered_questions", "topics")
                if digest.get(key)}
        return json.dumps(keep, ensure_ascii=False, indent=0).replace("<", "\\u003c").replace(">", "\\u003e")[:12000]

    def _campaign_weekly(self, video):
        """The video's product weekly figures in its own shop, and the weeks whose exports are whole."""
        shop = video.get("shop") or "local"
        return product_weekly(self.performance.analytics(shop), self.performance.videos(shop=shop),
                              self.performance.summary(shop)["weeks"], video.get("product"))

    def _campaign_lifts(self, video, rows):
        """For each campaign touching the observation window: how the product's weeks inside it compare with the
        quiet weeks before. Same method as the data board; says plainly when the figures cannot support a comparison."""
        label = product_key(video.get("product")) or "该产品"
        try:
            totals, complete = self._campaign_weekly(video)
            calendar = self.campaigns.public()["campaigns"]
        except Exception:
            return []
        lines = []
        for row in rows:
            result = weekly_impact(row, calendar, totals, complete)
            if result.get("missing") == "weeks":
                text = "活动日占 4 天以上的整周没有可用数据（活动太短，或这些周的导出有缺口），无法按周对比。"
            elif result.get("missing") == "baseline":
                text = f"此前没有足够的无活动整周（只有 {result['baseline']} 周），无法按周对比。"
            else:
                gmv, orders = ratio(result["gmv"]), ratio(result["orders"])
                text = (f"{label} 活动周（{result['weeks']} 周）周均 GMV ${result['gmv'][0]:,.0f}，此前 {result['baseline']} 个无活动周 ${result['gmv'][1]:,.0f}"
                        f"（{'×' + str(gmv) if gmv is not None else '对照为 0，没有倍数'}）；周均订单 {result['orders'][0]:.1f} 对 {result['orders'][1]:.1f}"
                        f"（{'×' + str(orders) if orders is not None else '没有倍数'}）。")
            lines.append({"活动": row["name"], "同产品按周对比": text})
        return lines

    def _draft_figures(self, context, video_id):
        """The draft's data: window totals, each exported week's own rates, and its peers' medians (peer_reference)."""
        video, summary = context.get("review_video") or {}, context.get("review_summary") or {}
        try:
            weeks = self.performance.rate_series(video_id)
            reference = self.peer_reference({**video, "video_id": video_id}, product_key(video.get("product")))
        except Exception:
            weeks, reference = [], {}
        posted = video.get("posted_on") or str(video.get("posted_at") or "")[:10]
        campaigns = self.campaigns.overlapping(posted, video.get("window_end")) if hasattr(self, "campaigns") else []
        notes = {"official": "官方原文", "media": "多家媒体一致", "disputed": "来源说法不一", "planning": "第三方规划参考，日期待核对", "manual": "人工登记"}
        return {"发布日期": posted, "观察窗截至": video.get("window_end"),
                **({"观察窗内的平台活动": {
                    "活动": [f"{row['name']}（{row['start']} 至 {row['end']}，太平洋时间；{notes.get(row['confidence'], '')}"
                           f"{'，已人工核对' if row.get('verified') else ''}）" for row in campaigns],
                    "该产品在这些活动里的周数据（整个产品的所有视频，不是这一条视频）": self._campaign_lifts(video, campaigns),
                    "判读要求": "202H 是高客单产品，平台活动期的折扣和流量会直接抬高销量。结论里要把活动带来的增量和内容本身的表现分开说；"
                              "日期标为待核对或说法不一的活动，只能说可能重叠，不能当成事实。"
                              "周数据对比说明的是这个产品在活动周比此前无活动周高或低多少，不等于活动造成的：自然趋势、当周发布的视频数量、库存都会影响；"
                              "高客单品每周只有几十单，几单的差别就能拉动倍数，要同时看订单数再下判断；活动日期未核对的，倍数只能说明那几周的变化。"}} if campaigns else {}),
                "总计": {key: video.get(key) for key in ("views", "window_views", "impressions", "clicks", "orders", "gmv", "ctr_pct", "ctor_pct")},
                "每周（完播率 finish_rate_pct、点击率 ctr_pct、点击后转化 ctor_pct）": weeks,
                "同类对照（同方向、观察窗截至前 8 周的视频；某项不足 5 条时这一项改用同产品同期，pool 写明用的哪组；"
                "window_views 是观察窗播放，ctor_pct_selling 只算出过单的视频）": reference,
                "发布后14天观察与证据状态（冻结于本次任务发起时）": context.get("review_context") or {"说明": "历史任务没有冻结此项，不能用今天的状态补写历史。"},
                "行动边界": "closing_allowed为false时结论写成阶段判断并说明待补数据，照样推荐参考价值，不输出建议动作行。为true时可按需建议加码、换执行或停测，动作可省略；用户确认结论和参考价值即可保存复盘。播放分档不是成败结论，数据完整但样本少时须说明局限。",
                **({"发布前审查（发布前预测的失败原因，请对照数据说明哪些应验、哪些没有）": {
                    "事前验尸": context["prepublish_review"].get("premortem"),
                    "杀手清单": [f"{row.get('at')} {row.get('problem')}（{row.get('resolution') or '未处理'}）"
                                 for row in context["prepublish_review"].get("findings") or []]}}
                   if context.get("prepublish_review") else {})}

    def draft_media(self, task, cancel, progress):
        """The whole brief for a retrospective draft from the posted video's sheets and figures: why it sold, or (no
        orders) where it lost people, or (too little exposure) what can and cannot be judged.

        Never raises: a video that cannot be had still gets the brief, told plainly that no footage was seen."""
        context = task["snapshot"].get("creation_context") or {}
        video = context.get("review_video") or {}
        video_id = str(video.get("video_id") or "")
        folder = self._prepublish_root() / "drafts" / re.sub(r"[^\w-]", "_", str(task.get("task_id") or uuid4().hex))
        valid = bool(re.fullmatch(r"\d{6,25}", video_id))
        job, record = self._published_job(video_id) if valid else (None, None)
        lines = [row for row in (job or {}).get("lines") or [] if str(row.get("spoken") or "").strip()]
        review_text = self._comment_digest(video.get("product") or task["snapshot"].get("product"))
        figures = self._draft_figures(context, video_id)
        mode = draft_mode(video.get("orders"), video.get("window_views"))
        try:
            if not valid:
                raise PrepareError("没有视频编号")
            source, label = self._published_video(video_id, video, record, cancel, progress)
            shutil.rmtree(folder, ignore_errors=True)
            report = prepare(source, [row["spoken"] for row in lines], folder, cancel, on_step=progress)
            report["shot_labels"] = self._shot_truth(job, source, cancel, progress)
        except Exception as exc:  # The draft still runs on the script and figures, and says it saw no footage.
            reason = str(exc)[:300] or type(exc).__name__
            folder.mkdir(parents=True, exist_ok=True)
            return {"status": "unavailable", "reason": reason, "folder": str(folder), "mode": DRAFT_MODES[mode],
                    "prompt": draft_prompt(lines, None, source=f"没能取得成片（{reason}）", review_text=review_text, figures=figures, mode=mode)}
        images = [(f"第 {n} 张：{sheet['label']}", folder / sheet["file"]) for n, sheet in enumerate(report["sheets"], 1)]
        return {"status": "attached", "source": label, "images": images, "folder": str(folder), "mode": DRAFT_MODES[mode],
                "prompt": draft_prompt(lines, report, source=label, review_text=review_text, figures=figures, mode=mode)}

    def delete_prepublish_reviews(self, identifier):
        """Order deletion removes its review folders too."""
        if JOB_ID.fullmatch(str(identifier)):
            shutil.rmtree(self._prepublish_root(identifier), ignore_errors=True)
