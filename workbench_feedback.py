"""Explicit video/window feedback origins and their next-round execution records."""
from __future__ import annotations

import copy
import math
import re
import statistics
from datetime import date, timedelta

from creation_store import CreationError, text
from direction_store import product_key
from performance_store import RATE_MIN_VIEWS
from prepublish_review import parse_card
from review_policy import review_context, report_window
from manual_review_status import ManualReviewStatus

JUDGED = {"focus", "follow", "watch", "drop"}
DRAFT_ACTIONS = {"加码": "scale", "换执行": "execution", "停测": "stop"}
DRAFT_REFERENCES = (("中等", "medium"), ("中", "medium"), ("高", "high"), ("弱", "weak"), ("无", "none"))
SUMMARY_METRICS = ("views", "window_views", "clicks", "orders", "gmv", "ctr_pct", "ctor_pct", "gpm")
STRONG, WEAK = 1.2, 0.8  # this video against its peers' median
RECENT_DAYS = 56  # peers are the videos of the eight weeks up to the reviewed video's window end
MIN_PEERS = 5  # a direction with fewer peers than this for a figure falls back to the whole product
POOLS = {"direction": "同方向", "product": "同产品"}
CTOR_EXPECTED_ORDERS = 3  # fewer expected orders than this and zero orders says nothing


def draft_funnel(video, weeks, reference):
    """The four funnel steps of a posted video against its peers (see peer_reference), each judged strong, weak, flat
    or unknown.

    Rates come from the video's busiest exported week, the same week each peer contributes to the medians. A rate
    is judged only when that week reached RATE_MIN_VIEWS, and conversion only when the clicks would be expected to
    bring CTOR_EXPECTED_ORDERS orders at the selling peers' median; otherwise the step is unknown and says why."""
    week = max((row for row in weeks or [] if isinstance(row.get("views"), (int, float))), key=lambda row: row["views"], default=None)
    week_views = week["views"] if week else None
    median = lambda key: (reference or {}).get(key, {}).get("median") if isinstance((reference or {}).get(key), dict) else None
    number = lambda value: isinstance(value, (int, float)) and math.isfinite(value)
    rated = bool(week_views and week_views >= RATE_MIN_VIEWS)
    clicks = week_views * week["ctr_pct"] / 100 if rated and number(week.get("ctr_pct")) else None
    ctor_reference = median("ctor_pct_selling")
    expected = clicks * ctor_reference / 100 if clicks is not None and number(ctor_reference) else None
    thin = f"播放最多那周只有 {round(week_views or 0)} 次播放" if week else "没有每周数据"
    steps = [("views", "观察窗播放", video.get("window_views"), "window_views", "count", True, "近期还没有可对照的视频"),
             ("finish", "完播率", (week or {}).get("finish_rate_pct"), "finish_rate_pct", "pct", rated, thin),
             ("ctr", "点击率", (week or {}).get("ctr_pct"), "ctr_pct", "pct", rated, thin),
             ("ctor", "点击后转化", (week or {}).get("ctor_pct"), "ctor_pct_selling", "pct",
              expected is not None and expected >= CTOR_EXPECTED_ORDERS,
              thin if not rated else f"约 {round(clicks or 0)} 次点击，按同类转化预计约 {expected or 0:.1f} 单，"
              f"不到 {CTOR_EXPECTED_ORDERS} 单比不出高低")]
    result = []
    for key, label, value, figure, unit, enough, why in steps:
        base, peers = median(figure), (reference or {}).get(figure) or {}
        # The ratio is drawn whenever both figures exist; only a step with enough data is judged from it.
        ratio = value / base if number(value) and number(base) and base > 0 else None
        verdict = "unknown" if ratio is None or not enough else "strong" if ratio >= STRONG else "weak" if ratio <= WEAK else "flat"
        result.append({"key": key, "label": label, "value": value if number(value) else None, "reference": base if number(base) else None,
                       "pool": POOLS.get(peers.get("pool"), ""), "peers": peers.get("videos"),
                       "unit": unit, "ratio": round(ratio, 2) if ratio is not None else None, "verdict": verdict,
                       "note": "" if verdict != "unknown" else why if not enough or not number(base) else "数据缺失"})
    # Where viewers are first lost: the earliest weak rate, else exposure itself.
    drop = next((row["key"] for row in result[1:] if row["verdict"] == "weak"), "views" if result[0]["verdict"] == "weak" else None)
    return {"week": {key: week.get(key) for key in ("start", "end", "views")} if week else None, "steps": result, "drop": drop,
            "period": (reference or {}).get("period"), "direction_id": (reference or {}).get("direction_id")}


