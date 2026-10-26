#!/usr/bin/env python3
"""Render a Chinese reading library from validated, read-only review cases."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import date
from urllib.parse import quote

from case_store import CaseStore, StoreError, case_id, CONTENT_ORIGIN_LABELS


STATUS_LABELS = {"unverified": "未核对", "user_reported": "用户提供", "source_checked": "来源已核对"}
KIND_LABELS = {"preference": "用户偏好", "fact": "事实", "editorial": "文案观察", "performance": "市场表现", "review_correction": "审查纠错"}
POLARITY_LABELS = {"positive": "正向", "negative": "负向", "mixed": "混合", "unknown": "待评估"}
DIMENSION_LABELS = {"reach": "播放与触达", "conversion": "转化", "performance": "整体表现", "purchase": "购买理由", "evidence": "证据", "source_matching": "素材对应", "review_behavior": "审查判断"}
SOURCE_TYPE_LABELS = {"video_analysis": "视频拆解", "structure_template": "结构模板", "traffic_reference": "流量参考"}
SOURCE_TRANSCRIPT_LABELS = {"verified": "已核对", "inferred": "推测整理", "none": "无逐字稿"}
METRIC_LABELS = (("video_id", "视频 ID"), ("creator_handle", "账号"), ("published_title", "发布标题"), ("views_display", "播放量"), ("product_impressions_display", "商品曝光"), ("product_clicks_display", "商品点击"), ("items_sold", "售出件数"), ("ctor_display", "平台 CTOR"), ("ticket_value", "客单价背景"))
ASSET_LABELS = (("video", "当前有效视频"), ("screenshot", "表现截图"), ("transcript", "转写文件"), ("source_manifest", "来源清单"), ("contact_sheet", "抽样画面"))
VARIANT_LABELS = {"direction": "方向变体", "execution": "执行变体"}


def shown(value, fallback="未提供"):
    return fallback if value is None or value == "" else str(value)


def plain(value):
    """Escape inline prose without interpreting archived content as Markdown."""
    text = shown(value).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", text)


def cell(value):
    return plain(value).replace("\n", "<br>")


def link(label, path):
    target = quote(Path(path).resolve().as_posix(), safe="/:~-._")
    return f"[{plain(label)}](<{target}>)"


def source_reference(value, label):
    if value:
        try:
            path = Path(value).expanduser()
            if path.is_absolute() and not str(value).startswith(("\\\\", "//")) and path.is_file():
                return link(label, path)
        except OSError:
            pass
    return plain(value)


def readable_quote(text):
    # Prose wraps to the reader's viewport; escape archived Markdown literally.
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join("> " + plain(line) if line else ">" for line in text.split("\n"))


def block(text, language="text"):
    text = str(text)
    longest = max((len(match.group()) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text}\n{fence}"


def json_block(value):
    return block(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), "json")


def default_runtime_root():
    configured = os.environ.get("AGENT_RUNTIME_ROOT")
    return Path(configured).expanduser() if configured else Path(os.environ.get("USERPROFILE") or Path.home()) / ".agent-runtime"


def default_output_root():
    return default_runtime_root() / "product-video-script-review" / "library"


def overlaps(first, second):
    return first.is_relative_to(second) or second.is_relative_to(first)


def protected_directories(root, cases):
    protected = {root.resolve(), (default_runtime_root() / "product-video-script-review" / "evidence").resolve()}
    # Backups may live in a user-selected evidence tree, outside the default.
    for item in cases:
        snapshots = [item.get("current"), *(event.get("current") for event in item.get("events", []))]
        for snapshot in snapshots:
            for value in (snapshot or {}).get("assets", {}).values():
                path = Path(value).expanduser().resolve()
                protected.update(parent for parent in path.parents if parent.name.casefold() == "evidence")
    return protected


def validate_destination(output_root, root, cases):
    raw = Path(output_root).expanduser().absolute()
    cursor = raw
    while True:
        if cursor.is_symlink() or getattr(cursor, "is_junction", lambda: False)():
            raise StoreError(f"output path contains a link or junction: {cursor}")
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    destination = raw.resolve()
    for protected in protected_directories(root, cases):
        if overlaps(destination, protected):
            raise StoreError(f"output root overlaps source or evidence directory: {protected}")
    if destination.exists() and not destination.is_dir():
        raise StoreError(f"output root is not a directory: {destination}")
    return destination


def asset_links(item, missing):
    result = {}
    for field, label in ASSET_LABELS:
        value = (item.get("current") or {}).get("assets", {}).get(field)
        if not value:
            result[field] = "未提供"
            continue
        path = Path(value).expanduser()
        if not path.is_absolute() or not path.is_file():
            missing.append({"case_id": item["case_id"], "asset": field, "path": str(path)})
            result[field] = f"**附件缺失**：{plain(value)}"
        else:
            result[field] = link(label, path)
    return result


def evidence_lines(evidence, publish=None):
    if not evidence:
        return ["证据状态：未提供。"]
    result = ["证据状态：" + plain(STATUS_LABELS.get(evidence.get("status"), evidence.get("status"))) + "。"]
    for key, label in (("source", "来源"), ("window", "统计窗口"), ("context", "适用条件与限制")):
        if evidence.get(key):
            value = source_reference(evidence[key], "查看证据来源") if key == "source" else plain(evidence[key])
            result.extend(["", f"**{label}**：{value}"])
    if evidence.get("as_of"):
        elapsed = ""
        if (publish or {}).get("published_on"):
            days = (date.fromisoformat(evidence["as_of"]) - date.fromisoformat(publish["published_on"])).days
            elapsed = f"（发布后第 {days} 天）"
        result.extend(["", f"**数据截至**：{plain(evidence['as_of'])}{elapsed}"])
    return result


def direction_lines(current):
    direction = current.get("test_direction")
    lines = ["**测试方向**", ""]
    if not direction:
        return lines + ["未登记；不根据旧稿、购买理由或表现推补。"]
    lines.append(f"- 方向编号：{plain(direction['direction_id'])}")
    chain = " → ".join(plain(direction[field]) for field in ("main_difference", "concern", "closing"))
    lines.append(f"- 方向链：{chain}")
    lines.append(f"- 变体类型：{VARIANT_LABELS[direction['variant_type']]}")
    for field, label in (("execution_change", "执行变化"), ("script_version", "方向绑定文案版本")):
        if direction.get(field):
            lines.append(f"- {label}：{plain(direction[field])}")
    if not direction.get("script_version"):
        lines.append("- 方向绑定文案版本：未提供。")
    production_version = (current.get("production") or {}).get("script_version")
    if direction.get("script_version") and production_version and direction["script_version"] != production_version:
        lines += ["", "**版本待核对**：测试方向绑定的文案版本与制作记录不同，不能据本条表现判断该方向的效果。"]
    return lines


def release_lines(current):
    lines = []
    if (current.get("publish") or {}).get("published_on"):
        lines += ["**发布日期**：" + plain(current["publish"]["published_on"]), ""]
    if current.get("production"):
        lines += ["**制作记录**", ""]
        for field, label in (("script_version", "文案版本"), ("davinci_project", "达芬奇项目"), ("davinci_timeline", "达芬奇时间线"), ("voiceover", "配音文件"), ("source_ledger", "素材台账")):
            if current["production"].get(field):
                lines.append(f"- {label}：{plain(current['production'][field])}")
        lines.append("")
    return lines


def assessments_table(assessments):
    if not assessments:
        return ["尚无当前维度评价。"]
    result = ["| 维度 | 当前评价 | 依据 |", "|---|---|---|"]
    for assessment in assessments:
        dimension = assessment["dimension"]
        polarity = assessment["polarity"]
        result.append(f"| {cell(DIMENSION_LABELS.get(dimension, dimension))} | {cell(POLARITY_LABELS.get(polarity, polarity))} | {cell(assessment.get('reason'))} |")
    return result


def lifecycle_action(event):
    lifecycle = event.get("lifecycle")
    if lifecycle is None:
        return None
    return "归档案例" if lifecycle["archived"] else "恢复案例"


def render_case(item, output_root, assets):
    identifier = item["case_id"]
    current = item.get("current") or {}
    title = current.get("title") or f"待整理案例 {identifier}"
    folder = output_root / identifier
    lines = [f"# {plain(title)}", "", f"案例编号：{identifier} · {link('返回中文总目录', output_root / 'INDEX.md')}"]
    if item.get("archived", False):
        lines += ["", "**已归档**：不出现在默认目录与搜索中。当前版本、评价和附件保留；归档不表示负例。", "", link("查看归档目录", output_root / "ARCHIVE.md")]
    lines += ["", "## 1. 当前结论", ""]
    lines += [f"**内容来源：{CONTENT_ORIGIN_LABELS.get(current.get('content_origin'), '未标注')}**", ""]
    if current:
        lines += [f"**分类：{plain(current['category'])}**", "", plain(current["conclusion"]), "", f"记录类型：{plain(KIND_LABELS.get(current['kind'], current['kind']))}。", ""]
        lines += ["**购买理由**", "", plain(current.get("purchase_reason")) if current.get("purchase_reason") else "未单独整理。", ""]
        lines += [*direction_lines(current), "", *release_lines(current)]
        lines += assessments_table(current.get("assessments", []))
    else:
        lines += ["**待整理：尚无当前快照。** 原始稿件和历史反馈保留在历史页，不能据其推定当前结论或当前有效素材。"]
    lines += ["", "## 2. 对应视频", ""]
    for field, label in ASSET_LABELS:
        entry = assets[field]
        lines.append("- " + entry if entry.startswith("[") else f"- {label}：{entry}")
    lines += ["", "**入库时来源与统计窗口（历史快照）**", "", *evidence_lines(current.get("evidence"), current.get("publish")), "", "## 3. 表现数据", "", f"当前销售表现统一从[工作台销售数据表](http://127.0.0.1:8767/#case/{identifier})按视频ID读取。下表是入库时快照，不作为当前数据回填。", "", "| 指标 | 入库时快照 |", "|---|---|"]
    metrics = current.get("metrics") or {}
    for field, label in METRIC_LABELS:
        lines.append(f"| {label} | {cell(metrics.get(field))} |")
    lines += ["", "按来源原值显示；未提供项不填 0。CTOR 沿用平台名称，不改称播放到成交转化率；客单价和表现判断仅适用于本案例已注明的条件。", "", "## 4. 原文", ""]
    source_case = current.get("source_case") or {}
    if source_case:
        source_type = SOURCE_TYPE_LABELS.get(source_case.get("case_type"), source_case.get("case_type"))
        transcript_status = SOURCE_TRANSCRIPT_LABELS.get(source_case.get("transcript_status"), source_case.get("transcript_status"))
        lines += [f"**历史来源**：{plain(source_case.get('library_id'))} · {plain(source_type)}", "", f"原库分级：{plain(source_case.get('tier'))}；原库转写状态：{plain(transcript_status)}。原库分级不自动构成转化正向结论。", "", source_reference(source_case.get("file_path"), "查看完整拆解来源"), ""]
    transcript = current.get("transcript") or {}
    if transcript.get("text"):
        lines += [f"文本状态：{plain(transcript.get('status'))}", "", f"文本来源：{source_reference(transcript.get('source'), '查看原文来源')}", "", readable_quote(transcript["text"])]
    elif source_case.get("transcript_status") == "none":
        lines += ["原库未提供口播逐字稿。完整资料与拆解见上方来源，不用结构模板或拆解内容代替口播。"]
    elif source_case.get("transcript_status") == "inferred":
        lines += ["原库文本属于推测整理，未作为已核对口播显示。完整拆解与原始记录保留在来源和历史页。"]
    else:
        lines += ["尚未整理当前有效视频的原文。历史原稿未自动沿用。"]
    translation = current.get("translation_zh") or {}
    if translation:
        channel = translation["channel"]
        lines += ["", "### 中文对照", ""]
        if channel == "on_screen":
            lines += ["以下对照的是屏显文字，不作为口播转写。", ""]
        elif channel == "historical_excerpt":
            lines += ["以下只对照历史记录中的文字片段；原片未留可核实的完整文案。", ""]
        if channel != "transcript":
            lines += ["**来源文字**", "", readable_quote(translation["source_text"]), ""]
        lines += ["**中文口语译文**", "", readable_quote(translation["text"])]
    lines += ["", "## 5. 可借鉴点与问题", "", "**可借鉴点**", ""]
    lines += ["- " + plain(value) for value in current.get("highlights", [])] or ["尚未整理。"]
    lines += ["", "**问题与限制**", ""]
    lines += ["- " + plain(value) for value in current.get("limitations", [])] or ["尚未整理；这不代表没有问题。"]
    native = current.get("native_reference")
    if native:
        lines += ["", "### 本土表达与场景参考", "", "语言：" + plain(native["language"]) + "；市场：" + plain(native["market"])]
        for field, label in (("scene", "画面与使用场景"), ("speaker", "说话者与表达姿态"), ("audience", "受众与触发情境")):
            lines += ["", "**" + label + "**：" + plain(native[field])]
        for entry in native["entries"]:
            channel = "口播" if entry["channel"] == "speech" else "屏幕文字"
            lines += ["", f"#### {plain(entry['function'])} · {entry['start_sec']:g}–{entry['end_sec']:g}s · {channel}", "", block(entry["original"])]
            for field, label in (("meaning_zh", "中文原意"), ("context", "当时场景与前后承接"), ("analysis", "表达分析"), ("adaptation", "复用示例（非原话）"), ("boundary", "适用边界")):
                lines += ["", "**" + label + "**：" + plain(entry[field])]
    lines += ["", "## 6. 历史修订", "", plain(current.get("history_note"),) if current.get("history_note") else "原始记录和反馈按时间保留；历史评价不替代当前结论。", "", link("查看完整历史、旧稿和原始反馈", folder / "history.md")]
    for event in sorted(item.get("events", []), key=lambda value: (value["created_at"], value["event_id"])):
        action = lifecycle_action(event)
        if action:
            lines += ["", f"- {plain(event['created_at'])} · **{action}**：{plain(event['feedback']['text'])}"]
    related = item.get("related_case_ids", [])
    if related:
        lines += ["", "关联案例：" + "、".join(link(value, output_root / value / "case.md") for value in related) + "。"]
    return "\n".join(lines) + "\n"


def render_history(item, root, output_root):
    identifier = item["case_id"]
    original = item.get("original") or {}
    lines = [f"# {identifier} · 历史资料", "", link("返回当前案例", output_root / identifier / "case.md")]
    if item.get("archived", False):
        lines += ["", "**已归档**：原文、反馈与附件仍保留，归档不改变文案评价。"]
    lines += ["", "这里保留原始稿件和全部历史反馈，包含已被修正的关联与判断；不代表当前有效结论。", "", "## 初次收录", "", f"历史标题：{plain(item.get('title'))}", "", f"创建时间：{plain(item.get('created_at'))}", "", link("原始记录 JSON", root / "cases" / identifier / "original.json")]
    for key, label in (("source", "来源"), ("url", "原始链接")):
        if original.get(key):
            lines += ["", f"**{label}**", "", block(original[key])]
    if original.get("text"):
        lines += ["", "**原始文本（历史）**", "", block(original["text"])]
    for event in sorted(item.get("events", []), key=lambda value: (value["created_at"], value["event_id"])):
        feedback = event["feedback"]
        action = lifecycle_action(event)
        label = action or KIND_LABELS.get(feedback['kind'], feedback['kind'])
        lines += ["", f"## {plain(event['created_at'])} · {plain(label)}", "", f"事件：{plain(event['event_id'])}", "", link("原始事件 JSON", root / "cases" / identifier / "events" / (event["event_id"] + ".json")), "", "**操作原因**" if action else "**反馈原文**", "", block(feedback["text"])]
        if action:
            lines += ["", "归档状态：" + ("已归档" if event["lifecycle"]["archived"] else "使用中") + "；当前快照和原有评价不因此改变。"]
        if feedback.get("assessments"):
            lines += ["", "**当时评价**", "", *assessments_table(feedback["assessments"])]
        for field, label in (("analysis", "分析原文"), ("scope", "适用范围")):
            if feedback.get(field):
                lines += ["", f"**{label}**", "", block(feedback[field])]
        if feedback.get("evidence"):
            lines += ["", "**当时证据（原字段完整保留）**", "", json_block(feedback["evidence"])]
        if event.get("draft_version"):
            lines += ["", "**当时稿件版本**", "", json_block(event["draft_version"])]
        if event.get("current"):
            lines += ["", "**该事件保存的当前快照**", "", json_block(event["current"])]
    return "\n".join(lines) + "\n"


def render_index(cases, output_root, all_assets, *, archived=False):
    selected = [item for item in cases if bool(item.get("archived", False)) == archived]
    archived_count = sum(bool(item.get("archived", False)) for item in cases)
    active_count = len(cases) - archived_count
    if archived:
        lines = ["# 已归档案例", "", f"已归档 {archived_count} 条案例。归档仅调整默认展示与检索范围，不改变评价，也不表示负例。", "", link(f"返回中文总目录（使用中 {active_count} 条）", output_root / "INDEX.md")]
    else:
        lines = ["# 带货视频审查案例库", "", f"使用中 {active_count} 条案例；已归档 {archived_count} 条。默认目录仅显示使用中的案例。", "", link(f"查看归档目录（{archived_count} 条）", output_root / "ARCHIVE.md")]
    lines += ["", "表格显示最新整理结论与当前有效视频；旧素材、旧评价和修订过程在各案例的历史页。", "", "播放、点击、售出件数和平台 CTOR 是不同指标。正向或负向评价按本案例的用户反馈、客单背景和统计条件理解，不据单个案例推导行业门槛或文案因果。测试方向只显示人工登记的当前方向，未登记的旧案例不推补。", "", "| 当前标题 | 分类 | 内容来源 | 测试方向 | CTOR | 播放 | 售出件数 | 客单背景 | 当前有效视频 | 阅读档案 |", "|---|---|---|---|---|---|---|---|---|---|"]
    for item in selected:
        identifier = item["case_id"]
        current = item.get("current") or {}
        metrics = current.get("metrics") or {}
        direction = current.get("test_direction") or {}
        direction_label = f"{direction['direction_id']} · {VARIANT_LABELS[direction['variant_type']]}" if direction else "未登记"
        values = [cell(current.get("title") or f"待整理案例 {identifier}"), cell(current.get("category") or "待整理"), cell(CONTENT_ORIGIN_LABELS.get(current.get("content_origin"), "未标注")), cell(direction_label), cell(metrics.get("ctor_display")), cell(metrics.get("views_display")), cell(metrics.get("items_sold")), cell(metrics.get("ticket_value")), all_assets[identifier]["video"].replace("\n", "<br>"), link(identifier, output_root / identifier / "case.md")]
        lines.append("| " + " | ".join(values) + " |")
    if not selected:
        lines += ["", "暂无已归档案例。" if archived else "暂无使用中的案例。"]
    lines += ["", "表内表现指标保留入库时快照；当前销售数据统一在工作台按视频ID查询销售数据表。每条阅读档案按“当前结论 → 对应视频 → 表现数据 → 原文 → 可借鉴点与问题 → 历史修订”排列。缺失附件明确标出，未整理的旧记录不会自动冒充当前版本。"]
    return "\n".join(lines) + "\n"


def publish_files(files, output_root):
    # Validate every target and encode every document before touching old output.
    prepared = []
    for relative, content in files.items():
        target = output_root / relative
        cursor = target
        while cursor != output_root:
            if cursor.is_symlink() or getattr(cursor, "is_junction", lambda: False)():
                raise StoreError(f"output target contains a link or junction: {cursor}")
            cursor = cursor.parent
        if not target.resolve().is_relative_to(output_root):
            raise StoreError(f"output target escapes output root: {target}")
        if target.exists() and not target.is_file():
            raise StoreError(f"output target is not a file: {target}")
        prepared.append((target, content.encode("utf-8")))
    staged = []
    try:
        for target, data in prepared:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".library-", suffix=".tmp", dir=target.parent)
            staged.append((Path(temporary), target))
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        for temporary, target in staged:
            os.replace(temporary, target)
    finally:
        for temporary, _ in staged:
            if temporary.exists():
                temporary.unlink()


def render_library(root, output_root=None):
    root = Path(root).resolve()
    store = CaseStore(root)
    # The store checks all immutable records and compares its index before any write.
    index = store.read_index()
    cases = [store.read_case(case_id(entry["case_id"])) for entry in index["cases"]]
    output_root = validate_destination(output_root or default_output_root(), root, cases)
    missing = []
    all_assets = {item["case_id"]: asset_links(item, missing) for item in cases}
    files = {}
    for item in cases:
        identifier = item["case_id"]
        files[Path(identifier) / "case.md"] = render_case(item, output_root, all_assets[identifier])
        files[Path(identifier) / "history.md"] = render_history(item, root, output_root)
    # Publish navigation pages last, after their target documents are available.
    files[Path("ARCHIVE.md")] = render_index(cases, output_root, all_assets, archived=True)
    files[Path("INDEX.md")] = render_index(cases, output_root, all_assets)
    publish_files(files, output_root)
    archived_count = sum(bool(item.get("archived", False)) for item in cases)
    return {"ok": True, "case_count": len(cases), "active_case_count": len(cases) - archived_count, "archived_case_count": archived_count, "output_root": str(output_root), "files": [str(output_root / path) for path in files], "missing_attachments": missing, "unorganized_cases": [item["case_id"] for item in cases if not item.get("current")]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="validated skill/case-store root")
    parser.add_argument("--output-root", type=Path, help="reading output directory; defaults to the agent runtime library")
    args = parser.parse_args(argv)
    try:
        result = render_library(args.root, args.output_root)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        return 0
    except (OSError, StoreError, TypeError, ValueError, KeyError) as exc:
        sys.stderr.write(f"render_case_library: {exc}\n")
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
