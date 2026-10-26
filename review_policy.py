"""Review readiness is separate from the legacy report window and traffic bands."""
from datetime import date, datetime, timedelta
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def review_today():
    try:
        return datetime.now(ZoneInfo("Asia/Shanghai")).date()
    except ZoneInfoNotFoundError:
        # The installed Windows runtime may not ship the IANA timezone database.
        from datetime import timezone
        return datetime.now(timezone(timedelta(hours=8))).date()


def day(value):
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def report_window(posted_on):
    posted = day(posted_on)
    return (posted - timedelta(days=posted.weekday()) + timedelta(days=13)).isoformat() if posted else None


def review_context(video, today=None):
    today = day(today) if today is not None else review_today()
    posted = day(video.get("registered_on") or video.get("posted_on"))
    end = posted + timedelta(days=14) if posted else None
    age = (today - posted).days if posted else None
    observing = bool(end and today < end)
    complete = video.get("observation_coverage_complete") is True
    conflict = bool(video.get("registered_on") and video.get("posted_on") and video["registered_on"] != video["posted_on"])
    if conflict:
        complete = False
    data_through = video.get("last_data")
    if not posted:
        key, label, reason = "missing", "缺发布日期", "补齐实际发布日期后才能确定14天观察期。"
    elif age < 0:
        key, label, reason = "missing", "发布日期待核对", "登记日期晚于今天，请核对实际发布时间。"
    elif conflict:
        key, label, reason = "missing", "发布日期待核对", "登记发布日期与销售报表不一致，请先核对。"
    elif observing:
        key, label, reason = "observing", "持续观察", f"发布未满14天，{end.isoformat()}起复查；阶段表现不作为最终成败。"
    elif not complete:
        key, label, reason = "missing", "待补数据", "尚未取得这条视频首14天完整的播放和订单数据。"
    elif not isinstance(video.get("window_views"), (int, float)) or video["window_views"] < 500:
        key, label, reason = "insufficient", "样本较少", "已覆盖首14天，但曝光较少，不能据此判定方向失败。"
    else:
        key, label, reason = "preliminary", "初步证据", "首14天数据已覆盖；单条表现仍不能证明某种做法有效。"
    views = video.get("window_views")
    valid = lambda value: isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    traffic = ("报表窗播放较高" if views >= 10000 else "报表窗播放中等" if views >= 3000 else "报表窗播放较少") if valid(views) else "播放数据待补"
    # Reviewing early is allowed; the form only reminds what the conclusion cannot yet rest on.
    missing = video.get("observation_missing_days")
    if observing:
        warning = f"发布未满14天（{end.isoformat()} 满），现在的结论只是阶段判断，之后可再改。"
    elif posted and not complete and not conflict and isinstance(missing, list) and len(missing) == 3:
        warning = f"首14天缺 {missing[2]} 天数据（{missing[0]} 至 {missing[1]} 之间），结论按已有数据下，补齐后可再改。"
    elif key == "missing":
        warning = reason + "现在也可以先下结论，之后可再改。"
    else:
        warning = ""
    orders = video.get("orders")
    sales = "已有成交" if valid(orders) and orders > 0 else "覆盖期内暂无成交" if valid(orders) and orders == 0 else "成交数据待补"
    return {"observation": {"posted_on": posted.isoformat() if posted else None, "ends_on": end.isoformat() if end else None,
                            "age_days": age, "observing": observing, "data_through": data_through,
                            "coverage_complete": complete, "missing_days": video.get("observation_missing_days")},
            "evidence": {"key": key, "label": label, "reason": reason},
            "performance": {"traffic": traffic, "sales": sales},
            "closing_allowed": bool(posted and age >= 14 and complete), "early_warning": warning}
