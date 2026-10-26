"""Read-only comparison of two recorded videos in an explicit common calendar window."""
from datetime import date, timedelta
import math

from direction_store import product_key


METRICS = {"views": ("views",), "clicks": ("clicks",), "orders": ("orders",), "gmv": ("gmv",),
           "ctr_pct": ("clicks", "views"), "ctor_pct": ("orders", "clicks"), "gpm": ("gmv", "views")}


def _video_window(video, start, end, metric):
    result = {"video_id": video["video_id"], "title": video.get("headline") or video.get("title") or video["video_id"],
              "account": video.get("account") or "", "value": None, "totals": {}, "missing_dates": [], "issues": []}
    coverage, needed = set(), METRICS[metric]
    totals = {key: 0 for key in needed}
    for row in video.get("series") or []:
        try:
            first, last = date.fromisoformat(row["start"]), date.fromisoformat(row["end"])
        except (KeyError, ValueError, TypeError):
            result["issues"].append("存在日期无效的销售记录")
            continue
        if last < first:
            result["issues"].append("存在起止日期倒置的销售记录")
            continue
        if last < start or first > end:
            continue
        if first < start or last > end:
            result["issues"].append("销售记录跨越比较边界，请补日数据或调整到完整报表周期")
            continue
        dates = {first + timedelta(days=i) for i in range((last - first).days + 1)}
        if coverage & dates:
            result["issues"].append("比较窗口存在重叠的销售记录")
            continue
        coverage.update(dates)
        for key in needed:
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                result["issues"].append(f"{row['start']} 至 {row['end']} 缺少有效的 {key} 数据")
            else:
                totals[key] += value
    expected = {start + timedelta(days=i) for i in range((end - start).days + 1)}
    result["missing_dates"] = [day.isoformat() for day in sorted(expected - coverage)]
    if result["missing_dates"]:
        result["issues"].append(f"窗口内缺少 {len(result['missing_dates'])} 天的本视频数据")
    result["totals"] = totals
    if not result["issues"]:
        if len(needed) == 1:
            result["value"] = totals[needed[0]]
        elif not totals[needed[1]]:
            result["issues"].append("分母为零，不能计算比率")
        else:
            result["value"] = totals[needed[0]] / totals[needed[1]] * (1000 if metric == "gpm" else 100)
    result["issues"] = list(dict.fromkeys(result["issues"]))
    return result


def compare_experiment(plan, read_video):
    """read_video is PerformanceStore.video; no totals are taken from all-time summaries."""
    card = plan.get("experiment") or {}
    result = {"plan_id": plan.get("id"), "status": "pending", "metric": card.get("metric", "ctor_pct"),
              "window_start": card.get("window_start", ""), "window_end": card.get("window_end", ""),
              "baseline": None, "test": None, "delta": None, "relative_change_pct": None,
              "issues": [], "note": "按相同日历窗口比较；结果仅描述表现差异，不自动判胜或证明改动的因果效果。"}
    if not all(card.get(key) for key in ("baseline_video_id", "test_video_id", "window_start", "window_end")):
        result["issues"] = ["请补齐基准视频、测试视频和共同比较日期"]
        return result
    if result["metric"] not in METRICS:
        result["issues"] = ["测试指标无效"]
        return result
    try:
        start, end = date.fromisoformat(card["window_start"]), date.fromisoformat(card["window_end"])
    except (ValueError, TypeError):
        result["issues"] = ["比较日期无效"]
        return result
    if end < start or (end - start).days > 366:
        result["issues"] = ["比较窗口无效"]
        return result
    if card["baseline_video_id"] == card["test_video_id"]:
        result["issues"] = ["基准视频与测试视频须为不同视频"]
        return result
    videos = [read_video(card[key]) for key in ("baseline_video_id", "test_video_id")]
    if any(video is None for video in videos):
        result["issues"] = ["视频尚未匹配到销售数据，请导入对应数据后再比较"]
        return result
    if any(product_key(video.get("product")) != product_key(plan.get("product")) for video in videos):
        result["issues"] = ["两条视频须与测试卡属于同一产品"]
    elif not all(video.get("account") for video in videos) or videos[0]["account"] != videos[1]["account"]:
        result["issues"] = ["两条视频须具有相同且明确的账号，避免账号差异混入测试结果"]
    if result["issues"]:
        result["status"] = "incompatible"
        return result
    result["baseline"], result["test"] = [_video_window(video, start, end, result["metric"]) for video in videos]
    result["issues"] = [f"{label}：{issue}" for label, side in (("基准", result["baseline"]), ("测试", result["test"])) for issue in side["issues"]]
    if result["issues"]:
        result["status"] = "incomplete"
        return result
    baseline, test = result["baseline"]["value"], result["test"]["value"]
    result.update(status="ready", delta=round(test - baseline, 6),
                  relative_change_pct=round((test / baseline - 1) * 100, 6) if baseline else None)
    return result
