"""「我去拍了」值守模式：用户出门拍素材时，工作台按计划自己跑不花钱、不用拍板的活。

五条道：
  网络道  从 Apify 抓新评论（review_scrape）
  配音道  执行池里的单：按你放进池子时定好的音色和设置配一条，对齐过了就选用它；同时最多 VOICE_PARALLEL 条，不重试（花钱）
  AI 道   翻译评论、选题骨架、按选题机会主题或待写计划写稿候选；同一时间最多 AI_PARALLEL 件
  达芬奇道 执行池里的单：等自己的配音选用后（已配音的直接来），用池子里定好的达人和素材新建时间线 → AI 剪辑，
          同一达人的单最多 RESOLVE_PARALLEL 张同剪（共用这个达人的达芬奇工程），别的达人等前面剪完；按达人分组排，停在等确认；不重试
  本机道  收尾检查缺口（完整备份 11-20 起不再提供，代码留着；旧会话里的备份项照旧能跑完）

执行池（away-pool.json）：候选稿「采用并入池」、采用稿页或制作单配音区「放进执行池」放进来，音色、达人和素材在弹窗的池子里定；放进池子就是同意给它配音。
池子里的单开跑配音时才移出池子，没轮到的留着下次用。
已经做过的步骤不再做（fly 11-23）：已有一条对齐过、对得上现在稿子的配音就不再配（还没选用的开剪前自动选用），
已为这条配音和这个达人建好、还没剪过的时间线直接拿来剪，不再新建；素材默认用这张单自己上次勾的。

不做：池子以外的配音、采用 / 定稿、确认剪辑完成、挑评论、发布、上传、删除。每件活失败自动重试一次，再失败就跳过并记进报告；
到「回来时间 - STOP_BEFORE」后不再开始新活，正在跑的跑完。计划和进度存运行目录 away-mode.json，重启后接着排。
"""
import copy
import hashlib
import json
import math
import os
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from direction_store import product_key
from elevenlabs_client import DEFAULT_MODEL, MODELS, SETTING_RANGES, clean_settings, model
import markets
from material_pick import MaterialLibrary, latest_materials, script_lines


def _fresh(take, script_sha):
    """Passed the alignment check and was read from the script as it is now (old takes carry no fingerprint)."""
    if not isinstance(take, dict) or (take.get("quality") or {}).get("status") != "passed":
        return False
    return not script_sha or not take.get("tts_sha256") or take["tts_sha256"] == script_sha


def ready_take(job, script_sha=None):
    """A voiceover this order can be cut with as it is, so the pool never pays for one again: the chosen one,
    else its latest take that passed the check for the current script. None when there is none."""
    gone = set(job.get("deleted_take_ids") or [])
    takes = [take for take in job.get("takes") or [] if isinstance(take, dict) and take.get("id") and take["id"] not in gone]
    chosen = job.get("selected_take")
    if chosen:
        take = next((take for take in takes if take["id"] == chosen), None)
        if take is None:
            return {"id": chosen}  # chosen, details not at hand: cut with it, as before
        if _fresh(take, script_sha):
            return take
    fresh = [take for take in takes if _fresh(take, script_sha)]
    return max(fresh, key=lambda take: str(take.get("created_at") or "")) if fresh else None


def built_timeline(job, take_id, creator):
    """A timeline already built for this voiceover in this creator's project that no edit has touched and nobody
    finished by hand: the pool cuts on it instead of building another."""
    if not take_id or not creator:
        return None
    used = {edit.get("timeline_uid") for edit in job.get("edits") or [] if isinstance(edit, dict)}
    used.add((job.get("manual_completion") or {}).get("timeline_uid"))
    for timeline in reversed(job.get("timelines") or []):
        uid = str((timeline or {}).get("unique_id") or "") if isinstance(timeline, dict) else ""
        if (uid and not uid.startswith("pending:") and uid not in used and not timeline.get("archived")
                and timeline.get("take_id") == take_id and timeline.get("creator") == creator
                and timeline.get("task_status") in (None, "pending")):
            return timeline
    return None


def own_pick(job):
    """The creator and footage this order's own last cut used: what was already picked for it."""
    by_uid = {row.get("unique_id"): row for row in job.get("timelines") or [] if isinstance(row, dict)}
    for edit in reversed(job.get("edits") or []):
        names = [name for name in (edit.get("materials") or []) if isinstance(name, str) and name.strip()] if isinstance(edit, dict) else []
        if names:
            return (by_uid.get(edit.get("timeline_uid")) or {}).get("creator") or "", names
    return "", []


def needs_table(job):
    try:
        from case_replica import needs_table as check
    except Exception:
        return False
    return check(job)

AI_PARALLEL = 2
VOICE_PARALLEL = 2
# Edits of one creator share that creator's DaVinci project turn by turn (resolve_share); another creator's waits.
RESOLVE_PARALLEL = 2
MAX_POOL = 8
STOP_BEFORE = timedelta(minutes=15)
TICK_SECONDS = 5
MAX_ATTEMPTS = 2
MINUTES = (60, 90, 120, 180)
DEFAULT_MINUTES = 90
DEFAULT_AI_LIMIT = 12
MAX_AI_LIMIT = 40
MAX_TOPICS = 6
DEFAULT_TOPICS = 2
RECENT_DAYS = 7
MAX_THEMES = 8
HISTORY = 10

# lane, label, estimated minutes (writing per direction, skeletons per theme), whether it counts against the AI limit
KINDS = {
    "scrape": {"lane": "net", "label": "抓新评论", "minutes": 15},
    "translate": {"lane": "ai", "label": "翻译评论", "minutes": 6, "ai": True},
    "tags": {"lane": "ai", "label": "AI 补标签建议", "minutes": 6, "ai": True},
    "skeletons": {"lane": "ai", "label": "选题骨架", "minutes": 1, "ai": True},
    "writing": {"lane": "ai", "label": "写稿候选", "minutes": 7, "ai": True},
    "voice": {"lane": "voice", "label": "配音", "minutes": 2},
    "edit": {"lane": "resolve", "label": "AI 剪辑", "minutes": 15, "ai": True},
    "backup": {"lane": "local", "label": "完整备份", "minutes": 10, "exclusive": True},
    "check": {"lane": "local", "label": "收尾检查缺口", "minutes": 1, "final": True},
}
ORDER = ("scrape", "voice", "edit", "writing", "skeletons", "translate", "tags", "backup", "check")
# translate and tags wait for the scrape, so they cover the comments it brings in
AFTER = {"translate": ("scrape",), "tags": ("scrape", "translate")}
TERMINAL = {"done", "failed", "skipped", "cancelled"}
AI_ACTIVE = {"queued", "running", "stopping"}
EDIT_ACTIVE = {"queued", "running"}
EDIT_FAILED = {"failed", "interrupted", "cancelled"}
# what an AI edit that finished asks of the user
EDIT_OUTCOME = {"needs_material": "剪了，但素材不够，要补拍或换素材", "needs_repair": "剪了，有地方要修",
                "verification_failed": "剪了，核对没过，要修", "needs_attention": "剪了，有地方要你看"}


class AwayError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _utc(clock):
    return clock().astimezone(timezone.utc)


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _short(exc, limit=200):
    text = str(exc).strip() or type(exc).__name__
    return text[:limit]


