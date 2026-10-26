"""How much one product moved in a campaign's weeks, from the per-video weekly figures.

This mirrors weeklyImpact in static/campaigns.js, which the data board uses; tests/test_campaigns.py runs both
on the same figures so the board and the AI's retrospective brief never disagree.
"""
from __future__ import annotations

from datetime import date, timedelta

from direction_store import product_key

MIN_CAMPAIGN_DAYS = 4   # days of a Monday-start week that must lie inside the campaign
MAX_BASELINE_WEEKS = 4
LOOKBACK_WEEKS = 6
MIN_BASELINE_WEEKS = 2


def _day(value):
    return date.fromisoformat(value)


def week_overlap(week, other):
    """Days of the week beginning Monday `week` that fall inside another campaign's [start, end]."""
    first, last = _day(week), _day(week) + timedelta(days=6)
    start, end = max(first, _day(other["start"])), min(last, _day(other["end"]))
    return 0 if end < start else (end - start).days + 1


def weekly_impact(row, campaigns, totals, complete):
    """The product's weeks mostly inside `row` against the nearest earlier weeks with no campaign day at all.

    totals: {Monday: {"gmv", "orders", "items"}} for the product; complete: Mondays whose seven days are all in the exports.
    Returns {"gmv": [inside, before], "orders": [...], "weeks": n, "baseline": m}, or {"missing": "weeks" | "baseline", ...}.
    """
    inside = sorted(week for week in complete if week_overlap(week, row) >= MIN_CAMPAIGN_DAYS)
    if not inside:
        return {"missing": "weeks"}
    baseline = []
    week = _day(inside[0]) - timedelta(days=7)
    for _ in range(LOOKBACK_WEEKS):
        if len(baseline) >= MAX_BASELINE_WEEKS:
            break
        key = week.isoformat()
        if key in complete and not any(week_overlap(key, other) > 0 for other in campaigns):
            baseline.append(key)
        week -= timedelta(days=7)
    if len(baseline) < MIN_BASELINE_WEEKS:
        return {"missing": "baseline", "weeks": len(inside), "baseline": len(baseline)}
    zero = {"gmv": 0, "orders": 0, "items": 0}

    def mean(weeks, field):
        return sum(totals.get(item, zero)[field] for item in weeks) / len(weeks)

    return {"gmv": [mean(inside, "gmv"), mean(baseline, "gmv")], "orders": [mean(inside, "orders"), mean(baseline, "orders")],
            "weeks": len(inside), "baseline": len(baseline)}


def ratio(pair):
    return round(pair[0] / pair[1], 2) if pair[1] > 0 else None


def product_weekly(analytics, videos, weeks, product=None):
    """(totals, complete) from one shop's analytics: the per-video weekly rows of the wanted product (all when None)."""
    wanted = product_key(product) if product else None
    ids = {str(video["video_id"]) for video in videos if wanted is None or product_key(video.get("product")) == wanted}
    totals = {}
    for index, week, _views, _impressions, _clicks, orders, items, gmv in analytics["weekly"]:
        if str(analytics["videos"][index][0]) not in ids:
            continue
        entry = totals.setdefault(week, {"gmv": 0, "orders": 0, "items": 0})
        entry["orders"] += orders or 0
        entry["items"] += items or 0
        entry["gmv"] += gmv or 0
    return totals, {entry["week"] for entry in weeks if entry.get("complete")}
