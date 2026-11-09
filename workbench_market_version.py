"""「做英国版」 on a 制作单: one writing round adapts an American job's lines, the person checks them, then a new job.

The American job is never changed, since its voiceover, timelines and publications belong to its words. The adapted
script is saved as a script of its own and gets its own job, which keeps the original's picture requirements row by row.
"""
from __future__ import annotations

import re
import sys
from uuid import uuid4

from ai_tasks import AITaskError
from creation_store import CreationError
from elevenlabs_client import VoiceError
import markets
from variant_parse import parse_variants, publish_titles, variant_script

HAN = re.compile(r"[㐀-鿿]")
LIVE = ("queued", "running")


def instruction(job, original):
    """The request for the round. What may change is the writing skill's 「目标市场 · 美国稿改英国稿」."""
    count = len(job["lines"])
    lines = [
        "把下面这条美国制作单的口播改成英国稿，按写稿技能「目标市场」里的“美国稿改英国稿”执行。只出 1 条，按交付格式写全。",
        f"口播（配音用）代码块必须正好 {count} 行，和下面逐行对应，不增删行、不调顺序、不重写句子结构；不需要改的行逐字保留。"
        f"意群表也正好 {count} 行，画面要证明什么、素材标签、证据逐行沿用。",
        "", f"制作单：{job.get('title') or job['id']}", "美国稿口播：",
    ]
    lines += [f"{number}. {line['spoken']}" + (f"（{line['zh']}）" if line.get("zh") else "") for number, line in enumerate(job["lines"], 1)]
    have = [f"{number}. {line['proof']}" for number, line in enumerate(job["lines"], 1) if line.get("proof")]
    if have:
        lines += ["画面要求：", *have]
    if original:
        lines += ["", "美国稿完整内容（开头屏显、可换句备选和发布文案也要一起改）：", original]
    return "\n".join(lines)[:19000]


def review(job, task):
    """What the person checks: each American line beside its British one. A round without one line for each line fails."""
    if task["status"] != "completed":
        word = {"failed": "失败", "cancelled": "已取消", "interrupted": "被中断"}.get(task["status"], task["status"])
        return {"error": f"写稿任务没有完成（{word}），可以重新改一版。"}
    turn = next((item for item in reversed(task["rounds"]) if item["status"] == "completed" and (item.get("output") or "").strip()), None)
    output = turn["output"] if turn else ""
    count = len(job["lines"])
    chosen = next((item for item in parse_variants(output) if len(item["lines"]) == count), None) if output else None
    if not chosen:
        return {"error": f"结果里没有正好 {count} 行的口播，没有生成英国版；可以到写稿任务里查看，或重新改一版。"}
    if any(HAN.search(line["spoken"]) for line in chosen["lines"]):
        return {"error": "结果的口播里混有中文，没有生成英国版；可以重新改一版。"}
    rows = [{"number": mine["number"], "before": mine["spoken"], "spoken": theirs["spoken"], "zh": theirs.get("zh") or ""}
            for mine, theirs in zip(job["lines"], chosen["lines"])]
    return {"review": {"round_id": turn["round_id"], "variant_index": chosen["index"], "lines": rows,
                       "titles": publish_titles(output, chosen["index"]), "problems": chosen["problems"]}}


