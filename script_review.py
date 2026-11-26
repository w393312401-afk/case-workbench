"""口播稿审查：按产品专属审稿 skill（如 sharpal-202h-script-review）对候选稿、采用稿、制作单做单稿深审。

审稿结果按目标键（candidate / script / job）持久化存放在运行目录 `script-reviews/reviews.json`。
当前已配置 202H、198H 专属审稿技能；没有对应专属技能的产品不会暴露审稿入口，后续新增产品技能只需在
PRODUCT_SKILLS 中登记即可自动接入。
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from uuid import uuid4

import markets
from direction_store import product_key
from task_activity import activity_label, record_activity
from variant_parse import parse_variants


APP_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = APP_ROOT / ".agents" / "skills"
PRODUCT_SKILLS = {"202H": "sharpal-202h-script-review", "198H": "sharpal-198h-script-review"}
MAX_TARGETS = 500
MAX_HISTORY = 20
MAX_PARALLEL = 4
MAX_REFERENCE_CHARS = 140_000
# Progress lines from the model arrive many times a minute; the task panel keeps one every so often.
ACTIVITY_PROGRESS_SECONDS = 10


class ScriptReviewError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value, limit=300):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _hash(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]


def _product_skill_name(product):
    key = product_key(product)
    return PRODUCT_SKILLS.get(key) if key else None


def skill_info(product, *, skill_data_root=None, skill_root=None):
    """Return skill descriptor and reference texts for `product`, or None if unsupported."""
    name = _product_skill_name(product)
    if not name:
        return None
    root = Path(skill_root) if skill_root else SKILL_ROOT
    folder = root / name
    skill_md = folder / "SKILL.md"
    if not skill_md.is_file():
        return None
    refs = []
    refs_dir = folder / "references"
    if refs_dir.is_dir():
        for path in sorted(refs_dir.glob("*.md")):
            try:
                refs.append({"label": f"{name}/{path.name}", "path": str(path.resolve()),
                             "text": path.read_text(encoding="utf-8-sig")})
            except OSError:
                continue
    # 口径时效优先：若工作台产品资料存在，一并附为最新口径依据。
    if skill_data_root:
        key = product_key(product)
        prod_dir = Path(skill_data_root) / "references" / "products"
        for filename in (f"{key}.md", f"{key}-selling-point-grading.md", f"{key}_universal_stone_holder.md"):
            candidate = prod_dir / filename
            if candidate.is_file():
                try:
                    text = candidate.read_text(encoding="utf-8-sig")
                except OSError:
                    continue
                if sum(len(item["text"]) for item in refs) + len(text) <= MAX_REFERENCE_CHARS:
                    refs.append({"label": f"工作台最新产品文档/{filename}", "path": str(candidate.resolve()), "text": text})
    return {
        "name": name,
        "path": str(skill_md.resolve()),
        "references": refs,
    }


def make_target_key(kind, **ids):
    if kind == "candidate":
        task_id = str(ids.get("task_id") or "").strip()
        round_id = str(ids.get("round_id") or "").strip()
        variant_index = ids.get("variant_index")
        if not task_id or not round_id or type(variant_index) is not int or variant_index < 0:
            raise ScriptReviewError("候选稿标识不完整。")
        return f"candidate:{task_id}:{round_id}:{variant_index}"
    if kind == "script":
        script_id = str(ids.get("script_id") or "").strip()
        if not script_id:
            raise ScriptReviewError("采用稿编号不能为空。")
        return f"script:{script_id}"
    if kind == "job":
        job_id = str(ids.get("job_id") or "").strip()
        if not job_id:
            raise ScriptReviewError("制作单编号不能为空。")
        return f"job:{job_id}"
    raise ScriptReviewError("不支持的审稿目标类型。")


def _clean_md_inline(text):
    value = str(text or "").strip()
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
    value = re.sub(r"\*([^*\n]+)\*", r"\1", value)
    value = re.sub(r"</?[^>]+>", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _split_table_row(line):
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", body)]


def _is_table_divider(line):
    if "|" not in line:
        return False
    cells = _split_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells if cell)


def parse_review_result(markdown):
    """Extract structured verdict, four gates, revised English spoken lines, and key revision points."""
    text = str(markdown or "").replace("\r\n", "\n").strip()
    verdict, verdict_label = "", ""
    if "可直接拍/配" in text or "可直接拍" in text:
        verdict, verdict_label = "pass", "可直接拍/配"
    elif "建议重写切口" in text or "重写切口" in text:
        verdict, verdict_label = "rewrite", "建议重写切口"
    elif "改完关键点再发" in text:
        verdict, verdict_label = "revise", "改完关键点再发"

    # 优先以「放行结论」所在行的内容覆盖判断，避免正文里提到其他状态词造成误判。
    for line in text.splitlines():
        if "放行结论" in line:
            if "可直接拍" in line:
                verdict, verdict_label = "pass", "可直接拍/配"
            elif "重写切口" in line:
                verdict, verdict_label = "rewrite", "建议重写切口"
            elif "改完关键点" in line:
                verdict, verdict_label = "revise", "改完关键点再发"
            break

    gates = []
    revised_lines = []
    lines = text.splitlines()
    index = 0
    while index < len(lines) - 1:
        if "|" in lines[index] and _is_table_divider(lines[index + 1]):
            headers = [_clean_md_inline(cell) for cell in _split_table_row(lines[index])]
            rows = []
            cursor = index + 2
            while cursor < len(lines) and lines[cursor].strip() and "|" in lines[cursor]:
                rows.append(_split_table_row(lines[cursor]))
                cursor += 1
            header_joined = " ".join(headers)
            if not gates and "关卡" in header_joined and "状态" in header_joined:
                for row in rows:
                    if len(row) < 2:
                        continue
                    name = _clean_md_inline(row[0])
                    raw_status = _clean_md_inline(row[1])
                    status_key = "stop" if "拦停" in raw_status else "warn" if "补强" in raw_status else "pass" if "通过" in raw_status else ""
                    summary = _clean_md_inline(row[2]) if len(row) > 2 else ""
                    if name:
                        gates.append({"name": name, "status": raw_status, "key": status_key, "summary": summary})
            elif not revised_lines and ("口播" in header_joined and ("画面" in header_joined or "句序" in header_joined)):
                spoken_col = next((pos for pos, h in enumerate(headers) if "改后" in h and "中文" not in h), None)
                if spoken_col is None:
                    spoken_col = next((pos for pos, h in enumerate(headers) if "口播" in h and "原句" not in h), 1)
                for row in rows:
                    if spoken_col >= len(row):
                        continue
                    cell = row[spoken_col]
                    parts = re.split(r"<br\s*/?>|\n", cell, flags=re.I)
                    kept = []
                    for part in parts:
                        cleaned = _clean_md_inline(part)
                        if not cleaned:
                            continue
                        if re.match(r"^(?:中文|对应中文)\s*[：:]", cleaned):
                            break
                        kept.append(cleaned)
                    joined = " ".join(kept).strip()
                    if joined and not re.fullmatch(r"[（(]?删除[)）]?", joined):
                        revised_lines.append(joined)
            index = cursor
            continue
        index += 1

    # 提取第 2 节关键修改要点，供候选稿「填入继续修改框」使用。
    points = []
    in_changes = False
    current_title = ""
    for line in lines:
        heading = re.match(r"^(#{2,4})\s+(.+)$", line.strip())
        if heading:
            level, title = len(heading.group(1)), _clean_md_inline(heading.group(2))
            if any(token in title for token in ("致命红线", "优先修改", "修改清单", "改动清单")):
                in_changes = True
                current_title = ""
                continue
            if in_changes and level <= 4 and any(token in title for token in ("逐句口播", "对应画面", "核对范围", "逐句采纳", "待核实", "拍摄细节")):
                in_changes = False
                continue
            if in_changes:
                current_title = title
                points.append(title)
                continue
        if in_changes:
            stripped = line.strip()
            if re.match(r"^[-*]\s*\*{0,2}(?:【|最小改法|英文|问题与依据)", stripped):
                cleaned = _clean_md_inline(re.sub(r"^[-*]\s*", "", stripped))
                if cleaned and (not current_title or cleaned != current_title):
                    points.append(f"  {cleaned}")
            elif re.match(r"^\d+[.)]\s+", stripped):
                points.append(_clean_md_inline(stripped))

    return {
        "verdict": verdict,
        "verdict_label": verdict_label,
        "gates": gates,
        "revised_spoken": "\n".join(revised_lines),
        "revision_points": "\n".join(points[:24]).strip(),
        **parse_adoption(text),
    }


CHANGE_KINDS = ("未改", "必改", "建议", "画面", "新增", "删除", "合并")
SPOKEN_KINDS = {"必改", "建议", "新增", "删除", "合并"}
_SAME = re.compile(r"^[（(]?(?:同原句|同上|未改|无|—|-)[)）]?$")
_DELETED = re.compile(r"^[（(]?删除[)）]?$")


def _tables(text):
    """Every markdown table in `text`: (cleaned headers, raw rows)."""
    lines = text.splitlines()
    found, index = [], 0
    while index < len(lines) - 1:
        if "|" in lines[index] and _is_table_divider(lines[index + 1]):
            headers = [_clean_md_inline(cell) for cell in _split_table_row(lines[index])]
            rows, cursor = [], index + 2
            while cursor < len(lines) and lines[cursor].strip() and "|" in lines[cursor]:
                rows.append(_split_table_row(lines[cursor]))
                cursor += 1
            found.append((headers, rows))
            index = cursor
            continue
        index += 1
    return found


def _col(headers, test):
    return next((pos for pos, name in enumerate(headers) if test(name)), None)


def _cell(row, pos):
    return row[pos] if pos is not None and pos < len(row) else ""


def _spoken_and_zh(cell):
    """A cell may hold the spoken words and, after a <br>, `中文：...`; keep them apart."""
    spoken, zh = [], []
    target = spoken
    for part in re.split(r"<br\s*/?>|\n", str(cell or ""), flags=re.I):
        cleaned = _clean_md_inline(part)
        if not cleaned:
            continue
        match = re.match(r"^(?:中文|对应中文)\s*[：:]\s*(.*)$", cleaned)
        if match:
            target = zh
            cleaned = match.group(1).strip()
            if not cleaned:
                continue
        elif target is zh and re.match(r"^[（(]", cleaned):
            break  # a bracketed note after the Chinese, e.g. an alternative line, is not part of this line
        target.append(cleaned)
    return " ".join(spoken).strip(), " ".join(zh).strip()


def _sentence_numbers(cell):
    """`12（收口）` -> ([12], False); `5+6` -> ([5, 6], False); `7a` -> ([7], True)."""
    raw = _clean_md_inline(cell)
    match = re.match(r"^\s*(\d+)\s*([a-zA-Z])?((?:\s*[+＋、,，]\s*\d+)*)", raw)
    if not match:
        return [], False
    numbers = [int(match.group(1))] + [int(n) for n in re.findall(r"\d+", match.group(3) or "")]
    return numbers, bool(match.group(2))


_CHANGE_RANK = {"删除": 5, "必改": 4, "建议": 3, "合并": 3, "画面": 2, "未改": 0}


def _merge_split_rows(rows):
    """One original sentence written as several rows (`2.1`/`2.2`, or the same 句序 twice) becomes one row again.

    Models often split a long sentence to give each half its own picture; the script keeps one line per sentence,
    so the halves are joined: words with a space, Chinese without, pictures with 「；」, and the strongest change."""
    merged = []
    for row in rows:
        last = merged[-1] if merged else None
        same = (last is not None and len(row["sources"]) == 1 and last["sources"] == row["sources"]
                and row["change"] != "新增" and last["change"] != "新增")
        if not same:
            merged.append({**row, "_parts": [row]})
            continue
        last["_parts"].append(row)
    out = []
    for row in merged:
        parts = row.pop("_parts")
        if len(parts) == 1:
            out.append(row)
            continue
        kept = [part for part in parts if part["change"] != "删除"]
        change = max((part["change"] for part in parts), key=lambda kind: _CHANGE_RANK.get(kind, 1))
        if kept and change == "删除":
            change = "必改"  # only part of the sentence goes: the rest is a rewrite
        refs = []
        for part in parts:
            refs += [ref for ref in part.get("refs") or [] if ref not in refs]
        out.append({**row, "no": str(row["sources"][0]), "change": change,
                    "original": " ".join(part["original"] or part["spoken"] for part in parts if part["original"] or part["spoken"]).strip()
                    if any(part["original"] for part in parts) else "",
                    "spoken": " ".join(part["spoken"] for part in kept if part["spoken"]).strip(),
                    "zh": "".join(part["zh"] for part in kept if part["zh"]).strip(),
                    "proof": "；".join(part["proof"] for part in kept if part["proof"])[:240],
                    "refs": refs})
    return out


def parse_adoption(markdown):
    """The parts of a report the workbench adopts line by line: 改动清单, 逐句采纳表 and 待核实备选.

    `adopt_format` is "rows" for the 逐句采纳表 (原句 next to 改后), "legacy" for the older 改动后逐句口播 table that
    only lists the revised lines, or "" when neither is there."""
    text = str(markdown or "").replace("\r\n", "\n")
    changes, rows, alternatives, fmt = [], [], [], ""
    for headers, body in _tables(text):
        joined = " ".join(headers)
        if not changes and "编号" in joined and "类型" in joined:
            c_id, c_kind = _col(headers, lambda h: "编号" in h), _col(headers, lambda h: "类型" in h)
            c_lines, c_problem = _col(headers, lambda h: "句序" in h), _col(headers, lambda h: "问题" in h)
            c_basis = _col(headers, lambda h: "依据" in h)
            for row in body:
                ident = _clean_md_inline(_cell(row, c_id))
                if not re.fullmatch(r"R\d+", ident, re.I):
                    continue
                changes.append({"id": ident.upper(), "kind": _clean_md_inline(_cell(row, c_kind)),
                                "lines": _clean_md_inline(_cell(row, c_lines)),
                                "problem": _clean_md_inline(_cell(row, c_problem)),
                                "basis": _clean_md_inline(_cell(row, c_basis))})
        elif fmt != "rows" and any("原句" in h for h in headers) and _col(headers, lambda h: "改后" in h and "中文" not in h) is not None:
            c_no = _col(headers, lambda h: "句序" in h or "句号" in h)
            c_change = _col(headers, lambda h: "改动" in h and "改后" not in h)
            c_orig = _col(headers, lambda h: "原句" in h)
            c_spoken = _col(headers, lambda h: "改后" in h and "中文" not in h)
            c_zh = _col(headers, lambda h: "中文" in h and h != headers[c_spoken])
            c_proof = _col(headers, lambda h: "画面" in h)
            c_refs = _col(headers, lambda h: "编号" in h)
            parsed = []
            for row in body:
                numbers, new = _sentence_numbers(_cell(row, 0 if c_no is None else c_no))
                if not numbers:
                    continue
                spoken, zh = _spoken_and_zh(_cell(row, c_spoken))
                if c_zh is not None:
                    zh = _clean_md_inline(_cell(row, c_zh)) or zh
                change = _clean_md_inline(_cell(row, c_change))
                change = next((kind for kind in CHANGE_KINDS if kind in change), change)
                original = _clean_md_inline(_cell(row, c_orig))
                parsed.append({
                    "no": _clean_md_inline(_cell(row, 0 if c_no is None else c_no)).split("（")[0].split("(")[0].strip(),
                    "sources": [] if new else numbers, "after": numbers[0] if new else None,
                    "change": "新增" if new else "删除" if _DELETED.fullmatch(spoken or "") else change,
                    "original": "" if _SAME.fullmatch(original or "") else original,
                    "spoken": "" if _DELETED.fullmatch(spoken or "") else spoken,
                    "zh": "" if _DELETED.fullmatch(zh or "") else zh,
                    "proof": _clean_md_inline(_cell(row, c_proof))[:120],
                    "refs": [ref.upper() for ref in re.findall(r"R\d+", _cell(row, c_refs), re.I)],
                })
            if parsed:
                rows, fmt = _merge_split_rows(parsed), "rows"
        elif not fmt and "口播" in joined and ("画面" in joined or "句序" in joined):
            c_spoken = _col(headers, lambda h: "口播" in h)
            parsed = []
            for position, row in enumerate(body, 1):
                spoken, zh = _spoken_and_zh(_cell(row, c_spoken))
                if spoken:
                    parsed.append({"no": str(position), "sources": [position], "after": None, "change": "",
                                   "original": "", "spoken": spoken, "zh": zh, "proof": "", "refs": []})
            if parsed:
                rows, fmt = parsed, "legacy"
        elif not alternatives and any("可换" in h or "条件满足" in h for h in headers):
            c_no = _col(headers, lambda h: "句序" in h or "句号" in h)
            c_alt = _col(headers, lambda h: "可换" in h or "条件满足" in h)
            c_zh = _col(headers, lambda h: "中文" in h)
            c_check = _col(headers, lambda h: "核对" in h)
            for row in body:
                numbers, _ = _sentence_numbers(_cell(row, 0 if c_no is None else c_no))
                spoken, zh = _spoken_and_zh(_cell(row, c_alt))
                if c_zh is not None:
                    zh = _clean_md_inline(_cell(row, c_zh)) or zh
                if numbers and spoken:
                    alternatives.append({"no": str(numbers[0]), "spoken": spoken, "zh": zh,
                                         "check": _clean_md_inline(_cell(row, c_check))})
    return {"adopt_format": fmt, "adopt_rows": rows, "changes": changes, "alternatives": alternatives}


def _same_words(a, b):
    norm = lambda value: re.sub(r"[\s\"'“”‘’]+", " ", str(value or "")).strip().lower()
    return norm(a) == norm(b)


def _clean_spoken(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _same_script(record, payload):
    """The script is as it was reviewed, or as this review's own adoption left it, or its spoken lines still read
    exactly as the review's line table quotes them (a picture or 中文 changed since does not stop an older review)."""
    if not record.get("script_hash"):
        return True
    if payload["script_hash"] in (record["script_hash"], (record.get("adopted") or {}).get("script_hash")):
        return True
    return _same_spoken(record, payload.get("lines") or [])