def review_identity(value):
    if not isinstance(value, dict):
        return None
    return (product_key(value.get("product")), str(value.get("video_id") or ""), value.get("window_end"))


def review_draft(task):
    """The conclusion, recommended reference value and action an AI draft ends with
    (「结论：…」「参考价值：中等（依据）」「建议动作：…」), from its latest completed round."""
    turn = next((row for row in reversed(task.get("rounds") or []) if row.get("status") == "completed" and str(row.get("output") or "").strip()), None)
    if not turn:
        return None
    output = str(turn["output"])
    clean = lambda value: re.sub(r"[*_`#>]+", "", value).strip()
    conclusions = re.findall(r"^[\s*_#>-]*结论[*_\s]*[:：](.+)$", output, re.M)
    actions = re.findall(r"^[\s*_#>-]*建议动作[*_\s]*[:：](.+)$", output, re.M)
    result = {"conclusion": clean(conclusions[-1]) if conclusions else "", "action": DRAFT_ACTIONS.get(clean(actions[-1]).strip("。.!！"), "") if actions else "",
              "card": parse_card(output)}
    references = re.findall(r"^[\s*_#>-]*参考价值[*_\s]*[:：](.+)$", output, re.M)
    line = clean(references[-1]) if references else ""
    reference = next((key for word, key in DRAFT_REFERENCES if line.startswith(word)), "")
    if reference:
        reason = re.search(r"[（(](.+?)[）)]", line)
        result.update(reference_value=reference, reference_reason=reason.group(1).strip() if reason else "")
    return result


