"""Register the package actually posted, with a recoverable script/production journal."""
from __future__ import annotations

import copy
from datetime import date
import hashlib
import re
from urllib.parse import urlsplit
from uuid import uuid4

from creation_store import CreationError, text
from direction_store import fingerprint, now
from elevenlabs_client import VoiceError


class CarouselPublishMixin:
    def _carousel_publication_links(self, item, post_id):
        for other in self.production.public()["carousels"]:
            published = other.get("publication") or {}
            if other["id"] != item["id"] and published:
                if published.get("post_id") == post_id:
                    raise VoiceError(409, "这条帖子已登记到另一图文制作单，请先核对原登记。")
                if other["script_id"] == item["script_id"]:
                    raise VoiceError(409, "这条脚本已用于另一制作单的发布，请另存脚本版本后登记。")

    @staticmethod
    def _publication_reply(saved, script):
        return {**saved, "script": {key: script.get(key) for key in ("id", "version", "status", "video_id", "published_on")}}

    def save_carousel_publication(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "发布登记参数格式无效。")
        operation_body = {**body, "carousel_id": identifier}
        with self.lock:
            path, operation = self._operation("carousel_publication", operation_body)
            if operation.get("complete"):
                return copy.deepcopy(operation["result"])
            item = self.production.carousel(identifier)
            if not operation.get("prepared"):
                if body.get("revision") != self.production.public()["revision"]:
                    raise VoiceError(409, "制作资料已更新，请读取最新记录后重新登记。")
                if item["archived"]:
                    raise VoiceError(409, "制作单已归档，请恢复后再登记发布。")
                post = text(body.get("post_id"), "帖子 ID", True, 25)
                if not re.fullmatch(r"[0-9]{6,25}", post):
                    raise VoiceError(400, "帖子 ID 须为 6–25 位数字。")
                published = text(body.get("published_on"), "发布日期", True, 10)
                try:
                    date.fromisoformat(published)
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", published):
                        raise ValueError()
                except ValueError as exc:
                    raise VoiceError(400, "发布日期须为有效的 YYYY-MM-DD。") from exc
                url = text(body.get("post_url", ""), "帖子链接", False, 2000)
                if url:
                    try:
                        parts = urlsplit(url)
                        if parts.scheme not in {"https", "http"} or not parts.netloc or parts.username or parts.password:
                            raise ValueError()
                        linked_id = re.search(r"/(?:photo|video)/(\d{6,25})(?:/|$)", parts.path)
                        if linked_id and linked_id[1] != post:
                            raise VoiceError(400, "帖子链接中的 ID 与填写的帖子 ID 不一致。")
                    except ValueError as exc:
                        raise VoiceError(400, "帖子链接须为完整的 http 或 https 地址。") from exc
                package = next((entry for entry in item.get("deliveries", []) if entry["id"] == body.get("delivery_id")), None)
                if not package:
                    raise VoiceError(404, "请先整理成品包，再选择实际发布的那一包。")
                self._checked_delivery(identifier, package, complete=True)
                self._carousel_publication_links(item, post)
                script = self.creation.get_script(item["script_id"])
                previous = item.get("publication") or {}
                if script.get("video_id") and script["video_id"] != post and script["video_id"] != previous.get("post_id"):
                    raise VoiceError(409, "脚本已登记另一帖子，请核对原登记或另存脚本版本。")
                record = {"id": "PUB-" + uuid4().hex[:12], "delivery_id": package["id"], "delivery_sha256": package["sha256"],
                          "script_id": package["script_id"], "script_version": package["script_version"],
                          "selection_signature": package["selection_signature"], "post_id": post,
                          "published_on": published, "post_url": url, "account": text(body.get("account", ""), "发布账号", False, 200),
                          "note": text(body.get("note", ""), "发布备注", False, 2000), "updated_at": now(),
                          "expected_publication": fingerprint(item.get("publication"))}
                operation.update(prepared=True, publication=record, production_body=operation_body,
                                 script_body={"request_id": "carouselpub-script-" + hashlib.sha256(body["request_id"].encode()).hexdigest()[:32],
                                              "revision": self.creation.public()["revision"],
                                              "script_status": {"status": "published", "video_id": post, "published_on": published,
                                                                "note": f"登记图文发布；制作单 {identifier}，成品包 {package['id']}"}})
                self.creation._replace(path, operation)
            record, production_body, script_body = operation["publication"], operation["production_body"], operation["script_body"]
            prior = self.production.replay("carousel_publication", production_body)
            script_prior = self.creation.replay("script_status", {**script_body, "script_status": {**script_body["script_status"], "script_id": item["script_id"]}})
            if prior:
                if not script_prior:
                    raise CreationError(409, "发布关联不完整，请保留记录并核对脚本状态。")
                result = self._publication_reply(prior, script_prior["script"])
            else:
                if item["archived"]:
                    raise VoiceError(409, "制作单已归档，请恢复后再接续登记。")
                if fingerprint(item.get("publication")) != record["expected_publication"]:
                    raise VoiceError(409, "发布记录已被另一操作修改，本次未覆盖，请核对最新登记。")
                self.carousel_delivery_file(identifier, record["delivery_id"])
                self._carousel_publication_links(item, record["post_id"])
                if script_prior:
                    current = self.creation.get_script(item["script_id"])
                    if (current.get("video_id"), current.get("published_on"), current.get("status")) not in {
                        (record["post_id"], record["published_on"], "published"),
                        (record["post_id"], record["published_on"], "reviewed")}:
                        raise VoiceError(409, "脚本发布状态已被另一操作修改，本次未恢复旧状态，请核对后重新登记。")
                    changed = script_prior
                else:
                    changed = self.save_script_status(item["script_id"], script_body, carousel_id=identifier)
                saved = self.production.mutate("carousel_publication", production_body, record)
                result = self._publication_reply(saved, changed["script"])
            operation.update(complete=True, result=result)
            self.creation._replace(path, operation)
            self.state_cache = None
            return copy.deepcopy(result)

    def carousel_publication_live(self, item):
        published = item.get("publication") or {}
        return self.sales.resolve_case({"case_id": item["id"], "current": {"metrics": {"video_id": published["post_id"]} if published else {}}})
