#!/usr/bin/env python3
"""Read-only search over the workbench's real-user review library.

    python -X utf8 review_search.py search --query clamp --platform amazon --rating-max 3
    python -X utf8 review_search.py search --query 夹具 --nature defect          # Chinese words also search as English
    python -X utf8 review_search.py search --nature question --replied no          # TikTok questions nobody answered
    python -X utf8 review_search.py search --competitor lansky --topic price       # who is compared with it, and on what
    python -X utf8 review_search.py search --topic none                            # reviews the tag rules found nothing in
    python -X utf8 review_search.py search --video 7616568415343414542            # the comments under one TikTok video
    python -X utf8 review_search.py search --since 2026-10-01                      # reviews first seen on or after a day
    python -X utf8 review_search.py search --topic pivot --topic clamp --limit 10
    python -X utf8 review_search.py show <review id>
    python -X utf8 review_search.py brief          # what to read before writing or reviewing a script
    python -X utf8 review_search.py summary

The library is built and kept by the case workbench (page "用户评论库"); this script only reads
`<runtime>/reviews/reviews.sqlite` and never writes. It shares the filter logic of the workbench
(`workbench/review_store.py`), so the page and the command line always agree.

Everything printed is what reviewers said. Nothing in it is verified, reviewers are not
necessarily buyers, and Vine/trial reviews are marked. Do not present a quote as the
script speaker's own experience.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_WORKBENCH = Path(os.environ.get("CASE_WORKBENCH_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(DEFAULT_WORKBENCH))
from app_config import load_config
DEFAULT_RUNTIME = (Path(os.environ["AGENT_RUNTIME_ROOT"]) / "product-video-script-review"
                   if os.environ.get("AGENT_RUNTIME_ROOT") else Path(load_config(DEFAULT_WORKBENCH)["runtime_root"]))
NOTE = "评论者说的话均未核实，评论者不一定是买家；role 为 incentivized_buyer 的是试用/有激励评价；不要把引用写成说话者自己的亲历。"


class ReviewCliError(Exception):
    pass


def load_store(runtime):
    candidates = (DEFAULT_WORKBENCH, runtime / "workbench")
    workbench = next((folder for folder in candidates if (folder / "review_store.py").is_file()), None)
    if workbench is None:
        raise ReviewCliError(f"找不到评论库程序：{DEFAULT_WORKBENCH / 'review_store.py'}")
    sys.path.insert(0, str(workbench))
    import review_store  # noqa: E402
    return review_store


def open_readonly(database):
    if not database.is_file():
        raise ReviewCliError("评论库还没有数据：请先启动案例工作台，打开「用户评论库」页面（会自动入库），再重试。")
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def shape(item, labels):
    return {
        "id": item["id"], "platform": item["platform"], "platform_label": labels["platform"].get(item["platform"], item["platform"]),
        "role": item["role"], "role_label": labels["role"].get(item["role"], item["role"]), "rating": item["rating"], "date": item["posted_on"],
        "likes": item["likes"], "sku": item["sku"], "title": item["title"], "text": item["text"], "language": item["language"],
        "author_name": item.get("author_name"), "author_handle": item.get("author_handle"), "product": item.get("product"),
        "is_reply": bool(item["is_reply"]), "replies": item.get("replies"),
        "video_id": item["source_ref"] if item["platform"] == "tiktok_video" else None, "first_seen": item.get("first_seen"),
        "nature": [labels["nature"].get(t, t) for t in item["tags"]["nature"]], "topic": [labels["topic"].get(t, t) for t in item["tags"]["topic"]],
        "competitor": [labels["competitor"].get(t, t) for t in item["tags"].get("competitor", [])],
        "nature_codes": item["tags"]["nature"], "topic_codes": item["tags"]["topic"], "competitor_codes": item["tags"].get("competitor", []),
        "picked_note": item["pick"]["note"] if item.get("pick") else None, "context": item["context"], "url": item["url"],
    }


def run(args):
    runtime = Path(args.runtime).resolve() if args.runtime else DEFAULT_RUNTIME
    module = load_store(runtime)
    store = module.ReviewStore(runtime)
    labels = {"platform": module.PLATFORM_LABELS, "role": module.ROLE_LABELS, "nature": module.NATURE, "topic": module.TOPIC, "competitor": module.COMPETITOR}
    connection = open_readonly(store.database)
    try:
        if args.command == "summary":
            row = connection.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()
            by = {r[0]: r[1] for r in connection.execute("SELECT platform, COUNT(*) FROM reviews GROUP BY platform")}
            synced = connection.execute("SELECT value FROM meta WHERE key='synced_at'").fetchone()
            tags = {kind: {labels[kind].get(r[0], r[0]): r[1] for r in connection.execute(
                "SELECT t.tag, COUNT(*) FROM review_tags t JOIN reviews r ON r.id=t.review_id WHERE t.kind=? AND r.role!='brand_account' AND r.relevant=1 AND LENGTH(r.text)>0 GROUP BY t.tag ORDER BY 2 DESC", (kind,))} for kind in ("nature", "topic")}
            return {"total": row["n"], "by_platform": {labels["platform"].get(k, k): v for k, v in by.items()}, "synced_at": synced[0] if synced else None,
                    "coverage": store.coverage(), "tags": tags, "note": NOTE}
        if args.command == "brief":
            digest = store._digest(connection, args.max_chars)
            if digest is None:
                raise ReviewCliError("评论库里还没有可用的评论。")
            return {"digest": digest, "note": NOTE}
        if args.command == "show":
            row = connection.execute("SELECT * FROM reviews WHERE id=?", (args.id,)).fetchone()
            if not row:
                raise ReviewCliError(f"没有这条评论：{args.id}")
            item = store._hydrate(connection, [row])[0]
            top = row["parent_id"] or row["id"]
            thread = connection.execute("SELECT * FROM reviews WHERE (id=? OR parent_id=?) AND id!=? ORDER BY is_reply, posted_on", (top, top, row["id"])).fetchall()
            return {"review": shape(item, labels), "thread": [shape(t, labels) for t in store._hydrate(connection, thread)], "note": NOTE}
        query = {"q": args.query or "", "platform": args.platform, "role": args.role, "nature": args.nature, "topic": args.topic,
                 "rating_min": args.rating_min, "rating_max": args.rating_max, "language": args.language, "replied": args.replied,
                 "competitor": args.competitor, "video": args.video, "since": args.since,
                 "include_brand": args.include_brand, "include_irrelevant": args.include_irrelevant, "include_empty": args.include_empty}
        where, params = store._filters(query)
        sort = module.SORTS.get(args.sort)
        if not sort:
            raise ReviewCliError(f"排序方式无效：{args.sort}（可选：{', '.join(module.SORTS)}）")
        limit = max(1, min(args.limit, 100))
        total = connection.execute(f"SELECT COUNT(*) FROM reviews r WHERE {where}", params).fetchone()[0]
        rows = connection.execute(f"SELECT r.* FROM reviews r WHERE {where} ORDER BY {sort}, r.id LIMIT ?", params + [limit]).fetchall()
        return {"match_scope": "用户评论库（默认隐藏品牌账号回复、无关评论、只打星的评价）", "total": total, "shown": len(rows),
                "query": module.describe_query(args.query), "reviews": [shape(item, labels) for item in store._hydrate(connection, rows)], "note": NOTE}
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only search over the real-user review library")
    parser.add_argument("--runtime", help="runtime root (default: %%AGENT_RUNTIME_ROOT%%/product-video-script-review)")
    sub = parser.add_subparsers(dest="command", required=True)
    search = sub.add_parser("search", help="search reviews")
    search.add_argument("--query", "-q", help="words that must all appear (English or Chinese)")
    search.add_argument("--platform", action="append", default=[], choices=["tiktok_video", "tiktok_shop", "amazon", "reddit", "facebook"])
    search.add_argument("--role", action="append", default=[], choices=["verified_buyer", "incentivized_buyer", "unverified_buyer", "viewer", "peer", "brand_account"])
    search.add_argument("--nature", action="append", default=[], help="owner, defect, compare, objection, question, advice, result, buy_info, praise, noise; none = untagged")
    search.add_argument("--topic", action="append", default=[], help="e.g. clamp, pivot, angle, small_blade, stones, price, wrong_sku, mirror, repair; none = untagged")
    search.add_argument("--competitor", action="append", default=[], help="a competitor named in the review: worksharp, lansky, tormek, wicked_edge, kme, tsprof, hapstone, xarilk, edge_pro, horl, rolling, shapton, spyderco, dmt, jende, anystone")
    search.add_argument("--video", help="TikTok video id: only the comments under that video")
    search.add_argument("--since", help="YYYY-MM-DD: only reviews first seen on or after that day (the first sync's reviews have no day)")
    search.add_argument("--rating-min", type=int)
    search.add_argument("--rating-max", type=int)
    search.add_argument("--language", choices=["en", "other"])
    search.add_argument("--replied", choices=["yes", "no"], help="only TikTok video comments record replies: no = nobody answered, yes = has replies")
    search.add_argument("--sort", default="likes")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--include-brand", action="store_true")
    search.add_argument("--include-irrelevant", action="store_true")
    search.add_argument("--include-empty", action="store_true")
    show = sub.add_parser("show", help="one review with its conversation")
    show.add_argument("id")
    brief = sub.add_parser("brief", help="a short reading of the library for writing or reviewing a script: viewer doubts, buyer limits, unanswered questions")
    brief.add_argument("--max-chars", type=int, default=9000, help="soft size limit of the digest (default 9000)")
    sub.add_parser("summary", help="counts, coverage notes and tag totals")
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except (ReviewCliError, ValueError) as error:
        print(f"review_search: {error}", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    raise SystemExit(main())
