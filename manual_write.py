"""「手写新稿」: one script typed by hand becomes a completed round, with no AI involved.

The person gives a title, an optional opening screen text and the lines (口播 + 中文). They are laid out in the same
可制作格式 the writer delivers (口播 / 对应中文 / 意群表), so the candidate desk, adoption and 改稿 treat it like any other draft.
"""
from __future__ import annotations

import re

import variant_parse as parsing
from ai_tasks import SPOKEN_HEADING
from variant_parse import parse_variants

MAX_LINES = 80
MAX_CHARS = 600
MAX_TITLE = 60


class WriteError(ValueError):
    pass


def _one_line(value):
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


def clean(manual):
    """Validate what the person typed and return (title, hook, lines)."""
    if not isinstance(manual, dict):
        raise WriteError("手写稿内容格式无效。")
    title, hook = _one_line(manual.get("title")), _one_line(manual.get("hook"))
    if len(title) > MAX_TITLE or len(hook) > MAX_CHARS:
        raise WriteError("标题或开头屏显太长，请缩短。")
    rows = manual.get("lines")
    if not isinstance(rows, list) or not rows:
        raise WriteError("手写稿至少要写一句。")
    if len(rows) > MAX_LINES:
        raise WriteError(f"一条稿最多 {MAX_LINES} 句。")
    lines = []
    for number, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise WriteError("手写稿内容格式无效。")
        spoken, zh = _one_line(row.get("spoken")), _one_line(row.get("zh"))
        if len(spoken) > MAX_CHARS or len(zh) > MAX_CHARS:
            raise WriteError(f"第 {number} 句超过 {MAX_CHARS} 字符，请拆成两句。")
        if not spoken:
            raise WriteError(f"第 {number} 句还没有口播。")
        if not zh:
            raise WriteError(f"第 {number} 句还没有中文。")
        if "```" in spoken or "```" in zh:
            raise WriteError(f"第 {number} 句含有不能使用的符号。")
        lines.append({"spoken": spoken, "zh": zh})
    return title or "手写稿", hook, lines


def build(title, hook, lines):
    """The round's candidate text for one hand-written draft."""
    table = "\n".join(f"| {number} | {line['spoken'].replace('|', chr(92) + '|')} | 待补 | 待补 | 待补 |"
                      for number, line in enumerate(lines, 1))
    head = f"**变体 #1 · {title}**\n\n"
    if hook:
        head += f"开头屏显：`{hook.replace('`', '')}`（钩子）\n\n"
    text = (head + "**口播（配音用）**\n\n```\n" + "\n".join(line["spoken"] for line in lines)
            + "\n```\n\n**对应中文**\n\n```\n" + "\n".join(line["zh"] for line in lines)
            + "\n```\n\n**意群表**\n\n| # | 口播 | 画面要证明什么 | 素材标签 | 证据 |\n|---|---|---|---|---|\n" + table + "\n")
    parsed = parse_variants(text, loose=False)
    if len(parsed) != 1 or [line["spoken"] for line in parsed[0]["lines"]] != [line["spoken"] for line in lines]:
        raise WriteError("手写稿没能整理成可制作格式，请检查是否有特殊符号。")
    return text


# ---- 导入整稿: a whole delivered script pasted in one go ----

MAX_IMPORT = 60000
MAX_VARIANTS = 10
ITEM = re.compile(r"^\s*(?:\d{1,2}\s*[.、)）]|[-*•])\s+")


def _is_heading(line, pattern):
    """A short line that is only the section name, with or without its markdown decoration."""
    cleaned = re.sub(r"^[\s>#*]+|[\s*:：]+$", "", line).replace("**", "")
    return len(cleaned) <= 24 and bool(pattern.search(cleaned))


def _section_start(raw):
    return any(_is_heading(raw, pattern) for pattern in (SPOKEN_HEADING, parsing.CHINESE_HEADING, parsing.TABLE_HEADING))


def _body(lines, index, chinese):
    """The lines written under a section name that has no code block: one per row, numbering removed."""
    start = index + 1
    while start < len(lines) and not lines[start].strip() and start - index <= 3:
        start += 1
    rows, end = [], start
    while end < len(lines):
        raw = lines[end].strip()
        if not raw:
            ahead = next((lines[later].strip() for later in range(end + 1, min(end + 3, len(lines))) if lines[later].strip()), "")
            if rows and ahead and _belongs(ahead, chinese):
                end += 1
                continue
            break
        if not _belongs(raw, chinese):
            break
        rows.append(raw)
        end += 1
    if rows and all(ITEM.match(row) for row in rows):
        rows = [ITEM.sub("", row) for row in rows]
    return rows, end


def _belongs(raw, chinese):
    if raw.startswith(("#", "**", "|", "```", "---", "🔴")) or _section_start(raw):
        return False
    body = ITEM.sub("", raw)
    if body == raw and parsing._label_like(raw):
        return False
    return parsing._mostly_cjk(body) if chinese else not parsing._mostly_cjk(body)


