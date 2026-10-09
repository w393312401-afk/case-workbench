"""Workbench phase-four API composition; writes remain explicit and recoverable."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

from creation_store import CreationError, text
from direction_store import product_key, fingerprint, now


class CreationWorkbenchMixin:
    def creation_catalog(self, product=None):
        result = self.creation.public(product)
        for script in result["scripts"]:
            script["script_id"] = script["id"]
            source = script.get("source")
            if source:
                script["source"] = {key: value for key, value in source.items() if key not in {"snapshot", "candidate_text"}}
        return result

    def _creation_direction(self, product, identifier, *, optional=False, allow_archived=False):
        if not identifier and optional:
            return None
        catalog = self.direction_catalog(product)
        item = next((item for item in catalog["directions"] if item["direction_id"] == identifier), None)
        if item is None:
            raise CreationError(400, "所选产品下没有这条方向，请重新选择。")
        if item.get("status") == "archived" and not allow_archived:
            raise CreationError(409, "该方向已归档，请恢复方向后再创作。")
        return copy.deepcopy(item)

    @staticmethod
    def _script_alias(script):
        result = copy.deepcopy(script)
        result["script_id"] = result["id"]
        return result

    def script_detail(self, identifier):
        with self.lock:
            item = self.creation.get_script(identifier)
            result = {"revision": self.creation.public()["revision"], "script": self._script_alias(item)}
            video = item.get("video_id")
            result["sales_data"] = self.sales.resolve_case({"case_id": identifier, "current": {"metrics": {"video_id": video} if video else {}}})
            result["video"] = self.performance.video(video, self.video_case_links()) if video else None
            result["case"] = None
            if item.get("case_id") and self.store.folder(item["case_id"]).is_dir():
                result["case"] = self.detail(item["case_id"])
            source = result["script"].get("source")
            if source and source.get("task_id"):
                try:
                    self.ai._read(source["task_id"])
                except Exception:
                    source["task_deleted"] = True
            return result

    def save_script(self, body):
        with self.lock:
            prior = self.creation.replay("script", body)
            if prior:
                self._link_script_plan(prior["script"])
                return {**prior, "script": self._script_alias(prior["script"]), "catalog": self.creation_catalog()}
            value = body.get("script") or {}
            product = product_key(value.get("product"))
            if not product or product == "all":
                raise CreationError(400, "请先选择具体产品。")
            self._creation_direction(product, value.get("direction_id"), optional=True)
            source = None
            requested = body.get("source")
            if requested:
                if not isinstance(requested, dict):
                    raise CreationError(400, "候选来源格式无效。")
                source = self.ai.candidate(requested.get("task_id"), requested.get("round_id"), requested.get("task_revision"))
                if product != source["product"]:
                    raise CreationError(409, "候选与文案产品不一致。")
            result = self.creation.mutate("script", body, source)
            self._link_script_plan(result["script"])
            result["script"] = self._script_alias(result["script"])
            self.state_cache = None
            return {**result, "catalog": self.creation_catalog()}

    def save_script_status(self, identifier, body):
        with self.lock:
            body = copy.deepcopy(body)
            value = body.setdefault("script_status", {})
            if not isinstance(value, dict):
                raise CreationError(400, "文案状态格式无效。")
            if "case_id" in value:
                raise CreationError(400, "案例关联请通过明确沉淀或写回操作保存。")
            value["script_id"] = identifier
            prior = self.creation.replay("script_status", body)
            if prior:
                return {**prior, "script": self._script_alias(prior["script"]), "catalog": self.creation_catalog()}
            script = self.creation.get_script(identifier)
            video = str(value.get("video_id", script["video_id"]) or "")
            if video:
                actual = self.performance.video_brief([video]).get(video)
                if actual and actual.get("product") and product_key(actual["product"]) != script["product"]:
                    raise CreationError(409, "视频与文案所属产品不同，不能关联。")
                for other in self.creation.public()["scripts"]:
                    if other["id"] != identifier and other.get("video_id") == video:
                        raise CreationError(409, "这条视频已登记到另一文案版本，请先解除原绑定。")
                for case in self.store.read_index()["cases"]:
                    current = case.get("current") or {}
                    if str((current.get("metrics") or {}).get("video_id") or "") == video:
                        if product_key(case.get("product")) != script["product"]:
                            raise CreationError(409, "这条视频的已有案例属于另一产品。")
                        assigned = (current.get("test_direction") or {}).get("direction_id")
                        if assigned and script.get("direction_id") and assigned != script["direction_id"]:
                            raise CreationError(409, "视频已有案例使用另一方向，请先在案例中核对统一方向。")
                linked = self.directions.find_link(self.directions.read(), script["product"], video)
                if linked and linked.get("direction_id") and script.get("direction_id") and linked["direction_id"] != script["direction_id"]:
                    raise CreationError(409, "视频已关联另一方向，请先统一视频方向。")
            if script.get("case_id") and self.store.folder(script["case_id"]).is_dir():
                bound = self.store.read_case(script["case_id"])
                bound_video = str(((bound.get("current") or {}).get("metrics") or {}).get("video_id") or "")
                if video != bound_video:
                    raise CreationError(409, "文案已沉淀到案例，不能单独改绑视频；请保存新文案版本。")
            result = self.creation.mutate("script_status", body)
            result["script"] = self._script_alias(result["script"])
            self.state_cache = None
            return {**result, "catalog": self.creation_catalog()}

    def _operation(self, kind, body):
        key = body.get("request_id") if isinstance(body, dict) else None
        if not isinstance(key, str) or not re.fullmatch(r"[\w-]{10,100}", key):
            raise CreationError(400, "缺少有效的操作标识。")
        path = self.creation.root / "operations" / (key + ".json")
        digest = fingerprint([kind, body])
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("hash") != digest:
                raise CreationError(409, "操作标识已用于其他修改。")
            if value.get("source_deleted"):
                raise CreationError(410, "此操作的关联案例已删除，不能再次写回或恢复。")
            return path, value
        return path, {"kind": kind, "hash": digest, "request_id": key, "created_at": now()}

    def script_to_case(self, identifier, body):
        """A journal plus an event marker closes the two-store crash/retry gap."""
        with self.lock:
            operation_body = {**body, "script_id": identifier}
            path, operation = self._operation("script_case", operation_body)
            if operation.get("complete"):
                return {"case": self.detail(operation["case_id"]), "script": self._script_alias(self.creation.get_script(identifier)), "catalog": self.creation_catalog(), "replayed": True}
            script = self.creation.get_script(identifier)
            marker = "[creation:" + body["request_id"] + "]"
            matched = []
            for entry in self.store.read_index()["cases"]:
                case = self.store.read_case(entry["case_id"])
                if any(marker in (event.get("feedback") or {}).get("text", "") for event in case["events"]):
                    matched.append(case)
            if len(matched) > 1:
                raise CreationError(409, "发现重复的创作事件标识，请检查案例记录。")
            if not operation.get("prepared") and body.get("revision") != self.creation.public()["revision"]:
                raise CreationError(409, "文案资料已更新，请刷新后重新沉淀。")
            if not matched and script.get("case_id") and body.get("case_id") != script["case_id"]:
                raise CreationError(409, "该文案版本已经关联案例，请打开原案例继续记录，不能重复沉淀到另一案例。")
            if matched:
                case_result = self.detail(matched[0]["case_id"])
            else:
                target = body.get("case_id") or None
                if target:
                    existing = self.detail(target)
                    if product_key(existing.get("product")) != script["product"]:
                        raise CreationError(409, "不能写回另一产品的案例。")
                    if body.get("case_revision") != existing["revision"]:
                        raise CreationError(409, "目标案例已更新，请读取最新内容后再写回。")
                    old_video = str((existing["current"].get("metrics") or {}).get("video_id") or "")
                    if old_video and old_video != script.get("video_id"):
                        raise CreationError(409, "目标案例对应另一条视频，不能把新稿覆盖到旧视频。请另建案例，或选择相同视频的案例。")
                    current = copy.deepcopy(existing["current"])
                else:
                    current = {"title": script["title"], "category": "待验证", "kind": "editorial", "assessments": [],
                               "content_origin": "self_produced", "evidence": {"status": "unverified", "source": "用户核对后采纳的独立文案", "context": "文案采纳不代表市场效果已验证。"}}
                conclusion = text(body.get("conclusion"), "沉淀说明", limit=12000)
                current.update(title=text(body.get("title") or script["title"], "案例标题", limit=500), conclusion=conclusion,
                               transcript={"text": script["text"], "status": "用户已核对的创作文案；不是原片转写", "source": "独立文案版本 " + identifier})
                current["production"] = {**current.get("production", {}), "script_version": identifier}
                current.pop("translation_zh", None)
                if script.get("translation_zh"):
                    current["translation_zh"] = {"source_text": script["text"], "text": script["translation_zh"], "channel": "transcript"}
                if script.get("video_id"):
                    current.setdefault("metrics", {})["video_id"] = script["video_id"]
                if script.get("published_on"):
                    current["publish"] = {"published_on": script["published_on"]}
                direction = self._creation_direction(script["product"], script.get("direction_id"), optional=True)
                if direction:
                    current["test_direction"] = {key: direction[key] for key in ("direction_id", "main_difference", "concern", "closing")}
                    current["test_direction"].update(variant_type="execution", execution_change="采纳独立文案版本 " + identifier, script_version=identifier)
                else:
                    current.pop("test_direction", None)
                operation.update(prepared=True, script_id=identifier, target_case_id=target)
                self.creation._replace(path, operation)
                saved = self.save(target, {"request_id": "case-" + hashlib.sha256(body["request_id"].encode()).hexdigest(), "revision": body.get("case_revision"),
                                           "product": script["product"], "current": current, "note": conclusion + "\n" + marker})
                case_result = saved["case"]
            operation.update(case_id=case_result["case_id"], script_id=identifier)
            self.creation._replace(path, operation)
            status_body = {"request_id": "link-" + hashlib.sha256(body["request_id"].encode()).hexdigest(), "revision": self.creation.public()["revision"],
                           "script_status": {"script_id": identifier, "status": "reviewed" if script.get("video_id") and script.get("published_on") else script["status"],
                                             "case_id": case_result["case_id"], "note": "用户明确沉淀到案例"}}
            # A prior completed association is detected even when the original response was lost.
            latest = self.creation.get_script(identifier)
            if latest.get("case_id") == case_result["case_id"]:
                changed = {"script": latest}
            else:
                changed = self.creation.mutate("script_status", status_body)
            operation["complete"] = True
            self.creation._replace(path, operation)
            self.state_cache = None
            return {"case": case_result, "script": self._script_alias(changed["script"]), "catalog": self.creation_catalog()}

    def cleanup_creation_case(self, identifier):
        self.creation.forget_case(identifier)
        self.ai.scrub_case_references(identifier)
        for path in (self.creation.root / "operations").glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            material = (record.get("mapped") or {}).get("material") or {}
            if identifier in {record.get("case_id"), record.get("target_case_id")} or material.get("kind") == "case" and str(material.get("ref_id") or "").startswith(identifier):
                self.creation._replace(path, {"hash": record["hash"], "request_id": record["request_id"], "source_deleted": True})

    @staticmethod
    def _review_material(review):
        return {"id": "review:" + review["id"], "type": "review", "source_id": review["id"],
                "title": str(review.get("text") or "评论摘录")[:100], "text": review.get("text") or "",
                "product": product_key(review.get("product")), "note": (review.get("pick") or {}).get("note", ""),
                "source_label": review.get("platform") or "评论", "source_url": review.get("url") or review.get("source_url") or "",
                "added": (review.get("pick") or {}).get("added"), "unverified": True}

    def material_catalog(self, product=None):
        with self.lock:
            catalog = self.creation.public()
            picks = self.reviews.picks()
            items = [self._review_material(review) for review in picks]
            material_revision = fingerprint([catalog["revision"], items])
            for item in catalog["materials"]:
                items.append({**item, "type": item.get("type") or {"case": "expression", "video": "hook"}.get(item["kind"], "expression"),
                              "source_id": item.get("source_id") or item.get("ref_id", "").split(":", 1)[0],
                              "source_label": item.get("source_label") or item["title"], "added": item["created_at"]})
            if product and product != "all":
                items = [item for item in items if item["product"] == product_key(product)]
            themes = catalog["theme_links"]
            if product and product != "all":
                themes = [item for item in themes if item["product"] == product_key(product)]
            return {"revision": material_revision, "creation_revision": catalog["revision"], "items": items, "theme_links": themes}

    def save_material(self, body):
        with self.lock:
            value = body.get("item")
            if not isinstance(value, dict) or value.get("type") not in {"review", "expression", "hook"}:
                raise CreationError(400, "素材类型无效。")
            operation = body.get("operation", "add")
            if operation not in {"add", "remove", "note"}:
                raise CreationError(400, "素材操作无效。")
            path, receipt = self._operation("material", body)
            if receipt.get("complete"):
                return self.material_catalog()
            if not receipt.get("prepared") and body.get("revision") != self.material_catalog()["revision"]:
                raise CreationError(409, "素材篮已更新，请刷新后重新保存；本次没有覆盖其他窗口的修改。")
            receipt["prepared"] = True
            if value["type"] == "review":
                source = str(value.get("source_id") or value.get("id", "").removeprefix("review:"))
                current_review = self.reviews.review(source)
                if not current_review:
                    raise CreationError(404, "评论来源不存在。")
                current_pick = current_review.get("pick")
                if "review_before" not in receipt:
                    receipt["review_before"] = current_pick
                    receipt["review_note"] = value.get("note") if value.get("note") is not None else (current_pick or {}).get("note", "")
                already_applied = (current_pick is None if operation == "remove" else current_pick is not None and current_pick.get("note", "") == receipt["review_note"])
                if not already_applied and current_pick != receipt["review_before"]:
                    raise CreationError(409, "这条摘录已被其他窗口修改；本次重试没有覆盖新备注，请刷新核对。")
                self.creation._replace(path, receipt)
                if not already_applied:
                    self.reviews.set_pick(source, operation != "remove", receipt["review_note"])
            else:
                if receipt.get("mapped"):
                    self.creation.mutate("material", receipt["mapped"])
                    receipt["complete"] = True
                    receipt.pop("mapped", None)
                    self.creation._replace(path, receipt)
                    return self.material_catalog()
                catalog = self.creation.public()
                old = next((item for item in catalog["materials"] if item["id"] == value.get("id")), None)
                if operation in {"remove", "note"} and not old:
                    raise CreationError(404, "该素材已经移出，请刷新素材篮。")
                if operation in {"remove", "note"}:
                    material = {**old, "note": value.get("note", old["note"])}
                else:
                    source_id = text(value.get("source_id"), "素材来源", limit=100)
                    quote = text(value.get("text"), "素材内容", limit=100000)
                    if value["type"] == "expression":
                        case = self.store.read_case(source_id)
                        expressions = [entry.get("original") for entry in ((case.get("current") or {}).get("native_reference") or {}).get("entries", [])]
                        if quote not in expressions:
                            raise CreationError(409, "原生表达已变化或不是此案例的原句，请重新选择。")
                        product, label = product_key(case.get("product")), case.get("current", {}).get("title", case["title"])
                    else:
                        video = self.performance.video(source_id, self.video_case_links())
                        if not video:
                            raise CreationError(404, "视频素材来源不存在。")
                        product, label = product_key(video.get("product")), video.get("title") or source_id
                    if value.get("product") and product != product_key(value["product"]):
                        raise CreationError(409, "素材与产品不一致。")
                    material = {"kind": "case" if value["type"] == "expression" else "video", "type": value["type"],
                                "ref_id": source_id + ":" + hashlib.sha256(quote.encode()).hexdigest()[:16], "source_id": source_id,
                                "product": product, "title": label, "text": quote, "note": value.get("note", ""),
                                "source_label": label, "source_url": value.get("source_url", "")}
                mapped = {"request_id": body["request_id"], "revision": catalog["revision"], "material": material,
                          "operation": "remove" if operation == "remove" else "save"}
                receipt["mapped"] = mapped
                self.creation._replace(path, receipt)
                self.creation.mutate("material", mapped)
            receipt["complete"] = True
            receipt.pop("mapped", None)
            self.creation._replace(path, receipt)
            return self.material_catalog()

    def save_material_theme(self, body):
        with self.lock:
            path, receipt = self._operation("theme", body)
            if receipt.get("complete"):
                return self.material_catalog()
            if not receipt.get("prepared") and body.get("revision") != self.material_catalog()["revision"]:
                raise CreationError(409, "素材篮或主题关联已更新，请刷新后重新保存。")
            receipt["prepared"] = True
            if receipt.get("mapped"):
                self.creation.mutate("theme_link", receipt["mapped"])
                receipt["complete"] = True
                receipt.pop("mapped", None)
                self.creation._replace(path, receipt)
                return self.material_catalog()
            review = self.reviews.review(body.get("review_id"))
            if not review:
                raise CreationError(404, "评论来源不存在。")
            product = product_key(review.get("product"))
            if product != product_key(body.get("product")):
                raise CreationError(409, "评论与所选产品不一致。")
            catalog = self.direction_catalog(product)
            role, target = body.get("role"), body.get("target_id")
            point = next((item for item in catalog["selling_points"] if item["id"] == target), None) if role == "selling_point" else None
            direction = self._creation_direction(product, target) if role == "concern" else None
            if not point and not direction:
                raise CreationError(400, "请选择同产品的卖点或方向顾虑。")
            link = {"product": product, "theme": str(review.get("text") or "评论主题")[:500], "review_id": review["id"],
                    "source_quote": str(review.get("text") or ""), "role": role, "target_id": target, "note": body.get("note", ""),
                    "selling_point_id": point["id"] if point else "", "direction_id": direction["direction_id"] if direction else "",
                    "concern": str(review.get("text") or "评论所述顾虑") if direction else ""}
            mapped = {"revision": self.creation.public()["revision"], "request_id": body["request_id"], "theme_link": link}
            receipt["mapped"] = mapped
            self.creation._replace(path, receipt)
            self.creation.mutate("theme_link", mapped)
            receipt["complete"] = True
            receipt.pop("mapped", None)
            self.creation._replace(path, receipt)
            return self.material_catalog()

    def save_plan(self, body):
        with self.lock:
            prior = self.creation.replay("plan", body)
            if prior:
                return {**prior, "catalog": self.creation_catalog()}
            value = body.get("plan") or {}
            if value.get("id"):
                old = next((plan for plan in self.creation.public()["plans"] if plan["id"] == value["id"]), {})
                value = {**old, **value}
            self._creation_direction(value.get("product"), value.get("direction_id"),
                                     allow_archived=bool(value.get("id") and value.get("status") in {"done", "cancelled"}))
            if value.get("task_id"):
                task = self.ai.get(value["task_id"])
                if product_key(task.get("product")) != product_key(value.get("product")) or (task.get("context") or {}).get("direction_id") != value.get("direction_id"):
                    raise CreationError(409, "任务不属于此计划的产品和方向。")
            if value.get("script_id"):
                script = self.creation.get_script(value["script_id"])
                if script["product"] != product_key(value.get("product")) or script.get("direction_id") != value.get("direction_id"):
                    raise CreationError(409, "文案不属于此计划的产品和方向。")
            result = self.creation.mutate("plan", body)
            return {**result, "catalog": self.creation_catalog()}

    def _link_task_plan(self, task):
        key = "task-plan-" + task["task_id"]
        if key in self.creation.read()["requests"]:
            return
        raw = self.ai._read(task["task_id"])
        planned = (raw["snapshot"].get("creation_context") or {}).get("plan") or {}
        plan = next((item for item in self.creation.public()["plans"] if item["id"] == planned.get("id")), None)
        if not plan or plan.get("task_id") == task["task_id"]:
            return
        status = "working" if plan["status"] == "pending" else plan["status"]
        body = {"revision": self.creation.public()["revision"], "request_id": key,
                "plan": {"id": plan["id"], "task_id": task["task_id"], "status": status}}
        self.creation.mutate("plan", body)

    def _link_script_plan(self, script):
        key = "script-plan-" + script["id"]
        if key in self.creation.read()["requests"]:
            return
        source = script.get("source") or {}
        plan = next((item for item in self.creation.public()["plans"] if item["id"] == source.get("plan_id")), None)
        if not plan or plan.get("script_id") == script["id"]:
            return
        if plan["product"] != script["product"] or plan["direction_id"] != script.get("direction_id"):
            return  # An explicitly selected different direction is not credited to the original plan.
        self.creation.mutate("plan", {"revision": self.creation.public()["revision"], "request_id": key,
                                     "plan": {"id": plan["id"], "script_id": script["id"]}})

    def create_ai_task(self, body):
        with self.lock:
            body = dict(body)
            if not body.get("action"):
                body["action"] = "variants"
            identifier = body.get("case_id") or None
            # Durable task receipts must win over live material/direction changes.
            key, digest = self.ai._request(body, "create", identifier)
            prior = self.ai._prior(key, digest)
            if prior:
                task = self.ai.get(prior["task_id"])
                self._link_task_plan(task)
                return task
            case = self.detail(identifier) if identifier else None
            product = product_key(case.get("product") if case else body.get("product"))
            if not product or product == "all":
                raise CreationError(400, "请先选择具体产品，再开始协作。")
            direction_id = body.get("direction_id") or (((case or {}).get("current") or {}).get("test_direction") or {}).get("direction_id")
            direction = self._creation_direction(product, direction_id, optional=True)
            materials = self.material_catalog(product)["items"] if body.get("include_basket") or body.get("material_ids") else []
            if "material_ids" in body:
                selected = body["material_ids"]
                if not isinstance(selected, list) or any(not isinstance(value, str) for value in selected):
                    raise CreationError(400, "所选素材编号格式无效。")
                if set(selected) - {item["id"] for item in materials}:
                    raise CreationError(409, "部分素材已移出或不属于当前产品，请刷新素材篮。")
                materials = [item for item in materials if item["id"] in selected]
            context = {"kind": "case" if case else "direction" if direction else "blank", "product": product,
                       "title": (case or {}).get("current", {}).get("title") or (direction or {}).get("direction_id") or "空白创作",
                       "direction": direction, "materials": materials}
            if body.get("plan_id"):
                plan = next((item for item in self.creation.public()["plans"] if item["id"] == body["plan_id"]), None)
                if not plan:
                    raise CreationError(404, "待写计划不存在。")
                if plan["product"] != product or plan["direction_id"] != direction_id:
                    raise CreationError(409, "待写计划与所选产品或方向不一致。")
                if plan["status"] not in {"pending", "working"} or plan["action"] == "stop":
                    raise CreationError(409, "该计划已结束或为停测计划，不能继续生成稿件。")
                context["plan"] = copy.deepcopy(plan)
            if body.get("script_id"):
                script = self.creation.get_script(body["script_id"])
                if script["product"] != product:
                    raise CreationError(409, "文案与当前产品不一致。")
                context["script"] = {key: script[key] for key in ("id", "title", "text", "translation_zh")}
                context["sales_data"] = self.script_detail(script["id"])["sales_data"]
            task = self.ai.create(identifier, body, context)
            self._link_task_plan(task)
            return task
