"""Split a finished writing candidate into voiced variants without repairing any text.

The writing skill delivers each voiced script as a "口播（配音用）" code block with one meaning group
per line, then "对应中文" and an "意群表" (# | 口播 | 画面要证明什么 | 素材标签 | 证据). Mismatches are
reported as problems for the user to check; the parser never guesses missing lines.
"""
from __future__ import annotations

import re

from ai_tasks import SPOKEN_HEADING, VARIANT_LABEL

CHINESE_HEADING = re.compile(r"对应中文")
TABLE_HEADING = re.compile(r"意群表")
COLUMNS = (("number", ("#", "＃", "序号", "编号")), ("spoken", ("口播",)), ("proof", ("画面",)),
           ("tags", ("素材", "标签")), ("evidence", ("证据",)))


def _fence(lines, index, reach=3):
    """Return (body lines, closing index) of the code block starting within `reach` lines after index."""
    start = index + 1
    while start < len(lines) and not lines[start].strip() and start - index <= reach:
        start += 1
    if start >= len(lines) or not lines[start].strip().startswith("```"):
        return None, index
    end = start + 1
    while end < len(lines) and not lines[end].strip().startswith("```"):
        end += 1
    return lines[start + 1:end], end


def _cells(line):
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", body)]


def _table(lines, index, reach=3):
    start = index + 1
    while start < len(lines) and not lines[start].strip() and start - index <= reach:
        start += 1
    if start >= len(lines) or not lines[start].strip().startswith("|"):
        return None, index
    end = start
    while end < len(lines) and lines[end].strip().startswith("|"):
        end += 1
    header = _cells(lines[start])
    mapping = {}
    for position, name in enumerate(header):
        for key, words in COLUMNS:
            if key not in mapping and any(word in name for word in words):
                mapping[key] = position
                break
    rows = []
    for line in lines[start + 1:end]:
        cells = _cells(line)
        if all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells if cell):
            continue
        row = {key: cells[position] if position < len(cells) else "" for key, position in mapping.items()}
        number = re.sub(r"\D", "", row.get("number", ""))
        row["number"] = int(number) if number else len(rows) + 1
        rows.append(row)
    return rows, end - 1


def _lines(block):
    return [line.strip() for line in block if line.strip()]


def parse_variants(text):
    """Return every voiced variant in a candidate, in order, with per-line records and problems."""
    lines = str(text or "").splitlines()
    variants, label, current, index = [], None, None, 0
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("```"):
            block, end = _fence(lines, index - 1, reach=0)
            index = end + 1
            continue
        found = VARIANT_LABEL.search(line)
        if found:
            label = found.group(0).strip(" *·・")
        if SPOKEN_HEADING.search(line):
            block, end = _fence(lines, index)
            if block is not None:
                current = {"index": len(variants), "label": label or f"第 {len(variants) + 1} 条",
                           "spoken": _lines(block), "chinese": [], "groups": []}
                variants.append(current)
                index = end + 1
                continue
        if current is not None and not current["chinese"] and CHINESE_HEADING.search(line):
            block, end = _fence(lines, index)
            if block is not None:
                current["chinese"] = _lines(block)
                index = end + 1
                continue
        if current is not None and not current["groups"] and TABLE_HEADING.search(line):
            rows, end = _table(lines, index)
            if rows is not None:
                current["groups"] = rows
                index = end + 1
                continue
        index += 1
    return [_finish(variant) for variant in variants]


def _finish(variant):
    spoken, chinese, groups = variant["spoken"], variant["chinese"], variant["groups"]
    problems = []
    if not spoken:
        problems.append("口播代码块是空的。")
    if chinese and len(chinese) != len(spoken):
        problems.append(f"中文 {len(chinese)} 行，口播 {len(spoken)} 行，中文未逐行对应。")
    if not chinese:
        problems.append("没有找到逐行对应的中文。")
    if not groups:
        problems.append("没有找到意群表，画面要求需在制作单里补填。")
    elif len(groups) != len(spoken) or [row["number"] for row in groups] != list(range(1, len(spoken) + 1)):
        problems.append(f"意群表 {len(groups)} 行，口播 {len(spoken)} 行，编号未一一对应，请核对。")
    by_number = {row["number"]: row for row in groups}
    records = []
    for number, line in enumerate(spoken, 1):
        row = by_number.get(number, {})
        records.append({"number": number, "spoken": line,
                        "zh": chinese[number - 1] if len(chinese) == len(spoken) else "",
                        "proof": row.get("proof", ""), "tags": row.get("tags", ""), "evidence": row.get("evidence", "")})
    return {"index": variant["index"], "label": variant["label"], "lines": records, "problems": problems}


def spoken_lines(text):
    """Lines to voice from a saved script: its spoken code block when present, else its non-empty lines."""
    found = parse_variants(text)
    if found and found[0]["lines"]:
        return [line["spoken"] for line in found[0]["lines"]]
    return [line.strip() for line in str(text or "").splitlines() if line.strip() and not line.strip().startswith("```")]


def same_lines(left, right):
    squash = lambda values: [re.sub(r"\s+", " ", value).strip() for value in values]
    return squash(left) == squash(right)


def _blocks(text):
    """Text of each voiced variant, from where its 变体 number first appears to where the next one starts."""
    lines = str(text or "").splitlines()
    starts, heads, seen, inside = [], [], set(), False
    for index, line in enumerate(lines):
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside:
            continue
        found = VARIANT_LABEL.search(line)
        if found:
            number = re.search(r"\d+", found.group(0)).group(0)
            if number not in seen:
                seen.add(number)
                starts.append(index)
        if SPOKEN_HEADING.search(line) and _fence(lines, index)[0] is not None:
            heads.append(index)
    spans = []
    for position, head in enumerate(heads):
        start = max([value for value in starts if value <= head], default=0)
        if spans and start <= spans[-1][0]:
            start = heads[position - 1] + 1
        spans.append([start, len(lines)])
    for position in range(len(spans) - 1):
        spans[position][1] = spans[position + 1][0]
    return ["\n".join(lines[start:end]) for start, end in spans]


def variant_notes(text, index):
    """开头画面, 开头屏显 and 补拍清单 of one variant, as written; empty when absent."""
    blocks = _blocks(text)
    if not isinstance(index, int) or not 0 <= index < len(blocks):
        return {"opening": "", "onscreen": "", "pickups": []}
    clean = lambda line: re.sub(r"^[\s>*\-•]+|\*\*", "", line).strip()
    lines = blocks[index].splitlines()
    opening = next((clean(line) for line in lines if "开头画面" in line), "")
    onscreen = next((clean(line) for line in lines if "开头屏显" in line), "")
    pickups, collecting = [], False
    for line in lines:
        if "补拍清单" in line:
            collecting = True
            continue
        if collecting:
            if line.strip().startswith(("-", "*", "•")) and not line.strip().startswith("**"):
                pickups.append(clean(line))
            elif line.strip():
                break
    return {"opening": opening, "onscreen": onscreen, "pickups": pickups}