def normalize(text):
    """The pasted text with 口播 / 对应中文 put into code blocks and a tab-separated 意群表 turned back into a table.

    Copying a rendered reply drops the code fences and the table bars; nothing else is touched and no word changes."""
    lines, out, index = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"), [], 0
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("```"):
            end = index + 1
            while end < len(lines) and not lines[end].strip().startswith("```"):
                end += 1
            out += lines[index:end + 1]
            index = end + 1
            continue
        out.append(line)
        spoken, chinese = _is_heading(line, SPOKEN_HEADING), _is_heading(line, parsing.CHINESE_HEADING)
        if (spoken or chinese) and parsing._fence(lines, index)[0] is None:
            rows, end = _body(lines, index, chinese and not spoken)
            if rows:
                out += ["", "```", *rows, "```", ""]
                index = end
                continue
        if _is_heading(line, parsing.TABLE_HEADING):
            start = index + 1
            while start < len(lines) and not lines[start].strip() and start - index <= 3:
                start += 1
            end = start
            while end < len(lines) and "\t" in lines[end] and not lines[end].strip().startswith("|"):
                end += 1
            if end > start:
                rows = [[cell.strip().replace("|", "\\|") for cell in row.split("\t")] for row in lines[start:end]]
                out += ["", "| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
                out += ["| " + " | ".join(row) + " |" for row in rows[1:]]
                out.append("")
                index = end
                continue
        index += 1
    return "\n".join(out).strip() + "\n"


def _recognize(text, title):
    if not isinstance(text, str) or not text.strip():
        raise WriteError("还没有粘贴稿子。")
    if len(text) > MAX_IMPORT:
        raise WriteError("粘贴的内容太长，请只贴稿子本身。")
    title = _one_line(title)
    if len(title) > MAX_TITLE:
        raise WriteError("标题太长，请缩短。")
    candidate = normalize(text)
    found = parse_variants(candidate, loose=False)
    if len(found) == 1 and not parsing.VARIANT_LABEL.search(candidate) and not any(parsing._heading(line) for line in candidate.splitlines()):
        candidate = f"**变体 #1 · {(title or '导入稿').replace('*', '')[:20]}**\n\n" + candidate
        found = parse_variants(candidate, loose=False)
    return candidate, found


def import_text(text, title=""):
    """(candidate text, label, line count) of a pasted delivery that the workbench splits without guessing."""
    candidate, found = _recognize(text, title)
    if not found:
        raise WriteError("没有认出「口播（配音用）」，请用预览里的「填进逐句表格」。")
    if len(found) > MAX_VARIANTS:
        raise WriteError(f"一次最多导入 {MAX_VARIANTS} 条稿。")
    for variant in found:
        if not variant["lines"]:
            raise WriteError(f"{variant['label']} 的口播是空的。")
        if len(variant["lines"]) > MAX_LINES:
            raise WriteError(f"{variant['label']} 超过 {MAX_LINES} 句。")
    label = found[0]["label"] if len(found) == 1 else f"{len(found)} 条稿"
    return candidate, label, sum(len(variant["lines"]) for variant in found)


def _fallback_rows(text):
    """Spoken and Chinese lines of a paste that has no recognisable sections, paired in the order they appear."""
    loose = parse_variants(text)
    if loose and loose[0]["lines"] and all(line["zh"] for line in loose[0]["lines"]):
        return [{"spoken": line["spoken"], "zh": line["zh"]} for line in loose[0]["lines"]][:MAX_LINES]
    spoken, chinese = [], []
    for raw in str(text or "").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith(("#", "|", "```", "---", "🔴")) or _section_start(raw):
            continue
        body = _one_line(ITEM.sub("", raw).replace("**", ""))
        if not re.search(r"[A-Za-zÀ-ÿ一-鿿]", body):
            continue
        (chinese if parsing._mostly_cjk(body) else spoken).append(body)
    count = min(max(len(spoken), len(chinese)), MAX_LINES) if spoken else 0
    return [{"spoken": spoken[position] if position < len(spoken) else "", "zh": chinese[position] if position < len(chinese) else ""}
            for position in range(count)]


def preview(text, title=""):
    """What saving this paste would produce, for the person to check before it becomes a round."""
    candidate, found = _recognize(text, title)
    if not found:
        return {"recognized": False, "rows": _fallback_rows(text), "hook": parsing.screen_copy(candidate)[0].split("\n")[0]}
    blocks, variants = parsing._blocks(candidate), []
    for variant in found:
        position = variant["index"]
        notes, swap = parsing.variant_notes(candidate, position), parsing.swap_notes(candidate, position)
        variants.append({"label": variant["label"], "lines": len(variant["lines"]),
                         "chinese": sum(1 for line in variant["lines"] if line["zh"]),
                         "table": sum(1 for line in variant["lines"] if line["proof"] or line["tags"] or line["evidence"]),
                         "first": variant["lines"][0]["spoken"] if variant["lines"] else "",
                         "screens": len(parsing.screen_options(blocks[position])) if position < len(blocks) else 0,
                         "pickups": len(notes["pickups"]), "swap": bool(swap and swap["options"]),
                         "titles": len(parsing.publish_titles(candidate, position)), "problems": variant["problems"]})
    return {"recognized": True, "variants": variants}
