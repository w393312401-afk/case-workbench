"""TikTok Shop platform campaigns (活动日历) that explain swings in sales, kept with the workbench's creation data.

The built-in 2026 US nodes are a starting point, not a source of truth: only some dates come from an official page,
so every record says how well its dates are backed. A person who checks Seller Center edits a node; an edited
node is never overwritten by a later built-in refresh. Dates are Pacific Time calendar days.
"""
from __future__ import annotations

import copy
from datetime import date
import json
from pathlib import Path
import re
import threading
from uuid import uuid4

from creation_store import CreationError
from direction_store import fingerprint
from elevenlabs_client import VoiceError
from production_operations import durable_json, file_lock

MAX_CAMPAIGNS = 200
# official: platform page; media: several outlets agree; disputed: sources disagree; planning: a third-party planning calendar; manual: entered by hand
CONFIDENCE = ("official", "media", "disputed", "planning", "manual")
CAMPAIGN_ID = re.compile(r"TKC-[A-Za-z0-9-]{3,60}\Z")

CALENDAR_SOURCE = "https://moras.ai/blog/tiktok-shop-sales-calendar"
SELLER_GUIDE = "https://seller-us.tiktok.com/university/essay?knowledge_id=26854147802894"
MODERN_RETAIL = "https://www.modernretail.co/retailers/tik-tok-shop-deals-for-you-days-2026/"
FALL_POST = "https://www.tiktok.com/discover/tiktok-shop-promos-for-october"


def _seed(identifier, name, start, end, confidence, label, url, note=""):
    return {"id": "TKC-2026-" + identifier, "name": name, "start": start, "end": end, "confidence": confidence,
            "source_label": label, "source_url": url, "note": note, "verified": False}


# Researched 2026-10-25. Everything not marked official needs a look at the live campaign card in Seller Center.
SEED = [
    _seed("jumpstart", "Jumpstart（新年开季促）", "2026-01-23", "2026-01-31", "planning", "第三方活动日历整理", CALENDAR_SOURCE),
    _seed("feb-restock", "February Restock（2 月补货季）", "2026-02-20", "2026-03-02", "planning", "第三方活动日历整理", CALENDAR_SOURCE),
    _seed("spring-sale", "Spring Sale（春季大促）", "2026-03-19", "2026-03-29", "planning", "第三方活动日历整理", CALENDAR_SOURCE),
    _seed("apr-restock", "April Restock（4 月补货季）", "2026-04-16", "2026-04-26", "planning", "第三方活动日历整理", CALENDAR_SOURCE),
    _seed("deals-for-you-days", "Deals For You Days（年中大促）", "2026-06-17", "2026-07-02", "media",
          "Modern Retail 等多家媒体", MODERN_RETAIL, "北京时间 6/18 上午 8 点开启；高客单品最常借这一档上折扣。"),
    _seed("summer-sale", "Summer Sale（夏季大促）", "2026-07-22", "2026-08-02", "planning", "第三方活动日历整理", CALENDAR_SOURCE),
    _seed("back-to-school", "Back to School（返校季）", "2026-08-13", "2026-08-23", "planning", "第三方代理商指南（约数）", CALENDAR_SOURCE,
          "来源写的是 roughly；往年同名活动是 7/31–8/7，别直接当成定论。"),
    _seed("sep-restock", "September Restock（9 月补货季）", "2026-09-17", "2026-09-27", "planning", "第三方活动日历整理", CALENDAR_SOURCE),
    _seed("fall-deals", "Fall Deals For You（秋促）", "2026-10-04", "2026-10-18", "disputed",
          "TikTok 创作者帖 + 媒体转述（北京时间换算）", FALL_POST,
          "说法不一：TikTok 上的创作者帖写 10/4–10/18；媒体转述的北京时间 10/5–10/19，换算成太平洋时间约 10/4 傍晚至 10/19 清晨，与前者吻合；"
          "只有创作者社区更新写 10/2–10/11（可能是另一个阶段）。默认取 10/4–10/18，请以 Seller Center 活动卡为准。"),
    _seed("bfcm", "Black Friday / Cyber Monday（黑五网一）", "2026-11-11", "2026-11-30", "official",
          "TikTok Shop 美区卖家大学 · 2026 BFCM 卖家指南", SELLER_GUIDE,
          "媒体写的北京时间 11/12–12/1 换算后就是 11/11–11/30 太平洋时间，与官方指南一致；只有创作者日历写到 12/1，暂无法印证。"
          "11/11 16:00 PT 起售出的商品延长退货到 2027-02-10；履约时效按下单日分档放宽。"
          "指南建议新品至少在活动前两周上架；报名与创作者样品的截止日期指南里没有写。"),
    _seed("year-end", "Year-end / Holiday（年末大促）", "2026-12-01", "2026-12-31", "planning", "第三方活动日历整理", CALENDAR_SOURCE,
          "去年的 Holiday Haul 是 2025-12-02 至 2026-01-01；今年年末的官方日期没有找到。"),
]