def _same_spoken(record, lines):
    fmt, rows = record.get("adopt_format"), record.get("adopt_rows")
    if record.get("markdown"):
        parsed = parse_adoption(record["markdown"])
        fmt, rows = parsed["adopt_format"], parsed["adopt_rows"]
    if fmt != "rows" or not rows:
        return False  # an older report quotes only the revised lines, so it cannot vouch for the current ones
    current = [str(line.get("spoken") or "") for line in lines]
    seen = []
    for row in rows:
        sources = row.get("sources") or []
        if not sources:
            continue
        if any(type(n) is not int or not 1 <= n <= len(current) for n in sources):
            return False
        quoted = row.get("original") or (row.get("spoken") if row.get("change") in ("未改", "画面") else "")
        if not quoted or not _same_words(quoted, " ".join(current[n - 1] for n in sources)):
            return False
        seen += sources
    return len(seen) == len(set(seen))  # a sentence the table leaves out was not changed


def adoption_view(record, lines, *, job=False):
    """What the drawer offers to adopt from one finished review, matched against the lines as they are now.

    Each item names the original lines it replaces (1-based `sources`), what the review changed, and whether the
    spoken words and/or the picture differ. `available` is False, with a reason, when the report cannot be lined
    up with the script line by line."""
    record = record or {}
    if record.get("status") != "done":
        return None
    fmt = record.get("adopt_format")
    raw = record.get("adopt_rows")
    if record.get("markdown"):  # parsed again, so an older review gains what the parser learned since
        parsed = parse_adoption(record["markdown"])
        fmt, raw = parsed["adopt_format"], parsed["adopt_rows"]
        record = {**record, "changes": parsed["changes"], "alternatives": parsed["alternatives"]}
    original = [{"spoken": str(line.get("spoken") or "").strip(), "zh": str(line.get("zh") or "").strip(),
                 "proof": str(line.get("proof") or "").strip()} for line in (lines or [])]
    count = len(original)
    if not fmt or not raw:
        return {"available": False, "reason": "这份报告里没有逐句表，重新审查一次就能逐句采纳。", "items": []}
    changes = {item["id"]: item for item in record.get("changes") or []}
    alternatives = {item["no"]: item for item in record.get("alternatives") or []}
    if fmt == "legacy":
        if len(raw) != count:
            return {"available": False, "items": [],
                    "reason": f"这份是旧格式报告，改后 {len(raw)} 句和原稿 {count} 句对不上，重新审查一次就能逐句采纳。"}
    covered = []
    for row in raw:
        covered += row.get("sources") or []
    extra = sorted({n for n in covered if not 1 <= n <= count})
    twice = sorted({n for n in covered if covered.count(n) > 1})
    if extra or twice:
        what = f"第 {'、'.join(map(str, extra[:5]))} 句原稿里没有" if extra else f"第 {'、'.join(map(str, twice[:5]))} 句出现了两次改法"
        return {"available": False, "items": [], "reason": f"审查表的句序和原稿对不上（{what}），重新审查一次就能逐句采纳。"}
    if fmt == "rows":
        # A table that leaves out sentences it did not change: those sentences stay as they are.
        missing = [n for n in range(1, count + 1) if n not in covered]
        if missing:
            raw = list(raw)
            for n in missing:
                position = next((i for i, row in enumerate(raw) if (row.get("sources") or [row.get("after") or 0])[0] > n), len(raw))
                raw.insert(position, {"no": str(n), "sources": [n], "after": None, "change": "未改", "original": "",
                                      "spoken": original[n - 1]["spoken"], "zh": original[n - 1]["zh"], "proof": "", "refs": []})
    items = []
    for index, row in enumerate(raw):
        sources = list(row.get("sources") or [])
        before = [original[n - 1] for n in sources]
        orig_spoken = " ".join(line["spoken"] for line in before)
        orig_zh = " ".join(line["zh"] for line in before)
        orig_proof = before[0]["proof"] if len(before) == 1 else ""
        change = row.get("change") or ""
        spoken, zh = row.get("spoken") or "", row.get("zh") or ""
        if change == "删除":
            spoken_changed = True
        elif change == "新增":
            spoken_changed = bool(spoken)
        else:
            spoken_changed = bool(spoken) and (len(sources) > 1 or not _same_words(spoken, orig_spoken))
            if not spoken_changed:
                spoken, zh = orig_spoken, orig_zh or zh
            if spoken_changed and change not in SPOKEN_KINDS:
                change = "合并" if len(sources) > 1 else "建议"
            if not spoken_changed and change in SPOKEN_KINDS:
                change = "画面" if change != "未改" else change
        if fmt == "legacy":
            change = "改动" if spoken_changed else "未改"
        proof = row.get("proof") or ""
        proof_changed = (fmt == "rows" and change != "删除" and bool(proof) and len(sources) <= 1
                         and (change != "未改" or not orig_proof) and proof != orig_proof)
        if not spoken_changed and not proof_changed:
            change = "未改"
        elif not spoken_changed and change == "未改":
            change = "画面"
        reasons = [changes[ref]["problem"] for ref in row.get("refs") or [] if ref in changes and changes[ref].get("problem")]
        alt = alternatives.get(str(sources[0])) if spoken_changed and len(sources) == 1 else None
        items.append({
            "index": index, "no": row.get("no") or (str(sources[0]) if sources else ""), "sources": sources,
            "after": row.get("after"), "change": change,
            "orig_spoken": orig_spoken, "orig_zh": orig_zh, "orig_proof": orig_proof,
            "spoken": spoken, "zh": zh, "proof": proof if proof_changed else orig_proof,
            "spoken_changed": spoken_changed, "proof_changed": proof_changed,
            "refs": row.get("refs") or [], "reason": "；".join(reasons),
            "alternative": alt,
            # Kept for older pages: a job's spoken change is taken too now (as a new version and a new job).
            "spoken_locked": False,
        })
    changed = [item for item in items if item["change"] != "未改"]
    return {"available": bool(changed), "reason": "" if changed else "审查没有改任何句子，不用采纳。",
            "format": fmt, "items": items}


