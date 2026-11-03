"""Retrospective draft of a posted video: finds the video, prepares its footage and writes the brief.

Footage and downloads live under production/prepublish (the folder also still holds the records of the pre-publish
review, taken offline 2026-11-01; those are read by nothing and leave with their order).
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
from uuid import uuid4

from campaign_impact import product_weekly, ratio, weekly_impact
from direction_store import product_key
from elevenlabs_client import VoiceError
from production_validation import rate
from production_store import JOB_ID
from retro_draft import DRAFT_MODES, PrepareError, draft_mode, draft_prompt, label_shots, prepare


class RetroDraftWorkbenchMixin:
    """Needs self.production, self.performance, self.campaigns and self.store from the workbench."""

    def _draft_root(self):
        return Path(self.production.root) / "prepublish"

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
        cache = self._draft_root() / "videos" / str(video_id)
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
                "行动边界": "closing_allowed为false时结论写成阶段判断并说明待补数据，照样推荐参考价值，不输出建议动作行。为true时可按需建议加码、换执行或停测，动作可省略；用户确认结论和参考价值即可保存复盘。播放分档不是成败结论，数据完整但样本少时须说明局限。"}

    def draft_media(self, task, cancel, progress):
        """The whole brief for a retrospective draft from the posted video's sheets and figures: why it sold, or (no
        orders) where it lost people, or (too little exposure) what can and cannot be judged.

        Never raises: a video that cannot be had still gets the brief, told plainly that no footage was seen."""
        context = task["snapshot"].get("creation_context") or {}
        video = context.get("review_video") or {}
        video_id = str(video.get("video_id") or "")
        folder = self._draft_root() / "drafts" / re.sub(r"[^\w-]", "_", str(task.get("task_id") or uuid4().hex))
        valid = bool(re.fullmatch(r"\d{6,25}", video_id))
        job, record = self._published_job(video_id) if valid else (None, None)
        lines = [row for row in (job or {}).get("lines") or [] if str(row.get("spoken") or "").strip()]
        review_text = self._comment_digest(video.get("product") or task["snapshot"].get("product"))
        figures = self._draft_figures(context, video_id)
        mode = draft_mode(video.get("orders"), video.get("window_views"))
        graded = {"points": context.get("selling_points") or [], "audience": (context.get("direction") or {}).get("audience") or ""}
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
                    "prompt": draft_prompt(lines, None, source=f"没能取得成片（{reason}）", review_text=review_text, figures=figures, mode=mode, **graded)}
        images = [(f"第 {n} 张：{sheet['label']}", folder / sheet["file"]) for n, sheet in enumerate(report["sheets"], 1)]
        return {"status": "attached", "source": label, "images": images, "folder": str(folder), "mode": DRAFT_MODES[mode],
                "prompt": draft_prompt(lines, report, source=label, review_text=review_text, figures=figures, mode=mode, **graded)}

    def delete_prepublish_reviews(self, identifier):
        """Order deletion removes the old review records of the order too."""
        if JOB_ID.fullmatch(str(identifier)):
            shutil.rmtree(self._draft_root() / identifier, ignore_errors=True)
