"""Split a finished writing candidate into voiced variants without repairing any text.

The writing skill delivers each voiced script as a "口播（配音用）" code block with one meaning group
per line, then "对应中文" and an "意群表" (# | 口播 | 画面要证明什么 | 素材标签 | 证据). Mismatches are
reported as problems for the user to check; the parser never guesses missing lines.

When a candidate ignores that format (for example an "English" heading over quoted paragraphs), a loose pass
splits it by its 版本/变体 headings, takes the spoken and Chinese text as written and breaks paragraphs into
sentences. Loose variants are flagged so the user checks every line before voicing; no word is changed.
"""
from __future__ import annotations

import json
import re

from ai_tasks import SPOKEN_HEADING, VARIANT_LABEL
from carousel_plan import normalize_plan, slide_fields
from carousel_lineage import slide_lineage
from elevenlabs_client import VoiceError

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


def parse_variants(text, loose=True):
    """Return every voiced variant in a candidate, in order, with per-line records and problems.

    Strict "口播（配音用）" blocks win; only when none exist does the loose pass run (unless loose=False).
    """
    found = _strict_variants(text)
    if found or not loose:
        return found
    return [_finish(variant) for variant in _loose_variants(text)]


def _strict_variants(text):
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
        elif _heading(line):
            label = _heading(line)
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
    problems = [variant["loose"]] if variant.get("loose") else []
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
    return {"index": variant["index"], "label": variant["label"], "lines": records, "problems": problems,
            "loose": bool(variant.get("loose"))}


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
    if not _strict_variants(text):
        return ["\n".join(lines[start:end]) for start, end in (variant["span"] for variant in _loose_variants(text))]
    starts, heads, seen, inside, titles = [], [], set(), False, []
    for index, line in enumerate(lines):
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside:
            continue
        # Variants named 版本 A/B/C or headed by a markdown heading carry no 变体 number.
        if line.strip().startswith("#") or _heading(line):
            titles.append(index)
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
        previous = heads[position - 1] if position else -1
        heading = [value for value in titles if previous < value <= head]
        start = heading[0] if heading else max([value for value in starts if value <= head], default=0)
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


def _title_line(stripped):
    """A variant's own heading or 标题行 (方向编号 · 变体/版本 · 推进结构), shown by the workbench elsewhere."""
    if SPOKEN_HEADING.search(stripped) or re.fullmatch(r"(?:---+|\*\*\*+)", stripped):
        return bool(re.fullmatch(r"(?:---+|\*\*\*+)", stripped))
    if stripped.startswith("#") or _heading(stripped):
        return True
    bold = re.fullmatch(r"\*\*([^*]+)\*\*", stripped)
    return bool(bold and (VARIANT_LABEL.search(bold.group(1)) or LOOSE_VARIANT.search(bold.group(1)) or "·" in bold.group(1)))


def variant_script(text, index):
    """Markdown of one variant as written, without its carousel part and without the 意群表 (the job keeps its own)."""
    blocks = _blocks(text)
    if not isinstance(index, int) or not 0 <= index < len(blocks):
        return ""
    lines = blocks[index].splitlines()
    out, position, seen_label = [], 0, False
    while position < len(lines):
        line, stripped = lines[position], lines[position].strip()
        if stripped.startswith("```"):
            end = position + 1
            while end < len(lines) and not lines[end].strip().startswith("```"):
                end += 1
            fence = "\n".join(lines[position:end + 1])
            if not ('"photo_carousel"' in fence or "'photo_carousel'" in fence):
                out.extend(lines[position:end + 1])
            position = end + 1
            continue
        if not seen_label and stripped:
            if _title_line(stripped):
                position += 1
                continue
            seen_label = True
        if stripped.startswith(("**", "#")) and "图文" in stripped:
            break
        if stripped.startswith("|") and "叠字" in stripped:
            break
        if stripped.startswith(("**", "#")) and TABLE_HEADING.search(stripped):
            position += 1
            while position < len(lines) and (not lines[position].strip() or lines[position].strip().startswith("|")):
                position += 1
            continue
        out.append(line)
        position += 1
    while out and (not out[-1].strip() or re.fullmatch(r"\s*(?:---+|\*\*\*+)\s*", out[-1])):
        out.pop()
    while out and (not out[0].strip() or re.fullmatch(r"\s*(?:---+|\*\*\*+)\s*", out[0])):
        out.pop(0)
    return "\n".join(out)


