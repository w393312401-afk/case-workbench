"""Read-only HTTP diagnostics. Never print query strings, bodies or credentials."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re

REQUEST = re.compile(r'\[([^\]]+)\] "(GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS) ([^\s]+) HTTP/[^\"]+" (\d{3})')


def summarize(path, *, since=None, until=None, status="error", contains="", limit=20):
    """Only count HTTP facts; a 403/409 is not automatically harmless or a server failure."""
    statuses = None if status == "all" else {int(s) for s in status.split(",")} if status != "error" else None
    totals, grouped = Counter(), {}
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            match = REQUEST.search(line)
            if not match:
                continue
            text, method, target, code = match.groups()
            try:
                when = datetime.strptime(text, "%d/%b/%Y %H:%M:%S")
            except ValueError:
                continue
            if since and when < since or until and when > until:
                continue
            # Never retain or output query parameters (even when filtering).
            route = target.split("?", 1)[0].split("#", 1)[0]
            if contains and contains not in route:
                continue
            code = int(code)
            totals[str(code)] += 1
            if status == "error" and code < 400 or statuses is not None and code not in statuses:
                continue
            key = (method, route, code)
            entry = grouped.setdefault(key, {"method": method, "path": route, "status": code,
                                           "count": 0, "first": text, "last": text})
            entry["count"] += 1
            entry["last"] = text
    rows = sorted(grouped.values(), key=lambda row: (-row["count"], row["path"], row["status"]))
    return {"status_counts": dict(sorted(totals.items())), "matched_requests": sum(row["count"] for row in rows),
            "matched_groups": len(rows), "groups": rows[:limit], "limit": limit,
            "note": "仅汇总HTTP状态与路径；403需核对授权，409需核对冲突或业务保护。原始日志保留，参数与正文不输出。"}


def local_time(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            raise ValueError()
        return parsed
    except ValueError as exc:
        raise argparse.ArgumentTypeError("使用本机日志时间：YYYY-MM-DD HH:MM:SS") from exc


def main():
    parser = argparse.ArgumentParser(description="只读筛选工作台HTTP日志，默认仅显示4xx/5xx。")
    parser.add_argument("--file", type=Path, default=Path(__file__).resolve().parents[1] / "server.log")
    parser.add_argument("--since", type=local_time)
    parser.add_argument("--until", type=local_time)
    parser.add_argument("--status", default="error", help="error、all 或逗号分隔的状态码，例如403,409")
    parser.add_argument("--path", default="", help="只匹配路径，不读取查询参数")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100 or args.since and args.until and args.since > args.until:
        parser.error("数量须为1–100，起始时间不能晚于结束时间。")
    if args.status not in {"error", "all"} and not re.fullmatch(r"[1-5]\d\d(?:,[1-5]\d\d)*", args.status):
        parser.error("状态须为error、all或有效HTTP状态码。")
    try:
        result = summarize(args.file, since=args.since, until=args.until, status=args.status, contains=args.path, limit=args.limit)
    except OSError as exc:
        parser.exit(1, f"日志无法读取：{exc}\n")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("状态总数：" + " · ".join(f"{code}×{n}" for code, n in result["status_counts"].items()))
        print(f"筛选后 {result['matched_requests']} 次、{result['matched_groups']} 组，显示前 {args.limit} 组")
        for row in result["groups"]:
            print(f"{row['status']}  {row['count']:>5}次  {row['method']} {row['path']}  {row['first']} → {row['last']}")
        print(result["note"])


if __name__ == "__main__":
    main()
