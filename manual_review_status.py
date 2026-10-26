"""Safe live display labels from human reviews, independent of traffic bands."""
from datetime import datetime, timedelta, timezone

from direction_store import product_key
from review_policy import day, review_context, review_today


LABELS = {"pending": "待复盘", "unclassified": "待分类", "none": "无参考价值",
          "weak": "弱参考价值", "medium": "中等参考价值", "high": "高参考价值"}


def identity(value):
    return (product_key(value.get("product")).casefold(), str(value.get("video_id") or ""), value.get("window_end"))


def reviewed_day(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.astimezone(timezone(timedelta(hours=8))).date() if stamp.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


class ManualReviewStatus:
    """One coherent catalog snapshot for a response; it exposes no review text."""

    def __init__(self, entries, scripts=(), today=None, publications=()):
        self.entries = {identity(row): row for row in entries}
        self.today = day(today) if today is not None else review_today()
        self.registered = {}
        for script in scripts:
            self._register(script.get("product"), script.get("video_id"), script.get("published_on"))
        for job in publications:
            for receipt in [*job.get("history", []), job.get("publication")]:
                if receipt:
                    script = (receipt.get("source") or {}).get("script") or {}
                    self._register(script.get("product"), receipt.get("post_id"), receipt.get("published_on"))
        self.legacy = {(product_key(row.get("product")).casefold(), str(row.get("video_id") or ""), event.get("published_on"))
                       for row in scripts if row.get("status") == "reviewed" and row.get("video_id")
                       for event in row.get("history", [])
                       if event.get("status") == "reviewed" and event.get("published_on")}

    def _register(self, product, video_id, published_on):
        if product and video_id and published_on:
            self.registered[(product_key(product).casefold(), str(video_id))] = published_on

    def for_video(self, video, *, allow_legacy=True):
        row = self.entries.get(identity(video))
        registered = video.get("registered_on") or self.registered.get(identity(video)[:2])
        observation_video = {**video, "registered_on": registered} if registered else video
        observation = review_context(observation_video, self.today)["observation"]
        age, ends = observation.get("age_days"), day(observation.get("ends_on"))
        young = isinstance(age, (int, float)) and 0 <= age < 14
        key = "pending"
        if row and row.get("state") == "reviewed":
            reviewed = reviewed_day(row.get("at"))
            stage = young or (isinstance(age, (int, float)) and age >= 14 and reviewed and ends and reviewed < ends)
            if not stage:
                value = row.get("reference_value")
                key = value if value in {"none", "weak", "medium", "high"} else "unclassified"
        elif not row and allow_legacy and not young:
            legacy_key = (product_key(video.get("product")).casefold(), str(video.get("video_id") or ""), video.get("posted_on"))
            if legacy_key in self.legacy:
                key = "unclassified"
        return {"key": key, "label": LABELS[key]}