def build_adoption(view, picks):
    """The adopted lines in order: [{source, spoken, zh, proof?}], from the view and the person's ticks.

    `picks` maps an item index to {"spoken": bool, "proof": bool, "alt": bool}; an item not ticked keeps its original
    line(s). `proof` is only set on a line whose picture is taken from the review (or a new line)."""
    if not view or not view.get("available"):
        raise ScriptReviewError((view or {}).get("reason") or "这份审查结果不能逐句采纳。", 409)
    picks = picks if isinstance(picks, dict) else {}
    out, taken_spoken, taken_proof = [], 0, 0
    for item in view["items"]:
        pick = picks.get(item["index"]) or picks.get(str(item["index"])) or {}
        take_spoken = bool(pick.get("spoken")) and item["spoken_changed"] and not item["spoken_locked"]
        take_proof = bool(pick.get("proof")) and item["proof_changed"]
        sources = item["sources"]
        if item["change"] == "新增":
            if take_spoken:
                out.append({"source": None, "spoken": item["spoken"], "zh": item["zh"], "proof": item["proof"] or ""})
                taken_spoken += 1
            continue
        if not take_spoken:
            for position, number in enumerate(sources):
                line = {"source": number}
                if take_proof and len(sources) == 1:
                    line["proof"] = item["proof"]
                out.append(line)
            taken_proof += int(take_proof)
            continue
        taken_spoken += 1
        if item["change"] == "删除":
            continue
        spoken, zh = item["spoken"], item["zh"]
        if pick.get("alt") and item.get("alternative"):
            spoken, zh = item["alternative"]["spoken"], item["alternative"]["zh"] or zh
        line = {"source": sources[0], "spoken": spoken, "zh": zh}
        if take_proof:
            line["proof"] = item["proof"]
            taken_proof += 1
        out.append(line)
    if not taken_spoken and not taken_proof:
        raise ScriptReviewError("没有勾选要采纳的改动。")
    return {"lines": out, "spoken": taken_spoken, "proof": taken_proof}


