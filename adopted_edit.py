"""采用稿逐句改：在采用稿详情页把口播和对应中文逐句改，保存成同一组的新版本。

和写稿单「改稿」同一套规则（见 manual_edit.py）：改了中文没动口播的句子交给 AI 按整条稿的上下文重译，
其余句子照写的存。新版本沿用原稿的来源；原稿的口播和来源候选的那条稿逐行一致时，来源候选里这条稿的
口播、对应中文和意群表一并改写（改过的句子画面要求标「待更新」，新句标「待补」），制作单就能接着用
没动的句子的画面要求。已有制作单继续用原来那一版。
"""
from __future__ import annotations

import copy
import threading
from pathlib import Path

import manual_edit as editing
import markets
from creation_store import CreationError
from variant_parse import parse_variants, same_lines


def lines_of(script):
    """The adopted copy's sentences: one spoken line each, with its Chinese when the two columns pair up."""
    spoken = [line.strip() for line in str(script.get("text") or "").splitlines() if line.strip()]
    chinese = [line.strip() for line in str(script.get("translation_zh") or "").splitlines() if line.strip()]
    paired = len(chinese) == len(spoken)
    return [{"spoken": line, "zh": chinese[index] if paired else ""} for index, line in enumerate(spoken)]


def prepare(original, rows):
    """Check the edited rows against the version they came from; the record has manual_edit's shape."""
    if not original:
        raise CreationError(400, "这版采用稿没有可逐句改的口播。")
    if not isinstance(rows, list) or not rows:
        raise CreationError(400, "采用稿至少要留一句。")
    if len(rows) > editing.MAX_LINES:
        raise CreationError(400, f"一条稿最多 {editing.MAX_LINES} 句。")
    seen, lines = set(), []
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise CreationError(400, "改稿内容格式无效。")
        source = row.get("source")
        if source is not None and (type(source) is not int or not 1 <= source <= len(original) or source in seen):
            raise CreationError(400, "改稿对应的原句编号无效，请刷新后重试。")
        seen.add(source)
        spoken, zh = editing._one_line(row.get("spoken")), editing._one_line(row.get("zh"))
        translate = row.get("translate") is True
        if len(spoken) > editing.MAX_CHARS or len(zh) > editing.MAX_CHARS:
            raise CreationError(400, f"第 {number} 句超过 {editing.MAX_CHARS} 字符，请拆成两句。")
        if not zh:
            raise CreationError(400, f"第 {number} 句还没有中文。")
        if not translate and not spoken:
            raise CreationError(400, f"第 {number} 句还没有口播，填上或让它重译。")
        before = original[source - 1] if source else None
        line = {"source": source, "spoken": spoken, "zh": zh, "translate": translate}
        if before and (translate or before["spoken"] != spoken or before["zh"] != zh):
            line["before"] = dict(before)
        try:
            proof = editing.proof_of(row)
        except editing.EditError as exc:
            raise CreationError(400, str(exc)) from exc
        if proof:
            line["proof"] = proof
        lines.append(line)
    removed = [{"number": number, **line} for number, line in enumerate(original, 1) if number not in seen]
    moved = [line["source"] for line in lines] != list(range(1, len(original) + 1))
    if not moved and not removed and not any("before" in line or "proof" in line for line in lines):
        raise CreationError(400, "没有改动，不需要保存。")
    return {"lines": lines, "removed": removed}


def rebuild_candidate(source, original, record):
    """The source candidate with this draft's blocks rewritten, or None when they cannot follow the edit."""
    candidate, index = (source or {}).get("candidate_text"), (source or {}).get("variant_index")
    if not candidate or type(index) is not int:
        return None
    strict = parse_variants(candidate, loose=False)
    position = next((place for place, item in enumerate(strict) if item["index"] == index), None)
    if position is None:
        return None
    chosen = strict[position]["lines"]
    if len(chosen) != len(original) or not same_lines([line["spoken"] for line in chosen], [line["spoken"] for line in original]):
        return None
    numbers = [line["number"] for line in chosen]
    mapped = {**record, "variant_index": position,
              "lines": [{**line, "source": numbers[line["source"] - 1] if line["source"] else None} for line in record["lines"]]}
    try:
        return editing.build(candidate, mapped)
    except Exception:  # A layout build cannot follow keeps the parent's source untouched.
        return None


