"""The editor's shot list, checked before it places any footage.

Before: "least-used footage first" lived only in the handoff, and whether an edit kept to it showed after the cut. Of
499 shots in 24 edits, 15 used footage that had been used although a clip with the same label, used less and long
enough, was in the list; undoing that means cutting again. Now the editor writes picks.json once its shots are decided,
and its first placing call runs only when every used clip it picked either has no such alternative or carries the
reason why the alternatives do not fit.

The check never looks at pictures. "Same kind" is the same label apart from the clip code, in the same grinding stage;
anything looser would refuse shots for alternatives that show something else.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from edit_rhythm import RHYTHM_CONTEXT_FILE, check_plan

FILE = "picks.json"
# Refusals before the edit is let through with the open points on record: a check that cannot be satisfied must not
# cost the whole edit.
LIMIT = 3
SHOWN = 6
CODE = re.compile(r"[A-Za-z]+\d+[a-z]?")
EXAMPLE = '[{"group": 1, "key": "候选编号", "frames": 45, "shot_size": "近景", "reason": ""}]'
MISSING = ("放置前核对：当前目录还没有可读的 picks.json，本次调用没有执行。镜头全部定好后先写 picks.json"
           "（格式见交接「放置前核对」：" + EXAMPLE + "），再放置。")


def kind(row):
    """What the clip shows, without its own code: 「研磨 80目阶段推定 SOG026」 and 「… SOG035」 are one kind."""
    return " ".join(CODE.sub(" ", str(row.get("label") or "")).split())


def _same_kind(row, other):
    return bool(kind(row)) and kind(row) == kind(other) and (row.get("stage") or None) == (other.get("stage") or None)


def _code(row):
    return str(row.get("key") or "").split("-")[0]


def _times(count):
    return f"已用 {count} 次" if count else "未用过"


def read_picks(folder):
    """The listed shots, or None when the file is missing or is not a list of shots."""
    try:
        data = json.loads((Path(folder) / FILE).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    rows = data.get("picks") if isinstance(data, dict) else data
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None
    return rows


def _read_rhythm_context(folder):
    path = Path(folder) / RHYTHM_CONTEXT_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("fps") else None


def check(folder):
    """{"ok", "message", "issues", "picks", "used", "reasons"} for the shot list in the edit's folder.

    A pick is open when it has no reason and either a same-kind clip used less, long enough and not picked itself is
    listed, or the clip was already used three times or more. When rhythm-context.json is present, also checks
    cut-to-word timing, adjacent same-kind repeats, consecutive same shot_size, and slow single-shot openings.
    An empty list passes: the call places no footage."""
    data = json.loads((Path(folder) / "candidates.json").read_text(encoding="utf-8"))
    candidates = [row for row in data.get("candidates") or [] if isinstance(row, dict) and row.get("key")]
    rhythm = _read_rhythm_context(folder)
    result = {"ok": True, "message": "", "issues": [], "picks": 0, "used": 0, "reasons": []}
    if not data.get("usage_counted") and not rhythm:
        return result
    picks = read_picks(folder)
    if picks is None:
        return {**result, "ok": False, "message": MISSING, "issues": [{"key": "", "problem": "missing"}]}
    by_key = {row["key"]: row for row in candidates}
    by_code = {}
    for row in candidates:
        by_code.setdefault(_code(row), set()).add(row["key"])
    found = []
    for pick in picks:
        name = str(pick.get("key") or "").strip()
        keys = by_code.get(name) or set()
        row = by_key.get(name) or (by_key[next(iter(keys))] if len(keys) == 1 else None)
        found.append((pick, name, row))
    chosen = {row["key"] for _, _, row in found if row}
    lines = []
    if data.get("usage_counted"):
        for pick, name, row in found:
            if row is None:
                result["issues"].append({"key": name, "problem": "unknown"})
                lines.append(f"- {name or '（没写候选编号）'}：candidates.md 里没有这个候选编号，按清单里的编号写。")
                continue
            count, reason = int(row.get("use_count") or 0), str(pick.get("reason") or "").strip()
            result["used"] += bool(count)
            if not count:
                continue
            if reason:
                result["reasons"].append({"key": row["key"], "use_count": count, "reason": reason[:300]})
                continue
            need = pick.get("frames") if isinstance(pick.get("frames"), int) and not isinstance(pick.get("frames"), bool) else 0
            others = {}
            for other in candidates:
                if (other["key"] not in chosen and int(other.get("use_count") or 0) < count
                        and int(other.get("frames") or 0) >= max(need, 1) and _same_kind(row, other)):
                    others.setdefault(other["key"], other)
            if others:
                ranked = sorted(others.values(), key=lambda other: (int(other.get("use_count") or 0), -int(other.get("frames") or 0)))
                result["issues"].append({"key": row["key"], "problem": "less_used", "use_count": count,
                                         "alternatives": [other["key"] for other in ranked]})
                shown = "、".join(f"{_code(other)}（{_times(int(other.get('use_count') or 0))}，{other.get('frames')} 帧）" for other in ranked[:SHOWN])
                lines.append(f"- {_code(row)}「{kind(row)}」（{_times(count)}{f'，要 {need} 帧' if need else ''}）：同标签更少用的有 {shown}"
                             f"{f' 等 {len(ranked)} 个' if len(ranked) > SHOWN else ''}。")
            elif count >= 3:
                result["issues"].append({"key": row["key"], "problem": "often_used", "use_count": count})
                lines.append(f"- {_code(row)}「{kind(row)}」（{_times(count)}）：用过 3 次及以上，在这一条写 reason，说明为什么非用它不可。")
    if rhythm and picks:
        rhythm_issues, rhythm_lines = check_plan(
            [(pick, row) for pick, _, row in found], by_key,
            rhythm.get("lines") or [], rhythm.get("spans") or [],
            rhythm.get("fps"), lead=int(rhythm.get("lead") or 0))
        result["issues"].extend(rhythm_issues)
        lines.extend(rhythm_lines)
    result["picks"] = len(picks)
    if lines:
        result.update(ok=False, message="\n".join([
            "放置前核对没通过，本次调用没有执行。picks.json 里这些镜头要先处理：", *lines,
            "处理办法：按提示调整镜头选择、切点帧数或景别；确实需要保留的，在 picks.json 这一条的 reason 写明原因"
            "（如“是另一把刀”“接上一个镜头的延续画面”“唯一的号召手势”“刻意一镜到底展示完整动作”）。改好 picks.json 再放置。"]))
    return result


class PickCheck:
    """One edit's check, asked by its MCP gate before each placing call; returns why the call may not run, or ""."""

    def __init__(self, folder, limit=LIMIT):
        self.folder, self.limit = Path(folder), limit
        self.calls, self.refused, self.waived, self.error, self.last = 0, 0, False, "", None

    def __call__(self):
        self.calls += 1
        try:
            self.last = check(self.folder)
        except Exception as exc:  # A check that cannot run never blocks the edit.
            self.error = str(exc)[:300] or type(exc).__name__
            return ""
        if self.last["ok"]:
            self.waived = False
            return ""
        if self.refused >= self.limit:
            self.waived = True
            return ""
        self.refused += 1
        return self.last["message"]

    def summary(self):
        last = self.last or {}
        return {"calls": self.calls, "refused": self.refused, "waived": self.waived, "picks": last.get("picks", 0),
                "used": last.get("used", 0), "reasons": last.get("reasons", []),
                "open": last.get("issues", []) if self.waived else [], "error": self.error}


def waived_question(summary):
    """What the user is told when footage went onto the timeline although the check still had open points."""
    if not (summary or {}).get("waived"):
        return ""
    keys = "、".join(dict.fromkeys(str(item.get("key") or "").split("-")[0] for item in summary.get("open") or [] if item.get("key")))
    return ("放置前的少用优先核对没有通过就放了素材" + (f"（{keys}）" if keys else "（没有写镜头清单）")
            + "：这些镜头有更少用的同标签片段可换，或用过 3 次以上却没写原因，请看一下是否要换。")
