"""制作单补全后写回采用稿：AI 补全或手动补全制作单的脚本信息后，把同一版采用稿补齐。

口播不变，所以写回的是同一版（不另存新版）：采用稿没有逐行中文时补上对应中文；制作单的原稿全文（开头画面、
屏显、对应中文、补拍清单、发布文案）加上制作单当前的意群表，存成这一版的「补全稿」，采用稿详情页的
「意群表」等分页读它。制作单的口播和采用稿对不上（比如照原片做的单）时不写回。
"""
from __future__ import annotations

import copy
import re

from direction_store import now
from production_store import supplement_markdown
from variant_parse import same_lines

TABLE_HEAD = "| # | 口播 | 画面要证明什么 | 素材标签 | 证据 |\n|---|---|---|---|---|"
CHINESE = re.compile(r"^\*\*对应中文\*\*")


def _cell(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().replace("|", "\\|")


def job_markdown(job):
    """The order's full script with its own 意群表; "" when the order has nothing beyond the spoken lines."""
    lines = job.get("lines") or []
    table = ""
    if any(line.get("proof") or line.get("tags") or line.get("evidence") for line in lines):
        rows = [f"| {line.get('number') or index} | {_cell(line.get('spoken'))} | {_cell(line.get('proof'))} | {_cell(line.get('tags'))} | {_cell(line.get('evidence'))} |"
                for index, line in enumerate(lines, 1)]
        table = "**意群表**\n\n" + TABLE_HEAD + "\n" + "\n".join(rows)
    base = str(job.get("fill_markdown") or "").strip()
    if not base and (job.get("supplement") or table):
        base = supplement_markdown(job)
    if not base:
        return ""
    if not table:
        return base
    # The table goes right after the 对应中文 block, where the writing skill puts it.
    parts = base.split("\n")
    at = next((index for index, line in enumerate(parts) if CHINESE.match(line.strip())), None)
    if at is not None:
        fences = [index for index in range(at + 1, len(parts)) if parts[index].strip().startswith("```")]
        if len(fences) >= 2:
            end = fences[1] + 1
            return "\n".join(parts[:end] + ["", table, ""] + parts[end:]).strip()
    return base + "\n\n" + table


class JobScriptSyncMixin:
    """Needs self.production and self.creation."""

    def _sync_job_to_script(self, job_id):
        """Write a filled order's script information back onto the adopted version it was made from."""
        try:
            job = self.production.job(job_id)
        except Exception:
            return None
        script_id = job.get("script_id")
        if not script_id or job.get("replica"):
            return None
        markdown = job_markdown(job)
        chinese = [str(line.get("zh") or "").strip() for line in job.get("lines") or []]
        with self.creation.lock:
            data = self.creation.read()
            item = next((row for row in data["scripts"] if row["id"] == script_id), None)
            if not item:
                return None
            spoken = [line.strip() for line in str(item.get("text") or "").splitlines() if line.strip()]
            if not same_lines(spoken, [line["spoken"] for line in job.get("lines") or []]):
                return None
            changed = []
            own = [line.strip() for line in str(item.get("translation_zh") or "").splitlines() if line.strip()]
            if len(own) != len(spoken) and chinese and all(chinese) and len(chinese) == len(spoken):
                item["translation_zh"] = "\n".join(chinese)
                changed.append("对应中文")
            filled = item.get("filled") or {}
            if markdown and (filled.get("markdown") != markdown or filled.get("job_id") != job["id"]):
                item["filled"] = {"markdown": markdown, "job_id": job["id"], "at": now()}
                changed.append("脚本信息")
            if not changed:
                return None
            item["updated_at"] = now()
            item.setdefault("history", []).append({"status": item.get("status", "adopted"), "at": item["updated_at"], "event": "job_fill",
                                                   "note": f"制作单 {job['id']} 补全后写回{'和'.join(changed)}；口播没变，仍是这一版。",
                                                   "job_id": job["id"]})
            self.creation.write(data)
            self.state_cache = None
            return copy.deepcopy(item)

    def _sync_job_to_script_quietly(self, job_id):
        try:
            return self._sync_job_to_script(job_id)
        except Exception as exc:  # the order is saved either way; the next fill or save writes it back again
            import sys
            print(f"制作单补全没能写回采用稿：{exc}", file=sys.stderr)
            return None