def summary(record):
    changed = sum(1 for line in record["lines"] if "before" in line)
    added = sum(1 for line in record["lines"] if not line["source"])
    translated = sum(1 for line in record["lines"] if line.get("translated"))
    pictures = sum(1 for line in record["lines"] if line.get("proof"))
    parts = [f"改 {changed} 句" if changed else "", f"加 {added} 句" if added else "",
             f"删 {len(record['removed'])} 句" if record["removed"] else "", f"其中 {translated} 句按中文重译" if translated else "",
             f"换 {pictures} 条画面" if pictures else ""]
    return "逐句改稿：" + ("，".join(part for part in parts if part) or "调了顺序")


class AdoptedEditMixin:
    """Needs self.lock, self.creation, self.ai and CreationWorkbenchMixin's helpers."""

    def _adopted_edit_skills(self, task):
        rounds = [turn for turn in (task or {}).get("rounds") or [] if turn.get("skills")]
        skills = rounds[-1]["skills"] if rounds else []
        # A skill moved or deleted since the draft was written is left out rather than failing the edit.
        return [{"name": item["name"], "path": item["path"]} for item in skills
                if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("path"), str)
                and Path(item["path"]).is_file()]

    def edit_script_lines(self, identifier, body):
        if not isinstance(body, dict):
            raise CreationError(400, "保存参数格式无效。")
        key = body.get("request_id")
        with self.lock:
            prior = self.creation._read_catalog()["requests"].get(key) if isinstance(key, str) else None
            if prior:
                result = copy.deepcopy(prior["result"])
                return {**result, "script": self._script_alias(result["script"]), "catalog": self.creation_catalog()}
            parent = self.creation.get_script(identifier)
            original = lines_of(parent)
            record = prepare(original, body.get("lines"))
            title = body.get("title", parent.get("title"))
            source = copy.deepcopy(parent.get("source"))
        waiting = editing.pending(record)
        if waiting:
            self.ai._available()
            task = None
            if (source or {}).get("task_id"):
                try:
                    task = self.ai.get(source["task_id"])
                except Exception:
                    task = None
            market = markets.stored(((task or {}).get("context") or {}).get("market")) if task else markets.DEFAULT
            settings = self.ai.preferences(kind="writing")
            try:
                output = self.ai.bridge.run(editing.prompt(record, market), cwd=str(self.ai.runtime),
                                            cancel_event=threading.Event(), on_progress=lambda message: None,
                                            model=settings.get("model"), reasoning_effort=settings.get("reasoning_effort"),
                                            skills=self._adopted_edit_skills(task))
            except Exception as exc:
                raise CreationError(502, f"AI 重译没有完成：{exc}。改动还在页面上，可以再保存一次，或直接改口播。") from exc
            try:
                record = editing.answer(record, output)
            except editing.EditError as exc:
                raise CreationError(422, str(exc)) from exc
        with self.lock:
            rebuilt = rebuild_candidate(source, original, record)
            if rebuilt is not None:
                source = {**source, "candidate_text": rebuilt}
            request = {"request_id": key, "revision": self.creation.public()["revision"],
                       "script": {"product": parent["product"], "parent_id": parent["id"], "confirmed": True,
                                  "title": title, "direction_id": parent.get("direction_id", ""),
                                  "text": "\n".join(line["spoken"] for line in record["lines"]),
                                  "translation_zh": "\n".join(line["zh"] for line in record["lines"])}}
            result = self.creation.mutate("script", request, source if rebuilt is not None else None)
            result["edit"] = {"note": summary(record), "table_kept": rebuilt is not None,
                              "translated": [number for number, line in enumerate(record["lines"], 1) if line.get("translated")]}
            self._link_script_plan(result["script"])
            result["script"] = self._script_alias(result["script"])
            self.state_cache = None
            return {**result, "catalog": self.creation_catalog()}
