"""「改稿」 on one candidate draft: the person rewrites its lines by hand and the result is saved as a new round.

Lines whose Chinese was rewritten are spoken again by the writer, in the creator's voice and with the whole draft
as context; every other line is kept word for word. The draft's 口播, 对应中文 and 意群表 blocks are replaced in the
candidate text where they stand, so the other drafts and all notes of the round stay exactly as written.
"""
from __future__ import annotations

import json
import re

import markets
from ai_tasks import SPOKEN_HEADING
from variant_parse import CHINESE_HEADING, COLUMNS, SWAP_LINE, TABLE_HEADING, _cells, _fence, parse_variants

HAN = re.compile(r"[㐀-鿿]")
MAX_LINES = 80
MAX_CHARS = 600
STALE = "待更新："
MISSING = "待补"


class EditError(ValueError):
    pass


def _one_line(value):
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


def _spans(lines):
    """Where each voiced draft's blocks sit: fence line indexes of 口播 and 对应中文, and the 意群表 rows."""
    found, current, index = [], None, 0
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("```"):
            index = _fence(lines, index - 1, reach=0)[1] + 1
            continue
        if SPOKEN_HEADING.search(line):
            body, end = _fence(lines, index)
            if body is not None:
                current = {"head": index, "spoken": (end - len(body) - 1, end), "chinese": None, "table": None}
                found.append(current)
                index = end + 1
                continue
        if current is not None and current["chinese"] is None and CHINESE_HEADING.search(line):
            body, end = _fence(lines, index)
            if body is not None:
                current["chinese"] = (end - len(body) - 1, end)
                index = end + 1
                continue
        if current is not None and current["table"] is None and TABLE_HEADING.search(line):
            start = index + 1
            while start < len(lines) and not lines[start].strip() and start - index <= 3:
                start += 1
            if start < len(lines) and lines[start].strip().startswith("|"):
                end = start
                while end < len(lines) and lines[end].strip().startswith("|"):
                    end += 1
                current["table"] = (start, end)
                index = end
                continue
        index += 1
    for position, item in enumerate(found):
        item["stop"] = found[position + 1]["head"] if position + 1 < len(found) else len(lines)
    return found


def _draft(text, index):
    variants = parse_variants(text, loose=False)
    if not variants:
        raise EditError("这一轮没有按可制作格式交付，先在「更多操作」里整理格式再改稿。")
    if type(index) is not int or not 0 <= index < len(variants):
        raise EditError("所选稿件编号无效，请刷新后重试。")
    chosen = variants[index]
    if not chosen["lines"] or any(not line["zh"] for line in chosen["lines"]):
        raise EditError("这条稿的中文没有和口播逐行对应，先整理格式再改稿。")
    return chosen


def prepare(text, index, rows):
    """Check what the person sent against the draft it edits and return the record kept with the new round.

    Each row names the line it came from (`source`, 1-based) or none for a new line; `translate` marks a line whose
    foreign words are to be written again from its Chinese."""
    draft = _draft(text, index)
    original = {line["number"]: line for line in draft["lines"]}
    if not isinstance(rows, list) or not rows:
        raise EditError("改稿至少要留一句。")
    if len(rows) > MAX_LINES:
        raise EditError(f"一条稿最多 {MAX_LINES} 句。")
    seen, lines = set(), []
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise EditError("改稿内容格式无效。")
        source = row.get("source")
        if source is not None and (type(source) is not int or source not in original or source in seen):
            raise EditError("改稿对应的原句编号无效，请刷新后重试。")
        seen.add(source)
        spoken, zh, translate = _one_line(row.get("spoken")), _one_line(row.get("zh")), row.get("translate") is True
        if len(spoken) > MAX_CHARS or len(zh) > MAX_CHARS:
            raise EditError(f"第 {number} 句超过 {MAX_CHARS} 字符，请拆成两句。")
        if not zh:
            raise EditError(f"第 {number} 句还没有中文。")
        if not translate and not spoken:
            raise EditError(f"第 {number} 句还没有口播，填上或让它重译。")
        before = original.get(source)
        line = {"source": source, "spoken": spoken, "zh": zh, "translate": translate}
        if before and (translate or before["spoken"] != spoken or before["zh"] != zh):
            line["before"] = {"spoken": before["spoken"], "zh": before["zh"]}
        proof = proof_of(row, (before or {}).get("proof"))
        if proof:
            line["proof"] = proof
        lines.append(line)
    removed = [{"number": number, "spoken": line["spoken"], "zh": line["zh"]}
               for number, line in original.items() if number not in seen]
    moved = [line["source"] for line in lines] != list(original)
    if not moved and not removed and not any("before" in line or "proof" in line for line in lines):
        raise EditError("没有改动，不需要保存。")
    return {"variant_index": index, "label": draft["label"], "lines": lines, "removed": removed}


