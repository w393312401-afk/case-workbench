"""The whole-group plan of a photo carousel and its group-level acceptance.

A good carousel is judged as a group: one route (商品展示/真人出镜/前后对比/故事情节), 1–3 core selling-point tags,
a 首图钩子 → 中间展示 → 尾图转化 rhythm, more than one shot type, and a music direction for publishing. The plan is
metadata about the group; it never changes a slide's words or image contract, so existing image signatures stay valid.
"""
from __future__ import annotations

import json

from direction_store import fingerprint
from elevenlabs_client import VoiceError
from carousel_lineage import LINEAGE_RULES, lineage_context, mapping_issues, slide_lineage

ROUTES = {"product": "商品展示", "on_person": "真人出镜", "before_after": "前后对比", "story": "故事情节"}
STAGES = {"hook": "首图钩子", "show": "中间展示", "convert": "尾图转化"}
SHOTS = {"wide": "全景", "close": "近景", "detail": "特写", "on_body": "上身/手持", "in_use": "使用场景", "flat": "平铺"}
MAX_TAGS = 3
GROUP_CHECKS = {"hook": "首图识别与钩子", "progression": "中间推进与多角度", "consistency": "画风一致",
                "tags": "卖点标签", "close": "尾图收口", "read_through": "连读检查", "delete_test": "删图检查",
                "script_fidelity": "原稿推进与意思", "proof_alignment": "本张任务与画面证据"}
GROUP_REVIEW_SCHEMA = {"type": "object", "properties": {
    "summary": {"type": "string"},
    "checks": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string", "enum": list(GROUP_CHECKS)}, "status": {"type": "string", "enum": ["pass", "fail", "uncertain"]},
        "note": {"type": "string"}}, "required": ["id", "status", "note"], "additionalProperties": False}},
    "slides": {"type": "array", "items": {"type": "object", "properties": {
        "number": {"type": "integer"}, "observed_shot": {"type": "string", "enum": [*SHOTS, "other"]},
        "note": {"type": "string"}}, "required": ["number", "observed_shot", "note"], "additionalProperties": False}},
    "redundant_slides": {"type": "array", "items": {"type": "integer"}},
    "suggestions": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary", "checks", "slides", "redundant_slides", "suggestions"], "additionalProperties": False}


def _text(value, limit):
    return str(value or "").strip()[:limit] if isinstance(value, (str, int, float)) else ""


def empty_plan():
    return {"route": "", "tags": [], "music": ""}


def normalize_plan(raw, *, strict=False):
    """The stored plan. AI output is cleaned leniently (problems listed); a user save is refused when invalid."""
    raw = raw if isinstance(raw, dict) else {}
    problems = []
    route = raw.get("route", "")
    if route not in {"", *ROUTES}:
        route = next((key for key, label in ROUTES.items() if label == route), None)
        if route is None:
            if strict:
                raise VoiceError(400, "请选择商品展示、真人出镜、前后对比或故事情节。")
            problems.append("整组方案的路线无法识别，请在制作单里补选。")
            route = ""
    tags = []
    values = raw.get("tags", [])
    if not isinstance(values, list):
        if strict:
            raise VoiceError(400, "卖点标签须逐条填写。")
        values = []
    for entry in values:
        entry = {"en": entry, "zh": ""} if isinstance(entry, str) else entry
        if not isinstance(entry, dict):
            continue
        tag = {"en": _text(entry.get("en"), 80), "zh": _text(entry.get("zh"), 80)}
        if tag["en"] or tag["zh"]:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        if strict:
            raise VoiceError(400, f"核心卖点标签最多 {MAX_TAGS} 个。")
        problems.append(f"AI 给了 {len(tags)} 个卖点标签，只保留前 {MAX_TAGS} 个。")
        tags = tags[:MAX_TAGS]
    music = raw.get("music", "")
    if not isinstance(music, str) or len(music) > 300:
        if strict:
            raise VoiceError(400, "音乐方向须是 300 字以内的文字。")
        music = _text(music, 300)
    return {"route": route, "tags": tags, "music": music.strip()}, problems


def slide_fields(raw, *, strict=False):
    """A slide's stage and shot type, accepting either the key or the Chinese label."""
    raw = raw if isinstance(raw, dict) else {}
    result, problems = {}, []
    for key, choices, label in (("stage", STAGES, "环节"), ("shot", SHOTS, "景别")):
        value = raw.get(key, "")
        if value not in {"", *choices}:
            value = next((name for name, text in choices.items() if text == value), None)
            if value is None:
                if strict:
                    raise VoiceError(400, f"第 {raw.get('number')} 张的{label}无效。")
                problems.append(f"第 {raw.get('number')} 张的{label}无法识别，请在制作单里补选。")
                value = ""
        result[key] = value
    return result, problems


