"""AI tag suggestions for the reviews the rules left untagged (2026-11-15, 用户声音第 8 条).

About a third of the library carries neither a 性质 nor a 主题 tag, so topic opportunities and quick filters never
see it, and fixing tags by hand never happened. This asks the model, a batch of reviews at a time, to pick tags
from the existing taxonomy only. Nothing is written to the library until a person accepts a suggestion; an
accepted tag goes through ReviewStore.set_tag, the same as a hand edit, so it survives every rebuild.

Suggestions live in reviews/tag-suggestions.json. A run works in one background thread, one batch per model call.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading

from review_store import TAG_KINDS

BATCH_SIZE = 40
# One run covers at most this many reviews; the button offers the rest next time.
MAX_REVIEWS = 600
TEXT_LIMIT = 600
KINDS = ("nature", "topic")
KIND = "tagging"


class TagSuggestError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(text):
    return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()[:16]


def _text(value, limit=300):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def parse_items(output):
    """The model's answer as {review id: {"nature": [...], "topic": [...]}}, keeping only codes the taxonomy has."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(output or "").strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise TagSuggestError(502, "AI 没有交回标签建议。")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise TagSuggestError(502, "AI 交回的标签建议格式不对。") from None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise TagSuggestError(502, "AI 交回的标签建议格式不对。")
    result = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        tags = {}
        for kind in KINDS:
            values = item.get(kind) if isinstance(item.get(kind), list) else []
            tags[kind] = sorted({value for value in values if isinstance(value, str) and value in TAG_KINDS[kind]})
        if any(tags.values()):
            result[item["id"]] = tags
    return result


def prompt(rows):
    """The batch prompt: the two tag lists with their meaning, then the reviews as JSON lines."""
    lists = "\n".join(f"- {kind}（{'性质' if kind == 'nature' else '主题'}）：" + "；".join(f"{code}={label}" for code, label in TAG_KINDS[kind].items())
                      for kind in KINDS)
    reviews = "\n".join(json.dumps({"id": row["id"], "source": row["platform"], "role": row["role"], "text": _text(row["text"], TEXT_LIMIT)},
                                   ensure_ascii=False) for row in rows)
    return f"""你在给磨刀器带货团队的评论库补标签。下面这些评论，规则一个标签都没打上。

只能从这两组标签里选，代码原样照抄，不能造新代码：
{lists}

要求：
1. 每条评论按它真正说了什么来选：性质最多 2 个，主题最多 3 个；没有合适的就留空数组，不要硬凑。
2. 玩笑、表情、和产品无关的闲聊，性质选 noise（无信息），主题留空。
3. 评论里的说法不需要你核实，只判断它在说什么。
4. 只交回 JSON，不要别的文字，格式：{{"items":[{{"id":"评论 id","nature":["..."],"topic":["..."]}}]}}

评论（每行一条 JSON）：
{reviews}
"""