class FeedbackWorkbenchMixin:
    def review_status_snapshot(self):
        with self.lock:
            return ManualReviewStatus(self.review_followups.read()["entries"], self.creation.read()["scripts"],
                                      publications=self._video_publications().read()["jobs"].values())

    def annotate_review_status(self, videos, snapshot=None):
        snapshot = snapshot if snapshot is not None else self.review_status_snapshot()
        result = copy.deepcopy(videos)
        for video in result:
            video["review_status"] = snapshot.for_video(video)
        return result

    def review_videos(self, videos=None):
        """Include registered posts before a sales export arrives, without making up sales."""
        videos = copy.deepcopy(self.performance.videos() if videos is None else videos)
        by_id = {str(row["video_id"]): row for row in videos}
        scripts = self.creation.read()["scripts"]
        registrations = {}

        def add(identifier, published, source):
            if not identifier or not published or not source.get("product"):
                return
            registrations[str(identifier)] = {"video_id": str(identifier), "product": product_key(source.get("product")),
                "posted_on": published, "registered_on": published, "title": source.get("title") or str(identifier),
                "headline": source.get("title") or str(identifier), "account": source.get("account") or "",
                "window_end": report_window(published), "registered": True}

        for script in scripts:
            add(script.get("video_id"), script.get("published_on"), script)
        # One script can have several published videos. Its single video_id is
        # not an inventory; include every receipt, newest correction per post.
        for entry in self._video_publications().read()["jobs"].values():
            for receipt in [*entry.get("history", []), entry.get("publication")]:
                if receipt:
                    source = (receipt.get("source") or {}).get("script") or {}
                    add(receipt.get("post_id"), receipt.get("published_on"), {**source, "account": receipt.get("account") or source.get("account")})
        for identifier, registration in registrations.items():
            if identifier in by_id:
                video = by_id[identifier]
                if product_key(video.get("product")) == registration["product"]:
                    video.update(registered=True, registered_on=registration["posted_on"])
            else:
                video = {**registration, "registration_only": True, "status": "incomplete", "status_label": "待补数据",
                         "views": None, "window_views": None, "orders": None, "last_data": None,
                         "observation_coverage_complete": False, "flags": []}
                videos.append(video)
                by_id[identifier] = video
        for video in videos:
            video["review_context"] = review_context(video)
        return self.annotate_review_status(videos)

    def feedback_chain(self, video_id, product, window_end=None):
        product = product_key(text(product, "产品", limit=200))
        video_id = text(video_id, "视频编号", limit=25)
        if product == "all" or not re.fullmatch(r"\d{6,25}", video_id):
            raise CreationError(400, "请指定视频编号与具体产品。")
        with self.lock, self.store.index_snapshot():
            video = self.performance.video(video_id)
            review_video = next((row for row in self.review_videos() if str(row["video_id"]) == video_id), None)
            if video and review_video:
                video.update({key: review_video[key] for key in ("review_context", "registered", "registered_on") if key in review_video})
            elif not video:
                video = review_video
            if not video or product_key(video.get("product")) != product:
                raise CreationError(404, "当前产品下未找到这条视频的表现资料。")
            window_end = window_end or video.get("window_end")
            if window_end:
                try:
                    if date.fromisoformat(window_end).isoformat() != window_end:
                        raise ValueError()
                except (TypeError, ValueError) as exc:
                    raise CreationError(400, "观察窗须为 YYYY-MM-DD。") from exc
            identity = (product, video_id, window_end)
            review_catalog = self.review_followups.public(product)
            review = next((row for row in review_catalog["entries"] if review_identity(row) == identity), None)
            source = self.review_followups.source_snapshot(review) if review and review["state"] == "reviewed" else None
            creation, production = self.creation.read(), self.production.records()
            scripts = [row for row in creation["scripts"] if product_key(row.get("product")) == product]
            plans = [row for row in creation["plans"] if product_key(row.get("product")) == product
                     and review_identity(row.get("source_review")) == identity]
            plan_ids = {row["id"] for row in plans}
            retrospectives, next_tasks = [], []
            for task in self.ai.list_tasks(product=product):
                raw = self.ai._read(task["task_id"])
                snapshot = raw.get("snapshot") or {}
                if product_key(snapshot.get("product")) != product:
                    continue
                context = snapshot.get("creation_context") or {}
                ref = context.get("source_review") or (context.get("plan") or {}).get("source_review")
                explicit = review_identity(ref) == identity
                if task.get("action") == "variants" and explicit:
                    next_tasks.append(self._feedback_task(task, ref, True))
                if task.get("action") != "retrospective":
                    continue
                metrics_video = ((snapshot.get("current") or {}).get("metrics") or {}).get("video_id")
                sales_video = (snapshot.get("sales_data") or {}).get("video") or {}
                frozen_video = context.get("review_video") or sales_video
                bound_id = str(metrics_video or frozen_video.get("video_id") or "")
                frozen_window = frozen_video.get("window_end")
                if explicit or not ref and bound_id == video_id and (not frozen_window or frozen_window == window_end):
                    retrospectives.append(self._feedback_task(task, ref, explicit, frozen_window, raw if context.get("review_draft") else None))
            next_scripts = []
            linked_script_ids = {row.get("script_id") for row in plans if row.get("script_id")}
            next_task_ids = {row["task_id"] for row in next_tasks}
            for script in scripts:
                origin = script.get("source") or {}
                context = (origin.get("snapshot") or {}).get("creation_context") or {}
                planned = context.get("plan") or {}
                if (script["id"] in linked_script_ids or origin.get("plan_id") in plan_ids or planned.get("id") in plan_ids
                        or origin.get("task_id") in next_task_ids):
                    next_scripts.append(self._feedback_script(script))
            next_script_ids = {row["id"] for row in next_scripts}
            publications = self._video_publications().read().get("jobs", {})
            source_script_ids = {row["id"] for row in scripts if str(row.get("video_id") or "") == video_id}
            source_outputs, next_outputs = [], []
            for key, kind in (("jobs", "video"), ("carousels", "carousel")):
                for row in production.get(key, []):
                    if product_key(row.get("product")) != product:
                        continue
                    entry = publications.get(row["id"], {}) if kind == "video" else {}
                    records = [entry.get("publication"), *entry.get("history", [])] if kind == "video" else [row.get("publication"), *row.get("publication_history", [])]
                    if any(isinstance(record, dict) and str(record.get("post_id") or "") == video_id for record in records):
                        source_outputs.append(self._feedback_output(row, kind))
                        for record in records:
                            if isinstance(record, dict) and str(record.get("post_id") or "") == video_id:
                                source_script_ids.add(record.get("script_id") or row.get("script_id"))
                    if row.get("script_id") in next_script_ids:
                        next_outputs.append(self._feedback_output(row, kind))
            cases = [{"id": row["case_id"], "title": (row.get("current") or {}).get("title") or row["case_id"]}
                     for row in self.store.read_index()["cases"] if product_key(row.get("product")) == product and
                     str(((row.get("current") or {}).get("metrics") or {}).get("video_id") or "") == video_id]
            variant_links = []
            for direction in sorted({row.get("direction_id") for row in plans if row.get("direction_id")} |
                                    {row.get("direction_id") for row in next_scripts if row.get("direction_id")}):
                try:
                    progress = self.direction_progress(direction, product)
                except Exception as exc:
                    if getattr(exc, "status", None) not in {400, 404, 409}:
                        raise
                    continue
                for variant in progress["variants"]:
                    records = variant["records"]
                    if any(row["id"] in plan_ids for row in records["plans"]) or any(row["id"] in next_script_ids for row in records["scripts"]):
                        if sum(row.get("key") == variant.get("key") for row in progress["variants"]) == 1:
                            variant_links.append({"direction_id": direction, "index": variant["index"], "key": variant["key"],
                                                  "snapshot": copy.deepcopy(variant["snapshot"])})
            drafts = sorted((row for row in retrospectives if row["review_draft"]), key=lambda row: row.get("created_at") or "")
            summary = self.review_summary(video, product, scripts)
            manual_status = ManualReviewStatus(review_catalog["entries"], scripts).for_video(
                {**video, "window_end": window_end}, allow_legacy=window_end == video.get("window_end"))
            summary["review_status"] = copy.deepcopy(manual_status)
            try:
                funnel = draft_funnel(video, self.performance.rate_series(video_id), self.peer_reference(video, product, scripts))
            except Exception:  # The chart is a convenience; the review itself never depends on it.
                funnel = None
            return {"product": product, "video_id": video_id, "window_end": window_end,
                    "review_status": manual_status,
                    "current_window": window_end == video.get("window_end"), "review_context": video.get("review_context") or review_context(video),
                    "video": {key: video.get(key) for key in ("title", "headline", "account", "posted_on", "window_end", "registration_only")},
                    "campaigns": self.campaigns.overlapping(video.get("posted_on"), window_end),
                    "summary": summary, "funnel": funnel, "draft": drafts[-1] if drafts else None,
                    "review": copy.deepcopy(review), "review_revision": review_catalog["revision"], "source_review": source,
                    "experiences": [item for item in self.review_experience_catalog({"product": [product], "status": ["all"]})["items"]
                                    if item["source"]["review"]["video_id"] == video_id and item["source"]["review"]["window_end"] == window_end],
                    "creation_revision": self.creation.revision(creation), "cases": cases,
                    "source_scripts": [self._feedback_script(row) for row in scripts if row["id"] in source_script_ids],
                    "source_outputs": source_outputs, "retrospectives": retrospectives, "next_tasks": next_tasks,
                    "plans": [{**copy.deepcopy(row), "review_current": bool(source and row.get("source_review") == source)} for row in plans],
                    "next_scripts": next_scripts, "next_outputs": next_outputs, "variants": variant_links}

    @staticmethod
    def _feedback_script(row):
        return {key: copy.deepcopy(row.get(key)) for key in ("id", "product", "direction_id", "title", "version", "status", "archived", "parent_id")}

    @staticmethod
    def _feedback_output(row, kind):
        return {"kind": kind, **{key: copy.deepcopy(row.get(key)) for key in ("id", "script_id", "title", "status", "archived")}}

    def peer_reference(self, video, product, scripts=None):
        """What a retrospective compares a video with: the same direction's videos of the eight weeks up to its window
        end, each figure falling back to the whole product over the same weeks when the direction has fewer than
        MIN_PEERS videos for it. The reviewed video itself is never its own peer.

        Views are the median observation-window views of judged videos posted in those weeks; rates are the median of
        each peer's busiest week inside them (PerformanceStore.rate_medians)."""
        video_id = str(video.get("video_id") or "")
        until = video.get("window_end") or date.today().isoformat()
        since = (date.fromisoformat(until) - timedelta(days=RECENT_DAYS)).isoformat()
        directions = self._video_directions(product, scripts)
        direction = directions.get(video_id)
        rows = [row for row in self.performance.videos() if product_key(row.get("product")) == product and str(row["video_id"]) != video_id]
        pools = [("direction", [row for row in rows if directions.get(str(row["video_id"])) == direction])] if direction else []
        pools.append(("product", rows))
        reference = {"period": {"start": since, "end": until}, "direction_id": direction or ""}
        for name, members in reversed(pools):  # The direction, when it has enough peers, overwrites the product.
            recent = [row["window_views"] for row in members if row.get("status") in JUDGED and since <= str(row.get("posted_on") or "") <= until
                      and isinstance(row.get("window_views"), (int, float)) and math.isfinite(row["window_views"])]
            figures = {"window_views": {"median": round(statistics.median(recent), 2), "videos": len(recent)} if recent else None,
                       **self.performance.rate_medians([row["video_id"] for row in members], since, until)}
            for key, value in figures.items():
                if value and (name == "product" or value["videos"] >= MIN_PEERS):
                    reference[key] = {**value, "pool": name}
        return reference

    def _video_directions(self, product, scripts=None):
        """Each video's test direction: a direction assigned to the video itself in the catalog (卖点与方向 or the
        video's direction picker), else the script it was published from, else its case."""
        if scripts is None:
            scripts = [row for row in self.creation.read()["scripts"] if product_key(row.get("product")) == product]
        directions = {}
        for row in self.store.read_index()["cases"]:
            current = row.get("current") or {}
            found = str((current.get("metrics") or {}).get("video_id") or "")
            if product_key(row.get("product")) == product and found and (current.get("test_direction") or {}).get("direction_id"):
                directions.setdefault(found, current["test_direction"]["direction_id"])
        for row in scripts:  # The script a video was published from names its direction more directly than a case does.
            if row.get("video_id") and row.get("direction_id"):
                directions[str(row["video_id"])] = row["direction_id"]
        directions_store = getattr(self, "directions", None)
        for link in directions_store.read()["video_links"] if directions_store is not None else []:
            if product_key(link.get("product")) == product and link.get("direction_id"):
                directions[str(link["video_id"])] = link["direction_id"]
        return directions

    def review_summary(self, video, product, scripts=None):
        """What a review needs before any writing: this video's figures and where they sit among the same product's and
        the same direction's judged videos (observation-window views, the figure every video's status is judged on)."""
        directions = self._video_directions(product, scripts)
        judged = [row for row in self.performance.videos() if product_key(row.get("product")) == product
                  and row.get("status") in JUDGED and isinstance(row.get("window_views"), (int, float)) and math.isfinite(row["window_views"])]

        def place(rows):
            views = [row["window_views"] for row in rows]
            if not views:
                return None
            own = video.get("window_views")
            top = max(1, math.ceil(100 * sum(value >= own for value in views) / len(views))) if video.get("status") in JUDGED and own is not None else None
            return {"count": len(views), "median_window_views": statistics.median(views), "top_pct": top,
                    "median_ctr_pct": statistics.median(values) if (values := [row["ctr_pct"] for row in rows if row.get("ctr_pct") is not None]) else None}

        direction_id = directions.get(str(video.get("video_id")))
        return {"status": video.get("status"), "status_label": video.get("status_label"),
                "review_status": self.review_status_snapshot().for_video(video), "posted_on": video.get("posted_on"),
                "window_end": video.get("window_end"), "metrics": {key: video.get(key) for key in SUMMARY_METRICS},
                "product": place(judged), "direction_id": direction_id,
                "direction": place([row for row in judged if directions.get(str(row["video_id"])) == direction_id]) if direction_id else None}

    @staticmethod
    def _feedback_task(task, source, explicit, window=None, draft_task=None):
        return {"task_id": task["task_id"], "title": task.get("case_title") or "一起复盘", "status": task.get("status"),
                "created_at": task.get("created_at"), "source_review": copy.deepcopy(source),
                "window_known": bool(explicit or window), "explicit_review": bool(explicit),
                "review_draft": draft_task is not None, "draft": review_draft(draft_task) if draft_task else None}