def proof_of(row, before=""):
    """A new 画面要求 sent with a line (e.g. taken from 口播审查), or "" when none was sent or it is unchanged."""
    proof = _one_line(row.get("proof")) if isinstance(row, dict) else ""
    if len(proof) > MAX_CHARS:
        raise EditError("画面要求太长，请缩短。")
    return proof if proof and proof != _one_line(before or "") else ""


def pending(record):
    """New 1-based numbers of the lines still waiting for the writer."""
    return [number for number, line in enumerate(record["lines"], 1) if line["translate"]]


def prompt(record, market):
    """Ask for the marked lines only, with the whole draft around them."""
    item = markets.MARKETS[markets.stored(market)]
    rows = []
    for number, line in enumerate(record["lines"], 1):
        if line["translate"]:
            was = (line.get("before") or {}).get("spoken") or line["spoken"]
            rows.append(f"{number}. 【重写】中文：{line['zh']}" + (f"\n   改之前的口播（只作参考，意思已经变了）：{was}" if was else ""))
        else:
            rows.append(f"{number}. {line['spoken']}\n   中文：{line['zh']}")
    numbers = "、".join(str(number) for number in pending(record))
    return f"""你在帮一位 TikTok 带货达人改口播稿。用户已经用中文理顺了这条稿的逻辑，现在只需要把标了【重写】的句子按它的中文意思重新说成{item['language']}口播，其余句子一个字都不动。

目标市场：{item['label']}。达人口吻、口语化和这个市场的措辞，按已加载的写稿技能（「故事感与第一人称」「开头、中段与收口」和「目标市场」）执行。

整条稿（按顺序，一行一个意群）：
{chr(10).join(rows)}

要求：
1. 只写第 {numbers} 句。每句只出一行，不拆成两句，也不和别的句子合并。
2. 按中文的意思写，不是逐字翻译：要像这位达人对着镜头顺口说出来的话，和上一句、下一句接得上（可以用自然的衔接词，不要重复邻句已经说过的词）。
3. 中文里的每个信息点都要在，不加中文没有的卖点、数字、价格、承诺或活动，也不漏掉。数字和型号照中文写。
4. 语气、人称和用词跟没改的句子保持一致；长度和邻句相当，念起来不拖。
5. 只放要念的词，不加引号、编号、括号动作或表情。

只输出一个 JSON，不要任何别的文字：
{{"lines":[{{"number":句子编号,"spoken":"{item['language']}口播"}}]}}
"""