CAROUSEL_COLUMNS = (("source_lines", ("原稿意群",)), ("proof_goal", ("本张任务",)),
                    ("visual_method", ("表达方式",)), ("evidence_requirement", ("画面证据",)),
                    ("number", ("张",)), ("role", ("作用",)), ("overlay", ("叠字",)), ("body", ("正文",)),
                    ("zh", ("中文",)), ("source", ("素材",)), ("forbidden", ("禁写",)), ("stage", ("环节",)), ("shot", ("景别",)))


def _carousel_table(lines):
    """The per-slide table (张 | 作用 | 叠字 | … | 素材来源 | 禁写) of a carousel, keyed by slide number."""
    for index, line in enumerate(lines):
        if not (line.strip().startswith("|") and "叠字" in line and "张" in line):
            continue
        header = _cells(line)
        mapping = {}
        for position, name in enumerate(header):
            for key, words in CAROUSEL_COLUMNS:
                if key not in mapping and any(word in name for word in words):
                    mapping[key] = position
                    break
        rows = {}
        for row_line in lines[index + 1:]:
            if not row_line.strip().startswith("|"):
                break
            cells = _cells(row_line)
            if all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells if cell):
                continue
            row = {key: cells[position] if position < len(cells) else "" for key, position in mapping.items()}
            number = re.sub(r"\D", "", row.get("number", ""))
            if number:
                rows[int(number)] = row
        return rows
    return None


def parse_carousels(text):
    """Every variant that carries a 图文版: its slides (copy.json merged with the per-slide table) and captions."""
    found = []
    pairs = [(variant["index"], variant["label"], block) for variant, block in zip(parse_variants(text), _blocks(text))]
    if not any(_carousel_copy(block) or _carousel_table(block.splitlines()) for _, _, block in pairs):
        # A carousel-only candidate has no spoken block: fall back to its 版本/变体 sections.
        pairs = _carousel_sections(text)
    for index, label, block in pairs:
        copy, slides, problems = _carousel_copy(block), [], []
        rows = _carousel_table(block.splitlines())
        if copy is None:
            if not rows:
                continue
            copy = _copy_from_table(rows)
            problems.append("没有找到图文 JSON（copy.json），已按逐张表拆出叠字和正文；发布文案请在制作单里补填。")
        for position, item in enumerate(copy["slides"], 1):
            item = item if isinstance(item, dict) else {}
            number = item.get("number") if type(item.get("number")) is int else position
            row = (rows or {}).get(number, {})
            lines = [str(line).strip() for line in item.get("title_lines") or [] if str(line).strip()]
            fields, wrong = slide_fields({"number": number, "stage": item.get("stage") or row.get("stage", ""),
                                          "shot": item.get("shot") or row.get("shot", "")})
            problems += wrong
            lineage = {key: item[key] if key in item else row[key]
                       for key in ("source_lines", "proof_goal", "visual_method", "evidence_requirement")
                       if key in item or key in row}
            # The JSON uses integer arrays; the table may write [1,2] or 1、2. Never
            # strip signs or guess numbers from prose such as "unknown / group 2".
            if "source_lines" not in item and isinstance(lineage.get("source_lines"), str):
                raw = lineage["source_lines"].strip()
                try:
                    lineage["source_lines"] = json.loads(raw)
                except (ValueError, TypeError):
                    if re.fullmatch(r"[0-9]+(?:\s*[,，、]\s*[0-9]+)*", raw):
                        lineage["source_lines"] = [int(value) for value in re.split(r"\s*[,，、]\s*", raw)]
                if type(lineage.get("source_lines")) is int:
                    lineage["source_lines"] = [lineage["source_lines"]]
            try:
                fields.update(slide_lineage(lineage, strict=True))
            except VoiceError as exc:
                problems.append(f"第 {number} 张原稿对应无效：{exc}")
                # Keep the submitted data so creation cannot silently discard an
                # illegal mapping and treat it as a compatible historical slide.
                fields.update(lineage)
            slides.append({"number": number, "title_lines": lines, "body": str(item.get("body") or "").strip(),
                           "title_zh": str(item.get("title_zh") or "").strip(), "body_zh": str(item.get("body_zh") or "").strip(),
                           "role": row.get("role", ""), "source": row.get("source", ""), "forbidden": row.get("forbidden", ""),
                           **fields})
        if rows is None:
            problems.append("没有找到逐张表，作用、素材来源和禁写请在制作单里补填。")
        elif sorted(rows) != [slide["number"] for slide in slides]:
            problems.append(f"逐张表 {len(rows)} 行，图文 JSON {len(slides)} 张，编号没有一一对应，请核对。")
        if not 3 <= len(slides) <= 8:
            problems.append(f"图文共 {len(slides)} 张，通常是 3–5 张，请核对。")
        if any(not slide["title_lines"] for slide in slides):
            problems.append("有的张没有叠字标题。")
        plan, wrong = normalize_plan(copy.get("plan"))
        problems += wrong
        found.append({"index": index, "label": label, "slides": slides, "plan": plan,
                      "caption_en": str(copy.get("caption_en") or "").strip(), "caption_zh": str(copy.get("caption_zh") or "").strip(),
                      "copy": copy, "problems": problems})
    return found


