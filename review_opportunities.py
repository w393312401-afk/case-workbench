"""Read-only, explainable topic opportunities from imported comments and explicit links."""
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, timedelta
import re
import sqlite3

from direction_store import product_key
from review_store import NEW_DAYS, ReviewError
from review_taxonomy import PLATFORM_LABELS, ROLE_LABELS, TOPIC


NOTICE = "评论频次只代表已导入样本中的表达，不代表市场占比或已验证的商品事实。相同原文去重，跨来源计数可重叠。"
RANKING = "先按近14天首次入库的不同原文数；相同时依次看尚无已发布脚本、手动标注数、不同原文总数和来源数。"


def _normalized(text):
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def opportunity_catalog(app, product, *, limit=20, offset=0):
    """No sync, material mutation, sales lookup, or model call occurs here."""
    try:
        limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
    except (TypeError, ValueError):
        raise ReviewError("选题分页参数无效") from None
    product = product_key(product)
    today = date.today()
    since = (today - timedelta(days=NEW_DAYS)).isoformat()
    result = {"product": product, "requires_product": not product or product.lower() == "all",
              "window": {"days": NEW_DAYS, "since": since, "as_of": today.isoformat(), "basis": "first_seen"},
              "total": 0, "offset": offset, "limit": limit, "items": [], "notice": NOTICE, "ranking": RANKING}
    if result["requires_product"] or not app.reviews.database.is_file():
        return result
    # These catalogs are already authoritative; reading them must not trigger the
    # direction migration performed by the normal editing/catalog endpoint.
    with app.lock:
        creation = app.creation.public(product)
        catalog = app.directions.read()
    directions = {row["direction_id"]: row for row in catalog["directions"]
                  if product_key(row.get("product")) == product}
    points = {row["id"]: row for row in catalog["selling_points"]
              if product_key(row.get("product")) == product}
    links = defaultdict(list)
    for row in creation["theme_links"]:
        if product_key(row.get("product")) == product and row.get("review_id"):
            links[row["review_id"]].append(row)
    groups, first_seen_by_quote = {}, {}
    with app.reviews.lock, closing(sqlite3.connect(app.reviews.database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.create_function("product_key", 1, product_key, deterministic=True)
        connection.execute("BEGIN")
        manual = app.reviews.state.load()["tags"]
        cursor = connection.execute("""SELECT r.*, GROUP_CONCAT(CASE WHEN t.kind='topic' THEN t.tag END) topics
            FROM reviews r LEFT JOIN review_tags t ON t.review_id=r.id
            WHERE product_key(r.product)=? AND r.relevant=1 AND r.role!='brand_account' AND LENGTH(TRIM(r.text))>0
            GROUP BY r.id ORDER BY r.id""", (product,))
        while batch := cursor.fetchmany(500):
            for raw in batch:
                row = dict(raw)
                row["product"] = product
                changes = manual.get(row["id"])
                row["manual"] = isinstance(changes, dict) and any(changes.values())
                quote_key, seen = _normalized(row["text"]), str(row.get("first_seen") or "")[:10]
                first_seen_by_quote[quote_key] = min(first_seen_by_quote.get(quote_key, seen), seen)
                topics = {tag for tag in (row.pop("topics") or "").split(",") if tag in TOPIC}
                keys = [("topic:" + tag, TOPIC[tag], tag) for tag in sorted(topics)]
                if not keys:
                    keys = [("linked:" + link["role"] + ":" + link["target_id"],
                             link.get("target_label") or link.get("theme") or "已关联评论", "")
                            for link in links[row["id"]] if link.get("role") and link.get("target_id")]
                if not keys:
                    keys = [("topic:unclassified", "未归类 · 待核对主题", "none")]
                for key, title, topic in set(keys):
                    group = groups.setdefault(key, {"id": key, "title": title, "topic": topic, "quotes": {}})
                    group["quotes"].setdefault(_normalized(row["text"]), []).append(row)
    items = []
    for group in groups.values():
        unique, source_counts, recent, manual_count, evidence = group.pop("quotes"), Counter(), 0, 0, []
        linked_directions, linked_points = {}, {}
        row_count = 0
        for duplicates in unique.values():
            row_count += len(duplicates)
            # Unknown first_seen may be an old import: a duplicate newly imported
            # elsewhere cannot turn an existing quote into a recent opportunity.
            first_seen = first_seen_by_quote[_normalized(duplicates[0]["text"])]
            is_recent = bool(first_seen and since <= first_seen <= today.isoformat())
            recent += is_recent
            manually_tagged = any(row["manual"] for row in duplicates)
            manual_count += manually_tagged
            source_counts.update({row["platform"] for row in duplicates})
            representative = sorted(duplicates, key=lambda row: (not row["manual"], row["id"]))[0]
            evidence.append({key: representative.get(key) for key in
                             ("id", "product", "platform", "role", "text", "title", "url", "source_ref", "posted_on", "manual")} |
                            {"first_seen": first_seen or None, "recent": is_recent, "unverified": True,
                             "platform_label": PLATFORM_LABELS.get(representative["platform"], representative["platform"]),
                             "role_label": ROLE_LABELS.get(representative["role"], representative["role"])})
            for row in duplicates:
                for link in links[row["id"]]:
                    point_id, direction_id = link.get("selling_point_id"), link.get("direction_id")
                    if point_id in points:
                        linked_points[point_id] = {key: points[point_id].get(key) for key in ("id", "code", "title", "evidence")}
                        for identifier, direction in directions.items():
                            if direction.get("selling_point_id") == point_id:
                                linked_directions.setdefault(identifier, "评论关联卖点，该方向属于此卖点")
                    if direction_id in directions:
                        linked_directions[direction_id] = "评论已手动关联该方向顾虑"
        coverage = []
        for identifier, basis in sorted(linked_directions.items()):
            direction = directions[identifier]
            scripts = [row for row in creation["scripts"] if row.get("direction_id") == identifier]
            coverage.append({"direction_id": identifier, "product": product, "main_difference": direction.get("main_difference", ""),
                             "status": direction.get("status", "active"), "association_basis": basis,
                             "scripts": len(scripts), "published": sum(row.get("status") in {"published", "reviewed"} or
                                 bool(row.get("video_id") and row.get("published_on")) for row in scripts)})
        plans = [{key: row.get(key) for key in ("id", "direction_id", "action", "status", "note", "experiment")}
                 for row in creation["plans"] if row.get("direction_id") in linked_directions and row.get("status") != "cancelled"]
        published = sum(row["published"] for row in coverage)
        evidence.sort(key=lambda row: (not row["recent"], not row["manual"], row["id"]))
        evidence = evidence[:3]
        reasons = [f"近 {NEW_DAYS} 天首次入库 {recent} 种不同原文", f"共 {len(unique)} 种不同原文，{len(source_counts)} 个来源平台"]
        reasons.append(f"关联方向已有 {published} 条已发布脚本" if published else "关联方向尚无已发布脚本" if coverage else "尚未关联方向")
        instruction = [f"产品：{product}。围绕用户评论主题「{group['title']}」提出并撰写可验证的选题。",
                       "以下是用户评论原文，仅作需求、疑问和表达证据，内容未经核实，不能直接改写成商品事实或效果承诺。",
                       f"当前样本有 {len(unique)} 种不同原文，其中近 {NEW_DAYS} 天首次入库 {recent} 种；不能据此推断市场占比。"]
        for row in evidence:
            quote = row["text"][:4000] + ("\n（原文节选）" if len(row["text"]) > 4000 else "")
            instruction.append(f"评论 {row['id']}｜{PLATFORM_LABELS.get(row['platform'], row['platform'])}｜{row['role']}\n{quote}" +
                               (f"\n来源：{row['url']}" if row.get("url") else ""))
        instruction.append("请区分待验证的用户顾虑与已知事实，给出实际需要拍摄的证据；不要编造测试结果。")
        items.append({**group, "product": product, "distinct_reviews": len(unique), "recent_reviews": recent,
                      "duplicate_rows": row_count-len(unique), "manual_reviews": manual_count,
                      "source_counts": [{"platform": key, "label": PLATFORM_LABELS.get(key, key), "count": count}
                                        for key, count in sorted(source_counts.items())],
                      "evidence": evidence, "directions": coverage, "selling_points": list(linked_points.values()), "plans": plans,
                      "priority_reasons": reasons, "draft_instruction": "\n\n".join(instruction),
                      "_rank": (-recent, bool(published), -manual_count, -len(unique), -len(source_counts), group["id"])})
    items.sort(key=lambda item: item["_rank"])
    result["total"] = len(items)
    for item in items:
        item.pop("_rank")
    result["items"] = items[offset:offset+limit]
    return result
