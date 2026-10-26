"""Read-only current performance from imported sales tables, matched by exact video ID.

CaseStore metrics/evidence remain historical records. This module never writes them.
"""
from __future__ import annotations

import copy
import math
from pathlib import Path
import sqlite3


SOURCE_LABEL = "销售数据表"
DISPLAY_FIELDS = {
    "views": "views_display", "impressions": "product_impressions_display",
    "clicks": "product_clicks_display",
}
PERFORMANCE_FIELDS = (*DISPLAY_FIELDS.values(), "items_sold", "ctor_display")


def _identity(case):
    metrics = (case.get("current") or {}).get("metrics") or {}
    value = metrics.get("video_id")
    return value.strip() if isinstance(value, str) else ""


def unavailable_data(video_id="", *, status="unavailable", reason=None):
    reasons = {
        "missing_video_id": "案例未绑定视频 ID，无法定位销售数据表中的对应视频。",
        "not_found": "销售数据表中未找到此视频 ID；没有使用旧截图数字补齐。",
        "unavailable": "销售数据表暂不可读取；没有使用旧截图数字补齐。",
    }
    return {"status": status, "source_label": SOURCE_LABEL,
            "metrics": {"video_id": video_id} if video_id else {},
            "evidence": {"source": SOURCE_LABEL, "status": "unverified",
                         "context": reason or reasons[status]}, "video": None}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return format(value, ".15g")


def _matched(video_id, raw):
    video = copy.deepcopy(raw)
    video["video_id"] = video_id
    metrics = {"video_id": video_id}
    account, title = video.get("account"), video.get("title")
    if isinstance(account, str) and account.strip():
        metrics["creator_handle"] = "@" + account.strip().lstrip("@")
    if isinstance(title, str) and title.strip():
        metrics["published_title"] = title.strip()
    for source, field in DISPLAY_FIELDS.items():
        value = _number(video.get(source))
        if value is not None:
            metrics[field] = value
    items = video.get("items")
    if _number(items) is not None and items >= 0 and items == int(items):
        metrics["items_sold"] = int(items)
    # CTOR is orders / product clicks. Items can differ from orders.
    orders, clicks = video.get("orders"), video.get("clicks")
    metric_coverage = video.get("metric_coverage") or {}
    complete_rate = all(field not in metric_coverage or metric_coverage[field].get("provided_rows") == metric_coverage[field].get("total_rows") for field in ("orders", "clicks"))
    if complete_rate and _number(orders) is not None and _number(clicks) is not None and clicks > 0:
        metrics["ctor_display"] = f"{round(orders * 100 / clicks, 2):g}%"
    first, last = video.get("first_data"), video.get("last_data")
    coverage = video.get("coverage") or {}
    window = f"已导入覆盖：{first} 至 {last}" if first and last else "已导入覆盖区间未记录"
    if coverage.get("covered_days") is not None:
        window += f"（{coverage['covered_days']} 天）"
    context = ["数值为此视频已导入日表/周表覆盖内的累计，月表仅作参考；未覆盖的时段未补零，不能称为发布以来完整累计。"]
    gaps, missing = coverage.get("gaps") or [], coverage.get("missing_since_posting")
    if gaps:
        context.append("覆盖内缺少日期：" + "、".join(gaps) + "。")
    if missing:
        context.append("从登记发布日期至此视频数据截至日缺少 " + str(len(missing)) + " 天：" + "、".join(missing) + "。")
    elif missing is None:
        context.append("发布日期或覆盖完整性未核定。")
    incomplete = [field for field, counts in (video.get("metric_coverage") or {}).items()
                  if counts.get("provided_rows", 0) < counts.get("total_rows", 0)]
    if incomplete:
        context.append("部分行缺少指标（只累计已提供值）：" + "、".join(incomplete) + "。")
    if video.get("excluded_overlapping_periods"):
        context.append("有重叠周表：保留较新的完整区间，未把较旧重叠区间重复累计或拆分估算。")
    files = video.get("source_files") or []
    source = SOURCE_LABEL + ("：" + "；".join(Path(path).name for path in files) if files else "")
    evidence = {"source": source, "window": window, "status": "source_checked", "context": " ".join(context)}
    if last:
        evidence["as_of"] = last
    return {"status": "matched", "source_label": SOURCE_LABEL, "metrics": metrics,
            "evidence": evidence, "video": video}


