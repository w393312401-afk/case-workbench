"""Persistent, explicit decisions for a video's observation window."""
from __future__ import annotations

import copy
from datetime import date
import json
import re

from creation_store import CreationStore, CreationError, text
from direction_store import fingerprint, now, product_key
from review_policy import review_today

DECISIONS = {"", "scale", "execution", "stop"}
REFERENCE_VALUES = {"", "none", "weak", "medium", "high"}

class ReviewFollowupStore(CreationStore):
    def __init__(self, runtime, lock=None):
        super().__init__(runtime, lock)
        self.root = self.root / "followups"
        self.path = self.root / "catalog.json"

    @staticmethod
    def empty():
        return {"schema_version": 1, "entries": [], "requests": {}}

    def read(self):
        if not self.path.exists():
            return self.empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("schema_version") != 1
                    or not isinstance(data.get("entries"), list) or not isinstance(data.get("requests"), dict)):
                raise ValueError()
            for entry in data["entries"]:
                if not isinstance(entry, dict) or entry.get("state") not in {"pending", "reviewed", "deferred", "observing"}:
                    raise ValueError()
                if not isinstance(entry.get("reference_value", ""), str) or entry.get("reference_value", "") not in REFERENCE_VALUES:
                    raise ValueError()
            return data
        except (OSError, ValueError, TypeError) as exc:
            raise CreationError(500, "复盘记录无法读取，请保留文件并从备份恢复。") from exc

    def _read_catalog(self):
        # The inherited cached reader checks creation fields (scripts, plans…); this file holds entries,
        # so replay() and revision checks must read it with this store's own validation.
        return self.read()

    def public(self, product=None):
        with self.lock:
            data = self.read()
            entries = [copy.deepcopy(row) for row in data["entries"]
                       if not product or product == "all" or product_key(row.get("product")) == product_key(product)]
            return {"entries": entries, "revision": self.revision(data)}

    @staticmethod
    def source_snapshot(entry):
        return CreationStore._plan_review_source({"product": entry.get("product"), "video_id": entry.get("video_id"),
            "window_end": entry.get("window_end"), "reviewed_at": entry.get("at"), "note": entry.get("note")})

    def validate_source(self, value, product):
        """New work may freeze a reviewed conclusion; old frozen origins never change."""
        source = CreationStore._plan_review_source(value)
        if source["product"] != product_key(product):
            raise CreationError(409, "复盘来源与当前产品不一致。")
        entry = next((row for row in self.read()["entries"] if self.key(row) == self.key(source)), None)
        if not entry or entry.get("state") != "reviewed":
            raise CreationError(409, "请先登记这条视频在此观察窗的复盘结论，再安排下一轮。")
        if self.source_snapshot(entry) != source:
            raise CreationError(409, "复盘结论已更新，请重新读取后再安排下一轮。")
        return copy.deepcopy(source)

    @staticmethod
    def key(video):
        return [product_key(video.get("product")), video.get("video_id"), video.get("window_end")]

    @staticmethod
    def _reference_value(value):
        if not isinstance(value, str) or value not in REFERENCE_VALUES:
            raise CreationError(400, "参考价值分类无效。")
        return value

    def _finish(self, data, body, entry):
        result = {"entry": copy.deepcopy(entry), "revision": self.revision(data)}
        data["requests"][body["request_id"]] = {"hash": fingerprint(["followup", body]), "result": copy.deepcopy(result), "at": now()}
        self._prune_requests(data)
        self.write(data)
        return result

    def _classify(self, data, body):
        # Classification is metadata on an existing conclusion. It must not change
        # the reviewed-at identity used by frozen plans, lessons, or the queue.
        allowed = {"request_id", "revision", "product", "video_id", "window_end", "classification_only", "reference_value"}
        if set(body) - allowed or "reference_value" not in body:
            raise CreationError(400, "仅修改分类时请只提供视频来源和参考价值。")
        value = self._reference_value(body["reference_value"])
        old = next((entry for entry in data["entries"] if self.key(entry) == self.key(body)), None)
        if not old or old.get("state") != "reviewed":
            raise CreationError(409, "请先完成人工复盘，再保存参考价值分类。")
        entry = copy.deepcopy(old)
        if value != old.get("reference_value", ""):
            stamp = now()
            entry.update(reference_value=value, classified_at=stamp)
            event = {key: copy.deepcopy(val) for key, val in entry.items() if key not in {"product", "video_id", "window_end", "history"}}
            event.update(kind="classification", reviewed_at=old["at"], at=stamp)
            entry["history"] = [*old.get("history", []), event]
            data["entries"][data["entries"].index(old)] = entry
        return self._finish(data, body, entry)

    def save(self, body, video):
        if not isinstance(body, dict) or not isinstance(body.get("request_id"), str) or not re.fullmatch(r"[\w-]{10,100}", body["request_id"]):
            raise CreationError(400, "缺少保存标识，请刷新后重试。")
        with self.lock:
            prior = self.replay("followup", body)
            if prior:
                return prior
            data = self.read()
            if body.get("revision") != self.revision(data):
                raise CreationError(409, "复盘记录已更新，请重新读取后保存。")
            if "classification_only" in body and type(body["classification_only"]) is not bool:
                raise CreationError(400, "分类保存方式无效。")
            if body.get("classification_only") is True:
                return self._classify(data, body)
            if {"outcome", "evidence", "next_review_on", "observation_reason"} & body.keys():
                raise CreationError(400, "这些复盘操作已移除，请刷新页面后保存结论、参考价值和下一步动作。")
            if not video or not video.get("window_end"):
                raise CreationError(404, "尚未找到这条视频的观察窗，请先同步视频数据。")
            if self.key(body) != self.key(video):
                raise CreationError(409, "视频所属产品或观察窗已变化，请重新读取。")
            state = body.get("state")
            if state not in {"pending", "reviewed", "deferred"}:
                raise CreationError(400, "复盘状态无效。")
            today = review_today()
            # A review may be saved before 14 days or with gaps in the data; the form warns, the store does not block.
            decision = body.get("decision", "")
            if (not isinstance(decision, str) or decision not in DECISIONS or
                    decision and state != "reviewed"):
                raise CreationError(400, "复盘决定无效。")
            old = next((entry for entry in data["entries"] if self.key(entry) == self.key(video)), None)
            # Saving the conclusion does not undo an already recorded action.
            # Historical decisions can survive edits, but cannot be submitted anew.
            if "decision" not in body and state == "reviewed" and (old or {}).get("state") == "reviewed":
                decision = old.get("decision", "")
            note = text(body.get("note", ""), "复盘结论或备注", state == "reviewed", 4000)
            deferred = text(body.get("defer_until", ""), "稍后处理日期", state == "deferred", 10) if state == "deferred" else ""
            if deferred:
                try:
                    day = date.fromisoformat(deferred)
                    if day.isoformat() != deferred or day <= today:
                        raise ValueError()
                except ValueError as exc:
                    raise CreationError(400, "稍后处理日期须为未来的 YYYY-MM-DD。") from exc
            reference_value = self._reference_value(body.get("reference_value", (old or {}).get("reference_value", "")))
            if state != "reviewed" and reference_value != (old or {}).get("reference_value", ""):
                raise CreationError(400, "请先完成人工复盘，再保存参考价值分类。")
            # Historical assessments remain source data; current actions cannot edit them.
            outcome = (old or {}).get("outcome", "")
            evidence = (old or {}).get("evidence", "")
            event = {"state": state, "note": note, "defer_until": deferred, "decision": decision,
                     "outcome": outcome, "evidence": evidence, "next_review_on": "",
                     "observation_reason": "", "at": now(), "reference_value": reference_value}
            if reference_value != (old or {}).get("reference_value", ""):
                event["classified_at"] = event["at"]
            elif (old or {}).get("classified_at"):
                event["classified_at"] = old["classified_at"]
            entry = {"product": product_key(video["product"]), "video_id": video["video_id"], "window_end": video["window_end"],
                     **event, "history": [*(old or {}).get("history", []), copy.deepcopy(event)]}
            if old:
                data["entries"][data["entries"].index(old)] = entry
            else:
                data["entries"].append(entry)
            return self._finish(data, body, entry)
