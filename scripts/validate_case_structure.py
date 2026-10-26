#!/usr/bin/env python3
"""Validate version 1 case documents against the live case_template.md.

The numbered H2 headings, required H3 blocks, and table contracts have one
owner: the template. This module keeps no second list of section or field
names. Standalone exports use the same validation via --case-file.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = SKILL_ROOT / "sources" / "video-cases" / "case_template.md"
SUPPORTED_VERSION = 1
SECTION_COUNT = 8
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*#*\s*$")
NUMBERED_HEADING_RE = re.compile(r"^(\d+)\.\s+\S")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
TIMESTAMP_RE = re.compile(
    r"^\s*\[\s*\d+(?:\.\d+)?s?\s*[-–—]\s*\d+(?:\.\d+)?s?\s*\]\s*\S",
    re.MULTILINE,
)
VARIABLE_RE = re.compile(r"(?<!\{)\{[^{}\n]+\}(?!\})")
FIXED_ROWS_MARKER = "<!-- fixed-rows -->"
SKELETON_SEGMENT_RE = re.compile(r"^\s*\[([^\]\n|｜]+)[|｜]([^\]\n|｜]+)\]\s*$", re.MULTILINE)
SKELETON_VISUAL_RE = re.compile(r"^\s*画面\s*[:：][ \t]*\S", re.MULTILINE)


@dataclass(frozen=True)
class CodeBlock:
    start: int
    end: int
    content: str


@dataclass(frozen=True)
class MarkdownView:
    lines: tuple[str, ...]
    visible: tuple[str, ...]
    code_blocks: tuple[CodeBlock, ...]
    unclosed_fence: bool


@dataclass(frozen=True)
class Table:
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    line: int


@dataclass(frozen=True)
class TableRequirement:
    header: tuple[str, ...]
    fixed_rows: tuple[str, ...] | None = None


@dataclass(frozen=True)
class SectionRequirement:
    number: int
    heading: str
    table_headers: tuple[tuple[str, ...], ...]
    block_headings: tuple[str, ...] = ()
    required_tables: tuple[TableRequirement, ...] = ()


@dataclass(frozen=True)
class TemplateSchema:
    sections: tuple[SectionRequirement, ...]


def markdown_view(text: str) -> MarkdownView:
    """Hide fenced code from structural parsing, retaining its actual content."""
    lines = tuple(text.splitlines())
    visible = list(lines)
    blocks = []
    fence = None
    start = 0
    for i, line in enumerate(lines):
        match = FENCE_RE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)
                start = i
                visible[i] = ""
            continue
        visible[i] = ""
        if (match and match.group(1)[0] == fence[0]
                and len(match.group(1)) >= len(fence)
                and not match.group(2).strip()):
            blocks.append(CodeBlock(start, i, "\n".join(lines[start + 1:i])))
            fence = None
    return MarkdownView(lines, tuple(visible), tuple(blocks), fence is not None)


def split_frontmatter(text: str) -> tuple[str | None, str]:
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:])
    return None, text


def scalar_field(frontmatter: str, name: str) -> object:
    """Read only the two simple scalars needed by standalone structure checks."""
    values = re.findall(rf"^{re.escape(name)}:\s*(.*?)\s*$", frontmatter, re.MULTILINE)
    if len(values) != 1:
        return None
    value = re.sub(r"\s+#.*$", "", values[0]).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    if re.fullmatch(r"\d+", value):
        return int(value)
    return value


def headings(view: MarkdownView, level: int) -> list[tuple[int, str]]:
    result = []
    for i, line in enumerate(view.visible):
        match = HEADING_RE.match(line)
        if match and len(match.group(1)) == level:
            result.append((i, match.group(2)))
    return result


def table_row(line: str) -> tuple[str, ...] | None:
    value = line.strip()
    if "|" not in value:
        return None
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|") and not value.endswith("\\|"):
        value = value[:-1]
    return tuple(cell.strip() for cell in re.split(r"(?<!\\)\|", value))


def normalized_cell(cell: str) -> str:
    return re.sub(r"\s+", " ", cell.replace("**", "").replace("`", "")).strip()


def meaningful_content(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*(?:---+|\*\*\*+)\s*$", "", text).strip()


def has_complete_skeleton_segment(block: str) -> bool:
    segments = list(SKELETON_SEGMENT_RE.finditer(block))
    for index, match in enumerate(segments):
        if not all(group.strip() for group in match.groups()):
            continue
        end = segments[index + 1].start() if index + 1 < len(segments) else len(block)
        content = block[match.end():end]
        if VARIABLE_RE.search(content) and SKELETON_VISUAL_RE.search(content):
            return True
    return False


def is_separator(row: tuple[str, ...]) -> bool:
    return bool(row) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in row)


def tables(lines: tuple[str, ...], start: int, end: int) -> list[Table]:
    found = []
    i = start
    while i + 1 < end:
        header = table_row(lines[i])
        separator = table_row(lines[i + 1])
        if (not header or not separator or len(header) != len(separator)
                or not is_separator(separator)):
            i += 1
            continue
        rows = []
        j = i + 2
        while j < end:
            row = table_row(lines[j])
            if row is None:
                break
            rows.append(row)
            j += 1
        found.append(Table(tuple(normalized_cell(c) for c in header), tuple(rows), i))
        i = j
    return found


def load_template_schema(path: Path = DEFAULT_TEMPLATE) -> TemplateSchema:
    view = markdown_view(path.read_text(encoding="utf-8-sig"))
    if view.unclosed_fence:
        raise ValueError(f"模板 {path} 含未闭合的代码围栏")
    h2 = headings(view, 2)
    numbered = [(i, title) for i, title in h2 if NUMBERED_HEADING_RE.match(title)]
    numbers = [int(NUMBERED_HEADING_RE.match(title).group(1)) for _, title in numbered]
    if numbers != list(range(1, SECTION_COUNT + 1)):
        raise ValueError(f"模板必须含依次编号 1–{SECTION_COUNT} 的实际 H2 标题；当前为 {numbers}")
    sections = []
    for start, title in numbered:
        end = next((i for i, _ in h2 if i > start), len(view.lines))
        required_tables = []
        for table in tables(view.visible, start + 1, end):
            previous = table.line - 1
            while previous > start and not view.visible[previous].strip():
                previous -= 1
            fixed_rows = None
            if view.visible[previous].strip() == FIXED_ROWS_MARKER:
                if (not table.rows or len(table.header) < 2
                        or any(len(row) != len(table.header) for row in table.rows)):
                    raise ValueError(f"模板 {path} 第 {table.line + 1} 行固定字段表无有效数据行")
                fixed_rows = tuple(normalized_cell(row[0]) for row in table.rows)
                if not all(fixed_rows) or len(set(fixed_rows)) != len(fixed_rows):
                    raise ValueError(f"模板 {path} 第 {table.line + 1} 行固定字段表含空白或重复字段")
            required_tables.append(TableRequirement(table.header, fixed_rows))
        sections.append(SectionRequirement(
            int(NUMBERED_HEADING_RE.match(title).group(1)), title,
            tuple(t.header for t in required_tables),
            tuple(heading for line, heading in headings(view, 3) if start < line < end),
            tuple(required_tables),
        ))
    return TemplateSchema(tuple(sections))


def validate_case_structure(
    text: str,
    *,
    schema: TemplateSchema | None = None,
    template_path: Path = DEFAULT_TEMPLATE,
    strict_content: bool = False,
) -> list[str]:
    """Return structure errors; strict mode also checks template content carriers.

    The default preserves the original version 1 validation contract. Neither
    mode certifies transcript accuracy, evidence quality, or factual claims.
    """
    schema = schema or load_template_schema(template_path)
    errors = []
    frontmatter, body = split_frontmatter(text)
    if frontmatter is None:
        errors.append("缺少文件顶部 YAML frontmatter")
        frontmatter = ""
    version = scalar_field(frontmatter, "case_structure_version")
    if type(version) is not int or version != SUPPORTED_VERSION:
        errors.append(f"case_structure_version 必须为整数 {SUPPORTED_VERSION}（当前 {version!r}）")
    transcript_status = scalar_field(frontmatter, "transcript_status")
    if strict_content and transcript_status not in ("verified", "inferred", "none"):
        errors.append("transcript_status 必须为 verified、inferred 或 none")
    view = markdown_view(body)
    if view.unclosed_fence:
        errors.append("正文含未闭合的代码围栏")
    h1 = headings(view, 1)
    h2 = headings(view, 2)
    if len(h1) != 1:
        errors.append(f"正文必须有且仅有一个 H1 案例标题（当前 {len(h1)} 个）")
    actual = [title for _, title in h2]
    expected = [s.heading for s in schema.sections]
    if actual != expected:
        errors.append(
            "H2 正文结构必须与 case_template.md 完全一致且顺序固定；"
            f"应为 {expected!r}；当前为 {actual!r}"
        )
    if len(h1) == 1 and h2:
        if h1[0][0] >= h2[0][0]:
            errors.append("H1 案例标题必须位于第 1 节之前")
        else:
            prefix = "\n".join(view.lines[:h2[0][0]])
            prefix = re.sub(r"<!--.*?-->", "", prefix, flags=re.DOTALL)
            prefix_lines = [line for line in prefix.splitlines() if line.strip()]
            if len(prefix_lines) != 1 or not HEADING_RE.match(prefix_lines[0]):
                errors.append("H1 与第 1 节之间不得放正文或主线；主线、购买理由和证据放入第 1 节")
    for requirement in schema.sections:
        matches = [(i, title) for i, title in h2 if title == requirement.heading]
        if len(matches) != 1:
            continue  # The heading error already identifies missing/duplicate sections.
        start = matches[0][0]
        end = next((i for i, _ in h2 if i > start), len(view.lines))
        section_text = "\n".join(view.lines[start + 1:end]).strip()
        meaningful = re.sub(r"<!--.*?-->", "", section_text, flags=re.DOTALL).strip()
        meaningful = re.sub(r"(?m)^\s*(?:---+|\*\*\*+)\s*$", "", meaningful).strip()
        if not meaningful:
            errors.append(f"第 {requirement.number} 节为空")
        if strict_content and requirement.block_headings:
            blocks = [(line, title) for line, title in headings(view, 3) if start < line < end]
            actual_blocks = tuple(title for _, title in blocks)
            if actual_blocks != requirement.block_headings:
                errors.append(
                    f"第 {requirement.number} 节 H3 内容块必须与模板一致且顺序固定；"
                    f"应为 {list(requirement.block_headings)!r}；当前为 {list(actual_blocks)!r}"
                )
            for index, (block_start, block_heading) in enumerate(blocks):
                block_end = blocks[index + 1][0] if index + 1 < len(blocks) else end
                if not meaningful_content("\n".join(view.lines[block_start + 1:block_end])):
                    errors.append(f"第 {requirement.number} 节 H3 内容块为空：{block_heading}")
        actual_tables = tables(view.visible, start + 1, end)
        cursor = 0
        for table_index, header in enumerate(requirement.table_headers):
            match_index = next((i for i in range(cursor, len(actual_tables))
                                if actual_tables[i].header == header), None)
            if match_index is None:
                errors.append(
                    f"第 {requirement.number} 节缺少表格或表头顺序不符：{' | '.join(header)}"
                )
                continue
            table = actual_tables[match_index]
            cursor = match_index + 1
            if not any(len(row) == len(header) and not is_separator(row)
                       and any(normalized_cell(c) for c in row)
                       for row in table.rows):
                errors.append(f"第 {requirement.number} 节表格没有非空数据行：{' | '.join(header)}")
            if any(len(row) != len(header) for row in table.rows):
                errors.append(f"第 {requirement.number} 节表格数据行列数与表头不符：{' | '.join(header)}")
            if strict_content and table_index < len(requirement.required_tables):
                fixed_rows = requirement.required_tables[table_index].fixed_rows
                if fixed_rows is not None:
                    actual_rows = tuple(normalized_cell(row[0]) for row in table.rows)
                    if actual_rows != fixed_rows:
                        errors.append(
                            f"第 {requirement.number} 节固定字段及顺序必须与模板一致；"
                            f"应为 {list(fixed_rows)!r}；当前为 {list(actual_rows)!r}"
                        )
                    for row in table.rows:
                        if len(row) == len(header) and any(
                            not normalized_cell(meaningful_content(cell)) for cell in row[1:]
                        ):
                            errors.append(
                                f"第 {requirement.number} 节固定字段值不能为空：{normalized_cell(row[0])}"
                            )
        code = [b.content for b in view.code_blocks if start < b.start and b.end < end]
        if requirement.number == 5 and transcript_status == "verified":
            if not any(TIMESTAMP_RE.search(block) for block in code):
                errors.append("第 5 节 verified 案例必须有带 [开始s - 结束s] 时间戳的原文转写代码块")
        if requirement.number == 8 and not any(VARIABLE_RE.search(block) for block in code):
            errors.append("第 8 节必须有含 {变量} 的可复用骨架代码块")
        if strict_content and requirement.number == 8 and not any(
            has_complete_skeleton_segment(block) for block in code
        ):
            errors.append("第 8 节骨架代码块至少有一段同时包含 [时窗｜功能]、{变量} 和非空画面：说明")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 case_template.md 校验版本 1 案例正文（含外部导出）")
    parser.add_argument("--case-file", type=Path, required=True, help="要检查的案例 Markdown 文件")
    parser.add_argument("--strict-content", action="store_true", help="同时校验模板 H3 内容块、固定字段及骨架段")
    args = parser.parse_args(argv)
    try:
        text = args.case_file.read_text(encoding="utf-8-sig")
        errors = validate_case_structure(text, strict_content=args.strict_content)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if errors:
        print(f"FAILED: {args.case_file}：{len(errors)} 个正文结构问题", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"OK: {args.case_file} 通过 case_structure_version: {SUPPORTED_VERSION} 正文结构校验")
    return 0


if __name__ == "__main__":
    sys.exit(main())