def structure_checks(item):
    """Instant checks from the plan and slides alone; warnings advise, they never block making or delivery."""
    plan = item.get("plan") or empty_plan()
    slides = item.get("slides") or []
    checks = []

    def add(key, label, ok, note, *, level="warn"):
        checks.append({"id": key, "label": label, "status": "pass" if ok else level, "note": note})

    count = len(slides)
    add("count", "张数", 3 <= count <= 5, f"共 {count} 张" + ("" if 3 <= count <= 5 else "，建议 3–5 张"))
    add("route", "展示路线", bool(plan.get("route")),
        ROUTES.get(plan.get("route"), "") or "未选路线（商品展示、真人出镜、前后对比或故事情节）")
    tags = plan.get("tags") or []
    add("tags", "核心卖点标签", 1 <= len(tags) <= MAX_TAGS,
        "、".join(tag.get("en") or tag.get("zh") for tag in tags) if tags else "还没有卖点标签，建议 1–3 个")
    stages = [slide.get("stage", "") for slide in slides]
    if slides:
        add("hook", "首图钩子", stages[0] == "hook", "第 1 张是首图钩子" if stages[0] == "hook" else
            f"第 1 张标的是「{STAGES.get(stages[0], '未标')}」，首图应承担钩子")
        add("convert", "尾图转化", stages[-1] == "convert", f"第 {slides[-1]['number']} 张是尾图转化" if stages[-1] == "convert" else
            f"最后一张标的是「{STAGES.get(stages[-1], '未标')}」，尾图应重申卖点并引导下单")
    shots = [slide.get("shot", "") for slide in slides]
    known = {shot for shot in shots if shot}
    unknown = [str(slide["number"]) for slide in slides if not slide.get("shot")]
    if unknown:
        add("shots", "多角度展示", False, f"第 {'、'.join(unknown)} 张还没标景别")
    else:
        varied = len(known) >= 2 and known != {"flat"}
        add("shots", "多角度展示", varied, "、".join(SHOTS[shot] for shot in SHOTS if shot in known) +
            ("" if varied else "：只有一种景别，避免只停留在单一平铺"))
    add("music", "音乐方向", bool(plan.get("music")), plan.get("music") or "未写音乐方向（发布时在 TikTok 里选热门且贴合调性的音乐）",
        level="info")
    return checks


def slide_image(item, slide, current_output, base_hash):
    """The image a group review reads for a slide: its valid selected output, else its chosen base."""
    output = next((entry for entry in slide.get("outputs", []) if entry["id"] == slide.get("output_id")), None)
    if output and current_output(item, slide, output) and not output.get("stale"):
        return {"number": slide["number"], "kind": "output", "id": output["id"], "sha256": output["sha256"]}
    base = slide.get("base")
    if base:
        return {"number": slide["number"], "kind": "base", "id": base["id"], "sha256": base_hash(slide)}
    return None


def group_signature(item, images):
    """What a group review read: the plan, every slide's words, stage, shot and image identity."""
    slides = [{**{key: slide.get(key) for key in ("number", "title_lines", "body", "role", "stage", "shot", "source", "forbidden")},
               **slide_lineage(slide)}
              for slide in item.get("slides") or []]
    return fingerprint({"plan": item.get("plan") or empty_plan(), "slides": slides, "images": images,
                        "caption": item.get("caption_en", ""), "script_context": lineage_context(item), "review_version": 2})


