"""AI three-part skeletons for comment topic opportunities (2026-11-08, 方案 B).

A skeleton follows the winning-video order: an opening in the viewer's own scene or worry (from the
comments), one or two selling points that answer it with a shot that proves it (S/A points from the
product catalog only), and a closing order reason (B points only). The AI never invents a point:
codes outside the catalog are dropped and become gaps. The writing rules themselves stay in the
writing skill's 「开头、中段与收口」; this prompt only points to them.

Skeletons are stored per product and theme in reviews/opportunity-skeletons.json. Generation runs
one theme at a time in a background thread so a batch never fires many model calls at once.
"""
from collections import deque
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading

import comment_sticker
from direction_store import fingerprint, product_key

SKILL ="product-video-script-writing"
MAX_QUOTES = 12
BATCH = 20
GAP_LABELS = {"no_point": "缺卖点", "no_closing": "缺收口理由", "unverified": "待核实"}
VERDICTS = {"existing": "补进现有方向", "new": "新开方向", "skip": "不值得做"}
# Lower sorts first: complete chains lead, then chains with named gaps (so a judged theme stays in view),
# then themes not judged yet, "not worth it" last.
RANKS = {"complete": 0, "gaps": 1, "none": 2, "skip": 3}
MAX_NOTES = 3
# What the comment behind an opening is doing; the writing skill picks how to answer from this.
OPENING_KINDS = {"objection": "质疑", "question": "提问", "statement": "陈述", "feeling": "感受"}
# A catalog code inside free text ("S9 待核", "B11 须核对"), not a steel name such as S30V.
CODE = re.compile(r"(?<![A-Za-z0-9])([SAB]\d{1,2})(?![A-Za-z0-9])")
POINT_SOURCE_FIELDS = ("id", "code", "tier", "title", "status", "rationale", "evidence")
DIRECTION_SOURCE_FIELDS = ("direction_id", "selling_point_id", "main_difference", "audience", "concern", "closing", "lead_reason", "status")


def catalog_sources(points, directions):
    """Hash semantic catalog content, excluding timestamps and unrelated product records."""
    def signature(row, fields):
        return fingerprint({key: re.sub(r"\s+", " ", str(row.get(key) or "")).strip() for key in fields})

    return {"points": {code: signature(row, POINT_SOURCE_FIELDS) for code, row in points.items()},
            "directions": {key: signature(row, DIRECTION_SOURCE_FIELDS) for key, row in directions.items()}}


def source_basis(skeleton, sources):
    """A complete chain depends on its chosen points/direction; a gap may depend on the whole catalog."""
    if skeleton.get("state") != "complete":
        return {"version": 1, "catalog": fingerprint(sources)}
    codes = {row["code"] for row in skeleton.get("middle") or []}
    if skeleton.get("closing"):
        codes.add(skeleton["closing"]["code"])
    direction_id = (skeleton.get("verdict") or {}).get("direction_id")
    return {"version": 1, "writing_skill": sources.get("writing_skill"),
            "points": {code: sources["points"].get(code) for code in sorted(codes)},
            "directions": {direction_id: sources["directions"].get(direction_id)} if direction_id else {}}


def order_reason(row):
    """A B point may close a video unless the product notes say it is not a reason to order (只适合中段回应顾虑)."""
    return "不当下单理由" not in str(row.get("rationale") or "")


class SkeletonError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).isoformat()