def _carousel_copy(block):
    """The photo_carousel JSON of a block, in a ```json fence or an untagged fence."""
    for match in re.finditer(r"```(?:json|JSON)?\s*(\{.*?\})\s*```", block, re.S):
        try:
            value = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("content_type") == "photo_carousel" and isinstance(value.get("slides"), list):
            return value
    return None


def _copy_from_table(rows):
    """copy.json rebuilt from the per-slide table alone; text is taken as written, the caption left empty."""
    slides = []
    for number in sorted(rows):
        row = rows[number]
        overlay = re.sub(r"<br\s*/?>", " / ", row.get("overlay", ""), flags=re.I)
        slides.append({"number": number, "title_lines": [part.strip() for part in overlay.split("/") if part.strip()],
                       "body": row.get("body", ""), "title_zh": row.get("zh", ""), "body_zh": ""})
    return {"content_type": "photo_carousel", "slides": slides, "caption_en": "", "caption_zh": ""}


def _carousel_sections(text):
    lines = str(text or "").splitlines()
    inside, starts = False, []
    for index, line in enumerate(lines):
        if line.strip().startswith("```"):
            inside = not inside
            continue
        label = None if inside else _heading(line)
        if label:
            starts.append((index, label))
    if not starts or starts[0][0] > 0:
        starts.insert(0, (0, None))
    return [(position, label or f"第 {position + 1} 条",
             "\n".join(lines[start:starts[position + 1][0] if position + 1 < len(starts) else len(lines)]))
            for position, (start, label) in enumerate(starts)]


def carousel_text(item, chinese=False):
    """Plain-text form of a carousel stored as the adopted script version (one block per slide, then the caption)."""
    lines = []
    for slide in item["slides"]:
        title = slide["title_zh"] if chinese else " / ".join(slide["title_lines"])
        body = slide["body_zh"] if chinese else slide["body"]
        lines.append(f"{slide['number']:02d} {title}".rstrip())
        if body:
            lines.append(body)
    caption = item["caption_zh"] if chinese else item["caption_en"]
    if caption:
        lines += ["", ("发布文案：" if chinese else "Caption: ") + caption]
    return "\n".join(lines).strip()


# ---- format check and re-layout verification ----

WORD = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)*")


def _words(text):
    return WORD.findall(str(text or "").lower().replace("’", "'").replace("‘", "'"))


def _contains(haystack, needle):
    """Whether needle occurs in haystack as one contiguous run of words."""
    if not needle:
        return True
    size = len(needle)
    return any(haystack[start] == needle[0] and haystack[start:start + size] == needle
               for start in range(len(haystack) - size + 1))


def _han(text):
    return re.sub(r"[^一-鿿A-Za-z0-9]", "", str(text or ""))


def delivery_ready(text, carousel=False):
    """Whether a candidate already follows the code-block delivery format the workbench splits without guessing."""
    if not parse_variants(text, loose=False):
        return False
    if not carousel:
        return True
    blocks = _blocks(text)
    # Only a per-slide table missing its JSON can be re-laid out; a candidate without any carousel cannot.
    return any(_carousel_copy(block) for block in blocks) or not any(_carousel_table(block.splitlines()) for block in blocks)


def looks_like_script(text):
    """A candidate worth re-laying out: loosely recognisable variants, or a fair amount of English to voice."""
    return bool(parse_variants(text)) or len(_words(re.sub(r"```.*?```", " ", str(text or ""), flags=re.S))) >= 40