def build_mixed_adoption(views, picks, count):
    """Like build_adoption, with each pick naming the review it comes from (several models' reviews of one script).

    A sentence takes its spoken words from at most one review and its picture from at most one; a pick that would
    give a sentence two different rewrites is refused rather than guessed."""
    for review_id, view in views.items():
        if not view or not view.get("available"):
            raise ScriptReviewError((view or {}).get("reason") or "其中一份审查结果不能逐句采纳。", 409)
    items = {(review_id, item["index"]): item for review_id, view in views.items() for item in view["items"]}
    spoken_at, proof_at, added = {}, {}, {}
    taken_spoken = taken_proof = 0
    for pick in picks:
        item = items.get((pick["review_id"], pick["index"]))
        if not item:
            raise ScriptReviewError("采纳的句子和审查对不上，请刷新后重试。", 409)
        if pick["spoken"] and item["spoken_changed"]:
            if item["change"] == "新增":
                added.setdefault(item.get("after") or 0, []).append((item, pick))
                taken_spoken += 1
                continue
            for number in item["sources"]:
                if number in spoken_at:
                    raise ScriptReviewError(f"第 {number} 句选了两种改法，只能留一种。", 409)
                spoken_at[number] = (item, pick)
            taken_spoken += 1
        if pick["proof"] and item["proof_changed"] and len(item["sources"]) == 1:
            number = item["sources"][0]
            if number in proof_at:
                raise ScriptReviewError(f"第 {number} 句的画面选了两种，只能留一种。", 409)
            proof_at[number] = item["proof"]
            taken_proof += 1
    if not taken_spoken and not taken_proof:
        raise ScriptReviewError("没有勾选要采纳的改动。")
    out, done = [], set()

    def add_after(number):
        for item, pick in added.get(number, []):
            out.append({"source": None, "spoken": item["spoken"], "zh": item["zh"], "proof": item["proof"] or ""})

    add_after(0)
    for number in range(1, count + 1):
        if number not in done:
            chosen = spoken_at.get(number)
            if chosen:
                item, pick = chosen
                done.update(item["sources"])
                if item["change"] != "删除":
                    spoken, zh = item["spoken"], item["zh"]
                    if pick["alt"] and item.get("alternative"):
                        spoken, zh = item["alternative"]["spoken"], item["alternative"]["zh"] or zh
                    line = {"source": item["sources"][0], "spoken": spoken, "zh": zh}
                    proof = proof_at.get(item["sources"][0])
                    if proof:
                        line["proof"] = proof
                    out.append(line)
            else:
                done.add(number)
                line = {"source": number}
                if number in proof_at:
                    line["proof"] = proof_at[number]
                out.append(line)
        add_after(number)
    return {"lines": out, "spoken": taken_spoken, "proof": taken_proof}


def _extract_screen_line(markdown):
    for line in str(markdown or "").splitlines():
        match = re.match(r"^\s*(?:[-*]\s*)?(?:\*{0,2})?(?:开头屏显|首屏屏显|屏显)(?:\*{0,2})?\s*[：:]\s*(.+)$", line)
        if match:
            return match.group(1).strip()
    return ""


def build_review_prompt(payload, *, inline_references=None, skill_name=None):
    """Build single-script review prompt following the product's review SKILL.md."""
    skill_name = skill_name or _product_skill_name(payload.get("product")) or "sharpal-202h-script-review"
    market_info = markets.MARKETS.get(payload.get("market") or markets.DEFAULT, markets.MARKETS[markets.DEFAULT])
    market_label = market_info.get("label") or payload.get("market") or "美国"
    lines = payload.get("lines") or []
    rows = []
    for idx, line in enumerate(lines, 1):
        spoken = str(line.get("spoken") or "").strip()
        zh = str(line.get("zh") or "").strip()
        proof = str(line.get("proof") or "").strip()
        row = f"{idx}. {spoken}"
        if zh:
            row += f"  | 中文：{zh}"
        if proof:
            row += f"  | 画面：{proof}"
        rows.append(row)
    script_block = "\n".join(rows) if rows else str(payload.get("raw_text") or "").strip()
    screen = payload.get("screen") or ""
    extra = []
    if screen:
        extra.append(f"- 开头屏显：{screen}")
    if payload.get("direction_id"):
        extra.append(f"- 关联方向：{payload['direction_id']}")
    extra_block = ("\n" + "\n".join(extra)) if extra else ""

    parts = []
    if inline_references:
        parts.append("以下是本次审稿必须严格遵循的产品事实、转化数据与画面库参考资料（如与旧记忆冲突，以工作台最新产品文档为准）：\n")
        for ref in inline_references:
            parts.append(f"=== 参考：{ref['label']} ===\n{ref['text'].strip()}\n")
        parts.append("---\n")

    parts.append(
        f"请严格按照 `{skill_name}` 技能的「单稿深审」流程与强制输出格式审查以下口播稿：\n"
        "1. 输出必须按技能「单稿深审输出结构」的 6 节依次给出：审稿模式与裁决总览（含四关体检紧凑表）→ 改动清单（编号 R1、R2…，类型只用 必改/建议/画面）→ 逐句采纳表（表头照写：句序｜改动｜原句口播｜改后口播｜改后中文｜画面｜编号；原稿每一句占一行，下面列出的原稿有几句就至少几行；一格只放一句，不放备选句）→ 待核实备选 → 拍摄细节与避坑 → 核对范围声明。\n"
        "2. 诊断、原因、策略说明与画面描述一律使用简体中文；改动后口播使用目标市场对应语言（默认美式英语）并逐句附中文对照。\n"
        "3. 严守最小修改原则：成立的切口不擅自推翻；仅对高要求实拍硬证明镜头标注 `[需核实素材/缺则补拍]`。\n"
        "4. 直接输出 Markdown 审查报告正文，不要包在最外层代码块里。\n\n"
        f"【待审稿件信息】\n"
        f"- 来源：{payload.get('source_label') or payload.get('kind')}\n"
        f"- 产品：{payload.get('product') or '未指定'}\n"
        f"- 目标市场：{market_label}\n"
        f"- 标题：{payload.get('title') or '未命名稿件'}\n"
        f"- 当前核对边界：{payload.get('scope_note') or '纯文字口播稿（无实拍成片与当天挂车价格页核验）'}"
        f"{extra_block}\n\n"
        f"【口播与分镜内容】\n{script_block}\n"
    )
    return "\n".join(parts)