class ReviewTagSuggestions:
    def __init__(self, app, root):
        self.app = app
        self.path = Path(root) / "tag-suggestions.json"
        self.lock = threading.RLock()
        self.worker = None
        self.cancel = threading.Event()
        with self.lock:
            data = self._load()
            # A restart ends a run in flight; what it already suggested stays.
            if data["run"].get("status") in {"queued", "running"}:
                data["run"].update(status="failed", error="工作台重启，补标签中断了，请再点一次。", finished_at=_now())
                data["run"].pop("progress", None)
                self._save(data)

    def busy(self):
        with self.lock:
            return bool(self.worker and self.worker.is_alive())

    # ---------- storage ----------
    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("suggestions"), dict):
                data.setdefault("run", {})
                data.setdefault("dismissed", {})
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "run": {}, "suggestions": {}, "dismissed": {}}

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, self.path)

    def _run_update(self, **fields):
        with self.lock:
            data = self._load()
            data["run"].update(fields)
            for key in [key for key, value in fields.items() if value is None]:
                data["run"].pop(key, None)
            self._save(data)

    # ---------- reading ----------
    def _untagged(self):
        """Reviews with neither a 性质 nor a 主题 tag, as the page shows them (brand replies and star-only reviews left out)."""
        return self.app.reviews.search({"nature": ["none"], "topic": ["none"]}, limit=2000, sort="likes")["reviews"]

    def status(self):
        untagged = {row["id"]: row for row in self._untagged()}
        with self.lock:
            data = self._load()
        labels = {kind: TAG_KINDS[kind] for kind in KINDS}
        pending = []
        for identifier, entry in data["suggestions"].items():
            row = untagged.get(identifier)
            # A review tagged since, or whose text changed, no longer needs this suggestion.
            if not row or entry.get("text") != _digest(row["text"]):
                continue
            pending.append({"id": identifier, "text": row["text"], "platform": row["platform"], "role": row["role"], "product": row.get("product") or "",
                            "likes": row.get("likes"), "nature": entry.get("nature", []), "topic": entry.get("topic", [])})
        pending.sort(key=lambda item: -(item["likes"] or 0))
        handled = set(data["suggestions"]) | set(data["dismissed"])
        remaining = sum(1 for identifier in untagged if identifier not in handled)
        run = {key: data["run"].get(key) for key in ("status", "progress", "error", "done", "total", "suggested", "started_at", "finished_at", "model")
               if data["run"].get(key) is not None}
        return {"untagged": len(untagged), "remaining": remaining, "pending": pending, "run": run, "labels": labels,
                "running": run.get("status") in {"queued", "running"}}

    # ---------- running ----------
    def start(self, body):
        body = body if isinstance(body, dict) else {}
        settings = self._model_settings(body)
        with self.lock:
            if self.busy():
                raise TagSuggestError(409, "AI 正在补标签，请等这一轮完成。")
            data = self._load()
            handled = set(data["suggestions"]) | set(data["dismissed"])
            rows = [row for row in self._untagged() if row["id"] not in handled and row["text"].strip()][:MAX_REVIEWS]
            if not rows:
                raise TagSuggestError(400, "没有待补标签的评论。")
            data["run"] = {"status": "queued", "done": 0, "total": len(rows), "suggested": 0, "started_at": _now(),
                           "model": settings.get("model"), "reasoning_effort": settings.get("reasoning_effort")}
            self._save(data)
            self.cancel.clear()
            self.worker = threading.Thread(target=self._drain, args=(rows, settings), daemon=True, name="review-tag-suggestions")
            self.worker.start()
        return self.status()

    def _model_settings(self, body):
        picked = {"model": body.get("model"), "reasoning_effort": body.get("reasoning_effort")} if "model" in body else {}
        try:
            return self.app.ai.resolve_default_settings(picked, kind=KIND)
        except Exception as exc:
            if picked:
                raise TagSuggestError(400, str(exc)[:200] or "模型选择无效。")
            return {"model": None, "reasoning_effort": None}

    def stop(self):
        self.cancel.set()
        return self.status()

    def _drain(self, rows, settings):
        folder = self.path.parent.resolve()
        done = suggested = 0
        try:
            for start in range(0, len(rows), BATCH_SIZE):
                if self.cancel.is_set():
                    break
                batch = rows[start:start + BATCH_SIZE]
                self._run_update(status="running", progress=f"第 {start // BATCH_SIZE + 1} 批，共 {(len(rows) + BATCH_SIZE - 1) // BATCH_SIZE} 批")
                output = self.app.ai.bridge.run(prompt(batch), cwd=str(folder), cancel_event=self.cancel,
                                                on_progress=lambda text: None, model=settings.get("model"),
                                                reasoning_effort=settings.get("reasoning_effort"))
                if self.cancel.is_set():
                    break
                found = parse_items(output)
                texts = {row["id"]: row["text"] for row in batch}
                with self.lock:
                    data = self._load()
                    for identifier, tags in found.items():
                        if identifier in texts:  # an id the model made up is ignored
                            data["suggestions"][identifier] = {**tags, "text": _digest(texts[identifier]), "created_at": _now()}
                            suggested += 1
                    done += len(batch)
                    data["run"].update(done=done, suggested=suggested)
                    self._save(data)
            stopped = self.cancel.is_set()
            self._run_update(status="cancelled" if stopped else "done", progress=None, finished_at=_now(),
                             error="已停止。" if stopped else None)
        except Exception as exc:
            stopped = self.cancel.is_set()
            self._run_update(status="cancelled" if stopped else "failed", progress=None, finished_at=_now(),
                             error="已停止。" if stopped else (_text(exc, 400) or "AI 没有完成，请再点一次。"))

    # ---------- deciding ----------
    def accept(self, body):
        """Write the accepted tags as hand edits. items: [{"id", "nature": [...], "topic": [...]}]; a person may have
        dropped some of the suggested tags first, but never adds codes outside the taxonomy."""
        items = (body or {}).get("items") if isinstance(body, dict) else None
        if not isinstance(items, list) or not items:
            raise TagSuggestError(400, "选择要接受的建议。")
        accepted = 0
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise TagSuggestError(400, "建议格式不对。")
            for kind in KINDS:
                for tag in item.get(kind) or []:
                    if tag not in TAG_KINDS[kind]:
                        raise TagSuggestError(400, f"未知标签：{kind}/{tag}")
        for item in items:
            for kind in KINDS:
                for tag in item.get(kind) or []:
                    self.app.reviews.set_tag(item["id"], kind, tag, True)
            accepted += 1
            with self.lock:
                data = self._load()
                data["suggestions"].pop(item["id"], None)
                self._save(data)
        return {**self.status(), "accepted": accepted}

    def dismiss(self, body):
        """Leave these reviews untagged and do not suggest for them again while their text stays the same."""
        ids = (body or {}).get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids) or not ids:
            raise TagSuggestError(400, "选择不采用的建议。")
        with self.lock:
            data = self._load()
            for identifier in ids:
                entry = data["suggestions"].pop(identifier, None)
                if entry:
                    data["dismissed"][identifier] = entry.get("text")
            self._save(data)
        return self.status()