def reformat_problems(original, formatted, carousel=False):
    """Why a re-laid-out candidate cannot replace the original; empty when no spoken or slide word changed."""
    found = parse_variants(formatted, loose=False)
    if not found:
        return ["整理稿仍没有「口播（配音用）」代码块。"]
    problems, source_words, source_han = [], _words(original), _han(original)
    expected = len(parse_variants(original))
    if expected and len(found) < expected:
        problems.append(f"原稿识别到 {expected} 条，整理稿只有 {len(found)} 条。")
    for variant in found:
        spoken = [line["spoken"] for line in variant["lines"]]
        if not _contains(source_words, _words(" ".join(spoken))):
            problems.append(f"{variant['label']} 的口播和原稿逐字对不上。")
        if any(line["zh"] and _han(line["zh"]) not in source_han for line in variant["lines"]):
            problems.append(f"{variant['label']} 的中文和原稿对不上。")
    if carousel:
        for item in parse_carousels(formatted):
            for slide in item["slides"]:
                if not (_contains(source_words, _words(" ".join(slide["title_lines"]))) and _contains(source_words, _words(slide["body"]))):
                    problems.append(f"{item['label']} 第 {slide['number']} 张的叠字或正文和原稿对不上。")
    return problems


# ---- loose pass: candidates that ignored the "口播（配音用）" code-block format ----

LOOSE_VARIANT = re.compile(r"(?:版本|变体|发散|稿|Version|Variant)\s*[#＃]?\s*(?:[A-Za-z](?![A-Za-z])|\d+|[一二三四五六七八九十]+)"
                           r"|第\s*(?:\d+|[一二三四五六七八九十]+)\s*(?:条|版|稿)", re.I)
LOOSE_SPOKEN = re.compile(r"(?:English|英文|口播|配音|Voice\s*-?\s*over|VO|Script|Spoken)"
                          r"(?:\s*(?:口播|稿|文案|正文|版|script|version|text))?(?:\s*[（(][^）)]{0,20}[）)])?", re.I)
LOOSE_CHINESE = re.compile(r"(?:对应)?中文(?:对应|翻译|版|稿)?(?:\s*[（(][^）)]{0,20}[）)])?|Chinese|译文|翻译")
CJK = re.compile(r"[一-鿿]")
ABBREVIATIONS = {"mr.", "mrs.", "ms.", "dr.", "st.", "vs.", "e.g.", "i.e.", "etc.", "approx.", "no."}


def _clean(line):
    """A line with its markdown decoration removed, for matching labels such as **English**: or ## 版本 A."""
    value = re.sub(r"^[\s>#]+", "", line).replace("**", "").replace("__", "").strip()
    return re.sub(r"[\s:：]+$", "", value)


def _heading(line):
    """Label of a variant heading (a markdown heading or bold line that starts with 版本/变体/Version …)."""
    raw = line.strip()
    if not raw.startswith(("#", "**")):
        return None
    cleaned = _clean(raw)
    if not LOOSE_VARIANT.match(cleaned):
        return None
    return cleaned[:30].rstrip(" :：·・")


def _label_like(raw):
    cleaned = _clean(raw)
    return (raw.startswith(("#", "**", "|", "```", "---")) or bool(re.match(r"[一-鿿]{1,10}\s*[:：]", raw))
            or bool(LOOSE_SPOKEN.fullmatch(cleaned)) or bool(LOOSE_CHINESE.fullmatch(cleaned)) or bool(TABLE_HEADING.search(cleaned)))


def _mostly_cjk(text):
    letters = re.findall(r"[A-Za-z]|[一-鿿]", text)
    return bool(letters) and len(CJK.findall(text)) * 2 > len(letters)


def _loose_content(lines, index, stop, english):
    """Paragraphs under a label: a code block (lines kept as they are), quoted paragraphs or plain paragraphs.

    Returns (paragraphs, splittable, last index read); code-block lines are not split any further.
    """
    start = index + 1
    while start < stop and not lines[start].strip():
        start += 1
    if start >= stop:
        return [], False, index
    first = lines[start].strip()
    if first.startswith("```"):
        end = start + 1
        while end < stop and not lines[end].strip().startswith("```"):
            end += 1
        return _lines(lines[start + 1:end]), False, end
    quoted = first.startswith(">")
    stops = lambda raw: _label_like(raw) or raw.startswith(">") or (english and _mostly_cjk(raw))
    if not quoted and stops(first):
        return [], False, index
    paragraphs, current, end = [], [], start
    while end < stop:
        raw = lines[end].strip()
        if quoted:
            if raw.startswith(">"):
                body = raw.lstrip(">").strip()
            elif not raw and end + 1 < stop and lines[end + 1].strip().startswith(">"):
                body = ""
            else:
                break
        else:
            if raw and stops(raw):
                break
            if not raw:
                following = next((lines[ahead].strip() for ahead in range(end + 1, stop) if lines[ahead].strip()), "")
                if not following or stops(following):
                    break
            body = raw
        if body:
            current.append(body)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
        end += 1
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs, True, end - 1


