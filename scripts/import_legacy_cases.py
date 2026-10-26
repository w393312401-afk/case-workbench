#!/usr/bin/env python3
"""Import legacy VSL references without changing their source or existing cases.

Dry-run is the default. Only --apply writes; media is linked, never copied.
The supported frontmatter is the source library's scalar/flow YAML subset.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys

from case_store import CaseStore, StoreError, add_schema, case_id, load_json, require, string

LIBRARY_ID = re.compile(r"VSL-(?:LT|T[0-4])-\d{3}\Z")
MIGRATION_NOTE = "仅迁移历史资料；本次未重新播放媒体、听辨音轨或核验播放量、销量与转化数据。历史分级及策略只作编辑参考，不代表已验证转化表现。"
TYPE_LABELS = {"video_analysis": "历史视频拆解参考", "structure_template": "结构模板参考", "traffic_reference": "流量参考（非转化证据）"}


def split_flow(text, separator=","):
    parts, start, depth, quote, escaped = [], 0, 0, None, False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == separator and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
        require(depth >= 0, "unbalanced frontmatter flow value")
    require(not quote and depth == 0, "unclosed frontmatter flow value")
    parts.append(text[start:].strip())
    return parts


def yaml_value(text):
    text = text.strip()
    if text.startswith("{"):
        require(text.endswith("}"), "invalid frontmatter mapping")
        result = {}
        for part in split_flow(text[1:-1]):
            if not part:
                continue
            require(":" in part, "frontmatter mapping entry requires colon")
            key, value = part.split(":", 1)
            key = key.strip().strip("\"'")
            require(key not in result, f"duplicate frontmatter key: {key}")
            result[key] = yaml_value(value)
        return result
    if text.startswith("["):
        require(text.endswith("]"), "invalid frontmatter sequence")
        return [yaml_value(part) for part in split_flow(text[1:-1]) if part]
    if text.startswith('"'):
        try:
            return json.loads(text)
        except ValueError as exc:
            raise StoreError(f"invalid quoted frontmatter value: {exc}") from exc
    if text.startswith("'"):
        require(text.endswith("'"), "unclosed quoted frontmatter value")
        return text[1:-1].replace("''", "'")
    if text.lower() in ("null", "~", ""):
        return None
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    require(not text.startswith(("|", ">", "&", "*", "!")), "unsupported frontmatter YAML value")
    return text


def frontmatter(text):
    lines = text.lstrip("\ufeff").splitlines()
    require(lines and lines[0].strip() == "---", "missing frontmatter")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    require(end is not None, "unclosed frontmatter")
    meta = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)", line)
        require(match is not None, f"unsupported frontmatter line: {line}")
        key, value = match.groups()
        require(key not in meta, f"duplicate frontmatter key: {key}")
        meta[key] = yaml_value(value)
    return meta, "\n".join(lines[end + 1:])


def clean(text):
    return re.sub(r"\s+", " ", text.replace("**", "").replace("`", "")).strip()


def section(body, heading):
    match = re.search(r"^##\s+(?:\d+\.\s*)?" + re.escape(heading) + r"[^\n]*\n(.*?)(?=^##\s|\Z)", body, re.M | re.S)
    return match.group(1).strip() if match else ""


def table_value(body, key):
    for line in body.splitlines():
        cells = re.split(r"(?<!\\)\|", line.strip().strip("|"))
        if len(cells) >= 2 and clean(cells[0]) == key:
            return clean(cells[1])
    return ""


def labeled_paragraph(body, label):
    for paragraph in re.split(r"\n\s*\n", body):
        plain = clean(paragraph)
        if plain.startswith(label + "：") or plain.startswith(label + ":"):
            return plain[len(label) + 1:].strip()
    return ""


def summary(body, meta):
    spine = labeled_paragraph(body, "脚本主线判断") or table_value(body, "唯一主线")
    if spine:
        return spine
    narrative = section(body, "叙事结构拆解")
    beats = []
    for line in narrative.splitlines():
        cells = [clean(cell) for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if len(cells) >= 3 and cells[0] not in {"阶段", "---", ""} and not re.fullmatch(r"[-: ]+", cells[0]):
            beats.append(cells[2])
    if beats:
        return "历史叙事摘要：" + " → ".join(beats)
    return meta.get("template_usage") or f"历史资料：{meta['title']}；详见源文档。"


def highlights(body):
    strategy = section(body, "关键成功因子") or section(body, "创意核心逻辑")
    result = []
    for line in strategy.splitlines():
        if re.match(r"^(?:\*\*)?\d+[.、]\s", line):
            result.append(clean(re.sub(r"^(?:\*\*)?\d+[.、]\s*", "", line)))
    if not result:
        for line in section(body, "视频结构判断").splitlines():
            if line.startswith("- "):
                result.append(clean(line[2:]))
    return result


def extract_transcript(body, status, library_id):
    if status not in {"verified", "inferred"}:
        return None, "来源未提供可靠完整转写；未把模板、片段、屏显或静默描述填入口播原文。"
    content = section(body, "口播原文完整转写")
    if library_id == "VSL-T3-007" or re.search(r"没有可核验的完整原文|不是从真实音频逐字|仅为视觉推断", content):
        return None, "历史 inferred 为视觉猜测，低置信且无可核验原声转写；口播待补，不能据此认定静默。"
    blocks = re.findall(r"```(?:text|plaintext)?\s*\n(.*?)```", content, re.S)
    transcript = blocks[0].strip() if blocks else ""
    if not transcript:
        return None, "转写状态保留自历史记录，但未找到独立完整转写正文；待核对。"
    return {"text": transcript, "status": status,
            "source": "历史 Markdown 口播转写章节；本次未重新听辨。" + ("旧 ASR 低置信，措辞待核验。" if status == "inferred" else "历史 verified 标记不代表本次重新验证。")}, ""


def local_references(body):
    paths = re.findall(r"`([^`\n]+)`", body)
    # Markdown destinations can contain balanced parentheses; do not combine
    # adjacent links or truncate filenames such as 'creator (@handle).mp4'.
    for match in re.finditer(r"\]\(", body):
        start = match.end()
        if body[start:start + 1] == "<":
            end = body.find(">)", start + 1)
            if end != -1:
                paths.append(body[start + 1:end])
            continue
        depth = 1
        for end in range(start, len(body)):
            if body[end] == "\n":
                break
            if body[end] == "(":
                depth += 1
            elif body[end] == ")":
                depth -= 1
                if depth == 0:
                    paths.append(body[start:end])
                    break
    return list(dict.fromkeys(path.replace("\\\\", "\\") for path in paths if re.match(r"(?:[A-Za-z]:[/\\]|/(?!/))", path)))


def assets_for(body):
    assets, missing = {}, []
    for reference in local_references(body):
        path = Path(reference)
        suffix = path.suffix.lower()
        if suffix in {".mp4", ".mov", ".m4v", ".webm"}:
            kind = "video"
        elif suffix in {".srt", ".vtt"}:
            kind = "transcript"
        elif suffix in {".jpg", ".jpeg", ".png", ".webp"}:
            # screenshot is presented as performance data in the case UI.
            # Scene frames must not acquire that meaning during migration.
            if "contact" not in path.name.lower():
                if not path.is_file():
                    missing.append(reference)
                continue
            kind = "contact_sheet"
        elif suffix == ".json" and path.name in {"timeline.json", "video_overview.json", "manifest.json"}:
            kind = "source_manifest"
        else:
            continue
        if path.is_absolute() and not str(path).startswith(("\\\\", "//")) and path.is_file():
            assets.setdefault(kind, str(path.resolve()))
        else:
            missing.append(reference)
    return assets, missing


def make_value(library_id, path, text, meta, body):
    case_type = "structure_template" if meta["tier"] == "T4" or library_id == "VSL-T3-003" else "traffic_reference" if meta["tier"] == "LT" else "video_analysis"
    evidence = meta.get("evidence", {})
    require(isinstance(evidence, dict), f"{library_id}: evidence must be an object")
    evidence_source = evidence.get("source") or "历史资料未记录数据来源"
    string(evidence_source, "legacy evidence.source")
    user_reported = "用户" in evidence_source and bool(re.search(r"背书|称.{0,6}爆款|标记.{0,4}爆款|标注[^；。]*爆款|已验证", evidence_source))
    limitations = [MIGRATION_NOTE]
    for value in (labeled_paragraph(body, "事实与推断边界"), table_value(body, "验证边界")):
        if value:
            limitations.append(value)
    if case_type == "structure_template":
        limitations.append("结构模板或历史骨架记录，不作为真实完整成片、原声音轨或转化证据。")
    elif case_type == "traffic_reference":
        limitations.append("非带货流量参考；播放量不代表购买、销量或转化效果。")
    transcript, transcript_limit = extract_transcript(body, meta["transcript_status"], library_id)
    if transcript_limit:
        limitations.append(transcript_limit)
    elif meta["transcript_status"] == "inferred":
        limitations.append("历史 ASR 为 inferred，低置信；未重新听辨，不能据误识别词确定产品规格。")
    assets, missing = assets_for(body)
    if "video" not in assets and case_type != "structure_template":
        limitations.append("原视频待关联：未找到源文档明确指向且当前存在的本地视频；不推测文件位置。")
    if missing:
        limitations.append("历史附件路径当前不可用，待关联：" + "；".join(missing))
    metric_labels = {"views": "历史播放量", "units": "原订单/销量记录（沿用旧库口径）", "gmv": "原 GMV"}
    preserved_metrics = [f"{label}：{evidence[key]}" for key, label in metric_labels.items() if evidence.get(key) is not None]
    context = MIGRATION_NOTE + (" " + "；".join(preserved_metrics) + "。" if preserved_metrics else "")
    if evidence.get("units") is not None:
        context += "旧库 units 字段混有订单与软描述，未将其改写为售出件数。"
    current = {
        "title": meta["title"], "category": TYPE_LABELS[case_type], "conclusion": summary(body, meta),
        "kind": "editorial", "assessments": [],
        "source_case": {"library_id": library_id, "file_path": str(path), "tier": meta["tier"], "transcript_status": meta["transcript_status"], "case_type": case_type},
        "evidence": {"status": "user_reported" if user_reported else "unverified", "source": evidence_source, "context": context},
        "highlights": highlights(body), "limitations": limitations,
        "history_note": f"从 {library_id} 导入；完整历史分析保存于 original.text，源 Markdown 继续作为完整拆解出处。",
    }
    metrics = {}
    if evidence.get("views") is not None:
        metrics["views_display"] = str(evidence["views"])
    if metrics:
        current["metrics"] = metrics
    if assets:
        current["assets"] = assets
    if transcript:
        current["transcript"] = transcript
    product = table_value(section(body, "案例基本信息"), "产品名称")
    if product:
        product = re.split(r"[；;]|，沿用", product, maxsplit=1)[0].strip()
    value = {"title": meta["title"], "original": {"text": text, "source": f"旧库 {library_id} | {path}"},
             "feedback": {"kind": "editorial", "text": f"导入历史参考 {library_id}。{MIGRATION_NOTE}", "evidence": current["evidence"]}, "current": current}
    if product:
        value["product"] = product
    add_schema(value)
    return value, missing


def source_cases(source_root):
    source_root = Path(source_root).resolve()
    index = load_json(source_root / "examples" / "index.json")
    require(isinstance(index, dict) and isinstance(index.get("cases"), dict), "legacy index requires cases object")
    result, seen = [], set()
    for library_id, entry in sorted(index["cases"].items()):
        require(LIBRARY_ID.fullmatch(library_id), f"invalid legacy library ID: {library_id}")
        require(isinstance(entry, dict), f"{library_id}: index entry must be an object")
        string(entry.get("file"), f"{library_id}.file")
        path = (source_root / entry["file"]).resolve()
        require(path.is_relative_to(source_root / "examples") and path.suffix.lower() == ".md" and path.is_file(), f"{library_id}: missing or unsafe Markdown path: {path}")
        require(path not in seen, f"duplicate source path: {path}")
        seen.add(path)
        text = path.read_bytes().decode("utf-8-sig")
        meta, body = frontmatter(text)
        require(meta.get("library_id") == library_id, f"{library_id}: frontmatter library_id disagrees with index")
        for key in ("title", "tier", "transcript_status"):
            string(meta.get(key), f"{library_id}.{key}")
        require(type(entry.get("archived")) is bool, f"{library_id}: index archived must be boolean")
        require(entry["archived"] == ("_archive" in path.relative_to(source_root).parts), f"{library_id}: archived flag disagrees with source path")
        mismatches = [key for key in ("title", "tier", "transcript_status", "evidence", "category") if entry.get(key) != meta.get(key)]
        value, missing = make_value(library_id, path, text, meta, body)
        value["feedback"]["scope"] = f"legacy_import:{library_id}:" + ("archived" if entry["archived"] else "active")
        result.append({"library_id": library_id, "value": value, "archived": entry["archived"], "index_mismatches": mismatches, "missing_assets": missing})
    return result


def fingerprints(value):
    result = set()
    original = value.get("original", {}).get("text", "")
    if original:
        original_body = original
        if original.startswith("---\n") or original.startswith("---\r\n"):
            try:
                original_body = frontmatter(original)[1]
            except StoreError:
                pass  # Existing user copy need not be a legacy source file.
        result.add(("original_text", hashlib.sha256(re.sub(r"\s+", "", original_body).encode()).hexdigest()))
    current = value.get("current") or {}
    transcript = current.get("transcript", {}).get("text", "")
    for content in (original, transcript):
        normalized = re.sub(r"[^\w]", "", re.sub(r"\[[^\]]*\]", "", content)).casefold()
        if len(normalized) >= 80:
            result.add(("script_text", hashlib.sha256(normalized.encode()).hexdigest()))
    video_paths = [current.get("assets", {}).get("video")]
    video_paths.extend(path for path in local_references(original) if Path(path).suffix.lower() in {".mp4", ".mov", ".m4v", ".webm"})
    for video in video_paths:
        if not video:
            continue
        result.add(("video_path", str(Path(video).resolve()).casefold()))
    video_id = current.get("metrics", {}).get("video_id")
    if video_id:
        result.add(("video_id", video_id))
    return result


def deleted_library_ids(store, source_root):
    """Minimal deletion identities prevent a retained source from reappearing."""
    path = store.safe("deleted_case_sources.json")
    if not path.exists():
        return set()
    data = load_json(path)
    require(isinstance(data, dict) and set(data) == {"schema_version", "sources"}
            and data["schema_version"] == 1 and isinstance(data["sources"], list), "invalid deleted source registry")
    source_root = Path(source_root).resolve()
    deleted = set()
    for item in data["sources"]:
        require(isinstance(item, dict) and set(item) == {"library_id", "source_root", "case_id", "deleted_at"}, "invalid deleted source identity")
        require(isinstance(item["library_id"], str) and LIBRARY_ID.fullmatch(item["library_id"]), "invalid deleted source library ID")
        case_id(item["case_id"])
        string(item["deleted_at"], "deleted_at")
        string(item["source_root"], "source_root")
        registered_root = Path(item["source_root"])
        require(registered_root.is_absolute() and not str(registered_root).startswith(("\\\\", "//")), "deleted source root must be local and absolute")
        if registered_root.resolve() == source_root:
            deleted.add(item["library_id"])
    return deleted


def import_cases(root, source_root, apply=False):
    """Preflight all sources, then add only new, unambiguous references."""
    store = CaseStore(root)
    index = store.read_index()
    deleted = deleted_library_ids(store, source_root)
    entries = source_cases(source_root)
    existing = [store.read_case(item["case_id"]) for item in index["cases"]]
    by_id = {case["case_id"]: case for case in existing}
    identities, matches = defaultdict(set), defaultdict(set)
    for case in existing:
        for event in case["events"]:
            source = event.get("current", {}).get("source_case", {})
            if source.get("library_id"):
                identities[source["library_id"]].add(case["case_id"])
        for fingerprint in fingerprints(case):
            matches[fingerprint].add(case["case_id"])
    for entry in entries:
        for fingerprint in fingerprints(entry["value"]):
            matches[fingerprint].add(entry["library_id"])
    mapping = []
    for entry in entries:
        library_id, value = entry["library_id"], entry["value"]
        current = value["current"]
        known = sorted(identities[library_id])
        duplicates = sorted({candidate for fingerprint in fingerprints(value) for candidate in matches[fingerprint] if candidate != library_id and candidate not in known})
        status = "identity_conflict" if len(known) > 1 else "existing" if known else "deleted" if library_id in deleted else "possible_duplicate" if duplicates else "ready"
        if status == "existing":
            case = by_id[known[0]]
            # Resume only an interrupted import's untouched first event. A
            # later user edit or explicit restore always takes precedence.
            if (len(case["events"]) == 1 and not case["archived"]
                    and case["events"][0]["feedback"].get("scope") == f"legacy_import:{library_id}:archived"
                    and case["archive_event_id"] is None):
                status = "archive_pending"
        mapping.append({"library_id": library_id, "case_id": known[0] if len(known) == 1 else None,
                        "status": status, "title": current["title"], "product": value.get("product"),
                        "case_type": current["source_case"]["case_type"], "tier": current["source_case"]["tier"],
                        "source_archived": entry["archived"], "transcript_status": current["source_case"]["transcript_status"],
                        "has_transcript": "transcript" in current, "evidence_status": current["evidence"]["status"],
                        "video_status": "linked" if "video" in current.get("assets", {}) else "not_applicable" if current["source_case"]["case_type"] == "structure_template" else "pending",
                        "assets": current.get("assets", {}), "missing_assets": entry["missing_assets"],
                        "index_mismatches": entry["index_mismatches"], "possible_duplicates": duplicates,
                        "identity_conflicts": known if len(known) > 1 else []})
    if apply:
        for entry, item in zip(entries, mapping):
            if item["status"] == "archive_pending":
                store.set_archived(item["case_id"], True, "补齐中断导入的旧库归档状态；首事件之后未发生用户编辑")
                item["status"] = "completed_archive"
                continue
            if item["status"] != "ready":
                continue
            case = store.add(entry["value"])
            if entry["archived"]:
                case = store.set_archived(case["case_id"], True, "导入时保留旧库归档状态")
            item["case_id"], item["status"] = case["case_id"], "imported"
    return {"mode": "apply" if apply else "dry_run", "source_root": str(Path(source_root).resolve()), "root": str(store.root),
            "source_count": len(entries), "existing_case_count": len(existing), "status_counts": dict(Counter(item["status"] for item in mapping)),
            "case_type_counts": dict(Counter(item["case_type"] for item in mapping)),
            "source_archived_count": sum(entry["archived"] for entry in entries), "mapping": mapping,
            "note": "同 library_id 保持已有记录与用户编辑；已彻底删除的来源不会重新导入；可能重复项只报告并跳过，不自动合并。"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[2] / "tiktok-video-script-voiceover")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="read and report only (default)")
    modes.add_argument("--apply", action="store_true", help="explicitly import new cases")
    args = parser.parse_args(argv)
    try:
        report = import_cases(args.root, args.source_root, apply=args.apply)
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        return 0
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        sys.stderr.write(f"import_legacy_cases: {exc}\n")
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
