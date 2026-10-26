"""Photo carousel jobs: adopt a variant's 图文版 as a script version and keep its slides as a production job.

The adopted script holds the carousel as plain text (one block per slide, then the caption); the job keeps the
structured slides, matched back to the source candidate by that exact text so the two can never drift apart.
"""
from __future__ import annotations

import copy
import hashlib
import json

from direction_store import product_key
from elevenlabs_client import VoiceError
from production_store import CAROUSEL_ID
from task_activity import settle_resolved_activity
from variant_parse import carousel_text, parse_carousels, parse_variants, publish_titles


class CarouselWorkbenchMixin:
    """Needs self.production, self.creation, self.ai and self.lock from the workbench."""

    def carousel_variants(self, task_id, round_id):
        task = self.ai.get(task_id)
        turn = next((item for item in task.get("rounds", []) if item.get("round_id") == round_id), None)
        if not turn:
            raise VoiceError(404, "指定候选轮次不存在。")
        if turn.get("status") != "completed" or not str(turn.get("output") or "").strip():
            raise VoiceError(409, "只有完整完成的候选才可以制作图文。")
        carousels = parse_carousels(turn["output"])
        if not carousels:
            raise VoiceError(409, "这一轮候选没有图文版。写稿不再附图文版：定稿后用口播稿的意群表配合图文技能另做逐张表。")
        for item in carousels:
            item["text"], item["translation_zh"] = carousel_text(item), carousel_text(item, chinese=True)
        return {"task_id": task_id, "round_id": round_id, "task_revision": task.get("revision"), "carousels": carousels}

    def create_carousel(self, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "图文制作单参数格式无效。")
        prior = self.production.replay("carousel", body)
        if prior:
            return prior
        value = body.get("carousel") if isinstance(body.get("carousel"), dict) else {}
        with self.lock:
            script = self.creation.get_script(str(value.get("script_id") or ""))
            source = script.get("source") or {}
            if value.get("table_id"):
                chosen, table = self.carousel_from_table(script, str(value["table_id"]))
                resolved = {"product": product_key(script["product"]), "direction_id": script.get("direction_id", ""),
                            "script_id": script["id"], "script_version": script["version"], "script_family": script["family_id"],
                            "title": script["title"], "variant_label": table.get("variant_label") or chosen["label"],
                            "slides": copy.deepcopy(chosen["slides"]), "caption_en": chosen["caption_en"],
                            "caption_zh": chosen["caption_zh"], "copy": copy.deepcopy(chosen["copy"]),
                            "problems": chosen["problems"], "plan": copy.deepcopy(chosen.get("plan")),
                            "source": {"task_id": source.get("task_id", ""), "round_id": source.get("round_id", ""),
                                       "variant_index": table.get("variant_index"), "table_id": table["id"],
                                       **({"video_job_id": table["video_job_id"]} if table.get("video_job_id") else {})}}
                # A table is a frozen handoff. In particular, never re-read the
                # latest candidate to recover proof/tags after this point.
                context = copy.deepcopy(table.get("script_context") or {})
                if "records" in table:
                    context.update(script_id=table["script_id"], script_version=table["script_version"],
                                   records=copy.deepcopy(table["records"]),
                                   source_note=table.get("note") or context.get("source_note", ""))
                if context:
                    resolved["script_context"] = context
                return self.production.mutate("carousel", body, resolved)
            carousels = parse_carousels(source.get("candidate_text") or "")
            index = value.get("variant_index")
            hinted = next((item for item in carousels if item["index"] == index), None) if type(index) is int else None
            matches = lambda item: carousel_text(item) == str(script["text"]).strip()
            chosen = hinted if hinted and matches(hinted) else next((item for item in carousels if matches(item)), None)
            if not chosen:
                raise VoiceError(409, "这条脚本版本不是从图文版采纳的（正文和来源候选里的图文对不上），不能建图文制作单。")
            resolved = {"product": product_key(script["product"]), "direction_id": script.get("direction_id", ""),
                        "script_id": script["id"], "script_version": script["version"], "script_family": script["family_id"],
                        "title": script["title"], "variant_label": chosen["label"], "slides": copy.deepcopy(chosen["slides"]),
                        "caption_en": chosen["caption_en"], "caption_zh": chosen["caption_zh"], "copy": copy.deepcopy(chosen["copy"]),
                        "problems": chosen["problems"], "plan": copy.deepcopy(chosen.get("plan")),
                        "source": {"task_id": source.get("task_id", ""), "round_id": source.get("round_id", ""), "variant_index": chosen["index"]}}
            original = next((item for item in parse_variants(source.get("candidate_text") or "")
                             if item["index"] == chosen["index"]), None)
            if original and original.get("lines"):
                resolved["script_context"] = {"script_id": script["id"], "script_version": script["version"],
                                              "records": copy.deepcopy(original["lines"]),
                                              "source_note": "来自采纳时候选中同一变体的原稿意群。"}
            return self.production.mutate("carousel", body, resolved)

    def carousel_detail(self, identifier):
        if not CAROUSEL_ID.fullmatch(str(identifier)):
            raise VoiceError(404, "这张图文制作单不存在。")
        with self.lock:
            catalog = self.production.public()
            item = next((entry for entry in catalog["carousels"] if entry["id"] == identifier), None)
            if not item:
                raise VoiceError(404, "这张图文制作单不存在。")
            try:
                full = self.creation.get_script(item["script_id"])
                script = {key: full.get(key) for key in ("id", "title", "status", "version", "product", "direction_id")}
                # The adopted script's 🔴 title-and-hashtag lines, copied at publish time like the video order's.
                titles = publish_titles((full.get("source") or {}).get("candidate_text") or "",
                                        (item.get("source") or {}).get("variant_index"))
            except Exception:
                script, titles = None, []
            if hasattr(self, "carousel_live"):
                self.carousel_live(item)
            item["clip_picking_live"] = copy.deepcopy(getattr(self, "_clip_ai_live", {}).get(identifier))
            settings = self.production.product_settings(catalog, item["product"])
            clip_picks = self.carousel_clip_picks(identifier) if hasattr(self, "carousel_clip_picks") else None
        # All business fields above belong to one independent catalog snapshot.
        # Hashing new images and reading sales evidence must not stall unrelated writes or pages.
        if hasattr(self, "carousel_images_live"):
            self.carousel_images_live(item)
        if hasattr(self, "carousel_delivery_live"):
            self.carousel_delivery_live(item)
        if hasattr(self, "carousel_group_live"):
            self.carousel_group_live(item)
        sales_data = self.carousel_publication_live(item) if hasattr(self, "carousel_publication_live") else None
        with self.lock:
            try:
                unchanged = self.production.current_revision() == catalog["revision"]
            except (OSError, VoiceError):
                unchanged = False
            if unchanged:
                settle_resolved_activity(self, "carousel", item)
        return {"carousel": item, "revision": catalog["revision"], "script": script,
                "sales_data": sales_data, "material_timelines": settings.get("material_timelines", []),
                "project": settings["project"], "clip_picks": clip_picks, "publish_titles": titles,
                "script_registrations": self.script_registrations(item["script_id"]) if hasattr(self, "script_registrations") else []}

    def save_carousel_slides(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "参数格式无效。")
        request = {**body, "carousel_id": identifier}
        with self.lock:
            prior = self.production.replay("carousel_slides", request)
            if prior:
                return prior
            item = self.production.carousel(identifier)
            context = None
            # Existing jobs can recover their original table's frozen source
            # when the user supplies mappings. Never accept a client snapshot
            # or substitute the script's latest revision.
            mappings = body.get("slides")
            if (not item.get("script_context") and isinstance(mappings, list) and
                    any(isinstance(row, dict) and row.get("source_lines") for row in mappings)):
                table_id = (item.get("source") or {}).get("table_id")
                if table_id:
                    try:
                        raw = (self._table_dir(table_id) / "state.json").read_bytes()
                        table = json.loads(raw)
                    except (OSError, ValueError) as exc:
                        raise VoiceError(409, "原逐张表快照不可读，未补写原稿对应。") from exc
                    if any(table.get(key) != item.get(key) for key in ("script_id", "script_version")):
                        raise VoiceError(409, "原逐张表与本单脚本版本不一致，未补写原稿对应。")
                    context = copy.deepcopy(table.get("script_context") or {})
                    if "records" in table:
                        context["records"] = copy.deepcopy(table["records"])
                    records = context.get("records")
                    if (not isinstance(records, list) or not records or
                            any(not isinstance(row, dict) or type(row.get("number")) is not int or row["number"] < 1 or
                                not isinstance(row.get("spoken"), str) or not row["spoken"].strip() for row in records) or
                            len({row["number"] for row in records}) != len(records)):
                        raise VoiceError(409, "原逐张表没有完整意群原句，未补写原稿对应。")
                    context.update(script_id=item["script_id"], script_version=item["script_version"],
                                   lineage_required=True, source_table_id=table_id,
                                   source_sha256=hashlib.sha256(raw).hexdigest(),
                                   source_note="从本单原逐张表恢复冻结原稿；" + str(table.get("note") or context.get("source_note") or ""))
            return self.production.mutate("carousel_slides", request, {"script_context": context} if context else None)

    def archive_carousel(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "参数格式无效。")
        result = self.production.mutate("carousel_archive", {**body, "carousel_id": identifier})
        activity = getattr(self, "activity", None)
        if activity is not None:
            activity.archive_owner(identifier, result["carousel"].get("archived", False))
        return result

    def set_carousel_calendar_status(self, identifier, body):
        if not isinstance(body, dict) or set(body) - {"request_id", "publication_id", "calendar_uploaded"}:
            raise VoiceError(400, "发布日历参数无效。")
        with self.lock:
            result = self.production.mutate("carousel_calendar", {**body, "carousel_id": identifier})
            self.state_cache = None
            return result

    def save_carousel_requirements(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "制作要求格式无效。")
        return self.production.mutate("carousel_requirements", {**body, "carousel_id": identifier})

    def select_carousel_output(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "选版参数格式无效。")
        with self.lock:
            item = self.production.carousel(identifier)
            slide = next((entry for entry in item["slides"] if entry["number"] == body.get("slide")), None)
            output = next((entry for entry in (slide or {}).get("outputs", []) if entry["id"] == body.get("output_id")), None)
            if not output:
                raise VoiceError(404, "这张成图不属于本张。")
            from workbench_carousel_images import image_hash
            if image_hash(self.carousel_output(identifier, output["id"], "full")) != output["sha256"]:
                raise VoiceError(409, "成图文件已变化，请重新登记并核对。")
            return self.production.mutate("carousel_select_output", {**body, "carousel_id": identifier})
