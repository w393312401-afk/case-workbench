"""Import a finished conversation candidate without running an AI task.

The operation journal bridges the task record and the existing script catalog.
Only the supplied, unchanged script versions receive candidate provenance.
"""
from __future__ import annotations

import copy
import hashlib
from uuid import uuid4

from ai_tasks import _hash, _now
from creation_store import CreationError, text
from direction_store import product_key
from variant_parse import parse_variants, spoken_lines


def _result(app, operation, *, replayed=False):
    return {"task": app.ai.get(operation["task"]["task_id"]),
            "scripts": [app._script_alias(app.creation.get_script(item["script_id"]))
                        for item in operation["commit_body"]["bindings"]],
            "catalog": app.creation_catalog(), "replayed": replayed}


def _prepare(app, body, operation):
    if body.get("confirmed") is not True:
        raise CreationError(400, "请明确确认导入已完成候选并补关联现有脚本。")
    catalog = app.creation.read()
    if body.get("revision") != app.creation.revision(catalog):
        raise CreationError(409, "创作资料已更新，请读取最新记录后重新导入。")
    product = product_key(text(body.get("product"), "产品", limit=200))
    if not product or product == "all":
        raise CreationError(400, "请先选择具体产品。")
    direction_id = text(body.get("direction_id", ""), "方向编号", False, 200)
    direction = app._creation_direction(product, direction_id, optional=True)
    title = text(body.get("title"), "任务标题", limit=500)
    instruction = text(body.get("instruction"), "原任务要求", limit=20000)
    origin = {"label": text(body.get("origin_label"), "来源说明", limit=500),
              "reference": text(body.get("origin_reference"), "来源记录", limit=4000)}
    output = body.get("output")
    if not isinstance(output, str) or not output.strip() or len(output) > 500000:
        raise CreationError(400, "完整候选正文格式无效。")
    variants = parse_variants(output, loose=False)
    if not variants or any(item["problems"] for item in variants):
        raise CreationError(400, "候选须包含完整口播、逐行中文及无缺漏的意群表。")
    bindings = body.get("bindings")
    if not isinstance(bindings, list) or not bindings or len(bindings) > 100:
        raise CreationError(400, "请提供要补关联的脚本和候选变体编号。")
    scripts = {item["id"]: item for item in catalog["scripts"]}
    indexed = {item["index"]: item for item in variants}
    seen, frozen = set(), []
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != {"script_id", "variant_index"}:
            raise CreationError(400, "脚本关联参数格式无效。")
        identifier, index = binding["script_id"], binding["variant_index"]
        if not isinstance(identifier, str) or identifier in seen:
            raise CreationError(400, "脚本编号无效或重复。")
        seen.add(identifier)
        script = scripts.get(identifier)
        if not script:
            raise CreationError(404, "要补关联的脚本不存在。")
        if type(index) is not int or index not in indexed:
            raise CreationError(400, "候选变体编号无效。")
        if script["product"] != product or script.get("direction_id", "") != direction_id:
            raise CreationError(409, "脚本与候选的产品或方向不一致。")
        if script.get("source"):
            raise CreationError(409, "这条脚本已有来源，请保留原关联。")
        lines = indexed[index]["lines"]
        chinese = [line.strip() for line in script.get("translation_zh", "").splitlines() if line.strip()]
        if (spoken_lines(script["text"]) != [line["spoken"] for line in lines]
                or chinese != [line["zh"] for line in lines]):
            raise CreationError(409, "候选的口播或中文与现有脚本未逐行完全一致，未补关联。")
        frozen.append({"script_id": identifier, "text": script["text"],
                       "translation_zh": script.get("translation_zh", "")})
    stamp = _now()
    origin.update(imported_at=stamp, candidate_sha256=hashlib.sha256(output.encode("utf-8")).hexdigest())
    context = {"kind": "direction" if direction else "blank", "product": product,
               "title": title, "direction": direction, "materials": [], "import_origin": origin}
    snapshot = {"case_id": None, "title": title, "product": product,
                "current": {"title": title}, "original_reference": None, "assets": {},
                "source_analysis": None, "resources": [], "captured_at": stamp,
                "creation_context": context, "import_origin": origin}
    task_id = "AIT-" + uuid4().hex[:12]
    while task_id in app.ai._locations or app.ai._path(task_id, None).parent.exists():
        task_id = "AIT-" + uuid4().hex[:12]
    round_id = "AIR-" + uuid4().hex[:12]
    notice = "已完成对话稿导入；未在工作台重新调用模型或执行独立内容复核。"
    turn = {"round_id": round_id, "kind": "external_import", "instruction": instruction + "\n\n" + notice
            + "\n来源：" + origin["label"] + "\n来源记录：" + origin["reference"],
            "status": "completed", "output": output, "error": None, "created_at": stamp,
            "completed_at": stamp, "messages": []}
    task = {"schema_version": 1, "task_id": task_id, "case_id": None, "case_title": title,
            "action": "variants", "status": "completed", "revision": 0,
            "base_revision": _hash(snapshot), "base_event_id": None,
            "created_at": stamp, "updated_at": stamp, "progress": notice,
            "snapshot": snapshot, "rounds": [turn],
            "requests": [{"request_id": body["request_id"], "hash": operation["hash"], "round_id": round_id}]}
    operation.update(prepared=True, task=task, script_texts=frozen,
                     commit_body={"request_id": "import-link-" + hashlib.sha256(body["request_id"].encode()).hexdigest(),
                                  "revision": body["revision"], "bindings": copy.deepcopy(bindings)})


def import_completed_task(app, body):
    """Journal, publish one completed task, and atomically link existing scripts."""
    if not isinstance(body, dict):
        raise CreationError(400, "导入参数必须是对象。")
    with app.lock:
        path, operation = app._operation("completed_task_import", body)
        if operation.get("complete"):
            return _result(app, operation, replayed=True)
        if not operation.get("prepared"):
            _prepare(app, body, operation)
            app.creation._replace(path, operation)
        expected = operation["task"]
        task_path = app.ai._path(expected["task_id"], None)
        if task_path.exists():
            task = app.ai._read(expected["task_id"])
            # A retry cannot adopt an edited/continued task or silently replace it.
            keys = set(expected) - {"revision", "updated_at"}
            if task["revision"] != 1 or any(task.get(key) != expected[key] for key in keys):
                raise CreationError(409, "导入任务已变化，请保留现有记录并核对来源。")
        else:
            if operation.get("task_published") or operation.get("task_publish_started"):
                raise CreationError(409, "导入任务的写入结果不明或任务已被删除，请核对现有记录；重试不会重新创建。")
            # Persist the attempt before publication. If the result is lost and
            # the task later disappears, a retry cannot distinguish deletion
            # from an interrupted first write and must not resurrect the ID.
            operation["task_publish_started"] = True
            app.creation._replace(path, operation)
            task = copy.deepcopy(expected)
            app.ai._write(task, initial=True)
        if not operation.get("task_published"):
            operation["task_published"] = True
            app.creation._replace(path, operation)
        if "sources" not in operation:
            source = app.ai.candidate(task["task_id"], task["rounds"][0]["round_id"], task["revision"])
            operation["sources"] = [{**copy.deepcopy(frozen),
                                     "source": {**copy.deepcopy(source), "variant_index": binding["variant_index"]}}
                                    for binding, frozen in zip(operation["commit_body"]["bindings"], operation["script_texts"])]
            app.creation._replace(path, operation)
        app.creation.mutate("script_sources", operation["commit_body"], operation["sources"])
        operation["complete"] = True
        app.creation._replace(path, operation)
        app.state_cache = None
        return _result(app, operation)
