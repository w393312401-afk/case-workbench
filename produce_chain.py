"""「开始制作」：一张制作单点一次，接着跑完 配音 → 选用 → 建时间线 → AI 剪辑。

Every step is the workbench's own (generate_take, select_take, build_timeline, start_edit); this only strings them
together in a background thread, so the page can be closed or moved to the next order meanwhile. Nothing is ever
repeated: a voiceover costs money and an edit may already have built a timeline. The state lives in memory only;
after a restart the order simply sits at the step it reached, with its usual buttons.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone

from elevenlabs_client import DEFAULT_MODEL, VOICE_ID, VoiceError, clean_settings

REQUEST_ID = re.compile(r"[\w-]{10,80}")
STAGES = ("voice", "select", "timeline", "edit")
LABELS = {"voice": "配音", "select": "选用配音", "timeline": "建时间线", "edit": "开始剪辑"}
MODES = ("explain", "replica")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _short(exc, limit=240):
    text = str(getattr(exc, "message", "") or exc).strip() or exc.__class__.__name__
    return text[:limit]


class ProduceChain:
    def __init__(self, app, *, start_thread=True):
        self.app = app
        self.lock = threading.Lock()
        self.runs = {}
        self.start_thread = start_thread

    def status(self, job_id):
        """What the order page shows: the step under way, or where the last run stopped and why."""
        with self.lock:
            run = self.runs.get(job_id)
            return {key: run[key] for key in ("stage", "running", "error", "note", "since", "request_id")} if run else None

    def dismiss(self, job_id, body=None):
        with self.lock:
            run = self.runs.get(job_id)
            if run and not run["running"]:
                self.runs.pop(job_id)
        return {"ok": True}

    def _plan(self, job_id, body):
        if not isinstance(body, dict) or not isinstance(body.get("request_id"), str) or not REQUEST_ID.fullmatch(body["request_id"]):
            raise VoiceError(400, "请求格式不对，刷新页面后再试。")
        data = self.app.production.records()
        job = next((row for row in data.get("jobs") or [] if row.get("id") == job_id), None)
        if not job:
            raise VoiceError(404, "这张制作单不存在。")
        if job.get("archived"):
            raise VoiceError(409, "制作单已归档，恢复后再开始。")
        creators = list((data.get("settings") or {}).get("creators") or [])
        creator = body.get("creator")
        if not isinstance(creator, str) or creator not in creators:
            raise VoiceError(400, "先选一位达人（达人名在内容列表的「更多 → 制作设置」里添加）。")
        mode = body.get("mode") or ("replica" if job.get("replica") else "explain")
        if mode not in MODES or (mode == "replica" and not job.get("replica")):
            raise VoiceError(400, "选画面模式不对。")
        materials = body.get("materials")
        if materials is None:
            materials = self._default_materials(data, job, creator)
        if (not isinstance(materials, list) or len(materials) > 40 or
                not all(isinstance(name, str) and name.strip() and len(name) <= 200 for name in materials)):
            raise VoiceError(400, "素材时间线格式不对。")
        plan = {"request_id": body["request_id"], "creator": creator, "mode": mode,
                "materials": [name.strip() for name in materials], "voice": None}
        if not job.get("selected_take"):
            voice_id, model_id = body.get("voice_id"), body.get("model_id") or DEFAULT_MODEL
            if not isinstance(voice_id, str) or not VOICE_ID.fullmatch(voice_id):
                raise VoiceError(400, "先选一个音色。")
            plan["voice"] = {"voice_id": voice_id, "model_id": model_id, "settings": clean_settings(model_id, body.get("settings")),
                             "market": body.get("market") if isinstance(body.get("market"), str) else "us"}
        return job, plan

    @staticmethod
    def _default_materials(data, job, creator):
        """This order's own last pick for the creator, else what the creator's latest cut in the project used."""
        try:
            from away_mode import own_pick
            from material_pick import latest_materials
            own_creator, own = own_pick(job)
            if own and own_creator in ("", creator):
                return list(own)
            return list(latest_materials(data, job, creator))
        except Exception:
            return []

    def defaults(self, job_id, creator):
        """The material timelines 「开始制作」 would cut from for this creator, shown beside the button before it is pressed."""
        data = self.app.production.records()
        job = next((row for row in data.get("jobs") or [] if row.get("id") == job_id), None)
        if not job:
            raise VoiceError(404, "这张制作单不存在。")
        creators = list((data.get("settings") or {}).get("creators") or [])
        creator = creator if creator in creators else (creators[0] if creators else "")
        return {"creator": creator, "materials": self._default_materials(data, job, creator) if creator else []}

    def start(self, job_id, body):
        job, plan = self._plan(job_id, body)
        voicing = getattr(self.app, "_voicing", None)
        with self.lock:
            run = self.runs.get(job_id)
            if run and run["running"]:
                if run["request_id"] == plan["request_id"]:
                    return {"producing": {key: run[key] for key in ("stage", "running", "error", "note", "since", "request_id")}}
                raise VoiceError(409, f"这张单正在{LABELS.get(run['stage'], '制作')}，等它走完。")
            if voicing and job_id in voicing():
                raise VoiceError(409, "这张单正在配音，等它结束。")
            run = {"stage": "voice" if plan["voice"] else "timeline", "running": True, "error": "", "note": "",
                   "since": _now(), "request_id": plan["request_id"]}
            self.runs[job_id] = run
        thread = threading.Thread(target=self._run, args=(job_id, plan, run), daemon=True, name="produce-chain")
        if self.start_thread:
            thread.start()
        else:
            thread.run()
        return {"producing": self.status(job_id)}

    def _stage(self, run, stage):
        with self.lock:
            run["stage"] = stage

    def _stop(self, run, *, error="", note=""):
        with self.lock:
            run.update(running=False, error=error, note=note)

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

    def _run(self, job_id, plan, run):
        request_id = plan["request_id"]
        try:
            if plan["voice"]:
                voice = plan["voice"]
                before = {take.get("id") for take in self.app.production.job(job_id).get("takes") or []}
                body = {"request_id": f"{request_id}-take", "voice_id": voice["voice_id"], "model_id": voice["model_id"],
                        "settings": voice["settings"]}
                if voice["market"] != "us":
                    body["market"] = voice["market"]
                self.app.generate_take(job_id, body)
                fresh = [take for take in self.app.production.job(job_id).get("takes") or [] if take.get("id") not in before]
                take = max(fresh, key=lambda row: str(row.get("created_at") or ""), default=None)
                if not take:
                    return self._stop(run, error="配音完成了，但找不到新的那一版，刷新后看一眼。")
                if (take.get("quality") or {}).get("status") != "passed":
                    return self._stop(run, note="配好了一条，但意群对齐没过核验，没有自动选用。试听后手动选用，再接着建线和剪辑。")
                self._stage(run, "select")
                self._select(job_id, take["id"], request_id)
            self._stage(run, "timeline")
            job = self.app.production.job(job_id)
            from away_mode import built_timeline
            built = built_timeline(job, job.get("selected_take"), plan["creator"])
            if built:  # built for this voiceover already and never cut: cut on it, build nothing
                target = {"timeline": built["unique_id"]}
            else:
                result = self.app.build_timeline(job_id, {"request_id": f"{request_id}-build", "creator": plan["creator"]})
                target = ({"timeline_request_id": f"{request_id}-build"} if result.get("queued")
                          else {"timeline": (result.get("timeline") or {}).get("unique_id") or result.get("unique_id")})
            if not plan["materials"]:
                return self._stop(run, note=f"配音和时间线都好了。{plan['creator']} 还没有用过的素材时间线，去「审片」里勾选素材再开剪。")
            self._stage(run, "edit")
            started = self.app.start_edit(job_id, {"request_id": f"{request_id}-edit", **target, "mode": plan["mode"],
                                                   "instruction": "", "materials": plan["materials"]})
            if not (started.get("edit") or {}).get("id"):
                return self._stop(run, error="剪辑没有加进队列，去「审片」里手动开始。")
            with self.lock:  # the edit now reports for itself
                if self.runs.get(job_id) is run:
                    self.runs.pop(job_id)
        except Exception as exc:
            self._stop(run, error=f"{LABELS.get(run['stage'], '制作')}没走完：{_short(exc)}")