def answer(record, output):
    """Fill the writer's lines into the record; anything other than exactly the lines asked for is refused."""
    text = str(output or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    body = fenced.group(1) if fenced else text[text.find("{"):text.rfind("}") + 1] if "{" in text and "}" in text else ""
    try:
        data = json.loads(body)
    except ValueError:
        raise EditError("AI 没按约定格式交回重译的句子，可以回到原稿再提交一次。") from None
    rows = data.get("lines") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise EditError("AI 没按约定格式交回重译的句子，可以回到原稿再提交一次。")
    written = {row.get("number"): _one_line(row.get("spoken")) for row in rows if isinstance(row, dict)}
    result = {**record, "lines": [dict(line) for line in record["lines"]]}
    for number in pending(record):
        spoken = written.get(number, "")
        if not spoken:
            raise EditError(f"AI 没有交回第 {number} 句，可以回到原稿再提交一次。")
        if HAN.search(spoken):
            raise EditError(f"AI 交回的第 {number} 句里混有中文，没有保存；可以回到原稿再提交一次。")
        if len(spoken) > MAX_CHARS:
            raise EditError(f"AI 交回的第 {number} 句太长，没有保存；可以回到原稿再提交一次。")
        result["lines"][number - 1].update(spoken=spoken, translate=False, translated=True)
    return result


def _table_rows(lines, span, record, original):
    """The 意群表 for the edited lines: a kept line keeps its row, a reworded one is marked 待更新, a new one 待补."""
    start, end = span
    header = _cells(lines[start])
    mapping = {}
    for position, name in enumerate(header):
        for key, words in COLUMNS:
            if key not in mapping and any(word in name for word in words):
                mapping[key] = position
                break
    if "spoken" not in mapping:
        return None
    body, rule = [], None
    for raw in lines[start + 1:end]:
        cells = _cells(raw)
        if all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells if cell):
            rule = rule or raw
            continue
        body.append(cells)
    by_number = {}
    for position, cells in enumerate(body, 1):
        digits = re.sub(r"\D", "", cells[mapping["number"]]) if "number" in mapping and mapping["number"] < len(cells) else ""
        by_number.setdefault(int(digits) if digits else position, cells)
    out = [lines[start], rule or "|" + "---|" * len(header)]
    for number, line in enumerate(record["lines"], 1):
        kept = by_number.get(line["source"])
        cells = list(kept) if kept else [""] * len(header)
        cells += [""] * (len(header) - len(cells))
        if "number" in mapping:
            cells[mapping["number"]] = str(number)
        cells[mapping["spoken"]] = line["spoken"].replace("|", "\\|")
        if "proof" in mapping and line.get("proof"):
            # A picture sent with the line replaces the row's, whether or not the words changed.
            cells[mapping["proof"]] = line["proof"].replace("|", "\\|")
        elif "proof" in mapping:
            proof = cells[mapping["proof"]]
            if not kept or not proof:
                cells[mapping["proof"]] = MISSING
            elif original[line["source"]]["spoken"] != line["spoken"] and not proof.startswith(STALE):
                cells[mapping["proof"]] = STALE + proof
        out.append("| " + " | ".join(cells) + " |")
    return out


def build(text, record):
    """The round's candidate text with the edited draft's blocks replaced; nothing else moves."""
    if pending(record):
        raise EditError("还有句子没有重译完。")
    index = record["variant_index"]
    draft = _draft(text, index)
    original = {line["number"]: line for line in draft["lines"]}
    lines = str(text).splitlines()
    spans = _spans(lines)
    if index >= len(spans) or spans[index]["chinese"] is None:
        raise EditError("这条稿的中文没有和口播逐行对应，先整理格式再改稿。")
    span = spans[index]
    # A 可换句 note names its line by number; the number follows the line to where it now stands.
    where = {line["source"]: number for number, line in enumerate(record["lines"], 1) if line["source"]}
    for position in range(span["chinese"][1] + 1, span["stop"]):
        named = SWAP_LINE.search(lines[position])
        if named and "备选" not in lines[position] and int(named.group(1)) in where:
            lines[position] = lines[position][:named.start(1)] + str(where[int(named.group(1))]) + lines[position][named.end(1):]
    replacements = [(span["spoken"], [line["spoken"] for line in record["lines"]], True),
                    (span["chinese"], [line["zh"] for line in record["lines"]], True)]
    if span["table"]:
        rows = _table_rows(lines, span["table"], record, original)
        if rows:
            replacements.append((span["table"], rows, False))
    # Later blocks first, so the earlier ones keep their line numbers.
    for (start, end), body, fenced in sorted(replacements, key=lambda item: -item[0][0]):
        if fenced:
            lines[start + 1:end] = body
        else:
            lines[start:end] = body
    result = "\n".join(lines) + ("\n" if str(text).endswith("\n") else "")
    check = parse_variants(result, loose=False)
    if len(check) != len(spans) or [line["spoken"] for line in check[index]["lines"]] != [line["spoken"] for line in record["lines"]]:
        raise EditError("改稿没能写回这一轮的格式，原稿保持不变。")
    return result