def _creator(item):
    """Whose DaVinci project a cut runs in; every cut of one session is of the same product."""
    return str((item.get("params") or {}).get("creator") or "").casefold() if item["kind"] == "edit" else ""


def _creator_order(items):
    """Cuts grouped by creator, so the creators' DaVinci projects are switched as seldom as possible: the creator
    being cut goes first, the rest in the order their first cut was put in. Everything else keeps its place."""
    running = [_creator(row) for row in items if row["kind"] == "edit" and row["status"] == "running"]
    rank = {}
    for name in running + [_creator(row) for row in items if row["kind"] == "edit"]:
        rank.setdefault(name, len(rank))
    cuts = iter(sorted((row for row in items if row["kind"] == "edit"), key=lambda row: rank[_creator(row)]))
    return [next(cuts) if row["kind"] == "edit" else row for row in items]


class AwayMode:
    def __init__(self, app, root, *, clock=None, start_thread=True):
        self.app = app
        self.path = Path(root) / "away-mode.json"
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.lock = threading.RLock()
        self._backup = None  # {"item": id, "thread": Thread, "result": ..., "error": ...}
        self._edit_starts = {}  # item id -> {"thread", "error", "edit_id"} while the build and edit are being queued
        self._voices = {}  # item id -> {"thread", "error", "result"} while a pool order is being voiced
        self.pool_path = Path(root) / "away-pool.json"
        self.library = MaterialLibrary(app, root)  # each creator's material timelines, listed for ticking in the dialog
        self._wake = threading.Event()
        self._closed = threading.Event()
        with self.lock:
            data = self._load()
            session = data.get("current")
            if session and session.get("status") == "running":
                changed = False
                for item in session["items"]:
                    # Writing tasks and an Apify run survive a restart on their own; the in-process workers do not.
                    if item["status"] == "running" and item["kind"] == "edit" and item["refs"].get("edit_id"):
                        continue  # the edit itself survives a restart (or is marked interrupted); keep following it
                    if item["status"] == "running" and item["kind"] not in {"writing", "scrape"}:
                        if item["kind"] == "voice":
                            item["attempts"] = MAX_ATTEMPTS  # it may already be billed: the user looks at the order instead
                        self._failed(item, "工作台重启，这件活中断了。" + ("配音可能已经扣费，去制作单看一眼。" if item["kind"] == "voice" else ""))
                        changed = True
                if changed:
                    self._save(data)
        self.thread = None
        if start_thread:
            self.thread = threading.Thread(target=self._loop, daemon=True, name="away-mode")
            self.thread.start()

    # ---------- storage ----------
    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("current", None)
                data.setdefault("history", [])
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "current": None, "history": []}

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def close(self):
        self._closed.set()
        self._wake.set()

    # ---------- planning ----------
    def _product(self, value):
        product = product_key(value)
        if not product or product.lower() == "all":
            raise AwayError(400, "先选一个具体产品。")
        return product

    def _catalog(self, product):
        from review_opportunities import opportunity_catalog
        return [item for item in opportunity_catalog(self.app, product, limit=100).get("items", [])
                if item.get("topic") != "none" and item.get("id") != "topic:unclassified"]

    def _topics(self, product):
        """Themes ready to write the way the 选题机会 page does: a current three-part skeleton, not written in RECENT_DAYS.
        Ranked as the page ranks them; the first DEFAULT_TOPICS are ticked."""
        since = _utc(self.clock) - timedelta(days=RECENT_DAYS)
        recent = [task for task in self.app.ai.list_tasks(product=product)
                  if task.get("action") == "variants" and (_parse(task.get("created_at")) or since) > since]
        found = []
        for item in self._catalog(product):
            skeleton = item.get("skeleton") or {}
            if skeleton.get("stale") or not skeleton.get("generated_at") or skeleton.get("state") in {None, "none"}:
                continue
            title = str(item.get("title") or item["id"])[:60]
            if any(f"「{title}」" in str(task.get("instruction") or "") for task in recent):
                continue
            found.append({"id": item["id"], "title": title, "reviews": item.get("distinct_reviews"),
                          "reason": "；".join(item.get("priority_reasons") or [])[:120]})
        return found[:MAX_TOPICS]

    def _plans(self, product):
        """待写计划 still open for this product (加码 / 换执行 and the like; 停测 plans are not for writing)."""
        rows = self.app.creation.public(product, include_frozen_source=False).get("plans") or []
        return [{"id": row["id"], "direction_id": row.get("direction_id"), "note": str(row.get("note") or "")[:120],
                 "created_at": str(row.get("created_at") or "")[:10]}
                for row in rows if row.get("status") in {"pending", "working"} and row.get("direction_id") and row.get("action") != "stop"
                and not (row.get("experiment") or {}).get("decision") == "stop"]

    def _themes(self, product):
        found = []
        for item in self._catalog(product):
            skeleton = item.get("skeleton") or {}
            if skeleton.get("status") in {"queued", "running"}:
                continue
            if skeleton.get("stale") or not skeleton.get("generated_at"):
                found.append({"id": item["id"], "title": str(item.get("title") or item.get("label") or item["id"])[:60],
                              "reason": skeleton.get("stale_reason") or ("上次生成失败" if skeleton.get("status") == "failed" else "还没有骨架")})
        return found[:MAX_THEMES]

    def plan(self, product):
        """What could run now for this product, with what is ticked by default. Reads only; starts nothing."""
        product = self._product(product)
        items = []
        try:
            scrape = self.app.review_scraper.overview(product)
            platforms = [row for row in scrape["platforms"] if row.get("targets")]
            if not scrape.get("token"):
                items.append(self._suggest("scrape", False, "还没有 Apify 令牌，在设置里填入后才能抓", available=False))
            elif not platforms:
                items.append(self._suggest("scrape", False, "这个产品还没有抓取目标，先在评论页填一次", available=False))
            else:
                names = "、".join(row["label"] for row in platforms)
                items.append(self._suggest("scrape", True, f"{names}，从 {scrape['recommended_start']} 抓到今天；Apify 按实际条数收费"))
        except Exception as exc:
            items.append(self._suggest("scrape", False, f"读不到抓取设置：{_short(exc, 80)}", available=False))
        try:
            remaining = self.app.review_translations.status().get("remaining", 0)
        except Exception:
            remaining = 0
        items.append(self._suggest("translate", True, f"还有 {remaining} 条没翻译；抓到的新评论也会一起翻" if remaining
                                   else "抓到新评论后自动翻译"))
        try:
            themes = self._themes(product)
        except Exception as exc:
            themes = []
            items.append(self._suggest("skeletons", False, f"读不到选题机会：{_short(exc, 80)}", available=False))
        else:
            if themes:
                row = self._suggest("skeletons", True, f"{len(themes)} 个主题需要重新排三段骨架")
                row["themes"] = themes
                items.append(row)
            else:
                items.append(self._suggest("skeletons", False, "所有主题的骨架都是最新的", available=False))
        try:
            topics, plans = self._topics(product), self._plans(product)
        except Exception as exc:
            topics, plans = [], []
            note = f"读不到选题机会或待写计划：{_short(exc, 80)}"
        else:
            note = ("和在选题机会里点「写稿」一样：带上主题的评论和三段骨架，每个主题写一轮候选稿（含独立复核）；待写计划按计划写。只出候选，不采用"
                    if topics or plans else f"没有可写的主题（骨架齐全、近 {RECENT_DAYS} 天没写过的），也没有待写计划")
        row = self._suggest("writing", bool(topics or plans), note, available=bool(topics or plans))
        row["topics"] = [{**topic, "default": index < DEFAULT_TOPICS} for index, topic in enumerate(topics)]
        row["plans"] = plans
        items.append(row)
        pool = self._pool_plan(product)  # what gets cut is decided in the pool only; voiced orders are put there too
        # 完整备份 is no longer offered (fly, 11-20): the daily automatic backup covers it
        items.append(self._suggest("check", True, "之前失败、需要你出手的事", fixed=True))
        return {"product": product, "items": items, "pool": pool, "minutes": list(MINUTES), "default_minutes": DEFAULT_MINUTES,
                "ai_limit": DEFAULT_AI_LIMIT, "ai_parallel": AI_PARALLEL, "stop_before_minutes": int(STOP_BEFORE.total_seconds() // 60),
                "never": "不会做：执行池以外的配音、采用 / 定稿、确认剪辑完成、挑评论、发布、上传新内容、任何删除", "current": self.current()}

    @staticmethod
    def _suggest(kind, enabled, detail, *, available=True, fixed=False):
        spec = KINDS[kind]
        return {"kind": kind, "label": spec["label"], "lane": spec["lane"], "enabled": bool(enabled and available),
                "available": available, "fixed": fixed, "detail": detail}

    # ---------- starting / stopping ----------
    def start(self, body):
        if not isinstance(body, dict):
            raise AwayError(400, "请求格式不对。")
        product = self._product(body.get("product"))
        try:
            minutes = int(body.get("minutes") or DEFAULT_MINUTES)
            ai_limit = int(body.get("ai_limit") or DEFAULT_AI_LIMIT)
        except (TypeError, ValueError):
            raise AwayError(400, "时长和 AI 上限要填数字。") from None
        if not 20 <= minutes <= 8 * 60:
            raise AwayError(400, "时长要在 20 分钟到 8 小时之间。")
        if not 1 <= ai_limit <= MAX_AI_LIMIT:
            raise AwayError(400, f"AI 上限要在 1 到 {MAX_AI_LIMIT} 之间。")
        chosen = body.get("items")
        if not isinstance(chosen, list):
            raise AwayError(400, "选一下这次要做的活。")
        with self.lock:
            data = self._load()
            if data.get("current") and data["current"].get("status") == "running":
                raise AwayError(409, "值守已经在进行，先停下再重新开始。")
        planned = self.plan(product)
        plan = {row["kind"]: row for row in planned["items"]}
        self._pool_offer = {row["id"]: row for row in planned["pool"]["jobs"]}
        items = []
        for raw in chosen:
            if isinstance(raw, dict) and raw.get("kind") == "pool":
                items += self._pool_items(product, raw.get("jobs"), creators=planned["pool"].get("creators", []))
                continue
            if not isinstance(raw, dict) or raw.get("kind") not in KINDS or raw["kind"] in {"check", "voice", "edit", "tags", "backup"}:
                continue
            kind, offered = raw["kind"], plan.get(raw["kind"])
            if not offered or not offered.get("available") or any(row["kind"] == kind for row in items if kind != "writing"):
                continue
            if kind == "writing":
                topics = {row["id"]: row for row in offered.get("topics", [])}
                plans = {row["id"]: row for row in offered.get("plans", [])}
                for value in [value for value in dict.fromkeys(raw.get("topics") or []) if value in topics][:MAX_TOPICS]:
                    items.append(self._item("writing", f"写稿 · 主题「{topics[value]['title']}」",
                                            {"source": "topic", "topic": value, "title": topics[value]["title"]}))
                for value in [value for value in dict.fromkeys(raw.get("plans") or []) if value in plans][:MAX_TOPICS]:
                    plan_row = plans[value]
                    items.append(self._item("writing", f"写稿 · 待写计划 {plan_row['direction_id']}",
                                            {"source": "plan", "plan_id": value, "direction_id": plan_row["direction_id"], "note": plan_row["note"]}))
            elif kind == "skeletons":
                known = {row["id"]: row for row in offered.get("themes", [])}
                wanted = raw.get("themes")
                themes = [value for value in dict.fromkeys(wanted) if value in known] if isinstance(wanted, list) else list(known)
                if themes:
                    items.append(self._item("skeletons", f"选题骨架 · {len(themes)} 个主题", {"themes": themes},
                                            cost=len(themes), minutes=len(themes) * KINDS["skeletons"]["minutes"] + 1))
            else:
                items.append(self._item(kind, KINDS[kind]["label"], {}))
        if not items:
            raise AwayError(400, "至少勾一件要做的活。")
        items.sort(key=lambda item: ORDER.index(item["kind"]))
        cost = sum(item["cost"] for item in items)
        if cost > ai_limit:
            raise AwayError(400, f"这次要用 {cost} 次 AI，超过上限 {ai_limit}：少勾几个方向或主题，或调高上限。")
        items.append(self._item("check", KINDS["check"]["label"], {}))
        now = _utc(self.clock)
        session = {"id": now.strftime("%Y%m%d-%H%M%S"), "product": product, "status": "running", "minutes": minutes,
                   "ai_limit": ai_limit, "ai_cost": cost, "started_at": _iso(now), "return_at": _iso(now + timedelta(minutes=minutes)),
                   "finished_at": None, "stopped": False, "seen": False, "items": items}
        with self.lock:
            data = self._load()
            if data.get("current") and data["current"].get("status") == "running":
                raise AwayError(409, "值守已经在进行，先停下再重新开始。")
            if data.get("current"):
                data["history"] = ([data["current"]] + data["history"])[:HISTORY]
            data["current"] = session
            self._save(data)
        self._wake.set()
        return self.current()

    @staticmethod
    def _item(kind, label, params, *, cost=None, minutes=None):
        spec = KINDS[kind]
        return {"id": uuid.uuid4().hex[:10], "kind": kind, "lane": spec["lane"], "label": label, "params": params,
                "cost": (1 if spec.get("ai") else 0) if cost is None else cost,
                "minutes": spec["minutes"] if minutes is None else minutes,
                "status": "pending", "attempts": 0, "started_at": None, "finished_at": None, "error": None,
                "result": {}, "refs": {}}

    def stop(self, body=None):
        with self.lock:
            data = self._load()
            session = data.get("current")
            if not session or session.get("status") != "running":
                raise AwayError(409, "现在没有在值守。")
            session["stopped"] = True
            for item in session["items"]:
                if item["status"] == "pending" and item["kind"] != "check":
                    item.update(status="cancelled", error="你让它停下了。", finished_at=_iso(_utc(self.clock)))
            self._save(data)
        self._wake.set()
        return self.current()

    def seen(self, body):
        wanted = (body or {}).get("id")
        with self.lock:
            data = self._load()
            session = data.get("current")
            if not session or session.get("id") != wanted:
                raise AwayError(404, "没有这次值守的记录。")
            if session.get("status") == "running":
                raise AwayError(409, "值守还没结束。")
            session["seen"] = True
            self._save(data)
        return self.current()

    # ---------- reading ----------
    def current(self):
        with self.lock:
            session = copy.deepcopy(self._load().get("current"))
        if not session:
            return None
        session["report"] = self.report(session)
        for item in session["items"]:
            item.pop("refs", None)
        return session

    def report(self, session):
        """Three groups for the card on the home page: done, waiting for the user, not done."""
        done, waiting, failed = [], [], []
        product = session["product"]
        for item in session["items"]:
            kind, result, status = item["kind"], item.get("result") or {}, item["status"]
            if status == "failed":
                link = self._link(item, product)
                failed.append({"text": f"{item['label']}：{item.get('error') or '没做成'}", "link": link and {**link, "label": "去看"}})
                continue
            if status in {"skipped", "cancelled"}:
                failed.append({"text": f"{item['label']}：{item.get('error') or '没开始'}", "link": None, "skipped": True})
                continue
            if status != "done":
                continue
            if kind == "writing":
                waiting.append({"text": f"{item['label']}：候选稿写好了，挑采用稿", "link": self._link(item, product)})
            elif kind == "skeletons":
                count = result.get("done", 0)
                done.append({"text": f"选题骨架更新了 {count} 个主题" + (f"，{result['failed']} 个没成" if result.get("failed") else ""),
                             "link": self._link(item, product)})
            elif kind == "scrape":
                done.append({"text": f"抓评论完成，评论库新增 {result.get('added') or 0} 条" +
                             (f"，Apify 花费 ${result['cost_usd']}" if result.get("cost_usd") else "") +
                             (f"（{result['note']}）" if result.get("note") else ""), "link": self._link(item, product)})
            elif kind == "translate":
                done.append({"text": f"评论翻译完成 {result.get('done', 0)} 条" + (f"，{result['failed']} 条没翻成" if result.get("failed") else ""),
                             "link": self._link(item, product)})
            elif kind == "tags":
                suggested = result.get("suggested", 0)
                (waiting if suggested else done).append(
                    {"text": f"AI 给 {suggested} 条评论补了标签建议，到「逐条确认」里接受" if suggested else "AI 补标签：这次没有新建议",
                     "link": self._link(item, product)})
            elif kind == "voice":
                done.append({"text": f"{item['label']}：配好了一条，已选用" + (f"（{result['duration']:.1f} 秒）" if isinstance(result.get("duration"), (int, float)) else ""),
                             "link": self._link(item, product)})
            elif kind == "edit":
                outcome = EDIT_OUTCOME.get(result.get("status"))
                waiting.append({"text": f"{item['label']}：" + (outcome or "剪完了，看一遍再确认完成") +
                                (f"（用的是已建好的时间线 {result['reused_timeline']}）" if result.get("reused_timeline") else ""),
                                "link": self._link(item, product)})
            elif kind == "backup":
                done.append({"text": "完整备份已存好" + (f"：{result['path']}" if result.get("path") else ""), "link": None})
            elif kind == "check":
                for row in result.get("waiting", []):
                    waiting.append(row)
        return {"done": done, "waiting": waiting, "failed": failed,
                "counts": {"done": len(done), "waiting": len(waiting), "failed": len(failed),
                           "skipped": len([row for row in failed if row.get("skipped")])}}

    @staticmethod
    def _link(item, product):
        kind = item["kind"]
        if kind == "writing" and (item.get("result") or {}).get("task_id"):
            return {"hash": f"#ai/{item['result']['task_id']}", "label": "去挑"}
        if kind == "skeletons":
            return {"hash": "#reviews?tab=opportunities", "label": "看看"}
        if kind == "edit":
            return {"hash": f"#production/{item['params']['job_id']}?step=cut", "label": "去看"}
        if kind == "voice":
            return {"hash": f"#production/{item['params']['job_id']}", "label": "去听"}
        if kind in {"scrape", "translate", "tags"}:
            return {"hash": "#reviews", "label": "看看"}
        return None

    # ---------- scheduling ----------
    def _loop(self):
        while not self._closed.is_set():
            try:
                self.tick()
            except Exception:  # never let one bad poll end the scheduler
                pass
            self._wake.wait(TICK_SECONDS)
            self._wake.clear()

    def tick(self):
        with self.lock:
            data = self._load()
            session = data.get("current")
            if not session or session.get("status") != "running":
                return
            now = _utc(self.clock)
            for item in session["items"]:
                if item["status"] == "running":
                    self._poll(session, item, now)
            cutoff = _parse(session["return_at"]) - STOP_BEFORE
            items = session["items"]
            for item in _creator_order(items):
                if item["status"] != "pending" or KINDS[item["kind"]].get("final"):
                    continue
                before = next((row for row in items if row["id"] == item.get("after")), None) if item.get("after") else None
                if before and before["status"] in TERMINAL and before["status"] != "done":
                    item.update(status="skipped", error="配音没成，没剪。", finished_at=_iso(now))
                    continue
                if now >= cutoff:
                    item.update(status="skipped", error="快到你回来的时间了，没开始。", finished_at=_iso(now))
                    continue
                if not self._ready(item, items):
                    continue
                if now + timedelta(minutes=item["minutes"]) > _parse(session["return_at"]):
                    item.update(status="skipped", error="剩下的时间不够跑完它。", finished_at=_iso(now))
                    continue
                self._launch(session, item, now)
            self._pool_return(session, now)
            others = [item for item in items if not KINDS[item["kind"]].get("final")]
            if all(item["status"] in TERMINAL for item in others):
                for item in items:
                    if KINDS[item["kind"]].get("final") and item["status"] not in TERMINAL:
                        item.update(status="running", started_at=_iso(now), attempts=item["attempts"] + 1)
                        try:
                            item["result"] = self._check(session["product"])
                            item.update(status="done", finished_at=_iso(_utc(self.clock)))
                        except Exception as exc:
                            self._failed(item, f"检查没做完：{_short(exc)}")
                session.update(status="done", finished_at=_iso(_utc(self.clock)))
            self._save(data)

    def _ready(self, item, items):
        kind = item["kind"]
        busy = [row for row in items if row["status"] == "running"]
        if KINDS[kind].get("exclusive"):
            # The backup needs every other job finished and the AI idle; it also holds saving while it runs.
            if any(row["status"] not in TERMINAL for row in items if row is not item and not KINDS[row["kind"]].get("final")):
                return False
            try:
                self.app.ensure_data_idle()
            except Exception:
                return False
            return True
        if any(KINDS[row["kind"]].get("exclusive") for row in busy):
            return False
        for before in AFTER.get(kind, ()):
            if any(row["kind"] == before and row["status"] not in TERMINAL for row in items):
                return False
        if item.get("after") and any(row["id"] == item["after"] and row["status"] != "done" for row in items):
            return False  # a pool order is cut only after its own voiceover is chosen
        lane = KINDS[kind]["lane"]
        running_in_lane = sum(1 for row in busy if row["lane"] == lane)
        if lane == "ai":
            return running_in_lane < AI_PARALLEL
        if lane == "voice":
            return running_in_lane < VOICE_PARALLEL
        if lane == "resolve" and running_in_lane:
            mine = _creator(item)
            return bool(mine) and running_in_lane < RESOLVE_PARALLEL and all(
                _creator(row) == mine for row in busy if row["lane"] == lane)
        return running_in_lane == 0

    def _launch(self, session, item, now):
        item.update(status="running", started_at=_iso(now), error=None, attempts=item["attempts"] + 1)
        try:
            getattr(self, "_start_" + item["kind"])(session, item)
        except Exception as exc:
            self._failed(item, _short(exc))

    def _failed(self, item, message):
        """A first failure goes back to the queue once; a second one is final."""
        now = _iso(_utc(self.clock))
        if item["attempts"] < MAX_ATTEMPTS:
            item.update(status="pending", error=f"第 {item['attempts']} 次没成，稍后重试：{message}")
        else:
            item.update(status="failed", error=message, finished_at=now)

    def _done(self, item, result):
        item.update(status="done", result=result, finished_at=_iso(_utc(self.clock)), error=None)

    def _poll(self, session, item, now):
        try:
            getattr(self, "_poll_" + item["kind"])(session, item)
        except Exception as exc:
            self._failed(item, _short(exc))

    # ---------- each kind ----------
    def _start_scrape(self, session, item):
        product = session["product"]
        overview = self.app.review_scraper.overview(product)
        if overview.get("busy") and overview.get("job"):
            item["refs"]["job"] = overview["job"]["id"]  # one already running: follow it instead of starting another
            return
        platforms = {row["platform"]: {"targets": row["targets"]} for row in overview["platforms"] if row.get("targets")}
        if not platforms:
            item.update(status="skipped", error="没有抓取目标。", finished_at=_iso(_utc(self.clock)))
            return
        result = self.app.review_scraper.start({"product": product, "start": overview["recommended_start"],
                                                "end": date.today().isoformat(), "platforms": platforms})
        item["refs"]["job"] = (result.get("job") or {}).get("id")

    def _poll_scrape(self, session, item):
        overview = self.app.review_scraper.overview(session["product"])
        job = overview.get("job") or {}
        if job.get("id") != item["refs"].get("job"):
            raise RuntimeError("找不到这次抓取的记录。")
        if job.get("status") == "running":
            return
        rows = list((job.get("platforms") or {}).values())
        failed = [row for row in rows if row.get("status") == "failed"]
        if rows and len(failed) == len(rows):
            raise RuntimeError(failed[0].get("error") or "Apify 没抓成。")
        cost = round(sum(float(row.get("cost_usd") or 0) for row in rows), 2)
        note = "；".join(filter(None, [f"{len(failed)} 个平台没成" if failed else "", job.get("sync_error") or ""]))
        self._done(item, {"added": job.get("added") or 0, "cost_usd": cost or None, "note": note or None})

    def _start_translate(self, session, item):
        worker = self.app.review_translations
        if worker.busy():
            item["refs"]["attached"] = True  # the scrape already started translating its comments
            return
        try:
            worker.start({})
        except Exception as exc:
            if getattr(exc, "status", None) == 400:  # nothing left to translate
                self._done(item, {"done": 0, "total": 0})
                return
            raise

    def _poll_translate(self, session, item):
        worker = self.app.review_translations
        if worker.busy():
            return
        status = worker.status()
        if status.get("remaining") and not item["refs"].get("rerun"):
            item["refs"]["rerun"] = True  # a run that ended early (or was the scrape's): one more pass for what is left
            try:
                worker.start({})
                return
            except Exception:
                pass
        run = status.get("run") or {}
        if run.get("status") == "failed" and not run.get("done"):
            raise RuntimeError(run.get("error") or "翻译没成。")
        self._done(item, {"done": run.get("done", 0), "total": run.get("total", 0), "failed": run.get("failed", 0)})

    def _start_tags(self, session, item):
        worker = self.app.review_tags
        if worker.busy():
            item["refs"]["attached"] = True
            return
        try:
            worker.start({})
        except Exception as exc:
            if getattr(exc, "status", None) == 400:
                self._done(item, {"suggested": 0})
                return
            raise

    def _poll_tags(self, session, item):
        worker = self.app.review_tags
        if worker.busy():
            return
        run = worker.status().get("run") or {}
        if run.get("status") == "failed":
            raise RuntimeError(run.get("error") or "补标签没成。")
        self._done(item, {"suggested": run.get("suggested", 0), "done": run.get("done", 0)})

    def _start_skeletons(self, session, item):
        themes = item["refs"].get("retry") or item["params"]["themes"]
        self.app.opportunity_skeletons.start({"product": session["product"], "themes": themes})
        item["refs"]["themes"] = themes

    def _poll_skeletons(self, session, item):
        states = self.app.opportunity_skeletons.states(session["product"])["themes"]
        themes = item["refs"].get("themes") or item["params"]["themes"]
        rows = [states.get(theme) or {} for theme in themes]
        if any(row.get("status") in {"queued", "running"} for row in rows):
            return
        bad = [theme for theme, row in zip(themes, rows) if row.get("status") != "done"]
        earlier = item["result"].get("done", 0) if item["refs"].get("retry") else 0
        ok = earlier + len(themes) - len(bad)
        item["result"] = {"done": ok, "failed": len(bad)}
        if bad and item["attempts"] < MAX_ATTEMPTS:
            item["refs"]["retry"] = bad
            self._failed(item, f"{len(bad)} 个主题没生成成")
            return
        if bad and not ok:
            item.update(status="failed", error="骨架都没生成成。", finished_at=_iso(_utc(self.clock)))
            return
        self._done(item, {"done": ok, "failed": len(bad)})

    def _start_writing(self, session, item):
        params, product = item["params"], session["product"]
        body = {"request_id": f"away-{session['id']}-{item['id']}-{item['attempts']}", "product": product,
                "action": "variants", "include_basket": False}
        if params.get("source") == "plan":
            body.update(plan_id=params["plan_id"], direction_id=params["direction_id"],
                        instruction=f"按待写计划写稿。{params.get('note') or ''}".strip())
        else:
            # Read the theme again now: its evidence and skeleton brief are what the 选题机会 page would hand over today.
            found = next((row for row in self._catalog(product) if row["id"] == params["topic"]), None)
            if not found or not found.get("draft_instruction"):
                raise RuntimeError("这个主题已不在选题机会里。")
            body["instruction"] = found["draft_instruction"]
        task = self.app.create_ai_task(body)
        item["refs"]["task_id"] = task["task_id"]
        item["result"] = {"task_id": task["task_id"]}

    def _poll_writing(self, session, item):
        task = self.app.ai.get(item["refs"]["task_id"])
        if task.get("busy") or task.get("status") in AI_ACTIVE:
            return
        if task.get("status") == "completed":
            self._done(item, {"task_id": task["task_id"]})
            return
        rounds = task.get("rounds") or []
        reason = task.get("last_error") or (rounds[-1].get("error") if rounds else None) or "写稿没完成。"
        raise RuntimeError(str(reason)[:200])

    # ---------- execution pool ----------
    def _pool_load(self):
        try:
            data = json.loads(self.pool_path.read_text(encoding="utf-8"))
            entries = data.get("entries") if isinstance(data, dict) else None
            return [row for row in entries if isinstance(row, dict) and isinstance(row.get("job_id"), str)] if isinstance(entries, list) else []
        except (OSError, ValueError):
            return []

    def _pool_save(self, entries):
        temporary = self.pool_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"entries": entries}, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temporary, self.pool_path)

    @staticmethod
    def _voice_config(value):
        if not isinstance(value, dict):
            raise AwayError(400, "配音设置不对，回配音区重新放一次。")
        voice_id, model_id, market = value.get("voice_id"), value.get("model_id"), value.get("market") or "us"
        settings = value.get("settings") if isinstance(value.get("settings"), dict) else {}
        if not isinstance(voice_id, str) or not voice_id.isalnum() or not 10 <= len(voice_id) <= 64:
            raise AwayError(400, "先选一个音色（新音色要先生成过一次存进音色库）。")
        if not isinstance(model_id, str) or not model_id or len(model_id) > 80 or not isinstance(market, str) or len(market) > 20:
            raise AwayError(400, "配音模型或语言不对。")
        try:
            clean = clean_settings(model_id, {key: value for key, value in settings.items() if key in model(model_id)["settings"]})
        except Exception as exc:
            raise AwayError(400, str(exc)[:200] or "配音设置不对。") from None
        return {"voice_id": voice_id, "model_id": model_id, "settings": clean, "market": market}

    def _default_voice(self, job):
        """What an order put in the pool is voiced with until the user changes it there: the product's default voice
        (else the first in the library) with that voice's own model and settings, or this order's last take."""
        data = self.app.production.records()
        voices = [row for row in data.get("voices") or [] if isinstance(row, dict) and row.get("voice_id")]
        takes = job.get("takes") or []
        last = takes[-1] if takes else None
        if last and last.get("voice_id"):
            return self._voice_config({"voice_id": last["voice_id"], "model_id": last.get("model_id") or DEFAULT_MODEL,
                                       "settings": last.get("settings") or {}, "market": last.get("market") or "us"})
        wanted = (data.get("defaults") or {}).get(job.get("product"))
        voice = next((row for row in voices if row["voice_id"] == wanted), voices[0] if voices else None)
        if not voice:
            raise AwayError(409, "音色库是空的，先在制作单里生成一次配音。")
        settings = voice.get("settings") if isinstance(voice.get("settings"), dict) else {}
        return self._voice_config({"voice_id": voice["voice_id"], "model_id": voice.get("model_id") or DEFAULT_MODEL,
                                   "settings": settings, "market": "us"})

    def _script_sha(self, job):
        """Fingerprint of the text a new take would read now, as the workbench stores it on each take."""
        try:
            tts = self.app._tts(job, self.app.production.readings(job.get("product")))
            return hashlib.sha256(tts["text"].encode()).hexdigest()
        except Exception:
            return None

    def _voiced(self, job, entry=None):
        """The voiceover a pool order is cut with without voicing again; None when it needs one (or the user asked
        for a new one in the pool)."""
        if (entry or {}).get("revoice"):
            return None
        return ready_take(job, self._script_sha(job))

    def pool(self, product=None):
        entries = self._pool_load()
        return [row for row in entries if not product or product_key(row.get("product")) == product_key(product)]

    def pool_change(self, body):
        """add (from an order's voice form), update (creator, materials) or remove one order of the execution pool."""
        if not isinstance(body, dict) or body.get("op") not in {"add", "update", "remove"} or not isinstance(body.get("job_id"), str):
            raise AwayError(400, "执行池请求格式不对。")
        op, job_id = body["op"], body["job_id"]
        with self.lock:
            entries = self._pool_load()
            found = next((row for row in entries if row["job_id"] == job_id), None)
            if op == "remove":
                entries = [row for row in entries if row["job_id"] != job_id]
            elif op == "add":
                try:
                    job = self.app.production.job(job_id)
                except Exception:
                    raise AwayError(404, "这张制作单不存在。") from None
                if job.get("archived"):
                    raise AwayError(409, "制作单已归档。")
                if found is None and len(self.pool(job.get("product"))) >= MAX_POOL:
                    raise AwayError(409, f"执行池最多放 {MAX_POOL} 张单。")
                row = found or {"job_id": job_id, "added_at": _iso(_utc(self.clock)), "creator": "", "materials": []}
                if "voice" in body and body["voice"] is not None:
                    voice = self._voice_config(body["voice"])
                elif found is not None and "voice" not in body:
                    voice = found.get("voice")  # added again: keep what was set in the pool
                elif job.get("selected_take") or self._voiced(job):
                    voice = None  # already voiced: cut with that voiceover; switch to a new voiceover in the pool
                else:
                    voice = self._default_voice(job)
                row.update(product=job.get("product"), voice=voice)
                if voice is not None and "voice" in body:
                    row["revoice"] = bool(self._voiced(job))  # a voice picked by hand for a voiced order: voice it again
                if found is None:
                    if voice is None:  # picked for this order before: no need to pick it again
                        creator, materials = own_pick(job)
                        if materials:
                            row.update(creator=creator, materials=materials, ready=True)
                    entries.append(row)
            else:
                if not found:
                    raise AwayError(404, "这张单不在执行池里。")
                if isinstance(body.get("creator"), str):
                    found["creator"] = body["creator"].strip()[:40]
                if isinstance(body.get("materials"), list):
                    found["materials"] = [str(name).strip() for name in body["materials"] if str(name).strip()][:30]
                if "voice" in body:
                    try:
                        job = self.app.production.job(job_id)
                        voiced = bool(job.get("selected_take") or self._voiced(job))
                    except Exception:
                        voiced = False
                    if body["voice"] is None:
                        if not voiced:
                            raise AwayError(409, "这张单还没选用配音，要选一个音色来配。")
                        found["voice"], found["revoice"] = None, False
                    else:
                        found["voice"] = self._voice_config(body["voice"])
                        # the pool shows a voiced order as 「已配音，直接剪」: a voice sent for it was picked by hand
                        found["revoice"] = voiced
                if isinstance(body.get("ready"), bool):
                    if body["ready"] and not found.get("materials"):
                        raise AwayError(409, "先勾这张单要用的素材时间线，再标就绪。")
                    found["ready"] = body["ready"]  # confirmed by the user: ticked to run by default
            self._pool_save(entries)
        return {"pool": self.pool()}

    def _pool_plan(self, product):
        """The pool's orders for this product, each with what it will be voiced with and cut from."""
        entries = self.pool(product)
        try:
            data = self.app.production.records()
        except Exception as exc:
            return {"jobs": [], "note": f"读不到制作单：{_short(exc, 80)}"}
        jobs = {job["id"]: job for job in data["jobs"]}
        creators = list((data.get("settings") or {}).get("creators") or [])
        voices = {row.get("voice_id"): row for row in data.get("voices") or [] if isinstance(row, dict)}
        listing = self.library.listing() if entries else None
        rows = []
        for entry in entries:
            job = jobs.get(entry["job_id"])
            problem = "制作单不在了" if not job else "制作单已归档" if job.get("archived") else "正在配音" if job.get("id") in getattr(self.app, "_voicing", lambda: set())() else ""
            take = self._voiced(job, entry) if job and not problem else None
            # done already: not voiced again, whatever voice the pool still holds for it
            voice = {} if take else entry.get("voice") or {}
            name = (voices.get(voice.get("voice_id")) or {}).get("name") or (voices.get(voice.get("voice_id")) or {}).get("note") or voice.get("voice_id", "")
            if not problem and not voice and not take and job and not job.get("selected_take"):
                problem = "选用的配音没了"
            row = {"id": entry["job_id"], "title": str((job or {}).get("title") or entry["job_id"])[:40], "creator": entry.get("creator") or "",
                   "materials": list(entry.get("materials") or []), "pool_materials": bool(entry.get("materials")),
                   "voice": (f"{name} · {voice.get('model_id', '')}" + (f" · {voice['market']}" if voice.get("market") not in (None, "us") else ""))
                            if voice else "已配音，直接剪", "voiced": not voice,
                   "voice_config": dict(voice) if voice else None, "can_direct": bool(job and (job.get("selected_take") or take)),
                   "take_note": ("已有一条配好、对齐过的配音还没选用，开剪前自动选用它，不再配" if take and take.get("id") != job.get("selected_take")
                                 else "已配好音，不再配" if take and entry.get("voice") else ""),
                   "mode": "replica" if job and job.get("replica") and needs_table(job) else "explain",
                   "problem": problem, "ready": bool(entry.get("ready")) and not problem, "default": not problem and bool(entry.get("ready")),
                   # the voiceover lines with their storyboard notes: what tells which creator's footage fits
                   "script": script_lines(job) if job else []}
            if job and not problem:
                if not row["creator"]:
                    timelines = job.get("timelines") or []
                    row["creator"] = (timelines[-1].get("creator") if timelines else "") or job.get("creator") or (creators[0] if creators else "")
                # every creator's library, so switching the creator lists theirs; their latest cut is what starts ticked
                row["latest"] = {name: latest_materials(data, job, name) for name in creators}
                row["libraries"] = {}
                for name in creators:
                    try:
                        row["libraries"][name] = self.library.library(data, job, name, listing)
                    except Exception:
                        row["libraries"][name] = {"names": [], "seen_at": ""}
                # a timeline built for this voiceover and not cut yet: cut on it rather than build another
                take_id = (take or {}).get("id") or (job.get("selected_take") if not voice else None)
                row["built"] = {name: built["name"] for name in creators
                                if (built := built_timeline(job, take_id, name)) and built.get("name")}
                own_creator, own = own_pick(job)
                if own_creator and not entry.get("creator") and own:
                    row["creator"] = own_creator
                if not row["materials"]:  # this order's own last pick first: it was chosen for this script
                    row["materials"] = list(own if own and own_creator in ("", row["creator"]) else row["latest"].get(row["creator"]) or [])
            rows.append(row)
        pooled = {row["id"] for row in rows}
        waiting = [job for job in data["jobs"] if not job.get("archived") and product_key(job.get("product")) == product
                   and job.get("selected_take") and not job.get("edits") and job["id"] not in pooled]
        options = {"voices": [{"voice_id": row["voice_id"], "label": str(row.get("name") or row.get("note") or row["voice_id"])[:40],
                               "model_id": row.get("model_id"), "settings": row.get("settings") if isinstance(row.get("settings"), dict) else {}}
                              for row in data.get("voices") or [] if isinstance(row, dict) and row.get("voice_id")],
                   "models": [{"id": row["id"], "name": row["name"], "settings": [{"key": key, "low": SETTING_RANGES[key][0], "high": SETTING_RANGES[key][1],
                                                                                    "default": SETTING_RANGES[key][2]} for key in row["settings"]]}
                              for row in MODELS],
                   "markets": markets.public()}
        return {"jobs": rows, "creators": creators, **options,
                "waiting": [{"id": job["id"], "title": str(job.get("title") or job["id"])[:40]} for job in waiting[:6]], "waiting_count": len(waiting),
                "note": "没配音的单按定好的音色配一条，对齐过了就选用，再新建时间线 AI 剪辑，停在等你确认；配音同时最多 2 条，会花配音的钱" if rows else ""}

    def _pool_return(self, session, now):
        """A pool order that left the pool but was not cut (stopped, failed, skipped) goes back with its creator and
        footage. Voiced by now: it waits to be cut as it is, still ready. Not voiced: it keeps its voice but is not
        ready, so the next run does not pay for a voiceover again unless the user ticks it."""
        items = session["items"]
        for item in items:
            params = item.get("params") or {}
            if item["kind"] != "edit" or not params.get("pool") or item["status"] not in TERMINAL or item["status"] == "done" or item.get("returned"):
                continue
            before = next((row for row in items if row["id"] == item.get("after")), None) if item.get("after") else None
            if before and before["status"] not in TERMINAL:
                continue  # stopped while voicing: wait to see whether the voiceover was chosen
            item["returned"] = True
            try:
                job = self.app.production.job(params["job_id"])
            except Exception:
                continue
            if job.get("archived"):
                continue
            voiced = bool(job.get("selected_take"))
            voice = None if voiced else (before or {}).get("params", {}).get("voice")
            if not voiced and not voice:
                continue
            entries = self._pool_load()
            found = next((row for row in entries if row["job_id"] == params["job_id"]), None)
            if found is None:
                entries.append({"job_id": params["job_id"], "added_at": _iso(now), "product": job.get("product") or session["product"],
                                "creator": params.get("creator") or "", "materials": list(params.get("materials") or []),
                                "voice": voice, "ready": voiced, "back_from": session["id"]})
            else:  # put back in by hand meanwhile: fill in only what is still blank
                found["creator"] = found.get("creator") or params.get("creator") or ""
                found["materials"] = found.get("materials") or list(params.get("materials") or [])
            self._pool_save(entries)

    def _pool_items(self, product, wanted, *, creators):
        offered = getattr(self, "_pool_offer", {})
        entries = {row["job_id"]: row for row in self.pool(product)}
        items = []
        for raw in (wanted or [])[:MAX_POOL]:
            if not isinstance(raw, dict) or raw.get("id") not in offered or raw["id"] not in entries:
                continue
            job, entry = offered[raw["id"]], entries[raw["id"]]
            if job["problem"]:
                raise AwayError(409, f"「{job['title']}」{job['problem']}，先从池子里移出。")
            creator = str(raw.get("creator") or job["creator"] or "").strip()
            materials = [str(name).strip() for name in (raw.get("materials") or job["materials"] or []) if str(name).strip()][:30]
            if creator not in creators:
                raise AwayError(400, f"「{job['title']}」要选一个达人账号来建线。")
            if not materials:
                raise AwayError(400, f"「{job['title']}」要选至少一条素材时间线。")
            # not started unless its cut fits in too: a voiceover with no time left to cut it is money spent for nothing
            try:
                record = self.app.production.job(job["id"])
            except Exception:
                raise AwayError(409, f"「{job['title']}」读不到制作单，先从池子里移出。") from None
            take = self._voiced(record, entry)
            edit = self._item("edit", f"AI 剪辑 · {job['title']}", {"job_id": job["id"], "creator": creator, "materials": materials,
                                                                   "mode": job["mode"], "recut_from": None, "pool": True,
                                                                   # voiced but not chosen yet: chosen right before the cut
                                                                   "take_id": take["id"] if take and take.get("id") != record.get("selected_take") else None})
            if take or not entry.get("voice"):
                items.append(edit)  # voiced already: straight to DaVinci
                continue
            # not started unless its cut fits in too: a voiceover with no time left to cut it is money spent for nothing
            voice = self._item("voice", f"配音 · {job['title']}", {"job_id": job["id"], "voice": entry["voice"], "pool": True},
                               minutes=KINDS["voice"]["minutes"] + KINDS["edit"]["minutes"])
            edit["after"] = voice["id"]
            items += [voice, edit]
        return items

    def _start_voice(self, session, item):
        item["attempts"] = MAX_ATTEMPTS  # a voiceover costs money: never sent twice by the scheduler
        params = item["params"]
        with self.lock:  # it leaves the pool once it starts; the session now carries it
            self._pool_save([row for row in self._pool_load() if row["job_id"] != params["job_id"]])
        holder = {"error": None, "result": None}
        request_id = f"away-{session['id']}-{item['id']}-take"

        def run():
            try:
                job_id, voice = params["job_id"], params["voice"]
                before = {take.get("id") for take in self.app.production.job(job_id).get("takes") or []}
                body = {"request_id": request_id, "voice_id": voice["voice_id"], "model_id": voice["model_id"], "settings": voice.get("settings") or {}}
                if voice.get("market") and voice["market"] != "us":
                    body["market"] = voice["market"]
                self.app.generate_take(job_id, body)
                takes = [take for take in self.app.production.job(job_id).get("takes") or [] if take.get("id") not in before]
                take = max(takes, key=lambda row: str(row.get("created_at") or ""), default=None)
                if not take:
                    raise RuntimeError("配音完成了，但找不到新的那一版。")
                if (take.get("quality") or {}).get("status") != "passed":
                    raise RuntimeError("配了一条，但意群对齐没过核验，没选用也没剪；回来试听一下。")
                self._select(job_id, take["id"], request_id)
                holder["result"] = {"take_id": take["id"], "duration": take.get("duration")}
            except Exception as exc:
                holder["error"] = _short(exc)
            finally:
                self._wake.set()

        holder["thread"] = threading.Thread(target=run, daemon=True, name="away-voice")
        self._voices[item["id"]] = holder
        holder["thread"].start()

    def _select(self, job_id, take_id, request_id):
        for attempt in range(3):  # the selection needs the catalog revision; another save may slip in between
            try:
                data = self.app.production.records()
                self.app.select_take(job_id, {"request_id": f"{request_id}-select-{attempt}", "take_id": take_id,
                                              "revision": self.app.production.revision(data)})
                return
            except Exception:
                if attempt == 2:
                    raise

    def _poll_voice(self, session, item):
        holder = self._voices.get(item["id"])
        if not holder:
            raise RuntimeError("工作台重启，配音中断了；可能已经扣费，去制作单看一眼。")
        if holder["thread"].is_alive():
            return
        self._voices.pop(item["id"], None)
        if holder["error"]:
            raise RuntimeError(holder["error"])
        self._done(item, holder["result"] or {})

    def _start_edit(self, session, item):
        if self.app.resolve.closed():
            item["attempts"] = MAX_ATTEMPTS  # nothing to retry until someone opens DaVinci
            raise RuntimeError("达芬奇没有打开，这张单没剪。")
        params = item["params"]
        if params.get("pool") and not item.get("after"):
            with self.lock:  # a voiced pool order leaves the pool when its cut starts
                self._pool_save([row for row in self._pool_load() if row["job_id"] != params["job_id"]])
        build_id = f"away-{session['id']}-{item['id']}-build"
        holder = {"error": None, "edit_id": None, "reused": None}

        def run():
            try:
                job = self.app.production.job(params["job_id"])
                if params.get("take_id") and job.get("selected_take") != params["take_id"]:
                    self._select(params["job_id"], params["take_id"], f"away-{session['id']}-{item['id']}-take")
                    job = self.app.production.job(params["job_id"])
                built = None if params.get("recut_from") else built_timeline(job, job.get("selected_take"), params.get("creator"))
                if params.get("recut_from"):
                    # the workbench's own 「另建时间线再剪」: a twin of the last timeline, which itself stays as it is
                    target = {"recut_from": params["recut_from"]}
                elif built:  # built for this voiceover already and never cut: cut on it, build nothing
                    target = {"timeline": built["unique_id"]}
                    holder["reused"] = built.get("name") or built["unique_id"]
                else:
                    built = self.app.build_timeline(params["job_id"], {"request_id": build_id, "creator": params["creator"]})
                    target = ({"timeline_request_id": build_id} if built.get("queued")
                              else {"timeline": ((built.get("timeline") or {}).get("unique_id") or built.get("unique_id"))})
                started = self.app.start_edit(params["job_id"], {"request_id": f"away-{session['id']}-{item['id']}-edit", **target,
                                                                  "mode": params["mode"], "instruction": "", "materials": params["materials"]})
                holder["edit_id"] = (started.get("edit") or {}).get("id")
                if not holder["edit_id"]:
                    holder["error"] = "剪辑没有加进队列。"
            except Exception as exc:
                holder["error"] = _short(exc)
            finally:
                self._wake.set()

        holder["thread"] = threading.Thread(target=run, daemon=True, name="away-edit")
        self._edit_starts[item["id"]] = holder
        holder["thread"].start()

    def _poll_edit(self, session, item):
        holder = self._edit_starts.get(item["id"])
        if holder and holder["thread"].is_alive():
            return
        if holder:
            self._edit_starts.pop(item["id"], None)
            if holder["error"]:
                item["attempts"] = MAX_ATTEMPTS  # an edit is long and may already have built a timeline: never repeat it
                raise RuntimeError(holder["error"])
            item["refs"]["edit_id"] = holder["edit_id"]
            if holder.get("reused"):
                item["refs"]["reused_timeline"] = holder["reused"]
        edit_id = item["refs"].get("edit_id")
        if not edit_id:
            item["attempts"] = MAX_ATTEMPTS
            raise RuntimeError("工作台重启，剪辑还没加进队列就中断了。")
        job = self.app.production.job(item["params"]["job_id"])
        edit = next((row for row in job.get("edits") or [] if row.get("id") == edit_id), None)
        if not edit:
            item["attempts"] = MAX_ATTEMPTS
            raise RuntimeError("找不到这次剪辑的记录。")
        if edit.get("status") in EDIT_ACTIVE:
            return
        if edit.get("status") in EDIT_FAILED:
            item["attempts"] = MAX_ATTEMPTS
            raise RuntimeError(str(edit.get("error") or "AI 剪辑没完成。")[:200])
        self._done(item, {"edit_id": edit_id, "status": edit.get("status"), "timeline": edit.get("timeline_name"),
                          **({"reused_timeline": item["refs"]["reused_timeline"]} if item["refs"].get("reused_timeline") else {})})

    def _start_backup(self, session, item):
        holder = {"item": item["id"], "result": None, "error": None}

        def run():
            try:
                holder["result"] = self.app.data_transfer.backup()
            except Exception as exc:
                holder["error"] = _short(exc)
            finally:
                self._wake.set()

        holder["thread"] = threading.Thread(target=run, daemon=True, name="away-backup")
        self._backup = holder
        holder["thread"].start()

    def _poll_backup(self, session, item):
        holder = self._backup
        if not holder or holder["item"] != item["id"]:
            raise RuntimeError("工作台重启，备份中断了。")
        if holder["thread"].is_alive():
            return
        self._backup = None
        if holder["error"]:
            raise RuntimeError(holder["error"])
        result = holder["result"] or {}
        archive = result.get("backup") or {}
        self._done(item, {"path": archive.get("path"), "bytes": archive.get("bytes")})

    def _check(self, product):
        """What waits for the user, read-only: orders to voice, orders to edit, failures to look at."""
        waiting = []
        jobs = [job for job in self.app.production.records()["jobs"]
                if not job.get("archived") and product_key(job.get("product")) == product]
        try:
            stuck = self.app.needs_you(product).get("items") or []
        except Exception:
            stuck = []
        if stuck:
            waiting.append({"text": f"{len(stuck)} 件之前失败的事需要你出手（上传、导出或图文）", "link": {"hash": "#content", "label": "去看"}})
        return {"waiting": waiting}
