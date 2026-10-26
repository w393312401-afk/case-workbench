"""Read-only execution progress backed by explicit, unambiguous variant provenance.

Candidate indexes are indexes in generated prose, never catalog variant indexes.
Manual direction/script/plan statuses do not constitute production or publication.
"""
from __future__ import annotations

import copy

from direction_store import fingerprint, product_key
from experiment_cards import compare_experiment
from production_store import job_status
from elevenlabs_client import VoiceError


SNAPSHOT_KEYS = ("progression", "baseline", "variant_type", "main_difference", "concern", "closing", "execution_change")
IDENTITY_KEYS = tuple(key for key in SNAPSHOT_KEYS if key not in {"baseline", "progression"})
RECORD_KINDS = ("tasks", "scripts", "productions", "publications", "plans", "videos")


def variant_snapshot(variant):
    return {key: copy.deepcopy(variant[key]) for key in SNAPSHOT_KEYS if key in variant}


def variant_key(variant):
    # Structure/baseline annotations can be corrected independently of the chain.
    return fingerprint({key: variant.get(key, "") for key in IDENTITY_KEYS})


def build_direction_progress(direction, *, catalog_revision, tasks=(), scripts=(), plans=(),
                             production=None, video_publications=None, videos=(), read_video=None,
                             variant_links=()):
    product, identifier = product_key(direction.get("product")), direction["direction_id"]
    variants = direction.get("variants") or []
    result = {"product": product, "direction_id": identifier, "catalog_revision": catalog_revision,
              "variants": [{"index": index, "key": variant_key(variant), "snapshot": variant_snapshot(variant),
                            "records": {kind: [] for kind in RECORD_KINDS}}
                           for index, variant in enumerate(variants)],
              "unassigned": {kind: [] for kind in RECORD_KINDS}}
    links, link_counts = {}, {}
    for row in variant_links:
        key = (row.get("kind"), row.get("record_id"))
        link_counts[key] = link_counts.get(key, 0) + 1
    for row in variant_links:
        if product_key(row.get("product")) != product or row.get("direction_id") != identifier:
            continue
        key = (row.get("kind"), row.get("record_id"))
        # The store enforces uniqueness. If imported data violates it, do not
        # let list order decide which variant receives the historical work.
        links[key] = row if link_counts[key] == 1 else {**row, "variant_ref": {}}
    by_key = {}
    for index, variant in enumerate(variants):
        if not variant.get("conflict"):
            by_key.setdefault(variant_key(variant), []).append(index)

    def match(snapshot):
        if not isinstance(snapshot, dict) or not all(key in snapshot for key in ("main_difference", "concern", "closing")):
            return None
        matched = by_key.get(variant_key(snapshot), [])
        return matched[0] if len(matched) == 1 else None

    def scoped(row, kind=None):
        return product_key(row.get("product")) == product and (row.get("direction_id") == identifier or
            not row.get("direction_id") and (kind, row.get("id")) in links)

    def linked_owners(link):
        index = ref_match(link.get("variant_ref"))
        return {index} if index is not None else set()

    def association(link, inherited_from=None):
        if not link:
            return None
        return {"id": link["id"], "evidence": copy.deepcopy(link.get("evidence") or {}),
                "created_at": link.get("created_at", ""), "inherited_from": inherited_from}

    def selected(plan):
        if not isinstance(plan, dict):
            return set()
        return {index for snapshot in plan.get("variants") or [] if (index := match(snapshot)) is not None}

    def ref_match(ref):
        return match(ref.get("snapshot")) if isinstance(ref, dict) else None

    def append(kind, record, indexes):
        if indexes:
            for index in sorted(indexes):
                result["variants"][index]["records"][kind].append(copy.deepcopy(record))
        else:
            result["unassigned"][kind].append({**record, "reason": "缺少可唯一核对的变体来源"})

    task_owners, script_owners, video_owners, production_owners = {}, {}, {}, {}
    production_rows = {}
    for task in tasks:
        context = task.get("context") or {}
        if product_key(task.get("product") or context.get("product")) != product or context.get("direction_id") != identifier:
            continue
        owners = selected(context.get("writing_plan")) if task.get("action") == "variants" else set()
        task_owners[task["task_id"]] = owners
        append("tasks", {"id": task["task_id"], "title": context.get("title") or task.get("case_title") or "写稿任务",
                         "status": task.get("status", ""), "action": task.get("action", ""),
                         "created_at": task.get("created_at", "")}, owners)

    for script in scripts:
        if not scoped(script, "script"):
            continue
        source = script.get("source") or {}
        owners = set()
        explicit = links.get(("script", script["id"]))
        if explicit:
            owners = linked_owners(explicit)
        elif source.get("variant_ref") is not None:
            index = ref_match(source["variant_ref"])
            owners = {index} if index is not None else set()
        else:
            context = (source.get("snapshot") or {}).get("creation_context") or {}
            frozen_direction = context.get("direction") or {}
            plan = context.get("writing_plan") or {}
            # A single requested catalog variant has a unique owner. Multiple
            # generated candidates remain ambiguous even if their numeric index
            # coincidentally equals a catalog position.
            if (product_key((source.get("snapshot") or {}).get("product")) == product and
                    frozen_direction.get("direction_id") == identifier and
                    len(plan.get("variants") or []) == 1 and not plan.get("new_variant_count")):
                owners = selected(plan)
        script_owners[script["id"]] = owners
        append("scripts", {"id": script["id"], "title": script.get("title", "文案"),
                           "status": script.get("status", ""), "version": script.get("version"),
                           "created_at": script.get("created_at", ""), "association": association(explicit)}, owners)
        video = str(script.get("video_id") or "")
        if video:
            video_owners.setdefault(video, set()).update(owners)

    # Explicit script links survive removal of the original AI task. Production
    # records never fall back to their candidate variant_index.
    production = production or {}
    for kind, rows in (("video", production.get("jobs", [])), ("carousel", production.get("carousels", []))):
        for row in rows:
            if not scoped(row, "production") and not (not row.get("direction_id") and
                    product_key(row.get("product")) == product and row.get("script_id") in script_owners):
                continue
            explicit = links.get(("production", row["id"]))
            inherited = links.get(("script", row.get("script_id")))
            owners = linked_owners(explicit) if explicit else script_owners.get(row.get("script_id"), set())
            production_owners[row["id"]] = owners
            production_rows[row["id"]] = row
            status = job_status(row) if kind == "video" else row.get("status", "draft")
            append("productions", {"id": row["id"], "kind": kind, "script_id": row.get("script_id", ""),
                                    "title": row.get("title") or (row.get("script") or {}).get("title") or "制作单",
                                    "status": status, "archived": bool(row.get("archived")),
                                    "created_at": row.get("created_at", ""),
                                    "association": association(explicit) or association(inherited, row.get("script_id"))}, owners)

    publication_seen = set()

    def publication(record, kind, owner_id="", script_id="", current=True):
        if not isinstance(record, dict) or not record.get("post_id") or not record.get("published_on"):
            return
        script_id = record.get("script_id") or script_id
        # A historical publication's frozen script wins over later job changes.
        owners = script_owners.get(script_id, set())
        owner = production_rows.get(owner_id)
        explicit = links.get(("video", str(record["post_id"])))
        production_link = links.get(("production", owner_id))
        inherited_link = None
        if explicit:
            owners = linked_owners(explicit)
        elif production_link and owner and script_id == owner.get("script_id"):
            owners = production_owners.get(owner_id, set())
            inherited_link = production_link
        elif owner_id and not script_id:
            owners = production_owners.get(owner_id, set())
        source_current = current
        if owner:
            source_current = current and script_id == owner.get("script_id")
            if kind == "video":
                from workbench_video_publish import reviewed_source
                try:
                    source_current = source_current and record.get("source_signature") == fingerprint(reviewed_source(owner))
                except VoiceError:
                    source_current = False
            elif kind == "carousel":
                from workbench_carousel_delivery import carousel_selection_signature
                source_current = (source_current and bool(record.get("selection_signature")) and
                                  record["selection_signature"] == carousel_selection_signature(owner))
        video = str(record["post_id"])
        marker = (video, owner_id, script_id)
        if marker in publication_seen:
            return
        publication_seen.add(marker)
        video_owners.setdefault(video, set()).update(owners)
        append("publications", {"id": record.get("id") or video, "kind": kind, "video_id": video,
                                "published_on": record["published_on"], "production_id": owner_id,
                                "script_id": script_id, "current": current, "source_current": bool(source_current),
                                "registered_at": record.get("registered_at", ""),
                                "association": association(explicit or inherited_link)}, owners)

    for owner_id, entry in (video_publications or {}).get("jobs", {}).items():
        if owner_id not in production_owners:
            continue
        # Current first: a corrected registration of the same post must win
        # duplicate suppression over the earlier file/date/account receipt.
        publication(entry.get("publication"), "video", owner_id)
        for record in entry.get("history", []):
            publication(record, "video", owner_id, current=False)
    for carousel in production.get("carousels", []):
        if carousel.get("id") in production_owners:
            current = carousel.get("publication") or {}
            publication(current, "carousel", carousel["id"], carousel.get("script_id", ""))
            for record in carousel.get("publication_history", []):
                if record.get("id") != current.get("id"):
                    publication(record, "carousel", carousel["id"], carousel.get("script_id", ""), current=False)
    registered_scripts = {row["script_id"] for variant in result["variants"] for row in variant["records"]["publications"]}
    registered_scripts.update(row["script_id"] for row in result["unassigned"]["publications"])
    for script in scripts:
        if scoped(script, "script") and script.get("id") not in registered_scripts:
            # A dated video binding is an actual publication registration; a
            # manually selected 'published' status by itself is not.
            publication({"post_id": script.get("video_id"), "published_on": script.get("published_on"),
                         "script_id": script["id"]}, "script")

    direction_videos = {str(row.get("video_id") or "") if isinstance(row, dict) else str(row) for row in videos}
    direction_videos.update(record_id for kind, record_id in links if kind == "video")
    for video_id in sorted(direction_videos - {""}):
        explicit = links.get(("video", video_id))
        owners = linked_owners(explicit) if explicit else video_owners.get(video_id, set())
        # A reviewed video binding takes precedence over inferred script links,
        # but association alone never manufactures a dated publication receipt.
        if len(owners) != 1:
            owners = set()
        video_owners[video_id] = owners
        append("videos", {"id": video_id, "video_id": video_id,
                          "association": association(explicit)}, owners)

    for plan in plans:
        if not scoped(plan):
            continue
        candidates = []
        if plan.get("variant_ref") is not None:
            index = ref_match(plan["variant_ref"])
            # An old or edited explicit reference must not be silently replaced
            # by a different task, script, or test-video association.
            owners = {index} if index is not None else set()
        else:
            if plan.get("script_id") in script_owners:
                candidates.append(script_owners[plan["script_id"]])
            if plan.get("task_id") in task_owners:
                candidates.append(task_owners[plan["task_id"]])
            video = (plan.get("experiment") or {}).get("test_video_id")
            if video in video_owners:
                candidates.append(video_owners[video])
            # Every available link must identify the same unique variant.
            owners = candidates[0] if candidates and len(candidates[0]) == 1 and all(item == candidates[0] for item in candidates) else set()
        card = plan.get("experiment") or {}
        comparison = compare_experiment(plan, read_video or (lambda _: None)) if card else {}
        append("plans", {"id": plan["id"], "title": card.get("hypothesis") or plan.get("note") or "下一轮计划",
                         "status": plan.get("status", ""), "action": plan.get("action", ""),
                         "experiment": bool(card), "comparison_status": comparison.get("status", ""),
                         "issues": comparison.get("issues", []), "decision": card.get("decision", ""),
                         "decision_recorded": bool(card.get("decision") in {"scale", "execution", "stop"} and card.get("decision_note")),
                         "created_at": plan.get("created_at", "")}, owners)

    for variant in result["variants"]:
        _summarize_variant(variant)
    result["summary"] = {
        "variants_with_scripts": sum(bool(row["records"]["scripts"]) for row in result["variants"]),
        "variants_in_production": sum(bool(row["records"]["productions"]) for row in result["variants"]),
        "variants_published": sum(bool(row["records"]["publications"]) for row in result["variants"]),
        "variants_with_videos": sum(bool(row["records"]["videos"]) for row in result["variants"]),
        "variants_tested": sum(any(plan["comparison_status"] == "ready" for plan in row["records"]["plans"]) for row in result["variants"]),
        "unassigned_count": sum(len(rows) for rows in result["unassigned"].values())}
    return result