THIRD_PARTY = "只有第三方日历。确认 2026 年美区是否真的办过「{name}」、起止日各是哪天；没有办过就删除这条。"
# Per built-in node: the one thing worth checking. Shown in the checklist; never stored with a person's edits.
# Names shipped before the Chinese ones were added; a saved edit that still carries one follows the built-in name.
LEGACY_NAMES = {"TKC-2026-" + key: old for key, old, _ in (
    ("jumpstart", "Jumpstart", "Jumpstart（新年开季促）"),
    ("feb-restock", "February Restock", "February Restock（2 月补货季）"),
    ("spring-sale", "Spring Sale", "Spring Sale（春季大促）"),
    ("apr-restock", "April Restock", "April Restock（4 月补货季）"),
    ("summer-sale", "Summer Sale", "Summer Sale（夏季大促）"),
    ("back-to-school", "Back to School", "Back to School（返校季）"),
    ("sep-restock", "September Restock", "September Restock（9 月补货季）"),
)}
CHECKS = {
    "TKC-2026-jumpstart": THIRD_PARTY.format(name="Jumpstart"),
    "TKC-2026-feb-restock": THIRD_PARTY.format(name="February Restock"),
    "TKC-2026-spring-sale": THIRD_PARTY.format(name="Spring Sale"),
    "TKC-2026-apr-restock": THIRD_PARTY.format(name="April Restock"),
    "TKC-2026-deals-for-you-days": "媒体一致、尚无官方原文。扫一眼活动卡确认 6/17–7/2（北京时间 6/18 上午 8 点开）即可。",
    "TKC-2026-summer-sale": THIRD_PARTY.format(name="Summer Sale"),
    "TKC-2026-back-to-school": THIRD_PARTY.format(name="Back to School") + "来源只写了「约」8/13–8/23，往年同名活动是 7/31–8/7，起止日要重点看。",
    "TKC-2026-sep-restock": THIRD_PARTY.format(name="September Restock"),
    "TKC-2026-fall-deals": "三种说法，对活动卡选一种：10/4–10/18（创作者帖，默认）；北京时间 10/5–10/19（换算后基本同上）；10/2–10/11（创作者社区更新）。"
                           "你最近的 202H 数据就落在这段里，这条优先。",
    "TKC-2026-bfcm": "官方指南写 11/11–11/30，已核实；只需确认结束日是 11/30 还是创作者日历写的 12/1。",
    "TKC-2026-year-end": "今年年末有没有 Holiday Haul 一类的年终大促、起止日是哪天（去年是 12/2–次年 1/1）。官方没公布就先不动。",
}


def campaign_id(value):
    if not isinstance(value, str) or not CAMPAIGN_ID.fullmatch(value):
        raise CreationError(400, "活动编号无效。")
    return value


def iso_day(value, label):
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except ValueError as exc:
        raise CreationError(400, f"{label}须为 YYYY-MM-DD。") from exc
    return value


def validate(value):
    keys = {"id", "name", "start", "end", "confidence", "source_label", "source_url", "note", "verified"}
    if not isinstance(value, dict) or set(value) - keys or not {"name", "start", "end"} <= set(value):
        raise CreationError(400, "活动格式无效。")
    name = value["name"]
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80 or any(ord(char) < 32 for char in name):
        raise CreationError(400, "活动名称须为 1–80 个字符。")
    start, end = iso_day(value["start"], "开始日期"), iso_day(value["end"], "结束日期")
    if end < start or (date.fromisoformat(end) - date.fromisoformat(start)).days > 120:
        raise CreationError(400, "结束日期不能早于开始日期，且活动不超过 120 天。")
    confidence = value.get("confidence", "manual")
    if confidence not in CONFIDENCE:
        raise CreationError(400, "日期可信度无效。")
    url = value.get("source_url", "")
    if not isinstance(url, str) or len(url) > 500 or (url and not re.fullmatch(r"https?://[^\s]+", url)):
        raise CreationError(400, "来源链接须是 http(s) 地址。")
    label, note = value.get("source_label", ""), value.get("note", "")
    if not isinstance(label, str) or len(label) > 120 or not isinstance(note, str) or len(note) > 1000:
        raise CreationError(400, "来源说明不超过 120 字，备注不超过 1000 字。")
    verified = value.get("verified", False)
    if not isinstance(verified, bool):
        raise CreationError(400, "核对标记无效。")
    result = {"name": name.strip(), "start": start, "end": end, "confidence": confidence,
              "source_label": label.strip(), "source_url": url, "note": note.strip(), "verified": verified}
    if "id" in value:
        result["id"] = campaign_id(value["id"])
    return result