def _sentences(paragraph):
    """English sentences of a paragraph, split at . ! ? … before a capital or digit; common abbreviations kept."""
    words, sentences, current = paragraph.split(), [], []
    for position, word in enumerate(words):
        current.append(word)
        following = words[position + 1] if position + 1 < len(words) else ""
        ends = re.search(r"[.!?…][\"'”’)\]]*$", word) and word.lower() not in ABBREVIATIONS
        if ends and re.match(r"[\"'“‘(\[]?[A-Z0-9]", following):
            sentences.append(" ".join(current))
            current = []
    if current:
        sentences.append(" ".join(current))
    return sentences


def _sentences_zh(paragraph):
    parts = re.findall(r"[^。！？!?…]+[。！？!?…]*[”」』）)]*", paragraph)
    return [part.strip() for part in parts if part.strip()] or [paragraph.strip()]


def _pair(spoken, chinese, splittable):
    """Meaning groups: sentences where English and Chinese agree paragraph by paragraph, else whole paragraphs."""
    if not splittable:
        return spoken, chinese
    if len(chinese) == len(spoken):
        groups, translated = [], []
        for english, zh in zip(spoken, chinese):
            parts, zh_parts = _sentences(english), _sentences_zh(zh)
            if len(parts) == len(zh_parts):
                groups += parts
                translated += zh_parts
            else:
                groups.append(english)
                translated.append(zh)
        return groups, translated
    return ([part for english in spoken for part in _sentences(english)],
            [part for zh in chinese for part in _sentences_zh(zh)])


def _loose_variants(text):
    """Variants of a candidate without a "口播（配音用）" block; each keeps its source span and a warning."""
    lines = str(text or "").splitlines()
    inside, fenced = False, []
    for line in lines:
        if line.strip().startswith("```"):
            fenced.append(True)
            inside = not inside
            continue
        fenced.append(inside)
    starts = [(index, _heading(line)) for index, line in enumerate(lines) if not fenced[index] and _heading(line)]
    if not starts or starts[0][0] > 0:
        starts.insert(0, (0, None))
    sections = [(start, starts[position + 1][0] if position + 1 < len(starts) else len(lines), label)
                for position, (start, label) in enumerate(starts)]
    variants = []
    for start, stop, label in sections:
        heads = [index for index in range(start, stop) if not fenced[index] and not lines[index].strip().startswith(">")
                 and LOOSE_SPOKEN.fullmatch(_clean(lines[index]))]
        for position, head in enumerate(heads):
            limit = heads[position + 1] if position + 1 < len(heads) else stop
            spoken, splittable, end = _loose_content(lines, head, limit, english=True)
            spoken = [line for line in spoken if not _mostly_cjk(line)]
            if not spoken:
                continue
            chinese, groups, index = [], [], end + 1
            while index < limit:
                cleaned = _clean(lines[index])
                if not chinese and not fenced[index] and LOOSE_CHINESE.fullmatch(cleaned):
                    chinese, _, close = _loose_content(lines, index, limit, english=False)
                    index = close + 1
                    continue
                if not groups and not fenced[index] and TABLE_HEADING.search(cleaned):
                    rows, close = _table(lines, index)
                    if rows is not None:
                        groups = rows
                        index = close + 1
                        continue
                index += 1
            if not splittable:
                chinese = [part for zh in chinese for part in _sentences_zh(zh)] if len(chinese) != len(spoken) else chinese
            spoken, chinese = _pair(spoken, chinese, splittable)
            heading = _clean(lines[head])
            variants.append({"index": len(variants), "label": label or f"第 {len(variants) + 1} 条",
                             "spoken": spoken, "chinese": chinese, "groups": groups,
                             "span": (start if position == 0 else head, limit),
                             "loose": f"没有「口播（配音用）」代码块，已按「{heading}」{'段落' if splittable else '代码块'}宽松识别"
                                      f"{'，意群按句拆分' if splittable else ''}；请逐行核对后再配音。"})
    return variants


def publish_titles(text, index):
    """The 🔴 title-and-hashtag lines written for one variant, one copyable line each.

    Each variant's lines sit together under its 发布文案; when the groups line up with the voiced variants the
    group at the variant's index is it, and a single group belongs to the only variant there is."""
    groups, current, inside = [], [], False
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            inside = not inside
            continue
        if stripped.startswith("🔴"):
            current.append(stripped)
        elif stripped and current and not inside:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    if len(groups) == 1:
        return groups[0]
    if isinstance(index, int) and 0 <= index < len(groups) and len(groups) == len(_blocks(text)):
        return groups[index]
    return []