class MarketVersionMixin:
    def _uk_original(self, job):
        """The American script's full text, so its on-screen hook and publish captions are adapted too."""
        if job.get("fill_markdown"):
            return job["fill_markdown"]
        try:
            script = self.creation.get_script(job["script_id"])
        except CreationError:
            return ""
        return variant_script((script.get("source") or {}).get("candidate_text") or "", (job.get("source") or {}).get("variant_index"))

    def start_uk_version(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "英国版参数格式无效。")
        with self.lock:
            job = self.production.job(identifier)
            if job.get("archived"):
                raise VoiceError(409, "已归档的制作单不能做英国版。")
            if markets.stored(job.get("market")) != markets.DEFAULT or job.get("derived_from"):
                raise VoiceError(409, "只有美国稿的制作单可以做英国版。")
            version = job.get("uk_version") or {}
            if version.get("status") == "running":
                try:
                    if self.ai.get(version["task_id"])["status"] in LIVE:
                        return {"job": job}
                except AITaskError:
                    pass
            if version.get("status") == "created" and any(item["id"] == version.get("job_id") for item in self.production.public()["jobs"]):
                raise VoiceError(409, "这张制作单已经有英国版了。")
            wanted = job.get("direction_id") or ""
            known = any(entry["direction_id"] == wanted and entry.get("status") != "archived"
                        for entry in self.direction_catalog(job["product"])["directions"]) if wanted else False
            task = self.create_ai_task({"request_id": "uk-" + uuid4().hex, "action": "variants", "product": job["product"], "market": "uk",
                                        **({"direction_id": wanted} if known else {}), "instruction": instruction(job, self._uk_original(job))})
            saved = self.production.mutate("job_uk_start", {"request_id": "uk-" + uuid4().hex, "job_id": identifier, "task_id": task["task_id"]})
            return {"job": saved["job"]}

    def _uk_version_round_finished(self, task_id):
        """A finished round goes onto the job that asked for it; a later round in the same task replaces what is waiting."""
        try:
            waiting = [job for job in self.production.public()["jobs"]
                       if (job.get("uk_version") or {}).get("task_id") == task_id and job["uk_version"].get("status") in ("running", "review")]
            if not waiting:
                return
            task = self.ai.get(task_id)
            if task["status"] in LIVE:
                return
            for job in waiting:
                self.production.mutate("job_uk_result", {"request_id": "uk-" + uuid4().hex, "job_id": job["id"], "task_id": task_id, **review(job, task)})
        except Exception as exc:  # the job keeps its mark; the next read settles it
            print(f"制作单英国版未写回：{exc}", file=sys.stderr)

    def _settle_uk_version(self, identifier):
        """A round that ended while nobody was listening (e.g. across a restart) is written back on the next read."""
        try:
            version = self.production.job(identifier).get("uk_version") or {}
        except VoiceError:
            return
        if version.get("status") != "running":
            return
        try:
            status = self.ai.get(version["task_id"])["status"]
        except AITaskError:
            self.production.mutate("job_uk_result", {"request_id": "uk-" + uuid4().hex, "job_id": identifier, "task_id": version["task_id"],
                                                     "error": "找不到这轮写稿任务，可以重新改一版。"})
            return
        if status not in LIVE:
            self._uk_version_round_finished(version["task_id"])

    def confirm_uk_version(self, identifier, body):
        """Save what was checked as a new script and a new job. A repeat after a partial failure reuses what already exists."""
        if not isinstance(body, dict):
            raise VoiceError(400, "英国版参数格式无效。")
        with self.lock:
            job = self.production.job(identifier)
            version = job.get("uk_version") or {}
            if version.get("status") == "created":
                return {"job": self.production.job(version["job_id"])}
            if version.get("status") != "review":
                raise VoiceError(409, "英国版还没有可核对的结果。")
            checked = version["review"]
            task = self.ai.get(version["task_id"])
            latest = next((item for item in reversed(task["rounds"]) if item["status"] == "completed"), None)
            if task["status"] != "completed" or not latest or latest["round_id"] != checked["round_id"]:
                raise VoiceError(409, "写稿任务有了新结果，请重新打开核对。")
            direction = job.get("direction_id") or ""
            known = any(entry["direction_id"] == direction and entry.get("status") != "archived"
                        for entry in self.direction_catalog(job["product"])["directions"]) if direction else False
            script = self.save_script({"revision": self.creation.public()["revision"], "request_id": "uk-" + uuid4().hex,
                "script": {"product": job["product"], **({"direction_id": direction} if known else {}),
                           "title": f"{job.get('title') or job['id']} · 英国版"[:500],
                           "text": "\n".join(line["spoken"] for line in checked["lines"]),
                           "translation_zh": "\n".join(line["zh"] for line in checked["lines"]), "confirmed": True},
                "source": {"task_id": task["task_id"], "round_id": checked["round_id"], "task_revision": task["revision"],
                           "variant_index": checked["variant_index"]}})["script"]
            catalog = self.production.public()
            created = next((item for item in catalog["jobs"] if item["script_id"] == script["id"] and not item.get("archived")), None)
            if not created:
                created = self.create_production_job({"revision": catalog["revision"], "request_id": "uk-" + uuid4().hex,
                                                      "job": {"script_id": script["id"], "variant_index": checked["variant_index"]}})["job"]
            return self.production.mutate("job_uk_link", {"request_id": "uk-" + uuid4().hex, "job_id": identifier, "target_id": created["id"],
                                                          "task_id": task["task_id"]})
