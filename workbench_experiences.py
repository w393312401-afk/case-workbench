"""Source-checked review lessons, searchable and frozen into later writing tasks."""
from __future__ import annotations

import copy

from creation_store import CreationError
from direction_store import now, product_key


class ReviewExperiencesWorkbenchMixin:
    def _experience_source_states(self):
        entries = self.review_followups.read()["entries"]
        return {(product_key(row.get("product")), str(row.get("video_id") or ""), row.get("window_end")): row for row in entries}

    @staticmethod
    def _experience_source_status(item, entries):
        ref = (item.get("source") or {}).get("review") or {}
        row = entries.get((product_key(ref.get("product")), str(ref.get("video_id") or ""), ref.get("window_end")))
        if not row:
            return "missing"
        return "current" if (row.get("state") == "reviewed" and row.get("at") == ref.get("reviewed_at") and
                             row.get("note") == ref.get("note")) else "changed"

    @staticmethod
    def _experience_reference_value(item, entries):
        ref = (item.get("source") or {}).get("review") or {}
        row = entries.get((product_key(ref.get("product")), str(ref.get("video_id") or ""), ref.get("window_end")))
        return (row or {}).get("reference_value", "")

    def review_experience_catalog(self, query):
        allowed = {"product", "q", "kind", "status", "direction_id"}
        if set(query) - allowed or any(len(value) != 1 for value in query.values()):
            raise CreationError(400, "经验检索条件无效。")
        with self.lock:
            result = self.review_experiences.public(product=query.get("product", [None])[0], query=query.get("q", [""])[0],
                kind=query.get("kind", [""])[0], status=query.get("status", ["active"])[0],
                direction_id=query.get("direction_id", [""])[0])
            sources = self._experience_source_states()
            for item in result["items"]:
                state = self._experience_source_status(item, sources)
                item["source_status"] = state
                item["source_status_label"] = "来源复盘一致" if state == "current" else "来源待核对，暂停自动引用"
                item["reference_value"] = self._experience_reference_value(item, sources)
            return result

    def review_experiences_for_task(self, product, direction_id="", limit=8):
        with self.lock:
            sources = self._experience_source_states()
            return self.review_experiences.selected_for_task(product, direction_id, limit,
                source_filter=lambda item: self._experience_source_status(item, sources) == "current",
                reference_value_for_item=lambda item: self._experience_reference_value(item, sources))

    def _freeze_experience_source(self, reference, product):
        ref = self.review_followups.validate_source(reference, product)
        row = self._experience_source_states()[(product_key(ref["product"]), ref["video_id"], ref["window_end"])]
        # A historical review may have no original sales snapshot. Do not replace
        # its evidence with today's different report window without saying so.
        current = self.performance.video(ref["video_id"])
        same_window = bool(current and product_key(current.get("product")) == product_key(product) and
                           current.get("window_end") == ref["window_end"])
        video = {key: copy.deepcopy(current.get(key)) for key in ("video_id", "title", "headline", "account", "posted_on", "last_data", "window_end")} if same_window else {"video_id": ref["video_id"]}
        video.update(captured_at=now(), data_status="captured_when_saved" if same_window else "historical_data_unavailable",
            metrics={key: copy.deepcopy(current.get(key)) for key in
                     ("window_views", "views", "views_source", "views_shop", "clicks", "orders", "gmv", "ctr_pct", "ctor_pct")}
                    if same_window else {})
        publications = {}
        for job_id, entry in self._video_publications().read()["jobs"].items():
            for receipt in [*entry.get("history", []), entry.get("publication")]:
                if not receipt or str(receipt.get("post_id")) != ref["video_id"]:
                    continue
                script = (receipt.get("source") or {}).get("script") or {}
                if product_key(script.get("product")) != product_key(product):
                    continue
                # Re-registering the same post corrects its receipt. Freeze the
                # latest registration, not both the mistake and its correction.
                identity = (job_id, ref["video_id"])
                publications[identity] = {key: copy.deepcopy(receipt.get(key)) for key in
                    ("id", "post_id", "job_id", "script_id", "script_version", "published_on", "account", "source_signature")}
                publications[identity]["script"] = {key: copy.deepcopy(script.get(key)) for key in
                    ("title", "direction_id", "variant_label", "text_sha256", "script_version")}
        return {"review": {**copy.deepcopy(ref), **{key: copy.deepcopy(row.get(key, "")) for key in
                    ("decision", "outcome", "evidence", "reference_value")}}, "video": video, "publications": list(publications.values())}

    def save_review_experience(self, body):
        if not isinstance(body, dict) or not isinstance(body.get("experience"), dict):
            raise CreationError(400, "请提供要保存的复盘经验。")
        with self.lock:
            prior = self.review_experiences.replay("experience", body)
            if prior:
                return prior
            value = body["experience"]
            old = None
            if value.get("id"):
                old = next((item for item in self.review_experiences.public(status="all")["items"] if item["id"] == value["id"]), None)
                if old is None:
                    raise CreationError(404, "这条经验不存在，请重新读取。")
            product = product_key(value.get("product") or (old or {}).get("product"))
            scope = value.get("scope", (old or {}).get("scope"))
            direction = value.get("direction_id", (old or {}).get("direction_id", ""))
            if scope == "direction":
                self._creation_direction(product, direction, allow_archived=True)
            source = None if old else self._freeze_experience_source(body.get("source_review"), product)
            return self.review_experiences.save(body, source=source)