def sales_direction_summary(summary, data_by_case):
    """Replace current observations and rebuild coverage counts/cohorts, without ranking directions."""
    result = copy.deepcopy(summary)
    result["source_label"] = SOURCE_LABEL
    result["metric_source"] = "sales_data"
    result["limitations"] = list(result.get("limitations") or []) + [
        "当前表现只读取按视频 ID 匹配的销售数据表；缺失时不使用历史截图指标。",
        "每条观察保留自身已导入覆盖和截至日；覆盖累计不等于发布以来完整累计。",
    ]
    for group in result.get("groups", []):
        observations = group.get("observations", [])
        cohorts = {}
        missing = {"metrics": 0, "evidence_window": 0, "script_version": 0, "video_id": 0}
        availability = {field: 0 for field in PERFORMANCE_FIELDS}
        statuses = {}
        for item in observations:
            data = copy.deepcopy(data_by_case.get(item["case_id"]) or unavailable_data())
            item["sales_data"] = data
            item["metrics"], item["evidence"] = data["metrics"], data["evidence"]
            metrics, evidence = item["metrics"], item["evidence"]
            statuses[data["status"]] = statuses.get(data["status"], 0) + 1
            present = [field for field in PERFORMANCE_FIELDS if field in metrics]
            missing["metrics"] += not present
            missing["evidence_window"] += not bool(evidence.get("window"))
            missing["script_version"] += not bool(item.get("script_version"))
            missing["video_id"] += not bool(metrics.get("video_id"))
            for field in present:
                availability[field] += 1
            values = {"evidence_window": evidence.get("window"), "evidence_status": evidence.get("status"),
                      "data_as_of": evidence.get("as_of"), "script_version": item.get("script_version"),
                      "variant_type": (item.get("test_direction") or {}).get("variant_type"),
                      "version_conflict": bool(item.get("version_conflict")), "sales_status": data["status"]}
            key = tuple(values.values())
            cohort = cohorts.setdefault(key, {**values, "case_ids": [], "case_count": 0})
            cohort["case_ids"].append(item["case_id"])
            cohort["case_count"] += 1
        group.update(missing_counts=missing, metric_availability=availability,
                     sales_status_counts=statuses, cohorts=list(cohorts.values()))
    return result


class SalesDataResolver:
    def __init__(self, performance):
        self.performance = performance

    def resolve_cases(self, cases):
        cases = list(cases)
        ids = {video_id for case in cases if (video_id := _identity(case))}
        available = self.performance is not None
        if available and hasattr(self.performance, "database"):
            available = self.performance.database.is_file()
        rows = {}
        if ids and available:
            try:
                rows = self.performance.video_brief(ids)
            except (sqlite3.Error, OSError, ValueError):
                available = False
        # Public-page observations are useful even for reference cases that have
        # no sales export. Keep them separate so they never imply sales coverage.
        public = {video_id: row["public_stats"] for video_id, row in rows.items()
                  if isinstance(row.get("public_stats"), dict)}
        missing = ids - rows.keys()
        public_store = getattr(self.performance, "public", None)
        if missing and public_store is not None:
            try:
                public.update(public_store.latest(missing))
            except (sqlite3.Error, OSError, ValueError):
                pass
        result = {}
        for case in cases:
            video_id = _identity(case)
            if not video_id:
                data = unavailable_data(status="missing_video_id")
            elif not available:
                data = unavailable_data(video_id)
            elif video_id not in rows:
                data = unavailable_data(video_id, status="not_found")
            else:
                data = _matched(video_id, rows[video_id])
            if video_id in public:
                snapshot = public[video_id]
                data["public_stats"] = {field: snapshot.get(field) for field in
                    ("views", "likes", "comments", "shares", "saves", "observed_at", "last_seen_at")}
            result[case["case_id"]] = data
        return result

    def resolve_case(self, case):
        return self.resolve_cases([case])[case["case_id"]]

    def direction_summary(self, summary):
        cases = [{"case_id": item["case_id"], "current": {"metrics": item.get("metrics") or {}}}
                 for group in summary.get("groups", []) for item in group.get("observations", [])]
        return sales_direction_summary(summary, self.resolve_cases(cases))


def prompt_sales_projection(snapshot):
    """Use only frozen table data as current performance. Legacy tasks never read a live database."""
    result = copy.deepcopy(snapshot)
    current = result.get("current") or {}
    data = result.get("sales_data")
    if not isinstance(data, dict):
        data = unavailable_data(_identity({"current": current}), reason="此历史任务没有冻结销售数据表快照，当前表现不可用；旧指标仅作历史记录。")
    result["sales_data"] = data
    if result.get("current") is not None:
        current["metrics"] = copy.deepcopy(data["metrics"])
        current["evidence"] = copy.deepcopy(data["evidence"])
        (current.get("assets") or {}).pop("screenshot", None)
    (result.get("assets") or {}).pop("screenshot", None)
    result["performance_history_boundary"] = "原始任务保留旧案例指标、证据与原评价供追溯；本提示 current 的表现来自冻结 sales_data。来源正文、original_reference 与原评价里的旧数字不作为当前表现。"
    summary = result.get("direction_summary")
    if isinstance(summary, dict) and isinstance(summary.get("data"), dict):
        observations = [item for group in summary["data"].get("groups", []) for item in group.get("observations", [])]
        frozen = {item["case_id"]: item.get("sales_data") or unavailable_data(
            (item.get("metrics") or {}).get("video_id", ""), reason="此历史任务的方向观察没有冻结销售数据表数据，当前表现不可用。") for item in observations}
        summary["data"] = sales_direction_summary(summary["data"], frozen)
    return result