class CampaignStore:
    def __init__(self, runtime_root, lock=None):
        self.root = Path(runtime_root) / "creation"
        self.path = self.root / "campaigns.json"
        self.lock = lock or threading.RLock()

    @staticmethod
    def empty():
        return {"schema_version": 1, "records": {}, "removed": []}

    def read(self):
        if not self.path.exists():
            return self.empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if (not isinstance(data, dict) or set(data) != {"schema_version", "records", "removed"} or data["schema_version"] != 1
                    or not isinstance(data["records"], dict) or not isinstance(data["removed"], list)
                    or len(data["records"]) > MAX_CAMPAIGNS):
                raise ValueError()
            for identifier, record in data["records"].items():
                if validate({**record, "id": identifier}) != {**record, "id": identifier}:
                    raise ValueError()
            for identifier in data["removed"]:
                campaign_id(identifier)
            return data
        except (OSError, ValueError, TypeError, RecursionError, CreationError) as exc:
            raise CreationError(500, "保存的活动日历无法读取，请保留原文件并核对备份。") from exc

    @staticmethod
    def merged(data):
        """Built-in nodes (unless removed) with the person's edits over them, then their own entries, by start date."""
        rows = {}
        for row in SEED:
            if row["id"] not in data["removed"]:
                rows[row["id"]] = {**copy.deepcopy(row), "builtin": True, "edited": False}
        names = {row["id"]: row["name"] for row in SEED}
        for identifier, record in data["records"].items():
            builtin = identifier in names
            rows[identifier] = {**copy.deepcopy(record), "id": identifier, "builtin": builtin, "edited": builtin}
            if builtin and record["name"] == LEGACY_NAMES.get(identifier):
                rows[identifier]["name"] = names[identifier]
        for identifier, row in rows.items():
            row["check"] = CHECKS.get(identifier, "")
        return sorted(rows.values(), key=lambda row: (row["start"], row["end"], row["id"]))

    def public(self):
        with self.lock:
            data = self.read()
            return {"campaigns": self.merged(data), "revision": fingerprint(data), "timezone": "America/Los_Angeles"}

    def overlapping(self, start, end):
        """Campaigns whose days touch the closed range [start, end]; empty when the range is unknown."""
        if not start or not end:
            return []
        with self.lock:
            return [row for row in self.merged(self.read()) if row["start"] <= end and row["end"] >= start]

    def mutate(self, body):
        if not isinstance(body, dict) or body.get("action") not in {"save", "delete", "restore"} or not isinstance(body.get("revision"), str):
            raise CreationError(400, "活动保存参数无效。")
        action = body["action"]
        if action == "save":
            if set(body) != {"action", "campaign", "revision"}:
                raise CreationError(400, "活动保存参数无效。")
            record = validate(body["campaign"])
        else:
            if set(body) != {"action", "id", "revision"}:
                raise CreationError(400, "活动保存参数无效。")
            identifier = campaign_id(body["id"])
        with self.lock:
            try:
                with file_lock(self.root / "campaigns.lock", "活动日历正在保存，请稍后重试。"):
                    data = self.read()
                    if body["revision"] != fingerprint(data):
                        raise CreationError(409, "活动日历已在其他浏览器或设备更新，请刷新后重试。")
                    builtin = {row["id"] for row in SEED}
                    if action == "save":
                        identifier = record.pop("id", None) or "TKC-" + uuid4().hex[:12]
                        if identifier not in builtin and identifier not in data["records"] and len(data["records"]) >= MAX_CAMPAIGNS:
                            raise CreationError(400, "活动太多了，请先删除不再需要的。")
                        data["records"][identifier] = record
                        if identifier in data["removed"]:
                            data["removed"].remove(identifier)
                    elif action == "delete":
                        if identifier in builtin:
                            data["records"].pop(identifier, None)
                            if identifier not in data["removed"]:
                                data["removed"].append(identifier)
                        elif data["records"].pop(identifier, None) is None:
                            raise CreationError(404, "这条活动不存在，请刷新后重试。")
                    else:
                        if identifier not in builtin:
                            raise CreationError(400, "只有内置活动可以恢复默认。")
                        data["records"].pop(identifier, None)
                        if identifier in data["removed"]:
                            data["removed"].remove(identifier)
                    data["removed"] = sorted(set(data["removed"]))
                    durable_json(self.path, data)
                    return {"campaigns": self.merged(data), "revision": fingerprint(data), "timezone": "America/Los_Angeles"}
            except VoiceError as exc:
                raise CreationError(exc.status, str(exc)) from exc