def _summarize_variant(variant):
    records = variant["records"]
    tasks, scripts, productions, publications, plans, videos = (records[key] for key in RECORD_KINDS)
    def stage(status, label, count=0):
        return {"status": status, "label": label, "count": count}
    writing_tasks = [row for row in tasks if row.get("action") == "variants"]
    writing = stage("done", f"已存 {len(scripts)} 稿", len(scripts)) if scripts else (
        stage("working", "写稿中", len(writing_tasks)) if any(row["status"] in {"queued", "running"} for row in writing_tasks) else
        stage("attention", "写稿任务完成", len(writing_tasks)) if any(row["status"] == "completed" for row in writing_tasks) else
        stage("attention", "写稿待处理", len(writing_tasks)) if writing_tasks else stage("empty", "未关联文案"))
    def newest(rows):
        # Catalog lists are appended in creation order; timestamps also make
        # this deterministic when callers compose video and carousel records.
        return max(enumerate(rows), key=lambda pair: (pair[1].get("created_at", ""), pair[0]))[1] if rows else None
    active_productions = [row for row in productions if not row.get("archived") and row["status"] != "cancelled"]
    incomplete_productions = [row for row in active_productions if row["status"] != "approved"]
    production_focus = newest(incomplete_productions)
    completed_productions = sum(row["status"] == "approved" for row in productions)
    production = (stage("attention", "制作待处理", len(productions)) if production_focus and production_focus["status"] in {"failed", "interrupted", "needs_repair", "needs_material", "verification_failed", "unverified", "awaiting_review"} else
                  stage("working", "制作中", len(productions)) if production_focus else
                  stage("done", "已有制作完成记录", len(productions)) if completed_productions else
                  stage("unknown", "未关联制作") if publications or videos else stage("empty", "未关联制作"))
    production.update(completed_count=completed_productions,
                      history_label=f"已有 {completed_productions} 张制作完成" if completed_productions else "")
    publishing = (stage("done", f"已登记 {len({row['video_id'] for row in publications})} 条发布", len(publications)) if publications else
                  stage("attention", f"已关联 {len(videos)} 条视频", len(videos)) if videos else stage("empty", "未登记发布"))
    tests = [row for row in plans if row["experiment"]]
    active_tests = [row for row in tests if row["status"] != "cancelled"]
    undecided_tests = [row for row in active_tests if not row["decision_recorded"]]
    test_focus = newest(undecided_tests)
    completed_tests = sum(row["decision_recorded"] for row in tests)
    testing = (stage("attention", "可比较，待记录决定", len(tests)) if test_focus and test_focus["comparison_status"] == "ready" else
               stage("attention", "比较数据待补齐", len(tests)) if test_focus and test_focus["comparison_status"] in {"incomplete", "incompatible"} else
               stage("working", "测试卡待补齐", len(tests)) if test_focus else
               stage("done", "已记录测试决定", len(tests)) if completed_tests else stage("empty", "未关联测试卡"))
    testing.update(completed_count=completed_tests,
                   history_label=f"已有 {completed_tests} 次测试决定" if completed_tests else "")
    variant["stages"] = {"writing": writing, "production": production, "publishing": publishing, "testing": testing}
    variant["counts"] = {key: len(rows) for key, rows in records.items()}
    def action(kind, label, row=None, **extra):
        return {"kind": kind, "label": label, **({"id": row["id"]} if row else {}), **extra}
    published_productions = {row["production_id"] for row in publications if row.get("source_current")}
    unfinished_productions = [row for row in active_productions if row["id"] not in published_productions]
    if production_focus or unfinished_productions:
        row = production_focus or newest(unfinished_productions)
        next_action = action("production", "登记发布" if row["status"] == "approved" else "继续制作", row, production_kind=row["kind"])
    elif active_tests:
        selected = test_focus or newest(active_tests)
        next_action = action("test", "查看结果与决定" if selected["decision_recorded"] or selected["comparison_status"] == "ready" else "补齐测试信息", selected)
    elif publications:
        latest = max(publications, key=lambda row: (bool(row.get("current")), row.get("registered_at", ""), row["published_on"]))
        next_action = action("test", "建立测试卡", video_id=latest["video_id"])
    elif videos:
        next_action = action("test", "建立测试卡", video_id=videos[-1]["video_id"])
    elif active_productions:
        row = active_productions[-1]
        next_action = action("production", "登记发布" if row["status"] == "approved" else "继续制作", row, production_kind=row["kind"])
    elif scripts:
        next_action = action("script", "查看文案并制作", scripts[-1])
    elif writing_tasks:
        row = next((row for row in writing_tasks if row["status"] in {"queued", "running", "completed"}), writing_tasks[0])
        next_action = action("task", "查看写稿结果" if row["status"] == "completed" else "查看写稿任务", row)
    else:
        next_action = action("write", "写这一稿")
    variant["next_action"] = next_action