class ScriptReviewStore:
    def __init__(self, runtime_root):
        self.root = Path(runtime_root) / "script-reviews"
        self.path = self.root / "reviews.json"
        self.lock = threading.RLock()
        self._recover_interrupted()

    def _read_unlocked(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise ScriptReviewError("审稿记录文件无法读取，请检查运行目录 script-reviews/reviews.json。", 500) from exc
        targets = data.get("targets") if isinstance(data, dict) else None
        return targets if isinstance(targets, dict) else {}

    def _write_unlocked(self, targets):
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.root, prefix="reviews.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "targets": targets}, handle, ensure_ascii=False, indent=1)
            os.replace(temp, self.path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    def _recover_interrupted(self):
        with self.lock:
            try:
                targets = self._read_unlocked()
            except ScriptReviewError:
                return
            changed = False
            now = _now()
            for bucket in targets.values():
                if not isinstance(bucket, dict):
                    continue
                for record in [bucket.get("latest"), *(bucket.get("history") or [])]:
                    if isinstance(record, dict) and record.get("status") == "running":
                        record["status"] = "failed"
                        record["error"] = "工作台已重启，这次审查中断，请重新审查。"
                        record["updated_at"] = now
                        record["finished_at"] = now
                        changed = True
            if changed:
                self._write_unlocked(targets)

    def get(self, target_key):
        with self.lock:
            bucket = self._read_unlocked().get(target_key)
            return copy.deepcopy(bucket) if isinstance(bucket, dict) else None

    def summary(self, keys):
        with self.lock:
            targets = self._read_unlocked()
            result = {}
            for key in keys:
                bucket = targets.get(key)
                latest = bucket.get("latest") if isinstance(bucket, dict) else None
                if isinstance(latest, dict):
                    result[key] = {
                        "id": latest.get("id"),
                        "status": latest.get("status"),
                        "verdict": latest.get("verdict") or "",
                        "verdict_label": latest.get("verdict_label") or "",
                        "script_hash": latest.get("script_hash") or "",
                        "updated_at": latest.get("updated_at") or "",
                        "count": len(bucket.get("history") or []),
                        "adopted": bool(latest.get("adopted")),
                    }
            return result

    def keys(self):
        with self.lock:
            return list(self._read_unlocked())

    def remove(self, keys):
        """Drop whole targets; `keys` may hold exact keys or prefixes ending in ":". Returns {key: [review ids]}."""
        wanted = [key for key in keys or [] if isinstance(key, str) and key]
        with self.lock:
            targets = self._read_unlocked()
            removed = {}
            for key in list(targets):
                if any(key == want or (want.endswith(":") and key.startswith(want)) for want in wanted):
                    bucket = targets.pop(key)
                    removed[key] = [item.get("id") for item in (bucket.get("history") or []) if isinstance(item, dict) and item.get("id")]
            if removed:
                self._write_unlocked(targets)
            return removed

    def mark_adopted(self, target_key, review_id, adopted):
        """Note on a finished review that it was adopted (when, what, and where the result went)."""
        with self.lock:
            targets = self._read_unlocked()
            bucket = targets.get(target_key)
            if not isinstance(bucket, dict):
                return None
            for item in [bucket.get("latest"), *(bucket.get("history") or [])]:
                if isinstance(item, dict) and item.get("id") == review_id:
                    item["adopted"] = copy.deepcopy(adopted)
            self._write_unlocked(targets)
            return copy.deepcopy(bucket)

    def upsert_run(self, target_key, record):
        with self.lock:
            targets = self._read_unlocked()
            bucket = targets.get(target_key)
            if not isinstance(bucket, dict):
                bucket = {"latest": None, "history": []}
            history = [item for item in (bucket.get("history") or []) if isinstance(item, dict)]
            replaced = False
            for idx, item in enumerate(history):
                if item.get("id") == record.get("id"):
                    history[idx] = copy.deepcopy(record)
                    replaced = True
                    break
            if not replaced:
                history.insert(0, copy.deepcopy(record))
            history = history[:MAX_HISTORY]
            # The newest run is the latest one; a slower run of the same batch finishing later does not take its place.
            bucket["latest"] = copy.deepcopy(history[0])
            bucket["history"] = history
            targets[target_key] = bucket
            if len(targets) > MAX_TARGETS:
                ordered = sorted(
                    targets,
                    key=lambda k: (targets[k].get("latest") or {}).get("updated_at", ""),
                )
                for old in ordered[:-MAX_TARGETS]:
                    del targets[old]
            self._write_unlocked(targets)
            return copy.deepcopy(bucket)


class ScriptReviewWorkbenchMixin:
    """Workbench endpoints for 口播稿审查.

    Requires: self.script_reviews, self.script_review_runs, self.ai, self.creation, self.production, self.store.
    """

    def _review_target_payload(self, params):
        kind = str(params.get("kind") or "").strip()
        if kind == "candidate":
            task_id = str(params.get("task_id") or "").strip()
            round_id = str(params.get("round_id") or "").strip()
            raw_index = params.get("variant_index")
            if isinstance(raw_index, str) and raw_index.isdigit():
                variant_index = int(raw_index)
            elif type(raw_index) is int:
                variant_index = raw_index
            else:
                raise ScriptReviewError("候选稿序号格式无效。")
            target_key = make_target_key("candidate", task_id=task_id, round_id=round_id, variant_index=variant_index)
            task = self.ai.get(task_id)
            turn = next((item for item in (task.get("rounds") or []) if item.get("round_id") == round_id), None)
            if not turn or turn.get("status") != "completed" or not isinstance(turn.get("output"), str):
                raise ScriptReviewError("这一轮候选稿还没有生成完成。", 409)
            variants = parse_variants(turn["output"])
            if variant_index >= len(variants):
                raise ScriptReviewError("找不到对应的候选稿。", 404)
            variant = variants[variant_index]
            ctx = task.get("context") or {}
            product = task.get("product") or ctx.get("product") or ""
            variant_label = variant.get("label") or f"第 {variant_index + 1} 条"
            try:
                from content_naming import task_display_metadata
                task_label = task_display_metadata(task).get("display_title") or ""
            except Exception:
                task_label = ""
            task_label = task_label or task.get("title") or task_id
            lines = variant.get("lines") or []
            has_proof = any(str(line.get("proof") or "").strip() for line in lines)
            screen = _extract_screen_line(variant.get("markdown") or turn.get("output"))
            raw_text = "\n".join(str(line.get("spoken") or "").strip() for line in lines if str(line.get("spoken") or "").strip())
            return {
                "target_key": target_key,
                "kind": "candidate",
                "variant_index": variant_index,
                "source_label": f"候选稿（{task.get('title') or task_id} · {variant.get('label') or f'第 {variant_index + 1} 条'}）",
                "product": product,
                "market": ctx.get("market") or markets.DEFAULT,
                "direction_id": ctx.get("direction_id") or "",
                "title": f"{task_label} · {variant_label}",
                "owner_id": task_id,
                "owner_label": task_label,
                "variant_label": variant_label,
                "screen": screen,
                "lines": [{"spoken": line.get("spoken") or "", "zh": line.get("zh") or "", "proof": line.get("proof") or ""} for line in lines],
                "raw_text": raw_text,
                "scope_note": "含候选稿口播、中文对照与意群画面文本（无实拍成片与当天挂车价格页核验）" if has_proof else "仅纯文字口播稿（无实拍成片、分镜与当天挂车价格页核验）",
                "script_hash": _hash(json.dumps({"screen": screen, "lines": lines}, ensure_ascii=False)),
            }

        if kind == "script":
            script_id = str(params.get("script_id") or "").strip()
            target_key = make_target_key("script", script_id=script_id)
            script = self.creation.get_script(script_id)
            if not script:
                raise ScriptReviewError("找不到这份采用稿。", 404)
            product = script.get("product") or ""
            spoken_list = [line.strip() for line in str(script.get("text") or "").splitlines() if line.strip()]
            zh_list = [line.strip() for line in str(script.get("translation_zh") or "").splitlines() if line.strip()]
            lines = []
            screen = ""
            has_proof = False
            draft_md = ""
            try:
                draft_md = (self.script_draft(script_id) or {}).get("markdown") or ""
            except Exception:
                draft_md = ""
            if draft_md:
                screen = _extract_screen_line(draft_md)
                parsed = parse_variants(draft_md)
                if parsed and len(parsed[0].get("lines") or []) == len(spoken_list):
                    for idx, item in enumerate(parsed[0]["lines"]):
                        proof = str(item.get("proof") or "").strip()
                        if proof:
                            has_proof = True
                        lines.append({
                            "spoken": spoken_list[idx],
                            "zh": zh_list[idx] if idx < len(zh_list) else (item.get("zh") or ""),
                            "proof": proof,
                        })
            if not lines:
                for idx, spoken in enumerate(spoken_list):
                    lines.append({"spoken": spoken, "zh": zh_list[idx] if idx < len(zh_list) else "", "proof": ""})
            return {
                "target_key": target_key,
                "kind": "script",
                "source_label": f"采用稿（第 {script.get('version') or 1} 版）",
                "product": product,
                "market": script.get("market") or markets.DEFAULT,
                "direction_id": script.get("direction_id") or "",
                "title": script.get("title") or script_id,
                "owner_id": script_id,
                "owner_label": activity_label(script, script_id),
                "screen": screen,
                "lines": lines,
                "raw_text": "\n".join(spoken_list),
                "scope_note": "含采用稿口播、中文对照与意群画面文本（无实拍成片与当天挂车价格页核验）" if has_proof else "仅采用稿纯文字口播（无实拍成片、分镜与当天挂车价格页核验）",
                "script_hash": _hash(json.dumps({"screen": screen, "text": script.get("text") or "", "lines": lines}, ensure_ascii=False)),
            }

        if kind == "job":
            job_id = str(params.get("job_id") or "").strip()
            target_key = make_target_key("job", job_id=job_id)
            job = self.production.job(job_id)
            if not job:
                raise ScriptReviewError("找不到这张制作单。", 404)
            product = job.get("product") or ""
            job_lines = job.get("lines") or []
            lines = []
            numbers = []
            has_proof = False
            for row in job_lines:
                spoken = str(row.get("spoken") or "").strip()
                zh = str(row.get("translation_zh") or row.get("zh") or "").strip()
                proof = str(row.get("proof") or "").strip()
                if proof:
                    has_proof = True
                if spoken or zh or proof:
                    lines.append({"spoken": spoken, "zh": zh, "proof": proof})
                    numbers.append(row.get("number"))
            screen = _extract_screen_line(job.get("fill_markdown") or job.get("supplement_markdown") or "")
            raw_text = "\n".join(item["spoken"] for item in lines if item["spoken"])
            if not raw_text and job.get("script_id"):
                script = self.creation.get_script(job["script_id"])
                if script:
                    raw_text = str(script.get("text") or "").strip()
            return {
                "target_key": target_key,
                "kind": "job",
                "line_numbers": numbers,
                "source_label": f"制作单（{job_id}）",
                "product": product,
                "market": job.get("market") or markets.DEFAULT,
                "direction_id": job.get("direction_id") or "",
                "title": job.get("display_title") or job.get("title") or job.get("variant_label") or job_id,
                "owner_id": job_id,
                "owner_label": activity_label(job, job_id),
                "screen": screen,
                "lines": lines,
                "raw_text": raw_text,
                "scope_note": "含制作单口播、中文对照与意群画面要求（无实拍成片与当天挂车价格页核验）" if has_proof else "仅制作单文字口播（无实拍成片与当天挂车价格页核验）",
                "script_hash": _hash(json.dumps({"screen": screen, "lines": lines, "raw": raw_text}, ensure_ascii=False)),
            }

        raise ScriptReviewError("不支持的审稿目标类型。")

    def _review_activity(self, record, payload, **fields):
        """Show a review in the task panel next to its script's other runs. Observation only."""
        # The panel reads "<head> · <name>" split at the first " · ", so the head itself must not hold one.
        label = payload.get("owner_label") or payload.get("title") or ""
        variant = str(payload.get("variant_label") or "").replace(" · ", " ")
        head = f"口播审查（{variant}）" if variant else "口播审查"
        # Each reviewed script is its own row (later reviews of it fold in), not a step hidden inside its task or order.
        return record_activity(self, "review:" + record["id"], kind="review", owner_id=record["target_key"],
                               source_id=record["id"], product=payload.get("product") or "", title=f"{head} · {label}",
                               step=record["target_key"], **fields)

    def _review_activity_finished(self, record, payload):
        status = record.get("status")
        if status == "done":
            changes = len(record.get("changes") or [])
            verdict = record.get("verdict_label") or "审查完成"
            detail = f"{verdict}，{changes} 处改动" if changes else verdict
            # A review that asks for changes waits on the reader until it is adopted or reviewed again.
            asks = record.get("verdict") in {"revise", "stop"} or changes
            return self._review_activity(record, payload, status="needs_attention" if asks else "completed", phase="审完",
                                         message=f"{detail}，打开审查逐句采纳。" if asks else detail,
                                         result_count=changes)
        if status == "cancelled":
            return self._review_activity(record, payload, status="cancelled", phase="审查", message="已停止。")
        return self._review_activity(record, payload, status="failed", phase="审查",
                                     message=_text(record.get("error"), 300) or "审查失败，请重试。")

    def _settle_review_activity(self, target_key, message, *, keep=None):
        activity = getattr(self, "activity", None)
        bucket = self.script_reviews.get(target_key) or {}
        keep_ids = set(keep) if isinstance(keep, (set, list, tuple)) else {keep}
        ids = ["review:" + item["id"] for item in bucket.get("history") or []
               if isinstance(item, dict) and item.get("id") and item.get("id") not in keep_ids]
        if activity is None or not ids:
            return
        try:
            activity.settle(ids, message)
        except Exception:
            pass  # Observation only.

    def forget_script_reviews(self, keys):
        """The reviewed script, job or writing task is gone: its reviews and their task-panel rows go with it."""
        try:
            removed = self.script_reviews.remove(keys)
        except Exception:
            return {}
        activity = getattr(self, "activity", None)
        for key, ids in removed.items():
            if activity is None:
                break
            try:
                activity.delete_owner(key)
                for review_id in ids:
                    activity.delete_run("review:" + review_id)
            except Exception:
                pass  # The task log is secondary; the deletion itself is complete.
        return removed

    def _review_target_gone(self, key):
        """True only when the target is known to be deleted (a 404), never on a read that merely failed."""
        parts = str(key).split(":")
        try:
            if parts[0] == "job" and len(parts) == 2:
                self.production.job(parts[1])
            elif parts[0] == "script" and len(parts) == 2:
                self.creation.get_script(parts[1])
            elif parts[0] == "candidate" and len(parts) == 4:
                task = self.ai.get(parts[1])
                return not any(turn.get("round_id") == parts[2] for turn in task.get("rounds") or [])
            else:
                return False
        except Exception as exc:
            return getattr(exc, "status", None) == 404
        return False

    def prune_script_reviews(self):
        """At start: reviews whose script, job or writing task was deleted earlier (before deletes cleaned them up)."""
        try:
            gone = [key for key in self.script_reviews.keys() if self._review_target_gone(key)]
        except Exception:
            return {}
        return self.forget_script_reviews(gone) if gone else {}

    def backfill_review_activity(self):
        """Reviews finished before reviews were listed in the task panel are added once, at their own times.

        One row per reviewed script (its latest review). A review whose script is gone stays out, since it can no
        longer be opened. Never raises: this is observation only. Returns (added, skipped_missing)."""
        activity = getattr(self, "activity", None)
        if activity is None:
            return 0, 0
        try:
            with self.script_reviews.lock:
                targets = copy.deepcopy(self.script_reviews._read_unlocked())
        except Exception:
            return 0, 0
        try:
            scripts = (self.creation.read() or {}).get("scripts") or []
        except Exception:
            scripts = []
        added = missing = 0
        for key, bucket in targets.items():
            record = bucket.get("latest") if isinstance(bucket, dict) else None
            if not isinstance(record, dict) or not record.get("id") or record.get("status") not in {"done", "failed", "cancelled"}:
                continue
            parts = str(key).split(":")
            params = ({"kind": "candidate", "task_id": parts[1], "round_id": parts[2], "variant_index": parts[3]} if parts[0] == "candidate" and len(parts) == 4
                      else {"kind": "script", "script_id": parts[1]} if parts[0] == "script" and len(parts) == 2
                      else {"kind": "job", "job_id": parts[1]} if parts[0] == "job" and len(parts) == 2 else None)
            if params is None:
                continue
            try:
                payload = self._review_target_payload(params)
            except Exception:
                missing += 1
                continue
            fields = {"status": "failed", "phase": "审查", "message": _text(record.get("error"), 300) or "审查失败，请重试。"}
            if record["status"] == "cancelled":
                fields = {"status": "cancelled", "phase": "审查", "message": "已停止。"}
            elif record["status"] == "done":
                changes = len(record.get("changes") or [])
                verdict = record.get("verdict_label") or "审查完成"
                detail = f"{verdict}，{changes} 处改动" if changes else verdict
                asks = record.get("verdict") in {"revise", "stop"} or changes
                adopted = ""
                if parts[0] == "candidate":
                    try:
                        task = self.ai.get(parts[1])
                        if any(record["id"] in str(turn.get("instruction") or "") for turn in task.get("rounds") or []):
                            adopted = "已采纳过，见写稿任务里的新一轮。"
                    except Exception:
                        pass
                elif parts[0] == "script" and any(item.get("parent_id") == parts[1] for item in scripts if isinstance(item, dict)):
                    adopted = "这份采用稿已有新版本。"
                if not adopted and record.get("script_hash") and record["script_hash"] != payload["script_hash"]:
                    adopted = "稿子在这次审查之后改过了。"
                if asks and not adopted:
                    fields = {"status": "needs_attention", "phase": "审完", "message": f"{detail}，打开审查逐句采纳。", "result_count": changes}
                else:
                    fields = {"status": "completed", "phase": "审完", "message": f"{detail}。{adopted}" if adopted else detail, "result_count": changes}
            label = payload.get("owner_label") or payload.get("title") or ""
            variant = str(payload.get("variant_label") or "").replace(" · ", " ")
            head = f"口播审查（{variant}）" if variant else "口播审查"
            try:
                if activity.backfill("review:" + record["id"], started_at=record.get("created_at"),
                                     finished_at=record.get("finished_at") or record.get("updated_at"),
                                     kind="review", owner_id=key, source_id=record["id"], product=payload.get("product") or "",
                                     title=f"{head} · {label}", step=key, **fields):
                    added += 1
            except Exception:
                continue
        return added, missing

    def script_review_status(self, params):
        payload = self._review_target_payload(params)
        skill = skill_info(payload["product"], skill_data_root=self.store.root)
        bucket = self.script_reviews.get(payload["target_key"])
        latest = bucket.get("latest") if bucket else None
        history = bucket.get("history") if bucket else []
        stale = bool(latest and latest.get("status") == "done" and not _same_script(latest, payload))
        latest, history = self._with_adoption(payload, latest), [self._with_adoption(payload, item) for item in history or []]
        return {
            "target_key": payload["target_key"],
            "kind": payload["kind"],
            "product": payload["product"],
            "title": payload["title"],
            "market": payload["market"],
            "available": bool(skill),
            "skill": skill["name"] if skill else None,
            "script_hash": payload["script_hash"],
            "stale": stale,
            "latest": latest,
            "history": history,
        }

    def _with_adoption(self, payload, record):
        """A finished record carries what can be adopted from it into the script as it is now."""
        if not isinstance(record, dict) or record.get("status") != "done":
            return record
        if not _same_script(record, payload):
            adoption = {"available": False, "items": [], "reason": "稿件在这次审查之后改过了，重新审查后再逐句采纳。"}
        else:
            adoption = adoption_view(record, payload["lines"], job=payload["kind"] == "job")
        return {**record, "adoption": adoption}

    def script_review_adopt(self, body):
        """Take the ticked lines of a finished review into the script it reviewed.

        A candidate gets a new round (the same 改稿 path, no AI), an adopted copy a new version (the line edit path),
        and a production job its 意群表 pictures in place; spoken changes on a job go into a new version of its adopted
        copy and a new job made from it (the old job keeps its voice and edits)."""
        if not isinstance(body, dict):
            raise ScriptReviewError("采纳请求格式错误。")
        payload = self._review_target_payload(body)
        bucket = self.script_reviews.get(payload["target_key"]) or {}
        history = {item.get("id"): item for item in bucket.get("history") or [] if isinstance(item, dict)}
        raw_picks = body.get("picks")
        if not isinstance(raw_picks, list):
            raise ScriptReviewError("请勾选要采纳的改动。")
        # A pick may name its own review: several models' reviews of the same script, taken sentence by sentence.
        review_ids = []
        for pick in raw_picks:
            rid = pick.get("review_id") if isinstance(pick, dict) else None
            rid = rid if isinstance(rid, str) and rid else body.get("review_id")
            if rid not in review_ids:
                review_ids.append(rid)
        if not review_ids:
            review_ids = [body.get("review_id")]
        records = [history.get(rid) for rid in review_ids]
        if any(not item or item.get("status") != "done" for item in records):
            raise ScriptReviewError("找不到这次审查结果，请刷新后重试。", 404)
        record = records[0]
        adopted = record.get("adopted") or {}
        if len(records) == 1 and adopted.get("job_id"):
            # Its spoken changes already went into a new version and a new job; that job is the answer.
            return {"kind": record.get("kind") or "job", "note": adopted.get("note") or "", "repeated": True,
                    "new_job": True, "job": self.production.job(adopted["job_id"])}
        if any(not _same_script(item, payload) for item in records):
            raise ScriptReviewError("稿件在这次审查之后改过了，重新审查后再采纳。", 409)
        is_job = payload["kind"] == "job"
        if len(records) == 1:
            picks = {}
            for pick in raw_picks:
                if isinstance(pick, dict) and type(pick.get("index")) is int:
                    picks[pick["index"]] = {key: pick.get(key) is True for key in ("spoken", "proof", "alt")}
            built = build_adoption(adoption_view(record, payload["lines"], job=is_job), picks)
            pick_key = sorted(picks.items())
        else:
            views = {item["id"]: adoption_view(item, payload["lines"], job=is_job) for item in records}
            mixed = []
            for pick in raw_picks:
                if isinstance(pick, dict) and type(pick.get("index")) is int:
                    mixed.append({"review_id": pick.get("review_id") or body.get("review_id"), "index": pick["index"],
                                  **{key: pick.get(key) is True for key in ("spoken", "proof", "alt")}})
            built = build_mixed_adoption(views, mixed, len(payload["lines"]))
            pick_key = sorted((item["review_id"], item["index"], item["spoken"], item["proof"], item["alt"]) for item in mixed)
        original = payload["lines"]
        lines = []
        for line in built["lines"]:
            if line["source"] is not None and "spoken" not in line:
                base = original[line["source"] - 1]
                line = {**line, "spoken": base["spoken"], "zh": base["zh"]}
            lines.append(line)
        request_id = "sradopt-" + _hash(json.dumps([review_ids, payload["script_hash"], pick_key], ensure_ascii=False))
        note = "、".join(part for part in (f"改 {built['spoken']} 句口播" if built["spoken"] else "",
                                            f"换 {built['proof']} 条画面" if built["proof"] else "") if part)
        kind = payload["kind"]
        stamp = {"at": _now(), "note": note, "review_id": record["id"], **({"review_ids": review_ids} if len(review_ids) > 1 else {})}
        if kind == "candidate":
            task = self.ai.get(body.get("task_id"))
            try:
                detail = self.ai.continue_task(task["task_id"], {
                    "request_id": request_id, "task_revision": task["revision"],
                    "manual_edit": {"round_id": body.get("round_id"), "variant_index": payload["variant_index"],
                                    "origin": f"口播审查 {record['id']}",
                                    "lines": [{**{k: v for k, v in line.items() if k != "proof"}, "translate": False,
                                               **({"proof": line["proof"]} if line.get("proof") else {})} for line in lines]},
                })
            except Exception as exc:
                # The same ticks were adopted already (a second click): the round made then is the answer.
                if "请求编号已用于" not in str(exc):
                    raise
                return {"kind": kind, "note": note, "repeated": True, "task": self.ai.get(task["task_id"])}
            self._mark_adopted(payload["target_key"], [item["id"] for item in records], stamp)
            self._settle_review_activity(payload["target_key"], f"已采纳：{note or '存为新一轮'}。")
            return {"kind": kind, "note": note, "task": detail.get("task") if isinstance(detail, dict) and "task" in detail else detail}
        if kind == "script":
            result = self.edit_script_lines(body.get("script_id"), {"request_id": request_id, "lines": lines})
            self._mark_adopted(payload["target_key"], [item["id"] for item in records], {**stamp, "script_id": (result.get("script") or {}).get("id") or ""})
            self._settle_review_activity(payload["target_key"], f"已采纳：{note or '存为新版本'}。")
            return {"kind": kind, "note": note, "script": result.get("script"), "edit": result.get("edit")}
        if built["spoken"]:
            return self._adopt_into_new_job(body, records, payload, lines, request_id, note, stamp)
        numbers = payload.get("line_numbers") or []
        proofs = {numbers[line["source"] - 1]: line["proof"] for line in lines
                  if line.get("source") and line.get("proof") and line["source"] - 1 < len(numbers)}
        job = self.production.job(body.get("job_id"))
        rows = [{"number": row["number"], "proof": proofs.get(row["number"], row.get("proof") or ""),
                 "tags": row.get("tags") or "", "evidence": row.get("evidence") or ""} for row in job.get("lines") or []]
        result = self.save_job_lines(job["id"], {"request_id": request_id, "revision": self.production.public()["revision"],
                                                 "lines": rows})
        # The picture is now part of the job; the review stays usable for what was not taken yet.
        after = self._review_target_payload({"kind": "job", "job_id": job["id"]})
        self._mark_adopted(payload["target_key"], [item["id"] for item in records], {**stamp, "script_hash": after["script_hash"]})
        self._settle_review_activity(payload["target_key"], f"已采纳：{note or '更新了画面'}。")
        return {"kind": kind, "note": note, "job": result.get("job")}

    def _adopt_into_new_job(self, body, records, payload, lines, request_id, note, stamp):
        """Spoken changes on a production job: the job's adopted copy gets a new version, and a new job is made from it.

        The old job keeps its voice, timelines and edits as they are. The new job carries over each kept line's
        picture, tags and evidence; a changed line keeps its picture unless the review's was taken."""
        job = self.production.job(body.get("job_id"))
        script_id = job.get("script_id") or ""
        script = self.creation.get_script(script_id) if script_id else None
        if not script:
            raise ScriptReviewError("找不到这张制作单用的采用稿，没法存新版本。", 409)
        script_lines = [line.strip() for line in str(script.get("text") or "").splitlines() if line.strip()]
        old_rows = job.get("lines") or []
        if [_clean_spoken(line) for line in script_lines] != [_clean_spoken(row.get("spoken")) for row in old_rows]:
            raise ScriptReviewError("这张制作单的口播和它的采用稿对不上，请到采用稿上审查后采纳。", 409)
        rows = []
        for line in lines:
            old = old_rows[line["source"] - 1] if line.get("source") else {}
            rows.append({"source": line.get("source"), "spoken": line["spoken"], "zh": line["zh"],
                         "proof": line.get("proof") or old.get("proof") or "",
                         "tags": old.get("tags") or "", "evidence": old.get("evidence") or ""})
        sent = []
        for row, line in zip(rows, lines):
            old = old_rows[row["source"] - 1] if row["source"] else {}
            entry = {"source": row["source"], "spoken": row["spoken"], "zh": row["zh"]}
            # A changed line names its picture so the source table does not mark it 待更新; a kept line keeps its own.
            if row["proof"] and (line.get("proof") or not row["source"] or _clean_spoken(row["spoken"]) != _clean_spoken(old.get("spoken"))):
                entry["proof"] = row["proof"]
            sent.append(entry)
        edited = self.edit_script_lines(script_id, {"request_id": request_id, "lines": sent})
        new_script = edited.get("script") or {}
        created = self.create_production_job({"request_id": request_id + "-job", "revision": self.production.public()["revision"],
                                              "job": {"script_id": new_script.get("id")}})
        new_job = created["job"]
        filled = []
        for row, line in zip(rows, new_job.get("lines") or []):
            filled.append({"number": line["number"], "proof": row["proof"] or line.get("proof") or "",
                           "tags": row["tags"] or line.get("tags") or "", "evidence": row["evidence"] or line.get("evidence") or ""})
        update = {"request_id": request_id + "-lines", "revision": self.production.public()["revision"], "lines": filled}
        if job.get("supplement") and not new_job.get("supplement"):
            update["supplement"] = job["supplement"]
        if len(filled) == len(new_job.get("lines") or []):
            new_job = self.save_job_lines(new_job["id"], update).get("job") or new_job
        note = f"{note}，采用稿存为第 {new_script.get('version') or '新'} 版，并新建了制作单"
        self._mark_adopted(payload["target_key"], [item["id"] for item in records],
                                         {**stamp, "note": note, "script_id": new_script.get("id") or "", "job_id": new_job["id"]})
        self._settle_review_activity(payload["target_key"], f"已采纳：{note}。")
        return {"kind": "job", "note": note, "new_job": True, "job": new_job, "script": new_script}

    def _mark_adopted(self, target_key, review_ids, stamp):
        for review_id in review_ids:
            self.script_reviews.mark_adopted(target_key, review_id, stamp)

    def script_review_summary(self, keys):
        clean_keys = [key.strip() for key in (keys or []) if isinstance(key, str) and key.strip()][:80]
        return {"items": self.script_reviews.summary(clean_keys)}

    def script_review_busy(self):
        with self.lock:
            return bool(getattr(self, "script_review_runs", None))

    def script_review_stop(self, body):
        if not isinstance(body, dict) or not isinstance(body.get("target_key"), str) or not body["target_key"].strip():
            raise ScriptReviewError("请提供要停止的审稿目标。")
        target_key = body["target_key"].strip()
        with self.lock:
            runs = [run for run in self.script_review_runs.values() if run.get("target_key") == target_key]
            for run in runs:
                run["event"].set()
            run = bool(runs)
        bucket = self.script_reviews.get(target_key)
        latest = bucket.get("latest") if bucket else None
        if latest and latest.get("status") == "running" and not run:
            latest["status"] = "cancelled"
            latest["error"] = "已停止。"
            latest["updated_at"] = _now()
            latest["finished_at"] = _now()
            self.script_reviews.upsert_run(target_key, latest)
            record_activity(self, "review:" + latest["id"], status="cancelled", phase="审查", message="已停止。")
        return {"ok": True, "target_key": target_key}

    def script_review_start(self, body):
        if not isinstance(body, dict):
            raise ScriptReviewError("审稿请求格式错误。")
        allowed = {"kind", "task_id", "round_id", "variant_index", "script_id", "job_id", "model", "reasoning_effort", "models"}
        if set(body) - allowed:
            raise ScriptReviewError("审稿请求包含不支持的字段。")
        payload = self._review_target_payload(body)
        if not payload["raw_text"] and not payload["lines"]:
            raise ScriptReviewError("这份稿件还没有可审查的口播内容。", 409)
        skill = skill_info(payload["product"], skill_data_root=self.store.root)
        if not skill:
            raise ScriptReviewError(f"当前产品（{payload['product'] or '未指定'}）尚未配置专属口播审稿技能。", 409)

        target_key = payload["target_key"]
        choices = body.get("models")
        if choices is None:
            choices = [{k: body[k] for k in ("model", "reasoning_effort") if k in body}]
        if not isinstance(choices, list) or not 1 <= len(choices) <= MAX_PARALLEL:
            raise ScriptReviewError(f"一次最多同时用 {MAX_PARALLEL} 个模型审查。")
        settings_list, seen = [], set()
        for choice in choices:
            if not isinstance(choice, dict) or set(choice) - {"model", "reasoning_effort"}:
                raise ScriptReviewError("模型选择格式无效。")
            # Only a single-model run remembers its choice as the next default for 口播审查.
            settings = self.ai.resolve_default_settings(choice, kind="script_review" if len(choices) == 1 else None)
            mark = (settings.get("model"), settings.get("reasoning_effort"))
            if mark in seen:
                continue  # the same model and effort twice is one review
            seen.add(mark)
            settings_list.append(settings)

        with self.lock:
            if any(run.get("target_key") == target_key for run in self.script_review_runs.values()):
                bucket = self.script_reviews.get(target_key)
                return {
                    "target_key": target_key,
                    "available": True,
                    "skill": skill["name"],
                    "stale": False,
                    "latest": bucket.get("latest") if bucket else None,
                    "history": bucket.get("history") if bucket else [],
                }
            batch = ("SRB-" + uuid4().hex[:10]) if len(settings_list) > 1 else ""
            started = []
            for settings in settings_list:
                now = _now()
                record = {
                    "id": "SR-" + uuid4().hex[:10],
                    "target_key": target_key,
                    "kind": payload["kind"],
                    "product": payload["product"],
                    "title": payload["title"],
                    "market": payload["market"],
                    "skill": skill["name"],
                    "status": "running",
                    "verdict": "",
                    "verdict_label": "",
                    "gates": [],
                    "revised_spoken": "",
                    "revision_points": "",
                    "markdown": "",
                    "model": settings.get("model"),
                    "reasoning_effort": settings.get("reasoning_effort"),
                    "script_hash": payload["script_hash"],
                    "progress": "正在读取 202H 审稿技能与产品口径…",
                    "error": None,
                    "created_at": now,
                    "updated_at": now,
                    "finished_at": None,
                }
                if batch:
                    record.update(batch=batch, batch_size=len(settings_list))
                bucket = self.script_reviews.upsert_run(target_key, record)
                cancel_event = threading.Event()
                self.script_review_runs[record["id"]] = {"id": record["id"], "target_key": target_key, "event": cancel_event}
                started.append((record, settings, cancel_event))
        # An earlier review of the same script stops asking once this one starts; the new runs carry the outcome.
        self._settle_review_activity(target_key, "已重新审查，看最新一次的结果。", keep={item[0]["id"] for item in started})
        for record, settings, cancel_event in started:
            self._review_activity(record, payload, status="running", phase="审查", message=record["progress"])
            threading.Thread(target=self._run_review, args=(record, payload, skill, settings, cancel_event),
                             daemon=True, name=f"script-review-{record['id']}").start()
        bucket = self.script_reviews.get(target_key) or {}
        return {
            "target_key": target_key,
            "available": True,
            "skill": skill["name"],
            "stale": False,
            "latest": bucket.get("latest"),
            "history": bucket.get("history") or [],
        }

    def _run_review(self, record, payload, skill, settings, cancel_event):
        target_key = payload["target_key"]
        progress_at = [0.0]
        current = copy.deepcopy(record)
        try:
            is_antigravity = str(settings.get("model") or "").startswith("antigravity:")
            prompt = build_review_prompt(
                payload,
                inline_references=None if is_antigravity else skill["references"],
                skill_name=skill["name"],
            )
            skills_arg = [{"name": skill["name"], "path": skill["path"]}]

            def on_progress(message):
                text = _text(message, 200)
                if not text or cancel_event.is_set():
                    return
                current["progress"] = text
                current["updated_at"] = _now()
                self.script_reviews.upsert_run(target_key, current)
                if time.monotonic() - progress_at[0] >= ACTIVITY_PROGRESS_SECONDS:
                    progress_at[0] = time.monotonic()
                    self._review_activity(current, payload, status="running", phase="审查", message=text)

            extra_kwargs = {"references": skill["references"]} if is_antigravity else {}
            output = self.ai.bridge.run(
                prompt,
                cwd=str(self.script_reviews.root.resolve()),
                cancel_event=cancel_event,
                on_progress=on_progress,
                model=settings.get("model"),
                reasoning_effort=settings.get("reasoning_effort"),
                skills=skills_arg,
                **extra_kwargs,
            )
            if cancel_event.is_set():
                raise ScriptReviewError("已停止。", 409)
            markdown = str(output or "").strip()
            if not markdown:
                raise ScriptReviewError("AI 没有返回审查报告，请重试。", 502)
            parsed = parse_review_result(markdown)
            now_done = _now()
            current.update(
                status="done",
                markdown=markdown,
                verdict=parsed["verdict"],
                verdict_label=parsed["verdict_label"],
                gates=parsed["gates"],
                revised_spoken=parsed["revised_spoken"],
                revision_points=parsed["revision_points"],
                adopt_format=parsed["adopt_format"],
                adopt_rows=parsed["adopt_rows"],
                changes=parsed["changes"],
                alternatives=parsed["alternatives"],
                progress=None,
                error=None,
                updated_at=now_done,
                finished_at=now_done,
            )
        except Exception as exc:
            now_end = _now()
            cancelled = cancel_event.is_set()
            current.update(
                status="cancelled" if cancelled else "failed",
                progress=None,
                error="已停止。" if cancelled else (_text(exc, 300) or "审查失败，请重试。"),
                updated_at=now_end,
                finished_at=now_end,
            )
        finally:
            self.script_reviews.upsert_run(target_key, current)
            self._review_activity_finished(current, payload)
            with self.lock:
                self.script_review_runs.pop(current["id"], None)