def group_review_prompt(snapshot):
    return f"""这是挂车图文的整组只读验收，只看已提供的图片，不生成、修复、排版或修改任何文件。
按提供顺序实际查看每张图片，第 N 张图片就是第 N 张图文；images 里 kind=base 的张还没出成图，只是选好的底图，叠字尚未排上，文字类判断以锁定文案为准并在 note 里说明。
整组方案与锁定文案：{json.dumps(snapshot, ensure_ascii=False)}
{LINEAGE_RULES}
按优质图文标准逐项判断整组，而不是只看单张是否漂亮：
- hook 首图识别与钩子：承接原稿开头的任务，第一眼能否认出本次话题和观看理由；不强制改成痛点开头。
- progression 中间推进与多角度：中间每张是否按原稿推进补充用途、细节、体验、证据或情节；景别和对照是否服务该张任务，避免重复换角度，但不为凑景别新增内容。
- consistency 画风一致：整组色调、光线、字体和标题层级是否一致，适合连续浏览；场景和景别可以变化。
- tags 卖点标签：plan.tags 里的 1–3 个核心卖点是否在图中或叠字里清楚可读、能被画面或已确认资料支撑；没有标签或超过 3 个时判 fail。
- close 尾图收口：最后一张是否承接前面的价值，保留原稿已有的购买判断和行动引导；原稿没有的价格优势、折扣或效果承诺不能补入，也不强制改成结果收尾。
- read_through 连读检查：只按顺序读标题与必要说明，再对照画面，是否形成完整、可理解的购买理由，前面提出的问题后面有回答。
- delete_test 删图检查：假设去掉某张，商品理解、证据、决策信息或必要情节是否受损；几乎无影响的张写进 redundant_slides。
- script_fidelity 原稿推进与意思：将 script_context.records 的冻结原稿按 source_lines 与逐张内容对照，检查受众、主打、开头承诺、必要论据、限定条件、因果顺序、购买判断与收口是否保留。合并相邻意群、压缩重复允许；不要求每句话逐字搬上图，不能把遗漏必要理由误当压缩。缺原稿或映射只能 uncertain；不能因为图文自己读得通就通过。
- proof_alignment 本张任务与画面证据：逐张对照 proof_goal、evidence_requirement、原意群 proof/evidence 与实际图片；明确描述看到了什么、缺什么。文字写拧旋钮而图片只有整机，不能通过；结果证据被遮挡也不能通过。只检验原稿实际需要的证明，不要求所有图组有结果图。visual_method 的对照、特写、步骤拼图必须服务原任务，不是独立通过理由。
每张在 slides 里写实际看到的景别 observed_shot（wide 全景、close 近景、detail 特写、on_body 上身/手持、in_use 使用场景、flat 平铺、other 其他）和一句具体说明。
看不清或无法判断时用 uncertain，不猜；不因有真人、角度多、before/after 或「立即购买」就单独判通过。
suggestions 给 0–5 条最能补齐链路的具体建议（换哪张、补什么画面、文案怎么收窄），不要泛泛而谈。
返回 checks 九项各一次、slides 逐张各一条，以及 summary、redundant_slides、suggestions；仅返回约定 JSON。
"""


def normalize_group_review(answer, numbers, snapshot=None):
    """A complete group review has every check once and every slide once; anything else is recorded as incomplete."""
    if isinstance(answer, str):
        try:
            answer = json.loads(answer)
        except ValueError:
            answer = None
    if not isinstance(answer, dict):
        return {"completed": False, "status": "uncertain", "summary": "整组验收没有返回可读结果。", "checks": [],
                "slides": [], "redundant_slides": [], "suggestions": []}
    checks, seen = [], set()
    for entry in answer.get("checks") or []:
        if isinstance(entry, dict) and entry.get("id") in GROUP_CHECKS and entry["id"] not in seen and \
                entry.get("status") in {"pass", "fail", "uncertain"}:
            seen.add(entry["id"])
            checks.append({"id": entry["id"], "label": GROUP_CHECKS[entry["id"]], "status": entry["status"],
                           "note": _text(entry.get("note"), 1000)})
    order = list(GROUP_CHECKS)
    checks.sort(key=lambda entry: order.index(entry["id"]))
    if snapshot is not None:
        issues = mapping_issues(snapshot)
        for check in checks:
            if check["id"] not in {"script_fidelity", "proof_alignment"}:
                continue
            if issues["invalid"] and check["id"] == "script_fidelity":
                check.update(status="fail", note="；".join(issues["invalid"]))
            elif issues["missing"] and check["status"] == "pass":
                check.update(status="uncertain", note="；".join(issues["missing"]))
    slides, numbered = [], set()
    for entry in answer.get("slides") or []:
        if isinstance(entry, dict) and entry.get("number") in numbers and entry["number"] not in numbered:
            numbered.add(entry["number"])
            shot = entry.get("observed_shot") if entry.get("observed_shot") in {*SHOTS, "other"} else "other"
            slides.append({"number": entry["number"], "observed_shot": shot, "note": _text(entry.get("note"), 600)})
    slides.sort(key=lambda entry: entry["number"])
    completed = seen == set(GROUP_CHECKS) and numbered == set(numbers)
    status = "pass" if completed and all(entry["status"] == "pass" for entry in checks) else \
        "fail" if any(entry["status"] == "fail" for entry in checks) else "uncertain"
    return {"completed": completed, "status": status, "summary": _text(answer.get("summary"), 1500), "checks": checks,
            "slides": slides,
            "redundant_slides": sorted({n for n in answer.get("redundant_slides") or [] if n in numbers}),
            "suggestions": [_text(entry, 500) for entry in (answer.get("suggestions") or [])[:5] if _text(entry, 500)]}