def _text(value, limit=300):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def parse_json(output):
    text = str(output or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise SkeletonError(502, "AI 没有交回骨架，请再点一次。")
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        raise SkeletonError(502, "AI 交回的骨架格式不对，请再点一次。") from None
    if not isinstance(data, dict):
        raise SkeletonError(502, "AI 交回的骨架格式不对，请再点一次。")
    return data


def normalize(raw, *, points, directions, quote_ids, quote_texts=None):
    """Keep only what the catalog and the quotes can back; everything else becomes a gap."""
    gaps = []

    def gap(kind, text):
        text = _text(text, 200)
        if text and not any(row["kind"] == kind and row["text"] == text for row in gaps):
            gaps.append({"kind": kind, "label": GAP_LABELS[kind], "text": text})

    def quotes(value):
        return [str(item) for item in (value if isinstance(value, list) else []) if str(item) in quote_ids][:4]

    def point(code, tiers):
        code = _text(code, 10).upper()
        row = points.get(code)
        return row if row and row.get("tier") in tiers and row.get("status") != "merged" else None

    candidates = []
    opening = []
    for row in (raw.get("opening") if isinstance(raw.get("opening"), list) else [])[:2]:
        if isinstance(row, dict) and _text(row.get("pain_zh")):
            cited = quotes(row.get("quotes"))
            kind = row.get("kind") if row.get("kind") in OPENING_KINDS else ""
            entry = {"pain_zh": _text(row.get("pain_zh")), "line_en": _text(row.get("line_en"), 200),
                     "line_zh": _text(row.get("line_zh"), 200), "audience_zh": _text(row.get("audience_zh")),
                     "quotes": cited}
            if kind:
                entry.update(kind=kind, kind_label=OPENING_KINDS[kind])
            for key in ("belief_zh", "concede_zh", "need_zh"):
                if _text(row.get(key)):
                    entry[key] = _text(row.get(key), 120)
            # The commenter's own words travel with the opening, so the script answers this comment.
            source = next((item for item in cited if _text((quote_texts or {}).get(item))), "")
            if source:
                entry["quote"] = {"id": source, "text": _text(quote_texts[source], 600)}
            opening.append(entry)
    middle = []
    for row in (raw.get("middle") if isinstance(raw.get("middle"), list) else [])[:3]:
        if not isinstance(row, dict):
            continue
        found = point(row.get("code"), {"S", "A"})
        if not found:
            gap("no_point", f"{_text(row.get('code'), 10) or '中段'}不在卖点库：{_text(row.get('answer_zh'), 120)}")
            continue
        if len(middle) == 2 or any(item["code"] == found["code"] for item in middle):
            continue
        middle.append({"code": found["code"], "point_id": found["id"], "title": found.get("title", ""),
                       "status": found.get("status", ""), "answer_zh": _text(row.get("answer_zh")),
                       "shot_zh": _text(row.get("shot_zh")), "quotes": quotes(row.get("quotes"))})
        if found.get("status") == "candidate":
            candidates.append((found["code"], "卖点"))
    closing = None
    raw_closing = raw.get("closing")
    if isinstance(raw_closing, dict) and raw_closing.get("code"):
        found = point(raw_closing.get("code"), {"B"})
        if found and not order_reason(found):
            gap("no_closing", f"{found['code']} 在产品资料里不当下单理由，只适合中段回应顾虑")
        elif found:
            closing = {"code": found["code"], "point_id": found["id"], "title": found.get("title", ""),
                       "status": found.get("status", ""), "reason_zh": _text(raw_closing.get("reason_zh"))}
            if found.get("status") == "candidate":
                candidates.append((found["code"], "理由"))
        else:
            gap("no_closing", f"{_text(raw_closing.get('code'), 10)} 不是可用的 B 类下单理由")
    for row in raw.get("gaps") if isinstance(raw.get("gaps"), list) else []:
        if isinstance(row, dict) and row.get("kind") in GAP_LABELS:
            gap(row["kind"], row.get("text_zh"))
    notes = [_text(row, 200) for row in (raw.get("notes") if isinstance(raw.get("notes"), list) else []) if isinstance(row, str)]
    for code, what in candidates:
        if not any(row["kind"] == "unverified" and code in row["text"] for row in gaps):
            gap("unverified", f"{code} 还是待核候选{what}")
    if not middle and not any(row["kind"] == "no_point" for row in gaps):
        gap("no_point", "卖点库里没有能回答这个烦恼的卖点")
    if not closing and not any(row["kind"] == "no_closing" for row in gaps):
        gap("no_closing", "卖点库里没有合适的 B 类下单理由")
    verdict = raw.get("verdict") if isinstance(raw.get("verdict"), dict) else {}
    kind = verdict.get("kind") if verdict.get("kind") in VERDICTS else "new"
    direction_id = _text(verdict.get("direction_id"), 80)
    if kind == "existing" and direction_id not in directions:
        kind, direction_id = "new", ""
    if kind != "existing":
        direction_id = ""
    verdict = {"kind": kind, "label": VERDICTS[kind], "direction_id": direction_id, "reason_zh": _text(verdict.get("reason_zh"))}
    return settle({"opening": opening, "middle": middle, "closing": closing, "verdict": verdict, "gaps": gaps, "notes": notes})


def settle(skeleton):
    """Split what holds the chain back (gaps) from reminders for the writer (notes), then set the state.

    A real gap is only: no usable middle point, no usable closing reason, or a point the chain uses
    that is still an unverified candidate. Boundary reminders ("don't claim it handles serrated
    knives") help the writer but do not stop the video from being made, so they become notes and the
    theme can still rank as complete. A note about a point the chain does not use is dropped. Safe to
    run again on a saved skeleton, which is how skeletons saved before this split get re-sorted."""
    skeleton = dict(skeleton)
    middle, closing = skeleton.get("middle") or [], skeleton.get("closing")
    parts = middle + ([closing] if closing else [])
    used = {row["code"] for row in parts}
    candidates = {row["code"] for row in parts if row.get("status") == "candidate"}
    gaps, notes = [], []

    def note(text):
        text = _text(text, 200)
        codes = set(CODE.findall(text))
        if text and text not in notes and (not codes or codes & used):
            notes.append(text)

    for row in skeleton.get("gaps") or []:
        kind, text = row.get("kind"), row.get("text", "")
        if ((kind == "no_point" and not middle) or (kind == "no_closing" and not closing)
                or (kind == "unverified" and set(CODE.findall(text)) & candidates)):
            gaps.append(row)
        else:
            note(text)
    for text in skeleton.get("notes") or []:
        note(text)
    verdict = skeleton.get("verdict") or {}
    skeleton.update(gaps=gaps[:6], notes=notes[:MAX_NOTES],
                    state="skip" if verdict.get("kind") == "skip" else
                    "complete" if skeleton.get("opening") and middle and closing and not gaps else "gaps")
    return skeleton


def brief(skeleton):
    """The skeleton as text for the writing request."""
    rows = ["AI 三段骨架（选题参考：观众顾虑 → 与顾虑相关的使用价值及画面 → 购买理由；写法以写稿技能「开头、中段与收口」为准）："]
    for row in skeleton["opening"]:
        rows.append(f"开头{'（' + row['kind_label'] + '类评论）' if row.get('kind_label') else ''}：{row['pain_zh']}"
                    + (f"（参考说法：{row['line_en']}｜{row['line_zh']}）" if row.get("line_en") else "")
                    + (f"；筛出：{row['audience_zh']}" if row.get("audience_zh") else "")
                    + (f"；观众的看法：{row['belief_zh']}" if row.get("belief_zh") else "")
                    + (f"；能认同的部分：{row['concede_zh']}" if row.get("concede_zh") else "")
                    + (f"；转向的需求：{row['need_zh']}" if row.get("need_zh") else "")
                    + (f"；对应原评论 {row['quote']['id']}：{row['quote']['text']}" if row.get("quote") else ""))
    for row in skeleton["middle"]:
        rows.append(f"中段：{row['code']} {row['title']}；{row['answer_zh']}" + (f"；要拍的证明镜头：{row['shot_zh']}" if row.get("shot_zh") else ""))
    if skeleton.get("closing"):
        rows.append(f"收口：{skeleton['closing']['code']} {skeleton['closing']['title']}；{skeleton['closing']['reason_zh']}")
    for row in skeleton["gaps"]:
        rows.append(f"缺口（{row['label']}）：{row['text']}")
    for text in skeleton.get("notes") or []:
        rows.append(f"写稿提醒：{text}")
    rows.append("骨架只是建议，不是口播逐项清单：先结合评论判断具体处境与犹豫，选择能解释该人为何需要产品的一项使用价值，并用对应动作或结果说明；回应要符合评论是在表达感受、陈述、提问还是质疑，不套固定句式。其他配置、材质与收口只在能回答本条顾虑时采用；需要介绍配置时，可结合具体使用动作或选择说明其作用，避免独立报一遍部件又重复解释用途，不因出现配置或数字就一律删除。每句都应推进同一个购买理由或回答已出现的疑问，画面可承担的细节不必全念；同一信息即使隔句换词复述，没有新增用途或回应时也可合并或省去，但有强调、承接或解释证据作用的保留。收口应自然接上前文：购买理由已讲清时不必再次复述同一人群条件或结果，保留要求的入口和可换句；用户明确指定结尾或单列时点时按其要求。要保留能够帮助观众想象何时怎样使用、以及对其顾虑有什么作用的具体例子，不以篇幅更短代替清楚解释。用户指定开箱清点、逐项展示或锁定内容时按要求保留。不得把猜测写成评论者或说话者亲历。卖点和理由以产品资料为准：卖点库“依据”写明已核的下单理由（例如质保年限、退货期限）可以直接写进收口，按依据里的边界说；只有价格数字、运费和优惠这类随渠道和日期变的内容，没核对发布当天的挂车页时不写进口播或屏显，稿外列待核。骨架给的收口理由属于后一类而不能说时，换一个已核的 B 类理由收口，不能因此不带下单理由。")
    sticker = comment_sticker.marker(skeleton)
    if sticker:
        rows.append(sticker)
    return "\n".join(rows)


class OpportunitySkeletons:
    def __init__(self, app, root):
        self.app = app
        self.path = Path(root) / "opportunity-skeletons.json"
        self.lock = threading.RLock()
        self.queue = deque()
        self.worker = None
        self.cancel = threading.Event()
        self.current = None
        with self.lock:
            data = self._load()
            # A restart ends whatever was queued or running; the card offers to try again.
            changed = False
            for themes in data["products"].values():
                for row in themes.values():
                    if row.get("status") in {"queued", "running"}:
                        row.update(status="failed", error="工作台重启，生成中断了，请再点一次。", finished_at=_now())
                        row.pop("progress", None)
                        changed = True
            if changed:
                self._save(data)

    def busy(self):
        with self.lock:
            return bool(self.queue or self.current or self.worker and self.worker.is_alive())

    # ---------- storage ----------
    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("products"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "products": {}}

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def _update(self, product, theme, **fields):
        with self.lock:
            data = self._load()
            row = data["products"].setdefault(product, {}).setdefault(theme, {})
            row.update(fields)
            for key in [key for key, value in fields.items() if value is None]:
                row.pop(key, None)
            self._save(data)
            return copy.deepcopy(row)

    # ---------- reading ----------
    @staticmethod
    def public(row, basis=None, *, sources=None):
        if not row:
            return {"status": "none", "state": "none"}
        result = {key: copy.deepcopy(row.get(key)) for key in ("status", "progress", "error", "finished_at", "queued_at", "model", "reasoning_effort")
                  if row.get(key) is not None}
        if row.get("skeleton"):
            result.update(settle(copy.deepcopy(row["skeleton"])))
            result["generated_at"] = row.get("generated_at")
            comments_stale = basis is not None and row.get("basis") != basis
            saved_sources = row.get("source_basis")
            sources_stale = not saved_sources or sources is None or saved_sources != source_basis(result, sources)
            result["stale"] = bool(comments_stale or sources_stale)
            if result["stale"]:
                reasons = (["评论有变化"] if comments_stale else [])
                if sources_stale:
                    reasons.append("写稿资料有变化" if saved_sources else "旧骨架缺少资料版本")
                result["stale_reason"] = "；".join(reasons) + "，重新生成后可带入写稿"
        result.setdefault("state", "none")
        return result

    def attach(self, product, items):
        """Called by opportunity_catalog: add each theme's skeleton, its rank, and the brief for writing."""
        product = product_key(product)
        sources = self._sources(*self._catalog(product))
        with self.lock:
            themes = copy.deepcopy(self._load()["products"].get(product, {}))
        for item in items:
            if item.get("topic") == "none":
                item["skeleton"] = {"status": "unavailable", "state": "none"}
                item["_skeleton_rank"] = RANKS["none"]
                continue
            skeleton = self.public(themes.get(item["id"]), item.get("basis"), sources=sources)
            item["skeleton"] = skeleton
            item["_skeleton_rank"] = RANKS["none"] if skeleton.get("stale") else RANKS.get(skeleton.get("state"), RANKS["none"])
            if not skeleton.get("stale") and skeleton.get("opening") is not None and skeleton.get("state") != "none":
                item["draft_instruction"] += "\n\n" + brief(skeleton)

    def states(self, product):
        product = product_key(product)
        sources = self._sources(*self._catalog(product))
        with self.lock:
            themes = self._load()["products"].get(product, {})
            return {"product": product, "running": any(row.get("status") in {"queued", "running"} for row in themes.values()),
                    "themes": {key: self.public(row, sources=sources) for key, row in themes.items()}}

    # ---------- generating ----------
    def start(self, body):
        if not isinstance(body, dict):
            raise SkeletonError(400, "请求格式不对。")
        product = product_key(body.get("product"))
        if not product or product.lower() == "all":
            raise SkeletonError(400, "先选择一个具体产品。")
        themes = body.get("themes")
        if not isinstance(themes, list) or not themes or not all(isinstance(item, str) and item for item in themes):
            raise SkeletonError(400, "选择要生成骨架的主题。")
        themes = list(dict.fromkeys(themes))[:BATCH]
        if any(item == "topic:unclassified" for item in themes):
            raise SkeletonError(400, "待整理证据池还不是一个主题，先整理标签再生成骨架。")
        settings = self._model_settings(body)
        with self.lock:
            data = self._load()
            existing = data["products"].setdefault(product, {})
            added = []
            for theme in themes:
                if existing.get(theme, {}).get("status") in {"queued", "running"}:
                    continue
                existing.setdefault(theme, {}).update(status="queued", queued_at=_now(), model=settings["model"],
                                                      reasoning_effort=settings["reasoning_effort"])
                existing[theme].pop("error", None)
                existing[theme].pop("progress", None)
                self.queue.append((product, theme))
                added.append(theme)
            self._save(data)
            if added and not (self.worker and self.worker.is_alive()):
                self.cancel.clear()
                self.worker = threading.Thread(target=self._drain, daemon=True, name="opportunity-skeletons")
                self.worker.start()
        return self.states(product)

    def _model_settings(self, body):
        """The model for this batch: the one picked in the request, else what 骨架 last used, else the global default."""
        picked = ({"model": body.get("model"), "reasoning_effort": body.get("reasoning_effort")}
                  if isinstance(body, dict) and "model" in body else {})
        try:
            return self.app.ai.resolve_default_settings(picked, kind="skeleton")
        except Exception as exc:
            if picked:
                raise SkeletonError(400, str(exc)[:200] or "模型选择无效。")
            try:
                settings = self.app.ai.preferences()
                return {"model": settings.get("model"), "reasoning_effort": settings.get("reasoning_effort")}
            except Exception:
                return {"model": None, "reasoning_effort": None}

    def stop(self, body):
        product = product_key((body or {}).get("product"))
        with self.lock:
            data = self._load()
            for theme, row in data["products"].get(product, {}).items():
                if row.get("status") == "queued":
                    row.update(status="cancelled", error="已停止。", finished_at=_now())
            self._save(data)
            self.queue = deque(entry for entry in self.queue if entry[0] != product)
            if self.current and self.current[0] == product:
                self.cancel.set()
        return self.states(product)

    def _drain(self):
        while True:
            with self.lock:
                if not self.queue:
                    self.current = None
                    return
                self.current = self.queue.popleft()
                self.cancel.clear()
            product, theme = self.current
            self._generate(product, theme, self.cancel)

    def _theme(self, product, theme):
        from review_opportunities import opportunity_catalog
        # Reading the catalog for one theme must not apply skeleton ranking to itself: use the raw groups.
        catalog = opportunity_catalog(SkeletonFreeApp(self.app), product, limit=100, evidence_limit=MAX_QUOTES)
        return next((item for item in catalog["items"] if item["id"] == theme), None)

    def _catalog(self, product):
        catalog = self.app.directions.read()
        rows = [row for row in catalog["selling_points"] if product_key(row.get("product")) == product]
        points = {row["code"]: row for row in rows}
        merged = {row["id"] for row in rows if row.get("status") == "merged"}
        # A direction whose lead point was merged into another code is retired even while only paused: offering it
        # made skeletons say 「补进 202H-S9」 for a direction nobody can write for any more.
        directions = {row["direction_id"]: row for row in catalog["directions"]
                      if product_key(row.get("product")) == product and row.get("status") != "archived"
                      and row.get("selling_point_id") not in merged}
        return points, directions

    def _skill(self):
        root = Path(getattr(self.app.ai, "skill_code_root", "") or "")
        path = root.parent / SKILL / "SKILL.md"
        return [{"name": SKILL, "path": str(path)}] if path.is_file() else []

    def _sources(self, points, directions, *, references=None):
        sources = catalog_sources(points, directions)
        skills = self._skill()
        texts = {}
        if skills:
            root = Path(skills[0]["path"]).parent
            for name in ("SKILL.md", "references/writing-and-native.md", "references/guidance.md"):
                try:
                    texts[name] = (root / name).read_text(encoding="utf-8")
                    if references is not None and name != "SKILL.md":
                        references.append({"label": name, "path": str((root / name).resolve()), "text": texts[name]})
                except OSError:
                    texts[name] = None
        sources["writing_skill"] = fingerprint(texts) if texts else None
        return sources

    @staticmethod
    def prompt(product, item, points, directions):
        quotes = "\n".join(f"- {row['id']}｜{row.get('platform_label') or row.get('platform')}｜{row.get('role_label') or row.get('role')}｜"
                           f"{_text(row.get('text'), 600)}" for row in item["evidence"])
        def point_rows(tiers):
            return "\n".join(f"- {row['code']}｜{row.get('title', '')}｜{'待核候选' if row.get('status') == 'candidate' else '在测'}"
                             f"｜对谁、怎么用：{_text(row.get('rationale'), 140) or '未写'}｜依据：{_text(row.get('evidence'), 160) or '未写'}"
                             for row in sorted(points.values(), key=lambda row: row["code"])
                             if row.get("tier") in tiers and row.get("status") != "merged"
                             and (tiers != {"B"} or order_reason(row))) or "（没有）"
        covered = {row["direction_id"]: row for row in item.get("directions") or []}
        direction_rows = "\n".join(f"- {key}｜主打：{row.get('main_difference', '')}｜人群：{_text(row.get('audience'), 120)}｜顾虑链：{_text(row.get('concern'), 160)}"
                                   + (f"｜已关联本主题，{covered[key]['published']} 条已发布" if key in covered else "")
                                   for key, row in sorted(directions.items())) or "（没有）"
        return f"""按 {SKILL} 技能「开头、中段与收口」的规则，把一组用户评论整理成一条带货短视频的三段选题骨架。只做选题判断，不写整稿。

产品：{product}
评论主题：{item['title']}（{item['distinct_reviews']} 种不同原文，近 14 天新入库 {item['recent_reviews']} 种）
评论原文（编号｜来源｜身份｜原文；评论者说法未经核实，只能当作观众的处境、烦恼和疑问，不能当作商品事实）：
{quotes}

卖点库里的 S/A 卖点（中段只能从这里选）：
{point_rows({"S", "A"})}

卖点库里的 B 类下单理由（收口只能从这里选）：
{point_rows({"B"})}

这个产品现有的方向：
{direction_rows}

选题骨架（用于提出可检验的内容方向，不保证效果，也不是成稿时必须逐项照念）：
1. 开头说到心里：用评论里观众自己的场景或烦恼开场，不懂这个品类的人也认得出；这一步留人，也筛出该看的人。区分原评论的陈述、感受、提问或质疑，回应的语气与其原意对应，不机械把陈述说成提问或强行套一句固定回应。写 1–2 个，pain_zh 用观众的处境说（不是产品功能），line_en 给一句美国 TikTok 达人口吻的开头参考，line_zh 是它的中文，audience_zh 写这个开头筛出谁。每个开头只对应一种看法或处境，quotes 的第一条放最能代表它的那条评论；主题里有两种不同看法（例如“没必要”和“不耐用”）就各写一个，不合成一个。评论里有质疑（说没必要、不值、不如别的做法、会有坏处）而卖点库能回应时，至少有一个开头来自质疑：质疑是想买的人卡住的地方，比单纯提问更值得回应。kind 写这条评论在做什么：objection 质疑、question 提问、statement 陈述、feeling 感受。kind 是 objection 时再写两项：belief_zh 是观众的看法本身（不要改写成痛点），concede_zh 是这个看法里说得对、可以先认同的部分；评论里核实不了的技术说法不写进 concede_zh，在 notes 标待核；need_zh 是认同之后要转向的需求：这群人自己真正在意、并且卖点库里有 S 卖点能满足的那件事（质疑“镜面没必要”的人，在意的可能是每次补磨回到同一角度），要和评论接得上，不是评论争的那个点，也不是它换个说法（评论争镜面，need_zh 就不能是“按需选择收尾精度”“想亮的时候能磨亮”）：问自己“一个觉得这东西没必要的人，为什么还会买这套”，答案才是 need_zh。其他 kind 不写这三项。
2. 中段讲卖点并且画面证明：只选 1–2 个卖点，每个都必须回答开头那个烦恼；开头是质疑时，第一个卖点回答的是 need_zh（观众为什么要买），必须是和评论所争卖点不同的另一个 S 卖点（评论争镜面，第一个卖点就不能是讲镜面精修的那个），按卖点“对谁、怎么用”一栏挑这群人最可能在意的；评论争的点最多作为第二个卖点，或留给写稿带一句；verdict 的方向也跟第一个卖点走；answer_zh 说具体哪类人、什么情况下怎样使用以及能获得什么价值，shot_zh 写可拍的使用动作或有依据的结果，不能只罗列配置和材质。配置或机制要和该使用动作、选择或价值连起来，而不是让部件清点代替具体解释。补充内容须推进同一购买理由或回应已出现的疑问，不能为了把配置讲全而堆叠，也不能压掉理解使用价值所需的具体情境；用户明确要求开箱或逐项展示时按其要求。若评论没说清具体使用困扰，区分假设与评论原话，不虚构经历或把假设写成普遍事实。
3. 收口给下单理由：从 B 类里选一个和开头烦恼最搭的，reason_zh 说为什么搭；若前文已有此购买理由，保留理由供取舍即可，不必在口播结尾再换词重复，实际入口自然承接前文。依据栏写明已核的理由（例如质保年限）可以直接选；价格数字、运费和优惠随渠道和日期变，选了就在 notes 写明发布当天要核对什么，有已核的理由同样搭时优先选已核的。
- gaps 只写拍不成的原因：卖点库里没有能回答这个烦恼的卖点，就不要硬凑，middle 留空并在 gaps 写 no_point，说清楚缺什么样的卖点；没有合适的 B 类理由同理（closing 留空，写 no_closing）；选到待核候选的卖点或理由，写 unverified 并带上它的编号。
- 写稿时要守的边界（哪些话不能说、哪些说法要先核对、要拍什么才算证明）写进 notes，不写进 gaps：notes 最多 3 条，每条不超过 40 字，只关于这条骨架用到的开头和卖点，意思相近的合成一条；和本主题无关、这次不用的评论和卖点直接略过。
- 写短，卡片上一眼能读完：pain_zh、belief_zh、concede_zh、need_zh 各不超过 30 字，answer_zh 不超过 40 字，shot_zh 不超过 50 字，reason_zh 不超过 40 字；边界提醒放进 notes，不写进这几项。
- quotes 填支撑这一项的评论编号（只用上面给的编号）。
- verdict：这个主题对应现有方向就写 existing 和 direction_id；现有方向都不对就写 new；评论只是零散抱怨、和产品购买理由无关、或只能靠编造才能回应，就写 skip。reason_zh 一句话说理由。

只输出一个 JSON 对象，不要别的文字：
{{"opening":[{{"pain_zh":"角度一直靠猜，磨完也不知道对不对","line_en":"I was guessing my angle for years.","line_zh":"我猜了好几年角度。","audience_zh":"手磨磨不稳的人","kind":"statement","quotes":["评论编号"]}}],
"middle":[{{"code":"S1","answer_zh":"怎么回答开头的烦恼","shot_zh":"要拍的证明镜头","quotes":[]}}],
"closing":{{"code":"B3","reason_zh":"为什么这个理由最搭"}},
"verdict":{{"kind":"existing","direction_id":"{product}-S1","reason_zh":"一句话理由"}},
"gaps":[],
"notes":["写稿时要守的一条边界"]}}"""

    def _generate(self, product, theme, event):
        self._update(product, theme, status="running", progress="AI 正在读评论、对卖点…", started_at=_now())
        try:
            item = self._theme(product, theme)
            if not item:
                raise SkeletonError(404, "这个主题已经没有评论了，刷新页面看看。")
            points, directions = copy.deepcopy(self._catalog(product))
            references = []
            sources = self._sources(points, directions, references=references)
            with self.lock:
                row = self._load()["products"].get(product, {}).get(theme, {})
            settings = ({"model": row.get("model"), "reasoning_effort": row.get("reasoning_effort")}
                        if "model" in row else self._model_settings(None))

            def progress(text):
                self._update(product, theme, progress=_text(text, 200))

            folder = Path(self.path.parent)
            output = self.app.ai.bridge.run(self.prompt(product, item, points, directions), cwd=str(folder.resolve()),
                                            cancel_event=event, on_progress=progress, model=settings.get("model"),
                                            reasoning_effort=settings.get("reasoning_effort"), skills=self._skill(),
                                            **({"references": references} if str(settings.get("model") or "").startswith("antigravity:") else {}))
            if event.is_set():
                raise SkeletonError(409, "已停止。")
            skeleton = normalize(parse_json(output), points=points, directions=directions,
                                 quote_ids={row["id"] for row in item["evidence"]},
                                 quote_texts={row["id"]: row.get("text") for row in item["evidence"]})
            self._update(product, theme, status="done", skeleton=skeleton, basis=item.get("basis"),
                         source_basis=source_basis(skeleton, sources), title=item["title"],
                         generated_at=_now(), finished_at=_now(), progress=None, error=None)
        except Exception as exc:
            cancelled = event.is_set()
            self._update(product, theme, status="cancelled" if cancelled else "failed", finished_at=_now(), progress=None,
                         error="已停止。" if cancelled else (_text(exc, 400) or "AI 没有完成，请再点一次。"))


class SkeletonFreeApp:
    """The app as the catalog sees it, minus saved skeletons (generation reads raw themes)."""

    def __init__(self, app):
        self._app = app

    def __getattr__(self, name):
        if name == "opportunity_skeletons":
            raise AttributeError(name)
        return getattr(self._app, name)
